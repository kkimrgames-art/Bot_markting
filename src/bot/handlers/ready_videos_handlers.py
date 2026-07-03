"""
معالجات الفيديوهات الجاهزة - رفع فيديوهات من Google Drive على القنوات
"""
import os
import html
import asyncio
import logging
import tempfile
import time
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


async def _safe_answer(query, **kwargs):
    try:
        if query:
            await query.answer(**kwargs)
    except (BadRequest, Exception):
        pass


async def _safe_edit(query, text, reply_markup=None, parse_mode="HTML", **kwargs):
    if not query:
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


def _format_size(size_bytes: int) -> str:
    """تنسيق حجم الملف"""
    if not size_bytes:
        return "غير معروف"
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _format_date(iso_str: str) -> str:
    """تنسيق التاريخ ISO إلى صيغة مقروءة"""
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
        from ...agent.gdrive_manager import get_credentials
        creds = await get_credentials()
        is_authenticated = creds is not None and creds.valid
    except Exception:
        is_authenticated = False

    if is_authenticated:
        keyboard = [
            [InlineKeyboardButton("📹 عرض الفيديوهات", callback_data="rv_list_videos")],
            [InlineKeyboardButton("🔑 إعادة المصادقة", callback_data="rv_reauth")],
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
            "قم بالمصادقة أولاً لعرض الفيديوهات المتاحة في المجلد المحدد."
        )

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_MENU


# ==================== بدء المصادقة ====================
async def start_drive_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء عملية مصادقة Google Drive"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري إعداد رابط المصادقة...")

    try:
        from ...agent.gdrive_manager import create_auth_flow

        flow, auth_url, redirect_uri = await asyncio.to_thread(create_auth_flow)
        context.user_data["rv_flow"] = flow
        context.user_data["rv_redirect_uri"] = redirect_uri
        context.user_data["rv_auth_url"] = auth_url

        # حفظ نتائج المصادقة للمشاركة مع الخادم
        from .shared_state import oauth_callback_results
        oauth_callback_results.pop("rv_latest", None)

        keyboard = [
            [InlineKeyboardButton("🌐 فتح رابط المصادقة", url=auth_url)],
            [InlineKeyboardButton("📋 نسخ الرابط", callback_data="rv_copy_auth_url")],
            [InlineKeyboardButton("✅ أنا أكملت المصادقة", callback_data="rv_check_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ]

        text = (
            "🔐 <b>مصادقة Google Drive</b>\n\n"
            "1️⃣ اضغط على الزر أدناه لفتح صفحة مصادقة Google\n"
            "2️⃣ سجّل الدخول وامنح الصلاحيات المطلوبة\n"
            "3️⃣ بعد التوجيه للصفحة، عد هنا واضغط '✅ أنا أكملت المصادقة'\n\n"
            f"🔗 <b>رابط إعادة التوجيه:</b>\n<code>{html.escape(redirect_uri)}</code>"
        )

        await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return RV_AUTH_WAIT

    except Exception as e:
        logger.error(f"Drive auth error: {e}", exc_info=True)
        await _safe_edit(
            query,
            f"❌ خطأ في إعداد المصادقة:\n<code>{html.escape(str(e)[:200])}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_start_auth")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ]),
        )
        return RV_MENU


