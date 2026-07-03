"""
Professional Groq API Manager
Integrates with Groq's fast inference API for open source models.
"""
import random
import time
import logging
import os
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
import requests

logger = logging.getLogger(__name__)


class GroqManager:
    """Manages Groq API keys and models with failover logic."""
    
    # Prioritized list of Groq models (free tier and paid)
    DEFAULT_MODELS = [
        "llama-3.1-8b-instant",         # Fast and efficient (Most reliable for free tier)
        "llama-3.3-70b-versatile",      # Newest & Most capable
        "gemma2-9b-it",                 # Google's model on Groq
        "mixtral-8x7b-32768",           # Good for complex tasks
    ]

    def __init__(self, state_file: str = ".data/groq_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        
        # Load keys from environment or persisted bot state
        self.api_keys = self._load_configured_keys()
        
        # Initialize/Sync keys in state
        self._initialize_keys()

    def _user_models(self) -> List[str]:
        """User-saved model IDs from ai_models_store (with fallback to defaults)."""
        try:
            from . import ai_models_store
            return ai_models_store.get_models("groq")
        except Exception:
            return list(self.DEFAULT_MODELS)

    def _env_model_override(self) -> Optional[str]:
        """Honor GROQ_MODEL env if set (single-model override)."""
        raw = (os.getenv("GROQ_MODEL") or "").strip()
        return raw or None

    def _parse_keys_from_env(self) -> List[str]:
        raw = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY") or ""
        if not raw:
            return []
        
        return self._split_and_dedupe_keys(raw)

    def _split_and_dedupe_keys(self, raw: str) -> List[str]:
        parts = []
        seen = set()
        for token in raw.replace("\n", ",").replace("\r", ",").split(","):
            t = token.strip()
            if t and t not in seen:
                parts.append(t)
                seen.add(t)
        return parts

    def _load_ai_manager_keys(self) -> List[str]:
        try:
            from ..bot.persistence import load_state
            from .config import load_config

            state = load_state(load_config())
            ai_manager = state.get("ai_manager") if isinstance(state, dict) else {}
            provider_state = ai_manager.get("groq") if isinstance(ai_manager, dict) else {}
            raw_keys = (provider_state.get("active_keys") or provider_state.get("keys") or []) if isinstance(provider_state, dict) else []
            if isinstance(raw_keys, list):
                return self._split_and_dedupe_keys("\n".join(str(k or "").strip() for k in raw_keys))
        except Exception:
            pass
        return []

    def _load_configured_keys(self) -> List[str]:
        env_keys = self._parse_keys_from_env()
        if env_keys:
            logger.info(f"✅ Loaded {len(env_keys)} Groq key(s) from environment")
            return env_keys

        persisted_keys = self._split_and_dedupe_keys("\n".join((self.state.get("keys") or {}).keys()))
        if persisted_keys:
            logger.info(f"✅ Loaded {len(persisted_keys)} Groq key(s) from persisted state")
            return persisted_keys

        ai_manager_keys = self._load_ai_manager_keys()
        if ai_manager_keys:
            logger.info(f"✅ Loaded {len(ai_manager_keys)} Groq key(s) from bot state")
            return ai_manager_keys

        return []

    def _load_state(self) -> dict:
        # Try Supabase first
        try:
            from ..agent.supabase_storage import load_api_keys
            from ..agent.supabase_client import USE_SUPABASE, is_online
            if USE_SUPABASE and is_online():
                remote = load_api_keys("groq")
                if remote and "keys" in remote:
                    logger.info("✅ Loaded Groq state from Supabase")
                    return remote
        except Exception:
            pass

        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading Groq state: {e}")
        return {"keys": {}, "models": self.DEFAULT_MODELS}

    def _save_state(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
            
            # Sync to Supabase
            try:
                from ..agent.supabase_storage import save_api_keys
                save_api_keys("groq", self.state)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error saving Groq state: {e}")

    def _initialize_keys(self):
        """Syncs env keys with state, preserving block status."""
        state_keys = self.state.get("keys", {})
        new_keys_state = {}
        
        for key in self.api_keys:
            if key in state_keys:
                new_keys_state[key] = state_keys[key]
            else:
                new_keys_state[key] = {
                    "is_blocked": False,
                    "block_until": None,
                    "consecutive_errors": 0,
                    "last_check": None,
                    "usage_limit_reached": False
                }
        self.state["keys"] = new_keys_state
        self._save_state()

    def get_models(self) -> List[str]:
        """Returns the effective model list (user-saved first, then defaults)."""
        # Priority: explicit env override > user-saved models > defaults
        env_override = self._env_model_override()
        if env_override:
            return [env_override]
        user_models = self._user_models()
        if user_models:
            return list(user_models)
        return self.state.get("models") or list(self.DEFAULT_MODELS)

    def get_next_key(self) -> Optional[str]:
        """Finds a key that isn't blocked, using rotation.

        Also consults ai_quota_tracker for unified backoff state — if a key
        was marked as quota_exhausted by the unified tracker (e.g. during a
        cross-provider failover), it will be skipped here too.
        """
        now = datetime.now()

        # Clean up expired blocks (legacy state cleanup)
        updated = False
        for key, data in self.state["keys"].items():
            if data["is_blocked"] and data["block_until"]:
                try:
                    until = datetime.fromisoformat(data["block_until"]).replace(tzinfo=None)
                except ValueError:
                    until = now
                if now >= until:
                    data["is_blocked"] = False
                    data["block_until"] = None
                    data["usage_limit_reached"] = False
                    data["consecutive_errors"] = 0
                    updated = True

        if updated:
            self._save_state()

        # Legacy filter
        available = [k for k, d in self.state["keys"].items()
                    if not d["is_blocked"] and not d.get("usage_limit_reached", False)]

        # Apply unified quota tracker filter (cross-provider coordination)
        try:
            from . import ai_quota_tracker
            available = ai_quota_tracker.get_available_keys("groq", available)
        except Exception:
            pass

        if not available:
            logger.warning("⚠️ No available Groq keys! All keys exhausted or blocked.")
            return None

        # Rotate: pick randomly from available (but prefer least-recently-used)
        return random.choice(available)

    def mark_key_success(self, key: str):
        if key in self.state["keys"]:
            self.state["keys"][key]["consecutive_errors"] = 0
            self.state["keys"][key]["last_check"] = datetime.now().isoformat()
            self.state["keys"][key]["usage_limit_reached"] = False
            self.state["keys"][key]["is_blocked"] = False
            self.state["keys"][key]["block_until"] = None
            self._save_state()
        # Also notify unified quota tracker
        try:
            from . import ai_quota_tracker
            ai_quota_tracker.mark_key_success("groq", key)
        except Exception:
            pass

    def mark_key_error(self, key: str, status_code: int = 0):
        if key not in self.state["keys"]:
            return

        data = self.state["keys"][key]
        data["consecutive_errors"] += 1
        data["last_check"] = datetime.now().isoformat()

        if status_code == 402:
            # Quota exhausted
            self.mark_key_limit_reached(key)
            try:
                from . import ai_quota_tracker
                ai_quota_tracker.mark_key_failure("groq", key, status_code=status_code, error_category="quota_exhausted")
            except Exception:
                pass
            return

        if status_code == 429:
            # Rate limit
            self.block_key_seconds(key, seconds=120, reason="rate-limit")
            try:
                from . import ai_quota_tracker
                ai_quota_tracker.mark_key_failure("groq", key, status_code=status_code, error_category="rate_limit", retry_after_seconds=120)
            except Exception:
                pass
            return

        if status_code in {401, 403}:
            # Invalid key
            self.block_key(key, minutes=1440)
            try:
                from . import ai_quota_tracker
                ai_quota_tracker.mark_key_failure("groq", key, status_code=status_code, error_category="invalid_key")
            except Exception:
                pass
            return

        if data["consecutive_errors"] >= 5:
            # General errors - temporary cool down
            self.block_key(key, minutes=10)
            try:
                from . import ai_quota_tracker
                ai_quota_tracker.mark_key_failure("groq", key, status_code=status_code, error_category="other")
            except Exception:
                pass
            return

        self._save_state()

    def mark_key_limit_reached(self, key: str):
        if key in self.state["keys"]:
            self.state["keys"][key]["usage_limit_reached"] = True
            # Block for 6 hours if limit reached
            self.block_key(key, minutes=360) 

    def block_key(self, key: str, minutes: int = 15):
        if key in self.state["keys"]:
            self.state["keys"][key]["is_blocked"] = True
            self.state["keys"][key]["block_until"] = (datetime.now() + timedelta(minutes=minutes)).isoformat()
            self._save_state()
            logger.warning(f"🚫 Groq Key blocked for {minutes}m: ...{key[-8:]}")

    def block_key_seconds(self, key: str, seconds: int = 120, reason: str = "temporary"):
        if key in self.state["keys"]:
            seconds = max(15, int(seconds))
            self.state["keys"][key]["is_blocked"] = True
            self.state["keys"][key]["block_until"] = (datetime.now() + timedelta(seconds=seconds)).isoformat()
            self._save_state()
            logger.warning(f"🚫 Groq Key blocked for {seconds}s ({reason}): ...{key[-8:]}")

    def _classify_error(self, resp: requests.Response) -> tuple[int, Optional[int], str]:
        status = int(getattr(resp, "status_code", 0) or 0)
        retry_after = None
        try:
            ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
            if ra:
                retry_after = int(float(str(ra).strip()))
        except Exception:
            retry_after = None

        body = ""
        try:
            body = (resp.text or "")[:800]
        except Exception:
            body = ""
        low = body.lower()

        if status in {401, 403}:
            return status, retry_after, "invalid_key"

        if status == 402:
            return status, retry_after, "quota_exhausted"

        if status == 429:
            return status, retry_after, "rate_limit"

        if status >= 500:
            return status, retry_after, "transient"
        return status, retry_after, "other"

    def completion_with_fallback(self, prompt: str, system_prompt: Optional[str] = None, 
                                model: Optional[str] = None) -> Optional[str]:
        """
        Tries to generate content using Groq API.
        Strategy:
          - If an explicit model is passed, try it first.
          - Then iterate through every available model in get_models() (user-saved order).
          - If a model returns 404 / "model_not_found" / "deprecation" → try the next model.
          - If a key is invalid/rate-limited/quota-exhausted → try the next key.
          - This ensures graceful degradation when one model is unavailable.
        """
        # Build the ordered list of models to try
        candidate_models: List[str] = []
        if model:
            candidate_models.append(model)
        for m in self.get_models():
            if m not in candidate_models:
                candidate_models.append(m)

        try:
            max_keys = int(os.getenv("GROQ_MAX_KEYS_PER_REQUEST", "1") or "1")
        except Exception:
            max_keys = 1
        max_keys = max(1, min(5, max_keys))

        try:
            timeout_s = int(os.getenv("GROQ_TIMEOUT_SECONDS", "20") or "20")
        except Exception:
            timeout_s = 20
        timeout_s = max(8, min(60, timeout_s))

        tried_keys = set()
        total_keys_available = max(1, len(self.state.get("keys", {})))
        for _ in range(min(max_keys, total_keys_available)):
            api_key = self.get_next_key()
            if not api_key or api_key in tried_keys:
                break

            tried_keys.add(api_key)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            # Try each model with this key
            for m in candidate_models:
                logger.info(f"🤖 Trying Groq (Key: ...{api_key[-8:]}, Model: {m})")
                payload = {
                    "model": m,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 1000,
                }

                try:
                    resp = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=timeout_s
                    )

                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content", "")
                            if content:
                                self.mark_key_success(api_key)
                                return content

                    status, retry_after, cat = self._classify_error(resp)

                    if cat == "invalid_key":
                        logger.error(f"❌ Invalid Groq Key: ...{api_key[-8:]}")
                        self.block_key(api_key, minutes=1440)
                        break  # Move to next key

                    if cat == "quota_exhausted":
                        logger.warning(f"⚠️ Groq quota exhausted ({status}) for model {m}")
                        self.mark_key_error(api_key, status_code=402)
                        break  # Move to next key

                    if cat == "rate_limit":
                        logger.warning(f"⚠️ Groq rate limit ({status}) for model {m}")
                        self.block_key_seconds(api_key, seconds=int(retry_after or 90), reason="rate-limit")
                        break  # Move to next key

                    # For 404 / 400 / 500 / other — try the NEXT model with same key
                    logger.warning(
                        f"⚠️ Groq Error {status} with model {m}: {(resp.text or '')[:200]}. "
                        f"Will try next model if available."
                    )
                    self.mark_key_error(api_key, status_code=status)
                    continue  # Try next model with same key

                except Exception as e:
                    logger.error(f"❌ Exception calling Groq {m}: {e}")
                    self.mark_key_error(api_key)
                    continue  # Try next model

        return None

    def check_key_health(self, key: str) -> bool:
        """Calls Groq API to check if the key is valid."""
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 5
            }
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                               json=payload, headers=headers, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False


# Global instance
_groq_manager = None
def get_groq_manager() -> GroqManager:
    global _groq_manager
    if _groq_manager is None:
        _groq_manager = GroqManager()
    return _groq_manager
