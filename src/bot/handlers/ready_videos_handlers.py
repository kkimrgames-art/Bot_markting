"""
معالجات الفيديوهات الجاهزة - رفع فيديوهات من Google Drive على القنوات
"""
import os
import html
import asyncio
import logging
import tempfile
import time
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# ==================== حالات المحادثة ====================
(
    RV_MENU,
    RV_AUTH_WAIT,
    RV_VIDEO_LIST,
    RV_VIDEO_DETAIL,
    RV_SELECT_CHANNELS,
    RV_TITLE,
    RV_DESCRIPTION,
    RV_THUMBNAIL,
    RV_CONFIRM_UPLOAD,
) = range(9)

# حالات إضافة النص داخل الفيديو (Overlay) لكل قناة
(
    RV_OVERLAY_MENU,
    RV_OVERLAY_TEXT,
    RV_OVERLAY_POSITION,
    RV_OVERLAY_TIMING,
    RV_OVERLAY_DURATION,
) = range(9, 14)


# ==================== مساعدات عامة ====================
async def _safe_answer(query, **kwargs):
    try:
        if query:
            await query.answer(**kwargs)
    except Exception:
        pass


async def _send_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode="HTML"):
    """إرسال رسالة جديدة أو تعديلها حسب الإمكانية"""
    query = update.callback_query if update.callback_query else None

    # محاولة التعديل أولاً
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass

    # إرسال رسالة جديدة
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        await context.bot.send_message(
            chat_id=chat_id, text=text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )


def _format_size(size_bytes: int) -> str:
    if not size_bytes:
        return "غير معروف"
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _format_date(iso_str: str) -> str:
    if not iso_str:
        return "غير معروف"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16]


