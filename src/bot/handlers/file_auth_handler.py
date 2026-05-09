"""
معالج إضافة القنوات عبر ملف المصادقة
"""
import os
import json
import logging
import asyncio
import html
from pathlib import Path
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from ..auth_flow_utils import create_flow_from_file, exchange_code_and_get_creds, get_channel_info_from_creds, start_auth_flow
from ...agent.config import load_config
from ...agent.uploader import _find_client_secrets_file
from ..channel_manager import ChannelManager
from ..language_manager import LanguageManager
from ..download_utils import smart_download_file

logger = logging.getLogger(__name__)

# States
WAITING_FOR_FILE = 1
WAITING_FOR_AUTH_COMPLETION = 2
WAITING_FOR_LANGUAGE = 3
WAITING_FOR_CONFIRMATION = 4

async def start_reauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إعادة المصادقة لقناة موجودة - بدون الحاجة لرفع ملف جديد"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1]
    
    # جلب معلومات القناة
    cm = ChannelManager()
    channel = cm.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة.")
        return ConversationHandler.END
        
    # حفظ ID القناة المستهدفة
    context.user_data['target_channel_id'] = channel_id
    context.user_data['reauth_mode'] = True
    
    # Determine redirect URI (must match Google Console EXACTLY).
    cfg = load_config()
    client_secrets = _find_client_secrets_file(cfg)
    target_uri = (cfg.GOOGLE_REDIRECT_URI or os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    
    if not target_uri and external_url:
        target_uri = f"{external_url}/oauth2/callback"
    
    import html
    safe_name = html.escape(channel.channel_name)
    
    if client_secrets:
        # الملف موجود - يمكن المصادقة مباشرة بدون رفع
        text = (
            f"🔐 <b>إعادة المصادقة للقناة</b>\n\n"
            f"📺 القناة: <b>{safe_name}</b>\n\n"
            "✅ تم العثور على ملف المصادقة المحفوظ.\n"
            "اضغط <b>متابعة</b> لفتح رابط المصادقة.\n\n"
            f"⚠️ تأكد من إضافة الرابط التالي في Google Cloud:\n"
            f"<code>{html.escape(target_uri or 'http://localhost:8080/oauth2/callback')}</code>"
        )
        keyboard = [
            [InlineKeyboardButton("▶️ متابعة المصادقة", callback_data=f"reauth_direct:{channel_id}")],
            [InlineKeyboardButton("📂 رفع ملف جديد", callback_data="reauth_new_file")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
        ]
    else:
        # الملف غير موجود - نطلب رفعه
        text = (
            f"⚠️ <b>تأكيد إعادة المصادقة</b>\n\n"
            f"هل أنت متأكد أنك تريد إعادة مصادقة القناة:\n"
            f"<b>{safe_name}</b>\n\n"
            "⚠️ لم يتم العثور على ملف client_secret.json محفوظ.\n"
            "سيُطلب منك تحميل الملف."
        )
        keyboard = [
            [InlineKeyboardButton("✅ نعم، تابع", callback_data="reauth_confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
        ]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return WAITING_FOR_CONFIRMATION


async def reauth_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إعادة المصادقة مباشرة باستخدام ملف client_secret المحفوظ"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split(':')[1] if ':' in query.data else context.user_data.get('target_channel_id')
    
    cm = ChannelManager()
    channel = cm.get_channel(channel_id)
    if not channel:
        await query.edit_message_text("❌ القناة غير موجودة.")
        return ConversationHandler.END
    
    context.user_data['target_channel_id'] = channel_id
    context.user_data['reauth_mode'] = True
    
    cfg = load_config()
    client_secrets = _find_client_secrets_file(cfg)
    
    if not client_secrets:
        await query.edit_message_text("❌ ملف client_secret.json غير موجود. يرجى رفعه أولاً.")
        return ConversationHandler.END
    
    try:
        await query.edit_message_text("⏳ جاري تحضير رابط المصادقة...")
        
        # استخدام الملف المحفوظ مباشرة
        auth_url, server, flow = await asyncio.to_thread(start_auth_flow, client_secrets)
        
        context.user_data['oauth_flow'] = flow
        context.user_data['oauth_server'] = server
        
        redirect_uri = getattr(flow, "redirect_uri", None) or f"http://localhost:{server.port}/"
        
        text = (
            f"🔐 <b>رابط المصادقة جاهز</b>\n\n"
            f"📺 القناة: <b>{channel.channel_name}</b>\n\n"
            f"<a href=\"{auth_url}\">🔗 اضغط هنا للمصادقة</a>\n\n"
            "⚠️ <b>حل مشكلة mismatch:</b>\n"
            "يجب إضافة الرابط التالي في Google Cloud Console:\n"
            f"<code>{html.escape(redirect_uri)}</code>"
        )
        
        await query.edit_message_text(text, parse_mode='HTML', disable_web_page_preview=True)
        
        # بدء انتظار الكود
        task = context.application.create_task(wait_for_auth_code(update, context))
        if 'auth_tasks' not in context.bot_data:
            context.bot_data['auth_tasks'] = set()
        context.bot_data['auth_tasks'].add(task)
        task.add_done_callback(lambda t: context.bot_data['auth_tasks'].discard(t))
        
        return WAITING_FOR_AUTH_COMPLETION
        
    except Exception as e:
        logger.error(f"Error in reauth_direct: {e}")
        await query.edit_message_text(f"❌ حدث خطأ: {str(e)}")
        return ConversationHandler.END

async def reauth_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تأكيد إعادة المصادقة وطلب الملف"""
    query = update.callback_query
    await query.answer()
    
    text = (
        "📂 *إرسال ملف المصادقة*\n\n"
        "يرجى إرسال ملف `client_secret.json` الخاص بهذه القناة الآن.\n"
        "تأكد من أنه الملف الصحيح المرتبط بالحساب الذي تريد المصادقة عليه."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return WAITING_FOR_FILE

async def start_add_channel_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إضافة قناة عبر الملف"""
    query = update.callback_query
    await query.answer()
    
    # تنظيف أي بيانات سابقة
    context.user_data.pop('target_channel_id', None)
    context.user_data.pop('reauth_mode', None)
    
    text = (
        "📂 *إضافة قناة عبر ملف المصادقة*\n\n"
        "يرجى إرسال ملف `client_secret.json` الخاص ببيانات اعتماد Google Cloud Console.\n\n"
        "1. اذهب إلى Google Cloud Console\n"
        "2. حمل ملف JSON لـ OAuth Client ID (Desktop App)\n"
        "3. أرسل الملف هنا"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]]
    
    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return WAITING_FOR_FILE

async def receive_auth_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استلام ملف JSON ومعالجته"""
    if not update.message.document:
        await update.message.reply_text("❌ الرجاء إرسال ملف JSON.")
        return WAITING_FOR_FILE
        
    doc = update.message.document
    if not doc.file_name.endswith('.json'):
        await update.message.reply_text("❌ الملف يجب أن يكون بصيغة .json")
        return WAITING_FOR_FILE
        
    # تحميل الملف
    temp_dir = Path(".temp/auth")
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f"secret_{update.effective_user.id}.json"

    msg = await update.message.reply_text("⏳ جاري استلام الملف ومعالجته...")
    try:
        # استخدام التحميل الذكي لدعم السيرفر المحلي
        await smart_download_file(context, doc, str(file_path))
    except Exception as e:
        logger.error(f"Failed to download auth file: {e}")
        try:
            await msg.edit_text(f"❌ فشل تحميل الملف: {e}")
        except Exception:
            await update.message.reply_text(f"❌ فشل تحميل الملف: {e}")
        return WAITING_FOR_FILE
    
    try:
        # بدء عملية المصادقة
        await msg.edit_text("⏳ جاري تحضير رابط المصادقة...")
        
        # تشغيل في Thread منفصل لتجنب التجميد أثناء start_server
        flow, auth_url, server = await asyncio.to_thread(create_flow_from_file, str(file_path))
        
        # حفظ المتغيرات في context لاستخدامها لاحقاً
        context.user_data['oauth_flow'] = flow
        context.user_data['oauth_server'] = server
        context.user_data['secret_path'] = str(file_path)
        
        # عرض نفس Redirect URI المستخدم فعلياً داخل الـ Flow لتجنب mismatch
        redirect_uri = getattr(flow, "redirect_uri", None) or f"http://localhost:{server.port}/"
        logger.info(f"Redirect URI prepared: {redirect_uri}")
        
        # إرسال الرابط للمستخدم
        text = (
            "🔐 <b>رابط المصادقة جاهز</b>\n\n"
            "اضغط على الرابط أدناه لإكمال المصادقة:\n"
            f"<a href=\"{auth_url}\">🔗 اضغط هنا للمصادقة</a>\n\n"
            "⚠️ <b>حل مشكلة redirect_uri_mismatch:</b>\n"
            "إذا ظهر لك خطأ في جوجل، يجب أن تضيف <b>هذا الرابط بالضبط</b> في Google Cloud Console:\n"
            f"<code>{html.escape(redirect_uri)}</code>\n\n"
            "📌 <b>خطوات التصحيح:</b>\n"
            "1. اذهب لـ <a href=\"https://console.cloud.google.com/apis/credentials\">Credentials</a>\n"
            "2. اختر الـ OAuth Client ID الخاص بك\n"
            "3. أضف الرابط أعلاه في قسم <b>Authorized redirect URIs</b>\n"
            "4. احفظ التغييرات وانتظر دقيقة ثم حاول مرة أخرى."
        )
        
        await msg.edit_text(text, parse_mode='HTML', disable_web_page_preview=True)
        
        # بدء مهمة الخلفية لانتظار الكود
        task = context.application.create_task(wait_for_auth_code(update, context))
        
        # 🆕 حفظ المهمة لمنع الـ garbage collection
        if 'auth_tasks' not in context.bot_data:
            context.bot_data['auth_tasks'] = set()
        context.bot_data['auth_tasks'].add(task)
        task.add_done_callback(lambda t: context.bot_data['auth_tasks'].discard(t))
        
        return WAITING_FOR_AUTH_COMPLETION
        
    except Exception as e:
        logger.error(f"Error preparing auth: {e}")
        await update.message.reply_text(f"❌ حدث خطأ: {str(e)}")
        return ConversationHandler.END

async def wait_for_auth_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """انتظار الكود في الخلفية"""
    server = context.user_data.get('oauth_server')
    if not server:
        return
        
    try:
        logger.info(f"⏳ Waiting for auth response on port {server.port}...")
        # الانتظار (non-blocking via to_thread)
        response_uri = await asyncio.to_thread(server.wait_for_response, timeout=300)
        
        if response_uri and server.error:
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, f"❌ فشلت عملية المصادقة: {server.error}")
            return

        if response_uri:
            await process_auth_result(update, context, response_uri)
        else:
            try:
                server.stop()
            except Exception:
                pass
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, "❌ انتهت مهلة المصادقة أو فشلت. حاول مرة أخرى.")
            
    except Exception as e:
        logger.error(f"Auth wait error: {e}")
        try:
            server.stop()
        except Exception:
            pass
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, f"❌ خطأ أثناء المصادقة: {e}")

async def receive_auth_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال رابط المصادقة يدوياً من المستخدم"""
    url = update.message.text.strip()
    
    # التحقق من وجود كود في الرابط أو نص الكود نفسه
    if "code=" not in url and len(url) < 20: 
        await update.message.reply_text("⚠️ الرابط أو الكود الذي أرسلته يبدو غير صالح. يرجى التأكد من نسخ الرابط كاملاً من المتصفح.")
        return WAITING_FOR_AUTH_COMPLETION

    # إيقاف السيرفر إذا كان يعمل في الخلفية
    server = context.user_data.get('oauth_server')
    if server:
        try:
            server.stop()
        except:
            pass

    try:
        msg = await update.message.reply_text("⏳ جاري معالجة الكود المرفق...")
        await process_auth_result(update, context, url)
    except Exception as e:
        logger.error(f"Manual auth error: {e}")
        await update.message.reply_text(f"❌ حدث خطأ أثناء معالجة الرابط: {str(e)}")
    
    return WAITING_FOR_AUTH_COMPLETION

async def process_auth_result(update: Update, context: ContextTypes.DEFAULT_TYPE, response_uri: str):
    """معالجة نتيجة المصادقة (تبادل التوكن، جلب البيانات، الحفظ)"""
    logger.info("✅ Auth response received, exchanging tokens...")
    
    flow = context.user_data.get('oauth_flow')
    if not flow:
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, "❌ لم يتم العثور على جلسة مصادقة نشطة. يرجى البدء من جديد.")
        return

    # تبادل الكود عبر الرابط الكامل
    creds = await asyncio.to_thread(exchange_code_and_get_creds, flow, response_uri)
    
    # جلب معلومات القناة
    channel_info = await asyncio.to_thread(get_channel_info_from_creds, creds)
    logger.info(f"📺 Channel info retrieved: {channel_info['title']}")
    
    # تحقق من إعادة المصادقة (Re-auth Verification)
    if context.user_data.get('reauth_mode'):
        target_id = context.user_data.get('target_channel_id')
        cm = ChannelManager()
        target_channel = cm.get_channel(target_id)
        
        if not target_channel or channel_info['id'] != target_channel.youtube_channel_id:
             chat_id = update.effective_chat.id
             target_name = target_channel.channel_name if target_channel else "Unknown"
             await context.bot.send_message(
                 chat_id, 
                 f"❌ <b>فشل التحقق!</b>\n\n"
                 f"الحساب الذي سجلت الدخول به (<b>{html.escape(channel_info['title'])}</b>)\n"
                 f"لا يطابق القناة المستهدفة (<b>{html.escape(target_name)}</b>).\n\n"
                 "يرجى التأكد من تسجيل الدخول بنفس الحساب المرتبط بالقناة.",
                 parse_mode='HTML'
             )
             return

    # Load config to get paths
    from ...agent.config import load_config
    cfg = load_config()
    base_dir = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"

    # حفظ التوكن
    tokens_dir = Path(os.path.join(base_dir, "youtube_tokens"))
    tokens_dir.mkdir(parents=True, exist_ok=True)
    token_path = tokens_dir / f"{channel_info['id']}.json"
    
    with open(token_path, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
        
    # حفظ معلومات مؤقتة
    context.user_data['new_channel_info'] = channel_info
    
    chat_id = update.effective_chat.id
    
    # إذا كنا في وضع إعادة المصادقة، ننتهي هنا
    if context.user_data.get('reauth_mode'):
        try:
            target_id = context.user_data.get('target_channel_id')
            cm = ChannelManager()
            token_payload = None
            try:
                token_payload = json.loads(creds.to_json())
            except Exception:
                token_payload = None
            if target_id and token_payload:
                cm.update_channel(target_id, platform_credentials=token_payload)
        except Exception as e:
            logger.warning(f"Failed to persist refreshed auth token for channel: {e}")
        await context.bot.send_message(
            chat_id,
            f"✅ <b>تم تحديث المصادقة بنجاح!</b>\nبقناة: <b>{html.escape(channel_info['title'])}</b>",
            parse_mode='HTML'
        )
        # تنظيف الملف السري
        secret_path = context.user_data.get('secret_path')
        if secret_path and os.path.exists(secret_path):
            os.remove(secret_path)
        return

    # إبلاغ المستخدم وطلب اللغة (للإضافة الجديدة)
    lm = LanguageManager()
    keyboard = lm.get_languages_keyboard_by_region(page=0, callback_prefix="set_lang_file")
    
    msg_text = (
        f"✅ <b>تمت المصادقة بنجاح!</b>\n\n"
        f"📺 القناة: <b>{html.escape(channel_info['title'])}</b>\n"
        f"🆔 المعرف: <code>{channel_info['id']}</code>\n\n"
        "🌍 اختر لغة المحتوى للقناة:"
    )
    
    await context.bot.send_message(chat_id, msg_text, reply_markup=keyboard, parse_mode='HTML')

async def set_channel_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حفظ القناة بعد اختيار اللغة"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    lang_code = data.split(":")[1]
    
    channel_info = context.user_data.get('new_channel_info')
    if not channel_info:
        await query.edit_message_text("❌ انتهت صلاحية الجلسة. ابدأ من جديد.")
        return ConversationHandler.END
        
    # حفظ القناة
    cm = ChannelManager()
    cm.add_channel(
        channel_name=channel_info['title'],
        youtube_channel_id=channel_info['id'],
        language=lang_code,
        thumbnail=channel_info.get('thumbnail'),
        auth_method="oauth_file"
    )
    
    # تنظيف
    secret_path = context.user_data.get('secret_path')
    if secret_path and os.path.exists(secret_path):
        os.remove(secret_path)
        
    lm = LanguageManager()
    lang_name = lm.get_language_name(lang_code)
        
    await query.edit_message_text(
        f"✅ *تم إضافة القناة بنجاح!*\n\n"
        f"📺 الاسم: {channel_info['title']}\n"
        f"🆔 المعرف: `{channel_info['id']}`\n"
        f"🌍 اللغة: {lang_name}\n"
        f"🔓 الصلاحيات: كاملة (تم الرفع والتحقق)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def cancel_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء العملية"""
    if update.callback_query:
        await update.callback_query.edit_message_text("تم الإلغاء.")
    else:
        await update.message.reply_text("تم الإلغاء.")
        
    # تنظيف الخادم إذا كان يعمل
    server = context.user_data.get('oauth_server')
    if server:
        server.stop()
        
    return ConversationHandler.END
