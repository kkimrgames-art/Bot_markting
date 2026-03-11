import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class ResourceSnapshot:
    ram_total_mb: Optional[int] = None
    ram_available_mb: Optional[int] = None
    ram_used_percent: Optional[float] = None
    cpu_percent: Optional[float] = None


def _to_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _try_read_proc_meminfo() -> Tuple[Optional[int], Optional[int]]:
    try:
        path = "/proc/meminfo"
        if not os.path.exists(path):
            return None, None
        total_kb = None
        avail_kb = None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    parts = ln.split()
                    if len(parts) >= 2:
                        total_kb = int(float(parts[1]))
                elif ln.startswith("MemAvailable:"):
                    parts = ln.split()
                    if len(parts) >= 2:
                        avail_kb = int(float(parts[1]))
                if total_kb is not None and avail_kb is not None:
                    break
        if total_kb is None or avail_kb is None:
            return None, None
        return int(total_kb // 1024), int(avail_kb // 1024)
    except Exception:
        return None, None


def _try_windows_memory() -> Tuple[Optional[int], Optional[int]]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None, None
        total_mb = int(stat.ullTotalPhys // (1024 * 1024))
        avail_mb = int(stat.ullAvailPhys // (1024 * 1024))
        return total_mb, avail_mb
    except Exception:
        return None, None


def get_resource_snapshot() -> ResourceSnapshot:
    # Prefer psutil if available
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        ram_total_mb = int(vm.total // (1024 * 1024))
        ram_available_mb = int(vm.available // (1024 * 1024))
        ram_used_percent = float(vm.percent)

        cpu_percent = None
        try:
            # Very small sample; we don't want to block
            cpu_percent = float(psutil.cpu_percent(interval=0.1))
        except Exception:
            cpu_percent = None

        return ResourceSnapshot(
            ram_total_mb=ram_total_mb,
            ram_available_mb=ram_available_mb,
            ram_used_percent=ram_used_percent,
            cpu_percent=cpu_percent,
        )
    except Exception:
        pass

    # Fallback: Linux/Android /proc
    total_mb, avail_mb = _try_read_proc_meminfo()
    if total_mb is not None and avail_mb is not None and total_mb > 0:
        used_pct = 100.0 * float(total_mb - avail_mb) / float(total_mb)
        cpu_percent = None
        try:
            if hasattr(os, "getloadavg"):
                load1, _, _ = os.getloadavg()
                cpus = os.cpu_count() or 1
                cpu_percent = max(0.0, min(100.0, (float(load1) / float(cpus)) * 100.0))
        except Exception:
            cpu_percent = None
        return ResourceSnapshot(
            ram_total_mb=int(total_mb),
            ram_available_mb=int(avail_mb),
            ram_used_percent=float(used_pct),
            cpu_percent=cpu_percent,
        )

    # Fallback: Windows API
    total_mb, avail_mb = _try_windows_memory()
    if total_mb is not None and avail_mb is not None and total_mb > 0:
        used_pct = 100.0 * float(total_mb - avail_mb) / float(total_mb)
        return ResourceSnapshot(
            ram_total_mb=int(total_mb),
            ram_available_mb=int(avail_mb),
            ram_used_percent=float(used_pct),
            cpu_percent=None,
        )

    return ResourceSnapshot()


def should_defer_heavy_work(stage: str) -> Tuple[bool, str, int, Dict[str, Any]]:
    """Return (defer?, reason, retry_after_seconds, metrics)."""
    enabled = _to_bool(os.getenv("RESOURCE_GUARD_ENABLED", "1"), True)
    if not enabled:
        return False, "disabled", 0, {}

    # Defaults tuned for low-resource devices.
    try:
        max_ram_pct = float(os.getenv("MAX_RAM_PERCENT", "88") or 88)
    except Exception:
        max_ram_pct = 88.0
    try:
        min_avail_mb = int(os.getenv("MIN_AVAILABLE_RAM_MB", "400") or 400)
    except Exception:
        min_avail_mb = 400
    try:
        max_cpu_pct = float(os.getenv("MAX_CPU_PERCENT", "92") or 92)
    except Exception:
        max_cpu_pct = 92.0
    try:
        retry_after = int(os.getenv("RESOURCE_RETRY_SECONDS", "120") or 120)
    except Exception:
        retry_after = 120

    snap = get_resource_snapshot()
    metrics: Dict[str, Any] = {
        "stage": stage,
        "ram_total_mb": snap.ram_total_mb,
        "ram_available_mb": snap.ram_available_mb,
        "ram_used_percent": snap.ram_used_percent,
        "cpu_percent": snap.cpu_percent,
    }

    # If we couldn't measure anything, don't block work.
    if snap.ram_used_percent is None and snap.ram_available_mb is None and snap.cpu_percent is None:
        return False, "unknown_metrics", 0, metrics

    reasons = []
    if snap.ram_used_percent is not None and snap.ram_used_percent >= max_ram_pct:
        reasons.append(f"ram_used_percent={snap.ram_used_percent:.1f}%")
    if snap.ram_available_mb is not None and snap.ram_available_mb <= min_avail_mb:
        reasons.append(f"ram_available_mb={snap.ram_available_mb}MB")
    if snap.cpu_percent is not None and snap.cpu_percent >= max_cpu_pct:
        reasons.append(f"cpu_percent={snap.cpu_percent:.1f}%")

    if reasons:
        return True, "resource_guard:" + ",".join(reasons), max(30, retry_after), metrics

    return False, "ok", 0, metrics


def recommend_ffmpeg_threads() -> Optional[int]:
    """Return a safe default threads count for low-resource devices, unless user overrides."""
    # Explicit override wins
    env = (os.getenv("FFMPEG_THREADS") or "").strip()
    if env:
        try:
            v = int(env)
            if v > 0:
                return v
        except Exception:
            pass

    snap = get_resource_snapshot()
    cpus = os.cpu_count() or 4

    # Very conservative defaults for phones
    threads = 2
    if cpus >= 8:
        threads = 4
    elif cpus >= 6:
        threads = 3

    # If memory is very small, keep it low
    try:
        if snap.ram_total_mb is not None and snap.ram_total_mb <= 3000:
            threads = min(threads, 2)
    except Exception:
        pass

    # Allow env to force a cap without setting exact threads
    cap_env = (os.getenv("FFMPEG_THREADS_CAP") or "").strip()
    if cap_env:
        try:
            cap = int(cap_env)
            if cap > 0:
                threads = min(threads, cap)
        except Exception:
            pass

    return max(1, int(threads))
