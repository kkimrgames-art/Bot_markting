#!/usr/bin/env python3
"""
معالجات رفع الفيديوهات السحابي + اختصار الروابط
Cloud Upload & Link Shortener Bot Handlers
"""
import os
import html
import json
import asyncio
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# ==================== حالات المحادثة ====================
(
    CU_MENU,
    CU_CHOOSE_SOURCE,
    CU_CHOOSE_SERVICE,
    CU_CHOOSE_TOKEN,
    CU_CHOOSE_DRIVE_FOLDER,
    CU_CHOOSE_BUCKET,
    CU_ENTER_CF_ENDPOINT,
    CU_ENTER_CF_ACCESS_KEY,
    CU_ENTER_CF_SECRET_KEY,
    CU_ENTER_CF_BUCKET,
    CU_ENTER_CF_REGION,
    CU_SHORTEN_LINK,
    CU_LINK_TO_BLOGGER,
    CU_BLOGGER_POSITION,
    CU_CONFIRM,
    CU_LIST_CONFIGS,
    CU_CONFIG_DETAIL,
) = range(17)

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
    """جلب قائمة المصادر من Supabase مع دمج البيانات المحلية"""
    try:
        from ...agent.supabase_client import supabase_select
        from ...agent.auto_mod_fetcher import _local_select_rows, get_instance_id

        instance_id = get_instance_id()
        filters = {"instance_id": instance_id}

        # جلب من Supabase مع فلتر instance_id
        supabase_sources = supabase_select("auto_mod_sources", filters) or []

        # دمج مع البيانات المحلية لضمان ظهور المصادر الجديدة
        local_sources = _local_select_rows("auto_mod_sources", filters) or []

        # دمج القائمتين مع تجنب التكرار
        seen_ids = {src.get("id") for src in supabase_sources if src.get("id")}
        merged = list(supabase_sources)
        for src in local_sources:
            src_id = src.get("id")
            if src_id and src_id not in seen_ids:
                merged.append(src)

        return merged
    except Exception:
        return []


def _get_token_channels() -> List[Dict]:
    """جلب قنوات مع توكن OAuth"""
    result = []
    seen_paths = set()
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

        if os.path.exists(token_dir):
            for f in os.listdir(token_dir):
                if not f.endswith(".json"):
                    continue
                fp = os.path.join(token_dir, f)
                ch_id_from_file = f.replace(".json", "")
                matched_cfg = yt_id_to_config.get(ch_id_from_file)
                if matched_cfg:
                    ch_name = matched_cfg.get("channel_name") or ch_id_from_file
                else:
                    ch_name = ch_id_from_file
                if fp not in seen_paths:
                    seen_paths.add(fp)
                    result.append({
                        "channel_id": ch_id_from_file,
                        "channel_name": ch_name,
                        "token_path": fp,
                    })
    except Exception as e:
        logger.debug(f"Failed to get token channels: {e}")
    return result


SERVICE_LABELS = {
    "google_drive": "\U0001f5fd\ufe0f Google Drive",
    "supabase": "\u2699\ufe0f Supabase Storage",
    "claudeflare": "\u2601\ufe0f Claudeflare (S3)",
}

# ==================== القائمة الرئيسية ====================

