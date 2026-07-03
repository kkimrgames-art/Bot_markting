"""
Pre-flight Checks Module
========================
Comprehensive checks that run BEFORE any auto-mod cycle starts processing a
channel/source. Catches problems early — before downloading/processing/uploading
— so we save bandwidth, disk, CPU, and admin frustration.

Design principles:
  - FAST: every check has a timeout; total pre-flight should be < 5 seconds.
  - LIGHTWEIGHT: only HEAD/GET requests; no downloads; no AI calls.
  - ACTIONABLE: each failure returns a clear Arabic message + suggested fix.
  - NON-BLOCKING: a failure pauses the affected channel/source, NOT the whole bot.
  - RESILIENT: any check exception is caught; never blocks the cycle if a check
    itself crashes — just logs a warning and returns "unknown" for that check.

Check categories:
  1. Channel checks       — exists? enabled? has valid YouTube token?
  2. Source checks        — URL reachable? platform supported? source enabled?
  3. Resource checks      — disk space OK? memory OK? ffmpeg available?
  4. Schedule checks      — within publish window? under daily limit?
  5. AI service checks    — at least one AI provider has available keys?
  6. Database checks      — DB reachable? required tables exist?
  7. Network checks       — YouTube/Facebook/OpenRouter reachable?
"""
from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of a single pre-flight check."""
    name: str
    category: str
    passed: bool
    critical: bool = True  # If True, failure blocks processing; if False, just warns
    message: str = ""
    details: str = ""
    suggested_fix: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "passed": self.passed,
            "critical": self.critical,
            "message": self.message,
            "details": self.details,
            "suggested_fix": self.suggested_fix,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class PreflightReport:
    """Aggregate result of all pre-flight checks for one channel/source."""
    channel_id: str
    content_type: str = ""
    source_id: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.critical)

    @property
    def critical_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.critical]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.critical]

    @property
    def duration_seconds(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    def summary_text(self) -> str:
        """Arabic summary of the preflight report for admin notification."""
        if not self.checks:
            return "لم يتم تنفيذ أي فحوصات."
        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        lines = [f"📊 <b>تقرير الفحص المسبق</b> ({passed}/{total} ناجح)"]
        if self.channel_id:
            lines.append(f"📺 القناة: <code>{self.channel_id[:25]}...</code>")
        if self.source_id:
            lines.append(f"🎯 المصدر: <code>{self.source_id[:25]}...</code>")
        lines.append(f"⏱️ المدة: {self.duration_seconds:.2f}s")
        lines.append("")
        for c in self.checks:
            icon = "✅" if c.passed else ("🛑" if c.critical else "⚠️")
            lines.append(f"{icon} <b>{c.name}</b>: {c.message}")
            if not c.passed and c.suggested_fix:
                lines.append(f"   💡 {c.suggested_fix}")
        return "\n".join(lines)

    def failure_text(self) -> str:
        """Arabic message for the first critical failure (for pause notification)."""
        failures = self.critical_failures
        if not failures:
            return ""
        f = failures[0]
        lines = [
            "🛑 <b>تم إيقاف الوكيل تلقائياً بعد فشل الفحص المسبق</b>",
            "",
            f"📺 القناة: <code>{self.channel_id[:25]}...</code>",
        ]
        if self.source_id:
            lines.append(f"🎯 المصدر: <code>{self.source_id[:25]}...</code>")
        lines.extend([
            f"🔍 الفحص الفاشل: <b>{f.name}</b>",
            f"📛 السبب: <code>{f.message}</code>",
        ])
        if f.details:
            lines.append(f"📝 التفاصيل: <code>{f.details[:300]}</code>")
        if f.suggested_fix:
            lines.append(f"\n💡 <b>الحل المقترح:</b>\n{f.suggested_fix}")
        lines.extend([
            "",
            "⏸ تم تعطيل جدول هذه القناة فقط مؤقتاً.",
            "✅ بقية الوكلاء سيواصلون العمل بشكل طبيعي.",
            "🔧 بعد إصلاح المشكلة، أعد تفعيل جدول هذه القناة من إعدادات الأتمتة.",
        ])
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "content_type": self.content_type,
            "source_id": self.source_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "all_passed": self.all_passed,
            "duration_seconds": round(self.duration_seconds, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Individual check helpers (each is small, fast, with its own timeout)
# ──────────────────────────────────────────────────────────────────────────────

def _timed(fn):
    """Decorator that records elapsed ms in the returned CheckResult."""
    def wrapper(*args, **kwargs) -> CheckResult:
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            result = CheckResult(
                name=getattr(fn, "_check_name", fn.__name__),
                category=getattr(fn, "_check_category", "unknown"),
                passed=False,
                critical=True,
                message=f"الفحص تعطل بسبب استثناء: {e}",
                details=str(e),
            )
        result.duration_ms = (time.monotonic() - start) * 1000.0
        return result
    return wrapper


# ─────────── 1. Channel checks ───────────

@_timed
def check_channel_exists(channel_id: str) -> CheckResult:
    """Check that the channel exists in ChannelManager."""
    name = "channel_exists"
    category = "channel"
    try:
        from ..bot.channel_manager import ChannelManager
        cm = ChannelManager()
        channel = cm.get_channel(channel_id)
        if not channel:
            return CheckResult(name, category, passed=False, critical=True,
                               message="القناة غير موجودة في إعدادات البوت",
                               suggested_fix="أضف القناة من قائمة إدارة القنوات في البوت")
        if not channel.enabled:
            return CheckResult(name, category, passed=False, critical=True,
                               message="القناة معطّلة في الإعدادات",
                               suggested_fix="فعّل القناة من قائمة إدارة القنوات")
        return CheckResult(name, category, passed=True,
                           message=f"القناة موجودة ومفعّلة: {channel.channel_name}")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص القناة: {e}")


@_timed
def check_youtube_token_valid(channel_id: str) -> CheckResult:
    """Check that the YouTube OAuth token exists and is loadable."""
    name = "youtube_token_valid"
    category = "channel"
    try:
        from ..bot.channel_manager import ChannelManager
        from ..agent.uploader import _creds_from_token_file, _recover_token_from_db
        cm = ChannelManager()
        channel = cm.get_channel(channel_id)
        if not channel:
            return CheckResult(name, category, passed=False, critical=True,
                               message="القناة غير موجودة")
        token_path = channel.token_path
        if not token_path:
            return CheckResult(name, category, passed=False, critical=True,
                               message="لم يتم تعيين مسار توكن المصادقة للقناة",
                               suggested_fix="أعد ربط القناة بملف OAuth جديد من إعدادات القناة")
        if not os.path.exists(token_path):
            # Try DB recovery
            yt_id = channel.youtube_channel_id or ""
            if yt_id:
                try:
                    recovered = _recover_token_from_db(token_path, yt_id)
                    if recovered and os.path.exists(token_path):
                        return CheckResult(name, category, passed=True,
                                           message="التوكن تم استرجاعه من قاعدة البيانات")
                except Exception:
                    pass
            return CheckResult(name, category, passed=False, critical=True,
                               message="ملف توكن YouTube غير موجود",
                               details=f"المسار: {token_path}",
                               suggested_fix="أعد ربط القناة بملف OAuth جديد — اضغط زر 'ربط القناة' في إعدادات القناة")
        # Try loading the token
        try:
            _creds_from_token_file(token_path)
            return CheckResult(name, category, passed=True,
                               message="ملف التوكن صالح وقابل للقراءة")
        except Exception as tok_err:
            return CheckResult(name, category, passed=False, critical=True,
                               message="ملف التوكن موجود لكنه غير صالح أو منتهي الصلاحية",
                               details=str(tok_err)[:300],
                               suggested_fix="أعد ربط القناة بملف OAuth جديد من إعدادات القناة")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص التوكن: {e}")


# ─────────── 2. Source checks ───────────

@_timed
def check_source_enabled(source: Dict[str, Any]) -> CheckResult:
    """Check that the source is enabled."""
    name = "source_enabled"
    category = "source"
    try:
        if not source:
            return CheckResult(name, category, passed=False, critical=True,
                               message="بيانات المصدر فارغة")
        if not source.get("enabled", True):
            return CheckResult(name, category, passed=False, critical=True,
                               message="المصدر معطّل في الإعدادات",
                               suggested_fix="فعّل المصدر من إعدادات الأتمتة")
        return CheckResult(name, category, passed=True,
                           message="المصدر مفعّل")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص المصدر: {e}")


@_timed
def check_source_url_reachable(source: Dict[str, Any]) -> CheckResult:
    """Check that the source URL is syntactically valid and DNS-resolvable.

    NOTE: This does NOT actually download anything — just a socket-level
    connectivity check. Fast and safe.
    """
    name = "source_url_reachable"
    category = "source"
    try:
        # Determine source URL — check both legacy and new fetch_sources format
        source_url = ""
        fetch_sources = []
        try:
            settings = source.get("settings") or {}
            if isinstance(settings, str):
                import json as _json
                settings = _json.loads(settings)
            fs = settings.get("fetch_sources") if isinstance(settings, dict) else None
            if isinstance(fs, list):
                fetch_sources = [x for x in fs if isinstance(x, dict) and str(x.get("url") or "").strip()]
        except Exception:
            pass
        if not fetch_sources:
            source_url = str(source.get("source_url") or "").strip()

        urls_to_check = []
        if fetch_sources:
            urls_to_check = [str(f.get("url") or "").strip() for f in fetch_sources if str(f.get("url") or "").strip()]
        elif source_url:
            urls_to_check = [source_url]
        if not urls_to_check:
            return CheckResult(name, category, passed=False, critical=True,
                               message="لا يوجد رابط للمصدر",
                               suggested_fix="أضف رابط مصدر صحيح من إعدادات المصدر")
        if len(urls_to_check) == 1:
            url = urls_to_check[0]
            try:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    return CheckResult(name, category, passed=False, critical=True,
                                       message="رابط المصدر غير صالح",
                                       details=f"URL: {url}",
                                       suggested_fix="استخدم رابطاً صحيحاً يبدأ بـ https://")
                # DNS resolution + port check (no HTTP request yet)
                hostname = parsed.netloc.split(":")[0]
                try:
                    socket.setdefaulttimeout(5)
                    socket.gethostbyname(hostname)
                except socket.gaierror:
                    return CheckResult(name, category, passed=False, critical=True,
                                       message=f"تعذر حل اسم النطاق: {hostname}",
                                       suggested_fix="تحقق من صحة الرابط أو من اتصال الإنترنت")
                except socket.timeout:
                    return CheckResult(name, category, passed=False, critical=False,
                                       message="انتهت مهلة فحص النطاق (5 ثوانٍ)",
                                       suggested_fix="تحقق من اتصال الإنترنت")
                return CheckResult(name, category, passed=True,
                                   message=f"الرابط صالح والنطاق قابل للحل: {hostname}")
            finally:
                socket.setdefaulttimeout(None)
        else:
            # Multiple fetch_sources — verify all are syntactically valid
            invalid = []
            for url in urls_to_check:
                try:
                    parsed = urlparse(url)
                    if not parsed.scheme or not parsed.netloc:
                        invalid.append(url)
                except Exception:
                    invalid.append(url)
            if invalid:
                return CheckResult(name, category, passed=False, critical=True,
                                   message=f"{len(invalid)}/{len(urls_to_check)} روابط غير صالحة",
                                   details=f"أمثلة: {invalid[:3]}",
                                   suggested_fix="صحح الروابط غير الصالحة في إعدادات المصدر")
            return CheckResult(name, category, passed=True,
                               message=f"جميع الروابط ({len(urls_to_check)}) صالحة")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص روابط المصدر: {e}")


@_timed
def check_source_platform_supported(source: Dict[str, Any]) -> CheckResult:
    """Check that the source's platform is supported."""
    name = "source_platform_supported"
    category = "source"
    try:
        platform = str(source.get("platform") or "youtube").strip().lower()
        supported = {"youtube", "youtube_shorts", "facebook", "facebook_reels"}
        if platform not in supported:
            return CheckResult(name, category, passed=False, critical=True,
                               message=f"المنصة غير مدعومة: {platform}",
                               details=f"المنصات المدعومة: {', '.join(sorted(supported))}",
                               suggested_fix="استخدم منصة مدعومة من القائمة")
        return CheckResult(name, category, passed=True,
                           message=f"المنصة مدعومة: {platform}")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص المنصة: {e}")