# ==================== انتظار المصادقة ====================
async def check_drive_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """التحقق من إتمام المصادقة"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري التحقق...")

    flow = context.user_data.get("rv_flow")
    if not flow:
        await _safe_edit(
            query,
            "❌ انتهت جلسة المصادقة. يرجى المحاولة مرة أخرى.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 مصادقة Google Drive", callback_data="rv_start_auth")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ]),
        )
        return RV_MENU

    # محاولة الحصول على الكود من shared_state
    from .shared_state import oauth_callback_results
    callback_url = oauth_callback_results.pop("rv_latest", None) or oauth_callback_results.pop("latest", None)

    if not callback_url:
        keyboard = [
            [InlineKeyboardButton("🔄 تحقق مرة أخرى", callback_data="rv_check_auth")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
        ]
        await _safe_edit(
            query,
            "⏳ <b>لم يتم استلام كود المصادقة بعد.</b>\n\n"
            "تأكد من أنه تم توجيهك لصفحة النجاح.\n"
            "إذا كنت قد أكملت المصادقة، اضغط 'تحقق مرة أخرى'.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return RV_AUTH_WAIT

    try:
        from ...agent.gdrive_manager import exchange_code, save_credentials

        creds = await asyncio.to_thread(
            _exchange_code_sync, flow, callback_url
        )
        await save_credentials(creds)

        # تنظيف
        context.user_data.pop("rv_flow", None)
        context.user_data.pop("rv_redirect_uri", None)

        keyboard = [
            [InlineKeyboardButton("📹 عرض الفيديوهات", callback_data="rv_list_videos")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]
        await _safe_edit(
            query,
            "✅ <b>تمت المصادقة بنجاح!</b>\n\n"
            "يمكنك الآن تصفح الفيديوهات المتاحة في Google Drive.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return RV_MENU

    except Exception as e:
        logger.error(f"Drive auth exchange error: {e}", exc_info=True)
        await _safe_edit(
            query,
            f"❌ خطأ في تبادل كود المصادقة:\n<code>{html.escape(str(e)[:200])}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_start_auth")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ]),
        )
        return RV_MENU


def _exchange_code_sync(flow, callback_url):
    """نسخة sync من exchange_code"""
    from ...agent.gdrive_manager import exchange_code
    return exchange_code(flow, callback_url)


# ==================== عرض الفيديوهات ====================
async def list_drive_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """جلب وعرض الفيديوهات من Google Drive"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري تحميل قائمة الفيديوهات...")

    try:
        from ...agent.gdrive_manager import list_videos_in_folder

        videos = await list_videos_in_folder()

        if not videos:
            await _safe_edit(
                query,
                "📭 <b>لا توجد فيديوهات</b>\n\n"
                "لم يتم العثور على فيديوهات في المجلد المحدد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 تحديث القائمة", callback_data="rv_list_videos")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
                ]),
            )
            return RV_VIDEO_LIST

        # حفظ الفيديوهات في user_data
        context.user_data["rv_videos"] = videos
        context.user_data["rv_page"] = 0

        return await _show_videos_page(query, context, videos, page=0)

    except Exception as e:
        logger.error(f"List drive videos error: {e}", exc_info=True)
        await _safe_edit(
            query,
            f"❌ خطأ في جلب الفيديوهات:\n<code>{html.escape(str(e)[:200])}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 المحاولة مرة أخرى", callback_data="rv_list_videos")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ]),
        )
        return RV_MENU


async def _show_videos_page(query, context, videos: list, page: int = 0) -> int:
    """عرض صفحة من الفيديوهات"""
    per_page = 8
    total = len(videos)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total)
    page_videos = videos[start_idx:end_idx]

    text = f"📹 <b>فيديوهات جاهزة ({total} فيديو)</b>\n"
    text += f"📄 الصفحة {page + 1}/{total_pages}\n\n"

    keyboard = []
    for i, v in enumerate(page_videos, start=start_idx + 1):
        name = html.escape(v["name"][:40])
        size = _format_size(v.get("size", 0))
        text_line = f"{i}. 📹 {name} ({size})"
        keyboard.append([
            InlineKeyboardButton(text_line, callback_data=f"rv_select:{i-1}")
        ])

    # أزرار التنقل
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=f"rv_page:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=f"rv_page:{page+1}"))
    keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔄 تحديث القائمة", callback_data="rv_list_videos")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")])

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
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

    return await _show_videos_page(query, context, videos, page=page)


