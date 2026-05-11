import os
import subprocess
import logging
import random
from typing import Optional, Dict, Any
import re
import uuid
import hashlib

from .config import Config
from .separator import separate_audio, combine_audio_video
from .ffmpeg_utils import run_ffmpeg_command, validate_input_file, validate_output_file, ffmpeg_bin, ffprobe_bin

logger = logging.getLogger(__name__)

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _HAS_ARABIC_SUPPORT = True
except Exception:
    _HAS_ARABIC_SUPPORT = False

def _is_arabic_text(s: str) -> bool:
    return bool(re.search(r'[\u0600-\u06FF]', s or ""))

def _shape_text(s: str) -> str:
    if _HAS_ARABIC_SUPPORT and _is_arabic_text(s):
        try:
            return get_display(arabic_reshaper.reshape(s))
        except Exception:
            return s
    return s

def _ffmpeg_escape_path(p: str) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def _shorts_h264_level() -> str:
    lvl = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip()
    return lvl or "5.1"

def _pick_best_font(text: str, overlay_font: Optional[str], cfg: Config) -> Optional[str]:
    # First priority: custom font for this channel/session
    if overlay_font:
        # Normalize path to handle both forward and backward slashes
        overlay_font_norm = os.path.normpath(overlay_font)
        if os.path.exists(overlay_font_norm):
            return os.path.abspath(overlay_font_norm)
        # Also try with current directory if relative path
        if not os.path.isabs(overlay_font_norm):
            overlay_font_abs = os.path.abspath(overlay_font_norm)
            if os.path.exists(overlay_font_abs):
                return overlay_font_abs
    
    is_ar = _is_arabic_text(text)
    
    # Second priority: global fonts from settings
    if is_ar and getattr(cfg, "GLOBAL_FONT_AR", None):
        global_ar_norm = os.path.normpath(cfg.GLOBAL_FONT_AR)
        if os.path.exists(global_ar_norm):
            return os.path.abspath(global_ar_norm)
        # Also try with current directory if relative path
        if not os.path.isabs(global_ar_norm):
            global_ar_abs = os.path.abspath(global_ar_norm)
            if os.path.exists(global_ar_abs):
                return global_ar_abs
    
    if (not is_ar) and getattr(cfg, "GLOBAL_FONT_EN", None):
        global_en_norm = os.path.normpath(cfg.GLOBAL_FONT_EN)
        if os.path.exists(global_en_norm):
            return os.path.abspath(global_en_norm)
        # Also try with current directory if relative path
        if not os.path.isabs(global_en_norm):
            global_en_abs = os.path.abspath(global_en_norm)
            if os.path.exists(global_en_abs):
                return global_en_abs
    
    # Third priority: system fonts
    if is_ar:
        for f in ["C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arabtype.ttf"]:
            if os.path.exists(f):
                return os.path.abspath(f)
    else:
        for f in ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"]:
            if os.path.exists(f):
                return os.path.abspath(f)
    return None

