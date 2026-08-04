#!/usr/bin/env python3
"""
معالجات ناشر مقالات البلوجر - Blogger Article Publisher Handlers
"""
import os
import html
import json
import asyncio
import logging
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# ==================== حالات المحادثة ====================
(
    BL_MENU,
    BL_CHOOSE_SOURCE,
    BL_CHOOSE_TOKEN,
    BL_CHOOSE_BLOG,
    BL_SETTINGS,
    BL_SET_LINK_TITLE,
    BL_SET_AI_MODE,
    BL_SET_AI_PROMPT,
    BL_SET_LANGUAGE,
    BL_SET_TEMPLATES_ORDER,
    BL_ADD_TEMPLATE_TITLE,
    BL_ADD_TEMPLATE_CONTENT,
    BL_CONFIRM,
    BL_LIST_LINKS,
    BL_LINK_DETAIL,
    BL_EDIT_LINK,
) = range(16)


# ==================== مساعدات ====================

async def _safe_answer(query, **kwargs):
    try:
        if query:
            await query.answer(**kwargs)
    except Exception:
        pass


async def _edit_or_send(update, context, text, reply_markup=None, parse_mode="HTML"):
    query = update.callback_query if update.callback_query else None
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)


def _get_sources_list() -> List[Dict]:
    """جلب قائمة المصادر من auto_mod_sources"""
    try:
        from ...agent.supabase_client import supabase_select
        from ...agent.auto_mod_fetcher import get_instance_id, _local_select_rows

        instance_id = get_instance_id()
        filters = {"instance_id": instance_id}
        sources = supabase_select("auto_mod_sources", filters) or []

        # دمج مع البيانات المحلية لضمان ظهور المصادر الجديدة
        local_sources = _local_select_rows("auto_mod_sources", filters) or []
        seen_ids = {src.get("id") for src in sources if src.get("id")}
        for src in local_sources:
            src_id = src.get("id")
            if src_id and src_id not in seen_ids:
                sources.append(src)

        return sources
    except Exception:
        return []


def _get_token_channels() -> List[Dict]:
    """
    جلب قائمة القنوات التي لديها توكن OAuth (مصادقة Google).
    يعيد [{"channel_id": ..., "channel_name": ..., "token_path": ...}]
    """
    result = []
    seen_paths = set()

    # 1. جلب القنوات من channel_configs وربطها بملفات التوكن
    try:
        from ...agent.supabase_storage import list_channel_configs
        from ...agent.config import get_project_root
        token_dir = os.path.join(get_project_root(), ".data", "youtube_tokens")

        configs = list_channel_configs() or []
        yt_id_to_config = {}
        for cfg in configs:
            yt_ch_id = cfg.get("youtube_channel_id") or cfg.get("platform_channel_id") or ""
            if yt_ch_id:
                yt_id_to_config[yt_ch_id] = cfg

        # البحث في مجلد التوكنات وربطها بالقنوات
        if os.path.exists(token_dir):
            for f in os.listdir(token_dir):
                if not f.endswith(".json"):
                    continue
                fp = os.path.join(token_dir, f)
                ch_id_from_file = f.replace(".json", "")

                # محاولة الربط بقناة من channel_configs
                matched_cfg = yt_id_to_config.get(ch_id_from_file)
                if matched_cfg:
                    ch_name = matched_cfg.get("channel_name") or matched_cfg.get("youtube_channel_name") or ch_id_from_file
                    db_channel_id = matched_cfg.get("channel_id") or ch_id_from_file
                else:
                    ch_name = ch_id_from_file
                    db_channel_id = ch_id_from_file

                if fp not in seen_paths:
                    seen_paths.add(fp)
                    result.append({
                        "channel_id": db_channel_id,
                        "channel_name": ch_name,
                        "token_path": fp,
                    })
    except Exception as e:
        logger.debug(f"Error loading token channels: {e}")

    return result


# ==================== القائمة الرئيسية ====================

