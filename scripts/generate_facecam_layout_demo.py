import os
import sys
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.config import load_config
from src.agent.ffmpeg_utils import ffmpeg_bin
from src.agent.renderer import render_with_pip


def _run(cmd):
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    demo_dir = PROJECT_ROOT / ".output" / "facecam_demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    main_video = demo_dir / "main_demo.mp4"
    facecam_video = demo_dir / "facecam_demo.mp4"

    _run([
        ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "6",
        "-shortest",
        "-pix_fmt", "yuv420p",
        str(main_video),
    ])

    _run([
        ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30",
        "-t", "6",
        "-pix_fmt", "yuv420p",
        str(facecam_video),
    ])

    cfg = load_config(force_reload=True)
    cfg.BACKGROUND_REMOVAL_ENABLED = False
    cfg.BACKGROUND_DIR = str(demo_dir)
    cfg.REACTIONS_DIR = str(demo_dir)
    cfg.TEMP_DIR = str(demo_dir / "temp")
    Path(cfg.TEMP_DIR).mkdir(parents=True, exist_ok=True)

    output = render_with_pip(
        cfg=cfg,
        input_path=str(main_video),
        out_dir=str(demo_dir),
        facecam_enabled=True,
        facecam_path=str(facecam_video),
        facecam_layout="small_circle_top_right",
        facecam_position="top_right",
        facecam_shape="circle",
        facecam_scale=0.18,
    )
    print(f"Demo video created: {output}")


if __name__ == "__main__":
    main()