def _find_reaction_clip(reactions_dir: str, policy: str = "rotate", preferred_id: Optional[str] = None) -> Optional[str]:
    """Find a reaction clip based on directory and policy."""
    if not os.path.exists(reactions_dir):
        logger.warning(f"Reactions directory not found: {reactions_dir}")
        return None
    
    # Get all valid reaction clips
    exts = {".mp4", ".mov", ".mkv", ".webm"}
    reaction_clips = []
    
    for name in os.listdir(reactions_dir):
        p = os.path.join(reactions_dir, name)
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in exts:
            reaction_clips.append(p)
    
    if not reaction_clips:
        logger.warning(f"No valid reaction clips found in {reactions_dir}")
        return None
    
    logger.info(f"Found {len(reaction_clips)} reaction candidate(s)")

    # 1. Preferred ID matching
    if preferred_id:
        for p in reaction_clips:
            if preferred_id in os.path.basename(p):
                logger.info(f"Using preferred reaction clip: {os.path.basename(p)}")
                return p

    # 2. Select clip based on policy
    if policy == "random":
        chosen = random.choice(reaction_clips)
        logger.info(f"Randomly selected reaction clip: {os.path.basename(chosen)}")
        return chosen
    elif policy == "rotate":
        # For rotate policy, use a simple hash of current minute or similar for pseudo-rotation if no state
        # In a real system we'd use a counter, but here we can just pick one based on time
        import time
        idx = int(time.time() // 60) % len(reaction_clips)
        chosen = reaction_clips[idx]
        logger.info(f"Selected reaction clip (rotate): {os.path.basename(chosen)}")
        return chosen
    else:
        chosen = reaction_clips[0]
        logger.info(f"Using default reaction clip: {os.path.basename(chosen)}")
        return chosen


def _overlay_position_expr(position: str, margin: int) -> tuple[str, str]:
    pos = (position or "bottom_center").lower()
    m = str(max(0, margin))
    # overlay uses main_w/main_h for base and W/H for overlay
    if pos == "bottom_right":
        return f"main_w-W-{m}", f"main_h-H-{m}"
    if pos == "bottom_left":
        return m, f"main_h-H-{m}"
    if pos == "top_right":
        return f"main_w-W-{m}", m
    if pos == "top_left":
        return m, m
    if pos == "top_center":
        return f"(main_w-W)/2", m
    # default bottom_center - add small offset to ensure proper centering
    return f"(main_w-W)/2+2", f"main_h-H-{m}"


def _normalize_facecam_scale(facecam_scale: Optional[float], default: float = 0.28) -> float:
    try:
        scale = float(facecam_scale if facecam_scale is not None else default)
    except Exception:
        scale = default
    return min(max(scale, 0.1), 0.8)


def _facecam_overlay_position_expr(position: str, margin: int, *, preview: bool = False) -> tuple[str, str]:
    pos = (position or "top_center").lower()
    w_token = "w" if preview else "overlay_w"
    h_token = "h" if preview else "overlay_h"
    main_w = "W" if preview else "main_w"
    main_h = "H" if preview else "main_h"
    m = str(max(0, margin))
    if pos == "top_left":
        return m, m
    if pos == "top_center":
        return f"({main_w}-{w_token})/2", m
    if pos == "bottom_left":
        return m, f"{main_h}-{h_token}-{m}"
    if pos == "bottom_center":
        return f"({main_w}-{w_token})/2", f"{main_h}-{h_token}-{m}"
    if pos == "center":
        return f"({main_w}-{w_token})/2", f"({main_h}-{h_token})/2"
    return f"{main_w}-{w_token}-{m}", m


def _facecam_uses_compact_overlay(facecam_layout: Optional[str], facecam_position: str) -> bool:
    layout = str(facecam_layout or "").strip().lower()
    pos = (facecam_position or "top_center").lower()
    return layout == "small_circle_top_right" or pos in {"top_right", "top_left", "bottom_right", "bottom_left", "center"}


def _edge_stability_stddev(input_path: str, side: str, thickness: int, sample_secs: int = 6) -> float:
    """
    Measure per-frame YAVG stddev on an edge strip to decide if it's a static bar (low variance)
    or a moving/transparent overlay (high variance). Returns stddev in 0-255 domain.
    side: 'top'|'bottom'|'left'|'right'
    """
    try:
        # Build crop expression per side
        if side == "top":
            crop = f"crop=w=in_w:h={max(1, thickness)}:x=0:y=0"
        elif side == "bottom":
            crop = f"crop=w=in_w:h={max(1, thickness)}:x=0:y=in_h-{max(1, thickness)}"
        elif side == "left":
            crop = f"crop=w={max(1, thickness)}:h=in_h:x=0:y=0"
        else:  # right
            crop = f"crop=w={max(1, thickness)}:h=in_h:x=in_w-{max(1, thickness)}:y=0"

        cmd = [
            ffmpeg_bin(), "-v", "error", "-t", str(max(1, sample_secs)),
            "-i", input_path,
            "-vf", f"{crop},signalstats",
            "-f", "null", "-"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=max(10, sample_secs + 6))
        logs = (res.stderr or "") + "\n" + (res.stdout or "")
        import re as _re
        yvals = []
        for ln in logs.splitlines():
            m = _re.search(r"YAVG:([0-9]+\.?[0-9]*)", ln)
            if m:
                try:
                    yvals.append(float(m.group(1)))
                except Exception as e:
                    pass
        if len(yvals) < 2:
            return 9999.0  # unknown -> treat as unstable to be safe
        # simple stddev
        mean = sum(yvals) / len(yvals)
        var = sum((v - mean) * (v - mean) for v in yvals) / (len(yvals) - 1)
        return var ** 0.5
    except Exception as e:
        return 9999.0


def _detect_and_remove_borders(input_path: str, temp_dir: str) -> str:
    """
    Detect and remove letterbox/pillarbox borders from video.
    Returns path to processed video.
    """
    if os.getenv("LOW_RESOURCE_MODE") == "1" or os.getenv("FFMPEG_LOW_CPU") == "1":
        logger.info("LOW_RESOURCE_MODE enabled: Skipping aggressive border detection to save CPU/RAM.")
        return input_path

    # Use FFmpeg's cropdetect with diversified passes to capture black/colored/blurred borders
    # 1) strict luma-based
    cmd1 = [
        ffmpeg_bin(), "-y",
        "-i", input_path,
        "-vf", "cropdetect=limit=0.03:round=2:reset=30",
        "-t", "10",
        "-f", "null",
        "-"
    ]
    # 2) lenient luma-based
    cmd2 = [
        ffmpeg_bin(), "-y",
        "-i", input_path,
        "-vf", "cropdetect=limit=0.1:round=2:reset=15",
        "-t", "15",
        "-f", "null",
        "-"
    ]
    # 3) grayscale prefilter to emphasize structure (helps with colored bars)
    cmd3 = [
        ffmpeg_bin(), "-y",
        "-i", input_path,
        "-vf", "format=gray,cropdetect=limit=0.02:round=2:reset=20",
        "-t", "12",
        "-f", "null",
        "-"
    ]
    # 4) downscale then detect (helps with blurred moving bars by reducing high-frequency noise)
    cmd4 = [
        ffmpeg_bin(), "-y",
        "-i", input_path,
        "-vf", "scale=iw*0.5:ih*0.5:flags=bicubic,cropdetect=limit=0.08:round=2:reset=10",
        "-t", "15",
        "-f", "null",
        "-"
    ]
    
    try:
        # Run commands and capture outputs
        result1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=30)
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=35)
        result3 = subprocess.run(cmd3, capture_output=True, text=True, timeout=35)
        result4 = subprocess.run(cmd4, capture_output=True, text=True, timeout=40)

        # Combine outputs from all passes
        output = "\n".join([result1.stderr or "", result2.stderr or "", result3.stderr or "", result4.stderr or ""])
        
        # Parse cropdetect output to find the most common crop values
        crop_lines = [line for line in output.split('\n') if 'crop=' in line]
        if not crop_lines:
            # No borders detected, try one more aggressive pass
            cmd5 = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-vf", "cropdetect=limit=0.12:round=2:reset=10",
                "-t", "20",
                "-f", "null",
                "-"
            ]
            result5 = subprocess.run(cmd5, capture_output=True, text=True, timeout=60)
            crop_lines = [line for line in (result5.stderr or "").split('\n') if 'crop=' in line]
            if not crop_lines:
                # No borders detected, return original path
                return input_path
            
        # Extract crop values
        crop_values = []
        for line in crop_lines:
            # Extract crop=w:h:x:y values
            import re
            match = re.search(r'crop=(\d+):(\d+):(\d+):(\d+)', line)
            if match:
                w, h, x, y = map(int, match.groups())
                crop_values.append((w, h, x, y))
        
        if not crop_values:
            # No valid crop values found
            return input_path
            
        # Use the most common crop values, but also consider values that appear frequently
        from collections import Counter
        crop_counter = Counter(crop_values)
        most_common = crop_counter.most_common(5)  # consider more candidates
        total_obs = sum(c for _, c in crop_counter.items()) or 1
        
        # Select the crop that removes the most borders while being frequent
        best_crop = most_common[0][0]
        best_removed_area = -1.0
        
        # Get actual video dimensions
        original_width = 1920  # Assume 1080p, will be overridden
        original_height = 1080
        
        probe_cmd = [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
            input_path
        ]
        try:
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
            if probe_result.stdout.strip():
                dimensions = probe_result.stdout.strip().split('x')
                if len(dimensions) == 2:
                    original_width, original_height = map(int, dimensions)
        except Exception as e:
            pass
        
        # Safety thresholds (env-tunable)
        MIN_PX = max(6, int(float((os.getenv("BORDER_MIN_THICKNESS_PX") or 10))))
        STAB = float((os.getenv("BORDER_STABILITY_MIN") or 0.3))  # min fraction of identical detections
        MAX_RATIO = float((os.getenv("BORDER_MAX_CROP_RATIO") or 0.45))  # per-dimension max removal
        MIN_REMAIN = float((os.getenv("BORDER_MIN_REMAIN_RATIO") or 0.6))  # ensure we keep >=60% each dim
        ALLOW_SINGLE = (os.getenv("BORDER_ALLOW_SINGLE_SIDE") or "1").strip().lower() in {"1","true","yes","on"}

        # Evaluate each potential crop (top-N)
        for crop, count in most_common:
            w, h, x, y = crop
            # Calculate removed area (borders)
            width_diff = original_width - w
            height_diff = original_height - h
            removed_area = width_diff * original_height + height_diff * w - width_diff * height_diff
            
            # Prefer crops that remove more area and have higher confidence (count)
            # Adjust with a factor based on count to favor more consistent detections
            score = removed_area * (1 + count * 0.1)
            
            if score > best_removed_area:
                best_removed_area = score
                best_crop = crop
        
        w, h, x, y = best_crop
        
        # Compute side thicknesses for gating
        width_diff = original_width - w
        height_diff = original_height - h
        top_th = y
        left_th = x
        bottom_th = max(0, original_height - (y + h))
        right_th = max(0, original_width - (x + w))

        # Stability of chosen crop (helps ignore moving transparent overlays)
        top_count = crop_counter.get(best_crop, 0)
        stability = top_count / total_obs

        # Symmetry heuristics
        def _balanced(a, b):
            m = max(a, b, 1)
            return abs(a - b) <= max(4, 0.3 * m)

        has_hbars = top_th >= MIN_PX and bottom_th >= MIN_PX and _balanced(top_th, bottom_th)
        has_vbars = left_th >= MIN_PX and right_th >= MIN_PX and _balanced(left_th, right_th)

        # Per-dimension max removal and minimum remain ratios
        safe_ratio = (width_diff <= MAX_RATIO * original_width) and (height_diff <= MAX_RATIO * original_height)
        safe_remain = (w >= MIN_REMAIN * original_width) and (h >= MIN_REMAIN * original_height)

        # Decide if crop is acceptable
        significant = (width_diff >= MIN_PX or height_diff >= MIN_PX)
        allow_single_side = ALLOW_SINGLE and (top_th >= MIN_PX or bottom_th >= MIN_PX or left_th >= MIN_PX or right_th >= MIN_PX)
        stable_enough = (stability >= STAB)

        # Edge stability check (ignore moving/transparent overlays)
        try:
            EDGE_STD_T = float((os.getenv("EDGE_STAB_YAVG_STDDEV") or 1.8))
            EDGE_SECS = int(float((os.getenv("EDGE_STAB_SECS") or 6)))
        except Exception as e:
            EDGE_STD_T = 1.8
            EDGE_SECS = 6

        edges_stable = True
        if has_hbars:
            td = _edge_stability_stddev(input_path, "top", int(top_th), EDGE_SECS)
            bd = _edge_stability_stddev(input_path, "bottom", int(bottom_th), EDGE_SECS)
            edges_stable = (td <= EDGE_STD_T and bd <= EDGE_STD_T)
        elif has_vbars:
            ld = _edge_stability_stddev(input_path, "left", int(left_th), EDGE_SECS)
            rd = _edge_stability_stddev(input_path, "right", int(right_th), EDGE_SECS)
            edges_stable = (ld <= EDGE_STD_T and rd <= EDGE_STD_T)
        elif allow_single_side:
            # Only check the present side
            if top_th >= MIN_PX:
                edges_stable = _edge_stability_stddev(input_path, "top", int(top_th), EDGE_SECS) <= EDGE_STD_T
            elif bottom_th >= MIN_PX:
                edges_stable = _edge_stability_stddev(input_path, "bottom", int(bottom_th), EDGE_SECS) <= EDGE_STD_T
            elif left_th >= MIN_PX:
                edges_stable = _edge_stability_stddev(input_path, "left", int(left_th), EDGE_SECS) <= EDGE_STD_T
            elif right_th >= MIN_PX:
                edges_stable = _edge_stability_stddev(input_path, "right", int(right_th), EDGE_SECS) <= EDGE_STD_T

        accept = False
        if significant and safe_ratio and safe_remain and stable_enough and edges_stable:
            if has_hbars or has_vbars:
                accept = True
            elif allow_single_side:
                # Single-side removal only if very stable and not too aggressive
                accept = stability >= max(STAB, 0.4)

        if accept:
            cropped_path = os.path.join(temp_dir, f"cropped_{os.path.basename(input_path)}")
            crop_cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-vf", f"crop={w}:{h}:{x}:{y}",
                "-c:a", "copy",
                cropped_path
            ]
            if run_ffmpeg_command(crop_cmd):
                return cropped_path
        
        # No significant borders or crop failed
        return input_path
    except Exception as e:
        logger.warning(f"Failed to detect/remove borders: {e}")
        return input_path