# ==================== القائمة الرئيسية ====================
async def start_ready_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء ميزة الفيديوهات الجاهزة"""
    query = update.callback_query
    await _safe_answer(query)

    # التحقق من حالة المصادقة
    try:
        from src.agent.gdrive_manager import get_credentials
        creds = await get_credentials()
        is_authenticated = creds is not None and creds.valid
    except Exception:
        is_authenticated = False

    if is_authenticated:
        keyboard = [
            [InlineKeyboardButton("📹 عرض الفيديوهات", callback_data="rv_list_videos")],
            [InlineKeyboardButton("🔑 إعادة المصادقة", callback_data="rv_start_auth")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]
        text = (
            "🎬 <b>الفيديوهات الجاهزة</b>\n\n"
            "✅ تم الاتصال بـ Google Drive بنجاح.\n\n"
            "اختر من القائمة أدناه:"
        )
    else:
        keyboard = [
            [InlineKeyboardButton("🔐 مصادقة Google Drive", callback_data="rv_start_auth")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]
        text = (
            "🎬 <b>الفيديوهات الجاهزة</b>\n\n"
            "⚠️ لم يتم المصادقة مع Google Drive بعد.\n\n"
            "قم بالمصادقة أولاً لعرض كل الفيديوهات المتاحة في قرص Google Drive الخاص بك."
        )

    await _send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard))
    return RV_MENU


# ==================== بدء المصادقة ====================
async def start_drive_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء عملية مصادقة Google Drive"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري إعداد رابط المصادقة...")

    try:
        from src.agent.gdrive_manager import create_auth_flow

        flow, auth_url, redirect_uri = await asyncio.to_thread(create_auth_flow)
        context.user_data["rv_flow"] = flow
        context.user_data["rv_redirect_uri"] = redirect_uri
        context.user_data["rv_auth_url"] = auth_url

        # مسح النتائج القديمة
        from src.bot.shared_state import oauth_callback_results
        oauth_callback_results.pop("latest", None)

        keyboard = [
            [InlineKeyboardButton("🌐 فتح رابط المصادقة", url=auth_url)],
            [InlineKeyboardButton("✅ أنا أكملت المصادقة", callback_data="rv_check_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ]

        text = (
            "🔐 <b>مصادقة Google Drive</b>\n\n"
            "1️⃣ اضغط على الزر أدناه لفتح صفحة مصادقة Google\n"
            "2️⃣ سجّل الدخول وامنح الصلاحيات المطلوبة\n"
            "3️⃣ بعد التوجيه لصفحة النجاح، عد هنا واضغط '✅ أنا أكملت المصادقة'\n\n"
            f"🔗 <b>رابط إعادة التوجيه:</b>\n<code>{html.escape(redirect_uri)}</code>"
        )

        await _send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard))
        return RV_AUTH_WAIT

    except Exception as e:
        logger.error(f"Drive auth error: {e}", exc_info=True)
        text = f"❌ خطأ في إعداد المصادقة:\n<code>{html.escape(str(e)[:200])}</code>"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_start_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU


# ==================== التحقق من المصادقة ====================
async def check_drive_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """التحقق من إتمام المصادقة"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري التحقق...")

    flow = context.user_data.get("rv_flow")
    if not flow:
        text = "❌ انتهت جلسة المصادقة. يرجى المحاولة مرة أخرى."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 مصادقة Google Drive", callback_data="rv_start_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU

    # محاولة الحصول على الكود من shared_state
    from src.bot.shared_state import oauth_callback_results
    callback_url = oauth_callback_results.pop("latest", None)

    if not callback_url:
        text = (
            "⏳ <b>لم يتم استلام كود المصادقة بعد.</b>\n\n"
            "تأكد من أنه تم توجيهك لصفحة النجاح.\n"
            "إذا كنت قد أكملت المصادقة، اضغط 'تحقق مرة أخرى'."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحقق مرة أخرى", callback_data="rv_check_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_AUTH_WAIT

    try:
        from src.agent.gdrive_manager import exchange_code as gdrive_exchange_code, save_credentials

        creds = await asyncio.to_thread(gdrive_exchange_code, flow, callback_url)
        await save_credentials(creds)

        # تنظيف
        context.user_data.pop("rv_flow", None)
        context.user_data.pop("rv_redirect_uri", None)
        context.user_data.pop("rv_auth_url", None)

        text = (
            "✅ <b>تمت المصادقة بنجاح!</b>\n\n"
            "يمكنك الآن تصفح الفيديوهات المتاحة في Google Drive."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📹 عرض الفيديوهات", callback_data="rv_list_videos")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU

    except Exception as e:
        logger.error(f"Drive auth exchange error: {e}", exc_info=True)
        text = f"❌ خطأ في تبادل كود المصادقة:\n<code>{html.escape(str(e)[:200])}</code>"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_start_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU


# ==================== عرض الفيديوهات ====================
async def list_drive_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """جلب وعرض الفيديوهات من Google Drive"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري تحميل القائمة...")

    try:
        from src.agent.gdrive_manager import list_videos_in_folder

        # جلب كل الفيديوهات من الـ My Drive (الرئيسي + المجلدات الفرعية)
        videos = await list_videos_in_folder(folder_id=None)

        if not videos:
            text = "📭 <b>لا توجد فيديوهات</b>\n\nلم يتم العثور على فيديوهات في المجلد المحدد."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تحديث القائمة", callback_data="rv_list_videos")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ])
            await _send_or_edit(update, context, text, keyboard)
            return RV_VIDEO_LIST

        context.user_data["rv_videos"] = videos
        context.user_data["rv_page"] = 0
        return await _show_videos_page(update, context, videos, page=0)

    except Exception as e:
        logger.error(f"List drive videos error: {e}", exc_info=True)
        text = f"❌ خطأ في جلب الفيديوهات:\n<code>{html.escape(str(e)[:200])}</code>"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_list_videos")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU


async def _show_videos_page(update, context, videos: list, page: int = 0) -> int:
    """عرض صفحة من الفيديوهات"""
    per_page = 8
    total = len(videos)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total)
    page_videos = videos[start_idx:end_idx]

    text = f"📹 <b>فيديوهات جاهزة ({total} فيديو)</b>\n📄 الصفحة {page + 1}/{total_pages}\n\n"

    keyboard = []
    for i, v in enumerate(page_videos, start=start_idx + 1):
        name = html.escape(v["name"][:40])
        size = _format_size(v.get("size", 0))
        keyboard.append([
            InlineKeyboardButton(f"{i}. 📹 {name} ({size})", callback_data=f"rv_select:{i-1}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"rv_page:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"rv_page:{page+1}"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("🔄 تحديث", callback_data="rv_list_videos")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")])

    await _send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard))
    context.user_data["rv_page"] = page
    return RV_VIDEO_LIST


async def paginate_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """التنقل بين صفحات الفيديوهات"""
    query = update.callback_query
    await _safe_answer(query)

    page = int(query.data.split(":")[1])
    videos = context.user_data.get("rv_videos", [])
    if not videos:
        return RV_MENU

    return await _show_videos_page(update, context, videos, page=page)


# ==================== اختيار فيديو ====================
async def select_drive_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض تفاصيل فيديو محدد"""
    query = update.callback_query
    await _safe_answer(query)

    idx = int(query.data.split(":")[1])
    videos = context.user_data.get("rv_videos", [])
    if idx < 0 or idx >= len(videos):
        return RV_VIDEO_LIST

    video = videos[idx]
    context.user_data["rv_selected_video"] = video

    text = (
        f"📹 <b>{html.escape(video['name'])}</b>\n\n"
        f"📐 الحجم: {_format_size(video.get('size', 0))}\n"
        f"📅 الإنشاء: {_format_date(video.get('created_time', ''))}\n"
        f"🔄 آخر تعديل: {_format_date(video.get('modified_time', ''))}\n"
        f"🎬 النوع: {video.get('mime_type', 'غير معروف')}\n"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اختيار هذا الفيديو", callback_data=f"rv_confirm_video:{idx}")],
        [InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="rv_list_videos")],
    ])

    await _send_or_edit(update, context, text, keyboard)
    return RV_VIDEO_DETAIL


