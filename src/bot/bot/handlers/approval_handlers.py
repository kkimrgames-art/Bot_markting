import logging
import os
import asyncio
import time
import random
import uuid
import html
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..channel_manager import ChannelManager, resolve_youtube_token_path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...agent.config import load_config
from ..persistence import load_state, save_state, update_state
from ...agent.core import publish_pending_video

logger = logging.getLogger(__name__)


async def _safe_answer_callback(query, **kwargs):
    try:
        if query:
            await query.answer(**kwargs)
    except BadRequest as e:
        msg = str(e)
        if "Query is too old" in msg or "response timeout expired" in msg or "query id is invalid" in msg:
            return
        raise
    except Exception:
        return


def _tokens_dir_from_cfg(cfg) -> str:
    base = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"
    path = os.path.join(base, "youtube_tokens")
    os.makedirs(path, exist_ok=True)
    return path


def _resolve_publish_token_path(cfg, youtube_channel_id: str, publish_channel: Optional[Dict[str, Any]] = None) -> str:
    channel_id = str(youtube_channel_id or "").strip()
    if not channel_id:
        return ""

    try:
        explicit = str((publish_channel or {}).get("token_path") or "").strip()
    except Exception:
        explicit = ""
    if explicit and os.path.exists(explicit):
        return explicit

    resolved = resolve_youtube_token_path(channel_id, cfg)
    if resolved and os.path.exists(resolved):
        return resolved

    token_guess = os.path.join(_tokens_dir_from_cfg(cfg), f"{channel_id}.json")
    if os.path.exists(token_guess):
        return token_guess
    return resolved or ""


async def publish_auth_waiting_for_channel(context: ContextTypes.DEFAULT_TYPE, youtube_channel_id: str) -> None:
    cfg = load_config()
    state = load_state(cfg)

    waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    if not waiting:
        return

    targets = [w for w in waiting if (w.get("reason") == "auth_required") and (w.get("publish_channel_id") == youtube_channel_id)]
    if not targets:
        return

    # حاول أخذ مسار التوكن من بيانات القناة المحفوظة داخل الانتظار (أدق)، وإلا استخدم المسار الافتراضي.
    token_path = None
    try:
        for w in targets:
            token_path = _resolve_publish_token_path(cfg, youtube_channel_id, w.get("publish_channel") or {})
            if token_path and os.path.exists(token_path):
                break
    except Exception:
        token_path = None
    if not token_path:
        return

    from ...agent.uploader import upload_video_with_token, AuthenticationRequiredError
    from ...agent.core import _queue_telegram_notification
    from ...agent.core import compute_publish_at_strict

    published = 0
    failed = 0
    remaining = []
    for w in targets:
        outp = w.get("output_path")
        if not outp or not os.path.exists(outp):
            failed += 1
            continue
        title = w.get("title") or w.get("title_hint") or "Short"
        desc = w.get("description") or ""
        tags = w.get("hashtags") or []
        privacy = (w.get("privacy") or "unlisted").strip().lower()
        if privacy not in {"public", "unlisted", "private"}:
            privacy = "unlisted"

        platform = ((w.get("publish_channel") or {}).get("platform") or "youtube").strip().lower()
        if platform in {"facebook", "instagram"}:
            w["reason"] = "platform_requires_public_url"
            w["last_error"] = "platform_requires_public_url"
            remaining.append(w)
            continue

        try:
            publish_at = (w.get("publish_at") or "").strip() or None
            if not publish_at:
                try:
                    publish_at = compute_publish_at_strict(cfg, (w.get("publish_channel") or {}), str(w.get("video_id") or ""), "shorts")
                    w["publish_at"] = publish_at
                    save_state(state, cfg)
                except Exception as e:
                    # Strict mode: do not publish immediately
                    w["reason"] = "retryable_error"
                    w["stage"] = "schedule"
                    w["next_retry_at"] = time.time() + 3600
                    w["last_error"] = (f"schedule_failed: {e}" if e else "schedule_failed")[:500]
                    save_state(state, cfg)
                    remaining.append(w)
                    continue

            vid_id = await asyncio.to_thread(upload_video_with_token, cfg, token_path, outp, title, desc, tags, privacy, publish_at)
            published += 1
            _queue_telegram_notification(state, "success", {
                "url": f"https://youtu.be/{vid_id}",
                "title": title,
                "description": desc,
                "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or youtube_channel_id,
            })
        except AuthenticationRequiredError:
            remaining.append(w)
            break
        except Exception:
            failed += 1
            remaining.append(w)

    old_waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    keep_ids = {w.get("video_id") for w in remaining}
    state.setdefault("scheduler", {})["waiting_videos"] = [
        v for v in old_waiting
        if not ((v.get("reason") == "auth_required") and (v.get("publish_channel_id") == youtube_channel_id) and (v.get("video_id") not in keep_ids))
    ]
    save_state(state, cfg)

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    if admin_ids:
        admin_id = admin_ids[0]
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📤 <b>تمت محاولة نشر الفيديوهات المنتظرة بعد المصادقة</b>\n\n"
                    f"📺 القناة: <code>{html.escape(youtube_channel_id)}</code>\n"
                    f"✅ تم نشر: {published}\n"
                    f"❌ فشل: {failed}\n"
                    f"⏳ مازال في الانتظار: {len(remaining)}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def process_auth_required_waiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    auth_waiting = [w for w in waiting if w.get("reason") == "auth_required"]
    if not auth_waiting:
        return

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    if not admin_ids:
        return
    admin_id = admin_ids[0]

    by_channel: Dict[str, List[Dict[str, Any]]] = {}
    for w in auth_waiting:
        ch = w.get("publish_channel_id")
        if not ch:
            continue
        by_channel.setdefault(ch, []).append(w)

    for ch_id, items in by_channel.items():
        token_path = os.path.join(_tokens_dir_from_cfg(cfg), f"{ch_id}.json")
        if os.path.exists(token_path):
            await publish_auth_waiting_for_channel(context, ch_id)
            continue

        pending = context.application.bot_data.setdefault("pending_auths", {})
        if any((v or {}).get("channel_id") == ch_id for v in pending.values()):
            continue

        last_prompt = ((state.get("scheduler", {}) or {}).get("auth_prompts", {}) or {}).get(ch_id)
        if last_prompt and (time.time() - float(last_prompt)) < 120:
            continue

        from ...agent.uploader import _find_client_secrets_file
        from ...bot.auth_flow_utils import start_auth_flow

        client_secrets = _find_client_secrets_file(cfg)
        if not client_secrets:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"❌ ملف client_secret.json غير موجود. لا يمكن بدء المصادقة للقناة: {ch_id}")
            except Exception:
                pass
            continue

        auth_url, server, flow = start_auth_flow(client_secrets)
        auth_id = f"auth_wait_{ch_id}_{uuid.uuid4().hex[:6]}"
        pending[auth_id] = {
            "server": server,
            "flow": flow,
            "token_path": token_path,
            "channel_id": ch_id,
        }

        state.setdefault("scheduler", {}).setdefault("auth_prompts", {})[ch_id] = time.time()
        save_state(state, cfg)

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 اضغط للمصادقة", url=auth_url)]])
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ <b>مطلوب إعادة مصادقة</b>\n\n"
                    f"📺 القناة: <code>{html.escape(ch_id)}</code>\n"
                    f"🎬 فيديوهات بالانتظار: {len(items)}\n\n"
                    "اضغط الزر أدناه (على الكمبيوتر) لإكمال المصادقة، وبعدها سيتم نشر الفيديوهات تلقائياً."
                ),
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            from . import mod_handlers
            task = context.application.create_task(mod_handlers.wait_for_auth_code(context, auth_id, server, flow, token_path, admin_id, expected_channel_id=ch_id))
            context.application.bot_data.setdefault("auth_tasks", set()).add(task)
            task.add_done_callback(lambda t: context.application.bot_data.get("auth_tasks", set()).discard(t))
        except Exception as e:
            logger.error(f"Failed to start wait_for_auth_code task: {e}")


