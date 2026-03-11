"""
معالجات إضافية للغات - عرض جميع اللغات واختيار اللغة
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging
import math
import html

from ..language_manager import LanguageManager

logger = logging.getLogger(__name__)


async def receive_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال اللغة المختارة"""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(':')
    
    if len(data) < 2:
        return
    
    lang_code = data[1]
    
    # عرض جميع اللغات
    if lang_code == "more":
        await show_all_languages(update, context, page=0)
        return
    
    # حفظ اللغة
    context.user_data['new_channel']['language'] = lang_code
    
    lang = LanguageManager.get_language(lang_code)
    lang_name = lang.name if lang else lang_code
    
    text = (
        f"✅ اللغة: {LanguageManager.format_language_display(lang_code)}\n\n"
        "الخطوة 4/6: اختر نوع المحتوى"
    )
    
    keyboard = [
        [InlineKeyboardButton("🎮 ماين كرافت", callback_data="content:minecraft")],
        [InlineKeyboardButton("🎮 ألعاب (فيسبوك)", callback_data="content:games")],
        [InlineKeyboardButton("🎬 محتوى آخر", callback_data="content:other")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")]
    ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    # استيراد الحالة من channel_handlers
    from .channel_handlers import ADD_CONTENT_TYPE
    return ADD_CONTENT_TYPE


async def show_all_languages(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """عرض جميع اللغات مع pagination"""
    query = update.callback_query
    
    # الحصول على اللغات حسب المنطقة
    regions = LanguageManager.get_languages_by_region()
    
    text = "🌐 <b>اختر اللغة من القائمة:</b>\n\n"
    
    keyboard = []
    
    # عرض اللغات حسب المنطقة
    for region, languages in regions.items():
        # عنوان المنطقة
        text += f"<b>{html.escape(region)}:</b>\n"
        
        # اللغات
        row = []
        for lang in languages:
            row.append(InlineKeyboardButton(
                f"{lang.flag} {lang.name}",
                callback_data=f"lang:{lang.code}"
            ))
            
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        text += "\n"
    
    # زر الرجوع
    keyboard.append([InlineKeyboardButton("🔙 رجوع للغات الشائعة", callback_data="lang:back")])
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def back_to_popular_languages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الرجوع للغات الشائعة"""
    query = update.callback_query
    await query.answer()
    
    text = (
        "الخطوة 3/6: اختر لغة المحتوى\n\n"
        "🌍 <b>اللغات الشائعة:</b>"
    )
    
    # اللغات الأكثر شيوعاً
    popular = LanguageManager.get_popular_languages()
    keyboard = []
    
    # عرض اللغات الشائعة (صفين)
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
    
    # زر المزيد من اللغات
    keyboard.append([InlineKeyboardButton("🌐 المزيد من اللغات", callback_data="lang:more")])
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_add")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    from .channel_handlers import ADD_LANGUAGE
    return ADD_LANGUAGE
