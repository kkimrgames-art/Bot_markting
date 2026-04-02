"""
معالجات إدارة القنوات - إضافة، عرض، تعديل، حذف
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
import logging
import math
import html
import asyncio

from pathlib import Path
from ..channel_manager import ChannelManager
from ...agent.uploader import get_credentials, AuthenticationRequiredError
from ...agent.config import load_config
# Modified import: Use auto_mod_menu instead of main_menu
# Circular import moved inside function


logger = logging.getLogger(__name__)

# حالات conversation لإضافة قناة
(
    ADD_CHANNEL_NAME,
    ADD_PLATFORM,
    ADD_YOUTUBE_ID,
    ADD_PLATFORM_ID,
    ADD_ACCESS_TOKEN,
    ADD_TOKEN_EXPIRY,
    ADD_LANGUAGE,
    ADD_CONTENT_TYPE,
    ADD_PRIVACY,
    ADD_INTERVAL,
) = range(10)

# عدد القنوات في الصفحة
CHANNELS_PER_PAGE = 10


# ==================== إضافة قناة ====================

async def start_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إضافة قناة - اختيار الطريقة"""
    query = update.callback_query
    await query.answer()
    
    text = (
        "➕ <b>إضافة قناة جديدة</b>\n\n"
        "اختر طريقة الإضافة:\n\n"
        "1️⃣ <b>عبر ملف المصادقة (موصى به):</b>\n"
        "أرسل ملف <code>client_secret.json</code> وسيتم جلب البيانات والمصادقة تلقائياً.\n\n"
        "2️⃣ <b>إدخال يدوي:</b>\n"
        "أدخل الاسم والمعرف يدوياً (للقنوات المضافة مسبقاً أو للاختبار)."
    )
    
    keyboard = [
        [InlineKeyboardButton("📂 إضافة عبر ملف (Johny)", callback_data="add_method:file")],
        [InlineKeyboardButton("✍️ إدخال يدوي", callback_data="add_method:manual")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    # نستخدم حالة جديدة لانتظار اختيار الطريقة، أو نعيد استخدام ADD_CHANNEL_NAME كمدخل
    # الأفضل تعديل ConversationHandler ليشمل حالة جديدة، لكن للتبسيط:
    # سنستخدم ADD_CHANNEL_NAME كحالة "انتظار الاختيار" ونفحص الـ callback
    return ADD_CHANNEL_NAME


async def handle_add_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيار طريقة الإضافة"""
    query = update.callback_query
    await query.answer()
    
    method = query.data.split(':')[1]
    
    if method == "manual":
        # الطريقة اليدوية: طلب الاسم
        text = (
            "✍️ <b>إضافة قناة يدوياً</b>\n\n"
            "ابدأ بإدخال اسم القناة\n"
            "(مثال: قناة الألعاب)"
        )
        keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]]
        
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return ADD_CHANNEL_NAME
        
    elif method == "file":
        # توجيه للملف
        text = (
            "📂 <b>إضافة قناة عبر ملف</b>\n\n"
            "اضغط الزر أدناه للبدء:"
        )
        keyboard = [[InlineKeyboardButton("📂 ابدأ رفع الملف", callback_data="add_channel_file")]]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return ConversationHandler.END


async def receive_channel_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال اسم القناة"""
    channel_name = update.message.text.strip()
    
    if not channel_name:
        await update.message.reply_text("❌ الاسم فارغ. الرجاء إدخال اسم صحيح:")
        return ADD_CHANNEL_NAME
    
    # حفظ الاسم
    context.user_data['new_channel'] = {'channel_name': channel_name}

    safe_name = html.escape(channel_name)
    text = (
        f"✅ الاسم: {safe_name}\n\n"
        "اختر المنصة المراد النشر عليها:"
    )

    keyboard = [
        [InlineKeyboardButton("📺 يوتيوب", callback_data="platform:youtube")],
        [InlineKeyboardButton("📸 انستقرام", callback_data="platform:instagram")],
        [InlineKeyboardButton("📘 فيسبوك", callback_data="platform:facebook")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")],
    ]

    await update.message.reply_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

    return ADD_PLATFORM


async def receive_platform_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال اختيار المنصة"""
    query = update.callback_query
    await query.answer()

    platform = query.data.split(':')[1]
    context.user_data['new_channel']['platform'] = platform

    if platform == "youtube":
        text = (
            "📺 <b>إعداد قناة YouTube</b>\n\n"
            "أدخل معرف قناة YouTube\n"
            "<code>(مثال: UCxxxxxxxxxxxxx أو رابط القناة)</code>"
        )
        await query.edit_message_text(text=text, parse_mode='HTML')
        return ADD_YOUTUBE_ID

    platform_label = "انستقرام" if platform == "instagram" else "فيسبوك"
    text = (
        f"🔗 <b>إعداد قناة {platform_label}</b>\n\n"
        "أدخل معرف الحساب/الصفحة:"
    )
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]]
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return ADD_PLATFORM_ID


async def receive_youtube_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال معرف قناة YouTube"""
    youtube_id = update.message.text.strip()
    
    if not youtube_id:
        await update.message.reply_text("❌ المعرف فارغ. الرجاء إدخال معرف صحيح:")
        return ADD_YOUTUBE_ID
    
    context.user_data['new_channel'].setdefault('platform', 'youtube')
    context.user_data['new_channel']['youtube_channel_id'] = youtube_id
    context.user_data['new_channel']['platform_channel_id'] = youtube_id

    safe_id = html.escape(youtube_id)
    await _show_language_selection(
        update,
        context,
        intro_text=f"✅ معرف القناة: <code>{safe_id}</code>\n\nاختر لغة المحتوى:"
    )
    return ADD_LANGUAGE


async def receive_platform_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال معرف الحساب/الصفحة لمنصات غير YouTube"""
    platform_id = update.message.text.strip()

    if not platform_id:
        await update.message.reply_text("❌ المعرف فارغ. الرجاء إدخال معرف صحيح:")
        return ADD_PLATFORM_ID

    context.user_data['new_channel']['platform_channel_id'] = platform_id
    context.user_data['new_channel']['youtube_channel_id'] = platform_id

    safe_pid = html.escape(platform_id)
    platform = context.user_data['new_channel'].get('platform', 'instagram')
    platform_label = "انستقرام" if platform == "instagram" else "فيسبوك"

    text = (
        f"✅ معرف {platform_label}: <code>{safe_pid}</code>\n\n"
        "أدخل <b>Access Token</b> الخاص بالحساب:"
    )
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]]
    await update.message.reply_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return ADD_ACCESS_TOKEN


async def receive_access_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال Access Token للمنصات الخارجية"""
    access_token = update.message.text.strip()

    if not access_token:
        await update.message.reply_text("❌ التوكن فارغ. الرجاء إدخال التوكن الصحيح:")
        return ADD_ACCESS_TOKEN

    credentials = context.user_data['new_channel'].get('platform_credentials') or {}
    credentials['access_token'] = access_token
    context.user_data['new_channel']['platform_credentials'] = credentials

    text = (
        "📅 إذا كان للتوكن تاريخ انتهاء، أرسله بصيغة <code>YYYY-MM-DD</code>\n"
        "أو اكتب <b>(تخطي)</b> إذا لا يوجد تاريخ انتهاء."
    )
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]]
    await update.message.reply_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return ADD_TOKEN_EXPIRY


async def receive_token_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال تاريخ انتهاء التوكن (اختياري)"""
    expiry_text = update.message.text.strip()

    if expiry_text and expiry_text not in {"skip", "تخطي", "-", "لا"}:
        try:
            if len(expiry_text) == 10:
                expiry_text = f"{expiry_text}T00:00:00"
            expiry_dt = datetime.fromisoformat(expiry_text).replace(tzinfo=None)
            credentials = context.user_data['new_channel'].get('platform_credentials') or {}
            credentials['expires_at'] = expiry_dt.isoformat()
            context.user_data['new_channel']['platform_credentials'] = credentials
        except Exception:
            await update.message.reply_text("❌ التاريخ غير صالح. الرجاء إدخاله بصيغة YYYY-MM-DD أو اكتب (تخطي)")
            return ADD_TOKEN_EXPIRY

    await _show_language_selection(
        update,
        context,
        intro_text="✅ تم حفظ بيانات المصادقة.\n\nاختر لغة المحتوى:"
    )
    return ADD_LANGUAGE


async def _show_language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, intro_text: str):
    """عرض قائمة اللغات"""
    from ..language_manager import LanguageManager

    text = f"{intro_text}\n\n🌍 <b>اللغات الشائعة:</b>"
    popular = LanguageManager.get_popular_languages()
    keyboard = []

    row = []
    for lang_code in popular:
        lang = LanguageManager.get_language(lang_code)
        if lang:
            row.append(InlineKeyboardButton(
                f"{lang.flag} {lang.name}",
                callback_data=f"lang:{lang_code}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []

    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🌐 المزيد من اللغات", callback_data="lang:more")])
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif update.message:
        await update.message.reply_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )


async def receive_content_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال نوع المحتوى"""
    query = update.callback_query
    await query.answer()
    
    content_type = query.data.split(':')[1]
    context.user_data['new_channel']['content_type'] = content_type
    
    content_name = "ماين كرافت" if content_type == "minecraft" else ("ألعاب" if content_type == "games" else "محتوى آخر")
    
    text = (
        f"✅ نوع المحتوى: {content_name}\n\n"
        "اختر خصوصية الفيديو:"
    )
    
    keyboard = [
        [InlineKeyboardButton("🌍 عام (Public)", callback_data="privacy:public")],
        [InlineKeyboardButton("🔗 غير مدرج (Unlisted)", callback_data="privacy:unlisted")],
        [InlineKeyboardButton("🔒 خاص (Private)", callback_data="privacy:private")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
    return ADD_PRIVACY


async def receive_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال الخصوصية"""
    query = update.callback_query
    await query.answer()
    
    privacy = query.data.split(':')[1]
    context.user_data['new_channel']['privacy'] = privacy
    
    privacy_name = {"public": "عام", "unlisted": "غير مدرج", "private": "خاص"}[privacy]
    
    text = (
        f"✅ الخصوصية: {privacy_name}\n\n"
        "اختر فترة النشر:"
    )
    
    keyboard = [
        [InlineKeyboardButton("⏱️ كل دقيقة", callback_data="interval:60")],
        [InlineKeyboardButton("🕧 كل نصف ساعة", callback_data="interval:1800")],
        [InlineKeyboardButton("⏰ كل ساعة", callback_data="interval:3600")],
        [InlineKeyboardButton("⏰ كل ساعتين", callback_data="interval:7200")],
        [InlineKeyboardButton("⏰ كل 3 ساعات", callback_data="interval:10800")],
        [InlineKeyboardButton("⏰ كل 6 ساعات", callback_data="interval:21600")],
        [InlineKeyboardButton("⏰ كل 12 ساعة", callback_data="interval:43200")],
        [InlineKeyboardButton("⏰ كل 24 ساعة", callback_data="interval:86400")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
    return ADD_INTERVAL


async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال فترة النشر وإنشاء القناة"""
    query = update.callback_query
    await query.answer()
    
    interval = int(query.data.split(':')[1])
    context.user_data['new_channel']['publish_interval'] = interval
    
    # إنشاء القناة
    manager = ChannelManager()
    channel_data = context.user_data['new_channel']
    
    try:
        channel = manager.add_channel(**channel_data)
        
        if interval < 3600:
            minutes = max(1, interval // 60)
            interval_text = "دقيقة واحدة" if minutes == 1 else ("نصف ساعة" if minutes == 30 else f"{minutes} دقيقة")
        else:
            hours = interval // 3600
            interval_text = f"{hours} ساعة" if hours > 1 else "ساعة واحدة"
        
        safe_name = html.escape(channel.channel_name)
        safe_yid = html.escape(channel.youtube_channel_id)
        
        text = (
            "✅ <b>تم إضافة القناة بنجاح!</b>\n\n"
            f"📺 الاسم: <code>{safe_name}</code>\n"
            f"🆔 المعرف: <code>{safe_yid}</code>\n"
            f"🎮 النوع: {('ماين كرافت' if channel.content_type == 'minecraft' else ('ألعاب' if channel.content_type == 'games' else 'محتوى آخر'))}\n"
            f"🔒 الخصوصية: {channel.privacy}\n"
            f"⏰ فترة النشر: كل {interval_text}\n"
            f"🔑 معرف القناة: <code>{channel.channel_id}</code>\n\n"
            "💡 <b>هل تريد إضافة نص مخصص يظهر في كل فيديو شورتس؟</b>\n"
            "يمكنك إعداده الآن أو لاحقاً من إعدادات القناة."
        )
        
        keyboard = [
            [InlineKeyboardButton("✏️ إضافة نص مخصص للفيديو", callback_data=f"edit_custom_overlay:{channel.channel_id}")],
            [InlineKeyboardButton("📋 عرض القنوات", callback_data="list_channels:0")],
            [InlineKeyboardButton("➕ إضافة قناة أخرى", callback_data="add_channel")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]
        ]
        
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        # مسح البيانات المؤقتة
        context.user_data.pop('new_channel', None)
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error adding channel: {e}")
        
        safe_err = html.escape(str(e))
        text = f"❌ حدث خطأ أثناء إضافة القناة:\n<code>{safe_err}</code>"
        keyboard = [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
        
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        return ConversationHandler.END


async def cancel_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء إضافة القناة"""
    query = update.callback_query
    await query.answer()
    
    context.user_data.pop('new_channel', None)
    
    # from .main_menu import show_main_menu
    from .auto_mod_handlers import auto_mod_menu
    await auto_mod_menu(update, context) # edit=True is handled internally or ignored? Source had edit=True

    
    return ConversationHandler.END


# ==================== عرض القنوات ====================

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة القنوات مع pagination"""
    query = update.callback_query
    await query.answer()
    
    # استخراج رقم الصفحة بشكل آمن
    page = 0
    data = query.data or ""
    if ':' in data and data.startswith('list_channels:'):
        try:
            page = int(data.split(':')[1])
        except (ValueError, IndexError):
            page = 0
    elif ':' in data and ('view_channel' in data or 'confirm_delete' in data):
        # في حال تم استدعاء الصفحة للعودة من القنوات أو الحذف
        page = 0
    
    manager = ChannelManager()
    offset = page * CHANNELS_PER_PAGE
    channels, total = await asyncio.to_thread(manager.list_channels, offset=offset, limit=CHANNELS_PER_PAGE)
    
    if total == 0:
        text = (
            "📋 <b>قائمة القنوات</b>\n\n"
            "لا توجد قنوات حالياً.\n"
            "اضغط <b>'إضافة قناة'</b> لإضافة قناة جديدة."
        )
        keyboard = [
            [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]
        ]
    else:
        total_pages = math.ceil(total / CHANNELS_PER_PAGE)
        current_page = page + 1
        
        text = f"📋 <b>قائمة القنوات</b> (صفحة {current_page}/{total_pages})\n\n"
        text += f"إجمالي القنوات: <code>{total}</code>\n\n"
        
        # عرض القنوات
        keyboard = []
        for channel in channels:
            # التحقق من صلاحية التوكن (في خيط خلفي)
            is_auth_ok, _ = await asyncio.to_thread(manager._validate_platform_auth, channel)
            
            if not channel.enabled:
                status_icon = "💤"  # قناة معطلة
            elif not is_auth_ok:
                status_icon = "❌"  # توكن منتهي أو غير صالح (يحتاج تجديد)
            else:
                status_icon = "✅"  # كل شيء جاهز
                
            safe_name = html.escape(channel.channel_name[:30])  # تقصير الاسم والهروب
            
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_icon} {safe_name}",
                    callback_data=f"view_channel:{channel.channel_id}"
                )
            ])
        
        # أزرار التنقل
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ السابق", callback_data=f"list_channels:{page-1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="noop"))
        
        if (page + 1) < total_pages:
            nav_buttons.append(InlineKeyboardButton("▶️ التالي", callback_data=f"list_channels:{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        # أزرار الإدارة والرجوع
        keyboard.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="add_channel")])
        keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    from telegram.error import BadRequest
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

