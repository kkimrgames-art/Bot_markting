"""
Daily Report Generator
=====================
Professional, varied, non-noisy daily reports for the bot admin.

Key design principles:
  - ONE report per day (not multiple). Sent at a configurable hour.
  - VARIED FORMATS: 4 different report templates rotate daily so the admin
    doesn't see the same message structure every day.
  - RICH CONTENT: each report includes different metrics (DB stats, AI usage,
    errors, hibernation events, preflight status, resource usage).
  - NON-NOISY: the daily report is the ONLY scheduled message. All other
    notifications are throttled/deduplicated via NotificationGate.
  - ACTIONABLE: each report highlights what needs attention (if anything)
    and what's healthy (so the admin knows things are fine).

Report types (rotate daily):
  - Type 1: "Performance Summary" — published/failed/processed counts,
    success rate, top channels, AI usage breakdown.
  - Type 2: "Health Check" — all subsystems status (DB, AI, disk, memory,
    hibernation, preflight), errors in last 24h, heartbeat status.
  - Type 3: "Activity Log" — recent actions (uploads, pauses, recoveries),
    cooldowns, hibernation events timeline.
  - Type 4: "Deep Dive" — detailed per-channel breakdown, per-source stats,
    error patterns, suggested actions.
"""
from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# State persistence — track last report time + rotation index
# ──────────────────────────────────────────────────────────────────────────────

_LOCK = threading.RLock()


