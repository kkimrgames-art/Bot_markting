"""
Unified AI Quota Tracker
========================
Tracks per-key + per-provider quota exhaustion and backoff windows for all AI
services: Gemini, OpenRouter, Groq, Clarifai, Mistral.

Key features:
  - Per-key state: blocked_until, last_error_category, last_error_time,
    quota_reset_at (when the free quota is expected to refresh — daily/hourly),
    consecutive_quota_failures.
  - Per-provider state: provider_blocked_until, last_quota_exhausted_at.
  - Smart "is this provider usable right now?" check that skips providers
    whose quota is exhausted AND whose reset time hasn't arrived yet.
  - Automatic periodic reset detection: when a key's reset_at time arrives,
    the key is auto-unblocked (lazy evaluation, no background thread needed).
  - Persistence: local JSON file + optional Supabase sync (best-effort).
  - Thread-safe via RLock.

Free-tier reset windows (per provider, configurable via env vars):
  - Gemini:   1 day    (GEMINI_QUOTA_RESET_SECONDS=86400)
  - OpenRouter: 1 day  (OPENROUTER_QUOTA_RESET_SECONDS=86400)
  - Groq:     1 hour   (GROQ_QUOTA_RESET_SECONDS=3600)  -- hourly rate limits
  - Clarifai: 1 day    (CLARIFAI_QUOTA_RESET_SECONDS=86400)
  - Mistral:  1 hour   (MISTRAL_QUOTA_RESET_SECONDS=3600)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# Default free-tier reset windows in seconds (used when env vars not set)
_DEFAULT_RESET_WINDOWS: Dict[str, int] = {
    "gemini":     24 * 3600,  # 1 day
    "openrouter": 24 * 3600,  # 1 day
    "groq":       1 * 3600,   # 1 hour (Groq has hourly rate limits on free tier)
    "clarifai":   24 * 3600,  # 1 day
    "mistral":    1 * 3600,   # 1 hour (Mistral free tier rate limits)
}

# Per-provider env var name for the reset window
_PROVIDER_RESET_ENV: Dict[str, str] = {
    "gemini":     "GEMINI_QUOTA_RESET_SECONDS",
    "openrouter": "OPENROUTER_QUOTA_RESET_SECONDS",
    "groq":       "GROQ_QUOTA_RESET_SECONDS",
    "clarifai":   "CLARIFAI_QUOTA_RESET_SECONDS",
    "mistral":    "MISTRAL_QUOTA_RESET_SECONDS",
}

# Categories that indicate the quota is exhausted (vs. transient errors)
_QUOTA_LIKE_CATEGORIES = {"quota_exhausted", "rate_limit"}


def _state_file() -> Path:
    base = os.getenv("AI_QUOTA_STATE_FILE") or ".data/ai_quota_state.json"
    p = Path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now().isoformat()


def _now_ts() -> float:
    return time.time()


def _provider_reset_seconds(provider: str) -> int:
    env_name = _PROVIDER_RESET_ENV.get(provider, "")
    if env_name:
        try:
            v = int(os.getenv(env_name) or "")
            if v > 0:
                return max(60, min(30 * 86400, v))
        except Exception:
            pass
    return _DEFAULT_RESET_WINDOWS.get(provider, 3600)


# ────────────────────────────── Local JSON persistence ─────────────────────────

def _load_local() -> Dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {"keys": {}, "providers": {}, "updated_at": _now_iso()}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "keys" not in data or not isinstance(data["keys"], dict):
            data["keys"] = {}
        if "providers" not in data or not isinstance(data["providers"], dict):
            data["providers"] = {}
        return data
    except Exception as e:
        logger.warning(f"Failed to load AI quota state: {e}")
        return {"keys": {}, "providers": {}, "updated_at": _now_iso()}


def _save_local(state: Dict[str, Any]) -> None:
    p = _state_file()
    try:
        base_dir = str(p.parent)
        os.makedirs(base_dir, exist_ok=True)
        state["updated_at"] = _now_iso()
        fd, tmp_path = tempfile.mkstemp(prefix="ai_quota_", suffix=".tmp", dir=base_dir)
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
        logger.error(f"Failed to save AI quota state: {e}")


def _save_supabase(state: Dict[str, Any]) -> None:
    """Best-effort sync to Supabase (single JSON row)."""
    try:
        from .supabase_storage import supabase_upsert  # type: ignore
        from .supabase_client import USE_SUPABASE, is_online  # type: ignore
        if not (USE_SUPABASE and is_online()):
            return
        supabase_upsert(
            "ai_quota_state",
            {
                "id": "main",
                "keys": json.dumps(state.get("keys", {}), ensure_ascii=False),
                "providers": json.dumps(state.get("providers", {}), ensure_ascii=False),
                "updated_at": _now_iso(),
            },
            "id",
        )
    except Exception as e:
        logger.debug(f"Supabase AI quota save skipped: {e}")


def _load_supabase() -> Optional[Dict[str, Any]]:
    try:
        from .supabase_storage import supabase_select_one  # type: ignore
        from .supabase_client import USE_SUPABASE, is_online  # type: ignore
        if not (USE_SUPABASE and is_online()):
            return None
        result = supabase_select_one("ai_quota_state", "id", "main")
        if not result:
            return None
        keys_raw = result.get("keys") or "{}"
        prov_raw = result.get("providers") or "{}"
        return {
            "keys": json.loads(keys_raw) if isinstance(keys_raw, str) else keys_raw,
            "providers": json.loads(prov_raw) if isinstance(prov_raw, str) else prov_raw,
            "updated_at": result.get("updated_at"),
        }
    except Exception as e:
        logger.debug(f"Supabase AI quota load skipped: {e}")
        return None


# ────────────────────────────── Public API ─────────────────────────────────────

def _ensure_key_entry(state: Dict[str, Any], provider: str, key: str) -> Dict[str, Any]:
    keys = state.setdefault("keys", {})
    prov_dict = keys.setdefault(provider, {})
    if key not in prov_dict or not isinstance(prov_dict[key], dict):
        prov_dict[key] = {
            "blocked_until": None,        # ISO timestamp — key is blocked until this time
            "quota_reset_at": None,       # ISO timestamp — when free quota is expected to refresh
            "last_error_category": None,
            "last_error_time": None,
            "consecutive_quota_failures": 0,
            "last_success_at": None,
            "total_requests": 0,
            "total_errors": 0,
        }
    return prov_dict[key]


def _ensure_provider_entry(state: Dict[str, Any], provider: str) -> Dict[str, Any]:
    provs = state.setdefault("providers", {})
    if provider not in provs or not isinstance(provs[provider], dict):
        provs[provider] = {
            "blocked_until": None,            # ISO timestamp
            "last_quota_exhausted_at": None,  # ISO timestamp
            "consecutive_failures": 0,
            "total_calls": 0,
            "total_successes": 0,
        }
    return provs[provider]


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        return None


def _is_blocked(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Returns True if the entry's blocked_until is in the future."""
    now = now or datetime.now()
    blocked_until = _parse_iso(entry.get("blocked_until"))
    if not blocked_until:
        return False
    if now >= blocked_until:
        # Auto-expire — clear it
        entry["blocked_until"] = None
        return False
    return True