# ==================== اختيار فيديو ====================
async def select_drive_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض تفاصيل فيديو محدد"""
    query = update.callback_query
    await _safe_answer(query)

    idx = int(query.data.split(":")[1])
    videos = context.user_data.get("rv_videos", [])

    if idx < 0 or idx >= len(videos):
        await _safe_edit(query, "❌ فيديو غير موجود.")
        return RV_VIDEO_LIST

    video = videos[idx]
    context.user_data["rv_selected_video"] = video

    # عرض تفاصيل الفيديو
    text = (
        f"📹 <b>{html.escape(video['name'])}</b>\n\n"
        f"📐 الحجم: {_format_size(video.get('size', 0))}\n"
        f"📅 تاريخ الإنشاء: {_format_date(video.get('created_time', ''))}\n"
        f"🔄 آخر تعديل: {_format_date(video.get('modified_time', ''))}\n"
        f"🎬 النوع: {video.get('mime_type', 'غير معروف')}\n"
    )

    keyboard = [
        [InlineKeyboardButton("✅ اختيار هذا الفيديو", callback_data=f"rv_confirm_video:{idx}")],
        [InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="rv_list_videos")],
    ]

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_VIDEO_DETAIL


# ==================== تأكيد اختيار الفيديو ====================
async def confirm_video_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تأكيد اختيار الفيديو والانتقال لاختيار القنوات"""
    query = update.callback_query
    await _safe_answer(query)

    idx = int(query.data.split(":")[1])
    videos = context.user_data.get("rv_videos", [])
    if idx < 0 or idx >= len(videos):
        return RV_VIDEO_LIST

    video = videos[idx]
    context.user_data["rv_selected_video"] = video

    # الانتقال لاختيار القنوات
    return await _show_channel_selection(query, context)


# ==================== اختيار القنوات ====================
async def _show_channel_selection(query, context) -> int:
    """عرض قائمة القنوات للاختيار"""
    try:
        from ...bot.channel_manager import ChannelManager

        manager = ChannelManager()
        channels, total = await asyncio.to_thread(manager.list_channels, offset=0, limit=100)

        if not channels:
            await _safe_edit(
                query,
                "📭 <b>لا توجد قنوات مسجلة</b>\n\n"
                "أضف قناة أولاً من القائمة الرئيسية.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
                ]),
            )
            return RV_MENU

        video = context.user_data.get("rv_selected_video", {})
        text = (
            f"📺 <b>اختيار القنوات للنشر</b>\n\n"
            f"📹 الفيديو: <b>{html.escape(video.get('name', 'غير معروف'))}</b>\n\n"
            f"اختر القنوات التي تريد النشر عليها (يمكن اختيار عدة قنوات):\n"
        )

        # حفظ القنوات المختارة
        context.user_data["rv_selected_channels"] = []
        context.user_data["rv_all_channels"] = [
            {"id": ch.channel_id, "name": ch.channel_name}
            for ch in channels
        ]

        keyboard = []
        for ch in channels:
            ch_id = ch.channel_id
            ch_name = html.escape(ch.channel_name[:30])
            keyboard.append([
                InlineKeyboardButton(
                    f"⬜ {ch_name}",
                    callback_data=f"rv_toggle_ch:{ch_id}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton("✅ تأكيد الاختيار", callback_data="rv_confirm_channels")
        ])
        keyboard.append([
            InlineKeyboardButton("🔙 رجوع", callback_data="rv_list_videos")
        ])

        await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return RV_SELECT_CHANNELS

    except Exception as e:
        logger.error(f"Channel selection error: {e}", exc_info=True)
        await _safe_edit(
            query,
            f"❌ خطأ في جلب القنوات:\n<code>{html.escape(str(e)[:200])}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="rv_menu")],
            ]),
        )
        return RV_MENU