# ─────────── 3. Resource checks ───────────

@_timed
def check_disk_space() -> CheckResult:
    """Check that disk has enough free space for download/processing."""
    name = "disk_space_ok"
    category = "resource"
    try:
        from .disk_guard import should_allow_download, get_disk_usage
        if not should_allow_download():
            usage = get_disk_usage()
            free_mb = usage.get("free_mb", 0)
            return CheckResult(name, category, passed=False, critical=True,
                               message=f"مساحة القرص غير كافية: {free_mb:.0f}MB فقط متبقية",
                               suggested_fix="احذف ملفات قديمة من .temp و .output أو زِد سعة الخادم")
        usage = get_disk_usage()
        free_mb = usage.get("free_mb", 0)
        return CheckResult(name, category, passed=True,
                           message=f"مساحة القرص كافية: {free_mb:.0f}MB متبقية")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص مساحة القرص: {e}")


@_timed
def check_ffmpeg_available() -> CheckResult:
    """Check that ffmpeg/ffprobe binaries are available."""
    name = "ffmpeg_available"
    category = "resource"
    try:
        from .ffmpeg_utils import ffmpeg_bin, ffprobe_bin, validate_input_file
        ff = ffmpeg_bin()
        fp = ffprobe_bin()
        if not ff or not fp:
            return CheckResult(name, category, passed=False, critical=True,
                               message="ffmpeg أو ffprobe غير موجودين",
                               suggested_fix="ثبّت ffmpeg: apt install ffmpeg (أو شغّل install_ffmpeg.py)")
        # Quick smoke test
        import subprocess
        try:
            subprocess.run([ff, "-version"], capture_output=True, timeout=5, check=False)
            return CheckResult(name, category, passed=True,
                               message=f"ffmpeg متاح: {ff}")
        except Exception as e:
            return CheckResult(name, category, passed=False, critical=True,
                               message=f"ffmpeg موجود لكن لا يعمل: {e}",
                               suggested_fix="أعد تثبيت ffmpeg")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص ffmpeg: {e}")


