"""
DiskGuard — حماية من نفاد مساحة القرص على Render (حد ~512MB)

يراقب المساحة المتاحة، ينظف الملفات المؤقتة القديمة تلقائياً،
ويمنع التنزيل عند اقتراب نفاد المساحة.
"""
import os
import time
import shutil
import logging
from typing import Dict, Optional
from pathlib import Path

from .config import get_project_root

logger = logging.getLogger(__name__)

# ========== الحدود الافتراضية ==========
CRITICAL_THRESHOLD_MB = int(os.getenv("DISK_CRITICAL_MB", "50"))
WARNING_THRESHOLD_MB = int(os.getenv("DISK_WARNING_MB", "100"))
CLEANUP_MAX_AGE_HOURS = float(os.getenv("CLEANUP_MAX_AGE_HOURS", "2"))

# المجلدات المؤقتة المراد مراقبتها وتنظيفها
_PROJECT_ROOT = get_project_root()
CLEANUP_DIRS = [os.path.join(_PROJECT_ROOT, ".temp"), os.path.join(_PROJECT_ROOT, ".output")]


def _protected_empty_dirs() -> set[str]:
    return {
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".temp", "auto_mod"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".temp", "auto_mod_downloads"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".temp", "mods"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".temp", "renderer"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".output", "auto_mod_shorts"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".output", "auto_mod_long"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".output", "auto_mod_overlay"))),
        os.path.normcase(os.path.abspath(os.path.join(_PROJECT_ROOT, ".output", "auto_mod_facecam"))),
    }


def get_disk_usage() -> Dict[str, Optional[float]]:
    """الحصول على استخدام القرص بالميغابايت"""
    target_path = _PROJECT_ROOT
    try:
        stat = os.statvfs(target_path) if hasattr(os, "statvfs") else None
        if stat:
            total_mb = (stat.f_frsize * stat.f_blocks) / (1024 * 1024)
            free_mb = (stat.f_frsize * stat.f_bavail) / (1024 * 1024)
            used_mb = total_mb - free_mb
            return {
                "total_mb": round(total_mb, 1),
                "free_mb": round(free_mb, 1),
                "used_mb": round(used_mb, 1),
                "used_percent": round((used_mb / total_mb * 100) if total_mb > 0 else 0, 1),
            }
    except Exception:
        pass

    # Fallback: Windows
    try:
        import ctypes
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(target_path),
            None,
            ctypes.pointer(total_bytes),
            ctypes.pointer(free_bytes),
        )
        total_mb = total_bytes.value / (1024 * 1024)
        free_mb = free_bytes.value / (1024 * 1024)
        used_mb = total_mb - free_mb
        return {
            "total_mb": round(total_mb, 1),
            "free_mb": round(free_mb, 1),
            "used_mb": round(used_mb, 1),
            "used_percent": round((used_mb / total_mb * 100) if total_mb > 0 else 0, 1),
        }
    except Exception:
        pass

    # Fallback: shutil
    try:
        usage = shutil.disk_usage(target_path)
        total_mb = usage.total / (1024 * 1024)
        free_mb = usage.free / (1024 * 1024)
        used_mb = usage.used / (1024 * 1024)
        return {
            "total_mb": round(total_mb, 1),
            "free_mb": round(free_mb, 1),
            "used_mb": round(used_mb, 1),
            "used_percent": round((used_mb / total_mb * 100) if total_mb > 0 else 0, 1),
        }
    except Exception:
        return {"total_mb": None, "free_mb": None, "used_mb": None, "used_percent": None}


def get_temp_dirs_size_mb() -> float:
    """حجم المجلدات المؤقتة بالميغابايت"""
    total = 0
    for d in CLEANUP_DIRS:
        try:
            if os.path.isdir(d):
                for root, dirs, files in os.walk(d):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        except Exception:
            pass
    return round(total / (1024 * 1024), 2)