async def toggle_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تبديل تحديد قناة"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = query.data.split(":", 1)[1]
    selected = context.user_data.get("rv_selected_channels", [])
    all_channels = context.user_data.get("rv_all_channels", [])

    if ch_id in selected:
        selected.remove(ch_id)
    else:
        selected.append(ch_id)

    context.user_data["rv_selected_channels"] = selected

    # إعادة بناء الكيبورد
    video = context.user_data.get("rv_selected_video", {})
    text = (
        f"📺 <b>اختيار القنوات للنشر</b>\n\n"
        f"📹 الفيديو: <b>{html.escape(video.get('name', 'غير معروف'))}</b>\n\n"
        f"اختر القنوات التي تريد النشر عليها:\n"
    )

    keyboard = []
    for ch in all_channels:
        ch_id_item = ch["id"]
        ch_name = html.escape(ch["name"][:30])
        icon = "🟩" if ch_id_item in selected else "⬜"
        keyboard.append([
            InlineKeyboardButton(
                f"{icon} {ch_name}",
                callback_data=f"rv_toggle_ch:{ch_id_item}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(f"✅ تأكيد ({len(selected)} قناة مختارة)", callback_data="rv_confirm_channels")
    ])
    keyboard.append([
        InlineKeyboardButton("🔙 رجوع", callback_data="rv_list_videos")
    ])

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_SELECT_CHANNELS


async def confirm_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تأكيد اختيار القنوات والبدء بجمع البيانات لكل قناة"""
    query = update.callback_query
    await _safe_answer(query)

    selected = context.user_data.get("rv_selected_channels", [])
    if not selected:
        await _safe_edit(
            query,
            "⚠️ لم يتم اختيار أي قناة. يرجى اختيار قناة واحدة على الأقل.",
        )
        return RV_SELECT_CHANNELS

    # حفظ قائمة القنوات المتبقية للمعالجة
    context.user_data["rv_pending_channels"] = list(selected)
    context.user_data["rv_channel_metadata"] = {}  # {channel_id: {title, description, thumbnail_path}}

    # بدء المعالجة مع أول قناة
    return await _start_channel_metadata_input(query, context)


async def _start_channel_metadata_input(query, context) -> int:
    """بدء جمع البيانات (عنوان، وصف، صورة مصغرة) لقناة محددة"""
    pending = context.user_data.get("rv_pending_channels", [])
    all_channels = context.user_data.get("rv_all_channels", [])

    if not pending:
        # انتهت جميع القنوات → الانتقال للتأكيد النهائي
        return await _show_upload_confirmation(query, context)

    ch_id = pending[0]
    ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)

    context.user_data["rv_current_channel_id"] = ch_id
    context.user_data["rv_current_step"] = "title"  # title → description → thumbnail

    text = (
        f"📝 <b>بيانات النشر - {html.escape(ch_name)}</b>\n\n"
        f"القناة الحالية: {len(all_channels) - len(pending) + 1}/{len(all_channels)}\n\n"
        f"أرسل <b>العنوان</b> لهذا الفيديو على هذه القناة:"
    )

    keyboard = [
        [InlineKeyboardButton("🔙 إلغاء", callback_data="rv_cancel_metadata")],
    ]

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_TITLE


async def receive_video_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال عنوان الفيديو"""
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("❌ العنوان لا يمكن أن يكون فارغاً. أرسل العنوان:")
        return RV_TITLE

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data.setdefault("rv_channel_metadata", {})
    context.user_data["rv_channel_metadata"].setdefault(ch_id, {})
    context.user_data["rv_channel_metadata"][ch_id]["title"] = title

    context.user_data["rv_current_step"] = "description"

    text = (
        f"📝 <b>الوصف (اختياري)</b>\n\n"
        f"أرسل وصف الفيديو، أو اضغط 'تخطي' لإضافة الوصف لاحقاً:"
    )
    keyboard = [
        [InlineKeyboardButton("⏭️ تخطي الوصف", callback_data="rv_skip_desc")],
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return RV_DESCRIPTION


async def receive_video_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال وصف الفيديو"""
    description = (update.message.text or "").strip()

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["description"] = description

    context.user_data["rv_current_step"] = "thumbnail"

    text = (
        f"🖼️ <b>الصورة المصغرة (اختياري)</b>\n\n"
        f"أرسل صورة مصغرة للفيديو، أو اضغط 'تخطي' لتخطي هذه الخطوة:"
    )
    keyboard = [
        [InlineKeyboardButton("⏭️ تخطي الصورة المصغرة", callback_data="rv_skip_thumb")],
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return RV_THUMBNAIL


async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تخطي إضافة الوصف"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["description"] = ""

    context.user_data["rv_current_step"] = "thumbnail"

    text = (
        f"🖼️ <b>الصورة المصغرة (اختياري)</b>\n\n"
        f"أرسل صورة مصغرة للفيديو، أو اضغط 'تخطي' لتخطي هذه الخطوة:"
    )
    keyboard = [
        [InlineKeyboardButton("⏭️ تخطي الصورة المصغرة", callback_data="rv_skip_thumb")],
    ]

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_THUMBNAIL


async def receive_video_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال الصورة المصغرة"""
    ch_id = context.user_data.get("rv_current_channel_id")

    # تحميل الصورة
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document if update.message.document else None

    file = None
    if photo:
        file = photo
    elif document and document.mime_type and document.mime_type.startswith("image/"):
        file = document

    if not file:
        await update.message.reply_text(
            "❌ يرجى إرسال صورة ( PHOTO أو ملف صورة ).\n"
            "أو اضغط 'تخطي' لتخطي هذه الخطوة:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ تخطي", callback_data="rv_skip_thumb")],
            ]),
        )
        return RV_THUMBNAIL

    # تحميل الصورة إلى ملف مؤقت
    try:
        tmp_dir = tempfile.mkdtemp(prefix="rv_thumb_")
        ext = ".jpg"
        if document:
            fname = document.file_name or "thumb.jpg"
            ext = os.path.splitext(fname)[1] or ".jpg"
        thumb_path = os.path.join(tmp_dir, f"thumb{ext}")

        tg_file = await file.get_file()
        await tg_file.download_to_drive(thumb_path)

        context.user_data["rv_channel_metadata"][ch_id]["thumbnail_path"] = thumb_path
    except Exception as e:
        logger.error(f"Thumbnail download error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ خطأ في تحميل الصورة: {str(e)[:100]}\n"
            "يمكنك تخطي هذه الخطوة:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ تخطي", callback_data="rv_skip_thumb")],
            ]),
        )
        return RV_THUMBNAIL

    # الانتقال للقناة التالية
    return await _advance_to_next_channel(update, context)