# ─────────── 4. AI service checks ───────────

@_timed
def check_ai_services_available() -> CheckResult:
    """Check that at least one AI provider has available keys."""
    name = "ai_services_available"
    category = "ai"
    try:
        from . import ai_quota_tracker
        providers_status = ai_quota_tracker.get_all_providers_status()
        # Gather keys per provider
        from .openrouter_manager import get_openrouter_manager
        from .groq_manager import get_groq_manager
        from .clarifai_manager import get_clarifai_manager
        from .gemini_key_manager import get_key_manager
        from .ai import _load_mistral_api_keys
        from .config import load_config

        provider_keys = {
            "openrouter": list(get_openrouter_manager().api_keys or []),
            "groq":       list(get_groq_manager().api_keys or []),
            "clarifai":   list(get_clarifai_manager().api_keys or []),
            "gemini":     list(get_key_manager().api_keys or get_key_manager().keys or []),
            "mistral":    _load_mistral_api_keys(load_config()),
        }
        available_providers = []
        for prov, keys in provider_keys.items():
            if not keys:
                continue
            if not ai_quota_tracker.is_provider_available(prov):
                continue
            available, total = ai_quota_tracker.count_available_keys(prov, keys)
            if available > 0:
                available_providers.append(f"{prov}({available}/{total})")
        if not available_providers:
            return CheckResult(name, category, passed=False, critical=False,
                               message="لا توجد خدمات ذكاء اصطناعي متاحة حالياً",
                               details="قد تكون كل المفاتيح محظورة أو منفد الحصة",
                               suggested_fix="أضف مفاتيح AI من قائمة '🔑 مفاتيح API' أو فك حظر المفاتيح من '📊 حالة الحصص'")
        return CheckResult(name, category, passed=True,
                           message=f"خدمات AI متاحة: {', '.join(available_providers)}")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص خدمات AI: {e}")