async def publish_quota_waiting_for_channel(context: ContextTypes.DEFAULT_TYPE, youtube_channel_id: str) -> None:
    cfg = load_config()
    state = load_state(cfg)

    now = time.time()
    waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    targets = [
        w for w in waiting
        if (w.get("reason") == "quota_exceeded")
        and (w.get("publish_channel_id") == youtube_channel_id)
        and (float(w.get("next_retry_at") or 0) <= now)
    ]
    if not targets:
        return

    token_path = os.path.join(_tokens_dir_from_cfg(cfg), f"{youtube_channel_id}.json")
    if not os.path.exists(token_path):
        return

    from ...agent.uploader import upload_video_with_token, AuthenticationRequiredError, is_youtube_quota_error
    from ...agent.core import _queue_telegram_notification
    from ...agent.core import compute_publish_at_strict

    retry_after_s = 3600
    try:
        retry_after_s = int(os.getenv("YOUTUBE_QUOTA_RETRY_SECONDS", "3600") or 3600)
    except Exception:
        retry_after_s = 3600

    published = 0
    failed = 0
    keep_ids = set()
    for w in targets:
        outp = w.get("output_path")
        if not outp or not os.path.exists(outp):
            failed += 1
            continue
        title = w.get("title") or w.get("title_hint") or "Short"
        desc = w.get("description") or ""
        tags = w.get("hashtags") or []
        privacy = (w.get("privacy") or "unlisted").strip().lower()
        if privacy not in {"public", "unlisted", "private"}:
            privacy = "unlisted"

        try:
            publish_at = (w.get("publish_at") or "").strip() or None
            if not publish_at:
                try:
                    publish_at = compute_publish_at_strict(cfg, (w.get("publish_channel") or {}), str(w.get("video_id") or ""), "shorts")
                    w["publish_at"] = publish_at
                    save_state(state, cfg)
                except Exception as e:
                    # Strict mode: never publish immediately
                    w["reason"] = "retryable_error"
                    w["stage"] = "schedule"
                    w["next_retry_at"] = time.time() + 3600
                    w["last_error"] = (f"schedule_failed: {e}" if e else "schedule_failed")[:500]
                    keep_ids.add(w.get("video_id"))
                    save_state(state, cfg)
                    continue

            vid_id = await asyncio.to_thread(upload_video_with_token, cfg, token_path, outp, title, desc, tags, privacy, publish_at)
            published += 1
            _queue_telegram_notification(state, "success", {
                "url": f"https://youtu.be/{vid_id}",
                "title": title,
                "description": desc,
                "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or youtube_channel_id,
            })
        except AuthenticationRequiredError:
            # تحولت المشكلة إلى مصادقة
            w["reason"] = "auth_required"
            w.pop("next_retry_at", None)
            keep_ids.add(w.get("video_id"))
            _queue_telegram_notification(state, "auth_required", {
                "channel": (w.get("publish_channel") or {}).get("title") or youtube_channel_id,
                "title": title,
            })
        except Exception as e:
            if is_youtube_quota_error(e):
                w["next_retry_at"] = time.time() + max(60, retry_after_s)
                w["last_error"] = str(e)[:500]
                keep_ids.add(w.get("video_id"))
            else:
                failed += 1

    # تحديث قائمة الانتظار
    new_waiting = []
    for w in (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []:
        if w.get("publish_channel_id") != youtube_channel_id:
            new_waiting.append(w)
            continue
        if w.get("reason") == "quota_exceeded" and float(w.get("next_retry_at") or 0) <= now:
            # هذا عنصر تمت معالجته، نُبقيه فقط إذا أضفناه للـ keep_ids
            if w.get("video_id") in keep_ids:
                new_waiting.append(w)
            continue
        new_waiting.append(w)

    state.setdefault("scheduler", {})["waiting_videos"] = new_waiting
    save_state(state, cfg)

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    if admin_ids:
        admin_id = admin_ids[0]
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📤 <b>إعادة محاولة نشر بعد نفاد الحصة</b>\n\n"
                    f"📺 القناة: <code>{html.escape(youtube_channel_id)}</code>\n"
                    f"✅ تم نشر: {published}\n"
                    f"❌ فشل: {failed}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def process_quota_exceeded_waiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    quota_waiting = [w for w in waiting if w.get("reason") == "quota_exceeded"]
    if not quota_waiting:
        return

    by_channel: Dict[str, List[Dict[str, Any]]] = {}
    for w in quota_waiting:
        ch = w.get("publish_channel_id")
        if not ch:
            continue
        by_channel.setdefault(ch, []).append(w)

    for ch_id in by_channel.keys():
        await publish_quota_waiting_for_channel(context, ch_id)


async def cleanup_temp_garbage(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    # Directories to clean
    dirs_to_clean = [
        getattr(cfg, "TEMP_DIR", None) or ".temp",
        ".output",
        ".backup"
    ]

    max_age_h = 12
    try:
        max_age_h = int(os.getenv("TEMP_GARBAGE_MAX_AGE_HOURS", "12") or 12)
    except Exception:
        max_age_h = 12
    if max_age_h <= 0:
        return

    max_age_s = max_age_h * 3600
    now = time.time()

    protected = set()
    try:
        waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
        for w in waiting:
            ip = (w.get("input_path") or "").strip()
            if ip:
                try:
                    protected.add(os.path.abspath(ip))
                except Exception:
                    pass
            op = (w.get("output_path") or "").strip()
            if op:
                try:
                    protected.add(os.path.abspath(op))
                except Exception:
                    pass
    except Exception:
        protected = set()

    for d in dirs_to_clean:
        if not d or not os.path.isdir(d):
            continue
            
        try:
            for root, _dirs, files in os.walk(d):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        if os.path.abspath(fp) in protected:
                            continue
                    except Exception:
                        pass
                    try:
                        age = now - os.path.getmtime(fp)
                        if age >= max_age_s:
                            os.remove(fp)
                    except Exception:
                        continue
        except Exception:
            continue


async def process_retryable_waiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    now = time.time()
    waiting = (state.get("scheduler", {}) or {}).get("waiting_videos", []) or []
    targets = [w for w in waiting if (w.get("reason") == "retryable_error") and (float(w.get("next_retry_at") or 0) <= now)]
    if not targets:
        return

    # حاول عنصر واحد لكل تشغيل لتجنب الضغط الكبير
    w = targets[0]
    is_mod = bool(w.get("is_mod"))

    if is_mod:
        outp = w.get("output_path")
        if not outp or not os.path.exists(outp):
            return

        try:
            from ...agent.resource_guard import should_defer_heavy_work
            defer, reason, retry_after_s, _ = should_defer_heavy_work("upload")
            if defer:
                try:
                    w["attempts"] = int(w.get("attempts") or 0) + 1
                except Exception:
                    w["attempts"] = 1
                w["next_retry_at"] = time.time() + max(30, int(retry_after_s or 120))
                w["last_error"] = reason
                save_state(state, cfg)
                return
        except Exception:
            pass

        from ...agent.uploader import upload_video_with_token, upload_video, AuthenticationRequiredError, is_youtube_quota_error, is_retryable_error
        from ...agent.core import _queue_telegram_notification

        youtube_channel_id = w.get("publish_channel_id")
        token_guess = os.path.join(_tokens_dir_from_cfg(cfg), f"{youtube_channel_id}.json") if youtube_channel_id else None
        title = w.get("title") or w.get("title_hint") or "Short"
        desc = w.get("description") or ""
        tags = w.get("hashtags") or []
        privacy = (w.get("privacy") or "unlisted").strip().lower()
        if privacy not in {"public", "unlisted", "private"}:
            privacy = "unlisted"

        # Strict scheduling: ensure publish_at exists
        from ...agent.core import compute_publish_at_strict
        publish_at = (w.get("publish_at") or "").strip() or None
        if not publish_at:
            try:
                publish_at = compute_publish_at_strict(cfg, (w.get("publish_channel") or {}), str(w.get("video_id") or ""), "shorts")
                w["publish_at"] = publish_at
                save_state(state, cfg)
            except Exception as e:
                w["attempts"] = int(w.get("attempts") or 0) + 1
                w["next_retry_at"] = time.time() + 3600
                w["last_error"] = (f"schedule_failed: {e}" if e else "schedule_failed")[:500]
                save_state(state, cfg)
                return

        try:
            if token_guess and os.path.exists(token_guess):
                vid_id = await asyncio.to_thread(upload_video_with_token, cfg, token_guess, outp, title, desc, tags, privacy, publish_at)
            else:
                vid_id = await asyncio.to_thread(upload_video, cfg, outp, title, desc, tags, privacy, publish_at)
        except AuthenticationRequiredError:
            try:
                w["reason"] = "auth_required"
                w.pop("next_retry_at", None)
                _queue_telegram_notification(state, "auth_required", {
                    "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or (youtube_channel_id or "Unknown"),
                    "title": title,
                })
                save_state(state, cfg)
            except Exception:
                pass
            return
        except Exception as e:
            if is_youtube_quota_error(e):
                try:
                    retry_after_s = 3600
                    try:
                        retry_after_s = int(os.getenv("YOUTUBE_QUOTA_RETRY_SECONDS", "3600") or 3600)
                    except Exception:
                        retry_after_s = 3600
                    w["reason"] = "quota_exceeded"
                    w["next_retry_at"] = time.time() + max(60, retry_after_s)
                    w["last_error"] = str(e)[:500]
                    _queue_telegram_notification(state, "quota_exceeded", {
                        "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or (youtube_channel_id or "Unknown"),
                        "title": title,
                        "retry_after_s": retry_after_s,
                    })
                    save_state(state, cfg)
                except Exception:
                    pass
                return
            if is_retryable_error(e):
                try:
                    attempts = int(w.get("attempts") or 0) + 1
                    w["attempts"] = attempts
                    base_delay = int(os.getenv("RETRY_BASE_SECONDS", "60") or 60)
                    max_delay = int(os.getenv("RETRY_MAX_SECONDS", "21600") or 21600)
                    delay = min(max_delay, max(30, base_delay * (2 ** max(0, attempts - 1))))
                    try:
                        jitter_cap = int(os.getenv("RETRY_JITTER_MAX_SECONDS", "15") or 15)
                    except Exception:
                        jitter_cap = 15
                    if jitter_cap > 0:
                        jitter = random.randint(0, min(jitter_cap, int(delay)))
                        delay = delay + jitter
                    w["next_retry_at"] = time.time() + delay
                    w["last_error"] = str(e)[:500]
                    _queue_telegram_notification(state, "retryable_error", {
                        "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or (youtube_channel_id or "Unknown"),
                        "stage": "upload",
                        "error": str(e)[:200],
                        "retry_after_s": int(delay),
                    })
                    save_state(state, cfg)
                except Exception:
                    pass
                return
            return

        # success
        try:
            _queue_telegram_notification(state, "success", {
                "url": f"https://youtu.be/{vid_id}",
                "title": title,
                "description": desc,
                "channel": (w.get("publish_channel") or {}).get("channel_name") or (w.get("publish_channel") or {}).get("title") or (youtube_channel_id or "Unknown"),
            })
            save_state(state, cfg)
        except Exception:
            pass

        vid_key = w.get("video_id")
        try:
            fresh = load_state(cfg)
            old = (fresh.get("scheduler", {}) or {}).get("waiting_videos", []) or []
            fresh.setdefault("scheduler", {})["waiting_videos"] = [
                it for it in old
                if not (it.get("video_id") == vid_key and it.get("reason") == "retryable_error")
            ]
            save_state(fresh, cfg)
        except Exception:
            pass
        return

    from ...agent.core import run_once_for_channel

    pub_ch = w.get("publish_channel") or {}
    ch_url = w.get("channel_url")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_once_for_channel, cfg, ch_url, pub_ch, False, w)
    except Exception as e:
        # إذا حدث استثناء غير متوقع، فقط أجّل المحاولة
        try:
            attempts = int(w.get("attempts") or 0) + 1
            w["attempts"] = attempts
            base_delay = 60
            max_delay = 21600
            try:
                base_delay = int(os.getenv("RETRY_BASE_SECONDS", "60") or 60)
                max_delay = int(os.getenv("RETRY_MAX_SECONDS", "21600") or 21600)
            except Exception:
                pass
            delay = min(max_delay, max(30, base_delay * (2 ** max(0, attempts - 1))))
            try:
                jitter_cap = int(os.getenv("RETRY_JITTER_MAX_SECONDS", "15") or 15)
            except Exception:
                jitter_cap = 15
            if jitter_cap > 0:
                jitter = random.randint(0, min(jitter_cap, int(delay)))
                delay = delay + jitter
            w["next_retry_at"] = time.time() + delay
            w["last_error"] = str(e)[:500]
            save_state(state, cfg)
        except Exception:
            pass
        return

    publish_result = (result or {}).get("publish_result") or {}
    if publish_result.get("success") and publish_result.get("video_id"):
        vid_id = w.get("video_id")
        try:
            fresh = load_state(cfg)
            old = (fresh.get("scheduler", {}) or {}).get("waiting_videos", []) or []
            fresh.setdefault("scheduler", {})["waiting_videos"] = [
                it for it in old
                if not (it.get("video_id") == vid_id and it.get("reason") == "retryable_error")
            ]
            save_state(fresh, cfg)
        except Exception:
            pass




def _find_pending(state: Dict[str, Any], pending_id: str) -> Optional[Dict[str, Any]]:
    for item in state.get("pending_videos", []):
        if item.get("id") == pending_id:
            return item
    return None


async def send_pending_previews(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    # Prevent overlapping runs (sending videos can be slow and block the job queue)
    running_key = "send_pending_previews_running"
    if context.application.bot_data.get(running_key):
        return
    context.application.bot_data[running_key] = True

    try:
        # If publishing is active, previews may be delayed unless explicitly allowed
        try:
            allow_during_pub = str(os.getenv("ALLOW_PREVIEWS_DURING_PUBLISHING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            allow_during_pub = True
        if not allow_during_pub:
            try:
                lock = (state.get("publishing_lock") or {})
                if isinstance(lock, dict) and lock.get("active"):
                    until = lock.get("until")
                    if not until or float(until) > time.time():
                        return
            except Exception:
                pass

        pending_list: List[Dict[str, Any]] = state.get("pending_videos", [])
        if not pending_list:
            return

        chat_id = (state.get("agent", {}) or {}).get("chat_id")
        if not chat_id:
            if cfg.TELEGRAM_ALLOWED_USER_IDS:
                chat_id = cfg.TELEGRAM_ALLOWED_USER_IDS[0]
            else:
                return

        for item in pending_list:
            if item.get("status") != "waiting":
                continue
            if item.get("sent_to_telegram"):
                continue

            video_path = item.get("output_path")
            if not video_path or not os.path.exists(video_path):
                item["status"] = "error"
                continue

            title = item.get("title") or "Short Reaction"
            hashtags = item.get("hashtags") or []
            if isinstance(hashtags, list):
                tags_str = " ".join(str(t) for t in hashtags)
            else:
                tags_str = str(hashtags)

            targets = item.get("target_channels") or []
            channels_lines = []
            for ch in targets:
                name = ch.get("title") or ch.get("channel_id") or "Unknown"
                privacy = (ch.get("privacy") or "unlisted").strip().lower()
                channels_lines.append(f"- {name} ({privacy})")
            channels_text = "\n".join(channels_lines) if channels_lines else "- لا توجد قنوات مفعلة حالياً"

            # توليد وصف مقترح إن لم يكن مخزناً مسبقاً
            description = item.get("description") or ""
            if not description:
                try:
                    description = f"{(title or '').strip()}\n\n{tags_str or ''}".strip()
                    item["description"] = description
                    save_state(state, cfg)
                except Exception as e:
                    logger.warning(f"Failed to generate description for preview: {e}")
                    description = ""

            caption = (
                "🎬 <b>فيديو جديد بانتظار الموافقة</b>\n\n"
                f"<b>العنوان:</b> {html.escape(title)}\n\n"
                "<b>الوصف المقترح:</b>\n"
                f"{html.escape(description) or '-'}\n\n"
                "<b>الهاشتاقات:</b>\n"
                f"{html.escape(tags_str) or '-'}\n\n"
                "<b>قنوات النشر المستهدفة:</b>\n"
                f"{html.escape(channels_text)}"
            )

            keyboard = [
                [
                    InlineKeyboardButton("✅ موافق ونشر", callback_data=f"pv_approve:{item['id']}")
                ],
                [
                    InlineKeyboardButton("🗑️ إلغاء وحذف", callback_data=f"pv_delete:{item['id']}")
                ],
                [
                    InlineKeyboardButton("💾 إلغاء وحفظ", callback_data=f"pv_save:{item['id']}")
                ],
                [
                    InlineKeyboardButton("⏱️ تأجيل 1 ساعة", callback_data=f"pv_delay:1:{item['id']}")
                ],
                [
                    InlineKeyboardButton("⏱️ تأجيل 2 ساعة", callback_data=f"pv_delay:2:{item['id']}")
                ],
                [
                    InlineKeyboardButton("⏱️ تأجيل 3 ساعات", callback_data=f"pv_delay:3:{item['id']}")
                ],
                [
                    InlineKeyboardButton("♻️ إنشاء فيديو آخر", callback_data=f"pv_regen:{item['id']}")
                ],
            ]

            try:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_path,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
                item["sent_to_telegram"] = True
                save_state(state, cfg)
            except Exception as e:
                logger.error(f"Failed to send pending video preview: {e}")
            break

    finally:
        context.application.bot_data[running_key] = False


from ...agent.scheduler import is_scheduler_running

async def run_due_channel_publishes(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)
    
    # Check if global scheduler is running first
    if not is_scheduler_running():
        return

    if not ((state.get("agent") or {}).get("auto_publish_enabled", True)):
        return

    try:
        seq = bool((state.get("scheduler", {}) or {}).get("sequential_approval", True))
    except Exception:
        seq = True
    if seq:
        return

    # prevent overlapping runs
    running_key = "auto_publish_running"
    if context.application.bot_data.get(running_key):
        return
    context.application.bot_data[running_key] = True

    try:
        cm = ChannelManager()
        due_channels = cm.get_channels_ready_to_publish()
        if not due_channels:
            return

        pubs = state.get("publish_channels") or []

        for ch in due_channels:
            pub = None
            for p in pubs:
                if (p.get("channel_id") or "").strip() == (ch.youtube_channel_id or "").strip():
                    pub = p
                    break

            if not pub:
                continue

            try:
                loop = asyncio.get_running_loop()
            except Exception:
                loop = asyncio.get_event_loop()
            try:
                from ...agent.core import run_once_for_channel
                result = await loop.run_in_executor(None, run_once_for_channel, cfg, None, pub, False)
            except Exception as e:
                logger.error(f"Auto publish failed for {ch.channel_name}: {e}")
                from ...agent.core import _queue_telegram_notification
                st_tmp = load_state(cfg)
                _queue_telegram_notification(st_tmp, "error", {
                    "error": str(e),
                    "channel": ch.channel_name
                })
                save_state(st_tmp, cfg)
                continue

            ok = False
            try:
                pr = (result or {}).get("publish_result") or {}
                ok = bool(pr.get("success")) or bool((result or {}).get("skipped"))
            except Exception:
                ok = False
            if ok:
                try:
                    cm.mark_published(ch.channel_id)
                except Exception:
                    pass

    finally:
        context.application.bot_data[running_key] = False


async def approve_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return
    pending_id = parts[-1]

    cfg = load_config()
    state = load_state(cfg)
    item = _find_pending(state, pending_id)
    if not item:
        await query.edit_message_caption(caption="❌ هذا الفيديو لم يعد متاحاً أو تم التعامل معه.")
        return

    if item.get("status") in {"approved", "published"}:
        await query.edit_message_caption(caption="✅ تم نشر هذا الفيديو مسبقاً.")
        return

    if item.get("status") in {"publishing"}:
        try:
            await query.edit_message_caption(caption="⏳ جارٍ النشر بالفعل...")
        except Exception:
            pass
        return

    try:
        def _lock(st):
            lock = st.setdefault("publishing_lock", {"active": False, "until": None, "kind": None, "id": None})
            lock["active"] = True
            lock["until"] = time.time() + 60 * 30
            lock["kind"] = "pending"
            lock["id"] = pending_id
            it = _find_pending(st, pending_id)
            if it:
                it["status"] = "publishing"
                it["publishing_started_at"] = datetime.now().isoformat(timespec="seconds")
        update_state(cfg, _lock)
    except Exception:
        pass

    try:
        await query.edit_message_caption(caption="⏳ <b>جاري النشر...</b>", parse_mode="HTML", reply_markup=None)
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    try:
        result: Dict[str, Any] = await loop.run_in_executor(
            None,
            publish_pending_video,
            cfg,
            pending_id,
        )
    except Exception as e:
        logger.error(f"Failed to publish pending video: {e}")
        try:
            def _fail(st):
                it = _find_pending(st, pending_id)
                if it:
                    it["status"] = "failed"
                    it["publish_error"] = str(e)
                lock = (st.get("publishing_lock") or {})
                if lock.get("id") == pending_id:
                    st["publishing_lock"] = {"active": False, "until": None, "kind": None, "id": None}
            update_state(cfg, _fail)
        except Exception:
            pass
        await query.edit_message_caption(caption=f"❌ فشل النشر: {e}")
        return

    publish_results = result.get("publish_results", [])
    try:
        def _upd(st):
            it = _find_pending(st, pending_id)
            if not it:
                return
            it["status"] = "published"
            it["publish_results"] = publish_results
            it["published_at"] = datetime.now().isoformat(timespec="seconds")
            lock = (st.get("publishing_lock") or {})
            if lock.get("id") == pending_id:
                st["publishing_lock"] = {"active": False, "until": None, "kind": None, "id": None}
        update_state(cfg, _upd)
    except Exception:
        pass

    lines = ["✅ <b>تم النشر بنجاح</b>\n"]
    if publish_results:
        for r in publish_results:
            if not r.get("success"):
                continue
            ch_name = r.get("channel", "Unknown")
            url = r.get("url", "")
            lines.append(f"• {html.escape(ch_name)}: {html.escape(url)}")
    else:
        lines.append("⚠️ لم يتم العثور على نتائج نشر.")

    text = "\n".join(lines)
    try:
        await query.edit_message_caption(caption=text, parse_mode="Markdown")
    except Exception:
        await query.edit_message_text(text=text, parse_mode="Markdown")


async def delete_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return
    pending_id = parts[-1]

    cfg = load_config()
    state = load_state(cfg)
    item = _find_pending(state, pending_id)
    if not item:
        await query.edit_message_caption(caption="❌ هذا الفيديو لم يعد متاحاً أو تم التعامل معه.")
        return

    video_path = item.get("output_path")
    meta_path = item.get("meta_path")
    for path in [video_path, meta_path]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"Failed to delete file {path}: {e}")

    try:
        def _upd(st):
            it = _find_pending(st, pending_id)
            if it:
                it["status"] = "deleted"
        update_state(cfg, _upd)
    except Exception:
        pass

    try:
        await query.edit_message_caption(caption="🗑️ تم إلغاء الفيديو وحذفه من التخزين.")
    except Exception:
        await query.edit_message_text(text="🗑️ تم إلغاء الفيديو وحذفه من التخزين.")


async def save_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return
    pending_id = parts[-1]

    cfg = load_config()
    state = load_state(cfg)
    item = _find_pending(state, pending_id)
    if not item:
        await query.edit_message_caption(caption="❌ هذا الفيديو لم يعد متاحاً أو تم التعامل معه.")
        return

    try:
        def _upd(st):
            it = _find_pending(st, pending_id)
            if it:
                it["status"] = "saved"
        update_state(cfg, _upd)
    except Exception:
        pass

    try:
        await query.edit_message_caption(caption="💾 تم إلغاء النشر مع الاحتفاظ بالفيديو محلياً.")
    except Exception:
        await query.edit_message_text(text="💾 تم إلغاء النشر مع الاحتفاظ بالفيديو محلياً.")


async def delay_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    parts = (query.data or "").split(":")
    if len(parts) < 3:
        return
    try:
        hours = int(parts[1])
    except ValueError:
        hours = 1
    pending_id = parts[2]

    cfg = load_config()
    state = load_state(cfg)
    item = _find_pending(state, pending_id)
    if not item:
        await query.edit_message_caption(caption="❌ هذا الفيديو لم يعد متاحاً أو تم التعامل معه.")
        return

    run_at = datetime.now() + timedelta(hours=hours)
    try:
        def _upd(st):
            it = _find_pending(st, pending_id)
            if not it:
                return
            it["status"] = "scheduled"
            it["scheduled_at"] = run_at.isoformat(timespec="seconds")
        update_state(cfg, _upd)
    except Exception:
        pass

    msg = f"⏱️ تم تأجيل النشر لمدة {hours} ساعة/ساعات. سيتم النشر تلقائياً لاحقاً."
    try:
        await query.edit_message_caption(caption=msg)
    except Exception:
        await query.edit_message_text(text=msg)


async def regen_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    cfg = load_config()
    from ...agent.core import run_once

    async def _run_new_video() -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_once, cfg, None, False, False)

    try:
        asyncio.create_task(_run_new_video())
    except Exception as e:
        logger.error(f"Failed to start regeneration task: {e}")

    try:
        await query.edit_message_caption(caption="♻️ يتم الآن إنشاء فيديو جديد. سيتم إرساله للمراجعة عند جاهزيته.")
    except Exception:
        await query.edit_message_text(text="♻️ يتم الآن إنشاء فيديو جديد. سيتم إرساله للمراجعة عند جاهزيته.")


async def run_scheduled_publishes(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    state = load_state(cfg)

    pending_list: List[Dict[str, Any]] = state.get("pending_videos", [])
    if not pending_list:
        return

    now = datetime.now()

    for item in pending_list:
        if item.get("status") != "scheduled":
            continue
        ts = item.get("scheduled_at")
        if not ts:
            continue
        try:
            # نستخدم .replace(tzinfo=None) لضمان المقارنة مع datetime.now()
            when = datetime.fromisoformat(ts).replace(tzinfo=None)
        except Exception:
            continue
        if when > now:
            continue

        pending_id = item.get("id")
        if not pending_id:
            continue

        loop = asyncio.get_event_loop()
        try:
            result: Dict[str, Any] = await loop.run_in_executor(
                None,
                publish_pending_video,
                cfg,
                pending_id,
            )
        except Exception as e:
            logger.error(f"Failed to publish scheduled pending video: {e}")
            continue

        publish_results = result.get("publish_results", [])
        item["status"] = "published"
        item["publish_results"] = publish_results
        save_state(state, cfg)
        break


def _format_precheck_reason(reason: str) -> str:
    r = (reason or "").strip().lower()
    if r == "missing_token":
        return "لا يوجد ملف توكن (token)"
    if r == "waiting_issue":
        return "القناة في وضع انتظار (Auth/Quota)"
    return reason or "غير معروف"


async def send_precheck_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    """إرسال رسالة فحص جاهزية القنوات قبل بدء المعالجة."""
    cfg = load_config()
    state = load_state(cfg)

    sched = (state.get("scheduler", {}) or {})
    if not sched.get("precheck_required"):
        return

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    if not admin_ids:
        return
    admin_id = admin_ids[0]

    try:
        now_ts = time.time()
    except Exception:
        now_ts = 0

    try:
        last_sent = float(sched.get("precheck_prompt_last_sent_ts") or 0)
    except Exception:
        last_sent = 0
    if now_ts and (now_ts - last_sent) < 300:
        return

    report = sched.get("precheck_report") or {}
    ready = report.get("ready") or []
    unready = report.get("unready") or []

    lines: List[str] = []
    lines.append("🧪 <b>فحص جاهزية قنوات النشر قبل المعالجة</b>")
    lines.append("")

    if ready:
        lines.append("✅ <b>قنوات جاهزة:</b>")
        for r in ready[:30]:
            title = (r.get("title") or r.get("channel_id") or "Unknown")
            cid = (r.get("channel_id") or "").strip()
            lines.append(f"- {html.escape(title)} ({html.escape(cid)})")
        if len(ready) > 30:
            lines.append(f"... (+{len(ready) - 30})")
        lines.append("")
    else:
        lines.append("⚠️ لا توجد أي قناة جاهزة حالياً.")
        lines.append("")

    if unready:
        lines.append("❌ <b>قنوات غير جاهزة:</b>")
        for u in unready[:30]:
            title = (u.get("title") or u.get("channel_id") or "Unknown")
            cid = (u.get("channel_id") or "").strip()
            reasons = u.get("reasons") or []
            reasons_txt = ", ".join([_format_precheck_reason(x) for x in reasons])
            lines.append(f"- {html.escape(title)} ({html.escape(cid)}) — {html.escape(reasons_txt)}")
        if len(unready) > 30:
            lines.append(f"... (+{len(unready) - 30})")
        lines.append("")

    lines.append("اختر أحد الخيارين:")

    keyboard = [
        [
            InlineKeyboardButton("▶️ متابعة بالقنوات الجاهزة فقط", callback_data="precheck_continue"),
        ],
        [
            InlineKeyboardButton("🛠️ إلغاء/إصلاح القنوات غير الجاهزة", callback_data="precheck_fix"),
        ],
    ]

    msg = "\n".join(lines)
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
        def _upd(st):
            s = st.setdefault("scheduler", {})
            s["precheck_prompt_last_sent_ts"] = now_ts
        update_state(cfg, _upd)
    except Exception as e:
        logger.error(f"Failed to send precheck prompt: {e}")


async def precheck_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    cfg = load_config()
    state = load_state(cfg)

    try:
        lock = (state.get("publishing_lock") or {})
        if lock.get("active"):
            until = lock.get("until")
            if not until or float(until) > time.time():
                await _safe_answer_callback(query, text="⏳ يوجد نشر جارٍ حالياً. انتظر انتهاء النشر ثم حاول مرة أخرى.", show_alert=True)
                return
    except Exception:
        pass

    report = ((state.get("scheduler", {}) or {}).get("precheck_report") or {})
    ready_ids = report.get("ready_ids") or []
    ready_ids = [str(x).strip() for x in ready_ids if str(x).strip()]

    if not ready_ids:
        await _safe_answer_callback(query, text="⚠️ لا توجد قنوات جاهزة حالياً.", show_alert=True)
        return

    try:
        def _upd(st):
            sched = st.setdefault("scheduler", {})
            sched["precheck_mode"] = "ready_only"
            sched["precheck_required"] = False
            sched["precheck_run_allowed_channel_ids"] = ready_ids
        update_state(cfg, _upd)
    except Exception:
        pass

    try:
        await query.edit_message_text(
            text=(
                "✅ تم تفعيل وضع <b>المتابعة بالقنوات الجاهزة فقط</b> لهذه الدورة.\n\n"
                "سيبدأ الوكيل بالإنتاج وسيستهدف فقط القنوات التي لديها توكن صالح وليست في وضع انتظار."
            ),
            parse_mode="HTML",
        )
    except Exception:
        try:
            await query.edit_message_caption(
                caption=(
                    "✅ تم تفعيل وضع <b>المتابعة بالقنوات الجاهزة فقط</b> لهذه الدورة.\n\n"
                    "سيبدأ الوكيل بالإنتاج وسيستهدف فقط القنوات التي لديها توكن صالح وليست في وضع انتظار."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def precheck_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer_callback(query)

    cfg = load_config()
    state = load_state(cfg)
    report = ((state.get("scheduler", {}) or {}).get("precheck_report") or {})
    unready = report.get("unready") or []

    try:
        def _upd(st):
            sched = st.setdefault("scheduler", {})
            sched["precheck_mode"] = "strict"
            sched["precheck_required"] = True
            sched.pop("precheck_run_allowed_channel_ids", None)
        update_state(cfg, _upd)
    except Exception:
        pass

    lines: List[str] = []
    lines.append("🛠️ <b>إصلاح القنوات غير الجاهزة</b>")
    lines.append("")
    lines.append("الوكيل متوقف مؤقتاً حتى تصبح القنوات جاهزة.")
    lines.append("")
    lines.append("الخطوات المقترحة:")
    lines.append("- إذا كانت المشكلة <b>لا يوجد توكن</b>: أعد مصادقة القناة أو ارفع ملف المصادقة.")
    lines.append("- إذا كانت المشكلة <b>Quota</b>: انتظر حتى تتجدد الحصة ثم سيكمل تلقائياً.")
    lines.append("")

    keyboard: List[List[InlineKeyboardButton]] = []
    # أزرار إعادة المصادقة للقنوات التي نعرف internal_id لها
    for u in unready[:8]:
        internal_id = (u.get("internal_id") or "").strip()
        title = (u.get("title") or u.get("channel_id") or "Channel").strip()
        if internal_id:
            keyboard.append([InlineKeyboardButton(f"🔐 إعادة مصادقة: {title}", callback_data=f"reauth_start:{internal_id}")])

    keyboard.append([InlineKeyboardButton("📂 رفع ملف مصادقة لقناة", callback_data="add_channel_file")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])

    msg = "\n".join(lines)
    try:
        await query.edit_message_text(
            text=msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await query.edit_message_caption(
                caption=msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception:
            pass


async def send_telegram_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """إرسال إشعارات النشر (نجاح/فشل) إلى المستخدم."""
    cfg = load_config()
    state = load_state(cfg)

    notifications = state.get("telegram_notifications", [])
    if not notifications:
        return

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    if not admin_ids:
        return
    admin_id = admin_ids[0]

    # معالجة إشعار واحد في كل دورة لتفادي حظر تيليجرام وللحفاظ على استقرار الحالة
    notif = notifications[0]
    notif_type = notif.get("type")
    data = notif.get("data", {})
    
    message = ""
    if notif_type == "success":
        message = (
            "✅ <b>تم النشر بنجاح</b>\n\n"
            f"📺 <b>القناة:</b> {html.escape(data.get('channel', 'Unknown'))}\n"
            f"📝 <b>العنوان:</b> {html.escape(data.get('title', 'No Title'))}\n"
            f"🔗 <b>الرابط:</b> {html.escape(data.get('url', 'No URL'))}\n\n"
            "📄 <b>الوصف:</b>\n"
            f"{html.escape(data.get('description', '-')[:300])}..."
        )
    elif notif_type == "error":
        message = (
            "❌ <b>فشل في النشر</b>\n\n"
            f"📺 <b>القناة:</b> {html.escape(data.get('channel', 'Unknown'))}\n"
            f"⚠️ <b>الخطأ:</b> {html.escape(data.get('error', 'Unknown error'))}"
        )
    elif notif_type == "auth_required":
        message = (
            "🔐 <b>مطلوب إعادة مصادقة</b>\n\n"
            f"📺 <b>القناة:</b> {html.escape(data.get('channel', 'Unknown'))}\n"
            f"🎬 <b>الفيديو:</b> {html.escape(data.get('title', '-'))}")
    elif notif_type == "quota_exceeded":
        retry_s = data.get("retry_after_s")
        try:
            retry_s = int(retry_s)
        except Exception:
            retry_s = None
        retry_txt = f"سيتم إعادة المحاولة بعد ~{retry_s} ثانية" if retry_s else "سيتم إعادة المحاولة لاحقاً"
        message = (
            "⏳ <b>نفدت حصة YouTube API (Quota)</b>\n\n"
            f"📺 <b>القناة:</b> {html.escape(data.get('channel', 'Unknown'))}\n"
            f"🎬 <b>الفيديو:</b> {html.escape(data.get('title', '-'))}\n"
            f"🕒 {html.escape(retry_txt)}"
        )
    elif notif_type == "retryable_error":
        retry_s = data.get("retry_after_s")
        try:
            retry_s = int(retry_s)
        except Exception:
            retry_s = None
        retry_txt = f"سيتم إعادة المحاولة بعد ~{retry_s} ثانية" if retry_s else "سيتم إعادة المحاولة لاحقاً"
        message = (
            "🔁 <b>خطأ مؤقت — تمت جدولة إعادة المحاولة</b>\n\n"
            f"📺 <b>القناة:</b> {html.escape(data.get('channel', 'Unknown'))}\n"
            f"⚙️ <b>المرحلة:</b> {html.escape(data.get('stage', '-'))}\n"
            f"⚠️ <b>الخطأ:</b> {html.escape(data.get('error', '-'))}\n"
            f"🕒 {html.escape(retry_txt)}"
        )
    
    if message:
        import random
        import time

        max_attempts = 5
        base_sleep = 0.8

        for attempt in range(max_attempts):
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
                # إزالة الإشعار بعد الإرسال بنجاح
                notifications.pop(0)
                state["telegram_notifications"] = notifications
                save_state(state, cfg)
                break
            except Exception as e:
                msg = str(e or "")
                low = msg.lower()
                transient = (
                    "remoteprotocolerror" in low
                    or "server disconnected" in low
                    or "connection reset" in low
                    or "connection aborted" in low
                    or "readerror" in low
                    or "timed out" in low
                    or "timeout" in low
                )

                if transient and attempt < (max_attempts - 1):
                    delay = base_sleep * (2 ** min(attempt, 5))
                    delay = delay + random.random() * min(1.0, delay)
                    time.sleep(min(8.0, delay))
                    continue

                logger.error(f"Failed to send telegram notification: {e}")
                break