async def skip_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تخطي رفع الصورة المصغرة"""
    query = update.callback_query
    await _safe_answer(query)

    ch_id = context.user_data.get("rv_current_channel_id")
    context.user_data["rv_channel_metadata"][ch_id]["thumbnail_path"] = ""

    return await _advance_to_next_channel(query, context)


async def _advance_to_next_channel(query_or_update, context) -> int:
    """الانتقال للقناة التالية أو إظهار التأكيد النهائي"""
    pending = context.user_data.get("rv_pending_channels", [])
    if pending:
        pending.pop(0)
        context.user_data["rv_pending_channels"] = pending

    all_channels = context.user_data.get("rv_all_channels", [])
    remaining = len(pending)

    if remaining > 0:
        # عرض رسالة تقدم ثم الانتقال للقناة التالية
        next_ch = pending[0]
        next_name = next((c["name"] for c in all_channels if c["id"] == next_ch), next_ch)

        if hasattr(query_or_update, 'edit_message_text'):
            # CallbackQuery
            await _safe_answer(query_or_update, text="✅ تم الحفظ!")
            await _start_channel_metadata_input(query_or_update, context)
        else:
            # Message
            await query_or_update.message.reply_text(
                f"✅ تم حفظ بيانات القناة.\n"
                f"التالي: <b>{html.escape(next_name)}</b>",
                parse_mode="HTML",
            )
            # بدء جمع البيانات للتالية
            context.user_data["rv_current_channel_id"] = next_ch
            context.user_data["rv_current_step"] = "title"

            text = (
                f"📝 <b>بيانات النشر - {html.escape(next_name)}</b>\n\n"
                f"أرسل <b>العنوان</b> لهذا الفيديو على هذه القناة:"
            )
            keyboard = [
                [InlineKeyboardButton("🔙 إلغاء", callback_data="rv_cancel_metadata")],
            ]
            await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            return RV_TITLE
    else:
        # انتهت جميع القنوات
        if hasattr(query_or_update, 'edit_message_text'):
            return await _show_upload_confirmation(query_or_update, context)
        else:
            # إنشاء رسالة تأكيد جديدة
            return await _show_upload_confirmation_new(query_or_update, context)

    return RV_TITLE


# ==================== تأكيد الرفع ====================
async def _show_upload_confirmation(query, context) -> int:
    """عرض ملخص البيانات والتأكيد النهائي"""
    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])
    selected_ids = context.user_data.get("rv_selected_channels", [])

    text = f"📋 <b>ملخص عملية الرفع</b>\n\n"
    text += f"📹 الفيديو: <b>{html.escape(video.get('name', ''))}</b>\n\n"

    for ch_id in selected_ids:
        ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
        ch_meta = metadata.get(ch_id, {})
        title = html.escape(ch_meta.get("title", "بدون عنوان"))
        desc = ch_meta.get("description", "")
        has_thumb = "✅" if ch_meta.get("thumbnail_path") else "❌"

        text += f"📺 <b>{html.escape(ch_name)}</b>\n"
        text += f"   📝 العنوان: {title}\n"
        text += f"   📄 الوصف: {html.escape(desc[:50])}{'...' if len(desc) > 50 else ''}\n"
        text += f"   🖼️ الصورة المصغرة: {has_thumb}\n\n"

    keyboard = [
        [InlineKeyboardButton("🚀 بدء الرفع", callback_data="rv_start_upload")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="rv_menu")],
    ]

    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return RV_CONFIRM_UPLOAD


async def _show_upload_confirmation_new(update, context) -> int:
    """عرض ملخص التأكيد في رسالة جديدة"""
    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])
    selected_ids = context.user_data.get("rv_selected_channels", [])

    text = f"📋 <b>ملخص عملية الرفع</b>\n\n"
    text += f"📹 الفيديو: <b>{html.escape(video.get('name', ''))}</b>\n\n"

    for ch_id in selected_ids:
        ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
        ch_meta = metadata.get(ch_id, {})
        title = html.escape(ch_meta.get("title", "بدون عنوان"))
        desc = ch_meta.get("description", "")
        has_thumb = "✅" if ch_meta.get("thumbnail_path") else "❌"

        text += f"📺 <b>{html.escape(ch_name)}</b>\n"
        text += f"   📝 العنوان: {title}\n"
        text += f"   📄 الوصف: {html.escape(desc[:50])}{'...' if len(desc) > 50 else ''}\n"
        text += f"   🖼️ الصورة المصغرة: {has_thumb}\n\n"

    keyboard = [
        [InlineKeyboardButton("🚀 بدء الرفع", callback_data="rv_start_upload")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="rv_menu")],
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return RV_CONFIRM_UPLOAD


# ==================== بدء الرفع ====================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء رفع الفيديو على القنوات المحددة"""
    query = update.callback_query
    await _safe_answer(query, text="🚀 جاري رفع الفيديو...")

    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])
    selected_ids = context.user_data.get("rv_selected_channels", [])

    # حفظ حالة الرفع
    context.user_data["rv_upload_results"] = []
    context.user_data["rv_upload_queue"] = list(selected_ids)

    # بدء الرفع
    return await _process_next_upload(query, context)


