"""
ظ…ط¹ط§ظ„ط¬ ظپظٹط¯ظٹظˆظ‡ط§طھ ط§ظ„ظ…ظˆط¯ط§طھ - ظ†ط¸ط§ظ… ظ…ظ†ظپطµظ„ ط¹ظ† ط§ظ„ظ…ط­طھظˆظ‰ ط§ظ„ط­ط§ظ„ظٹ
ظٹطھط¶ظ…ظ†: ظ‚طµ ط§ظ„ط«ظˆط§ظ†ظٹ ط§ظ„ط£ظˆظ„ظ‰ ظˆط§ظ„ط£ط®ظٹط±ط©طŒ ط¥ط¶ط§ظپط© ظ†طµ ط¯ط¹ظˆط©طŒ طھط­ظˆظٹظ„ ظ„ط´ظˆط±طھط³
"""
import os
import subprocess
import logging
import time
import re
import uuid
import hashlib
import queue
import threading
from typing import Optional, Tuple, Dict, Any
from collections import deque
from pathlib import Path
from .ffmpeg_utils import ffmpeg_bin, ffprobe_bin
from .config import load_config

try:
    from fontTools.ttLib import TTFont
    HAS_FONTTOOLS = True
except Exception:
    TTFont = None
    HAS_FONTTOOLS = False

# ظ…ظƒطھط¨ط§طھ ظ…ط¹ط§ظ„ط¬ط© ط§ظ„ظ„ط؛ط© ط§ظ„ط¹ط±ط¨ظٹط© ًں†•
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_ARABIC_SUPPORT = True
except ImportError:
    HAS_ARABIC_SUPPORT = False

logger = logging.getLogger(__name__)