async def blogger_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """قائمة ناشر مقالات البلوجر"""
    query = update.callback_query
    await _safe_answer(query)

    from ...agent.blogger_db import get_blogger_links, ensure_tables_exist
    ensure_tables_exist()

    links = get_blogger_links()
    active_count = sum(1 for l in links if l.get("enabled", True))

    text = (
        "📝 <b>ناشر مقالات مع فيديو</b>\n\n"
        f"📊 الروابط النشطة: <b>{active_count}</b> | الإجمالي: <b>{len(links)}</b>\n\n"
        "هذه الميزة تتيح لك ربط أي مصدر (وكيل) بمدونة بلوجر.\n"
        "قبل نشر أي فيديو، يتم إنشاء مقال على البلوجر تلقائياً\n"
        "ووضع رابط المقال في وصف الفيديو.\n\n"
        "💡 <i>يتم استخدام نفس مصادقة Google المستخدمة لقنوات يوتيوب</i>"
    )

    keyboard = [
        [InlineKeyboardButton("➕ ربط مصدر جديد بمدونة", callback_data="bl_add_start")],
        [InlineKeyboardButton("📋 عرض الروابط الحالية", callback_data="bl_list_links")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="am_menu")],
    ]

    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_MENU


# ==================== إضافة ربط جديد ====================

async def add_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء إضافة رابط جديد - اختيار المصدر"""
    query = update.callback_query
    await _safe_answer(query)

    sources = _get_sources_list()

    if not sources:
        text = "❌ <b>لا توجد مصادر متاحة</b>\n\nأضف مصادر أولاً من قسم 'إدارة المصادر'."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_menu")],
        ])
        await _edit_or_send(update, context, text, keyboard)
        return BL_MENU

    context.user_data["bl_sources"] = sources

    text = "📡 <b>الخطوة 1: اختر المصدر (الوكيل)</b>\n\n"
    text += "اختر المصدر الذي تريد ربطه بمدونة بلوجر:\n"

    keyboard = []
    for s in sources:
        name = html.escape(s.get("source_name", s.get("id", "?"))[:30])
        sid = s.get("id", "")
        enabled_icon = "✅" if s.get("enabled", True) else "⏸️"
        keyboard.append([
            InlineKeyboardButton(f"{enabled_icon} {name}", callback_data=f"bl_src:{sid}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="bl_menu")])
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_CHOOSE_SOURCE


async def choose_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار المصدر"""
    query = update.callback_query
    await _safe_answer(query)

    source_id = query.data.split(":", 1)[1]
    sources = context.user_data.get("bl_sources", [])
    source = next((s for s in sources if s.get("id") == source_id), None)

    if not source:
        await _edit_or_send(update, context, "❌ المصدر غير موجود", InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")],
        ]))
        return BL_CHOOSE_SOURCE

    context.user_data["bl_selected_source"] = source
    context.user_data["bl_link_data"] = {
        "source_id": source_id,
        "channel_id": source.get("channel_id", ""),
    }

    # الانتقال لاختيار الحساب/التوكن
    return await _show_token_selection(update, context)


async def _show_token_selection(update, context) -> int:
    """عرض قائمة الحسابات المتاحة (التي لديها توكن Google)"""
    query = update.callback_query
    await _safe_answer(query)

    token_channels = _get_token_channels()

    if not token_channels:
        text = (
            "❌ <b>لا توجد حسابات بمصادقة Google</b>\n\n"
            "لازم يكون عندك قناة يوتيوب مضافة بمصادقة Google OAuth.\n"
            "أضف قناة من القائمة الرئيسية أولاً."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")],
        ])
        await _edit_or_send(update, context, text, keyboard)
        return BL_CHOOSE_SOURCE

    context.user_data["bl_token_channels"] = token_channels

    text = "🔐 <b>الخطوة 2: اختر الحساب (المصادقة)</b>\n\n"
    text += "اختر الحساب الذي تريد استخدامه لنشر المقالات.\n"
    text += "<i>نفس بيانات OAuth المستخدمة لقنوات يوتيوب.</i>\n\n"

    keyboard = []
    for ch in token_channels:
        name = html.escape(ch["channel_name"][:30])
        ch_id = ch["channel_id"]
        keyboard.append([
            InlineKeyboardButton(f"🌐 {name}", callback_data=f"bl_token:{ch_id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")])
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_CHOOSE_TOKEN