async def _process_next_upload(query_or_update, context) -> int:
    """معالجة رفع فيديو على القناة التالية"""
    queue = context.user_data.get("rv_upload_queue", [])
    results = context.user_data.get("rv_upload_results", [])
    video = context.user_data.get("rv_selected_video", {})
    metadata = context.user_data.get("rv_channel_metadata", {})
    all_channels = context.user_data.get("rv_all_channels", [])

    if not queue:
        # انتهت جميع عمليات الرفع
        return await _show_upload_results(query_or_update, context, results)

    ch_id = queue.pop(0)
    context.user_data["rv_upload_queue"] = queue

    ch_name = next((c["name"] for c in all_channels if c["id"] == ch_id), ch_id)
    ch_meta = metadata.get(ch_id, {})

    status_msg = f"⏳ جاري الرفع على: {ch_name}..."

    if hasattr(query_or_update, 'edit_message_text'):
        await _safe_edit(query_or_update, status_msg)
    else:
        await query_or_update.message.reply_text(status_msg)

    try:
        # تحميل الفيديو من Google Drive
        video_id = video.get("id", "")
        video_name = video.get("name", "video.mp4")

        from ...agent.gdrive_manager import download_video, get_credentials
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="rv_upload_")
        local_path = os.path.join(tmp_dir, video_name)

        await download_video(video_id, local_path)

        # جلب credentials للقناة
        channel_token_path = await _get_channel_token_path(ch_id)

        # رفع الفيديو على YouTube
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
            "channel_id": ch_id,
            "channel_name": ch_name,
            "success": True,
            "video_id": upload_result.get("video_id", ""),
            "title": title,
        })

        # تنظيف الملفات المؤقتة
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Upload to {ch_name} failed: {e}", exc_info=True)
        results.append({
            "channel_id": ch_id,
            "channel_name": ch_name,
            "success": False,
            "error": str(e)[:200],
        })

    context.user_data["rv_upload_results"] = results

    # المعالجة التالية
    return await _process_next_upload(query_or_update, context)