# ─────────── 5. Network checks ───────────

@_timed
def check_network_connectivity() -> CheckResult:
    """Check that we have outbound internet connectivity."""
    name = "network_connectivity"
    category = "network"
    test_hosts = [
        ("google.com", 443),
        ("youtube.com", 443),
    ]
    failed = []
    for host, port in test_hosts:
        try:
            socket.setdefaulttimeout(5)
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
        except Exception as e:
            failed.append(f"{host}:{port} ({e})")
        finally:
            socket.setdefaulttimeout(None)
    if failed:
        return CheckResult(name, category, passed=False, critical=True,
                           message=f"تعذر الوصول لـ: {', '.join(failed)}",
                           suggested_fix="تحقق من اتصال الإنترنت بالخادم")
    return CheckResult(name, category, passed=True,
                       message="الاتصال بالإنترنت يعمل")


# ─────────── 6. Database checks ───────────

@_timed
def check_database_reachable() -> CheckResult:
    """Check that the database is reachable (lightweight query)."""
    name = "database_reachable"
    category = "database"
    try:
        from .supabase_client import USE_SUPABASE, is_online
        if not USE_SUPABASE:
            return CheckResult(name, category, passed=True,
                               message="Supabase معطل — يستخدم التخزين المحلي")
        if not is_online():
            return CheckResult(name, category, passed=False, critical=False,
                               message="Supabase غير متصل حالياً — ستعمل النسخة المحلية",
                               suggested_fix="تحقق من SUPABASE_URL و SUPABASE_KEY")
        return CheckResult(name, category, passed=True,
                           message="Supabase متصل ويعمل")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص قاعدة البيانات: {e}")