async def choose_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار التوكن وجلب المدونات"""
    query = update.callback_query
    await _safe_answer(query, text="⏳ جاري جلب المدونات...")

    ch_id = query.data.split(":", 1)[1]
    token_channels = context.user_data.get("bl_token_channels", [])
    token_ch = next((c for c in token_channels if c["channel_id"] == ch_id), None)

    if not token_ch:
        await _edit_or_send(update, context, "❌ الحساب غير موجود", InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")],
        ]))
        return BL_CHOOSE_TOKEN

    context.user_data["bl_selected_token"] = token_ch
    context.user_data["bl_link_data"]["token_path"] = token_ch["token_path"]

    # جلب المدونات المتاحة
    try:
        from ...agent.blogger_integration import get_blogs_for_token
        blogs = await asyncio.to_thread(get_blogs_for_token, token_ch["token_path"])

        if not blogs:
            text = (
                "⚠️ <b>لا توجد مدونات بلوجر في هذا الحساب</b>\n\n"
                "تأكد من أن حسابك يحتوي على مدونة على Blogger.\n"
                "يمكنك إنشاء مدونة من <a href=\"https://www.blogger.com\">blogger.com</a>"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"bl_token:{ch_id}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")],
            ])
            await _edit_or_send(update, context, text, keyboard)
            return BL_CHOOSE_TOKEN

        context.user_data["bl_blogs"] = blogs

        text = "🌐 <b>الخطوة 3: اختر المدونة</b>\n\n"
        text += "اختر المدونة التي تريد النشر عليها:\n"

        keyboard = []
        for blog in blogs:
            name = html.escape(blog.get("name", "?"))[:35]
            blog_id = blog.get("id", "")
            blog_url = blog.get("url", "")
            keyboard.append([
                InlineKeyboardButton(f"📝 {name}", callback_data=f"bl_blog:{blog_id}")
            ])

        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="bl_show_tokens")])
        await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
        return BL_CHOOSE_BLOG

    except Exception as e:
        logger.error(f"Failed to fetch blogs: {e}", exc_info=True)
        text = f"❌ خطأ في جلب المدونات:\n<code>{html.escape(str(e)[:200])}</code>"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"bl_token:{ch_id}")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")],
        ])
        await _edit_or_send(update, context, text, keyboard)
        return BL_CHOOSE_TOKEN


async def choose_blog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة اختيار المدونة والانتقال للإعدادات"""
    query = update.callback_query
    await _safe_answer(query)

    blog_id = query.data.split(":", 1)[1]
    blogs = context.user_data.get("bl_blogs", [])
    blog = next((b for b in blogs if b.get("id") == blog_id), None)

    if not blog:
        await _edit_or_send(update, context, "❌ المدونة غير موجودة", InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_show_tokens")],
        ]))
        return BL_CHOOSE_BLOG

    link_data = context.user_data.get("bl_link_data", {})
    link_data["blog_id"] = blog_id
    link_data["blog_name"] = blog.get("name", "")
    link_data["blog_url"] = blog.get("url", "")
    context.user_data["bl_link_data"] = link_data
    context.user_data["bl_selected_blog"] = blog

    return await _show_settings_menu(update, context)


# ==================== إعدادات الربط ====================

