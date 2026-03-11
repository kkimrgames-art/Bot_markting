import subprocess
import logging
import os
from typing import List, Optional

from .config import get_project_root

logger = logging.getLogger(__name__)


_TOOLS_FFMPEG_DIR = os.path.join(get_project_root(), ".tools", "ffmpeg")


def _candidate_ffmpeg_dirs() -> List[str]:
    candidates = [
        _TOOLS_FFMPEG_DIR,
        os.path.abspath(os.path.join(get_project_root(), os.pardir, ".tools", "ffmpeg")),
        os.path.abspath(os.path.join(os.getcwd(), ".tools", "ffmpeg")),
    ]
    seen = set()
    ordered = []
    for path in candidates:
        norm = os.path.normpath(path)
        if norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    return ordered

def ffmpeg_bin() -> str:
    """Resolve ffmpeg binary path from env or local .tools directory."""
    # Priority for Termux standard path
    termux_bin = "/data/data/com.termux/files/usr/bin/ffmpeg"
    if os.path.exists(termux_bin):
        return termux_bin
    
    def _verify_bin(p: str) -> bool:
        if not p: return False
        try:
            import subprocess
            # Use shell=False for stability, check version quickly
            subprocess.run([p, "-version"], capture_output=True, text=True, timeout=2, check=True)
            return True
        except Exception:
            return False

    # Check for 'ffmpeg' is globally available via shutil
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if _verify_bin(sys_ffmpeg):
        return sys_ffmpeg
    
    envp = os.getenv("FFMPEG_BIN")
    if _verify_bin(envp):
        return envp
    
    # Check canonical and legacy .tools directories
    for base in _candidate_ffmpeg_dirs():
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                if os.name == 'nt' and "ffmpeg.exe" in files:
                    fpath = os.path.join(root, "ffmpeg.exe")
                    if _verify_bin(fpath):
                        return fpath
                if "ffmpeg" in files:
                    fpath = os.path.join(root, "ffmpeg")
                    if _verify_bin(fpath):
                        return fpath
    
    return None

def ffprobe_bin() -> str:
    """Resolve ffprobe binary path similarly to ffmpeg_bin."""
    # Special case for Termux
    if os.path.exists("/data/data/com.termux/files/usr/bin/ffprobe"):
        return "/data/data/com.termux/files/usr/bin/ffprobe"
        
    def _verify_bin(p: str) -> bool:
        if not p: return False
        try:
            import subprocess
            subprocess.run([p, "-version"], capture_output=True, text=True, timeout=2, check=True)
            return True
        except Exception:
            return False

    import shutil
    sys_ffprobe = shutil.which("ffprobe")
    if _verify_bin(sys_ffprobe):
        return sys_ffprobe

    envp = os.getenv("FFPROBE_BIN")
    if _verify_bin(envp):
        return envp

    for base in _candidate_ffmpeg_dirs():
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                if os.name == 'nt' and "ffprobe.exe" in files:
                    fpath = os.path.join(root, "ffprobe.exe")
                    if _verify_bin(fpath):
                        return fpath
                if "ffprobe" in files:
                    fpath = os.path.join(root, "ffprobe")
                    if _verify_bin(fpath):
                        return fpath

    return None

def run_ffmpeg_command(cmd: List[str], timeout: int = 300) -> bool:
    """
    Run an FFmpeg command with proper error handling and logging.
    
    Args:
        cmd: List of command arguments
        timeout: Timeout in seconds
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Determine timeout: env overrides default unless an explicit timeout is passed by caller
        try:
            env_timeout = int(os.getenv("FFMPEG_TIMEOUT", "0") or "0")
        except Exception:
            env_timeout = 0
        effective_timeout = timeout if timeout and timeout > 0 else 300
        if env_timeout > 0:
            effective_timeout = env_timeout
        # Rewrite binary path if needed
        if cmd:
            head = os.path.basename(cmd[0]).lower()
            if head in ("ffmpeg", "ffmpeg.exe"):
                cmd[0] = ffmpeg_bin()
            elif head in ("ffprobe", "ffprobe.exe"):
                cmd[0] = ffprobe_bin()

        try:
            env_threads = int(float((os.getenv("FFMPEG_THREADS", "0") or "0").strip() or "0"))
        except Exception:
            env_threads = 0

        if env_threads > 0 and "-threads" not in cmd:
            try:
                vcodec_idx = -1
                for i in range(len(cmd) - 1):
                    if cmd[i] == "-c:v" and (cmd[i + 1] or "").lower() == "libx264":
                        vcodec_idx = i
                        break
                if vcodec_idx != -1:
                    insert_at = len(cmd) - 1
                    if "-crf" in cmd:
                        try:
                            crf_i = cmd.index("-crf")
                            insert_at = min(len(cmd) - 1, crf_i + 2)
                        except Exception:
                            insert_at = len(cmd) - 1
                    cmd[insert_at:insert_at] = ["-threads", str(max(1, env_threads))]
            except Exception:
                pass

        logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
        
        # Run the command with timeout
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=effective_timeout,
            cwd=get_project_root()
        )
        
        if result.returncode == 0:
            logger.info("FFmpeg command completed successfully")
            return True
        else:
            logger.error(f"FFmpeg command failed with return code {result.returncode}")
            logger.error(f"Stderr: {result.stderr}")
            logger.error(f"Stdout: {result.stdout}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg command timed out after {effective_timeout} seconds")
        return False
    except FileNotFoundError:
        logger.error("FFmpeg binary not found. Please ensure FFmpeg is installed and in PATH.")
        return False
    except Exception as e:
        logger.error(f"FFmpeg command failed with exception: {e}")
        return False

def validate_input_file(file_path: str) -> bool:
    """
    Validate that an input file exists and is accessible.
    
    Args:
        file_path: Path to the file to validate
        
    Returns:
        True if valid, False otherwise
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"Input file does not exist: {file_path}")
            return False
        
        if not os.path.isfile(file_path):
            logger.error(f"Input path is not a file: {file_path}")
            return False
            
        if os.path.getsize(file_path) == 0:
            logger.error(f"Input file is empty: {file_path}")
            return False
            
        # Additional check: try to open the file
        with open(file_path, 'rb') as f:
            f.read(1)  # Try to read one byte
            
        return True
    except PermissionError:
        logger.error(f"Permission denied accessing file: {file_path}")
        return False
    except Exception as e:
        logger.error(f"Error validating input file {file_path}: {e}")
        return False