def _state_file() -> Path:
    base = os.getenv("DAILY_REPORT_STATE_FILE") or ".data/daily_report_state.json"
    p = Path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_state() -> Dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {
            "last_report_at": None,
            "last_report_type": None,
            "rotation_index": 0,
            "reports_sent": 0,
            "history": [],  # list of {type, sent_at, summary}
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "history" not in data or not isinstance(data["history"], list):
            data["history"] = []
        return data
    except Exception as e:
        logger.warning(f"Failed to load daily report state: {e}")
        return {"last_report_at": None, "last_report_type": None, "rotation_index": 0,
                "reports_sent": 0, "history": []}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_file()
    try:
        base_dir = str(p.parent)
        os.makedirs(base_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="daily_rep_", suffix=".tmp", dir=base_dir)
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
        logger.error(f"Failed to save daily report state: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Report type rotation
# ──────────────────────────────────────────────────────────────────────────────

REPORT_TYPES = ["performance", "health", "activity", "deep_dive"]
REPORT_TYPE_NAMES = {
    "performance": "📊 تقرير الأداء",
    "health": "🩺 تقرير الصحة",
    "activity": "📝 تقرير النشاط",
    "deep_dive": "🔬 تقرير تفصيلي",
}


def _get_next_report_type() -> str:
    """Rotate through report types. Returns the next type to use."""
    with _LOCK:
        state = _load_state()
        idx = int(state.get("rotation_index", 0)) % len(REPORT_TYPES)
        next_type = REPORT_TYPES[idx]
        return next_type


def _advance_rotation() -> None:
    """Advance the rotation index and update state."""
    with _LOCK:
        state = _load_state()
        state["rotation_index"] = (int(state.get("rotation_index", 0)) + 1) % len(REPORT_TYPES)
        state["last_report_at"] = datetime.now().isoformat()
        state["reports_sent"] = int(state.get("reports_sent", 0)) + 1
        _save_state(state)


# ──────────────────────────────────────────────────────────────────────────────
# Data collectors — gather metrics from all subsystems
# ──────────────────────────────────────────────────────────────────────────────

def _collect_automod_stats() -> Dict[str, Any]:
    """Collect AutoMod stats (published/failed/processed counts)."""
    try:
        from .auto_mod_fetcher import AutoModDB, get_instance_id
        db = AutoModDB(get_instance_id())
        stats = db.get_stats(use_cache=False)
        return stats or {}
    except Exception as e:
        logger.debug(f"Failed to collect AutoMod stats: {e}")
        return {}


def _collect_ai_quota_stats() -> Dict[str, Any]:
    """Collect AI quota tracker status for all providers."""
    try:
        from . import ai_quota_tracker
        providers_status = {}
        for prov in ["gemini", "openrouter", "groq", "clarifai", "mistral"]:
            try:
                status = ai_quota_tracker.get_provider_status(prov)
                providers_status[prov] = status
            except Exception:
                providers_status[prov] = {"available": True, "blocked": False, "total_calls": 0}
        return providers_status
    except Exception as e:
        logger.debug(f"Failed to collect AI quota stats: {e}")
        return {}


def _collect_error_stats() -> Dict[str, Any]:
    """Collect error tracker stats."""
    try:
        from .error_tracker import get_error_tracker
        et = get_error_tracker()
        status = et.status_dict()
        recent = et.recent_errors(limit=5)
        return {"status": status, "recent": recent}
    except Exception as e:
        logger.debug(f"Failed to collect error stats: {e}")
        return {"status": {}, "recent": []}


def _collect_hibernation_stats() -> Dict[str, Any]:
    """Collect hibernation manager status."""
    try:
        from . import hibernation_manager
        return {
            "is_hibernating": hibernation_manager.is_hibernating(),
            "meta": hibernation_manager.get_hibernation_meta(),
            "failure_count": hibernation_manager.get_failure_count(),
        }
    except Exception as e:
        logger.debug(f"Failed to collect hibernation stats: {e}")
        return {}


def _collect_preflight_stats() -> Dict[str, Any]:
    """Collect preflight cooldown stats."""
    try:
        from . import preflight_checks
        cooldowns = preflight_checks.list_active_cooldowns()
        return {
            "active_cooldowns": len(cooldowns),
            "cooldowns": cooldowns[:5],  # top 5
        }
    except Exception as e:
        logger.debug(f"Failed to collect preflight stats: {e}")
        return {"active_cooldowns": 0, "cooldowns": []}


def _collect_resource_stats() -> Dict[str, Any]:
    """Collect disk/memory stats."""
    try:
        from .disk_guard import status_dict as disk_status
        from .memory_guard import status_dict as mem_status
        return {
            "disk": disk_status(),
            "memory": mem_status(),
        }
    except Exception as e:
        logger.debug(f"Failed to collect resource stats: {e}")
        return {}


def _collect_heartbeat_stats() -> Dict[str, Any]:
    """Collect heartbeat monitor status."""
    try:
        from .heartbeat import get_heartbeat_monitor
        hb = get_heartbeat_monitor()
        return hb.status_dict() or {}
    except Exception as e:
        logger.debug(f"Failed to collect heartbeat stats: {e}")
        return {}


def _collect_all() -> Dict[str, Any]:
    """Collect all subsystem stats in one call."""
    return {
        "automod": _collect_automod_stats(),
        "ai_quota": _collect_ai_quota_stats(),
        "errors": _collect_error_stats(),
        "hibernation": _collect_hibernation_stats(),
        "preflight": _collect_preflight_stats(),
        "resources": _collect_resource_stats(),
        "heartbeats": _collect_heartbeat_stats(),
        "collected_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Report formatters — 4 different templates
# ──────────────────────────────────────────────────────────────────────────────

def _format_performance_report(data: Dict[str, Any]) -> str:
    """Type 1: Performance Summary — focus on publishing stats + AI usage."""
    am = data.get("automod", {})
    ai = data.get("ai_quota", {})

    published = int(am.get("published", 0))
    failed = int(am.get("failed", 0))
    processing = int(am.get("processing", 0))
    total = published + failed + processing
    success_rate = (published / total * 100) if total > 0 else 0

    text = (
        "📊 *تقرير الأداء اليومي*\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "\n"
        "📈 *نشر الفيديوهات:*\n"
        f"  ✅ منشور: `{published}`\n"
        f"  ❌ فاشل: `{failed}`\n"
        f"  ⏳ قيد المعالجة: `{processing}`\n"
        f"  📊 معدل النجاح: `{success_rate:.1f}%`\n"
        "\n"
    )

    # Channels/Sources
    text += (
        "🌐 *البنية التحتية:*\n"
        f"  📺 القنوات: `{am.get('total_channels', 0)}`\n"
        f"  📡 المصادر: `{am.get('total_sources', 0)}`\n"
        f"  ⏰ الجداول: `{am.get('total_schedules', 0)}`\n"
        "\n"
    )

    # AI usage summary
    text += "🤖 *استخدام الذكاء الاصطناعي:*\n"
    total_ai_calls = 0
    for prov in ["gemini", "openrouter", "groq", "clarifai", "mistral"]:
        prov_status = ai.get(prov, {})
        calls = int(prov_status.get("total_calls", 0))
        successes = int(prov_status.get("total_successes", 0))
        if calls > 0:
            total_ai_calls += calls
            avail = "✅" if prov_status.get("available") else "⏸️"
            text += f"  {avail} {prov}: `{calls}` نداء ({successes} نجاح)\n"
    if total_ai_calls == 0:
        text += "  _لا استخدام في آخر 24 ساعة_\n"

    return text


def _format_health_report(data: Dict[str, Any]) -> str:
    """Type 2: Health Check — all subsystems status."""
    hib = data.get("hibernation", {})
    pf = data.get("preflight", {})
    errs = data.get("errors", {})
    res = data.get("resources", {})
    hb = data.get("heartbeats", {})

    text = (
        "🩺 *تقرير الصحة اليومي*\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "\n"
    )

    # Hibernation status
    if hib.get("is_hibernating"):
        meta = hib.get("meta", {})
        text += f"💤 السبات: *نشط* (السبب: `{meta.get('reason', '?')[:60]}`)\n"
    else:
        text += "☀️ السبات: *غير نشط* (طبيعي)\n"

    # Preflight cooldowns
    cd_count = pf.get("active_cooldowns", 0)
    if cd_count > 0:
        text += f"⏸ فترات الهدنة: `{cd_count}` قناة/مصدر\n"
    else:
        text += "✅ فترات الهدنة: لا يوجد\n"

    # Errors
    err_status = errs.get("status", {})
    total_24h = int(err_status.get("total_errors_24h", 0))
    total_1h = int(err_status.get("total_errors_1h", 0))
    text += (
        f"❌ الأخطاء (24س): `{total_24h}`\n"
        f"❌ الأخطاء (1س): `{total_1h}`\n"
    )

    # Problematic components
    components = err_status.get("components", {})
    problem_components = [(c, i) for c, i in components.items() if i.get("pattern") != "normal"]
    if problem_components:
        text += "\n⚠️ *مكونات تحتاج انتباه:*\n"
        for comp, info in problem_components[:5]:
            text += f"  • `{comp}`: {info.get('pattern', '?')} — {info.get('suggested_action', '')[:50]}\n"

    # Resources
    disk = res.get("disk", {})
    mem = res.get("memory", {})
    text += (
        "\n💾 *الموارد:*\n"
        f"  القرص: `{disk.get('free_mb', '?')}MB` حر ({disk.get('level', '?')})\n"
        f"  الذاكرة: `{mem.get('rss_mb', '?')}MB` ({mem.get('level', '?')})\n"
    )

    # Heartbeats
    if hb:
        text += "\n💓 *المهام:*\n"
        for name, info in list(hb.items())[:5]:
            healthy = info.get("healthy", False)
            icon = "✅" if healthy else "❌"
            last_beat = info.get("last_beat_ago_seconds", 0)
            text += f"  {icon} {name}: آخر نبضة `{last_beat:.0f}s`\n"

    return text


def _format_activity_report(data: Dict[str, Any]) -> str:
    """Type 3: Activity Log — recent actions + timeline."""
    am = data.get("automod", {})
    errs = data.get("errors", {})
    pf = data.get("preflight", {})
    hib = data.get("hibernation", {})

    text = (
        "📝 *تقرير النشاط اليومي*\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "\n"
    )

    # Recent activity summary
    published = int(am.get("published", 0))
    failed = int(am.get("failed", 0))
    text += (
        "📋 *ملخص اليوم:*\n"
        f"  📤 فيديوهات منشورة: `{published}`\n"
        f"  ⚠️ محاولات فاشلة: `{failed}`\n"
    )

    # Recent errors (last 5)
    recent_errs = errs.get("recent", [])
    if recent_errs:
        text += "\n🔍 *آخر الأخطاء:*\n"
        for e in recent_errs[:5]:
            comp = e.get("component", "?")
            msg = e.get("message", "")[:60]
            ago = int(e.get("ago_seconds", 0))
            mins = ago // 60
            text += f"  • `{comp}` ({mins}د): {msg}\n"
    else:
        text += "\n✅ *لا أخطاء في آخر 24 ساعة.*\n"

    # Active cooldowns
    cooldowns = pf.get("cooldowns", [])
    if cooldowns:
        text += "\n⏸ *قنوات في فترة هدنة:*\n"
        for cd in cooldowns[:5]:
            ch = cd.get("channel_id", "?")[:20]
            reason = (cd.get("reason") or "غير معروف")[:50]
            remaining = cd.get("seconds_remaining", 0)
            mins = remaining // 60
            text += f"  • `{ch}...` ({mins}د متبقي): {reason}\n"

    # Hibernation events
    hib_meta = hib.get("meta", {})
    if hib_meta.get("last_exit_at"):
        text += (
            "\n💤 *آخر سبات:*\n"
            f"  انتهى: `{hib_meta.get('last_exit_at', '?')[:19]}`\n"
            f"  السبب: `{hib_meta.get('last_exit_reason', '?')[:50]}`\n"
        )

    return text


def _format_deep_dive_report(data: Dict[str, Any]) -> str:
    """Type 4: Deep Dive — detailed per-provider AI + error patterns."""
    ai = data.get("ai_quota", {})
    errs = data.get("errors", {})
    am = data.get("automod", {})

    text = (
        "🔬 *التقرير التفصيلي اليومي*\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "\n"
    )

    # AI provider deep dive
    text += "🤖 *تحليل خدمات الذكاء الاصطناعي:*\n"
    for prov in ["gemini", "openrouter", "groq", "clarifai", "mistral"]:
        prov_status = ai.get(prov, {})
        calls = int(prov_status.get("total_calls", 0))
        successes = int(prov_status.get("total_successes", 0))
        blocked = prov_status.get("blocked", False)
        consec_fails = int(prov_status.get("consecutive_failures", 0))

        if calls == 0 and not blocked:
            text += f"  • {prov}: _غير مستخدم_\n"
            continue

        success_rate = (successes / calls * 100) if calls > 0 else 0
        icon = "⏸" if blocked else "✅"
        text += f"  {icon} *{prov}*: `{calls}` نداء | `{success_rate:.0f}%` نجاح"
        if consec_fails > 0:
            text += f" | `{consec_fails}` فشل متتالي"
        if blocked:
            until = prov_status.get("blocked_until", "?")
            text += f" | محظور حتى `{str(until)[:19]}`"
        text += "\n"

    # Error patterns
    components = errs.get("status", {}).get("components", {})
    if components:
        text += "\n📊 *أنماط الأخطاء:*\n"
        for comp, info in sorted(components.items()):
            pattern = info.get("pattern", "normal")
            errs_24h = int(info.get("errors_24h", 0))
            errs_1h = int(info.get("errors_1h", 0))
            icon = "✅" if pattern == "normal" else "⚠️"
            text += f"  {icon} `{comp}`: {pattern} (24س: {errs_24h}, 1س: {errs_1h})\n"

    # Processed stats
    total = int(am.get("total_processed", 0))
    published = int(am.get("published", 0))
    failed = int(am.get("failed", 0))
    text += (
        "\n📦 *إجمالي المعالجة:*\n"
        f"  الكلي: `{total}`\n"
        f"  منشور: `{published}`\n"
        f"  فاشل: `{failed}`\n"
    )

    return text


# ──────────────────────────────────────────────────────────────────────────────
# Main report generator
# ──────────────────────────────────────────────────────────────────────────────

def generate_report(report_type: Optional[str] = None) -> Tuple[str, str]:
    """Generate a daily report.

    Args:
        report_type: optional specific type. If None, rotates automatically.

    Returns:
        (report_type, report_text)
    """
    if report_type is None:
        report_type = _get_next_report_type()

    data = _collect_all()

    formatters = {
        "performance": _format_performance_report,
        "health": _format_health_report,
        "activity": _format_activity_report,
        "deep_dive": _format_deep_dive_report,
    }
    formatter = formatters.get(report_type, _format_performance_report)
    text = formatter(data)

    return report_type, text


async def send_daily_report_if_due(force: bool = False) -> bool:
    """Send the daily report if it's due (24h since last report).

    Args:
        force: if True, send regardless of timing.

    Returns:
        True if a report was sent, False otherwise.
    """
    with _LOCK:
        state = _load_state()
        last_report_at = state.get("last_report_at")

        if not force and last_report_at:
            try:
                last_dt = datetime.fromisoformat(last_report_at)
                elapsed = datetime.now() - last_dt
                report_interval = int(os.getenv("DAILY_REPORT_INTERVAL_HOURS", "24") or "24")
                if elapsed < timedelta(hours=report_interval):
                    return False
            except Exception:
                pass

    # Generate the report
    try:
        report_type, report_text = generate_report()
    except Exception as e:
        logger.error(f"Failed to generate daily report: {e}")
        return False

    # Send via AlertSystem (bypasses throttle — daily report is unique each day)
    try:
        from .alert_system import get_alert_system
        alert = get_alert_system()
        # Use a unique title per type so throttle doesn't block it
        title = REPORT_TYPE_NAMES.get(report_type, "📊 التقرير اليومي")
        # Force-send: temporarily bypass throttle by using a unique key
        sent = await alert._send_telegram(report_text)
        if sent:
            with _LOCK:
                state = _load_state()
                state["last_report_at"] = datetime.now().isoformat()
                state["last_report_type"] = report_type
                state["reports_sent"] = int(state.get("reports_sent", 0)) + 1
                # Add to history (keep last 30)
                history = state.get("history", [])
                history.append({
                    "type": report_type,
                    "sent_at": state["last_report_at"],
                    "summary": report_text[:100] + "...",
                })
                state["history"] = history[-30:]
                _save_state(state)
            _advance_rotation()
            logger.info(f"📤 Daily report sent (type: {report_type})")
            return True
        else:
            logger.warning("Failed to send daily report via Telegram")
            return False
    except Exception as e:
        logger.error(f"Failed to send daily report: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Notification gate — throttles non-critical notifications to reduce noise
# ──────────────────────────────────────────────────────────────────────────────

class NotificationGate:
    """Smart throttle for non-critical notifications.

    Categorizes notifications by importance:
      - 'critical': always sent (auth failure, channel paused, hibernation)
      - 'important': sent but throttled (max 1 per hour per category)
      - 'normal': throttled aggressively (max 1 per 3 hours per category)
      - 'verbose': usually suppressed (cycle start/end, source search)

    Also enforces a global hourly cap to prevent flooding.
    """

    def __init__(self):
        self._sent: Dict[str, float] = {}  # category -> last_sent_timestamp
        self._hourly_count: int = 0
        self._hourly_window_start: float = time.time()
        self._lock = threading.RLock()

        # Per-category throttle windows (seconds)
        self._throttle_windows = {
            "critical": 0,           # never throttle
            "important": 3600,       # 1 hour
            "normal": 3 * 3600,      # 3 hours
            "verbose": 6 * 3600,     # 6 hours (rarely sent)
        }

        # Global hourly cap (max notifications per hour, excluding critical)
        self._hourly_cap = int(os.getenv("NOTIFICATION_HOURLY_CAP", "10") or "10")

    def should_send(self, category: str, key: str) -> bool:
        """Check if a notification should be sent.

        Args:
            category: 'critical', 'important', 'normal', or 'verbose'
            key: unique key for this notification (e.g. "channel:abc123:auth_failed")

        Returns:
            True if the notification should be sent, False to throttle.
        """
        with self._lock:
            now = time.time()

            # Reset hourly window if needed
            if now - self._hourly_window_start > 3600:
                self._hourly_window_start = now
                self._hourly_count = 0

            # Critical: always send
            if category == "critical":
                return True

            # Check hourly cap (excluding critical)
            if self._hourly_count >= self._hourly_cap:
                logger.debug(f"🔕 Notification throttled (hourly cap reached): {key}")
                return False

            # Check per-category throttle
            throttle_key = f"{category}:{key}"
            window = self._throttle_windows.get(category, 3600)
            last_sent = self._sent.get(throttle_key, 0)
            if window > 0 and now - last_sent < window:
                logger.debug(f"🔕 Notification throttled (category={category}): {key}")
                return False

            # Update tracking
            self._sent[throttle_key] = now
            self._hourly_count += 1
            return True

    def reset(self) -> None:
        """Reset all throttle state (admin action)."""
        with self._lock:
            self._sent.clear()
            self._hourly_count = 0
            self._hourly_window_start = time.time()

    def status(self) -> Dict[str, Any]:
        """Return current gate status for UI."""
        with self._lock:
            now = time.time()
            window_elapsed = now - self._hourly_window_start
            window_remaining = max(0, 3600 - window_elapsed)
            return {
                "hourly_count": self._hourly_count,
                "hourly_cap": self._hourly_cap,
                "window_remaining_seconds": int(window_remaining),
                "throttled_keys": len(self._sent),
            }


# Singleton
_gate: Optional[NotificationGate] = None


def get_notification_gate() -> NotificationGate:
    global _gate
    if _gate is None:
        _gate = NotificationGate()
    return _gate


# ──────────────────────────────────────────────────────────────────────────────
# Public helper — wrapped send for non-critical notifications
# ──────────────────────────────────────────────────────────────────────────────

async def send_throttled_notification(
    category: str,
    key: str,
    title: str,
    details: str = "",
) -> bool:
    """Send a notification through the throttle gate.

    Args:
        category: 'critical', 'important', 'normal', or 'verbose'
        key: unique key for this notification
        title: alert title
        details: alert details

    Returns:
        True if sent, False if throttled.
    """
    gate = get_notification_gate()
    if not gate.should_send(category, key):
        return False

    try:
        from .alert_system import get_alert_system
        alert = get_alert_system()
        level_map = {
            "critical": "critical",
            "important": "warning",
            "normal": "info",
            "verbose": "info",
        }
        level = level_map.get(category, "info")
        # For throttled notifications, bypass AlertSystem's own throttle
        # by sending directly via _send_telegram
        icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        icon = icons.get(level, "📢")
        timestamp = datetime.now().strftime("%H:%M:%S")
        message = f"{icon} *{title}*\n🕐 `{timestamp}`\n\n{details}" if details else f"{icon} *{title}*\n🕐 `{timestamp}`"
        return await alert._send_telegram(message)
    except Exception as e:
        logger.warning(f"Failed to send throttled notification: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Telegram UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_report_status_text() -> str:
    """Return Arabic status text for Telegram UI."""
    with _LOCK:
        state = _load_state()

    last_at = state.get("last_report_at")
    last_type = state.get("last_report_type")
    reports_sent = int(state.get("reports_sent", 0))
    next_type = _get_next_report_type()

    text = (
        "📊 *حالة التقارير اليومية*\n\n"
        f"📤 التقارير المرسلة: `{reports_sent}`\n"
        f"🔄 نوع التقرير القادم: *{REPORT_TYPE_NAMES.get(next_type, '?')}*\n"
    )

    if last_at:
        try:
            last_dt = datetime.fromisoformat(last_at)
            elapsed = datetime.now() - last_dt
            hours = int(elapsed.total_seconds() // 3600)
            mins = int((elapsed.total_seconds() % 3600) // 60)
            text += (
                f"🕐 آخر تقرير: `{hours}س {mins}د` مضت\n"
                f"📋 نوعه: *{REPORT_TYPE_NAMES.get(last_type, '?')}*\n"
            )
        except Exception:
            text += f"🕐 آخر تقرير: `{last_at}`\n"
    else:
        text += "🕐 آخر تقرير: _لم يُرسل بعد_\n"

    # Notification gate status
    gate = get_notification_gate()
    gate_status = gate.status()
    text += (
        f"\n🔕 *بوابة الإشعارات:*\n"
        f"  رسائل آخر ساعة: `{gate_status['hourly_count']}/{gate_status['hourly_cap']}`\n"
        f"  مفاتيح مُخنقة: `{gate_status['throttled_keys']}`\n"
        f"  إعادة ضبط النافذة: `{gate_status['window_remaining_seconds']}s`\n"
    )

    # Rotation info
    text += (
        "\n🔄 *دورة أنواع التقارير:*\n"
        "  📊 الأداء → 🩺 الصحة → 📝 النشاط → 🔬 تفصيلي\n"
    )

    return text


def get_report_history(limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent report history."""
    with _LOCK:
        state = _load_state()
    history = state.get("history", [])
    return list(reversed(history[-limit:]))  # most recent first


async def send_report_now(report_type: Optional[str] = None) -> Tuple[bool, str]:
    """Send a report immediately (admin action).

    Args:
        report_type: optional specific type. If None, uses rotation.

    Returns:
        (success, message)
    """
    try:
        if report_type and report_type not in REPORT_TYPES:
            return False, f"نوع تقرير غير صالح: {report_type}"

        actual_type, text = generate_report(report_type=report_type)
        from .alert_system import get_alert_system
        alert = get_alert_system()
        sent = await alert._send_telegram(text)
        if sent:
            with _LOCK:
                state = _load_state()
                state["last_report_at"] = datetime.now().isoformat()
                state["last_report_type"] = actual_type
                state["reports_sent"] = int(state.get("reports_sent", 0)) + 1
                history = state.get("history", [])
                history.append({
                    "type": actual_type,
                    "sent_at": state["last_report_at"],
                    "summary": text[:100] + "...",
                })
                state["history"] = history[-30:]
                _save_state(state)
            if report_type is None:
                _advance_rotation()
            return True, f"✅ تم إرسال تقرير *{REPORT_TYPE_NAMES.get(actual_type, '?')}*"
        else:
            return False, "❌ فشل إرسال التقرير عبر Telegram"
    except Exception as e:
        return False, f"❌ خطأ: {e}"


def reset_notification_gate() -> None:
    """Reset the notification gate (admin action)."""
    get_notification_gate().reset()