async def _show_settings_menu(update, context) -> int:
    """عرض قائمة إعدادات الربط"""
    query = update.callback_query
    await _safe_answer(query)

    ld = context.user_data.get("bl_link_data", {})
    source = context.user_data.get("bl_selected_source", {})
    blog = context.user_data.get("bl_selected_blog", {})

    ai_mode = ld.get("ai_mode", "ai_prompt")
    ai_mode_label = {
        "ai_prompt": "🤖 برومت AI مخصص",
        "templates": "📄 مقالات افتراضية",
        "fallback": "📝 بدون AI (احتياطي)",
    }.get(ai_mode, ai_mode)

    templates_order = ld.get("templates_order", "sequential")
    order_label = "ترتيبي 🔢" if templates_order == "sequential" else "عشوائي 🎲"

    lang = ld.get("article_language", "ar")
    lang_label = {"ar": "العربية 🇸🇦", "en": "English 🇬🇧", "tr": "Türkçe 🇹🇷", "fr": "Français 🇫🇷", "es": "Español 🇪🇸"}.get(lang, lang)

    link_title = html.escape(ld.get("link_title", "🔗 اقرأ المزيد على المدونة"))
    prompt_preview = html.escape((ld.get("ai_prompt", "") or "")[:80]) or "⟨لم يتم تحديد⟩"

    templates_raw = ld.get("templates", "[]")
    try:
        templates_list = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
        template_count = len(templates_list)
    except Exception:
        template_count = 0

    text = (
        "⚙️ <b>الخطوة 4: إعدادات المقالات</b>\n\n"
        f"📡 المصدر: <b>{html.escape(source.get('source_name', '?'))}</b>\n"
        f"📝 المدونة: <b>{html.escape(blog.get('name', '?'))}</b>\n\n"
        f"🔗 عنوان الرابط: <code>{link_title}</code>\n"
        f"🤖 وضع المقالات: <b>{ai_mode_label}</b>\n"
        f"🌍 اللغة: <b>{lang_label}</b>\n"
    )

    if ai_mode == "ai_prompt":
        text += f"💬 البرومبت: <code>{prompt_preview}</code>\n"
    elif ai_mode == "templates":
        text += f"📄 عدد القوالب: <b>{template_count}</b>\n"
        text += f"🔀 ترتيب الاستخدام: <b>{order_label}</b>\n"

    keyboard = [
        [InlineKeyboardButton(f"🔗 تعديل عنوان الرابط", callback_data="bl_set_link_title")],
        [InlineKeyboardButton(f"🤖 وضع المقالات: {ai_mode_label}", callback_data="bl_set_ai_mode")],
        [InlineKeyboardButton(f"🌍 اللغة: {lang_label}", callback_data="bl_set_language")],
    ]

    if ai_mode == "ai_prompt":
        keyboard.append([InlineKeyboardButton("💬 تعديل البرومبت", callback_data="bl_set_ai_prompt")])
    elif ai_mode == "templates":
        keyboard.append([InlineKeyboardButton(f"📄 إدارة القوالب ({template_count})", callback_data="bl_manage_templates")])
        keyboard.append([InlineKeyboardButton(f"🔀 الترتيب: {order_label}", callback_data="bl_toggle_order")])

    keyboard.append([InlineKeyboardButton("✅ تأكيد وحفظ", callback_data="bl_confirm_save")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="bl_add_start")])

    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_SETTINGS


# ==================== تعديل الإعدادات ====================

async def set_link_title_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    text = "🔗 <b>تعديل عنوان الرابط</b>\n\n"
    text += "أرسل العنوان الذي سيظهر فوق رابط المقال في وصف الفيديو.\n"
    text += "مثال: <code>🔗 اقرأ المقال كاملاً هنا</code>"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="bl_settings")],
    ])
    await _edit_or_send(update, context, text, keyboard)
    return BL_SET_LINK_TITLE


async def receive_link_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("❌ العنوان فارغ. أرسل العنوان:")
        return BL_SET_LINK_TITLE
    context.user_data["bl_link_data"]["link_title"] = title
    return await _show_settings_menu(update, context)


async def set_ai_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    text = "🤖 <b>اختر وضع إنشاء المقالات</b>\n\n"
    keyboard = [
        [InlineKeyboardButton("🤖 بروبت AI مخصص", callback_data="bl_mode:ai_prompt")],
        [InlineKeyboardButton("📄 مقالات افتراضية", callback_data="bl_mode:templates")],
        [InlineKeyboardButton("📝 بدون AI (احتياطي)", callback_data="bl_mode:fallback")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="bl_settings")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_SET_AI_MODE


async def choose_ai_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    mode = query.data.split(":", 1)[1]
    context.user_data["bl_link_data"]["ai_mode"] = mode
    return await _show_settings_menu(update, context)


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    text = "🌍 <b>اختر لغة المقالات</b>\n\n"
    keyboard = [
        [InlineKeyboardButton("🇸🇦 العربية", callback_data="bl_lang:ar")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="bl_lang:en")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="bl_lang:tr")],
        [InlineKeyboardButton("🇫🇷 Français", callback_data="bl_lang:fr")],
        [InlineKeyboardButton("🇪🇸 Español", callback_data="bl_lang:es")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="bl_settings")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_SET_LANGUAGE


