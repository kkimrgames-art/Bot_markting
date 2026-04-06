#!/usr/bin/env python3
"""
Supabase Storage - طبقة التخزين المتكاملة مع Supabase
تدعم حفظ واسترجاع البيانات مع Fallback للتخزين المحلي
"""
import os
import json
import time
import logging
from typing import Optional, Any, Dict, List
from pathlib import Path
from datetime import datetime

from .config import get_project_root

from .supabase_client import (
    supabase_upsert,
    supabase_select,
    supabase_select_one,
    supabase_delete,
    supabase_insert_many,
    supabase_storage_upload,
    supabase_storage_download_to_file,
    supabase_storage_delete,
    is_online,
    queue_sync_operation,
    sync_pending_operations,
    USE_SUPABASE
)

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(get_project_root())


def _project_data_path(*parts: str) -> Path:
    return _PROJECT_ROOT.joinpath(".data", *parts)

# مسارات التخزين المحلي
LOCAL_STATE_PATH = _project_data_path("tg_state.json")
LOCAL_CHANNELS_DIR = _project_data_path("channels")

_BOT_STATE_CACHE: Optional[Dict[str, Any]] = None
_BOT_STATE_CACHE_TS: float = 0.0
_BOT_STATE_CACHE_TTL_SEC: Optional[float] = None


def _bot_state_cache_ttl_sec() -> float:
    global _BOT_STATE_CACHE_TTL_SEC
    if _BOT_STATE_CACHE_TTL_SEC is not None:
        return _BOT_STATE_CACHE_TTL_SEC
    raw = (os.environ.get("SUPABASE_BOT_STATE_CACHE_TTL_SEC") or "").strip()
    try:
        ttl = float(raw) if raw else 30.0
    except Exception:
        ttl = 30.0
    _BOT_STATE_CACHE_TTL_SEC = max(0.0, ttl)
    return _BOT_STATE_CACHE_TTL_SEC


# ========== Bot State Storage ==========

def save_bot_state(state: Dict[str, Any]) -> bool:
    """حفظ حالة البوت في Supabase والمحلي"""
    
    def _save_local(data):
        try:
            LOCAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOCAL_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في الحفظ المحلي: {e}")
    
    full_state = dict(state or {})

    publish_channels = full_state.get("publish_channels", []) or []
    published_by_channel = full_state.get("published_by_channel", {}) or {}
    publishing_inflight = full_state.get("publishing_inflight", {}) or {}
    facecam_clips_by_channel = full_state.get("facecam_clips_by_channel", {}) or {}
    ai_metadata_history = full_state.get("ai_metadata_history", {}) or {}
    
    # حفظ الحالة الرئيسية - البيانات المهمة فقط
    # البيانات غير المهمة (awaiting, last_output, telegram_notifications, facecam_missing_notified) تُحفظ محلياً فقط
    bot_state_data = {
        "id": "main",
        **{k: v for k, v in full_state.items() if not isinstance(v, (list, dict)) or k in [
            # بيانات القنوات (مهمة)
            "channels",
            "enabled_channels",

            # إعدادات النشر (مهمة)
            "quality",
            "pip",
            "schedule",
            "conditions",
            "proxy",

            # حالة النشر (مهمة)
            "agent",
            "publishing_lock",
            "ai",
            "enhance",

            # البيانات غير المهمة تم استبعادها (تُحفظ محلياً فقط):
            # - awaiting: حالة انتظار المستخدم (مؤقتة)
            # - last_output: آخر مخرجات (مؤقتة)
            # - telegram_notifications: إشعارات تيليجرام (مؤقتة)
            # - facecam_missing_notified: إشعارات FaceCam (مؤقتة)
            # - enhance, ai, downloader, scheduler: إعدادات محلية
        ]},
        "updated_at": datetime.now().isoformat()
    }
    
    # تحويل القيم غير القابلة للتسلسل
    for key, value in list(bot_state_data.items()):
        if isinstance(value, (list, dict)):
            bot_state_data[key] = json.dumps(value) if not isinstance(value, str) else value
    
    success = supabase_upsert("bot_state", bot_state_data, "id", lambda _: _save_local(full_state))
    
    # حفظ قنوات النشر
    for ch in publish_channels:
        ch_data = {
            "channel_id": ch.get("channel_id"),
            "internal_id": ch.get("internal_id"),
            "title": ch.get("title"),
            "enabled": ch.get("enabled", True),
            "lang": ch.get("lang", "ar"),
            "privacy": ch.get("privacy", "public"),
            "token_path": ch.get("token_path"),
            "content_type": ch.get("content_type", "minecraft"),
            "quality": ch.get("quality", "auto"),
            "overlay_font_path": ch.get("overlay_font_path"),
            "overlay_position": ch.get("overlay_position", "bottom_center"),
            "custom_description": ch.get("custom_description"),
            "custom_description_mode": ch.get("custom_description_mode", "append"),
            "facecam_enabled": ch.get("facecam_enabled", False),
            "facecam_clip_id": ch.get("facecam_clip_id"),
            "facecam_position": ch.get("facecam_position", "top_right"),
            "facecam_scale": ch.get("facecam_scale"),
            "description_sections": json.dumps(ch.get("description_sections")) if ch.get("description_sections") else None,
            "sections_mode": ch.get("sections_mode", "append"),
            "added_date": ch.get("added_date"),
        }
        supabase_upsert("publish_channels", ch_data, "channel_id")
    
    # حفظ الفيديوهات المنشورة
    for channel_id, videos in published_by_channel.items():
        for video_id, youtube_url in videos.items():
            supabase_upsert("published_videos", {
                "channel_id": channel_id,
                "video_id": video_id,
                "youtube_url": youtube_url
            }, "id")
    
    # حفظ النشر قيد التنفيذ
    for key, data in publishing_inflight.items():
        parts = key.split("::")
        if len(parts) == 2:
            supabase_upsert("publishing_inflight", {
                "id": key,
                "channel_id": parts[0],
                "video_id": parts[1],
                "ts": data.get("ts")
            }, "id")
    
    # حفظ مقاطع Facecam
    for channel_id, clips in facecam_clips_by_channel.items():
        for clip in clips:
            supabase_upsert("facecam_clips", {
                "id": clip.get("id"),
                "channel_id": channel_id,
                "path": clip.get("path"),
                "name": clip.get("name"),
                "enabled": clip.get("enabled", True),
                "created_at": clip.get("created_at")
            }, "id")
    
    # حفظ تاريخ AI Metadata
    save_ai_history = (os.environ.get("SAVE_AI_METADATA_HISTORY") or "true").strip().lower() in {"1", "true", "yes", "on"}
    if save_ai_history:
        for video_type, channels_data in ai_metadata_history.items():
            for channel_key, history_list in (channels_data or {}).items():
                parts = channel_key.split("::")
                channel_id = parts[0]
                lang = parts[1] if len(parts) > 1 else "en"
                
                for entry in (history_list or []):
                    supabase_upsert("ai_metadata_history", {
                        "channel_internal_id": channel_id,
                        "lang": lang,
                        "video_type": video_type,
                        "title": entry.get("title"),
                        "description": entry.get("desc"),
                        "hashtags": entry.get("hashtags"),
                        "ts": entry.get("ts")
                    }, "id")
    
    _save_local(full_state)
    
    # تحديث الكاش المحلي لضمان اتساق البيانات عند القراءة التالية
    global _BOT_STATE_CACHE, _BOT_STATE_CACHE_TS
    _BOT_STATE_CACHE = dict(full_state)
    _BOT_STATE_CACHE_TS = time.time()
    
    return success


