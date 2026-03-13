"""
معالجات تعديل إعدادات القنوات والنشر
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters
import logging
import asyncio
import os
from typing import Dict, Optional
import uuid
from datetime import datetime

from ..channel_manager import ChannelManager
from ..persistence import load_state, save_state
from ...agent.config import load_config
from ...agent.ffmpeg_utils import convert_still_image_to_loop_video
from ...agent.renderer import generate_overlay_preview
from telegram.error import BadRequest
import re
from typing import Dict, Optional, List
import html

logger = logging.getLogger(__name__)


OVERLAY_TEXT_INPUT = "OVERLAY_TEXT_INPUT"
OVERLAY_SIZE_INPUT = "OVERLAY_SIZE_INPUT"
CUSTOM_DESC_INPUT = "CUSTOM_DESC_INPUT"
DESCRIPTION_SECTIONS_INPUT = "DESCRIPTION_SECTIONS_INPUT"
FACECAM_SCALE_INPUT = "FACECAM_SCALE_INPUT"
FACECAM_X_INPUT = "FACECAM_X_INPUT"
FACECAM_Y_INPUT = "FACECAM_Y_INPUT"
FACECAM_UPLOAD_INPUT = "FACECAM_UPLOAD_INPUT"
FACECAM_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
FACECAM_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
try:
    FACECAM_UPLOAD_FILTER = filters.VIDEO | filters.Document.VIDEO | filters.PHOTO | filters.Document.IMAGE
except Exception:
    FACECAM_UPLOAD_FILTER = filters.VIDEO | filters.Document.ALL | filters.PHOTO

CUSTOM_OVERLAY_TEXT = "CUSTOM_OVERLAY_TEXT"
CUSTOM_OVERLAY_TIMING = "CUSTOM_OVERLAY_TIMING"
CUSTOM_OVERLAY_DURATION = "CUSTOM_OVERLAY_DURATION"
CUSTOM_OVERLAY_POSITION = "CUSTOM_OVERLAY_POSITION"


def _clean_text_for_telegram(text: str) -> str:
    """
    تنظيف النص وتجهيزه للعرض في تيليجرام باستخدام HTML
    """
    if not text:
        return ""
    
    # الهروب من رموز HTML الأساسية لضمان عدم تعطل البوت
    text = html.escape(text)
    
    # إزالة الرموز غير القابلة للطباعة
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    
    # الحفاظ على الأسطر الجديدة ولكن تنظيف الفراغات المتكررة
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()


# ==================== تعديل الإعدادات ====================


def _legacy_facecam_document_is_image(document) -> bool:
    mime_type = str(getattr(document, "mime_type", "") or "").strip().lower()
    file_name = str(getattr(document, "file_name", "") or "")
    extension = os.path.splitext(file_name)[1].lower()
    return mime_type.startswith("image/") or extension in FACECAM_IMAGE_EXTENSIONS


def _legacy_facecam_clips_map(state) -> dict:
    clips_map = state.get("facecam_clips_by_channel")
    if not isinstance(clips_map, dict):
        clips_map = {}
        state["facecam_clips_by_channel"] = clips_map
    return clips_map

async def toggle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تفعيل/تعطيل القناة"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    
    if not channel:
        logger.error(f"toggle_channel: channel not found for id={channel_id}, callback_data={query.data}")
        await query.edit_message_text("❌ القناة غير موجودة")
        return
    
    # عكس الحالة
    new_status = not channel.enabled
    manager.update_channel(channel_id, enabled=new_status)
    
    status_text = "تفعيل" if new_status else "تعطيل"
    await query.answer(f"✅ تم {status_text} القناة")
    
    # إعادة عرض صفحة القناة
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


async def edit_content_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تعديل نوع المحتوى"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    text = "🎮 <b>اختر نوع المحتوى الجديد:</b>"
    
    keyboard = [
        [InlineKeyboardButton("🎮 ماين كرافت", callback_data=f"set_content:minecraft:{channel_id}")],
        [InlineKeyboardButton("🎮 ألعاب (فيسبوك)", callback_data=f"set_content:games:{channel_id}")],
        [InlineKeyboardButton("🎬 محتوى آخر", callback_data=f"set_content:other:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_content_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ نوع المحتوى"""
    query = update.callback_query
    await query.answer()
    
    _, content_type, channel_id = query.data.split(':')
    
    manager = ChannelManager()
    manager.update_channel(channel_id, content_type=content_type)
    
    content_name = "ماين كرافت" if content_type == "minecraft" else ("ألعاب" if content_type == "games" else "محتوى آخر")
    await query.answer(f"✅ تم تغيير نوع المحتوى إلى: {content_name}")
    
    # إعادة عرض صفحة القناة
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