async def confirm_video_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تأكيد اختيار الفيديو والانتقال لاختيار القنوات"""
    query = update.callback_query
    await _safe_answer(query)

    idx = int(query.data.split(":")[1])
    videos = context.user_data.get("rv_videos", [])
    if idx < 0 or idx >= len(videos):
        return RV_VIDEO_LIST

    context.user_data["rv_selected_video"] = videos[idx]
    return await _show_channel_selection(update, context)


# ==================== اختيار القنوات ====================
async def _show_channel_selection(update, context) -> int:
    """عرض قائمة القنوات للاختيار"""
    try:
        from src.bot.channel_manager import ChannelManager

        manager = ChannelManager()
        channels, total = await asyncio.to_thread(manager.list_channels, offset=0, limit=100)

        if not channels:
            text = "📭 <b>لا توجد قنوات مسجلة</b>\n\nأضف قناة أولاً من القائمة الرئيسية."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ])
            await _send_or_edit(update, context, text, keyboard)
            return RV_MENU

        video = context.user_data.get("rv_selected_video", {})
        text = (
            f"📺 <b>اختيار القنوات للنشر</b>\n\n"
            f"📹 الفيديو: <b>{html.escape(video.get('name', ''))}</b>\n\n"
            f"اختر القنوات (يمكن اختيار عدة قنوات):\n"
            f"🔍 يتم التحقق من جاهزية توكن كل قناة تلقائياً.\n"
        )

        context.user_data["rv_selected_channels"] = []
        context.user_data["rv_all_channels"] = [
            {
                "id": ch.channel_id,
                "name": ch.channel_name,
                "youtube_channel_id": getattr(ch, "youtube_channel_id", "") or "",
            }
            for ch in channels
        ]

        # التحقق من جاهزية التوكن لكل قناة قبل العرض
        ready_map = {}
        for ch in channels:
            ready, _p, reason = await _check_channel_token_ready(ch.channel_id)
            ready_map[ch.channel_id] = (ready, reason)

        keyboard = []
        for ch in channels:
            ready, reason = ready_map.get(ch.channel_id, (False, ""))
            status_icon = "✅" if ready else "⚠️"
            label = f"{status_icon} {html.escape(ch.channel_name[:28])}"
            if not ready:
                label += " (بدون توكن)"
            keyboard.append([
                InlineKeyboardButton(label, callback_data=f"rv_toggle_ch:{ch.channel_id}")
            ])
        keyboard.append([InlineKeyboardButton("✅ تأكيد الاختيار", callback_data="rv_confirm_channels")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="rv_list_videos")])

        await _send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard))
        return RV_SELECT_CHANNELS

    except Exception as e:
        logger.error(f"Channel selection error: {e}", exc_info=True)
        text = f"❌ خطأ في جلب القنوات:\n<code>{html.escape(str(e)[:200])}</code>"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")]])
        await _send_or_edit(update, context, text, keyboard)
        return RV_MENU


async def toggle_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تبديل تحديد قناة"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = query.data.split(":", 1)[1]
    selected = context.user_data.get("rv_selected_channels", [])
    all_channels = context.user_data.get("rv_all_channels", [])

    # التحقق من جاهزية التوكن قبل السماح باختيار القناة
    if ch_id not in selected:
        ready, _p, reason = await _check_channel_token_ready(ch_id)
        if not ready:
            ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
            await _send_or_edit(
                update, context,
                f"⚠️ <b>لا يمكن اختيار هذه القناة:</b>\n"
                f"📺 {html.escape(ch_name)}\n\n"
                f"السبب: {html.escape(reason)}\n\n"
                f"🔑 أضف توكن القناة أولاً من إدارة القنوات، ثم عُد للمحاولة."
            )
            return RV_SELECT_CHANNELS

    if ch_id in selected:
        selected.remove(ch_id)
    else:
        selected.append(ch_id)
    context.user_data["rv_selected_channels"] = selected

    video = context.user_data.get("rv_selected_video", {})
    text = (
        f"📺 <b>اختيار القنوات للنشر</b>\n\n"
        f"📹 الفيديو: <b>{html.escape(video.get('name', ''))}</b>\n\n"
        f"اختر القنوات:\n"
    )

    keyboard = []
    for ch in all_channels:
        icon = "🟩" if ch["id"] in selected else "⬜"
        keyboard.append([
            InlineKeyboardButton(f"{icon} {html.escape(ch['name'][:30])}", callback_data=f"rv_toggle_ch:{ch['id']}")
        ])
    keyboard.append([InlineKeyboardButton(f"✅ تأكيد ({len(selected)} قناة)", callback_data="rv_confirm_channels")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="rv_list_videos")])

    await _send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard))
    return RV_SELECT_CHANNELS


async def confirm_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تأكيد اختيار القنوات"""
    query = update.callback_query
    await _safe_answer(query)

    selected = context.user_data.get("rv_selected_channels", [])
    if not selected:
        await _send_or_edit(update, context, "⚠️ اختر قناة واحدة على الأقل.")
        return RV_SELECT_CHANNELS

    # فحص نهائي: تأكد أن كل قناة مختارة لديها توكن جاهز
    not_ready = []
    for ch_id in selected:
        ready, _p, reason = await _check_channel_token_ready(ch_id)
        if not ready:
            ch_name = next((c["name"] for c in context.user_data.get("rv_all_channels", []) if c["id"] == ch_id), ch_id)
            not_ready.append(f"• {html.escape(ch_name)}: {html.escape(reason)}")

    if not_ready:
        await _send_or_edit(
            update, context,
            "⚠️ <b>بعض القنوات المختارة غير جاهزة للنشر:</b>\n\n"
            + "\n".join(not_ready)
            + "\n\n🔑 أضف توكن القناة من إدارة القنوات ثم عُد للمحاولة."
        )
        return RV_SELECT_CHANNELS

    context.user_data["rv_pending_channels"] = list(selected)
    context.user_data["rv_channel_metadata"] = {}

    return await _start_channel_metadata_input(update, context)


