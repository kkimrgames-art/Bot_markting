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
AI_MENU, AI_SELECT_PROVIDER, AI_ADD_KEY, AI_TEST_KEY, AI_REMOVE_KEY, AI_MODELS_MENU, AI_ADD_MODEL, AI_REMOVE_MODEL = range(8)

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
    "gemini": AIProvider(
        name="Gemini (Google)",
        models=["gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-1.5-flash-latest"],
        api_url="https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent",
        headers_template={"Content-Type": "application/json"},
        test_payload={"contents": [{"parts": [{"text": "Test"}]}], "generationConfig": {"maxOutputTokens": 5}},
        key_env_var="GEMINI_API_KEYS"
    ),
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
        models=["mistral-large-latest", "mistral-small-latest", "mistral-tiny"],
        api_url="https://api.mistral.ai/v1/chat/completions",
        headers_template={"Authorization": "Bearer {key}", "Content-Type": "application/json"},
        test_payload={"model": "mistral-small-latest", "messages": [{"role": "user", "content": "Test"}], "max_tokens": 5},
        key_env_var="MISTRAL_API_KEY"
    ),
    "clarifai": AIProvider(
        name="Clarifai",
        models=["GPT-4o", "GLM_4_6", "Kimi-K2-Thinking", "MiniMax-M2"],
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

    if provider_id == "gemini":
        try:
            from ...agent.gemini_key_manager import get_key_manager
            manager = get_key_manager()
            existing = manager.state.get("keys", {}) if isinstance(manager.state.get("keys"), dict) else {}
            manager.api_keys = list(keys)
            manager.API_KEYS = list(keys)
            # Gemini keys have richer per-key stats; preserve them where possible.
            new_keys_state = {}
            from datetime import datetime as _dt
            for key in keys:
                if key in existing:
                    new_keys_state[key] = existing[key]
                else:
                    new_keys_state[key] = {
                        "requests_today": 0,
                        "requests_this_minute": 0,
                        "last_request_time": None,
                        "last_reset_day": _dt.now().date().isoformat(),
                        "last_reset_minute": _dt.now().replace(second=0, microsecond=0).isoformat(),
                        "is_blocked": False,
                        "block_until": None,
                        "total_requests": 0,
                        "errors": 0,
                    }
            manager.state["keys"] = new_keys_state
            manager._save_state()
        except Exception as e:
            logger.warning(f"Failed to sync Gemini runtime state: {e}")
        return

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
        "يمكنك إضافة عدة مفاتيح لكل مزود لتجنب حدود الاستخدام.\n\n"
        "<b>الخدمات المدعومة:</b>\n"
        "• Gemini (Google) — مفاتيح مجانية\n"
        "• Groq — نماذج Llama / Gemma\n"
        "• OpenRouter — وصول لكل النماذج\n"
        "• Mistral — نماذج Mistral الرسمية\n"
        "• Clarifai — منصة متعددة النماذج\n"
    )

    keyboard = [
        [InlineKeyboardButton("🔑 مفاتيح API", callback_data="ai_keys_menu")],
        [InlineKeyboardButton("🧠 إدارة النماذج (Models)", callback_data="ai_models_menu")],
        [InlineKeyboardButton("📊 حالة الحصص والمفاتيح", callback_data="ai_quota_status")],
        [InlineKeyboardButton("📈 إحصائيات الاستخدام", callback_data="ai_stats")],
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
    
    text = "📈 <b>إحصائيات أنظمة الذكاء الاصطناعي</b>\n\n"
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


# ==================== حالة الحصص والمفاتيح (Quota Status) ====================

def _get_provider_keys_for_quota(provider_id: str) -> List[str]:
    """Gather all configured keys for a provider (from runtime managers)."""
    try:
        if provider_id == "openrouter":
            from ...agent.openrouter_manager import get_openrouter_manager
            return list(get_openrouter_manager().api_keys or [])
        if provider_id == "groq":
            from ...agent.groq_manager import get_groq_manager
            return list(get_groq_manager().api_keys or [])
        if provider_id == "clarifai":
            from ...agent.clarifai_manager import get_clarifai_manager
            return list(get_clarifai_manager().api_keys or [])
        if provider_id == "gemini":
            from ...agent.gemini_key_manager import get_key_manager
            km = get_key_manager()
            return list(km.api_keys or km.keys or [])
        if provider_id == "mistral":
            from ...agent.ai import _load_mistral_api_keys
            from ...agent.config import load_config
            return _load_mistral_api_keys(load_config())
    except Exception as e:
        logger.warning(f"Failed to load keys for {provider_id}: {e}")
    return []


async def show_ai_quota_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض حالة الحصص والمفاتيح لكل مزود"""
    query = update.callback_query
    await query.answer()

    try:
        from ...agent import ai_quota_tracker
    except Exception as e:
        await query.edit_message_text(f"❌ فشل تحميل وحدة الحصص: {e}")
        return AI_MENU

    # Run cleanup to clear expired entries
    try:
        cleared = ai_quota_tracker.cleanup_expired()
        if cleared:
            logger.info(f"🧹 Quota tracker cleanup cleared {cleared} expired entries")
    except Exception:
        pass

    text = "📊 <b>حالة الحصص والمفاتيح</b>\n\n"
    text += "<i>يتم تحديث الحالة تلقائياً عند تجدد الحصص.</i>\n\n"

    keyboard = []

    for pid, provider in AI_PROVIDERS.items():
        all_keys = _get_provider_keys_for_quota(pid)
        if not all_keys:
            text += (
                f"🔹 <b>{html.escape(provider.name)}:</b>\n"
                f"  ⚠️ لا توجد مفاتيح مُعدة\n\n"
            )
            continue

        prov_status = ai_quota_tracker.get_provider_status(pid)
        available_count = sum(1 for k in all_keys if ai_quota_tracker.is_key_available(pid, k))
        blocked_count = len(all_keys) - available_count

        prov_icon = "✅" if prov_status.get("available") else "⏸️"
        text += (
            f"{prov_icon} <b>{html.escape(provider.name)}:</b>\n"
            f"  • المفاتيح المتاحة: {available_count}/{len(all_keys)}\n"
            f"  • المفاتيح المحظورة: {blocked_count}\n"
        )
        if not prov_status.get("available"):
            text += f"  • الخدمة محظورة حتى: <code>{prov_status.get('blocked_until', '?')}</code>\n"
        if prov_status.get("last_quota_exhausted_at"):
            text += f"  • آخر نفاد حصة: <code>{prov_status.get('last_quota_exhausted_at', '?')}</code>\n"
        text += "\n"

        # Add per-provider detail button
        keyboard.append([InlineKeyboardButton(
            f"🔍 تفاصيل {provider.name} ({available_count}/{len(all_keys)})",
            callback_data=f"ai_quota_prov:{pid}"
        )])

    # Global actions
    keyboard.append([InlineKeyboardButton("♻️ فك حظر كل المفاتيح", callback_data="ai_quota_unblock_all")])
    keyboard.append([InlineKeyboardButton("🧹 تنظيف الحالات المنتهية", callback_data="ai_quota_cleanup")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="ai_main_menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MENU


async def show_provider_quota_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض تفاصيل مفاتيح مزود محدد"""
    query = update.callback_query
    await query.answer()

    provider_id = query.data.split(":")[1]
    if provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MENU

    provider = AI_PROVIDERS[provider_id]

    try:
        from ...agent import ai_quota_tracker
    except Exception as e:
        await query.edit_message_text(f"❌ فشل تحميل وحدة الحصص: {e}")
        return AI_MENU

    all_keys = _get_provider_keys_for_quota(provider_id)
    if not all_keys:
        await query.edit_message_text(
            f"❌ لا توجد مفاتيح مُعدة لـ {provider.name}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="ai_quota_status")]
            ])
        )
        return AI_MENU

    text = f"🔍 <b>تفاصيل مفاتيح {html.escape(provider.name)}</b>\n\n"

    keyboard = []
    for i, key in enumerate(all_keys):
        status = ai_quota_tracker.get_key_status(provider_id, key)
        masked = f"...{key[-8:]}" if len(key) > 10 else key
        icon = "✅" if status.get("available") else "🚫"

        text += f"{icon} <code>{masked}</code>\n"
        if status.get("blocked"):
            text += f"   • محظور حتى: <code>{status.get('blocked_until', '?')}</code>\n"
        if status.get("quota_pending"):
            text += f"   • الحصة ستجدد: <code>{status.get('quota_reset_at', '?')}</code>\n"
        if status.get("last_error_category"):
            text += f"   • آخر خطأ: <code>{status.get('last_error_category')}</code>\n"
        if status.get("last_success_at"):
            text += f"   • آخر نجاح: <code>{status.get('last_success_at', '?')[:19]}</code>\n"
        text += "\n"

        # Add unblock button for blocked keys
        if not status.get("available"):
            keyboard.append([InlineKeyboardButton(
                f"🔓 فك حظر {masked}",
                callback_data=f"ai_quota_unblock:{provider_id}:{i}"
            )])

    keyboard.append([InlineKeyboardButton("🔙 رجوع لقائمة الحصص", callback_data="ai_quota_status")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MENU


async def handle_quota_unblock_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فك حظر مفتاح محدد يدوياً"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    provider_id = parts[1]
    idx = int(parts[2])

    if provider_id not in AI_PROVIDERS:
        await query.message.reply_text("❌ مزود غير صالح.")
        return AI_MENU

    try:
        from ...agent import ai_quota_tracker
    except Exception as e:
        await query.message.reply_text(f"❌ فشل تحميل وحدة الحصص: {e}")
        return AI_MENU

    all_keys = _get_provider_keys_for_quota(provider_id)
    if 0 <= idx < len(all_keys):
        key = all_keys[idx]
        success = ai_quota_tracker.force_unblock_key(provider_id, key)
        if success:
            masked = f"...{key[-8:]}" if len(key) > 10 else key
            await query.message.reply_text(
                f"✅ تم فك حظر المفتاح <code>{masked}</code>\n"
                f"سيعاد استخدامه في الطلبات القادمة.",
                parse_mode='HTML'
            )
        else:
            await query.message.reply_text("⚠️ لم يتم العثور على المفتاح في السجل.")
    else:
        await query.message.reply_text("❌ فهرس غير صالح.")

    # Refresh the detail view
    return await show_provider_quota_detail(update, context)


async def handle_quota_unblock_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فك حظر كل المفاتيح والمزودين لكل المزودين"""
    query = update.callback_query
    await query.answer()

    try:
        from ...agent import ai_quota_tracker
    except Exception as e:
        await query.message.reply_text(f"❌ فشل تحميل وحدة الحصص: {e}")
        return AI_MENU

    keys_count = ai_quota_tracker.force_unblock_all_keys()
    providers_count = ai_quota_tracker.force_unblock_all_providers()
    await query.message.reply_text(
        f"✅ تم فك حظر {keys_count} مفتاح و {providers_count} مزود.\n"
        f"سيتم استخدامها في الطلبات القادمة."
    )

    return await show_ai_quota_status(update, context)


async def handle_quota_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنظيف الحالات المنتهية يدوياً"""
    query = update.callback_query
    await query.answer()

    try:
        from ...agent import ai_quota_tracker
    except Exception as e:
        await query.message.reply_text(f"❌ فشل تحميل وحدة الحصص: {e}")
        return AI_MENU

    cleared = ai_quota_tracker.cleanup_expired()
    await query.message.reply_text(
        f"🧹 تم تنظيف {cleared} حالة منتهية الصلاحية."
    )

    return await show_ai_quota_status(update, context)


async def exit_ai_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الخروج إلى القائمة الرئيسية للأتمتة"""
    query = update.callback_query
    if query:
        await query.answer()

    # استيراد محلي لتجنب التعارض (Circular Import)
    from .auto_mod_handlers import auto_mod_menu
    await auto_mod_menu(update, context)
    return ConversationHandler.END


# ==================== إدارة النماذج (Models) ====================

def _get_provider_models_with_state(provider_id: str):
    """Returns (user_saved_models, default_models) for a provider."""
    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
        defaults = ai_models_store.get_default_models(provider_id)
        return entries, defaults
    except Exception as e:
        logger.warning(f"ai_models_store unavailable: {e}")
        return [], list(AI_PROVIDERS.get(provider_id).models) if provider_id in AI_PROVIDERS else []


async def show_ai_models_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة اختيار المزود لإدارة النماذج"""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "🧠 <b>إدارة النماذج (Models)</b>\n\n"
        "اختر الخدمة التي تريد إدارة نماذجها:\n\n"
        "<i>عند إضافة عدة نماذج، سيحاول البوت استخدامها بالترتيب. "
        "إذا فشل نموذج (مثلاً: تم إيقافه أو حدث خطأ)، ينتقل تلقائياً للنموذج التالي.</i>"
    )

    keyboard = []
    for pid, provider in AI_PROVIDERS.items():
        try:
            from ...agent import ai_models_store
            entries = ai_models_store.get_model_entries(pid)
            count = len(entries)
            enabled = sum(1 for e in entries if e.get("enabled", True))
        except Exception:
            count = 0
            enabled = 0
        label = f"🔹 {provider.name} ({enabled}/{count})" if count > 0 else f"🔹 {provider.name}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ai_mdl_prov:{pid}")])

    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="ai_main_menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MODELS_MENU


