"""
معالج تعديل اللغة للقنوات الموجودة
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging
import html

from ..channel_manager import ChannelManager
from ..language_manager import LanguageManager

logger = logging.getLogger(__name__)


async def edit_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تعديل لغة القناة"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    text = "🌍 <b>اختر اللغة الجديدة:</b>\n\n<b>اللغات الشائعة:</b>"
    
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
                callback_data=f"set_language:{lang_code}:{channel_id}"
            ))
            
            if len(row) == 2:
                keyboard.append(row)
                row = []
    
    if row:
        keyboard.append(row)
    
    # زر المزيد من اللغات
    keyboard.append([InlineKeyboardButton("🌐 المزيد من اللغات", callback_data=f"more_languages:{channel_id}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"view_channel:{channel_id}")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ اللغة الجديدة"""
    query = update.callback_query
    await query.answer()
    
    _, lang_code, channel_id = query.data.split(':')
    
    manager = ChannelManager()
    manager.update_channel(channel_id, language=lang_code)
    
    lang_display = LanguageManager.format_language_display(lang_code)
    await query.answer(f"✅ تم تغيير اللغة إلى: {lang_display}")
    
    # إعادة عرض صفحة القناة
    from .channel_handlers import view_channel
    try:
        update.callback_query.data = f"view_channel:{channel_id}"
    except Exception:
        pass
    await view_channel(update, context)


async def show_more_languages_for_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض جميع اللغات لتعديل القناة"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    # الحصول على اللغات حسب المنطقة
    regions = LanguageManager.get_languages_by_region()
    
    text = "🌐 <b>اختر اللغة من القائمة:</b>\n\n"
    
    keyboard = []
    
    # عرض اللغات حسب المنطقة
    for region, languages in regions.items():
        # اللغات
        row = []
        for lang in languages:
            row.append(InlineKeyboardButton(
                f"{lang.flag} {lang.name}",
                callback_data=f"set_language:{lang.code}:{channel_id}"
            ))
            
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
    
    # زر الرجوع
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"edit_language:{channel_id}")])
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