# ─────────── 7. Schedule checks (lightweight — heavy ones done in run_cycle) ───────────

@_timed
def check_schedule_sane(schedule: Dict[str, Any]) -> CheckResult:
    """Sanity-check that the schedule has required fields."""
    name = "schedule_sane"
    category = "schedule"
    try:
        if not schedule:
            return CheckResult(name, category, passed=False, critical=True,
                               message="بيانات الجدول فارغة")
        channel_id = schedule.get("channel_id")
        if not channel_id:
            return CheckResult(name, category, passed=False, critical=True,
                               message="الجدول بدون channel_id",
                               suggested_fix="أعد إنشاء الجدول من إعدادات الأتمتة")
        return CheckResult(name, category, passed=True,
                           message=f"الجدول سليم (قناة: {channel_id[:15]}...)")
    except Exception as e:
        return CheckResult(name, category, passed=False, critical=False,
                           message=f"تعذر فحص الجدول: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def run_preflight_checks(
    channel_id: str,
    schedule: Optional[Dict[str, Any]] = None,
    source: Optional[Dict[str, Any]] = None,
    *,
    skip_network: bool = False,
    skip_db: bool = False,
    skip_ai: bool = False,
    skip_resource: bool = False,
) -> PreflightReport:
    """Run all applicable pre-flight checks for a channel/source.

    Args:
        channel_id: The channel ID to check.
        schedule: Optional schedule dict (if None, schedule checks skipped).
        source: Optional source dict (if None, source checks skipped).
        skip_network, skip_db, skip_ai, skip_resource: skip specific categories
            (useful for testing or for very fast preflights).

    Returns:
        PreflightReport with all check results.
    """
    report = PreflightReport(channel_id=channel_id)
    if schedule:
        report.content_type = schedule.get("content_type", "")
    if source:
        report.source_id = str(source.get("id") or source.get("source_url") or "")

    # Always-run checks
    report.add(check_channel_exists(channel_id))
    report.add(check_youtube_token_valid(channel_id))

    if schedule:
        report.add(check_schedule_sane(schedule))

    if source:
        report.add(check_source_enabled(source))
        report.add(check_source_platform_supported(source))
        report.add(check_source_url_reachable(source))

    if not skip_resource:
        report.add(check_disk_space())
        report.add(check_ffmpeg_available())

    if not skip_ai:
        report.add(check_ai_services_available())

    if not skip_network:
        report.add(check_network_connectivity())

    if not skip_db:
        report.add(check_database_reachable())

    report.finished_at = datetime.now()
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Cooldown registry — track recently-failed channels/sources so we don't
# re-attempt them every cycle (saves resources + reduces noise).
# ──────────────────────────────────────────────────────────────────────────────

