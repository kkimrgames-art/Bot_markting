"""
معالجات نظام الجلب التلقائي للمودات - واجهة تيليجرام
نظام إدارة المصادر والجدولة والحالة عبر بوت تيليجرام
"""
import os
import html
import asyncio
import logging
import os
import re
import time
import json
from typing import Optional
import uuid
from io import BytesIO
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from ...agent.auto_mod_fetcher import (
    AutoModDB,
    AutoModFetcher,
    _project_local_path,
    get_instance_id,
    normalize_source_settings,
    merge_source_settings,
    resolve_facecam_layout_config,
    resolve_facebook_page_from_video_url,
)
from ...agent.ffmpeg_utils import convert_still_image_to_loop_video
from ...agent.supabase_storage import upload_facecam_to_storage, delete_facecam_from_storage, delete_all_facecam_for_source
# from .channel_handlers import list_channels  # Removed to avoid circular import

async def _list_channels_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .channel_handlers import list_channels
    return await list_channels(update, context)


async def _open_ai_menu_from_auto_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .ai_manager_handler import show_ai_menu
    await show_ai_menu(update, context)
    return ConversationHandler.END


async def _open_api_keys_menu_from_auto_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .api_key_handlers import api_keys_menu
    await api_keys_menu(update, context)
    return ConversationHandler.END




logger = logging.getLogger(__name__)

# ==================== حالات المحادثة ====================
(
    AM_MENU,
    AM_SOURCES,
    AM_ADD_SOURCE_CHANNEL,
    AM_ADD_SOURCE_KIND,
    AM_ADD_CONTAINER_SELECT,
    AM_ADD_SOURCE_URL,
    AM_ADD_SOURCE_NAME,
    AM_ADD_SOURCE_TYPE,
    AM_SCHEDULE,
    AM_SCHEDULE_INTERVAL,
    AM_SCHEDULE_LIMIT,
    AM_SCHEDULE_HOURS,
    AM_STATUS,
    AM_CONFIG,
    AM_ADD_CONTENT_TYPE_NAME,
    AM_EDIT_SOURCE_CHANNEL,
    AM_ADD_SOURCE_FACECAM,
    AM_ADD_SOURCE_CUSTOMIZE,
    AM_SOURCE_TEXT_INPUT,
    AM_COOKIES_UPLOAD,
    AM_VIEW_CONTAINERS,
    AM_VIEW_CONTAINER_VIDEOS,
    AM_VIEW_FACECAM_VIDEOS,
    AM_CLIENT_SECRET_UPLOAD,
) = range(24)


async def _safe_answer(query, **kwargs):
    try:
        if query:
            await query.answer(**kwargs)
    except (BadRequest, Exception):
        pass


async def _safe_edit_message_text(query, text: str, *, reply_markup=None, parse_mode: str = "HTML", **kwargs):
    if not query:
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


def _get_db() -> AutoModDB:
    return AutoModDB(get_instance_id())


OVERLAY_DURATION_OPTIONS = [1.0, 1.5, 2.0, 3.0]
OVERLAY_ANIMATION_DURATION_OPTIONS = [0.3, 0.5, 0.8, 1.0, 1.5]
TAIL_TRIM_OPTIONS = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
VIDEO_EFFECT_DURATION_OPTIONS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
FACECAM_ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
FACECAM_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
try:
    FACECAM_UPLOAD_FILTER = filters.VIDEO | filters.Document.VIDEO | filters.PHOTO | filters.Document.IMAGE
except Exception:
    FACECAM_UPLOAD_FILTER = filters.VIDEO | filters.Document.ALL | filters.PHOTO