async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    lang = query.data.split(":", 1)[1]
    context.user_data["bl_link_data"]["article_language"] = lang
    return await _show_settings_menu(update, context)


async def set_ai_prompt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    text = (
        "💬 <b>تعديل البرومبت</b>\n\n"
        "أرسل البرومبت الذي سيوجه الذكاء الاصطناعي لإنشاء المقال.\n\n"
        "💡 <b>نصائح:</b>\n"
        "• حدد نوع المقال وطوله وأسلوبه\n"
        "• اذكر ما تريد تضمينه في المقال\n"
        "• كن محدداً للحصول على نتائج أفضل\n\n"
        "مثال:\n"
        "<code>اكتب مقال شامل عن المود المذكور في العنوان، يتضمن شرحاً مفصلاً ومميزات المود وكيفية التثبيت.</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="bl_settings")],
    ])
    await _edit_or_send(update, context, text, keyboard)
    return BL_SET_AI_PROMPT


async def receive_ai_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text("❌ البرومبت فارغ. أرسل البرومبت:")
        return BL_SET_AI_PROMPT
    context.user_data["bl_link_data"]["ai_prompt"] = prompt
    return await _show_settings_menu(update, context)


# ==================== إدارة القوالب ====================

async def manage_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    ld = context.user_data.get("bl_link_data", {})
    templates_raw = ld.get("templates", "[]")
    try:
        templates = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
    except Exception:
        templates = []

    if not templates:
        text = "📄 <b>إدارة القوالب</b>\n\nلا توجد قوالب بعد. أضف قالباً جديداً."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة قالب", callback_data="bl_add_template")],
            [InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="bl_settings")],
        ])
    else:
        text = f"📄 <b>إدارة القوالب</b> ({len(templates)} قالب)\n\n"
        keyboard = []
        for i, t in enumerate(templates):
            title = html.escape(t.get("title", "بدون عنوان")[:30])
            keyboard.append([
                InlineKeyboardButton(f"{i+1}. {title}", callback_data=f"noop"),
                InlineKeyboardButton("🗑", callback_data=f"bl_del_template:{i}"),
            ])
        keyboard.append([InlineKeyboardButton("➕ إضافة قالب", callback_data="bl_add_template")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="bl_settings")])

    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_SETTINGS


async def add_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    text = "📄 <b>إضافة قالب جديد</b>\n\nأرسل <b>عنوان</b> القالب:\n"
    text += "\n💡 يمكنك استخدام المتغيرات: {video_title}, {date}, {time}, {datetime}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع", callback_data="bl_manage_templates")],
    ])
    await _edit_or_send(update, context, text, keyboard)
    return BL_ADD_TEMPLATE_TITLE


async def receive_template_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("❌ العنوان فارغ. أرسل العنوان:")
        return BL_ADD_TEMPLATE_TITLE
    context.user_data["bl_new_template"] = {"title": title, "content": "", "labels": []}
    text = "📝 <b>محتوى القالب</b>\n\nأرسل <b>محتوى</b> القالب بصيغة HTML:\n"
    text += "\n💡 مثال:\n"
    text += "<code>&lt;h2&gt;{video_title}&lt;/h2&gt;&lt;p&gt;شاهد الفيديو على قناتنا!&lt;/p&gt;</code>"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي المحتوى", callback_data="bl_skip_template_content")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="bl_manage_templates")],
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    return BL_ADD_TEMPLATE_CONTENT