def load_bot_state() -> Dict[str, Any]:
    """تحميل حالة البوت من Supabase أو المحلي"""

    global _BOT_STATE_CACHE, _BOT_STATE_CACHE_TS
    
    def _load_local():
        try:
            if LOCAL_STATE_PATH.exists():
                with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"خطأ في التحميل المحلي: {e}")
        return {}

    ttl = _bot_state_cache_ttl_sec()
    now = time.time()
    if ttl > 0 and _BOT_STATE_CACHE is not None and (now - _BOT_STATE_CACHE_TS) < ttl:
        try:
            return json.loads(json.dumps(_BOT_STATE_CACHE))
        except Exception:
            return dict(_BOT_STATE_CACHE)
    
    # محاولة التحميل من Supabase
    if USE_SUPABASE and is_online():
        try:
            # تحميل الحالة الرئيسية
            result = supabase_select_one("bot_state", "id", "main")
            if result:
                state = dict(result)
                
                # تحويل JSONB إلى dict
                for key in ["channels", "enabled_channels", "quality", "pip", "schedule",
                           "conditions", "proxy", "awaiting", "last_output", "agent",
                           "publishing_lock", "enhance", "ai", "downloader", "scheduler",
                           "telegram_notifications", "facecam_missing_notified"]:
                    if key in state and isinstance(state[key], str):
                        try:
                            state[key] = json.loads(state[key])
                        except:
                            pass
                
                # تحميل قنوات النشر
                channels = supabase_select("publish_channels")
                if channels:
                    state["publish_channels"] = channels
                
                # تحميل الفيديوهات المنشورة
                videos = supabase_select("published_videos")
                if videos:
                    published_by_channel = {}
                    for v in videos:
                        ch_id = v.get("channel_id")
                        if ch_id not in published_by_channel:
                            published_by_channel[ch_id] = {}
                        published_by_channel[ch_id][v.get("video_id")] = v.get("youtube_url")
                    state["published_by_channel"] = published_by_channel
                
                # تحميل النشر قيد التنفيذ
                inflight = supabase_select("publishing_inflight")
                if inflight:
                    state["publishing_inflight"] = {
                        f"{i['channel_id']}::{i['video_id']}": {"ts": i.get("ts")}
                        for i in inflight
                    }
                
                # تحميل مقاطع Facecam
                clips = supabase_select("facecam_clips")
                if clips:
                    facecam_by_channel = {}
                    for c in clips:
                        ch_id = c.get("channel_id")
                        if ch_id not in facecam_by_channel:
                            facecam_by_channel[ch_id] = []
                        facecam_by_channel[ch_id].append({
                            "id": c.get("id"),
                            "path": c.get("path"),
                            "name": c.get("name"),
                            "enabled": c.get("enabled", True),
                            "created_at": c.get("created_at")
                        })
                    state["facecam_clips_by_channel"] = facecam_by_channel
                
                # تحميل تاريخ AI Metadata
                ai_meta = supabase_select("ai_metadata_history")
                if ai_meta:
                    ai_history = {}
                    for row in ai_meta:
                        v_type = row.get("video_type", "shorts")
                        ch_id = row.get("channel_internal_id")
                        lang = row.get("lang", "en")
                        key = f"{ch_id}::{lang}"
                        
                        if v_type not in ai_history:
                            ai_history[v_type] = {}
                        if key not in ai_history[v_type]:
                            ai_history[v_type][key] = []
                            
                        # إعادة بناء الكائن كما هو متوقع في الذاكرة
                        ai_history[v_type][key].append({
                            "title": row.get("title"),
                            "desc": row.get("description"),
                            "hashtags": row.get("hashtags"),
                            "ts": row.get("ts")
                        })
                    state["ai_metadata_history"] = ai_history
                
                _BOT_STATE_CACHE = dict(state)
                _BOT_STATE_CACHE_TS = time.time()
                return state
                
        except Exception as e:
            logger.error(f"❌ فشل التحميل من Supabase: {e}")
    
    # Fallback: تحميل محلي
    state = _load_local()
    try:
        _BOT_STATE_CACHE = dict(state)
        _BOT_STATE_CACHE_TS = time.time()
    except Exception:
        pass
    return state


# ========== Channel Configs Storage ==========