def _seconds_label(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except Exception:
        return str(value or "0")


def _position_label(position: str) -> str:
    return {
        "top": "أعلى",
        "center": "المنتصف",
        "bottom": "أسفل",
    }.get((position or "top").strip().lower(), "أعلى")


def _timing_label(timing: str) -> str:
    return {
        "start": "البداية",
        "end": "النهاية",
        "full": "كامل الفيديو",
    }.get((timing or "full").strip().lower(), "كامل الفيديو")


def _mode_label(mode: str) -> str:
    return "عشوائي" if (mode or "fixed").strip().lower() == "random" else "ثابت"


def _fetch_sources(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        raw = (settings or {}).get("fetch_sources")
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            out.append({
                "url": url,
                "name": str(item.get("name") or "").strip(),
                "platform": str(item.get("platform") or "").strip().lower(),
                "enabled": bool(item.get("enabled", True)),
            })
        return out
    except Exception:
        return []


def _fetch_sources_status(settings: Dict[str, Any]) -> str:
    items = _fetch_sources(settings)
    if not items:
        return "افتراضي (رابط واحد)"
    enabled = sum(1 for x in items if bool((x or {}).get("enabled", True)))
    return f"{enabled}/{len(items)}"


def _fetch_sources_for_ui(src: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return fetch sources for UI with legacy fallback to source_url."""
    settings = _source_settings(src)
    items = _fetch_sources(settings)
    if items:
        return items
    legacy_url = str((src or {}).get("source_url") or "").strip()
    if not legacy_url:
        return []
    return [{
        "url": legacy_url,
        "name": "الرابط الافتراضي",
        "platform": str((src or {}).get("platform") or "").strip().lower() or None,
        "enabled": True,
    }]


def _fetch_sources_status_for_ui(src: Dict[str, Any]) -> str:
    items = _fetch_sources_for_ui(src)
    if not items:
        return "افتراضي (رابط واحد)"
    enabled = sum(1 for x in items if bool((x or {}).get("enabled", True)))
    return f"{enabled}/{len(items)}"


def _placement_label(placement: str) -> str:
    return "قبل الوصف" if (placement or "append").strip().lower() == "prepend" else "بعد الوصف"


def _split_overlay_texts(raw_text: str) -> List[str]:
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


def _split_description_texts(raw_text: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    for line in (raw_text or "").splitlines():
        if line.strip() == "---":
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
            continue
        current.append(line.rstrip())
    block = "\n".join(current).strip()
    if block:
        blocks.append(block)
    if blocks:
        return blocks
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


def _source_settings(source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return normalize_source_settings((source or {}).get("settings"))


def _draft_source_settings(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    draft = context.user_data.setdefault("am_new_source", {})
    settings = normalize_source_settings(draft.get("source_settings") or {})
    draft["source_settings"] = settings
    return settings


def _update_draft_source_settings(context: ContextTypes.DEFAULT_TYPE, updates: Dict[str, Any]) -> Dict[str, Any]:
    draft = context.user_data.setdefault("am_new_source", {})
    merged = merge_source_settings(draft.get("source_settings") or {}, updates)
    draft["source_settings"] = merged
    return merged


def _ensure_draft_source_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    draft = context.user_data.setdefault("am_new_source", {})
    source_id = str(draft.get("source_id") or "").strip()
    if not source_id:
        source_id = str(uuid.uuid4())
        draft["source_id"] = source_id
    return source_id


def _facecam_config(settings: Dict[str, Any]) -> Dict[str, Any]:
    facecam = (settings or {}).get("facecam")
    return facecam if isinstance(facecam, dict) else {}


def _facecam_clips(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    clips = _facecam_config(settings).get("clips")
    return [dict(item) for item in clips or [] if isinstance(item, dict)]


def _resolved_facecam_config(settings: Dict[str, Any]) -> Dict[str, Any]:
    facecam = _facecam_config(settings)
    return resolve_facecam_layout_config(
        facecam.get("layout"),
        position=facecam.get("position"),
        shape=facecam.get("shape"),
        scale=facecam.get("scale"),
    )


def _facecam_position_label(position: str) -> str:
    return {
        "top_center": "أعلى الفيديو",
        "bottom_center": "أسفل الفيديو",
        "top_right": "أعلى اليمين",
        "top_left": "أعلى اليسار",
        "bottom_right": "أسفل اليمين",
        "bottom_left": "أسفل اليسار",
        "center": "وسط الفيديو",
    }.get((position or "top_center").strip().lower(), "أعلى الفيديو")


def _facecam_layout_label(settings: Dict[str, Any]) -> str:
    resolved = _resolved_facecam_config(settings)
    return {
        "top_center": "دائري أعلى الفيديو",
        "bottom_center": "دائري أسفل الفيديو",
        "small_circle_top_left": "دائرة صغيرة أعلى اليسار",
        "small_circle_top_right": "دائرة صغيرة أعلى اليمين",
        "small_circle_bottom_right": "دائرة صغيرة أسفل اليمين",
        "small_circle_bottom_left": "دائرة صغيرة أسفل اليسار",
    }.get(resolved.get("layout"), _facecam_position_label(resolved.get("position")))


def _build_facecam_settings(selection: str, clips: List[Dict[str, Any]], *, enabled: bool = True) -> Dict[str, Any]:
    resolved = resolve_facecam_layout_config(selection, position=selection)
    return {
        "layout": resolved["layout"],
        "enabled": enabled,
        "position": resolved["position"],
        "shape": resolved["shape"],
        "scale": resolved["scale"],
        "clips": [dict(item) for item in clips if isinstance(item, dict)],
    }


def _facecam_status(settings: Dict[str, Any]) -> str:
    facecam = _facecam_config(settings)
    clips = _facecam_clips(settings)
    enabled_count = sum(1 for clip in clips if clip.get("enabled"))
    if not facecam.get("enabled"):
        return "❌ معطل"
    return f"✅ {_facecam_layout_label(settings)} / {enabled_count} مقطع"


def _truncate_facecam_clip_name(name: str, limit: int = 26) -> str:
    clean = str(name or "facecam").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _facecam_details(settings: Dict[str, Any]) -> str:
    facecam = _facecam_config(settings)
    resolved = _resolved_facecam_config(settings)
    clips = _facecam_clips(settings)
    enabled_count = sum(1 for clip in clips if clip.get("enabled"))
    lines = [
        f"الحالة: {'✅ مفعل' if facecam.get('enabled') else '❌ معطل'}",
        f"الوضعية: <code>{html.escape(_facecam_layout_label(settings))}</code>",
        f"الموضع: <code>{html.escape(_facecam_position_label(resolved.get('position')))}</code>",
        f"الشكل: <code>{html.escape(str(resolved.get('shape') or 'circle'))}</code>",
        f"الحجم: <code>{resolved.get('scale', 0.28)}</code>",
        f"المقاطع: <code>{enabled_count}/{len(clips)}</code> مفعّل",
    ]
    for idx, clip in enumerate(clips[:5], start=1):
        status = "✅" if clip.get("enabled") else "⏸"
        lines.append(f"{idx}. {status} <code>{html.escape(_truncate_facecam_clip_name(clip.get('name')))}</code>")
    if len(clips) > 5:
        lines.append(f"… وباقي <code>{len(clips) - 5}</code> مقاطع")
    return "\n".join(lines)


def _facecam_storage_paths(source_id: str, clip_id: str, extension: str) -> tuple[str, str]:
    rel_path = os.path.join(".data", "facecam_sources", source_id, f"{clip_id}{extension}").replace("\\", "/")
    abs_path = _project_local_path(*rel_path.split("/"))
    return rel_path, abs_path


def _facecam_document_is_image(document: Any) -> bool:
    mime_type = str(getattr(document, "mime_type", "") or "").strip().lower()
    file_name = str(getattr(document, "file_name", "") or "")
    extension = os.path.splitext(file_name)[1].lower()
    return mime_type.startswith("image/") or extension in FACECAM_IMAGE_EXTENSIONS


def _delete_facecam_clip_file(clip: Dict[str, Any]) -> None:
    clip_id = str((clip or {}).get("id") or "").strip()
    if clip_id:
        try:
            delete_facecam_from_storage(clip_id)
        except Exception:
            pass
    raw_path = str((clip or {}).get("path") or "").strip()
    if not raw_path:
        return
    abs_path = _project_local_path(*raw_path.replace("\\", "/").split("/")) if not os.path.isabs(raw_path) else raw_path
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        pass


def _cleanup_source_facecam_storage(source: Optional[Dict[str, Any]]) -> None:
    source = source or {}
    source_id = str(source.get("id") or "").strip()
    facecam_cfg = normalize_source_settings(source.get("settings")).get("facecam") or {}
    for clip in list(facecam_cfg.get("clips") or []):
        clip_id = str(clip.get("id") or "").strip()
        if clip_id:
            try:
                delete_facecam_from_storage(clip_id)
            except Exception:
                pass
        _delete_facecam_clip_file(clip)
    if source_id:
        try:
            delete_all_facecam_for_source(source_id)
        except Exception:
            pass
    if not source_id:
        return
    source_dir = _project_local_path(".data", "facecam_sources", source_id)
    try:
        if os.path.isdir(source_dir):
            shutil.rmtree(source_dir, ignore_errors=True)
    except Exception:
        pass


def _set_draft_facecam_settings(context: ContextTypes.DEFAULT_TYPE, facecam_update: Dict[str, Any]) -> Dict[str, Any]:
    merged = _update_draft_source_settings(context, {"facecam": facecam_update})
    context.user_data.setdefault("am_new_source", {})["facecam_settings"] = {"facecam": dict(merged.get("facecam") or {})}
    return merged


async def _download_facecam_clip(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    message = update.message
    if not message:
        return None, "❌ لم أستلم رسالة صالحة للرفع."

    telegram_file = None
    file_name = "facecam.mp4"
    treat_as_image = False
    if message.video:
        telegram_file = message.video
    elif message.photo:
        telegram_file = message.photo[-1]
        file_name = "facecam.jpg"
        treat_as_image = True
    elif message.document:
        telegram_file = message.document
        file_name = telegram_file.file_name or file_name
        treat_as_image = _facecam_document_is_image(telegram_file)
    else:
        return None, "❌ أرسل فيديو أو صورة صالحة."

    if message.video:
        file_name = getattr(message.video, "file_name", None) or file_name

    try:
        from ...agent.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None

    file_size = int(getattr(telegram_file, "file_size", 0) or 0)
    if (not cfg or not getattr(cfg, "LOCAL_BOT_API_URL", None)) and file_size > 20 * 1024 * 1024:
        return None, "❌ الملف كبير جداً لرفع البوت الحالي. اختر ملفاً أصغر من 20MB."

    clip_id = str(uuid.uuid4())
    extension = os.path.splitext(file_name)[1].lower()
    if treat_as_image:
        extension = extension or ".jpg"
        if extension not in FACECAM_IMAGE_EXTENSIONS:
            return None, "❌ صيغة الصورة غير مدعومة. استخدم jpg أو jpeg أو png أو webp أو bmp."
        temp_rel_path, temp_abs_path = _facecam_storage_paths(source_id, f"{clip_id}_src", extension)
        rel_path, abs_path = _facecam_storage_paths(source_id, clip_id, ".mp4")
    else:
        extension = extension or ".mp4"
        if extension not in FACECAM_ALLOWED_EXTENSIONS:
            return None, "❌ صيغة غير مدعومة. استخدم mp4 أو mov أو webm أو mkv، أو أرسل صورة مدعومة."
        temp_rel_path, temp_abs_path = _facecam_storage_paths(source_id, clip_id, extension)
        rel_path, abs_path = temp_rel_path, temp_abs_path

    os.makedirs(os.path.dirname(temp_abs_path), exist_ok=True)

    try:
        tg_file = await context.bot.get_file(telegram_file.file_id)
        await tg_file.download_to_drive(temp_abs_path)
    except Exception as exc:
        return None, f"❌ فشل حفظ المقطع: {exc}"

    if treat_as_image:
        if not convert_still_image_to_loop_video(temp_abs_path, abs_path):
            try:
                if os.path.isfile(temp_abs_path):
                    os.remove(temp_abs_path)
            except Exception:
                pass
            return None, "❌ تعذر تحويل الصورة إلى فيديو متوافق مع Facecam."
        try:
            if os.path.isfile(temp_abs_path):
                os.remove(temp_abs_path)
        except Exception:
            pass
        file_name = f"{os.path.splitext(file_name)[0] or 'facecam'}.mp4"

    try:
        await asyncio.to_thread(upload_facecam_to_storage, source_id, clip_id, abs_path)
    except Exception:
        pass

    return {
        "id": clip_id,
        "path": rel_path,
        "name": file_name,
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, None


def _short_overlay_status(settings: Dict[str, Any]) -> str:
    overlay = (settings or {}).get("shorts_overlay") or {}
    texts = overlay.get("texts") or []
    if not texts:
        return "❌ بلا نصوص"
    if not overlay.get("enabled"):
        return f"⏸ {len(texts)} نص"
    return f"✅ {len(texts)} نص / {_mode_label(overlay.get('selection_mode'))}"


def _short_desc_status(settings: Dict[str, Any]) -> str:
    desc = (settings or {}).get("extra_description") or {}
    texts = desc.get("texts") or []
    if not texts:
        return "❌ بلا نصوص"
    if not desc.get("enabled"):
        return f"⏸ {len(texts)} نص"
    return f"✅ {len(texts)} نص / {_mode_label(desc.get('selection_mode'))}"


def _overlay_details(settings: Dict[str, Any]) -> str:
    overlay = (settings or {}).get("shorts_overlay") or {}
    texts = overlay.get("texts") or []
    return (
        f"الحالة: {'✅ مفعل' if overlay.get('enabled') else '❌ معطل'}\n"
        f"عدد النصوص: <code>{len(texts)}</code>\n"
        f"الاختيار: <code>{html.escape(_mode_label(overlay.get('selection_mode')))}</code>\n"
        f"التوقيت: <code>{html.escape(_timing_label(overlay.get('timing')))}</code>\n"
        f"المدة: <code>{overlay.get('duration', 2.0)}</code> ثانية\n"
        f"الموضع: <code>{html.escape(_position_label(overlay.get('screen_position')))}</code>\n"
        f"أنيميشن الظهور: <code>{html.escape(_overlay_animation_status(settings, 'intro'))}</code>\n"
        f"أنيميشن الاختفاء: <code>{html.escape(_overlay_animation_status(settings, 'outro'))}</code>"
    )


def _description_details(settings: Dict[str, Any]) -> str:
    desc = (settings or {}).get("extra_description") or {}
    texts = desc.get("texts") or []
    return (
        f"الحالة: {'✅ مفعل' if desc.get('enabled') else '❌ معطل'}\n"
        f"عدد النصوص: <code>{len(texts)}</code>\n"
        f"الاختيار: <code>{html.escape(_mode_label(desc.get('selection_mode')))}</code>\n"
        f"الموضع داخل الوصف: <code>{html.escape(_placement_label(desc.get('placement')))}</code>"
    )


def _raw_review_status(settings: Dict[str, Any]) -> str:
    return "✅ مراجعة خام أولاً" if (settings or {}).get("require_raw_review") else "❌ مباشر"


def _tail_trim_status(settings: Dict[str, Any]) -> str:
    trim_cfg = (settings or {}).get("tail_trim") or {}
    seconds = trim_cfg.get("seconds", 0.0)
    try:
        seconds = max(0.0, float(seconds))
    except Exception:
        seconds = 0.0
    if trim_cfg.get("enabled") and seconds > 0:
        return f"✅ {_seconds_label(seconds)} ثانية من النهاية"
    return "❌ بدون قص"


def _video_effect_target_label(target: str) -> str:
    return "البداية" if (target or "intro").strip().lower() == "intro" else "النهاية"


def _video_effect_type_label(effect_type: str) -> str:
    return {
        "blur": "Blur عادي",
        "black_blur": "Black Blur",
        "none": "بدون تأثير",
    }.get((effect_type or "none").strip().lower(), "بدون تأثير")


def _build_video_effect_config(effect_type: str, duration: float = 0.0) -> Dict[str, Any]:
    normalized_type = (effect_type or "none").strip().lower()
    try:
        duration = max(0.0, float(duration or 0.0))
    except Exception:
        duration = 0.0
    enabled = normalized_type in {"blur", "black_blur"} and duration > 0
    return {
        "enabled": enabled,
        "type": normalized_type if enabled else "none",
        "duration": duration if enabled else 0.0,
    }


def _overlay_animation_target_label(target: str) -> str:
    return "الظهور" if (target or "intro").strip().lower() == "intro" else "الاختفاء"


def _overlay_animation_type_label(animation_type: str) -> str:
    return {
        "fade": "Fade",
        "blur": "Blur",
        "none": "بدون أنيميشن",
    }.get((animation_type or "none").strip().lower(), "بدون أنيميشن")


def _build_overlay_animation_config(animation_type: str, duration: float = 0.0) -> Dict[str, Any]:
    normalized_type = (animation_type or "none").strip().lower()
    try:
        duration = max(0.0, float(duration or 0.0))
    except Exception:
        duration = 0.0
    enabled = normalized_type in {"fade", "blur"} and duration > 0
    return {
        "enabled": enabled,
        "type": normalized_type if enabled else "none",
        "duration": duration if enabled else 0.0,
    }


def _overlay_animation_status(settings: Dict[str, Any], target: str) -> str:
    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    overlay = (settings or {}).get("shorts_overlay") or {}
    anim_cfg = overlay.get(f"{target_key}_animation") or {}
    anim_type = str(anim_cfg.get("type") or "none").strip().lower()
    try:
        duration = max(0.0, float(anim_cfg.get("duration", 0.0) or 0.0))
    except Exception:
        duration = 0.0
    if anim_cfg.get("enabled") and anim_type in {"fade", "blur"} and duration > 0:
        return f"✅ {_overlay_animation_type_label(anim_type)} / {_seconds_label(duration)}ث"
    return "❌ بدون أنيميشن"


def _video_effect_status(settings: Dict[str, Any], target: str) -> str:
    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    effects = (settings or {}).get("video_effects") or {}
    effect_cfg = effects.get(target_key) or {}
    effect_type = str(effect_cfg.get("type") or "none").strip().lower()
    try:
        duration = max(0.0, float(effect_cfg.get("duration", 0.0) or 0.0))
    except Exception:
        duration = 0.0
    if effect_cfg.get("enabled") and effect_type in {"blur", "black_blur"} and duration > 0:
        return f"✅ {_video_effect_type_label(effect_type)} / {_seconds_label(duration)}ث"
    return "❌ بدون تأثير"


def _source_privacy_status(settings: Dict[str, Any]) -> str:
    privacy = str((settings or {}).get("privacy") or "").strip().lower()
    if privacy == "public":
        return "🌍 علني"
    if privacy == "private":
        return "🔒 خاص"
    if privacy == "unlisted":
        return "🔗 غير مدرج"
    return "⚙️ حسب القناة"


def _source_shorts_only_status(settings: Dict[str, Any]) -> str:
    if "shorts_only" not in (settings or {}):
        return "⚙️ حسب الكشف التلقائي"
    return "✅ شورتس فقط" if bool((settings or {}).get("shorts_only")) else "❌ كلا النوعين"

def _source_hflip_status(settings: Dict[str, Any]) -> str:
    if "hflip" not in (settings or {}):
        return "⚙️ حسب الإعدادات العامة"
    return "✅ مفعل" if bool((settings or {}).get("hflip")) else "❌ معطل"


def _set_draft_source_hflip(context: ContextTypes.DEFAULT_TYPE, value: Optional[bool]) -> Dict[str, Any]:
    draft = context.user_data.setdefault("am_new_source", {})
    current = normalize_source_settings(draft.get("source_settings") or {})
    raw = dict(current)
    raw.pop("hflip", None)
    raw.pop("hflip_enabled", None)
    if value is not None:
        raw["hflip"] = bool(value)
    normalized = normalize_source_settings(raw)
    draft["source_settings"] = normalized
    return normalized


async def _ask_source_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📝 <b>أدخل اسمًا مختصرًا للمصدر:</b>\n\n"
        "مثال: <code>WoodyyCraft</code> أو <code>قناة المودات</code>\n\n"
        "أو أرسل <code>auto</code> لاستخراج الاسم تلقائيًا من الرابط."
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_NAME


async def _ask_source_tail_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✂️ <b>قص ثابت من نهاية الفيديو</b> <i>(اختياري)</i>\n\n"
        "سيتم تطبيق هذا القص مباشرة بعد تنزيل الفيديو وقبل أي معالجة أخرى مثل التحويل أو النصوص أو الفيس كام.\n\n"
        "اختر مقدار القص من <b>نهاية</b> كل فيديو لهذا المصدر:"
    )
    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for idx, seconds in enumerate(TAIL_TRIM_OPTIONS, start=1):
        row.append(InlineKeyboardButton(f"{_seconds_label(seconds)} ثانية", callback_data=f"am_src_trim:{seconds}"))
        if idx % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬜ بدون قص", callback_data="am_src_trim:off")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")])

    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def _ask_source_video_effect_kind(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    settings = _draft_source_settings(context)
    target_label = _video_effect_target_label(target)
    status = _video_effect_status(settings, target)
    text = (
        f"✨ <b>تأثير {target_label} الفيديو</b> <i>(اختياري)</i>\n\n"
        f"الحالة الحالية: <code>{html.escape(status)}</code>\n\n"
        f"اختر نوع التأثير المتحرك الذي سيُطبَّق في <b>{target_label}</b> هذا المصدر."
    )
    keyboard = [
        [InlineKeyboardButton("⬜ بدون تأثير", callback_data=f"am_src_fx_kind:{target}:none")],
        [InlineKeyboardButton("🌫 Blur عادي", callback_data=f"am_src_fx_kind:{target}:blur")],
        [InlineKeyboardButton("⬛ Black Blur", callback_data=f"am_src_fx_kind:{target}:black_blur")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def _ask_source_video_effect_duration(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, effect_type: str):
    target_label = _video_effect_target_label(target)
    type_label = _video_effect_type_label(effect_type)
    text = (
        f"⏱ <b>مدة تأثير {target_label}</b>\n\n"
        f"النوع المختار: <code>{html.escape(type_label)}</code>\n\n"
        "اختر المدة المناسبة لهذا التأثير:"
    )
    keyboard = [[InlineKeyboardButton(f"{_seconds_label(val)} ثانية", callback_data=f"am_src_fx_dur:{target}:{effect_type}:{val}")] for val in VIDEO_EFFECT_DURATION_OPTIONS]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"am_src_fx_menu:{target}")])
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def _continue_source_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.get("am_new_source", {}) or {}
    if draft.get("source_url"):
        if not draft.get("tail_trim_configured"):
            return await _ask_source_tail_trim(update, context)

        if not draft.get("intro_effect_configured"):
            return await _ask_source_video_effect_kind(update, context, "intro")

        if not draft.get("outro_effect_configured"):
            return await _ask_source_video_effect_kind(update, context, "outro")

        if not draft.get("hflip_configured"):
            return await _ask_source_hflip(update, context)

        if not draft.get("privacy_configured"):
            return await _ask_source_privacy(update, context)

        if not draft.get("overlay_configured"):
            return await add_source_overlay_start(update, context)

        if not draft.get("description_configured"):
            return await add_source_description_start(update, context)

        if not draft.get("raw_review_configured"):
            return await add_source_raw_review_start(update, context)

        return await _ask_source_name(update, context)

    return AM_ADD_SOURCE_CUSTOMIZE


async def gdrive_connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)

    from ...agent.config import load_config
    from ...agent.uploader import _find_client_secrets_file
    from ..auth_flow_utils import DRIVE_SCOPES, start_auth_flow_scopes

    cfg = load_config()
    client_secrets = _find_client_secrets_file(cfg)
    if not client_secrets:
        await query.edit_message_text(
            "❌ ملف client_secret.json غير موجود. أضف/ارفع ملف المصادقة أولاً (نفس الملف المستخدم ليوتيوب).",
            parse_mode="HTML",
        )
        return AM_CONFIG

    try:
        await query.edit_message_text("⏳ جاري تحضير رابط مصادقة Google Drive...", parse_mode="HTML")
        auth_url, server, flow = await asyncio.to_thread(
            start_auth_flow_scopes,
            client_secrets,
            DRIVE_SCOPES,
            include_granted_scopes=False,
        )
        context.user_data["am_gdrive_flow"] = flow
        context.user_data["am_gdrive_server"] = server
        redirect_uri = getattr(flow, "redirect_uri", None) or f"http://localhost:{server.port}/oauth2/callback"

        text = (
            "☁️ <b>ربط Google Drive</b>\n\n"
            f"<a href=\"{auth_url}\">🔗 اضغط هنا للمصادقة</a>\n\n"
            "⚠️ إذا ظهر لك خطأ <b>redirect_uri_mismatch</b> أضف هذا الرابط بالضبط في Google Cloud Console:\n"
            f"<code>{html.escape(redirect_uri)}</code>\n\n"
            "📌 بعد المصادقة سيتم الحفظ تلقائياً."
        )
        keyboard = [
            [InlineKeyboardButton("✅ تم - لدي رابط التحويل", callback_data="am_gdrive_have_url")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_config")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML", disable_web_page_preview=True)

        task = context.application.create_task(gdrive_wait_for_auth_code(update, context))
        context.bot_data.setdefault("am_gdrive_auth_tasks", set()).add(task)
        task.add_done_callback(lambda t: context.bot_data.get("am_gdrive_auth_tasks", set()).discard(t))
        return AM_CONFIG
    except Exception as e:
        logger.error(f"Google Drive auth start failed: {e}")
        await query.edit_message_text(f"❌ خطأ: <code>{html.escape(str(e)[:200])}</code>", parse_mode="HTML")
        return AM_CONFIG


async def gdrive_wait_for_auth_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    server = context.user_data.get("am_gdrive_server")
    if not server:
        return
    try:
        response_uri = await asyncio.to_thread(server.wait_for_response, timeout=300)
        if response_uri and server.error:
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, f"❌ فشلت مصادقة Google Drive: {server.error}")
            return
        if response_uri:
            await gdrive_process_auth_result(update, context, response_uri)
    except Exception as e:
        logger.error(f"Google Drive auth wait error: {e}")


async def gdrive_receive_auth_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    text = (
        "🔗 <b>أرسل رابط التحويل النهائي</b>\n\n"
        "بعد إكمال المصادقة في المتصفح، انسخ رابط الصفحة النهائية (الذي يحتوي على <code>code=</code>) وأرسله هنا."
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_config")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    context.user_data["am_gdrive_awaiting_url"] = True
    return AM_SOURCE_TEXT_INPUT


async def gdrive_receive_auth_url_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("am_gdrive_awaiting_url"):
        return AM_SOURCE_TEXT_INPUT
    url = (update.message.text or "").strip()
    if "code=" not in url and len(url) < 20:
        await update.message.reply_text("⚠️ الرابط/الكود يبدو غير صالح. أرسل رابط التحويل الكامل.")
        return AM_SOURCE_TEXT_INPUT
    context.user_data.pop("am_gdrive_awaiting_url", None)
    await gdrive_process_auth_result(update, context, url)
    return ConversationHandler.END


async def gdrive_process_auth_result(update: Update, context: ContextTypes.DEFAULT_TYPE, response_uri: str):
    from ..auth_flow_utils import exchange_code_and_get_creds

    flow = context.user_data.get("am_gdrive_flow")
    if not flow:
        return
    try:
        creds = await asyncio.to_thread(exchange_code_and_get_creds, flow, response_uri)
        token_payload = None
        try:
            token_payload = json.loads(creds.to_json())
        except Exception:
            token_payload = None

        if not token_payload:
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, "❌ فشل استخراج توكن Google Drive.")
            return

        db = _get_db()
        config = db.get_config()
        config.setdefault("settings", {})
        config["settings"].setdefault("google_drive", {})
        config["settings"]["google_drive"]["token_json"] = token_payload
        config["settings"]["google_drive"]["updated_at"] = time.time()
        db.save_config(config)

        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, "✅ تم ربط Google Drive بنجاح! يمكنك الآن إضافة مصادر Drive.")
    except Exception as e:
        logger.error(f"Google Drive auth processing failed: {e}")
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, f"❌ فشل ربط Google Drive: {e}")
    return


# ==================== القائمة الرئيسية ====================

async def auto_mod_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """القائمة الرئيسية لنظام الجلب التلقائي"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    db = _get_db()
    config = await asyncio.to_thread(db.get_config)
    stats = await asyncio.to_thread(db.get_stats)

    enabled = "✅ مفعل" if config.get("auto_fetch_enabled") else "❌ معطل"
    content_type = config.get("default_content_type", "minecraft_mods")

    text = (
        "🤖 <b>نظام الجلب التلقائي للمودات</b>\n\n"
        f"📊 الحالة: {enabled}\n"
        f"🆔 النسخة: <code>{get_instance_id()[:20]}</code>\n"
        f"📦 نوع المحتوى: <code>{html.escape(content_type)}</code>\n\n"
        f"📈 <b>الإحصائيات:</b>\n"
        f"• القنوات: {stats.get('total_channels', 0)}\n"
        f"• المصادر: {stats.get('total_sources', 0)}\n"
        f"• الجداول: {stats.get('total_schedules', 0)}\n"
        f"• المنشور: {stats.get('published', 0)}\n"
        f"• الفاشل: {stats.get('failed', 0)}\n"
    )

    toggle_text = "⏸ إيقاف" if config.get("auto_fetch_enabled") else "▶️ تشغيل"

    keyboard = [
        [InlineKeyboardButton("📋 القنوات", callback_data="list_channels:0"),
         InlineKeyboardButton("📡 إدارة المصادر", callback_data="am_sources")],
        [InlineKeyboardButton("⏰ الجدولة", callback_data="am_schedule"),
         InlineKeyboardButton("📊 الحالة", callback_data="am_status")],
        [InlineKeyboardButton("📦 حاويات الفيديو", callback_data="am_view_containers"),
         InlineKeyboardButton("🎬 فيديوهات الفيس كام", callback_data="am_fc_viewer")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="am_config")],
        [InlineKeyboardButton("🤖 الذكاء الاصطناعي", callback_data="ai_main_menu"),
         InlineKeyboardButton("🔑 مفاتيح API", callback_data="api_keys_menu")],
        [InlineKeyboardButton(toggle_text, callback_data="am_toggle"),
         InlineKeyboardButton("🚀 تشغيل الآن", callback_data="am_run_now"),
         InlineKeyboardButton("🧪 اختبار", callback_data="am_test_render")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_MENU


# ==================== تشغيل/إيقاف ====================

async def toggle_auto_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل حالة التشغيل"""
    query = update.callback_query
    await _safe_answer(query)

    db = _get_db()
    config = await asyncio.to_thread(db.get_config)
    new_state = not config.get("auto_fetch_enabled", False)
    config["auto_fetch_enabled"] = new_state
    await asyncio.to_thread(db.save_config, config)

    status = "✅ تم تفعيل" if new_state else "⏸ تم إيقاف"
    await query.answer(f"{status} الجلب التلقائي")

    return await auto_mod_menu(update, context)


# ==================== تشغيل دورة فورية ====================

async def run_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تشغيل دورة جلب فورية"""
    query = update.callback_query
    await _safe_answer(query)

    text = "⏳ <b>جاري تشغيل دورة جلب...</b>\n\nقد يستغرق هذا بضع دقائق."
    await query.edit_message_text(text, parse_mode="HTML")

    try:
        fetcher = AutoModFetcher()

        async def notify(msg):
            try:
                await context.bot.send_message(update.effective_chat.id, msg)
            except Exception:
                pass

        results = await fetcher.run_cycle(notify_func=notify, force=True)
        waiting_raw_review = results.get("waiting_raw_review", 0)

        result_text = (
            "✅ <b>انتهت دورة الجلب</b>\n\n"
            f"📊 النتائج:\n"
            f"• تمت المعالجة: {results.get('processed', 0)}\n"
            f"• تم النشر: {results.get('published', 0)}\n"
            f"• فشل: {results.get('failed', 0)}\n"
            f"• بانتظار مراجعة خام: {waiting_raw_review}\n"
            f"• تم التخطي: {results.get('skipped', 0)}\n"
        )

        if results.get("status") == "disabled":
            result_text = "⚠️ الجلب التلقائي معطل. قم بتفعيله أولاً."
        elif results.get("status") == "no_schedules":
            result_text = "⚠️ لا توجد جداول نشر نشطة. أضف جدول نشر أولاً."
        elif results.get("status") == "busy":
            result_text = "⏳ توجد دورة جلب أخرى قيد التشغيل بالفعل، لذلك تم تجاهل التشغيل اليدوي لمنع التكرار."
        elif results.get("status") == "waiting_raw_review":
            result_text = (
                "⏸ <b>تم إيقاف الدورة بانتظار مراجعة خام</b>\n\n"
                f"📊 النتائج الحالية:\n"
                f"• تمت المعالجة: {results.get('processed', 0)}\n"
                f"• تم النشر: {results.get('published', 0)}\n"
                f"• فشل: {results.get('failed', 0)}\n"
                f"• بانتظار مراجعة خام: {waiting_raw_review or 1}\n"
                f"• تم التخطي: {results.get('skipped', 0)}\n\n"
                "لن يتم جلب فيديو جديد من هذا المسار حتى يصدر قرارك على المراجعة الحالية."
            )

    except Exception as e:
        result_text = f"❌ حدث خطأ: <code>{html.escape(str(e)[:200])}</code>"

    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")]]
    await query.message.reply_text(result_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_MENU


async def test_render_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة المصادر لاختيار مصدر لتوليد فيديو اختبار."""
    query = update.callback_query
    await _safe_answer(query)

    db = _get_db()
    sources = await asyncio.to_thread(db.get_sources)

    if not sources:
        text = "⚠️ لا توجد مصادر متاحة حاليًا لتوليد فيديو اختبار. أضف مصدرًا أولاً."
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_MENU

    text = (
        "🧪 <b>اختبار وتطوير</b>\n\n"
        "اختر مصدرًا لتوليد فيديو نهائي تجريبي باستخدام نفس خط المعالجة الحقيقي،\n"
        "لكن <b>بدون نشره على YouTube</b> وبدون تعديل أي حالة نشر رسمية."
    )

    keyboard = []
    for src in sources[:12]:
        src_id = str(src.get("id") or "")
        if not src_id:
            continue
        status = "⏸" if not src.get("enabled", True) else "✅"
        src_name = (src.get("source_name") or "مصدر").strip()[:32]
        keyboard.append([
            InlineKeyboardButton(f"{status} {src_name}", callback_data=f"am_test_render_src:{src_id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_MENU


async def test_render_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ فيديو اختبار لمصدر محدد وإرساله داخل تيليجرام."""
    query = update.callback_query
    await _safe_answer(query)
    source_id = query.data.split(":", 1)[1]

    await query.edit_message_text(
        "⏳ <b>جاري إنشاء فيديو اختبار...</b>\n\nسيتم استخدام خط المعالجة الحقيقي بدون نشر رسمي.",
        parse_mode="HTML",
    )

    preview_path = ""
    try:
        fetcher = AutoModFetcher()

        async def notify(msg):
            try:
                await context.bot.send_message(update.effective_chat.id, msg)
            except Exception:
                pass

        results = await fetcher.run_test_render(source_id, notify_func=notify)
        preview_path = str(results.get("preview_video_path") or "")

        if results.get("status") == "no_target_source":
            result_text = "⚠️ لم يتم العثور على المصدر المطلوب لتنفيذ فيديو الاختبار."
        elif results.get("status") == "busy":
            result_text = "⏳ توجد دورة معالجة أخرى تعمل الآن، لذا تم تأجيل فيديو الاختبار لمنع التداخل."
        elif preview_path and os.path.exists(preview_path):
            caption = (
                "🧪 <b>فيديو الاختبار جاهز</b>\n"
                f"📺 المصدر: <code>{html.escape(str(results.get('preview_source_name') or 'مصدر'))}</code>\n"
                f"🎬 الفيديو: <code>{html.escape(str(results.get('preview_video_title') or 'بدون عنوان')[:80])}</code>\n"
                "🚫 لم يتم النشر على YouTube ولم يتم تعديل الحالة الرسمية."
            )
            try:
                with open(preview_path, "rb") as video_file:
                    telegram_video = BytesIO(video_file.read())
                    telegram_video.name = os.path.basename(preview_path) or "preview.mp4"
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=telegram_video,
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=120,
                    )
                    telegram_video.close()
                result_text = (
                    "✅ <b>تم إنشاء فيديو الاختبار وإرساله هنا في تيليجرام.</b>\n\n"
                    "لم يتم رفع الفيديو إلى YouTube، ولم يتم تحديث حالة النشر أو الجدولة أو الأتمتة الرسمية."
                )
            except Exception as send_e:
                video_size_mb = os.path.getsize(preview_path) / (1024 * 1024)
                if "413" in str(send_e) or "Entity Too Large" in str(send_e) or video_size_mb > 49.5:
                    result_text = (
                        f"✅ <b>تم إنشاء فيديو الاختبار بنجاح.</b>\n\n"
                        f"⚠️ <i>ملاحظة:</i> الفيديو كبير جداً ({video_size_mb:.1f} MB) لعرضه مباشرة في تيليجرام (الحد الأقصى للمعاينة 50 ميجابايت).\n\n"
                        f"عملية المعالجة تعمل بشكل سليم وهذا لا يؤثر على الرفع الفعلي لليوتيوب.\n\n"
                        "🚫 لم يتم النشر على YouTube ولم يتم تعديل الحالة الرسمية."
                    )
                else:
                    raise send_e
        else:
            result_text = (
                "⚠️ انتهى مسار الاختبار بدون ملف فيديو نهائي قابل للإرسال.\n"
                "قد يكون السبب عدم العثور على فيديو جديد مناسب أو حدوث فشل أثناء المعالجة."
            )

    except Exception as e:
        result_text = f"❌ فشل فيديو الاختبار: <code>{html.escape(str(e)[:200])}</code>"
    finally:
        if preview_path:
            AutoModFetcher._cleanup_file(preview_path)

    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")]]
    await query.message.reply_text(result_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_MENU


# ==================== وظيفة الخلفية (JobQueue) ====================

async def run_auto_mod_job(context: ContextTypes.DEFAULT_TYPE):
    """
    وظيفة مجدولة لتشغيل دورة جلب تلقائي في الخلفية
    """
    from ...agent.config import load_config
    from ...agent.alert_system import get_alert_system
    cfg = load_config()
    db = _get_db()
    config = db.get_config()

    if not config.get("auto_fetch_enabled"):
        logger.info("🔄 [AutoMod] Skipping cycle: Auto-fetch is disabled in settings.")
        return

    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS
    admin_id = admin_ids[0] if admin_ids else None
    if not admin_id:
        try:
            admin_id = get_alert_system().get_admin_chat_id()
        except Exception:
            admin_id = None
    
    if not admin_id:
        logger.info("🧪 [AutoMod] Running in silent mode (no admin IDs configured).")

    try:
        # منع التداخل إذا كانت هناك دورة جارية بالفعل
        running_key = "auto_mod_cycle_running"
        if context.application.bot_data.get(running_key):
            logger.warning("🔄 [AutoMod] Skipping cycle: Another cycle is still running (Render timeout/slow processing).")
            if admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text="⚠️ <b>تخطي دورة جلب!</b>\n\nهناك دورة جلب لا تزال تعمل، سيتم تخطي الدورة الحالية لتخفيف الضغط على الخادم (طبيعي للسيرفرات المجانية).",
                        parse_mode="HTML"
                    )
                except: pass
            return
            
        context.application.bot_data[running_key] = True
        logger.info("🚀 [AutoMod] Starting automated fetching cycle...")

        async def notify(msg: str):
            if not admin_id:
                return
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🤖 <b>تحديث الأتمتة:</b>\n\n{html.escape(msg)}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.debug(f"Failed to send background auto-mod notification: {e}")

        # تشغيل الدورة (مع مهلة زمنية 5 دقائق لمنع التعليق)
        fetcher = AutoModFetcher()
        try:
            await asyncio.wait_for(fetcher.run_cycle(notify_func=notify), timeout=300)
            logger.info("✅ [AutoMod] Cycle completed successfully.")
        except asyncio.TimeoutError:
            logger.error("⏱️ [AutoMod] Cycle timed out after 5 minutes.")
            if admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text="⚠️ <b>انتهاء مهلة الأتمتة!</b>\n\nاستغرقت الدورة أكثر من 5 دقائق وتم إنهاؤها تلقائياً. قد يكون هذا بسبب حجم الفيديوهات أو بطء الشبكة.",
                        parse_mode="HTML"
                    )
                except: pass

    except Exception as e:
        logger.error(f"❌ [AutoMod] Error in background auto-mod job: {e}")
        if admin_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"❌ <b>خطأ في الأتمتة:</b>\n<code>{html.escape(str(e)[:200])}</code>",
                    parse_mode="HTML"
                )
            except: pass
    finally:
        context.application.bot_data[running_key] = False


# ==================== إدارة المصادر ====================

async def sources_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة مصادر الجلب"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    db = _get_db()
    sources = await asyncio.to_thread(db.get_sources)

    if not sources:
        text = (
            "📡 <b>مصادر الجلب</b>\n\n"
            "لا توجد مصادر مضافة بعد.\n\n"
            "اضغط <b>إضافة مصدر</b> لإضافة قناة يوتيوب كمصدر."
        )
    else:
        text = "📡 <b>مصادر الجلب</b>\n\n"
        for i, src in enumerate(sources, 1):
            status = "✅" if src.get("enabled") else "❌"
            settings = _source_settings(src)
            source_name = html.escape(src.get('source_name', 'مصدر'))
            content_type = html.escape(src.get('content_type', 'minecraft_mods'))
            source_url = html.escape(src.get('source_url', '')[:50])
            channel_id_short = html.escape(src.get('channel_id', '')[:15])
            facecam_status = html.escape(_facecam_status(settings))
            overlay_status = html.escape(_short_overlay_status(settings))
            intro_effect_status = html.escape(_video_effect_status(settings, "intro"))
            outro_effect_status = html.escape(_video_effect_status(settings, "outro"))
            text += (
                f"{i}. {status} <b>{source_name}</b>\n"
                f"   📦 <code>{content_type}</code>\n"
                f"   🔗 <code>{source_url}</code>\n"
                f"   📺 القناة: <code>{channel_id_short}...</code>\n"
                f"   🎬 Facecam: <code>{facecam_status}</code>\n"
                f"   📝 النص: <code>{overlay_status}</code>\n"
                f"   ✨ البداية: <code>{intro_effect_status}</code>\n"
                f"   🏁 النهاية: <code>{outro_effect_status}</code>\n\n"
            )

    keyboard = [
        [InlineKeyboardButton("➕ إضافة مصدر", callback_data="am_add_source")],
    ]

    # أزرار حذف/تبديل/تعديل لكل مصدر
    for i, src in enumerate(sources[:8]):
        src_id = src.get("id", "")
        enabled = src.get("enabled", True)
        toggle = "⏸" if enabled else "▶️"
        keyboard.append([
            InlineKeyboardButton(f"{toggle} {src.get('source_name', '')[:15]}", callback_data=f"am_toggle_src:{src_id}"),
            InlineKeyboardButton("✏️", callback_data=f"am_edit_src:{src_id}"),
            InlineKeyboardButton("🗑", callback_data=f"am_del_src:{src_id}"),
        ])

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")])

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_SOURCES


async def edit_shorts_only_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل setting shorts_only"""
    query = update.callback_query
    await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        await query.answer("❌ خطأ: لم يتم العثور على المصدر", show_alert=True)
        return await sources_menu(update, context)

    db = _get_db()
    settings = _source_settings(src)
    new_value = not bool(settings.get("shorts_only"))
    await asyncio.to_thread(db.update_source_settings, src.get("id"), {"shorts_only": new_value})
    await query.answer("✅ تم التبديل")

    return await _show_edit_source_menu(update, context)


async def toggle_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل حالة مصدر"""
    query = update.callback_query
    await _safe_answer(query)
    src_id = query.data.split(":", 1)[1]

    db = _get_db()
    sources = await asyncio.to_thread(db.get_sources)
    src = next((s for s in sources if s.get("id") == src_id), None)
    if src:
        await asyncio.to_thread(db.toggle_source, src_id, not src.get("enabled", True))
        await query.answer("✅ تم التبديل")

    return await sources_menu(update, context)


async def delete_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف مصدر"""
    query = update.callback_query
    await _safe_answer(query)
    src_id = query.data.split(":", 1)[1]

    db = _get_db()
    source = next((item for item in (await asyncio.to_thread(db.get_sources)) if item.get("id") == src_id), None)
    success = await asyncio.to_thread(db.remove_source, src_id)
    if success:
        await asyncio.to_thread(_cleanup_source_facecam_storage, source or {"id": src_id})
        await query.answer("🗑 تم الحذف")
    else:
        await query.answer("❌ تعذر حذف المصدر")

    return await sources_menu(update, context)


# ==================== تعديل قناة مصدر ====================

async def edit_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء تعديل مصدر - اختيار نوع التعديل"""
    query = update.callback_query
    await _safe_answer(query)
    src_id = query.data.split(":", 1)[1]

    return await _show_edit_source_menu(update, context, src_id=src_id)


async def _get_edit_source(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, Any]]:
    src_id = context.user_data.get("am_edit_source_id")
    if not src_id:
        return None
    db = _get_db()
    sources = await asyncio.to_thread(db.get_sources)
    return next((s for s in sources if s.get("id") == src_id), None)


async def _show_edit_source_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, src_id: Optional[str] = None):
    query = update.callback_query
    if src_id:
        context.user_data["am_edit_source_id"] = src_id
    context.user_data.pop("am_facecam_upload_mode", None)
    src = await _get_edit_source(context)
    if not src:
        if query:
            await query.answer("❌ خطأ: لم يتم العثور على المصدر", show_alert=True)
        return await sources_menu(update, context)

    src_name = src.get("source_name", "مصدر")
    platform = src.get("platform", "youtube")
    platform_map = {
        "youtube_long": "طويلة فقط (عادية)",
        "youtube_shorts": "شورتس فقط",
        "youtube_any": "أي نوع",
        "youtube": "يوتيوب (افتراضي)",
        "facebook_long": "فيسبوك طويلة",
        "facebook_reels": "فيسبوك ريلز",
        "facebook_any": "فيسبوك أي نوع",
        "container": "حاوية داخلية",
    }
    dur_label = platform_map.get(platform, platform)

    settings = _source_settings(src)
    fc_label = _facecam_status(settings)
    overlay_status = _short_overlay_status(settings)
    desc_status = _short_desc_status(settings)
    raw_review_status = _raw_review_status(settings)
    tail_trim_status = _tail_trim_status(settings)
    intro_effect_status = _video_effect_status(settings, "intro")
    outro_effect_status = _video_effect_status(settings, "outro")
    hflip_status = _source_hflip_status(settings)
    privacy_status = _source_privacy_status(settings)
    shorts_only_status = _source_shorts_only_status(settings)
    fetch_sources_status = _fetch_sources_status_for_ui(src)

    text = (
        f"✏️ <b>تعديل المصدر:</b> <code>{html.escape(src_name)}</code>\n\n"
        f"نوع الفيديوهات: <code>{html.escape(dur_label)}</code>\n"
        f"📥 قنوات الجلب: <code>{html.escape(fetch_sources_status)}</code>\n"
        f"🔒 خصوصية النشر: <code>{html.escape(privacy_status)}</code>\n"
        f"🎬 فيس كام: <code>{html.escape(fc_label)}</code>\n"
        f"📝 نص داخل الشورتس: <code>{html.escape(overlay_status)}</code>\n"
        f"📄 نص إضافي في الوصف: <code>{html.escape(desc_status)}</code>\n"
        f"↔️ قلب الفيديو: <code>{html.escape(hflip_status)}</code>\n"
        f"✂️ قص النهاية: <code>{html.escape(tail_trim_status)}</code>\n"
        f"✨ تأثير البداية: <code>{html.escape(intro_effect_status)}</code>\n"
        f"🏁 تأثير النهاية: <code>{html.escape(outro_effect_status)}</code>\n"
        f"🧪 مراجعة الخام: <code>{html.escape(raw_review_status)}</code>\n"
        f"📹 شورتس فقط: <code>{html.escape(shorts_only_status)}</code>\n\n"
        "ماذا تود تعديله؟"
    )

    keyboard = [
        [InlineKeyboardButton("📺 تغيير القناة المستهدفة", callback_data="am_edit_ch_start")],
        [InlineKeyboardButton(f"📥 قنوات الجلب: {fetch_sources_status}", callback_data="am_edit_fetch_menu")],
        [InlineKeyboardButton("⏳ تغيير نوع الفيديوهات (المدة)", callback_data="am_edit_dur_start")],
        [InlineKeyboardButton(f"🔒 خصوصية النشر: {privacy_status}", callback_data="am_edit_priv_menu")],
        [InlineKeyboardButton(f"🎬 فيس كام: {fc_label}", callback_data="am_edit_fc_start")],
        [InlineKeyboardButton(f"📝 إدارة نص الشورتس: {overlay_status}", callback_data="am_edit_ov_menu")],
        [InlineKeyboardButton(f"📄 إدارة نص الوصف: {desc_status}", callback_data="am_edit_desc_menu")],
        [InlineKeyboardButton(f"↔️ قلب الفيديو: {hflip_status}", callback_data="am_edit_hflip_menu")],
        [InlineKeyboardButton(f"✂️ قص النهاية: {tail_trim_status}", callback_data="am_edit_trim_menu")],
        [InlineKeyboardButton(f"✨ تأثير البداية: {intro_effect_status}", callback_data="am_edit_fx_menu:intro")],
        [InlineKeyboardButton(f"🏁 تأثير النهاية: {outro_effect_status}", callback_data="am_edit_fx_menu:outro")],
        [InlineKeyboardButton(f"🧪 مراجعة الخام: {raw_review_status}", callback_data="am_edit_raw_toggle")],
        [InlineKeyboardButton(f"📹 شورتس فقط: {shorts_only_status}", callback_data="am_edit_shorts_only_toggle")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_edit_source_menu(update, context)


async def edit_source_fetch_sources_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    items = _fetch_sources_for_ui(src)
    src_name = src.get("source_name", "مصدر")

    text = f"📥 <b>قنوات الجلب</b>\nللمصدر: <code>{html.escape(src_name)}</code>\n\n"
    if not items:
        text += "الحالة: <code>افتراضي (رابط واحد)</code>\n\n"
    else:
        enabled_count = sum(1 for x in items if bool((x or {}).get("enabled", True)))
        text += f"الحالة: <code>{enabled_count}/{len(items)} فعّالة</code>\n\n"
        for i, it in enumerate(items, start=1):
            url = str((it or {}).get("url") or "").strip()
            name = str((it or {}).get("name") or f"قناة {i}").strip() or f"قناة {i}"
            en = bool((it or {}).get("enabled", True))
            icon = "✅" if en else "❌"
            text += f"{i}. {icon} <code>{html.escape(name)}</code>\n<code>{html.escape(url[:140])}</code>\n\n"

    keyboard: List[List[InlineKeyboardButton]] = []
    for idx, it in enumerate(items):
        en = bool((it or {}).get("enabled", True))
        toggle_label = "✅ تفعيل" if not en else "⏸ تعطيل"
        keyboard.append([
            InlineKeyboardButton(toggle_label, callback_data=f"am_edit_fetch_toggle:{idx}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"am_edit_fetch_del:{idx}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ إضافة قناة جلب", callback_data="am_edit_fetch_add")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")])

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_fetch_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    context.user_data["am_text_input_mode"] = "edit_fetch_add"
    text = (
        "➕ <b>إضافة قناة جلب</b>\n\n"
        "أرسل رابط واحد فقط (قناة / قائمة تشغيل / رابط فيديو).\n"
        "يجب أن يبدأ بـ <code>http</code>.\n\n"
        "🔙 للرجوع: اضغط رجوع من القائمة السابقة."
    )
    if query:
        await query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, parse_mode="HTML")
    return AM_SOURCE_TEXT_INPUT


async def edit_source_fetch_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    raw = (query.data or "")
    idx_str = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        idx = int(idx_str)
    except Exception:
        return await edit_source_fetch_sources_menu(update, context)

    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    items = _fetch_sources_for_ui(src)
    if idx < 0 or idx >= len(items):
        return await edit_source_fetch_sources_menu(update, context)

    current = dict(items[idx] or {})
    current["enabled"] = not bool(current.get("enabled", True))
    items[idx] = current

    success = await _update_edit_source_settings(context, {"fetch_sources": items})
    if query:
        await query.answer("✅ تم التحديث" if success else "❌ تعذر التحديث", show_alert=not success)
    return await edit_source_fetch_sources_menu(update, context)


async def edit_source_fetch_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    raw = (query.data or "")
    idx_str = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        idx = int(idx_str)
    except Exception:
        return await edit_source_fetch_sources_menu(update, context)

    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    items = _fetch_sources_for_ui(src)
    if idx < 0 or idx >= len(items):
        return await edit_source_fetch_sources_menu(update, context)

    items.pop(idx)
    success = await _update_edit_source_settings(context, {"fetch_sources": items})
    if query:
        await query.answer("🗑 تم الحذف" if success else "❌ تعذر الحذف", show_alert=not success)
    return await edit_source_fetch_sources_menu(update, context)


async def _show_tail_trim_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    settings = _source_settings(src)
    trim_status = _tail_trim_status(settings)
    text = (
        f"✂️ <b>إدارة قص نهاية الفيديو</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"الحالة الحالية: <code>{html.escape(trim_status)}</code>\n\n"
        "سيتم تطبيق هذا القص مباشرة بعد تنزيل كل فيديو من هذا المصدر، وقبل أي معالجة أخرى."
    )
    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for idx, seconds in enumerate(TAIL_TRIM_OPTIONS, start=1):
        row.append(InlineKeyboardButton(f"{_seconds_label(seconds)} ثانية", callback_data=f"am_edit_trim:{seconds}"))
        if idx % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬜ بدون قص", callback_data="am_edit_trim:off")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")])

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def _show_edit_source_privacy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    privacy_status = _source_privacy_status(settings)
    text = (
        f"🔒 <b>خصوصية نشر الفيديوهات</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"الحالة الحالية: <code>{html.escape(privacy_status)}</code>\n\n"
        "اختر الخصوصية التي ستُستخدم عند رفع الفيديوهات من هذا المصدر."
    )
    keyboard = [
        [InlineKeyboardButton("🌍 علني (Public)", callback_data="am_edit_priv:public")],
        [InlineKeyboardButton("🔗 غير مدرج (Unlisted)", callback_data="am_edit_priv:unlisted")],
        [InlineKeyboardButton("🔒 خاص (Private)", callback_data="am_edit_priv:private")],
        [InlineKeyboardButton("⚙️ حسب خصوصية القناة", callback_data="am_edit_priv:default")],
        [InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")],
    ]
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_privacy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_edit_source_privacy_menu(update, context)


async def edit_source_privacy_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    value = (query.data.split(":", 1)[1] if ":" in (query.data or "") else "").strip().lower()
    if value not in {"public", "private", "unlisted", "default"}:
        return await _show_edit_source_privacy_menu(update, context)
    target_value = None if value == "default" else value
    success = await _update_edit_source_settings(context, {"privacy": target_value})
    await query.answer("✅ تم تحديث الخصوصية" if success else "❌ تعذر تحديث الخصوصية", show_alert=True)
    return await _show_edit_source_menu(update, context)


async def _show_edit_source_hflip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    hflip_status = _source_hflip_status(settings)
    text = (
        f"↔️ <b>إعداد قلب الفيديو أفقيًا (Mirror)</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"الحالة الحالية: <code>{html.escape(hflip_status)}</code>\n\n"
        "اختر طريقة تطبيق القلب لهذا المصدر."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل للمصدر", callback_data="am_edit_hflip:on")],
        [InlineKeyboardButton("❌ تعطيل للمصدر", callback_data="am_edit_hflip:off")],
        [InlineKeyboardButton("⚙️ حسب الإعدادات العامة", callback_data="am_edit_hflip:default")],
        [InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")],
    ]
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_hflip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_edit_source_hflip_menu(update, context)


async def edit_source_hflip_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = (query.data.split(":", 1)[1] if ":" in (query.data or "") else "").strip().lower()
    if choice == "on":
        success = await _update_edit_source_settings(context, {"hflip": True})
    elif choice == "off":
        success = await _update_edit_source_settings(context, {"hflip": False})
    elif choice == "default":
        success = await _update_edit_source_settings(context, {"hflip": None})
    else:
        return await _show_edit_source_hflip_menu(update, context)
    await query.answer("✅ تم تحديث إعداد قلب الفيديو" if success else "❌ تعذر تحديث إعداد قلب الفيديو", show_alert=True)
    return await _show_edit_source_menu(update, context)


async def _show_video_effect_editor(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    settings = _source_settings(src)
    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    target_label = _video_effect_target_label(target_key)
    status = _video_effect_status(settings, target_key)
    text = (
        f"✨ <b>إدارة تأثير {target_label} الفيديو</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"الحالة الحالية: <code>{html.escape(status)}</code>\n\n"
        f"اختر نوع التأثير المتحرك الذي سيُطبَّق عند <b>{target_label}</b> الفيديو لهذا المصدر."
    )
    keyboard = [
        [InlineKeyboardButton("⬜ بدون تأثير", callback_data=f"am_edit_fx_kind:{target_key}:none")],
        [InlineKeyboardButton("🌫 Blur عادي", callback_data=f"am_edit_fx_kind:{target_key}:blur")],
        [InlineKeyboardButton("⬛ Black Blur", callback_data=f"am_edit_fx_kind:{target_key}:black_blur")],
        [InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")],
    ]

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def _show_video_effect_duration_editor(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, effect_type: str):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    target_label = _video_effect_target_label(target_key)
    type_label = _video_effect_type_label(effect_type)
    text = (
        f"⏱ <b>مدة تأثير {target_label}</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"النوع المختار: <code>{html.escape(type_label)}</code>\n\n"
        "اختر المدة المطلوبة:"
    )
    keyboard = [[InlineKeyboardButton(f"{_seconds_label(val)} ثانية", callback_data=f"am_edit_fx_dur:{target_key}:{effect_type}:{val}")] for val in VIDEO_EFFECT_DURATION_OPTIONS]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"am_edit_fx_menu:{target_key}")])

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_tail_trim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_tail_trim_editor(update, context)


async def edit_source_video_effect_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    target = (query.data.split(":", 1)[1] if query and ":" in query.data else "intro").strip().lower()
    return await _show_video_effect_editor(update, context, target)


async def edit_source_video_effect_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return await _show_edit_source_menu(update, context)
    _, target, effect_type = parts
    target = "intro" if target == "intro" else "outro"

    if effect_type == "none":
        success = await _update_edit_source_settings(context, {"video_effects": {target: _build_video_effect_config("none")}})
        await query.answer("✅ تم تعطيل التأثير" if success else "❌ تعذر تحديث التأثير", show_alert=True)
        return await _show_edit_source_menu(update, context)

    return await _show_video_effect_duration_editor(update, context, target, effect_type)


async def edit_source_video_effect_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        return await _show_edit_source_menu(update, context)
    _, target, effect_type, raw_duration = parts
    target = "intro" if target == "intro" else "outro"
    try:
        duration = float(raw_duration)
    except Exception:
        await query.answer("❌ مدة التأثير غير صالحة", show_alert=True)
        return await _show_video_effect_duration_editor(update, context, target, effect_type)

    success = await _update_edit_source_settings(context, {"video_effects": {target: _build_video_effect_config(effect_type, duration)}})
    await query.answer("✅ تم تحديث التأثير" if success else "❌ تعذر تحديث التأثير", show_alert=True)
    return await _show_edit_source_menu(update, context)


async def edit_source_tail_trim_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]

    if choice == "off":
        success = await _update_edit_source_settings(context, {"tail_trim": {"enabled": False, "seconds": 0.0}})
    else:
        try:
            seconds = float(choice)
        except Exception:
            await query.answer("❌ قيمة القص غير صالحة", show_alert=True)
            return await _show_tail_trim_editor(update, context)
        success = await _update_edit_source_settings(context, {"tail_trim": {"enabled": True, "seconds": seconds}})

    await query.answer("✅ تم تحديث قص النهاية" if success else "❌ تعذر تحديث قص النهاية", show_alert=True)
    return await _show_edit_source_menu(update, context)