# ==================== جمع البيانات لكل قناة ====================
async def _start_channel_metadata_input(update, context) -> int:
    """بدء جمع العنوان للقناة الحالية"""
    pending = context.user_data.get("rv_pending_channels", [])
    all_channels = context.user_data.get("rv_all_channels", [])

    if not pending:
        return await _show_upload_confirmation(update, context)

    ch_id = pending[0]
    ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
    context.user_data["rv_current_channel_id"] = ch_id

    total = len(all_channels)
    remaining = len(pending)
    done = total - remaining + 1

    text = (
        f"📝 <b>بيانات النشر - {html.escape(ch_name)}</b>\n\n"
        f"القناة {done}/{total}\n\n"
        f"أرسل <b>العنوان</b> لهذا الفيديو:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 إلغاء", callback_data="rv_cancel_metadata")],
    ])

    await _send_or_edit(update, context, text, keyboard)
    return RV_TITLE


async def receive_video_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال عنوان الفيديو"""
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("❌ العنوان فارغ. أرسل العنوان:")
        return RV_TITLE

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data.setdefault("rv_channel_metadata", {})
    context.user_data["rv_channel_metadata"].setdefault(ch_id, {})
    context.user_data["rv_channel_metadata"][ch_id]["title"] = title

    text = "📝 <b>الوصف (اختياري)</b>\n\nأرسل الوصف، أو اضغط 'تخطي':"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي الوصف", callback_data="rv_skip_desc")],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    return RV_DESCRIPTION


async def receive_video_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال وصف الفيديو"""
    description = (update.message.text or "").strip()

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["description"] = description

    text = "🖼️ <b>الصورة المصغرة (اختياري)</b>\n\nأرسل صورة، أو اضغط 'تخطي':"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي الصورة", callback_data="rv_skip_thumb")],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    return RV_THUMBNAIL