async def receive_template_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    content = (update.message.text or "").strip()
    template = context.user_data.get("bl_new_template", {})
    template["content"] = content
    context.user_data["bl_new_template"] = template

    # حفظ القالب
    ld = context.user_data.get("bl_link_data", {})
    templates_raw = ld.get("templates", "[]")
    try:
        templates = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
    except Exception:
        templates = []
    templates.append(template)
    ld["templates"] = json.dumps(templates, ensure_ascii=False)
    context.user_data["bl_link_data"] = ld

    await update.message.reply_text("✅ تم إضافة القالب بنجاح!", parse_mode="HTML")
    return await manage_templates(update, context)


async def skip_template_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    template = context.user_data.get("bl_new_template", {})
    template["content"] = "<p>{video_title}</p><p>شاهد الفيديو على قناتنا!</p>"
    context.user_data["bl_new_template"] = template
    return await receive_template_content(update, context)


async def delete_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    idx = int(query.data.split(":", 1)[1])

    ld = context.user_data.get("bl_link_data", {})
    templates_raw = ld.get("templates", "[]")
    try:
        templates = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
    except Exception:
        templates = []

    if 0 <= idx < len(templates):
        templates.pop(idx)
        ld["templates"] = json.dumps(templates, ensure_ascii=False)
        context.user_data["bl_link_data"] = ld

    return await manage_templates(update, context)


async def toggle_templates_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    ld = context.user_data.get("bl_link_data", {})
    current = ld.get("templates_order", "sequential")
    ld["templates_order"] = "random" if current == "sequential" else "sequential"
    context.user_data["bl_link_data"] = ld
    return await _show_settings_menu(update, context)


# ==================== تأكيد وحفظ ====================

async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    ld = context.user_data.get("bl_link_data", {})
    source = context.user_data.get("bl_selected_source", {})
    blog = context.user_data.get("bl_selected_blog", {})

    text = (
        "✅ <b>تأكيد إنشاء الربط</b>\n\n"
        f"📡 المصدر: <b>{html.escape(source.get('source_name', '?'))}</b>\n"
        f"📝 المدونة: <b>{html.escape(blog.get('name', '?'))}</b>\n"
        f"🌐 رابط: <code>{html.escape(blog.get('url', ''))}</code>\n\n"
        f"🔗 عنوان الرابط: <code>{html.escape(ld.get('link_title', ''))}</code>\n"
        f"🤖 الوضع: <code>{ld.get('ai_mode', 'ai_prompt')}</code>\n"
        f"🌍 اللغة: <code>{ld.get('article_language', 'ar')}</code>\n"
    )

    keyboard = [
        [InlineKeyboardButton("✅ نعم، احفظ", callback_data="bl_do_save")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="bl_add_start")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_CONFIRM


async def do_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    ld = context.user_data.get("bl_link_data", {})
    blog = context.user_data.get("bl_selected_blog", {})

    # حفظ في قاعدة البيانات
    from ...agent.blogger_db import save_blogger_link

    save_data = {
        "source_id": ld.get("source_id", ""),
        "channel_id": ld.get("channel_id", ""),
        "blog_id": ld.get("blog_id", ""),
        "blog_name": blog.get("name", ""),
        "blog_url": blog.get("url", ""),
        "enabled": True,
        "link_title": ld.get("link_title", "🔗 اقرأ المزيد على المدونة"),
        "ai_mode": ld.get("ai_mode", "ai_prompt"),
        "ai_prompt": ld.get("ai_prompt", ""),
        "templates_order": ld.get("templates_order", "sequential"),
        "templates": ld.get("templates", "[]"),
        "article_language": ld.get("article_language", "ar"),
    }

    # حفظ التوكن في extra_data أو ربطه بالقناة
    token_path = ld.get("token_path", "")
    if token_path:
        save_data["token_path"] = token_path

    try:
        ok = save_blogger_link(save_data)
        if ok:
            await _edit_or_send(update, context, "✅ <b>تم حفظ الربط بنجاح!</b>\n\n"
                "الآن عند نشر أي فيديو من هذا المصدر، سيتم إنشاء مقال على المدونة تلقائياً.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 عرض الروابط", callback_data="bl_list_links")],
                    [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="am_menu")],
                ]))
        else:
            await _edit_or_send(update, context, "❌ فشل حفظ الربط", InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="bl_settings")],
            ]))
    except Exception as e:
        logger.error(f"Save blogger link failed: {e}", exc_info=True)
        await _edit_or_send(update, context, f"❌ خطأ: <code>{html.escape(str(e)[:200])}</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="bl_settings")]]))

    # تنظيف
    for key in list(context.user_data.keys()):
        if key.startswith("bl_"):
            context.user_data.pop(key, None)

    return BL_MENU


# ==================== عرض الروابط ====================

async def list_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    from ...agent.blogger_db import get_blogger_links
    links = get_blogger_links()

    if not links:
        text = "📭 <b>لا توجد روابط</b>\n\nلم يتم ربط أي مصدر بمدونة بعد."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة ربط", callback_data="bl_add_start")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_menu")],
        ])
        await _edit_or_send(update, context, text, keyboard)
        return BL_MENU

    text = f"📋 <b>الروابط الحالية</b> ({len(links)})\n\n"
    keyboard = []
    for link in links:
        name = html.escape(link.get("blog_name", "?"))[:25]
        source_name = html.escape(link.get("source_id", ""))[:15]
        enabled = "✅" if link.get("enabled", True) else "⏸️"
        link_id = link.get("id", "")
        keyboard.append([
            InlineKeyboardButton(f"{enabled} {name} (مصدر: {source_name})", callback_data=f"bl_link_detail:{link_id}"),
        ])

    keyboard.append([InlineKeyboardButton("➕ إضافة ربط جديد", callback_data="bl_add_start")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="bl_menu")])
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_LIST_LINKS


