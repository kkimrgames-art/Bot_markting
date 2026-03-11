
import os
import sys
import platform
import zipfile
import tarfile
import urllib.request
import shutil
import logging
from pathlib import Path

from src.agent.config import get_project_root

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FFmpegInstaller")

# Configuration
PROJECT_ROOT = Path(get_project_root())
TOOLS_DIR = PROJECT_ROOT / ".tools"
FFMPEG_DIR = TOOLS_DIR / "ffmpeg"

WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# Linux amd64 static build (John Van Sickle)
LINUX_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

def is_ffmpeg_installed():
    """Check if ffmpeg is already in .tools/ffmpeg"""
    candidates = [
        FFMPEG_DIR,
        Path.cwd() / ".tools" / "ffmpeg",
        PROJECT_ROOT.parent / ".tools" / "ffmpeg",
    ]
    seen = set()
    for base in candidates:
        norm = base.resolve() if base.exists() else base
        key = str(norm)
        if key in seen:
            continue
        seen.add(key)
        if os.name == 'nt':
            ffmpeg_exe = list(base.glob("**/ffmpeg.exe"))
            if ffmpeg_exe:
                logger.info(f"✅ FFmpeg found in {base}")
                return True
        else:
            ffmpeg_bin = list(base.glob("**/ffmpeg"))
            if ffmpeg_bin:
                logger.info(f"✅ FFmpeg found in {base}")
                return True
    return False

def download_file(url, target_path):
    logger.info(f"Downloading {url} to {target_path}...")
    def report(count, block_size, total_size):
        percent = int(count * block_size * 100 / total_size)
        if count % 100 == 0:
            sys.stdout.write(f"\rDownloading: {percent}%")
            sys.stdout.flush()
    
    urllib.request.urlretrieve(url, target_path, reporthook=report)
    print("\nDownload complete.")

def install():
    if is_ffmpeg_installed():
        logger.info("✅ FFmpeg is already installed in .tools directory.")
        return True

    TOOLS_DIR.mkdir(exist_ok=True)
    FFMPEG_DIR.mkdir(exist_ok=True)

    system = platform.system().lower()
    
    if system == "windows":
        url = WIN_URL
        archive_path = TOOLS_DIR / "ffmpeg.zip"
    elif system == "linux":
        url = LINUX_URL
        archive_path = TOOLS_DIR / "ffmpeg.tar.xz"
    else:
        logger.error(f"Unsupported system: {system}")
        return False

    try:
        if not archive_path.exists():
            download_file(url, archive_path)
        
        logger.info(f"Extracting {archive_path} to {FFMPEG_DIR}...")
        
        if archive_path.suffix == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                zip_ref.extractall(FFMPEG_DIR)
        else:
            with tarfile.open(archive_path, "r:xz") as tar_ref:
                tar_ref.extractall(FFMPEG_DIR)
        
        # Cleanup
        # archive_path.unlink()
        
        # Set executive permissions on Linux
        if system == "linux":
            for binary in FFMPEG_DIR.glob("**/ffmpeg*"):
                if binary.is_file():
                    os.chmod(binary, 0o755)
                    logger.info(f"Set +x permission for {binary}")

        logger.info("✨ FFmpeg installation complete!")
        return True
    except Exception as e:
        logger.error(f"Installation failed: {e}")
        return False

if __name__ == "__main__":
    install()