async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تخطي الوصف"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["description"] = ""

    text = "🖼️ <b>الصورة المصغرة (اختياري)</b>\n\nأرسل صورة، أو اضغط 'تخطي':"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي الصورة", callback_data="rv_skip_thumb")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_THUMBNAIL


async def receive_video_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال الصورة المصغرة"""
    ch_id = context.user_data.get("rv_current_channel_id")

    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document if update.message.document else None

    file = photo or (document if document and document.mime_type and document.mime_type.startswith("image/") else None)

    if not file:
        await update.message.reply_text(
            "❌ أرسل صورة فقط، أو اضغط تخطي:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ تخطي", callback_data="rv_skip_thumb")]]),
        )
        return RV_THUMBNAIL

    try:
        tmp_dir = tempfile.mkdtemp(prefix="rv_thumb_")
        ext = ".jpg"
        if document:
            ext = os.path.splitext(document.file_name or "thumb.jpg")[1] or ".jpg"
        thumb_path = os.path.join(tmp_dir, f"thumb{ext}")

        tg_file = await file.get_file()
        await tg_file.download_to_drive(thumb_path)

        context.user_data["rv_channel_metadata"][ch_id]["thumbnail_path"] = thumb_path
    except Exception as e:
        logger.error(f"Thumbnail download error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ خطأ: {str(e)[:100]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ تخطي", callback_data="rv_skip_thumb")]]),
        )
        return RV_THUMBNAIL

    return await _ask_overlay_menu(update, context)


async def skip_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تخطي الصورة المصغرة"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["thumbnail_path"] = ""

    return await _ask_overlay_menu(update, context)


async def _advance_to_next_channel(update, context) -> int:
    """الانتقال للقناة التالية أو عرض التأكيد"""
    pending = context.user_data.get("rv_pending_channels", [])
    if pending:
        pending.pop(0)

    if pending:
        return await _start_channel_metadata_input(update, context)
    else:
        return await _show_upload_confirmation(update, context)


# ==================== إضافة نص داخل الفيديو (Overlay) ====================
async def _ask_overlay_menu(update, context) -> int:
    """سؤال المستخدم عن إضافة نص داخل الفيديو لهذه القناة"""
    ch_id = context.user_data.get("rv_current_channel_id")
    ch_name = next((c["name"] for c in context.user_data.get("rv_all_channels", []) if c["id"] == ch_id), ch_id)
    text = (
        f"📝 <b>نص داخل الفيديو (Overlay)</b>\n\n"
        f"📺 القناة: <b>{html.escape(ch_name)}</b>\n\n"
        f"هل تريد إضافة نص يظهر داخل الفيديو (بنفس جودة نظام المودات)؟\n"
        f"يمكنك تحديد النص ومكانه ومدته لاحقاً."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ إضافة نص للفيديو", callback_data="rv_overlay_add")],
        [InlineKeyboardButton("⏭️ بدون نص", callback_data="rv_overlay_skip")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_OVERLAY_MENU


async def overlay_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار القائمة: إضافة نص أو تخطي"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = context.user_data.get("rv_current_channel_id")
    metadata = context.user_data.setdefault("rv_channel_metadata", {})
    metadata.setdefault(ch_id, {})

    if query.data == "rv_overlay_skip":
        metadata[ch_id]["overlay"] = {"enabled": False}
        return await _advance_to_next_channel(update, context)

    text = (
        "✍️ <b>أدخل النص الذي سيظهر داخل الفيديو</b>\n\n"
        "أرسل النص الآن (يدعم العربية والإنجليزية ورموز الإيموجي).\n"
        "مثال: اشترك في القناة 🔔"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي النص", callback_data="rv_overlay_skip")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_OVERLAY_TEXT


async def receive_overlay_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال نص الـ overlay"""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ النص فارغ. أرسل النص الذي تريد إظهاره داخل الفيديو:")
        return RV_OVERLAY_TEXT

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id].setdefault("overlay", {})["text"] = text

    return await _ask_overlay_position(update, context)