async def link_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    link_id = query.data.split(":", 1)[1]
    from ...agent.blogger_db import get_blogger_link, get_blogger_articles

    link = get_blogger_link(link_id)
    if not link:
        await _edit_or_send(update, context, "❌ الرابط غير موجود", InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="bl_list_links")],
        ]))
        return BL_LIST_LINKS

    articles = get_blogger_articles(link_id=link_id, limit=5)

    ai_mode = link.get("ai_mode", "ai_prompt")
    ai_mode_label = {"ai_prompt": "🤖 AI", "templates": "📄 قوالب", "fallback": "📝 احتياطي"}.get(ai_mode, ai_mode)
    enabled = "مفعّل ✅" if link.get("enabled", True) else "معطّل ⏸️"

    text = (
        f"📝 <b>تفاصيل الربط</b>\n\n"
        f"المصدر: <code>{html.escape(link.get('source_id', ''))}</code>\n"
        f"المدونة: <b>{html.escape(link.get('blog_name', ''))}</b>\n"
        f"الرابط: <code>{html.escape(link.get('blog_url', ''))}</code>\n"
        f"الحالة: {enabled}\n"
        f"الوضع: {ai_mode_label}\n"
        f"اللغة: <code>{link.get('article_language', 'ar')}</code>\n"
        f"عنوان الرابط: <code>{html.escape(link.get('link_title', ''))}</code>\n"
    )

    if articles:
        text += f"\n📊 آخر {len(articles)} مقالات:\n"
        for a in articles[-3:]:
            a_title = html.escape(a.get("article_title", "?")[:40])
            a_url = a.get("blog_post_url", "")
            text += f"  • {a_title}\n"

    keyboard = [
        [InlineKeyboardButton("⏸️/▶️ تفعيل/تعطيل", callback_data=f"bl_toggle:{link_id}")],
        [InlineKeyboardButton("🗑 حذف الربط", callback_data=f"bl_delete:{link_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="bl_list_links")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return BL_LINK_DETAIL


async def toggle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    link_id = query.data.split(":", 1)[1]
    from ...agent.blogger_db import get_blogger_link, save_blogger_link
    link = get_blogger_link(link_id)
    if link:
        link["enabled"] = not link.get("enabled", True)
        save_blogger_link(link)
    return await link_detail(update, context)


async def delete_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    link_id = query.data.split(":", 1)[1]
    from ...agent.blogger_db import delete_blogger_link
    delete_blogger_link(link_id)
    return await list_links(update, context)


# ==================== إلغاء ====================

async def cancel_blogger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    for key in list(context.user_data.keys()):
        if key.startswith("bl_"):
            context.user_data.pop(key, None)
    return await blogger_menu(update, context)


# ==================== Callback لـ bl_show_tokens ====================

async def show_tokens_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _show_token_selection(update, context)


# ==================== بناء ConversationHandler ====================

def get_blogger_conversation_handler() -> ConversationHandler:
    """بناء وتسجيل ConversationHandler لناشر مقالات البلوجر"""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(blogger_menu, pattern=r"^(bl_menu|am_blogger)$"),
        ],
        states={
            BL_MENU: [
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(list_links, pattern=r"^bl_list_links$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^am_menu$"),
            ],
            BL_CHOOSE_SOURCE: [
                CallbackQueryHandler(choose_source, pattern=r"^bl_src:"),
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_CHOOSE_TOKEN: [
                CallbackQueryHandler(choose_token, pattern=r"^bl_token:"),
                CallbackQueryHandler(show_tokens_again, pattern=r"^bl_show_tokens$"),
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_CHOOSE_BLOG: [
                CallbackQueryHandler(choose_blog, pattern=r"^bl_blog:"),
                CallbackQueryHandler(show_tokens_again, pattern=r"^bl_show_tokens$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_SETTINGS: [
                CallbackQueryHandler(set_link_title_start, pattern=r"^bl_set_link_title$"),
                CallbackQueryHandler(set_ai_mode, pattern=r"^bl_set_ai_mode$"),
                CallbackQueryHandler(set_language, pattern=r"^bl_set_language$"),
                CallbackQueryHandler(set_ai_prompt_start, pattern=r"^bl_set_ai_prompt$"),
                CallbackQueryHandler(manage_templates, pattern=r"^bl_manage_templates$"),
                CallbackQueryHandler(toggle_templates_order, pattern=r"^bl_toggle_order$"),
                CallbackQueryHandler(confirm_save, pattern=r"^bl_confirm_save$"),
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_SET_LINK_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link_title),
                CallbackQueryHandler(lambda u, c: _show_settings_menu(u, c), pattern=r"^bl_settings$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_SET_AI_MODE: [
                CallbackQueryHandler(choose_ai_mode, pattern=r"^bl_mode:"),
                CallbackQueryHandler(lambda u, c: _show_settings_menu(u, c), pattern=r"^bl_settings$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_SET_AI_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ai_prompt),
                CallbackQueryHandler(lambda u, c: _show_settings_menu(u, c), pattern=r"^bl_settings$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_SET_LANGUAGE: [
                CallbackQueryHandler(choose_language, pattern=r"^bl_lang:"),
                CallbackQueryHandler(lambda u, c: _show_settings_menu(u, c), pattern=r"^bl_settings$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_ADD_TEMPLATE_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_template_title),
                CallbackQueryHandler(manage_templates, pattern=r"^bl_manage_templates$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_ADD_TEMPLATE_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_template_content),
                CallbackQueryHandler(skip_template_content, pattern=r"^bl_skip_template_content$"),
                CallbackQueryHandler(manage_templates, pattern=r"^bl_manage_templates$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_CONFIRM: [
                CallbackQueryHandler(do_save, pattern=r"^bl_do_save$"),
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_LIST_LINKS: [
                CallbackQueryHandler(link_detail, pattern=r"^bl_link_detail:"),
                CallbackQueryHandler(add_link_start, pattern=r"^bl_add_start$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
            BL_LINK_DETAIL: [
                CallbackQueryHandler(toggle_link, pattern=r"^bl_toggle:"),
                CallbackQueryHandler(delete_link, pattern=r"^bl_delete:"),
                CallbackQueryHandler(list_links, pattern=r"^bl_list_links$"),
                CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_blogger, pattern=r"^bl_menu$"),
            CallbackQueryHandler(cancel_blogger, pattern=r"^am_menu$"),
        ],
        allow_reentry=True,
        per_message=False,
    )