async def handle_models_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة نماذج مزود محدد"""
    query = update.callback_query
    await query.answer()

    provider_id = query.data.split(":")[1]
    context.user_data["selected_ai_provider"] = provider_id
    provider = AI_PROVIDERS[provider_id]

    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
        defaults = ai_models_store.get_default_models(provider_id)
    except Exception:
        entries = []
        defaults = list(provider.models)

    text = (
        f"🧠 <b>نماذج {html.escape(provider.name)}</b>\n\n"
        f"<b>النماذج المحفوظة ({len(entries)}):</b>\n"
    )
    if not entries:
        text += "<i>لا توجد نماذج محفوظة بعد. يتم استخدام الإعدادات الافتراضية.</i>\n"
    else:
        for i, e in enumerate(entries, 1):
            mid = html.escape(str(e.get("id") or ""))
            status = "✅" if e.get("enabled", True) else "❌"
            text += f"{i}. {status} <code>{mid}</code>\n"

    text += f"\n<b>النماذج الافتراضية:</b>\n"
    for m in defaults[:5]:
        text += f"• <code>{html.escape(m)}</code>\n"
    if len(defaults) > 5:
        text += f"• <i>...و {len(defaults) - 5} أخرى</i>\n"

    keyboard = [
        [InlineKeyboardButton("➕ إضافة نموذج", callback_data="ai_mdl_add")],
        [InlineKeyboardButton("🗑️ حذف نموذج", callback_data="ai_mdl_remove_list")],
        [InlineKeyboardButton("🔄 تفعيل/تعطيل نموذج", callback_data="ai_mdl_toggle_list")],
        [InlineKeyboardButton("♻️ استعادة الافتراضي", callback_data=f"ai_mdl_reset:{provider_id}")],
        [InlineKeyboardButton("🔙 رجوع لقائمة المزودين", callback_data="ai_models_menu")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MODELS_MENU


async def add_model_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء إضافة نموذج جديد"""
    query = update.callback_query
    await query.answer()

    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح. حاول مرة أخرى.")
        return await show_ai_models_menu(update, context)

    provider = AI_PROVIDERS[provider_id]

    try:
        from ...agent import ai_models_store
        defaults = ai_models_store.get_default_models(provider_id)
    except Exception:
        defaults = list(provider.models)

    examples_text = "\n".join(f"• <code>{html.escape(m)}</code>" for m in defaults[:5])

    await query.edit_message_text(
        f"📝 <b>إضافة نموذج لـ {html.escape(provider.name)}</b>\n\n"
        f"أرسل معرّف النموذج (Model ID) الذي تريد إضافته.\n"
        f"يمكنك إرسال عدة نماذج دفعة واحدة (واحد في كل سطر).\n\n"
        f"<b>أمثلة على النماذج:</b>\n{examples_text}\n\n"
        f"<i>سيتم تفعيل النموذج الجديد تلقائياً ووضعه في نهاية القائمة.</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"ai_mdl_prov:{provider_id}")]
        ]),
        parse_mode='HTML'
    )
    return AI_ADD_MODEL


