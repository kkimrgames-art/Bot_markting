import logging
import subprocess
import os
import shutil
from typing import Optional
from .config import Config
from .ffmpeg_utils import ffmpeg_bin

logger = logging.getLogger(__name__)

# تم تعطيل فصل الموسيقى نهائياً - لا نحتاج لاستيراد demucs
# demucs يتطلب PyTorch والذي قد يسبب مشاكل في تحميل DLL
DEMUCS_AVAILABLE = False
 

def separate_audio_demucs(cfg: Config, input_path: str, output_dir: str) -> str:
    """Demucs separator (معطّل حالياً)"""
    logger.warning("⚠️ Demucs separation is currently disabled - returning original audio")
    return input_path


def separate_audio_light_mode(cfg: Config, input_path: str, output_dir: str) -> str:
    """Light mode separator (معطّل حالياً)"""
    logger.warning("⚠️ Light-mode separation is currently disabled - returning original audio")
    return input_path


def separate_audio_full_mode(cfg: Config, input_path: str, output_dir: str) -> str:
    """Full mode separator (معطّل حالياً)"""
    logger.warning("⚠️ Full-mode separation is currently disabled - returning original audio")
    # نحافظ على نفس الواجهة لكن لا نغيّر الصوت نهائياً
    return input_path


def separate_audio(cfg: Config, input_path: str, output_dir: str) -> str:
    """واجهة موحّدة لفصل الصوت (معطّلة حالياً).

    ترجع دائماً مسار الصوت الأصلي بدون أي فصل للموسيقى.
    """
    try:
        mode = (cfg.AUDIO_MODE or "light").lower()
    except Exception:
        mode = "light"

    if mode == "full":
        logger.info("Using FULL audio mode (separation DISABLED, returning original audio)")
        return separate_audio_full_mode(cfg, input_path, output_dir)
    else:
        logger.info("Using LIGHT audio mode (separation DISABLED, returning original audio)")
        return separate_audio_light_mode(cfg, input_path, output_dir)


def combine_audio_video(cfg: Config, video_path: str, audio_path: str, output_dir: str) -> str:
    """
    Combine processed audio with original video.
    
    Args:
        cfg: Configuration object
        video_path: Path to original video file
        audio_path: Path to processed audio file
        output_dir: Directory to save output files
        
    Returns:
        Path to final video file with processed audio
    """
    if not ffmpeg_bin():
        logger.warning("⚠️ FFmpeg not found. Cannot combine audio/video, returning original video.")
        return video_path

    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    output_path = os.path.join(output_dir, f"{base_name}_final.mp4")
    
    # Combine video and processed audio
    cmd = [
        ffmpeg_bin(), "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",  # Copy video stream without re-encoding
        "-c:a", "aac",   # Encode audio to AAC
        "-b:a", "128k",  # Audio bitrate
        "-shortest",     # Finish when the shortest stream ends
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to combine audio and video: {e}")
        raise RuntimeError(f"Failed to combine audio and video: {e}")