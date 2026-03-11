import json
import os
import tempfile
import threading
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import hashlib

from ..agent.config import Config, load_config, ensure_dirs, ensure_channels_file
from ..agent.sqlite_storage import load_state as sqlite_load_state
from ..agent.sqlite_storage import save_state as sqlite_save_state
from ..utils.resilient_fs import ResilientFS

# Supabase integration
try:
    from ..agent.supabase_storage import (
        load_bot_state as supabase_load_state,
        save_bot_state as supabase_save_state,
        full_sync_local_to_supabase
    )
    from ..agent.supabase_client import (
        USE_SUPABASE,
        is_online as supabase_is_online,
        start_background_sync
    )
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    USE_SUPABASE = False

logger = logging.getLogger(__name__)

STATE_PATH_DEFAULT = ".data/tg_state.json"
RAW_REVIEW_SKIP_COOLDOWN_SECONDS = 24 * 60 * 60


_STATE_LOCK = threading.RLock()
_SYNC_STARTED = False
_CACHED_STATE: Dict[str, Any] | None = None
_CACHED_STATE_TS: float = 0.0
_CACHED_STATE_KEY: str | None = None
_CACHED_STATE_TTL_SEC: float | None = None
_CRITICAL_STATE_HASH: str | None = None
_LAST_SUPABASE_SAVE_TS: float = 0.0