async def receive_ai_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال وحفظ النموذج الجديد"""
    text_raw = update.message.text.strip()
    new_models = [m.strip() for m in text_raw.split("\n") if m.strip()]

    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await update.message.reply_text("❌ مزود غير صالح.")
        return ConversationHandler.END

    try:
        from ...agent import ai_models_store
    except Exception as e:
        await update.message.reply_text(f"❌ فشل تحميل وحدة النماذج: {e}")
        return ConversationHandler.END

    added = 0
    duplicate = 0
    for m in new_models:
        if ai_models_store.add_model(provider_id, m):
            added += 1
        else:
            duplicate += 1

    # Force runtime managers to refresh their cached model list
    try:
        _refresh_runtime_models(provider_id)
    except Exception as e:
        logger.warning(f"Runtime model refresh failed for {provider_id}: {e}")

    msg = f"✅ تم إضافة {added} نموذج جديد لـ {AI_PROVIDERS[provider_id].name}."
    if duplicate > 0:
        msg += f"\n⚠️ تم تجاهل {duplicate} نموذج موجود مسبقاً."
    msg += "\n\n💾 تم حفظ النماذج محلياً وقاعدة البيانات (إن وُجدت)."
    msg += f"\n\nيمكنك إضافة المزيد من النماذج، أو الضغط على التالي للعودة لقائمة النماذج."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 العودة لقائمة النماذج", callback_data=f"ai_mdl_prov:{provider_id}")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية للذكاء", callback_data="ai_main_menu")],
    ])
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode='HTML')
    return AI_MODELS_MENU


async def list_models_for_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة النماذج للحذف"""
    query = update.callback_query
    await query.answer()

    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MODELS_MENU

    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
    except Exception:
        entries = []

    if not entries:
        await query.edit_message_text(
            "❌ لا توجد نماذج محفوظة لهذا المزود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"ai_mdl_prov:{provider_id}")]
            ])
        )
        return AI_REMOVE_MODEL

    text = f"🗑️ <b>حذف نموذج من {html.escape(AI_PROVIDERS[provider_id].name)}</b>\n\nاختر النموذج المراد حذفه:"
    keyboard = []
    for i, e in enumerate(entries):
        mid = str(e.get("id") or "")
        masked = mid if len(mid) <= 40 else (mid[:37] + "...")
        status = "✅" if e.get("enabled", True) else "❌"
        keyboard.append([InlineKeyboardButton(f"🗑️ {status} {masked}", callback_data=f"ai_mdl_rem:{i}")])

    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"ai_mdl_prov:{provider_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_REMOVE_MODEL