async def edit_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تعديل الخصوصية"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    text = "🔒 <b>اختر خصوصية الفيديو:</b>"
    
    keyboard = [
        [InlineKeyboardButton("🌍 عام (Public)", callback_data=f"set_privacy:public:{channel_id}")],
        [InlineKeyboardButton("🔗 غير مدرج (Unlisted)", callback_data=f"set_privacy:unlisted:{channel_id}")],
        [InlineKeyboardButton("🔒 خاص (Private)", callback_data=f"set_privacy:private:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ الخصوصية"""
    query = update.callback_query
    await query.answer()
    
    _, privacy, channel_id = query.data.split(':')
    
    manager = ChannelManager()
    manager.update_channel(channel_id, privacy=privacy)
    
    privacy_map = {"public": "عام", "unlisted": "غير مدرج", "private": "خاص"}
    privacy_name = privacy_map.get(privacy, privacy)
    await query.answer(f"✅ تم تغيير الخصوصية إلى: {privacy_name}")
    
    # إعادة عرض صفحة القناة
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


async def edit_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تعديل فترة النشر"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    text = "⏰ <b>اختر فترة النشر الجديدة:</b>"
    
    keyboard = [
        [InlineKeyboardButton("⏱️ كل دقيقة", callback_data=f"set_interval:60:{channel_id}")],
        [InlineKeyboardButton("🕧 كل نصف ساعة", callback_data=f"set_interval:1800:{channel_id}")],
        [InlineKeyboardButton("⏰ كل ساعة", callback_data=f"set_interval:3600:{channel_id}")],
        [InlineKeyboardButton("⏰ كل ساعتين", callback_data=f"set_interval:7200:{channel_id}")],
        [InlineKeyboardButton("⏰ كل 3 ساعات", callback_data=f"set_interval:10800:{channel_id}")],
        [InlineKeyboardButton("⏰ كل 6 ساعات", callback_data=f"set_interval:21600:{channel_id}")],
        [InlineKeyboardButton("⏰ كل 12 ساعة", callback_data=f"set_interval:43200:{channel_id}")],
        [InlineKeyboardButton("⏰ كل 24 ساعة", callback_data=f"set_interval:86400:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ فترة النشر"""
    query = update.callback_query
    await query.answer()
    
    _, interval_str, channel_id = query.data.split(':')
    interval = int(interval_str)
    
    manager = ChannelManager()
    manager.update_channel(channel_id, publish_interval=interval)
    
    if interval < 3600:
        minutes = max(1, interval // 60)
        interval_text = "كل دقيقة" if minutes == 1 else ("كل نصف ساعة" if minutes == 30 else f"كل {minutes} دقيقة")
    else:
        hours = interval // 3600
        interval_text = f"كل {hours} ساعة" if hours > 1 else "كل ساعة"
    await query.answer(f"✅ تم تغيير فترة النشر إلى: {interval_text}")
    
    # إعادة عرض صفحة القناة
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


async def edit_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تعديل جودة الفيديو"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    quality_options = [
        ("🎞️ 480p", "480p"),
        ("🎬 720p (افتراضي)", "720p"),
        ("🎥 1080p", "1080p"),
        ("📺 1440p", "1440p"),
        ("🖥️ 4K", "2160p"),
        ("⚡ AOTU (أفضل جودة تلقائية)", "aotu"),
    ]
    
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"set_quality:{value}:{channel_id}")]
        for label, value in quality_options
    ]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")])
    
    text = "🎞️ <b>اختر جودة الفيديو المطلوبة:</b>\n\n" \
           "⚡ <b>AOTU:</b> سيحاول البوت دائماً الحصول على أفضل جودة متاحة للفيديو (أعلى دقة و <code>bitrate</code>)"
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ جودة الفيديو"""
    query = update.callback_query
    await query.answer()
    
    _, quality, channel_id = query.data.split(':')
    
    manager = ChannelManager()
    manager.update_channel(channel_id, video_quality=quality)
    
    quality_names = {
        "480p": "480p",
        "720p": "720p",
        "1080p": "1080p",
        "1440p": "1440p",
        "2160p": "4K",
    }
    quality_label = quality_names.get(quality, quality)
    await query.answer(f"✅ تم ضبط الجودة على: {quality_label}")
    
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


# ==================== إعدادات نص ماين كرافت ====================

async def edit_minecraft_overlay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض خيارات تعديل نص التعليق"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة")
        return
    
    extra = getattr(channel, "extra_data", {}) or {}
    current_pos = extra.get("overlay_position", "bottom_center")
    current_text = (extra.get("overlay_text") or "").strip()
    current_size = extra.get("overlay_font_size")
    
    pos_text = "أعلى" if str(current_pos).startswith("top") else "أسفل"
    
    text = (
        "🅰️ <b>إعدادات نص التعليق للفيديو</b>\n\n"
        f"📝 النص: <b>{'مفعل' if extra.get('overlay_enabled', True) else 'معطل'}</b>\n"
        f"📍 الموضع: <b>{pos_text}</b>\n"
        f"🔤 الخط: <b>{'مخصص' if extra.get('overlay_font_path') else 'افتراضي'}</b>\n\n"
        f"📝 النص المخصص: <b>{'موجود' if current_text else 'غير محدد'}</b>\n"
        f"🔠 حجم الخط: <b>{current_size if current_size else 64}</b>\n\n"
        "يمكنك تعديل إعدادات النص الذي يظهر في كل فيديو لهذه القناة."
    )
    
    keyboard = [
        [
            InlineKeyboardButton("📝 تفعيل/تعطيل النص", callback_data=f"toggle_overlay_text:{channel_id}"),
            InlineKeyboardButton("📍 تغيير الموضع", callback_data=f"edit_overlay_position:{channel_id}")
        ],
        [
            InlineKeyboardButton("✏️ تعيين نص مخصص", callback_data=f"set_overlay_text_start:{channel_id}"),
            InlineKeyboardButton("🔠 تغيير حجم الخط", callback_data=f"set_overlay_size_start:{channel_id}"),
        ],
        [
            InlineKeyboardButton("🔄 تعيين خط مخصص", callback_data=f"start_overlay_font_upload:{channel_id}"),
            InlineKeyboardButton("🗑️ حذف الخط", callback_data=f"remove_overlay_font:{channel_id}")
        ],
        [InlineKeyboardButton("👁️ معاينة الخط", callback_data=f"preview_overlay_font:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def toggle_overlay_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل تفعيل/تعطيل نص التعليق"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    
    if not channel:
        await query.answer("❌ القناة غير موجودة")
        return
    
    extra = getattr(channel, "extra_data", {}) or {}
    current_state = extra.get("overlay_enabled", True)
    
    # تحديث الحالة المعاكسة
    extra["overlay_enabled"] = not current_state
    manager.update_channel(channel_id, extra_data=extra)
    
    status = "تم تفعيل" if not current_state else "تم تعطيل"
    await query.answer(f"✅ {status} نص التعليق", show_alert=True)
    
    # تحديث الواجهة
    await edit_minecraft_overlay(update, context)


async def set_overlay_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    context.user_data["channel_id"] = channel_id
    await query.edit_message_text(
        text=(
            "✏️ <b>تعيين نص مخصص للقناة</b>\n\n"
            "أرسل النص الذي تريد ظهوره دائماً في فيديوهات هذه القناة.\n"
            "للإلغاء اضغط زر الرجوع."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_minecraft_overlay:{channel_id}")]]),
        parse_mode='HTML'
    )
    return OVERLAY_TEXT_INPUT


async def set_overlay_text_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get("channel_id")
    if not channel_id or not update.message:
        return ConversationHandler.END
    txt = (update.message.text or "").strip()
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await update.message.reply_text("❌ القناة غير موجودة")
        return ConversationHandler.END
    extra = getattr(channel, "extra_data", {}) or {}
    extra["overlay_text"] = txt
    manager.update_channel(channel_id, extra_data=extra)
    await update.message.reply_text("✅ تم حفظ النص المخصص")
    return ConversationHandler.END


async def set_overlay_size_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    context.user_data["channel_id"] = channel_id
    await query.edit_message_text(
        text=(
            "🔠 <b>تغيير حجم خط النص</b>\n\n"
            "أرسل رقم حجم الخط (مثال: <code>64</code> أو <code>72</code>).\n"
            "للإلغاء اضغط زر الرجوع."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_minecraft_overlay:{channel_id}")]]),
        parse_mode='HTML'
    )
    return OVERLAY_SIZE_INPUT


async def set_overlay_size_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get("channel_id")
    if not channel_id or not update.message:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    try:
        size = int(raw)
    except Exception:
        await update.message.reply_text("❌ أدخل رقم صحيح.")
        return OVERLAY_SIZE_INPUT
    if size < 18 or size > 200:
        await update.message.reply_text("❌ الحجم غير منطقي. اختر رقم بين 18 و 200.")
        return OVERLAY_SIZE_INPUT
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await update.message.reply_text("❌ القناة غير موجودة")
        return ConversationHandler.END
    extra = getattr(channel, "extra_data", {}) or {}
    extra["overlay_font_size"] = size
    manager.update_channel(channel_id, extra_data=extra)
    await update.message.reply_text("✅ تم حفظ حجم الخط")
    return ConversationHandler.END


async def edit_custom_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة")
        return

    extra = getattr(channel, "extra_data", {}) or {}
    custom_desc = (extra.get("custom_description") or "").strip()
    mode = (extra.get("custom_description_mode") or "append").strip().lower()

    mode_label = {"append": "إلحاق بعد وصف AI", "prepend": "قبل وصف AI", "template": "قالب {ai}"}.get(mode, mode)

    text = (
        "📄 <b>إعدادات وصف الفيديو</b>\n\n"
        f"✅ وصف مخصص: <b>{'موجود' if custom_desc else 'غير محدد'}</b>\n"
        f"🧩 الدمج: <b>{mode_label}</b>\n\n"
        "يمكنك حفظ وصف طويل لكل قناة، وسيتم دمجه تلقائياً مع وصف الذكاء الاصطناعي عند النشر."
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ تعيين/تعديل الوصف", callback_data=f"set_custom_desc_start:{channel_id}"),
            InlineKeyboardButton("🧩 طريقة الدمج", callback_data=f"edit_custom_desc_mode:{channel_id}"),
        ],
        [InlineKeyboardButton("🗑️ حذف الوصف", callback_data=f"delete_custom_desc:{channel_id}")],
        [
            InlineKeyboardButton("📄 إدارة أقسام الوصف", callback_data=f"edit_desc_sections:{channel_id}")
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")],
    ]

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_custom_description_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    context.user_data["channel_id"] = channel_id

    await query.edit_message_text(
        text=(
            "✏️ <b>إرسال الوصف المخصص</b>\n\n"
            "أرسل الآن الوصف الذي تريد إضافته لكل فيديو لهذه القناة.\n"
            "إذا أردت التحكم بمكان وصف الذكاء الاصطناعي داخل النص، استخدم <code>{ai}</code> داخل الوصف."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_custom_desc:{channel_id}")]]),
        parse_mode='HTML'
    )
    return CUSTOM_DESC_INPUT


async def set_custom_description_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get("channel_id")
    if not channel_id or not update.message:
        return ConversationHandler.END

    desc = (update.message.text or "").strip()
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await update.message.reply_text("❌ القناة غير موجودة")
        return ConversationHandler.END

    extra = getattr(channel, "extra_data", {}) or {}
    extra["custom_description"] = desc
    if not (extra.get("custom_description_mode") or "").strip():
        extra["custom_description_mode"] = "append"
    manager.update_channel(channel_id, extra_data=extra)
    await update.message.reply_text("✅ تم حفظ الوصف المخصص")
    return ConversationHandler.END


async def edit_description_sections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة")
        return
    extra = getattr(channel, "extra_data", {}) or {}
    sections = extra.get("description_sections") or []
    sections_mode = (extra.get("sections_mode") or "append").strip().lower()
    count = len(sections) if isinstance(sections, list) else 0
    mode_label = {"append": "إلحاق بعد وصف AI", "prepend": "قبل وصف AI", "template": "قالب {sections}"}.get(sections_mode, sections_mode)
    text = (
        "📄 إعداد أقسام الوصف الثابتة لهذه القناة\n\n"
        f"عدد الأقسام: {count}\n"
        f"طريقة الدمج: {mode_label}\n\n"
        "يمكنك حفظ أقسام بعناوين ثابتة تظهر في كل وصف للفيديو على هذه القناة."
    )
    keyboard = [
        [InlineKeyboardButton("✏️ تعيين/تعديل الأقسام", callback_data=f"set_desc_sections_start:{channel_id}")],
        [InlineKeyboardButton("🧩 طريقة الدمج", callback_data=f"edit_desc_sections_mode:{channel_id}")],
        [InlineKeyboardButton("🗑️ حذف الأقسام", callback_data=f"delete_desc_sections:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_custom_desc:{channel_id}")],
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def set_description_sections_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    context.user_data["channel_id"] = channel_id
    await query.edit_message_text(
        text=(
            "أرسل الأقسام بصيغة JSON قائمة من عناصر تحتوي title و content.\n"
            "مثال:\n"
            '<code>[{"title":"روابط","content":"موقعنا: example.com"}, {"title":"سياسة","content":"لا ننتهك حقوق"}]</code>'
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_desc_sections:{channel_id}")]]),
        parse_mode='HTML'
    )
    return DESCRIPTION_SECTIONS_INPUT


async def set_description_sections_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get("channel_id")
    if not channel_id or not update.message:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    import json
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("invalid")
        norm = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if not title and not content:
                continue
            norm.append({"title": title, "content": content})
        manager = ChannelManager()
        channel = manager.get_channel(channel_id)
        if not channel:
            await update.message.reply_text("❌ القناة غير موجودة")
            return ConversationHandler.END
        extra = getattr(channel, "extra_data", {}) or {}
        extra["description_sections"] = norm
        if not (extra.get("sections_mode") or "").strip():
            extra["sections_mode"] = "append"
        manager.update_channel(channel_id, extra_data=extra)
        await update.message.reply_text("✅ تم حفظ الأقسام")
    except Exception:
        await update.message.reply_text("❌ صيغة غير صحيحة. يرجى إرسال JSON صالح.")
        return DESCRIPTION_SECTIONS_INPUT
    return ConversationHandler.END


async def edit_description_sections_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    kb = [
        [
            InlineKeyboardButton("إلحاق بعد وصف AI", callback_data=f"set_desc_sections_mode:append:{channel_id}"),
            InlineKeyboardButton("قبل وصف AI", callback_data=f"set_desc_sections_mode:prepend:{channel_id}"),
        ],
        [InlineKeyboardButton("قالب {sections}", callback_data=f"set_desc_sections_mode:template:{channel_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_desc_sections:{channel_id}")],
    ]
    await query.edit_message_text("اختر طريقة دمج الأقسام:", reply_markup=InlineKeyboardMarkup(kb))


async def set_description_sections_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(':')
    mode = parts[1]
    channel_id = parts[2]
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.answer("❌ القناة غير موجودة", show_alert=True)
        return
    extra = getattr(channel, "extra_data", {}) or {}
    extra["sections_mode"] = mode
    manager.update_channel(channel_id, extra_data=extra)
    await edit_description_sections(update, context)


async def delete_description_sections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.answer("❌ القناة غير موجودة", show_alert=True)
        return
    extra = getattr(channel, "extra_data", {}) or {}
    extra.pop("description_sections", None)
    manager.update_channel(channel_id, extra_data=extra)
    await edit_description_sections(update, context)


async def delete_custom_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.answer("❌ القناة غير موجودة")
        return

    extra = getattr(channel, "extra_data", {}) or {}
    extra.pop("custom_description", None)
    extra.pop("custom_description_mode", None)
    manager.update_channel(channel_id, extra_data=extra)
    await query.answer("✅ تم حذف الوصف")
    update.callback_query.data = f"edit_custom_desc:{channel_id}"
    await edit_custom_description(update, context)


async def edit_facecam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # استخراج channel_id مع دفاع برمجي
    parts = query.data.split(':')
    channel_id = parts[1] if len(parts) > 1 else context.user_data.get("facecam_pos_channel_id")
    
    if not channel_id:
        await query.answer("❌ لم يتم العثور على معرف القناة")
        return

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة")
        return

    extra = getattr(channel, "extra_data", {}) or {}
    enabled = bool(extra.get("facecam_enabled", False))
    clip_id = (extra.get("facecam_clip_id") or "").strip()
    pos = (extra.get("facecam_position") or "top_center").strip().lower()
    scale = extra.get("facecam_scale")

    cfg = None
    try:
        from ...agent.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    st = load_state(cfg)
    clips_map = _legacy_facecam_clips_map(st)
    clips = clips_map.get(channel_id) or []
    selected_name = None
    for it in clips:
        if str(it.get("id") or "").strip() == clip_id:
            selected_name = it.get("name") or "FaceCam"
            break

    import html
    escaped_selected_name = html.escape(selected_name if selected_name else 'غير محدد')
    escaped_pos = html.escape(pos)

    text = (
        "🎥 <b>إعدادات FaceCam</b>\n\n"
        f"✅ مفعّل: <b>{'نعم' if enabled else 'لا'}</b>\n"
        f"🎞️ المقطع: <b>{escaped_selected_name}</b>\n"
        f"📍 الموضع: <b>{escaped_pos}</b>\n"
        f"📐 الحجم: <b>{scale if scale is not None else 0.40}</b>\n\n"
        "يمكنك رفع مقاطع FaceCam واختيارها لأي قناة مع التحكم بالحجم والمكان.\n"
        "ملاحظة: عند الضغط على إعادة التعيين، يتم ضبط الموضع إلى أعلى المنتصف والحجم إلى 0.40."
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ تفعيل/تعطيل", callback_data=f"toggle_facecam:{channel_id}"),
            InlineKeyboardButton("🎞️ اختيار مقطع", callback_data=f"facecam_select:{channel_id}"),
        ],
        [
            InlineKeyboardButton("📤 رفع مقطع جديد", callback_data=f"facecam_upload_start:{channel_id}"),
        ],
        [
            InlineKeyboardButton("📍 ضبط الموضع والحجم", callback_data=f"facecam_control_menu:{channel_id}"),
            InlineKeyboardButton("👁️ معاينة التنسيق", callback_data=f"preview_facecam_layout:{channel_id}"),
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")],
    ]

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def toggle_facecam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.answer("❌ القناة غير موجودة")
        return
    extra = getattr(channel, "extra_data", {}) or {}
    extra["facecam_enabled"] = not bool(extra.get("facecam_enabled", False))
    manager.update_channel(channel_id, extra_data=extra)
    await query.answer("✅ تم تحديث FaceCam")
    update.callback_query.data = f"edit_facecam:{channel_id}"
    await edit_facecam(update, context)


async def facecam_select_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]

    cfg = None
    try:
        from ...agent.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    st = load_state(cfg)
    clips_map = _legacy_facecam_clips_map(st)
    clips = clips_map.get(channel_id) or []

    context.user_data["facecam_select_channel_id"] = channel_id

    text = "🎞️ <b>إدارة مكتبة FaceCam</b>\n\n"
    text += "اختر مقطعاً للتحكم به (المعاينة، الحذف، أو تغيير حالة التفعيل)."
    
    if not clips:
        text = "🎞️ <b>مكتبة FaceCam</b>\n\nلا توجد مقاطع حالياً لهذه القناة. استخدم زر رفع مقطع جديد."
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_facecam:{channel_id}")]]
    else:
        keyboard = []
        for idx, it in enumerate(clips[:30]):
            enabled = it.get("enabled", True)
            badge = "✅" if enabled else "❌"
            name = (it.get("name") or "FaceCam")[:24]
            keyboard.append([InlineKeyboardButton(f"{badge} {name}", callback_data=f"manage_facecam_clip:{idx}:{channel_id}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_facecam:{channel_id}")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def set_facecam_clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(':')[1])
    channel_id = context.user_data.get("facecam_select_channel_id")
    ids = context.user_data.get("facecam_select_ids") or []
    if not channel_id or idx < 0 or idx >= len(ids):
        await query.answer("❌ اختيار غير صالح")
        return
    clip_id = str(ids[idx]).strip()
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if not channel:
        await query.answer("❌ القناة غير موجودة")
        return
    extra = getattr(channel, "extra_data", {}) or {}
    extra["facecam_clip_id"] = clip_id
    manager.update_channel(channel_id, extra_data=extra)
    await query.answer("✅ تم اختيار مقطع FaceCam")
    update.callback_query.data = f"edit_facecam:{channel_id}"
    await edit_facecam(update, context)


async def facecam_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]

    cfg = None
    try:
        from ...agent.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    st = load_state(cfg)
    clips_map = _legacy_facecam_clips_map(st)
    clips = clips_map.get(channel_id) or []

    context.user_data["facecam_delete_channel_id"] = channel_id
    context.user_data["facecam_delete_ids"] = [str(it.get("id") or "") for it in clips]

    text = "🗑️ <b>اختر مقطع لحذفه من المكتبة</b>\n\n"
    if not clips:
        text += "لا توجد مقاطع."
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data=f"facecam_select:{channel_id}")]]
    else:
        keyboard = []
        for idx, it in enumerate(clips[:30]):
            name = (it.get("name") or "FaceCam")[:28]
            keyboard.append([InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_facecam_clip_idx:{idx}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"facecam_select:{channel_id}")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def delete_facecam_clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(':')[1])
    channel_id = context.user_data.get("facecam_delete_channel_id")
    ids = context.user_data.get("facecam_delete_ids") or []
    if not channel_id or idx < 0 or idx >= len(ids):
        await query.answer("❌ اختيار غير صالح")
        return
    clip_id = str(ids[idx]).strip()

    cfg = None
    try:
        from ...agent.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    st = load_state(cfg)
    clips_map = _legacy_facecam_clips_map(st)
    clips = clips_map.get(channel_id) or []
    new_clips = []
    removed_path = None
    for it in clips:
        if str(it.get("id") or "").strip() == clip_id:
            removed_path = it.get("path")
            continue
        new_clips.append(it)
    clips_map[channel_id] = new_clips
    st["facecam_clips_by_channel"] = clips_map
    save_state(st, cfg)

    try:
        manager = ChannelManager()
        channel = manager.get_channel(channel_id)
        if channel:
            extra = getattr(channel, "extra_data", {}) or {}
            if str(extra.get("facecam_clip_id") or "").strip() == clip_id:
                extra.pop("facecam_clip_id", None)
                manager.update_channel(channel_id, extra_data=extra)
    except Exception:
        pass
    if removed_path:
        try:
            if os.path.exists(removed_path):
                os.remove(removed_path)
        except Exception:
            pass
    await query.answer("✅ تم حذف المقطع")
    update.callback_query.data = f"facecam_select:{channel_id}"
    await facecam_select_menu(update, context)


async def facecam_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    context.user_data["channel_id"] = channel_id

    await query.edit_message_text(
        text=(
            "📤 <b>رفع مقطع FaceCam</b>\n\n"
            "أرسل الآن فيديو أو صورة FaceCam. سيتم تحويل الصورة تلقائيًا إلى فيديو متوافق ثم حفظها في مكتبة هذه القناة."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_facecam:{channel_id}")]]),
        parse_mode='HTML'
    )
    return FACECAM_UPLOAD_INPUT


async def facecam_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get("channel_id")
    if not channel_id or not update.message:
        return ConversationHandler.END

    doc = None
    treat_as_image = False
    if update.message.video:
        doc = update.message.video
        fname = "facecam.mp4"
        file_id = doc.file_id
    elif update.message.photo:
        doc = update.message.photo[-1]
        fname = "facecam.jpg"
        file_id = doc.file_id
        treat_as_image = True
    elif update.message.document:
        doc = update.message.document
        fname = doc.file_name or "facecam.mp4"
        file_id = doc.file_id
        treat_as_image = _legacy_facecam_document_is_image(doc)
    else:
        await update.message.reply_text("❌ أرسل فيديو أو صورة صالحة.")
        return FACECAM_UPLOAD_INPUT

    # التحقق من حجم الملف (حد تيليجرام 20 ميجا للبوتات العادية)
    cfg = load_config()
    if not cfg.LOCAL_BOT_API_URL and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "❌ *الملف كبير جداً!*\n\n"
            "تيليجرام يسمح للبوتات بتحميل ملفات بحد أقصى *20 ميجابايت* فقط.\n"
            "يرجى إرسال ملف أصغر لاستخدامه كـ FaceCam.\n\n"
            "💡 *نصيحة:* يمكنك استخدام خادم Bot API محلي لرفع الحد إلى 2000 ميجا.",
            parse_mode="Markdown"
        )
        return FACECAM_UPLOAD_INPUT

    st = load_state(cfg)
    clips_map = _legacy_facecam_clips_map(st)
    clips = clips_map.get(channel_id) or []
    fc_id = str(uuid.uuid4())
    out_dir = os.path.join(".data", "facecam")
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(fname)[1].lower()
    if treat_as_image:
        ext = ext or ".jpg"
        if ext not in FACECAM_IMAGE_EXTENSIONS:
            await update.message.reply_text("❌ صيغة الصورة غير مدعومة. استخدم jpg أو jpeg أو png أو webp أو bmp.")
            return FACECAM_UPLOAD_INPUT
        raw_path = os.path.join(out_dir, f"{fc_id}_src{ext}")
        out_path = os.path.join(out_dir, f"{fc_id}.mp4")
    else:
        ext = ext or ".mp4"
        if ext not in FACECAM_VIDEO_EXTENSIONS:
            await update.message.reply_text("❌ صيغة غير مدعومة. استخدم mp4/mov/webm أو أرسل صورة مدعومة.")
            return FACECAM_UPLOAD_INPUT
        raw_path = os.path.join(out_dir, f"{fc_id}{ext}")
        out_path = raw_path

    try:
        f = await context.bot.get_file(file_id)
        await f.download_to_drive(raw_path)
    except Exception as e:
        await update.message.reply_text(f"❌ فشل حفظ الملف: {e}")
        return ConversationHandler.END

    if treat_as_image:
        if not convert_still_image_to_loop_video(raw_path, out_path):
            try:
                if os.path.isfile(raw_path):
                    os.remove(raw_path)
            except Exception:
                pass
            await update.message.reply_text("❌ تعذر تحويل الصورة إلى فيديو متوافق مع FaceCam.")
            return FACECAM_UPLOAD_INPUT
        try:
            if os.path.isfile(raw_path):
                os.remove(raw_path)
        except Exception:
            pass
        fname = f"{os.path.splitext(fname)[0] or 'facecam'}.mp4"

    clip_entry = {
        "id": fc_id,
        "path": out_path,
        "name": fname,
        "enabled": True,  # 🆕 تفعيل المقطع تلقائياً عند الرفع
        "created_at": datetime.now().isoformat(),
    }
    clips.append(clip_entry)
    clips_map[channel_id] = clips
    save_state(st, cfg)
    await update.message.reply_text("✅ تم رفع المقطع/الصورة بنجاح وتفعيلها لهذه القناة.")
    return ConversationHandler.END


# ==================== حذف القناة ====================

async def delete_channel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض رسالة تأكيد حذف القناة"""
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    
    from ..channel_manager import ChannelManager
    cm = ChannelManager()
    channel = cm.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة.")
        return

    import html
    escaped_channel_name = html.escape(channel.channel_name)
    text = (
        f"🚨 <b>تأكيد حذف القناة</b>\n\n"
        f"هل أنت متأكد من رغبتك في حذف القناة: <b>{escaped_channel_name}</b>؟\n"
        f"سيتم حذف كافة الإعدادات والتوكنات المرتبطة بها.\n\n"
        f"⚠️ <b>تحذير:</b> لا يمكن التراجع عن هذه الخطوة."
    )
    keyboard = [
        [InlineKeyboardButton("✅ نعم، احذف القناة نهائياً", callback_data=f"confirm_delete:{channel_id}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_channel:{channel_id}")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def delete_channel_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ عملية الحذف الفعلي للقناة"""
    query = update.callback_query
    await query.answer()
    channel_id = query.data.split(':')[1]
    
    from ..channel_manager import ChannelManager
    cm = ChannelManager()
    success = cm.delete_channel(channel_id)
    
    if success:
        text = "✅ <b>تم حذف القناة وكافة إعداداتها بنجاح.</b>"
    else:
        text = "❌ <b>حدث خطأ أثناء محاولة حذف القناة. قد لا تكون موجودة.</b>"
        
    keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==================== نص مخصص للفيديو (Custom Overlay Text) ====================

async def edit_custom_overlay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة النصوص المخصصة الحالية وتوفير خيار إضافة جديد"""
    query = update.callback_query
    await query.answer()

    channel_id = query.data.split(':')[1]
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)

    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة.")
        return

    context.user_data['edit_channel_id'] = channel_id

    # عرض النصوص الحالية
    overlays = getattr(channel, "custom_overlay_texts", None) or []
    
    import html
    safe_name = html.escape(channel.channel_name)
    text = f"✏️ <b>النصوص المخصصة للقناة:</b> <code>{safe_name}</code>\n\n"
    
    keyboard = []
    
    if not overlays:
        text += "لا يوجد نصوص مخصصة حالياً.\n"
    else:
        text += "النصوص الحالية (يتم اختيار واحد عشوائياً لكل فيديو):\n\n"
        for i, ov in enumerate(overlays):
            o_text = html.escape(ov.get("text", "")[:30])
            o_timing = ov.get("timing", "full")
            o_pos = "أعلى" if ov.get("screen_position", "top") == "top" else "أسفل"
            
            # زر لحذف النص
            btn_text = f"❌ حذف: {o_text} ({o_timing} - {o_pos})"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"del_custom_overlay:{channel_id}:{i}")])
    
    keyboard.append([InlineKeyboardButton("➕ إضافة نص جديد", callback_data=f"add_custom_overlay:{channel_id}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع للقناة", callback_data=f"view_channel:{channel_id}")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return ConversationHandler.END


async def add_custom_overlay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء إضافة نص مخصص جديد"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    context.user_data['edit_channel_id'] = channel_id
    context.user_data['custom_overlay'] = {}
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"edit_custom_overlay:{channel_id}")]]
    
    await query.edit_message_text(
        f"✍️ <b>إضافة نص مخصص</b>\n\nأرسل النص الذي تريده أن يظهر على الفيديو (مثال: اشترك في القناة!):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return CUSTOM_OVERLAY_TEXT


async def add_custom_overlay_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال النص والمطالبة بوقت الظهور"""
    text = update.message.text.strip()
    if not text:
        return CUSTOM_OVERLAY_TEXT
        
    context.user_data['custom_overlay']['text'] = text
    channel_id = context.user_data.get('edit_channel_id')
    
    keyboard = [
        [InlineKeyboardButton("⏱ طوال الفيديو", callback_data="ov_time:full")],
        [InlineKeyboardButton("▶️ بداية الفيديو", callback_data="ov_time:start")],
        [InlineKeyboardButton("⏹ نهاية الفيديو", callback_data="ov_time:end")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"edit_custom_overlay:{channel_id}")]
    ]
    
    await update.message.reply_text(
        "⏱ <b>اختر متى يظهر النص:</b>\n\n"
        "- <b>طوال الفيديو:</b> النص يظهر من البداية للنهاية.\n"
        "- <b>بداية الفيديو:</b> يظهر في أول N ثانية ثم يختفي.\n"
        "- <b>نهاية الفيديو:</b> يظهر في آخر N ثانية.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return CUSTOM_OVERLAY_TIMING


async def add_custom_overlay_timing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال وقت الظهور وتحديد ما إذا كان يحتاج لمدة أو الذهاب للموضع"""
    query = update.callback_query
    await query.answer()
    
    timing = query.data.split(':')[1]
    context.user_data['custom_overlay']['timing'] = timing
    channel_id = context.user_data.get('edit_channel_id')
    
    if timing in ("start", "end"):
        keyboard = [
             [InlineKeyboardButton("1 ثانية", callback_data="ov_dur:1.0"), InlineKeyboardButton("2 ثانية", callback_data="ov_dur:2.0")],
             [InlineKeyboardButton("3 ثواني", callback_data="ov_dur:3.0"), InlineKeyboardButton("5 ثواني", callback_data="ov_dur:5.0")],
             [InlineKeyboardButton("❌ إلغاء", callback_data=f"edit_custom_overlay:{channel_id}")]
        ]
        await query.edit_message_text(
            "⏳ <b>ما هي مدة الظهور (بالثواني)؟</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return CUSTOM_OVERLAY_DURATION
    else:
        # full
        context.user_data['custom_overlay']['duration'] = 0.0
        keyboard = [
            [InlineKeyboardButton("⬆️ أعلى المنتصف", callback_data="ov_pos:top")],
            [InlineKeyboardButton("⬇️ أسفل المنتصف", callback_data="ov_pos:bottom")],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"edit_custom_overlay:{channel_id}")]
        ]
        await query.edit_message_text(
            "📍 <b>موضع النص:</b>\nاختر أين يظهر النص على الشاشة:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return CUSTOM_OVERLAY_POSITION


async def add_custom_overlay_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    dur = float(query.data.split(':')[1])
    context.user_data['custom_overlay']['duration'] = dur
    channel_id = context.user_data.get('edit_channel_id')
    
    keyboard = [
        [InlineKeyboardButton("⬆️ أعلى المنتصف", callback_data="ov_pos:top")],
        [InlineKeyboardButton("⬇️ أسفل المنتصف", callback_data="ov_pos:bottom")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"edit_custom_overlay:{channel_id}")]
    ]
    await query.edit_message_text(
        "📍 <b>موضع النص:</b>\nاختر أين يظهر النص على الشاشة:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return CUSTOM_OVERLAY_POSITION


async def add_custom_overlay_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال الموضع وحفظ النص في القناة"""
    query = update.callback_query
    await query.answer()
    
    pos = query.data.split(':')[1]
    context.user_data['custom_overlay']['screen_position'] = pos
    
    channel_id = context.user_data.get('edit_channel_id')
    ov_data = context.user_data.pop('custom_overlay', {})
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if channel:
        overlays = getattr(channel, "custom_overlay_texts", None) or []
        overlays.append(ov_data)
        manager.update_channel(channel_id, custom_overlay_texts=overlays)
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقناة", callback_data=f"view_channel:{channel_id}")]]
        await query.edit_message_text(
            f"✅ <b>تم إضافة النص المخصص بنجاح!</b>\nسيتم اختياره عشوائياً في الفيديوهات القادمة.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text("❌ القناة غير موجودة.")
        
    return ConversationHandler.END


async def delete_custom_overlay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف نص مخصص معين"""
    query = update.callback_query
    await query.answer()
    
    # del_custom_overlay:channel_id:idx
    parts = query.data.split(':')
    channel_id = parts[1]
    idx = int(parts[2])
    
    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if channel:
        overlays = getattr(channel, "custom_overlay_texts", None) or []
        if 0 <= idx < len(overlays):
            overlays.pop(idx)
            manager.update_channel(channel_id, custom_overlay_texts=overlays)
            
        # العودة لقائمة النصوص
        query.data = f"edit_custom_overlay:{channel_id}"
        await edit_custom_overlay_start(update, context)
    else:
        await query.edit_message_text("❌ القناة غير موجودة.")
