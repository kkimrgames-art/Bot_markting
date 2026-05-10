"""
معالج إدارة الذكاء الاصطناعي للبوت (Telegram)
إدارة مفاتيح وخدمات الذكاء الاصطناعي المعتمدة (Groq, OpenRouter, Clarifai, Mistral)
"""
import os
import json
import asyncio
import logging
import requests
import html
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler

from ...agent.config import load_config
from ..persistence import load_state, save_state

logger = logging.getLogger(__name__)

try:
    from ...agent.supabase_storage import save_bot_state as _force_supabase_save_state
    from ...agent.supabase_client import USE_SUPABASE, is_online as _supabase_is_online
except Exception:
    _force_supabase_save_state = None
    USE_SUPABASE = False


# ==================== حالات محادثة إدارة الذكاء الاصطناعي ====================
AI_MENU, AI_SELECT_PROVIDER, AI_ADD_KEY, AI_TEST_KEY, AI_REMOVE_KEY = range(5)

@dataclass
class AIProvider:
    """معلومات مزود الذكاء الاصطناعي"""
    name: str
    models: List[str]
    api_url: str
    headers_template: Dict[str, str]
    test_payload: Dict[str, Any]
    key_env_var: str

# تعريف مزودي الذكاء الاصطناعي المدعومين
AI_PROVIDERS = {
    "groq": AIProvider(
        name="Groq",
        models=["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "gemma2-9b-it"],
        api_url="https://api.groq.com/openai/v1/chat/completions",
        headers_template={"Authorization": "Bearer {key}", "Content-Type": "application/json"},
        test_payload={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": "Test"}], "max_tokens": 5},
        key_env_var="GROQ_API_KEYS"
    ),
    "openrouter": AIProvider(
        name="OpenRouter",
        models=["meta-llama/llama-3.1-8b-instruct:free", "mistralai/mistral-7b-instruct:free"],
        api_url="https://openrouter.ai/api/v1/chat/completions",
        headers_template={"Authorization": "Bearer {key}", "Content-Type": "application/json", "HTTP-Referer": "https://github.com/OpenRouterTeam/openrouter-python"},
        test_payload={"model": "meta-llama/llama-3.1-8b-instruct:free", "messages": [{"role": "user", "content": "Test"}], "max_tokens": 5},
        key_env_var="OPENROUTER_API_KEYS"
    ),
    "mistral": AIProvider(
        name="Mistral",
        models=["mistral-tiny", "mistral-small"],
        api_url="https://api.mistral.ai/v1/chat/completions",
        headers_template={"Authorization": "Bearer {key}", "Content-Type": "application/json"},
        test_payload={"model": "mistral-tiny", "messages": [{"role": "user", "content": "Test"}], "max_tokens": 5},
        key_env_var="MISTRAL_API_KEY"
    ),
    "clarifai": AIProvider(
        name="Clarifai",
        models=["openai/GPT-3-5-Turbo"],
        api_url="https://api.clarifai.com/v2/models/{model}/outputs",
        headers_template={"Authorization": "Key {key}", "Content-Type": "application/json"},
        test_payload={"inputs": [{"data": {"text": {"raw": "Test"}}}]},
        key_env_var="CLARIFAI_API_KEY"
    )
}

# ==================== وظائف المساعدة ====================

def _load_ai_state():
    cfg = load_config()
    state = load_state(cfg)
    if "ai_manager" not in state:
        state["ai_manager"] = {}
    
    # ضمان وجود مدخل لكل مزود
    for pid in AI_PROVIDERS:
        if pid not in state["ai_manager"]:
            state["ai_manager"][pid] = {"keys": [], "active_keys": [], "blocked_keys": [], "stats": {}}
    
    return state, cfg


def _dedupe_keys(keys: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for key in keys or []:
        cleaned = str(key or "").strip()
        if not cleaned or cleaned in seen:
            continue
        out.append(cleaned)
        seen.add(cleaned)
    return out


def _sync_provider_runtime_state(provider_id: str, keys: List[str]) -> None:
    keys = _dedupe_keys(keys)

    def _default_key_state(existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        existing = dict(existing or {})
        return {
            "is_blocked": bool(existing.get("is_blocked", False)),
            "block_until": existing.get("block_until"),
            "consecutive_errors": int(existing.get("consecutive_errors", 0) or 0),
            "last_check": existing.get("last_check"),
            "usage_limit_reached": bool(existing.get("usage_limit_reached", False)),
        }

    if provider_id == "openrouter":
        from ...agent.openrouter_manager import get_openrouter_manager
        manager = get_openrouter_manager()
        existing = manager.state.get("keys", {}) if isinstance(manager.state.get("keys"), dict) else {}
        manager.api_keys = list(keys)
        manager.state["keys"] = {key: _default_key_state(existing.get(key)) for key in keys}
        manager._save_state()
        return

    if provider_id == "groq":
        from ...agent.groq_manager import get_groq_manager
        manager = get_groq_manager()
        existing = manager.state.get("keys", {}) if isinstance(manager.state.get("keys"), dict) else {}
        manager.api_keys = list(keys)
        manager.state["keys"] = {key: _default_key_state(existing.get(key)) for key in keys}
        manager._save_state()
        return

    if provider_id == "clarifai":
        from ...agent.clarifai_manager import get_clarifai_manager
        manager = get_clarifai_manager()
        existing = manager.state.get("keys", {}) if isinstance(manager.state.get("keys"), dict) else {}
        manager.api_keys = list(keys)
        manager.state["keys"] = {key: _default_key_state(existing.get(key)) for key in keys}
        manager._save_state()
        return

    if provider_id == "mistral":
        from ...agent.supabase_storage import save_api_keys
        save_api_keys("mistral", {"keys": {key: {"active": True} for key in keys}})
        return


def _persist_ai_state_with_status(state: Dict[str, Any], cfg) -> str:
    """
    احفظ الحالة محلياً دائماً، ثم حاول فرض حفظ فوري إلى Supabase
    لتفادي ضياع مفاتيح AI عند إعادة التشغيل قبل دورة المزامنة التالية.
    """
    save_state(state, cfg)

    if not (USE_SUPABASE and _force_supabase_save_state):
        return "local"

    try:
        if _supabase_is_online() and _force_supabase_save_state(state):
            primary = (os.environ.get("SUPABASE_PRIMARY_STORAGE") or "").strip().lower() in {"1", "true", "yes", "on"}
            return "database_only" if primary else "database_and_local"
    except Exception as e:
        logger.warning(f"Immediate Supabase save for AI keys failed: {e}")

    return "local"

async def _test_api_key(provider_id: str, key: str) -> Dict[str, Any]:
    """اختبار مفتاح API"""
    provider = AI_PROVIDERS[provider_id]
    headers = {k: v.format(key=key) for k, v in provider.headers_template.items()}
    url = provider.api_url
    
    if provider_id == "clarifai":
        url = url.format(model="GPT-3-5-Turbo")
        
    payload = provider.test_payload
    start_time = asyncio.get_event_loop().time()
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, headers=headers, timeout=15))
        end_time = asyncio.get_event_loop().time()
        
        status = response.status_code
        if status < 400:
            return {"success": True, "response_time": end_time - start_time, "status_code": status}
        else:
            try:
                err = response.json().get("error", {}).get("message", response.text)
            except:
                err = response.text
            return {"success": False, "error": err, "status_code": status}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==================== معالجات Telegram ====================

async def show_ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة إدارة الذكاء الاصطناعي"""
    query = update.callback_query
    if query:
        await query.answer()
    
    text = (
        "🤖 <b>إدارة أنظمة الذكاء الاصطناعي</b>\n\n"
        "هذا النظام مسؤول عن توليد العناوين والوصف تلقائياً.\n"
        "يمكنك إضافة عدة مفاتيح لكل مزود لتجنب حدود الاستخدام."
    )
    
    keyboard = [
        [InlineKeyboardButton("🔑 مفاتيح API", callback_data="ai_keys_menu")],
        [InlineKeyboardButton("📊 إحصائيات الاستخدام", callback_data="ai_stats")],
        [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="am_menu")]
    ]
    
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    return AI_MENU

async def show_ai_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة اختيار المزود لإدارة المفاتيح"""
    query = update.callback_query
    await query.answer()
    
    text = "🔑 <b>إدارة المفاتيح</b>\n\nاختر المزود الذي تريد إدارة مفاتيحه:"
    
    keyboard = []
    for pid, provider in AI_PROVIDERS.items():
        keyboard.append([InlineKeyboardButton(f"🔹 {provider.name}", callback_data=f"ai_prov:{pid}")])
    
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="ai_main_menu")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_SELECT_PROVIDER

async def handle_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض خيارات المزود المختار"""
    query = update.callback_query
    await query.answer()
    
    provider_id = query.data.split(":")[1]
    context.user_data["selected_ai_provider"] = provider_id
    provider = AI_PROVIDERS[provider_id]
    
    state, _ = _load_ai_state()
    p_state = state["ai_manager"].get(provider_id, {})
    keys = p_state.get("keys", [])
    
    text = (
        f"⚙️ <b>إدارة {html.escape(provider.name)}</b>\n\n"
        f"• عدد المفاتيح المسجلة: {len(keys)}\n"
        f"• المفاتيح النشطة: {len(p_state.get('active_keys', []))}\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("➕ إضافة مفتاح", callback_data="ai_add_key")],
        [InlineKeyboardButton("🗑️ حذف مفتاح", callback_data="ai_remove_key_list")],
        [InlineKeyboardButton("🧪 اختبار الربط", callback_data="ai_test_provider")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="ai_keys_menu")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_SELECT_PROVIDER

async def add_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء عملية إضافة مفتاح"""
    query = update.callback_query
    await query.answer()
    
    provider_id = context.user_data.get("selected_ai_provider")
    name = AI_PROVIDERS[provider_id].name
    
    await query.edit_message_text(f"📝 أرسل مفتاح API الخاص بـ <b>{html.escape(name)}</b> الآن:\n\nيمكنك إرسال عدة مفاتيح دفعة واحدة بوضع كل مفتاح في سطر.", parse_mode='HTML')
    return AI_ADD_KEY

async def receive_ai_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال وحفظ المفتاح"""
    keys_text = update.message.text.strip()
    new_keys = [k.strip() for k in keys_text.split("\n") if k.strip()]
    
    provider_id = context.user_data.get("selected_ai_provider")
    state, cfg = _load_ai_state()
    
    p_state = state["ai_manager"][provider_id]
    added = 0
    duplicate = 0
    
    for k in new_keys:
        if k not in p_state["keys"]:
            p_state["keys"].append(k)
            if k not in p_state["active_keys"]:
                p_state["active_keys"].append(k)
            added += 1
        else:
            duplicate += 1
            
    save_target = _persist_ai_state_with_status(state, cfg)
    try:
        _sync_provider_runtime_state(provider_id, p_state["keys"])
    except Exception as e:
        logger.warning(f"AI provider state sync failed for {provider_id}: {e}")
    
    msg = f"✅ تم حفظ {added} مفتاح جديد لمزود {AI_PROVIDERS[provider_id].name}."
    if duplicate > 0:
        msg += f"\n⚠️ تم تجاهل {duplicate} مفتاح موجود مسبقاً."
    if save_target == "database_only":
        msg += "\n💾 تم حفظ المفاتيح بنجاح في قاعدة البيانات."
    elif save_target == "database_and_local":
        msg += "\n💾 تم حفظ المفاتيح بنجاح في قاعدة البيانات وتم تحديث النسخة المحلية."
    else:
        msg += "\n💾 تم حفظ المفاتيح محلياً. تعذر تأكيد الحفظ في قاعدة البيانات حالياً."
        
    await update.message.reply_text(msg)
    
    return await show_ai_menu(update, context)

async def test_provider_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختبار المفاتيح الحالية للمزود"""
    query = update.callback_query
    await query.answer()
    
    provider_id = context.user_data.get("selected_ai_provider")
    state, _ = _load_ai_state()
    keys = state["ai_manager"][provider_id].get("active_keys", [])
    
    if not keys:
        await query.message.reply_text("❌ لا توجد مفاتيح نشطة لاختبارها.")
        return AI_SELECT_PROVIDER
        
    status_msg = await query.message.reply_text(f"⏳ جاري اختبار {len(keys)} مفتاح لـ {AI_PROVIDERS[provider_id].name}...")
    
    results = []
    for k in keys:
        res = await _test_api_key(provider_id, k)
        results.append(res)
        
    success = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    
    text = (
        f"🧪 <b>نتائج اختبار {html.escape(AI_PROVIDERS[provider_id].name)}</b>\n\n"
        f"✅ مفاتيح ناجحة: {len(success)}\n"
        f"❌ مفاتيح فاشلة: {len(failed)}\n\n"
    )
    
    if failed:
        text += "⚠️ بعض الأخطاء المكتشفة:\n"
        for i, f in enumerate(failed[:3], 1):
            text += f"{i}. <code>{html.escape(str(f.get('error'))[:100])}</code>\n"
            
    await status_msg.edit_text(text, parse_mode='HTML')
    return AI_SELECT_PROVIDER

async def list_keys_for_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة المفاتيح لحذفها"""
    query = update.callback_query
    await query.answer()
    
    provider_id = context.user_data.get("selected_ai_provider")
    state, _ = _load_ai_state()
    keys = state["ai_manager"][provider_id].get("keys", [])
    
    if not keys:
        await query.edit_message_text("❌ لا توجد مفاتيح مسجلة.")
        return AI_SELECT_PROVIDER
        
    text = f"🗑️ <b>حذف مفتاح {html.escape(AI_PROVIDERS[provider_id].name)}</b>\n\nاختر المفتاح المراد حذفه:"
    keyboard = []
    for i, k in enumerate(keys):
        # إظهار آخر 8 أحرف فقط للأمان
        masked = f"...{k[-8:]}" if len(k) > 10 else k
        keyboard.append([InlineKeyboardButton(f"🗑️ {masked}", callback_data=f"ai_rem:{i}")])
        
    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"ai_prov:{provider_id}")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_REMOVE_KEY

async def handle_key_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ عملية الحذف"""
    query = update.callback_query
    await query.answer()
    
    idx = int(query.data.split(":")[1])
    provider_id = context.user_data.get("selected_ai_provider")
    state, cfg = _load_ai_state()
    
    keys = state["ai_manager"][provider_id]["keys"]
    if 0 <= idx < len(keys):
        removed_key = keys.pop(idx)
        # إزالته من القوائم الأخرى أيضاً
        if removed_key in state["ai_manager"][provider_id].get("active_keys", []):
            state["ai_manager"][provider_id]["active_keys"].remove(removed_key)
        if removed_key in state["ai_manager"][provider_id].get("blocked_keys", []):
            state["ai_manager"][provider_id]["blocked_keys"].remove(removed_key)
            
        save_target = _persist_ai_state_with_status(state, cfg)
        try:
            _sync_provider_runtime_state(provider_id, state["ai_manager"][provider_id].get("keys", []))
        except Exception as e:
            logger.warning(f"AI provider state sync failed for {provider_id}: {e}")
        status_suffix = (
            "\n💾 تم تحديث قاعدة البيانات بنجاح."
            if save_target in {"database_only", "database_and_local"}
            else "\n💾 تم تحديث الحفظ المحلي فقط حالياً."
        )
        await query.message.reply_text(f"✅ تم حذف المفتاح بنجاح.{status_suffix}")
        
    return await list_keys_for_removal(update, context)

async def show_ai_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض إحصائيات الذكاء الاصطناعي"""
    query = update.callback_query
    await query.answer()
    
    state, _ = _load_ai_state()
    ai_state = state["ai_manager"]
    
    text = "📊 <b>إحصائيات أنظمة الذكاء الاصطناعي</b>\n\n"
    for pid, provider in AI_PROVIDERS.items():
        pst = ai_state.get(pid, {})
        text += (
            f"🔹 <b>{html.escape(provider.name)}:</b>\n"
            f"  • المفاتيح: {len(pst.get('keys', []))} (نشط: {len(pst.get('active_keys', []))})\n"
            f"  • الطلبات: {pst.get('stats', {}).get('total_requests', 0)}\n\n"
        )
        
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="ai_main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MENU

async def exit_ai_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الخروج إلى القائمة الرئيسية للأتمتة"""
    query = update.callback_query
    if query:
        await query.answer()
    
    # استيراد محلي لتجنب التعارض (Circular Import)
    from .auto_mod_handlers import auto_mod_menu
    await auto_mod_menu(update, context)
    return ConversationHandler.END

def get_ai_manager_conv():
    """الحصول على ConversationHandler الخاص بإدارة الذكاء الاصطناعي"""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(show_ai_menu, pattern="^ai_main_menu$"),
            CallbackQueryHandler(show_ai_keys_menu, pattern="^ai_keys_menu$"),
            CallbackQueryHandler(show_ai_stats, pattern="^ai_stats$"),
            # دخول من القائمة الرئيسية عبر callback (settings -> ai_management)
            CallbackQueryHandler(show_ai_menu, pattern="^ai_management$"),
        ],
        states={
            AI_MENU: [
                CallbackQueryHandler(show_ai_keys_menu, pattern="^ai_keys_menu$"),
                CallbackQueryHandler(show_ai_stats, pattern="^ai_stats$"),
            ],
            AI_SELECT_PROVIDER: [
                CallbackQueryHandler(handle_provider_selection, pattern="^ai_prov:"),
                CallbackQueryHandler(add_key_start, pattern="^ai_add_key$"),
                CallbackQueryHandler(test_provider_connection, pattern="^ai_test_provider$"),
                CallbackQueryHandler(list_keys_for_removal, pattern="^ai_remove_key_list$"),
                CallbackQueryHandler(show_ai_keys_menu, pattern="^ai_keys_menu$"),
            ],
            AI_ADD_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ai_key),
                CallbackQueryHandler(handle_provider_selection, pattern="^ai_prov:"),
            ],
            AI_REMOVE_KEY: [
                CallbackQueryHandler(handle_key_removal, pattern="^ai_rem:"),
                CallbackQueryHandler(handle_provider_selection, pattern="^ai_prov:"),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(show_ai_menu, pattern="^ai_main_menu$"),
            CallbackQueryHandler(exit_ai_manager, pattern="^am_menu$")
        ],
        allow_reentry=True,
        name="ai_manager_conv",
        per_message=False
    )
