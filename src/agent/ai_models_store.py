"""
AI Models Storage Module
=======================
Central registry for per-provider custom model IDs, persisted to local JSON
and (optionally) Supabase. Allows the bot admin to add/edit/delete multiple
models per AI provider so the runtime can automatically fall over to the
next model when one fails (rate limit / quota / network / etc.).

Supported providers: gemini, openrouter, groq, clarifai, mistral
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# Default model lists per provider (used when nothing user-configured exists).
# Kept intentionally conservative — admins can add their own via Telegram.
DEFAULT_MODELS: Dict[str, List[str]] = {
    "gemini": [
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
        "gemini-1.5-flash-latest",
    ],
    "openrouter": [
        "meta-llama/llama-3.1-8b-instruct:free",
        "mistralai/mistral-7b-instruct:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "microsoft/phi-3-medium-128k-instruct:free",
        "huggingfaceh4/zephyr-7b-beta:free",
        "openchat/openchat-7b:free",
    ],
    "groq": [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "gemma2-9b-it",
        "mixtral-8x7b-32768",
    ],
    "clarifai": [
        "GPT-4o",
        "GLM_4_6",
        "Kimi-K2-Thinking",
        "MiniMax-M2",
    ],
    "mistral": [
        "mistral-large-latest",
        "mistral-small-latest",
        "mistral-tiny",
    ],
}

# Provider-specific fields stored for each model entry:
#   {
#     "models": {
#        "openrouter": [
#            {"id": "meta-llama/llama-3.1-8b-instruct:free", "enabled": true, "added_at": "..."},
#            ...
#        ], ...
#     }
#   }


def _state_file() -> Path:
    base = os.getenv("AI_MODELS_STATE_FILE") or ".data/ai_models_state.json"
    p = Path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now().isoformat()


# ────────────────────────────── Local JSON persistence ─────────────────────────

def _load_local() -> Dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {"models": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "models" not in data or not isinstance(data["models"], dict):
            data["models"] = {}
        return data
    except Exception as e:
        logger.warning(f"Failed to load AI models state: {e}")
        return {"models": {}}


def _save_local(state: Dict[str, Any]) -> None:
    p = _state_file()
    try:
        base_dir = str(p.parent)
        os.makedirs(base_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="ai_models_", suffix=".tmp", dir=base_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp_path, str(p))
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to save AI models state: {e}")


# ────────────────────────────── Supabase sync (optional) ───────────────────────

def _load_supabase() -> Optional[Dict[str, Any]]:
    try:
        from .supabase_storage import supabase_select_one  # type: ignore
        from .supabase_client import USE_SUPABASE, is_online  # type: ignore
        if not (USE_SUPABASE and is_online()):
            return None
        result = supabase_select_one("ai_models_state", "id", "main")
        if not result:
            return None
        models_raw = result.get("models")
        if isinstance(models_raw, str):
            try:
                models = json.loads(models_raw)
            except Exception:
                models = {}
        elif isinstance(models_raw, dict):
            models = models_raw
        else:
            models = {}
        return {"models": models, "updated_at": result.get("updated_at")}
    except Exception as e:
        logger.debug(f"Supabase AI models load skipped: {e}")
        return None


def _save_supabase(state: Dict[str, Any]) -> None:
    try:
        from .supabase_storage import supabase_upsert  # type: ignore
        from .supabase_client import USE_SUPABASE, is_online  # type: ignore
        if not (USE_SUPABASE and is_online()):
            return
        supabase_upsert(
            "ai_models_state",
            {
                "id": "main",
                "models": json.dumps(state.get("models", {}), ensure_ascii=False),
                "updated_at": _now_iso(),
            },
            "id",
        )
    except Exception as e:
        logger.debug(f"Supabase AI models save skipped: {e}")


# ────────────────────────────── Public API ─────────────────────────────────────

def list_providers() -> List[str]:
    """Return all supported provider IDs."""
    return list(DEFAULT_MODELS.keys())


def get_default_models(provider: str) -> List[str]:
    """Return the built-in default model list for a provider."""
    return list(DEFAULT_MODELS.get(provider, []))


def get_models(provider: str) -> List[str]:
    """
    Return the effective ordered list of enabled model IDs for the provider.
    - User-saved models take priority (in the order they were added).
    - If none configured, falls back to defaults (env overrides respected by caller).
    """
    provider = (provider or "").strip().lower()
    with _LOCK:
        state = _load_local()
        # Try Supabase if local is empty
        if not state.get("models"):
            remote = _load_supabase()
            if remote and remote.get("models"):
                state = remote
                _save_local(state)

        entries = (state.get("models") or {}).get(provider) or []
        if isinstance(entries, list):
            ids = [
                str(e.get("id") or "").strip()
                for e in entries
                if isinstance(e, dict) and e.get("enabled", True) and str(e.get("id") or "").strip()
            ]
            if ids:
                return ids
        return list(DEFAULT_MODELS.get(provider, []))


def get_model_entries(provider: str) -> List[Dict[str, Any]]:
    """Return raw model entries (with enabled flag) for a provider."""
    provider = (provider or "").strip().lower()
    with _LOCK:
        state = _load_local()
        if not state.get("models"):
            remote = _load_supabase()
            if remote and remote.get("models"):
                state = remote
                _save_local(state)

        entries = (state.get("models") or {}).get(provider) or []
        if isinstance(entries, list):
            return list(entries)
        return []


def add_model(provider: str, model_id: str) -> bool:
    """Add a model id to a provider if not present. Returns True if added."""
    provider = (provider or "").strip().lower()
    model_id = (model_id or "").strip()
    if not provider or not model_id:
        return False

    with _LOCK:
        state = _load_local()
        if not state.get("models"):
            remote = _load_supabase()
            if remote and remote.get("models"):
                state = remote
        models_dict = state.setdefault("models", {})
        entries = models_dict.setdefault(provider, [])

        for e in entries:
            if isinstance(e, dict) and str(e.get("id") or "").strip() == model_id:
                return False  # Already exists

        entries.append({
            "id": model_id,
            "enabled": True,
            "added_at": _now_iso(),
        })
        _save_local(state)
        _save_supabase(state)
        return True


def remove_model(provider: str, model_id: str) -> bool:
    """Remove a model id from a provider. Returns True if removed."""
    provider = (provider or "").strip().lower()
    model_id = (model_id or "").strip()
    if not provider or not model_id:
        return False

    with _LOCK:
        state = _load_local()
        models_dict = state.setdefault("models", {})
        entries = models_dict.get(provider) or []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("id") or "").strip() == model_id)
        ]
        if len(new_entries) == len(entries):
            return False
        models_dict[provider] = new_entries
        _save_local(state)
        _save_supabase(state)
        return True


def toggle_model(provider: str, model_id: str) -> Optional[bool]:
    """Toggle a model's enabled flag. Returns new state (True/False) or None if not found."""
    provider = (provider or "").strip().lower()
    model_id = (model_id or "").strip()
    if not provider or not model_id:
        return None

    with _LOCK:
        state = _load_local()
        models_dict = state.setdefault("models", {})
        entries = models_dict.get(provider) or []
        for e in entries:
            if isinstance(e, dict) and str(e.get("id") or "").strip() == model_id:
                new_state = not bool(e.get("enabled", True))
                e["enabled"] = new_state
                _save_local(state)
                _save_supabase(state)
                return new_state
        return None