async def _ask_overlay_position(update, context) -> int:
    """اختيار مكان النص داخل الفيديو"""
    text = (
        "📍 <b>مكان النص داخل الفيديو</b>\n\n"
        "اختر المكان المفضل لظهور النص:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ أعلى الفيديو", callback_data="rv_overlay_pos:top")],
        [InlineKeyboardButton("➡️ منتصف الفيديو", callback_data="rv_overlay_pos:center")],
        [InlineKeyboardButton("⬇️ أسفل الفيديو", callback_data="rv_overlay_pos:bottom")],
        [InlineKeyboardButton("⏭️ تخطي النص", callback_data="rv_overlay_skip")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_OVERLAY_POSITION


async def choose_overlay_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار المكان"""
    query = update.callback_query
    await _safe_answer(query)

    pos = query.data.split(":", 1)[1]
    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["overlay"]["screen_position"] = pos

    return await _ask_overlay_timing(update, context)


async def _ask_overlay_timing(update, context) -> int:
    """اختيار توقيت ظهور النص"""
    text = (
        "⏱️ <b>توقيت ظهور النص</b>\n\n"
        "اختر متى يظهر النص في الفيديو:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏁 من البداية", callback_data="rv_overlay_timing:start")],
        [InlineKeyboardButton("🏁 حتى النهاية", callback_data="rv_overlay_timing:end")],
        [InlineKeyboardButton("🔁 طوال الفيديو", callback_data="rv_overlay_timing:full")],
        [InlineKeyboardButton("⏭️ تخطي النص", callback_data="rv_overlay_skip")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_OVERLAY_TIMING


async def choose_overlay_timing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار التوقيت"""
    query = update.callback_query
    await _safe_answer(query)

    timing = query.data.split(":", 1)[1]
    ch_id = context.user_data.get("rv_current_channel_id")
    overlay = context.user_data["rv_channel_metadata"][ch_id]["overlay"]
    overlay["timing"] = timing
    overlay["enabled"] = True
    overlay.setdefault("font_size", 56)

    if timing == "full":
        # طوال الفيديو: لا نحتاج مدة
        overlay["duration"] = 0.0
        return await _advance_to_next_channel(update, context)

    text = (
        "⏳ <b>مدة ظهور النص (بالثواني)</b>\n\n"
        "أرسل المدة بالثواني، مثال: <code>3</code> أو <code>5.5</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي النص", callback_data="rv_overlay_skip")],
    ])
    await _send_or_edit(update, context, text, keyboard)
    return RV_OVERLAY_DURATION


async def receive_overlay_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال مدة ظهور النص"""
    raw = (update.message.text or "").strip().replace(",", ".")
    try:
        duration = max(0.5, float(raw))
    except Exception:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً بالثواني، مثال: 3")
        return RV_OVERLAY_DURATION

    ch_id = context.user_data.get("rv_current_channel_id")
    overlay = context.user_data["rv_channel_metadata"][ch_id]["overlay"]
    overlay["duration"] = duration
    overlay["enabled"] = True

    return await _advance_to_next_channel(update, context)


# ==================== تأكيد الرفع ====================
async def _show_upload_confirmation(update, context) -> int:
    """عرض ملخص والتأكيد"""
    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])
    selected_ids = context.user_data.get("rv_selected_channels", [])

    text = f"📋 <b>ملخص عملية الرفع</b>\n\n📹 الفيديو: <b>{html.escape(video.get('name', ''))}</b>\n\n"

    for ch_id in selected_ids:
        ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
        ch_meta = metadata.get(ch_id, {})
        title = html.escape(ch_meta.get("title", "بدون عنوان"))
        has_thumb = "✅" if ch_meta.get("thumbnail_path") else "❌"
        overlay = ch_meta.get("overlay") or {}
        if overlay.get("enabled") and overlay.get("text"):
            ov_text = html.escape(overlay.get("text", ""))
            ov_pos = overlay.get("screen_position", "top")
            ov_timing = overlay.get("timing", "full")
            if ov_timing == "full":
                ov_dur = "طوال الفيديو"
            else:
                ov_dur = f"{overlay.get('duration', 2.0)} ث"
            ov_line = f"✍️ {ov_text} | 📍 {ov_pos} | ⏱️ {ov_timing} ({ov_dur})"
        else:
            ov_line = "❌"
        text += f"📺 <b>{html.escape(ch_name)}</b>\n   📝 {title}\n   🖼️ {has_thumb}\n   📝 نص: {ov_line}\n\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 بدء الرفع", callback_data="rv_start_upload")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="rv_menu")],
    ])

    await _send_or_edit(update, context, text, keyboard)
    return RV_CONFIRM_UPLOAD


# ==================== بدء الرفع ====================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء رفع الفيديو"""
    query = update.callback_query
    await _safe_answer(query, text="🚀 جاري الرفع...")

    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])
    selected_ids = context.user_data.get("rv_selected_channels", [])

    context.user_data["rv_upload_results"] = []
    context.user_data["rv_upload_queue"] = list(selected_ids)

    return await _process_next_upload(update, context)