def save_channel_config(channel: Dict[str, Any]) -> bool:
    """حفظ إعدادات قناة في Supabase"""
    channel_id = channel.get("channel_id")
    if not channel_id:
        return False
    
    def _save_local(data):
        local_data = dict(data)
        if "custom_overlay_texts" in channel:
            local_data["custom_overlay_texts"] = channel["custom_overlay_texts"]
            
        try:
            LOCAL_CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
            path = LOCAL_CHANNELS_DIR / f"{channel_id}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(local_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ القناة محلياً: {e}")
    
    # تحويل scheduling_settings إلى JSON string
    data = dict(channel)
    if "scheduling_settings" in data and isinstance(data["scheduling_settings"], dict):
        data["scheduling_settings"] = json.dumps(data["scheduling_settings"])
    if "intro_videos" in data and isinstance(data["intro_videos"], list):
        data["intro_videos"] = json.dumps(data["intro_videos"])
    if "outro_videos" in data and isinstance(data["outro_videos"], list):
        data["outro_videos"] = json.dumps(data["outro_videos"])
    
    # Supabase schema doesn't have custom_overlay_texts, so drop it before upsert
    data.pop("custom_overlay_texts", None)

    return supabase_upsert("channel_configs", data, "channel_id", _save_local)


def load_channel_config(channel_id: str) -> Optional[Dict[str, Any]]:
    """تحميل إعدادات قناة من Supabase"""
    
    def _load_local():
        try:
            path = LOCAL_CHANNELS_DIR / f"{channel_id}.json"
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"خطأ في تحميل القناة محلياً: {e}")
        return None
    
    result = supabase_select_one("channel_configs", "channel_id", channel_id, _load_local)
    
    if result:
        # تحويل JSON strings إلى dict/list
        for key in ["scheduling_settings", "intro_videos", "outro_videos"]:
            if key in result and isinstance(result[key], str):
                try:
                    result[key] = json.loads(result[key])
                except:
                    pass
    
    return result