def reorder_models(provider: str, new_order: List[str]) -> bool:
    """Reorder models for a provider. Models not in new_order are appended in original order."""
    provider = (provider or "").strip().lower()
    if not provider:
        return False
    with _LOCK:
        state = _load_local()
        models_dict = state.setdefault("models", {})
        entries = models_dict.get(provider) or []
        by_id = {str(e.get("id") or "").strip(): e for e in entries if isinstance(e, dict)}
        new_entries: List[Dict[str, Any]] = []
        for mid in new_order:
            mid = (mid or "").strip()
            if mid and mid in by_id:
                new_entries.append(by_id.pop(mid))
        # Append remaining entries not in new_order
        for e in entries:
            if isinstance(e, dict):
                mid = str(e.get("id") or "").strip()
                if mid in by_id:
                    new_entries.append(by_id.pop(mid))
        models_dict[provider] = new_entries
        _save_local(state)
        _save_supabase(state)
        return True


def reset_to_defaults(provider: Optional[str] = None) -> None:
    """Clear all user-saved models (optionally for one provider only)."""
    with _LOCK:
        state = _load_local()
        models_dict = state.setdefault("models", {})
        if provider:
            provider = provider.strip().lower()
            models_dict.pop(provider, None)
        else:
            models_dict.clear()
        _save_local(state)
        _save_supabase(state)
