#!/usr/bin/env python3
"""
Border Detection v6 — Longest Content Run
==========================================
ROOT CAUSE of all previous failures:
  Videos have content EMBEDDED inside borders (thumbnails, previews, text boxes).
  Previous algorithms saw this embedded content and stopped prematurely.

v6 SOLUTION:
  Instead of walking from the edge and stopping at first content,
  v6 scans the ENTIRE frame and finds the LONGEST continuous run of
  real content. Everything above/below that run is border.

  This correctly handles:
  - [border 85px][thumbnail 8px][border 75px][REAL CONTENT 300px][border 82px]
  → detects: top=168px, bottom=82px (the thumbnail is part of the border!)
"""
import subprocess, json, os, sys, tempfile, logging, shutil
import numpy as np
from PIL import Image, ImageDraw
from typing import Tuple, Dict, Optional, List
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


@dataclass
class BorderInfo:
    detected: bool
    thickness: int
    color: Tuple[int, int, int]
    is_black: bool


@dataclass 
class VideoBorders:
    top: BorderInfo
    bottom: BorderInfo
    left: BorderInfo
    right: BorderInfo
    width: int
    height: int

    def has_any_border(self) -> bool:
        return any(b.detected for b in [self.top, self.bottom, self.left, self.right])

    def content_box(self) -> Tuple[int, int, int, int]:
        x = self.left.thickness
        y = self.top.thickness
        w = self.width - self.left.thickness - self.right.thickness
        h = self.height - self.top.thickness - self.bottom.thickness
        return (x, y, max(1, w), max(1, h))


def extract_frames(video_path: str, num_frames: int = 7) -> List[np.ndarray]:
    tmpdir = tempfile.mkdtemp(prefix="border_frames_")
    probe = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path
    ], capture_output=True, text=True, timeout=15)
    duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 10))

    frames = []
    for i in range(num_frames):
        t = duration * (i + 0.5) / num_frames
        frame_path = os.path.join(tmpdir, f"frame_{i:02d}.png")
        try:
            subprocess.run([
                "ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path,
                "-frames:v", "1", "-q:v", "2", frame_path
            ], capture_output=True, timeout=10)
            if os.path.exists(frame_path) and os.path.getsize(frame_path) > 100:
                img = Image.open(frame_path)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                frames.append(np.array(img))
        except Exception:
            pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    return frames


def _is_content_row(row: np.ndarray, std_threshold: float = 35.0) -> bool:
    """A row is 'content' if it has high pixel variance (real video, not uniform border)."""
    std = row.astype(float).std(axis=0).mean()
    return std > std_threshold


def _is_content_col(col: np.ndarray, std_threshold: float = 35.0) -> bool:
    """A column is 'content' if it has high pixel variance."""
    std = col.astype(float).std(axis=0).mean()
    return std > std_threshold