def list_channel_configs(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """تحميل جميع إعدادات القنوات"""
    
    def _load_local():
        channels = []
        try:
            if LOCAL_CHANNELS_DIR.exists():
                for path in LOCAL_CHANNELS_DIR.glob("*.json"):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            ch = json.load(f)
                            if not enabled_only or ch.get("enabled", True):
                                channels.append(ch)
                    except:
                        pass
        except Exception as e:
            logger.error(f"خطأ في تحميل القنوات محلياً: {e}")
        return channels
    
    filters = {"enabled": True} if enabled_only else None
    result = supabase_select("channel_configs", filters, _load_local)
    
    if result:
        for ch in result:
            for key in ["scheduling_settings", "intro_videos", "outro_videos"]:
                if key in ch and isinstance(ch[key], str):
                    try:
                        ch[key] = json.loads(ch[key])
                    except:
                        pass
    
    return result or []


def delete_channel_config(channel_id: str) -> bool:
    """حذف إعدادات قناة"""
    
    def _delete_local(cid):
        try:
            path = LOCAL_CHANNELS_DIR / f"{cid}.json"
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.error(f"خطأ في حذف القناة محلياً: {e}")
    
    return supabase_delete("channel_configs", "channel_id", channel_id, _delete_local)


# ========== API Keys Storage ==========

def save_gemini_keys_state(keys_state: Dict[str, Any]) -> bool:
    """حفظ حالة مفاتيح Gemini"""
    keys = keys_state.get("keys", {})
    
    for key, state in keys.items():
        data = {
            "key": key,
            "requests_today": state.get("requests_today", 0),
            "requests_this_minute": state.get("requests_this_minute", 0),
            "last_request_time": state.get("last_request_time"),
            "last_reset_day": state.get("last_reset_day"),
            "last_reset_minute": state.get("last_reset_minute"),
            "is_blocked": state.get("is_blocked", False),
            "block_until": state.get("block_until"),
            "total_requests": state.get("total_requests", 0),
            "errors": state.get("errors", 0),
            "last_error_category": state.get("last_error_category"),
            "last_error_time": state.get("last_error_time")
        }
        supabase_upsert("api_keys_gemini", data, "key")
    
    return True


def load_gemini_keys_state() -> Dict[str, Any]:
    """تحميل حالة مفاتيح Gemini"""
    result = supabase_select("api_keys_gemini")
    
    if result:
        keys = {}
        for row in result:
            key = row.pop("key", None)
            if key:
                # إزالة الحقول الإضافية من Supabase
                row.pop("created_at", None)
                row.pop("updated_at", None)
                keys[key] = row
        return {"keys": keys}
    
    return {"keys": {}}


def save_openrouter_state(state: Dict[str, Any]) -> bool:
    """حفظ حالة OpenRouter"""
    keys = state.get("keys", {})
    models = state.get("models", [])
    dynamic_models = state.get("dynamic_models", [])
    
    # حفظ المفاتيح
    for key, key_state in keys.items():
        supabase_upsert("api_keys_openrouter", {"key": key, **key_state}, "key")
    
    # حفظ النماذج
    for model in set(models):
        supabase_upsert("openrouter_models", {"model_name": model, "is_dynamic": False}, "model_name", on_conflict="model_name")
    for model in set(dynamic_models):
        supabase_upsert("openrouter_models", {"model_name": model, "is_dynamic": True}, "model_name", on_conflict="model_name")
    
    # حفظ الحالة العامة
    supabase_upsert("openrouter_state", {
        "id": "main",
        "last_model_refresh": state.get("last_model_refresh"),
        "last_self_heal": state.get("last_self_heal")
    }, "id")
    
    return True


def load_openrouter_state() -> Dict[str, Any]:
    """تحميل حالة OpenRouter"""
    keys_result = supabase_select("api_keys_openrouter")
    models_result = supabase_select("openrouter_models")
    state_result = supabase_select_one("openrouter_state", "id", "main")
    
    state = {
        "keys": {},
        "models": [],
        "dynamic_models": []
    }
    
    if keys_result:
        for row in keys_result:
            key = row.pop("key", None)
            if key:
                row.pop("created_at", None)
                row.pop("updated_at", None)
                state["keys"][key] = row
    
    if models_result:
        for row in models_result:
            model_name = row.get("model_name")
            if model_name:
                if row.get("is_dynamic"):
                    state["dynamic_models"].append(model_name)
                else:
                    state["models"].append(model_name)
    
    if state_result:
        state["last_model_refresh"] = state_result.get("last_model_refresh")
        state["last_self_heal"] = state_result.get("last_self_heal")
    
    return state


def save_groq_state(state: Dict[str, Any]) -> bool:
    """حفظ حالة Groq"""
    keys = state.get("keys", {})
    models = state.get("models", [])
    
    for key, key_state in keys.items():
        supabase_upsert("api_keys_groq", {"key": key, **key_state}, "key")
    
    for model in set(models):
        supabase_upsert("groq_models", {"model_name": model}, "model_name", on_conflict="model_name")
    
    return True


def load_groq_state() -> Dict[str, Any]:
    """تحميل حالة Groq"""
    keys_result = supabase_select("api_keys_groq")
    models_result = supabase_select("groq_models")
    
    state = {"keys": {}, "models": []}
    
    if keys_result:
        for row in keys_result:
            key = row.pop("key", None)
            if key:
                row.pop("created_at", None)
                row.pop("updated_at", None)
                state["keys"][key] = row
    
    if models_result:
        state["models"] = [r.get("model_name") for r in models_result if r.get("model_name")]
    
    return state


def save_clarifai_state(state: Dict[str, Any]) -> bool:
    """حفظ حالة Clarifai"""
    return supabase_upsert("clarifai_state", {
        "id": "main",
        "keys": json.dumps(state.get("keys", {})),
        "models": json.dumps(state.get("models", []))
    }, "id")


def load_clarifai_state() -> Dict[str, Any]:
    """تحميل حالة Clarifai"""
    result = supabase_select_one("clarifai_state", "id", "main")
    
    if result:
        keys = result.get("keys", "{}")
        models = result.get("models", "[]")
        
        return {
            "keys": json.loads(keys) if isinstance(keys, str) else keys,
            "models": json.loads(models) if isinstance(models, str) else models
        }
    
    return {"keys": {}, "models": []}


def save_mistral_state(state: Dict[str, Any]) -> bool:
    """حفظ حالة مفاتيح Mistral"""
    keys = state.get("keys", {})
    return supabase_upsert("api_keys_mistral", {
        "id": "main",
        "keys": json.dumps(keys)
    }, "id")


def load_mistral_state() -> Dict[str, Any]:
    """تحميل حالة مفاتيح Mistral"""
    result = supabase_select_one("api_keys_mistral", "id", "main")
    if result:
        keys = result.get("keys", "{}")
        if isinstance(keys, str):
            try:
                keys = json.loads(keys)
            except:
                keys = {}
        return {"keys": keys}
    return {"keys": {}}


def save_api_keys(provider: str, state: Dict[str, Any]) -> bool:
    """دالة توزيع لحفظ حالة مفاتيح API حسب المزود"""
    if provider == "gemini":
        return save_gemini_keys_state(state)
    elif provider == "groq":
        return save_groq_state(state)
    elif provider == "openrouter":
        return save_openrouter_state(state)
    elif provider == "clarifai":
        return save_clarifai_state(state)
    elif provider == "mistral":
        return save_mistral_state(state)
    return False


def load_api_keys(provider: str) -> Dict[str, Any]:
    """دالة توزيع لتحميل حالة مفاتيح API حسب المزود"""
    if provider == "gemini":
        return load_gemini_keys_state()
    elif provider == "groq":
        return load_groq_state()
    elif provider == "openrouter":
        return load_openrouter_state()
    elif provider == "clarifai":
        return load_clarifai_state()
    elif provider == "mistral":
        return load_mistral_state()
    return {"keys": {}}


# ========== Admin Phones Storage ==========

def save_admin_phones(phones: List[str]) -> bool:
    """حفظ قائمة أرقام المديرين"""
    return supabase_upsert("admin_phones", {
        "id": "main",
        "phones": json.dumps(phones)
    }, "id")


def load_admin_phones() -> List[str]:
    """تحميل قائمة أرقام المديرين"""
    result = supabase_select_one("admin_phones", "id", "main")
    if result:
        phones = result.get("phones", "[]")
        if isinstance(phones, str):
            try:
                return json.loads(phones)
            except:
                return []
        return phones
    return []


# ========== Downloader State Storage ==========

def mark_video_processed(video_id: str, channel_url: str = None) -> bool:
    """تسجيل فيديو كمعالج"""
    data = {"video_id": video_id}
    if channel_url:
        data["channel_url"] = channel_url
    
    supabase_upsert("downloader_processed_videos", data, "video_id")
    
    if channel_url:
        supabase_upsert("downloader_channel_history", {
            "channel_url": channel_url,
            "video_id": video_id
        }, "id")
    
    return True


def is_video_processed(video_id: str) -> bool:
    """التحقق إذا كان الفيديو معالجاً"""
    result = supabase_select_one("downloader_processed_videos", "video_id", video_id)
    return result is not None


def get_processed_videos() -> List[str]:
    """الحصول على قائمة الفيديوهات المعالجة"""
    result = supabase_select("downloader_processed_videos")
    return []


# ========== Source Rate Limit Tracking ==========

def mark_source_rate_limited(source_url: str, duration: int = 3600) -> bool:
    """تسجيل مصدر كخاضع للقيود (rate-limited) لفترة زمنية محددة"""
    if not source_url:
        return False
    
    expires_at = time.time() + duration
    
    # تحديث في الذاكرة أولاً (لسرعة التحقق)
    state = load_bot_state()
    rate_limits = state.get("source_rate_limits", {})
    rate_limits[source_url] = expires_at
    state["source_rate_limits"] = rate_limits
    save_bot_state(state)
    
    # تسجيل في جدول منفصل لسهولة الفحص لاحقاً وتتبع الأنماط
    data = {
        "source_url": source_url,
        "expires_at": datetime.fromtimestamp(expires_at).isoformat(),
        "reason": "yt-dlp_rate_limited"
    }
    supabase_upsert("source_cool_downs", data, "source_url")
    
    logger.warning(f"❄️ Source marked for cool-down: {source_url} (expires in {duration}s)")
    return True


def is_source_rate_limited(source_url: str) -> bool:
    """التحقق إذا كان المصدر تحت فترة التبريد"""
    if not source_url:
        return False
        
    state = load_bot_state()
    rate_limits = state.get("source_rate_limits", {})
    
    expires_at = rate_limits.get(source_url)
    if expires_at and time.time() < expires_at:
        return True
        
    # تنظيف إذا انتهى الوقت (اختياري، يتم عند الحفظ القادم)
    if expires_at:
        del rate_limits[source_url]
        state["source_rate_limits"] = rate_limits
        save_bot_state(state)
        
    return False


# Backward-compatible helpers (used by downloader)
def save_processed_video(video_id: str, channel_url: Optional[str] = None) -> bool:
    """حفظ فيديو كـ processed لمنع التكرار."""
    if not video_id:
        return False
    data = {"video_id": video_id}
    if channel_url:
        data["channel_url"] = channel_url
    try:
        return bool(supabase_upsert("downloader_processed_videos", data, "video_id"))
    except Exception:
        # supabase_upsert already queues offline; if it fails hard, just ignore
        return False


def save_channel_history(channel_url: str, video_id: str) -> bool:
    """تسجيل video_id ضمن تاريخ قناة مصدر معينة."""
    if not (channel_url and video_id):
        return False
    data = {
        "channel_url": channel_url,
        "video_id": video_id,
    }
    try:
        # Unique index exists on (channel_url, video_id) in DB; use on_conflict for idempotent upsert.
        return bool(supabase_upsert("downloader_channel_history", data, "id", on_conflict="channel_url,video_id"))
    except Exception:
        return False


# ========== Full Sync ==========

async def sync_supabase_to_local() -> bool:
    """
    استعادة البيانات من Supabase إلى التخزين المحلي (للبيئات غير الدائمة مثل Render)
    """
    if not USE_SUPABASE or not is_online():
        logger.warning("⚠️ لا يمكن الاستعادة من Supabase (غير متصل أو معطل)")
        return False

    logger.info("⬇️ بدء استعادة البيانات من Supabase إلى المحلي...")
    
    try:
        # 1. استعادة إعدادات القنوات والتوكنات
        channels = list_channel_configs()
        if channels:
            LOCAL_CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
            tokens_dir = _project_data_path("youtube_tokens")
            tokens_dir.mkdir(parents=True, exist_ok=True)
            
            for ch in channels:
                channel_id = ch.get("channel_id")
                if not channel_id:
                    continue
                
                # حفظ ملف القناة
                ch_path = LOCAL_CHANNELS_DIR / f"{channel_id}.json"
                with open(ch_path, "w", encoding="utf-8") as f:
                    json.dump(ch, f, ensure_ascii=False, indent=2)
                
                # استخراج وحفظ التوكن إذا وجد
                creds_str = ch.get("platform_credentials")
                yt_id = ch.get("youtube_channel_id")
                
                if yt_id and creds_str:
                    token_path = tokens_dir / f"{yt_id}.json"
                    try:
                        # creds_str قد يكون JSON string أو dict
                        creds_data = json.loads(creds_str) if isinstance(creds_str, str) else creds_str
                        if creds_data:
                            with open(token_path, "w", encoding="utf-8") as f:
                                json.dump(creds_data, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logger.warning(f"Failed to restore token for {yt_id}: {e}")

            logger.info(f"✅ تم استعادة {len(channels)} قناة وتوكناتها.")
        
        # 2. استعادة حالة البوت
        state = load_bot_state()
        if state:
            LOCAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOCAL_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info("✅ تم استعادة حالة البوت.")

        # 3. استعادة بيانات AutoMod (المصادر، الجداول، الحاويات، ومفاتيح API)
        automod_tables = {
            "auto_mod_sources": _project_data_path("auto_mod_sources.json"),
            "auto_mod_schedule": _project_data_path("auto_mod_schedule.json"),
            "auto_mod_processed": _project_data_path("auto_mod_processed.json"),
            "youtube_api_keys": _project_data_path("youtube_api_keys.json"),
            "video_containers": _project_data_path("video_containers.json"),
            "video_container_videos": _project_data_path("video_container_videos.json")
        }
        
        for table, local_path in automod_tables.items():
            try:
                rows = supabase_select(table)
                if rows:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(local_path, "w", encoding="utf-8") as f:
                        json.dump(rows, f, ensure_ascii=False, indent=2)
                    logger.info(f"✅ تم استعادة {len(rows)} سجل لجدول {table}.")
                else:
                    logger.info(f"ℹ️ جدول {table} فارغ أو غير موجود.")
            except Exception as e:
                logger.warning(f"⚠️ فشل استعادة جدول {table}: {e}")

        # 4. استعادة بيانات FaceCam ومزامنة الملفات
        try:
            # مزامنة الفهرس
            fc_clips = supabase_select("facecam_clips")
            if fc_clips:
                # حفظ في الفهرس المحلي (حسب ما يتوقعه bot/persistence.py أو auto_mod_fetcher)
                # ملاحظة: Facecam clips تُخزن غالباً في bot_state ولكن جداول Supabase منفصلة
                logger.info(f"✅ تم استعادة {len(fc_clips)} مرجع لمقاطع FaceCam.")
            
            # مزامنة فهرس التخزين وتحميل الملفات المادية المفقودة
            fc_storage_rows = supabase_select("facecam_storage")
            if fc_storage_rows:
                FACECAM_STORAGE_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(FACECAM_STORAGE_LOCAL_PATH, "w", encoding="utf-8") as f:
                    json.dump(fc_storage_rows, f, ensure_ascii=False, indent=2)
                
                # تحميل الملفات المادية
                facecam_dir = _project_data_path("facecam")
                facecam_dir.mkdir(parents=True, exist_ok=True)
                
                download_count = 0
                for row in fc_storage_rows:
                    clip_id = row.get("id")
                    obj_path = row.get("storage_path")
                    if not clip_id or not obj_path:
                        continue
                        
                    # تحديد المسار المحلي (نحاول الحفاظ على نفس البنية)
                    # المسار الموجود في الحقل local_path قد يكون مطلقاً لنظام قديم، لذا نستخدم clip_id
                    ext = os.path.splitext(obj_path)[1] or ".mp4"
                    local_file = facecam_dir / f"{clip_id}{ext}"
                    
                    if not local_file.exists():
                        bucket = row.get("storage_bucket") or FACECAM_STORAGE_BUCKET
                        if supabase_storage_download_to_file(bucket, obj_path, str(local_file)):
                            download_count += 1
                
                if download_count > 0:
                    logger.info(f"✅ تم تحميل {download_count} ملف FaceCam مفقود من التخزين.")
        except Exception as e:
            logger.warning(f"⚠️ فشل مزامنة ملفات FaceCam: {e}")

        return True
    except Exception as e:
        logger.error(f"❌ فشل استعادة البيانات من Supabase: {e}")
        return False


async def full_sync_local_to_supabase():
    """مزامنة كاملة للبيانات المحلية إلى Supabase"""
    if not USE_SUPABASE:
        logger.info("⏭️ Supabase معطل، تخطي المزامنة")
        return False
    
    if not is_online():
        logger.warning("⚠️ لا يوجد اتصال بـ Supabase")
        return False
    
    logger.info("🔄 بدء المزامنة الكاملة من المحلي إلى Supabase...")
    
    try:
        # 1. مزامنة حالة البوت
        if LOCAL_STATE_PATH.exists():
            with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            save_bot_state(state)
            logger.info("✅ تم مزامنة حالة البوت")
        
        # 2. مزامنة إعدادات القنوات
        if LOCAL_CHANNELS_DIR.exists():
            for path in LOCAL_CHANNELS_DIR.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        channel = json.load(f)
                    save_channel_config(channel)
                except Exception as e:
                    logger.error(f"خطأ في مزامنة {path.name}: {e}")
            logger.info("✅ تم مزامنة إعدادات القنوات")
        
        # 3. مزامنة حالات API Keys
        for state_file, save_func in [
            (_project_data_path("gemini_keys_state.json"), save_gemini_keys_state),
            (_project_data_path("openrouter_state.json"), save_openrouter_state),
            (_project_data_path("groq_state.json"), save_groq_state),
            (_project_data_path("clarifai_state.json"), save_clarifai_state),
        ]:
            try:
                path = Path(state_file)
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    save_func(data)
                    logger.info(f"✅ تم مزامنة {path.name}")
            except Exception as e:
                logger.error(f"خطأ في مزامنة {state_file}: {e}")
        
        # 4. مزامنة العمليات المعلقة
        await sync_pending_operations()
        
        logger.info("✅ اكتملت المزامنة الكاملة بنجاح!")
        return True
        
    except Exception as e:
        logger.error(f"❌ فشلت المزامنة الكاملة: {e}")
        return False


VIDEO_CONTAINERS_LOCAL_PATH = _project_data_path("video_containers.json")
VIDEO_CONTAINER_VIDEOS_LOCAL_PATH = _project_data_path("video_container_videos.json")
VIDEO_CONTAINER_FILES_DIR = _project_data_path("video_container_files")
VIDEO_CONTAINER_FILES_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_CONTAINER_BUCKET = (os.environ.get("VIDEO_CONTAINER_BUCKET") or "video_containers").strip() or "video_containers"


def _map_android_storage_path_to_local(path_str: str) -> Optional[str]:
    if not path_str:
        return None
    if os.name != "nt":
        return None
    try:
        raw = str(path_str).strip()
        if not raw:
            return None
        raw = raw.replace("\\", "/")
        if raw.startswith("/sdcard/"):
            raw = "/storage/emulated/0/" + raw[len("/sdcard/"):]
        prefix = "/storage/emulated/0/"
        if not raw.startswith(prefix):
            return None

        rel = raw[len(prefix):]
        if not rel:
            return None

        # Start from the project root (AutoModBot directory) extending to the parent "وكيل الردات فعل"
        repo_root = Path(__file__).resolve().parents[2] # points to C:\Users\Sidivall AI\Desktop\وكيل الردات فعل\AutoModBot
        target_dir_name = "وكيل الردات فعل" 
        
        rel_parts = [p for p in rel.split("/") if p]
        
        # If the path starts with "وكيل الردات فعل", align it with repo_root.parent
        if rel_parts and rel_parts[0] == target_dir_name:
            mapped = os.path.join(str(repo_root.parent), *rel_parts)
            return mapped
            
        # fallback if format is different
        return os.path.join(str(repo_root.parent), target_dir_name, *rel_parts)

    except Exception:
        return None


def _load_local_list(path: Path) -> List[Dict[str, Any]]:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def _save_local_list(path: Path, items: List[Dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _local_upsert_by_id(path: Path, item: Dict[str, Any]) -> None:
    items = _load_local_list(path)
    item_id = item.get("id")
    if item_id:
        replaced = False
        for i, existing in enumerate(items):
            if existing.get("id") == item_id:
                items[i] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
    else:
        items.append(item)
    _save_local_list(path, items)


def _local_delete_by_id(path: Path, item_id: str) -> None:
    if not item_id:
        return
    items = _load_local_list(path)
    items = [x for x in items if x.get("id") != item_id]
    _save_local_list(path, items)


def create_video_container(name: str, owner_phone: str, *, description: str = "", settings: Optional[Dict[str, Any]] = None, visibility: str = "private") -> Dict[str, Any]:
    import uuid
    now = datetime.now().isoformat()
    cid = str(uuid.uuid4())
    payload = {
        "id": cid,
        "name": (name or "").strip() or "container",
        "description": (description or "").strip(),
        "owner_phone": (owner_phone or "").strip(),
        "visibility": (visibility or "private").strip().lower(),
        "settings": json.dumps(settings or {}, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }
    supabase_upsert("video_containers", payload, "id", lambda data: _local_upsert_by_id(VIDEO_CONTAINERS_LOCAL_PATH, data))
    out = dict(payload)
    try:
        out["settings"] = json.loads(out.get("settings") or "{}")
    except Exception:
        out["settings"] = {}
    return out


def list_video_containers(*, owner_phone: Optional[str] = None) -> List[Dict[str, Any]]:
    def _fallback():
        items = _load_local_list(VIDEO_CONTAINERS_LOCAL_PATH)
        if owner_phone:
            return [x for x in items if (x.get("owner_phone") or "").strip() == owner_phone.strip()]
        return items

    filters = {"owner_phone": owner_phone.strip()} if owner_phone else None
    rows = supabase_select("video_containers", filters, _fallback) or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            if isinstance(d.get("settings"), str):
                d["settings"] = json.loads(d["settings"] or "{}")
        except Exception:
            d["settings"] = {}
        out.append(d)
    out.sort(key=lambda x: (x.get("created_at") or ""), reverse=True)
    return out


def get_video_container(container_id: str) -> Optional[Dict[str, Any]]:
    if not container_id:
        return None

    def _fallback():
        for x in _load_local_list(VIDEO_CONTAINERS_LOCAL_PATH):
            if x.get("id") == container_id:
                return [x]
        return []

    row = supabase_select_one("video_containers", "id", container_id, _fallback)
    if not row:
        return None
    out = dict(row)
    try:
        if isinstance(out.get("settings"), str):
            out["settings"] = json.loads(out["settings"] or "{}")
    except Exception:
        out["settings"] = {}
    return out


def update_video_container(container_id: str, *, name: Optional[str] = None, description: Optional[str] = None, settings: Optional[Dict[str, Any]] = None, visibility: Optional[str] = None) -> bool:
    if not container_id:
        return False
    current = get_video_container(container_id)
    if not current:
        return False
    now = datetime.now().isoformat()
    new_settings = current.get("settings") if isinstance(current.get("settings"), dict) else {}
    if isinstance(settings, dict):
        merged = dict(new_settings)
        merged.update(settings)
        new_settings = merged
    payload = {
        "id": container_id,
        "name": (name.strip() if isinstance(name, str) and name.strip() else current.get("name")),
        "description": (description.strip() if isinstance(description, str) else current.get("description", "")),
        "owner_phone": current.get("owner_phone", ""),
        "visibility": (visibility.strip().lower() if isinstance(visibility, str) and visibility.strip() else current.get("visibility", "private")),
        "settings": json.dumps(new_settings or {}, ensure_ascii=False),
        "created_at": current.get("created_at"),
        "updated_at": now,
    }
    return bool(supabase_upsert("video_containers", payload, "id", lambda data: _local_upsert_by_id(VIDEO_CONTAINERS_LOCAL_PATH, data)))


def delete_video_container(container_id: str) -> bool:
    if not container_id:
        return False

    def _fallback_local(cid: str):
        _local_delete_by_id(VIDEO_CONTAINERS_LOCAL_PATH, cid)
        vids = _load_local_list(VIDEO_CONTAINER_VIDEOS_LOCAL_PATH)
        vids = [x for x in vids if x.get("container_id") != cid]
        _save_local_list(VIDEO_CONTAINER_VIDEOS_LOCAL_PATH, vids)

    return bool(supabase_delete("video_containers", "id", container_id, _fallback_local))


def add_video_to_container(container_id: str, file_path: str, *, caption: str = "", uploader_phone: str = "") -> Optional[Dict[str, Any]]:
    if not (container_id and file_path and os.path.exists(file_path)):
        return None
    import uuid
    import shutil
    vid_id = str(uuid.uuid4())
    ext = Path(file_path).suffix or ".mp4"
    title = (caption or "").strip() or Path(file_path).stem
    object_path = f"containers/{container_id}/{vid_id}{ext}"

    storage_provider = "local"
    storage_bucket = None
    storage_path = None
    local_target = str((VIDEO_CONTAINER_FILES_DIR / container_id / f"{vid_id}{ext}").resolve())

    try:
        Path(local_target).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    uploaded_key = supabase_storage_upload(VIDEO_CONTAINER_BUCKET, object_path, file_path, content_type="video/mp4", upsert=True)
    if uploaded_key:
        storage_provider = "supabase"
        storage_bucket = VIDEO_CONTAINER_BUCKET
        storage_path = uploaded_key
    else:
        try:
            shutil.copy2(file_path, local_target)
        except Exception:
            return None

    now = datetime.now().isoformat()
    payload = {
        "id": vid_id,
        "container_id": container_id,
        "title": title,
        "original_name": os.path.basename(file_path),
        "storage_provider": storage_provider,
        "storage_bucket": storage_bucket,
        "storage_path": storage_path,
        "local_path": local_target if storage_provider == "local" else None,
        "uploader_phone": (uploader_phone or "").strip(),
        "created_at": now,
    }
    supabase_upsert("video_container_videos", payload, "id", lambda data: _local_upsert_by_id(VIDEO_CONTAINER_VIDEOS_LOCAL_PATH, data))
    return payload


def list_container_videos(container_id: str) -> List[Dict[str, Any]]:
    if not container_id:
        return []

    def _fallback():
        return [x for x in _load_local_list(VIDEO_CONTAINER_VIDEOS_LOCAL_PATH) if x.get("container_id") == container_id]

    rows = supabase_select("video_container_videos", {"container_id": container_id}, _fallback) or []
    out = [dict(r) for r in rows]
    out.sort(key=lambda x: (x.get("created_at") or ""), reverse=True)
    return out


def get_container_video(video_id: str) -> Optional[Dict[str, Any]]:
    if not video_id:
        return None

    def _fallback():
        for x in _load_local_list(VIDEO_CONTAINER_VIDEOS_LOCAL_PATH):
            if x.get("id") == video_id:
                return [x]
        return []

    row = supabase_select_one("video_container_videos", "id", video_id, _fallback)
    return dict(row) if row else None


def download_container_video_to_file(video_id: str, dest_path: str) -> bool:
    row = get_container_video(video_id)
    if not row:
        return False
    provider = (row.get("storage_provider") or "local").strip().lower()
    if provider == "supabase":
        bucket = row.get("storage_bucket") or VIDEO_CONTAINER_BUCKET
        obj = row.get("storage_path")
        ok = bool(supabase_storage_download_to_file(bucket, obj, dest_path))
        if ok:
            return True
        local_path = row.get("local_path")
        if local_path and os.path.exists(local_path):
            try:
                import shutil
                Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, dest_path)
                return True
            except Exception:
                return False
        return False
    local_path = row.get("local_path")
    mapped_local = _map_android_storage_path_to_local(local_path) if local_path else None
    local_path_effective = None
    if local_path and os.path.exists(local_path):
        local_path_effective = local_path
    elif mapped_local and os.path.exists(mapped_local):
        local_path_effective = mapped_local

    if local_path_effective:
        try:
            import shutil
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path_effective, dest_path)
            return True
        except Exception:
            return False
    bucket = row.get("storage_bucket") or VIDEO_CONTAINER_BUCKET
    obj = row.get("storage_path")
    if obj:
        return bool(supabase_storage_download_to_file(bucket, obj, dest_path))
    return False


# ========== Facecam Storage (Supabase) ==========

FACECAM_STORAGE_BUCKET = (os.environ.get("FACECAM_STORAGE_BUCKET") or "facecam_videos").strip() or "facecam_videos"
FACECAM_STORAGE_LOCAL_PATH = _project_data_path("facecam_storage_index.json")


def upload_facecam_to_storage(source_id: str, clip_id: str, file_path: str) -> Optional[Dict[str, str]]:
    """رفع فيديو الفيس كام إلى Supabase Storage وإرجاع معلومات التخزين"""
    if not (source_id and clip_id and file_path and os.path.exists(file_path)):
        return None
    ext = Path(file_path).suffix or ".mp4"
    object_path = f"facecam/{source_id}/{clip_id}{ext}"
    
    content_type = "video/mp4"
    if ext.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        content_type = f"image/{ext.lower().lstrip('.')}"
    
    uploaded_key = supabase_storage_upload(FACECAM_STORAGE_BUCKET, object_path, file_path, content_type=content_type, upsert=True)
    now_iso = datetime.now().isoformat()
    record = {
        "id": clip_id,
        "source_id": source_id,
        "storage_bucket": FACECAM_STORAGE_BUCKET,
        "storage_path": uploaded_key,
        "local_path": os.path.abspath(file_path),
        "created_at": now_iso,
    }
    supabase_upsert("facecam_storage", record, "id", lambda data: _local_upsert_by_id(FACECAM_STORAGE_LOCAL_PATH, data))

    # نعيد معلومات حتى في حالة local-only لضمان استمرار عمل fallback.
    return {
        "storage_bucket": FACECAM_STORAGE_BUCKET,
        "storage_path": uploaded_key or "",
    }


def download_facecam_from_storage(clip_id: str, dest_path: str) -> bool:
    """تحميل فيديو الفيس كام من Supabase Storage"""
    def _fallback():
        for x in _load_local_list(FACECAM_STORAGE_LOCAL_PATH):
            if x.get("id") == clip_id:
                return [x]
        return []
    
    row = supabase_select_one("facecam_storage", "id", clip_id, _fallback)
    if not row:
        return False
    
    bucket = row.get("storage_bucket") or FACECAM_STORAGE_BUCKET
    obj = row.get("storage_path")
    if obj:
        ok = bool(supabase_storage_download_to_file(bucket, obj, dest_path))
        if ok:
            return True
    
    local_path = row.get("local_path")
    if local_path and os.path.exists(local_path):
        try:
            import shutil
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest_path)
            return True
        except Exception:
            return False

    # Fallbackات محلية إضافية عند فقد local_path في الفهرس
    try:
        import shutil

        candidates = []
        facecam_dir = _project_data_path("facecam")
        if facecam_dir.exists():
            candidates.extend([str(p) for p in facecam_dir.glob(f"{clip_id}.*")])

        sources_root = _project_data_path("facecam_sources")
        if sources_root.exists():
            for src_dir in sources_root.iterdir():
                if src_dir.is_dir():
                    candidates.extend([str(p) for p in src_dir.glob(f"{clip_id}.*")])

        for cand in candidates:
            if os.path.isfile(cand):
                Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cand, dest_path)
                return True
    except Exception:
        pass

    return False


def delete_facecam_from_storage(clip_id: str) -> bool:
    """حذف فيديو الفيس كام من Supabase Storage وقاعدة البيانات"""
    def _fallback_local(cid: str):
        _local_delete_by_id(FACECAM_STORAGE_LOCAL_PATH, cid)
    
    def _fallback():
        for x in _load_local_list(FACECAM_STORAGE_LOCAL_PATH):
            if x.get("id") == clip_id:
                return [x]
        return []
    
    row = supabase_select_one("facecam_storage", "id", clip_id, _fallback)
    if row:
        bucket = row.get("storage_bucket") or FACECAM_STORAGE_BUCKET
        obj = row.get("storage_path")
        if obj:
            supabase_storage_delete(bucket, obj)
    
    return bool(supabase_delete("facecam_storage", "id", clip_id, _fallback_local))


def delete_all_facecam_for_source(source_id: str) -> int:
    """حذف جميع فيديوهات الفيس كام المرتبطة بمصدر معين"""
    if not source_id:
        return 0
    
    def _fallback():
        return [x for x in _load_local_list(FACECAM_STORAGE_LOCAL_PATH) if x.get("source_id") == source_id]
    
    rows = supabase_select("facecam_storage", {"source_id": source_id}, _fallback) or []
    count = 0
    for row in rows:
        clip_id = row.get("id")
        if clip_id:
            bucket = row.get("storage_bucket") or FACECAM_STORAGE_BUCKET
            obj = row.get("storage_path")
            if obj:
                supabase_storage_delete(bucket, obj)
            supabase_delete("facecam_storage", "id", clip_id, lambda cid: _local_delete_by_id(FACECAM_STORAGE_LOCAL_PATH, cid))
            count += 1
    
    if not rows:
        local_items = _load_local_list(FACECAM_STORAGE_LOCAL_PATH)
        remaining = []
        for x in local_items:
            if x.get("source_id") == source_id:
                count += 1
            else:
                remaining.append(x)
        if count > 0:
            _save_local_list(FACECAM_STORAGE_LOCAL_PATH, remaining)
    
    return count


def get_facecam_storage_info(clip_id: str) -> Optional[Dict[str, Any]]:
    """الحصول على معلومات تخزين فيديو الفيس كام"""
    def _fallback():
        for x in _load_local_list(FACECAM_STORAGE_LOCAL_PATH):
            if x.get("id") == clip_id:
                return [x]
        return []
    
    row = supabase_select_one("facecam_storage", "id", clip_id, _fallback)
    return dict(row) if row else None


if __name__ == "__main__":
    import asyncio
    
    print("🔍 اختبار طبقة التخزين...")
    
    # اختبار التحميل
    state = load_bot_state()
    print(f"📊 تم تحميل الحالة: {len(state)} حقل")
    
    channels = list_channel_configs()
    print(f"📺 عدد القنوات: {len(channels)}")
    
    # اختبار المزامنة
    asyncio.run(full_sync_local_to_supabase())