async def cloud_upload_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """قائمة رفع الفيديوهات السحابي"""
    query = update.callback_query
    await _safe_answer(query)

    text = (
        "\U0001f4e6 <b>رفع الفيديوهات السحابي</b>\n\n"
        "هذه الميزة ترفع نسخة من كل فيديو قبل نشره على YouTube\n"
        "إلى خدمة تخزين سحابي وتحصل على رابط تحميل دائم.\n\n"
        "\U0001f517 اختياري: اختصار الرابط تلقائياً\n"
        "\U0001f4dd اختياري: إضافة الرابط في مقالات البلوجر"
    )
    keyboard = [
        [InlineKeyboardButton("\u2795 إعداد رفع جديد", callback_data="cu_add_start")],
        [InlineKeyboardButton("\U0001f4cb القائمة الحالية", callback_data="cu_list_configs")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="am_menu")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_MENU


# ==================== إعداد جديد ====================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE, *, page: int = 0) -> int:
    """بدء إعداد رفع سحابي جديد مع ترقيم الصفحات"""
    query = update.callback_query
    if query:
        await _safe_answer(query)

    sources = _get_sources_list()
    if not sources:
        await _edit_or_send(update, context, "\u274c لا توجد مصادر. أضف مصادر أولاً من قائمة الأتمتة.", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_menu")],
        ]))
        return CU_MENU

    context.user_data["cu_data"] = {}
    per_page = 8
    total_pages = max(1, (len(sources) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    current = sources[start:start + per_page]

    context.user_data["cu_sources_list"] = sources
    context.user_data["cu_sources_page"] = page

    keyboard = []
    for src in current:
        sid = src.get("id", "")
        name = html.escape(str(src.get("name") or src.get("source_name") or sid)[:40])
        keyboard.append([InlineKeyboardButton(f"\U0001f3ac {name}", callback_data=f"cu_src:{sid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"cu_src_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"cu_src_page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_menu")])

    text = f"\U0001f3ac <b>اختر المصدر</b>\n\nالمصدر الذي تريد رفع فيديوهاته سحابياً:\nالصفحة: {page + 1}/{total_pages}"
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_CHOOSE_SOURCE


async def choose_source_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الانتقال بين صفحات قائمة المصادر"""
    query = update.callback_query
    await _safe_answer(query)
    page = int(query.data.split(":", 1)[1])
    return await add_start(update, context, page=page)


async def choose_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    source_id = query.data.split(":", 1)[1]
    context.user_data["cu_data"]["source_id"] = source_id

    text = "\U0001f680 <b>اختر خدمة التخزين السحابي</b>\n\n"
    keyboard = [
        [InlineKeyboardButton(SERVICE_LABELS["google_drive"], callback_data="cu_svc:google_drive")],
        [InlineKeyboardButton(SERVICE_LABELS["supabase"], callback_data="cu_svc:supabase")],
        [InlineKeyboardButton(SERVICE_LABELS["claudeflare"], callback_data="cu_svc:claudeflare")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_CHOOSE_SERVICE


# ==================== Google Drive ====================

async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    service = query.data.split(":", 1)[1]
    cu = context.user_data.setdefault("cu_data", {})
    cu["service"] = service
    cu["service_label"] = SERVICE_LABELS.get(service, service)

    if service == "google_drive":
        return await _show_token_selection(update, context)
    elif service == "supabase":
        return await _show_bucket_selection(update, context)
    elif service == "claudeflare":
        return await _ask_cf_endpoint(update, context)
    return CU_MENU


async def _show_token_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض قائمة حسابات Google المتاحة"""
    tokens = _get_token_channels()
    if not tokens:
        await _edit_or_send(update, context, "\u274c لا توجد حسابات Google مصادق عليها. أضف قناة YouTube أولاً.", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
        ]))
        return CU_MENU

    context.user_data["cu_tokens"] = tokens
    keyboard = []
    for idx, t in enumerate(tokens[:10]):
        ch_name = html.escape(str(t.get("channel_name") or t.get("channel_id", ""))[:35])
        keyboard.append([InlineKeyboardButton(f"\U0001f510 {ch_name}", callback_data=f"cu_token:{idx}")])
    keyboard.append([InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")])
    await _edit_or_send(update, context, "\U0001f510 <b>اختر حساب Google Drive</b>\n\n(نفس حسابات YouTube المصادق عليها):", InlineKeyboardMarkup(keyboard))
    return CU_CHOOSE_TOKEN


async def choose_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    idx = int(query.data.split(":", 1)[1])
    tokens = context.user_data.get("cu_tokens", [])
    token_path = tokens[idx].get("token_path", "") if 0 <= idx < len(tokens) else ""
    cu = context.user_data.setdefault("cu_data", {})
    cu["token_path"] = token_path

    # جلب المجلدات
    await query.edit_message_text("\u23f3 جاري جلب المجلدات...")
    try:
        from ...agent.cloud_uploader import list_drive_folders
        folders = list_drive_folders(token_path)
    except ValueError as e:
        await _edit_or_send(update, context, f"\u274c <b>خطأ في صلاحيات Google Drive</b>\n\n{e}", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_show_tokens")],
        ]))
        return CU_CHOOSE_TOKEN
    except Exception as e:
        logger.warning(f"Failed to list Drive folders: {e}")
        folders = []

    keyboard = [[InlineKeyboardButton("\U0001f4c1 الجذر (بدون مجلد)", callback_data="cu_folder:")]]
    for f in folders[:15]:
        fid = f.get("id", "")
        fname = html.escape(str(f.get("name", ""))[:35])
        keyboard.append([InlineKeyboardButton(f"\U0001f4c2 {fname}", callback_data=f"cu_folder:{fid}")])
    keyboard.append([InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_show_tokens")])
    await query.edit_message_text("\U0001f4c2 <b>اختر المجلد في Drive</b>", reply_markup=InlineKeyboardMarkup(keyboard))
    return CU_CHOOSE_DRIVE_FOLDER


async def show_tokens_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _show_token_selection(update, context)


async def choose_drive_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    folder_id = query.data.split(":", 1)[1]
    context.user_data["cu_data"]["drive_folder_id"] = folder_id
    return await _ask_shorten_link(update, context)


# ==================== Supabase ====================

async def _show_bucket_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض حاويات Supabase"""
    await (update.callback_query or update).edit_message_text("\u23f3 جاري جلب الحاويات...")
    try:
        from ...agent.cloud_uploader import list_supabase_buckets
        buckets = list_supabase_buckets()
    except Exception as e:
        logger.warning(f"Failed to list buckets: {e}")
        buckets = []

    if not buckets:
        await _edit_or_send(update, context, "\u274c لا توجد حاويات في Supabase Storage.\nأنشئ حاوية من Supabase Dashboard أولاً.", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
        ]))
        return CU_MENU

    keyboard = []
    for b in buckets[:15]:
        bname = html.escape(str(b.get("name", ""))[:30])
        pub = "\u2705 عام" if b.get("public") else "\U0001f512 خاص"
        keyboard.append([InlineKeyboardButton(f"\U0001f4e6 {bname} ({pub})", callback_data=f"cu_bucket:{bname}")])
    keyboard.append([InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")])
    await _edit_or_send(update, context, "\U0001f4e6 <b>اختر حاوية Supabase</b>", InlineKeyboardMarkup(keyboard))
    return CU_CHOOSE_BUCKET


async def choose_bucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    bucket_name = query.data.split(":", 1)[1]
    context.user_data["cu_data"]["bucket_name"] = bucket_name
    return await _ask_shorten_link(update, context)


# ==================== Claudeflare ====================

async def _ask_cf_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = "\u2601\ufe0f <b>إعدادات Claudeflare (S3)</b>\n\n\U0001f310 أدخل Endpoint URL:\n<code>مثال: https://xxx.r2.cloudflarestorage.com</code>"
    keyboard = [[InlineKeyboardButton("\U0001f519 إلغاء", callback_data="cu_add_start")]]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_ENTER_CF_ENDPOINT


async def receive_cf_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cu_data"]["claudflare_endpoint"] = update.message.text.strip()
    await update.message.reply_text("\U0001f511 أدخل Access Key:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f519 إلغاء", callback_data="cu_add_start")],
    ]))
    return CU_ENTER_CF_ACCESS_KEY


async def receive_cf_access_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cu_data"]["claudflare_access_key"] = update.message.text.strip()
    await update.message.reply_text("\U0001f512 أدخل Secret Key:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f519 إلغاء", callback_data="cu_add_start")],
    ]))
    return CU_ENTER_CF_SECRET_KEY


async def receive_cf_secret_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cu_data"]["claudflare_secret_key"] = update.message.text.strip()
    await update.message.reply_text("\U0001f4e6 أدخل اسم الحاوية (Bucket):", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f519 إلغاء", callback_data="cu_add_start")],
    ]))
    return CU_ENTER_CF_BUCKET


async def receive_cf_bucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cu_data"]["claudflare_bucket"] = update.message.text.strip()
    text = "\U0001f5fa\ufe0f أدخل Region (اترك فارغاً لـ <code>auto</code>):"
    keyboard = [[InlineKeyboardButton("\u2328\ufe0f تخطي (auto)", callback_data="cu_cf_region:auto")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CU_ENTER_CF_REGION


async def choose_cf_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    region = query.data.split(":", 1)[1]
    context.user_data["cu_data"]["claudflare_region"] = region
    return await _ask_shorten_link(update, context)


# ==================== اختصار الرابط ====================

async def _ask_shorten_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cu = context.user_data.get("cu_data", {})
    service_label = cu.get("service_label", "")
    text = f"\U0001f517 <b>اختصار الروابط التلقائي</b>\n\nهل تريد اختصار رابط التحميل تلقائياً عبر cuty.io؟\n\nالخدمة: {service_label}"
    keyboard = [
        [InlineKeyboardButton("\u2705 نعم، فعّل اختصار الروابط", callback_data="cu_shorten:yes")],
        [InlineKeyboardButton("\u274c لا، بدون اختصار", callback_data="cu_shorten:no")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_SHORTEN_LINK


async def choose_shorten(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]
    cu = context.user_data.setdefault("cu_data", {})
    cu["shorten_link"] = (choice == "yes")

    if cu["shorten_link"]:
        return await _ask_link_to_blogger(update, context)
    else:
        # تخطي سؤال البلوجر أيضاً
        cu["link_to_blogger"] = False
        cu["blogger_link_position"] = "bottom"
        return await _show_confirm(update, context)


# ==================== ربط مع البلوجر ====================

async def _ask_link_to_blogger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (
        "\U0001f4dd <b>ربط مع ناشر مقالات البلوجر</b>\n\n"
        "هل تريد إضافة رابط التحميل المختصر تلقائياً\n"
        "في المقالات المنشورة على البلوجر؟"
    )
    keyboard = [
        [InlineKeyboardButton("\u2705 نعم، أضف الرابط في المقالات", callback_data="cu_blogger_link:yes")],
        [InlineKeyboardButton("\u274c لا", callback_data="cu_blogger_link:no")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_LINK_TO_BLOGGER


async def choose_link_to_blogger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    choice = query.data.split(":", 1)[1]
    cu = context.user_data.setdefault("cu_data", {})
    cu["link_to_blogger"] = (choice == "yes")

    if cu["link_to_blogger"]:
        return await _ask_blogger_position(update, context)
    else:
        cu["blogger_link_position"] = "bottom"
        return await _show_confirm(update, context)


async def _ask_blogger_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (
        "\U0001f4cd <b>موضع الرابط في المقال</b>\n\n"
        "أين تريد وضع رابط التحميل في المقال؟"
    )
    keyboard = [
        [InlineKeyboardButton("\u2b06\ufe0f أعلى المقال (بعد العنوان مباشرة)", callback_data="cu_bpos:top")],
        [InlineKeyboardButton("\U0001f4cf منتصف المقال", callback_data="cu_bpos:middle")],
        [InlineKeyboardButton("\u2b07\ufe0f أسفل المقال", callback_data="cu_bpos:bottom")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_add_start")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_BLOGGER_POSITION


async def choose_blogger_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    position = query.data.split(":", 1)[1]
    context.user_data["cu_data"]["blogger_link_position"] = position
    return await _show_confirm(update, context)


# ==================== التأكيد ====================

async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cu = context.user_data.get("cu_data", {})
    service = cu.get("service", "")
    svc_label = cu.get("service_label", service)

    lines = [
        "\u2705 <b>تأكيد الإعداد</b>\n",
        f"\U0001f3ac المصدر: <code>{html.escape(str(cu.get('source_id', '')))}</code>",
        f"\U0001f680 الخدمة: {svc_label}",
    ]

    if service == "google_drive":
        lines.append(f"\U0001f510 الحساب: <code>{html.escape(os.path.basename(cu.get('token_path', '')))}</code>")
        folder = cu.get("drive_folder_id", "")
        if folder:
            lines.append(f"\U0001f4c2 المجلد: <code>{html.escape(folder)}</code>")
        else:
            lines.append("\U0001f4c2 المجلد: الجذر")
    elif service == "supabase":
        lines.append(f"\U0001f4e6 الحاوية: <code>{html.escape(cu.get('bucket_name', ''))}</code>")
    elif service == "claudeflare":
        lines.append(f"\U0001f310 Endpoint: <code>{html.escape(cu.get('claudflare_endpoint', ''))}</code>")
        lines.append(f"\U0001f4e6 الحاوية: <code>{html.escape(cu.get('claudflare_bucket', ''))}</code>")
        lines.append(f"\U0001f5fa\ufe0f Region: <code>{html.escape(cu.get('claudflare_region', 'auto'))}</code>")

    shorten = cu.get("shorten_link", False)
    lines.append(f"\U0001f517 اختصار الروابط: {'\u2705 مفعّل' if shorten else '\u274c معطّل'}")

    blogger = cu.get("link_to_blogger", False)
    lines.append(f"\U0001f4dd ربط مع البلوجر: {'\u2705 نعم' if blogger else '\u274c لا'}")
    if blogger:
        pos = cu.get("blogger_link_position", "bottom")
        pos_label = {"top": "\u2b06\ufe0f أعلى", "middle": "\U0001f4cf منتصف", "bottom": "\u2b07\ufe0f أسفل"}.get(pos, pos)
        lines.append(f"\U0001f4cd موضع الرابط: {pos_label}")

    lines.append("\n\u26a0\ufe0f تأكيد الحفظ؟")

    keyboard = [
        [InlineKeyboardButton("\u2705 حفظ", callback_data="cu_do_save")],
        [InlineKeyboardButton("\U0001f519 إلغاء", callback_data="cu_add_start")],
    ]
    await _edit_or_send(update, context, "\n".join(lines), InlineKeyboardMarkup(keyboard))
    return CU_CONFIRM


async def do_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    cu = context.user_data.get("cu_data", {})
    if not cu.get("source_id") or not cu.get("service"):
        await _edit_or_send(update, context, "\u274c بيانات غير مكتملة", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_menu")],
        ]))
        return CU_MENU

    from ...agent.cloud_upload_db import save_cloud_config
    save_cloud_config(cu)

    await _edit_or_send(update, context, "\u2705 <b>تم الحفظ بنجاح!</b>\n\nالإعداد مفعل الآن وسيتم رفع نسخة من كل فيديو قبل نشره.", InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4cb عرض الإعدادات", callback_data="cu_list_configs")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_menu")],
    ]))
    return CU_MENU


# ==================== عرض الإعدادات ====================

async def list_configs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)

    from ...agent.cloud_upload_db import get_cloud_configs
    configs = get_cloud_configs()

    text = f"\U0001f4cb <b>إعدادات الرفع السحابي</b> ({len(configs)})\n\n"
    keyboard = []
    for cfg in configs:
        cid = cfg.get("id", "")
        svc = cfg.get("service", "")
        svc_label = SERVICE_LABELS.get(svc, svc)
        src = html.escape(str(cfg.get("source_id", ""))[:15])
        enabled = "\u2705" if cfg.get("enabled", True) else "\u23f8\ufe0f"
        shorten = " \U0001f517" if cfg.get("shorten_link") else ""
        blogger = " \U0001f4dd" if cfg.get("link_to_blogger") else ""
        keyboard.append([InlineKeyboardButton(
            f"{enabled} {svc_label} ({src}){shorten}{blogger}",
            callback_data=f"cu_cfg:{cid}",
        )])

    keyboard.append([InlineKeyboardButton("\u2795 إعداد جديد", callback_data="cu_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_menu")])
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_LIST_CONFIGS


async def config_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    config_id = query.data.split(":", 1)[1]

    from ...agent.cloud_upload_db import get_cloud_config, delete_cloud_config
    cfg = get_cloud_config(config_id)
    if not cfg:
        await _edit_or_send(update, context, "\u274c الإعداد غير موجود", InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_list_configs")],
        ]))
        return CU_LIST_CONFIGS

    svc = cfg.get("service", "")
    svc_label = SERVICE_LABELS.get(svc, svc)
    enabled = "\u2705 مفعّل" if cfg.get("enabled", True) else "\u23f8\ufe0f معطّل"
    shorten = "\u2705 مفعّل" if cfg.get("shorten_link") else "\u274c معطّل"
    blogger = "\u2705 نعم" if cfg.get("link_to_blogger") else "\u274c لا"
    pos = cfg.get("blogger_link_position", "bottom")
    pos_label = {"top": "\u2b06\ufe0f أعلى", "middle": "\U0001f4cf منتصف", "bottom": "\u2b07\ufe0f أسفل"}.get(pos, pos)

    text = (
        f"\U0001f4c4 <b>تفاصيل الإعداد</b>\n\n"
        f"المصدر: <code>{html.escape(cfg.get('source_id', ''))}</code>\n"
        f"الخدمة: {svc_label}\n"
        f"الحالة: {enabled}\n"
        f"\U0001f517 اختصار الروابط: {shorten}\n"
        f"\U0001f4dd ربط البلوجر: {blogger}\n"
    )
    if cfg.get("link_to_blogger"):
        text += f"\U0001f4cd موضع الرابط: {pos_label}\n"

    keyboard = [
        [InlineKeyboardButton("\u23f8\ufe0f/\u25b6\ufe0f تفعيل/تعطيل", callback_data=f"cu_toggle:{config_id}")],
        [InlineKeyboardButton("\U0001f5d1\ufe0f حذف", callback_data=f"cu_delete:{config_id}")],
        [InlineKeyboardButton("\U0001f519 رجوع", callback_data="cu_list_configs")],
    ]
    await _edit_or_send(update, context, text, InlineKeyboardMarkup(keyboard))
    return CU_CONFIG_DETAIL


async def toggle_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    config_id = query.data.split(":", 1)[1]
    from ...agent.cloud_upload_db import get_cloud_config, save_cloud_config
    cfg = get_cloud_config(config_id)
    if cfg:
        cfg["enabled"] = not cfg.get("enabled", True)
        save_cloud_config(cfg)
    return await config_detail(update, context)


async def delete_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_answer(query)
    config_id = query.data.split(":", 1)[1]
    from ...agent.cloud_upload_db import delete_cloud_config
    delete_cloud_config(config_id)
    return await list_configs(update, context)


# ==================== إلغاء ====================

async def cancel_cloud(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    for key in list(context.user_data.keys()):
        if key.startswith("cu_"):
            context.user_data.pop(key, None)
    callback_data = query.data if query else ""
    if callback_data in ("am_menu", "am_add_source"):
        return ConversationHandler.END
    return await cloud_upload_menu(update, context)


# ==================== بناء ConversationHandler ====================

def get_cloud_upload_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cloud_upload_menu, pattern=r"^(cu_menu|am_cloud_upload)$"),
        ],
        states={
            CU_MENU: [
                CallbackQueryHandler(add_start, pattern=r"^cu_add_start$"),
                CallbackQueryHandler(list_configs, pattern=r"^cu_list_configs$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^am_menu$"),
            ],
            CU_CHOOSE_SOURCE: [
                CallbackQueryHandler(choose_source, pattern=r"^cu_src:"),
                CallbackQueryHandler(choose_source_page, pattern=r"^cu_src_page:\d+$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_menu$"),
            ],
            CU_CHOOSE_SERVICE: [
                CallbackQueryHandler(choose_service, pattern=r"^cu_svc:"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_CHOOSE_TOKEN: [
                CallbackQueryHandler(choose_token, pattern=r"^cu_token:"),
                CallbackQueryHandler(show_tokens_again, pattern=r"^cu_show_tokens$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_CHOOSE_DRIVE_FOLDER: [
                CallbackQueryHandler(choose_drive_folder, pattern=r"^cu_folder:"),
                CallbackQueryHandler(show_tokens_again, pattern=r"^cu_show_tokens$"),
            ],
            CU_CHOOSE_BUCKET: [
                CallbackQueryHandler(choose_bucket, pattern=r"^cu_bucket:"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_ENTER_CF_ENDPOINT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cf_endpoint),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_ENTER_CF_ACCESS_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cf_access_key),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_ENTER_CF_SECRET_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cf_secret_key),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_ENTER_CF_BUCKET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cf_bucket),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_ENTER_CF_REGION: [
                CallbackQueryHandler(choose_cf_region, pattern=r"^cu_cf_region:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: asyncio.ensure_future(
                    _cf_region_from_text(u, c)
                )),
            ],
            CU_SHORTEN_LINK: [
                CallbackQueryHandler(choose_shorten, pattern=r"^cu_shorten:"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_LINK_TO_BLOGGER: [
                CallbackQueryHandler(choose_link_to_blogger, pattern=r"^cu_blogger_link:"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_BLOGGER_POSITION: [
                CallbackQueryHandler(choose_blogger_position, pattern=r"^cu_bpos:"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_CONFIRM: [
                CallbackQueryHandler(do_save, pattern=r"^cu_do_save$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_add_start$"),
            ],
            CU_LIST_CONFIGS: [
                CallbackQueryHandler(config_detail, pattern=r"^cu_cfg:"),
                CallbackQueryHandler(add_start, pattern=r"^cu_add_start$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_menu$"),
            ],
            CU_CONFIG_DETAIL: [
                CallbackQueryHandler(toggle_config, pattern=r"^cu_toggle:"),
                CallbackQueryHandler(delete_config, pattern=r"^cu_delete:"),
                CallbackQueryHandler(list_configs, pattern=r"^cu_list_configs$"),
                CallbackQueryHandler(cancel_cloud, pattern=r"^cu_menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_cloud, pattern=r"^cu_menu$"),
            CallbackQueryHandler(cancel_cloud, pattern=r"^am_menu$"),
            CallbackQueryHandler(cancel_cloud, pattern=r"^am_add_source$"),
        ],
        allow_reentry=True,
        per_message=False,
    )


async def _cf_region_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """معالجة إدخال region كنص"""
    if update.message and update.message.text:
        context.user_data["cu_data"]["claudflare_region"] = update.message.text.strip()
    return await _ask_shorten_link(update, context)