def _find_background(background_dir: str) -> Optional[str]:
    """
    Find a random background image from the backgrounds directory.
    """
    if not os.path.exists(background_dir):
        return None
    
    # Get all valid background images
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    backgrounds = []
    
    try:
        for name in os.listdir(background_dir):
            p = os.path.join(background_dir, name)
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in exts:
                backgrounds.append(p)
        
        if backgrounds:
            return random.choice(backgrounds)
    except Exception as e:
        logger.warning(f"Failed to find backgrounds: {e}")
    
    return None


def render_with_pip(
    cfg: Config,
    input_path: str,
    out_dir: str,
    reaction_id: Optional[str] = None,
    quality: Optional[Dict[str, Any]] = None,
    music_detection: Optional[Dict[str, Any]] = None,
    pip_policy: str = "rotate",
    overlay_text: Optional[str] = None,
    overlay_font: Optional[str] = None,
    overlay_position: str = "bottom",
    overlay_font_size: Optional[int] = None,
    watermark_text: Optional[str] = None,
    watermark_font: Optional[str] = None,
    watermark_seed: Optional[str] = None,
    effects_seed: Optional[str] = None,
    facecam_enabled: bool = True,
    facecam_path: Optional[str] = None,
    facecam_layout: Optional[str] = None,
    facecam_position: str = "top_center",
    facecam_scale: Optional[float] = None,
    facecam_x: Optional[str] = None,
    facecam_y: Optional[str] = None,
    facecam_shape: Optional[str] = None,
    override_pip: Optional[str] = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cfg.TEMP_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_path = os.path.join(out_dir, f"{base}_pip.mp4")
    
    # Check if input video has audio stream
    probe_cmd = [
        ffprobe_bin(), "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0",
        input_path
    ]
    try:
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        has_audio = "audio" in probe_result.stdout.strip()
    except Exception:
        has_audio = True
    
    if not validate_input_file(input_path):
        raise RuntimeError(f"Invalid input file: {input_path}")
    
    processed_input_path = _detect_and_remove_borders(input_path, cfg.TEMP_DIR)
    input_path = processed_input_path
    
    processed_audio_path: Optional[str] = None
    if getattr(cfg, "AUDIO_SEPARATION_ENABLED", False):
        try:
            processed_audio_path = separate_audio(cfg, input_path, out_dir)
            if not (processed_audio_path and os.path.exists(processed_audio_path) and os.path.getsize(processed_audio_path) > 0):
                processed_audio_path = None
        except Exception:
            processed_audio_path = None

    reaction = None
    if override_pip and os.path.exists(override_pip):
        reaction = override_pip
    elif reaction_id and os.path.exists(reaction_id):
        reaction = reaction_id
    else:
        reaction = _find_reaction_clip(cfg.REACTIONS_DIR, pip_policy, preferred_id=reaction_id)

    use_facecam = bool((facecam_path and os.path.exists(facecam_path)) and (facecam_enabled or True))
    # الحجم الافتراضي يماثل عرض الفيديو لملء أعلى الإطار
    fc_scale = float(facecam_scale) if facecam_scale is not None else 1.0
    fc_scale = max(0.05, min(fc_scale, 2.0))

    q = quality or {}
    target_res = (q.get("resolution") or "").lower()
    target_h = {"480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160}.get(target_res, 0)
    
    fps_val = q.get("fps")
    fps = int(fps_val) if fps_val else None
    
    is_low_res = os.getenv("LOW_RESOURCE_MODE") == "1" or os.getenv("FFMPEG_LOW_CPU") == "1"
    # 🔧 Quality fix: Render uses CRF 23 + superfast for far better quality than 28 + ultrafast
    default_crf_val = "23" if is_low_res else "18"
    default_preset_val = "superfast" if is_low_res else "veryfast"

    try:
        default_crf = int(os.getenv("SHORTS_RENDER_CRF", os.getenv("SHORTS_X264_CRF", default_crf_val)) or default_crf_val)
    except Exception:
        default_crf = int(default_crf_val)
    crf = int(q.get("crf") or default_crf)
    preset = str(q.get("preset") or os.getenv("SHORTS_RENDER_PRESET", os.getenv("SHORTS_X264_PRESET", default_preset_val)) or default_preset_val)
    
    ffmpeg_threads = getattr(cfg, "FFMPEG_THREADS", None)
    if is_low_res or os.getenv("RENDER") == "true":
        ffmpeg_threads = 1
    elif not ffmpeg_threads:
        try:
            from .resource_guard import recommend_ffmpeg_threads
            ffmpeg_threads = recommend_ffmpeg_threads() or 2
        except Exception:
            ffmpeg_threads = 2

    if not target_h:
        max_res = getattr(cfg, "MAX_RESOLUTION", "720p")
        target_h = {"480p": 480, "720p": 720, "1080p": 1080}.get(max_res, 720)

    desired_w = int(target_h)
    desired_h = int(round(desired_w * 16 / 9))
    if desired_w % 2: desired_w += 1
    if desired_h % 2: desired_h += 1

    # --- Modular Command Construction ---
    current_idx = 0
    inputs = ["-i", input_path]
    current_idx += 1
    
    reaction_idx = -1
    if reaction and os.path.exists(reaction):
        inputs.extend(["-i", reaction])
        reaction_idx = current_idx
        current_idx += 1
    
    bg_path = None
    if getattr(cfg, "BACKGROUND_REMOVAL_ENABLED", True):
        bg_path = _find_background(getattr(cfg, "BACKGROUND_DIR", "background"))
    
    bg_idx = -1
    if bg_path and os.path.exists(bg_path):
        inputs.extend(["-loop", "1", "-i", bg_path])
        bg_idx = current_idx
        current_idx += 1
        
    fc_idx = -1
    if use_facecam:
        try:
            facecam_path = _detect_and_remove_borders(facecam_path, cfg.TEMP_DIR)
        except Exception:
            pass
        inputs.extend(["-stream_loop", "-1", "-i", facecam_path])
        fc_idx = current_idx
        current_idx += 1
        
    audio_idx = -1
    if processed_audio_path and os.path.exists(processed_audio_path):
        inputs.extend(["-i", processed_audio_path])
        audio_idx = current_idx
        current_idx += 1
        
    current_v = "[0:v]"
    chains = []
    
    # Probe input dimensions
    video_width, video_height = 1080, 1920
    try:
        pr = subprocess.run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", input_path], capture_output=True, text=True, timeout=5)
        if pr.stdout.strip():
            video_width, video_height = map(int, pr.stdout.strip().split('x'))
    except Exception: pass

    video_duration_s = None
    try:
        prd = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if prd.stdout.strip():
            video_duration_s = float(prd.stdout.strip())
    except Exception:
        video_duration_s = None

    # 1. Base (Background removal/replacement)
    if bg_idx != -1:
        chains.append(f"[{bg_idx}:v]scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2[bg_base]")
        chains.append(f"[0:v]scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2[main_sc]")
        chains.append(f"[bg_base][main_sc]overlay=0:0[vbase]")
        current_v = "[vbase]"
    else:
        chains.append(f"[0:v]null[vbase]")
        current_v = "[vbase]"

    # 2. Reaction PIP
    if reaction_idx != -1:
        base_s = float(cfg.PIP_SCALE) if cfg.PIP_SCALE else 0.25
        pip_s = max(0.4, min(base_s + (0.1 if bg_idx != -1 else 0.05), 0.8))
        x_ex, y_ex = _overlay_position_expr(cfg.PIP_POSITION, int(cfg.PIP_MARGIN))
        chains.append(f"[{reaction_idx}:v]scale=iw*{pip_s}:ih*{pip_s}:flags=lanczos[pip_sc]")
        chains.append(f"{current_v}[pip_sc]overlay=x={x_ex}:y={y_ex}[vbase]")
        current_v = "[vbase]"
        
    # 3. Text Overlay
    temp_files_to_clean = []
    if overlay_text:
        shaped = _shape_text(overlay_text)
        f_size = overlay_font_size or 64
        pos = (overlay_position or "bottom_center").lower()
        y_ex = "10" if "top" in pos else "h-th-20"
        x_ex = "10" if "left" in pos else ("w-tw-10" if "right" in pos else "(w-tw)/2")
        tmp_dir = getattr(cfg, "TEMP_DIR", ".temp")
        os.makedirs(tmp_dir, exist_ok=True)
        # Use absolute path for Windows FFmpeg stability
        text_file = os.path.abspath(os.path.join(tmp_dir, f"overlay_{uuid.uuid4().hex}.txt"))
        
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(shaped)
            temp_files_to_clean.append(text_file)
            
            tf_esc = _ffmpeg_escape_path(text_file)
            font_path = _pick_best_font(shaped, overlay_font, cfg)
            shaping_opt = ":text_shaping=1" if _is_arabic_text(overlay_text) else ""
            draw = f"textfile='{tf_esc}'{shaping_opt}:fontsize={f_size}:fontcolor=white:borderw=2:bordercolor=black:box=1:boxcolor=black@0.4:x={x_ex}:y={y_ex}"
            if font_path:
                draw += f":fontfile='{_ffmpeg_escape_path(font_path)}'"
            chains.append(f"{current_v}drawtext={draw}[vbase]")
        except Exception as e:
            logger.error(f"Error preparing text overlay: {e}")
            # Fallback: continue without text if file creation fails
            chains.append(f"{current_v}null[vbase]")
            
        current_v = "[vbase]"

    if watermark_text and str(os.getenv("SHORTS_WATERMARK_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}:
        wm = _shape_text(str(watermark_text))
        tmp_dir = getattr(cfg, "TEMP_DIR", ".temp")
        os.makedirs(tmp_dir, exist_ok=True)
        wm_file = os.path.abspath(os.path.join(tmp_dir, f"watermark_{uuid.uuid4().hex}.txt"))

        try:
            with open(wm_file, "w", encoding="utf-8") as f:
                f.write(wm)
            temp_files_to_clean.append(wm_file)

            positions = [
                "top_center",
                "bottom_center",
                "bottom_left",
                "top_left",
            ]
            seed = (watermark_seed or "") + "::" + wm
            idx = int(hashlib.md5(seed.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(positions)
            pos = positions[idx]
            y_ex = "12" if "top" in pos else "h-th-28"
            x_ex = "12" if "left" in pos else "(w-tw)/2"

            try:
                alpha = float(os.getenv("SHORTS_WATERMARK_ALPHA", "0.22") or "0.22")
            except Exception:
                alpha = 0.22
            alpha = max(0.05, min(alpha, 0.9))

            wm_colors = [
                "0xFFFFFF",  # white
                "0x87CEFA",  # light sky blue
                "0xA0522D",  # sienna (brown)
                "0xFF7F7F",  # light red
            ]
            try:
                cidx = int(hashlib.md5((seed + "::color").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(wm_colors)
            except Exception:
                cidx = 0
            wm_color = wm_colors[cidx]

            tf_esc = _ffmpeg_escape_path(wm_file)
            font_path = _pick_best_font(wm, watermark_font or overlay_font, cfg)
            shaping_opt = ":text_shaping=1" if _is_arabic_text(watermark_text) else ""
            draw = f"textfile='{tf_esc}'{shaping_opt}:fontsize=54:fontcolor={wm_color}@{alpha}:borderw=2:bordercolor=black@{min(0.45, alpha + 0.18)}:x={x_ex}:y={y_ex}"
            if font_path:
                draw += f":fontfile='{_ffmpeg_escape_path(font_path)}'"
            chains.append(f"{current_v}drawtext={draw}[vbase]")
            current_v = "[vbase]"
        except Exception as e:
            logger.error(f"Error preparing watermark overlay: {e}")
            chains.append(f"{current_v}null[vbase]")
            current_v = "[vbase]"

    # 4. Facecam
    if fc_idx != -1:
        fc_pos = (facecam_position or "top_center").lower()
        compact_facecam = _facecam_uses_compact_overlay(facecam_layout, fc_pos)
        if compact_facecam:
            fc_margin = max(18, int(video_width * 0.022))
            fc_w = int(video_width * _normalize_facecam_scale(facecam_scale, 0.18))
            max_width = max(96, video_width - (fc_margin * 2))
            fc_w = min(max(fc_w, 96), max_width)
        else:
            fc_margin = 0
            fc_w = int(video_width)
            if fc_w < 80:
                fc_w = 80
        
        if facecam_x is not None and facecam_y is not None:
            fx, fy = str(facecam_x), str(facecam_y)
        else:
            if compact_facecam:
                fx, fy = _facecam_overlay_position_expr(fc_pos, fc_margin)
            elif "top_left" in fc_pos:
                fx, fy = "0", "0"
            elif "top_center" in fc_pos:
                fx, fy = "(main_w-overlay_w)/2", "0"
            elif "bottom_left" in fc_pos:
                fx, fy = "0", "main_h-overlay_h"
            elif "bottom_center" in fc_pos:
                fx, fy = "(main_w-overlay_w)/2", "main_h-overlay_h"
            elif "center" == fc_pos:
                fx, fy = "(main_w-overlay_w)/2", "(main_h-overlay_h)/2"
            else:
                fx, fy = "main_w-overlay_w", "0"
            
        try:
            fc_shape = (facecam_shape or "").strip().lower()
        except Exception:
            fc_shape = ""

        if fc_shape == "circle":
            size_expr = f"min(iw\\,ih)"
            chains.append(f"[{fc_idx}:v]fps={fps or 30},scale={fc_w}:-2:flags=lanczos,crop={size_expr}:{size_expr},format=rgba[fc_sq]")
            geq = f"[fc_sq]geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(min(W\\,H)/2)*(min(W\\,H)/2))\\,255\\,0)'[fc_sc]"
            chains.append(geq)
        else:
            chains.append(f"[{fc_idx}:v]scale={fc_w}:-2:flags=lanczos,fps={fps or 30}[fc_sc]")

        chains.append(f"{current_v}[fc_sc]overlay=x={fx}:y={fy}:shortest=1[vbase]")
        current_v = "[vbase]"

    # 5. Final Scale/FPS
    post_ops = f"scale={desired_w}:{desired_h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={desired_w}:{desired_h}:(ow-iw)/2:(oh-ih)/2"
    if fps: post_ops += f",fps={fps}"
    chains.append(f"{current_v}{post_ops}[vfinal]")

    # 6. Simple intro/outro effects (shorts-safe, no slide/transforms)
    try:
        effects_enabled = str(os.getenv("SHORTS_EFFECTS_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    except Exception:
        effects_enabled = True

    if effects_enabled and video_duration_s and video_duration_s > 0:
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
        outro_start = max(0.0, float(video_duration_s) - float(outro_d))

        intro_types = ["fade", "noise", "blur", "darken", "desat"]
        outro_types = ["fade", "noise", "blur", "darken", "desat"]

        base_seed = (effects_seed or watermark_seed or input_path or "")
        intro_idx = int(hashlib.md5((base_seed + "::intro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(intro_types)
        outro_idx = int(hashlib.md5((base_seed + "::outro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(outro_types)
        intro_type = intro_types[intro_idx]
        outro_type = outro_types[outro_idx]

        def _intro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=in:st=0:d={intro_d}"
            if kind == "noise":
                return f"noise=alls=20:allf=t+u:enable='between(t,0,{intro_d})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,0,{intro_d})'"
            if kind == "darken":
                return f"eq=brightness=-0.12:saturation=0.8:enable='between(t,0,{intro_d})'"
            return f"hue=s=0:enable='between(t,0,{intro_d})'"

        def _outro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=out:st={outro_start}:d={outro_d}"
            if kind == "noise":
                return f"noise=alls=22:allf=t+u:enable='between(t,{outro_start},{video_duration_s})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,{outro_start},{video_duration_s})'"
            if kind == "darken":
                return f"eq=brightness=-0.14:saturation=0.75:enable='between(t,{outro_start},{video_duration_s})'"
            return f"hue=s=0:enable='between(t,{outro_start},{video_duration_s})'"

        fx = f"{_intro_filter(intro_type)},{_outro_filter(outro_type)}"
        chains.append(f"[vfinal]{fx}[vfinal]")

    filter_complex = ";".join(chains)
    maps = ["-map", "[vfinal]"]
    if audio_idx != -1: maps.extend(["-map", f"{audio_idx}:a"])
    else: maps.extend(["-map", "0:a"])

    gop = int(fps or 30)
    if gop < 1:
        gop = 30
    try:
        audio_bitrate = str(os.getenv("AUDIO_BITRATE", "384k") or "384k")
    except Exception:
        audio_bitrate = "384k"
    try:
        audio_rate = str(int(os.getenv("AUDIO_SAMPLE_RATE", "48000") or 48000))
    except Exception:
        audio_rate = "48000"

    # 🔧 YouTube quality: tune=film for visual content, bitrate controls for clean re-encode
    is_low_res = os.getenv("LOW_RESOURCE_MODE") == "1" or os.getenv("FFMPEG_LOW_CPU") == "1"
    min_rate = "5M" if is_low_res else "8M"
    max_rate = "10M" if is_low_res else "16M"
    buf_size = "15M" if is_low_res else "24M"
    try:
        if q.get("min_bitrate"):
            min_rate = str(q.get("min_bitrate"))
            import re as _re
            num = float(_re.sub(r'[^0-9.]', '', min_rate))
            unit = _re.sub(r'[0-9.]', '', min_rate).lower() or "m"
            max_rate = f"{num * 2:.0f}{unit}"
            buf_size = f"{num * 3:.0f}{unit}"
    except Exception:
        pass

    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_complex, "-threads", str(ffmpeg_threads)] + maps + [
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-profile:v", "high",
        "-level", _shorts_h264_level(),
        "-g", str(gop),
        "-keyint_min", str(max(1, gop // 2)),
        "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-tune", "film",
        "-b:v", min_rate,
        "-maxrate", max_rate,
        "-bufsize", buf_size,
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-ar", audio_rate,
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-use_editlist", "0",
        "-shortest",
        out_path,
    ]

    logger.info("🚀 Starting modular rendering...")
    try:
        if not run_ffmpeg_command(cmd):
            logger.error("❌ Modular rendering failed.")
            raise RuntimeError("FFmpeg failed")
    finally:
        # Clean up temporary text files AFTER ffmpeg has finished
        for tmp_f in temp_files_to_clean:
            try:
                if os.path.exists(tmp_f):
                    os.remove(tmp_f)
            except Exception:
                pass

    if not validate_output_file(out_path):
        raise RuntimeError("Output validation failed")
        
    return out_path


def generate_overlay_preview(
    cfg: Config,
    font_path: Optional[str],
    text: str,
    font_size: int,
    position: str,
    output_path: str,
    facecam_path: Optional[str] = None,
    facecam_position: Optional[str] = None,
    facecam_layout: Optional[str] = None,
    facecam_scale: Optional[float] = None,
    facecam_x: Optional[str] = None,
    facecam_y: Optional[str] = None
) -> bool:
    """
    توليد صورة معاينة (PNG) تحاكي فيديو شورتس (1080x1920)
    تظهر كيف سيبدو النص المختار مع الحجم والموضع، وكذلك الفيس كام إذا وجد.
    """
    try:
        shaped = _shape_text(text)
        f_size = font_size or 64
        pos = (position or "bottom").lower()
        
        y_ex = "10" if "top" in pos else "h-th-100"
        x_ex = "(w-tw)/2"
        
        filters = []
        
        # 1. إضافة الفيس كام أولاً إذا كان موجوداً
        if facecam_path and os.path.exists(facecam_path):
            try:
                facecam_path = _detect_and_remove_borders(facecam_path, getattr(cfg, "TEMP_DIR", ".temp"))
            except Exception:
                pass
            fc_pos = (facecam_position or "top_center").lower()
            compact_facecam = _facecam_uses_compact_overlay(facecam_layout, fc_pos)
            if compact_facecam:
                fc_margin = max(18, int(1080 * 0.022))
                fc_w = int(1080 * _normalize_facecam_scale(facecam_scale, 0.18))
                fc_w = min(max(fc_w, 96), 1080 - (fc_margin * 2))
            else:
                fc_margin = 0
                fc_w = 1080
                if fc_w < 80:
                    fc_w = 80
            
            if facecam_x is not None and facecam_y is not None:
                fx, fy = str(facecam_x), str(facecam_y)
            else:
                if compact_facecam:
                    fx, fy = _facecam_overlay_position_expr(fc_pos, fc_margin, preview=True)
                elif "top_left" in fc_pos:
                    fx, fy = "0", "0"
                elif "top_center" in fc_pos:
                    fx, fy = "(W-w)/2", "0"
                elif "bottom_left" in fc_pos:
                    fx, fy = "0", "H-h"
                elif "bottom_center" in fc_pos:
                    fx, fy = "(W-w)/2", "H-h"
                elif "center" == fc_pos:
                    fx, fy = "(W-w)/2", "(H-h)/2"
                else:
                    fx, fy = "W-w", "0"
            
            # فلتر الفيس كام
            filters.append(f"[1:v]scale={fc_w}:-1[fc]")
            filters.append(f"[0:v][fc]overlay=x={fx}:y={fy}[bg_fc]")
            current_v = "[bg_fc]"
        else:
            current_v = "[0:v]"
            
        # 2. إضافة النص
        tmp_dir = getattr(cfg, "TEMP_DIR", ".temp")
        os.makedirs(tmp_dir, exist_ok=True)
        # Use absolute path for Windows stability
        text_file = os.path.abspath(os.path.join(tmp_dir, f"overlay_prev_{uuid.uuid4().hex}.txt"))
        
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(shaped)
            tf_esc = _ffmpeg_escape_path(text_file)
            f_use = None
            if font_path and os.path.exists(font_path):
                f_use = os.path.abspath(font_path)
            else:
                f_use = _pick_best_font(shaped, None, cfg)
            draw = f"drawtext=textfile='{tf_esc}':fontsize={f_size}:fontcolor=white:borderw=2:bordercolor=black:box=1:boxcolor=black@0.4:x={x_ex}:y={y_ex}"
            if f_use:
                draw += f":fontfile='{_ffmpeg_escape_path(f_use)}'"
            filters.append(f"{current_v}{draw}[outv]")
            
            # إضافة نص توضيحي للمعاينة
            filters.append(f"[outv]drawtext=text='SHORT PREVIEW CANVASS (1080x1920)':fontsize=40:fontcolor=gray@0.5:x=(w-tw)/2:y=h-50[final]")
            
            filter_complex = ";".join(filters)
            
            # خلفية سوداء 1080x1920
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1"
            ]
            
            if facecam_path and os.path.exists(facecam_path):
                cmd.extend(["-i", facecam_path])
                
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[final]",
                "-vframes", "1",
                output_path
            ])
            
            success = run_ffmpeg_command(cmd)
            return success
        finally:
            # Clean up temporary text file AFTER ffmpeg has finished
            try:
                if os.path.exists(text_file):
                    os.remove(text_file)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to generate overlay preview: {e}")
        return False
