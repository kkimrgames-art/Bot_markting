"""
YouTube API Key Manager — نظام إدارة مفاتيح YouTube Data API v3
يدعم مفاتيح متعددة مع تتبع الحصص والتدوير التلقائي
"""
import os
import json
import time
import hashlib
import logging
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import get_project_root

logger = logging.getLogger(__name__)

# ========== ثوابت YouTube API ==========
YOUTUBE_DAILY_QUOTA = 10_000  # حصة يومية مجانية لكل مفتاح
SEARCH_COST = 100             # تكلفة search.list
VIDEO_LIST_COST = 1           # تكلفة videos.list (per item)
CHANNEL_LIST_COST = 1         # تكلفة channels.list

# ملف التخزين المحلي (fallback)
_LOCAL_KEYS_PATH = Path(get_project_root()) / ".data" / "youtube_api_keys.json"
_LOCAL_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()

# === Supabase table name ===
_TABLE_NAME = "youtube_api_keys"


class YouTubeAPIKeyManager:
    """
    مدير مفاتيح YouTube API المتقدم:
    - تخزين في Supabase مع fallback محلي
    - تتبع الحصة (quota) لكل مفتاح
    - تدوير تلقائي عند نفاد الحصة
    - إعادة تعيين يومية (midnight Pacific Time)
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "YouTubeAPIKeyManager":
        """Singleton pattern"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._keys: List[Dict[str, Any]] = []
        self._loaded = False
        self._ensure_loaded()

    # ==================== تحميل وحفظ ====================

    def _ensure_loaded(self):
        """تحميل المفاتيح عند أول استخدام"""
        if self._loaded:
            return
        with _lock:
            if self._loaded:
                return
            self._load_keys()
            self._seed_from_env()
            self._loaded = True

    def _load_keys(self):
        """تحميل المفاتيح من Supabase أو المحلي"""
        try:
            from src.agent.supabase_client import supabase_select
            result = supabase_select(_TABLE_NAME)
            if result is not None:
                self._keys = result
                logger.info(f"🔑 Loaded {len(self._keys)} YouTube API keys from Supabase")
                self._save_local()
                return
        except Exception as e:
            logger.debug(f"Supabase load failed, using local: {e}")

        # Fallback: ملف محلي
        self._load_local()

    def _load_local(self):
        """تحميل من ملف محلي"""
        try:
            if _LOCAL_KEYS_PATH.exists():
                with open(_LOCAL_KEYS_PATH, "r", encoding="utf-8") as f:
                    self._keys = json.load(f)
                logger.info(f"🔑 Loaded {len(self._keys)} YouTube API keys from local storage")
        except Exception as e:
            logger.warning(f"Failed to load local keys: {e}")
            self._keys = []

    def _save_local(self):
        """حفظ في ملف محلي"""
        try:
            with open(_LOCAL_KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._keys, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save local keys: {e}")

    def _save_key_to_supabase(self, key_data: Dict):
        """حفظ/تحديث مفتاح في Supabase"""
        try:
            from src.agent.supabase_client import supabase_upsert
            supabase_upsert(_TABLE_NAME, key_data, key_field="key_id", on_conflict="key_id")
        except Exception as e:
            logger.debug(f"Supabase save failed: {e}")

    def _delete_from_supabase(self, key_id: str):
        """حذف مفتاح من Supabase"""
        try:
            from src.agent.supabase_client import supabase_delete
            supabase_delete(_TABLE_NAME, "key_id", key_id)
        except Exception as e:
            logger.debug(f"Supabase delete failed: {e}")

    def _seed_from_env(self):
        """إضافة مفتاح من متغير البيئة إذا لم يكن موجوداً"""
        env_key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
        if not env_key:
            return

        # تحقق إذا كان المفتاح موجوداً بالفعل
        for k in self._keys:
            if k.get("api_key") == env_key:
                return

        # إضافة تلقائية
        self.add_key(env_key, label="ENV (auto)", added_by="system")
        logger.info("🔑 Added YouTube API key from YOUTUBE_API_KEY env var")

    # ==================== إدارة المفاتيح ====================

    def add_key(self, api_key: str, label: str = "", added_by: str = "admin") -> Dict:
        """إضافة مفتاح جديد"""
        with _lock:
            # تحقق من عدم التكرار
            for k in self._keys:
                if k.get("api_key") == api_key:
                    return k  # موجود بالفعل

            key_id = hashlib.md5(api_key.encode()).hexdigest()[:12]
            now = datetime.now(timezone.utc).isoformat()

            key_data = {
                "key_id": key_id,
                "api_key": api_key,
                "label": label or f"Key-{key_id[:6]}",
                "quota_used": 0,
                "quota_limit": YOUTUBE_DAILY_QUOTA,
                "last_reset": now,
                "is_active": True,
                "added_by": added_by,
                "created_at": now,
            }

            self._keys.append(key_data)
            self._save_local()
            self._save_key_to_supabase(key_data)
            logger.info(f"🔑 Added YouTube API key: {label} ({key_id})")
            return key_data

    def remove_key(self, key_id: str) -> bool:
        """حذف مفتاح"""
        with _lock:
            before = len(self._keys)
            self._keys = [k for k in self._keys if k.get("key_id") != key_id]
            if len(self._keys) < before:
                self._save_local()
                self._delete_from_supabase(key_id)
                logger.info(f"🗑️ Removed YouTube API key: {key_id}")
                return True
            return False

    def list_keys(self) -> List[Dict]:
        """عرض جميع المفاتيح مع حالة الحصة"""
        self._ensure_loaded()
        self._auto_reset_quotas()
        return [
            {
                "key_id": k["key_id"],
                "label": k.get("label", ""),
                "api_key_masked": k["api_key"][:8] + "..." + k["api_key"][-4:],
                "quota_used": k.get("quota_used", 0),
                "quota_limit": k.get("quota_limit", YOUTUBE_DAILY_QUOTA),
                "quota_remaining": max(0, k.get("quota_limit", YOUTUBE_DAILY_QUOTA) - k.get("quota_used", 0)),
                "is_active": k.get("is_active", True),
                "added_by": k.get("added_by", ""),
                "created_at": k.get("created_at", ""),
            }
            for k in self._keys
        ]

    def get_total_quota_info(self) -> Dict:
        """معلومات الحصة الإجمالية"""
        self._auto_reset_quotas()
        active = [k for k in self._keys if k.get("is_active", True)]
        total_limit = sum(k.get("quota_limit", YOUTUBE_DAILY_QUOTA) for k in active)
        total_used = sum(k.get("quota_used", 0) for k in active)
        return {
            "total_keys": len(self._keys),
            "active_keys": len(active),
            "total_limit": total_limit,
            "total_used": total_used,
            "total_remaining": max(0, total_limit - total_used),
        }

    # ==================== تدوير المفاتيح ====================

    def get_active_key(self) -> Optional[str]:
        """
        الحصول على أفضل مفتاح متاح (الأقل استخداماً).
        يُعيد None إذا لم تتوفر مفاتيح أو كلها نفدت.
        """
        self._ensure_loaded()
        self._auto_reset_quotas()

        with _lock:
            active = [
                k for k in self._keys
                if k.get("is_active", True)
                and k.get("quota_used", 0) < k.get("quota_limit", YOUTUBE_DAILY_QUOTA)
            ]

            if not active:
                logger.warning(
                    "⚠️ [YouTube API] No active API keys available. "
                    "The bot is now forced to use scraping (yt-dlp) which is high-risk for rate-limiting. "
                    "Please add a YOTUBE_API_KEY to your .env file or via the Bot Settings for smoother operation."
                )
                return None

            # اختيار الأقل استخداماً
            best = min(active, key=lambda k: k.get("quota_used", 0))
            return best["api_key"]

    def record_usage(self, api_key: str, units: int = SEARCH_COST):
        """تسجيل استخدام الحصة"""
        with _lock:
            for k in self._keys:
                if k.get("api_key") == api_key:
                    k["quota_used"] = k.get("quota_used", 0) + units
                    self._save_local()
                    self._save_key_to_supabase(k)
                    remaining = k.get("quota_limit", YOUTUBE_DAILY_QUOTA) - k["quota_used"]
                    logger.debug(f"📊 API key {k.get('label', '?')}: used {units} units, {remaining} remaining")
                    return

    def report_quota_exceeded(self, api_key: str):
        """الإبلاغ عن نفاد حصة مفتاح"""
        with _lock:
            for k in self._keys:
                if k.get("api_key") == api_key:
                    k["quota_used"] = k.get("quota_limit", YOUTUBE_DAILY_QUOTA)
                    self._save_local()
                    self._save_key_to_supabase(k)
                    logger.warning(f"🚫 API key {k.get('label', '?')} quota exceeded — switching to next")
                    return

    # ==================== إعادة تعيين الحصة ====================

    def _auto_reset_quotas(self):
        """
        إعادة تعيين الحصة اليومية.
        YouTube يعيد التعيين عند منتصف الليل بتوقيت المحيط الهادئ (PT).
        """
        # PT = UTC-8 (or UTC-7 during DST)
        try:
            from zoneinfo import ZoneInfo
            pt_tz = ZoneInfo("America/Los_Angeles")
        except ImportError:
            # fallback
            pt_tz = timezone(timedelta(hours=-8))

        now_pt = datetime.now(pt_tz)
        today_midnight_pt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)

        with _lock:
            changed = False
            for k in self._keys:
                last_reset_str = k.get("last_reset", "")
                try:
                    last_reset = datetime.fromisoformat(last_reset_str)
                    if last_reset.tzinfo is None:
                        last_reset = last_reset.replace(tzinfo=timezone.utc)
                    last_reset_pt = last_reset.astimezone(pt_tz)
                except Exception:
                    last_reset_pt = today_midnight_pt - timedelta(days=1)

                # إذا كان آخر تعيين قبل منتصف ليلة اليوم
                if last_reset_pt < today_midnight_pt:
                    old_used = k.get("quota_used", 0)
                    k["quota_used"] = 0
                    k["last_reset"] = datetime.now(timezone.utc).isoformat()
                    changed = True
                    if old_used > 0:
                        logger.info(f"🔄 Reset quota for key {k.get('label', '?')}: {old_used} → 0")

            if changed:
                self._save_local()

    # ==================== Validation ====================

    def validate_key(self, api_key: str) -> bool:
        """التحقق من صلاحية مفتاح API عبر طلب بسيط"""
        try:
            import httpx
            resp = httpx.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "id", "q": "test", "maxResults": 1, "key": api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                return True
            elif resp.status_code == 403:
                data = resp.json()
                error_msg = data.get("error", {}).get("message", "")
                if "quota" in error_msg.lower():
                    # المفتاح صالح لكن الحصة نفدت
                    return True
                return False
            elif resp.status_code == 400:
                return False
            return False
        except Exception:
            return True  # عند فشل الشبكة نفترض صالح


def get_key_manager() -> YouTubeAPIKeyManager:
    """Helper function for easy access"""
    return YouTubeAPIKeyManager.get_instance()