def validate_output_file(file_path: str) -> bool:
    """
    Validate that an output file was created successfully.
    
    Args:
        file_path: Path to the file to validate
        
    Returns:
        True if valid, False otherwise
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"Output file was not created: {file_path}")
            return False
        
        if os.path.getsize(file_path) == 0:
            logger.error(f"Output file is empty: {file_path}")
            return False
            
        # Additional check: try to open the file
        with open(file_path, 'rb') as f:
            f.read(1)  # Try to read one byte
            
        return True
    except PermissionError:
        logger.error(f"Permission denied accessing output file: {file_path}")
        return False
    except Exception as e:
        logger.error(f"Error validating output file {file_path}: {e}")
        return False


def convert_still_image_to_loop_video(
    input_path: str,
    output_path: str,
    *,
    duration_seconds: float = 4.0,
    fps: int = 30,
) -> bool:
    """Convert a still image into a short MP4 clip suitable for reuse as a looping video input."""
    try:
        if not validate_input_file(input_path):
            return False

        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        safe_duration = max(1.0, float(duration_seconds or 4.0))
        safe_fps = max(1, int(fps or 30))
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            input_path,
            "-t",
            f"{safe_duration:g}",
            "-vf",
            f"fps={safe_fps},format=yuv420p",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output_path,
        ]
        if not run_ffmpeg_command(cmd, timeout=max(60, int(safe_duration * 20))):
            return False
        return validate_output_file(output_path)
    except Exception as e:
        logger.error(f"Failed to convert still image to loop video: {e}")
        return False

def get_file_info(file_path: str) -> Optional[dict]:
    """
    Get media file information using ffprobe.
    
    Args:
        file_path: Path to the media file
        
    Returns:
        Dictionary with file information or None if failed
    """
    if not validate_input_file(file_path):
        return None

    try:
        cmd = [
            ffprobe_bin(), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            import json
            return json.loads(result.stdout)
        else:
            logger.warning(f"Failed to get file info for {file_path}")
            return None

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout getting file info for {file_path}")
        return None
    except Exception as e:
        logger.warning(f"Error getting file info: {e}")
        return None


def get_video_stream_summary(file_path: str) -> Optional[dict]:
    if not validate_input_file(file_path):
        return None
    try:
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,width,height,bit_rate,r_frame_rate,avg_frame_rate",
            "-of", "json",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        import json
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0] or {}

        def _parse_rate(val: Optional[str]) -> Optional[float]:
            if not val:
                return None
            try:
                v = str(val)
                if "/" in v:
                    a, b = v.split("/", 1)
                    a_f = float(a)
                    b_f = float(b)
                    if b_f == 0:
                        return None
                    return a_f / b_f
                return float(v)
            except Exception:
                return None

        out = {
            "codec": s.get("codec_name"),
            "width": int(s.get("width") or 0) if str(s.get("width") or "").isdigit() else s.get("width"),
            "height": int(s.get("height") or 0) if str(s.get("height") or "").isdigit() else s.get("height"),
            "bit_rate": int(s.get("bit_rate") or 0) if str(s.get("bit_rate") or "").isdigit() else s.get("bit_rate"),
            "fps": _parse_rate(s.get("avg_frame_rate") or s.get("r_frame_rate")),
        }
        return out
    except Exception:
        return None

def get_container_bitrate(file_path: str) -> Optional[int]:
    if not validate_input_file(file_path):
        return None
    try:
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-show_entries", "format=duration,size,bit_rate",
            "-of", "json",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        import json
        data = json.loads(result.stdout or "{}")
        fmt = data.get("format") or {}
        try:
            size = int(fmt.get("size") or 0)
        except Exception:
            size = 0
        try:
            duration = float(fmt.get("duration") or 0.0)
        except Exception:
            duration = 0.0
        try:
            br = int(fmt.get("bit_rate") or 0)
        except Exception:
            br = 0
        if br and br > 0:
            return br
        if size > 0 and duration > 0:
            return int((size * 8) / duration)
        return None
    except Exception:
        return None