def _is_quota_pending(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Returns True if the entry's quota_reset_at is in the future (quota still exhausted)."""
    now = now or datetime.now()
    reset_at = _parse_iso(entry.get("quota_reset_at"))
    if not reset_at:
        return False
    if now >= reset_at:
        # Quota should have reset by now — auto-clear
        entry["quota_reset_at"] = None
        entry["consecutive_quota_failures"] = 0
        return False
    return True


# ─────────── Key-level API ───────────

def mark_key_success(provider: str, key: str) -> None:
    """Call this whenever a key returns a successful response."""
    with _LOCK:
        state = _load_local()
        entry = _ensure_key_entry(state, provider, key)
        entry["blocked_until"] = None
        entry["quota_reset_at"] = None  # Success implies quota is fine
        entry["consecutive_quota_failures"] = 0
        entry["last_error_category"] = None
        entry["last_error_time"] = None
        entry["last_success_at"] = _now_iso()
        entry["total_requests"] = int(entry.get("total_requests", 0)) + 1
        _save_local(state)
        _save_supabase(state)


def mark_key_failure(
    provider: str,
    key: str,
    *,
    status_code: int = 0,
    error_category: str = "other",
    retry_after_seconds: Optional[int] = None,
) -> None:
    """Call this whenever a key returns an error.

    For 'quota_exhausted' / 'rate_limit' categories, the key is blocked and its
    quota_reset_at is set to now + provider's reset window.

    For 'invalid_key' (401/403), the key is blocked for 24h.

    For other categories (transient, network, timeout, empty, bad_request, other),
    the key is NOT blocked — the caller should just try the next key/model.
    """
    with _LOCK:
        state = _load_local()
        entry = _ensure_key_entry(state, provider, key)
        entry["last_error_category"] = error_category or "other"
        entry["last_error_time"] = _now_iso()
        entry["total_errors"] = int(entry.get("total_errors", 0)) + 1
        # Don't increment total_requests on failure — only on success.
        # (We want to track usage, not error counts.)

        cat = (error_category or "other").lower()
        now = datetime.now()

        if cat == "invalid_key":
            # Invalid key — block for 24 hours
            entry["blocked_until"] = (now + timedelta(hours=24)).isoformat()
            logger.warning(f"🚫 [quota_tracker] Key ...{key[-8:]} blocked 24h (invalid_key) provider={provider}")

        elif cat == "quota_exhausted":
            # Free-tier quota exhausted — block until reset
            reset_seconds = retry_after_seconds or _provider_reset_seconds(provider)
            reset_seconds = max(60, min(30 * 86400, int(reset_seconds)))
            entry["quota_reset_at"] = (now + timedelta(seconds=reset_seconds)).isoformat()
            entry["blocked_until"] = entry["quota_reset_at"]
            entry["consecutive_quota_failures"] = int(entry.get("consecutive_quota_failures", 0)) + 1
            logger.warning(
                f"🚫 [quota_tracker] Key ...{key[-8:]} quota_exhausted, "
                f"blocked {reset_seconds}s (until {entry['blocked_until']}) provider={provider}"
            )

        elif cat == "rate_limit":
            # Rate limit — short-term block (typically a few minutes)
            wait_s = int(retry_after_seconds or 90)
            wait_s = max(30, min(wait_s, 600))
            entry["blocked_until"] = (now + timedelta(seconds=wait_s)).isoformat()
            # Note: rate_limit does NOT set quota_reset_at — quota might still be fine
            logger.warning(
                f"🚫 [quota_tracker] Key ...{key[-8:]} rate_limit, "
                f"blocked {wait_s}s provider={provider}"
            )

        elif cat in {"transient", "network", "timeout"}:
            # Transient — short cooldown (30s) so we don't hammer a failing endpoint
            entry["blocked_until"] = (now + timedelta(seconds=30)).isoformat()
            logger.info(f"⏳ [quota_tracker] Key ...{key[-8:]} transient ({cat}), 30s cooldown provider={provider}")

        # bad_request / empty / other → don't block, just log
        _save_local(state)
        _save_supabase(state)


def is_key_available(provider: str, key: str) -> bool:
    """Returns True if the key can be used right now (not blocked / quota OK)."""
    with _LOCK:
        state = _load_local()
        keys = state.get("keys", {}).get(provider, {})
        entry = keys.get(key)
        if not entry:
            return True  # New key — assume available
        now = datetime.now()
        # Auto-clear expired blocks (lazy evaluation)
        if _is_blocked(entry, now):
            _save_local(state)
            return False
        if _is_quota_pending(entry, now):
            _save_local(state)
            return False
        _save_local(state)
        return True


def get_available_keys(provider: str, all_keys: List[str]) -> List[str]:
    """Filter `all_keys` to only those currently available."""
    with _LOCK:
        return [k for k in all_keys if is_key_available(provider, k)]


def get_key_status(provider: str, key: str) -> Dict[str, Any]:
    """Returns a status dict for a key (for UI display)."""
    with _LOCK:
        state = _load_local()
        entry = state.get("keys", {}).get(provider, {}).get(key, {})
        if not entry:
            return {
                "available": True,
                "blocked": False,
                "quota_pending": False,
                "blocked_until": None,
                "quota_reset_at": None,
                "last_error_category": None,
                "last_error_time": None,
                "last_success_at": None,
                "consecutive_quota_failures": 0,
            }
        now = datetime.now()
        blocked = _is_blocked(entry, now)
        quota_pending = _is_quota_pending(entry, now)
        _save_local(state)
        return {
            "available": not (blocked or quota_pending),
            "blocked": blocked,
            "quota_pending": quota_pending,
            "blocked_until": entry.get("blocked_until"),
            "quota_reset_at": entry.get("quota_reset_at"),
            "last_error_category": entry.get("last_error_category"),
            "last_error_time": entry.get("last_error_time"),
            "last_success_at": entry.get("last_success_at"),
            "consecutive_quota_failures": int(entry.get("consecutive_quota_failures", 0)),
            "total_requests": int(entry.get("total_requests", 0)),
            "total_errors": int(entry.get("total_errors", 0)),
        }


def force_unblock_key(provider: str, key: str) -> bool:
    """Manually unblock a key (admin action via Telegram UI)."""
    with _LOCK:
        state = _load_local()
        entry = state.get("keys", {}).get(provider, {}).get(key)
        if not entry:
            return False
        entry["blocked_until"] = None
        entry["quota_reset_at"] = None
        entry["consecutive_quota_failures"] = 0
        entry["last_error_category"] = None
        entry["last_error_time"] = None
        _save_local(state)
        _save_supabase(state)
        return True


def force_unblock_all_keys(provider: Optional[str] = None) -> int:
    """Unblock all keys for a provider (or all providers if None). Returns count."""
    with _LOCK:
        state = _load_local()
        count = 0
        for prov, prov_dict in state.get("keys", {}).items():
            if provider and prov != provider:
                continue
            for key, entry in prov_dict.items():
                if entry.get("blocked_until") or entry.get("quota_reset_at"):
                    entry["blocked_until"] = None
                    entry["quota_reset_at"] = None
                    entry["consecutive_quota_failures"] = 0
                    entry["last_error_category"] = None
                    entry["last_error_time"] = None
                    count += 1
        _save_local(state)
        _save_supabase(state)
        return count


def force_unblock_all_providers(provider: Optional[str] = None) -> int:
    """Unblock all providers (or one provider if specified). Returns count.

    Also clears last_quota_exhausted_at so smart ordering doesn't penalize
    providers that were previously blocked but have been manually cleared.
    """
    with _LOCK:
        state = _load_local()
        count = 0
        for prov, entry in state.get("providers", {}).items():
            if provider and prov != provider:
                continue
            if entry.get("blocked_until") or entry.get("last_quota_exhausted_at"):
                entry["blocked_until"] = None
                entry["last_quota_exhausted_at"] = None
                entry["consecutive_failures"] = 0
                count += 1
        _save_local(state)
        _save_supabase(state)
        return count


# ─────────── Provider-level API ───────────

def mark_provider_success(provider: str) -> None:
    with _LOCK:
        state = _load_local()
        entry = _ensure_provider_entry(state, provider)
        entry["blocked_until"] = None
        entry["consecutive_failures"] = 0
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        entry["total_successes"] = int(entry.get("total_successes", 0)) + 1
        _save_local(state)
        _save_supabase(state)


def mark_provider_failure(provider: str, *, error_category: str = "other") -> None:
    """Mark a provider as failed.

    If the error category is quota-like, set a provider-level block using the
    provider's reset window. Otherwise just increment consecutive_failures —
    after 3 consecutive failures, set a short 5-minute block to skip the
    provider for a while.
    """
    with _LOCK:
        state = _load_local()
        entry = _ensure_provider_entry(state, provider)
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        cat = (error_category or "other").lower()
        now = datetime.now()

        if cat in _QUOTA_LIKE_CATEGORIES:
            reset_seconds = _provider_reset_seconds(provider)
            entry["blocked_until"] = (now + timedelta(seconds=reset_seconds)).isoformat()
            entry["last_quota_exhausted_at"] = now.isoformat()
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            logger.warning(
                f"🚫 [quota_tracker] Provider '{provider}' quota-exhausted, "
                f"blocked {reset_seconds}s (category={cat})"
            )
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            if entry["consecutive_failures"] >= 3:
                # Short provider-level block
                entry["blocked_until"] = (now + timedelta(minutes=5)).isoformat()
                logger.info(
                    f"⏳ [quota_tracker] Provider '{provider}' blocked 5m "
                    f"(consecutive_failures={entry['consecutive_failures']})"
                )
        _save_local(state)
        _save_supabase(state)


def is_provider_available(provider: str) -> bool:
    """Returns True if the provider is currently usable (not provider-blocked)."""
    with _LOCK:
        state = _load_local()
        entry = state.get("providers", {}).get(provider)
        if not entry:
            return True
        now = datetime.now()
        blocked_until = _parse_iso(entry.get("blocked_until"))
        if not blocked_until:
            return True
        if now >= blocked_until:
            # Auto-expire
            entry["blocked_until"] = None
            entry["consecutive_failures"] = 0
            _save_local(state)
            return True
        return False


def get_provider_status(provider: str) -> Dict[str, Any]:
    with _LOCK:
        state = _load_local()
        entry = state.get("providers", {}).get(provider, {})
        now = datetime.now()
        blocked_until = _parse_iso(entry.get("blocked_until"))
        blocked = bool(blocked_until) and now < blocked_until
        if blocked_until and now >= blocked_until:
            # Auto-expire
            entry["blocked_until"] = None
            entry["consecutive_failures"] = 0
            _save_local(state)
            blocked = False
        return {
            "available": not blocked,
            "blocked": blocked,
            "blocked_until": entry.get("blocked_until"),
            "last_quota_exhausted_at": entry.get("last_quota_exhausted_at"),
            "consecutive_failures": int(entry.get("consecutive_failures", 0)),
            "total_calls": int(entry.get("total_calls", 0)),
            "total_successes": int(entry.get("total_successes", 0)),
        }


# ─────────── Diagnostic / UI helpers ───────────

def get_all_providers_status() -> Dict[str, Dict[str, Any]]:
    """Returns status for all 5 providers."""
    return {p: get_provider_status(p) for p in _DEFAULT_RESET_WINDOWS.keys()}


def get_provider_keys_status(provider: str, keys: List[str]) -> List[Dict[str, Any]]:
    """Returns status for each key in `keys` (with masked key for UI)."""
    out = []
    for k in keys:
        status = get_key_status(provider, k)
        status["masked_key"] = f"...{k[-8:]}" if len(k) > 10 else k
        status["key"] = k  # full key for internal use
        out.append(status)
    return out


def get_next_available_key(provider: str, all_keys: List[str]) -> Optional[str]:
    """Returns the first available key from `all_keys`, or None if all blocked."""
    with _LOCK:
        for k in all_keys:
            if is_key_available(provider, k):
                return k
        return None


def count_available_keys(provider: str, all_keys: List[str]) -> Tuple[int, int]:
    """Returns (available_count, total_count)."""
    with _LOCK:
        available = sum(1 for k in all_keys if is_key_available(provider, k))
        return (available, len(all_keys))


def cleanup_expired() -> int:
    """Force-clear expired entries. Returns count of cleared entries."""
    with _LOCK:
        state = _load_local()
        cleared = 0
        now = datetime.now()
        for prov, prov_dict in state.get("keys", {}).items():
            for key, entry in prov_dict.items():
                if _is_blocked(entry, now) is False and entry.get("blocked_until"):
                    entry["blocked_until"] = None
                    cleared += 1
                if _is_quota_pending(entry, now) is False and entry.get("quota_reset_at"):
                    entry["quota_reset_at"] = None
                    entry["consecutive_quota_failures"] = 0
                    cleared += 1
        for prov, entry in state.get("providers", {}).items():
            if _is_blocked(entry, now) is False and entry.get("blocked_until"):
                entry["blocked_until"] = None
                entry["consecutive_failures"] = 0
                cleared += 1
        if cleared:
            _save_local(state)
            _save_supabase(state)
        return cleared


# ─────────── Smart provider ordering ───────────

def get_smart_provider_order(
    candidate_providers: List[str],
    provider_keys: Dict[str, List[str]],
) -> List[str]:
    """
    Returns providers sorted by:
      1. Available providers first (not provider-blocked)
      2. Providers with most available keys first
      3. Providers that have never had quota issues first

    Skips providers with no API keys or no available keys entirely.
    """
    with _LOCK:
        scored: List[Tuple[str, int, int]] = []
        for prov in candidate_providers:
            all_keys = provider_keys.get(prov, [])
            if not all_keys:
                continue
            if not is_provider_available(prov):
                continue
            available, total = count_available_keys(prov, all_keys)
            if available == 0:
                # All keys blocked — skip this provider entirely
                continue
            # Score: prioritize providers with more available keys
            # (so we don't immediately jump to a provider with only 1 key left)
            status = get_provider_status(prov)
            quota_penalty = -50 if status.get("last_quota_exhausted_at") else 0
            score = available * 10 + quota_penalty
            scored.append((prov, score, available))

        # Sort descending by score
        scored.sort(key=lambda x: (-x[1], -x[2]))
        return [s[0] for s in scored]