async def _get_channel_token_path(channel_id: str) -> str:
    """الحصول على مسار توكن القناة"""
    try:
        from ...bot.channel_manager import ChannelManager
        manager = ChannelManager()
        channel = await asyncio.to_thread(manager.get_channel, channel_id)
        if channel:
            candidates = manager._channel_token_candidates(channel)
            for path in candidates:
                resolved = manager._resolve_storage_path(path) if hasattr(manager, '_resolve_storage_path') else path
                if os.path.exists(resolved):
                    return resolved
    except Exception as e:
        logger.error(f"Failed to get channel token: {e}")

    # بحث افتراضي
    data_dir = os.path.join(os.getcwd(), ".data", "youtube_tokens")
    if os.path.exists(data_dir):
        for f in os.listdir(data_dir):
            if f.endswith(".json"):
                return os.path.join(data_dir, f)
    return ""


async def _upload_to_youtube(
    channel_token_path: str,
    file_path: str,
    title: str,
    description: str,
    thumbnail_path: str,
    channel_id: str,
) -> dict:
    """رفع فيديو على YouTube"""
    from ...agent.config import load_config

    cfg = load_config()

    # استيراد دالة الرفع
    from ...agent.uploader import upload_video_with_token

    result = await asyncio.to_thread(
        upload_video_with_token,
        token_path=channel_token_path,
        file_path=file_path,
        title=title,
        description=description,
        tags=[],
        category_id="22",  # People & Blogs
        privacy_status="public",
        thumbnail_path=thumbnail_path if thumbnail_path else None,
    )

    return result


async def _show_upload_results(query_or_update, context, results: list) -> int:
    """عرض نتائج الرفع"""
    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count

    text = f"🏁 <b>نتائج عملية الرفع</b>\n\n"
    text += f"✅ نجح: {success_count}\n❌ فشل: {fail_count}\n\n"

    for r in results:
        icon = "✅" if r.get("success") else "❌"
        ch_name = html.escape(r.get("channel_name", ""))
        if r.get("success"):
            vid_id = r.get("video_id", "")
            text += f"{icon} {ch_name}: تم الرفع بنجاح\n"
            if vid_id:
                text += f"   🔗 https://youtube.com/watch?v={vid_id}\n"
        else:
            error = html.escape(r.get("error", "خطأ غير معروف")[:100])
            text += f"{icon} {ch_name}: {error}\n"

    keyboard = [
        [InlineKeyboardButton("📹 رفع فيديو آخر", callback_data="rv_list_videos")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ]

    if hasattr(query_or_update, 'edit_message_text'):
        await _safe_edit(query_or_update, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # تنظيف بيانات المستخدم
    context.user_data.pop("rv_selected_video", None)
    context.user_data.pop("rv_selected_channels", None)
    context.user_data.pop("rv_channel_metadata", None)
    context.user_data.pop("rv_pending_channels", None)
    context.user_data.pop("rv_upload_queue", None)
    context.user_data.pop("rv_upload_results", None)

    return ConversationHandler.END


# ==================== إلغاء ====================
async def cancel_ready_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إلغاء العملية والرجوع للقائمة الرئيسية"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    # تنظيف بيانات المستخدم
    for key in list(context.user_data.keys()):
        if key.startswith("rv_"):
            context.user_data.pop(key, None)

    from .auto_mod_handlers import auto_mod_menu
    return await auto_mod_menu(update, context)


# ==================== إعادة المصادقة ====================
async def reauth_drive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إعادة المصادقة مع Google Drive"""
    query = update.callback_query
    await _safe_answer(query)

    return await start_drive_auth(update, context)


# ==================== نسخ رابط المصادقة ====================
async def copy_auth_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إرسال رابط المصادقة كنص"""
    query = update.callback_query
    await _safe_answer(query, text="📋 تم نسخ الرابط!")

    auth_url = context.user_data.get("rv_auth_url", "")
    if auth_url and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔗 رابط المصادقة:\n<code>{html.escape(auth_url)}</code>",
            parse_mode="HTML",
        )

    return RV_AUTH_WAIT
