"""
نظام إدارة مفاتيح Gemini الاحترافي
يدير 70+ مفتاح مع مراعاة حدود الخطة المجانية
"""
import os
import random
import time
import logging
import tempfile
import threading
from typing import Optional, List
from datetime import datetime, timedelta
import json
from pathlib import Path

logger = logging.getLogger(__name__)


_KM_LOCK = threading.RLock()


class GeminiKeyManager:
    """مدير مفاتيح Gemini الاحترافي"""
    
    # جميع مفاتيح Gemini
    API_KEYS = [
        "AIzaSyD7E2B9iYXV9_2houLLHDWXUA-K53kUGq0",
        "AIzaSyBY58cZFvFzRQpzzYJ7m1VjwvS7af-vHhM",
        "AIzaSyBSbEdSARy5ims96kxF1om2725VZxwl6nU",
        "AIzaSyBx2D9UdlvuSCH4Z7jaT16S0MREkNIuVNc",
        "AIzaSyAuI06np4vmdKkWN1JucexLW1mO0ESEyts",
        "AIzaSyC4_fX42vOZYfuF56i_lNSJjiEs02vX3Uo",
        "AIzaSyDZfiLXBVs8yBk0CDb4hvLZ_l8P6tKy6og",
        "AIzaSyC_3oOs766IXUlWFdwUSNTVAhg_GLFfb1E",
        "AIzaSyA9sCmQsOZIPKRKX14aMJsC8Mt7IFPsYE8",
        "AIzaSyD_YyLNsLeKeFYUX7KvTw5fVJyneWMNjl8",
        "AIzaSyD1c3SzOGL4M8O1qZ6AejUy7jm567Nbf5w",
        "AIzaSyBSeMPla9eDR2VCmgS0fub-EujNsLO7EDk",
        "AIzaSyCxWJz_Cybce02eEXZrOnvZcEaD9yiFIQI",
        "AIzaSyAq6w_rvoIiZLxX191-UZDrhibbA6adDAw",
        "AIzaSyCzpJ4I958670qvycQgL9oay1Mpjp1q3zA",
        "AIzaSyCJajG5U6RH9KPMaZYXAcMGNldpOunf4nU",
        "AIzaSyDtfQhkANpIM-cHRy-UdMhHfROzSjBX5SY",
        "AIzaSyD-461tFFQsQcOxadaAQVWG1VN5ZvCxAYU",
        "AIzaSyAGoM8irNjhKPZcU8hBv1rpLpIhhy5eZ0E",
        "AIzaSyCyjFbianBAZ3eYOqIH2Yf0J70FUgWh5wg",
        "AIzaSyASQK3r61z8OKrW6WIru6DZt3T0NFwkPCI",
        "AIzaSyCxhQrKeoPrgZSQshH_Ij4sqLNxbjVuIDE",
        "AIzaSyCO9ok1iCqfFbtVyDJdVpK_JoLraNS6aHU",
        "AIzaSyAV3uTUtTKlSHgU3cge20vnQbusNeM-Wxk",
        "AIzaSyAnnYx15P89izzx_rJy9en4kaLVJ_Nuk40",
        "AIzaSyAhpl7qAaouwRb6niHi9UMNnOxQVzTZXeo",
        "AIzaSyCeSyykhVy6BXB_Do-V934k--VxwvdWAok",
        "AIzaSyDLlAe9XxHdbJ8affKNqWKD5509E0roQ2E",
        "AIzaSyC2Q_XqKuefQ0badweq13D5mjLVMhmA6BU",
        "AIzaSyA5W4wz0mpnajLAzHj3w_X_U9ACNr0Web4",
        "AIzaSyChdkhUNWaXlDDyGN7Zy-DEHOTHw79guH8",
        "AIzaSyB8ZRBRJ-ouTxBHVzOXZaCjD_z2PfZzYFE",
        "AIzaSyAMbRJtbmTa8xjGJuIX3wUcvIwSFuMB5gM",
        "AIzaSyAjHe3qCwNjcaEEie9ZC84gMIr2dy5NR28",
        "AIzaSyClKt_GqxphM-5aFYUMLzT925OgTtxxIhY",
        "AIzaSyBDASshsMMS7K17y9j6I0A7amdnOEJQ78Q",
        "AIzaSyC5F-PHaWU9yb4_mv_K7E6RbrhOJyYS5-U",
        "AIzaSyAERiSzXO9roZ49OLyd9FvhezU5u193c2g",
        "AIzaSyAQuKvRKMP48MB2RePpur8Wp6f5iYBpkaA",
        "AIzaSyBnxTW0uaIjIacKMHShqsjL_EMzk6NribQ",
        "AIzaSyDDK44eqIOF7rfz2RTscDRoAbk7IPwaKt8",
        "AIzaSyBIlkoOdvE7oCP8oJlXUrE2YcbFCp5Ioyo",
        "AIzaSyBELBROWW0EBVVmuTNvt32sD6hbj0Tq6MU",
        "AIzaSyBGYJPjJ5aycTP5i--CXPKXe53wuA1jHgE",
        "AIzaSyD9XM4KEqt_VmzBbLqCROtaydXICu7-ymw",
        "AIzaSyCulDVPsLQXChqpbYVERipHvdOGohhpMP0",
        "AIzaSyAybDJWXToHHzg9VUERbNwnGc7c6l6GNqE",
        "AIzaSyBOPeEmj0j2svyoAyqMwpFYr4Iy2qYliEk",
        "AIzaSyBstNKoy12AYAGy7rxfNK2x-wK-gvaVFEA",
        "AIzaSyC7Z3wBae-3J47R-LaPGnJGTrC22SvDg5M",
        "AIzaSyCQJ2CwE6_oSsBvzUs3TnaiHccXkjtgcWc",
        "AIzaSyBomvlphVTvUleoxzbJM8boL1KvkPuUZVc",
        "AIzaSyBlHvj5vAYWFD_Qs2RPlu5WBUIoPi4WuYQ",
        "AIzaSyArno9YCJbb7WfUG5llZgve644RIVRrntw",
        "AIzaSyBNR0YlKBy7UHf_9Gwd7hUtax41bqwAqX4",
        "AIzaSyA52Gg_lI3DN8sv0HykxPhLsmEYpCj6qlU",
        "AIzaSyBn2jVshv_jn60Yi_-UUmw9P31mDIs4w74",
        "AIzaSyAYgcYKr8ROUQjZdZwV9mn7K41gOtcsu1c",
        "AIzaSyDry4JMnQJwyGncKF9va6OC0LAX8b48WHs",
        "AIzaSyDFFCW_iKzlKrxEe4QARSzCL2nw0qsSrh8",
        "AIzaSyD8HVd2wuBDRnGDrKpobj_G2xRAHI6Z-88",
        "AIzaSyCwKaFunKbgO5FE8uEgqxbWgab8LcwBzvc",
        "AIzaSyBiMajBlgp6rC6tTvkAUa6s34EG4VeYZxk",
        "AIzaSyBSbT3AFUoZ4TulCuSgx8QLWp4N2qGe4vg",
        "AIzaSyDskR5hdZtbk8JOo4XjqZaj0F0i5bsJUYg",
    ]
    
    # حدود الخطة المجانية
    FREE_TIER_LIMITS = {
        "requests_per_minute": 15,  # 15 طلب في الدقيقة
        "requests_per_day": 1500,   # 1500 طلب في اليوم
        "tokens_per_minute": 1000000,  # 1M توكن في الدقيقة
    }
    
    def __init__(self, state_file: str = ".data/gemini_keys_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.key_source = "none"
        self.api_keys = self._load_configured_keys()
        # Keep backward compatibility for existing code that still references API_KEYS
        self.API_KEYS = list(self.api_keys)
            
        # Initialize/Sync keys in state
        self._initialize_keys()

    def _parse_keys_from_env(self) -> List[str]:
        raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
        return self._split_and_dedupe_keys(raw)

    def _split_and_dedupe_keys(self, raw: str) -> List[str]:
        parts = []
        seen = set()
        for token in (raw or "").replace("\n", ",").replace("\r", ",").split(","):
            t = token.strip()
            if not t or t in seen:
                continue
            seen.add(t)
            parts.append(t)
        return parts

    def _allow_embedded_keys(self) -> bool:
        raw = (
            os.getenv("ALLOW_EMBEDDED_GEMINI_KEYS")
            or os.getenv("GEMINI_ALLOW_EMBEDDED_KEYS")
            or ""
        ).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _load_ai_manager_keys(self) -> List[str]:
        try:
            from ..bot.persistence import load_state
            from .config import load_config

            state = load_state(load_config())
            ai_manager = state.get("ai_manager") if isinstance(state, dict) else {}
            provider_state = ai_manager.get("gemini") if isinstance(ai_manager, dict) else {}
            raw_keys = (provider_state.get("active_keys") or provider_state.get("keys") or []) if isinstance(provider_state, dict) else []
            if isinstance(raw_keys, list):
                return self._split_and_dedupe_keys("\n".join(str(k or "").strip() for k in raw_keys))
        except Exception:
            pass
        return []

    def _load_configured_keys(self) -> List[str]:
        env_keys = self._parse_keys_from_env()
        if env_keys:
            self.key_source = "environment"
            logger.info(f"✅ Loaded {len(env_keys)} Gemini key(s) from environment")
            return env_keys

        persisted_keys = self._split_and_dedupe_keys("\n".join((self.state.get("keys") or {}).keys()))
        if persisted_keys:
            self.key_source = "persisted_state"
            logger.info(f"✅ Loaded {len(persisted_keys)} Gemini key(s) from persisted state")
            return persisted_keys

        ai_manager_keys = self._load_ai_manager_keys()
        if ai_manager_keys:
            self.key_source = "bot_state"
            logger.info(f"✅ Loaded {len(ai_manager_keys)} Gemini key(s) from bot state")
            return ai_manager_keys

        if self._allow_embedded_keys():
            embedded = self._split_and_dedupe_keys(",".join(self.API_KEYS))
            self.key_source = "embedded"
            logger.warning(
                "⚠️ Using embedded Gemini keys because ALLOW_EMBEDDED_GEMINI_KEYS is enabled. "
                "This legacy fallback is not recommended for long-term use."
            )
            return embedded

        logger.warning(
            "⚠️ No Gemini API keys configured. Set GEMINI_API_KEYS or GEMINI_API_KEY. "
            "Gemini will be skipped until keys are configured."
        )
        return []

    @property
    def keys(self) -> List[str]:
        return list(self.api_keys)

    def has_configured_keys(self) -> bool:
        return bool(self.api_keys)
    
    def _load_state(self) -> dict:
        """تحميل حالة المفاتيح"""
        # Try Supabase first
        try:
            from ..agent.supabase_storage import load_api_keys
            from ..agent.supabase_client import USE_SUPABASE, is_online
            if USE_SUPABASE and is_online():
                remote = load_api_keys("gemini")
                if remote and "keys" in remote:
                    logger.info("✅ Loaded Gemini keys state from Supabase")
                    return remote
        except Exception as e:
            logger.warning(f"Failed to load keys from Supabase: {e}")

        with _KM_LOCK:
            if self.state_file.exists():
                try:
                    with open(self.state_file, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Error loading key state: {e}")
        
        return {"keys": {}, "current_index": 0}
    
    def _save_state(self):
        """حفظ حالة المفاتيح"""
        with _KM_LOCK:
            try:
                base_dir = str(self.state_file.parent)
                os.makedirs(base_dir, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(prefix="gemini_keys_", suffix=".tmp", dir=base_dir)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        json.dump(self.state, f, indent=2)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(tmp_path, str(self.state_file))
                finally:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                
                # Sync to Supabase
                try:
                    from ..agent.supabase_storage import save_api_keys
                    save_api_keys("gemini", self.state)
                except Exception as e:
                    pass  # Quiet fail for sync
                    
            except Exception as e:
                logger.error(f"Error saving key state: {e}")
    
    def _initialize_keys(self):
        """Syncs keys from self.API_KEYS with the state, preserving block status/counters."""
        state_keys = self.state.get("keys", {})
        new_keys_state = {}
        
        for key in self.api_keys:
            if key in state_keys:
                # Keep existing stats
                new_keys_state[key] = state_keys[key]
            else:
                # Add new key
                new_keys_state[key] = {
                    "requests_today": 0,
                    "requests_this_minute": 0,
                    "last_request_time": None,
                    "last_reset_day": datetime.now().date().isoformat(),
                    "last_reset_minute": datetime.now().replace(second=0, microsecond=0).isoformat(),
                    "is_blocked": False,
                    "block_until": None,
                    "total_requests": 0,
                    "errors": 0
                }
        
        self.state["keys"] = new_keys_state
        self._save_state()
    
    def get_next_key(self) -> Optional[str]:
        """الحصول على المفتاح التالي المتاح"""
        with _KM_LOCK:
            now = datetime.now()
            current_day = now.date().isoformat()
            current_minute = now.replace(second=0, microsecond=0).isoformat()

            for key, data in self.state["keys"].items():
                data.setdefault("errors", 0)
                data.setdefault("last_error_category", None)
                data.setdefault("last_error_time", None)

                if data["last_reset_day"] != current_day:
                    data["requests_today"] = 0
                    data["last_reset_day"] = current_day

                if data["last_reset_minute"] != current_minute:
                    data["requests_this_minute"] = 0
                    data["last_reset_minute"] = current_minute

                if data["is_blocked"] and data["block_until"]:
                    try:
                        block_until = datetime.fromisoformat(data["block_until"]).replace(tzinfo=None)
                    except ValueError:
                        block_until = now
                    if now >= block_until:
                        data["is_blocked"] = False
                        data["block_until"] = None
                        data["errors"] = 0
                        data["last_error_category"] = None
                        data["last_error_time"] = None

                try:
                    lec = (data.get("last_error_category") or "").lower()
                    if lec in {"network", "timeout", "transient", "exception", "empty"}:
                        data["errors"] = 0
                        data["last_error_category"] = None
                        data["last_error_time"] = None
                    else:
                        let = data.get("last_error_time")
                        if let:
                            try:
                                t = datetime.fromisoformat(let).replace(tzinfo=None)
                            except ValueError:
                                t = now - timedelta(minutes=11)
                            if now - t > timedelta(minutes=10):
                                data["errors"] = 0
                                data["last_error_category"] = None
                                data["last_error_time"] = None
                except Exception:
                    pass

            available_keys = []
            for key, data in self.state["keys"].items():
                if (
                    (not data["is_blocked"])
                    and data["requests_today"] < self.FREE_TIER_LIMITS["requests_per_day"]
                    and data["requests_this_minute"] < self.FREE_TIER_LIMITS["requests_per_minute"]
                ):
                    available_keys.append(key)

            if not self.state["keys"]:
                logger.warning("⚠️ Gemini is not configured with any API keys.")
                return None

            if not available_keys:
                logger.warning("⚠️ No available Gemini keys! All configured keys are blocked or exhausted.")
                return None

            selected_key = random.choice(available_keys)

            logger.info(
                f"✅ Selected Gemini key: ...{selected_key[-10:]} "
                f"(Today: {self.state['keys'][selected_key]['requests_today']}/{self.FREE_TIER_LIMITS['requests_per_day']}, "
                f"This minute: {self.state['keys'][selected_key]['requests_this_minute']}/{self.FREE_TIER_LIMITS['requests_per_minute']})"
            )

            return selected_key
    
    def mark_request(
        self,
        key: str,
        success: bool = True,
        status_code: int = 0,
        error_category: str = "other",
        retry_after_seconds: Optional[int] = None,
    ):
        """تسجيل طلب على المفتاح مع تصنيف الأخطاء لتقليل الحظر الخاطئ."""
        with _KM_LOCK:
            if key not in self.state["keys"]:
                return

            data = self.state["keys"][key]
            data.setdefault("errors", 0)
            data.setdefault("last_error_category", None)
            data.setdefault("last_error_time", None)

            data["requests_today"] += 1
            data["requests_this_minute"] += 1
            data["total_requests"] += 1
            data["last_request_time"] = datetime.now().isoformat()

            if success:
                data["errors"] = 0
                data["last_error_category"] = None
                data["last_error_time"] = None
                self._save_state()
                return

            data["last_error_category"] = (error_category or "other")
            data["last_error_time"] = datetime.now().isoformat()

            cat = (error_category or "other").lower()

            if cat in {"network", "timeout", "transient", "exception", "empty"} or int(status_code or 0) >= 500:
                self._save_state()
                return

            if cat == "bad_request":
                self._save_state()
                return

            data["errors"] += 1

            if cat == "invalid_key" or status_code in {401, 403}:
                data["is_blocked"] = True
                data["block_until"] = (datetime.now() + timedelta(hours=24)).isoformat()
                logger.warning(f"🚫 Key blocked for 24h (invalid): ...{key[-10:]}")
            elif cat == "quota_exhausted":
                data["is_blocked"] = True
                data["block_until"] = (datetime.now() + timedelta(hours=6)).isoformat()
                logger.warning(f"🚫 Key blocked for 6h (quota): ...{key[-10:]}")
            elif cat == "rate_limit" or status_code == 429:
                wait_s = int(retry_after_seconds or 90)
                wait_s = max(30, min(wait_s, 600))
                data["is_blocked"] = True
                data["block_until"] = (datetime.now() + timedelta(seconds=wait_s)).isoformat()
                logger.warning(f"🚫 Key blocked for {wait_s}s (rate-limit): ...{key[-10:]}")
            else:
                if data["errors"] >= 20:
                    data["is_blocked"] = True
                    data["block_until"] = (datetime.now() + timedelta(minutes=2)).isoformat()
                    logger.warning(f"🚫 Key blocked for 2 minutes (transient): ...{key[-10:]}")

            self._save_state()
    
    def get_stats(self) -> dict:
        """الحصول على إحصائيات المفاتيح"""
        total_requests_today = sum(data["requests_today"] for data in self.state["keys"].values())
        total_requests_all_time = sum(data["total_requests"] for data in self.state["keys"].values())
        available_keys = sum(1 for data in self.state["keys"].values() 
                           if not data["is_blocked"] and 
                           data["requests_today"] < self.FREE_TIER_LIMITS["requests_per_day"])
        blocked_keys = sum(1 for data in self.state["keys"].values() if data["is_blocked"])
        
        return {
            "total_keys": len(self.api_keys),
            "available_keys": available_keys,
            "blocked_keys": blocked_keys,
            "total_requests_today": total_requests_today,
            "total_requests_all_time": total_requests_all_time,
            "daily_limit_per_key": self.FREE_TIER_LIMITS["requests_per_day"],
            "minute_limit_per_key": self.FREE_TIER_LIMITS["requests_per_minute"],
        }


# مثيل عام
_key_manager = None

def get_key_manager() -> GeminiKeyManager:
    """الحصول على مدير المفاتيح"""
    global _key_manager
    if _key_manager is None:
        _key_manager = GeminiKeyManager()
    return _key_manager