async def _show_overlay_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    settings = _source_settings(src)
    overlay = settings.get("shorts_overlay") or {}
    texts = overlay.get("texts") or []
    text = (
        f"📝 <b>إدارة النص داخل فيديو الشورتس</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"{_overlay_details(settings)}\n\n"
        "يمكنك إضافة عدة أسطر، وسيتم اختيارها ثابتًا أو عشوائيًا حسب الإعداد."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل" if not overlay.get("enabled") else "⏸ تعطيل", callback_data="am_edit_ov_toggle")],
        [InlineKeyboardButton("✍️ إضافة/استبدال النصوص", callback_data="am_edit_ov_text")],
        [
            InlineKeyboardButton("ثابت", callback_data="am_edit_ov_mode:fixed"),
            InlineKeyboardButton("عشوائي", callback_data="am_edit_ov_mode:random"),
        ],
        [
            InlineKeyboardButton("البداية", callback_data="am_edit_ov_time:start"),
            InlineKeyboardButton("النهاية", callback_data="am_edit_ov_time:end"),
            InlineKeyboardButton("كامل", callback_data="am_edit_ov_time:full"),
        ],
        [
            InlineKeyboardButton("1ث", callback_data="am_edit_ov_dur:1.0"),
            InlineKeyboardButton("1.5ث", callback_data="am_edit_ov_dur:1.5"),
            InlineKeyboardButton("2ث", callback_data="am_edit_ov_dur:2.0"),
            InlineKeyboardButton("3ث", callback_data="am_edit_ov_dur:3.0"),
        ],
        [
            InlineKeyboardButton("أعلى", callback_data="am_edit_ov_pos:top"),
            InlineKeyboardButton("وسط", callback_data="am_edit_ov_pos:center"),
            InlineKeyboardButton("أسفل", callback_data="am_edit_ov_pos:bottom"),
        ],
        [
            InlineKeyboardButton(
                f"ظهور: {_overlay_animation_type_label((overlay.get('intro_animation') or {}).get('type'))}",
                callback_data="am_edit_ov_anim_menu:intro",
            ),
            InlineKeyboardButton(
                f"اختفاء: {_overlay_animation_type_label((overlay.get('outro_animation') or {}).get('type'))}",
                callback_data="am_edit_ov_anim_menu:outro",
            ),
        ],
    ]
    for idx, item in enumerate(texts[:5]):
        label = item.replace("\n", " ")[:24]
        keyboard.append([InlineKeyboardButton(f"🗑 حذف: {label}", callback_data=f"am_edit_ov_del:{idx}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")])
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def _show_description_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)

    settings = _source_settings(src)
    desc = settings.get("extra_description") or {}
    texts = desc.get("texts") or []
    text = (
        f"📄 <b>إدارة النص الإضافي داخل الوصف</b>\n"
        f"للمصدر: <code>{html.escape(src.get('source_name', 'مصدر'))}</code>\n\n"
        f"{_description_details(settings)}\n\n"
        "إذا أرسلت عدة فقرات، افصل بينها بسطر يحوي <code>---</code> فقط."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل" if not desc.get("enabled") else "⏸ تعطيل", callback_data="am_edit_desc_toggle")],
        [InlineKeyboardButton("✍️ إضافة/استبدال النصوص", callback_data="am_edit_desc_text")],
        [
            InlineKeyboardButton("ثابت", callback_data="am_edit_desc_mode:fixed"),
            InlineKeyboardButton("عشوائي", callback_data="am_edit_desc_mode:random"),
        ],
        [
            InlineKeyboardButton("قبل الوصف", callback_data="am_edit_desc_place:prepend"),
            InlineKeyboardButton("بعد الوصف", callback_data="am_edit_desc_place:append"),
        ],
    ]
    for idx, item in enumerate(texts[:5]):
        label = item.replace("\n", " ")[:24]
        keyboard.append([InlineKeyboardButton(f"🗑 حذف: {label}", callback_data=f"am_edit_desc_del:{idx}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع للمصدر", callback_data="am_edit_src_menu")])
    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception as e:
            # Ignore "Message is not modified" error
            if "Message is not modified" not in str(e):
                logger.error(f"Error editing message: {e}")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL

