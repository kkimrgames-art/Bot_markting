"""
نظام بوت تيليجرام المستقل للأتمتة (Auto-Mod Only)
"""
import os
import logging
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

from .handlers import (
    auto_mod_handlers, 
    channel_handlers, 
    file_auth_handler, 
    edit_handlers, 
    language_handlers, 
    language_edit_handlers,
    api_key_handlers
)
from .raw_review import handle_raw_review_callback
from ..agent.config import load_config, update_admin_id

logger = logging.getLogger(__name__)

def build_application(token: str):
    # زيادة أوقات الانتظار (Timeouts) لتجنب أخطاء الشبكة أثناء Polling المستمر
    application = ApplicationBuilder().token(token).read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30).build()

    async def _auto_detect_admin(update, context):
        if not update.effective_user:
            return
        cfg = load_config()
        if not cfg.TELEGRAM_ALLOWED_USER_IDS:
            user_id = update.effective_user.id
            success = update_admin_id(user_id)
            if success:
                try:
                    from ..agent.alert_system import get_alert_system
                    get_alert_system().configure(bot_app=context.application, admin_chat_id=user_id)
                except Exception as e:
                    logger.debug(f"Failed to refresh AlertSystem after auto-detecting admin: {e}")
                logger.info(f"🔑 Auto-detected admin: {user_id}")
            else:
                logger.warning(f"⚠️ Failed to persist auto-detected admin: {user_id}")
            if update.message:
                await update.message.reply_text(
                    f"✅ تم تسجيلك كمسؤول للنظام.\nالمعرف الخاص بك: `{user_id}`",
                    parse_mode="Markdown",
                )

    application.add_handler(MessageHandler(filters.ALL, _auto_detect_admin), group=-1)

    application.add_handler(CallbackQueryHandler(handle_raw_review_callback, pattern=r"^rawrev:"))

    from .handlers.auto_mod_handlers import get_auto_mod_conversation_handler
    application.add_handler(get_auto_mod_conversation_handler())

    from .handlers.ai_manager_handler import get_ai_manager_conv
    application.add_handler(get_ai_manager_conv())

    file_auth_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(file_auth_handler.start_add_channel_file, pattern="^add_channel_file$"),
            CallbackQueryHandler(file_auth_handler.start_reauth, pattern="^reauth_start:")
        ],
        states={
            file_auth_handler.WAITING_FOR_FILE: [
                MessageHandler(filters.Document.ALL, file_auth_handler.receive_auth_file),
                CallbackQueryHandler(file_auth_handler.cancel_auth, pattern="^main_menu$")
            ],
            file_auth_handler.WAITING_FOR_CONFIRMATION: [
                CallbackQueryHandler(file_auth_handler.reauth_confirm, pattern="^reauth_confirm$"),
                CallbackQueryHandler(file_auth_handler.reauth_direct, pattern="^reauth_direct:"),
                CallbackQueryHandler(file_auth_handler.reauth_confirm, pattern="^reauth_new_file$"),
                CallbackQueryHandler(file_auth_handler.cancel_auth, pattern="^main_menu$")
            ],
            file_auth_handler.WAITING_FOR_AUTH_COMPLETION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, file_auth_handler.receive_auth_url),
                CallbackQueryHandler(file_auth_handler.set_channel_language, pattern="^set_lang_file:"),
                CallbackQueryHandler(file_auth_handler.cancel_auth, pattern="^main_menu$")
            ],
        },
        fallbacks=[CallbackQueryHandler(file_auth_handler.cancel_auth, pattern="^main_menu$")],
        per_message=False
    )
    application.add_handler(file_auth_conv)

    add_channel_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(channel_handlers.start_add_channel, pattern="^add_channel$")
        ],
        states={
            channel_handlers.ADD_CHANNEL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, channel_handlers.receive_channel_name),
                CallbackQueryHandler(channel_handlers.handle_add_method_choice, pattern="^add_method:")
            ],
            channel_handlers.ADD_YOUTUBE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, channel_handlers.receive_youtube_id)
            ],
            channel_handlers.ADD_LANGUAGE: [
                CallbackQueryHandler(language_handlers.receive_language, pattern="^lang:"),
            ],
            channel_handlers.ADD_CONTENT_TYPE: [
                CallbackQueryHandler(channel_handlers.receive_content_type, pattern="^content:")
            ],
            channel_handlers.ADD_PRIVACY: [
                CallbackQueryHandler(channel_handlers.receive_privacy, pattern="^privacy:")
            ],
            channel_handlers.ADD_INTERVAL: [
                CallbackQueryHandler(channel_handlers.receive_interval, pattern="^interval:")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(channel_handlers.cancel_add_channel, pattern="^(cancel_add|main_menu)$")
        ],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(add_channel_conv)

    application.add_handler(CallbackQueryHandler(channel_handlers.list_channels, pattern="^list_channels:"))
    application.add_handler(CallbackQueryHandler(channel_handlers.view_channel, pattern="^view_channel:"))
    application.add_handler(CallbackQueryHandler(channel_handlers.callback_noop, pattern="^noop$"))

    application.add_handler(CallbackQueryHandler(edit_handlers.toggle_channel, pattern="^toggle_channel:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.edit_content_type, pattern="^edit_content:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.set_content_type, pattern="^set_content:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.edit_privacy, pattern="^edit_privacy:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.set_privacy, pattern="^set_privacy:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.edit_interval, pattern="^edit_interval:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.set_interval, pattern="^set_interval:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.edit_quality, pattern="^edit_quality:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.set_quality, pattern="^set_quality:"))

    application.add_handler(CallbackQueryHandler(edit_handlers.edit_minecraft_overlay, pattern="^edit_minecraft_overlay:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.toggle_overlay_text, pattern="^toggle_overlay_text:"))

    overlay_text_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.set_overlay_text_start, pattern="^set_overlay_text_start:")],
        states={
            edit_handlers.OVERLAY_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_handlers.set_overlay_text_receive)
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(overlay_text_conv)

    overlay_size_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.set_overlay_size_start, pattern="^set_overlay_size_start:")],
        states={
            edit_handlers.OVERLAY_SIZE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_handlers.set_overlay_size_receive)
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(overlay_size_conv)

    custom_desc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.set_custom_description_start, pattern="^set_custom_desc_start:")],
        states={
            edit_handlers.CUSTOM_DESC_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_handlers.set_custom_description_receive)
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(custom_desc_conv)

    desc_sections_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.set_description_sections_start, pattern="^set_desc_sections_start:")],
        states={
            edit_handlers.DESCRIPTION_SECTIONS_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_handlers.set_description_sections_receive)
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(desc_sections_conv)

    facecam_upload_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.facecam_upload_start, pattern="^facecam_upload_start:")],
        states={
            edit_handlers.FACECAM_UPLOAD_INPUT: [
                MessageHandler(edit_handlers.FACECAM_UPLOAD_FILTER, edit_handlers.facecam_upload_receive)
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(facecam_upload_conv)

    custom_overlay_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_handlers.add_custom_overlay_start, pattern="^add_custom_overlay:")],
        states={
            edit_handlers.CUSTOM_OVERLAY_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_handlers.add_custom_overlay_text)
            ],
            edit_handlers.CUSTOM_OVERLAY_TIMING: [
                CallbackQueryHandler(edit_handlers.add_custom_overlay_timing, pattern="^ov_time:")
            ],
            edit_handlers.CUSTOM_OVERLAY_DURATION: [
                CallbackQueryHandler(edit_handlers.add_custom_overlay_duration, pattern="^ov_dur:")
            ],
            edit_handlers.CUSTOM_OVERLAY_POSITION: [
                CallbackQueryHandler(edit_handlers.add_custom_overlay_position, pattern="^ov_pos:")
            ],
        },
        fallbacks=[CallbackQueryHandler(auto_mod_handlers.auto_mod_menu, pattern="^main_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(custom_overlay_conv)

    application.add_handler(CallbackQueryHandler(edit_handlers.edit_custom_overlay_start, pattern="^edit_custom_overlay:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.delete_custom_overlay, pattern="^del_custom_overlay:"))

    application.add_handler(CallbackQueryHandler(edit_handlers.edit_facecam, pattern="^edit_facecam:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.toggle_facecam, pattern="^toggle_facecam:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.facecam_select_menu, pattern="^facecam_select:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.set_facecam_clip, pattern="^set_facecam_clip_idx:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.facecam_delete_menu, pattern="^facecam_delete_menu:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.delete_facecam_clip, pattern="^delete_facecam_clip_idx:"))

    application.add_handler(CallbackQueryHandler(language_edit_handlers.edit_language, pattern="^edit_language:"))
    application.add_handler(CallbackQueryHandler(language_edit_handlers.set_language, pattern="^set_language:"))
    application.add_handler(CallbackQueryHandler(language_edit_handlers.show_more_languages_for_edit, pattern="^more_languages:"))

    application.add_handler(CallbackQueryHandler(edit_handlers.delete_channel_confirm, pattern="^delete_channel:"))
    application.add_handler(CallbackQueryHandler(edit_handlers.delete_channel_confirmed, pattern="^confirm_delete:"))

    # === API Key Management ===
    api_key_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(api_key_handlers.add_key_start, pattern="^api_key_add$")],
        states={
            api_key_handlers.API_KEY_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_handlers.add_key_receive),
                CallbackQueryHandler(api_key_handlers.api_keys_menu, pattern="^api_keys_menu$")
            ],
        },
        fallbacks=[CallbackQueryHandler(api_key_handlers.api_keys_menu, pattern="^api_keys_menu$")],
        allow_reentry=True,
        per_message=False
    )
    application.add_handler(api_key_conv)
    application.add_handler(CallbackQueryHandler(api_key_handlers.api_keys_menu, pattern="^api_keys_menu$"))
    application.add_handler(CallbackQueryHandler(api_key_handlers.delete_key_menu, pattern="^api_key_delete_menu$"))
    application.add_handler(CallbackQueryHandler(api_key_handlers.delete_key_confirm, pattern="^api_key_delete:"))

    logger.info("✅ Auto-Mod Bot handlers registered (including Channel Management).")
    return application


async def run_polling_forever(application):
    await application.initialize()
    await application.start()
    if application.updater:
        await application.updater.start_polling()

    logger.info("🚀 Bot is polling...")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        if application.updater and application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def run_webhook_forever(application, webhook_url: str, secret_token: str | None = None):
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=secret_token or None,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )

    logger.info("🚀 Bot webhook is active.")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await application.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await application.stop()
        await application.shutdown()


async def start_bot():
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.error("❌ TELEGRAM_BOT_TOKEN is missing!")
        return
    application = build_application(token)
    await run_polling_forever(application)

if __name__ == "__main__":
    import asyncio
    asyncio.run(start_bot())
