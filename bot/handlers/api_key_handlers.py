"""
معالجات إدارة مفاتيح YouTube API عبر بوت تيليجرام
- إضافة / حذف / عرض المفاتيح
- عرض حالة الحصة
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from ...agent.youtube_api_keys import get_key_manager

logger = logging.getLogger(__name__)

# حالات المحادثة
API_KEY_INPUT = 100


async def _safe_answer(query, **kwargs):
    try:
        await query.answer(**kwargs)
    except Exception:
        pass


# ==================== القائمة الرئيسية ====================

async def api_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة إدارة مفاتيح API"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    mgr = get_key_manager()
    keys = mgr.list_keys()
    info = mgr.get_total_quota_info()

    # شريط الحصة الإجمالية
    if info["total_limit"] > 0:
        pct = info["total_used"] / info["total_limit"]
        bar_len = 10
        filled = int(pct * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
    else:
        bar = "░" * 10
        pct = 0

    text = (
        f"🔑 **إدارة مفاتيح YouTube API**\n\n"
        f"📊 الحصة الإجمالية: {bar} {info['total_used']:,}/{info['total_limit']:,}\n"
        f"📦 المفاتيح: {info['active_keys']} نشط / {info['total_keys']} إجمالي\n"
        f"✨ المتبقي: **{info['total_remaining']:,}** وحدة\n"
    )

    if keys:
        text += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for k in keys:
            status = "✅" if k["is_active"] else "❌"
            q_pct = k["quota_used"] / max(1, k["quota_limit"])
            q_bar_len = 6
            q_filled = int(q_pct * q_bar_len)
            q_bar = "█" * q_filled + "░" * (q_bar_len - q_filled)
            text += (
                f"\n{status} **{k['label']}**\n"
                f"   `{k['api_key_masked']}`\n"
                f"   {q_bar} {k['quota_used']:,}/{k['quota_limit']:,}\n"
            )

    buttons = [
        [InlineKeyboardButton("➕ إضافة مفتاح", callback_data="api_key_add")],
    ]

    if keys:
        buttons.append([InlineKeyboardButton("🗑️ حذف مفتاح", callback_data="api_key_delete_menu")])

    buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="api_keys_menu")])
    buttons.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    return ConversationHandler.END


# ==================== إضافة مفتاح ====================

async def add_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء إضافة مفتاح — طلب إدخال المفتاح"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    text = (
        "🔑 **إضافة مفتاح YouTube API**\n\n"
        "أرسل مفتاح API الخاص بك.\n"
        "يمكنك الحصول على مفتاح مجاني من:\n"
        "[Google Cloud Console](https://console.cloud.google.com/apis/credentials)\n\n"
        "📌 تأكد من تفعيل **YouTube Data API v3**\n\n"
        "أرسل المفتاح الآن:"
    )

    buttons = [[InlineKeyboardButton("❌ إلغاء", callback_data="api_keys_menu")]]

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return API_KEY_INPUT


async def add_key_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استلام المفتاح والتحقق منه"""
    api_key = update.message.text.strip()

    if not api_key or len(api_key) < 20:
        await update.message.reply_text(
            "❌ المفتاح غير صالح. يجب أن يكون أطول من 20 حرف.\nأعد المحاولة أو اضغط إلغاء.",
        )
        return API_KEY_INPUT

    # التحقق من المفتاح
    mgr = get_key_manager()
    status_msg = await update.message.reply_text("⏳ جارٍ التحقق من المفتاح...")

    is_valid = mgr.validate_key(api_key)
    if not is_valid:
        await status_msg.edit_text(
            "❌ **المفتاح غير صالح!**\n"
            "تأكد من أن المفتاح صحيح و YouTube Data API v3 مفعّل.\n\n"
            "أعد المحاولة أو اضغط إلغاء.",
            parse_mode="Markdown",
        )
        return API_KEY_INPUT

    # إضافة المفتاح
    user = update.effective_user
    label = f"Key-{len(mgr.list_keys()) + 1}"
    added_by = f"@{user.username}" if user.username else str(user.id)
    key_data = mgr.add_key(api_key, label=label, added_by=added_by)

    await status_msg.edit_text(
        f"✅ **تم إضافة المفتاح بنجاح!**\n\n"
        f"📌 الاسم: **{key_data.get('label', label)}**\n"
        f"🔑 المفتاح: `{api_key[:8]}...{api_key[-4:]}`\n"
        f"📊 الحصة: {YOUTUBE_DAILY_QUOTA:,} وحدة يومياً",
        parse_mode="Markdown",
    )

    # عرض القائمة
    buttons = [
        [InlineKeyboardButton("➕ إضافة مفتاح آخر", callback_data="api_key_add")],
        [InlineKeyboardButton("🔑 إدارة المفاتيح", callback_data="api_keys_menu")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    await update.message.reply_text(
        "ماذا تريد أن تفعل؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


# ==================== حذف مفتاح ====================

async def delete_key_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة المفاتيح للحذف"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    mgr = get_key_manager()
    keys = mgr.list_keys()

    if not keys:
        text = "⚠️ لا توجد مفاتيح للحذف."
        buttons = [[InlineKeyboardButton("🔙 رجوع", callback_data="api_keys_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    text = "🗑️ **اختر المفتاح للحذف:**\n"
    buttons = []
    for k in keys:
        btn_text = f"❌ {k['label']} ({k['api_key_masked']})"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"api_key_delete:{k['key_id']}")])

    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="api_keys_menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def delete_key_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تأكيد حذف المفتاح"""
    query = update.callback_query
    await _safe_answer(query)

    key_id = query.data.split(":")[1]
    mgr = get_key_manager()
    success = mgr.remove_key(key_id)

    if success:
        await query.edit_message_text(f"✅ تم حذف المفتاح `{key_id}` بنجاح.", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ المفتاح غير موجود.")

    # العودة للقائمة
    buttons = [[InlineKeyboardButton("🔑 إدارة المفاتيح", callback_data="api_keys_menu")]]
    await query.message.reply_text("🔙", reply_markup=InlineKeyboardMarkup(buttons))


# ==================== Helper for import ==================

YOUTUBE_DAILY_QUOTA = 10_000