async def edit_source_channel_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة القنوات لتعديل المصدر"""
    query = update.callback_query
    await _safe_answer(query)
    src_id = context.user_data.get("am_edit_source_id")
    
    if not src_id:
        return await sources_menu(update, context)
        
    db = _get_db()
    sources = await asyncio.to_thread(db.get_sources)
    src = next((s for s in sources if s.get("id") == src_id), None)
    src_name = src.get("source_name", "مصدر") if src else "مصدر"
    current_ch = src.get("channel_id", "") if src else ""

    try:
        from ..channel_manager import ChannelManager
        cm = ChannelManager()
        channels, total = await asyncio.to_thread(cm.list_channels, enabled_only=True, limit=50)

        if total == 0:
            text = "❌ لا توجد قنوات مفعلة."
            keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return AM_SOURCES

        text = (
            f"✏️ <b>تعديل قناة المصدر:</b> {html.escape(src_name)}\n\n"
            f"القناة الحالية: <code>{html.escape(current_ch[:20])}...</code>\n\n"
            "اختر القناة الجديدة:"
        )
        keyboard = []
        for ch in channels:
            icon = "✅" if ch.channel_id == current_ch else "📺"
            label = f"{icon} {ch.channel_name[:30]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"am_edit_ch:{ch.channel_id}")])

        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_EDIT_SOURCE_CHANNEL

    except Exception as e:
        logger.error(f"Error listing channels for edit: {e}")
        text = f"❌ خطأ: <code>{html.escape(str(e)[:100])}</code>"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_SOURCES


async def edit_source_choose_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تطبيق تغيير القناة المستهدفة"""
    query = update.callback_query
    await _safe_answer(query)

    new_channel_id = query.data.split(":", 1)[1]
    src_id = context.user_data.get("am_edit_source_id", "")

    if not src_id:
        await query.answer("❌ خطأ: لم يتم العثور على المصدر", show_alert=True)
        return await sources_menu(update, context)

    db = _get_db()
    success = await asyncio.to_thread(db.update_source_channel, src_id, new_channel_id)

    # الحصول على اسم القناة الجديدة
    ch_name = new_channel_id[:15]
    try:
        from ..channel_manager import ChannelManager
        cm = ChannelManager()
        channels, _ = await asyncio.to_thread(cm.list_channels, enabled_only=True, limit=50)
        ch_obj = next((c for c in channels if c.channel_id == new_channel_id), None)
        if ch_obj:
            ch_name = ch_obj.channel_name
    except Exception:
        pass

    if success:
        await query.answer(f"✅ تم تغيير القناة إلى: {ch_name}", show_alert=True)
    else:
        await query.answer("❌ فشل في تحديث القناة", show_alert=True)

    # تنظيف
    context.user_data.pop("am_edit_source_id", None)

    return await sources_menu(update, context)

async def edit_source_duration_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء تعديل نوع الفيديوهات (المدة)"""
    query = update.callback_query
    await _safe_answer(query)
    
    src_id = context.user_data.get("am_edit_source_id")
    if not src_id:
        return await sources_menu(update, context)
        
    db = _get_db()
    sources = db.get_sources()
    src = next((s for s in sources if s.get("id") == src_id), None)
    src_name = src.get("source_name", "مصدر") if src else "مصدر"

    text = (
        f"⏳ <b>تعديل نوع الفيديوهات للمصدر:</b> <code>{html.escape(src_name)}</code>\n\n"
        "اختر النوع الجديد:"
    )
    
    keyboard = [
        [InlineKeyboardButton("🎬 طويلة فقط", callback_data="am_set_dur:youtube_long")],
        [InlineKeyboardButton("📱 شورتس فقط", callback_data="am_set_dur:youtube_shorts")],
        [InlineKeyboardButton("🔄 أي نوع", callback_data="am_set_dur:youtube_any")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL

async def edit_source_choose_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تطبيق تغيير نوع الفيديوهات"""
    query = update.callback_query
    await _safe_answer(query)
    
    new_platform = query.data.split(":", 1)[1]
    src_id = context.user_data.get("am_edit_source_id", "")

    if not src_id:
        await query.answer("❌ خطأ: لم يتم العثور على المصدر", show_alert=True)
        return await sources_menu(update, context)

    db = _get_db()
    success = db.update_source_platform(src_id, new_platform)

    platform_map = {
        "youtube_long": "طويلة فقط",
        "youtube_shorts": "شورتس فقط",
        "youtube_any": "أي نوع"
    }

    if success:
        await query.answer(f"✅ تم التغيير إلى: {platform_map.get(new_platform, new_platform)}", show_alert=True)
    else:
        await query.answer("❌ فشل تغيير نوع الفيديوهات", show_alert=True)

    context.user_data.pop("am_edit_source_id", None)
    return await sources_menu(update, context)


def _build_facecam_menu_keyboard(settings: Dict[str, Any], *, mode: str) -> InlineKeyboardMarkup:
    prefix = "am_edit_fc_manage:" if mode == "edit" else "am_src_fc_manage:"
    back_callback = "am_edit_src_menu" if mode == "edit" else "am_sources"
    done_callback = "am_edit_src_menu" if mode == "edit" else "am_src_fc_done"
    facecam = _facecam_config(settings)
    clips = _facecam_clips(settings)
    keyboard: List[List[InlineKeyboardButton]] = []

    if mode == "edit":
        enabled = bool(facecam.get("enabled"))
        keyboard.append([
            InlineKeyboardButton("⬜ تعطيل" if enabled else "✅ تفعيل", callback_data=f"{prefix}toggle:{'off' if enabled else 'on'}")
        ])
    else:
        keyboard.append([InlineKeyboardButton("🚫 تعطيل الفيس كام", callback_data=f"{prefix}disable")])

    keyboard.append([
        InlineKeyboardButton("⬆️ دائري أعلى", callback_data=f"{prefix}pos:top_center"),
        InlineKeyboardButton("⬇️ دائري أسفل", callback_data=f"{prefix}pos:bottom_center"),
    ])
    keyboard.append([
        InlineKeyboardButton("↖️ دائرة صغيرة أعلى اليسار", callback_data=f"{prefix}pos:small_circle_top_left"),
        InlineKeyboardButton("↗️ دائرة صغيرة أعلى اليمين", callback_data=f"{prefix}pos:small_circle_top_right"),
    ])
    keyboard.append([
        InlineKeyboardButton("↘️ دائرة صغيرة أسفل اليمين", callback_data=f"{prefix}pos:small_circle_bottom_right"),
        InlineKeyboardButton("↙️ دائرة صغيرة أسفل اليسار", callback_data=f"{prefix}pos:small_circle_bottom_left"),
    ])
    keyboard.append([InlineKeyboardButton("📤 إضافة / رفع مقطع جديد", callback_data=f"{prefix}upload")])

    for clip in clips[:5]:
        clip_name = _truncate_facecam_clip_name(clip.get("name"))
        keyboard.append([InlineKeyboardButton(f"🗑 حذف {clip_name}", callback_data=f"{prefix}del:{clip.get('id')}")])

    keyboard.append([InlineKeyboardButton("✅ تم" if mode == "edit" else "➡️ متابعة", callback_data=done_callback)])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


async def _show_add_facecam_manager(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: Optional[str] = None):
    settings = _draft_source_settings(context)
    text = (
        "🎬 <b>مكتبة Facecam للمصدر الجديد</b>\n\n"
        f"{_facecam_details(settings)}\n\n"
        "أرسل الآن فيديو أو صورة لإضافتها إلى هذا المصدر. سيتم تحويل الصورة تلقائيًا إلى فيديو متوافق، ويمكنك رفع أكثر من مقطع، "
        "وسيتم اختيار مقطع عشوائيًا أثناء المعالجة."
    )
    if notice:
        text += f"\n\n{notice}"
    markup = _build_facecam_menu_keyboard(settings, mode="add")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return AM_ADD_SOURCE_FACECAM


async def _show_edit_facecam_manager(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: Optional[str] = None):
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    src_name = src.get("source_name", "مصدر")
    text = (
        f"🎬 <b>إدارة Facecam للمصدر:</b> <code>{html.escape(src_name)}</code>\n\n"
        f"{_facecam_details(settings)}\n\n"
        "يمكنك من هنا تفعيل/تعطيل الـ Facecam، تغيير الوضعية، وإضافة أو حذف المقاطع الخاصة بهذا المصدر."
    )
    if notice:
        text += f"\n\n{notice}"
    markup = _build_facecam_menu_keyboard(settings, mode="edit")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL


async def edit_source_facecam_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["am_facecam_upload_mode"] = "edit"
    return await _show_edit_facecam_manager(update, context)


async def edit_source_choose_facecam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = (query.data.split(":", 1)[1] if query and query.data else "").strip().lower()
    if choice == "yes":
        return await _update_edit_facecam_setting(update, context, {"enabled": True}, notice="✅ تم تفعيل الفيس كام.")
    if choice == "no":
        return await _update_edit_facecam_setting(update, context, {"enabled": False}, notice="✅ تم تعطيل الفيس كام.")
    return await _show_edit_facecam_manager(update, context)


async def edit_source_choose_facecam_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    position = (query.data.split(":", 1)[1] if query and query.data else "top_center").strip().lower()
    src = await _get_edit_source(context)
    current_settings = _source_settings(src) if src else {}
    return await _update_edit_facecam_setting(
        update,
        context,
        _build_facecam_settings(position, _facecam_clips(current_settings), enabled=True),
        notice="✅ تم تحديث وضعية الفيس كام.",
    )


async def _update_edit_facecam_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, facecam_update: Dict[str, Any], *, notice: Optional[str] = None):
    success = await _update_edit_source_settings(context, {"facecam": facecam_update})
    if not success and update.callback_query:
        await update.callback_query.answer("❌ تعذر تحديث إعدادات الفيس كام", show_alert=True)
    return await _show_edit_facecam_manager(update, context, notice if success else "❌ تعذر تحديث إعدادات الفيس كام.")


async def edit_source_facecam_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    data = query.data.split(":", 2)
    action = data[1] if len(data) > 1 else ""
    value = data[2] if len(data) > 2 else ""

    if action == "toggle":
        return await _update_edit_facecam_setting(update, context, {"enabled": value == "on"}, notice="✅ تم تحديث حالة الفيس كام.")
    if action == "pos":
        src = await _get_edit_source(context)
        current_settings = _source_settings(src) if src else {}
        return await _update_edit_facecam_setting(
            update,
            context,
            _build_facecam_settings(value, _facecam_clips(current_settings), enabled=True),
            notice="✅ تم تحديث وضعية الفيس كام.",
        )
    if action == "upload":
        context.user_data["am_facecam_upload_mode"] = "edit"
        return await _show_edit_facecam_manager(update, context, "📤 أرسل الآن فيديو أو صورة Facecam وسيتم ربطها بهذا المصدر.")
    if action == "del":
        src = await _get_edit_source(context)
        if not src:
            return await sources_menu(update, context)
        settings = _source_settings(src)
        clips = _facecam_clips(settings)
        kept: List[Dict[str, Any]] = []
        removed: Optional[Dict[str, Any]] = None
        for clip in clips:
            if str(clip.get("id")) == value and removed is None:
                removed = clip
                continue
            kept.append(clip)
        if removed:
            _delete_facecam_clip_file(removed)
            current_facecam = _facecam_config(settings)
            selection = str(current_facecam.get("layout") or current_facecam.get("position") or "top_center")
            await _update_edit_source_settings(
                context,
                {"facecam": _build_facecam_settings(selection, kept, enabled=current_facecam.get("enabled", True))},
            )
            return await _show_edit_facecam_manager(update, context, "✅ تم حذف مقطع الفيس كام.")
        return await _show_edit_facecam_manager(update, context, "⚠️ لم يتم العثور على هذا المقطع.")
    return await _show_edit_facecam_manager(update, context)


async def edit_source_facecam_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("am_facecam_upload_mode") != "edit":
        return AM_EDIT_SOURCE_CHANNEL
    src_id = context.user_data.get("am_edit_source_id", "")
    if not src_id:
        return await sources_menu(update, context)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    clip_entry, error_message = await _download_facecam_clip(update, context, src_id)
    if error_message:
        await update.message.reply_text(error_message)
        return AM_EDIT_SOURCE_CHANNEL
    clips = _facecam_clips(settings)
    clips.append(clip_entry or {})
    current_facecam = _facecam_config(settings)
    selection = str(current_facecam.get("layout") or current_facecam.get("position") or "top_center")
    await _update_edit_source_settings(context, {"facecam": _build_facecam_settings(selection, clips, enabled=True)})
    return await _show_edit_facecam_manager(update, context, "✅ تم رفع مقطع/صورة Facecam جديد لهذا المصدر.")


async def edit_source_overlay_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_overlay_editor(update, context)


async def edit_source_description_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_description_editor(update, context)


async def _update_edit_source_settings(context: ContextTypes.DEFAULT_TYPE, settings_update: Dict[str, Any]) -> bool:
    src_id = context.user_data.get("am_edit_source_id", "")
    if not src_id:
        return False
    db = _get_db()
    return await asyncio.to_thread(db.update_source_settings, src_id, settings_update)


async def edit_source_raw_review_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    enabled = bool(settings.get("require_raw_review"))
    success = await _update_edit_source_settings(context, {"require_raw_review": not enabled})
    await query.answer("✅ تم تحديث مراجعة الخام" if success else "❌ تعذر التحديث", show_alert=True)
    return await _show_edit_source_menu(update, context)