# ==================== عرض تفاصيل القناة ====================

async def view_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض تفاصيل قناة محددة بتصميم عصري وجديد"""
    query = update.callback_query
    await query.answer()
    
    data = query.data or ""
    parts = data.split(':')
    channel_id = parts[-1] if parts else ""
    
    manager = ChannelManager()
    channel = await asyncio.to_thread(manager.get_channel, channel_id)
    
    if not channel:
        await query.edit_message_text(
            "❌ <b>عذراً، لم يتم العثور على القناة!</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 العودة للقائمة", callback_data="list_channels:0")
            ]]),
            parse_mode='HTML'
        )
        return

    # استخراج البيانات الإضافية
    extra = getattr(channel, "extra_data", {}) or {}
    
    # 🎨 تحسين عرض الحالة
    status_emoji = "🟢" if channel.enabled else "🔴"
    status_text = "نشطة وتعمل" if channel.enabled else "متوقفة مؤقتاً"
    
    # 🎮 نوع المحتوى
    content_map = {
        "minecraft": "🕹️ ماين كرافت",
        "games": "🎮 ألعاب وفيديو",
        "other": "🎬 محتوى متنوع"
    }
    content_type = content_map.get(channel.content_type, "🎬 محتوى عام")
    
    # 🔒 الخصوصية
    privacy_map = {"public": "🌍 عام (Public)", "unlisted": "🔗 غير مدرج", "private": "🔒 خاص"}
    privacy = privacy_map.get(channel.privacy, f"🔑 {channel.privacy}")
    
    # 🌍 اللغة
    from ..language_manager import LanguageManager
    lang_display = LanguageManager.format_language_display(channel.language)
    
    # ⏰ الجدولة
    hours = channel.publish_interval // 3600
    if hours >= 1:
        interval_text = f"كل {hours} ساعة" if hours > 1 else "كل ساعة"
    else:
        mins = channel.publish_interval // 60
        interval_text = f"كل {mins} دقيقة"

    # 🕐 حساب الوقت المتبقي للنشر التالي
    next_publish_text = "غير محدد"
    if channel.next_publish:
        from datetime import datetime
        try:
            next_time = datetime.fromisoformat(channel.next_publish).replace(tzinfo=None)
            now = datetime.now()
            if next_time > now:
                delta = next_time - now
                h, r = divmod(int(delta.total_seconds()), 3600)
                m, _ = divmod(r, 60)
                next_publish_text = f"بعد {h}س و {m}د ⏳"
            else:
                next_publish_text = "قيد المعالجة الآن ⚡"
        except Exception:
            next_publish_text = "في انتظار الدورة التالية"

    # 🎞️ الجودة والمميزات
    quality = extra.get("video_quality") or "720p"
    facecam = "✅ مفعل" if extra.get("facecam_enabled") and extra.get("facecam_clip_id") else "❌ غير مفعل"
    overlay = "✅ مفعل" if extra.get("overlay_text_enabled", True) and channel.content_type == "minecraft" else "—"
    
    # نصوص مخصصة
    _cot = getattr(channel, "custom_overlay_texts", None) or []
    custom_overlay_info = f"✅ {len(_cot)} نص" if _cot else "❌ غير مفعل"

    # 🔐 حالة المصادقة (OAuth)
    auth_status = "⌛ جاري الفحص..."
    auth_icon = "❓"
    try:
        from pathlib import Path
        try:
            from ...agent.config import load_config
            cfg = load_config()
            base_dir = os.path.dirname(getattr(cfg, "TELEGRAM_DB_PATH", "") or "") or ".data"
        except Exception:
            base_dir = ".data"
        token_path = Path(base_dir) / "youtube_tokens" / f"{channel.youtube_channel_id}.json"
        
        def _check_auth():
            if not token_path.exists():
                return "مفقودة (يرجى الربط)", "❌"
            try:
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_file(str(token_path))
                if creds.valid or (creds.expired and creds.refresh_token):
                    return "متصلة وجاهزة", "✅"
                else:
                    return "تحتاج إعادة مصادقة", "⚠️"
            except Exception:
                return "خطأ في الاتصال", "⚠️"

        auth_status, auth_icon = await asyncio.to_thread(_check_auth)
    except Exception:
        auth_status = "خطأ في النظام"
        auth_icon = "⚠️"

    import html
    safe_name = html.escape(channel.channel_name)

    # 📝 تجميع الرسالة بتصميم أنيق
    text = (
        f"📺 <b>إدارة القناة:</b> <code>{safe_name}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>الحالة العامة:</b> {status_emoji} {status_text}\n"
        f"🆔 <b>معرف اليوتيوب:</b> <code>{channel.youtube_channel_id}</code>\n"
        f"🔐 <b>حالة الربط:</b> {auth_icon} {auth_status}\n\n"
        
        f"🛠 <b>الإعدادات الأساسية:</b>\n"
        f"🔹 <b>النوع:</b> {content_type}\n"
        f"🔹 <b>المرئية:</b> {privacy}\n"
        f"🔹 <b>اللغة:</b> {lang_display}\n\n"
        
        f"⏱ <b>الجدولة والنشر:</b>\n"
        f"🔸 <b>الوتيرة:</b> {interval_text}\n"
        f"🔸 <b>النشر القادم:</b> {next_publish_text}\n"
        f"🔸 <b>إجمالي المنشورات:</b> <code>{channel.total_published}</code> منشور\n\n"
        
        f"🎬 <b>خيارات المعالجة:</b>\n"
        f"✨ <b>جودة الفيديو:</b> <code>{quality}</code>\n"
        f"✂️ <b>القص التلقائي:</b> {'✅ مفعل' if extra.get('auto_trim_enabled', True) else '❌ معطل'}\n"
        f"👤 <b>كاميرا الوجه:</b> {facecam}\n"
        f"🅰️ <b>نص التعليق:</b> {overlay}\n"
        f"✏️ <b>نص مخصص:</b> {custom_overlay_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID الداخلي:</b> <code>{channel.channel_id}</code>"
    )

    # ⌨️ لوحة التحكم (Keyboard) مقسمة بشكل منطقي
    keyboard = [
        # الصف الأول: الحالة والربط
        [
            InlineKeyboardButton("🔄 " + ("إيقاف" if channel.enabled else "تشغيل"), callback_data=f"toggle_channel:{channel_id}"),
            InlineKeyboardButton("🔑 تحديث الربط", callback_data=f"reauth_start:{channel_id}")
        ],
        # الصف الثاني: تعديل البيانات
        [
            InlineKeyboardButton("🎮 النوع", callback_data=f"edit_content:{channel_id}"),
            InlineKeyboardButton("🌍 المرئية", callback_data=f"edit_privacy:{channel_id}"),
            InlineKeyboardButton("🌐 اللغة", callback_data=f"edit_language:{channel_id}")
        ],
        # الصف الثالث: الجدولة
        [
            InlineKeyboardButton("⏱ الوتيرة", callback_data=f"edit_interval:{channel_id}"),
            InlineKeyboardButton("📅 الجدولة الذكية", callback_data=f"sched_channel:{channel_id}"),
            InlineKeyboardButton("🚀 أنشر الآن", callback_data=f"publish_now:{channel_id}")
        ],
        # الصف الرابع: الفيديو والمحتوى
        [
            InlineKeyboardButton("🎞 الجودة", callback_data=f"edit_quality:{channel_id}"),
            InlineKeyboardButton("🎥 FaceCam", callback_data=f"edit_facecam:{channel_id}"),
            InlineKeyboardButton("✂️ القص", callback_data=f"edit_trim:{channel_id}")
        ],
        [
            InlineKeyboardButton("📄 الوصف", callback_data=f"edit_custom_desc:{channel_id}")
        ]
    ]

    # نص مخصص للفيديو (متاح لجميع أنواع المحتوى)
    keyboard.append([InlineKeyboardButton("✏️ نص مخصص للفيديو", callback_data=f"edit_custom_overlay:{channel_id}")])

    # خيارات إضافية لماين كرافت
    if channel.content_type == "minecraft":
        keyboard.append([InlineKeyboardButton("🅰️ إعداد نص التعليق", callback_data=f"edit_minecraft_overlay:{channel_id}")])


    # الصف الأخير: إدارة النظام
    keyboard.append([
        InlineKeyboardButton("🧹 تصفير السجل", callback_data=f"reset_mem:{channel_id}"),
        InlineKeyboardButton("🗑 حذف القناة", callback_data=f"delete_channel:{channel_id}")
    ])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة لقائمة القنوات", callback_data="list_channels:0")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def callback_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback فارغ (للأزرار غير القابلة للضغط)"""
    query = update.callback_query
    await query.answer()