async def handle_model_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف نموذج محدد"""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":")[1])
    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MODELS_MENU

    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
    except Exception:
        entries = []

    if 0 <= idx < len(entries):
        removed = entries[idx]
        mid = str(removed.get("id") or "")
        try:
            ai_models_store.remove_model(provider_id, mid)
            _refresh_runtime_models(provider_id)
            await query.message.reply_text(f"✅ تم حذف النموذج:\n<code>{html.escape(mid)}</code>", parse_mode='HTML')
        except Exception as e:
            await query.message.reply_text(f"❌ فشل الحذف: {e}")
    else:
        await query.message.reply_text("❌ فهرس غير صالح.")

    return await list_models_for_removal(update, context)


async def list_models_for_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة النماذج للتفعيل/التعطيل"""
    query = update.callback_query
    await query.answer()

    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MODELS_MENU

    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
    except Exception:
        entries = []

    if not entries:
        await query.edit_message_text(
            "❌ لا توجد نماذج محفوظة لهذا المزود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"ai_mdl_prov:{provider_id}")]
            ])
        )
        return AI_MODELS_MENU

    text = f"🔄 <b>تفعيل/تعطيل نموذج في {html.escape(AI_PROVIDERS[provider_id].name)}</b>\n\nاختر النموذج:"
    keyboard = []
    for i, e in enumerate(entries):
        mid = str(e.get("id") or "")
        masked = mid if len(mid) <= 40 else (mid[:37] + "...")
        status = "✅" if e.get("enabled", True) else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {masked}", callback_data=f"ai_mdl_tgl:{i}")])

    keyboard.append([InlineKeyboardButton("🔙 عودة", callback_data=f"ai_mdl_prov:{provider_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return AI_MODELS_MENU


async def handle_model_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تبديل حالة تفعيل نموذج"""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":")[1])
    provider_id = context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MODELS_MENU

    try:
        from ...agent import ai_models_store
        entries = ai_models_store.get_model_entries(provider_id)
    except Exception:
        entries = []

    if 0 <= idx < len(entries):
        mid = str(entries[idx].get("id") or "")
        new_state = ai_models_store.toggle_model(provider_id, mid)
        if new_state is None:
            await query.message.reply_text("❌ لم يتم العثور على النموذج.")
        else:
            _refresh_runtime_models(provider_id)
            status = "✅ مُفعّل" if new_state else "❌ مُعطّل"
            await query.message.reply_text(f"{status} النموذج:\n<code>{html.escape(mid)}</code>", parse_mode='HTML')
    else:
        await query.message.reply_text("❌ فهرس غير صالح.")

    return await list_models_for_toggle(update, context)


async def handle_models_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استعادة النماذج الافتراضية"""
    query = update.callback_query
    await query.answer()

    provider_id = query.data.split(":")[1] if ":" in query.data else context.user_data.get("selected_ai_provider")
    if not provider_id or provider_id not in AI_PROVIDERS:
        await query.edit_message_text("❌ مزود غير صالح.")
        return AI_MODELS_MENU

    try:
        from ...agent import ai_models_store
        ai_models_store.reset_to_defaults(provider_id)
        _refresh_runtime_models(provider_id)
        await query.message.reply_text(
            f"✅ تم حذف جميع النماذج المخصصة لـ {AI_PROVIDERS[provider_id].name}.\n"
            f"سيتم استخدام النماذج الافتراضية."
        )
    except Exception as e:
        await query.message.reply_text(f"❌ فشل: {e}")

    context.user_data["selected_ai_provider"] = provider_id
    return await handle_models_provider_selection(update, context)


def _refresh_runtime_models(provider_id: str) -> None:
    """Force runtime managers to reload models from ai_models_store on next call."""
    # The managers read from ai_models_store on every get_models() call,
    # so no explicit refresh is needed. But we can clear any cached state if added later.
    try:
        if provider_id == "groq":
            from ...agent.groq_manager import get_groq_manager
            mgr = get_groq_manager()
            # Force-clear state["models"] so the manager doesn't fall back to it
            mgr.state.pop("models", None)
            mgr._save_state()
        elif provider_id == "clarifai":
            from ...agent.clarifai_manager import get_clarifai_manager
            mgr = get_clarifai_manager()
            mgr.state.pop("models", None)
            mgr._save_state()
        elif provider_id == "openrouter":
            from ...agent.openrouter_manager import get_openrouter_manager
            mgr = get_openrouter_manager()
            # OpenRouter's get_models() reads user_models first; no override needed.
            pass
    except Exception as e:
        logger.warning(f"Failed to refresh runtime models for {provider_id}: {e}")


def get_ai_manager_conv():
    """الحصول على ConversationHandler الخاص بإدارة الذكاء الاصطناعي"""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(show_ai_menu, pattern="^ai_main_menu$"),
            CallbackQueryHandler(show_ai_keys_menu, pattern="^ai_keys_menu$"),
            CallbackQueryHandler(show_ai_stats, pattern="^ai_stats$"),
            CallbackQueryHandler(show_ai_models_menu, pattern="^ai_models_menu$"),
            CallbackQueryHandler(show_ai_quota_status, pattern="^ai_quota_status$"),
            CallbackQueryHandler(show_provider_quota_detail, pattern="^ai_quota_prov:"),
            CallbackQueryHandler(handle_quota_unblock_key, pattern="^ai_quota_unblock:"),
            CallbackQueryHandler(handle_quota_unblock_all, pattern="^ai_quota_unblock_all$"),
            CallbackQueryHandler(handle_quota_cleanup, pattern="^ai_quota_cleanup$"),
            # دخول من القائمة الرئيسية عبر callback (settings -> ai_management)
            CallbackQueryHandler(show_ai_menu, pattern="^ai_management$"),
        ],
        states={
            AI_MENU: [
                CallbackQueryHandler(show_ai_keys_menu, pattern="^ai_keys_menu$"),
                CallbackQueryHandler(show_ai_stats, pattern="^ai_stats$"),
                CallbackQueryHandler(show_ai_models_menu, pattern="^ai_models_menu$"),
                CallbackQueryHandler(show_ai_quota_status, pattern="^ai_quota_status$"),
                CallbackQueryHandler(show_provider_quota_detail, pattern="^ai_quota_prov:"),
                CallbackQueryHandler(handle_quota_unblock_key, pattern="^ai_quota_unblock:"),
                CallbackQueryHandler(handle_quota_unblock_all, pattern="^ai_quota_unblock_all$"),
                CallbackQueryHandler(handle_quota_cleanup, pattern="^ai_quota_cleanup$"),
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
            ],
            AI_MODELS_MENU: [
                CallbackQueryHandler(handle_models_provider_selection, pattern="^ai_mdl_prov:"),
                CallbackQueryHandler(add_model_start, pattern="^ai_mdl_add$"),
                CallbackQueryHandler(list_models_for_removal, pattern="^ai_mdl_remove_list$"),
                CallbackQueryHandler(list_models_for_toggle, pattern="^ai_mdl_toggle_list$"),
                CallbackQueryHandler(handle_model_toggle, pattern="^ai_mdl_tgl:"),
                CallbackQueryHandler(handle_models_reset, pattern="^ai_mdl_reset:"),
                CallbackQueryHandler(show_ai_models_menu, pattern="^ai_models_menu$"),
            ],
            AI_ADD_MODEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ai_model),
                CallbackQueryHandler(handle_models_provider_selection, pattern="^ai_mdl_prov:"),
            ],
            AI_REMOVE_MODEL: [
                CallbackQueryHandler(handle_model_removal, pattern="^ai_mdl_rem:"),
                CallbackQueryHandler(handle_models_provider_selection, pattern="^ai_mdl_prov:"),
                CallbackQueryHandler(list_models_for_removal, pattern="^ai_mdl_remove_list$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(show_ai_menu, pattern="^ai_main_menu$"),
            CallbackQueryHandler(exit_ai_manager, pattern="^am_menu$")
        ],
        allow_reentry=True,
        name="ai_manager_conv",
        per_message=False
    )