def cleanup_old_files(max_age_hours: float = None):
    """حذف الملفات الأقدم من max_age_hours ساعة"""
    max_age = max_age_hours or CLEANUP_MAX_AGE_HOURS
    cutoff = time.time() - (max_age * 3600)
    removed_count = 0
    freed_mb = 0
    protected_dirs = _protected_empty_dirs()

    for d in CLEANUP_DIRS:
        try:
            if not os.path.isdir(d):
                continue
            for root, dirs, files in os.walk(d, topdown=False):
                for f in files:
                    fpath = os.path.join(root, f)
                    try:
                        mtime = os.path.getmtime(fpath)
                        if mtime < cutoff:
                            size = os.path.getsize(fpath)
                            os.remove(fpath)
                            removed_count += 1
                            freed_mb += size / (1024 * 1024)
                    except OSError:
                        pass
                # حذف المجلدات الفارغة
                for dr in dirs:
                    dpath = os.path.join(root, dr)
                    try:
                        if os.path.normcase(os.path.abspath(dpath)) in protected_dirs:
                            continue
                        if not os.listdir(dpath):
                            os.rmdir(dpath)
                    except OSError:
                        pass
        except Exception:
            pass

    if removed_count > 0:
        logger.info(f"🧹 Disk cleanup: removed {removed_count} files, freed {freed_mb:.1f} MB")
    return removed_count, freed_mb


def emergency_cleanup():
    """حذف فوري لجميع الملفات المؤقتة — حالة طوارئ"""
    removed_count = 0
    freed_mb = 0
    for d in CLEANUP_DIRS:
        try:
            if os.path.isdir(d):
                for root, dirs, files in os.walk(d):
                    for f in files:
                        fpath = os.path.join(root, f)
                        try:
                            size = os.path.getsize(fpath)
                            os.remove(fpath)
                            removed_count += 1
                            freed_mb += size / (1024 * 1024)
                        except OSError:
                            pass
        except Exception:
            pass

    # تنظيف __pycache__ أيضاً
    for root, dirs, files in os.walk(".", topdown=False):
        if "__pycache__" in dirs:
            pycache_path = os.path.join(root, "__pycache__")
            try:
                shutil.rmtree(pycache_path, ignore_errors=True)
            except Exception:
                pass

    logger.warning(f"🚨 EMERGENCY cleanup: removed {removed_count} files, freed {freed_mb:.1f} MB")
    return removed_count, freed_mb


def should_allow_download() -> bool:
    """هل هناك مساحة كافية للتنزيل؟"""
    usage = get_disk_usage()
    free = usage.get("free_mb")
    if free is None:
        return True  # لا نستطيع قياس = نسمح

    if free < CRITICAL_THRESHOLD_MB:
        logger.warning(f"🚨 Disk CRITICAL: only {free:.0f}MB free. Running emergency cleanup...")
        emergency_cleanup()
        # إعادة الفحص بعد التنظيف
        usage = get_disk_usage()
        free = usage.get("free_mb", 0)
        if free < CRITICAL_THRESHOLD_MB:
            logger.error(f"🚨 Disk STILL critical after cleanup: {free:.0f}MB free. Blocking download.")
            return False

    if free < WARNING_THRESHOLD_MB:
        logger.warning(f"⚠️ Disk WARNING: only {free:.0f}MB free. Running cleanup...")
        cleanup_old_files(max_age_hours=0.5)  # 30 min

    return True


def status_dict() -> Dict:
    """ملخص حالة القرص"""
    usage = get_disk_usage()
    temp_size = get_temp_dirs_size_mb()
    free = usage.get("free_mb")
    level = "ok"
    if free is not None:
        if free < CRITICAL_THRESHOLD_MB:
            level = "critical"
        elif free < WARNING_THRESHOLD_MB:
            level = "warning"
    return {
        **usage,
        "temp_dirs_mb": temp_size,
        "level": level,
    }