async def _process_next_upload(update, context) -> int:
    """معالجة رفع على القناة التالية"""
    queue = context.user_data.get("rv_upload_queue", [])
    results = context.user_data.get("rv_upload_results", [])
    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])

    if not queue:
        return await _show_upload_results(update, context, results)

    ch_id = queue.pop(0)
    context.user_data["rv_upload_queue"] = queue

    ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
    ch_meta = metadata.get(ch_id, {})

    # إرسال رسالة تحديث
    chat_id = update.effective_chat.id
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"⏳ جاري الرفع على: {ch_name}...")

    try:
        video_id = video.get("id", "")
        video_name = video.get("name", "video.mp4")

        from src.agent.gdrive_manager import download_video as gdrive_download
        import tempfile as _tf

        tmp_dir = _tf.mkdtemp(prefix="rv_upload_")
        local_path = os.path.join(tmp_dir, video_name)

        await gdrive_download(video_id, local_path)

        # تطبيق النص داخل الفيديو (Overlay) بنفس جودة نظام المودات
        overlay = ch_meta.get("overlay") or {}
        if overlay.get("enabled") and overlay.get("text"):
            try:
                from src.agent.mod_video_processor import ModVideoProcessor

                processor = ModVideoProcessor()
                overlaid_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}_overlay.mp4")
                await asyncio.to_thread(
                    processor.add_custom_overlay_text,
                    local_path,
                    overlaid_path,
                    overlay["text"],
                    overlay.get("timing", "full"),
                    float(overlay.get("duration", 2.0) or 0.0),
                    overlay.get("screen_position", "top"),
                    None,  # overlay_image_path
                    None,  # intro_animation
                    None,  # outro_animation
                    None,  # custom_font
                    int(overlay.get("font_size", 56) or 56),
                )
                if os.path.exists(overlaid_path) and os.path.getsize(overlaid_path) > 0:
                    local_path = overlaid_path
                    logger.info(f"✅ Overlay text applied for channel {ch_name}")
                else:
                    logger.warning(f"⚠️ Overlay output empty, uploading original video for {ch_name}")
            except Exception as ov_err:
                logger.error(f"Overlay text failed for {ch_name}: {ov_err}", exc_info=True)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ تعذّر إضافة النص داخل الفيديو لـ {ch_name}، سيتم رفع الفيديو الأصلي."
                )

        channel_token_path = await _get_channel_token_path(ch_id)

        if not channel_token_path:
            raise RuntimeError(f"لم يتم العثور على توكن القناة {ch_name}")

        title = ch_meta.get("title", video_name)
        description = ch_meta.get("description", "")
        thumbnail_path = ch_meta.get("thumbnail_path", "")

        upload_result = await _upload_to_youtube(
            channel_token_path=channel_token_path,
            file_path=local_path,
            title=title,
            description=description,
            thumbnail_path=thumbnail_path,
            channel_id=ch_id,
        )

        results.append({
            "channel_id": ch_id, "channel_name": ch_name,
            "success": True, "video_id": upload_result.get("video_id", ""), "title": title,
        })

        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Upload to {ch_name} failed: {e}", exc_info=True)
        results.append({
            "channel_id": ch_id, "channel_name": ch_name,
            "success": False, "error": str(e)[:200],
        })

    context.user_data["rv_upload_results"] = results
    return await _process_next_upload(update, context)


