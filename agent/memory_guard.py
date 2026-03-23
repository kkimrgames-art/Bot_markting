"""
MemoryGuard — حماية من تسرب الذاكرة على المدى الطويل

يراقب استخدام RAM، يفرض garbage collection دوري،
ويمنع العمليات الثقيلة عند اقتراب الحد.
"""
import gc
import os
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

MAX_RSS_MB = int(os.getenv("MAX_RSS_MB", "400"))
WARNING_RSS_MB = int(os.getenv("WARNING_RSS_MB", "350"))
GC_INTERVAL_SECONDS = int(os.getenv("GC_INTERVAL_SECONDS", "300"))

_last_gc_time: float = 0.0
_gc_count: int = 0


def get_memory_usage() -> Dict[str, Optional[float]]:
    """الحصول على استخدام الذاكرة بالميغابايت"""
    # Try psutil first
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem = process.memory_info()
        vm = psutil.virtual_memory()
        return {
            "rss_mb": round(mem.rss / (1024 * 1024), 1),
            "vms_mb": round(mem.vms / (1024 * 1024), 1),
            "system_total_mb": round(vm.total / (1024 * 1024), 1),
            "system_available_mb": round(vm.available / (1024 * 1024), 1),
            "system_percent": vm.percent,
        }
    except Exception:
        pass

    # Fallback: /proc/self/status (Linux)
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    return {"rss_mb": round(rss_kb / 1024, 1)}
    except Exception:
        pass

    return {"rss_mb": None}


def force_gc():
    """إجبار garbage collection"""
    global _last_gc_time, _gc_count
    before = get_memory_usage().get("rss_mb", 0) or 0

    gc.collect()
    gc.collect()  # مرتين لأن بعض الكائنات لا تُحرر في المرة الأولى

    after = get_memory_usage().get("rss_mb", 0) or 0
    _last_gc_time = time.time()
    _gc_count += 1

    freed = before - after
    if freed > 1:
        logger.info(f"♻️ GC: freed {freed:.1f}MB (RSS: {before:.0f} → {after:.0f}MB)")
    return freed


def should_run_gc() -> bool:
    """هل حان وقت GC؟"""
    global _last_gc_time
    if time.time() - _last_gc_time < GC_INTERVAL_SECONDS:
        return False
    return True


def should_defer_heavy_work() -> tuple:
    """هل يجب تأجيل العمليات الثقيلة؟ (bool, reason, retry_seconds)"""
    mem = get_memory_usage()
    rss = mem.get("rss_mb")
    if rss is None:
        return False, "unknown", 0

    if rss >= MAX_RSS_MB:
        # محاولة GC أولاً
        freed = force_gc()
        rss_after = get_memory_usage().get("rss_mb", rss)
        if rss_after >= MAX_RSS_MB:
            return True, f"rss={rss_after:.0f}MB >= {MAX_RSS_MB}MB limit", 120
        logger.info(f"♻️ GC freed enough memory: {rss:.0f} → {rss_after:.0f}MB")
        return False, "ok_after_gc", 0

    if rss >= WARNING_RSS_MB:
        force_gc()
        return False, f"warning_rss={rss:.0f}MB", 0

    return False, "ok", 0


def periodic_maintenance():
    """صيانة دورية — يجب استدعاؤها كل بضع دقائق"""
    if should_run_gc():
        force_gc()


def status_dict() -> Dict:
    """ملخص حالة الذاكرة"""
    mem = get_memory_usage()
    rss = mem.get("rss_mb")
    level = "ok"
    if rss is not None:
        if rss >= MAX_RSS_MB:
            level = "critical"
        elif rss >= WARNING_RSS_MB:
            level = "warning"
    return {
        **mem,
        "max_rss_mb": MAX_RSS_MB,
        "level": level,
        "gc_count": _gc_count,
        "last_gc_ago_seconds": round(time.time() - _last_gc_time, 0) if _last_gc_time else None,
    }