def _supabase_primary_storage() -> bool:
    """If enabled, Supabase is the primary storage and local persistence is used only as fallback."""
    val = (os.environ.get("SUPABASE_PRIMARY_STORAGE") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _state_cache_ttl_sec() -> float:
    global _CACHED_STATE_TTL_SEC
    if _CACHED_STATE_TTL_SEC is not None:
        return _CACHED_STATE_TTL_SEC
    raw = (os.environ.get("SUPABASE_STATE_CACHE_TTL_SEC") or "").strip()
    try:
        ttl = float(raw) if raw else 120.0
    except Exception:
        ttl = 120.0
    _CACHED_STATE_TTL_SEC = max(60.0, ttl)
    return _CACHED_STATE_TTL_SEC


def _state_path(cfg: Config) -> str:
    # نستخدم مسار JSON مشتق من ملف قاعدة تيليجرام نفسه لمنع تلوث الحالة بين البيئات
    db_path = Path(cfg.TELEGRAM_DB_PATH)
    base = str(db_path.parent) or ".data"
    ResilientFS.makedirs(base, exist_ok=True)
    stem = db_path.stem or "tg_state"
    return os.path.join(base, f"{stem}.json")


def _legacy_state_path(cfg: Config) -> str:
    base = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"
    return os.path.join(base, "tg_state.json")


def _should_read_legacy_state_path(cfg: Config) -> bool:
    try:
        name = Path(cfg.TELEGRAM_DB_PATH).name.lower()
    except Exception:
        name = os.path.basename(str(getattr(cfg, "TELEGRAM_DB_PATH", "") or "")).lower()
    return name in {"tg_state.db", "bot_state.db"}


def _sqlite_state_path(cfg: Config) -> Path:
    return Path(cfg.TELEGRAM_DB_PATH)


def _state_cache_key(cfg: Config) -> str:
    try:
        return str(_sqlite_state_path(cfg).resolve())
    except Exception:
        return str(_sqlite_state_path(cfg))


def _ensure_background_sync():
    """بدء المزامنة الخلفية مرة واحدة فقط"""
    global _SYNC_STARTED
    if not _SYNC_STARTED and SUPABASE_AVAILABLE and USE_SUPABASE:
        try:
            start_background_sync(interval_seconds=60)
            _SYNC_STARTED = True
            logger.info("🔄 تم بدء المزامنة الخلفية مع Supabase")
        except Exception as e:
            logger.warning(f"⚠️ فشل بدء المزامنة الخلفية: {e}")


def _min_supabase_save_interval_sec() -> float:
    raw = (os.environ.get("SUPABASE_MIN_SAVE_INTERVAL_SEC") or "").strip()
    try:
        val = float(raw) if raw else 60.0
    except Exception:
        val = 60.0
    return max(5.0, val)


def _critical_subset(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    تحديد البيانات الحرجة التي يجب حفظها في قاعدة البيانات.
    البيانات غير المهمة (مثل awaiting, last_output, telegram_notifications) يتم حفظها محلياً فقط.
    """
    keys = {
        # بيانات القنوات (مهمة جداً)
        "channels",
        "enabled_channels",
        "publish_channels",

        # إعدادات النشر (مهمة)
        "quality",
        "pip",
        "schedule",
        "conditions",
        "proxy",

        # حالة النشر (مهمة)
        "agent",
        "publishing_lock",
        "pending_videos",
        "published_by_channel",
        "publishing_inflight",
        "raw_review",
        "ai",
        "enhance",

        # بيانات FaceCam (مهمة)
        "facecam_clips_by_channel",

        # تم استبعاد البيانات غير المهمة (تُحفظ محلياً فقط):
        # - awaiting: حالة انتظار المستخدم (مؤقتة)
        # - last_output: آخر مخرجات (مؤقتة)
        # - telegram_notifications: إشعارات تيليجرام (مؤقتة)
        # - facecam_missing_notified: إشعارات FaceCam (مؤقتة)
    }
    out: Dict[str, Any] = {}
    for k in keys:
        if k in state:
            out[k] = state.get(k)
    return out


def _stable_hash(data: Dict[str, Any]) -> str:
    try:
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = str(data)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def load_state(cfg: Config | None = None) -> Dict[str, Any]:
    global _CACHED_STATE, _CACHED_STATE_TS, _CACHED_STATE_KEY
    cfg = cfg or load_config()
    path = _state_path(cfg)
    legacy_path = _legacy_state_path(cfg)
    cache_key = _state_cache_key(cfg)
    
    # بدء المزامنة الخلفية
    _ensure_background_sync()
    
    with _STATE_LOCK:
        ttl = _state_cache_ttl_sec()
        now = time.time()
        if (
            ttl > 0
            and _CACHED_STATE is not None
            and _CACHED_STATE_KEY == cache_key
            and (now - _CACHED_STATE_TS) < ttl
        ):
            try:
                return json.loads(json.dumps(_CACHED_STATE))
            except Exception:
                return dict(_CACHED_STATE)

        # محاولة التحميل من Supabase أولاً
        if SUPABASE_AVAILABLE and USE_SUPABASE:
            try:
                if supabase_is_online():
                    state = supabase_load_state()
                    if state and len(state) > 5:  # تأكد أن الحالة ليست فارغة
                        logger.debug("✅ تم تحميل الحالة من Supabase")
                        _ensure_state_fields(state, cfg)
                        try:
                            _CACHED_STATE = dict(state)
                            _CACHED_STATE_TS = time.time()
                            _CACHED_STATE_KEY = cache_key
                        except Exception:
                            pass
                        return state
            except Exception as e:
                logger.warning(f"⚠️ فشل التحميل من Supabase، استخدام المحلي: {e}")

        # محاولة التحميل من SQLite المحلي
        try:
            state = sqlite_load_state(_sqlite_state_path(cfg))
            if state:
                _ensure_state_fields(state, cfg)
                try:
                    _CACHED_STATE = dict(state)
                    _CACHED_STATE_TS = time.time()
                    _CACHED_STATE_KEY = cache_key
                except Exception:
                    pass
                return state
        except Exception as e:
            logger.warning(f"⚠️ فشل التحميل من SQLite: {e}")
        
        # Fallback: تحميل من الملف المحلي
        json_path = path
        if not ResilientFS.exists(json_path) and _should_read_legacy_state_path(cfg):
            json_path = legacy_path
        if not ResilientFS.exists(json_path):
            default_state = {
                "channels": [],
                "enabled_channels": {},  # قاموس لتمكين/تعطيل القنوات
                "publish_channels": [],  # قنوات النشر المصادق عليها
                "mode": cfg.AUDIO_MODE or "light",
                "quality": {"resolution": "720p", "fps": 30, "crf": 23, "preset": "medium"},
                "pip": {"policy": "random", "override_file": None},
                "schedule": {"daily": cfg.RUN_DAILY_AT},
                "conditions": {"wifi": cfg.RUN_ONLY_ON_WIFI, "charging": cfg.RUN_ONLY_WHILE_CHARGING},
                "proxy": {"url": None},
                "title_style": None,
                "hashtags_policy": None,
                "awaiting": {},  # لكل مستخدم: {type: str}
                "last_output": None,
                "pending_videos": [],
                "facecam_clips": [],
                "facecam_clips_by_channel": {},
                "agent": {"auto_publish_enabled": True},
                "publishing_lock": {"active": False, "until": None, "kind": None, "id": None},
                "raw_review": {"pending": {}, "approved": {}, "blocked": {}, "skipped": {}},
                "ai": {"backoff_until": None, "provider_order": "smart"},
            }
            # محاولة الكشف التلقائي عن القنوات المصادق عليها
            _detect_and_add_publish_channels(default_state, cfg)
            try:
                _CACHED_STATE = dict(default_state)
                _CACHED_STATE_TS = time.time()
                _CACHED_STATE_KEY = cache_key
            except Exception:
                pass
            return default_state
        
        try:
            with ResilientFS.open(json_path, "r", encoding="utf-8") as f:
                state = json.load(f)
                _ensure_state_fields(state, cfg)
                try:
                    _CACHED_STATE = dict(state)
                    _CACHED_STATE_TS = time.time()
                    _CACHED_STATE_KEY = cache_key
                except Exception:
                    pass
                return state
        except Exception:
            return {}


def _ensure_state_fields(state: Dict[str, Any], cfg: Config) -> None:
    """التأكد من وجود جميع الحقول المطلوبة"""
    if "publish_channels" not in state:
        state["publish_channels"] = []
        _detect_and_add_publish_channels(state, cfg)
    if "pending_videos" not in state:
        state["pending_videos"] = []
    if "facecam_clips" not in state:
        state["facecam_clips"] = []
    if "facecam_clips_by_channel" not in state:
        state["facecam_clips_by_channel"] = {}
    if "agent" not in state:
        state["agent"] = {"auto_publish_enabled": True}
    if "auto_publish_enabled" not in (state.get("agent") or {}):
        state.setdefault("agent", {})["auto_publish_enabled"] = True
    if "telegram_notifications" not in state:
        state["telegram_notifications"] = []
    if "publishing_lock" not in state:
        state["publishing_lock"] = {"active": False, "until": None, "kind": None, "id": None}
    raw_review = state.get("raw_review")
    if not isinstance(raw_review, dict):
        raw_review = {}
    for key in ("pending", "approved", "blocked", "skipped"):
        if not isinstance(raw_review.get(key), dict):
            raw_review[key] = {}
    now_ts = time.time()
    expired_skip_keys = []
    for key, entry in (raw_review.get("skipped") or {}).items():
        try:
            if float((entry or {}).get("skip_until_ts") or 0) <= now_ts:
                expired_skip_keys.append(key)
        except Exception:
            expired_skip_keys.append(key)
    for key in expired_skip_keys:
        raw_review["skipped"].pop(key, None)
    state["raw_review"] = raw_review
    if "ai" not in state:
        state["ai"] = {"backoff_until": None, "provider_order": "smart"}
    
    # تحرير القفل إذا انتهت صلاحيته
    try:
        lock = (state.get("publishing_lock") or {})
        if isinstance(lock, dict) and lock.get("active"):
            until = lock.get("until")
            if until is not None and float(until) < time.time():
                state["publishing_lock"] = {"active": False, "until": None, "kind": None, "id": None}
    except Exception:
        pass


def save_state(state: Dict[str, Any], cfg: Config | None = None) -> None:
    global _CACHED_STATE, _CACHED_STATE_TS, _CACHED_STATE_KEY, _CRITICAL_STATE_HASH, _LAST_SUPABASE_SAVE_TS
    cfg = cfg or load_config()
    path = _state_path(cfg)
    base_dir = os.path.dirname(path) or "."
    ResilientFS.makedirs(base_dir, exist_ok=True)
    
    with _STATE_LOCK:
        try:
            _CACHED_STATE = dict(state)
            _CACHED_STATE_TS = time.time()
            _CACHED_STATE_KEY = _state_cache_key(cfg)
        except Exception:
            pass

        local_failed = False
        primary = _supabase_primary_storage()

        # Primary mode: try Supabase first; only persist locally on failure/offline.
        if primary and SUPABASE_AVAILABLE and USE_SUPABASE:
            try:
                if supabase_is_online():
                    supabase_save_state(state)
                    _CRITICAL_STATE_HASH = _stable_hash(_critical_subset(state))
                    _LAST_SUPABASE_SAVE_TS = time.time()
                else:
                    local_failed = True
            except Exception as e:
                local_failed = True
                logger.warning(f"⚠️ فشل الحفظ في Supabase (سيتم استخدام fallback محلي): {e}")

        # Local persistence (SQLite + JSON):
        # - Always in legacy mode
        # - In primary mode only when Supabase failed/offline
        if (not primary) or local_failed:
            local_failed = False
            try:
                sqlite_save_state(state, _sqlite_state_path(cfg))
            except Exception as e:
                local_failed = True
                logger.warning(f"⚠️ فشل حفظ الحالة في SQLite: {e}")

            try:
                # محاولة الحفظ الآمن باستخدام ملف مؤقت
                tmp_path = os.path.join(base_dir, f"tg_state_{int(time.time())}.tmp")
                try:
                    with ResilientFS.open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    
                    # استبدال الملف القديم بالمؤقت
                    def _safe_replace(src, dst):
                        if os.path.exists(dst):
                            try:
                                os.remove(dst)
                            except:
                                pass
                        os.rename(src, dst)
                    
                    ResilientFS.run(_safe_replace, tmp_path, path)
                finally:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except:
                            pass
            except Exception as e:
                local_failed = True
                logger.warning(f"⚠️ فشل حفظ الحالة في ملف JSON: {e}")

        # سياسة احترافية: نحفظ في Supabase فقط عند الضرورة:
        # - إذا فشل الحفظ المحلي (Fallback)
        # - أو إذا تغيرت البيانات الحرجة مع احترام حد أدنى للفاصل الزمني لتقليل الضغط
        if SUPABASE_AVAILABLE and USE_SUPABASE and (not primary):
            now = time.time()
            critical_hash = _stable_hash(_critical_subset(state))
            critical_changed = (_CRITICAL_STATE_HASH != critical_hash)
            interval_ok = (now - _LAST_SUPABASE_SAVE_TS) >= _min_supabase_save_interval_sec()
            force_all = (os.environ.get("SUPABASE_FORCE_SAVE_ALL") or "").strip().lower() in {"1", "true", "yes", "on"}

            should_save_supabase = force_all or local_failed or (critical_changed and interval_ok)

            if should_save_supabase:
                try:
                    supabase_save_state(state)
                    _CRITICAL_STATE_HASH = critical_hash
                    _LAST_SUPABASE_SAVE_TS = now
                    logger.debug("✅ تم حفظ الحالة في Supabase")
                except Exception as e:
                    logger.warning(f"⚠️ فشل الحفظ في Supabase (سيتم المزامنة لاحقاً): {e}")
            elif critical_changed:
                _CRITICAL_STATE_HASH = critical_hash


def update_state(cfg: Config | None, updater):
    cfg = cfg or load_config()
    with _STATE_LOCK:
        st = load_state(cfg)
        updater(st)
        save_state(st, cfg)
        return st


def set_awaiting(user_id: int, kind: str | None, cfg: Config | None = None) -> None:
    state = load_state(cfg)
    if kind:
        state.setdefault("awaiting", {})[str(user_id)] = {"type": kind}
    else:
        if str(user_id) in state.get("awaiting", {}):
            del state["awaiting"][str(user_id)]
    save_state(state, cfg)


def get_awaiting(user_id: int, cfg: Config | None = None) -> str | None:
    state = load_state(cfg)
    ent = state.get("awaiting", {}).get(str(user_id))
    return (ent or {}).get("type")


def _raw_review_video_key(source_id: str, video_id: str) -> str:
    return f"{str(source_id)}::{str(video_id)}"


def get_pending_raw_review(source_id: str, cfg: Config | None = None) -> Optional[Dict[str, Any]]:
    state = load_state(cfg)
    pending = ((state.get("raw_review") or {}).get("pending") or {}).get(str(source_id))
    return dict(pending) if isinstance(pending, dict) else None


def has_pending_raw_reviews(cfg: Config | None = None) -> bool:
    state = load_state(cfg)
    pending = ((state.get("raw_review") or {}).get("pending") or {})
    return any(isinstance(entry, dict) and entry for entry in pending.values())


def set_pending_raw_review(source_id: str, entry: Dict[str, Any], cfg: Config | None = None) -> Dict[str, Any]:
    payload = dict(entry or {})

    def _updater(state: Dict[str, Any]) -> None:
        _ensure_state_fields(state, cfg or load_config())
        state.setdefault("raw_review", {}).setdefault("pending", {})[str(source_id)] = payload

    update_state(cfg, _updater)
    return payload


def clear_pending_raw_review(source_id: str, cfg: Config | None = None) -> None:
    def _updater(state: Dict[str, Any]) -> None:
        _ensure_state_fields(state, cfg or load_config())
        state.setdefault("raw_review", {}).setdefault("pending", {}).pop(str(source_id), None)

    update_state(cfg, _updater)


def find_pending_raw_review_by_token(token: str, cfg: Config | None = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    state = load_state(cfg)
    pending = ((state.get("raw_review") or {}).get("pending") or {})
    for source_id, entry in pending.items():
        if isinstance(entry, dict) and str(entry.get("token") or "") == str(token or ""):
            return str(source_id), dict(entry)
    return None, None


def is_raw_review_approved(source_id: str, video_id: str, cfg: Config | None = None) -> bool:
    state = load_state(cfg)
    key = _raw_review_video_key(source_id, video_id)
    approved = ((state.get("raw_review") or {}).get("approved") or {})
    return key in approved


def is_raw_review_blocked(source_id: str, video_id: str, cfg: Config | None = None) -> bool:
    state = load_state(cfg)
    key = _raw_review_video_key(source_id, video_id)
    blocked = ((state.get("raw_review") or {}).get("blocked") or {})
    return key in blocked


def is_raw_review_skip_active(source_id: str, video_id: str, cfg: Config | None = None) -> bool:
    state = load_state(cfg)
    key = _raw_review_video_key(source_id, video_id)
    skipped = ((state.get("raw_review") or {}).get("skipped") or {})
    entry = skipped.get(key)
    if not isinstance(entry, dict):
        return False
    try:
        if float(entry.get("skip_until_ts") or 0) > time.time():
            return True
    except Exception:
        pass

    def _updater(st: Dict[str, Any]) -> None:
        _ensure_state_fields(st, cfg or load_config())
        st.setdefault("raw_review", {}).setdefault("skipped", {}).pop(key, None)

    update_state(cfg, _updater)
    return False


def _decide_pending_raw_review(
    token: str,
    *,
    decision: str,
    decided_by: Optional[int] = None,
    cfg: Config | None = None,
    skip_cooldown_seconds: int = RAW_REVIEW_SKIP_COOLDOWN_SECONDS,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    result: Dict[str, Any] = {}

    def _updater(state: Dict[str, Any]) -> None:
        _ensure_state_fields(state, cfg or load_config())
        raw_review = state.setdefault("raw_review", {})
        pending = raw_review.setdefault("pending", {})

        found_source_id = None
        found_entry = None
        for source_id, entry in pending.items():
            if isinstance(entry, dict) and str(entry.get("token") or "") == str(token or ""):
                found_source_id = str(source_id)
                found_entry = dict(entry)
                break

        if not found_source_id or not found_entry:
            return

        pending.pop(found_source_id, None)
        video_id = str(found_entry.get("video_id") or "")
        key = _raw_review_video_key(found_source_id, video_id)
        for bucket in ("approved", "blocked", "skipped"):
            raw_review.setdefault(bucket, {}).pop(key, None)

        payload = dict(found_entry)
        payload["decision"] = decision
        payload["decided_at"] = datetime.now(timezone.utc).isoformat()
        if decided_by is not None:
            payload["decided_by"] = int(decided_by)

        if decision == "skipped":
            payload["skip_until_ts"] = time.time() + max(60, int(skip_cooldown_seconds or 60))
            raw_review.setdefault("skipped", {})[key] = payload
        elif decision == "blocked":
            raw_review.setdefault("blocked", {})[key] = payload
        elif decision == "approved":
            raw_review.setdefault("approved", {})[key] = payload

        result["source_id"] = found_source_id
        result["entry"] = payload

    update_state(cfg, _updater)
    return result.get("entry"), result.get("source_id")


def approve_pending_raw_review(token: str, decided_by: Optional[int] = None, cfg: Config | None = None):
    return _decide_pending_raw_review(token, decision="approved", decided_by=decided_by, cfg=cfg)


def skip_pending_raw_review(
    token: str,
    decided_by: Optional[int] = None,
    cfg: Config | None = None,
    skip_cooldown_seconds: int = RAW_REVIEW_SKIP_COOLDOWN_SECONDS,
):
    return _decide_pending_raw_review(
        token,
        decision="skipped",
        decided_by=decided_by,
        cfg=cfg,
        skip_cooldown_seconds=skip_cooldown_seconds,
    )


def block_pending_raw_review(token: str, decided_by: Optional[int] = None, cfg: Config | None = None):
    return _decide_pending_raw_review(token, decision="blocked", decided_by=decided_by, cfg=cfg)


def _detect_and_add_publish_channels(state: Dict[str, Any], cfg: Config) -> None:
    """الكشف التلقائي عن القنوات المصادق عليها وإضافتها إلى الحالة"""
    try:
        from ..agent.uploader import get_credentials, _get_channel_info
        
        # التحقق من وجود توكن صالح
        creds = get_credentials(cfg)
        if creds and creds.valid:
            # الحصول على معلومات القناة
            channel_id, channel_title = _get_channel_info(creds)
            
            # التحقق من أن القناة غير موجودة مسبقاً
            existing_channels = state.get("publish_channels", [])
            if not any(ch.get("channel_id") == channel_id for ch in existing_channels):
                # إضافة القناة المصادق عليها
                channel_info = {
                    "channel_id": channel_id,
                    "title": channel_title,
                    "enabled": True,
                    "token_path": os.path.join(base_dir, "youtube_token.json"),
                    "added_date": str(os.path.getmtime(os.path.join(base_dir, "youtube_token.json"))) if os.path.exists(os.path.join(base_dir, "youtube_token.json")) else ""
                }
                existing_channels.append(channel_info)
                state["publish_channels"] = existing_channels
                
                # حفظ الحالة المحدثة
                save_state(state, cfg)
                
                # تسجيل المعلومات
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"تم الكشف التلقائي عن قناة YouTube مصادق عليها: {channel_title} ({channel_id})")
                
        else:
            # التوكن غير صالح أو غير موجود
            import logging
            logger = logging.getLogger(__name__)
            logger.info("لا يوجد توكن YouTube صالح للكشف التلقائي عن القنوات")
            
    except Exception as e:
        # تسجيل الخطأ ولكن لا إيقاف التشغيل
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"فشل الكشف التلقائي عن القنوات المصادق عليها: {e}")
        logger.debug(f"تفاصيل الخطأ: {type(e).__name__}: {e}")
