import os
from .config import Config, get_project_root


def touch(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8"):
            pass


def health_checks() -> dict:
    import shutil
    import sys
    checks = {
        "python": True,
        "python_exe": sys.executable,
        "ffmpeg_in_path": shutil.which("ffmpeg") is not None,
        "yt_dlp_import": False,
        "yt_dlp_version": None,
    }
    try:
        import yt_dlp
        checks["yt_dlp_import"] = True
        v = getattr(yt_dlp, "__version__", None)
        if not v:
            v = getattr(getattr(yt_dlp, "version", None), "__version__", None)
        checks["yt_dlp_version"] = v
    except Exception:
        checks["yt_dlp_import"] = False
    return checks


def summarize_config(cfg: Config) -> dict:
    return {
        "AUDIO_MODE": cfg.AUDIO_MODE,
        "OUTPUT_DIR": cfg.OUTPUT_DIR,
        "TEMP_DIR": cfg.TEMP_DIR,
        "REACTIONS_DIR": cfg.REACTIONS_DIR,
        "CHANNEL_LIST_PATH": cfg.CHANNEL_LIST_PATH,
        "TG_MODE": cfg.TG_MODE,
    }


def clear_mod_temp_dirs() -> None:
    """حدف الملفات المؤقتة الخاصة بإنشاء الفيديوهات"""
    import shutil
    import logging
    logger = logging.getLogger(__name__)
    
    project_root = get_project_root()

    # المجلدات المستهدفة
    dirs_to_clear = [
        os.path.join(project_root, ".temp", "mod_creation_uploads"),
        os.path.join(project_root, ".temp", "mod_long_videos"),
        os.path.join(project_root, ".output", "mod_long_videos")
    ]
    
    for dir_path in dirs_to_clear:
        if os.path.exists(dir_path):
            try:
                # حذف محتويات المجلد دون حذف المجلد نفسه لضمان استمرارية الوصول
                for item in os.listdir(dir_path):
                    item_path = os.path.join(dir_path, item)
                    try:
                        if os.path.isfile(item_path) or os.path.islink(item_path):
                            os.unlink(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    except Exception as e:
                        logger.warning(f"Could not delete {item_path}: {e}")
                logger.info(f"🧹 تم تنظيف المجلد: {dir_path}")
            except Exception as e:
                logger.error(f"Error clearing directory {dir_path}: {e}")