def _terminate_subprocess_tree(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    for stop in ("terminate", "kill"):
        try:
            getattr(proc, stop)()
        except Exception:
            continue
        try:
            proc.wait(timeout=8)
            return
        except Exception:
            continue


def _run_ffmpeg_with_idle_timeout(
    cmd: list[str],
    *,
    timeout_s: int = 300,
    idle_timeout_s: int = 90,
    label: str = "FFmpeg",
) -> Tuple[int, str]:
    """Run an FFmpeg command with both hard-timeout AND idle-timeout.

    Unlike ``subprocess.run(timeout=N)`` which only enforces a hard wall-clock
    timeout, this function monitors stderr output and kills the process if
    **no output at all** is produced for ``idle_timeout_s`` seconds.  This is
    critical on Render free-tier where FFmpeg can stall at 0% CPU due to
    resource throttling, causing the bot to appear frozen for the entire
    hard-timeout duration.

    Returns (returncode, stderr_tail).  returncode == -1 indicates a timeout.
    """
    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creation_flag:
            popen_kwargs["creationflags"] = creation_flag
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    stderr_parts: deque[bytes] = deque(maxlen=200)
    stderr_lock = threading.Lock()
    stderr_total_bytes = 0

    def _stderr_tail_text() -> str:
        with stderr_lock:
            snapshot = list(stderr_parts)
        return b"".join(snapshot).decode(errors="ignore")[-2500:]

    # Background thread to drain stderr so the pipe buffer never fills up
    # (which would block FFmpeg and cause a deadlock).
    drain_done = threading.Event()

    def _drain_stderr() -> None:
        nonlocal stderr_total_bytes
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                with stderr_lock:
                    stderr_parts.append(chunk)
                    stderr_total_bytes += len(chunk)
        except Exception:
            pass
        finally:
            drain_done.set()
            try:
                proc.stderr.close()
            except Exception:
                pass

    drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
    drain_thread.start()

    start_ts = time.monotonic()
    last_activity_ts = start_ts
    last_output_len = 0

    while True:
        try:
            proc.wait(timeout=2.0)
            break  # Process finished
        except subprocess.TimeoutExpired:
            pass

        now = time.monotonic()

        # Hard timeout
        if timeout_s > 0 and (now - start_ts) >= timeout_s:
            logger.warning(f"âڈ° [{label}] Hard timeout after {int(now - start_ts)}s â€” killing FFmpeg")
            _terminate_subprocess_tree(proc)
            drain_done.wait(timeout=5)
            stderr_text = _stderr_tail_text()
            return -1, stderr_text

        # Idle timeout: check if stderr has produced new output
        with stderr_lock:
            current_len = stderr_total_bytes
        if current_len != last_output_len:
            last_output_len = current_len
            last_activity_ts = now
        elif idle_timeout_s > 0 and (now - last_activity_ts) >= idle_timeout_s:
            logger.warning(
                f"âڈ° [{label}] Idle timeout â€” no output for {int(now - last_activity_ts)}s â€” killing FFmpeg"
            )
            _terminate_subprocess_tree(proc)
            drain_done.wait(timeout=5)
            stderr_text = _stderr_tail_text()
            return -1, stderr_text

    drain_done.wait(timeout=5)
    stderr_text = _stderr_tail_text()
    return int(proc.returncode or 0), stderr_text


def _run_ffmpeg_command_with_progress(
    cmd: list[str],
    *,
    timeout_s: int,
    idle_timeout_s: Optional[int] = None,
    progress_label: str = "FFmpeg",
) -> Tuple[int, str, str]:
    progress_cmd = list(cmd)
    try:
        if "-progress" not in progress_cmd:
            progress_cmd[1:1] = ["-progress", "pipe:1"]
        if "-nostats" not in progress_cmd:
            progress_cmd[1:1] = ["-nostats"]
    except Exception:
        pass

    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore",
        "bufsize": 1,
    }
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creation_flag:
            popen_kwargs["creationflags"] = creation_flag
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(progress_cmd, **popen_kwargs)
    events: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
    stderr_tail_parts: deque[str] = deque(maxlen=160)
    stderr_tail_lock = threading.Lock()

    def _stderr_tail_text() -> str:
        with stderr_tail_lock:
            snapshot = list(stderr_tail_parts)
        return "\n".join(snapshot)[-2500:]

    def _pump(stream: Any, stream_name: str) -> None:
        try:
            while True:
                line = stream.readline()
                if line == "":
                    break
                events.put((stream_name, line.rstrip()))
        except Exception:
            pass
        finally:
            events.put((stream_name, None))
            try:
                stream.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=_pump, args=(proc.stdout, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=_pump, args=(proc.stderr, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    start_ts = time.monotonic()
    last_activity_ts = start_ts
    last_progress_ts = start_ts
    last_log_ts = start_ts
    closed_streams = set()
    current_out_time = ""

    while True:
        now = time.monotonic()
        if timeout_s > 0 and (now - start_ts) >= timeout_s:
            _terminate_subprocess_tree(proc)
            stderr_tail = _stderr_tail_text()
            return -1, stderr_tail, f"ffmpeg timed out after {int(timeout_s)}s"

        effective_idle_timeout = int(idle_timeout_s or 0)
        if effective_idle_timeout > 0 and (now - last_progress_ts) >= effective_idle_timeout:
            _terminate_subprocess_tree(proc)
            stderr_tail = _stderr_tail_text()
            return -1, stderr_tail, f"ffmpeg stalled with no progress for {effective_idle_timeout}s"

        try:
            stream_name, payload = events.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None and len(closed_streams) >= 2:
                break
            continue

        if payload is None:
            closed_streams.add(stream_name)
            if proc.poll() is not None and len(closed_streams) >= 2:
                break
            continue

        last_activity_ts = time.monotonic()
        if stream_name == "stderr":
            if payload:
                with stderr_tail_lock:
                    stderr_tail_parts.append(payload)
            continue

        if payload.startswith("out_time="):
            current_out_time = payload.split("=", 1)[1].strip()
            last_progress_ts = last_activity_ts
        elif payload.startswith("out_time_ms="):
            current_out_time = payload.split("=", 1)[1].strip()
            last_progress_ts = last_activity_ts
        elif payload.startswith("progress="):
            state = payload.split("=", 1)[1].strip()
            last_progress_ts = last_activity_ts
            if (last_activity_ts - last_log_ts) >= 20:
                suffix = f" | out_time={current_out_time}" if current_out_time else ""
                logger.info(f"âڈ±ï¸ڈ {progress_label}: state={state}{suffix}")
                last_log_ts = last_activity_ts
        elif payload:
            with stderr_tail_lock:
                stderr_tail_parts.append(payload)

    proc.wait(timeout=5)
    stderr_tail = _stderr_tail_text()
    return int(proc.returncode or 0), stderr_tail, ""


def _parse_volume_ratio(raw: str, default_ratio: float) -> float:
    try:
        s = (raw or "").strip()
        if not s:
            return default_ratio
        v = float(s)
        # Accept 60/70 style percentages
        if v > 1.5:
            v = v / 100.0
        return max(0.0, min(4.0, v))
    except Exception:
        return default_ratio


def _get_random_volume_level(min_percent: int = 90, max_percent: int = 100) -> float:
    """
    ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ظ…ط³طھظˆظ‰ طµظˆطھ ط¹ط´ظˆط§ط¦ظٹ ط¨ظٹظ† ط§ظ„ط­ط¯ ط§ظ„ط£ط¯ظ†ظ‰ ظˆط§ظ„ط£ظ‚طµظ‰
    
    ظٹط³طھط®ط¯ظ… ظ„طھظ†ظˆظٹط¹ ظ…ط³طھظˆظ‰ ط§ظ„طµظˆطھ ط¨ظٹظ† ط§ظ„ظپظٹط¯ظٹظˆظ‡ط§طھ ظ„طھط¬ظ†ط¨ ط§ظ„طھظƒط±ط§ط±
    """
    import random
    percent = random.randint(min_percent, max_percent)
    return percent / 100.0


def _is_low_resource_env() -> bool:
    """Detect if running in a low-resource environment (Render free tier, etc.).
    Uses multiple signals to avoid missing detection."""
    render_explicit = str(os.getenv("RENDER", "")).strip().lower() in {"1", "true", "yes", "on"}
    render_platform = bool(
        os.getenv("RENDER_SERVICE_ID")
        or os.getenv("RENDER_INSTANCE_ID")
        or os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RENDER_SERVICE_NAME")
        or os.getenv("RENDER_GIT_REPOSITORY")
    )
    low_res = str(os.getenv("LOW_RESOURCE_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
    low_cpu = str(os.getenv("FFMPEG_LOW_CPU", "")).strip().lower() in {"1", "true", "yes", "on"}
    if render_explicit or render_platform or low_res or low_cpu:
        return True

    try:
        from .resource_guard import get_resource_snapshot

        snap = get_resource_snapshot()
        total_mb = int(snap.ram_total_mb or 0)
        available_mb = int(snap.ram_available_mb or 0)
        cpu_count = int(os.cpu_count() or 0)
        if total_mb and total_mb <= 3584:
            return True
        if available_mb and available_mb <= 900:
            return True
        if cpu_count and cpu_count <= 2 and total_mb and total_mb <= 6144:
            return True
    except Exception:
        pass

    return False


def _safe_processing_fps(raw_fps: Optional[float]) -> float:
    try:
        fps = float(raw_fps or 0.0)
    except Exception:
        fps = 0.0
    if fps <= 0:
        fps = 30.0
    is_low = _is_low_resource_env()
    try:
        default_max = 30.0 if is_low else 60.0
        max_fps = float((os.getenv("SHORTS_MAX_FPS", str(default_max)) or str(default_max)).strip())
    except Exception:
        max_fps = 30.0 if is_low else 60.0
    max_fps = max(15.0, min(120.0, max_fps))
    return min(fps, max_fps)


"""ظ…ط¹ط§ظ„ط¬ ظپظٹط¯ظٹظˆظ‡ط§طھ ط§ظ„ظ…ظˆط¯ط§طھ"""


def _ffmpeg_memory_guard_args() -> list:
    """ط¥ط±ط¬ط§ط¹ ظˆط³ظٹط·ط§طھ ط­ظ…ط§ظٹط© ط§ظ„ط°ط§ظƒط±ط© ظ„ظ€ FFmpeg ظپظٹ ط¨ظٹط¦ط§طھ ط§ظ„ظ…ظˆط§ط±ط¯ ط§ظ„ظ…ط­ط¯ظˆط¯ط© (Render).
    
    ظٹط­ط¯ ظ…ظ† طھط®طµظٹطµ ط§ظ„ط°ط§ظƒط±ط© ظ„ظ…ظ†ط¹ OOM kill ط£ط«ظ†ط§ط، ط§ظ„طھط±ظ…ظٹط² ط§ظ„ط«ظ‚ظٹظ„.
    """
    if not _is_low_resource_env():
        return []
    
    try:
        max_alloc_mb = int((os.getenv("FFMPEG_MAX_ALLOC_MB", "256") or "256").strip())
    except Exception:
        max_alloc_mb = 256
    max_alloc_bytes = max(64, max_alloc_mb) * 1024 * 1024
    
    return ["-max_alloc", str(max_alloc_bytes)]


def _shorts_target_resolution() -> Tuple[int, int]:
    def _env_int(name: str, default: int) -> int:
        try:
            return int(float((os.getenv(name, str(default)) or str(default)).strip()))
        except Exception:
            return default

    is_low = _is_low_resource_env()
    if is_low:
        default_width = _env_int("SHORTS_LOW_RESOURCE_WIDTH", 720)
        default_height = _env_int("SHORTS_LOW_RESOURCE_HEIGHT", 1280)
    else:
        default_width = 1080
        default_height = 1920

    width = _env_int("SHORTS_TARGET_WIDTH", default_width)
    height = _env_int("SHORTS_TARGET_HEIGHT", default_height)

    width = max(144, width)
    height = max(256, height)
    if width % 2 != 0:
        width -= 1
    if height % 2 != 0:
        height -= 1

    if width <= 0 or height <= 0:
        return (720, 1280) if is_low else (1080, 1920)
    return width, height


def _get_shorts_encoder_settings() -> dict:
    """
    Get optimal encoder settings for shorts. Auto-detects GPU encoders.
    Optimized for YouTube quality with faststart and proper B-frames.
    """
    def _env_str(name: str, default: str) -> str:
        return (os.getenv(name, default) or default).strip()
    
    def _env_int(name: str, default: int) -> int:
        try:
            return int((os.getenv(name, str(default)) or str(default)).strip())
        except Exception:
            return default

    def _test_encoder(encoder: str, preset: Optional[str], extra_args: Optional[list] = None) -> bool:
        """Verify that an advertised encoder can actually complete a tiny encode."""
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [
                ffmpeg_bin(), "-y",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-an",
                "-c:v", encoder,
            ]
            if preset:
                cmd.extend(["-preset", preset])
            if extra_args:
                cmd.extend(extra_args)
            cmd.append(tmp_path)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Shorts encoder self-test failed for {encoder}: {e}")
            return False
        finally:
            try:
                if 'tmp_path' in locals() and tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
    
    is_low_res = _is_low_resource_env()
    # YouTube-optimized settings for shorts
    if is_low_res:
        # ًں”§ FORCE lightweight settings on Render â€” env vars CANNOT override
        settings = {
            "encoder": "libx264",
            "preset": "ultrafast",
            "crf": "28",
            "threads": 1,
            "extra_args": [
                "-profile:v", "high",
                "-level", "4.2",
                "-bf", "2",
                "-g", "30",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-use_editlist", "0",
                "-map_metadata", "-1",
            ],
            "min_bitrate": "3M",
            "audio_bitrate": "128k",
            "audio_sample_rate": 44100,
            "is_gpu": False
        }
    else:
        settings = {
            "encoder": "libx264",
            "preset": _env_str("SHORTS_X264_PRESET", "medium"),
            "crf": _env_str("SHORTS_X264_CRF", "20"),
            "threads": _env_int("FFMPEG_THREADS", 0),
            "extra_args": [
                "-profile:v", "high",
                "-level", _env_str("SHORTS_H264_LEVEL", "4.2"),
                "-bf", "2",
                "-g", "30",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-use_editlist", "0",
                "-map_metadata", "-1",
            ],
            "min_bitrate": _env_str("SHORTS_MIN_BITRATE", "5M"),
            "audio_bitrate": _env_str("AUDIO_BITRATE", "256k"),
            "audio_sample_rate": _env_int("AUDIO_SAMPLE_RATE", 44100),
            "is_gpu": False
        }
    
    hwaccel_mode = _env_str("FFMPEG_USE_HWACCEL", "auto")
    if hwaccel_mode.lower() in {"false", "0", "no", "off", "disabled"}:
        return settings
    if hwaccel_mode.lower() == "auto":
        render_mode = str(os.getenv("RENDER", "")).strip().lower() in {"1", "true", "yes", "on"}
        allow_on_render = str(os.getenv("FFMPEG_ALLOW_HWACCEL_ON_RENDER", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if render_mode and not allow_on_render:
            return settings
    
    try:
        cmd = [ffmpeg_bin(), "-hide_banner", "-encoders"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = result.stdout if result.returncode == 0 else ""
        
        # 1. Check for Android MediaCodec (Best for Termux)
        if "h264_mediacodec" in out:
            settings["encoder"] = "h264_mediacodec"
            settings["preset"] = None # MediaCodec doesn't use presets like x264
            settings["crf"] = None
            # MediaCodec relies on bitrate mainly
            settings["extra_args"] = [
                "-b:v", "6M", # Target 6Mbps
                "-maxrate", "10M",
                "-bufsize", "12M",
                "-profile:v", "high",
                "-level", "4.2",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            settings["is_gpu"] = True
            logger.info("âœ… Shorts Hardware Accel: Android MediaCodec (h264_mediacodec)")
            return settings

        # 2. NVIDIA NVENC
        if "h264_nvenc" in out:
            nvenc_preset = _env_str("FFMPEG_NVENC_PRESET", "p4")
            nvenc_args = [
                "-rc", "vbr",
                "-cq", _env_str("SHORTS_X264_CRF", "23"),
                "-b:v", settings["min_bitrate"],
                "-maxrate", "10M",
                "-bufsize", "14M",
                "-profile:v", "high",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            if _test_encoder("h264_nvenc", nvenc_preset, nvenc_args):
                settings["encoder"] = "h264_nvenc"
                settings["preset"] = nvenc_preset
                settings["crf"] = None
                settings["extra_args"] = nvenc_args
                settings["is_gpu"] = True
                logger.info("âœ… Shorts GPU: NVIDIA NVENC")
                return settings
            logger.warning("âڑ ï¸ڈ Shorts NVENC listed but self-test failed; falling back to CPU encoder")
        
        # 3. Intel QuickSync
        if "h264_qsv" in out:
            qsv_preset = "faster"
            qsv_args = [
                "-global_quality", _env_str("SHORTS_X264_CRF", "23"),
                "-look_ahead", "0",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            if _test_encoder("h264_qsv", qsv_preset, qsv_args):
                settings["encoder"] = "h264_qsv"
                settings["preset"] = qsv_preset
                settings["extra_args"] = qsv_args
                settings["crf"] = None
                settings["is_gpu"] = True
                logger.info("âœ… Shorts GPU: Intel QuickSync")
                return settings
            logger.warning("âڑ ï¸ڈ Shorts Intel QuickSync listed but self-test failed; falling back to CPU encoder")
            
        # 4. Apple VideoToolbox (Mac)
        if "h264_videotoolbox" in out:
            settings["encoder"] = "h264_videotoolbox"
            settings["preset"] = None
            settings["crf"] = None
            settings["extra_args"] = [
                 "-q:v", "60", # 0-100 quality scale
                 "-profile:v", "high",
                 "-movflags", "+faststart",
                 "-map_metadata", "-1",
            ]
            settings["is_gpu"] = True
            logger.info("âœ… Shorts GPU: Apple VideoToolbox")
            return settings

    except Exception as e:
        logger.warning(f"Error checking for hwaccel: {e}")
    
    return settings

class ModVideoProcessor:
    
    def __init__(self, temp_dir: Optional[str] = None):
        if temp_dir is None:
            from .config import load_config
            cfg = load_config()
            temp_dir = os.path.join(cfg.TEMP_DIR, "mods")
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    def process_mod_video(
        self,
        input_video: str,
        output_dir: str,
        video_id: str,
        trim_start: float = 1.0,
        trim_end: float = 1.0,
        add_cta: bool = True,
        cta_text: str = "ظ„طھط­ظ…ظٹظ„ ط§ظ„طھط·ط¨ظٹظ‚ ط§ظ„ظ…ط³طھط®ط¯ظ… ظپظٹ ط§ظ„ط´ط±ط­\nط­ظ…ظ„ طھط·ط¨ظٹظ‚ظ†ط§ ط§ظ„ط¢ظ† ظ…ظ† ط§ظ„ط±ط§ط¨ط· ظپظٹ ط§ظ„ظˆطµظپ",
        top_text: Optional[str] = None,
        convert_to_shorts: bool = True,
        custom_font: Optional[str] = None,
        top_text_size: int = 64,
        top_text_y: int = 150,
        is_custom: bool = False,
        enhance: bool = False,
        shorts_format: str = "crop",
        cta_position: Optional[str] = None,
        video_effects: Optional[Dict[str, Any]] = None,
        hflip: bool = False,
        progress_callback: Optional[callable] = None,
    ) -> Tuple[str, dict]:
        """
        ظ…ط¹ط§ظ„ط¬ط© ظپظٹط¯ظٹظˆ ظ…ظˆط¯
        
        Args:
            input_video: ظ…ط³ط§ط± ط§ظ„ظپظٹط¯ظٹظˆ ط§ظ„ظ…ط¯ط®ظ„
            output_dir: ظ…ط¬ظ„ط¯ ط§ظ„ط¥ط®ط±ط§ط¬
            video_id: ظ…ط¹ط±ظپ ط§ظ„ظپظٹط¯ظٹظˆ
            trim_start: ط¹ط¯ط¯ ط§ظ„ط«ظˆط§ظ†ظٹ ط§ظ„ظ…ط±ط§ط¯ ظ‚طµظ‡ط§ ظ…ظ† ط§ظ„ط¨ط¯ط§ظٹط©
            trim_end: ط¹ط¯ط¯ ط§ظ„ط«ظˆط§ظ†ظٹ ط§ظ„ظ…ط±ط§ط¯ ظ‚طµظ‡ط§ ظ…ظ† ط§ظ„ظ†ظ‡ط§ظٹط©
            add_cta: ط¥ط¶ط§ظپط© ظ†طµ ط§ظ„ط¯ط¹ظˆط© ظپظٹ ط§ظ„ظ†ظ‡ط§ظٹط©
            cta_text: ظ†طµ ط§ظ„ط¯ط¹ظˆط©
            top_text: ظ†طµ ظٹط¸ظ‡ط± ظپظٹ ط£ط¹ظ„ظ‰ ط§ظ„ظپظٹط¯ظٹظˆ ط·ظˆط§ظ„ ط§ظ„ظˆظ‚طھ
            convert_to_shorts: طھط­ظˆظٹظ„ ط§ظ„ظپظٹط¯ظٹظˆ ظ„طµظٹط؛ط© ط´ظˆط±طھط³
            custom_font: ظ…ط³ط§ط± ظ…ظ„ظپ ط®ط· ظ…ط®طµطµ
            top_text_size: ط­ط¬ظ… ط®ط· ط§ظ„ظ†طµ ط§ظ„ط¹ظ„ظˆظٹ
            top_text_y: ظ…ظˆظ‚ط¹ ط§ظ„ظ†طµ ط§ظ„ط¹ظ„ظˆظٹ (Y)
            is_custom: ظ‡ظ„ ظ‡ط°ط§ ظ‡ظˆ ط§ظ„ظ†ظ…ط· ط§ظ„ظ…ط®طµطµ (ظ„طھط؛ظٹظٹط± ط´ظƒظ„ ط§ظ„ظ†طµ)
            video_effects: ط¥ط¹ط¯ط§ط¯ط§طھ طھط£ط«ظٹط±ط§طھ ط§ظ„ط¨ط¯ط§ظٹط©/ط§ظ„ظ†ظ‡ط§ظٹط© ط§ظ„ط®ط§طµط© ط¨ط§ظ„ظ…طµط¯ط±
            hflip: ظ‡ظ„ ظٹطھظ… ظ‚ظ„ط¨ ط§ظ„ظپظٹط¯ظٹظˆ ط£ظپظ‚ظٹط§ظ‹ (Mirror)
        
        Returns:
            tuple: (ظ…ط³ط§ط± ط§ظ„ظپظٹط¯ظٹظˆ ط§ظ„ظ…ط¹ط§ظ„ط¬, ظ…ط¹ظ„ظˆظ…ط§طھ ط¥ط¶ط§ظپظٹط©)
        """
        os.makedirs(output_dir, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # ط§ظ„طھط£ظƒط¯ ظ…ظ† ظˆط¬ظˆط¯ ط§ظ„ط¨ط±ط§ظ…ط¬ ط§ظ„ظ„ط§ط²ظ…ط©
        if not ffmpeg_bin() or not ffprobe_bin():
             logger.warning("âڑ ï¸ڈ FFmpeg or FFprobe not found. Returning original video without processing.")
             return input_video, {"status": "skipped_no_ffmpeg", "original_path": input_video}
        
        
        # ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ظ…ط¹ظ„ظˆظ…ط§طھ ط§ظ„ظپظٹط¯ظٹظˆ
        duration = self._get_video_duration(input_video)
        width, height = self._get_video_dimensions(input_video)
        final_width, final_height = width, height
        
        logger.info(f"Processing mod video: {video_id} (Custom: {is_custom})")
        logger.info(f"Original duration: {duration}s, dimensions: {width}x{height}")
        
        # Start timing for each step
        step_timings = {
            "total": time.time(),
            "trim": None,
            "flip": None,
            "convert": None,
            "overlay": None,
            "effects": None,
            "enhance": None,
            "encode": None
        }
        
        # ط­ط³ط§ط¨ ط§ظ„ظ…ط¯ط© ط§ظ„ط¬ط¯ظٹط¯ط©
        new_duration = duration - trim_start - trim_end
        
        if new_duration <= 0:
            raise ValueError(f"Video too short after trimming: {new_duration}s")
        
        # ط§ظ„ظ…ط³ط§ط±ط§طھ ط§ظ„ظ…ط¤ظ‚طھط©
        trimmed_path = self.temp_dir / f"{video_id}_trimmed.mp4"
        flipped_path = self.temp_dir / f"{video_id}_flipped.mp4"
        resized_path = self.temp_dir / f"{video_id}_resized.mp4"
        overlay_path = self.temp_dir / f"{video_id}_overlay.mp4"
        final_path = Path(output_dir) / f"{video_id}_mod.mp4"
        try:
            apply_processing_effects = str(os.getenv("SHORTS_EFFECTS_APPLY_DURING_PROCESSING", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            apply_processing_effects = False
        
        try:
            current_path = input_video
            target_fps = self._get_video_fps(current_path)
            explicit_effects = video_effects if isinstance(video_effects, dict) else None
            intro_cfg = self._normalize_explicit_video_effect((explicit_effects or {}).get("intro") or {})
            outro_cfg = self._normalize_explicit_video_effect((explicit_effects or {}).get("outro") or {})
            has_post_convert_processing = bool(
                top_text
                or enhance
                or apply_processing_effects
                or intro_cfg.get("enabled")
                or outro_cfg.get("enabled")
            )
            skip_final_encode = False
            
            # ط§ظ„ط®ط·ظˆط© 1: ظ‚طµ ط§ظ„ط¨ط¯ط§ظٹط© ظˆط§ظ„ظ†ظ‡ط§ظٹط© (ظپظ‚ط· ط¥ط°ط§ ظƒط§ظ†طھ ط§ظ„ظ‚ظٹظ… ط£ظƒط¨ط± ظ…ظ† 0)
            # ًں”§ ط§ط³طھط®ط¯ط§ظ… stream copy ظ„ظ„ط­ظپط§ط¸ ط¹ظ„ظ‰ ط§ظ„ط¬ظˆط¯ط© ط§ظ„ط£طµظ„ظٹط©
            # ط³ظٹطھظ… ط§ظ„طھط±ظ…ظٹط² ظ„ط§ط­ظ‚ط§ظ‹ ظپظٹ convert_to_shorts ط£ظˆ ط§ظ„طھط±ظ…ظٹط² ط§ظ„ظ†ظ‡ط§ط¦ظٹ
            if trim_start > 0 or trim_end > 0:
                step_start = time.time()
                logger.info("âœ‚ï¸ڈ Step 1/5: Trimming video...")
                if progress_callback:
                    try: progress_callback("1/5 âœ‚ï¸ڈ ظ‚طµ ط§ظ„ظپظٹط¯ظٹظˆ...")
                    except Exception: pass
                self._trim_video(current_path, trimmed_path, trim_start, trim_end, force_encode=False)
                current_path = str(trimmed_path)
                step_timings["trim"] = time.time() - step_start
                logger.info(f"âœ… Step 1/5 completed in {step_timings['trim']:.2f}s")
            
            # ط§ظ„طھط­ظ‚ظ‚ ط§ظ„ظ…ط¨ظƒط±: ظ‡ظ„ ط³ظٹطھظ… طھط­ظˆظٹظ„ ط§ظ„ظپظٹط¯ظٹظˆ ظپط¹ظ„ط§ظ‹ ط¥ظ„ظ‰ طµظٹط؛ط© Shorts ط¨طھط±ظ…ظٹط² ظƒط§ظ…ظ„طں
            will_convert_to_shorts = False
            if convert_to_shorts:
                fmt = (shorts_format or "crop").strip().lower()
                try:
                    skip_if_vertical = str(os.getenv("AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    skip_if_vertical = True

                should_skip_conversion = False
                if skip_if_vertical and fmt == "crop" and width > 0 and height > 0:
                    try:
                        should_skip_conversion = abs((width / height) - (9.0 / 16.0)) <= 0.02
                    except Exception:
                        should_skip_conversion = False

                if not should_skip_conversion:
                    will_convert_to_shorts = True

            # ط§ظ„ط®ط·ظˆط© 1.5: ظ‚ظ„ط¨ ط§ظ„ظپظٹط¯ظٹظˆ ط£ظپظ‚ظٹط§ظ‹ (ط¥ط°ط§ طھظ… ط·ظ„ط¨ظ‡)
            # ًں”§ طھط­ط³ظٹظ†: ط¥ط°ط§ ظƒظ†ط§ ط³ظ†ظ‚ظˆظ… ط¨طھط±ط¬ظ…ظٹط² ط§ظ„ظپظٹط¯ظٹظˆ ظ„طھط­ظˆظٹظ„ظ‡ ط¥ظ„ظ‰ ShortsطŒ ط³ظ†ظ…ط±ط± ط·ظ„ط¨ ط§ظ„ظ‚ظ„ط¨ ظ„ظ‡ ظ‡ظ†ط§ظƒ ظ„طھط¬ظ†ط¨ طھط±ظ…ظٹط² ظ…ط²ط¯ظˆط¬.
            if hflip and not will_convert_to_shorts:
                logger.info("â†”ï¸ڈ Flipping video horizontally (separate encode)...")
                self._flip_video(current_path, flipped_path)
                current_path = str(flipped_path)
            
            # ط§ظ„ط®ط·ظˆط© 2: طھط­ظˆظٹظ„ ظ„طµظٹط؛ط© ط´ظˆط±طھط³ (9:16)
            if convert_to_shorts:
                if not will_convert_to_shorts:
                    logger.info(f"ًں“گ Step 2/5: Skipping shorts conversion (already 9:16: {width}x{height})")
                    if progress_callback:
                        try: progress_callback("2/5 ًں“گ طھط®ط·ظٹ ط§ظ„طھط­ظˆظٹظ„ (ط§ظ„ظپظٹط¯ظٹظˆ ط¹ظ…ظˆط¯ظٹ 9:16 ظ…ط³ط¨ظ‚ط§ظ‹)...")
                        except Exception: pass
                    step_timings["convert"] = 0.0
                else:
                    step_start = time.time()
                    if hflip:
                        logger.info("ًں“گ Step 2/5: Converting to shorts format (incl. horizontal flip)...")
                    else:
                        logger.info("ًں“گ Step 2/5: Converting to shorts format...")
                    if progress_callback:
                        try: progress_callback("2/5 ًں“گ طھط­ظˆظٹظ„ ظ„طµظٹط؛ط© ط´ظˆط±طھط³...")
                        except Exception: pass
                    self._convert_to_shorts(current_path, resized_path, width, height, shorts_format=shorts_format, hflip=hflip)
                    current_path = str(resized_path)
                    final_width, final_height = _shorts_target_resolution()
                    step_timings["convert"] = time.time() - step_start
                    logger.info(f"âœ… Step 2/5 completed in {step_timings['convert']:.2f}s")
                    if not has_post_convert_processing:
                        skip_final_encode = True
            
            # ط§ظ„ط®ط·ظˆط© 3: ط¥ط¶ط§ظپط© ظ†طµ ط¹ظ„ظˆظٹ (ط§ط®طھظٹط§ط±ظٹ)
            if top_text:
                step_start = time.time()
                logger.info("ًں“‌ Step 3/5: Adding text overlay...")
                if progress_callback:
                    try: progress_callback("3/5 ًں“‌ ط¥ط¶ط§ظپط© ظ†طµ...")
                    except Exception: pass
                self._add_top_overlay_text(current_path, overlay_path, top_text, custom_font, top_text_size, top_text_y, is_custom)
                current_path = str(overlay_path)
                step_timings["overlay"] = time.time() - step_start
                logger.info(f"âœ… Step 3/5 completed in {step_timings['overlay']:.2f}s")

            # ط§ظ„ط®ط·ظˆط© 4: ط¥ط¶ط§ظپط© طھط£ط«ظٹط±ط§طھ ط§ظ„ط¨ط¯ط§ظٹط©/ط§ظ„ظ†ظ‡ط§ظٹط©
            effects_start = time.time()
            if convert_to_shorts:
                explicit_effects = video_effects if isinstance(video_effects, dict) else None
                if explicit_effects:
                    intro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("intro") or {})
                    outro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("outro") or {})
                    if intro_cfg.get("enabled") or outro_cfg.get("enabled"):
                        try:
                            effects_path = self.temp_dir / f"{video_id}_effects.mp4"
                            self._apply_configured_intro_outro_effects(current_path, effects_path, explicit_effects)
                            current_path = str(effects_path)
                        except Exception as effects_err:
                            logger.warning(f"Configured effects failed and will be skipped: {effects_err}")
                elif apply_processing_effects:
                    try:
                        effects_path = self.temp_dir / f"{video_id}_effects.mp4"
                        self.add_simple_intro_outro_effects(current_path, effects_path, seed=video_id, apply_outro=False)
                        current_path = str(effects_path)

                        outro_path = self.temp_dir / f"{video_id}_outro.mp4"
                        self._apply_outro_blur_black(current_path, outro_path, 1.0)
                        current_path = str(outro_path)
                    except Exception as effects_err:
                        logger.warning(f"Default effects failed and will be skipped: {effects_err}")
            step_timings["effects"] = time.time() - effects_start
            if step_timings["effects"] > 0.1:
                logger.info(f"âœ… Step 4/5 (effects) completed in {step_timings['effects']:.2f}s")
            
            # ط§ظ„ط®ط·ظˆط© 5: طھط­ط³ظٹظ† ط³ظٹظ†ظ…ط§ط¦ظٹ (ط§ط®طھظٹط§ط±ظٹ)
            if enhance:
                step_start = time.time()
                enhance_path = self.temp_dir / f"{video_id}_enhanced.mp4"
                ok = self._apply_cinematic_teal_boost(current_path, enhance_path)
                if ok:
                    current_path = str(enhance_path)
                step_timings["enhance"] = time.time() - step_start
                logger.info(f"âœ… Step 5/5 (enhance) completed in {step_timings['enhance']:.2f}s")
            
            # ط§ظ„ط®ط·ظˆط© 6: ط§ظ„طھط±ظ…ظٹط² ط§ظ„ظ†ظ‡ط§ط¦ظٹ
            encode_start = time.time()
            if convert_to_shorts and skip_final_encode:
                logger.info("âڈ­ï¸ڈ Step 6/6: Skipping final encoding and reusing the shorts-converted file directly...")
                if progress_callback:
                    try: progress_callback("6/6 âڈ­ï¸ڈ طھط®ط·ظٹ ط§ظ„طھط±ظ…ظٹط² ط§ظ„ظ†ظ‡ط§ط¦ظٹ...")
                    except Exception: pass
                try:
                    if os.path.exists(str(final_path)):
                        os.remove(str(final_path))
                except Exception:
                    pass
                try:
                    os.replace(current_path, str(final_path))
                except Exception:
                    import shutil
                    shutil.copy2(current_path, str(final_path))
                current_path = str(final_path)
                step_timings["encode"] = time.time() - encode_start
                logger.info(f"âœ… Step 6/6 skipped in {step_timings['encode']:.2f}s")
            else:
                logger.info("ًںژ¬ Step 6/6: Final encoding...")
                if progress_callback:
                    try: progress_callback("6/6 ًںژ¬ ط§ظ„طھط±ظ…ظٹط² ط§ظ„ظ†ظ‡ط§ط¦ظٹ...")
                    except Exception: pass
                if convert_to_shorts:
                    ok_final = self._encode_final_shorts(current_path, str(final_path), target_fps)
                    if not ok_final:
                        raise RuntimeError(f"Failed to encode final shorts: {final_path}")
                else:
                    self._optimize_for_youtube(current_path, str(final_path))
                step_timings["encode"] = time.time() - encode_start
                logger.info(f"âœ… Step 6/6 (final encode) completed in {step_timings['encode']:.2f}s")
            
            # ظ…ط¹ظ„ظˆظ…ط§طھ ط§ظ„ظپظٹط¯ظٹظˆ ط§ظ„ظ…ط¹ط§ظ„ط¬
            info = {
                "original_duration": duration,
                "new_duration": new_duration,
                "final_size": f"{final_width}x{final_height}",
                "final_path": str(final_path)
            }
            return str(final_path), info


            
        finally:
            # طھظ†ط¸ظٹظپ ط§ظ„ظ…ظ„ظپط§طھ ط§ظ„ظ…ط¤ظ‚طھط©
            for temp_file in [trimmed_path, flipped_path, resized_path, overlay_path, self.temp_dir / f"{video_id}_effects.mp4", self.temp_dir / f"{video_id}_outro.mp4", self.temp_dir / f"{video_id}_enhanced.mp4"]:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete temp file {temp_file}: {e}")

    def _normalize_explicit_video_effect(self, raw_effect: Any) -> Dict[str, Any]:
        effect_type = "none"
        duration = 0.0
        enabled = False

        if isinstance(raw_effect, dict):
            effect_type = str(raw_effect.get("type") or raw_effect.get("effect") or "none").strip().lower()
            enabled = bool(raw_effect.get("enabled", effect_type not in {"none", "off", "disabled", "no"}))
            duration = raw_effect.get("duration", 1.0)
        elif isinstance(raw_effect, str):
            effect_type = raw_effect.strip().lower()
            enabled = effect_type not in {"none", "off", "disabled", "no"}
            duration = 1.0

        aliases = {
            "none": "none",
            "off": "none",
            "disabled": "none",
            "no": "none",
            "blur": "blur",
            "normal_blur": "blur",
            "black_blur": "black_blur",
            "blur_black": "black_blur",
            "black": "black_blur",
        }
        effect_type = aliases.get(effect_type, "none")
        try:
            duration = max(0.0, float(duration or 0.0))
        except Exception:
            duration = 0.0

        if effect_type == "none" or not enabled:
            return {"enabled": False, "type": "none", "duration": 0.0}
        return {"enabled": True, "type": effect_type, "duration": min(max(duration, 0.3), 3.0)}

    def _apply_configured_intro_outro_effects(self, input_path: str, output_path: str, video_effects: Optional[Dict[str, Any]]) -> None:
        import shutil

        explicit_effects = video_effects if isinstance(video_effects, dict) else {}
        intro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("intro") or {})
        outro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("outro") or {})

        if not intro_cfg.get("enabled") and not outro_cfg.get("enabled"):
            shutil.copy2(input_path, output_path)
            return

        duration_s = self._get_video_duration(input_path)
        if not duration_s or duration_s <= 0:
            shutil.copy2(input_path, output_path)
            return

        intro_d = min(float(intro_cfg.get("duration", 0.0) or 0.0), max(0.15, duration_s * 0.45)) if intro_cfg.get("enabled") else 0.0
        outro_d = min(float(outro_cfg.get("duration", 0.0) or 0.0), max(0.15, duration_s * 0.45)) if outro_cfg.get("enabled") else 0.0
        total_fx = intro_d + outro_d
        max_total = max(0.3, duration_s * 0.9)
        if total_fx > max_total and total_fx > 0:
            scale = max_total / total_fx
            intro_d *= scale
            outro_d *= scale

        outro_start = max(0.0, duration_s - outro_d)
        vf_parts = ["setpts=PTS-STARTPTS"]
        is_low = _is_low_resource_env()

        # ًں”§ On low-resource envs: use lighter blur radius, skip eq filter
        blur_radius_heavy = 6 if is_low else 12
        blur_radius_light = 4 if is_low else 8

        if intro_cfg.get("enabled") and intro_d > 0:
            vf_parts.append(f"fade=t=in:st=0:d={intro_d:.3f}")
            radius = blur_radius_heavy if intro_cfg.get('type') == 'black_blur' else blur_radius_light
            vf_parts.append(f"boxblur=luma_radius={radius}:enable='between(t,0,{intro_d:.3f})'")
            if intro_cfg.get("type") == "black_blur" and not is_low:
                # eq filter is expensive â€” skip on low-resource environments
                vf_parts.append(f"eq=brightness=-0.18:saturation=0.92:enable='between(t,0,{intro_d:.3f})'")

        if outro_cfg.get("enabled") and outro_d > 0:
            vf_parts.append(f"fade=t=out:st={outro_start:.3f}:d={outro_d:.3f}")
            radius = blur_radius_heavy if outro_cfg.get('type') == 'black_blur' else blur_radius_light
            vf_parts.append(f"boxblur=luma_radius={radius}:enable='between(t,{outro_start:.3f},{duration_s:.3f})'")
            if outro_cfg.get("type") == "black_blur" and not is_low:
                vf_parts.append(f"eq=brightness=-0.18:saturation=0.92:enable='between(t,{outro_start:.3f},{duration_s:.3f})'")

        vf_parts.append("format=yuv420p")
        vf = ",".join(vf_parts)
        ff_threads, base_preset, base_crf = self._shorts_x264_settings()

        if is_low:
            # ًں”§ FORCE lightweight encoding on Render â€” env vars cannot override
            preset = "ultrafast"
            crf = 30
            ff_threads = 1
            logger.info(f"ًں”§ [Effects] Low-resource mode: preset={preset}, crf={crf}, threads={ff_threads}")
        else:
            default_effects_preset = base_preset
            preset = str(os.getenv("SHORTS_EFFECTS_PRESET", default_effects_preset) or default_effects_preset).strip() or default_effects_preset
            default_effects_crf = base_crf
            crf = int(os.getenv("SHORTS_EFFECTS_CRF", str(default_effects_crf)) or str(default_effects_crf))

        level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if is_low else "5.1") or "5.1").strip() or "5.1"
        has_audio = self._has_audio(input_path)
        fps = self._get_video_fps(input_path)
        fps = _safe_processing_fps(fps)
        gop = max(1, int(round(fps)))
        audio_bitrate = "128k" if is_low else "384k"

        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-vsync", "cfr",
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-keyint_min", str(max(1, gop // 2)),
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,0)",
            "-threads", str(ff_threads),
        ]
        if has_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output_path)]
        timeout_s = self._resolve_ffmpeg_timeout(
            input_path,
            "SHORTS_EFFECTS_TIMEOUT_SECONDS",
            300,
            1800,
            10.0,
            12.0,
            extra_seconds=120,
        )
        idle_timeout_s = min(90, max(30, timeout_s // 4))
        logger.info(f"ًںژ¬ [Effects] Running FFmpeg effects (timeout={timeout_s}s, idle={idle_timeout_s}s): {' '.join(cmd[-6:])}")
        rc, stderr_text = _run_ffmpeg_with_idle_timeout(
            cmd, timeout_s=timeout_s, idle_timeout_s=idle_timeout_s, label="Effects"
        )
        if rc != 0:
            raise RuntimeError(stderr_text[-2500:] or f"Effects FFmpeg exited with code {rc}")
    
    def _optimize_for_youtube(self, input_path: str, output_path: str) -> bool:
        # Ensure temp and output dirs exist
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        if os.path.dirname(output_path):
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        """
        Final optimization pass for YouTube:
        - Ensures Move Atom is at start (Fast Start)
        - Removes all metadata and Edit Lists
        - Ensures clean container without re-encoding
        """
        try:
            logger.info("ًںڑ€ Running final YouTube optimization pass...")
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-c", "copy",  # Stream copy (no re-encoding, fast)
                "-map_metadata", "-1",  # Remove all metadata
                "-movflags", "+faststart",  # Move atom to front
                "-use_editlist", "0",  # Disable edit lists
                "-f", "mp4",
                output_path
            ]
            
            _opt_timeout = self._resolve_ffmpeg_timeout(
                input_path, "FFMPEG_YOUTUBE_OPT_TIMEOUT_SECONDS", 120, 300, 3.0, 3.0, extra_seconds=30,
            )
            rc, _stderr = _run_ffmpeg_with_idle_timeout(
                cmd, timeout_s=_opt_timeout, idle_timeout_s=60, label="YouTubeOpt"
            )
            if rc == 0 and os.path.exists(output_path):
                logger.debug("âœ… YouTube optimization successful")
                return True
            else:
                logger.warning(f"Optimization failed: {result.stderr.decode()[:200]}")
                return False

        except Exception as e:
            logger.error(f"Error in YouTube optimization: {e}")
            return False

    def _encode_final_shorts(self, input_path: str, output_path: str, target_fps: Optional[float] = None) -> bool:
        try:
            def _run_encode(enc_settings: dict) -> Tuple[bool, str]:
                fps = float(target_fps) if target_fps and target_fps > 0 else self._get_video_fps(input_path)
                fps = _safe_processing_fps(fps)
                gop = int(round(fps))
                if gop < 1:
                    gop = 30
                has_audio = self._has_audio(input_path)
                is_low = _is_low_resource_env()

                out_s = str(output_path)
                if out_s.lower().endswith(".mp4"):
                    tmp_out = out_s[:-4] + ".tmp.mp4"
                else:
                    tmp_out = out_s + ".tmp.mp4"
                try:
                    Path(out_s).parent.mkdir(parents=True, exist_ok=True)
                    Path(tmp_out).parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass

                cmd = [
                    ffmpeg_bin(),
                    *_ffmpeg_memory_guard_args(),
                    "-hide_banner",
                    "-loglevel", "error",
                    "-nostats",
                    "-y",
                    "-fflags", "+genpts+igndts",
                    "-i", input_path,
                    "-map", "0:v",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                    "-c:v", str(enc_settings.get("encoder") or "libx264"),
                ]

                preset = enc_settings.get("preset")
                if preset:
                    cmd += ["-preset", str(preset)]

                crf = enc_settings.get("crf")
                if crf is not None and str(enc_settings.get("encoder") or "").lower() == "libx264":
                    cmd += ["-crf", str(crf)]

                extra_args = list(enc_settings.get("extra_args") or [])
                # Prevent conflicts/duplicates with options we enforce below
                skip_flags = {
                    "-movflags",
                    "-map_metadata",
                    "-use_editlist",
                    "-vsync",
                    "-r",
                    "-g",
                }
                filtered_extra: list[str] = []
                i = 0
                while i < len(extra_args):
                    tok = str(extra_args[i])
                    if tok in skip_flags:
                        i += 2
                        continue
                    filtered_extra.append(tok)
                    i += 1
                cmd += filtered_extra

                cmd += [
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-keyint_min", str(max(1, gop // 2)),
                    "-sc_threshold", "0",
                    "-force_key_frames", "expr:gte(t,0)",
                    "-pix_fmt", "yuv420p",
                ]

                if enc_settings.get("threads"):
                    cmd += ["-threads", str(int(enc_settings["threads"]))]

                if has_audio:
                    cmd += [
                        "-map", "0:a?",
                        "-c:a", "aac",
                        "-b:a", str(enc_settings.get("audio_bitrate") or "384k"),
                        "-ar", str(enc_settings.get("audio_sample_rate") or 48000),
                    ]
                else:
                    cmd += ["-an"]

                cmd += [
                    "-movflags", "+faststart",
                    "-map_metadata", "-1",
                    "-use_editlist", "0",
                    "-f", "mp4",
                    str(tmp_out),
                ]

                _timeout = self._resolve_ffmpeg_timeout(
                    input_path,
                    "FFMPEG_FINAL_ENCODE_TIMEOUT_SECONDS",
                    300,
                    600,
                    10.0,
                    8.0,
                    extra_seconds=90,
                )
                _idle_timeout = min(120, max(45, _timeout // 4))
                logger.info(f"ًںژ¬ [FinalEncode] timeout={_timeout}s idle={_idle_timeout}s encoder={enc_settings.get('encoder')}")
                rc, stderr = _run_ffmpeg_with_idle_timeout(
                    cmd, timeout_s=_timeout, idle_timeout_s=_idle_timeout, label="FinalEncode"
                )
                if rc != 0:
                    return False, stderr
                try:
                    self._validate_video_file(str(tmp_out))
                except Exception as e:
                    return False, str(e)

                try:
                    if os.path.exists(str(output_path)):
                        os.remove(str(output_path))
                except Exception:
                    pass
                try:
                    os.replace(str(tmp_out), str(output_path))
                except Exception:
                    import shutil
                    shutil.copy2(str(tmp_out), str(output_path))
                    try:
                        os.remove(str(tmp_out))
                    except Exception:
                        pass
                return os.path.exists(str(output_path)) and os.path.getsize(str(output_path)) > 0, stderr

            settings = _get_shorts_encoder_settings()
            ok, err = _run_encode(settings)
            if ok:
                return True

            if err:
                logger.error(f"Final shorts encode failed (encoder={settings.get('encoder')}), retrying fallback. ffmpeg stderr: {err[-1500:]}")

            # Fallback to libx264 if GPU encoder fails
            if str(settings.get("encoder") or "").lower() != "libx264":
                ff_threads, preset, crf = self._shorts_x264_settings()
                lvl = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
                fallback = {
                    "encoder": "libx264",
                    "preset": preset,
                    "crf": str(crf),
                    "threads": ff_threads,
                    "extra_args": ["-profile:v", "high", "-level", lvl],
                    "audio_bitrate": "128k" if _is_low_resource_env() else (settings.get("audio_bitrate") or "384k"),
                    "audio_sample_rate": settings.get("audio_sample_rate") or 48000,
                }
                ok2, err2 = _run_encode(fallback)
                if ok2:
                    return True
                if err2:
                    logger.error(f"Final shorts encode fallback failed. ffmpeg stderr: {err2[-1500:]}")
            return False
        except Exception as e:
            logger.error(f"Error encoding final shorts: {e}")
            return False

    def _apply_cinematic_teal_boost(self, input_path: str, output_path: Path) -> bool:
        """طھط­ط³ظٹظ† ط¨ط³ظٹط· ظ‚ط§ط¨ظ„ ظ„ظ„طھط¹ط¯ظٹظ„: طھط´ط¨ط¹ + طھط¨ط§ظٹظ† + طھط¹ط±ظٹط¶ + ظ‡ط§ظٹظ„ط§ظٹطھ ظ…ط¹ ظ…ط²ط¬ ط¨ظ†ط³ط¨ط© ظƒط«ط§ظپط©"""
        try:
            def _env_float(name: str, default: float) -> float:
                try:
                    raw = os.getenv(name, str(default)) or str(default)
                    import re
                    cleaned = re.sub(r"[^0-9\.\-]", "", raw).strip()
                    if cleaned == "":
                        return default
                    return float(cleaned)
                except Exception:
                    return default
            sat_pct = _env_float("ENHANCE_SAT_PCT", 25.0)
            contrast_pct = _env_float("ENHANCE_CONTRAST_PCT", 15.0)
            bright_pct = _env_float("ENHANCE_BRIGHTNESS_PCT", 10.0)
            highlights_pct = _env_float("ENHANCE_HIGHLIGHTS_PCT", 20.0)
            intensity_pct = _env_float("ENHANCE_INTENSITY_PCT", 70.0)
            sat = max(0.0, 1.0 + sat_pct / 100.0)
            contrast = max(0.0, 1.0 + contrast_pct / 100.0)
            brightness = max(-1.0, min(1.0, bright_pct / 100.0))
            hl = max(0.0, min(1.0, highlights_pct / 100.0))
            opacity = max(0.0, min(1.0, intensity_pct / 100.0))
            logger.info(
                f"Enhance params -> sat_pct={sat_pct}%, contrast_pct={contrast_pct}%, "
                f"bright_pct={bright_pct}%, highlights_pct={highlights_pct}%, intensity_pct={intensity_pct}% "
                f"(mapped: sat={sat}, contrast={contrast}, brightness={brightness}, hl={hl}, opacity={opacity})"
            )
            def _run_filter(filter_complex: str, map_out: bool = True) -> Tuple[bool, str]:
                ff_threads, preset, crf = self._shorts_x264_settings()
                has_audio = self._has_audio(input_path)
                # ًں”§ ط§ط³طھط®ط¯ط§ظ… ظ†ظپط³ ط¥ط¹ط¯ط§ط¯ط§طھ ط§ظ„ظˆط³ظٹط· ط§ظ„ظ…ط­ط³ظ‘ظ†ط©
                try:
                    lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    lossless_intermediate = False
                fps = self._get_video_fps(input_path)
                if not fps or fps <= 0:
                    fps = 30.0
                gop = int(round(fps))
                if gop < 1:
                    gop = 30
                if lossless_intermediate:
                    preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
                    crf = 0
                    v_profile = "high444"
                    v_pix_fmt = "yuv444p"
                else:
                    # ًں”§ Use _shorts_x264_settings() directly â€” already respects RENDER
                    _, base_preset, base_crf = self._shorts_x264_settings()
                    if _is_low_resource_env():
                        preset = base_preset  # Already forced to ultrafast
                        crf = base_crf        # Already forced to 28
                    else:
                        preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
                        crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
                    v_profile = None
                    v_pix_fmt = "yuv420p"
                # ًں†• ط§ط³طھط®ط¯ط§ظ… ظ…ط³طھظˆظ‰ طµظˆطھ ط¹ط´ظˆط§ط¦ظٹ (90-100%) ظ„طھظ†ظˆظٹط¹ ط§ظ„ظ…ط­طھظˆظ‰
                shorts_vol = _get_random_volume_level(90, 100)

                cmd = [
                    ffmpeg_bin(), "-y",
                    "-i", input_path,
                    "-filter_complex", filter_complex,
                    "-map", "[outv]" if map_out else "0:v",
                ]
                if has_audio:
                    cmd += [
                        "-map", "0:a?",
                        "-c:a", "aac",
                        "-b:a", "128k" if _is_low_resource_env() else "384k",
                        "-af", f"volume={shorts_vol}",
                    ]
                cmd += [
                    "-c:v", "libx264",
                    "-preset", preset,
                    "-crf", str(crf),
                    "-pix_fmt", v_pix_fmt,
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-threads", str(ff_threads),
                    str(output_path)
                ]
                if v_profile:
                    # x264 lossless is not supported with profile=high/yuv420p
                    # use high444 for intermediates
                    cmd[cmd.index("-c:v") + 2:cmd.index("-c:v") + 2] = ["-profile:v", v_profile]
                _enh_timeout = self._resolve_ffmpeg_timeout(
                    input_path, "FFMPEG_ENHANCE_TIMEOUT_SECONDS", 300, 600, 8.0, 8.0, extra_seconds=90,
                )
                _enh_idle = min(120, max(45, _enh_timeout // 4))
                rc, stderr_text = _run_ffmpeg_with_idle_timeout(
                    cmd, timeout_s=_enh_timeout, idle_timeout_s=_enh_idle, label="Enhance"
                )
                ok = (rc == 0) and os.path.exists(output_path) and os.path.getsize(output_path) > 0
                return ok, stderr_text

            # Try 1: eq + colorbalance (highlights) + blend (intensity)
            fc1 = (
                f"[0:v]split[orig][proc];"
                f"[proc]eq=contrast={contrast}:brightness={brightness}:saturation={sat},"
                f"colorbalance=rh={hl}:gh={hl}:bh={hl}[proc2];"
                f"[orig][proc2]blend=all_mode=normal:all_opacity={opacity}[outv]"
            )
            ok, err = _run_filter(fc1)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (with colorbalance) failed, retrying fallback. ffmpeg stderr: {err[:800]}")

            # Try 2: eq + blend only
            fc2 = (
                f"[0:v]split[orig][proc];"
                f"[proc]eq=contrast={contrast}:brightness={brightness}:saturation={sat}[proc2];"
                f"[orig][proc2]blend=all_mode=normal:all_opacity={opacity}[outv]"
            )
            ok, err = _run_filter(fc2)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (without colorbalance) failed, retrying minimal. ffmpeg stderr: {err[:800]}")

            # Try 3: eq only (no blend)
            fc3 = f"[0:v]eq=contrast={contrast}:brightness={brightness}:saturation={sat}[outv]"
            ok, err = _run_filter(fc3)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (eq only) failed. ffmpeg stderr: {err[:800]}")
            return False
        except Exception as e:
            logger.error(f"Enhance failed: {e}")
            return False
    
    def _get_best_font(self, text: str, custom_font: Optional[str] = None) -> str:
        """ط§ط®طھظٹط§ط± ط£ظپط¶ظ„ ط®ط· ط¨ظ†ط§ط،ظ‹ ط¹ظ„ظ‰ ط§ظ„ظ†طµ ظˆط§ظ„ظ„ط؛ط© ظˆط§ظ„ط¥ط¹ط¯ط§ط¯ط§طھ ط§ظ„ط¹ط§ظ„ظ…ظٹط©"""

        def _is_arabicish(s: str) -> bool:
            return bool(
                re.search(
                    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]",
                    s or "",
                )
            )

        def _is_thai(s: str) -> bool:
            return bool(re.search(r"[\u0E00-\u0E7F]", s or ""))

        def _iter_required_codepoints(s: str) -> set[int]:
            cps: set[int] = set()
            for ch in (s or ""):
                o = ord(ch)
                if ch.isspace():
                    continue
                if 0x0600 <= o <= 0x06FF:
                    cps.add(o)
                    continue
                if 0x0750 <= o <= 0x077F:
                    cps.add(o)
                    continue
                if 0x08A0 <= o <= 0x08FF:
                    cps.add(o)
                    continue
                if 0xFB50 <= o <= 0xFDFF:
                    cps.add(o)
                    continue
                if 0xFE70 <= o <= 0xFEFF:
                    cps.add(o)
                    continue
                if 0x0E00 <= o <= 0x0E7F:
                    cps.add(o)
                    continue
            return cps

        def _font_supports_required_chars(font_path: str, required: set[int]) -> bool:
            if not required:
                return True
            if not font_path or not os.path.exists(font_path):
                return False
            if not HAS_FONTTOOLS:
                return True
            try:
                tt = TTFont(font_path, recalcBBoxes=False, recalcTimestamp=False)
                cmap = tt.getBestCmap() or {}
                tt.close()
                if not cmap:
                    return False
                for cp in required:
                    if cp not in cmap:
                        return False
                return True
            except Exception:
                return False

        def _iter_font_files(font_dir: str) -> list[str]:
            out: list[str] = []
            if not font_dir or not os.path.isdir(font_dir):
                return out
            try:
                for root, _, files in os.walk(font_dir):
                    for name in files:
                        if name.lower().endswith((".ttf", ".otf", ".ttc")):
                            out.append(os.path.abspath(os.path.join(root, name)))
            except Exception:
                return out
            return out

        required_cps = _iter_required_codepoints(text)
        # 1. ط§ظ„ط£ظˆظ„ظˆظٹط© ط§ظ„ظ‚طµظˆظ‰ ظ„ظ„ط®ط· ط§ظ„ظ…ط®طµطµ (ط¥ط°ط§ طھظ… ط±ظپط¹ظ‡ ظ„ظ„ظ‚ظ†ط§ط© ط£ظˆ ط§ظ„ط¬ظ„ط³ط©)
        if custom_font:
            # Normalize path to handle both forward and backward slashes
            custom_font_norm = os.path.normpath(custom_font)
            if os.path.exists(custom_font_norm):
                cand = os.path.abspath(custom_font_norm)
                if _font_supports_required_chars(cand, required_cps):
                    return cand
                logger.warning(f"Custom font does not support required characters, skipping: {cand}")
            # Also try with current directory if relative path
            elif not os.path.isabs(custom_font_norm):
                custom_font_abs = os.path.abspath(custom_font_norm)
                if os.path.exists(custom_font_abs):
                    cand = custom_font_abs
                    if _font_supports_required_chars(cand, required_cps):
                        return cand
                    logger.warning(f"Custom font does not support required characters, skipping: {cand}")

        # 2. ط§ظƒطھط´ط§ظپ ط§ظ„ظ„ط؛ط© (ظٹط´ظ…ظ„ Arabic Presentation Forms)
        is_ar = _is_arabicish(text)
        is_thai = _is_thai(text)
        cfg = load_config()

        # 3. ط§ظ„ط£ظˆظ„ظˆظٹط© ط§ظ„ط«ط§ظ†ظٹط© ظ„ظ„ط®ط·ظˆط· ط§ظ„ط¹ط§ظ„ظ…ظٹط© ط§ظ„ظ…ط­ط¯ط¯ط© ظپظٹ ط§ظ„ط¥ط¹ط¯ط§ط¯ط§طھ
        if is_ar and cfg.GLOBAL_FONT_AR:
            global_ar_norm = os.path.normpath(cfg.GLOBAL_FONT_AR)
            if os.path.exists(global_ar_norm):
                cand = os.path.abspath(global_ar_norm)
                if _font_supports_required_chars(cand, required_cps):
                    return cand
                logger.warning(f"GLOBAL_FONT_AR does not support required characters, skipping: {cand}")
            # Also try with current directory if relative path
            elif not os.path.isabs(global_ar_norm):
                global_ar_abs = os.path.abspath(global_ar_norm)
                if os.path.exists(global_ar_abs):
                    cand = global_ar_abs
                    if _font_supports_required_chars(cand, required_cps):
                        return cand
                    logger.warning(f"GLOBAL_FONT_AR does not support required characters, skipping: {cand}")
        elif not is_ar and cfg.GLOBAL_FONT_EN:
            global_en_norm = os.path.normpath(cfg.GLOBAL_FONT_EN)
            if os.path.exists(global_en_norm):
                return os.path.abspath(global_en_norm)
            # Also try with current directory if relative path
            elif not os.path.isabs(global_en_norm):
                global_en_abs = os.path.abspath(global_en_norm)
                if os.path.exists(global_en_abs):
                    return global_en_abs

        # 4. ط§ظ„ط£ظˆظ„ظˆظٹط© ط§ظ„ط«ط§ظ„ط«ط© ظ„ظ„ط®ط· ط§ظ„ظ…ط­ظ„ظٹ ط§ظ„ظ…ظˆط«ظˆظ‚ (طھط¬ظ†ط¨ط§ظ‹ ظ„ظ…ط´ط§ظƒظ„ ط§ظ„ظ†ط¸ط§ظ…)
        local_ar_font = os.path.join(".data", "fonts", "fallback_ar.ttf")
        if is_ar and os.path.exists(local_ar_font):
            cand = os.path.abspath(local_ar_font)
            if _font_supports_required_chars(cand, required_cps):
                return cand

        # 5. ظ‚ط§ط¦ظ…ط© ط§ظ„ط®ط·ظˆط· ط§ظ„ط§ط­طھظٹط§ط·ظٹط© ظ„ظ„ظ†ط¸ط§ظ…
        if is_ar:
            font_candidates = []

            # ط®ط·ظˆط· ط§ظ„ظ…ط³طھط®ط¯ظ… (ط£ظˆظ„ظˆظٹط© ط¹ط§ظ„ظٹط©)
            for user_font_dir in (
                os.path.join("font", "arabic"),
                os.path.join("fonts", "arabic"),
                os.path.join(".data", "fonts"),
                os.path.join(".temp", "fonts"),
            ):
                font_candidates.extend(_iter_font_files(user_font_dir))

            # ط®ط·ظˆط· ط§ظ„ظ†ط¸ط§ظ…
            font_candidates.extend(
                [
                    "C:/Windows/Fonts/arialuni.ttf",
                    "C:/Windows/Fonts/tahoma.ttf",
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/arabtype.ttf",
                    "C:/Windows/Fonts/segoeui.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
                    "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
                    "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
                    "/system/fonts/NotoNaskhArabic-Regular.ttf", # Android
                    "/system/fonts/NotoSansArabic-Regular.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", # Linux
                ]
            )
        else:
            font_candidates = []
            if is_thai:
                font_candidates.extend(
                    [
                        "C:/Windows/Fonts/leelawui.ttf",
                        "C:/Windows/Fonts/LeelawUI.ttf",
                        "C:/Windows/Fonts/tahoma.ttf",
                        "/system/fonts/NotoSansThai-Regular.ttf",
                        "/system/fonts/NotoSansThaiUI-Regular.ttf",
                        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
                        "/usr/share/fonts/truetype/noto/NotoSansThaiUI-Regular.ttf",
                    ]
                )
            font_candidates.extend(
                [
                    "C:/Windows/Fonts/tahoma.ttf",       # ظٹط¯ط¹ظ… ط§ظ„طھط§ظٹظ„ط§ظ†ط¯ظٹط© ظˆط§ظ„ط¹ط±ط¨ظٹط© ط¨ط´ظƒظ„ ط¬ظٹط¯
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/segoeui.ttf",
                    "/system/fonts/Roboto-Regular.ttf", # Android
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", # Linux
                ]
            )

        fontfile = None
        for f in font_candidates:
            if not os.path.exists(f):
                continue
            cand = os.path.abspath(f)
            if _font_supports_required_chars(cand, required_cps):
                fontfile = cand
                break
        
        # 6. ط®ط· ط§ط­طھظٹط§ط·ظٹ ظ†ظ‡ط§ط¦ظٹ ط´ط§ظ…ظ„
        if not fontfile:
            for global_fallback in (
                os.path.join(".data", "fonts", "overlay_fallback.ttf"),
                os.path.join(".data", "fonts", "fallback_ar.ttf"),
                os.path.join(".temp", "fonts", "overlay_fallback.ttf"),
            ):
                if os.path.exists(global_fallback):
                    cand = os.path.abspath(global_fallback)
                    if _font_supports_required_chars(cand, required_cps):
                        fontfile = cand
                        break

        # 7. ط§ظ„طھظ†ط²ظٹظ„ ط§ظ„طھظ„ظ‚ط§ط¦ظٹ ظ„ط®ط· ط§ط­طھظٹط§ط·ظٹ ط¥ط°ط§ ظ„ظ… ظٹطھظ… ط§ظ„ط¹ط«ظˆط± ط¹ظ„ظ‰ ط£ظٹ ط®ط·
        if not fontfile:
            try:
                import urllib.request
                import tempfile
                
                fallback_dir = os.path.join(tempfile.gettempdir(), "automod_fonts")
                os.makedirs(fallback_dir, exist_ok=True)
                downloaded_font_path = os.path.join(fallback_dir, "Cairo-Regular.ttf")
                
                if not os.path.exists(downloaded_font_path):
                    logger.info("â¬‡ï¸ڈ Downloading fallback font 'Cairo-Regular.ttf' from Google Fonts...")
                    font_urls = [
                        "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/Cairo-Regular.ttf",
                        "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/static/Cairo-Regular.ttf",
                        "https://raw.githubusercontent.com/googlefonts/cairo/main/fonts/ttf/Cairo-Regular.ttf",
                    ]
                    last_download_error = None
                    for font_url in font_urls:
                        try:
                            urllib.request.urlretrieve(font_url, downloaded_font_path)
                            last_download_error = None
                            logger.info("âœ… Fallback font downloaded successfully.")
                            break
                        except Exception as download_err:
                            last_download_error = download_err
                            continue
                    if last_download_error is not None and not os.path.exists(downloaded_font_path):
                        raise last_download_error
                
                if os.path.exists(downloaded_font_path) and _font_supports_required_chars(downloaded_font_path, required_cps):
                    fontfile = downloaded_font_path
            except Exception as e:
                logger.warning(f"Failed to auto-download fallback font: {e}")

        if not fontfile:
            if required_cps:
                raise RuntimeError(
                    "No suitable font found for overlay text. "
                    "Set GLOBAL_FONT_AR/EN in .env (or in tg_state.json global_fonts) "
                    "or upload a font that supports the required language."
                )
            fontfile = "C:/Windows/Fonts/arial.ttf"
            if not os.path.exists(fontfile):
                # Try generic linux path if Windows doesn't exist
                fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            if not os.path.exists(fontfile):
                # Try Android path
                fontfile = "/system/fonts/Roboto-Regular.ttf"

        return os.path.abspath(fontfile)

    def _add_top_overlay_text(self, input_path: str, output_path: str, text: str, custom_font: Optional[str] = None, top_text_size: int = 64, top_text_y: int = 150, is_custom: bool = False):
        """ط¥ط¶ط§ظپط© ظ†طµ ظپظٹ ط£ط¹ظ„ظ‰ ط§ظ„ظپظٹط¯ظٹظˆ ط·ظˆط§ظ„ ط§ظ„ظˆظ‚طھ"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic text: {e}")
                display_text = text
        else:
            display_text = text

        # ط§ط³طھط®ط¯ط§ظ… UTF-8 (ط¨ط¯ظˆظ† BOM) ظ„ط¶ظ…ط§ظ† ط£ظ‚طµظ‰ طھظˆط§ظپظ‚ ظ…ط¹ FFmpeg 
        text_file = self.temp_dir / f"text_{uuid.uuid4().hex}.txt"
        try:
            # ظƒطھط§ط¨ط© ط§ظ„ظ†طµ ط¨طھط´ظپظٹط± UTF-8 ط¹ط§ط¯ظٹ
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)
            
            # ظˆط¸ظٹظپط© ظ…ط³ط§ط¹ط¯ط© ظ„ظ‡ط±ظˆط¨ ط§ظ„ظ…ط³ط§ط±ط§طھ ظپظٹ ظˆظٹظ†ط¯ظˆط² ظ„ظ€ FFmpeg filters
            def ffmpeg_escape_path(path_str):
                p = str(path_str).replace("\\", "/")
                p = p.replace(":", "\\:")
                return p
            
            text_file_esc = ffmpeg_escape_path(text_file)
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = ffmpeg_escape_path(fontfile)

            logger.info(f"FFmpeg Path Escaped - Text: {text_file_esc}, Font: {font_esc}")

            # ط§ظ„ط£ظ„ظˆط§ظ† ظˆط§ظ„ظ†ظ…ط· (ط¨ظ†ط§ط، ط¹ظ„ظ‰ ط·ظ„ط¨ ط§ظ„ظ…ط³طھط®ط¯ظ…: ظ†طµ ط£ط¨ظٹط¶ ظ…ط¹ ط®ظ„ظپظٹط© ط³ظˆط¯ط§ط، ط´ط¨ظ‡ ط´ظپط§ظپط©)
            if is_custom:
                # ظپظٹ ط§ظ„ظ†ظ…ط· ط§ظ„ظ…ط®طµطµطŒ ظ†ظƒط¨ط± ط§ظ„ط®ط· ظ‚ظ„ظٹظ„ط§ظ‹ ظ„ظٹظƒظˆظ† ط£ظˆط¶ط­
                top_text_size = int(top_text_size * 1.2) if top_text_size == 64 else top_text_size
            
            font_color = "white"
            box_opt = "box=1:boxcolor=black@0.6:boxborderw=15:"

            # ط¨ظ†ط§ط، ط§ظ„ظپظ„طھط±
            # ظ†ط³طھط®ط¯ظ… : ط¨ط¯ظ„ط§ظ‹ ظ…ظ† ' ' ظ„ظ„ظ…ط³ط§ط±ط§طھ ظ„ط£ظ†ظ†ط§ ظ‚ظ…ظ†ط§ ط¨ط§ظ„ظ‡ط±ظˆط¨ ظٹط¯ظˆظٹط§ظ‹
            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"{box_opt}"
                f"fontsize={top_text_size}:"
                f"fontcolor={font_color}:"
                f"x=(w-text_w)/2:"
                f"y={top_text_y}:"
                f"fix_bounds=1" # ط¶ظ…ط§ظ† ط¹ط¯ظ… ط®ط±ظˆط¬ ط§ظ„ظ†طµ ط¹ظ† ط§ظ„ط´ط§ط´ط©
            )
            
            logger.debug(f"Applying VF filter: {drawtext_filter}")

            # ًں”§ ط§ط³طھط®ط¯ط§ظ… ط¥ط¹ط¯ط§ط¯ط§طھ ظˆط³ظٹط· ظ…ط­ط³ظ‘ظ†ط© (طھط­طھط±ظ… RENDER / LOW_RESOURCE_MODE)
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            if _is_low_resource_env():
                preset = base_preset  # Already forced to ultrafast
                crf = base_crf        # Already forced to 28
            else:
                preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
                crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
            fps = self._get_video_fps(input_path)
            if not fps or fps <= 0:
                fps = 30.0

            low_resource = _is_low_resource_env()
            video_extra_args = []
            if low_resource:
                video_extra_args.extend(["-bf", "0", "-tune", "zerolatency"])

            base_cmd = [
                ffmpeg_bin(),
                "-y",
                *_ffmpeg_memory_guard_args(),
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                *video_extra_args,
                "-c:a", "copy",
                str(output_path)
            ]

            def _run_filter(vf: str) -> Tuple[int, str]:
                cmd = list(base_cmd)
                cmd[cmd.index("-vf") + 1] = vf
                _ovl_timeout = self._resolve_ffmpeg_timeout(
                    input_path, "FFMPEG_OVERLAY_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
                )
                return _run_ffmpeg_with_idle_timeout(
                    cmd, timeout_s=_ovl_timeout, idle_timeout_s=90, label="TopOverlay"
                )

            attempts = []

            attempts.append(drawtext_filter)
            attempts.append(drawtext_filter.replace(":fix_bounds=1", ""))

            no_font_filter = drawtext_filter.replace(f"fontfile='{font_esc}':", "")
            attempts.append(no_font_filter)
            attempts.append(no_font_filter.replace(":fix_bounds=1", ""))

            direct_text = display_text
            direct_text = direct_text.replace("\\\\", "\\\\\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            direct_text = direct_text.replace("'", "\\\\'").replace(":", "\\:").replace(",", "\\,")
            shaping_direct = "text_shaping=1:" if (is_ar and use_text_shaping) else ""
            attempts.append(
                f"drawtext=text='{direct_text}':fontfile='{font_esc}':{shaping_direct}{box_opt}fontsize={top_text_size}:fontcolor={font_color}:x=(w-text_w)/2:y={top_text_y}"
            )
            attempts.append(
                f"drawtext=text='{direct_text}':{shaping_direct}{box_opt}fontsize={top_text_size}:fontcolor={font_color}:x=(w-text_w)/2:y={top_text_y}"
            )

            last_stderr = ""
            for vf in attempts:
                logger.debug(f"Applying VF filter: {vf}")
                rc, stderr_text = _run_filter(vf)
                if rc == 0:
                    logger.info("âœ… Top overlay text added to video")
                    return
                last_stderr = (stderr_text or "")[-2500:]
                logger.error(f"FFmpeg failed to add top overlay text: {last_stderr}")

            raise RuntimeError(f"Failed to add top overlay text via FFmpeg drawtext. Last error: {last_stderr}")
        finally:
            # ط­ط°ظپ ظ…ظ„ظپ ط§ظ„ظ†طµ ط§ظ„ظ…ط¤ظ‚طھ
            if text_file.exists():
                try:
                    text_file.unlink()
                except:
                    pass

    def add_custom_overlay_text(
        self, input_path: str, output_path: str, text: str,
        timing: str = "full",
        duration: float = 2.0,
        screen_position: str = "top",
        overlay_image_path: Optional[str] = None,
        intro_animation: Optional[Dict[str, Any]] = None,
        outro_animation: Optional[Dict[str, Any]] = None,
        custom_font: Optional[str] = None,
        font_size: int = 56,
    ) -> None:
        # Ensure temp and output dirs exist
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        if os.path.dirname(output_path):
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        """ط¥ط¶ط§ظپط© ظ†طµ ظ…ط®طµطµ ط¹ظ„ظ‰ ط§ظ„ظپظٹط¯ظٹظˆ ط¨ظ„ظˆظ† ط£ط¨ظٹط¶ ظˆط­ط¯ظˆط¯ ط³ظˆط¯ط§ط، ط³ظ…ظٹظƒط©

        Args:
            timing: "start" | "end" | "full"
            duration: ظ…ط¯ط© ط§ظ„ط¸ظ‡ظˆط± ط¨ط§ظ„ط«ظˆط§ظ†ظٹ (ظپظ‚ط· ظ„ظ€ start/end)
            screen_position: "top" | "center" | "bottom"
        """
        def _normalize_animation(raw_animation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
            if not isinstance(raw_animation, dict):
                return {"enabled": False, "type": "none", "duration": 0.0}
            animation_type = str(raw_animation.get("type") or "none").strip().lower()
            if animation_type not in {"fade", "blur"}:
                return {"enabled": False, "type": "none", "duration": 0.0}
            try:
                anim_duration = max(0.0, float(raw_animation.get("duration", 0.0) or 0.0))
            except Exception:
                anim_duration = 0.0
            if not raw_animation.get("enabled") or anim_duration <= 0:
                return {"enabled": False, "type": "none", "duration": 0.0}
            return {"enabled": True, "type": animation_type, "duration": min(anim_duration, 2.0)}

        intro_anim = _normalize_animation(intro_animation)
        outro_anim = _normalize_animation(outro_animation)
        contains_non_ascii = any(ord(ch) > 127 for ch in (text or ""))
        if intro_anim.get("enabled") or outro_anim.get("enabled") or overlay_image_path or contains_non_ascii:
            return self._add_custom_overlay_text_via_image_overlay(
                input_path=input_path,
                output_path=output_path,
                text=text,
                timing=timing,
                duration=duration,
                screen_position=screen_position,
                overlay_image_path=overlay_image_path,
                intro_animation=intro_anim,
                outro_animation=outro_anim,
                custom_font=custom_font,
                font_size=font_size,
            )

        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}

        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic text for custom overlay: {e}")
                display_text = text
        else:
            display_text = text

        text_file = self.temp_dir / f"ctext_{uuid.uuid4().hex}.txt"
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)

            text_file_esc = str(text_file).replace("\\", "/").replace(":", "\\:")
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = str(fontfile).replace("\\", "/").replace(":", "\\:")

            # â€” ط§ظ„ظ…ظˆط¶ط¹ â€”
            pos_key = (screen_position or "top").strip().lower()
            if pos_key in {"bottom", "bottom_center", "bottom-center"}:
                y_expr = "h-text_h-80"
            elif pos_key in {"center", "middle"}:
                y_expr = "(h-text_h)/2"
            else:
                y_expr = "80"

            # â€” ط§ظ„طھظˆظ‚ظٹطھ (enable) â€”
            enable_opt = ""
            if timing in ("start", "end"):
                # ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ظ…ط¯ط© ط§ظ„ظپظٹط¯ظٹظˆ
                total_dur = None
                try:
                    prd = subprocess.run(
                        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
                        capture_output=True, text=True, timeout=5,
                    )
                    if prd.stdout.strip():
                        total_dur = float(prd.stdout.strip())
                except Exception:
                    total_dur = None

                if total_dur and total_dur > 0:
                    if timing == "start":
                        end_t = min(duration, total_dur)
                        enable_opt = f":enable='between(t,0,{end_t:.2f})'"
                    else:  # end
                        start_t = max(0, total_dur - duration)
                        enable_opt = f":enable='between(t,{start_t:.2f},{total_dur:.2f})'"
                # ط¥ط°ط§ ظ„ظ… ظ†طھظ…ظƒظ† ظ…ظ† ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ط§ظ„ظ…ط¯ط©طŒ ظ†ط¹ط±ط¶ ط§ظ„ظ†طµ ط·ظˆظ„ ط§ظ„ظپظٹط¯ظٹظˆ

            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            # ط§ظ„ظ†ظ…ط·: ط£ط¨ظٹط¶ ظ…ط¹ ط­ط¯ظˆط¯ ط³ظˆط¯ط§ط، ط³ظ…ظٹظƒط© (ط¨ط¯ظˆظ† ظ…ط±ط¨ط¹ ط®ظ„ظپظٹط©)
            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"fontsize={font_size}:"
                f"fontcolor=white:"
                f"borderw=4:"
                f"bordercolor=black:"
                f"x=(w-text_w)/2:"
                f"y={y_expr}:"
                f"fix_bounds=1"
                f"{enable_opt}"
            )

            logger.debug(f"Custom overlay VF filter: {drawtext_filter}")

            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            if _is_low_resource_env():
                preset = base_preset  # Already forced to ultrafast
                crf = base_crf        # Already forced to 28
            else:
                preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
                crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
            fps = self._get_video_fps(input_path)
            if not fps or fps <= 0:
                fps = 30.0

            base_cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                "-c:a", "copy",
                str(output_path)
            ]

            def _run_filter(vf: str) -> Tuple[int, str]:
                cmd = list(base_cmd)
                cmd[5] = vf
                _ovl_timeout = self._resolve_ffmpeg_timeout(
                    input_path, "FFMPEG_OVERLAY_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
                )
                return _run_ffmpeg_with_idle_timeout(
                    cmd, timeout_s=_ovl_timeout, idle_timeout_s=90, label="CustomOverlay"
                )

            attempts = [drawtext_filter]
            attempts.append(drawtext_filter.replace(":fix_bounds=1", ""))

            no_font_filter = drawtext_filter.replace(f"fontfile='{font_esc}':", "")
            attempts.append(no_font_filter)
            attempts.append(no_font_filter.replace(":fix_bounds=1", ""))

            last_stderr = ""
            for vf in attempts:
                logger.debug(f"Custom overlay attempt: {vf}")
                rc, stderr_text = _run_filter(vf)
                if rc == 0:
                    logger.info("âœ… Custom overlay text added to video")
                    return
                last_stderr = (stderr_text or "")[-2500:]
                logger.error(f"FFmpeg custom overlay failed: {last_stderr}")

            raise RuntimeError(f"Failed to add custom overlay text via FFmpeg. Last error: {last_stderr}")
        finally:
            if text_file.exists():
                try:
                    text_file.unlink()
                except:
                    pass

    def _add_custom_overlay_text_via_image_overlay(
        self,
        input_path: str,
        output_path: str,
        text: str,
        timing: str = "full",
        duration: float = 2.0,
        screen_position: str = "top",
        overlay_image_path: Optional[str] = None,
        intro_animation: Optional[Dict[str, Any]] = None,
        outro_animation: Optional[Dict[str, Any]] = None,
        custom_font: Optional[str] = None,
        font_size: int = 56,
    ) -> None:
        from PIL import Image, ImageDraw, ImageFont, ImageOps

        intro_animation = intro_animation or {"enabled": False, "type": "none", "duration": 0.0}
        outro_animation = outro_animation or {"enabled": False, "type": "none", "duration": 0.0}
        video_duration = self._get_video_duration(input_path)
        if video_duration <= 0:
            raise RuntimeError("Invalid video duration for overlay text animation")

        visible_start = 0.0
        visible_end = video_duration
        try:
            window_duration = max(0.5, float(duration or 0.0))
        except Exception:
            window_duration = 2.0

        timing_key = (timing or "full").strip().lower()
        if timing_key == "start":
            visible_end = min(video_duration, window_duration)
        elif timing_key == "end":
            visible_start = max(0.0, video_duration - window_duration)

        visible_window = max(0.05, visible_end - visible_start)
        intro_dur = min(max(0.0, float(intro_animation.get("duration", 0.0) or 0.0)), visible_window)
        outro_dur = min(max(0.0, float(outro_animation.get("duration", 0.0) or 0.0)), visible_window)
        total_anim = intro_dur + outro_dur
        if total_anim > visible_window and total_anim > 0:
            scale = visible_window / total_anim
            intro_dur *= scale
            outro_dur *= scale

        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))

        def _shape_line(value: str) -> str:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    return get_display(arabic_reshaper.reshape(value))
                except Exception:
                    return value
            return value

        def _resolve_overlay_image(raw_path: Optional[str]) -> Optional[str]:
            candidate = str(raw_path or "").strip()
            if not candidate:
                return None
            candidate = candidate.replace("\\", "/")
            probe_paths = [candidate]
            if not os.path.isabs(candidate):
                probe_paths.append(os.path.abspath(candidate))
            for path_value in probe_paths:
                try:
                    if path_value and os.path.isfile(path_value):
                        return path_value
                except Exception:
                    continue
            return None

        vw, vh = self._get_video_dimensions(input_path)
        if not vw or not vh:
            vw, vh = 1080, 1920

        fontfile = self._get_best_font(text, custom_font)
        try:
            font = ImageFont.truetype(str(fontfile), font_size)
        except Exception:
            font = ImageFont.load_default()

        overlay_image = None
        resolved_overlay_image = _resolve_overlay_image(overlay_image_path)
        if resolved_overlay_image:
            try:
                with Image.open(resolved_overlay_image) as raw_img:
                    overlay_image = raw_img.convert("RGBA")
            except Exception as img_err:
                logger.warning("Failed to load overlay image '%s': %s", resolved_overlay_image, img_err)
                overlay_image = None

        padding_x = 40
        padding_y = 24
        gap_x = 26
        stroke_w = 4
        line_spacing = 10
        max_overlay_w = max(220, int(vw - 80))
        measure = ImageDraw.Draw(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))

        def _wrap_lines(src_text: str, max_w: int) -> list[str]:
            raw_lines = (src_text or "").splitlines() or [src_text or ""]
            wrapped: list[str] = []
            for part in raw_lines:
                words = part.split()
                if not words:
                    wrapped.append("")
                    continue
                current = ""
                for word in words:
                    probe = f"{current} {word}".strip()
                    if measure.textlength(_shape_line(probe), font=font) <= max_w:
                        current = probe
                    else:
                        if current:
                            wrapped.append(current)
                        current = word
                if current:
                    wrapped.append(current)
            fallback = (src_text or text or "").strip() or " "
            return wrapped[:5] or [fallback]

        def _measure_text(lines: list[str]) -> tuple[int, int, list[int], list[str]]:
            display_lines = [_shape_line(line) for line in lines]
            max_w = 1
            total_h = 0
            heights: list[int] = []
            for display_line in display_lines:
                bbox = measure.textbbox((0, 0), display_line or " ", font=font, stroke_width=stroke_w)
                line_w = max(1, bbox[2] - bbox[0]) if bbox else max(1, int(measure.textlength(display_line or " ", font=font)))
                line_h = max(font_size, bbox[3] - bbox[1]) if bbox else font_size
                max_w = max(max_w, line_w)
                total_h += line_h
                heights.append(line_h)
            if len(display_lines) > 1:
                total_h += line_spacing * (len(display_lines) - 1)
            return max_w, max(total_h, font_size), heights, display_lines

        base_text_max = int(vw * (0.62 if overlay_image is not None else 0.82))
        logical_lines = _wrap_lines(text, max(140, base_text_max))
        text_width, text_height, line_heights, display_lines = _measure_text(logical_lines)

        image_box_size = 0
        if overlay_image is not None:
            desired = max(80, text_height)
            max_image_size = max(80, min(int(vh * 0.32), int(vw * 0.34)))
            image_box_size = max(80, min(desired, max_image_size))

            if text_width + image_box_size + gap_x + padding_x * 2 > max_overlay_w:
                text_limit = max(140, max_overlay_w - (image_box_size + gap_x + padding_x * 2))
                logical_lines = _wrap_lines(text, text_limit)
                text_width, text_height, line_heights, display_lines = _measure_text(logical_lines)

            if text_width + image_box_size + gap_x + padding_x * 2 > max_overlay_w:
                image_box_size = max(64, max_overlay_w - (text_width + gap_x + padding_x * 2))

        content_h = max(text_height, image_box_size if image_box_size > 0 else 0)
        overlay_w = text_width + padding_x * 2
        if image_box_size > 0:
            overlay_w += image_box_size + gap_x
        overlay_w = min(max_overlay_w, max(240, int(overlay_w)))
        overlay_h = max(120, int(content_h + padding_y * 2))

        img = Image.new("RGBA", (overlay_w, overlay_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        text_area_x = padding_x
        text_area_w = max(1, overlay_w - padding_x * 2)
        image_box_x = 0
        image_box_y = 0
        if image_box_size > 0:
            text_area_w = max(1, overlay_w - padding_x * 2 - image_box_size - gap_x)
            image_box_x = overlay_w - padding_x - image_box_size
            image_box_y = max(0, int((overlay_h - image_box_size) / 2))

        y_cursor = max(0, int((overlay_h - text_height) / 2))
        for idx, line in enumerate(display_lines):
            bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_w)
            line_w = max(1, bbox[2] - bbox[0]) if bbox else max(1, int(draw.textlength(line or " ", font=font)))
            x = text_area_x + max(0, int((text_area_w - line_w) / 2))
            draw.text(
                (x, y_cursor),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=stroke_w,
                stroke_fill=(0, 0, 0, 255),
            )
            y_cursor += line_heights[idx] + line_spacing

        if overlay_image is not None and image_box_size > 0:
            try:
                if hasattr(Image, "Resampling"):
                    resample_mode = Image.Resampling.LANCZOS
                else:
                    resample_mode = Image.LANCZOS
                fitted = ImageOps.contain(overlay_image, (image_box_size, image_box_size), method=resample_mode)
                if fitted.mode != "RGBA":
                    fitted = fitted.convert("RGBA")
                px = image_box_x + max(0, (image_box_size - fitted.width) // 2)
                py = image_box_y + max(0, (image_box_size - fitted.height) // 2)
                img.alpha_composite(fitted, (px, py))
            except Exception as resize_err:
                logger.warning("Failed to place overlay image: %s", resize_err)

        pos_key = (screen_position or "top").strip().lower()
        y_expr = "80"
        if pos_key in {"bottom", "bottom_center", "bottom-center"}:
            y_expr = "H-h-80"
        elif pos_key in {"center", "middle"}:
            y_expr = "(H-h)/2"

        overlay_path = self.temp_dir / f"overlay_text_{uuid.uuid4().hex}.png"
        img.save(overlay_path)

        fade_filters = []
        if intro_animation.get("enabled") and intro_animation.get("type") in {"fade", "blur"} and intro_dur > 0:
            fade_filters.append(f"fade=t=in:st={visible_start:.2f}:d={intro_dur:.2f}:alpha=1")
        if outro_animation.get("enabled") and outro_animation.get("type") in {"fade", "blur"} and outro_dur > 0:
            fade_start = max(visible_start, visible_end - outro_dur)
            fade_filters.append(f"fade=t=out:st={fade_start:.2f}:d={outro_dur:.2f}:alpha=1")

        blur_terms = []
        max_blur = 14.0
        if intro_animation.get("enabled") and intro_animation.get("type") == "blur" and intro_dur > 0:
            blur_terms.append(f"if(between(t,{visible_start:.2f},{visible_start + intro_dur:.2f}),{max_blur:.2f}*(1-((t-{visible_start:.2f})/{intro_dur:.2f})),0)")
        if outro_animation.get("enabled") and outro_animation.get("type") == "blur" and outro_dur > 0:
            outro_start = max(visible_start, visible_end - outro_dur)
            blur_terms.append(f"if(between(t,{outro_start:.2f},{visible_end:.2f}),{max_blur:.2f}*(((t-{outro_start:.2f})/{outro_dur:.2f})),0)")
        blur_expr = "+".join(blur_terms) if blur_terms else "0"

        overlay_chain = ["[1:v]format=rgba", "setpts=PTS-STARTPTS"]
        overlay_chain.extend(fade_filters)
        if blur_terms:
            overlay_chain.append(
                f"boxblur=luma_radius='{blur_expr}':luma_power=1:chroma_radius='{blur_expr}':chroma_power=1"
            )

        filter_complex = ",".join(overlay_chain) + f"[ovl];[0:v][ovl]overlay=x=(W-w)/2:y={y_expr}:shortest=1:enable='between(t,{visible_start:.2f},{visible_end:.2f})'[vout]"

        ff_threads, base_preset, base_crf = self._shorts_x264_settings()
        if _is_low_resource_env():
            preset = base_preset
            crf = base_crf
        else:
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
        level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0

        video_extra_args = []
        if _is_low_resource_env():
            video_extra_args.extend(["-bf", "0", "-tune", "zerolatency"])

        cmd = [
            ffmpeg_bin(),
            "-y",
            *_ffmpeg_memory_guard_args(),
            "-i", input_path,
            "-loop", "1",
            "-i", str(overlay_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-vsync", "cfr",
            "-r", f"{fps:.6f}",
            "-threads", str(ff_threads),
            *video_extra_args,
            "-c:a", "copy",
            str(output_path),
        ]

        try:
            logger.debug("Custom animated overlay filter: %s", filter_complex)
            _anim_timeout = self._resolve_ffmpeg_timeout(
                input_path, "FFMPEG_OVERLAY_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
            )
            rc, stderr_text = _run_ffmpeg_with_idle_timeout(
                cmd, timeout_s=_anim_timeout, idle_timeout_s=90, label="AnimatedOverlay"
            )
            if rc != 0:
                raise RuntimeError(stderr_text[-2500:] or "Animated overlay failed")
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("Animated overlay output file missing or empty")
            logger.info("✅ Custom animated overlay text added to video")
        finally:
            try:
                if overlay_path.exists():
                    overlay_path.unlink()
            except Exception:
                pass
    def add_watermark_text(self, input_path: str, output_path: str, text: str, seed: Optional[str] = None, custom_font: Optional[str] = None) -> None:
        """ط¥ط¶ط§ظپط© Watermark ط´ظپط§ظپ (ط§ط³ظ… ط§ظ„ظ‚ظ†ط§ط©) ط¹ظ„ظ‰ ظپظٹط¯ظٹظˆ ط§ظ„ط´ظˆط±طھط³"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception:
                display_text = text
        else:
            display_text = text

        text_file = self.temp_dir / f"wm_{uuid.uuid4().hex}.txt"
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)

            def ffmpeg_escape_path(path_str):
                p = str(path_str).replace("\\", "/")
                p = p.replace(":", "\\:")
                return p

            text_file_esc = ffmpeg_escape_path(text_file)
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = ffmpeg_escape_path(fontfile)

            positions = [
                "top_center",
                "bottom_center",
                "bottom_left",
                "top_left",
            ]
            seed_s = (seed or "") + "::" + (display_text or "")
            idx = int(hashlib.md5(seed_s.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(positions)
            pos = positions[idx]

            y_expr = "12" if "top" in pos else "h-text_h-28"
            x_expr = "12" if "left" in pos else "(w-text_w)/2"

            try:
                alpha = float(os.getenv("SHORTS_WATERMARK_ALPHA", "0.22") or "0.22")
            except Exception:
                alpha = 0.22
            alpha = max(0.05, min(alpha, 0.9))
            # طھظ†ظˆظٹط¹ ظ„ظˆظ† ط§ط³ظ… ط§ظ„ظ‚ظ†ط§ط© ظ„ظƒظ„ ظپظٹط¯ظٹظˆ ظ…ط¹ ط§ظ„ط­ظپط§ط¸ ط¹ظ„ظ‰ ظ†ظپط³ ط§ظ„ط´ظپط§ظپظٹط©
            wm_colors = [
                "0xFFFFFF",  # white
                "0x87CEFA",  # light sky blue
                "0xA0522D",  # sienna (brown)
                "0xFF7F7F",  # light red
            ]
            try:
                cidx = int(hashlib.md5((seed_s + "::color").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(wm_colors)
            except Exception:
                cidx = 0
            wm_color = wm_colors[cidx]

            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"fontsize=54:"
                f"fontcolor={wm_color}@{alpha}:"
                f"borderw=2:"
                f"bordercolor=black@{min(0.45, alpha + 0.18)}:"
                f"x={x_expr}:"
                f"y={y_expr}:"
                f"fix_bounds=1"
            )

            # ًں”§ ط§ط³طھط®ط¯ط§ظ… ط¥ط¹ط¯ط§ط¯ط§طھ ظˆط³ظٹط· ظ…ط­ط³ظ‘ظ†ط©
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            if _is_low_resource_env():
                base_preset = "ultrafast"
                base_crf = 26
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
            base_cmd = [
                ffmpeg_bin(),
                *_ffmpeg_memory_guard_args(),
                "-y",
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-threads", str(ff_threads),
                "-c:a", "copy",
                str(output_path),
            ]

            def _run_filter(vf: str) -> Tuple[int, str]:
                cmd = list(base_cmd)
                cmd[5] = vf
                _wm_timeout = self._resolve_ffmpeg_timeout(
                    input_path, "FFMPEG_WATERMARK_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
                )
                return _run_ffmpeg_with_idle_timeout(
                    cmd, timeout_s=_wm_timeout, idle_timeout_s=90, label="Watermark"
                )

            attempts = [
                drawtext_filter,
                drawtext_filter.replace(":fix_bounds=1", ""),
                drawtext_filter.replace(f"fontfile='{font_esc}':", ""),
                drawtext_filter.replace(f"fontfile='{font_esc}':", "").replace(":fix_bounds=1", ""),
            ]

            last_stderr = ""
            for vf in attempts:
                rc, stderr_text = _run_filter(vf)
                if rc == 0:
                    return
                last_stderr = (stderr_text or "")[-2500:]

            raise RuntimeError(f"Failed to add watermark via FFmpeg drawtext. Last error: {last_stderr}")
        finally:
            if text_file.exists():
                try:
                    text_file.unlink()
                except Exception:
                    pass

    def add_simple_intro_outro_effects(self, input_path: str, output_path: str, seed: Optional[str] = None, apply_intro: bool = True, apply_outro: bool = True) -> None:
        """ط¥ط¶ط§ظپط© طھط£ط«ظٹط±ط§طھ ط¸ظ‡ظˆط±/ط§ط®طھظپط§ط، ط¨ط³ظٹط·ط© ظ„ظ„ط´ظˆط±طھط³ (ط¨ط¯ظˆظ† ط§ظ†ط²ظ„ط§ظ‚/ط§طھط¬ط§ظ‡ط§طھ)"""
        try:
            effects_enabled = str(os.getenv("SHORTS_EFFECTS_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
        except Exception:
            effects_enabled = True
        if not effects_enabled:
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-c", "copy",
                "-map_metadata", "-1",
                "-movflags", "+faststart",
                "-use_editlist", "0",
                str(output_path),
            ]
            rc, _stderr = _run_ffmpeg_with_idle_timeout(cmd, timeout_s=300, idle_timeout_s=60, label="SimpleEffectsCopy")
            if rc != 0:
                raise RuntimeError(_stderr[-2500:] if _stderr else "SimpleEffects copy failed")
            return

        duration_s = None
        try:
            prd = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if prd.stdout.strip():
                duration_s = float(prd.stdout.strip())
        except Exception:
            duration_s = None

        if not duration_s or duration_s <= 0:
            duration_s = 30.0

        try:
            intro_d = float(os.getenv("SHORTS_EFFECTS_INTRO_SECONDS", "0.25") or "0.25")
        except Exception:
            intro_d = 0.25
        try:
            outro_d = float(os.getenv("SHORTS_EFFECTS_OUTRO_SECONDS", "0.25") or "0.25")
        except Exception:
            outro_d = 0.25

        intro_d = max(0.12, min(intro_d, 0.8))
        outro_d = max(0.12, min(outro_d, 0.8))
        outro_start = max(0.0, float(duration_s) - float(outro_d))

        intro_types = ["fade", "noise", "blur", "darken", "desat"]
        outro_types = ["fade", "noise", "blur", "darken", "desat"]
        base_seed = seed or input_path or ""
        
        # ًں”§ ط¥ط¶ط§ظپط© ظ…ظƒظˆظ† ط¹ط´ظˆط§ط¦ظٹ ط¥ط°ط§ ظƒط§ظ† ط§ظ„ظ€ seed ظپط§ط±ط؛ط§ظ‹ ط£ظˆ ظٹط³ط§ظˆظٹ ظ…ط³ط§ط± ط§ظ„ظ…ظ„ظپ ظپظ‚ط·
        # ظ‡ط°ط§ ظٹط¶ظ…ظ† طھط£ط«ظٹط±ط§طھ ظ…ط®طھظ„ظپط© ظ„ظƒظ„ ظپظٹط¯ظٹظˆ ط­طھظ‰ ظ„ظˆ ظƒط§ظ† ظ†ظپط³ ط§ظ„ظ…ظ„ظپ ط§ظ„ط£ط³ط§ط³ظٹ
        import random
        if not seed or seed == input_path:
            base_seed = f"{base_seed}::{uuid.uuid4().hex}::{random.random()}"
            logger.info(f"[FX] Using randomized seed for unique effects: {base_seed[:50]}...")
        else:
            logger.info(f"[FX] Using provided seed: {base_seed[:50]}...")
        
        intro_type = None
        outro_type = None
        if apply_intro:
            intro_idx = int(hashlib.md5((base_seed + "::intro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(intro_types)
            intro_type = intro_types[intro_idx]
        if apply_outro:
            outro_idx = int(hashlib.md5((base_seed + "::outro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(outro_types)
            outro_type = outro_types[outro_idx]

        def _intro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=in:st=0:d={intro_d}"
            if kind == "noise":
                return f"noise=alls=20:allf=t+u:enable='between(t,0,{intro_d})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,0,{intro_d})'"
            if kind == "darken":
                return f"eq=brightness=-0.05:saturation=0.95:enable='between(t,0,{intro_d})'"
            if kind == "desat":
                return f"fade=t=in:st=0:d={intro_d},eq=saturation=0.75:enable='between(t,0,{intro_d})'"
            return f"fade=t=in:st=0:d={intro_d},eq=saturation=0.75:enable='between(t,0,{intro_d})'"

        def _outro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=out:st={outro_start}:d={outro_d}"
            if kind == "noise":
                return f"noise=alls=22:allf=t+u:enable='between(t,{outro_start},{duration_s})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,{outro_start},{duration_s})'"
            if kind == "darken":
                return f"eq=brightness=-0.06:saturation=0.9:enable='between(t,{outro_start},{duration_s})'"
            if kind == "desat":
                return f"fade=t=out:st={outro_start}:d={outro_d},eq=saturation=0.75:enable='between(t,{outro_start},{duration_s})'"
            return f"fade=t=out:st={outro_start}:d={outro_d},eq=saturation=0.75:enable='between(t,{outro_start},{duration_s})'"

        vf_parts = []
        vf_parts.append("setpts=PTS-STARTPTS")
        if apply_intro:
            vf_parts.append(f"fade=t=in:st=0:d={intro_d}")
            if intro_type and intro_type != "fade":
                vf_parts.append(_intro_filter(intro_type))
        if apply_outro:
            vf_parts.append(f"fade=t=out:st={outro_start}:d={outro_d}")
            if outro_type and outro_type != "fade":
                vf_parts.append(_outro_filter(outro_type))
        vf_parts.append("format=yuv420p")
        vf = ",".join(vf_parts) if vf_parts else "null"

        # ًں”§ ط§ط³طھط®ط¯ط§ظ… ط¥ط¹ط¯ط§ط¯ط§طھ ظˆط³ظٹط· ظ…ط­ط³ظ‘ظ†ط©
        ff_threads, base_preset, base_crf = self._shorts_x264_settings()
        if _is_low_resource_env():
            base_preset = "ultrafast"
            base_crf = 26
        preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
        crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
        level = (os.getenv("SHORTS_H264_LEVEL", "4.2" if _is_low_resource_env() else "5.1") or "5.1").strip() or "5.1"
        has_audio = self._has_audio(input_path)
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        cmd = [
            ffmpeg_bin(),
            *_ffmpeg_memory_guard_args(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-vsync", "cfr",
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-keyint_min", str(max(1, gop // 2)),
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,0)",
            "-threads", str(ff_threads),
        ]
        if has_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "384k", "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output_path)]
        _fx_timeout = self._resolve_ffmpeg_timeout(
            input_path,
            "SHORTS_SIMPLE_EFFECTS_TIMEOUT_SECONDS",
            300,
            600,
            8.0,
            8.0,
            extra_seconds=90,
        )
        _fx_idle = min(90, max(30, _fx_timeout // 4))
        rc, stderr_text = _run_ffmpeg_with_idle_timeout(
            cmd, timeout_s=_fx_timeout, idle_timeout_s=_fx_idle, label="SimpleEffects"
        )
        if rc != 0:
            raise RuntimeError(stderr_text[-2500:] or f"SimpleEffects FFmpeg exited with code {rc}")
    
    def _get_video_duration(self, video_path: str) -> float:
        """ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ظ…ط¯ط© ط§ظ„ظپظٹط¯ظٹظˆ"""
        if not video_path or (not os.path.exists(video_path)) or os.path.getsize(video_path) <= 0:
            raise RuntimeError(f"Failed to get video duration: file missing or empty: {video_path}")
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get video duration: {result.stderr}")
        
        return float(result.stdout.strip())

    def _validate_video_file(self, video_path: str) -> None:
        if not video_path or not os.path.exists(video_path) or os.path.getsize(video_path) <= 0:
            raise RuntimeError(f"Video file empty or missing: {video_path}")
        _ = self._get_video_duration(video_path)
    
    def _get_video_dimensions(self, video_path: str) -> Tuple[int, int]:
        """ط§ظ„ط­طµظˆظ„ ط¹ظ„ظ‰ ط£ط¨ط¹ط§ط¯ ط§ظ„ظپظٹط¯ظٹظˆ"""
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get video dimensions: {result.stderr}")
        
        width, height = map(int, result.stdout.strip().split('x'))
        return width, height

    def _get_video_fps(self, video_path: str) -> float:
        try:
            cmd = [
                ffprobe_bin(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return 30.0
            lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            def _parse_frac(s: str) -> Optional[float]:
                try:
                    if "/" in s:
                        a, b = s.split("/", 1)
                        a = float(a)
                        b = float(b)
                        if b == 0:
                            return None
                        v = a / b
                        return v if v > 0 else None
                    v = float(s)
                    return v if v > 0 else None
                except Exception:
                    return None
            for s in lines:
                v = _parse_frac(s)
                if v and v > 0:
                    return float(v)
            return 30.0
        except Exception:
            return 30.0

    def _has_audio(self, video_path: str) -> bool:
        """ط§ظ„طھط­ظ‚ظ‚ ظ…ظ† ظˆط¬ظˆط¯ ظ…ط³ط§ط± طµظˆطھظٹ ظپظٹ ط§ظ„ظپظٹط¯ظٹظˆ"""
        try:
            cmd = [
                ffprobe_bin(),
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    def _shorts_x264_settings(self) -> Tuple[int, str, int]:
        def _env_int(name: str, default: int) -> int:
            try:
                raw = (os.getenv(name, str(default)) or str(default)).strip()
                return int(float(raw))
            except Exception:
                return default

        def _env_str(name: str, default: str) -> str:
            val = (os.getenv(name, default) or default).strip()
            return val if val else default

        def _env_bool(name: str, default: bool = False) -> bool:
            val = os.getenv(name)
            if val is None:
                return default
            return val.strip().lower() in {"1", "true", "yes", "on"}

        ff_threads = max(1, _env_int("FFMPEG_THREADS", 0))  # 0 = auto

        # Performance optimization: Default to veryfast for speed, CRF 23 for good enough quality
        # This is much faster than the previous 'slow'/'medium' preset
        preset = _env_str("SHORTS_X264_PRESET", _env_str("FFMPEG_X264_PRESET", "veryfast"))
        crf_default = _env_int("FFMPEG_X264_CRF", 23)
        crf = max(18, min(28, _env_int("SHORTS_X264_CRF", crf_default)))

        if _is_low_resource_env():
            # ًں”§ On Render / low-resource: FORCE lightweight settings
            # Do NOT read env vars here â€” they may contain desktop-quality values
            preset = "ultrafast"
            crf = 28
            ff_threads = 1

        return ff_threads, preset, crf

    def _resolve_ffmpeg_timeout(
        self,
        input_path: str,
        env_name: str,
        default_low: int,
        default_high: int,
        multiplier_low: float,
        multiplier_high: float,
        extra_seconds: int = 120,
        minimum_seconds: int = 120,
        maximum_seconds: int = 7200,
    ) -> int:
        is_low = _is_low_resource_env()
        default_timeout = default_low if is_low else default_high
        try:
            timeout_s = int((os.getenv(env_name, str(default_timeout)) or str(default_timeout)).strip())
        except Exception:
            timeout_s = default_timeout

        duration_s = 0.0
        try:
            duration_s = max(0.0, float(self._get_video_duration(input_path) or 0.0))
        except Exception:
            duration_s = 0.0

        multiplier = multiplier_low if is_low else multiplier_high
        if duration_s > 0 and multiplier > 0:
            timeout_s = max(timeout_s, int(round(duration_s * multiplier)) + int(extra_seconds))

        timeout_s = max(int(minimum_seconds), timeout_s)
        if maximum_seconds > 0:
            timeout_s = min(timeout_s, int(maximum_seconds))
        return timeout_s

    def _iter_retry_short_resolutions(self, target_width: int, target_height: int) -> list[Tuple[int, int]]:
        def _normalize_pair(width: int, height: int) -> Optional[Tuple[int, int]]:
            try:
                width = int(width)
                height = int(height)
            except Exception:
                return None
            width = max(144, width)
            height = max(256, height)
            if width % 2 != 0:
                width -= 1
            if height % 2 != 0:
                height -= 1
            if width <= 0 or height <= 0:
                return None
            return width, height

        candidates: list[Tuple[int, int]] = []
        seen: set[Tuple[int, int]] = set()

        primary = _normalize_pair(target_width, target_height)
        if primary:
            candidates.append(primary)
            seen.add(primary)

        env_width = os.getenv("SHORTS_FALLBACK_WIDTH")
        env_height = os.getenv("SHORTS_FALLBACK_HEIGHT")
        if env_width and env_height:
            fallback = _normalize_pair(env_width, env_height)
            if fallback and fallback not in seen and fallback[0] <= target_width and fallback[1] <= target_height:
                candidates.append(fallback)
                seen.add(fallback)

        for width, height in ((540, 960), (360, 640)):
            fallback = _normalize_pair(width, height)
            if not fallback:
                continue
            if fallback in seen:
                continue
            if fallback[0] > target_width or fallback[1] > target_height:
                continue
            candidates.append(fallback)
            seen.add(fallback)

        return candidates
    
    def _trim_video(self, input_path: str, output_path: str, start: float, end: float, force_encode: bool = False):
        """ظ‚طµ ط§ظ„ظپظٹط¯ظٹظˆ ظ…ظ† ط§ظ„ط¨ط¯ط§ظٹط© ظˆط§ظ„ظ†ظ‡ط§ظٹط©"""
        duration = self._get_video_duration(input_path)
        new_duration = duration - start - end

        def _env_int(name: str, default: int) -> int:
            try:
                raw = (os.getenv(name, str(default)) or str(default)).strip()
                return int(float(raw))
            except Exception:
                return default

        def _env_str(name: str, default: str) -> str:
            val = (os.getenv(name, default) or default).strip()
            return val if val else default

        def _env_bool(name: str, default: bool = False) -> bool:
            val = os.getenv(name)
            if val is None:
                return default
            return val.strip().lower() in {"1", "true", "yes", "on"}

        # Ensure temp dir exists right before ffmpeg call
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # ًں”§ ط§ط³طھط®ط¯ط§ظ… _shorts_x264_settings() ظ„ط§ط­طھط±ط§ظ… RENDER / LOW_RESOURCE_MODE
        ff_threads, x264_preset, x264_crf = self._shorts_x264_settings()
        trim_mode = _env_str("FFMPEG_TRIM_MODE", "copy").lower()
        if _env_bool("FFMPEG_LOW_CPU", False) and trim_mode == "encode":
            trim_mode = "copy"

        if force_encode and trim_mode == "copy":
            trim_mode = "encode"

        if trim_mode == "copy":
            cmd = [
                ffmpeg_bin(),
                *_ffmpeg_memory_guard_args(),
                "-y",
                "-ss", str(start),
                "-i", input_path,
                "-t", str(new_duration),
                "-map", "0",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-fflags", "+genpts",
                str(output_path),
            ]
        else:
            fps = self._get_video_fps(input_path)
            fps = _safe_processing_fps(fps)
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            cmd = [
                ffmpeg_bin(),
                *_ffmpeg_memory_guard_args(),
                "-y",
                "-ss", str(start),  # ط§ظ„ط¨ط¯ط§ظٹط©
                "-i", input_path,
                "-t", str(new_duration),  # ط§ظ„ظ…ط¯ط©
                "-map", "0:v",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", x264_preset,
                "-crf", str(x264_crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                "-c:a", "aac",
                "-b:a", "384k",  # YouTube recommended: 384kbps stereo
                "-shortest",
                str(output_path),
            ]
        
        _trim_timeout = self._resolve_ffmpeg_timeout(
            input_path, "FFMPEG_TRIM_TIMEOUT_SECONDS", 120, 180, 2.0, 2.0, extra_seconds=30,
        )
        rc, _stderr = _run_ffmpeg_with_idle_timeout(
            cmd, timeout_s=_trim_timeout, idle_timeout_s=60, label="Trim"
        )
        
        if rc != 0:
            raise RuntimeError(f"Failed to trim video: {_stderr[-500:]}")
        
        logger.info(f"âœ… Video trimmed: {start}s from start, {end}s from end")
    
    def _convert_to_shorts(self, input_path: str, output_path: str, orig_width: int, orig_height: int, shorts_format: str = "crop", hflip: bool = False):
        """طھط­ظˆظٹظ„ ط§ظ„ظپظٹط¯ظٹظˆ ظ„طµظٹط؛ط© ط´ظˆط±طھط³ (9:16 - 1080x1920)

        shorts_format:
            - crop: ظ‚طµ/ظ…ظ„ط، ط§ظ„ط´ط§ط´ط© (ظ‚ط¯ ظٹظ‚طµ ط£ط·ط±ط§ظپ ط§ظ„ظٹظ…ظٹظ†/ط§ظ„ظٹط³ط§ط±)
            - fit_blur: ط¹ط±ط¶ ظƒط§ظ…ظ„ + ط®ظ„ظپظٹط© ط¶ط¨ط§ط¨ظٹط© ظ…ظ† ظ†ظپط³ ط§ظ„ظپظٹط¯ظٹظˆ
            - partial_blur: طھظƒط¨ظٹط± ظ…طھظˆط³ط· (ط¥ط¸ظ‡ط§ط± ط¬ط²ط، ط£ظƒط¨ط± ظ…ظ† ط§ظ„ط£ط¹ظ„ظ‰/ط§ظ„ط£ط³ظپظ„) + ط®ظ„ظپظٹط© ط¶ط¨ط§ط¨ظٹط©
        """
        if hflip:
            hf_filter = "hflip,"
            hf_graph = "[0:v]hflip[vin];"
            vin = "vin"
        else:
            hf_filter = ""
            hf_graph = ""
            vin = "0:v"
        is_low = _is_low_resource_env()
        ff_threads, x264_preset, x264_crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        
        # ًں”§ طھط­ط³ظٹظ†: ط§ط³طھط®ط¯ط§ظ… CRF ظ…ظ†ط®ظپط¶ ط¬ط¯ط§ظ‹ (14) ط¨ط¯ظ„ط§ظ‹ ظ…ظ† lossless (0)
        # ظ‡ط°ط§ ظٹظ‚ظ„ظ„ ط­ط¬ظ… ط§ظ„ظپظٹط¯ظٹظˆ ط§ظ„ظˆط³ظٹط· ط¨ظ†ط³ط¨ط© 90% ظ…ط¹ ط§ظ„ط­ظپط§ط¸ ط¹ظ„ظ‰ ط¬ظˆط¯ط© ط´ط¨ظ‡ lossless
        # yuv420p ظ…طھظˆط§ظپظ‚ ظ…ط¹ ط¬ظ…ظٹط¹ ط§ظ„ظ…ط±ط§ط­ظ„ ط§ظ„ظ„ط§ط­ظ‚ط© (ظ„ط§ ظٹظˆط¬ط¯ طھط­ظˆظٹظ„ ظ…ط³ط§ط­ط© ط£ظ„ظˆط§ظ†)
        try:
            lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            lossless_intermediate = False
        
        fps = _safe_processing_fps(self._get_video_fps(input_path))
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        
        if lossless_intermediate:
            x264_preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
            x264_crf = 0
            # x264 lossless (crf=0) is not compatible with profile=high + yuv420p.
            v_profile = "high444"
            v_pix_fmt = "yuv444p"
        else:
            _, base_preset, base_crf = self._shorts_x264_settings()
            if is_low:
                x264_preset = base_preset
                x264_crf = base_crf
            else:
                x264_preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
                x264_crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            v_profile = "high"
            v_pix_fmt = "yuv420p"

        shorts_vol = _parse_volume_ratio(os.getenv("SHORTS_AUDIO_VOLUME", "60"), 0.6)
        scale_flags = "fast_bilinear" if is_low else "lanczos"
        audio_bitrate = "128k" if is_low else "384k"

        target_width, target_height = _shorts_target_resolution()
        if is_low and (target_width > 720 or target_height > 1280):
            logger.warning(
                f"âڑ ï¸ڈ Shorts target resolution {target_width}x{target_height} is too heavy for low-resource mode; clamping to 720x1280"
            )
            target_width, target_height = 720, 1280
        
        # ط­ط³ط§ط¨ ظ†ط³ط¨ط© ط§ظ„ط¹ط±ط¶ ظ„ظ„ط§ط±طھظپط§ط¹
        input_ratio = orig_width / orig_height
        target_ratio = target_width / target_height
        
        fmt = (shorts_format or "crop").strip().lower()

        try:
            skip_if_vertical = str(os.getenv("AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            skip_if_vertical = True
        if skip_if_vertical and fmt == "crop" and orig_width > 0 and orig_height > 0:
            try:
                if abs((orig_width / orig_height) - (9.0 / 16.0)) <= 0.02:
                    import shutil
                    shutil.copy2(input_path, output_path)
                    logger.info(f"âœ… Video already 9:16 ({orig_width}x{orig_height}); skipped shorts conversion.")
                    return
            except Exception:
                pass

        has_audio = self._has_audio(input_path)

        def _even(n: int) -> int:
            try:
                n = int(n)
            except Exception:
                return 2
            if n <= 0:
                return 2
            return n if (n % 2 == 0) else (n - 1)

        def _stderr_tail(raw: Any) -> str:
            if raw is None:
                return ""
            if isinstance(raw, bytes):
                return raw.decode(errors="ignore")[-2500:]
            return str(raw)[-2500:]

        def _build_cmd(
            attempt_width: int,
            attempt_height: int,
            attempt_scale_flags: str,
            attempt_preset: str,
            attempt_crf: int,
            attempt_profile: str,
            attempt_pix_fmt: str,
            attempt_audio_bitrate: str,
            apply_audio_filter: bool,
            tmp_out: str,
        ) -> list[str]:
            attempt_ratio = attempt_width / attempt_height
            cmd = [
                ffmpeg_bin(),
                *_ffmpeg_memory_guard_args(),
                "-hide_banner",
                "-loglevel", "error",
                "-nostats",
                "-y",
                "-i", input_path,
            ]

            if fmt == "fit_blur":
                blur_val = 10 if is_low else 20
                filter_complex = (
                    f"{hf_graph}"
                    f"[{vin}]scale={attempt_width}:{attempt_height}:force_original_aspect_ratio=increase:flags={attempt_scale_flags},"
                    f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                    f"crop={attempt_width}:{attempt_height},"
                    f"boxblur=luma_radius={blur_val}:luma_power=1[bg];"
                    f"[{vin}]scale={attempt_width}:{attempt_height}:force_original_aspect_ratio=decrease:flags={attempt_scale_flags},"
                    f"scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format={attempt_pix_fmt}[outv]"
                )
                cmd += [
                    "-filter_complex", filter_complex,
                    "-map", "[outv]",
                    "-c:v", "libx264",
                    "-preset", attempt_preset,
                    "-crf", str(attempt_crf),
                    "-profile:v", attempt_profile,
                    "-level", level,
                    "-pix_fmt", attempt_pix_fmt,
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-threads", str(ff_threads),
                ]
            elif fmt == "partial_blur":
                zoom = float(os.getenv("SHORTS_PARTIAL_ZOOM", "1.25") or "1.25")
                if zoom < 1.0:
                    zoom = 1.0
                if zoom > 2.0:
                    zoom = 2.0

                blur_val = 10 if is_low else 20
                fg_w = _even(int(attempt_width * zoom))
                fg_h = _even(int(attempt_height * zoom))
                filter_complex = (
                    f"{hf_graph}"
                    f"[{vin}]scale={attempt_width}:{attempt_height}:force_original_aspect_ratio=increase:flags={attempt_scale_flags},"
                    f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                    f"crop={attempt_width}:{attempt_height},"
                    f"boxblur=luma_radius={blur_val}:luma_power=1[bg];"
                    f"[{vin}]scale={fg_w}:{fg_h}:force_original_aspect_ratio=increase:flags={attempt_scale_flags},"
                    f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                    f"crop={attempt_width}:{attempt_height}[fg];"
                    f"[bg][fg]overlay=0:0,format={attempt_pix_fmt}[outv]"
                )
                cmd += [
                    "-filter_complex", filter_complex,
                    "-map", "[outv]",
                    "-c:v", "libx264",
                    "-preset", attempt_preset,
                    "-crf", str(attempt_crf),
                    "-profile:v", attempt_profile,
                    "-level", level,
                    "-pix_fmt", attempt_pix_fmt,
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-threads", str(ff_threads),
                ]
            else:
                if abs(input_ratio - attempt_ratio) < 0.01:
                    vf = f"{hf_filter}scale={attempt_width}:{attempt_height}:flags={attempt_scale_flags}"
                elif input_ratio > attempt_ratio:
                    safe_h = _even(orig_height)
                    new_width = _even(int(safe_h * attempt_ratio))
                    crop_x = _even((orig_width - new_width) // 2)
                    vf = f"{hf_filter}crop={new_width}:{safe_h}:{crop_x}:0,scale={attempt_width}:{attempt_height}:flags={attempt_scale_flags}"
                else:
                    scale_height = attempt_height
                    scale_width = int(scale_height * input_ratio)
                    if scale_width > attempt_width:
                        scale_width = attempt_width
                        scale_height = int(scale_width / input_ratio)

                    scale_width = _even(scale_width)
                    scale_height = _even(scale_height)

                    pad_x = (attempt_width - scale_width) // 2
                    pad_y = (attempt_height - scale_height) // 2

                    vf = f"{hf_filter}scale={scale_width}:{scale_height}:flags={attempt_scale_flags},pad={attempt_width}:{attempt_height}:{pad_x}:{pad_y}:black"

                cmd += [
                    "-vf", vf,
                    "-map", "0:v",
                    "-c:v", "libx264",
                    "-preset", attempt_preset,
                    "-crf", str(attempt_crf),
                    "-profile:v", attempt_profile,
                    "-level", level,
                    "-pix_fmt", attempt_pix_fmt,
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-threads", str(ff_threads),
                ]

            if has_audio:
                cmd += [
                    "-map", "0:a?",
                    "-c:a", "aac",
                    "-b:a", attempt_audio_bitrate,
                    "-ar", "48000",
                ]
                if apply_audio_filter:
                    cmd += ["-af", f"volume={shorts_vol}"]
                cmd += [
                    "-movflags", "+faststart",
                    "-shortest",
                ]
            else:
                cmd += ["-an", "-movflags", "+faststart"]

            cmd += ["-f", "mp4", str(tmp_out)]
            return cmd

        primary_timeout = self._resolve_ffmpeg_timeout(
            input_path,
            "FFMPEG_TIMEOUT_SECONDS",
            600,
            600,
            12.0,
            8.0,
            extra_seconds=120,
        )
        fallback_timeout = self._resolve_ffmpeg_timeout(
            input_path,
            "FFMPEG_FALLBACK_TIMEOUT_SECONDS",
            420,
            540,
            7.0,
            6.0,
            extra_seconds=90,
        )
        idle_timeout_default = 120 if is_low else 180
        try:
            progress_idle_timeout_s = int(
                (os.getenv("FFMPEG_PROGRESS_IDLE_TIMEOUT_SECONDS", str(idle_timeout_default)) or str(idle_timeout_default)).strip()
            )
        except Exception:
            progress_idle_timeout_s = idle_timeout_default
        progress_idle_timeout_s = max(45, min(progress_idle_timeout_s, max(45, primary_timeout - 15)))

        attempts: list[Dict[str, Any]] = [
            {
                "label": "primary",
                "width": target_width,
                "height": target_height,
                "scale_flags": scale_flags,
                "preset": x264_preset,
                "crf": x264_crf,
                "profile": v_profile,
                "pix_fmt": v_pix_fmt,
                "audio_bitrate": audio_bitrate,
                "apply_audio_filter": True,
                "timeout": primary_timeout,
            }
        ]
        for index, (fallback_width, fallback_height) in enumerate(self._iter_retry_short_resolutions(target_width, target_height)[1:], start=1):
            attempts.append(
                {
                    "label": f"fallback_{index}",
                    "width": fallback_width,
                    "height": fallback_height,
                    "scale_flags": "fast_bilinear",
                    "preset": "ultrafast",
                    "crf": 30 if index == 1 else 32,
                    "profile": "high",
                    "pix_fmt": "yuv420p",
                    "audio_bitrate": "96k" if has_audio else audio_bitrate,
                    "apply_audio_filter": False,
                    "timeout": fallback_timeout,
                }
            )

        out_s = str(output_path)
        last_error = ""
        for attempt_index, attempt in enumerate(attempts, start=1):
            if out_s.lower().endswith(".mp4"):
                tmp_out = out_s[:-4] + f".{attempt['label']}.tmp.mp4"
            else:
                tmp_out = out_s + f".{attempt['label']}.tmp.mp4"
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except Exception:
                pass

            cmd = _build_cmd(
                attempt_width=int(attempt["width"]),
                attempt_height=int(attempt["height"]),
                attempt_scale_flags=str(attempt["scale_flags"]),
                attempt_preset=str(attempt["preset"]),
                attempt_crf=int(attempt["crf"]),
                attempt_profile=str(attempt["profile"]),
                attempt_pix_fmt=str(attempt["pix_fmt"]),
                attempt_audio_bitrate=str(attempt["audio_bitrate"]),
                apply_audio_filter=bool(attempt["apply_audio_filter"]),
                tmp_out=tmp_out,
            )
            timeout_s = max(120, int(attempt["timeout"]))
            logger.info(
                f"ًںژ›ï¸ڈ Shorts conversion attempt {attempt_index}/{len(attempts)} "
                f"({attempt['label']}, {attempt['width']}x{attempt['height']}, preset={attempt['preset']}, timeout={timeout_s}s)"
            )

            returncode, stderr_text, stop_reason = _run_ffmpeg_command_with_progress(
                cmd,
                timeout_s=timeout_s,
                idle_timeout_s=min(progress_idle_timeout_s, max(45, timeout_s - 15)),
                progress_label=f"Shorts {attempt['label']}",
            )
            stderr_text = _stderr_tail(stderr_text)
            if stop_reason:
                last_error = stop_reason
                if stderr_text:
                    last_error = f"{last_error}: {stderr_text}"
                logger.warning(f"âڑ ï¸ڈ Shorts conversion attempt {attempt['label']} timed out/stalled. {last_error[-1200:]}")
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass
                continue

            if returncode != 0:
                last_error = stderr_text or f"ffmpeg exited with status {returncode}"
                logger.warning(f"âڑ ï¸ڈ Shorts conversion attempt {attempt['label']} failed. {last_error[-1200:]}")
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass
                continue

            try:
                self._validate_video_file(str(tmp_out))
            except Exception as e:
                last_error = f"output invalid: {e}"
                logger.warning(f"âڑ ï¸ڈ Shorts conversion attempt {attempt['label']} produced invalid output. {last_error[-1200:]}")
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass
                continue

            try:
                if os.path.exists(str(output_path)):
                    os.remove(str(output_path))
            except Exception:
                pass
            try:
                os.replace(str(tmp_out), str(output_path))
            except Exception:
                import shutil
                shutil.copy2(str(tmp_out), str(output_path))
                try:
                    os.remove(str(tmp_out))
                except Exception:
                    pass
            logger.info(f"âœ… Video converted to shorts format: {attempt['width']}x{attempt['height']}")
            return
        
        raise RuntimeError(f"Failed to convert to shorts after {len(attempts)} attempts: {last_error}")
    
    def _add_cta_text(self, input_path: str, output_path: str, text: str, duration: float, custom_font: Optional[str] = None):
        """ط¥ط¶ط§ظپط© ظ†طµ ط§ظ„ط¯ط¹ظˆط© ظپظٹ ظ†ظ‡ط§ظٹط© ط§ظ„ظپظٹط¯ظٹظˆ"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))

        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic CTA text: {e}")
                display_text = text
        else:
            display_text = text

        text_start = max(0, duration - 2.5)
        def _find_app_photo() -> Optional[str]:
            try:
                candidates: list[Path] = []
                env_dir = os.getenv("APP_PHOTO_DIR") or os.getenv("APP_PHOTO_PATH")
                if env_dir:
                    candidates.append(Path(env_dir))
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    candidates.append(repo_root / "app-photo")
                except Exception:
                    pass
                candidates.append(Path("app-photo"))
                for base in candidates:
                    if not base.exists():
                        continue
                    for ext in ("*.png", "*.jpg", "*.jpeg"):
                        files = list(base.glob(ext))
                        if files:
                            return str(files[0])
                return None
            except Exception:
                return None
        app_photo = _find_app_photo()
        try:
            self._add_cta_text_via_image_overlay(input_path, output_path, display_text, text_start, app_photo, custom_font)
        except Exception as e:
            logger.error(f"CTA overlay with fade failed: {e}")
            import shutil
            shutil.copy2(input_path, output_path)

    def _apply_outro_blur_black(self, input_path: str, output_path: str, duration: float = 1.0):
        d = self._get_video_duration(input_path)
        if not d or d <= 0:
            import shutil
            shutil.copy2(input_path, output_path)
            return
        fade_start = max(0.0, d - duration)
        vf = f"boxblur=luma_radius=10:enable='gte(t,{fade_start})',fade=t=out:st={fade_start}:d={duration}"
        has_audio = self._has_audio(input_path)
        ff_threads, preset, crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        try:
            lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            lossless_intermediate = False
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        if lossless_intermediate:
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
            crf = 0
        v_profile = "high444" if lossless_intermediate else "high"
        v_pix_fmt = "yuv444p" if lossless_intermediate else "yuv420p"
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", v_profile,
            "-level", level,
            "-pix_fmt", v_pix_fmt,
            "-vsync", "cfr",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-threads", str(ff_threads),
        ]
        if has_audio:
            shorts_vol = _parse_volume_ratio(os.getenv("SHORTS_AUDIO_VOLUME", "60"), 0.6)
            af = f"volume={shorts_vol},afade=t=out:st={fade_start}:d={duration}"
            cmd += [
                "-af", af,
                "-map", "0:a?",
                "-c:a", "aac",
                "-b:a", "384k",  # YouTube recommended: 384kbps stereo
            ]
        else:
            cmd += ["-an"]
        cmd.append(str(output_path))
        _outro_timeout = self._resolve_ffmpeg_timeout(
            input_path, "FFMPEG_OUTRO_BLUR_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
        )
        rc, _stderr = _run_ffmpeg_with_idle_timeout(
            cmd, timeout_s=_outro_timeout, idle_timeout_s=90, label="OutroBlur"
        )
        if rc != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            import shutil
            shutil.copy2(input_path, output_path)

    def _add_cta_text_reliable(self, input_path: str, output_path: str, text: str, duration: float, custom_font: Optional[str] = None):
        # ط´ط±ظٹط· ط£ط³ظˆط¯ ظپظٹ ط§ظ„ظ…ظ†طھطµظپ ظ…ط¹ ظ†طµ + طµظˆط±ط© ط§ظ„طھط·ط¨ظٹظ‚ - ط­ط¬ظ… ظ…ظ†ط§ط³ط¨ ظ„ظ„ظ…ط­طھظˆظ‰
        text_start = max(0.0, duration - 2.5)
        try:
            vw, vh = self._get_video_dimensions(input_path)
        except Exception:
            vw, vh = (1080, 1920)
        
        # ًں†• طھطµط؛ظٹط± ط§ظ„ط´ط±ظٹط· ظ„ظٹظ†ط§ط³ط¨ ط§ظ„ظ…ط­طھظˆظ‰ ظپظ‚ط·
        bar_h = max(180, min(300, int(vh * 0.18)))  # طھظƒط¨ظٹط± ط¶ط®ظ… ظ„ظ„ط§ط±طھظپط§ط¹ (ظƒط§ظ† 0.14)
        side_margin = 60  # ظ‡ط§ظ…ط´ ظ…ظ† ط§ظ„ط¬ظˆط§ظ†ط¨ ظ„ط¹ط¯ظ… ط§ظ„ط§ظ„طھطµط§ظ‚ ط¨ط§ظ„ط­ظˆط§ظپ
        margin = 20
        
        # ط§ط®طھظٹط§ط± ط§ظ„ط®ط· ظˆط¹ط±ط¶ ط§ظ„ظ†طµ
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        fontfile = self._get_best_font(text, custom_font)
        text_file = self.temp_dir / f"cta_{uuid.uuid4().hex}.txt"
        # ط§ظ„ط¨ط­ط« ط¹ظ† طµظˆط±ط© ط§ظ„طھط·ط¨ظٹظ‚
        def _find_app_photo() -> Optional[str]:
            try:
                candidates: list[Path] = []
                env_dir = os.getenv("APP_PHOTO_DIR") or os.getenv("APP_PHOTO_PATH")
                if env_dir:
                    candidates.append(Path(env_dir))
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    candidates.append(repo_root / "app-photo")
                except Exception:
                    pass
                candidates.append(Path("app-photo"))
                for base in candidates:
                    if not base.exists():
                        continue
                    for ext in ("*.png", "*.jpg", "*.jpeg"):
                        files = list(base.glob(ext))
                        if files:
                            return str(files[0])
                return None
            except Exception:
                return None
        app_photo = _find_app_photo()
        logo_w = 0
        logo_h = 0
        if app_photo and os.path.exists(app_photo):
            try:
                from PIL import Image
                with Image.open(app_photo) as im:
                    logo_w, logo_h = im.size
            except Exception:
                app_photo = None
                logo_w = logo_h = 0
        # ط­ط³ط§ط¨ ظ…ط³ط§ط­ط© ط§ظ„ظ†طµ ط§ظ„ظپط¹ظ„ظٹط© ط¨ط¹ط¯ ظˆط¶ط¹ ط§ظ„ط´ط¹ط§ط± ظٹظ…ظٹظ†ط§ظ‹
        # ط§ظ„ط´ط¹ط§ط± ظ„ظ† ظٹظڈظƒظژط¨ظ‘ظژط± ط£ط¨ط¯ط§ظ‹طŒ ظپظ‚ط· ظٹظڈطµط؛ظ‘ظژط± ط¥ط°ط§ طھط¬ط§ظˆط² ط§ط±طھظپط§ط¹ ط§ظ„ط´ط±ظٹط·
        max_logo_h = bar_h - 2 * margin
        scale_factor = 1.0
        if logo_h > 0:
            scale_factor = min(1.0, max_logo_h / float(logo_h))
        logo_w_scaled = int(logo_w * scale_factor)
        text_area_w = vw - (margin + (logo_w_scaled if logo_w_scaled > 0 else 0) + margin + margin)
        # طھط¬ظ‡ظٹط² ط£ط³ط·ط± ط§ظ„ظ†طµ (wrap) ط¨ط§ظ„طھط±طھظٹط¨ ط§ظ„ظ…ظ†ط·ظ‚ظٹ ط«ظ… طھط´ظƒظٹظ„ ظƒظ„ ط³ط·ط± (RTL) ط¹ظ†ط¯ ط§ظ„ط­ط§ط¬ط©
        try:
            from PIL import ImageFont as _ImageFont, ImageDraw as _ImageDraw, Image as _Image

            def _shape_line(s: str) -> str:
                if is_ar and HAS_ARABIC_SUPPORT:
                    try:
                        return get_display(arabic_reshaper.reshape(s))
                    except Exception:
                        return s
                return s

            def _load_font(size: int):
                try:
                    return _ImageFont.truetype(fontfile, size)
                except Exception:
                    return _ImageFont.load_default()

            fnt = _load_font(75)  # طھظƒط¨ظٹط± ط¶ط®ظ… (ظƒط§ظ† 56)
            tmp_img = _Image.new("RGB", (max(1, text_area_w), max(1, bar_h)))
            tmp_draw = _ImageDraw.Draw(tmp_img)

            def _wrap_lines_logical(s: str, max_w: int, max_lines: int = 5):
                # Split by existing newlines first
                parts = (s or "").split('\n')
                all_lines = []
                for part in parts:
                    words = part.split()
                    if not words:
                        continue
                    cur = ""
                    for w in words:
                        test = (cur + " " + w).strip()
                        if tmp_draw.textlength(_shape_line(test), font=fnt) <= max_w:
                            cur = test
                        else:
                            if cur:
                                all_lines.append(cur)
                            cur = w
                    if cur:
                        all_lines.append(cur)
                return all_lines[:max_lines]

            logical_lines = _wrap_lines_logical(text, max(1, text_area_w), max_lines=5)
            display_lines = [_shape_line(ln) for ln in logical_lines]
        except Exception:
            display_lines = [text[:100]]

        # ظƒطھط§ط¨ط© ط§ظ„ظ†طµ ظ„ظ…ظ„ظپ (ظ„ط£ط؛ط±ط§ط¶ ط§ظ„طھطھط¨ط¹/ط§ظ„طھظˆط§ظپظ‚)
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write("\n".join(display_lines))
        except Exception:
            pass
        # ظ‡ط±ظˆط¨ ط§ظ„ظ…ط³ط§ط±ط§طھ
        def ffmpeg_escape_path(path_str):
            return str(path_str).replace("\\", "/").replace(":", "\\:")
        text_file_esc = ffmpeg_escape_path(text_file)
        font_esc = ffmpeg_escape_path(fontfile)
        filter_parts = []
        # ًں†• ط­ط³ط§ط¨ ط¹ط±ط¶ ط§ظ„ط´ط±ظٹط· ط¨ظ†ط§ط،ظ‹ ط¹ظ„ظ‰ ط§ظ„ظ…ط­طھظˆظ‰ ط§ظ„ظپط¹ظ„ظٹ (ظ†طµ + طµظˆط±ط©)
        from PIL import Image, ImageDraw, ImageFont
        
        # طھط­ظ…ظٹظ„ ط§ظ„ط®ط· ط£ظˆظ„ط§ظ‹ ظ„ط­ط³ط§ط¨ ط¹ط±ط¶ ط§ظ„ظ†طµ
        try:
            font = ImageFont.truetype(fontfile, 65)  # طھظƒط¨ظٹط± ط¶ط®ظ… (ظƒط§ظ† 48)
        except Exception:
            font = None
            for fp in ["C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/system/fonts/NotoNaskhArabic-Regular.ttf"]:
                try:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, 38)
                        break
                except Exception:
                    continue
            if font is None:
                font = ImageFont.load_default()
        
        # ط­ط³ط§ط¨ ط¹ط±ط¶ ط§ظ„ظ†طµ
        tmp_img = Image.new("RGB", (1000, 200))
        tmp_draw = ImageDraw.Draw(tmp_img)
        
        text_lines = (text or "").split('\n')
        max_text_width = 0
        for line in text_lines:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    shaped_line = get_display(arabic_reshaper.reshape(line))
                except:
                    shaped_line = line
            else:
                shaped_line = line
            line_width = tmp_draw.textlength(shaped_line, font=font)
            max_text_width = max(max_text_width, line_width)
        
        # ًں†• ط¹ط±ط¶ ط§ظ„ط´ط±ظٹط· = ط§ظ„ظ†طµ + ط§ظ„ط´ط¹ط§ط± + ظ‡ظˆط§ظ…ط´ ط¯ط§ط®ظ„ظٹط©
        icon_size = max(120, min(220, bar_h - 40))  # طھظƒط¨ظٹط± ط¶ط®ظ… ظ„ظ„ط£ظٹظ‚ظˆظ†ط©
        gap = 20  # ط§ظ„ظ…ط³ط§ظپط© ط¨ظٹظ† ط§ظ„ط£ظٹظ‚ظˆظ†ط© ظˆط§ظ„ظ†طµ
        padding_x = 30  # ظ‡ط§ظ…ط´ ط¯ط§ط®ظ„ظٹ ظ„ظ„ط´ط±ظٹط·
        
        bar_w = int(max_text_width + icon_size + gap + padding_x * 2)
        bar_w = max(300, min(bar_w, vw - side_margin * 2))  # ط­ط¯ ط£ظ‚طµظ‰ ظˆط£ط¯ظ†ظ‰ ظ„ظ„ط¹ط±ط¶
        
        # ط¥ظ†ط´ط§ط، طµظˆط±ط© ط§ظ„ط´ط±ظٹط· ط¨ط§ظ„ط­ط¬ظ… ط§ظ„ظ…ط­ط³ظˆط¨
        overlay_img = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, int(0.75 * 255)))
        draw = ImageDraw.Draw(overlay_img)
        
        # ط¥ط¯ط±ط§ط¬ ط§ظ„ط´ط¹ط§ط± ط­ط³ط¨ ط§طھط¬ط§ظ‡ ط§ظ„ظ„ط؛ط©:
        # ًں†• طھطµط­ظٹط­: ظ„ظ„ط¹ط±ط¨ظٹط© (RTL): ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹظ…ظٹظ†طŒ ظ„ظ„ط¥ظ†ط¬ظ„ظٹط²ظٹط© (LTR): ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹط³ط§ط±
        logo_img = None
        logo_w_final = 0
        logo_h_final = 0
        if app_photo and os.path.exists(app_photo) and logo_w > 0 and logo_h > 0:
            try:
                logo_img = Image.open(app_photo).convert("RGBA")
                # طھطµط؛ظٹط± ط§ظ„ط£ظٹظ‚ظˆظ†ط© ظ„طھظ†ط§ط³ط¨ ط§ظ„ط´ط±ظٹط·
                target_size = icon_size
                if logo_w > logo_h:
                    new_w = target_size
                    new_h = max(1, int(logo_h * target_size / logo_w))
                else:
                    new_h = target_size
                    new_w = max(1, int(logo_w * target_size / logo_h))
                logo_img = logo_img.resize((new_w, new_h), Image.LANCZOS)
                logo_w_final, logo_h_final = logo_img.size
                
                # ًں†• طھطµط­ظٹط­ ط§ظ„ظ…ظˆط¶ط¹:
                # - ظ„ظ„ط¹ط±ط¨ظٹط© (RTL): ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹظ…ظٹظ† ظ…ظ† ط§ظ„ط´ط±ظٹط·
                # - ظ„ظ„ط¥ظ†ط¬ظ„ظٹط²ظٹط© (LTR): ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹط³ط§ط± ظ…ظ† ط§ظ„ط´ط±ظٹط·
                ly = (bar_h - logo_h_final) // 2
                if is_ar:
                    lx = bar_w - logo_w_final - padding_x  # ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹظ…ظٹظ† ظ„ظ„ط¹ط±ط¨ظٹط©
                else:
                    lx = padding_x  # ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹط³ط§ط± ظ„ظ„ط¥ظ†ط¬ظ„ظٹط²ظٹط©
                overlay_img.alpha_composite(logo_img, (lx, ly))
            except Exception:
                logo_img = None
                logo_w_final = logo_h_final = 0
        # ًں†• طھط­ط¯ظٹط¯ ظ…ط³ط§ط­ط© ط§ظ„ظ†طµ ط­ط³ط¨ ط§طھط¬ط§ظ‡ ط§ظ„ظ„ط؛ط© ظˆظ…ظˆط¶ط¹ ط§ظ„ط´ط¹ط§ط±
        if is_ar and logo_w_final > 0:
            # ط§ظ„ط¹ط±ط¨ظٹط©: ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹظ…ظٹظ†طŒ ط§ظ„ظ†طµ ط¹ظ„ظ‰ ط§ظ„ظٹط³ط§ط±
            text_area_left = padding_x
            text_area_right = bar_w - logo_w_final - gap - padding_x
        elif logo_w_final > 0:
            # ط§ظ„ط¥ظ†ط¬ظ„ظٹط²ظٹط©: ط§ظ„ط´ط¹ط§ط± ط¹ظ„ظ‰ ط§ظ„ظٹط³ط§ط±طŒ ط§ظ„ظ†طµ ط¹ظ„ظ‰ ط§ظ„ظٹظ…ظٹظ†
            text_area_left = logo_w_final + gap + padding_x
            text_area_right = bar_w - padding_x
        else:
            # ظ„ط§ ظٹظˆط¬ط¯ ط´ط¹ط§ط±
            text_area_left = padding_x
            text_area_right = bar_w - padding_x
        text_area_w2 = max(1, int(text_area_right - text_area_left))

        # طھط¬ظ‡ظٹط² ط£ط³ط·ط± ط§ظ„ظ†طµ
        spacing = 2
        display_lines = []
        for line in (text or "").split('\n'):
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    display_lines.append(get_display(arabic_reshaper.reshape(line)))
                except:
                    display_lines.append(line)
            else:
                display_lines.append(line)
        
        total_h = 0
        line_heights = []
        for ln in display_lines[:3]:  # ط­ط¯ ط£ظ‚طµظ‰ 3 ط£ط³ط·ط±
            bb = draw.textbbox((0, 0), ln, font=font)
            lh = (bb[3] - bb[1]) if bb else 0
            line_heights.append(lh)
            total_h += lh
        total_h += spacing * max(0, len(line_heights) - 1)
        text_y = max(0, (bar_h - total_h) // 2)
        y = text_y
        for i, ln in enumerate(display_lines[:3]):
            lw = draw.textlength(ln, font=font)
            if is_ar:
                # ظ…ط­ط§ط°ط§ط© ظ„ظ„ظٹظ…ظٹظ† ظ„ظ„ظ†طµ ط§ظ„ط¹ط±ط¨ظٹ
                x = text_area_right - lw
            else:
                # ظ…ط­ط§ط°ط§ط© ظ„ظ„ظٹط³ط§ط± ظ„ظ„ظ†طµ ط§ظ„ط¥ظ†ط¬ظ„ظٹط²ظٹ
                x = text_area_left
            draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
            y += line_heights[i] + spacing
        # ط­ظپط¸ طµظˆط±ط© overlay ظ…ط¤ظ‚طھط§ظ‹
        overlay_path = self.temp_dir / f"cta_bar_{uuid.uuid4().hex}.png"
        overlay_img.save(overlay_path)
        try:
            ovl_esc = ffmpeg_escape_path(overlay_path)
            overlay_input = str(overlay_path)
            # ًں”§ ط§ط³طھط®ط¯ط§ظ… ط¥ط¹ط¯ط§ط¯ط§طھ ظˆط³ظٹط· ظ…ط­ط³ظ‘ظ†ط©
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            # ط¥ط¶ط§ظپط© طھط£ط«ظٹط±ظٹ fade-in ظˆ fade-out ط¹ظ„ظ‰ ط·ط¨ظ‚ط© ط§ظ„ط¯ط¹ظˆط©
            video_duration = self._get_video_duration(input_path)
            if not video_duration:
                video_duration = float(duration or 0.0)
            show_start = max(0.0, float(video_duration) - 2.6)
            show_dur = max(1.2, min(2.6, float(video_duration) - show_start))
            show_end = float(show_start) + float(show_dur)
            # âœ… طھط³ط¬ظٹظ„ طھظپطµظٹظ„ظٹ ظ„ظ„طھظˆظ‚ظٹطھط§طھ ط§ظ„ظ…ط­ط³ظˆط¨ط©
            logger.info(f"[CTA_reliable] video_duration={video_duration:.2f}s show_start={show_start:.2f}s show_end={show_end:.2f}s show_dur={show_dur:.2f}s")
            try:
                seed_s = f"{input_path}::{output_path}::{text}"
                r = int(hashlib.md5(seed_s.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
                fade_in_dur = 0.25 + (r % 35) / 100.0
                fade_out_dur = 0.40 + ((r // 35) % 45) / 100.0
                in_kinds = ["fade", "slideup", "slidedown", "pop"]
                out_kinds = ["fade", "slideup", "slidedown", "pop"]
                in_kind = in_kinds[r % len(in_kinds)]
                out_kind = out_kinds[(r // 11) % len(out_kinds)]
            except Exception:
                fade_in_dur = 0.35
                fade_out_dur = 0.65
                in_kind = "fade"
                out_kind = "fade"
            fade_in_dur = max(0.18, min(fade_in_dur, 0.7))
            fade_out_dur = max(0.22, min(fade_out_dur, 0.9))
            if (fade_in_dur + fade_out_dur) > (show_dur - 0.05):
                fade_in_dur = max(0.18, show_dur * 0.35)
                fade_out_dur = max(0.22, show_dur * 0.45)
            fade_out_start_rel = max(0.0, float(show_dur) - float(fade_out_dur))
            fade_out_start_abs = float(show_start) + float(fade_out_start_rel)

            x_expr = "(W-w)/2"
            y_base = "(H-h)/2"
            slide_px = max(80, min(220, int(vh * 0.10)))
            intro_end_abs = float(show_start) + float(fade_in_dur)

            try:
                logger.info(
                    f"[CTA_fx] in={in_kind} out={out_kind} "
                    f"fade_in={fade_in_dur:.2f}s fade_out={fade_out_dur:.2f}s "
                    f"slide_px={int(slide_px)}"
                )
            except Exception:
                pass

            # ط­ط±ظƒط© y ط¨ط­ط³ط¨ ظ†ظˆط¹ ط§ظ„ط¯ط®ظˆظ„/ط§ظ„ط®ط±ظˆط¬
            y_expr = y_base
            if in_kind in {"slideup", "slidedown"} or out_kind in {"slideup", "slidedown"}:
                intro_sign = 1 if in_kind == "slideup" else (-1 if in_kind == "slidedown" else 0)
                out_sign = 1 if out_kind == "slidedown" else (-1 if out_kind == "slideup" else 0)
                intro_expr = y_base
                if intro_sign != 0:
                    intro_expr = f"if(lt(t,{intro_end_abs}),{y_base}+({intro_end_abs}-t)/{fade_in_dur}*{intro_sign*slide_px},{y_base})"
                out_expr = y_base
                if out_sign != 0:
                    out_expr = f"{y_base}+(t-{fade_out_start_abs})/{fade_out_dur}*{out_sign*slide_px}"
                # ط§ظ„ط®ط±ظˆط¬ ظ„ظ‡ ط£ظˆظ„ظˆظٹط© ظپظٹ ط§ظ„ظ†ظ‡ط§ظٹط©
                y_expr = f"if(gte(t,{fade_out_start_abs}),{out_expr},{intro_expr})"

            # طھط£ط«ظٹط± pop (طھظƒط¨ظٹط±/طھطµط؛ظٹط± ط®ظپظٹظپ) ط¹ظ„ظ‰ ط·ط¨ظ‚ط© ط§ظ„ط¯ط¹ظˆط©
            overlay_scale_filter = ""
            if in_kind == "pop" or out_kind == "pop":
                intro_scale = "1"
                out_scale = "1"
                if in_kind == "pop":
                    intro_scale = f"if(lt(t,{fade_in_dur}),0.85+0.15*(t/{fade_in_dur}),1)"
                if out_kind == "pop":
                    out_scale = f"if(gte(t,{fade_out_start_rel}),1-0.20*min(1,(t-{fade_out_start_rel})/{fade_out_dur}),1)"
                scale_expr = f"({intro_scale})*({out_scale})"
                # IMPORTANT: commas inside expressions must be escaped for ffmpeg filtergraph parsing
                scale_expr_esc = str(scale_expr).replace(",", "\\,")
                overlay_scale_filter = f",scale=iw*{scale_expr_esc}:ih*{scale_expr_esc}:eval=frame"

            enable_end = float(show_end) + 0.05
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-loop", "1", "-i", overlay_input,
                # Quote y expression because it contains commas (if/min) which otherwise break filter parsing
                "-filter_complex", f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in_dur}:alpha=1,fade=t=out:st={fade_out_start_rel}:d={fade_out_dur}:alpha=1{overlay_scale_filter},setpts=PTS-STARTPTS+{show_start}/TB[ovl];[0:v][ovl]overlay=x={x_expr}:y='{y_expr}':shortest=1:enable='between(t,{show_start},{enable_end})'[vout]",
                "-map", "[vout]",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-threads", str(ff_threads),
                "-vsync", "cfr",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "384k",
                "-ar", "48000",
                "-movflags", "+faststart",
                str(output_path)
            ]
            _cta_timeout = self._resolve_ffmpeg_timeout(
                input_path, "FFMPEG_CTA_TIMEOUT_SECONDS", 300, 600, 8.0, 8.0, extra_seconds=90,
            )
            _cta_idle = min(120, max(45, _cta_timeout // 4))
            rc, _stderr = _run_ffmpeg_with_idle_timeout(
                cmd, timeout_s=_cta_timeout, idle_timeout_s=_cta_idle, label="CTAReliable"
            )
            if rc != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError(_stderr[-2500:] if _stderr else "CTA Reliable overlay failed")
        finally:
            try:
                if text_file.exists():
                    text_file.unlink()
            except Exception:
                pass
            try:
                if overlay_path.exists():
                    overlay_path.unlink()
            except Exception:
                pass
    def _add_cta_text_via_image_overlay(self, input_path: str, output_path: str, text: str, text_start: float, app_photo: Optional[str], custom_font: Optional[str] = None):
        from PIL import Image, ImageDraw, ImageFont
        vw, vh = self._get_video_dimensions(input_path)
        if not vw or not vh:
            vw, vh = 1080, 1920
        
        # 1. Prepare Font and Size settings first
        # Bigger font as requested (V3 - Even Bigger)
        target_font_size = 64  # Increased from 56
        font_path = self._get_best_font(text, custom_font)
        try:
            font = ImageFont.truetype(font_path, target_font_size)
        except Exception:
            font = ImageFont.load_default()

        # 2. Text Logic Handling (RTL/Shaping)
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        def _shape_line(s: str) -> str:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    return get_display(arabic_reshaper.reshape(s))
                except Exception:
                    return s
            return s

        # 3. Logo Handling - Prepare logo to measure it
        logo_img = None
        logo_w = logo_h = 0
        scaling_factor_logo = 1.5 # Increased again
        padding_x = 40 # Increased padding
        padding_y = 30 
        
        # Target logo height related to font size but constrained
        target_logo_h = 180 # Increased from 160
        
        if app_photo and os.path.exists(app_photo):
            try:
                temp_logo = Image.open(app_photo).convert("RGBA")
                orig_w, orig_h = temp_logo.size
                # Resize keeping aspect ratio
                ratio = min(target_logo_h / orig_h, target_logo_h / orig_w)
                new_w = max(1, int(orig_w * ratio))
                new_h = max(1, int(orig_h * ratio))
                logo_img = temp_logo.resize((new_w, new_h), Image.LANCZOS)
                logo_w, logo_h = logo_img.size
            except Exception:
                logo_img = None
        
        # 4. Calculate Dynamic Box Dimensions
        # Initial max width constraints
        safe_margin = 40
        max_box_w = min(950, vw - safe_margin * 2) 
        
        # Helper to wrap text
        dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        
        # Available width for text = Total Max Width - Padding - Logo Width - Padding - Padding
        text_avail_w = max_box_w - (padding_x * 2) - (logo_w + padding_x if logo_w > 0 else 0)
        
        def _wrap_lines_logical(s: str, max_w: int, max_lines: int = 4):
            parts = (s or "").split('\n')
            all_lines = []
            for part in parts:
                words = part.split()
                if not words: continue
                cur = ""
                for w in words:
                    test = (cur + " " + w).strip()
                    if dummy_draw.textlength(_shape_line(test), font=font) <= max_w:
                        cur = test
                    else:
                        if cur: all_lines.append(cur)
                        cur = w
                if cur: all_lines.append(cur)
            return all_lines[:max_lines]

        logical_lines = _wrap_lines_logical(text, max(1, text_avail_w), max_lines=4)
        display_lines = [_shape_line(ln) for ln in logical_lines]
        
        # Measure total text block size
        text_total_h = 0
        text_max_w = 0
        line_heights = []
        spacing = 8 
        
        for ln in display_lines:
            bb = dummy_draw.textbbox((0, 0), ln, font=font)
            lh = (bb[3] - bb[1]) if bb else font.size 
            line_heights.append(lh)
            text_total_h += lh
            tw = dummy_draw.textlength(ln, font=font)
            text_max_w = max(text_max_w, tw)
            
        if len(display_lines) > 1:
            text_total_h += spacing * (len(display_lines) - 1)
            
        # 5. Finalize Box Dimensions based on content
        # Total Width = Padding + Text Width + Padding + Logo + Padding
        # But we usually want a fixed width bar or at least wide enough.
        # Let's keep the width reasonably wide for aesthetics, or tighten it?
        # User complained about "big black bar", usually height is the issue.
        # Let's make width dynamic too but with a minimum used width.
        
        content_w = padding_x + text_max_w + padding_x + (logo_w + padding_x if logo_w > 0 else 0)
        final_box_w = max(int(content_w), 600) # Minimum width 600px for good look
        final_box_w = min(final_box_w, max_box_w) # Cap at max
        
        # Total Height = Max(Text Height, Logo Height) + padding_y * 2
        content_h = max(text_total_h, logo_h)
        final_box_h = int(content_h + padding_y * 2)
        
        # 6. Draw Content
        img = Image.new("RGBA", (final_box_w, final_box_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Background with rounded corners aesthetic or just rect? 
        # Making it slightly transparent black
        draw.rectangle([(0, 0), (final_box_w, final_box_h)], fill=(0, 0, 0, int(0.85 * 255))) # 85% opacity
        
        rtl = bool(is_ar)
        
        # Position Logo
        logo_x = 0
        logo_y = (final_box_h - logo_h) // 2
        
        text_start_x = 0
        
        if logo_img:
            if rtl:
                # [ Text ...  Logo ]
                logo_x = final_box_w - padding_x - logo_w
                img.alpha_composite(logo_img, (logo_x, logo_y))
                text_right_limit = logo_x - padding_x
                text_left_limit = padding_x
            else:
                # [ Logo ... Text ]
                logo_x = padding_x
                img.alpha_composite(logo_img, (logo_x, logo_y))
                text_left_limit = logo_x + logo_w + padding_x
                text_right_limit = final_box_w - padding_x
        else:
             text_left_limit = padding_x
             text_right_limit = final_box_w - padding_x

        # Position Text (Vertically Centered)
        text_y = (final_box_h - text_total_h) // 2
        
        # Draw Text Lines
        for i, ln in enumerate(display_lines):
            lw = draw.textlength(ln, font=font)
            
            # Horizontal align
            if is_ar:
                # Right align within available space
                # x = text_right_limit - lw
                # But if we want it centered in the remaining space:
               avail_space = text_right_limit - text_left_limit
               x = text_left_limit + (avail_space - lw) # Right align
            else:
                # Left align
                x = text_left_limit
            
            draw.text((x, text_y), ln, font=font, fill=(255, 255, 255, 255))
            text_y += line_heights[i] + spacing

        # Save
        overlay_path = self.temp_dir / f"cta_overlay_{uuid.uuid4().hex}.png"
        img.save(overlay_path)
        
        # Get video duration for fade out timing
        video_duration = self._get_video_duration(input_path)
        fade_in_dur = 0.5
        fade_out_dur = 0.8 # Making it quicker to leave
        
        # Calculate fade out start time (end of video - fade duration - 0.2s buffer)
        fade_out_start = max(text_start + 1.0, video_duration - fade_out_dur - 0.2)
        
        def ffmpeg_escape_path(path_str):
            return str(path_str).replace("\\", "/").replace(":", "\\:")
        ovl_esc = ffmpeg_escape_path(overlay_path)
        overlay_input = str(overlay_path)
        ff_threads, preset, crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-loop", "1", "-i", overlay_input,
            "-filter_complex", f"[1:v]format=rgba,setpts=PTS-STARTPTS,fade=t=in:st={text_start}:d={fade_in_dur}:alpha=1,fade=t=out:st={fade_out_start}:d={fade_out_dur}:alpha=1[ovl];[0:v][ovl]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1:enable='between(t,{text_start},{fade_out_start + fade_out_dur})'[vout]",
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-threads", str(ff_threads),
            "-c:a", "copy",
            str(output_path)
        ]
        
        _cta2_timeout = self._resolve_ffmpeg_timeout(
            input_path, "FFMPEG_CTA_TIMEOUT_SECONDS", 300, 600, 8.0, 8.0, extra_seconds=90,
        )
        _cta2_idle = min(120, max(45, _cta2_timeout // 4))
        rc, _stderr = _run_ffmpeg_with_idle_timeout(
            cmd, timeout_s=_cta2_timeout, idle_timeout_s=_cta2_idle, label="CTAImageOverlay"
        )
        try:
            if overlay_path.exists():
                overlay_path.unlink()
        except Exception:
            pass
        if rc != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(_stderr[-2500:] if _stderr else "CTA image overlay failed")

    def _flip_video(self, input_path: str, output_path: Path) -> None:
        """ظ‚ظ„ط¨ ط§ظ„ظپظٹط¯ظٹظˆ ط£ظپظ‚ظٹط§ظ‹ (Mirror)"""
        try:
            ff_threads, preset, crf = self._shorts_x264_settings()
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            
            cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "hflip",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-threads", str(ff_threads),
                "-c:a", "copy", # ط§ظ„ط­ظپط§ط¸ ط¹ظ„ظ‰ ط§ظ„طµظˆطھ ظƒظ…ط§ ظ‡ظˆ
                str(output_path)
            ]
            
            timeout_s = self._resolve_ffmpeg_timeout(
                input_path, "FFMPEG_FLIP_TIMEOUT_SECONDS", 180, 300, 6.0, 5.0, extra_seconds=60,
            )
            rc, stderr_text = _run_ffmpeg_with_idle_timeout(
                cmd, timeout_s=timeout_s, idle_timeout_s=90, label="Flip"
            )
            if rc != 0 or not os.path.exists(output_path):
                raise RuntimeError(f"FFmpeg hflip failed: {stderr_text[-500:]}")
                
        except Exception as e:
            logger.error(f"Error flipping video: {e}")
            raise

