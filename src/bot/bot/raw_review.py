import asyncio
import html
import logging
import os
import uuid
from typing import Any, Dict
import json

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..agent.alert_system import get_alert_system
from ..agent.config import load_config
from ..utils.resilient_fs import ResilientFS
from .persistence import (
    RAW_REVIEW_SKIP_COOLDOWN_SECONDS,
    approve_pending_raw_review,
    block_pending_raw_review,
    clear_pending_raw_review,
    find_pending_raw_review_by_token,
    get_pending_raw_review,
    load_state,
    set_pending_raw_review,
    skip_pending_raw_review,
)
from .security import get_security_manager

logger = logging.getLogger(__name__)


async def _send_link_review_fallback(
    *,
    admin_chat_id: int,
    entry: Dict[str, Any],
    keyboard: InlineKeyboardMarkup,
    bot_app=None,
) -> bool:
    """Fallback: send a text review request (video URL + buttons) when file upload fails/unavailable."""
    caption = _review_caption(entry)
    video_url = str((entry or {}).get("video_url") or "").strip()
    if video_url:
        caption += f"\n\n🔗 <a href=\"{html.escape(video_url)}\">رابط الفيديو</a>"

    if bot_app is not None:
        try:
            await bot_app.bot.send_message(
                chat_id=admin_chat_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
            return True
        except Exception as send_error:
            logger.warning("Raw review link fallback via bot_app failed: %s", send_error)

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return False

    try:
        import aiohttp

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": int(admin_chat_id),
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": keyboard.to_dict(),
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            async with session.post(url, data=data, headers=headers) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.warning("Raw review link fallback failed (%s): %s", resp.status, body[:200])
    except Exception as http_error:
        logger.warning("Raw review link fallback HTTP error: %s", http_error)
    return False


async def _run_approved_review_now(
    entry: Dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
    *,
    max_attempts: int = 4,
    retry_delay_seconds: float = 2.0,
) -> None:
    from ..agent.auto_mod_fetcher import AutoModFetcher

    source_name = str((entry or {}).get("source_name") or "المصدر")
    video_title = str((entry or {}).get("video_title") or "بدون عنوان")
    fetcher = AutoModFetcher()

    async def _notify_admin(message: str) -> None:
        try:
            bot = getattr(context, "bot", None)
            admin_chat_id = get_alert_system().get_admin_chat_id()
            if bot and admin_chat_id:
                await bot.send_message(chat_id=admin_chat_id, text=message)
        except Exception as notify_error:
            logger.debug("Immediate raw-review resume notify failed: %s", notify_error)

    logger.info(
        "⚡ Scheduling immediate processing after raw-review approval for source=%s video=%s",
        source_name,
        video_title,
    )

    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            result = await fetcher.run_cycle(
                notify_func=_notify_admin,
                force=True,
                target_channel_id=(entry or {}).get("channel_id"),
                target_content_type=(entry or {}).get("content_type"),
                target_source_id=(entry or {}).get("source_id"),
                target_video_id=(entry or {}).get("video_id"),
                target_video_url=(entry or {}).get("video_url"),
                target_video_title=(entry or {}).get("video_title"),
                target_video_type=(entry or {}).get("video_type"),
                target_raw_video_path=((entry or {}).get("raw_video_path") or None),
            )
        except Exception as exc:
            logger.warning(
                "Immediate processing after raw-review approval failed on attempt %s/%s for %s: %s",
                attempt,
                max_attempts,
                video_title,
                exc,
            )
            if attempt >= max_attempts:
                await _notify_admin(
                    f"⚠️ تمت الموافقة على `{video_title[:40]}` لكن فشلت محاولة البدء الفوري."
                )
                return
            await asyncio.sleep(max(0.0, retry_delay_seconds))
            continue

        if result.get("status") != "busy":
            status = result.get("status")
            if status == "waiting_raw_review":
                await _notify_admin(
                    f"⏸ تمت الموافقة على `{video_title[:40]}` لكن لا يمكن متابعة المعالجة الآن لأن هناك مراجعة خام أخرى معلّقة."
                )
            elif status == "no_target_schedule":
                await _notify_admin(
                    f"⚠️ تمت الموافقة على `{video_title[:40]}` لكن الجدول/المصدر المرتبط به لم يعد موجودًا أو لم يعد نشطًا."
                )
            elif not result.get("processed") and not result.get("published") and not result.get("failed"):
                await _notify_admin(
                    f"ℹ️ تمت الموافقة على `{video_title[:40]}` لكن لم تبدأ أي معالجة فورية لهذه المحاولة."
                )
            return

        if attempt < max_attempts:
            await asyncio.sleep(max(0.0, retry_delay_seconds))

    await _notify_admin(
        f"⏳ تمت الموافقة على `{video_title[:40]}` لكن توجد دورة أخرى قيد التشغيل الآن، "
        "وسيُعاد الالتقاط تلقائيًا عند تحررها."
    )


def _schedule_approved_review_processing(entry: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    coroutine = _run_approved_review_now(entry, context)
    application = getattr(context, "application", None)
    create_task = getattr(application, "create_task", None)
    if callable(create_task):
        create_task(coroutine)
        return
    asyncio.create_task(coroutine)


def _review_caption(entry: Dict[str, Any]) -> str:
    title = html.escape(str(entry.get("video_title") or "بدون عنوان")[:120])
    source_name = html.escape(str(entry.get("source_name") or "مصدر")[:60])
    video_type = "شورتس" if str(entry.get("video_type") or "").lower() == "shorts" else "طويل"
    return (
        "🧪 <b>مراجعة فيديو خام قبل المعالجة</b>\n\n"
        f"📺 <b>العنوان:</b> <code>{title}</code>\n"
        f"🧭 <b>المصدر:</b> <code>{source_name}</code>\n"
        f"📐 <b>النوع المتوقع:</b> <code>{video_type}</code>\n"
        "اختر القرار لهذا الفيديو فقط:"
    )


async def request_raw_video_review(
    *,
    source_id: str,
    channel_id: str,
    source_name: str,
    source_url: str,
    content_type: str,
    video: Dict[str, Any],
    raw_video_path: str,
    video_type: str,
) -> bool:
    cfg = load_config()
    alert_system = get_alert_system()
    bot_app = alert_system.get_bot_app()
    admin_chat_id = alert_system.get_admin_chat_id()

    if not admin_chat_id:
        logger.warning("Raw review request skipped because Telegram admin chat is unavailable")
        return False

    existing_pending = get_pending_raw_review(source_id, cfg=cfg)
    if existing_pending:
        logger.warning(
            "Raw review request skipped for source %s because another pending review already exists (video_id=%s)",
            source_id,
            existing_pending.get("video_id"),
        )
        return False

    token = uuid.uuid4().hex
    raw_video_path_value = str(raw_video_path or "").strip()
    if raw_video_path_value:
        raw_video_path_value = os.path.abspath(raw_video_path_value)
    entry = {
        "token": token,
        "source_id": str(source_id),
        "channel_id": str(channel_id),
        "content_type": str(content_type),
        "source_name": str(source_name or ""),
        "source_url": str(source_url or ""),
        "video_id": str((video or {}).get("id") or ""),
        "video_title": str((video or {}).get("title") or "بدون عنوان"),
        "video_url": str((video or {}).get("url") or ""),
        "video_type": str(video_type or "long"),
        "raw_video_path": raw_video_path_value,
        "requested_at": str((video or {}).get("upload_date") or ""),
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ موافقة", callback_data=f"rawrev:approve:{token}")],
        [InlineKeyboardButton("⏭️ تخطي مؤقت", callback_data=f"rawrev:skip:{token}")],
        [InlineKeyboardButton("🚫 حظر دائم", callback_data=f"rawrev:block:{token}")],
    ])

    set_pending_raw_review(source_id, entry, cfg=cfg)

    raw_video_exists = bool(raw_video_path and ResilientFS.exists(raw_video_path))
    if not bot_app or not raw_video_exists:
        if not raw_video_exists:
            logger.warning("Raw review raw file is unavailable; falling back to link review")
        else:
            logger.warning("Raw review bot_app is unavailable; falling back to link review")

        sent = await _send_link_review_fallback(
            admin_chat_id=int(admin_chat_id),
            entry=entry,
            keyboard=keyboard,
            bot_app=bot_app,
        )
        if not sent:
            clear_pending_raw_review(source_id, cfg=cfg)
        return bool(sent)

    try:
        with ResilientFS.open(raw_video_path, "rb") as video_fp:
            await bot_app.bot.send_video(
                chat_id=admin_chat_id,
                video=video_fp,
                caption=_review_caption(entry),
                parse_mode="HTML",
                reply_markup=keyboard,
                supports_streaming=True,
            )
        return True
    except Exception as video_error:
        logger.warning(f"Raw review send_video failed, trying document fallback: {video_error}")
        try:
            with ResilientFS.open(raw_video_path, "rb") as doc_fp:
                await bot_app.bot.send_document(
                    chat_id=admin_chat_id,
                    document=doc_fp,
                    caption=_review_caption(entry),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    filename=os.path.basename(raw_video_path),
                )
            return True
        except Exception as doc_error:
            logger.warning(f"Raw review send failed: {doc_error}")
            sent = await _send_link_review_fallback(
                admin_chat_id=int(admin_chat_id),
                entry=entry,
                keyboard=keyboard,
                bot_app=bot_app,
            )
            if not sent:
                clear_pending_raw_review(source_id, cfg=cfg)
            return bool(sent)


async def handle_raw_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    cfg = load_config()
    user = update.effective_user
    security = get_security_manager(cfg)
    if not user or not security.is_user_allowed(user.id):
        await query.answer("❌ غير مصرح لك بهذا الإجراء", show_alert=True)
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("❌ طلب غير صالح", show_alert=True)
        return

    _, action, token = parts
    source_id, pending = find_pending_raw_review_by_token(token, cfg=cfg)
    if not pending or not source_id:
        try:
            state = load_state(cfg, force_refresh=True)
            rr = (state.get("raw_review") or {}) if isinstance(state, dict) else {}
            token_decisions = rr.get("token_decisions") or {}
            token_index = rr.get("token_index") or {}
            token_entry = token_index.get(token)
            decision_info = token_decisions.get(token)
        except Exception:
            token_entry = None
            decision_info = None

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if isinstance(decision_info, dict) and decision_info.get("decision"):
            decision = str(decision_info.get("decision"))
            if decision == "approved":
                await query.answer("✅ تمت الموافقة مسبقًا. إذا لم تبدأ المعالجة سيتم استئنافها قريبًا.", show_alert=True)
            elif decision == "blocked":
                await query.answer("🚫 تم الحظر مسبقًا.", show_alert=True)
            elif decision == "skipped":
                await query.answer("⏭️ تم التخطي مسبقًا.", show_alert=True)
            else:
                await query.answer("تم التعامل مع هذا الفيديو مسبقًا.", show_alert=True)
            return

        if isinstance(token_entry, dict) and token_entry:
            # Token still known (e.g., pending got overwritten). Allow late decision anyway.
            if action == "approve":
                decided, _ = approve_pending_raw_review(token, decided_by=user.id, cfg=cfg)
                if decided:
                    _schedule_approved_review_processing(decided, context)
                await query.answer("✅ تمت الموافقة. بدأت محاولة المعالجة الآن في الخلفية.", show_alert=True)
                return
            if action == "skip":
                skip_pending_raw_review(token, decided_by=user.id, cfg=cfg, skip_cooldown_seconds=RAW_REVIEW_SKIP_COOLDOWN_SECONDS)
                await query.answer("⏭️ تم التخطي مؤقتًا.", show_alert=True)
                return
            if action == "block":
                block_pending_raw_review(token, decided_by=user.id, cfg=cfg)
                await query.answer("🚫 تم الحظر الدائم.", show_alert=True)
                return

        await query.answer("تم التعامل مع هذا الفيديو مسبقًا أو انتهت صلاحيته.", show_alert=True)
        return

    should_resume_immediately = False
    if action == "approve":
        decided, _ = approve_pending_raw_review(token, decided_by=user.id, cfg=cfg)
        answer_text = "✅ تمت الموافقة. بدأت محاولة المعالجة الآن في الخلفية."
        should_resume_immediately = True
    elif action == "skip":
        decided, _ = skip_pending_raw_review(
            token,
            decided_by=user.id,
            cfg=cfg,
            skip_cooldown_seconds=RAW_REVIEW_SKIP_COOLDOWN_SECONDS,
        )
        answer_text = "⏭️ تم التخطي مؤقتًا. لن يُقترح هذا الفيديو الآن."
    elif action == "block":
        decided, _ = block_pending_raw_review(token, decided_by=user.id, cfg=cfg)
        answer_text = "🚫 تم الحظر الدائم. لن يُقترح هذا الفيديو مرة أخرى."
    else:
        await query.answer("❌ إجراء غير معروف", show_alert=True)
        return

    if not decided:
        await query.answer("تعذر حفظ القرار. حاول مرة أخرى.", show_alert=True)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if should_resume_immediately:
        _schedule_approved_review_processing(decided, context)
        try:
            if getattr(query, "message", None):
                await query.message.reply_text("⚙️ تمت الموافقة، وبدأت محاولة المعالجة الفورية الآن.")
        except Exception:
            pass
    else:
        raw_video_path = str((decided or {}).get("raw_video_path") or "").strip()
        if raw_video_path:
            try:
                ResilientFS.remove(raw_video_path)
            except Exception:
                logger.debug("Failed to cleanup skipped/blocked raw-review artifact: %s", raw_video_path)

    await query.answer(answer_text, show_alert=False)