async def _check_channel_token_ready(channel_id: str) -> tuple[bool, str, str]:
    """
    التحقق من وجود توكن صالح للقناة وجاهزيته للنشر.
    يرجع: (جاهز؟, مسار_التوكن, سبب_الفشل)
    """
    try:
        from src.bot.channel_manager import ChannelManager, resolve_channel_token_path

        manager = ChannelManager()
        channel = await asyncio.to_thread(manager.get_channel, channel_id)
        if channel is None:
            return False, "", "القناة غير موجودة محلياً"

        token_path = resolve_channel_token_path(channel)
        if not token_path or not os.path.exists(token_path):
            tp = channel.token_path
            if tp and os.path.exists(tp):
                token_path = tp
        if not token_path or not os.path.exists(token_path):
            return False, "", "ملف التوكن غير موجود لهذه القناة"

        try:
            from src.agent.uploader import _creds_from_token_file
            creds = _creds_from_token_file(token_path)
        except Exception as e:
            return False, token_path, f"تعذّر قراءة التوكن: {e}"

        # محاولة تجديد التوكن إذا انتهت صلاحيته
        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
            except Exception:
                pass

        if creds and creds.valid:
            return True, token_path, ""
        return False, token_path, "التوكن منتهٍ الصلاحية أو غير صالح"
    except Exception as e:
        logger.error(f"Failed to check channel token: {e}")
        return False, "", str(e)


async def _get_channel_token_path(channel_id: str) -> str:
    """الحصول على مسار توكن القناة (مع التحقق من صلاحيته)"""
    ready, path, _reason = await _check_channel_token_ready(channel_id)
    if ready and path:
        return path

    # احتياطي: أي ملف توكن متاح في مجلد youtube_tokens
    data_dir = os.path.join(os.getcwd(), ".data", "youtube_tokens")
    if os.path.exists(data_dir):
        for f in os.listdir(data_dir):
            if f.endswith(".json"):
                return os.path.join(data_dir, f)
    return ""


async def _upload_to_youtube(channel_token_path, file_path, title, description, thumbnail_path, channel_id) -> dict:
    """رفع فيديو على YouTube بجودة عالية بدون فقدان"""
    from src.agent.uploader import upload_video_with_token
    from src.agent.config import load_config

    cfg = load_config()

    result = await asyncio.to_thread(
        upload_video_with_token,
        cfg,
        channel_token_path,
        file_path,
        title,
        description,
        [],
        "public",
    )

    # رفع الصورة المصغرة بعد نجاح الرفع
    if result and thumbnail_path and os.path.exists(thumbnail_path):
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            def _upload_thumb():
                from src.agent.uploader import _creds_from_token_file
                creds = _creds_from_token_file(channel_token_path)
                youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
                youtube.thumbnails().set(
                    videoId=result,
                    media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
                ).execute()

            await asyncio.to_thread(_upload_thumb)
            logger.info(f"✅ Thumbnail uploaded for video {result}")
        except Exception as e:
            logger.warning(f"⚠️ Thumbnail upload failed (non-critical): {e}")

    return {"video_id": result}


async def _show_upload_results(update, context, results: list) -> int:
    """عرض نتائج الرفع"""
    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count

    text = f"🏁 <b>نتائج عملية الرفع</b>\n\n✅ نجح: {success_count}\n❌ فشل: {fail_count}\n\n"

    for r in results:
        icon = "✅" if r.get("success") else "❌"
        ch_name = html.escape(r.get("channel_name", ""))
        if r.get("success"):
            vid_id = r.get("video_id", "")
            text += f"{icon} {ch_name}: تم الرفع\n"
            if vid_id:
                text += f"   🔗 https://youtube.com/watch?v={vid_id}\n"
        else:
            error = html.escape(r.get("error", "")[:100])
            text += f"{icon} {ch_name}: {error}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 رفع فيديو آخر", callback_data="rv_list_videos")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])

    await _send_or_edit(update, context, text, keyboard)

    # تنظيف
    for key in list(context.user_data.keys()):
        if key.startswith("rv_"):
            context.user_data.pop(key, None)

    return ConversationHandler.END


# ==================== إلغاء ====================
async def cancel_ready_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إلغاء والرجوع"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    for key in list(context.user_data.keys()):
        if key.startswith("rv_"):
            context.user_data.pop(key, None)

    text = "🏠 <b>القائمة الرئيسية</b>\n\nاختر من القائمة أدناه:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 فيديوهات جاهزة", callback_data="am_ready_videos")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")],
    ])

    await _send_or_edit(update, context, text, keyboard)
    return ConversationHandler.END


# ==================== إعادة المصادقة ====================
async def reauth_drive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إعادة المصادقة"""
    query = update.callback_query
    await _safe_answer(query)
    return await start_drive_auth(update, context)