def detect_borders_v6(video_path: str, num_frames: int = 7) -> VideoBorders:
    """v6: Find the longest continuous content run. Everything else is border."""
    probe = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path
    ], capture_output=True, text=True, timeout=15)
    streams = json.loads(probe.stdout).get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})
    width = int(vs.get("width", 1080))
    height = int(vs.get("height", 1920))

    frames = extract_frames(video_path, num_frames=num_frames)
    if not frames:
        return VideoBorders(
            top=BorderInfo(False, 0, (0,0,0), False),
            bottom=BorderInfo(False, 0, (0,0,0), False),
            left=BorderInfo(False, 0, (0,0,0), False),
            right=BorderInfo(False, 0, (0,0,0), False),
            width=width, height=height,
        )

    h, w = frames[0].shape[:2]
    frames = [f for f in frames if f.shape[:2] == (h, w)]

    # ─── VERTICAL BORDERS (top/bottom) ───
    # For each row, check if it's "content" across ALL frames
    # A row is content if its std > threshold in the MAJORITY of frames
    row_is_content = []
    for r in range(h):
        content_count = 0
        for frame in frames:
            row = frame[r, :, :]
            if _is_content_row(row, std_threshold=35.0):
                content_count += 1
        # Content if majority of frames say it's content
        row_is_content.append(content_count >= len(frames) * 0.5)

    # Find the LONGEST continuous run of content rows
    longest_start = 0
    longest_len = 0
    current_start = -1
    current_len = 0

    for r in range(h):
        if row_is_content[r]:
            if current_start < 0:
                current_start = r
            current_len += 1
        else:
            if current_len > longest_len:
                longest_len = current_len
                longest_start = current_start
            current_start = -1
            current_len = 0
    
    if current_len > longest_len:
        longest_len = current_len
        longest_start = current_start

    # Top border = everything before the longest content run
    top_thickness = longest_start if longest_start > 0 else 0
    bottom_thickness = (h - 1 - (longest_start + longest_len - 1)) if (longest_start + longest_len) < h else 0

    # Get border colors
    def _get_border_color(thickness: int, edge: str) -> Tuple[int, int, int]:
        if thickness <= 0:
            return (0, 0, 0)
        colors = []
        for frame in frames:
            if edge == "top":
                strip = frame[:thickness, :, :].astype(float)
            else:
                strip = frame[-thickness:, :, :].astype(float)
            colors.append(np.median(strip.reshape(-1, 3), axis=0))
        mean_color = np.mean(colors, axis=0)
        return tuple(int(c) for c in mean_color)

    top_color = _get_border_color(top_thickness, "top")
    bottom_color = _get_border_color(bottom_thickness, "bottom")

    top = BorderInfo(
        detected=top_thickness >= 3,
        thickness=top_thickness,
        color=top_color,
        is_black=all(c < 30 for c in top_color),
    )
    bottom = BorderInfo(
        detected=bottom_thickness >= 3,
        thickness=bottom_thickness,
        color=bottom_color,
        is_black=all(c < 30 for c in bottom_color),
    )

    # ─── HORIZONTAL BORDERS (left/right) ───
    # Only check within the content area (between top and bottom borders)
    content_top = top_thickness
    content_bottom = h - bottom_thickness
    
    col_is_content = []
    for c in range(w):
        content_count = 0
        for frame in frames:
            col = frame[content_top:content_bottom, c, :]
            if _is_content_col(col, std_threshold=35.0):
                content_count += 1
        col_is_content.append(content_count >= len(frames) * 0.5)

    # Find longest content run for columns
    longest_col_start = 0
    longest_col_len = 0
    current_start = -1
    current_len = 0

    for c in range(w):
        if col_is_content[c]:
            if current_start < 0:
                current_start = c
            current_len += 1
        else:
            if current_len > longest_col_len:
                longest_col_len = current_len
                longest_col_start = current_start
            current_start = -1
            current_len = 0
    
    if current_len > longest_col_len:
        longest_col_len = current_len
        longest_col_start = current_start

    left_thickness = longest_col_start if longest_col_start > 0 else 0
    right_thickness = (w - 1 - (longest_col_start + longest_col_len - 1)) if (longest_col_start + longest_col_len) < w else 0

    def _get_border_color_h(thickness: int, edge: str) -> Tuple[int, int, int]:
        if thickness <= 0:
            return (0, 0, 0)
        colors = []
        for frame in frames:
            if edge == "left":
                strip = frame[content_top:content_bottom, :thickness, :].astype(float)
            else:
                strip = frame[content_top:content_bottom, -thickness:, :].astype(float)
            colors.append(np.median(strip.reshape(-1, 3), axis=0))
        mean_color = np.mean(colors, axis=0)
        return tuple(int(c) for c in mean_color)

    left_color = _get_border_color_h(left_thickness, "left")
    right_color = _get_border_color_h(right_thickness, "right")

    left = BorderInfo(
        detected=left_thickness >= 3,
        thickness=left_thickness,
        color=left_color,
        is_black=all(c < 30 for c in left_color),
    )
    right = BorderInfo(
        detected=right_thickness >= 3,
        thickness=right_thickness,
        color=right_color,
        is_black=all(c < 30 for c in right_color),
    )

    borders = VideoBorders(
        top=top, bottom=bottom, left=left, right=right,
        width=w, height=h,
    )
    
    logger.info(f"📐 Content run: rows {longest_start}-{longest_start+longest_len-1} ({longest_len}px = {longest_len/h*100:.0f}%)")
    logger.info(f"  Top border: {top_thickness}px, Bottom border: {bottom_thickness}px")
    if longest_col_len > 0:
        logger.info(f"  Content cols: {longest_col_start}-{longest_col_start+longest_col_len-1} ({longest_col_len}px)")
        logger.info(f"  Left border: {left_thickness}px, Right border: {right_thickness}px")
    
    return borders


def remove_borders(
    video_path: str, output_path: str,
    borders: Optional[VideoBorders] = None,
    padding_color: Tuple[int, int, int] = (0, 0, 0),
) -> Dict:
    result = {"success": False, "borders_detected": None, "error": None}

    if borders is None:
        borders = detect_borders_v6(video_path)
    result["borders_detected"] = {
        "top": {"detected": borders.top.detected, "thickness": borders.top.thickness, "color": borders.top.color},
        "bottom": {"detected": borders.bottom.detected, "thickness": borders.bottom.thickness, "color": borders.bottom.color},
        "left": {"detected": borders.left.detected, "thickness": borders.left.thickness, "color": borders.left.color},
        "right": {"detected": borders.right.detected, "thickness": borders.right.thickness, "color": borders.right.color},
    }

    if not borders.has_any_border():
        shutil.copy2(video_path, output_path)
        result["success"] = True
        return result

    x, y, cw, ch = borders.content_box()
