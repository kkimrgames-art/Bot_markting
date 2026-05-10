"""
Professional OpenRouter Key & Free Model Manager
Prioritizes free models from strongest to weakest.
"""
import random
import time
import logging
import os
import json
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
import requests

logger = logging.getLogger(__name__)

class OpenRouterManager:
    """Manages OpenRouter keys and free models with failover logic."""
    
    # Prioritized list of FREE models (Strongest to Weakest)
    # Based on user request: Strongest -> Weakest
    DEFAULT_FREE_MODELS = [
        "meta-llama/llama-3.1-8b-instruct:free",  # Very Good Open Model
        "mistralai/mistral-7b-instruct:free",     # Solid Standard
        "qwen/qwen-2.5-7b-instruct:free",         # Excellent Multilingual
        "microsoft/phi-3-medium-128k-instruct:free", # Good Logic
        "huggingfaceh4/zephyr-7b-beta:free",      # Good Instruction Following
        "openchat/openchat-7b:free",              # Decent Fallback
    ]

    def __init__(self, state_file: str = ".data/openrouter_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        
        # Load keys from environment or persisted bot state
        self.api_keys = self._load_configured_keys()
        
        # Initialize/Sync keys in state
        self._initialize_keys()
        
        # Discover free models dynamically
        self._refresh_models_if_needed()

    def _parse_keys_from_env(self) -> List[str]:
        raw = os.getenv("OPENROUTER_API_KEYS") or ""
        if not raw:
            # Check legacy single key
            single = os.getenv("OPENROUTER_API_KEY")
            return [single] if single else []
        
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
            provider_state = ai_manager.get("openrouter") if isinstance(ai_manager, dict) else {}
            raw_keys = (provider_state.get("active_keys") or provider_state.get("keys") or []) if isinstance(provider_state, dict) else []
            if isinstance(raw_keys, list):
                return self._split_and_dedupe_keys("\n".join(str(k or "").strip() for k in raw_keys))
        except Exception:
            pass
        return []

    def _load_configured_keys(self) -> List[str]:
        env_keys = self._parse_keys_from_env()
        if env_keys:
            logger.info(f"✅ Loaded {len(env_keys)} OpenRouter key(s) from environment")
            return env_keys

        persisted_keys = self._split_and_dedupe_keys("\n".join((self.state.get("keys") or {}).keys()))
        if persisted_keys:
            logger.info(f"✅ Loaded {len(persisted_keys)} OpenRouter key(s) from persisted state")
            return persisted_keys

        ai_manager_keys = self._load_ai_manager_keys()
        if ai_manager_keys:
            logger.info(f"✅ Loaded {len(ai_manager_keys)} OpenRouter key(s) from bot state")
            return ai_manager_keys

        return []

    def _is_disallowed_model(self, model_id: Optional[str]) -> bool:
        mid = (model_id or "").strip().lower()
        return "gemini" in mid

    def _sanitize_models(self, models: Optional[List[str]]) -> List[str]:
        cleaned = []
        seen = set()
        for model_id in models or []:
            mid = (model_id or "").strip()
            if not mid or self._is_disallowed_model(mid) or mid in seen:
                continue
            seen.add(mid)
            cleaned.append(mid)
        return cleaned

    def _load_state(self) -> dict:
        # Try Supabase first
        try:
            from ..agent.supabase_storage import load_api_keys
            from ..agent.supabase_client import USE_SUPABASE, is_online
            if USE_SUPABASE and is_online():
                remote = load_api_keys("openrouter")
                if remote and "keys" in remote:
                    logger.info("✅ Loaded OpenRouter state from Supabase")
                    return remote
        except Exception:
            pass

        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading OpenRouter state: {e}")
        return {"keys": {}, "models": self.DEFAULT_FREE_MODELS}

    def _save_state(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
            
            # Sync to Supabase
            try:
                from ..agent.supabase_storage import save_api_keys
                save_api_keys("openrouter", self.state)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error saving OpenRouter state: {e}")

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

    def _refresh_models_if_needed(self):
        """Refreshes the dynamic model list if expired (6 hours) or missing."""
        now = datetime.now()
        last_refresh_str = self.state.get("last_model_refresh")
        existing_models = self._sanitize_models(self.state.get("dynamic_models") or [])
        if existing_models != (self.state.get("dynamic_models") or []):
            self.state["dynamic_models"] = existing_models
            self._save_state()
        
        should_refresh = False
        if not last_refresh_str:
            should_refresh = True
        else:
            try:
                last_refresh = datetime.fromisoformat(last_refresh_str).replace(tzinfo=None)
                if now - last_refresh > timedelta(hours=6):
                    should_refresh = True
            except Exception:
                should_refresh = True
        
        if should_refresh or not self.state.get("dynamic_models"):
            logger.info("📡 Discovering free models from OpenRouter...")
            models = self._fetch_dynamic_models()
            if models:
                ranked = self._rank_models(models)
                self.state["dynamic_models"] = ranked
                self.state["last_model_refresh"] = now.isoformat()
                self._save_state()
                logger.info(f"✅ Discovered and ranked {len(ranked)} free models.")
            else:
                logger.warning("⚠️ Failed to fetch dynamic models. Using hardcoded fallback.")

    def _fetch_dynamic_models(self) -> List[str]:
        """Fetches all models from OpenRouter and filters for free ones."""
        try:
            resp = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                # Filter for free models (pricing.prompt and pricing.completion are "0")
                free_ids = []
                for m in data:
                    pricing = m.get("pricing", {})
                    try:
                        p_prompt = float(pricing.get("prompt", 1))
                        p_compl = float(pricing.get("completion", 1))
                        if p_prompt == 0 and p_compl == 0:
                            free_ids.append(m.get("id"))
                    except (ValueError, TypeError):
                        continue
                return self._sanitize_models(free_ids)
        except Exception as e:
            logger.error(f"Error fetching OpenRouter models: {e}")
        return []

    def _rank_models(self, models: List[str]) -> List[str]:
        """Ranks free models based on predefined family quality and versions."""
        # Quality score based on model family/name substring
        # Lower score = Better model
        RANK_ORDER = [
            "meta-llama/llama-3.1-8b",
            "meta-llama/llama-3.1",
            "mistralai/mistral-7b",
            "mistralai/pixtral-12b",
            "qwen/qwen-2.5",
            "qwen/qwen-2",
            "microsoft/phi-3",
            "zephyr-7b",
            "openchat",
        ]
        
        def get_rank(model_id: str) -> float:
            for i, pattern in enumerate(RANK_ORDER):
                if pattern in model_id:
                    # Give slight priority to models with ':free' suffix to be sure
                    bonus = -0.1 if model_id.endswith(":free") else 0
                    return float(i + bonus)
            return 999.0 # Unknown models go to the end

        # Sort by rank score
        ranked = sorted(self._sanitize_models(models), key=get_rank)
        
        # Ensure we have at least our core fallbacks if dynamic fetch was successful but list is small
        seen = set(ranked)
        for fallback in self.DEFAULT_FREE_MODELS:
            if fallback not in seen:
                ranked.append(fallback)
        
        return ranked

    def get_models(self) -> List[str]:
        """Returns the dynamic ranked list or fallbacks."""
        dynamic = self._sanitize_models(self.state.get("dynamic_models") or [])
        if dynamic:
            return dynamic
        return list(self.DEFAULT_FREE_MODELS)

    def get_next_key(self) -> Optional[str]:
        """Finds a key that isn't blocked, using rotation."""
        now = datetime.now()
        
        # Clean up blocks
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

        available = [k for k, d in self.state["keys"].items() if not d["is_blocked"] and not d.get("usage_limit_reached", False)]
        
        if not available:
            softened = [k for k, d in self.state["keys"].items() if (not d.get("is_blocked", False))]
            if softened:
                return random.choice(softened)

            keys_state = self.state.get("keys", {})
            if keys_state:
                best_key = None
                best_until = None
                for k, d in keys_state.items():
                    until_s = d.get("block_until")
                    if not until_s:
                        continue
                    try:
                        until = datetime.fromisoformat(until_s).replace(tzinfo=None)
                    except Exception:
                        continue
                    if best_until is None or until < best_until:
                        best_until = until
                        best_key = k

                if best_key and best_until:
                    if best_until <= (now + timedelta(seconds=30)):
                        keys_state[best_key]["is_blocked"] = False
                        keys_state[best_key]["block_until"] = None
                        keys_state[best_key]["usage_limit_reached"] = False
                        keys_state[best_key]["consecutive_errors"] = 0
                        self.state["keys"] = keys_state
                        self._save_state()
                        return best_key

                last_heal = self.state.get("last_self_heal")
                do_heal = True
                if last_heal:
                    try:
                        lh = datetime.fromisoformat(str(last_heal)).replace(tzinfo=None)
                        if now - lh < timedelta(minutes=10):
                            do_heal = False
                    except Exception:
                        do_heal = True

                if do_heal:
                    self.state["last_self_heal"] = now.isoformat()
                    k = random.choice(list(keys_state.keys()))
                    keys_state[k]["is_blocked"] = False
                    keys_state[k]["block_until"] = None
                    keys_state[k]["usage_limit_reached"] = False
                    keys_state[k]["consecutive_errors"] = 0
                    self.state["keys"] = keys_state
                    self._save_state()
                    return k

            logger.warning("⚠️ No available OpenRouter keys! All keys exhausted or blocked.")
            return None
            
        # Rotate: pick the one with the oldest last_check or just random
        # To be simple and effective like Gemini manager:
        return random.choice(available)

    def mark_key_success(self, key: str):
        if key in self.state["keys"]:
            self.state["keys"][key]["consecutive_errors"] = 0
            self.state["keys"][key]["last_check"] = datetime.now().isoformat()
            self._save_state()

    def mark_key_error(self, key: str, status_code: int = 0):
        if key not in self.state["keys"]:
            return
            
        data = self.state["keys"][key]
        data["consecutive_errors"] += 1
        data["last_check"] = datetime.now().isoformat()
        
        if status_code == 402:
            # Quota exhausted
            self.mark_key_limit_reached(key)
            return

        if status_code == 429:
            # Usually rate limit (temporary). Do NOT mark usage_limit_reached.
            self.block_key_seconds(key, seconds=120, reason="rate-limit")
            return

        if data["consecutive_errors"] >= 5:
            # General errors - temporary cool down
            self.block_key(key, minutes=10)
            return

        self._save_state()

    def mark_key_limit_reached(self, key: str):
        if key in self.state["keys"]:
            self.state["keys"][key]["usage_limit_reached"] = True
            # Block for 6 hours if limit reached (common for free tiers)
            self.block_key(key, minutes=360) 

    def block_key(self, key: str, minutes: int = 15):
        if key in self.state["keys"]:
            self.state["keys"][key]["is_blocked"] = True
            self.state["keys"][key]["block_until"] = (datetime.now() + timedelta(minutes=minutes)).isoformat()
            self._save_state()
            logger.warning(f"🚫 OpenRouter Key blocked for {minutes}m: ...{key[-8:]}")

    def block_key_seconds(self, key: str, seconds: int = 120, reason: str = "temporary"):
        if key in self.state["keys"]:
            seconds = max(15, int(seconds))
            self.state["keys"][key]["is_blocked"] = True
            self.state["keys"][key]["block_until"] = (datetime.now() + timedelta(seconds=seconds)).isoformat()
            self._save_state()
            logger.warning(f"🚫 OpenRouter Key blocked for {seconds}s ({reason}): ...{key[-8:]}")

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
            # Try to detect quota-like messages
            if re.search(r"quota|exceed|payment|required|billing", low):
                return status, retry_after, "quota_exhausted"
            return status, retry_after, "rate_limit"

        if status >= 500:
            return status, retry_after, "transient"
        return status, retry_after, "other"

    def completion_with_fallback(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """
        Tries to generate content using OpenRouter.
        Iterates through available keys, and for each key, iterates through models.
        """
        models = self.get_models()

        try:
            max_keys = int(os.getenv("OPENROUTER_MAX_KEYS_PER_REQUEST", "1") or "1")
        except Exception:
            max_keys = 1
        max_keys = max(1, min(5, max_keys))

        try:
            max_models = int(os.getenv("OPENROUTER_MAX_MODELS_PER_KEY", "2") or "2")
        except Exception:
            max_models = 2
        max_models = max(1, min(10, max_models))

        try:
            timeout_s = int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "20") or "20")
        except Exception:
            timeout_s = 20
        timeout_s = max(8, min(60, timeout_s))
        
        tried_keys = set()
        for _ in range(min(max_keys, len(self.state["keys"]))):
            api_key = self.get_next_key()
            if not api_key or api_key in tried_keys:
                break
            
            tried_keys.add(api_key)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/Sidivall/Agency-Bot",
                "X-Title": "Agency Bot",
                "Content-Type": "application/json"
            }

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            # For each key, try a limited number of models
            for model in (models[:max_models] if models else []):
                logger.info(f"🤖 Trying OpenRouter (Key: ...{api_key[-8:]}, Model: {model})")
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 1000,
                }
                
                try:
                    resp = requests.post(
                        "https://openrouter.ai/api/v1/chat/completions",
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
                        logger.error(f"❌ Invalid OpenRouter Key: ...{api_key[-8:]}")
                        self.block_key(api_key, minutes=1440)
                        break

                    if cat == "quota_exhausted":
                        logger.warning(f"⚠️ OpenRouter quota exhausted ({status}) for model {model}")
                        self.mark_key_error(api_key, status_code=402)
                        break

                    if cat == "rate_limit":
                        logger.warning(f"⚠️ OpenRouter rate limit ({status}) for model {model}")
                        self.block_key_seconds(api_key, seconds=int(retry_after or 90), reason="rate-limit")
                        break

                    logger.warning(f"⚠️ OpenRouter Error {status} with model {model}: {(resp.text or '')[:200]}")
                    self.mark_key_error(api_key, status_code=status)
                    continue # Try next model with same key

                except Exception as e:
                    logger.error(f"❌ Exception calling OpenRouter {model}: {e}")
                    self.mark_key_error(api_key)
                    continue # Try next model
                    
        return None

    def check_key_health(self, key: str) -> bool:
        """Calls OpenRouter auth endpoint to check if the key is valid."""
        try:
            headers = {"Authorization": f"Bearer {key}"}
            resp = requests.get("https://openrouter.ai/api/v1/auth/key", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                # We can inspect 'limit' or 'usage' if needed
                return True
            return False
        except Exception:
            return False

# Global instance
_or_manager = None
def get_openrouter_manager() -> OpenRouterManager:
    global _or_manager
    if _or_manager is None:
        _or_manager = OpenRouterManager()
    return _or_manager