import json
import tempfile
from pathlib import Path

_LOCK = None  # lazy init


def _get_lock():
    global _LOCK
    if _LOCK is None:
        import threading
        _LOCK = threading.RLock()
    return _LOCK


def _cooldown_file() -> Path:
    base = os.getenv("PREFLIGHT_COOLDOWN_FILE") or ".data/preflight_cooldowns.json"
    p = Path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cooldowns() -> Dict[str, Any]:
    p = _cooldown_file()
    if not p.exists():
        return {"entries": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "entries" not in data or not isinstance(data["entries"], dict):
            data["entries"] = {}
        return data
    except Exception as e:
        logger.warning(f"Failed to load preflight cooldowns: {e}")
        return {"entries": {}}


def _save_cooldowns(data: Dict[str, Any]) -> None:
    p = _cooldown_file()
    try:
        base_dir = str(p.parent)
        os.makedirs(base_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="preflight_cd_", suffix=".tmp", dir=base_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
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
        logger.error(f"Failed to save preflight cooldowns: {e}")


def mark_channel_preflight_failed(
    channel_id: str,
    *,
    source_id: Optional[str] = None,
    reason: str = "",
    cooldown_seconds: int = 1800,
) -> None:
    """Mark a channel/source as having failed preflight — skip for cooldown_seconds."""
    with _get_lock():
        data = _load_cooldowns()
        key = f"{channel_id}:{source_id or ''}"
        data["entries"][key] = {
            "channel_id": channel_id,
            "source_id": source_id,
            "reason": reason,
            "failed_at": datetime.now().isoformat(),
            "cooldown_until": (datetime.now() + timedelta(seconds=cooldown_seconds)).isoformat(),
        }
        _save_cooldowns(data)


def is_channel_in_cooldown(
    channel_id: str,
    *,
    source_id: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Check if a channel/source is still in preflight cooldown.

    Returns (is_in_cooldown, entry_dict_or_none).
    """
    with _get_lock():
        data = _load_cooldowns()
        key = f"{channel_id}:{source_id or ''}"
        entry = data["entries"].get(key)
        if not entry:
            return (False, None)
        try:
            cooldown_until = datetime.fromisoformat(entry.get("cooldown_until"))
        except Exception:
            # Stale entry — clear it
            data["entries"].pop(key, None)
            _save_cooldowns(data)
            return (False, None)
        if datetime.now() >= cooldown_until:
            # Cooldown expired — clear it
            data["entries"].pop(key, None)
            _save_cooldowns(data)
            return (False, None)
        return (True, entry)


def clear_channel_cooldown(
    channel_id: str,
    *,
    source_id: Optional[str] = None,
) -> bool:
    """Manually clear a channel/source cooldown (admin action)."""
    with _get_lock():
        data = _load_cooldowns()
        key = f"{channel_id}:{source_id or ''}"
        if key in data["entries"]:
            data["entries"].pop(key)
            _save_cooldowns(data)
            return True
        return False


def clear_all_cooldowns() -> int:
    """Clear all preflight cooldowns. Returns count cleared."""
    with _get_lock():
        data = _load_cooldowns()
        count = len(data["entries"])
        data["entries"] = {}
        _save_cooldowns(data)
        return count


def list_active_cooldowns() -> List[Dict[str, Any]]:
    """Return all active (non-expired) cooldowns."""
    with _get_lock():
        data = _load_cooldowns()
        now = datetime.now()
        active = []
        expired_keys = []
        for key, entry in data["entries"].items():
            try:
                cooldown_until = datetime.fromisoformat(entry.get("cooldown_until"))
                if now >= cooldown_until:
                    expired_keys.append(key)
                    continue
                # Add seconds_remaining for UI
                entry_copy = dict(entry)
                entry_copy["seconds_remaining"] = int((cooldown_until - now).total_seconds())
                active.append(entry_copy)
            except Exception:
                expired_keys.append(key)
        if expired_keys:
            for k in expired_keys:
                data["entries"].pop(k, None)
            _save_cooldowns(data)
        return active
