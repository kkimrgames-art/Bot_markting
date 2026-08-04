"""
Hibernation Manager
===================
Professional DB-failure hibernation system.

When the database (Supabase) becomes unreachable for an extended period:
  1. The bot enters HIBERNATION mode.
  2. ALL background tasks stop entirely (no cycles, no maintenance, no keep-alive).
  3. A background monitor task periodically pings the DB.
  4. When the DB recovers, the bot sends a "DB recovered" notification and
     automatically resumes all background tasks cleanly.

Key design properties:
  - SINGLE SOURCE OF TRUTH: a single asyncio.Event controls global hibernation.
  - FAST CHECK: every loop in the bot should call `await hibernation_manager.wait_if_hibernating()`
    at its top — this call returns immediately if not hibernating, or blocks
    until hibernation ends. No code changes needed inside the loop body.
  - NON-BLOCKING UI: Telegram handlers still work during hibernation (so admin
    can manually wake the bot or check status).
  - CLEAN RESUME: when DB recovers, the manager waits a few seconds for
    stability before unhibernating (avoids flapping).
  - PERSISTENT: hibernation state is persisted to local JSON so a process
    restart doesn't lose the hibernation state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────────────

# Global asyncio.Event — created lazily on first access (so it's bound to the
# running event loop). When set: bot is ACTIVE. When cleared: bot is HIBERNATING.
_hibernation_event: Optional[asyncio.Event] = None
_hibernation_event_lock = threading.Lock()

# Synchronous flag for non-async code paths (e.g. modules that can't await)
_is_hibernating_sync: bool = False
_sync_lock = threading.RLock()

# Hibernation metadata (reason, started_at, etc.)
_hibernation_meta: Dict[str, Any] = {}
_meta_lock = threading.RLock()

# Listeners that get notified when hibernation state changes
_listeners: List = []  # list of asyncio.Queue or callable


def _get_event() -> asyncio.Event:
    """Get (or create) the global hibernation asyncio.Event bound to the current loop."""
    global _hibernation_event
    with _hibernation_event_lock:
        if _hibernation_event is None:
            try:
                # Try to get the running loop
                loop = asyncio.get_running_loop()
                _hibernation_event = asyncio.Event()
                _hibernation_event.set()  # Start in ACTIVE mode
                logger.debug("💤 Hibernation event created (initial state: ACTIVE)")
            except RuntimeError:
                # No running loop — create one anyway (will be re-bound on first await)
                _hibernation_event = asyncio.Event()
                _hibernation_event.set()
        return _hibernation_event


def _state_file() -> Path:
    base = os.getenv("HIBERNATION_STATE_FILE") or ".data/hibernation_state.json"
    p = Path(base)
    if not os.path.isabs(str(p)):
        p = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, str(p))))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now().isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# Persistence (so a process restart preserves hibernation state)
# ──────────────────────────────────────────────────────────────────────────────

def _persist_state() -> None:
    """Save current hibernation state to local JSON."""
    with _meta_lock:
        state = {
            "is_hibernating": _is_hibernating_sync,
            "meta": dict(_hibernation_meta),
            "updated_at": _now_iso(),
        }
    p = _state_file()
    try:
        base_dir = str(p.parent)
        os.makedirs(base_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="hib_state_", suffix=".tmp", dir=base_dir)
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
        logger.error(f"Failed to persist hibernation state: {e}")


def _load_state() -> Dict[str, Any]:
    """Load hibernation state from local JSON (or return defaults)."""
    p = _state_file()
    if not p.exists():
        return {"is_hibernating": False, "meta": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return {
            "is_hibernating": bool(data.get("is_hibernating", False)),
            "meta": data.get("meta", {}) if isinstance(data.get("meta"), dict) else {},
            "updated_at": data.get("updated_at"),
        }
    except Exception as e:
        logger.warning(f"Failed to load hibernation state: {e}")
        return {"is_hibernating": False, "meta": {}}


def _restore_state_on_startup() -> None:
    """On module import, restore hibernation state from disk."""
    global _is_hibernating_sync
    state = _load_state()
    if state.get("is_hibernating"):
        meta = state.get("meta", {})
        # Only restore if the hibernation was recent (within last 24h)
        started_at = meta.get("started_at")
        if started_at:
            try:
                started_dt = datetime.fromisoformat(started_at)
                if datetime.now() - started_dt > timedelta(hours=24):
                    logger.info("💤 Stale hibernation state (>24h old) — clearing on startup")
                    _persist_state()
                    return
            except Exception:
                pass
        with _sync_lock:
            _is_hibernating_sync = True
        with _meta_lock:
            _hibernation_meta.clear()
            _hibernation_meta.update(meta)
        logger.warning(
            f"💤 Hibernation state RESTORED on startup "
            f"(reason: {meta.get('reason', 'unknown')}, started: {started_at})"
        )


# Restore on module import
try:
    _restore_state_on_startup()
except Exception as e:
    logger.warning(f"Failed to restore hibernation state on startup: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Public API — core hibernation control
# ──────────────────────────────────────────────────────────────────────────────

def is_hibernating() -> bool:
    """Check if the bot is currently in hibernation mode.

    This is a SYNCHRONOUS check — safe to call from any thread/context.
    """
    with _sync_lock:
        return _is_hibernating_sync


def get_hibernation_meta() -> Dict[str, Any]:
    """Return current hibernation metadata (reason, started_at, etc.)."""
    with _meta_lock:
        return dict(_hibernation_meta)


def enter_hibernation(reason: str = "Database unreachable", *, force: bool = False) -> bool:
    """Enter hibernation mode. Returns True if state changed, False if already hibernating.

    Args:
        reason: Human-readable reason for hibernation.
        force: If True, re-enters even if already hibernating (updates metadata).
    """
    global _is_hibernating_sync

    with _sync_lock:
        already = _is_hibernating_sync

    if already and not force:
        return False

    with _sync_lock:
        _is_hibernating_sync = True
    with _meta_lock:
        _hibernation_meta.clear()
        _hibernation_meta.update({
            "reason": reason,
            "started_at": _now_iso(),
            "forced": force,
        })

    # Clear the asyncio.Event so any `await wait_if_hibernating()` calls block
    try:
        ev = _get_event()
        ev.clear()
    except Exception as e:
        logger.warning(f"Failed to clear hibernation event: {e}")

    _persist_state()
    logger.warning(f"💤 BOT ENTERED HIBERNATION MODE — reason: {reason}")
    return True


def exit_hibernation(reason: str = "Database recovered") -> bool:
    """Exit hibernation mode. Returns True if state changed, False if not hibernating.

    Args:
        reason: Human-readable reason for exiting hibernation.
    """
    global _is_hibernating_sync

    with _sync_lock:
        was_hibernating = _is_hibernating_sync

    if not was_hibernating:
        return False

    with _sync_lock:
        _is_hibernating_sync = False
    with _meta_lock:
        started_at = _hibernation_meta.get("started_at")
        _hibernation_meta.clear()
        _hibernation_meta.update({
            "last_exit_reason": reason,
            "last_exit_at": _now_iso(),
            "last_started_at": started_at,
        })

    # Set the asyncio.Event so blocked wait_if_hibernating() calls unblock
    try:
        ev = _get_event()
        ev.set()
    except Exception as e:
        logger.warning(f"Failed to set hibernation event: {e}")

    _persist_state()
    logger.info(f"☀️ BOT EXITED HIBERNATION MODE — reason: {reason}")
    return True


async def wait_if_hibernating(timeout: Optional[float] = None) -> bool:
    """If the bot is hibernating, block until hibernation ends (or timeout).

    This is THE key function every background loop should call at its top:
        while True:
            await hibernation_manager.wait_if_hibernating()
            ... # normal loop body

    Args:
        timeout: max seconds to wait (None = wait forever).

    Returns:
        True if bot is active (either was already, or hibernation ended).
        False if timeout expired while still hibernating.
    """
    if not is_hibernating():
        return True

    ev = _get_event()
    try:
        if timeout is None:
            await ev.wait()
            return True
        else:
            try:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return not is_hibernating()
    except Exception as e:
        logger.warning(f"wait_if_hibernating error: {e}")
        return not is_hibernating()


# ──────────────────────────────────────────────────────────────────────────────
# DB health monitoring — runs as a background task
# ──────────────────────────────────────────────────────────────────────────────

# Track consecutive DB failures to trigger hibernation after a threshold
_consecutive_db_failures: int = 0
_failure_lock = threading.RLock()

# How many consecutive failures before entering hibernation
_HIBERNATION_FAILURE_THRESHOLD = int(os.getenv("HIBERNATION_FAILURE_THRESHOLD", "3") or "3")
# How often (in seconds) to ping the DB when in hibernation
_HIBERNATION_POLL_INTERVAL = int(os.getenv("HIBERNATION_POLL_INTERVAL", "30") or "30")
# How often (in seconds) to ping the DB when active (lightweight health check)
_ACTIVE_HEALTH_CHECK_INTERVAL = int(os.getenv("DB_HEALTH_CHECK_INTERVAL", "120") or "120")
# After DB recovers, wait this many seconds before exiting hibernation (stability window)
_RECOVERY_STABILITY_WINDOW = int(os.getenv("HIBERNATION_RECOVERY_WINDOW", "30") or "30")


def record_db_failure(error: Optional[Exception] = None) -> bool:
    """Called whenever a DB operation fails. Returns True if hibernation was triggered.

    Args:
        error: the exception that caused the failure (for logging/metadata).
    """
    global _consecutive_db_failures
    with _failure_lock:
        _consecutive_db_failures += 1
        count = _consecutive_db_failures

    if count >= _HIBERNATION_FAILURE_THRESHOLD and not is_hibernating():
        err_msg = str(error)[:200] if error else "Unknown error"
        reason = f"Database unreachable ({count} consecutive failures). Last error: {err_msg}"
        enter_hibernation(reason=reason)
        return True
    elif count < _HIBERNATION_FAILURE_THRESHOLD:
        logger.warning(
            f"⚠️ DB failure #{count}/{_HIBERNATION_FAILURE_THRESHOLD} "
            f"(will hibernate at {_HIBERNATION_FAILURE_THRESHOLD}): {error}"
        )
    return False


def record_db_success() -> None:
    """Called whenever a DB operation succeeds. Resets the failure counter."""
    global _consecutive_db_failures
    with _failure_lock:
        _consecutive_db_failures = 0


def get_failure_count() -> int:
    """Return the current consecutive DB failure count."""
    with _failure_lock:
        return _consecutive_db_failures


async def _ping_db() -> bool:
    """Lightweight DB connectivity check. Returns True if reachable."""
    try:
        from .supabase_client import USE_SUPABASE, is_online, reset_connection
        if not USE_SUPABASE:
            # Local-only mode — always "reachable"
            return True
        # Force a fresh check (bypass the cached is_online)
        # by resetting the connection check timestamp
        try:
            from . import supabase_client as _sc
            # Reset the last_connection_check so is_online() actually pings
            _sc._last_connection_check = 0
        except Exception:
            pass
        online = is_online()
        if online:
            record_db_success()
        return online
    except Exception as e:
        logger.debug(f"DB ping error: {e}")
        return False


async def db_health_monitor_loop():
    """Background task that monitors DB health and manages hibernation transitions.

    Behavior:
      - When ACTIVE: ping DB every _ACTIVE_HEALTH_CHECK_INTERVAL seconds.
        If DB fails, record_db_failure() is called; if threshold reached,
        enter_hibernation() is triggered.
      - When HIBERNATING: ping DB every _HIBERNATION_POLL_INTERVAL seconds.
        When DB recovers, wait _RECOVERY_STABILITY_WINDOW seconds, then
        exit_hibernation() and send admin notification.

    This task NEVER exits — it runs forever (supervised by TaskSupervisor).
    """
    logger.info(
        f"🩺 DB health monitor started "
        f"(active_check_interval={_ACTIVE_HEALTH_CHECK_INTERVAL}s, "
        f"hibernation_poll_interval={_HIBERNATION_POLL_INTERVAL}s, "
        f"failure_threshold={_HIBERNATION_FAILURE_THRESHOLD}, "
        f"recovery_window={_RECOVERY_STABILITY_WINDOW}s)"
    )

    recovery_confirm_count = 0
    last_notification_state = None  # Track to avoid spamming

    while True:
        try:
            online = await _ping_db()
            currently_hibernating = is_hibernating()

            if currently_hibernating:
                if online:
                    recovery_confirm_count += 1
                    logger.info(
                        f"🌞 DB appears recovered (#{recovery_confirm_count}/"
                        f"{max(1, _RECOVERY_STABILITY_WINDOW // _HIBERNATION_POLL_INTERVAL + 1)})"
                    )
                    # Wait for stability window before exiting hibernation
                    stability_checks_needed = max(1, _RECOVERY_STABILITY_WINDOW // _HIBERNATION_POLL_INTERVAL + 1)
                    if recovery_confirm_count >= stability_checks_needed:
                        # Confirm recovery — exit hibernation
                        meta = get_hibernation_meta()
                        started_at = meta.get("started_at")
                        duration_str = ""
                        if started_at:
                            try:
                                started_dt = datetime.fromisoformat(started_at)
                                duration = datetime.now() - started_dt
                                duration_str = f" (مدة السبات: {duration.total_seconds():.0f}s)"
                            except Exception:
                                pass

                        exit_hibernation(reason="Database recovered")
                        recovery_confirm_count = 0

                        # Send admin notification
                        try:
                            from .alert_system import get_alert_system
                            await get_alert_system().alert(
                                "info",
                                "☀️ عودة قاعدة البيانات للعمل",
                                f"✅ تم استرداد اتصال قاعدة البيانات بنجاح{duration_str}.\n"
                                f"🔄 تم إعادة تشغيل جميع المهام تلقائياً.\n"
                                f"📊 سيعمل البوت الآن بشكل طبيعي."
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send recovery notification: {e}")

                        last_notification_state = "recovered"
                        # After exit, immediately go back to active monitoring
                        await asyncio.sleep(_ACTIVE_HEALTH_CHECK_INTERVAL)
                        continue
                    else:
                        # Not yet stable — keep polling
                        await asyncio.sleep(_HIBERNATION_POLL_INTERVAL)
                        continue
                else:
                    # Still down — reset recovery counter
                    if recovery_confirm_count > 0:
                        logger.info("💤 DB went down again during recovery window — resetting counter")
                    recovery_confirm_count = 0
                    # Send hibernation notification (only once)
                    if last_notification_state != "hibernating":
                        try:
                            from .alert_system import get_alert_system
                            meta = get_hibernation_meta()
                            await get_alert_system().alert(
                                "critical",
                                "💤 دخول البوت في وضع السبات",
                                f"🛑 تم إيقاف جميع المهام بسبب تعطل قاعدة البيانات.\n\n"
                                f"📛 السبب: `{meta.get('reason', 'unknown')[:200]}`\n"
                                f"🕐 بدأ السبات: `{meta.get('started_at', '?')}`\n\n"
                                f"🔄 سيحاول البوت تلقائياً استئناف العمل عند عودة قاعدة البيانات.\n"
                                f"⏱ يتم الفحص كل `{_HIBERNATION_POLL_INTERVAL}` ثانية."
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send hibernation notification: {e}")
                        last_notification_state = "hibernating"
                    await asyncio.sleep(_HIBERNATION_POLL_INTERVAL)
                    continue
            else:
                # ACTIVE mode
                recovery_confirm_count = 0  # Reset
                if not online:
                    # DB failed while active — record it
                    triggered = record_db_failure(error=Exception("DB ping failed during active check"))
                    if triggered:
                        last_notification_state = None  # Will be set on next hibernating iteration
                else:
                    if last_notification_state == "hibernating":
                        # Edge case: hibernation was cleared externally (e.g. manual wake)
                        last_notification_state = None

                await asyncio.sleep(_ACTIVE_HEALTH_CHECK_INTERVAL)
                continue

        except asyncio.CancelledError:
            logger.info("🛑 DB health monitor task cancelled")
            raise
        except Exception as e:
            logger.error(f"💥 DB health monitor loop error: {e}", exc_info=True)
            await asyncio.sleep(30)  # Wait before retrying


# ──────────────────────────────────────────────────────────────────────────────
# Telegram UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_status_text() -> str:
    """Return Arabic status text for Telegram UI."""
    if is_hibernating():
        meta = get_hibernation_meta()
        started_at = meta.get("started_at", "?")
        reason = meta.get("reason", "غير معروف")
        duration_str = ""
        if started_at and started_at != "?":
            try:
                started_dt = datetime.fromisoformat(started_at)
                duration = datetime.now() - started_dt
                mins = int(duration.total_seconds() // 60)
                secs = int(duration.total_seconds() % 60)
                duration_str = f"{mins} دقيقة و {secs} ثانية"
            except Exception:
                pass
        return (
            "💤 <b>البوت في وضع السبات</b>\n\n"
            f"📛 السبب: <code>{reason[:200]}</code>\n"
            f"🕐 بدأ السبات: <code>{started_at}</code>\n"
            f"⏱ المدة: <code>{duration_str or 'غير معروف'}</code>\n"
            f"🔄 عدد فشل DB المتتالي: <code>{get_failure_count()}</code>\n\n"
            f"⏸ <b>جميع المهام متوقفة.</b>\n"
            f"🔁 سيحاول البوت استئناف العمل تلقائياً عند عودة DB.\n"
            f"⏱ يتم الفحص كل <code>{_HIBERNATION_POLL_INTERVAL}</code> ثانية."
        )
    else:
        meta = get_hibernation_meta()
        last_exit = meta.get("last_exit_at")
        last_exit_reason = meta.get("last_exit_reason", "")
        last_hibernation_duration = ""
        if meta.get("last_started_at") and last_exit:
            try:
                started_dt = datetime.fromisoformat(meta["last_started_at"])
                exited_dt = datetime.fromisoformat(last_exit)
                duration = exited_dt - started_dt
                mins = int(duration.total_seconds() // 60)
                secs = int(duration.total_seconds() % 60)
                last_hibernation_duration = f"{mins} دقيقة و {secs} ثانية"
            except Exception:
                pass
        text = (
            "☀️ <b>البوت يعمل بشكل طبيعي</b>\n\n"
            f"✅ قاعدة البيانات: <b>متصلة</b>\n"
            f"✅ جميع المهام: <b>نشطة</b>\n"
            f"✅ عداد الفشل: <code>{get_failure_count()}</code>\n"
        )
        if last_exit:
            text += (
                f"\n📊 <b>آخر سبات:</b>\n"
                f"🕐 انتهى: <code>{last_exit}</code>\n"
            )
            if last_exit_reason:
                text += f"📛 السبب: <code>{last_exit_reason[:100]}</code>\n"
            if last_hibernation_duration:
                text += f"⏱ المدة: <code>{last_hibernation_duration}</code>\n"
        return text


def force_wake() -> bool:
    """Manually exit hibernation (admin action). Returns True if was hibernating."""
    return exit_hibernation(reason="Manual wake by admin")


def force_hibernate(reason: str = "Manual hibernation by admin") -> bool:
    """Manually enter hibernation (admin action). Returns True if state changed."""
    return enter_hibernation(reason=reason, force=True)


def reset_failure_counter() -> None:
    """Manually reset the DB failure counter (admin action)."""
    global _consecutive_db_failures
    with _failure_lock:
        _consecutive_db_failures = 0
    logger.info("🔄 DB failure counter manually reset")


# ──────────────────────────────────────────────────────────────────────────────
# Initialization helper — call from main.py to start the monitor
# ──────────────────────────────────────────────────────────────────────────────

_monitor_task_started = False
_monitor_task_lock = threading.Lock()


def start_db_health_monitor() -> Optional[asyncio.Task]:
    """Start the DB health monitor as an asyncio task. Safe to call multiple times.

    Returns the asyncio.Task if started, or None if already running.
    """
    global _monitor_task_started
    with _monitor_task_lock:
        if _monitor_task_started:
            return None
        _monitor_task_started = True
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(db_health_monitor_loop())
        logger.info("🚀 DB health monitor task started")
        return task
    except RuntimeError:
        # No running loop — caller needs to start it from within async context
        logger.warning("⚠️ start_db_health_monitor() called outside async context — will start lazily")
        return None