async def edit_source_overlay_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    overlay = settings.get("shorts_overlay") or {}
    if not overlay.get("texts") and not overlay.get("enabled"):
        await query.answer("أضف نصوصًا أولًا قبل التفعيل", show_alert=True)
        return await _show_overlay_editor(update, context)
    success = await _update_edit_source_settings(context, {
        "shorts_overlay": {"enabled": not overlay.get("enabled", False)}
    })
    await query.answer("✅ تم تحديث حالة النص داخل الفيديو" if success else "❌ تعذر التحديث", show_alert=True)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    mode = query.data.split(":", 1)[1]
    success = await _update_edit_source_settings(context, {"shorts_overlay": {"selection_mode": mode}})
    await query.answer("✅ تم تحديث طريقة الاختيار" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    timing = query.data.split(":", 1)[1]
    success = await _update_edit_source_settings(context, {"shorts_overlay": {"timing": timing}})
    await query.answer("✅ تم تحديث التوقيت" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    duration = float(query.data.split(":", 1)[1])
    success = await _update_edit_source_settings(context, {"shorts_overlay": {"duration": duration}})
    await query.answer("✅ تم تحديث المدة" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    position = query.data.split(":", 1)[1]
    success = await _update_edit_source_settings(context, {"shorts_overlay": {"screen_position": position}})
    await query.answer("✅ تم تحديث الموضع" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_overlay_editor(update, context)


async def _ask_source_overlay_animation_kind(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, *, edit_mode: bool = False):
    query = update.callback_query
    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    title = _overlay_animation_target_label(target_key)
    prefix = "am_edit_ov_anim_kind" if edit_mode else "am_src_ov_anim_kind"
    back_callback = f"am_edit_ov_menu" if edit_mode else "am_add_overlay_menu"
    text = f"✨ <b>اختر أنيميشن {title} النص:</b>"
    keyboard = [
        [InlineKeyboardButton("بدون أنيميشن", callback_data=f"{prefix}:{target_key}:none")],
        [InlineKeyboardButton("Fade", callback_data=f"{prefix}:{target_key}:fade")],
        [InlineKeyboardButton("Blur", callback_data=f"{prefix}:{target_key}:blur")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=back_callback)],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL if edit_mode else AM_ADD_SOURCE_CUSTOMIZE


async def _ask_source_overlay_animation_duration(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, animation_type: str, *, edit_mode: bool = False):
    query = update.callback_query
    target_key = "intro" if (target or "intro").strip().lower() == "intro" else "outro"
    title = _overlay_animation_target_label(target_key)
    prefix = "am_edit_ov_anim_dur" if edit_mode else "am_src_ov_anim_dur"
    back_callback = f"am_edit_ov_anim_menu:{target_key}" if edit_mode else "am_add_overlay_menu"
    text = f"⏳ <b>اختر مدة أنيميشن {title} النص ({html.escape(_overlay_animation_type_label(animation_type))}):</b>"
    keyboard = [[InlineKeyboardButton(f"{_seconds_label(val)} ثانية", callback_data=f"{prefix}:{target_key}:{animation_type}:{val}")] for val in OVERLAY_ANIMATION_DURATION_OPTIONS]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_callback)])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_EDIT_SOURCE_CHANNEL if edit_mode else AM_ADD_SOURCE_CUSTOMIZE


async def edit_source_overlay_animation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    target = query.data.split(":", 1)[1]
    return await _ask_source_overlay_animation_kind(update, context, target, edit_mode=True)


async def edit_source_overlay_animation_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    _, target, animation_type = query.data.split(":", 2)
    target_key = "intro" if target == "intro" else "outro"
    if animation_type == "none":
        success = await _update_edit_source_settings(context, {"shorts_overlay": {f"{target_key}_animation": _build_overlay_animation_config("none", 0.0)}})
        await query.answer("✅ تم تعطيل الأنيميشن" if success else "❌ تعذر التحديث", show_alert=False)
        return await _show_overlay_editor(update, context)
    return await _ask_source_overlay_animation_duration(update, context, target_key, animation_type, edit_mode=True)


async def edit_source_overlay_animation_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    _, target, animation_type, raw_duration = query.data.split(":", 3)
    target_key = "intro" if target == "intro" else "outro"
    duration = float(raw_duration)
    success = await _update_edit_source_settings(context, {"shorts_overlay": {f"{target_key}_animation": _build_overlay_animation_config(animation_type, duration)}})
    await query.answer("✅ تم تحديث الأنيميشن" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    idx = _safe_int(query.data.split(":", 1)[1], -1)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    overlay = settings.get("shorts_overlay") or {}
    texts = list(overlay.get("texts") or [])
    if 0 <= idx < len(texts):
        texts.pop(idx)
    enabled = bool(texts) and overlay.get("enabled", False)
    success = await _update_edit_source_settings(context, {"shorts_overlay": {"texts": texts, "enabled": enabled}})
    await query.answer("✅ تم حذف النص" if success else "❌ تعذر الحذف", show_alert=False)
    return await _show_overlay_editor(update, context)


async def edit_source_overlay_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["am_text_input_mode"] = "edit_overlay_texts"
    text = (
        "✍️ <b>أرسل الآن نصوص الشورتس</b>\n\n"
        "- كل سطر = خيار مستقل\n"
        "- سيتم استبدال النصوص الحالية بهذه القائمة\n"
        "- مثال:\n<code>اشترك الآن\nحلقة اليوم\nأفضل مود اليوم</code>"
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_edit_ov_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SOURCE_TEXT_INPUT


async def edit_source_description_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    desc = settings.get("extra_description") or {}
    if not desc.get("texts") and not desc.get("enabled"):
        await query.answer("أضف نصوصًا أولًا قبل التفعيل", show_alert=True)
        return await _show_description_editor(update, context)
    success = await _update_edit_source_settings(context, {
        "extra_description": {"enabled": not desc.get("enabled", False)}
    })
    await query.answer("✅ تم تحديث النص الإضافي" if success else "❌ تعذر التحديث", show_alert=True)
    return await _show_description_editor(update, context)


async def edit_source_description_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    mode = query.data.split(":", 1)[1]
    success = await _update_edit_source_settings(context, {"extra_description": {"selection_mode": mode}})
    await query.answer("✅ تم تحديث طريقة الاختيار" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_description_editor(update, context)


async def edit_source_description_placement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    placement = query.data.split(":", 1)[1]
    success = await _update_edit_source_settings(context, {"extra_description": {"placement": placement}})
    await query.answer("✅ تم تحديث موضع النص داخل الوصف" if success else "❌ تعذر التحديث", show_alert=False)
    return await _show_description_editor(update, context)


async def edit_source_description_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    idx = _safe_int(query.data.split(":", 1)[1], -1)
    src = await _get_edit_source(context)
    if not src:
        return await sources_menu(update, context)
    settings = _source_settings(src)
    desc = settings.get("extra_description") or {}
    texts = list(desc.get("texts") or [])
    if 0 <= idx < len(texts):
        texts.pop(idx)
    enabled = bool(texts) and desc.get("enabled", False)
    success = await _update_edit_source_settings(context, {"extra_description": {"texts": texts, "enabled": enabled}})
    await query.answer("✅ تم حذف النص" if success else "❌ تعذر الحذف", show_alert=False)
    return await _show_description_editor(update, context)


async def edit_source_description_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["am_text_input_mode"] = "edit_desc_texts"
    text = (
        "✍️ <b>أرسل الآن النصوص الإضافية للوصف</b>\n\n"
        "- إذا أردت عدة فقرات، افصل بينها بسطر يحوي <code>---</code> فقط\n"
        "- سيتم استبدال النصوص الحالية بهذه القائمة"
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_edit_desc_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SOURCE_TEXT_INPUT


# ==================== إضافة مصدر جديد ====================

async def add_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء إضافة مصدر - اختيار القناة المستهدفة"""
    query = update.callback_query
    await _safe_answer(query)
    context.user_data.pop("am_facecam_upload_mode", None)

    try:
        from ..channel_manager import ChannelManager
        cm = ChannelManager()
        channels, total = cm.list_channels(enabled_only=True, limit=50)

        if total == 0:
            text = "❌ لا توجد قنوات مفعلة. أضف قناة يوتيوب أولاً."
            keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return AM_SOURCES

        text = "📺 <b>اختر القناة المستهدفة للنشر:</b>\n\nالفيديوهات المجلوبة سيتم نشرها على هذه القناة."
        keyboard = []
        for ch in channels:
            label = f"📺 {html.escape(ch.channel_name[:30])}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"am_src_ch:{ch.channel_id}")])

        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_ADD_SOURCE_CHANNEL

    except Exception as e:
        logger.error(f"Error listing channels: {e}")
        text = f"❌ خطأ: <code>{html.escape(str(e)[:100])}</code>"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_SOURCES


async def add_source_choose_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختيار القناة المستهدفة"""
    query = update.callback_query
    await _safe_answer(query)

    channel_id = query.data.split(":", 1)[1]
    context.user_data["am_new_source"] = {"channel_id": channel_id}

    # اختيار نوع المحتوى
    text = (
        "📦 <b>اختر نوع المحتوى:</b>\n\n"
        "اختر نوع المحتوى لهذا المصدر، أو اضغط <b>نوع جديد</b> لإضافة نوع مخصص."
    )

    # أنواع المحتوى المتاحة
    types = [
        ("minecraft_mods", "🎮 مودات ماين كرافت"),
        ("minecraft_builds", "🏗 بناء ماين كرافت"),
        ("minecraft_shaders", "🌈 شيدرز ماين كرافت"),
        ("gaming_clips", "🕹 مقاطع ألعاب"),
        ("tutorials", "📚 شروحات"),
    ]

    keyboard = []
    for type_id, type_name in types:
        keyboard.append([InlineKeyboardButton(type_name, callback_data=f"am_src_type:{type_id}")])

    keyboard.append([InlineKeyboardButton("➕ نوع جديد", callback_data="am_src_type_custom")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_TYPE


async def add_source_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختيار نوع المحتوى"""
    query = update.callback_query
    await _safe_answer(query)

    data = query.data

    if data == "am_src_type_custom":
        text = "📝 <b>أدخل اسم نوع المحتوى الجديد:</b>\n\nمثال: <code>roblox_mods</code>, <code>fortnite_clips</code>"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_ADD_CONTENT_TYPE_NAME

    content_type = data.split(":", 1)[1]
    context.user_data["am_new_source"]["content_type"] = content_type

    text = (
        "📍 <b>اختر طريقة المصدر:</b>\n\n"
        "• <b>YouTube</b>: جلب من قناة/قائمة تشغيل\n"
        "• <b>Facebook</b>: جلب من صفحة/حساب/فيديو\n"
        "• <b>Google Drive</b>: جلب من مجلد (Folder)\n"
        "• <b>قاعدة بيانات</b>: جلب من حاوية فيديو (Containers)\n\n"
        "سيتم بعد ذلك تحديد المصدر الذي يعتمد عليه البوت ضمن أتمتة الجلب."
    )
    keyboard = [
        [InlineKeyboardButton("▶️ YouTube", callback_data="am_src_kind:youtube")],
        [InlineKeyboardButton("📘 Facebook", callback_data="am_src_kind:facebook")],
        [InlineKeyboardButton("☁️ Google Drive", callback_data="am_src_kind:gdrive")],
        [InlineKeyboardButton("📦 قاعدة بيانات (حاويات)", callback_data="am_src_kind:container")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_KIND


async def add_source_custom_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إدخال نوع محتوى مخصص"""
    text = update.message.text.strip()
    if not text or len(text) < 2:
        await update.message.reply_text("❌ اسم قصير جدًا. أدخل اسمًا أطول.")
        return AM_ADD_CONTENT_TYPE_NAME

    # تنظيف الاسم
    import re
    clean_name = re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]", "_", text).strip("_").lower()
    if not clean_name:
        clean_name = text[:20]

    context.user_data["am_new_source"]["content_type"] = clean_name

    msg_text = (
        f"✅ تم حفظ نوع المحتوى: <code>{html.escape(clean_name)}</code>\n\n"
        "📍 <b>اختر طريقة المصدر:</b>\n\n"
        "• <b>YouTube</b>: جلب من قناة/قائمة تشغيل\n"
        "• <b>Facebook</b>: جلب من صفحة/حساب/فيديو\n"
        "• <b>Google Drive</b>: جلب من مجلد (Folder)\n"
        "• <b>قاعدة بيانات</b>: جلب من حاوية فيديو (Containers)"
    )
    keyboard = [
        [InlineKeyboardButton("▶️ YouTube", callback_data="am_src_kind:youtube")],
        [InlineKeyboardButton("📘 Facebook", callback_data="am_src_kind:facebook")],
        [InlineKeyboardButton("☁️ Google Drive", callback_data="am_src_kind:gdrive")],
        [InlineKeyboardButton("📦 قاعدة بيانات (حاويات)", callback_data="am_src_kind:container")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_KIND


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return default


async def add_source_choose_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)

    kind = (query.data.split(":", 1)[1] if query and query.data else "").strip().lower()
    if kind not in {"youtube", "container", "facebook", "gdrive"}:
        return AM_ADD_SOURCE_KIND

    context.user_data.setdefault("am_new_source", {})
    context.user_data["am_new_source"]["source_kind"] = kind

    if kind == "container":
        return await _show_container_picker(update, context, page=0)

    if kind == "gdrive":
        db = _get_db()
        cfg = None
        try:
            cfg = db.get_config()
        except Exception:
            cfg = None
        settings = (cfg or {}).get("settings") or {}
        gcfg = settings.get("google_drive") or {}
        token_json = gcfg.get("token_json")
        linked = isinstance(token_json, dict) and bool(token_json) and (
            bool(token_json.get("refresh_token")) or bool(token_json.get("token")) or bool(token_json.get("access_token"))
        )
        if not linked:
            text = (
                "❌ <b>Google Drive غير مربوط</b>\n\n"
                "قبل إضافة مصدر Google Drive يجب ربط الحساب من قائمة الإعدادات."
            )
            keyboard = [
                [InlineKeyboardButton("☁️ ربط Google Drive", callback_data="am_gdrive_connect")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            return AM_ADD_SOURCE_KIND

        text = (
            "☁️ <b>مصدر Google Drive</b>\n\n"
            "أرسل <b>Folder ID</b> (معرف المجلد) الذي تريد أن يجلب منه البوت الفيديوهات.\n\n"
            "💡 يمكنك الحصول عليه من رابط المجلد:\n"
            "<code>https://drive.google.com/drive/folders/&lt;FOLDER_ID&gt;</code>\n\n"
            "⚠️ يجب أن تكون قد ربطت Google Drive من قائمة الإعدادات أولاً."
        )
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        context.user_data.setdefault("am_new_source", {})["platform"] = "google_drive"
        context.user_data["am_new_source"]["awaiting_url"] = True
        return AM_ADD_SOURCE_URL

    if kind == "facebook":
        text = (
            "⏳ <b>اختر نوع فيديوهات فيس بوك التي تود جلبها:</b>\n\n"
            "🎬 <b>طويلة فقط</b>: فيديوهات عادية\n"
            "📱 <b>ريلز فقط</b>: فيديوهات قصيرة (≈ أقل من 60 ثانية)\n"
            "🔄 <b>أي نوع</b>: أي فيديو"
        )
        keyboard = [
            [InlineKeyboardButton("🎬 طويلة فقط", callback_data="am_src_dur:facebook_long")],
            [InlineKeyboardButton("📱 ريلز فقط", callback_data="am_src_dur:facebook_reels")],
            [InlineKeyboardButton("🔄 أي نوع", callback_data="am_src_dur:facebook_any")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_ADD_SOURCE_URL

    text = (
        "⏳ <b>اختر نوع الفيديوهات التي تود جلبها من هذا المصدر:</b>\n\n"
        "🎬 <b>طويلة فقط</b>: فيديوهات عادية (أكثر من 60 ثانية، وأقل من 30 دقيقة)\n"
        "📱 <b>شورتس فقط</b>: الفيديوهات القصيرة (أقل من 60 ثانية)\n"
        "🔄 <b>أي نوع</b>: أي فيديو يتم تنزيله بحسب الرابط (بحد أقصى 30 دقيقة)"
    )
    keyboard = [
        [InlineKeyboardButton("🎬 طويلة فقط", callback_data="am_src_dur:youtube_long")],
        [InlineKeyboardButton("📱 شورتس فقط", callback_data="am_src_dur:youtube_shorts")],
        [InlineKeyboardButton("🔄 أي نوع", callback_data="am_src_dur:youtube_any")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_URL


async def _show_container_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, *, page: int = 0):
    query = update.callback_query
    if query:
        await _safe_answer(query)

    containers: List[Dict[str, Any]] = []
    try:
        from ...agent.supabase_storage import list_video_containers
        from ...agent.supabase_client import is_online as _sb_online
        logger.info(f"Container picker: Supabase online={_sb_online()}")
        containers = await asyncio.to_thread(list_video_containers)
        logger.info(f"Container picker: fetched {len(containers)} containers")
    except Exception as e:
        logger.error(f"Error fetching video containers: {e}", exc_info=True)
        containers = []

    if not containers:
        text = (
            "⚠️ <b>لا توجد حاويات متاحة حالياً.</b>\n\n"
            "قم بإنشاء حاوية ورفع فيديوهات إليها أولاً، ثم أعد المحاولة."
        )
        keyboard = [
            [InlineKeyboardButton("🔄 تحديث", callback_data="am_cont_refresh")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
        ]
        if query:
            try:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            except Exception:
                pass
        else:
            await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_ADD_CONTAINER_SELECT

    per_page = 8
    total_pages = max(1, (len(containers) + per_page - 1) // per_page)
    page = max(0, min(_safe_int(page, 0), total_pages - 1))
    start = page * per_page
    current = containers[start:start + per_page]

    context.user_data.setdefault("am_new_source", {})
    context.user_data["am_new_source"]["containers_list"] = containers
    context.user_data["am_new_source"]["containers_page"] = page

    from datetime import datetime as _dt
    _ts = _dt.now().strftime("%H:%M:%S")
    text = (
        "📦 <b>اختر حاوية من قاعدة البيانات:</b>\n"
        "سيقوم AutoModBot بجلب الفيديوهات منها تلقائياً.\n\n"
        f"الصفحة: {page + 1}/{total_pages}  •  🕐 {_ts}"
    )
    keyboard: List[List[InlineKeyboardButton]] = []
    for c in current:
        cid = str(c.get("id") or "").strip()
        name = str(c.get("name") or "container").strip()
        if not cid:
            continue
        label = f"📦 {name[:28]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"am_cont_sel:{cid}")])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ سابق", callback_data=f"am_cont_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("تالي ➡️", callback_data=f"am_cont_page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔄 تحديث", callback_data="am_cont_refresh")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")])

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception as e:
            if "Message is not modified" in str(e):
                logger.debug("Container picker: message content unchanged, skipping edit")
            else:
                logger.error(f"Error editing container picker message: {e}")
                await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_CONTAINER_SELECT


async def add_source_choose_container(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    data = query.data or ""

    if data == "am_cont_refresh":
        context.user_data.setdefault("am_new_source", {})
        context.user_data["am_new_source"].pop("containers_list", None)
        return await _show_container_picker(update, context, page=0)

    if data.startswith("am_cont_page:"):
        page = data.split(":", 1)[1]
        return await _show_container_picker(update, context, page=_safe_int(page, 0))

    if not data.startswith("am_cont_sel:"):
        return AM_ADD_CONTAINER_SELECT

    cid = data.split(":", 1)[1].strip()
    if not cid:
        return AM_ADD_CONTAINER_SELECT

    context.user_data.setdefault("am_new_source", {})
    context.user_data["am_new_source"]["platform"] = "container"
    context.user_data["am_new_source"]["source_url"] = f"container:{cid}"
    context.user_data["am_new_source"]["facecam_settings"] = {"facecam": {"enabled": False}}
    return await add_source_choose_video_duration(update, context, preset_platform="container")


async def add_source_choose_video_duration(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_platform: Optional[str] = None):
    """اختيار طول الفيديو ثم الانتقال لطلب الرابط"""
    query = update.callback_query
    await _safe_answer(query)

    video_platform = preset_platform or query.data.split(":", 1)[1]
    context.user_data["am_new_source"]["platform"] = video_platform

    text = (
        "🔗 <b>اختر إعدادات الفيس كام:</b>\n\n"
        "هل تريد إضافة فيديو فيس كام (كاميرا الوجه) على الفيديوهات من هذا المصدر؟"
    )
    keyboard = [
        [InlineKeyboardButton("✅ نعم", callback_data="am_src_fc:yes"), InlineKeyboardButton("⬜ لا", callback_data="am_src_fc:no")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_FACECAM


async def add_source_choose_facecam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختيار إعدادات الفيس كام"""
    query = update.callback_query
    await _safe_answer(query)

    choice = query.data.split(":", 1)[1]
    
    if choice == "yes":
        # اختيار وضعية الفيس كام
        text = (
            "🎬 <b>اختر وضعية الفيس كام:</b>\n\n"
            "اختر طريقة عرض فيديو الفيس كام داخل الفيديو النهائي."
        )
        keyboard = [
            [InlineKeyboardButton("⬆️ دائري أعلى الفيديو", callback_data="am_src_fc_pos:top_center")],
            [InlineKeyboardButton("⬇️ دائري أسفل الفيديو", callback_data="am_src_fc_pos:bottom_center")],
            [InlineKeyboardButton("↖️ دائرة صغيرة أعلى اليسار", callback_data="am_src_fc_pos:small_circle_top_left")],
            [InlineKeyboardButton("↗️ دائرة صغيرة أعلى اليمين", callback_data="am_src_fc_pos:small_circle_top_right")],
            [InlineKeyboardButton("↘️ دائرة صغيرة أسفل اليمين", callback_data="am_src_fc_pos:small_circle_bottom_right")],
            [InlineKeyboardButton("↙️ دائرة صغيرة أسفل اليسار", callback_data="am_src_fc_pos:small_circle_bottom_left")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_ADD_SOURCE_FACECAM
    else:
        context.user_data.pop("am_facecam_upload_mode", None)
        _set_draft_facecam_settings(
            context,
            _build_facecam_settings("top_center", _facecam_clips(_draft_source_settings(context)), enabled=False),
        )
        return await add_source_overlay_start(update, context)


async def add_source_choose_facecam_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختيار موضع الفيس كام"""
    query = update.callback_query
    await _safe_answer(query)
    
    position = query.data.split(":", 1)[1]
    _ensure_draft_source_id(context)
    existing_settings = _draft_source_settings(context)
    context.user_data["am_facecam_upload_mode"] = "add"
    _set_draft_facecam_settings(context, _build_facecam_settings(position, _facecam_clips(existing_settings), enabled=True))
    return await _show_add_facecam_manager(update, context)


async def add_source_facecam_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    data = query.data.split(":", 2)
    action = data[1] if len(data) > 1 else ""
    value = data[2] if len(data) > 2 else ""
    settings = _draft_source_settings(context)

    if action == "upload":
        context.user_data["am_facecam_upload_mode"] = "add"
        return await _show_add_facecam_manager(update, context, "📤 أرسل الآن فيديو أو صورة Facecam لإضافتها إلى هذا المصدر.")
    if action == "pos":
        _set_draft_facecam_settings(context, _build_facecam_settings(value, _facecam_clips(settings), enabled=True))
        return await _show_add_facecam_manager(update, context, "✅ تم تحديث وضعية الفيس كام.")
    if action == "del":
        kept: List[Dict[str, Any]] = []
        removed: Optional[Dict[str, Any]] = None
        for clip in _facecam_clips(settings):
            if str(clip.get("id")) == value and removed is None:
                removed = clip
                continue
            kept.append(clip)
        if removed:
            _delete_facecam_clip_file(removed)
            current_facecam = _facecam_config(settings)
            selection = str(current_facecam.get("layout") or current_facecam.get("position") or "top_center")
            _set_draft_facecam_settings(context, _build_facecam_settings(selection, kept, enabled=current_facecam.get("enabled", True)))
            return await _show_add_facecam_manager(update, context, "✅ تم حذف مقطع الفيس كام من المسودة.")
        return await _show_add_facecam_manager(update, context, "⚠️ لم يتم العثور على هذا المقطع.")
    if action == "disable":
        context.user_data.pop("am_facecam_upload_mode", None)
        current_facecam = _facecam_config(settings)
        selection = str(current_facecam.get("layout") or current_facecam.get("position") or "top_center")
        _set_draft_facecam_settings(context, _build_facecam_settings(selection, _facecam_clips(settings), enabled=False))
        return await add_source_overlay_start(update, context)
    return await _show_add_facecam_manager(update, context)


async def add_source_facecam_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("am_facecam_upload_mode", None)
    return await add_source_overlay_start(update, context)


async def add_source_facecam_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("am_facecam_upload_mode") != "add":
        return AM_ADD_SOURCE_FACECAM
    source_id = _ensure_draft_source_id(context)
    settings = _draft_source_settings(context)
    clip_entry, error_message = await _download_facecam_clip(update, context, source_id)
    if error_message:
        await update.message.reply_text(error_message)
        return AM_ADD_SOURCE_FACECAM
    clips = _facecam_clips(settings)
    clips.append(clip_entry or {})
    current_facecam = _facecam_config(settings)
    selection = str(current_facecam.get("layout") or current_facecam.get("position") or "top_center")
    _set_draft_facecam_settings(context, _build_facecam_settings(selection, clips, enabled=True))
    return await _show_add_facecam_manager(update, context, "✅ تم رفع مقطع/صورة Facecam جديد إلى هذا المصدر.")


async def add_source_overlay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data.pop("am_facecam_upload_mode", None)
    settings = _draft_source_settings(context)
    overlay_status = _short_overlay_status(settings)
    text = (
        "📝 <b>إعداد النص داخل فيديو الشورتس</b>\n\n"
        f"الحالة الحالية: <code>{html.escape(overlay_status)}</code>\n\n"
        "يمكنك إضافة أكثر من نص ليظهر داخل الفيديو، مع اختيار ثابت أو عشوائي."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل وإضافة نصوص", callback_data="am_src_ov:on")],
        [InlineKeyboardButton("⬜ تخطي / تعطيل", callback_data="am_src_ov:off")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    await _safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_choose_overlay_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]
    if choice == "off":
        _update_draft_source_settings(context, {"shorts_overlay": {"enabled": False, "texts": []}})
        context.user_data.setdefault("am_new_source", {})["overlay_configured"] = True
        return await _continue_source_creation(update, context)

    context.user_data["am_text_input_mode"] = "add_overlay_texts"
    text = (
        "✍️ <b>أرسل الآن نصوص الشورتس</b>\n\n"
        "- كل سطر = خيار مختلف\n"
        "- سيتم اختيار أحدها لاحقًا حسب الوضع الذي ستحدده\n\n"
        "مثال:\n<code>اشترك الآن\nأفضل مود اليوم\nلا يفوتك هذا المشهد</code>"
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SOURCE_TEXT_INPUT


async def add_source_overlay_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    mode = query.data.split(":", 1)[1]
    _update_draft_source_settings(context, {"shorts_overlay": {"selection_mode": mode}})

    text = "⏱ <b>اختر توقيت ظهور النص داخل الفيديو:</b>"
    keyboard = [
        [InlineKeyboardButton("في البداية", callback_data="am_src_ov_time:start")],
        [InlineKeyboardButton("في النهاية", callback_data="am_src_ov_time:end")],
        [InlineKeyboardButton("طوال الفيديو", callback_data="am_src_ov_time:full")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_overlay_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    timing = query.data.split(":", 1)[1]
    _update_draft_source_settings(context, {"shorts_overlay": {"timing": timing}})

    text = "⌛ <b>اختر مدة ظهور النص:</b>"
    keyboard = [[InlineKeyboardButton(f"{val} ثانية", callback_data=f"am_src_ov_dur:{val}")] for val in OVERLAY_DURATION_OPTIONS]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_overlay_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    duration = float(query.data.split(":", 1)[1])
    _update_draft_source_settings(context, {"shorts_overlay": {"duration": duration}})

    text = "📍 <b>اختر موضع النص داخل الفيديو:</b>"
    keyboard = [
        [InlineKeyboardButton("⬆️ أعلى", callback_data="am_src_ov_pos:top")],
        [InlineKeyboardButton("🎯 المنتصف", callback_data="am_src_ov_pos:center")],
        [InlineKeyboardButton("⬇️ أسفل", callback_data="am_src_ov_pos:bottom")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_overlay_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    position = query.data.split(":", 1)[1]
    _update_draft_source_settings(context, {"shorts_overlay": {"screen_position": position, "enabled": True}})
    return await _ask_source_overlay_animation_kind(update, context, "intro")


async def add_source_choose_overlay_animation_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    _, target, animation_type = query.data.split(":", 2)
    target_key = "intro" if target == "intro" else "outro"
    if animation_type == "none":
        _update_draft_source_settings(context, {"shorts_overlay": {f"{target_key}_animation": _build_overlay_animation_config("none", 0.0)}})
        if target_key == "intro":
            return await _ask_source_overlay_animation_kind(update, context, "outro")
        context.user_data.setdefault("am_new_source", {})["overlay_configured"] = True
        return await _continue_source_creation(update, context)
    return await _ask_source_overlay_animation_duration(update, context, target_key, animation_type)


async def add_source_choose_overlay_animation_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    _, target, animation_type, raw_duration = query.data.split(":", 3)
    target_key = "intro" if target == "intro" else "outro"
    duration = float(raw_duration)
    _update_draft_source_settings(context, {"shorts_overlay": {f"{target_key}_animation": _build_overlay_animation_config(animation_type, duration)}})
    if target_key == "intro":
        return await _ask_source_overlay_animation_kind(update, context, "outro")
    context.user_data.setdefault("am_new_source", {})["overlay_configured"] = True
    return await _continue_source_creation(update, context)


async def add_source_description_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    settings = _draft_source_settings(context)
    desc_status = _short_desc_status(settings)
    text = (
        "📄 <b>إعداد النص الإضافي داخل وصف الفيديو</b>\n\n"
        f"الحالة الحالية: <code>{html.escape(desc_status)}</code>\n\n"
        "هذا النص يُدمج مع الوصف النهائي قبل النشر، بدون استبدال الوصف الأساسي."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل وإضافة نصوص", callback_data="am_src_desc:on")],
        [InlineKeyboardButton("⬜ تخطي / تعطيل", callback_data="am_src_desc:off")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")],
    ]
    await _safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_choose_description_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]
    if choice == "off":
        _update_draft_source_settings(context, {"extra_description": {"enabled": False, "texts": []}})
        context.user_data.setdefault("am_new_source", {})["description_configured"] = True
        return await _continue_source_creation(update, context)

    context.user_data["am_text_input_mode"] = "add_desc_texts"
    text = (
        "✍️ <b>أرسل الآن النصوص الإضافية للوصف</b>\n\n"
        "- يمكنك إرسال نص واحد أو عدة فقرات\n"
        "- للفصل بين كل خيار وآخر، ضع سطرًا يحتوي على <code>---</code> فقط"
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_add_desc_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SOURCE_TEXT_INPUT


async def add_source_description_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    mode = query.data.split(":", 1)[1]
    _update_draft_source_settings(context, {"extra_description": {"selection_mode": mode}})
    text = "📌 <b>أين تريد دمج النص الإضافي داخل الوصف النهائي؟</b>"
    keyboard = [
        [InlineKeyboardButton("قبل الوصف", callback_data="am_src_desc_place:prepend")],
        [InlineKeyboardButton("بعد الوصف", callback_data="am_src_desc_place:append")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_desc_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_description_placement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    placement = query.data.split(":", 1)[1]
    _update_draft_source_settings(context, {"extra_description": {"placement": placement, "enabled": True}})
    context.user_data.setdefault("am_new_source", {})["description_configured"] = True
    return await _continue_source_creation(update, context)


async def add_source_raw_review_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    settings = _draft_source_settings(context)
    status = _raw_review_status(settings)
    text = (
        "🧪 <b>مراجعة الفيديو الخام قبل المعالجة</b>\n\n"
        f"الحالة الحالية: <code>{html.escape(status)}</code>\n\n"
        "عند التفعيل، لن يبدأ هذا المصدر بالمعالجة أو التعديل أو النشر قبل موافقتك الصريحة على الفيديو الخام."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل المراجعة اليدوية", callback_data="am_src_raw_review:on")],
        [InlineKeyboardButton("⬜ تعطيل والمتابعة المباشرة", callback_data="am_src_raw_review:off")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_desc_menu")],
    ]
    await _safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_choose_raw_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    enabled = query.data.split(":", 1)[1] == "on"
    _update_draft_source_settings(context, {"require_raw_review": enabled})
    context.user_data.setdefault("am_new_source", {})["raw_review_configured"] = True
    return await _continue_source_creation(update, context)


async def add_source_overlay_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await add_source_overlay_start(update, context)


async def add_source_description_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await add_source_description_start(update, context)


async def _ask_source_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """طلب رابط المصدر"""
    query = update.callback_query
    new_src = context.user_data.get("am_new_source", {}) or {}
    platform = (new_src.get("platform") or "youtube").strip().lower()
    if platform == "container":
        text = (
            "📦 <b>أدخل معرف الحاوية (Container ID):</b>\n\n"
            "أمثلة:\n"
            "• <code>container:2f6c0b2a-....</code>\n"
            "• <code>2f6c0b2a-....</code>\n\n"
            "سيتم جلب الفيديوهات من هذه الحاوية بدل يوتيوب."
        )
    elif platform.startswith("facebook"):
        is_reels = platform == "facebook_reels"
        text = (
            "🔗 <b>أدخل رابط فيس بوك المصدر:</b>\n\n"
            + ("📱 <b>وضع ريلز فقط:</b> يمكنك إدخال رابط الصفحة مباشرة وسيتم تلقائياً استخدام <code>/reels</code>.\n\n" if is_reels else "")
            + "أمثلة (قد يختلف الدعم حسب نوع الرابط):\n"
            + ("• <code>https://www.facebook.com/&lt;page&gt;</code>\n" if is_reels else "")
            + ("• <code>https://www.facebook.com/&lt;page&gt;/reels</code>\n" if is_reels else "")
            + "• <code>https://www.facebook.com/watch/?v=...</code>\n"
            + "• <code>https://www.facebook.com/reel/...</code>\n\n"
            + "💡 الأفضل عادةً إرسال رابط ريل مباشر لضمان نجاح الجلب.\n"
            "⚠️ إذا فشل الجلب، جرّب تزويد Cookies عبر متغير البيئة <code>YTDLP_COOKIES_PATH</code>."
        )
    else:
        if platform == "google_drive":
            text = (
                "☁️ <b>أدخل Folder ID لمجلد Google Drive:</b>\n\n"
                "مثال: <code>1AbCDefGhIjKlmNopQRstuVwxyz</code>\n\n"
                "⚠️ أرسل <b>Folder ID فقط</b> بدون نص إضافي."
            )
        else:
            text = (
                "🔗 <b>أدخل رابط مصدر يوتيوب:</b>\n\n"
                "أمثلة:\n"
                "• <code>https://www.youtube.com/@channelname/shorts</code>\n"
                "• <code>https://www.youtube.com/@channelname/videos</code>\n"
                "• <code>https://www.youtube.com/playlist?list=PLxxxxxx</code>\n\n"
                "⚠️ أرسل <b>رابط مصدر واحد فقط</b> في كل رسالة، وليس عدة روابط دفعة واحدة.\n"
                "💡 يمكن أن يكون الرابط قناة أو تبويب <code>/videos</code> أو <code>/shorts</code> أو Playlist."
            )
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    context.user_data["am_new_source"]["awaiting_url"] = True
    return AM_ADD_SOURCE_URL

async def add_source_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إدخال رابط المصدر"""
    if not context.user_data.get("am_new_source", {}).get("awaiting_url"):
        return AM_ADD_SOURCE_URL

    raw = update.message.text.strip()
    source_data = context.user_data.get("am_new_source", {}) or {}
    platform = (source_data.get("platform") or "youtube").strip().lower()

    if platform == "container":
        cid = raw
        if cid.lower().startswith("container:"):
            cid = cid.split(":", 1)[1].strip()
        if not cid or len(cid) < 8:
            await update.message.reply_text("❌ أدخل معرف حاوية صالح (UUID أو container:UUID).")
            return AM_ADD_SOURCE_URL
        context.user_data["am_new_source"]["source_url"] = f"container:{cid}"
    elif platform == "google_drive":
        folder_id = raw
        folder_id = folder_id.strip()
        if folder_id.startswith("http"):
            await update.message.reply_text("❌ أرسل Folder ID فقط (ليس رابط).")
            return AM_ADD_SOURCE_URL
        if not folder_id or len(folder_id) < 10:
            await update.message.reply_text("❌ Folder ID غير صالح. تحقق منه وأعد الإرسال.")
            return AM_ADD_SOURCE_URL
        context.user_data["am_new_source"]["source_url"] = f"gdrive:folder:{folder_id}"
        context.user_data["am_new_source"]["platform"] = "google_drive"
    else:
        url = raw
        url_matches = re.findall(r"https?://\S+", raw)
        if len(url_matches) > 1:
            await update.message.reply_text("⚠️ أرسل رابط مصدر واحد فقط في كل مرة، ثم أضف المصدر التالي بشكل منفصل.")
            return AM_ADD_SOURCE_URL
        if len(url_matches) == 1 and url_matches[0] != raw:
            await update.message.reply_text("⚠️ أرسل الرابط فقط بدون أي نص إضافي، وبمصدر واحد في الرسالة.")
            return AM_ADD_SOURCE_URL
        if not url.startswith("http"):
            await update.message.reply_text("❌ أدخل رابطًا صالحًا يبدأ بـ http")
            return AM_ADD_SOURCE_URL

        # Facebook UX: user may paste a video link instead of page/profile.
        if platform.startswith("facebook"):
            page_url = await asyncio.to_thread(resolve_facebook_page_from_video_url, url)
            if page_url:
                url = page_url

        context.user_data["am_new_source"]["source_url"] = url

    context.user_data["am_new_source"]["tail_trim_configured"] = False
    context.user_data["am_new_source"]["intro_effect_configured"] = False
    context.user_data["am_new_source"]["outro_effect_configured"] = False
    context.user_data["am_new_source"]["hflip_configured"] = False
    context.user_data["am_new_source"]["privacy_configured"] = False
    context.user_data["am_new_source"]["overlay_configured"] = False
    context.user_data["am_new_source"]["description_configured"] = False
    context.user_data["am_new_source"]["raw_review_configured"] = False

    return await _ask_source_tail_trim(update, context)


async def add_source_choose_tail_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]

    if choice == "off":
        _update_draft_source_settings(context, {"tail_trim": {"enabled": False, "seconds": 0.0}})
    else:
        try:
            seconds = float(choice)
        except Exception:
            await query.answer("❌ قيمة القص غير صالحة", show_alert=True)
            return await _ask_source_tail_trim(update, context)
        _update_draft_source_settings(context, {"tail_trim": {"enabled": True, "seconds": seconds}})

    context.user_data.setdefault("am_new_source", {})["tail_trim_configured"] = True
    return await _ask_source_video_effect_kind(update, context, "intro")


async def add_source_video_effect_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    target = (query.data.split(":", 1)[1] if query and ":" in query.data else "intro").strip().lower()
    return await _ask_source_video_effect_kind(update, context, target)


async def add_source_choose_video_effect_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return await _continue_source_creation(update, context)
    _, target, effect_type = parts
    target = "intro" if target == "intro" else "outro"

    if effect_type == "none":
        _update_draft_source_settings(context, {"video_effects": {target: _build_video_effect_config("none")}})
        context.user_data.setdefault("am_new_source", {})[f"{target}_effect_configured"] = True
        return await (_ask_source_video_effect_kind(update, context, "outro") if target == "intro" else _continue_source_creation(update, context))

    return await _ask_source_video_effect_duration(update, context, target, effect_type)


async def add_source_choose_video_effect_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        return await _continue_source_creation(update, context)
    _, target, effect_type, raw_duration = parts
    target = "intro" if target == "intro" else "outro"
    try:
        duration = float(raw_duration)
    except Exception:
        await query.answer("❌ مدة التأثير غير صالحة", show_alert=True)
        return await _ask_source_video_effect_duration(update, context, target, effect_type)

    _update_draft_source_settings(context, {"video_effects": {target: _build_video_effect_config(effect_type, duration)}})
    context.user_data.setdefault("am_new_source", {})[f"{target}_effect_configured"] = True
    return await (_ask_source_video_effect_kind(update, context, "outro") if target == "intro" else _continue_source_creation(update, context))


async def _ask_source_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = _draft_source_settings(context)
    privacy_status = _source_privacy_status(settings)
    text = (
        "🔒 <b>اختر خصوصية النشر لهذا المصدر</b> <i>(اختياري)</i>\n\n"
        f"الحالة الحالية: <code>{html.escape(privacy_status)}</code>\n\n"
        "سيتم استخدام هذا الخيار عند نشر الفيديوهات المجلوبة من هذا المصدر."
    )
    keyboard = [
        [InlineKeyboardButton("🌍 علني (Public)", callback_data="am_src_privacy:public")],
        [InlineKeyboardButton("🔗 غير مدرج (Unlisted)", callback_data="am_src_privacy:unlisted")],
        [InlineKeyboardButton("🔒 خاص (Private)", callback_data="am_src_privacy:private")],
        [InlineKeyboardButton("⚙️ حسب خصوصية القناة", callback_data="am_src_privacy:default")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def _ask_source_hflip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = _draft_source_settings(context)
    hflip_status = _source_hflip_status(settings)
    text = (
        "↔️ <b>قلب الفيديو أفقيًا (Mirror)</b> <i>(اختياري)</i>\n\n"
        f"الحالة الحالية: <code>{html.escape(hflip_status)}</code>\n\n"
        "عند التفعيل سيتم قلب الفيديو من اليمين إلى اليسار لهذا المصدر فقط."
    )
    keyboard = [
        [InlineKeyboardButton("✅ تفعيل للمصدر", callback_data="am_src_hflip:on")],
        [InlineKeyboardButton("❌ تعطيل للمصدر", callback_data="am_src_hflip:off")],
        [InlineKeyboardButton("⚙️ حسب الإعدادات العامة", callback_data="am_src_hflip:default")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_sources")],
    ]
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_ADD_SOURCE_CUSTOMIZE


async def add_source_choose_hflip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = (query.data.split(":", 1)[1] if ":" in (query.data or "") else "").strip().lower()
    if choice == "on":
        _set_draft_source_hflip(context, True)
    elif choice == "off":
        _set_draft_source_hflip(context, False)
    elif choice == "default":
        _set_draft_source_hflip(context, None)
    else:
        return await _ask_source_hflip(update, context)
    context.user_data.setdefault("am_new_source", {})["hflip_configured"] = True
    return await _continue_source_creation(update, context)


async def add_source_choose_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    choice = (query.data.split(":", 1)[1] if ":" in (query.data or "") else "").strip().lower()
    if choice not in {"public", "private", "unlisted", "default"}:
        return await _ask_source_privacy(update, context)
    value = None if choice == "default" else choice
    _update_draft_source_settings(context, {"privacy": value})
    context.user_data.setdefault("am_new_source", {})["privacy_configured"] = True
    return await _continue_source_creation(update, context)


async def add_source_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إدخال اسم المصدر وحفظه"""
    name = update.message.text.strip()
    source_data = context.user_data.get("am_new_source", {})

    if name.lower() == "auto":
        platform = (source_data.get("platform") or "youtube").strip().lower()
        if platform == "container":
            cid = (source_data.get("source_url") or "").strip()
            if cid.lower().startswith("container:"):
                cid = cid.split(":", 1)[1].strip()
            resolved = None
            try:
                from ...agent.supabase_storage import get_video_container
                c = get_video_container(cid)
                if c and c.get("name"):
                    resolved = str(c.get("name"))
            except Exception:
                resolved = None
            name = resolved or f"Container {cid[:8]}"
        else:
            name = AutoModDB._extract_channel_name(source_data.get("source_url", ""))

    db = _get_db()
    facecam_settings = source_data.get("facecam_settings")
    source_settings = merge_source_settings(facecam_settings or {}, source_data.get("source_settings") or {})
    source_id = source_data.get("source_id")
    success = db.add_source(
        channel_id=source_data.get("channel_id", ""),
        source_url=source_data.get("source_url", ""),
        source_name=name,
        content_type=source_data.get("content_type", "minecraft_mods"),
        platform=source_data.get("platform", "youtube"),
        facecam_settings=facecam_settings,
        source_settings=source_settings,
        source_id=source_id,
    )

    if success:
        overlay_status = _short_overlay_status(source_settings)
        desc_status = _short_desc_status(source_settings)
        raw_review_status = _raw_review_status(source_settings)
        tail_trim_status = _tail_trim_status(source_settings)
        intro_effect_status = _video_effect_status(source_settings, "intro")
        outro_effect_status = _video_effect_status(source_settings, "outro")
        hflip_status = _source_hflip_status(source_settings)
        facecam_status = _facecam_status(source_settings)
        privacy_status = _source_privacy_status(source_settings)
        text = (
            f"✅ <b>تم إضافة المصدر بنجاح!</b>\n\n"
            f"📛 الاسم: <code>{html.escape(name)}</code>\n"
            f"📦 النوع: <code>{html.escape(source_data.get('content_type', 'minecraft_mods'))}</code>\n"
            f"🔗 الرابط: <code>{html.escape(source_data.get('source_url', '')[:50])}</code>\n"
            f"🔒 الخصوصية: <code>{html.escape(privacy_status)}</code>\n"
            f"📝 نص الشورتس: <code>{html.escape(overlay_status)}</code>\n"
            f"🎬 Facecam: <code>{html.escape(facecam_status)}</code>\n"
            f"↔️ قلب الفيديو: <code>{html.escape(hflip_status)}</code>\n"
            f"📄 نص الوصف: <code>{html.escape(desc_status)}</code>\n"
            f"✂️ قص النهاية: <code>{html.escape(tail_trim_status)}</code>\n"
            f"✨ تأثير البداية: <code>{html.escape(intro_effect_status)}</code>\n"
            f"🏁 تأثير النهاية: <code>{html.escape(outro_effect_status)}</code>\n"
            f"🧪 مراجعة الخام: <code>{html.escape(raw_review_status)}</code>\n\n"
            f"⏱ <b>إعداد الأتمتة التلقائية:</b>\n"
            f"يرجى تحديد الفترة الزمنية للنشر التلقائي لهذا المصدر، أو الضغط على تخطي للعودة."
        )

        intervals = [
            ("⚡ 1د", 1), ("⚡ 5د", 5), ("🕙 10د", 10), ("🕙 15د", 15),
            ("🕒 30د", 30), ("🕓 1س", 60), ("🕓 2س", 120), ("🕓 3س", 180),
            ("🕘 4س", 240), ("🕘 6س", 360), ("🕗 8س", 480), ("🕗 12س", 720),
            ("📅 1يوم", 1440), ("📅 2يوم", 2880), ("📅 3يوم", 4320), ("📅 1أسبوع", 10080),
        ]

        keyboard = []
        for i in range(0, len(intervals), 4):
            row = []
            for label, mins in intervals[i:i+4]:
                row.append(InlineKeyboardButton(label, callback_data=f"am_sch_int:{mins}"))
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("⏭️ تخطي / للمصادر", callback_data="am_sources")])

        context.user_data["am_new_schedule"] = {
            "channel_id": source_data.get("channel_id", ""),
            "content_type": source_data.get("content_type", "minecraft_mods"),
            "source_name": name
        }

        context.user_data.pop("am_new_source", None)
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_SCHEDULE_LIMIT

    else:
        text = "❌ فشل إضافة المصدر. قد يكون مكررًا."

        keyboard = [
            [InlineKeyboardButton("➕ إضافة مصدر آخر", callback_data="am_add_source")],
            [InlineKeyboardButton("🔙 المصادر", callback_data="am_sources")],
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        context.user_data.pop("am_new_source", None)
        return AM_SOURCES


async def source_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("am_text_input_mode")
    raw_text = (update.message.text or "").strip()
    if not raw_text:
        await update.message.reply_text("⚠️ أرسل نصًا واحدًا على الأقل.")
        return AM_SOURCE_TEXT_INPUT

    if context.user_data.get("am_gdrive_awaiting_url"):
        context.user_data.pop("am_gdrive_awaiting_url", None)
        await gdrive_process_auth_result(update, context, raw_text)
        return ConversationHandler.END

    if mode == "add_overlay_texts":
        texts = _split_overlay_texts(raw_text)
        if not texts:
            await update.message.reply_text("⚠️ أرسل سطرًا واحدًا على الأقل.")
            return AM_SOURCE_TEXT_INPUT
        _update_draft_source_settings(context, {"shorts_overlay": {"texts": texts, "enabled": True}})
        context.user_data.pop("am_text_input_mode", None)
        keyboard = [
            [InlineKeyboardButton("ثابت", callback_data="am_src_ov_mode:fixed")],
            [InlineKeyboardButton("عشوائي", callback_data="am_src_ov_mode:random")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_overlay_menu")],
        ]
        await update.message.reply_text(
            "✅ تم حفظ نصوص الشورتس.\n\nالآن اختر طريقة الاختيار:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return AM_ADD_SOURCE_CUSTOMIZE

    if mode == "add_desc_texts":
        texts = _split_description_texts(raw_text)
        if not texts:
            await update.message.reply_text("⚠️ أرسل نصًا أو فقرة واحدة على الأقل.")
            return AM_SOURCE_TEXT_INPUT
        _update_draft_source_settings(context, {"extra_description": {"texts": texts, "enabled": True}})
        context.user_data.pop("am_text_input_mode", None)
        keyboard = [
            [InlineKeyboardButton("ثابت", callback_data="am_src_desc_mode:fixed")],
            [InlineKeyboardButton("عشوائي", callback_data="am_src_desc_mode:random")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="am_add_desc_menu")],
        ]
        await update.message.reply_text(
            "✅ تم حفظ نصوص الوصف.\n\nالآن اختر طريقة الاختيار:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return AM_ADD_SOURCE_CUSTOMIZE

    if mode == "edit_overlay_texts":
        texts = _split_overlay_texts(raw_text)
        if not texts:
            await update.message.reply_text("⚠️ أرسل سطرًا واحدًا على الأقل.")
            return AM_SOURCE_TEXT_INPUT
        success = await _update_edit_source_settings(context, {"shorts_overlay": {"texts": texts, "enabled": True}})
        context.user_data.pop("am_text_input_mode", None)
        await update.message.reply_text("✅ تم حفظ نصوص الشورتس الجديدة." if success else "❌ تعذر حفظ النصوص.")
        return await _show_overlay_editor(update, context)

    if mode == "edit_desc_texts":
        texts = _split_description_texts(raw_text)
        if not texts:
            await update.message.reply_text("⚠️ أرسل نصًا أو فقرة واحدة على الأقل.")
            return AM_SOURCE_TEXT_INPUT
        success = await _update_edit_source_settings(context, {"extra_description": {"texts": texts, "enabled": True}})
        context.user_data.pop("am_text_input_mode", None)
        await update.message.reply_text("✅ تم حفظ نصوص الوصف الجديدة." if success else "❌ تعذر حفظ النصوص.")
        return await _show_description_editor(update, context)

    if mode == "edit_fetch_add":
        url_matches = re.findall(r"https?://\S+", raw_text)
        if len(url_matches) > 1:
            await update.message.reply_text("⚠️ أرسل رابط واحد فقط في الرسالة.")
            return AM_SOURCE_TEXT_INPUT
        url = url_matches[0] if url_matches else raw_text
        url = url.strip()
        if not url.startswith("http"):
            await update.message.reply_text("❌ أدخل رابطًا صالحًا يبدأ بـ http")
            return AM_SOURCE_TEXT_INPUT

        src = await _get_edit_source(context)
        if not src:
            context.user_data.pop("am_text_input_mode", None)
            return AM_SOURCES
        items = _fetch_sources_for_ui(src)
        normalized_url = url.rstrip("/")
        existing_urls = {str((x or {}).get("url") or "").strip().rstrip("/") for x in items}
        if normalized_url in existing_urls:
            context.user_data.pop("am_text_input_mode", None)
            await update.message.reply_text("ℹ️ هذا الرابط موجود بالفعل ضمن قنوات الجلب.")
            return await edit_source_fetch_sources_menu(update, context)
        items.append({
            "url": url,
            "name": "",
            "platform": str(src.get("platform") or "").strip().lower(),
            "enabled": True,
        })
        success = await _update_edit_source_settings(context, {"fetch_sources": items})
        context.user_data.pop("am_text_input_mode", None)
        await update.message.reply_text("✅ تم إضافة قناة الجلب." if success else "❌ تعذر إضافة القناة.")
        return await edit_source_fetch_sources_menu(update, context)

    await update.message.reply_text("⚠️ لا يوجد حقل نصي نشط حالياً.")
    return AM_SOURCES


# ==================== إدارة الجدولة ====================

async def schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الجداول + المصادر المتاحة لإضافة جدول جديد"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    db = _get_db()
    schedules = db.get_all_schedules()
    sources = db.get_sources()

    keyboard = []
    text = "⏰ *إدارة جداول النشر التلقائي*\n\n"

    # --- 1. عرض المصادر المتاحة (هي المدخل الأساسي الآن كما طلب المستخدم) ---
    if sources:
        text += "📡 *اختر مصدرًا لضبط جدولته:* (قائمة المصادر الحالية)\n"
        for src in sources[:15]:
            src_id = src.get("id", "")
            src_name = src.get("source_name", "مصدر")[:25]
            # التحقق إذا كان لهذا المصدر جدول بالفعل (تبسيطي: فحص القناة والنوع)
            has_sch = any(
                s.get("channel_id") == src.get("channel_id") and
                s.get("content_type") == src.get("content_type")
                for s in schedules
            )
            status_icon = "📅" if has_sch else "➕"
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_icon} {src_name}",
                    callback_data=f"am_sch_src:{src_id}"
                )
            ])
    else:
        text += "⚠️ لا توجد مصادر مضافة. أضف مصدرًا أولاً من 'إدارة المصادر'.\n"

    # --- 2. عرض الجداول الموجودة حالياً (للمراجعة السريعة) ---
    if schedules:
        text += "\n━━━━━━━━━━━━━━━━━━━\n📋 *الجداول النشطة حالياً:*\n"
        for i, sch in enumerate(schedules, 1):
            status = "✅" if sch.get("enabled") else "❌"
            interval = sch.get("publish_interval_minutes", 120)
            
            # تحويل الدقائق لنص بسيط
            if interval < 60: interval_text = f"{interval}د"
            elif interval == 60: interval_text = "ساعة"
            else: interval_text = f"{interval/60:g}س"

            text += f"{i}. {status} `{sch.get('content_type', '')[:10]}` -> كل {interval_text}\n"

        # أزرار تحكم سريعة للجداول
        for sch in schedules[:4]:
            sch_id = sch.get("id", "")
            enabled = sch.get("enabled", True)
            toggle = "⏸" if enabled else "▶️"
            keyboard.append([
                InlineKeyboardButton(
                    f"{toggle} {sch.get('content_type', '')[:10]}",
                    callback_data=f"am_toggle_sch:{sch_id}"
                ),
                InlineKeyboardButton("🗑", callback_data=f"am_del_sch:{sch_id}"),
            ])

    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="am_menu")])

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_SCHEDULE

async def schedule_pick_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """المستخدم اختار مصدرًا → عرض القناة المرتبطة"""
    query = update.callback_query
    await _safe_answer(query)

    src_id = query.data.split(":", 1)[1]
    db = _get_db()
    sources = db.get_sources()
    src = next((s for s in sources if s.get("id") == src_id), None)

    if not src:
        await query.answer("❌ المصدر غير موجود", show_alert=True)
        return await schedule_menu(update, context)

    # حفظ بيانات المصدر المختار
    context.user_data["am_new_schedule"] = {
        "source_id": src_id,
        "source_name": src.get("source_name", "مصدر"),
        "channel_id": src.get("channel_id", ""),
        "content_type": src.get("content_type", "minecraft_mods"),
    }

    channel_id = src.get("channel_id", "")
    ch_name = channel_id[:20]

    # محاولة جلب اسم القناة
    try:
        from ..channel_manager import ChannelManager
        cm = ChannelManager()
        channels, _ = cm.list_channels(enabled_only=True, limit=50)
        ch_obj = next((c for c in channels if c.channel_id == channel_id), None)
        if ch_obj:
            ch_name = ch_obj.channel_name[:30]
    except Exception:
        pass

    text = (
        f"📡 <b>المصدر:</b> <code>{html.escape(src.get('source_name', ''))}</code>\n"
        f"📦 <b>النوع:</b> <code>{html.escape(src.get('content_type', 'minecraft_mods'))}</code>\n"
        f"🔗 <b>الرابط:</b> <code>{html.escape(src.get('source_url', '')[:50])}</code>\n\n"
        f"📺 <b>القناة المستهدفة:</b>\n"
        f"اضغط على القناة لإعداد جدول النشر لها."
    )

    keyboard = [
        [InlineKeyboardButton(
            f"📺 {html.escape(ch_name)}",
            callback_data=f"am_sch_ch:{channel_id}"
        )],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_schedule")],
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SCHEDULE_INTERVAL


async def schedule_pick_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """المستخدم اختار القناة → عرض أزرار فترات النشر"""
    query = update.callback_query
    await _safe_answer(query)

    channel_id = query.data.split(":", 1)[1]
    sch_data = context.user_data.get("am_new_schedule", {})
    sch_data["channel_id"] = channel_id

    src_name = sch_data.get("source_name", "مصدر")

    text = (
        f"⏱ <b>تحديد فترة النشر لنظام الأتمتة</b>\n\n"
        f"المصدر: <code>{html.escape(src_name)}</code>\n\n"
        "ما هي المدة التي تريدها بين كل عملية نشر تلقائية؟"
    )

    intervals = [
        ("⚡ 1د", 1), ("⚡ 5د", 5), ("🕙 10د", 10), ("🕙 15د", 15),
        ("🕒 30د", 30), ("🕓 1س", 60), ("🕓 2س", 120), ("🕓 3س", 180),
        ("🕘 4س", 240), ("🕘 6س", 360), ("🕗 8س", 480), ("🕗 12س", 720),
        ("📅 1يوم", 1440), ("📅 2يوم", 2880), ("📅 3يوم", 4320), ("📅 1أسبوع", 10080),
    ]

    keyboard = []
    # ترتيب 4 أزرار في كل صف
    for i in range(0, len(intervals), 4):
        row = []
        for label, mins in intervals[i:i+4]:
            row.append(InlineKeyboardButton(label, callback_data=f"am_sch_int:{mins}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="am_schedule")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SCHEDULE_LIMIT

async def schedule_pick_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """المستخدم اختار الفترة → عرض أزرار الحد اليومي"""
    query = update.callback_query
    await _safe_answer(query)

    interval = int(query.data.split(":", 1)[1])
    sch_data = context.user_data.get("am_new_schedule", {})
    sch_data["interval"] = interval

    # تحويل الفترة لنص مقروء
    interval_labels = {
        1: "كل دقيقة", 5: "كل 5 دقائق", 10: "كل 10 دقائق", 15: "كل 15 دقيقة",
        30: "كل 30 دقيقة", 60: "كل ساعة", 120: "كل ساعتين",
        180: "كل 3 ساعات", 240: "كل 4 ساعات", 360: "كل 6 ساعات",
        480: "كل 8 ساعات", 720: "كل 12 ساعة", 1440: "مرة يوميًا",
        2880: "كل يومين", 4320: "كل 3 أيام", 10080: "كل أسبوع"
    }
    interval_text = interval_labels.get(interval, f"كل {interval} دقيقة")

    text = (
        f"✅ <b>الفترة:</b> {html.escape(interval_text)}\n\n"
        f"🔢 <b>اختر الحد الأقصى للنشر يوميًا:</b>\n\n"
        "كم فيديو كحد أقصى يُنشر في اليوم الواحد؟"
    )

    limits = [
        ("1️⃣", 1), ("2️⃣", 2), ("3️⃣", 3),
        ("5️⃣", 5), ("🔟", 10), ("1️⃣5️⃣", 15),
        ("2️⃣0️⃣", 20), ("3️⃣0️⃣", 30), ("♾ بلا حد", 999),
    ]

    keyboard = []
    for i in range(0, len(limits), 3):
        row = []
        for label, val in limits[i:i+3]:
            row.append(InlineKeyboardButton(label, callback_data=f"am_sch_lim:{val}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_schedule")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_SCHEDULE_HOURS


async def schedule_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ الجدول الجديد"""
    query = update.callback_query
    await _safe_answer(query)

    daily_limit = int(query.data.split(":", 1)[1])
    sch_data = context.user_data.get("am_new_schedule", {})

    db = _get_db()

    success = db.save_schedule(
        channel_id=sch_data.get("channel_id", ""),
        content_type=sch_data.get("content_type", "minecraft_mods"),
        interval_minutes=sch_data.get("interval", 120),
        daily_limit=daily_limit,
    )

    # تحويل الفترة لنص مقروء
    interval = sch_data.get("interval", 120)
    interval_labels = {
        1: "كل دقيقة", 5: "كل 5 دقائق", 15: "كل 15 دقيقة",
        30: "كل 30 دقيقة", 60: "كل ساعة", 120: "كل ساعتين",
        180: "كل 3 ساعات", 240: "كل 4 ساعات", 360: "كل 6 ساعات",
        480: "كل 8 ساعات", 720: "كل 12 ساعة", 1440: "مرة يوميًا",
    }
    interval_text = interval_labels.get(interval, f"كل {interval} دقيقة")
    limit_text = "بلا حد" if daily_limit >= 999 else f"{daily_limit} فيديو"

    if success:
        text = (
            "✅ <b>تم إنشاء جدول النشر!</b>\n\n"
            f"📡 المصدر: <code>{html.escape(sch_data.get('source_name', ''))}</code>\n"
            f"📦 النوع: <code>{html.escape(sch_data.get('content_type', 'minecraft_mods'))}</code>\n"
            f"⏱ الفترة: {html.escape(interval_text)}\n"
            f"🔢 الحد اليومي: {html.escape(limit_text)}\n"
            f"🕐 ساعات النشر: 8:00 - 22:00"
        )
    else:
        text = "❌ فشل إنشاء الجدول."

    keyboard = [
        [InlineKeyboardButton("🔙 الجداول", callback_data="am_schedule")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="am_menu")],
    ]

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    context.user_data.pop("am_new_schedule", None)
    return AM_SCHEDULE


async def toggle_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل حالة جدول"""
    query = update.callback_query
    await _safe_answer(query)
    sch_id = query.data.split(":", 1)[1]
    db = _get_db()

    try:
        # جلب الجدول أولاً لمعرفة الحالة الحالية
        from ...agent.supabase_client import supabase_select
        records = await asyncio.to_thread(supabase_select, "auto_mod_schedule", {"id": sch_id})
        if records:
            rec = records[0]
            new_state = not rec.get("enabled", True)
            success = await asyncio.to_thread(db.toggle_schedule, sch_id, new_state)
            if success:
                await query.answer("✅ تم التبديل")
            else:
                await query.answer("❌ فشل التحديث")
    except Exception as e:
        await query.answer(f"❌ خطأ: {str(e)[:50]}")

    return await schedule_menu(update, context)


async def delete_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف جدول"""
    query = update.callback_query
    await _safe_answer(query)
    sch_id = query.data.split(":", 1)[1]
    db = _get_db()

    try:
        success = await asyncio.to_thread(db.delete_schedule, sch_id)
        if success:
            await query.answer("🗑 تم الحذف")
        else:
            await query.answer("❌ فشل الحذف")
    except Exception as e:
        await query.answer(f"❌ خطأ: {str(e)[:50]}")

    return await schedule_menu(update, context)


# ==================== الحالة التفصيلية ====================

async def status_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض الحالة التفصيلية"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    db = _get_db()
    stats = db.get_stats()
    config = db.get_config()

    text = (
        "📊 <b>الحالة التفصيلية</b>\n\n"
        f"🆔 النسخة: <code>{html.escape(get_instance_id()[:25])}</code>\n"
        f"📊 الجلب: {'✅ مفعل' if config.get('auto_fetch_enabled') else '❌ معطل'}\n\n"
        f"📡 المصادر: {stats.get('total_sources', 0)}\n"
        f"⏰ الجداول: {stats.get('total_schedules', 0)}\n\n"
        f"📈 <b>إحصائيات المعالجة:</b>\n"
        f"• إجمالي: {stats.get('total_processed', 0)}\n"
        f"• منشور: {stats.get('published', 0)} ✅\n"
        f"• فاشل: {stats.get('failed', 0)} ❌\n"
        f"• قيد المعالجة: {stats.get('processing', 0)} ⏳\n\n"
        f"⚙️ <b>الإعدادات:</b>\n"
        f"• نمط الشورتس: <code>{html.escape(config.get('shorts_format', 'crop'))}</code>\n"
        f"• التحسين: {'✅' if config.get('enhance_enabled') else '❌'}\n"
        f"• قلب أفقياً (Mirror): {'✅' if config.get('hflip_enabled') else '❌'}\n"
        f"• CTA: {'✅' if config.get('add_cta') else '❌'}\n"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 تحديث", callback_data="am_status")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")],
    ]

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_STATUS


# ==================== الإعدادات ====================

async def config_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض وتعديل الإعدادات"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    db = _get_db()
    config = db.get_config()

    text = (
        "⚙️ <b>إعدادات الجلب التلقائي</b>\n\n"
        f"📏 تردد الفحص: <code>{config.get('auto_fetch_interval_seconds', 60)} ثانية</code>\n"
        f"⏳ ترتيب الجلب: <code>{html.escape(config.get('settings', {}).get('fetch_order', 'newest'))}</code>\n"
        f"📐 نمط الشورتس: <code>{html.escape(config.get('shorts_format', 'crop'))}</code>\n"
        f"🎨 تحسين الألوان: {'✅' if config.get('enhance_enabled') else '❌'}\n"
        f"↔️ قلب أفقياً (Mirror): {'✅' if config.get('hflip_enabled') else '❌'}\n"
        f"📱 CTA (دعوة التطبيق): {'✅' if config.get('add_cta') else '❌'}\n"
    )

    keyboard = [
        [InlineKeyboardButton(
            f"📐 الشورتس: {config.get('shorts_format', 'crop')}",
            callback_data="am_cfg_shorts_fmt"
        )],
        [InlineKeyboardButton(
            f"🎨 التحسين: {'✅' if config.get('enhance_enabled') else '❌'}",
            callback_data="am_cfg_enhance"
        )],
        [InlineKeyboardButton(
            f"↔️ قلب الفيديو: {'✅' if config.get('hflip_enabled') else '❌'}",
            callback_data="am_cfg_hflip"
        )],
        [InlineKeyboardButton(
            f"📱 CTA: {'✅' if config.get('add_cta') else '❌'}",
            callback_data="am_cfg_cta"
        )],
        [InlineKeyboardButton(
            f"⏳ الترتيب: {config.get('settings', {}).get('fetch_order', 'newest').title()}",
            callback_data="am_cfg_fetch_order"
        )],
        [InlineKeyboardButton(
            f"📏 تردد الفحص: {config.get('auto_fetch_interval_seconds', 60)}s",
            callback_data="am_cfg_loop_interval"
        )],
        [InlineKeyboardButton("📂 رفع ملف المصادقة (client_secret.json)", callback_data="am_client_secret_start")],
        [InlineKeyboardButton("☁️ ربط Google Drive", callback_data="am_gdrive_connect")],
        [InlineKeyboardButton("🍪 تحديث ملف الكوكيز (cookies.txt)", callback_data="am_cookies_start")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")],
    ]

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_CONFIG


async def config_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل إعدادات"""
    query = update.callback_query
    await _safe_answer(query)

    db = _get_db()
    config = db.get_config()

    if query.data == "am_cfg_enhance":
        config["enhance_enabled"] = not config.get("enhance_enabled", False)
    elif query.data == "am_cfg_hflip":
        config["hflip_enabled"] = not config.get("hflip_enabled", False)
    elif query.data == "am_cfg_toggle_fetch":
        config["auto_fetch_enabled"] = not config.get("auto_fetch_enabled", False)
        logger.info(f"⚙️ [AutoMod] Global fetch toggled to: {config['auto_fetch_enabled']}")
    elif query.data == "am_cfg_cta":
        config["add_cta"] = not config.get("add_cta", True)
    elif query.data == "am_cfg_shorts_fmt":
        # تبديل بين الأنماط الثلاثة
        current = config.get("shorts_format", "crop")
        cycle = {"crop": "fit_blur", "fit_blur": "partial_blur", "partial_blur": "crop"}
        config["shorts_format"] = cycle.get(current, "crop")
    elif query.data == "am_cfg_loop_interval":
        # تبديل تردد الفحص (30ث، 60ث، 5د)
        intervals = [30, 60, 300]
        current = config.get("auto_fetch_interval_seconds", 60)
        try:
            next_idx = (intervals.index(current) + 1) % len(intervals)
        except ValueError:
            next_idx = 1 # دقيقة كافتراضي
        config["auto_fetch_interval_seconds"] = intervals[next_idx]
    elif query.data == "am_cfg_fetch_order":
        if "settings" not in config: config["settings"] = {}
        current = config["settings"].get("fetch_order", "newest")
        orders = ["newest", "oldest", "random"]
        try:
            next_idx = (orders.index(current) + 1) % len(orders)
        except ValueError:
            next_idx = 0
        config["settings"]["fetch_order"] = orders[next_idx]
        logger.info(f"⚙️ [AutoMod] Fetch order changed to: {config['settings']['fetch_order']}")

    db.save_config(config)
    return await config_menu(update, context)


async def cookies_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية رفع ملف الكوكيز"""
    query = update.callback_query
    await _safe_answer(query)

    text = (
        "🍪 <b>تحديث ملف الكوكيز (YouTube Cookies)</b>\n\n"
        "لتجنب حظر يوتيوب (Bot-check)، يرجى رفع ملف <code>cookies.txt</code> الخاص بحسابك.\n\n"
        "<b>كيفية الحصول على الملف:</b>\n"
        "1. استخدم إضافة متصفح مثل 'Get cookies.txt LOCALLY'.\n"
        "2. قم بتصدير الكوكيز بصيغة Netscape/Wget.\n"
        "3. أرسل الملف هنا مباشرة.\n\n"
        "<i>سيتم استبدال الملف القديم فوراً.</i>"
    )

    keyboard = [[InlineKeyboardButton("🔙 إلغاء", callback_data="am_config")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_COOKIES_UPLOAD


async def client_secret_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)

    text = (
        "📂 <b>رفع ملف المصادقة (client_secret.json)</b>\n\n"
        "يرجى إرسال ملف <code>client_secret.json</code> الآن (كمستند).\n\n"
        "سيتم حفظه واستخدامه لربط Google Drive و YouTube OAuth.\n\n"
        "<i>سيتم استبدال الملف القديم فوراً إن وجد.</i>"
    )
    keyboard = [[InlineKeyboardButton("🔙 إلغاء", callback_data="am_config")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_CLIENT_SECRET_UPLOAD


async def receive_client_secret_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("⚠️ يرجى إرسال ملف <code>client_secret.json</code> بصيغة مستند.", parse_mode="HTML")
        return AM_CLIENT_SECRET_UPLOAD

    doc = update.message.document
    if not str(doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("⚠️ يجب أن يكون الملف بصيغة <code>.json</code> (client_secret.json).", parse_mode="HTML")
        return AM_CLIENT_SECRET_UPLOAD

    try:
        from ...agent.config import load_config

        cfg = load_config()
        target_dir = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, "client_secret.json")

        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=target_path)

        await update.message.reply_text(
            "✅ تم حفظ ملف <code>client_secret.json</code> بنجاح. يمكنك الآن ربط Google Drive.",
            parse_mode="HTML",
        )
        return await config_menu(update, context)
    except Exception as e:
        logger.error(f"Error saving client_secret.json: {e}")
        await update.message.reply_text(
            f"❌ حدث خطأ أثناء حفظ الملف: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return AM_CLIENT_SECRET_UPLOAD


async def receive_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استلام وحفظ ملف الكوكيز"""
    if not update.message.document:
        await update.message.reply_text("⚠️ يرجى إرسال ملف <code>cookies.txt</code> بصيغة مستند.")
        return AM_COOKIES_UPLOAD

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ يجب أن يكون الملف بصيغة <code>.txt</code> (مثل cookies.txt).")
        return AM_COOKIES_UPLOAD

    try:
        # تحميل الملف
        file = await context.bot.get_file(doc.file_id)
        
        # إنشاء مسار الحفظ
        from ...agent.auto_mod_fetcher import project_root
        data_dir = os.path.join(project_root, ".data")
        os.makedirs(data_dir, exist_ok=True)
        out_path = os.path.join(data_dir, "yt_cookies.txt")

        # حفظ الملف
        await file.download_to_drive(out_path)
        
        # تحديث المتغير البيئي محلياً لهذه الجلسة
        os.environ["YT_COOKIES_PATH"] = out_path
        os.environ["YTDLP_COOKIES_PATH"] = out_path

        await update.message.reply_text(
            "✅ <b>تم تحديث ملف الكوكيز بنجاح!</b>\n\n"
            "سيستخدم البوت هذا الملف الآن في دورات الجلب القادمة لتجنب الحظر.",
            parse_mode="HTML"
        )
        return await config_menu(update, context)

    except Exception as e:
        logger.error(f"Error saving cookies file: {e}")
        await update.message.reply_text(f"❌ حدث خطأ أثناء حفظ الملف: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        return AM_COOKIES_UPLOAD



# ==================== عرض الحاويات والفيديوهات ====================

async def containers_viewer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة بجميع حاويات الفيديوهات في قاعدة البيانات"""
    query = update.callback_query
    if query:
        await _safe_answer(query)
    
    try:
        from ...agent.supabase_storage import list_video_containers
        containers = await asyncio.to_thread(list_video_containers)
    except Exception as e:
        logger.error(f"Error listing containers: {e}")
        containers = []

    if not containers:
        text = "⚠️ لا توجد حاويات متاحة في قاعدة البيانات حالياً."
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")]]
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return AM_MENU

    text = "📦 <b>قائمة حاويات الفيديوهات:</b>\nاختر حاوية لاستعراض محتوياتها:"
    keyboard = []
    for c in containers:
        name = c.get("name", "بدون اسم")
        cid = c.get("id")
        keyboard.append([InlineKeyboardButton(f"📦 {name}", callback_data=f"am_cont_view:{cid}")])
    
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")])

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    
    return AM_VIEW_CONTAINERS

async def container_videos_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض الفيديوهات داخل حاوية محددة"""
    query = update.callback_query
    if not query:
        return AM_MENU
    
    await _safe_answer(query)
    cid = query.data.split(":", 1)[1]
    
    try:
        from ...agent.supabase_storage import list_container_videos, get_video_container
        container = await asyncio.to_thread(get_video_container, cid)
        videos = await asyncio.to_thread(list_container_videos, cid)
    except Exception as e:
        logger.error(f"Error listing container videos: {e}")
        videos = []
        container = None

    c_name = container.get("name", "الحاوية") if container else "الحاوية"
    
    if not videos:
        text = f"📦 <b>{html.escape(c_name)}:</b>\n\n⚠️ هذه الحاوية فارغة حالياً."
        keyboard = [[InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="am_view_containers")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_VIEW_CONTAINER_VIDEOS

    text = f"📦 <b>{html.escape(c_name)}:</b>\nيوجد {len(videos)} فيديو:\n\n"
    
    from datetime import datetime
    
    # عرض أول 15 فيديو فقط للتبسيط
    for i, v in enumerate(videos[:15], 1):
        title = v.get("title") or v.get("original_name") or "فيديو"
        date = ""
        if v.get("created_at"):
            try:
                dt = datetime.fromisoformat(v.get("created_at").replace("Z", "+00:00"))
                date = dt.strftime("%Y-%m-%d")
            except: pass
        text += f"{i}. 🎬 {html.escape(title[:40])} ({date})\n"

    if len(videos) > 15:
        text += f"\n<i>... وهناك {len(videos)-15} فيديوهات أخرى</i>"

    keyboard = [[InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="am_view_containers")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_VIEW_CONTAINER_VIDEOS

# ==================== عرض وإدارة فيديوهات الفيس كام ====================

async def facecam_videos_viewer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض جميع فيديوهات الفيس كام المخزنة في قاعدة البيانات"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    try:
        from ...agent.supabase_storage import FACECAM_STORAGE_LOCAL_PATH, _load_local_list
        from ...agent.supabase_client import supabase_select, USE_SUPABASE, is_online

        if USE_SUPABASE and is_online():
            rows = await asyncio.to_thread(supabase_select, "facecam_storage") or []
        else:
            rows = _load_local_list(FACECAM_STORAGE_LOCAL_PATH)
    except Exception as e:
        logger.error(f"Error listing facecam videos: {e}")
        rows = []

    if not rows:
        text = "🎬 <b>فيديوهات الفيس كام</b>\n\n⚠️ لا توجد فيديوهات فيس كام مخزنة حالياً."
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")]]
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return AM_MENU

    text = f"🎬 <b>فيديوهات الفيس كام</b>\n\nإجمالي: <code>{len(rows)}</code> فيديو\n\n"
    keyboard: List[List[InlineKeyboardButton]] = []

    for i, row in enumerate(rows[:20], 1):
        clip_id = row.get("id", "")[:8]
        source_id = row.get("source_id", "")[:8]
        created = ""
        if row.get("created_at"):
            try:
                dt = datetime.fromisoformat(row.get("created_at").replace("Z", "+00:00"))
                created = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        text += f"{i}. 🎬 <code>{clip_id}</code> | مصدر: <code>{source_id}</code> | {created}\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 حذف {clip_id}", callback_data=f"am_fc_del:{row.get('id')}")
        ])

    if len(rows) > 20:
        text += f"\n<i>... وعندك {len(rows) - 20} فيديو آخر</i>"

    keyboard.append([InlineKeyboardButton("🗑 حذف الكل", callback_data="am_fc_del_all_confirm")])
    keyboard.append([InlineKeyboardButton("🔄 تحديث", callback_data="am_fc_viewer")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")])

    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return AM_VIEW_FACECAM_VIDEOS


async def facecam_video_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف فيديو فيس كام واحد"""
    query = update.callback_query
    await _safe_answer(query)

    clip_id = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    if not clip_id:
        await query.answer("❌ معرف الفيديو غير صالح", show_alert=True)
        return await facecam_videos_viewer_menu(update, context)

    try:
        from ...agent.supabase_storage import delete_facecam_from_storage
        success = await asyncio.to_thread(delete_facecam_from_storage, clip_id)
        if success:
            await query.answer("🗑 تم حذف الفيديو بنجاح", show_alert=False)
        else:
            await query.answer("❌ فشل حذف الفيديو", show_alert=True)
    except Exception as e:
        logger.error(f"Error deleting facecam video: {e}")
        await query.answer(f"❌ خطأ: {str(e)[:50]}", show_alert=True)

    return await facecam_videos_viewer_menu(update, context)


async def facecam_delete_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تأكيد حذف جميع فيديوهات الفيس كام"""
    query = update.callback_query
    await _safe_answer(query)

    text = (
        "⚠️ <b>تأكيد حذف جميع فيديوهات الفيس كام</b>\n\n"
        "هل أنت متأكد من حذف جميع فيديوهات الفيس كام من قاعدة البيانات؟\n"
        "هذا الإجراء لا يمكن التراجع عنه!"
    )
    keyboard = [
        [InlineKeyboardButton("✅ نعم، احذف الكل", callback_data="am_fc_del_all_yes")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="am_fc_viewer")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return AM_VIEW_FACECAM_VIDEOS


async def facecam_delete_all_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ حذف جميع فيديوهات الفيس كام"""
    query = update.callback_query
    await _safe_answer(query)

    try:
        from ...agent.supabase_storage import (
            FACECAM_STORAGE_BUCKET,
            FACECAM_STORAGE_LOCAL_PATH,
            _load_local_list,
            _save_local_list,
        )
        from ...agent.supabase_client import (
            supabase_select,
            supabase_delete,
            supabase_storage_delete,
            USE_SUPABASE,
            is_online,
        )

        count = 0
        if USE_SUPABASE and is_online():
            rows = await asyncio.to_thread(supabase_select, "facecam_storage") or []
            for row in rows:
                clip_id = row.get("id")
                bucket = row.get("storage_bucket") or FACECAM_STORAGE_BUCKET
                obj = row.get("storage_path")
                if obj:
                    supabase_storage_delete(bucket, obj)
                if clip_id:
                    supabase_delete("facecam_storage", "id", clip_id)
                    count += 1

        local_items = _load_local_list(FACECAM_STORAGE_LOCAL_PATH)
        if local_items:
            for item in local_items:
                clip_id = item.get("id")
                bucket = item.get("storage_bucket") or FACECAM_STORAGE_BUCKET
                obj = item.get("storage_path")
                if obj:
                    supabase_storage_delete(bucket, obj)
                count += 1
            _save_local_list(FACECAM_STORAGE_LOCAL_PATH, [])

        await query.answer(f"🗑 تم حذف {count} فيديو", show_alert=True)
    except Exception as e:
        logger.error(f"Error deleting all facecam videos: {e}")
        await query.answer(f"❌ خطأ: {str(e)[:50]}", show_alert=True)

    return await facecam_videos_viewer_menu(update, context)


# ==================== تسجيل المعالجات ====================

async def _end_auto_mod_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END


def _auto_mod_common_nav_handlers() -> list:
    """أزرار التنقل الأساسية داخل نظام الأوتو مود.

    هذه الأزرار قد تظهر أحياناً في رسائل أقدم ما زالت تحتوي على لوحة أزرار.
    لذا نُبقيها فعّالة من أي حالة داخل المحادثة حتى لا يبدو الزر وكأنه لا يستجيب.
    """
    return [
        CallbackQueryHandler(sources_menu, pattern=r"^am_sources$"),
        CallbackQueryHandler(schedule_menu, pattern=r"^am_schedule$"),
        CallbackQueryHandler(status_view, pattern=r"^am_status$"),
        CallbackQueryHandler(config_menu, pattern=r"^am_config$"),
        CallbackQueryHandler(containers_viewer_menu, pattern=r"^am_view_containers$"),
        CallbackQueryHandler(facecam_videos_viewer_menu, pattern=r"^am_fc_viewer$"),
        CallbackQueryHandler(toggle_auto_fetch, pattern=r"^am_toggle$"),
        CallbackQueryHandler(run_now, pattern=r"^am_run_now$"),
        CallbackQueryHandler(test_render_menu, pattern=r"^am_test_render$"),
        CallbackQueryHandler(test_render_run, pattern=r"^am_test_render_src:"),
        CallbackQueryHandler(_list_channels_wrapper, pattern=r"^list_channels:"),
        CallbackQueryHandler(_open_ai_menu_from_auto_mod, pattern=r"^ai_main_menu$"),
        CallbackQueryHandler(_open_api_keys_menu_from_auto_mod, pattern=r"^api_keys_menu$"),
        CallbackQueryHandler(auto_mod_menu, pattern=r"^main_menu$"),
        CallbackQueryHandler(auto_mod_menu, pattern=r"^am_menu$"),
    ]


def get_auto_mod_conversation_handler() -> ConversationHandler:
    """إنشاء ConversationHandler لنظام الجلب التلقائي"""
    import warnings
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning, message=r"If 'per_message=False'.*")
    
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", auto_mod_menu),
            CommandHandler("menu", auto_mod_menu),
            CallbackQueryHandler(auto_mod_menu, pattern=r"^(am_menu|auto_mod)$"),
        ],
        states={
            AM_MENU: [
                *_auto_mod_common_nav_handlers(),
            ],
            AM_VIEW_CONTAINERS: [
                CallbackQueryHandler(container_videos_viewer, pattern=r"^am_cont_view:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_VIEW_CONTAINER_VIDEOS: [
                CallbackQueryHandler(containers_viewer_menu, pattern=r"^am_view_containers$"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_VIEW_FACECAM_VIDEOS: [
                CallbackQueryHandler(facecam_video_delete, pattern=r"^am_fc_del:"),
                CallbackQueryHandler(facecam_delete_all_confirm, pattern=r"^am_fc_del_all_confirm$"),
                CallbackQueryHandler(facecam_delete_all_execute, pattern=r"^am_fc_del_all_yes$"),
                CallbackQueryHandler(facecam_videos_viewer_menu, pattern=r"^am_fc_viewer$"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SOURCES: [
                CallbackQueryHandler(add_source_start, pattern=r"^am_add_source$"),
                CallbackQueryHandler(toggle_source, pattern=r"^am_toggle_src:"),
                CallbackQueryHandler(edit_source_start, pattern=r"^am_edit_src:"),
                CallbackQueryHandler(delete_source, pattern=r"^am_del_src:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_EDIT_SOURCE_CHANNEL: [
                CallbackQueryHandler(edit_source_refresh, pattern=r"^am_edit_src_menu$"),
                CallbackQueryHandler(edit_source_fetch_sources_menu, pattern=r"^am_edit_fetch_menu$"),
                CallbackQueryHandler(edit_source_fetch_add_prompt, pattern=r"^am_edit_fetch_add$"),
                CallbackQueryHandler(edit_source_fetch_toggle, pattern=r"^am_edit_fetch_toggle:"),
                CallbackQueryHandler(edit_source_fetch_delete, pattern=r"^am_edit_fetch_del:"),
                CallbackQueryHandler(edit_source_privacy_menu, pattern=r"^am_edit_priv_menu$"),
                CallbackQueryHandler(edit_source_hflip_menu, pattern=r"^am_edit_hflip_menu$"),
                CallbackQueryHandler(edit_source_overlay_menu, pattern=r"^am_edit_ov_menu$"),
                CallbackQueryHandler(edit_source_description_menu, pattern=r"^am_edit_desc_menu$"),
                CallbackQueryHandler(edit_source_tail_trim_menu, pattern=r"^am_edit_trim_menu$"),
                CallbackQueryHandler(edit_source_video_effect_menu, pattern=r"^am_edit_fx_menu:"),
                CallbackQueryHandler(edit_source_overlay_animation_menu, pattern=r"^am_edit_ov_anim_menu:"),
                CallbackQueryHandler(edit_source_channel_list, pattern=r"^am_edit_ch_start$"),
                CallbackQueryHandler(edit_source_duration_start, pattern=r"^am_edit_dur_start$"),
                CallbackQueryHandler(edit_source_facecam_start, pattern=r"^am_edit_fc_start$"),
                CallbackQueryHandler(edit_source_tail_trim_value, pattern=r"^am_edit_trim:"),
                CallbackQueryHandler(edit_source_video_effect_kind, pattern=r"^am_edit_fx_kind:"),
                CallbackQueryHandler(edit_source_video_effect_duration, pattern=r"^am_edit_fx_dur:"),
                CallbackQueryHandler(edit_source_overlay_animation_kind, pattern=r"^am_edit_ov_anim_kind:"),
                CallbackQueryHandler(edit_source_overlay_animation_duration, pattern=r"^am_edit_ov_anim_dur:"),
                CallbackQueryHandler(edit_source_overlay_toggle, pattern=r"^am_edit_ov_toggle$"),
                CallbackQueryHandler(edit_source_overlay_text_prompt, pattern=r"^am_edit_ov_text$"),
                CallbackQueryHandler(edit_source_overlay_mode, pattern=r"^am_edit_ov_mode:"),
                CallbackQueryHandler(edit_source_overlay_timing, pattern=r"^am_edit_ov_time:"),
                CallbackQueryHandler(edit_source_overlay_duration, pattern=r"^am_edit_ov_dur:"),
                CallbackQueryHandler(edit_source_overlay_position, pattern=r"^am_edit_ov_pos:"),
                CallbackQueryHandler(edit_source_overlay_delete, pattern=r"^am_edit_ov_del:"),
                CallbackQueryHandler(edit_source_description_toggle, pattern=r"^am_edit_desc_toggle$"),
                CallbackQueryHandler(edit_source_description_text_prompt, pattern=r"^am_edit_desc_text$"),
                CallbackQueryHandler(edit_source_description_mode, pattern=r"^am_edit_desc_mode:"),
                CallbackQueryHandler(edit_source_description_placement, pattern=r"^am_edit_desc_place:"),
                CallbackQueryHandler(edit_source_description_delete, pattern=r"^am_edit_desc_del:"),
                CallbackQueryHandler(edit_source_privacy_set, pattern=r"^am_edit_priv:"),
                CallbackQueryHandler(edit_source_hflip_set, pattern=r"^am_edit_hflip:"),
                CallbackQueryHandler(edit_source_raw_review_toggle, pattern=r"^am_edit_raw_toggle$"),
                CallbackQueryHandler(edit_shorts_only_toggle, pattern=r"^am_edit_shorts_only_toggle$"),
                CallbackQueryHandler(edit_source_choose_channel, pattern=r"^am_edit_ch:"),
                CallbackQueryHandler(edit_source_choose_duration, pattern=r"^am_set_dur:"),
                CallbackQueryHandler(edit_source_choose_facecam, pattern=r"^am_edit_fc:"),
                CallbackQueryHandler(edit_source_choose_facecam_pos, pattern=r"^am_edit_fc_pos:"),
                CallbackQueryHandler(edit_source_facecam_manage, pattern=r"^am_edit_fc_manage:"),
                MessageHandler(FACECAM_UPLOAD_FILTER, edit_source_facecam_upload_receive),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_CHANNEL: [
                CallbackQueryHandler(add_source_choose_channel, pattern=r"^am_src_ch:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_TYPE: [
                CallbackQueryHandler(add_source_choose_type, pattern=r"^am_src_type"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_CONTENT_TYPE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_source_custom_type),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_KIND: [
                CallbackQueryHandler(add_source_choose_kind, pattern=r"^am_src_kind:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_CONTAINER_SELECT: [
                CallbackQueryHandler(add_source_choose_container, pattern=r"^am_cont_"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_URL: [
                CallbackQueryHandler(add_source_choose_video_duration, pattern=r"^am_src_dur:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_source_url),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_FACECAM: [
                CallbackQueryHandler(add_source_choose_facecam, pattern=r"^am_src_fc:"),
                CallbackQueryHandler(add_source_choose_facecam_pos, pattern=r"^am_src_fc_pos:"),
                CallbackQueryHandler(add_source_facecam_manage, pattern=r"^am_src_fc_manage:"),
                CallbackQueryHandler(add_source_facecam_done, pattern=r"^am_src_fc_done$"),
                MessageHandler(FACECAM_UPLOAD_FILTER, add_source_facecam_upload_receive),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_CUSTOMIZE: [
                CallbackQueryHandler(add_source_overlay_menu, pattern=r"^am_add_overlay_menu$"),
                CallbackQueryHandler(add_source_description_menu, pattern=r"^am_add_desc_menu$"),
                CallbackQueryHandler(add_source_video_effect_menu, pattern=r"^am_src_fx_menu:"),
                CallbackQueryHandler(add_source_choose_tail_trim, pattern=r"^am_src_trim:"),
                CallbackQueryHandler(add_source_choose_video_effect_kind, pattern=r"^am_src_fx_kind:"),
                CallbackQueryHandler(add_source_choose_video_effect_duration, pattern=r"^am_src_fx_dur:"),
                CallbackQueryHandler(add_source_choose_hflip, pattern=r"^am_src_hflip:"),
                CallbackQueryHandler(add_source_choose_privacy, pattern=r"^am_src_privacy:"),
                CallbackQueryHandler(add_source_choose_overlay_enabled, pattern=r"^am_src_ov:"),
                CallbackQueryHandler(add_source_overlay_mode, pattern=r"^am_src_ov_mode:"),
                CallbackQueryHandler(add_source_overlay_timing, pattern=r"^am_src_ov_time:"),
                CallbackQueryHandler(add_source_overlay_duration, pattern=r"^am_src_ov_dur:"),
                CallbackQueryHandler(add_source_overlay_position, pattern=r"^am_src_ov_pos:"),
                CallbackQueryHandler(add_source_choose_overlay_animation_kind, pattern=r"^am_src_ov_anim_kind:"),
                CallbackQueryHandler(add_source_choose_overlay_animation_duration, pattern=r"^am_src_ov_anim_dur:"),
                CallbackQueryHandler(add_source_choose_description_enabled, pattern=r"^am_src_desc:"),
                CallbackQueryHandler(add_source_description_mode, pattern=r"^am_src_desc_mode:"),
                CallbackQueryHandler(add_source_description_placement, pattern=r"^am_src_desc_place:"),
                CallbackQueryHandler(add_source_raw_review_start, pattern=r"^am_add_raw_review_menu$"),
                CallbackQueryHandler(add_source_choose_raw_review, pattern=r"^am_src_raw_review:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SOURCE_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, source_text_input),
                CallbackQueryHandler(add_source_overlay_menu, pattern=r"^am_add_overlay_menu$"),
                CallbackQueryHandler(add_source_description_menu, pattern=r"^am_add_desc_menu$"),
                CallbackQueryHandler(edit_source_overlay_menu, pattern=r"^am_edit_ov_menu$"),
                CallbackQueryHandler(edit_source_description_menu, pattern=r"^am_edit_desc_menu$"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_ADD_SOURCE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_source_name),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SCHEDULE: [
                CallbackQueryHandler(schedule_pick_source, pattern=r"^am_sch_src:"),
                CallbackQueryHandler(toggle_schedule, pattern=r"^am_toggle_sch:"),
                CallbackQueryHandler(delete_schedule, pattern=r"^am_del_sch:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SCHEDULE_INTERVAL: [
                CallbackQueryHandler(schedule_pick_interval, pattern=r"^am_sch_ch:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SCHEDULE_LIMIT: [
                CallbackQueryHandler(schedule_pick_limit, pattern=r"^am_sch_int:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_SCHEDULE_HOURS: [
                CallbackQueryHandler(schedule_save, pattern=r"^am_sch_lim:"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_STATUS: [
                *_auto_mod_common_nav_handlers(),
            ],
            AM_CONFIG: [
                CallbackQueryHandler(cookies_upload_start, pattern=r"^am_cookies_start$"),
                CallbackQueryHandler(client_secret_upload_start, pattern=r"^am_client_secret_start$"),
                CallbackQueryHandler(gdrive_connect_start, pattern=r"^am_gdrive_connect$"),
                CallbackQueryHandler(gdrive_receive_auth_url, pattern=r"^am_gdrive_have_url$"),
                CallbackQueryHandler(config_toggle, pattern=r"^am_cfg_"),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_COOKIES_UPLOAD: [
                MessageHandler(filters.Document.ALL, receive_cookies_file),
                *_auto_mod_common_nav_handlers(),
            ],
            AM_CLIENT_SECRET_UPLOAD: [
                MessageHandler(filters.Document.ALL, receive_client_secret_file),
                *_auto_mod_common_nav_handlers(),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(auto_mod_menu, pattern=r"^am_menu$"),
            CallbackQueryHandler(_end_auto_mod_conversation, pattern=r"^am_end$"),
        ],
        name="auto_mod_conversation",
        persistent=False,
        allow_reentry=True,
        per_message=False
    )
