"""
محرك الجلب التلقائي لفيديوهات المودات
نظام مستقل يعمل على Render مع دعم نسخ متعددة عبر Supabase
"""
import os
import sys

# إضافة مسار المشروع للجذر للسماح بالتشغيل المباشر
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def _project_local_path(*parts: str) -> str:
    return os.path.abspath(os.path.join(project_root, *parts))


def _resolve_project_runtime_path(raw_path: str) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return _project_local_path(raw)


def _ensure_runtime_dir(path: str) -> str:
    resolved = os.path.abspath(str(path or ""))
    if resolved:
        ResilientFS.makedirs(resolved, exist_ok=True)
    return resolved


def _create_runtime_dir_keepalive(path: str, marker_name: str = ".automod_active") -> tuple[str, str]:
    resolved = _ensure_runtime_dir(path)
    marker_path = os.path.join(resolved, marker_name)
    if resolved:
        with suppress(Exception):
            with ResilientFS.open(marker_path, "w", encoding="utf-8") as fh:
                fh.write(str(time.time()))
    return resolved, marker_path


def _release_runtime_dir_keepalive(marker_path: str) -> None:
    if marker_path:
        with suppress(Exception):
            ResilientFS.remove(marker_path)

import logging
import asyncio
import time
import uuid
import subprocess
import json
import re
import random
import shutil
import base64
import threading
from contextlib import suppress
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlparse

from src.utils.resilient_fs import ResilientFS
from src.agent.config import load_config
from src.agent.job_queue import JobQueue
from src.bot.persistence import (
    has_pending_raw_reviews,
    get_pending_raw_review,
    is_raw_review_approved,
    is_raw_review_blocked,
    is_raw_review_skip_active,
)

logger = logging.getLogger(__name__)
_YT_BOTCHECK_HINT_SHOWN = False
_FB_HINT_SHOWN = False
_COBALT_FALLBACK_DISABLED = False
_COBALT_FALLBACK_DISABLED_HOSTS: Dict[str, str] = {}
_COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST: Dict[str, float] = {}
_COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST: Dict[str, str] = {}
_COBALT_FALLBACK_COOLDOWN_SECONDS = 900
_COBALT_DISABLE_HINT_SHOWN = False

# === Invidious / Piped fallback instances ===
_INVIDIOUS_INSTANCES = [
    "https://vid.puffyan.us",
    "https://invidious.fdn.fr",
    "https://inv.tux.pizza",
    "https://invidious.privacyredirect.com",
    "https://invidious.projectsegfau.lt",
    "https://iv.datura.network",
]
_PIPED_API_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.in.projectsegfau.lt",
    "https://pipedapi.leptons.xyz",
]
_INVIDIOUS_PIPED_COOLDOWN_UNTIL: Dict[str, float] = {}
_INVIDIOUS_PIPED_COOLDOWN_SECONDS = 600
_COBALT_MISSING_AUTH_HINT_SHOWN = False
_MODERN_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_RUN_CYCLE_LOCK = threading.Lock()
_RUN_CYCLE_META_LOCK = threading.Lock()
_RUN_CYCLE_STARTED_MONOTONIC = 0.0


def _processing_lock_stale_minutes(default_minutes: int = 90) -> int:
    try:
        raw = (os.getenv("AUTO_MOD_PROCESSING_STALE_MINUTES", str(default_minutes)) or str(default_minutes)).strip()
        minutes = int(float(raw))
    except Exception:
        minutes = default_minutes
    return max(5, minutes)


def _should_force_reset_processing_on_boot() -> bool:
    raw = (os.getenv("AUTO_MOD_FORCE_RESET_PROCESSING_ON_BOOT", "true") or "true").strip().lower()
    force_reset = raw in {"1", "true", "yes", "on"}
    multi_instance_raw = (os.getenv("AUTOMODBOT_ALLOW_MULTI_INSTANCE", "") or "").strip().lower()
    multi_instance = multi_instance_raw in {"1", "true", "yes", "on"}
    return force_reset and not multi_instance


def _mark_run_cycle_started() -> None:
    global _RUN_CYCLE_STARTED_MONOTONIC
    with _RUN_CYCLE_META_LOCK:
        _RUN_CYCLE_STARTED_MONOTONIC = time.monotonic()


def _mark_run_cycle_finished() -> None:
    global _RUN_CYCLE_STARTED_MONOTONIC
    with _RUN_CYCLE_META_LOCK:
        _RUN_CYCLE_STARTED_MONOTONIC = 0.0


def _running_cycle_elapsed_seconds() -> int:
    with _RUN_CYCLE_META_LOCK:
        started = float(_RUN_CYCLE_STARTED_MONOTONIC or 0.0)
    if started <= 0:
        return 0
    return max(0, int(time.monotonic() - started))

_OVERLAY_POSITIONS = {
    "top": "top",
    "top_center": "top",
    "top-center": "top",
    "center": "center",
    "middle": "center",
    "bottom": "bottom",
    "bottom_center": "bottom",
    "bottom-center": "bottom",
}

_VIDEO_EFFECT_TYPES = {
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

_FACECAM_SHAPES = {
    "circle": "circle",
    "round": "circle",
    "rounded": "circle",
    "square": "square",
    "rect": "square",
    "rectangle": "square",
}

_FACECAM_ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}

_FACECAM_LAYOUT_PRESETS = {
    "top_center": {"layout": "top_center", "position": "top_center", "shape": "circle", "scale": 0.28},
    "bottom_center": {"layout": "bottom_center", "position": "bottom_center", "shape": "circle", "scale": 0.28},
    "small_circle_top_left": {"layout": "small_circle_top_left", "position": "top_left", "shape": "circle", "scale": 0.18},
    "small_circle_top_right": {"layout": "small_circle_top_right", "position": "top_right", "shape": "circle", "scale": 0.18},
    "small_circle_bottom_right": {"layout": "small_circle_bottom_right", "position": "bottom_right", "shape": "circle", "scale": 0.18},
    "small_circle_bottom_left": {"layout": "small_circle_bottom_left", "position": "bottom_left", "shape": "circle", "scale": 0.18},
}

_FACECAM_LAYOUT_ALIASES = {
    "top": "top_center",
    "top-center": "top_center",
    "top_center": "top_center",
    "bottom": "bottom_center",
    "bottom-center": "bottom_center",
    "bottom_center": "bottom_center",
    "small_circle_top_left": "small_circle_top_left",
    "small-circle-top-left": "small_circle_top_left",
    "top_left_small_circle": "small_circle_top_left",
    "small_top_left": "small_circle_top_left",
    "circle_small_top_left": "small_circle_top_left",
    "small_circle_top_right": "small_circle_top_right",
    "small-circle-top-right": "small_circle_top_right",
    "top_right_small_circle": "small_circle_top_right",
    "small_top_right": "small_circle_top_right",
    "circle_small_top_right": "small_circle_top_right",
    "small_circle_bottom_right": "small_circle_bottom_right",
    "small-circle-bottom-right": "small_circle_bottom_right",
    "bottom_right_small_circle": "small_circle_bottom_right",
    "small_bottom_right": "small_circle_bottom_right",
    "circle_small_bottom_right": "small_circle_bottom_right",
    "small_circle_bottom_left": "small_circle_bottom_left",
    "small-circle-bottom-left": "small_circle_bottom_left",
    "bottom_left_small_circle": "small_circle_bottom_left",
    "small_bottom_left": "small_circle_bottom_left",
    "circle_small_bottom_left": "small_circle_bottom_left",
    "custom": "custom",
}

_FACECAM_POSITION_ALIASES = {
    "top": "top_center",
    "top-center": "top_center",
    "top_center": "top_center",
    "bottom": "bottom_center",
    "bottom-center": "bottom_center",
    "bottom_center": "bottom_center",
    "top-right": "top_right",
    "top_right": "top_right",
    "top-left": "top_left",
    "top_left": "top_left",
    "bottom-right": "bottom_right",
    "bottom_right": "bottom_right",
    "bottom-left": "bottom_left",
    "bottom_left": "bottom_left",
    "center": "center",
}


def _normalize_facecam_position(value: Any) -> str:
    return _FACECAM_POSITION_ALIASES.get(str(value or "top_center").strip().lower(), "top_center")


def _normalize_facecam_scale_value(value: Any, default: float = 0.28) -> float:
    try:
        scale = float(value if value is not None else default)
    except Exception:
        scale = default
    return min(max(scale, 0.1), 0.8)


def _infer_facecam_layout(position: str, shape: str, scale: float) -> str:
    if position == "top_left" and shape == "circle" and scale <= 0.24:
        return "small_circle_top_left"
    if position == "top_right" and shape == "circle" and scale <= 0.24:
        return "small_circle_top_right"
    if position == "bottom_right" and shape == "circle" and scale <= 0.24:
        return "small_circle_bottom_right"
    if position == "bottom_left" and shape == "circle" and scale <= 0.24:
        return "small_circle_bottom_left"
    if position == "bottom_center" and shape == "circle" and 0.24 <= scale <= 0.32:
        return "bottom_center"
    if position == "top_center" and shape == "circle" and 0.24 <= scale <= 0.32:
        return "top_center"
    return "custom"


def resolve_facecam_layout_config(
    layout: Any = None,
    *,
    position: Any = None,
    shape: Any = None,
    scale: Any = None,
) -> Dict[str, Any]:
    layout_key = _FACECAM_LAYOUT_ALIASES.get(str(layout or "").strip().lower(), str(layout or "").strip().lower())
    if layout_key in _FACECAM_LAYOUT_PRESETS:
        return dict(_FACECAM_LAYOUT_PRESETS[layout_key])

    normalized_position = _normalize_facecam_position(position)
    normalized_shape = _FACECAM_SHAPES.get(str(shape or "circle").strip().lower(), "circle")
    normalized_scale = _normalize_facecam_scale_value(scale, 0.28)
    return {
        "layout": _infer_facecam_layout(normalized_position, normalized_shape, normalized_scale),
        "position": normalized_position,
        "shape": normalized_shape,
        "scale": normalized_scale,
    }


def parse_source_settings(raw_settings: Any) -> Dict[str, Any]:
    if isinstance(raw_settings, dict):
        return dict(raw_settings)
    if isinstance(raw_settings, str):
        raw_text = raw_settings.strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _split_configured_texts(value: Any, *, allow_blocks: bool = False) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []

    raw = value.strip()
    if not raw:
        return []

    if allow_blocks:
        parts = []
        current = []
        for line in raw.splitlines():
            if line.strip() == "---":
                block = "\n".join(current).strip()
                if block:
                    parts.append(block)
                current = []
                continue
            current.append(line.rstrip())
        block = "\n".join(current).strip()
        if block:
            parts.append(block)
        if parts:
            return parts

    return [line.strip() for line in raw.splitlines() if line.strip()]


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_source_settings(raw_settings: Any) -> Dict[str, Any]:
    settings = parse_source_settings(raw_settings)
    normalized = dict(settings)

    try:
        fetch_sources = normalized.get("fetch_sources")
        if isinstance(fetch_sources, dict):
            fetch_sources = [fetch_sources]
        if not isinstance(fetch_sources, list):
            fetch_sources = []

        cleaned: List[Dict[str, Any]] = []
        for item in fetch_sources:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("source_url") or "").strip()
            if not url:
                continue
            cleaned.append({
                "url": url,
                "name": str(item.get("name") or item.get("source_name") or "").strip(),
                "platform": str(item.get("platform") or "").strip().lower() or None,
                "enabled": bool(item.get("enabled", True)),
            })

        if cleaned:
            normalized["fetch_sources"] = cleaned
        else:
            normalized.pop("fetch_sources", None)
    except Exception:
        pass
    
    # Privacy setting: public/unlisted/private (default to None to use global config)
    if "privacy" in settings:
        privacy = str(settings.get("privacy", "")).strip().lower()
        if privacy in ["public", "unlisted", "private"]:
            normalized["privacy"] = privacy
        else:
            normalized["privacy"] = None
            
    # Video type filter: shorts_only (default to None to auto-detect)
    if "shorts_only" in settings:
        raw_shorts_only = settings.get("shorts_only")
        if raw_shorts_only is None:
            normalized["shorts_only"] = None
        elif isinstance(raw_shorts_only, str) and raw_shorts_only.strip().lower() in {"default", "inherit", "global", "auto"}:
            normalized["shorts_only"] = None
        else:
            normalized["shorts_only"] = _to_bool(raw_shorts_only)
    
    # Horizontal flip setting: true/false (None to use global config)
    if "hflip" in settings:
        raw_hflip = settings.get("hflip")
        if raw_hflip is None:
            normalized["hflip"] = None
        elif isinstance(raw_hflip, str) and raw_hflip.strip().lower() in {"default", "inherit", "global", "auto"}:
            normalized["hflip"] = None
        else:
            normalized["hflip"] = _to_bool(raw_hflip)
    
    # Backward compatibility: support hflip_enabled as alias for hflip
    if "hflip_enabled" in settings and "hflip" not in settings:
        raw_hflip_enabled = settings.get("hflip_enabled")
        if raw_hflip_enabled is None:
            normalized["hflip"] = None
        else:
            normalized["hflip"] = _to_bool(raw_hflip_enabled)

    def _normalize_overlay_animation_config(raw_animation: Any) -> Dict[str, Any]:
        animation_type = "none"
        duration = 0.0
        enabled = False

        if isinstance(raw_animation, dict):
            animation_type = str(raw_animation.get("type") or raw_animation.get("animation") or "none").strip().lower()
            enabled = _to_bool(raw_animation.get("enabled"), animation_type not in {"none", "off", "disabled", "no"})
            duration = raw_animation.get("duration", 0.6)
        elif isinstance(raw_animation, str):
            animation_type = raw_animation.strip().lower()
            enabled = animation_type not in {"none", "off", "disabled", "no"}
            duration = 0.6

        animation_aliases = {
            "none": "none",
            "off": "none",
            "disabled": "none",
            "no": "none",
            "fade": "fade",
            "blur": "blur",
        }
        animation_type = animation_aliases.get(animation_type, "none")
        try:
            duration = max(0.0, float(duration or 0.0))
        except Exception:
            duration = 0.0

        if animation_type == "none" or not enabled:
            return {"enabled": False, "type": "none", "duration": 0.0}

        duration = min(max(duration, 0.2), 2.0)
        return {"enabled": True, "type": animation_type, "duration": duration}

    def _normalize_video_effect_config(raw_effect: Any) -> Dict[str, Any]:
        effect_type = "none"
        duration = 0.0
        enabled = False

        if isinstance(raw_effect, dict):
            effect_type = str(raw_effect.get("type") or raw_effect.get("effect") or "none").strip().lower()
            enabled = _to_bool(raw_effect.get("enabled"), effect_type not in {"none", "off", "disabled", "no"})
            duration = raw_effect.get("duration", 1.0)
        elif isinstance(raw_effect, str):
            effect_type = raw_effect.strip().lower()
            enabled = effect_type not in {"none", "off", "disabled", "no"}
            duration = 1.0

        effect_type = _VIDEO_EFFECT_TYPES.get(effect_type, "none")
        try:
            duration = max(0.0, float(duration or 0.0))
        except Exception:
            duration = 0.0

        if effect_type == "none" or not enabled:
            return {"enabled": False, "type": "none", "duration": 0.0}

        duration = min(max(duration, 0.3), 3.0)
        return {"enabled": True, "type": effect_type, "duration": duration}

    def _normalize_facecam_clip(raw_clip: Any, index: int) -> Optional[Dict[str, Any]]:
        if isinstance(raw_clip, str):
            raw_path = raw_clip.strip()
            if not raw_path:
                return None
            raw_clip = {"path": raw_path}
        elif not isinstance(raw_clip, dict):
            return None

        raw_path = str(raw_clip.get("path") or "").strip()
        if not raw_path:
            return None

        clip_name = str(raw_clip.get("name") or os.path.basename(raw_path) or f"facecam_{index + 1}").strip()
        clip_id = str(raw_clip.get("id") or os.path.splitext(clip_name)[0] or uuid.uuid4()).strip()
        created_at = str(raw_clip.get("created_at") or "").strip()
        enabled = _to_bool(raw_clip.get("enabled"), True)
        return {
            "id": clip_id or str(uuid.uuid4()),
            "path": raw_path.replace("\\", "/"),
            "name": clip_name,
            "enabled": enabled,
            "created_at": created_at,
        }

    overlay_raw = settings.get("shorts_overlay") if isinstance(settings.get("shorts_overlay"), dict) else {}
    overlay_texts = _split_configured_texts(overlay_raw.get("texts"))
    overlay_timing = str(overlay_raw.get("timing") or "full").strip().lower()
    if overlay_timing not in {"start", "end", "full"}:
        overlay_timing = "full"
    overlay_duration = overlay_raw.get("duration", 2.0)
    try:
        overlay_duration = max(0.5, float(overlay_duration))
    except Exception:
        overlay_duration = 2.0
    overlay_position = _OVERLAY_POSITIONS.get(
        str(overlay_raw.get("screen_position") or "top").strip().lower(),
        "top",
    )
    overlay_mode = str(overlay_raw.get("selection_mode") or "fixed").strip().lower()
    if overlay_mode not in {"fixed", "random"}:
        overlay_mode = "fixed"
    normalized["shorts_overlay"] = {
        "enabled": bool(overlay_raw.get("enabled", False)) and bool(overlay_texts),
        "texts": overlay_texts,
        "selection_mode": overlay_mode,
        "timing": overlay_timing,
        "duration": overlay_duration,
        "screen_position": overlay_position,
        "intro_animation": _normalize_overlay_animation_config(overlay_raw.get("intro_animation") or settings.get("overlay_intro_animation")),
        "outro_animation": _normalize_overlay_animation_config(overlay_raw.get("outro_animation") or settings.get("overlay_outro_animation")),
    }

    desc_raw = settings.get("extra_description") if isinstance(settings.get("extra_description"), dict) else {}
    desc_texts = _split_configured_texts(desc_raw.get("texts"), allow_blocks=True)
    desc_mode = str(desc_raw.get("selection_mode") or "fixed").strip().lower()
    if desc_mode not in {"fixed", "random"}:
        desc_mode = "fixed"
    placement = str(desc_raw.get("placement") or "append").strip().lower()
    if placement not in {"append", "prepend"}:
        placement = "append"
    normalized["extra_description"] = {
        "enabled": bool(desc_raw.get("enabled", False)) and bool(desc_texts),
        "texts": desc_texts,
        "selection_mode": desc_mode,
        "placement": placement,
    }
    tail_trim_raw = settings.get("tail_trim")
    if isinstance(tail_trim_raw, dict):
        tail_trim_enabled = _to_bool(tail_trim_raw.get("enabled"), False)
        tail_trim_seconds = tail_trim_raw.get("seconds", 0.0)
    elif isinstance(tail_trim_raw, (int, float, str)):
        tail_trim_enabled = True
        tail_trim_seconds = tail_trim_raw
    else:
        tail_trim_enabled = False
        tail_trim_seconds = 0.0
    try:
        tail_trim_seconds = max(0.0, float(tail_trim_seconds or 0.0))
    except Exception:
        tail_trim_seconds = 0.0
    normalized["tail_trim"] = {
        "enabled": bool(tail_trim_enabled and tail_trim_seconds > 0),
        "seconds": tail_trim_seconds,
    }
    effects_root = settings.get("video_effects") if isinstance(settings.get("video_effects"), dict) else {}
    normalized["video_effects"] = {
        "intro": _normalize_video_effect_config(effects_root.get("intro") or settings.get("intro_effect")),
        "outro": _normalize_video_effect_config(effects_root.get("outro") or settings.get("outro_effect")),
    }
    facecam_raw = settings.get("facecam") if isinstance(settings.get("facecam"), dict) else {}
    facecam_resolved = resolve_facecam_layout_config(
        facecam_raw.get("layout") or settings.get("facecam_layout"),
        position=facecam_raw.get("position") or settings.get("facecam_position") or "top_center",
        shape=facecam_raw.get("shape") or settings.get("facecam_shape") or "circle",
        scale=facecam_raw.get("scale", settings.get("facecam_scale", 0.28)),
    )
    facecam_enabled = _to_bool(
        facecam_raw.get("enabled"),
        _to_bool(settings.get("facecam_enabled"), False),
    )
    raw_clips = facecam_raw.get("clips")
    if not isinstance(raw_clips, list):
        raw_clips = settings.get("facecam_clips") if isinstance(settings.get("facecam_clips"), list) else []
    normalized_clips = [
        clip
        for idx, clip in enumerate(raw_clips)
        for clip in [_normalize_facecam_clip(clip, idx)]
        if clip
    ]
    normalized["facecam"] = {
        "layout": facecam_resolved["layout"],
        "enabled": bool(facecam_enabled),
        "position": facecam_resolved["position"],
        "shape": facecam_resolved["shape"],
        "scale": facecam_resolved["scale"],
        "clips": normalized_clips,
    }
    normalized["facecam_layout"] = normalized["facecam"]["layout"]
    normalized["facecam_enabled"] = normalized["facecam"]["enabled"]
    normalized["facecam_position"] = normalized["facecam"]["position"]
    normalized["facecam_shape"] = normalized["facecam"]["shape"]
    normalized["facecam_scale"] = normalized["facecam"]["scale"]
    normalized["facecam_clips"] = list(normalized_clips)
    normalized["require_raw_review"] = _to_bool(settings.get("require_raw_review"), False)
    return normalized


def merge_source_settings(base_settings: Any, update_settings: Any) -> Dict[str, Any]:
    base = parse_source_settings(base_settings)
    updates = parse_source_settings(update_settings)
    merged = dict(base)

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged.get(key) or {})
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value

    return normalize_source_settings(merged)


def _choose_configured_text(texts: List[str], selection_mode: str = "fixed") -> str:
    clean = [str(item).strip() for item in (texts or []) if str(item).strip()]
    if not clean:
        return ""
    if selection_mode == "random" and len(clean) > 1:
        return random.choice(clean)
    return clean[0]


def pick_source_overlay_config(raw_settings: Any) -> Optional[Dict[str, Any]]:
    overlay = normalize_source_settings(raw_settings).get("shorts_overlay") or {}
    if not overlay.get("enabled"):
        return None
    chosen_text = _choose_configured_text(overlay.get("texts") or [], overlay.get("selection_mode", "fixed"))
    if not chosen_text:
        return None
    return {
        "text": chosen_text,
        "timing": overlay.get("timing", "full"),
        "duration": float(overlay.get("duration", 2.0) or 2.0),
        "screen_position": overlay.get("screen_position", "top"),
        "intro_animation": dict(overlay.get("intro_animation") or {"enabled": False, "type": "none", "duration": 0.0}),
        "outro_animation": dict(overlay.get("outro_animation") or {"enabled": False, "type": "none", "duration": 0.0}),
    }


def pick_source_tail_trim_seconds(raw_settings: Any) -> float:
    trim_cfg = normalize_source_settings(raw_settings).get("tail_trim") or {}
    try:
        seconds = max(0.0, float(trim_cfg.get("seconds", 0.0) or 0.0))
    except Exception:
        seconds = 0.0
    if not trim_cfg.get("enabled"):
        return 0.0
    return seconds


def pick_source_video_effects(raw_settings: Any) -> Dict[str, Dict[str, Any]]:
    effects = normalize_source_settings(raw_settings).get("video_effects") or {}
    intro = effects.get("intro") if isinstance(effects.get("intro"), dict) else {}
    outro = effects.get("outro") if isinstance(effects.get("outro"), dict) else {}
    return {
        "intro": {
            "enabled": bool(intro.get("enabled")),
            "type": str(intro.get("type") or "none"),
            "duration": float(intro.get("duration", 0.0) or 0.0),
        },
        "outro": {
            "enabled": bool(outro.get("enabled")),
            "type": str(outro.get("type") or "none"),
            "duration": float(outro.get("duration", 0.0) or 0.0),
        },
    }


def pick_source_facecam_config(raw_settings: Any) -> Dict[str, Any]:
    facecam = normalize_source_settings(raw_settings).get("facecam") or {}
    clips = facecam.get("clips") if isinstance(facecam.get("clips"), list) else []
    return {
        "layout": str(facecam.get("layout") or "top_center"),
        "enabled": bool(facecam.get("enabled")),
        "position": str(facecam.get("position") or "top_center"),
        "shape": str(facecam.get("shape") or "circle"),
        "scale": float(facecam.get("scale", 0.28) or 0.28),
        "clips": [dict(item) for item in clips if isinstance(item, dict)],
    }


def pick_source_facecam_clip(raw_settings: Any, channel_id: str = "") -> Tuple[Dict[str, Any], str]:
    facecam = pick_source_facecam_config(raw_settings)
    if not facecam.get("enabled"):
        return facecam, ""

    valid_source_clips: List[str] = []
    for clip in facecam.get("clips") or []:
        if not isinstance(clip, dict) or not clip.get("enabled"):
            continue
        clip_path = _resolve_project_runtime_path(str(clip.get("path") or ""))
        ext = os.path.splitext(clip_path)[1].lower()
        if clip_path and ext in _FACECAM_ALLOWED_EXTENSIONS and ResilientFS.isfile(clip_path):
            valid_source_clips.append(clip_path)

    if valid_source_clips:
        return facecam, random.choice(valid_source_clips)

    legacy_channel_id = str(channel_id or "").strip()
    if legacy_channel_id:
        legacy_dir = _project_local_path(".data", "facecam", legacy_channel_id)
        if ResilientFS.isdir(legacy_dir):
            legacy_clips = [
                os.path.join(legacy_dir, name)
                for name in ResilientFS.listdir(legacy_dir)
                if ResilientFS.isfile(os.path.join(legacy_dir, name))
                and os.path.splitext(name)[1].lower() in _FACECAM_ALLOWED_EXTENSIONS
            ]
            if legacy_clips:
                return facecam, random.choice(legacy_clips)

    return facecam, ""


def merge_source_extra_description(base_description: str, raw_settings: Any) -> str:
    extra_cfg = normalize_source_settings(raw_settings).get("extra_description") or {}
    if not extra_cfg.get("enabled"):
        return (base_description or "").strip()

    selected_text = _choose_configured_text(extra_cfg.get("texts") or [], extra_cfg.get("selection_mode", "fixed"))
    selected_text = selected_text.strip()
    if not selected_text:
        return (base_description or "").strip()

    original = (base_description or "").strip()
    if not original:
        return selected_text
    if extra_cfg.get("placement") == "prepend":
        return f"{selected_text}\n\n{original}"
    return f"{original}\n\n{selected_text}"


def _get_source_extra_description_text(raw_settings: Any) -> str:
    extra_cfg = normalize_source_settings(raw_settings).get("extra_description") or {}
    if not extra_cfg.get("enabled"):
        return ""
    return _choose_configured_text(extra_cfg.get("texts") or [], extra_cfg.get("selection_mode", "fixed")).strip()


def _looks_like_shorts_url(url: Any) -> bool:
    raw = str(url or "").strip().lower()
    if not raw:
        return False
    return any(marker in raw for marker in ("/shorts/", "/reel/", "/reels/"))


def _clean_youtube_video_id(candidate: Any) -> str:
    raw = str(candidate or "").strip()
    if not raw:
        return ""
    if any(token in raw for token in ("://", "/", "?", "&", "=")):
        return ""
    return raw


def _extract_youtube_video_id(url: Any, fallback_id: Any = None) -> str:
    video_id = _clean_youtube_video_id(fallback_id)
    raw = str(url or "").strip()
    if not raw:
        return video_id
    try:
        parsed = urlparse(raw)
    except Exception:
        return video_id

    host = (parsed.netloc or "").lower()
    if not any(domain in host for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return video_id

    path_parts = [part for part in (parsed.path or "").split("/") if part]
    if "youtu.be" in host and path_parts:
        return _clean_youtube_video_id(path_parts[0]) or video_id
    if "shorts" in [part.lower() for part in path_parts]:
        for idx, part in enumerate(path_parts):
            if part.lower() == "shorts" and idx + 1 < len(path_parts):
                return _clean_youtube_video_id(path_parts[idx + 1]) or video_id

    query_video_id = parse_qs(parsed.query or "").get("v", [""])[0]
    return _clean_youtube_video_id(query_video_id) or video_id


def _normalize_youtube_watch_url(url: Any, fallback_id: Any = None) -> str:
    raw = str(url or "").strip()
    video_id = _extract_youtube_video_id(raw, fallback_id=fallback_id)
    if not video_id:
        return raw
    return f"https://www.youtube.com/watch?v={video_id}"


def _infer_processing_video_type(video: Dict[str, Any], src_platform: Any, source_url: Any = None, source_settings: Any = None) -> str:
    explicit_video_type = str((video or {}).get("video_type") or "").strip().lower()
    if explicit_video_type in ("shorts", "long"):
        return explicit_video_type

    # Check if source has shorts_only setting enabled
    if source_settings and _to_bool(source_settings.get("shorts_only"), False):
        return "shorts"

    platform = str(src_platform or "").strip().lower()
    if ("shorts" in platform) or ("reels" in platform):
        return "shorts"
    if "long" in platform:
        return "long"

    for candidate_url in (
        (video or {}).get("url"),
        (video or {}).get("webpage_url"),
        (video or {}).get("original_url"),
        source_url,
    ):
        if _looks_like_shorts_url(candidate_url):
            return "shorts"

    try:
        vid_dur = float((video or {}).get("duration") or 0)
    except Exception:
        vid_dur = 0.0
    return "shorts" if (vid_dur and vid_dur <= 60.0) else "long"


def _append_required_exact_hashtags(tags: List[str], required_tags: List[str]) -> List[str]:
    required_lower = {str(tag or "").lower() for tag in required_tags if str(tag or "").strip()}
    out: List[str] = []
    seen_exact = set()
    for tag in tags or []:
        raw = str(tag or "").strip()
        if not raw:
            continue
        if raw.lower() in required_lower and raw not in required_tags:
            continue
        if raw in seen_exact:
            continue
        seen_exact.add(raw)
        out.append(raw)
    for required in required_tags:
        clean = str(required or "").strip()
        if clean and clean not in seen_exact:
            seen_exact.add(clean)
            out.append(clean)
    return out


def _join_hashtags(tags: List[str], max_chars: int) -> str:
    chosen: List[str] = []
    current_len = 0
    for raw in tags or []:
        tag = str(raw or "").strip()
        if not tag:
            continue
        next_len = len(tag) if not chosen else current_len + 1 + len(tag)
        if next_len > max_chars:
            break
        chosen.append(tag)
        current_len = next_len
    return " ".join(chosen).strip()


def _build_hashtag_only_upload_metadata(
    ai_meta: Dict[str, Any],
    *,
    source_title: str,
    source_name: str,
    content_type: str,
    target_lang: str,
    is_shorts: bool,
    source_description: str = "",
    source_settings: Any = None,
) -> Tuple[str, str, List[str]]:
    from src.agent.ai import _extract_hashtags_from_text, _keywords_from_hashtags, _sanitize_hashtag_list, _lang_requires_script_lock, optimize_hashtags
    from src.agent.local_metadata import extract_source_metadata_context

    merged_description = merge_source_extra_description(
        ai_meta.get("description", source_description or ""),
        source_settings,
    )
    extra_description_text = _get_source_extra_description_text(source_settings)
    source_context = ai_meta.get("source_context") if isinstance(ai_meta.get("source_context"), dict) else {}
    source_signals = extract_source_metadata_context(
        hint_title=source_title,
        source_description=" ".join(part for part in [source_description, extra_description_text] if part),
        lang=target_lang,
        content_type=content_type,
        source_name=source_name,
        source_context=source_context,
        max_hashtags=12,
    )

    hashtag_candidates: List[str] = list(ai_meta.get("hashtags") or []) + list(source_signals.get("hashtags") or [])
    for text in (
        ai_meta.get("title", ""),
        ai_meta.get("description", ""),
        merged_description,
        source_description,
        extra_description_text,
    ):
        hashtag_candidates.extend(_extract_hashtags_from_text(text))

    for keyword in list(ai_meta.get("tags") or []):
        clean = str(keyword or "").strip()
        if clean:
            hashtag_candidates.append(f"#{clean}")

    strict_local_script = _lang_requires_script_lock(target_lang)
    if is_shorts and not strict_local_script:
        hashtag_candidates.append("#shorts")

    title_tags, description_tags = optimize_hashtags(
        hashtag_candidates,
        target_lang,
        topic=source_signals.get("topic") or source_title or content_type,
        limit_title=8,
        limit_desc=18,
    )

    if is_shorts and not strict_local_script:
        required_title_tags = _sanitize_hashtag_list(["#shorts"], target_lang, 1)
        title_tags = required_title_tags + [tag for tag in title_tags if tag.lower() not in {req.lower() for req in required_title_tags}]

    if not title_tags:
        fallback_candidates = list(source_signals.get("hashtags") or [])
        if is_shorts and not strict_local_script:
            fallback_candidates.append("#shorts")
        title_tags = _sanitize_hashtag_list(
            fallback_candidates or (["#shorts"] if (is_shorts and not strict_local_script) else ["#video"]),
            target_lang,
            6,
        )
    if not description_tags:
        description_tags = list(title_tags)

    final_title = _join_hashtags(title_tags, 95)
    if not final_title:
        final_title = "#shorts" if (is_shorts and not strict_local_script) else "#video"

    final_description = _join_hashtags(description_tags, 4900)
    if not final_description:
        final_description = final_title

    upload_tags = _keywords_from_hashtags(description_tags, source_title or content_type, target_lang, limit=15)
    return final_title, final_description, upload_tags


def _parse_datetime_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        else:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _build_yt_opts(extra_opts: Optional[Dict[str, Any]] = None, cookies_path: Optional[Any] = None) -> Dict[str, Any]:
    """
    بناء إعدادات yt-dlp الموحدة مع دعم:
    - Proxy (عبر YTDLP_PROXY)
    - PO Token (عبر YOUTUBE_PO_TOKEN)
    - Cookies
    - IPv4
    - Sleep intervals
    """
    from src.agent.config import load_config
    cfg = load_config()
    cookies_info = None
    if isinstance(cookies_path, dict):
        cookies_info = dict(cookies_path)
        cookies_path = cookies_info.get("path") or ""
    if cookies_path is None:
        cookies_info = _resolve_cookiefile_details()
        cookies_path = cookies_info.get("path") or ""

    youtube_extractor_args = {}
    if not cookies_path:
        # بدون كوكيز، نستخدم مسار عملاء محافظ لتقليل bot-check قدر الإمكان.
        youtube_extractor_args = {
            "player_client": ["ios", "android", "mweb"],
        }

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_check_certificate": True,
        "socket_timeout": 30,
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": _MODERN_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
            "Sec-Fetch-Mode": "navigate",
        },
        "extractor_args": {
            "youtube": youtube_extractor_args,
        },
        # محاكاة متصفح حقيقي لتجاوز الفحص (معطل حالياً لتفادي وقوع خطأ في Render)
        # "impersonate": "chrome",
        # تأخير بين الطلبات لتقليل bot-check
        "sleep_interval": 5,
        "max_sleep_interval": 15,
        "sleep_requests": 2,
    }

    impersonate = (os.environ.get("YTDLP_IMPERSONATE") or "").strip()
    if impersonate and os.environ.get("YTDLP_SKIP_IMPERSONATE") != "1":
        opts["impersonate"] = impersonate

    # === Proxy (الأهم لتجاوز bot-check على سيرفرات datacenter) ===
    proxy = (os.environ.get("YTDLP_PROXY") or "").strip()
    if proxy:
        opts["proxy"] = proxy
        logger.debug("Using proxy for yt-dlp (scheme=%s)", _proxy_scheme_label(proxy))

    # === PO Token (Proof of Origin) ===
    po_token = (os.environ.get("YOUTUBE_PO_TOKEN") or "").strip()
    if po_token:
        opts["extractor_args"]["youtube"]["po_token"] = [po_token]
        logger.debug("Using PO Token for YouTube")

    # === IPv4 ===
    if getattr(cfg, "YTDLP_FORCE_IPV4", True):
        opts["source_address"] = "0.0.0.0"

    # === Cookies ===
    if cookies_path:
        opts["cookiefile"] = cookies_path
        logger.debug(
            "🍪 Using yt-dlp cookies file: %s | source=%s",
            _safe_path_label(cookies_path),
            (cookies_info or {}).get("source") or "resolved",
        )

    # دمج الإعدادات الإضافية
    if extra_opts:
        extra_opts = dict(extra_opts)
        # دمج extractor_args بشكل عميق
        if "extractor_args" in extra_opts:
            for key, val in extra_opts["extractor_args"].items():
                if key in opts["extractor_args"]:
                    opts["extractor_args"][key].update(val)
                else:
                    opts["extractor_args"][key] = val
            del extra_opts["extractor_args"]
        # دمج http_headers بشكل عميق
        if "http_headers" in extra_opts:
            opts["http_headers"].update(extra_opts["http_headers"])
            del extra_opts["http_headers"]
        opts.update(extra_opts)

    return opts


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _download_profile_overrides(ffmpeg_path: Optional[str], cookies_enabled: bool = False) -> List[Dict[str, Any]]:
    """ترتيب محافظ لمسارات تنزيل yt-dlp: جودة أعلى أولًا ثم توافق أوسع."""
    compatibility_profile = {
        "label": "compatibility_mobile",
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "mweb"],
            }
        },
    }

    if cookies_enabled:
        authenticated_default_profile = {
            "label": "authenticated_default",
        }

        authenticated_web_profile = {
            "label": "authenticated_web",
            "extractor_args": {
                "youtube": {
                    "player_client": ["web", "web_safari", "mweb"],
                }
            },
        }

        if not ffmpeg_path or not _env_flag("YTDLP_HIGH_QUALITY_FIRST", True):
            return [authenticated_default_profile, authenticated_web_profile]

        return [
            authenticated_default_profile,
            authenticated_web_profile,
            compatibility_profile,
        ]

    if not ffmpeg_path or not _env_flag("YTDLP_HIGH_QUALITY_FIRST", True):
        return [compatibility_profile]

    return [
        {
            "label": "high_quality_android_vr",
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr", "android", "mweb"],
                }
            },
        },
        {
            "label": "high_quality_web",
            "extractor_args": {
                "youtube": {
                    "player_client": ["web", "android", "mweb"],
                }
            },
        },
        compatibility_profile,
    ]


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _first_env_named(*names: str) -> Tuple[str, str]:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def _runtime_environment_name() -> str:
    markers = (
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_SERVICE_NAME",
        "RENDER_EXTERNAL_URL",
        "RENDER_INSTANCE_ID",
    )
    return "render" if any((os.environ.get(name) or "").strip() for name in markers) else "local_or_other"


def _resolve_remote_downloader_settings() -> Dict[str, Any]:
    base_url = _first_env("DOWNLOADER_WORKER_URL", "REMOTE_DOWNLOADER_URL").rstrip("/")
    token = _first_env("DOWNLOADER_WORKER_TOKEN", "REMOTE_DOWNLOADER_TOKEN")

    enabled_raw = os.environ.get("DOWNLOADER_WORKER_ENABLED")
    enabled = bool(base_url) if enabled_raw is None else _env_flag("DOWNLOADER_WORKER_ENABLED", bool(base_url))

    prefer_raw = os.environ.get("DOWNLOADER_WORKER_PREFER_REMOTE")
    default_prefer_remote = bool(base_url) and _runtime_environment_name() == "render"
    prefer_remote = default_prefer_remote if prefer_raw is None else _env_flag(
        "DOWNLOADER_WORKER_PREFER_REMOTE",
        default_prefer_remote,
    )

    timeout_raw = _first_env("DOWNLOADER_WORKER_TIMEOUT")
    timeout_seconds = 420.0
    if timeout_raw:
        with suppress(Exception):
            timeout_seconds = max(30.0, float(timeout_raw))

    return {
        "url": base_url,
        "token": token,
        "enabled": bool(base_url) and enabled,
        "prefer_remote": bool(base_url) and prefer_remote,
        "timeout_seconds": timeout_seconds,
        "host": _safe_url_host(base_url),
    }


def _yt_dlp_version_label() -> str:
    try:
        import yt_dlp

        version_obj = getattr(yt_dlp, "version", None)
        if hasattr(version_obj, "__version__"):
            return str(getattr(version_obj, "__version__", "unknown"))
        return str(version_obj or getattr(yt_dlp, "__version__", "unknown"))
    except Exception as exc:
        return f"unavailable:{exc.__class__.__name__}"


def _safe_path_label(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return "none"
    norm = os.path.normpath(raw)
    base = os.path.basename(norm)
    parent = os.path.basename(os.path.dirname(norm))
    if parent == ".data":
        return os.path.join(parent, base)
    return base or norm


def _proxy_scheme_label(proxy: str) -> str:
    raw = str(proxy or "").strip()
    if not raw:
        return "off"
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    return (parsed.scheme or "set").lower()


def _safe_url_host(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "unknown"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return parsed.hostname or parsed.netloc or parsed.path or "unknown"


def _cookie_resolution_result(path: str = "", source: str = "none", issues: Optional[List[str]] = None) -> Dict[str, Any]:
    normalized = os.path.normpath(path) if path else ""
    return {
        "path": normalized,
        "source": source,
        "available": bool(normalized),
        "path_label": _safe_path_label(normalized),
        "issues": [str(item) for item in (issues or []) if str(item).strip()],
    }


def _resolve_cookiefile_details() -> Dict[str, Any]:
    issues: List[str] = []
    data_dir = os.path.abspath(os.path.join(project_root, ".data"))

    path_env_name, raw_path = _first_env_named("YTDLP_COOKIES_PATH", "YT_COOKIES_PATH", "COOKIES_PATH")
    if raw_path:
        try:
            resolved_path = _resolve_project_runtime_path(raw_path)
            if os.path.exists(resolved_path):
                return _cookie_resolution_result(resolved_path, f"env_path:{path_env_name}", issues)
            issues.append(f"{path_env_name}=missing:{_safe_path_label(resolved_path)}")
        except Exception as exc:
            issues.append(f"{path_env_name}=invalid:{exc.__class__.__name__}")

    b64_env_name, raw_b64 = _first_env_named("YTDLP_COOKIES_B64", "YT_COOKIES_B64")
    txt_env_name, raw_txt = _first_env_named("YTDLP_COOKIES_TEXT", "YT_COOKIES_TEXT")
    inline_env_name = b64_env_name or txt_env_name
    if raw_b64 or raw_txt:
        try:
            ResilientFS.makedirs(data_dir, exist_ok=True)
            out_path = os.path.join(data_dir, "yt_dlp_cookies.txt")
            if raw_b64:
                content = base64.b64decode(raw_b64.encode("utf-8")).decode("utf-8", errors="replace")
                source = f"env_b64:{inline_env_name}"
            else:
                content = raw_txt
                source = f"env_text:{inline_env_name}"

            normalized_content = content.strip()
            if not normalized_content:
                issues.append(f"{inline_env_name}=empty")
            else:
                ResilientFS.write_text(out_path, normalized_content, encoding="utf-8")
                if os.path.exists(out_path):
                    return _cookie_resolution_result(out_path, source, issues)
                issues.append(f"{inline_env_name}=write_failed")
        except Exception as exc:
            issues.append(f"{inline_env_name}=error:{exc.__class__.__name__}")

    default_candidates = [
        (os.path.join(data_dir, "yt_dlp_cookies.txt"), "runtime_file:.data/yt_dlp_cookies.txt"),
        (os.path.join(data_dir, "yt_cookies.txt"), "runtime_file:.data/yt_cookies.txt"),
    ]
    for candidate_path, source in default_candidates:
        if os.path.exists(candidate_path):
            return _cookie_resolution_result(candidate_path, source, issues)

    common_cookie_files = [
        "www.youtube.com_cookies.txt",
        "youtube_cookies.txt",
        "cookies.txt",
        "youtube.com_cookies.txt",
    ]
    for filename in common_cookie_files:
        candidate_path = os.path.join(project_root, filename)
        if os.path.exists(candidate_path):
            return _cookie_resolution_result(candidate_path, f"project_file:{filename}", issues)

    return _cookie_resolution_result("", "none", issues)


def _build_ytdlp_runtime_diagnostics(
    context: str,
    cookies_info: Optional[Dict[str, Any]] = None,
    profile_labels: Optional[List[str]] = None,
) -> str:
    api_url, auth_scheme, auth_token = _resolve_cobalt_api_settings()
    remote_settings = _resolve_remote_downloader_settings()
    proxy = _first_env("YTDLP_PROXY")
    proxy_status = "off" if not proxy else f"on:{_proxy_scheme_label(proxy)}"
    po_token = _first_env("YOUTUBE_PO_TOKEN")
    impersonate = "skipped" if os.environ.get("YTDLP_SKIP_IMPERSONATE") == "1" else (_first_env("YTDLP_IMPERSONATE") or "off")
    cookie_summary = "cookies=none"
    if cookies_info:
        if cookies_info.get("available"):
            cookie_summary = f"cookies={cookies_info.get('source')}:{cookies_info.get('path_label')}"
        if cookies_info.get("issues"):
            cookie_summary += f" issues={';'.join(cookies_info['issues'][:2])}"

    parts = [
        f"ctx={context}",
        f"runtime={_runtime_environment_name()}",
        f"python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"yt_dlp={_yt_dlp_version_label()}",
        cookie_summary,
        f"proxy={proxy_status}",
        f"impersonate={impersonate}",
        f"po_token={'present' if po_token else 'missing'}",
        f"cobalt_auth={'present' if auth_token else 'missing'}",
        f"cobalt_auth_scheme={(auth_scheme.lower() if auth_scheme else 'none')}",
        f"cobalt_host={_safe_url_host(api_url)}",
        f"remote_worker={'on' if remote_settings['enabled'] else 'off'}:{remote_settings['host']}",
        f"remote_prefer={'yes' if remote_settings['prefer_remote'] else 'no'}",
    ]
    if profile_labels:
        parts.append(f"profiles={','.join(profile_labels)}")
    return " | ".join(parts)


def _build_youtube_botcheck_hint(cookies_info: Optional[Dict[str, Any]] = None) -> str:
    info = cookies_info or _resolve_cookiefile_details()
    has_proxy = bool(_first_env("YTDLP_PROXY"))
    has_po_token = bool(_first_env("YOUTUBE_PO_TOKEN"))
    _, _, cobalt_auth = _resolve_cobalt_api_settings()

    if not info.get("available"):
        base = (
            "No valid cookies were resolved. Configure YTDLP_COOKIES_PATH or "
            "YTDLP_COOKIES_B64 / YTDLP_COOKIES_TEXT (legacy YT_COOKIES_* aliases still work)."
        )
    else:
        base = f"Cookies were loaded from {info.get('source')} ({info.get('path_label')}) but YouTube still returned bot-check"
        if _runtime_environment_name() == "render":
            base += "; this usually points to datacenter/IP reputation or cookie/IP mismatch on Render"
        base += "."

    missing_steps = []
    if not has_proxy:
        missing_steps.append("YTDLP_PROXY")
    if not has_po_token:
        missing_steps.append("YOUTUBE_PO_TOKEN")
    if missing_steps:
        base += f" Consider {', '.join(missing_steps)} to reduce Render bot-check failures."
    if not cobalt_auth:
        base += " Cobalt auth token is missing, so fallback may fail on default/public Cobalt servers."
    return base


def _resolve_cobalt_api_settings() -> Tuple[str, str, str]:
    api_url = _first_env("COBALT_API_URL", "COBALT_URL") or "https://api.cobalt.tools/"
    jwt_token = _first_env("COBALT_API_JWT")
    if jwt_token:
        return api_url.strip(), "Bearer", jwt_token.strip()

    api_key = _first_env("COBALT_API_KEY", "COBALT_API_TOKEN", "COBALT_AUTH_TOKEN")
    if api_key:
        return api_url.strip(), "Api-Key", api_key.strip()

    return api_url.strip(), "", ""


def _get_cobalt_fallback_disable_state(api_url: str = "") -> Tuple[bool, str]:
    if _COBALT_FALLBACK_DISABLED:
        return True, "fallback disabled globally for this process"

    resolved_api_url = api_url or _resolve_cobalt_api_settings()[0]
    host = _safe_url_host(resolved_api_url)
    permanent_reason = _COBALT_FALLBACK_DISABLED_HOSTS.get(host)
    if permanent_reason:
        return True, permanent_reason

    cooldown_until = _COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST.get(host, 0.0)
    if cooldown_until and cooldown_until <= time.time():
        _COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST.pop(host, None)
        _COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST.pop(host, None)
        return False, ""

    if cooldown_until:
        reason = _COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST.get(host) or "fallback temporarily disabled for this host"
        remaining_seconds = max(1, int(cooldown_until - time.time()))
        return True, f"{reason} Retry after about {remaining_seconds}s."

    return False, ""


def _disable_cobalt_fallback(reason: str, api_url: str = ""):
    global _COBALT_FALLBACK_DISABLED, _COBALT_DISABLE_HINT_SHOWN
    if _COBALT_FALLBACK_DISABLED:
        if reason and not _COBALT_DISABLE_HINT_SHOWN:
            _COBALT_DISABLE_HINT_SHOWN = True
            logger.warning(f"⚠️ Cobalt API fallback disabled for this process: {reason}")
        return

    resolved_api_url = api_url or _resolve_cobalt_api_settings()[0]
    host = _safe_url_host(resolved_api_url)
    if host:
        _COBALT_FALLBACK_DISABLED_HOSTS[host] = reason or "fallback disabled for this host in this process"

    if reason and not _COBALT_DISABLE_HINT_SHOWN:
        _COBALT_DISABLE_HINT_SHOWN = True
        logger.warning("⚠️ Cobalt API fallback disabled for host=%s for this process: %s", host, reason)


def _temporarily_disable_cobalt_fallback(
    reason: str,
    api_url: str = "",
    cooldown_seconds: int = _COBALT_FALLBACK_COOLDOWN_SECONDS,
):
    resolved_api_url = api_url or _resolve_cobalt_api_settings()[0]
    host = _safe_url_host(resolved_api_url)
    if not host:
        host = "unknown"

    cooldown_until = time.time() + max(1, int(cooldown_seconds))
    _COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST[host] = max(
        _COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST.get(host, 0.0),
        cooldown_until,
    )
    _COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST[host] = reason or "fallback temporarily disabled for this host"
    logger.warning(
        "⚠️ Cobalt API fallback temporarily disabled for host=%s for %ss: %s",
        host,
        max(1, int(cooldown_seconds)),
        reason,
    )


def _resolve_yt_cookiefile() -> str:
    return _resolve_cookiefile_details().get("path") or ""


def _resolve_any_cookiefile() -> str:
    return _resolve_cookiefile_details().get("path") or ""


def _is_youtube_botcheck_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(phrase in s for phrase in [
        "sign in to confirm you\u2019re not a bot",
        "sign in to confirm you're not a bot",
        "confirm you are not a bot",
        "confirm you're not a bot",
        "to verify you are a human",
    ])


def _classify_download_error(err: Exception) -> Tuple[str, str]:
    raw_message = str(err).strip()
    lowered = raw_message.lower()

    if _is_youtube_botcheck_error(err):
        return "youtube_botcheck", "YouTube requested a human-verification / anti-bot challenge"
    if "requested format is not available" in lowered or "requested formats are not available" in lowered:
        return "format_unavailable", "The requested high-quality format is not available for this video/client"
    if "impersonate target" in lowered and "is not available" in lowered:
        return "impersonate_unavailable", "The requested yt-dlp impersonation target is not available in this runtime"
    if "rate-limited" in lowered or "too many requests" in lowered or "http error 429" in lowered:
        return "rate_limited", "The source temporarily rate-limited this download attempt"
    if any(token in lowered for token in ["timed out", "timeout", "connection reset", "network is unreachable", "name resolution", "temporary failure"]):
        return "network_failure", "Network/CDN communication failed while resolving or downloading the media"
    if any(token in lowered for token in ["private video", "members-only", "sign in to confirm your age", "video unavailable"]):
        return "availability_restricted", "The source video is unavailable or restricted for this runtime"

    return "unknown_error", raw_message or err.__class__.__name__


def _is_facebook_reel_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return ("/reel/" in u) or ("/reels/" in u) or ("facebook.com/reel/" in u) or ("facebook.com/reels/" in u)


def _normalize_facebook_source_url(source_url: str, platform: str) -> str:
    if not source_url:
        return source_url
    if not (platform or "").startswith("facebook"):
        return source_url

    raw = source_url.strip()
    if not raw:
        return raw

    if _is_facebook_reel_url(raw):
        return raw

    if platform == "facebook_reels":
        try:
            from urllib.parse import urlparse
            pr = urlparse(raw)
            if not pr.scheme or not pr.netloc:
                return raw
            if "facebook.com" not in pr.netloc.lower():
                return raw
            base = raw.split("?", 1)[0].rstrip("/")
            if base.endswith("/reels"):
                return base
            if base.endswith("/reels/"):
                return base.rstrip("/")
            return base + "/reels"
        except Exception:
            return raw

    return raw

# ==================== معرف النسخة ====================

def get_instance_id() -> str:
    """
    الحصول على معرف النسخة الفريد
    يستخدم INSTANCE_ID من المتغيرات البيئية، أو الملف المحلي .data/instance_id، أو RENDER_SERVICE_NAME، أو يولد واحدًا ويحفظه
    """
    # 1. فحص المتغيرات البيئية (أولوية قصوى)
    env_id = (
        os.environ.get("INSTANCE_ID")
        or os.environ.get("RENDER_SERVICE_NAME")
        or os.environ.get("HOSTNAME")
    )
    if env_id:
        return env_id

    # 2. فحص الملف المحلي (بمسار مطلق لتجنب التكرار عند تغيير CWD)
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
        data_dir = os.path.join(project_root, ".data")
        id_file = os.path.join(data_dir, "instance_id")
        
        if ResilientFS.exists(id_file):
            with ResilientFS.open(id_file, "r", encoding="utf-8") as f:
                saved_id = f.read().strip()
                if saved_id:
                    return saved_id
    except Exception as e:
        logger.error(f"Failed to read instance_id file: {e}")

    # 3. توليد معرف جديد وحفظه
    new_id = f"local_{uuid.uuid4().hex[:8]}"
    
    try:
        ResilientFS.makedirs(data_dir, exist_ok=True)
        with ResilientFS.open(id_file, "w", encoding="utf-8") as f:
            f.write(new_id)
        logger.info(f"Generated and saved new persistent instance_id: {new_id}")
    except Exception as e:
        logger.error(f"Failed to save new instance_id: {e}")
        return f"temp_{os.getpid()}"

    return new_id


# ==================== عمليات قاعدة البيانات ====================

# التخزين المؤقت للإحصائيات والإعدادات لتقليل الضغط على Supabase وتحسين الاستجابة
_stats_cache = {}  # {instance_id: (timestamp, stats_dict)}
_config_cache = {} # {instance_id: (timestamp, config_dict)}
_CACHE_TTL = 300   # 5 دقائق
_AUTO_MOD_LOCAL_LOCK = threading.RLock()


def _auto_mod_local_table_path(table: str) -> str:
    filenames = {
        "auto_mod_config": "auto_mod_config.json",
        "auto_mod_sources": "auto_mod_sources.json",
        "auto_mod_schedule": "auto_mod_schedule.json",
        "auto_mod_processed": "auto_mod_processed.json",
    }
    return _project_local_path(".data", filenames.get(table, f"{table}.json"))


def _load_local_table_rows(table: str) -> List[Dict[str, Any]]:
    path = _auto_mod_local_table_path(table)
    try:
        if not ResilientFS.exists(path):
            return []
        with ResilientFS.open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    except Exception as e:
        logger.warning(f"Failed to load local {table}: {e}")
        return []


def _save_local_table_rows(table: str, rows: List[Dict[str, Any]]) -> None:
    path = _auto_mod_local_table_path(table)
    ResilientFS.makedirs(os.path.dirname(path), exist_ok=True)
    with ResilientFS.open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _local_row_matches(row: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
    if not filters:
        return True
    return all(row.get(key) == value for key, value in filters.items())


def _local_select_rows(table: str, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    with _AUTO_MOD_LOCAL_LOCK:
        rows = _load_local_table_rows(table)
    return [dict(row) for row in rows if _local_row_matches(row, filters)]


def _local_upsert_row(table: str, data: Dict[str, Any], key_field: str = "id", on_conflict: Optional[str] = None) -> bool:
    payload = dict(data or {})
    if not payload:
        return False

    conflict_fields = [field.strip() for field in str(on_conflict or "").split(",") if field.strip()] or [key_field]

    def _matches(row: Dict[str, Any]) -> bool:
        for field in conflict_fields:
            if row.get(field) != payload.get(field):
                return False
        return True

    with _AUTO_MOD_LOCAL_LOCK:
        rows = _load_local_table_rows(table)
        match_index = next((idx for idx, row in enumerate(rows) if _matches(row)), None)
        if match_index is None and payload.get(key_field) not in (None, ""):
            match_index = next((idx for idx, row in enumerate(rows) if row.get(key_field) == payload.get(key_field)), None)

        if match_index is None:
            rows.append(payload)
        else:
            merged = dict(rows[match_index])
            merged.update(payload)
            rows[match_index] = merged

        _save_local_table_rows(table, rows)
    return True


def _local_delete_row(table: str, key_field: str, key_value: Any) -> bool:
    with _AUTO_MOD_LOCAL_LOCK:
        rows = _load_local_table_rows(table)
        new_rows = [row for row in rows if row.get(key_field) != key_value]
        if len(new_rows) != len(rows):
            _save_local_table_rows(table, new_rows)
    return True

class AutoModDB:
    """طبقة قاعدة البيانات لنظام الجلب التلقائي"""

    def __init__(self, instance_id: str = None):
        self.instance_id = instance_id or get_instance_id()

    def _supabase_primary_storage(self) -> bool:
        try:
            val = (os.environ.get("SUPABASE_PRIMARY_STORAGE") or "").strip().lower()
            return val in {"1", "true", "yes", "on"}
        except Exception:
            return False

    # ---------- الإعدادات العامة ----------

    def get_config(self, use_cache: bool = True) -> Dict[str, Any]:
        """جلب إعدادات النسخة"""
        now = time.time()
        if use_cache and self.instance_id in _config_cache:
            ts, cached_config = _config_cache[self.instance_id]
            if now - ts < _CACHE_TTL:
                return cached_config

        try:
            from src.agent.supabase_client import supabase_select_one
            result = supabase_select_one(
                "auto_mod_config", "instance_id", self.instance_id,
                fallback_local=lambda: _local_select_rows("auto_mod_config", {"instance_id": self.instance_id}),
            )
            if result:
                _config_cache[self.instance_id] = (now, result)
                return result
        except Exception as e:
            logger.warning(f"Failed to get config: {e}")
        
        # إعدادات افتراضية
        default_config = {
            "instance_id": self.instance_id,
            "auto_fetch_enabled": False,
            "shorts_format": "crop",
            "enhance_enabled": False,
            "add_cta": True,
            "default_content_type": "minecraft_mods",
            "settings": {},
        }
        return default_config

    def save_config(self, config: Dict[str, Any]) -> bool:
        """حفظ إعدادات النسخة"""
        try:
            from src.agent.supabase_client import supabase_upsert
            config["instance_id"] = self.instance_id
            config["updated_at"] = datetime.now(timezone.utc).isoformat()

            primary = self._supabase_primary_storage()
            if primary:
                supabase_upsert(
                    "auto_mod_config",
                    config,
                    key_field="instance_id",
                    fallback_local=lambda payload: _local_upsert_row("auto_mod_config", payload, key_field="instance_id"),
                )
            else:
                _local_upsert_row("auto_mod_config", config, key_field="instance_id")
                supabase_upsert(
                    "auto_mod_config",
                    config,
                    key_field="instance_id",
                    fallback_local=lambda payload: _local_upsert_row("auto_mod_config", payload, key_field="instance_id"),
                )
            
            # تحديث التخزين المؤقت
            _config_cache[self.instance_id] = (time.time(), config)
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False

    def _clear_cache(self):
        """تفريغ التخزين المؤقت للإحصائيات لإجبار الواجهة على التحديث"""
        if self.instance_id in _stats_cache:
            _stats_cache.pop(self.instance_id, None)
            logger.info(f"Stats cache cleared for instance {self.instance_id}")

    # ---------- مصادر الجلب ----------

    def get_sources(self, channel_id: str = None, content_type: str = None) -> List[Dict]:
        """جلب مصادر الجلب"""
        try:
            from .supabase_client import supabase_select
            filters = {"instance_id": self.instance_id}
            if channel_id:
                filters["channel_id"] = channel_id
            if content_type:
                filters["content_type"] = content_type
            result = supabase_select(
                "auto_mod_sources",
                filters,
                fallback_local=lambda: _local_select_rows("auto_mod_sources", filters),
            )
            return result or []
        except Exception as e:
            logger.error(f"Failed to get sources: {e}")
            return []

    def _get_source_by_id(self, source_id: str) -> Optional[Dict[str, Any]]:
        source = next((item for item in self.get_sources() if item.get("id") == source_id), None)
        if source:
            return source
        try:
            from .supabase_client import supabase_select
            records = supabase_select(
                "auto_mod_sources",
                {"id": source_id},
                fallback_local=lambda: _local_select_rows("auto_mod_sources", {"id": source_id}),
            )
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Failed to get source by id: {e}")
            return None

    def _save_existing_source(self, source_id: str, updates: Dict[str, Any]) -> bool:
        try:
            from src.agent.supabase_client import supabase_upsert
            source = self._get_source_by_id(source_id)
            if not source:
                return False
            payload = source.copy()
            payload.update(updates or {})
            payload["id"] = source.get("id", source_id)
            payload["instance_id"] = source.get("instance_id", self.instance_id)
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()

            primary = self._supabase_primary_storage()
            if primary:
                supabase_upsert(
                    "auto_mod_sources",
                    payload,
                    key_field="id",
                    fallback_local=lambda data: _local_upsert_row("auto_mod_sources", data, key_field="id"),
                )
            else:
                _local_upsert_row("auto_mod_sources", payload, key_field="id")
                supabase_upsert(
                    "auto_mod_sources",
                    payload,
                    key_field="id",
                    fallback_local=lambda data: _local_upsert_row("auto_mod_sources", data, key_field="id"),
                )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to save existing source: {e}")
            return False

    def add_source(self, channel_id: str, source_url: str, source_name: str = "",
                   content_type: str = "minecraft_mods", platform: str = "youtube",
                   facecam_settings: Dict = None, source_settings: Dict = None,
                   source_id: Optional[str] = None) -> bool:
        """إضافة مصدر جلب جديد"""
        try:
            from src.agent.supabase_client import supabase_upsert
            data = {
                "id": str(source_id or uuid.uuid4()),
                "instance_id": self.instance_id,
                "channel_id": channel_id,
                "source_url": source_url.strip(),
                "source_name": source_name or self._extract_channel_name(source_url),
                "content_type": content_type,
                "platform": platform,
                "enabled": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            merged_settings = merge_source_settings(facecam_settings, source_settings)
            if merged_settings:
                data["settings"] = merged_settings

            primary = self._supabase_primary_storage()
            if primary:
                supabase_upsert(
                    "auto_mod_sources",
                    data,
                    key_field="id",
                    fallback_local=lambda payload: _local_upsert_row(
                        "auto_mod_sources",
                        payload,
                        key_field="id",
                        on_conflict="instance_id,channel_id,source_url",
                    ),
                    on_conflict="instance_id,channel_id,source_url",
                )
            else:
                _local_upsert_row(
                    "auto_mod_sources",
                    data,
                    key_field="id",
                    on_conflict="instance_id,channel_id,source_url",
                )
                supabase_upsert(
                    "auto_mod_sources",
                    data,
                    key_field="id",
                    fallback_local=lambda payload: _local_upsert_row(
                        "auto_mod_sources",
                        payload,
                        key_field="id",
                        on_conflict="instance_id,channel_id,source_url",
                    ),
                    on_conflict="instance_id,channel_id,source_url",
                )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to add source: {e}")
            return False

    def remove_source(self, source_id: str) -> bool:
        """حذف مصدر جلب"""
        try:
            from src.agent.supabase_client import supabase_delete
            primary = self._supabase_primary_storage()
            if primary:
                supabase_delete(
                    "auto_mod_sources",
                    "id",
                    source_id,
                    fallback_local=lambda key: _local_delete_row("auto_mod_sources", "id", key),
                )
            else:
                _local_delete_row("auto_mod_sources", "id", source_id)
                supabase_delete(
                    "auto_mod_sources",
                    "id",
                    source_id,
                    fallback_local=lambda key: _local_delete_row("auto_mod_sources", "id", key),
                )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to remove source: {e}")
            return False
    
    def delete_processed_videos_for_source(self, source_id: str) -> bool:
        """حذف جميع سجلات الفيديوهات المعالجة لمصدر معين"""
        try:
            from src.agent.supabase_client import supabase_select, supabase_delete
            
            # Get all sources to find which channel(s) this source belongs to
            sources = self.get_sources()
            source = next((s for s in sources if s.get("id") == source_id), None)
            
            if not source:
                logger.warning(f"Source {source_id} not found, skipping processed videos deletion")
                return False
                
            channel_id = source.get("channel_id")
            
            # Delete all processed videos for this channel (since we don't track source_id in processed table)
            records = supabase_select("auto_mod_processed", {
                "instance_id": self.instance_id,
                "channel_id": channel_id,
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "instance_id": self.instance_id,
                "channel_id": channel_id,
            })) or []
            
            deleted_count = 0
            for rec in records:
                _local_delete_row("auto_mod_processed", "id", rec["id"])
                supabase_delete(
                    "auto_mod_processed",
                    "id",
                    rec["id"],
                    fallback_local=lambda key: _local_delete_row("auto_mod_processed", "id", key),
                )
                deleted_count += 1
                
            if deleted_count > 0:
                logger.info(f"Deleted {deleted_count} processed video records for source {source_id}")
                
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to delete processed videos for source {source_id}: {e}")
            return False

    def toggle_source(self, source_id: str, enabled: bool) -> bool:
        """تفعيل/تعطيل مصدر"""
        try:
            return self._save_existing_source(source_id, {"enabled": enabled})
        except Exception as e:
            logger.error(f"Failed to toggle source: {e}")
            return False

    def update_source_channel(self, source_id: str, new_channel_id: str) -> bool:
        """تغيير القناة المستهدفة لمصدر"""
        try:
            return self._save_existing_source(source_id, {"channel_id": new_channel_id})
        except Exception as e:
            logger.error(f"Failed to update source channel: {e}")
            return False

    def update_source_platform(self, source_id: str, new_platform: str) -> bool:
        """تغيير نوع الفيديوهات (المدة) المستهدفة لمصدر"""
        try:
            return self._save_existing_source(source_id, {"platform": new_platform})
        except Exception as e:
            logger.error(f"Failed to update source platform: {e}")
            return False

    def update_source_facecam(self, source_id: str, facecam_settings: Dict) -> bool:
        """تحديث إعدادات الفيس كام لمصدر"""
        return self.update_source_settings(source_id, facecam_settings)

    def update_source_settings(self, source_id: str, settings_update: Dict) -> bool:
        """تحديث إعدادات المصدر مع الحفاظ على الإعدادات الأخرى"""
        try:
            source = self._get_source_by_id(source_id)
            if not source:
                return False
            merged_settings = merge_source_settings(
                source.get("settings") if source else {},
                settings_update,
            )
            return self._save_existing_source(source_id, {"settings": merged_settings})
        except Exception as e:
            logger.error(f"Failed to update source settings: {e}")
            return False

    # ---------- الجدولة ----------

    def get_schedule(self, channel_id: str, content_type: str = "minecraft_mods") -> Optional[Dict]:
        """جلب إعدادات الجدولة لقناة"""
        try:
            from .supabase_client import supabase_select
            filters = {
                "instance_id": self.instance_id,
                "channel_id": channel_id,
                "content_type": content_type,
            }
            result = supabase_select(
                "auto_mod_schedule",
                filters,
                fallback_local=lambda: _local_select_rows("auto_mod_schedule", filters),
            )
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Failed to get schedule: {e}")
            return None

    def get_all_schedules(self) -> List[Dict]:
        """جلب جميع جداول النشر للنسخة"""
        try:
            from .supabase_client import supabase_select
            result = supabase_select(
                "auto_mod_schedule",
                {"instance_id": self.instance_id},
                fallback_local=lambda: _local_select_rows("auto_mod_schedule", {"instance_id": self.instance_id}),
            )
            return result or []
        except Exception as e:
            logger.error(f"Failed to get schedules: {e}")
            return []

    def _get_schedule_by_id(self, sch_id: str) -> Optional[Dict[str, Any]]:
        schedule = next((item for item in self.get_all_schedules() if item.get("id") == sch_id), None)
        if schedule:
            return schedule
        try:
            from .supabase_client import supabase_select
            records = supabase_select(
                "auto_mod_schedule",
                {"id": sch_id},
                fallback_local=lambda: _local_select_rows("auto_mod_schedule", {"id": sch_id}),
            )
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Failed to get schedule by id: {e}")
            return None

    def _save_existing_schedule(self, schedule: Dict[str, Any], updates: Dict[str, Any]) -> bool:
        try:
            from src.agent.supabase_client import supabase_upsert
            if not schedule or not schedule.get("id"):
                return False
            payload = schedule.copy()
            payload.update(updates or {})
            payload["instance_id"] = schedule.get("instance_id", self.instance_id)
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            primary = self._supabase_primary_storage()
            if primary:
                supabase_upsert(
                    "auto_mod_schedule",
                    payload,
                    key_field="id",
                    fallback_local=lambda data: _local_upsert_row("auto_mod_schedule", data, key_field="id"),
                )
            else:
                _local_upsert_row("auto_mod_schedule", payload, key_field="id")
                supabase_upsert(
                    "auto_mod_schedule",
                    payload,
                    key_field="id",
                    fallback_local=lambda data: _local_upsert_row("auto_mod_schedule", data, key_field="id"),
                )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to save existing schedule: {e}")
            return False

    def save_schedule(self, channel_id: str, content_type: str = "minecraft_mods",
                      interval_minutes: int = 120, daily_limit: int = 5,
                      publish_hours: Dict = None) -> bool:
        """حفظ إعدادات الجدولة"""
        try:
            from src.agent.supabase_client import supabase_upsert
            data = {
                "id": str(uuid.uuid4()),
                "instance_id": self.instance_id,
                "channel_id": channel_id,
                "content_type": content_type,
                "publish_interval_minutes": interval_minutes,
                "daily_limit": daily_limit,
                "publish_hours": publish_hours or {"start": 0, "end": 24},
                "enabled": True,
                "next_publish_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            primary = self._supabase_primary_storage()
            if primary:
                supabase_upsert(
                    "auto_mod_schedule",
                    data,
                    key_field="id",
                    fallback_local=lambda payload: _local_upsert_row(
                        "auto_mod_schedule",
                        payload,
                        key_field="id",
                        on_conflict="instance_id,channel_id,content_type",
                    ),
                    on_conflict="instance_id,channel_id,content_type",
                )
            else:
                _local_upsert_row(
                    "auto_mod_schedule",
                    data,
                    key_field="id",
                    on_conflict="instance_id,channel_id,content_type",
                )
                supabase_upsert(
                    "auto_mod_schedule",
                    data,
                    key_field="id",
                    fallback_local=lambda payload: _local_upsert_row(
                        "auto_mod_schedule",
                        payload,
                        key_field="id",
                        on_conflict="instance_id,channel_id,content_type",
                    ),
                    on_conflict="instance_id,channel_id,content_type",
                )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to save schedule: {e}")
            return False

    def update_last_publish(self, channel_id: str, content_type: str = "minecraft_mods") -> bool:
        """تحديث آخر وقت نشر"""
        try:
            schedule = self.get_schedule(channel_id, content_type)
            if not schedule:
                return False
            now = datetime.now(timezone.utc)
            try:
                interval = int(schedule.get("publish_interval_minutes", 120) or 120)
            except Exception:
                interval = 120
            return self._save_existing_schedule(schedule, {
                "last_publish_at": now.isoformat(),
                "next_publish_at": (now + timedelta(minutes=interval)).isoformat(),
                "total_published": (schedule.get("total_published", 0) or 0) + 1,
            })
        except Exception as e:
            logger.error(f"Failed to update last publish: {e}")
            return False

    def count_published_today(self, channel_id: str, content_type: str = "minecraft_mods") -> int:
        """عدّ الفيديوهات المنشورة اليوم فعلياً لهذا الجدول بالاعتماد على السجلات النهائية."""
        try:
            from .supabase_client import supabase_select

            filters = {
                "instance_id": self.instance_id,
                "channel_id": channel_id,
                "content_type": content_type,
                "status": "published",
            }
            records = supabase_select(
                "auto_mod_processed",
                filters,
                fallback_local=lambda: _local_select_rows("auto_mod_processed", filters),
            ) or []

            today_utc = datetime.now(timezone.utc).date()
            total = 0
            for rec in records:
                published_dt = _parse_datetime_utc(rec.get("updated_at"))
                if published_dt and published_dt.date() == today_utc:
                    total += 1
            return total
        except Exception as e:
            logger.error(f"Failed to count published today: {e}")
            return 0

    def update_next_publish_after_attempt(self, channel_id: str, content_type: str = "minecraft_mods", *, published: bool) -> bool:
        try:
            from src.agent.supabase_client import supabase_upsert
            schedule = self.get_schedule(channel_id, content_type)
            if not schedule:
                return False

            now = datetime.now(timezone.utc)
            try:
                interval = int(schedule.get("publish_interval_minutes", 120) or 120)
            except Exception:
                interval = 120

            data = schedule.copy()
            
            # If published successfully, wait for the full interval.
            # If failed, retry in 10 minutes instead of waiting for the full interval.
            next_interval = interval if published else 10
            
            next_dt = now + timedelta(minutes=next_interval)
            existing_next_dt = _parse_datetime_utc(schedule.get("next_publish_at"))
            
            # Safeguard: if not published but original schedule is still in the future, don't bring it forward
            if not published and existing_next_dt and existing_next_dt > next_dt:
                next_dt = existing_next_dt

            if published:
                data["last_publish_at"] = now.isoformat()
                data["total_published"] = (schedule.get("total_published", 0) or 0) + 1
                
            data["next_publish_at"] = next_dt.isoformat()
            data["updated_at"] = now.isoformat()

            _local_upsert_row(
                "auto_mod_schedule",
                data,
                key_field="id",
                on_conflict="instance_id,channel_id,content_type",
            )
            supabase_upsert(
                "auto_mod_schedule",
                data,
                key_field="id",
                fallback_local=lambda payload: _local_upsert_row(
                    "auto_mod_schedule",
                    payload,
                    key_field="id",
                    on_conflict="instance_id,channel_id,content_type",
                ),
                on_conflict="instance_id,channel_id,content_type",
            )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to update next publish after attempt: {e}")
            return False
    def delete_schedule(self, sch_id: str) -> bool:
        """حذف جدول نشر"""
        try:
            from src.agent.supabase_client import supabase_delete
            _local_delete_row("auto_mod_schedule", "id", sch_id)
            supabase_delete(
                "auto_mod_schedule",
                "id",
                sch_id,
                fallback_local=lambda key: _local_delete_row("auto_mod_schedule", "id", key),
            )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to delete schedule: {e}")
            return False

    def toggle_schedule(self, sch_id: str, enabled: bool) -> bool:
        """تبديل حالة جدول نشر"""
        try:
            schedule = self._get_schedule_by_id(sch_id)
            if not schedule:
                return False
            return self._save_existing_schedule(schedule, {"enabled": enabled})
        except Exception as e:
            logger.error(f"Failed to toggle schedule: {e}")
            return False

    # ---------- الفيديوهات المعالجة ----------

    def is_video_processed(self, source_video_id: str, channel_id: str) -> bool:
        """توافقاً مع السلوك الجديد: يعتبر الفيديو منتهياً فقط إذا كان منشوراً"""
        return self.is_video_published(source_video_id, channel_id)

    def get_video_process_record(self, source_video_id: str, channel_id: str) -> Optional[Dict[str, Any]]:
        try:
            from .supabase_client import supabase_select
            records = supabase_select("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }))
            if not records:
                return None
            return records[0]
        except Exception as e:
            logger.error(f"Failed to check processed: {e}")
            return None

    def get_video_process_state(self, source_video_id: str, channel_id: str) -> Tuple[Optional[str], Optional[datetime]]:
        rec = self.get_video_process_record(source_video_id, channel_id) or {}
        status = (rec.get("status") or "").strip().lower() or None
        updated_at = _parse_datetime_utc(rec.get("updated_at"))
        return status, updated_at

    def is_video_published(self, source_video_id: str, channel_id: str) -> bool:
        status, _ = self.get_video_process_state(source_video_id, channel_id)
        return status == "published"

    def is_video_locked(self, source_video_id: str, channel_id: str, *, stale_minutes: Optional[int] = None) -> bool:
        status, updated_at = self.get_video_process_state(source_video_id, channel_id)
        if status != "processing":
            return False
        if not updated_at:
            return True
        active_minutes = _processing_lock_stale_minutes() if stale_minutes is None else max(5, int(stale_minutes))
        try:
            return (datetime.now(timezone.utc) - updated_at) <= timedelta(minutes=active_minutes)
        except Exception:
            return True

    def mark_video_processing(self, source_video_id: str, channel_id: str,
                               content_type: str = "minecraft_mods", title: str = "") -> bool:
        """تسجيل فيديو كقيد المعالجة (لمنع التعارض)"""
        try:
            from src.agent.supabase_client import supabase_upsert
            payload = {
                "id": str(uuid.uuid4()),
                "instance_id": self.instance_id,
                "source_video_id": source_video_id,
                "channel_id": channel_id,
                "content_type": content_type,
                "status": "processing",
                "title": title,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _local_upsert_row(
                "auto_mod_processed",
                payload,
                key_field="id",
                on_conflict="source_video_id,channel_id",
            )
            ok = supabase_upsert(
                "auto_mod_processed",
                payload,
                key_field="id",
                fallback_local=lambda data: _local_upsert_row(
                    "auto_mod_processed",
                    data,
                    key_field="id",
                    on_conflict="source_video_id,channel_id",
                ),
                on_conflict="source_video_id,channel_id",
            )
            if not ok:
                logger.error(
                    f"Failed to persist processing claim for video={source_video_id[:20]}... channel={channel_id[:10]}..."
                )
                return False
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to mark processing: {e}")
            return False

    def _mark_video_terminal_status(self, source_video_id: str, channel_id: str,
                                    *, status: str, youtube_url: str = "",
                                    error_message: str = "") -> bool:
        try:
            from src.agent.supabase_client import supabase_select, supabase_upsert

            records = supabase_select("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }))
            rec = records[0] if records else {}

            if not rec:
                logger.warning(
                    f"Missing auto_mod_processed row for video={source_video_id[:20]}... channel={channel_id[:10]}...; creating terminal status '{status}'."
                )

            payload = {
                "id": rec.get("id", str(uuid.uuid4())),
                "instance_id": rec.get("instance_id", self.instance_id),
                "source_video_id": source_video_id,
                "channel_id": channel_id,
                "content_type": rec.get("content_type", "minecraft_mods"),
                "title": rec.get("title", ""),
                "status": status,
                "youtube_url": youtube_url if status == "published" else "",
                "error_message": (error_message[:500] if error_message else "") if status == "failed" else "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _local_upsert_row(
                "auto_mod_processed",
                payload,
                key_field="id",
                on_conflict="source_video_id,channel_id",
            )
            ok = supabase_upsert(
                "auto_mod_processed",
                payload,
                key_field="id",
                fallback_local=lambda data: _local_upsert_row(
                    "auto_mod_processed",
                    data,
                    key_field="id",
                    on_conflict="source_video_id,channel_id",
                ),
                on_conflict="source_video_id,channel_id",
            )
            if not ok:
                logger.error(
                    f"Failed to persist terminal status '{status}' for video={source_video_id[:20]}... channel={channel_id[:10]}..."
                )
                return False

            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to mark {status}: {e}")
            return False

    def mark_video_published(self, source_video_id: str, channel_id: str,
                              youtube_url: str = "") -> bool:
        """تسجيل فيديو كمنشور"""
        return self._mark_video_terminal_status(
            source_video_id,
            channel_id,
            status="published",
            youtube_url=youtube_url,
        )

    def mark_video_failed(self, source_video_id: str, channel_id: str,
                           error_message: str = "") -> bool:
        """تسجيل فيديو كفاشل"""
        return self._mark_video_terminal_status(
            source_video_id,
            channel_id,
            status="failed",
            error_message=error_message,
        )

    def release_video_processing(self, source_video_id: str, channel_id: str) -> bool:
        """تحرير قفل المعالجة عند الإيقاف الآمن قبل الوصول إلى حالة نهائية."""
        try:
            from src.agent.supabase_client import supabase_select, supabase_delete

            records = supabase_select("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }))
            rec = records[0] if records else None
            if not rec:
                return True
            if (rec.get("status") or "").strip().lower() != "processing":
                return True
            _local_delete_row("auto_mod_processed", "id", rec["id"])
            supabase_delete(
                "auto_mod_processed",
                "id",
                rec["id"],
                fallback_local=lambda key: _local_delete_row("auto_mod_processed", "id", key),
            )
            self._clear_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to release processing: {e}")
            return False

    def touch_video_processing(self, source_video_id: str, channel_id: str) -> bool:
        """تحديث نبضة فيديو قيد المعالجة لمنع اعتباره متوقفاً أثناء مهام FFmpeg الطويلة."""
        try:
            from src.agent.supabase_client import supabase_select, supabase_upsert
            records = supabase_select("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "source_video_id": source_video_id,
                "channel_id": channel_id,
            }))
            rec = records[0] if records else None
            if not rec:
                return False
            if (rec.get("status") or "").strip().lower() != "processing":
                return False
            rec["updated_at"] = datetime.now(timezone.utc).isoformat()
            _local_upsert_row(
                "auto_mod_processed",
                rec,
                key_field="id",
                on_conflict="source_video_id,channel_id",
            )
            ok = supabase_upsert(
                "auto_mod_processed",
                rec,
                key_field="id",
                fallback_local=lambda data: _local_upsert_row(
                    "auto_mod_processed",
                    data,
                    key_field="id",
                    on_conflict="source_video_id,channel_id",
                ),
                on_conflict="source_video_id,channel_id",
            )
            if ok:
                self._clear_cache()
            return bool(ok)
        except Exception as e:
            logger.debug(f"Failed to touch processing heartbeat: {e}")
            return False

    def reset_stale_processing(self, *, stale_minutes: Optional[int] = None, force_reset_all: bool = False) -> int:
        """إعادة تعيين أي فيديوهات 'قيد المعالجة' تابعة لهذه النسخة تم مقاطعتها"""
        try:
            from src.agent.supabase_client import supabase_select, supabase_delete
            stale_threshold_minutes = _processing_lock_stale_minutes() if stale_minutes is None else max(5, int(stale_minutes))
            now_utc = datetime.now(timezone.utc)
            records = supabase_select("auto_mod_processed", {
                "instance_id": self.instance_id,
                "status": "processing"
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "instance_id": self.instance_id,
                "status": "processing",
            }))
            count = 0
            kept_recent = 0
            for rec in records:
                if not force_reset_all:
                    updated_at = _parse_datetime_utc(rec.get("updated_at"))
                    if updated_at and (now_utc - updated_at) <= timedelta(minutes=stale_threshold_minutes):
                        kept_recent += 1
                        continue
                _local_delete_row("auto_mod_processed", "id", rec["id"])
                supabase_delete(
                    "auto_mod_processed",
                    "id",
                    rec["id"],
                    fallback_local=lambda key: _local_delete_row("auto_mod_processed", "id", key),
                )
                count += 1
            if count > 0:
                if force_reset_all:
                    logger.info(f"🔄 Force-reset {count} processing locks on boot for instance {self.instance_id}")
                else:
                    logger.info(f"🔄 Reset {count} stale processing locks for instance {self.instance_id}")
                self._clear_cache()
            if kept_recent > 0 and not force_reset_all:
                logger.info(
                    f"⏸ Kept {kept_recent} active processing locks (stale threshold: {stale_threshold_minutes} min) for instance {self.instance_id}"
                )
            return count
        except Exception as e:
            logger.error(f"Failed to reset stale processing: {e}")
            return 0

    def get_stats(self, use_cache: bool = True) -> Dict[str, Any]:
        """إحصائيات النظام"""
        now = time.time()
        if use_cache and self.instance_id in _stats_cache:
            ts, cached_stats = _stats_cache[self.instance_id]
            if now - ts < _CACHE_TTL:
                return cached_stats

        try:
            from .supabase_client import supabase_select
            all_processed = supabase_select("auto_mod_processed", {
                "instance_id": self.instance_id
            }, fallback_local=lambda: _local_select_rows("auto_mod_processed", {
                "instance_id": self.instance_id,
            })) or []
            sources = self.get_sources()
            schedules = self.get_all_schedules()

            published = sum(1 for r in all_processed if r.get("status") == "published")
            failed = sum(1 for r in all_processed if r.get("status") == "failed")
            processing = sum(1 for r in all_processed if r.get("status") == "processing")

            # جلب عدد القنوات
            total_channels = 0
            try:
                from src.bot.channel_manager import ChannelManager
                _, total_channels = ChannelManager().list_channels(limit=1)
            except Exception as e:
                logger.debug(f"Error getting channels count for stats: {e}")

            stats = {
                "total_channels": total_channels,
                "total_sources": len(sources),
                "total_schedules": len(schedules),
                "total_processed": len(all_processed),
                "published": published,
                "failed": failed,
                "processing": processing,
                "instance_id": self.instance_id,
                "cached_at": datetime.fromtimestamp(now).strftime("%H:%M:%S")
            }
            
            _stats_cache[self.instance_id] = (now, stats)
            return stats
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {"error": str(e)}

    # ---------- أدوات مساعدة ----------

    @staticmethod
    def _extract_channel_name(url: str) -> str:
        """استخراج اسم مختصر من رابط المصدر (يوتيوب/فيسبوك)"""
        import re
        if "facebook.com" in (url or ""):
            m = re.search(r"facebook\.com/([^/?\s]+)", url)
            if m:
                return m.group(1)
        match = re.search(r"youtube\.com/@([^/?\s]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"youtube\.com/channel/([^/?\s]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"youtube\.com/c/([^/?\s]+)", url)
        if match:
            return match.group(1)
        return url[:50]


# ==================== محرك الجلب ====================

class AutoModFetcher:
    """محرك الجلب التلقائي لفيديوهات المودات"""

    def __init__(self, instance_id: str = None):
        self.instance_id = instance_id or get_instance_id()
        self.db = AutoModDB(self.instance_id)

    async def fetch_videos_from_source(self, source_url: str, items_range: str = "1-10", platform: str = "youtube") -> List[Dict]:
        """
        جلب أحدث الفيديوهات من مصدر (يوتيوب/فيسبوك)
        يعتمد الآن حصرياً على yt-dlp مع retry لتجاوز الحظر
        """
        if (platform or "").strip().lower() == "container" or (source_url or "").strip().lower().startswith("container:"):
            try:
                return await self._fetch_from_container(source_url, items_range)
            except Exception as e:
                logger.error(f"Failed to fetch from container source {source_url}: {e}")
                return []

        from src.agent.supabase_storage import is_source_rate_limited
        if is_source_rate_limited(source_url):
            logger.info(f"❄️ [CoolDown] Skipping source because it's rate-limited: {source_url}")
            return []

        try:
            # === جلب حصري عبر yt-dlp مع retry ===
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._fetch_sync, source_url, items_range, platform)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch from {source_url}: {e}")
            return []

    @staticmethod
    def _parse_items_range(items_range: str) -> Tuple[int, int]:
        raw = (items_range or "").strip()
        if not raw:
            return (1, 10)
        try:
            parts = raw.split("-", 1)
            start = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 else start
            start = max(1, start)
            end = max(start, end)
            return (start, end)
        except Exception:
            return (1, 10)

    async def _fetch_from_container(self, source_url: str, items_range: str) -> List[Dict]:
        container_id = (source_url or "").strip()
        if container_id.lower().startswith("container:"):
            container_id = container_id.split(":", 1)[1].strip()
        if not container_id:
            return []
        from src.agent.supabase_storage import list_container_videos
        videos = list_container_videos(container_id) or []
        
        # ترتيب الفيديوهات حسب تاريخ الإنشاء تصاعدياً (الأقدم أولاً - FIFO)
        videos.sort(key=lambda x: str(x.get("created_at", "")), reverse=False)
        
        start, end = self._parse_items_range(items_range)
        subset = videos[start - 1:end]
        out: List[Dict[str, Any]] = []
        for v in subset:
            vid_id = v.get("id")
            if not vid_id:
                continue
            out.append({
                "id": vid_id,
                "title": v.get("title", ""),
                "description": "",
                "url": f"container://{vid_id}",
                "duration": None,
                "view_count": None,
                "upload_date": v.get("created_at"),
            })
        return out

    # (تمت إزالة نظام YouTube Data API القديم. الجلب يعتمد الآن على yt-dlp فقط.)

    def _fetch_sync(self, source_url: str, items_range: str = "1-10", platform: str = "youtube") -> List[Dict]:
        """جلب الفيديوهات بشكل متزامن عبر yt-dlp مع retry وتخطي القيود بذكاء"""

        source_url = _normalize_facebook_source_url(source_url, platform)
        cookies_info = _resolve_cookiefile_details()

        # تحسين رابط المصدر لليوتيوب شورتس
        if platform == "youtube_shorts" and ("youtube.com" in source_url or "youtu.be" in source_url):
            if "@" in source_url or "/c/" in source_url or "/channel/" in source_url or "/user/" in source_url:
                if not source_url.endswith("/shorts"):
                    base_url = source_url.split("?")[0].rstrip("/")
                    if not base_url.endswith("/shorts"):
                        source_url = base_url + "/shorts"
                        logger.info(f"🔗 Targeted source URL adjusted for Shorts: {source_url}")

        url_lc = (source_url or "").lower()
        header_overrides = {}
        if "facebook.com" in url_lc or platform.startswith("facebook"):
            header_overrides = {
                "Origin": "https://www.facebook.com",
                "Referer": "https://www.facebook.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }

        ydl_opts = _build_yt_opts({
            "extract_flat": True,
            "playlist_items": items_range,
            "ignoreerrors": True,
            "http_headers": header_overrides,
        }, cookies_path=cookies_info)

        # === Retry مع backoff ذكي ===
        max_retries = 3
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                videos = self._fetch_sync_attempt(source_url, items_range, platform, ydl_opts=ydl_opts)
                if videos is not None:
                    return videos
            except Exception as e:
                last_error = e
                error_msg = str(e)

                if "impersonate target" in error_msg.lower() and "is not available" in error_msg.lower():
                    logger.warning(f"⚠️ Impersonate target not available in this environment. Retrying without it...")
                    os.environ["YTDLP_SKIP_IMPERSONATE"] = "1"
                    ydl_opts.pop("impersonate", None)
                    continue

                if "rate-limited" in error_msg.lower() or "too many requests" in error_msg.lower():
                    from src.agent.supabase_storage import mark_source_rate_limited
                    mark_source_rate_limited(source_url, duration=3600)

                    if attempt < max_retries:
                        wait_time = 30 + (20 * attempt)
                        logger.warning(f"⏳ YouTube Rate limited! Waiting {wait_time}s before retrying...")
                        time.sleep(wait_time)
                        continue
                    break

                is_retryable, error_type = _is_retryable_ytdlp_error(error_msg)
                if _is_youtube_botcheck_error(e) and not is_retryable:
                    is_retryable = True
                    error_type = "403_forbidden"

                if not is_retryable or attempt >= max_retries:
                    if _is_youtube_botcheck_error(e):
                        global _YT_BOTCHECK_HINT_SHOWN
                        if not _YT_BOTCHECK_HINT_SHOWN:
                            _YT_BOTCHECK_HINT_SHOWN = True
                            logger.error(
                                "🚫 YouTube bot-check detected! %s | %s",
                                _build_youtube_botcheck_hint(cookies_info),
                                _build_ytdlp_runtime_diagnostics("fetch_botcheck", cookies_info=cookies_info),
                            )
                    logger.error(f"yt-dlp fetch error (attempt {attempt + 1}/{max_retries + 1}): {error_msg}")
                    break

                logger.warning(f"yt-dlp retryable error ({error_type}), attempt {attempt + 1}/{max_retries + 1}: {error_msg}")
                ydl_opts = _build_retry_opts(ydl_opts, attempt + 1, error_type)

                wait_time = min(5 * (2 ** attempt), 30)
                time.sleep(wait_time)

        # === فشل نهائي — تسجيل و تحديث yt-dlp ===
        if last_error:
            logger.error(f"yt-dlp fetch error after {max_retries} retries: {last_error}")
            if (platform or "").startswith("facebook") or "facebook.com" in (source_url or "").lower():
                global _FB_HINT_SHOWN
                if not _FB_HINT_SHOWN:
                    _FB_HINT_SHOWN = True
                    logger.error(
                        "📘 Facebook fetch failed. On Render you usually need Cookies and/or a Proxy. "
                        "Set YTDLP_COOKIES_B64 or YTDLP_COOKIES_PATH, and optionally YTDLP_PROXY. "
                        "Some links may stop working due to Facebook changes."
                    )
            try:
                from src.agent.error_tracker import get_error_tracker
                et = get_error_tracker()
                et.record_error("download", "fetch_error", str(last_error))

                if et.consecutive_fails("download") >= 5:
                    logger.warning("🔄 Too many download failures. Attempting yt-dlp auto-update...")
                    try:
                        result = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                            capture_output=True, text=True, timeout=120
                        )
                        if result.returncode == 0:
                            logger.info("✅ yt-dlp updated successfully!")
                            et.record_success("download")
                        else:
                            logger.warning(f"⚠️ yt-dlp update failed: {result.stderr[:200]}")
                    except Exception as upd_err:
                        logger.warning(f"⚠️ yt-dlp auto-update error: {upd_err}")
            except Exception:
                pass

        return []

    def _fetch_sync_attempt(self, source_url: str, items_range: str, platform: str, ydl_opts: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """محاولة واحدة لجلب الفيديوهات عبر yt-dlp"""
        import yt_dlp

        if ydl_opts is None:
            url_lc = (source_url or "").lower()
            header_overrides = {}
            if "facebook.com" in url_lc or platform.startswith("facebook"):
                header_overrides = {
                    "Origin": "https://www.facebook.com",
                    "Referer": "https://www.facebook.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                }

            ydl_opts = _build_yt_opts({
                "extract_flat": True,
                "playlist_items": items_range,
                "ignoreerrors": True,
                # لا نستخدم noplaylist هنا لأنه يمنع جلب قائمة فيديوهات القناة
                "http_headers": header_overrides,
            })
        else:
            ydl_opts = dict(ydl_opts)

        if os.environ.get("YTDLP_SKIP_IMPERSONATE") == "1":
            ydl_opts.pop("impersonate", None)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(source_url, download=False)
            except Exception as e:
                if "impersonate target" in str(e).lower() and "is not available" in str(e).lower():
                    # If it fails here, we raise to trigger the retry logic in _fetch_sync
                    raise
                raise 
            
            if not info:
                return []

            entries = info.get("entries") or []
            videos = []
            for entry in entries:
                if not entry:
                    continue
                vid_id = entry.get("id") or entry.get("url", "")
                if not vid_id:
                    continue
                fb_url = entry.get("webpage_url") or entry.get("url") or ""
                if platform == "facebook_reels" and fb_url and (not _is_facebook_reel_url(fb_url)):
                    continue

                duration = entry.get("duration")
                if duration is not None:
                    logger.debug(f"ℹ️ Video {vid_id} duration: {duration}s")

                videos.append({
                    "id": vid_id,
                    "title": entry.get("title", ""),
                    "description": entry.get("description", ""),
                    "url": entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}",
                    "duration": duration,
                    "view_count": entry.get("view_count"),
                    "upload_date": entry.get("upload_date"),
                })
            return videos

    async def download_video(self, video_url: str, output_dir: str, max_duration: Optional[int] = None) -> Optional[str]:
        """تنزيل فيديو من URL مع تحديد مدة قصوى اختيارية"""
        try:
            output_dir = _ensure_runtime_dir(output_dir)
            if (video_url or "").strip().lower().startswith("container://"):
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._download_container_sync, video_url, output_dir)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, self._download_sync, video_url, output_dir, max_duration
            )
            return result
        except Exception as e:
            logger.error(f"Failed to download {video_url}: {e}")
            return None

    def _download_container_sync(self, video_url: str, output_dir: str) -> Optional[str]:
        try:
            vid_id = (video_url or "").split("container://", 1)[1].strip()
        except Exception:
            return None
        if not vid_id:
            return None
        try:
            from pathlib import Path as _Path
            from src.agent.supabase_storage import get_container_video, download_container_video_to_file

            rec = get_container_video(vid_id) or {}
            try:
                logger.info(
                    f"📥 [AutoMod] Container download: video={vid_id[:20]}..., provider={(rec.get('storage_provider') or '')}, "
                    f"bucket={(rec.get('storage_bucket') or '')}, storage_path={(str(rec.get('storage_path') or '')[:60])}, "
                    f"local_path={(str(rec.get('local_path') or '')[:60])}"
                )
            except Exception:
                pass
            ext = ".mp4"
            candidate = rec.get("storage_path") or rec.get("local_path") or ""
            try:
                cand_ext = _Path(str(candidate)).suffix
                if cand_ext:
                    ext = cand_ext
            except Exception:
                pass

            os.makedirs(output_dir, exist_ok=True)
            dest = os.path.join(output_dir, f"{vid_id}{ext}")
            ok = download_container_video_to_file(vid_id, dest)
            if ok and os.path.exists(dest):
                return dest
            try:
                logger.warning(
                    f"❌ [AutoMod] Container download failed: video={vid_id[:20]}..., ok={ok}, dest_exists={os.path.exists(dest)}"
                )
            except Exception:
                pass
            return None
        except Exception as e:
            logger.error(f"Failed to download container video {vid_id}: {e}")
            return None

    def _download_sync(self, video_url: str, output_dir: str, max_duration: Optional[int] = None) -> Optional[str]:
        """تنزيل بشكل متزامن — بأعلى جودة ممكنة مع FFmpeg merge وفلترة المدة وretry"""
        output_dir = _ensure_runtime_dir(output_dir)
        cookies_info = _resolve_cookiefile_details()
        normalized_video_url = _normalize_youtube_watch_url(video_url)
        if normalized_video_url and normalized_video_url != video_url:
            logger.info(
                "🔁 Normalized YouTube download URL for yt-dlp: %s -> %s",
                video_url,
                normalized_video_url,
            )
            video_url = normalized_video_url
        max_retries = 3
        last_error = None
        remote_settings = _resolve_remote_downloader_settings()
        remote_worker_eligible = bool(_extract_youtube_video_id(video_url)) and remote_settings.get("enabled")
        remote_worker_tried = False

        if remote_worker_eligible and remote_settings.get("prefer_remote"):
            remote_worker_tried = True
            logger.info(
                "🌐 Trying remote downloader worker first for YouTube URL | host=%s | %s",
                remote_settings.get("host"),
                _build_ytdlp_runtime_diagnostics("download_remote_first", cookies_info=cookies_info),
            )
            remote_result = self._download_via_remote_worker(video_url, output_dir, max_duration=max_duration)
            if remote_result:
                return remote_result
            logger.warning(
                "⚠️ Remote downloader worker returned no file. Falling back to local yt-dlp. | host=%s",
                remote_settings.get("host"),
            )

        for attempt in range(1, max_retries + 1):
            try:
                result = self._download_sync_attempt(video_url, output_dir, max_duration)
                if result:
                    return result
                # إذا لم يرجع ملف لكن بدون استثناء
                if attempt < max_retries:
                    wait_time = 5 * attempt
                    logger.warning(f"⏳ Download attempt {attempt}/{max_retries} returned None. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                return None
            except Exception as e:
                last_error = e
                error_code, error_reason = _classify_download_error(e)
                if _is_youtube_botcheck_error(e):
                    if remote_worker_eligible and not remote_worker_tried:
                        remote_worker_tried = True
                        logger.warning(
                            "🚫 YouTube bot-check detected (%s): %s. Attempting fallback via remote downloader worker... | %s",
                            error_code,
                            error_reason,
                            _build_ytdlp_runtime_diagnostics("download_botcheck_remote_fallback", cookies_info=cookies_info),
                        )
                        remote_result = self._download_via_remote_worker(video_url, output_dir, max_duration=max_duration)
                        if remote_result:
                            return remote_result

                    # Bot-check — محاولة التجاوز عبر Invidious/Piped أولاً ثم Cobalt
                    if _extract_youtube_video_id(video_url):
                        logger.warning(
                            "🚫 YouTube bot-check detected (%s): %s. Attempting Invidious/Piped fallback... | %s",
                            error_code,
                            error_reason,
                            _build_ytdlp_runtime_diagnostics("download_botcheck_invidious", cookies_info=cookies_info),
                        )
                        inv_result = self._download_via_invidious_piped(video_url, output_dir)
                        if inv_result:
                            return inv_result

                    cobalt_api_url, _, _ = _resolve_cobalt_api_settings()
                    cobalt_disabled, cobalt_disable_reason = _get_cobalt_fallback_disable_state(cobalt_api_url)
                    if not cobalt_disabled:
                        logger.warning(
                            "🚫 Invidious/Piped failed. Attempting fallback via Cobalt API... | %s",
                            _build_ytdlp_runtime_diagnostics("download_botcheck", cookies_info=cookies_info),
                        )
                        cobalt_result = self._download_via_cobalt(video_url, output_dir)
                        if cobalt_result:
                            return cobalt_result
                    elif cobalt_disable_reason:
                        logger.warning(
                            "⏭️ Skipping Cobalt API fallback for host=%s: %s",
                            _safe_url_host(cobalt_api_url),
                            cobalt_disable_reason,
                        )
                    
                    global _YT_BOTCHECK_HINT_SHOWN
                    if not _YT_BOTCHECK_HINT_SHOWN:
                        _YT_BOTCHECK_HINT_SHOWN = True
                        logger.error(
                            "🚫 All fallbacks failed (Invidious/Piped + Cobalt). %s | %s",
                            _build_youtube_botcheck_hint(cookies_info),
                            _build_ytdlp_runtime_diagnostics("download_all_fallbacks_failed", cookies_info=cookies_info),
                        )
                    logger.error(f"yt-dlp download error ({error_code}): {error_reason} | raw={e}")
                    return None

                if attempt < max_retries:
                    if "impersonate target" in str(e).lower() and "is not available" in str(e).lower():
                        logger.warning(f"⚠️ Impersonate target not available for download. Retrying without it...")
                        os.environ["YTDLP_SKIP_IMPERSONATE"] = "1"
                        continue

                    if "rate-limited" in str(e).lower() or "too many requests" in str(e).lower():
                        wait_time = 30 + (20 * attempt)
                        logger.warning(f"⏳ Download Rate limited! Waiting {wait_time}s before retrying...")
                    else:
                        wait_time = 5 * (3 ** (attempt - 1))  # 5s, 15s, 45s
                        logger.warning(
                            f"⏳ Download attempt {attempt}/{max_retries} failed ({error_code}): {error_reason}. "
                            f"Retrying in {wait_time}s... | raw={e}"
                        )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"yt-dlp download error after {max_retries} retries ({error_code}): {error_reason} | raw={e}"
                    )

        return None

    def _download_via_remote_worker(self, video_url: str, output_dir: str, max_duration: Optional[int] = None) -> Optional[str]:
        settings = _resolve_remote_downloader_settings()
        if not settings.get("enabled") or not settings.get("url"):
            return None

        try:
            import httpx

            output_dir, keepalive_path = _create_runtime_dir_keepalive(output_dir)
            endpoint = f"{settings['url']}/download"
            headers = {
                "Accept": "application/octet-stream",
                "Content-Type": "application/json",
            }
            if settings.get("token"):
                headers["Authorization"] = f"Bearer {settings['token']}"

            payload: Dict[str, Any] = {"url": video_url}
            if max_duration is not None:
                payload["max_duration"] = max_duration

            timeout_seconds = float(settings.get("timeout_seconds") or 420.0)
            timeout = httpx.Timeout(timeout_seconds, connect=min(20.0, timeout_seconds))

            # === Retry logic for busy worker (503) or rate-limited (429) ===
            max_worker_retries = 3
            retry_backoff = [15, 30, 60]  # seconds between retries

            for worker_attempt in range(1, max_worker_retries + 1):
                logger.info(
                    "🌐 Remote worker request (attempt %d/%d) for %s | host=%s",
                    worker_attempt, max_worker_retries,
                    video_url,
                    settings.get("host"),
                )

                try:
                    with httpx.stream("POST", endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                        # Retry on 503 (worker busy/queued) or 429 (rate limit)
                        if resp.status_code in (503, 429):
                            response_body = resp.read().decode("utf-8", errors="replace")[:400]
                            if worker_attempt < max_worker_retries:
                                wait = retry_backoff[worker_attempt - 1] if worker_attempt - 1 < len(retry_backoff) else 60
                                logger.warning(
                                    "⏳ Remote worker busy (status=%s). Waiting %ds before retry %d/%d... | body=%s",
                                    resp.status_code, wait, worker_attempt + 1, max_worker_retries,
                                    response_body,
                                )
                                time.sleep(wait)
                                continue
                            else:
                                logger.warning(
                                    "Remote worker still busy after %d retries. Falling back. | body=%s",
                                    max_worker_retries, response_body,
                                )
                                return None

                        if resp.status_code != 200:
                            response_body = resp.read().decode("utf-8", errors="replace")[:400]
                            logger.warning(
                                "Remote downloader worker error: status=%s host=%s body=%s",
                                resp.status_code,
                                settings.get("host"),
                                response_body,
                            )
                            return None

                        video_id = _extract_youtube_video_id(video_url) or "remote_download"
                        suggested_name = (resp.headers.get("x-downloader-filename") or "").strip()
                        ext = os.path.splitext(suggested_name)[1] or ".mp4"
                        output_file = os.path.join(output_dir, f"{video_id}_remote{ext}")

                        with ResilientFS.open(output_file, "wb") as fh:
                            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                                if chunk:
                                    fh.write(chunk)

                        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                            logger.info("✅ Remote downloader worker successful: %s", output_file)
                            return output_file

                        logger.warning(
                            "Remote downloader worker finished without a readable file. host=%s output=%s",
                            settings.get("host"),
                            output_file,
                        )
                        return None
                except httpx.TimeoutException:
                    if worker_attempt < max_worker_retries:
                        wait = retry_backoff[worker_attempt - 1] if worker_attempt - 1 < len(retry_backoff) else 60
                        logger.warning(
                            "⏳ Remote worker timeout. Retrying in %ds... (%d/%d)",
                            wait, worker_attempt + 1, max_worker_retries,
                        )
                        time.sleep(wait)
                        continue
                    raise

            return None  # exhausted retries
        except Exception as exc:
            logger.warning(
                "Remote downloader worker request failed for host=%s: %s",
                settings.get("host"),
                exc,
            )
            return None
        finally:
            try:
                _release_runtime_dir_keepalive(keepalive_path)
            except Exception:
                pass

    def _download_via_invidious_piped(self, video_url: str, output_dir: str) -> Optional[str]:
        """Fallback: تنزيل الفيديو عبر خوادم Invidious/Piped العامة لتجاوز حظر YouTube"""
        video_id = _extract_youtube_video_id(video_url)
        if not video_id:
            return None
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed — skipping Invidious/Piped fallback")
            return None

        output_dir = _ensure_runtime_dir(output_dir)
        now = time.time()

        # --- محاولة Invidious أولاً ---
        for instance_url in _INVIDIOUS_INSTANCES:
            host = _safe_url_host(instance_url)
            cooldown_until = _INVIDIOUS_PIPED_COOLDOWN_UNTIL.get(host, 0.0)
            if cooldown_until and cooldown_until > now:
                continue

            api_endpoint = f"{instance_url.rstrip('/')}/api/v1/videos/{video_id}"
            try:
                logger.info("🔄 Invidious fallback: trying %s for video %s", host, video_id)
                resp = httpx.get(
                    api_endpoint,
                    headers={"Accept": "application/json", "User-Agent": _MODERN_USER_AGENT},
                    timeout=20.0,
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.debug("Invidious %s returned %s", host, resp.status_code)
                    _INVIDIOUS_PIPED_COOLDOWN_UNTIL[host] = now + _INVIDIOUS_PIPED_COOLDOWN_SECONDS
                    continue

                data = resp.json()
                # اختيار أفضل stream متاح
                adaptive = data.get("adaptiveFormats") or []
                format_streams = data.get("formatStreams") or []

                # أولاً: البحث عن stream مدمج بأعلى جودة
                best_combined = None
                best_combined_height = 0
                for s in format_streams:
                    if s.get("url"):
                        h = int(s.get("resolution", "0p").replace("p", "") or 0)
                        if h > best_combined_height:
                            best_combined = s.get("url")
                            best_combined_height = h

                download_url = best_combined
                if not download_url:
                    # fallback: أول adaptive video stream
                    for s in adaptive:
                        if s.get("url") and s.get("type", "").startswith("video/"):
                            download_url = s.get("url")
                            break

                if not download_url:
                    logger.debug("Invidious %s: no download URL found in response", host)
                    continue

                # تنزيل الملف
                output_file = os.path.join(output_dir, f"{video_id}_invidious.mp4")
                with httpx.stream("GET", download_url, timeout=300.0, follow_redirects=True) as stream_resp:
                    if stream_resp.status_code != 200:
                        logger.debug("Invidious %s: stream download returned %s", host, stream_resp.status_code)
                        continue
                    with ResilientFS.open(output_file, "wb") as f:
                        for chunk in stream_resp.iter_bytes(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)

                if os.path.exists(output_file) and os.path.getsize(output_file) > 10000:
                    logger.info("✅ Invidious fallback successful via %s: %s (%s)", host, output_file, f"{best_combined_height}p" if best_combined_height else "adaptive")
                    return output_file
                else:
                    with suppress(Exception):
                        ResilientFS.remove(output_file)
                    logger.debug("Invidious %s: downloaded file too small or missing", host)

            except Exception as exc:
                logger.debug("Invidious %s failed: %s", host, exc)
                _INVIDIOUS_PIPED_COOLDOWN_UNTIL[host] = now + _INVIDIOUS_PIPED_COOLDOWN_SECONDS
                continue

        # --- محاولة Piped ---
        for instance_url in _PIPED_API_INSTANCES:
            host = _safe_url_host(instance_url)
            cooldown_until = _INVIDIOUS_PIPED_COOLDOWN_UNTIL.get(host, 0.0)
            if cooldown_until and cooldown_until > now:
                continue

            api_endpoint = f"{instance_url.rstrip('/')}/streams/{video_id}"
            try:
                logger.info("🔄 Piped fallback: trying %s for video %s", host, video_id)
                resp = httpx.get(
                    api_endpoint,
                    headers={"Accept": "application/json", "User-Agent": _MODERN_USER_AGENT},
                    timeout=20.0,
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.debug("Piped %s returned %s", host, resp.status_code)
                    _INVIDIOUS_PIPED_COOLDOWN_UNTIL[host] = now + _INVIDIOUS_PIPED_COOLDOWN_SECONDS
                    continue

                data = resp.json()
                video_streams = data.get("videoStreams") or []
                audio_streams = data.get("audioStreams") or []

                # البحث عن أفضل stream فيديو+صوت أو فيديو فقط
                best_url = None
                best_quality = 0
                for s in video_streams:
                    url = s.get("url") or ""
                    if not url:
                        continue
                    # نفضل streams التي تحتوي video+audio
                    is_video_only = s.get("videoOnly", True)
                    q = int(str(s.get("quality", "0p")).replace("p", "") or 0)
                    # إعطاء أولوية أعلى للـ streams غير video-only
                    effective_q = q + (1000 if not is_video_only else 0)
                    if effective_q > best_quality:
                        best_quality = effective_q
                        best_url = url

                if not best_url:
                    logger.debug("Piped %s: no suitable video stream found", host)
                    continue

                output_file = os.path.join(output_dir, f"{video_id}_piped.mp4")
                with httpx.stream("GET", best_url, timeout=300.0, follow_redirects=True) as stream_resp:
                    if stream_resp.status_code != 200:
                        logger.debug("Piped %s: stream download returned %s", host, stream_resp.status_code)
                        continue
                    with ResilientFS.open(output_file, "wb") as f:
                        for chunk in stream_resp.iter_bytes(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)

                if os.path.exists(output_file) and os.path.getsize(output_file) > 10000:
                    logger.info("✅ Piped fallback successful via %s: %s", host, output_file)
                    return output_file
                else:
                    with suppress(Exception):
                        ResilientFS.remove(output_file)
                    logger.debug("Piped %s: downloaded file too small or missing", host)

            except Exception as exc:
                logger.debug("Piped %s failed: %s", host, exc)
                _INVIDIOUS_PIPED_COOLDOWN_UNTIL[host] = now + _INVIDIOUS_PIPED_COOLDOWN_SECONDS
                continue

        logger.warning("⚠️ All Invidious/Piped instances failed for video %s", video_id)
        return None

    def _download_via_cobalt(self, video_url: str, output_dir: str) -> Optional[str]:
        """تنزيل الفيديو كـ Fallback باستخدام Cobalt API لتجاوز bot-check"""
        api_url = ""
        try:
            import httpx
            import urllib.parse
            global _COBALT_MISSING_AUTH_HINT_SHOWN
            api_url, auth_scheme, auth_token = _resolve_cobalt_api_settings()
            cobalt_disabled, cobalt_disable_reason = _get_cobalt_fallback_disable_state(api_url)
            if cobalt_disabled:
                logger.warning(
                    "⏭️ Skipping Cobalt API fallback request for host=%s: %s",
                    _safe_url_host(api_url),
                    cobalt_disable_reason,
                )
                return None

            output_dir, keepalive_path = _create_runtime_dir_keepalive(output_dir)
            
            if not auth_token and not _COBALT_MISSING_AUTH_HINT_SHOWN:
                _COBALT_MISSING_AUTH_HINT_SHOWN = True
                logger.warning(
                    "⚠️ Cobalt fallback is running without auth token. Public/default servers may reject the request. | host=%s",
                    _safe_url_host(api_url),
                )

            logger.info("🔄 Cobalt API Fallback started for %s | host=%s", video_url, _safe_url_host(api_url))
            
            # استخراج معرف الفيديو للإسم
            vid_id = "unknown_cobalt_vid"
            if "watch?v=" in video_url:
                vid_id = urllib.parse.parse_qs(urllib.parse.urlparse(video_url).query).get('v', [vid_id])[0]
            elif "youtu.be/" in video_url:
                vid_id = video_url.split("youtu.be/")[-1].split("?")[0]
            elif "shorts/" in video_url:
                vid_id = video_url.split("shorts/")[-1].split("?")[0]
            
            # محاكاة لمتصفح حقيقي لتجاوز حماية Cloudflare (التي تحظر curl/httpx)
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Origin": "https://cobalt.tools",
                "Referer": "https://cobalt.tools/",
                "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "Accept-Language": "en-US,en;q=0.9"
            }
            if auth_token:
                headers["Authorization"] = f"{auth_scheme} {auth_token}"
            payload = {
                "url": video_url,
                "videoQuality": "1080",
                "filenamePattern": "basic"
            }
            
            # الطلب الأول يعطينا رابط التنزيل المباشر
            # ملاحظة: بعض سرفرات Cobalt تتطلب JWT، إذا فشل هذا فالبديل هو الكوكيز المحلية فقط
            resp = httpx.post(
                api_url,
                json=payload, 
                headers=headers,
                timeout=30.0
            )
            
            if resp.status_code != 200:
                response_text = (resp.text or "").strip()
                lowered = response_text.lower()
                if "error.api.auth.api-key.missing" in lowered or ("api-key" in lowered and "missing" in lowered):
                    _disable_cobalt_fallback(
                        "configured Cobalt server requires Api-Key auth. "
                        "Set COBALT_API_TOKEN / COBALT_AUTH_TOKEN (or COBALT_API_KEY) with a valid instance-specific key.",
                        api_url=api_url,
                    )
                    return None
                if "error.api.auth.jwt.missing" in lowered or ("jwt" in lowered and "missing" in lowered):
                    _disable_cobalt_fallback(
                        "configured/default Cobalt server requires Bearer JWT auth. "
                        "Set COBALT_API_JWT or use an instance that accepts Api-Key auth.",
                        api_url=api_url,
                    )
                    return None
                if resp.status_code in (401, 403):
                    _temporarily_disable_cobalt_fallback(
                        "configured/default Cobalt server rejected the provided auth or requires an interactive challenge. "
                        "Use a valid instance-specific token or a self-hosted Cobalt server.",
                        api_url=api_url,
                    )
                    return None
                logger.warning(f"Cobalt API error: {resp.status_code} - {resp.text}")
                return None
                
            data = resp.json()
            if data.get("status") not in ("redirect", "stream", "picker"):
                logger.warning(f"Cobalt API unexpected status: {data}")
                return None
                
            if data.get("status") == "picker":
                picker_items = data.get("picker", [])
                if picker_items:
                    download_url = picker_items[0].get("url")
                else:
                    download_url = None
            else:
                download_url = data.get("url")
                
            if not download_url:
                logger.warning(f"Cobalt API did not return a valid download URL. Response: {data}")
                return None
                
            # تنزيل الملف الفعلي
            output_file = os.path.join(output_dir, f"{vid_id}_cobalt.mp4")
            
            logger.info("Cobalt API successfully resolved URL, starting physical download...")
            try:
                with httpx.stream("GET", download_url, timeout=300.0) as stream_resp:
                    if stream_resp.status_code != 200:
                        logger.warning(f"Stream error: {stream_resp.status_code}")
                        return None
                        
                    with ResilientFS.open(output_file, "wb") as f:
                        for chunk in stream_resp.iter_bytes(chunk_size=1024*1024):
                            f.write(chunk)
            finally:
                _release_runtime_dir_keepalive(keepalive_path)
                        
            logger.info(f"✅ Cobalt API Fallback successful: {output_file}")
            return output_file
            
        except Exception as e:
            lowered = str(e).lower()
            if any(token in lowered for token in [
                "name resolution",
                "no address associated",
                "temporary failure",
                "network is unreachable",
                "connection refused",
                "timed out",
                "timeout",
            ]):
                _temporarily_disable_cobalt_fallback(
                    "configured Cobalt host could not be reached from this runtime.",
                    api_url=api_url,
                    cooldown_seconds=600,
                )
            logger.error(f"Cobalt API Fallback failed: {e}")
            return None

    def _download_sync_attempt(self, video_url: str, output_dir: str, max_duration: Optional[int] = None) -> Optional[str]:
        """محاولة واحدة للتنزيل — بأعلى جودة ممكنة مع FFmpeg merge وفلترة المدة"""
        import yt_dlp
        video_url = _normalize_youtube_watch_url(video_url)
        output_dir, keepalive_path = _create_runtime_dir_keepalive(output_dir)
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        cookies_info = _resolve_cookiefile_details()
        cookies_path = cookies_info.get("path") or ""

        # الحصول على مسار FFmpeg للدمج
        try:
            from src.agent.ffmpeg_utils import ffmpeg_bin
            ffmpeg_path = ffmpeg_bin()
        except Exception:
            ffmpeg_path = None

        # أولوية الجودة: أعلى جودة ممكنة مع دعم دقة الشورتس العمودية (1920x1080)
        if ffmpeg_path:
            fmt = (
                "bestvideo[height<=1920][width<=1920][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1920][width<=1920]+bestaudio/"
                "bestvideo+bestaudio/"
                "best[ext=mp4]/"
                "best/"
                "b"
            )
        else:
            fmt = "best[ext=mp4]/best/b"
            logger.warning(
                "⚠️ FFmpeg not found — skipping merged high-quality streams and using a single compatibility profile only "
                "(typically max ~720p)"
            )

        base_dl_extra = {
            "format": fmt,
            "outtmpl": output_template,
            "noplaylist": True,
            "merge_output_format": "mp4" if ffmpeg_path else None,
            "retries": 5,
            "fragment_retries": 5,
        }

        # إضافة فلتر المدة إذا كان مطلوباً (للشورتس مثلاً)
        if max_duration:
            def duration_filter(info_dict):
                dur = info_dict.get("duration")
                if dur and float(dur) > max_duration:
                    return f"Video is too long ({dur}s > {max_duration}s)"
                return None
            base_dl_extra["match_filter"] = duration_filter

        if ffmpeg_path:
            base_dl_extra["ffmpeg_location"] = ffmpeg_path
        else:
            base_dl_extra["postprocessors"] = []

        download_profiles = _download_profile_overrides(ffmpeg_path, cookies_enabled=bool(cookies_path))
        profile_labels = [
            str(profile.get("label") or f"profile_{idx}")
            for idx, profile in enumerate(download_profiles, start=1)
        ]
        if cookies_path:
            logger.info(
                "🍪 yt-dlp cookie resolution active before download: %s | %s",
                cookies_info.get("source") or cookies_info.get("path_label"),
                _build_ytdlp_runtime_diagnostics("download_attempt", cookies_info=cookies_info, profile_labels=profile_labels),
            )
        else:
            logger.warning(
                "🍪 No yt-dlp cookies resolved before download. %s",
                _build_ytdlp_runtime_diagnostics("download_attempt", cookies_info=cookies_info, profile_labels=profile_labels),
            )
        last_profile_error = None

        try:
            for profile_index, profile in enumerate(download_profiles, start=1):
                profile_label = profile.get("label") or f"profile_{profile_index}"
                dl_extra = dict(base_dl_extra)
                for key, value in profile.items():
                    if key != "label":
                        dl_extra[key] = value

                ydl_opts = _build_yt_opts(dl_extra, cookies_path=cookies_info)
                if os.environ.get("YTDLP_SKIP_IMPERSONATE") == "1":
                    ydl_opts.pop("impersonate", None)

                try:
                    logger.info(
                        f"📥 Trying download profile {profile_index}/{len(download_profiles)}: {profile_label}"
                    )
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        try:
                            info = ydl.extract_info(video_url, download=True)
                        except Exception as e:
                            if "impersonate target" in str(e).lower() and "is not available" in str(e).lower():
                                raise
                            raise

                        if info:
                            dl_height = info.get("height") or info.get("resolution", "?")
                            dl_format = info.get("format", "?")
                            dl_format_id = info.get("format_id", "?")
                            dl_vcodec = info.get("vcodec", "?")
                            logger.info(
                                f"📹 Downloaded quality ({profile_label}): {dl_height}p | "
                                f"format={dl_format} | format_id={dl_format_id} | "
                                f"vcodec={dl_vcodec} | url={video_url[:60]}"
                            )

                            filename = ydl.prepare_filename(info)
                            base = os.path.splitext(filename)[0]
                            for ext in [".mp4", ".mkv", ".webm"]:
                                if ResilientFS.exists(base + ext):
                                    return base + ext
                            if ResilientFS.exists(filename):
                                return filename
                except Exception as e:
                    last_profile_error = e
                    error_code, error_reason = _classify_download_error(e)
                    if profile_index < len(download_profiles):
                        logger.warning(
                            f"⚠️ Download profile {profile_label} failed ({error_code}): {error_reason}. "
                            f"Trying fallback profile. | raw={e}"
                        )
                        continue
                    raise
        finally:
            _release_runtime_dir_keepalive(keepalive_path)
        if last_profile_error:
            raise last_profile_error
        return None

    async def process_video(self, input_path: str, video_id: str,
                            shorts_format: str = "crop",
                            enhance: bool = False,
                            add_cta: bool = True,
                            hflip: bool = False,
                            video_type: str = "shorts",
                            video_effects: Optional[Dict[str, Any]] = None,
                            trim_start: float = 1.0,
                            trim_end: float = 1.0) -> Optional[str]:
        """معالجة الفيديو — يدعم شورتس وطويل
        
        video_type:
            - 'shorts': تحويل لشورتس 9:16 مع جميع المعالجات
            - 'long': الحفاظ على الفيديو كما هو مع تحسين YouTube فقط
        """
        try:
            from src.agent.mod_video_processor import ModVideoProcessor

            output_dir = _project_local_path(".output", "auto_mod_shorts" if video_type == "shorts" else "auto_mod_long")
            ResilientFS.makedirs(output_dir, exist_ok=True)

            mvp = ModVideoProcessor(temp_dir=_project_local_path(".temp", "auto_mod"))

            if video_type == "long":
                # --- مسار الفيديوهات الطويلة ---
                # stream copy فقط (بدون إعادة ترميز = 0 فقدان جودة)
                final_path = os.path.join(output_dir, f"{video_id}_mod.mp4")
                loop = asyncio.get_running_loop()
                try:
                    process_timeout_s = int((os.getenv("AUTO_MOD_PROCESS_TIMEOUT_SECONDS", "1800") or "1800").strip())
                except Exception:
                    process_timeout_s = 1800
                process_timeout_s = max(120, process_timeout_s)
                import functools
                opt_func = functools.partial(
                    mvp._optimize_for_youtube,
                    input_path=input_path,
                    output_path=final_path,
                )
                ok = await asyncio.wait_for(loop.run_in_executor(None, opt_func), timeout=process_timeout_s)
                if ok and ResilientFS.exists(final_path):
                    return final_path
                # fallback: نسخ مباشر
                ResilientFS.copy2(input_path, final_path)
                return final_path if ResilientFS.exists(final_path) else None
            else:
                # --- مسار الشورتس ---
                loop = asyncio.get_running_loop()
                try:
                    process_timeout_s = int((os.getenv("AUTO_MOD_PROCESS_TIMEOUT_SECONDS", "1800") or "1800").strip())
                except Exception:
                    process_timeout_s = 1800
                process_timeout_s = max(120, process_timeout_s)
                import functools
                process_func = functools.partial(
                    mvp.process_mod_video,
                    input_video=input_path,
                    output_dir=output_dir,
                    video_id=video_id,
                    trim_start=trim_start,
                    trim_end=trim_end,
                    add_cta=add_cta,
                    convert_to_shorts=True,
                    shorts_format=shorts_format,
                    enhance=enhance,
                    video_effects=video_effects,
                    hflip=hflip,
                )
                out_path, info = await asyncio.wait_for(loop.run_in_executor(None, process_func), timeout=process_timeout_s)
                return out_path
        except asyncio.TimeoutError:
            logger.error(f"Failed to process video {video_id}: processing timed out")
            return None
        except Exception as e:
            logger.error(f"Failed to process video {video_id}: {e}")
            return None

    async def _apply_source_tail_trim(self, input_path: str, video_id: str, trim_seconds: float) -> Optional[str]:
        try:
            seconds = max(0.0, float(trim_seconds or 0.0))
        except Exception:
            seconds = 0.0
        if seconds <= 0:
            return input_path

        from src.agent.mod_video_processor import ModVideoProcessor

        root, ext = os.path.splitext(input_path)
        if not ext:
            ext = ".mp4"
        output_path = f"{root}.tailtrim_{video_id[:24]}_{uuid.uuid4().hex[:8]}{ext}"
        ResilientFS.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        mvp = ModVideoProcessor(temp_dir=_project_local_path(".temp", "auto_mod"))
        loop = asyncio.get_running_loop()
        import functools

        trim_func = functools.partial(mvp._trim_video, input_path, output_path, 0.0, seconds)
        await loop.run_in_executor(None, trim_func)
        if ResilientFS.exists(output_path):
            return output_path
        return None

    @staticmethod
    def _resolve_reusable_video_artifact(path: Optional[str]) -> Optional[str]:
        resolved = _resolve_project_runtime_path(path or "")
        if not resolved:
            return None
        try:
            from src.agent.ffmpeg_utils import validate_input_file

            if validate_input_file(resolved):
                return resolved
        except Exception:
            return None
        return None

    @staticmethod
    def _expected_processed_output_path(video_id: str, video_type: str) -> str:
        output_dir = _project_local_path(".output", "auto_mod_shorts" if video_type == "shorts" else "auto_mod_long")
        return os.path.join(output_dir, f"{video_id}_mod.mp4")

    def _get_reusable_processed_output_path(self, video_id: str, video_type: str) -> Optional[str]:
        candidate = self._expected_processed_output_path(video_id, video_type)
        return self._resolve_reusable_video_artifact(candidate)

    async def schedule_jobs(self, notify_func=None):
        """
        Check schedules and enqueue jobs for agents that are due.
        Replaces the old immediate execution in run_cycle.
        """
        config = self.db.get_config()
        if not config.get("auto_fetch_enabled"):
            return {"status": "disabled"}

        schedules = self.db.get_all_schedules()
        active = [s for s in schedules if s.get("enabled")]
        
        queue = JobQueue()
        enqueued_count = 0
        
        for schedule in active:
            channel_id = schedule["channel_id"]
            
            # 1. Check if agent is already in queue or processing
            if queue.is_agent_busy_or_queued(channel_id):
                continue
                
            # 2. Check time and limits
            if not self._is_publish_time(schedule):
                continue
            if self._reached_daily_limit(schedule):
                continue
                
            # 3. Add to queue
            queue.add_job(
                agent_id=channel_id,
                task_type="process_schedule",
                payload={
                    "channel_id": channel_id,
                    "content_type": schedule.get("content_type", "minecraft_mods"),
                    "force": True # Worker execution is always "forced" in terms of bypassing time checks again, or we re-check inside? run_cycle checks again.
                }
            )
            enqueued_count += 1
            
        if enqueued_count > 0:
            logger.info(f"🗓️ Scheduled {enqueued_count} jobs.")
            
        return {"status": "ok", "enqueued": enqueued_count}

    async def run_cycle(self, notify_func=None, force: bool = False, *, target_channel_id: Optional[str] = None, target_content_type: Optional[str] = None,
                        target_source_id: Optional[str] = None, target_video_id: Optional[str] = None, target_video_url: Optional[str] = None,
                        target_video_title: Optional[str] = None, target_video_type: Optional[str] = None,
                        preview_mode: bool = False, preview_source: Optional[Dict[str, Any]] = None, preview_only: bool = False,
                        **kwargs) -> Dict[str, Any]:
        """
        تشغيل دورة جلب واحدة مع إشعارات تفصيلية:
        1. فحص الإعدادات
        2. فحص الجدولة
        3. جلب فيديوهات جديدة
        4. معالجة ونشر

        Args:
            notify_func: دالة اختيارية لإرسال إشعارات (message)
            force: تخطي فحص الوقت والحد اليومي (للتشغيل اليدوي الفوري)
            target_channel_id: عند التحديد، حصر التشغيل القسري في جدول قناة محدد
            target_content_type: عند التحديد، حصر التشغيل القسري في نوع محتوى محدد
            target_source_id: عند التحديد، حصر التشغيل القسري في مصدر محدد داخل الجدول المستهدف
            target_video_id: عند التحديد مع target_source_id، حصر الاستئناف الفوري في فيديو محدد داخل المصدر
            target_video_url: عند التحديد مع target_video_id، استخدام رابط الفيديو الموافق عليه مباشرة دون جلب فيديو جديد من المصدر
            target_video_title: عنوان اختياري للفيديو الموافق عليه عند الاستئناف المباشر
            target_video_type: نوع اختياري (`shorts` أو `long`) للفيديو الموافق عليه عند الاستئناف المباشر
            target_raw_video_path: مسار اختياري لملف خام محفوظ من مرحلة raw review لإعادة استخدامه عند الاستئناف
            preview_mode: عند التفعيل، تنفيذ نفس مسار المعالجة لإنتاج فيديو تجريبي دون رفعه أو تعديل الحالات الرسمية

        Returns:
            نتائج الدورة
        """

        async def _notify(msg: str):
            """إرسال إشعار آمن"""
            if notify_func:
                try:
                    await notify_func(msg)
                except Exception:
                    pass

        preview_mode = bool(preview_mode)
        preview_source = None
        if preview_mode and target_source_id:
            preview_source = self.db._get_source_by_id(str(target_source_id))
            if not preview_source:
                await _notify("⚠️ لم يتم العثور على المصدر المطلوب لتنفيذ فيديو الاختبار.")
                return {
                    "status": "no_target_source",
                    "message": "Target source not found",
                    "processed": 0,
                    "published": 0,
                    "failed": 0,
                    "skipped": 0,
                    "waiting_raw_review": 0,
                }
            target_channel_id = target_channel_id or preview_source.get("channel_id")
            target_content_type = target_content_type or preview_source.get("content_type", "minecraft_mods")

        if not _RUN_CYCLE_LOCK.acquire(blocking=False):
            running_for = _running_cycle_elapsed_seconds()
            logger.info(
                f"⏳ [AutoMod] Overlapping run_cycle request ignored (current cycle still running for {running_for}s)."
            )
            await _notify(
                f"⏳ توجد دورة جلب أخرى قيد التشغيل منذ `{running_for}` ثانية، وتم تجاهل هذه المحاولة لمنع التكرار."
            )
            return {
                "status": "busy",
                "message": "Another auto-mod cycle is already running",
                "running_for_seconds": running_for,
            }

        try:
            _mark_run_cycle_started()
            meta_notifications_enabled = bool(force or preview_mode)
            # ========== بدء الدورة ==========
            cycle_start = time.time()
            if meta_notifications_enabled:
                await _notify(
                    "🔄 *بدء دورة الجلب التلقائي*\n"
                    f"🆔 النسخة: `{self.instance_id[:20]}`\n"
                    f"🕐 الوقت: `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`"
                )

            # ========== فحص الإعدادات ==========
            config = self.db.get_config()
            if not config.get("auto_fetch_enabled") and not force:
                await _notify("⚠️ الجلب التلقائي *معطل*. قم بتفعيله أولاً.")
                return {"status": "disabled", "message": "Auto fetch is disabled"}

            # ========== فحص الجداول ==========
            schedules = self.db.get_all_schedules()
            active = [s for s in schedules if s.get("enabled")]
            schedule_total = len(schedules)

            if target_channel_id or target_content_type:
                filtered_active = []
                for schedule in active:
                    if target_channel_id and schedule.get("channel_id") != target_channel_id:
                        continue
                    if target_content_type and schedule.get("content_type", "minecraft_mods") != target_content_type:
                        continue
                    filtered_active.append(schedule)
                active = filtered_active

            if not active:
                if preview_mode and preview_source and target_channel_id:
                    active = [{
                        "enabled": True,
                        "channel_id": target_channel_id,
                        "content_type": target_content_type or "minecraft_mods",
                        "preview_only": True,
                    }]
                    schedule_total = max(schedule_total, 1)
                if target_channel_id or target_content_type:
                    if not active:
                        if meta_notifications_enabled:
                            await _notify("⚠️ لم يتم العثور على جدول نشر نشط يطابق الاستهداف المطلوب.")
                        return {"status": "no_target_schedule", "message": "No matching active schedule"}
                elif not active:
                    if meta_notifications_enabled:
                        await _notify("⚠️ لا توجد *جداول نشر نشطة*. أضف جدول نشر أولاً.")
                    return {"status": "no_schedules", "message": "No active schedules"}

            if meta_notifications_enabled:
                await _notify(
                    f"📋 تم العثور على *{len(active)}* جدول نشر نشط من أصل {max(schedule_total, len(active))}."
                )

            results = {"processed": 0, "published": 0, "failed": 0, "skipped": 0, "waiting_raw_review": 0, "previewed": 0}
            errors_log = []
            auto_paused = False  # علامة لإيقاف الأتمتة تلقائياً عند خطأ حرج
            pending_raw_review_paused = False
            approved_target_resume = bool(force and target_source_id and target_video_id)
            direct_target_resume = bool(approved_target_resume and target_video_url)

            if has_pending_raw_reviews() and not approved_target_resume and not preview_mode:
                await _notify(
                    "⏸ توجد مراجعة فيديو خام معلّقة بالفعل، لذلك تم إيقاف دورة الأتمتة بالكامل حتى يصدر قرارك."
                )
                return {
                    **results,
                    "status": "waiting_raw_review",
                    "message": "Pending raw review exists",
                    "waiting_raw_review": 1,
                }

            for sch_idx, schedule in enumerate(active, 1):
                channel_id = schedule["channel_id"]
                content_type = schedule.get("content_type", "minecraft_mods")
                schedule_force = bool(force)
                if schedule_force and (target_channel_id or target_content_type):
                    if target_channel_id and channel_id != target_channel_id:
                        schedule_force = False
                    if target_content_type and content_type != target_content_type:
                        schedule_force = False

                # فحص الوقت
                if not schedule_force and not self._is_publish_time(schedule):
                    results["skipped"] += 1
                    logger.info(f"⏭ [AutoMod] Skipping schedule {sch_idx}: Not publish time yet (Channel: {channel_id[:10]}...)")
                    if meta_notifications_enabled:
                        await _notify(
                            f"⏭ الجدول {sch_idx}: *تخطي* — لم يحن وقت النشر بعد.\n"
                            f"   📺 القناة: `{channel_id[:20]}...`"
                        )
                    continue

                # فحص الحد اليومي
                if not schedule_force and self._reached_daily_limit(schedule):
                    results["skipped"] += 1
                    logger.info(f"⏭ [AutoMod] Skipping schedule {sch_idx}: Daily limit reached (Channel: {channel_id[:10]}...)")
                    if meta_notifications_enabled:
                        await _notify(
                            f"⏭ الجدول {sch_idx}: *تخطي* — تم بلوغ الحد اليومي.\n"
                            f"   📺 القناة: `{channel_id[:20]}...`"
                        )
                    continue

                logger.info(f"📡 [AutoMod] Processing schedule {sch_idx}... (Channel: {channel_id[:10]}...)")
                await _notify(
                    f"📡 الجدول {sch_idx}: *بدء المعالجة*\n"
                    f"   📺 القناة: `{channel_id[:20]}...`\n"
                    f"   📦 النوع: `{content_type}`"
                )

                # جلب المصادر لهذه القناة ونوع المحتوى
                if preview_mode and preview_source and target_source_id and schedule_force:
                    preview_channel = preview_source.get("channel_id")
                    preview_content_type = preview_source.get("content_type", "minecraft_mods")
                    sources_list = [preview_source] if (preview_channel == channel_id and preview_content_type == content_type) else []
                else:
                    sources_list = self.db.get_sources(channel_id, content_type)
                if target_source_id and schedule_force and not preview_mode:
                    sources_list = [
                        source for source in sources_list
                        if str(source.get("id") or f"{channel_id}:{source.get('source_url', '')}") == str(target_source_id)
                    ]
                if not sources_list:
                    logger.info(f"⚠️ [AutoMod] No sources found for channel {channel_id[:10]}... content_type={content_type}")
                    if meta_notifications_enabled:
                        await _notify(
                            f"⚠️ الجدول {sch_idx}: لا توجد مصادر للقناة `{channel_id[:20]}...`"
                        )
                    continue

                if meta_notifications_enabled:
                    await _notify(f"🔍 تم العثور على *{len(sources_list)}* مصدر للبحث.")

                schedule_done = False
                for source in sources_list:
                    if schedule_done:
                        break

                    src_name = source.get("source_name", "مصدر")
                    src_platform = source.get("platform", "youtube")
                    source_settings = normalize_source_settings(source.get("settings"))
                    require_raw_review = bool(source_settings.get("require_raw_review"))
                    legacy_src_url = source.get("source_url", "")
                    source_id = str(source.get("id") or f"{channel_id}:{legacy_src_url}")
                    if not source.get("enabled") and not (preview_mode and source_id == str(target_source_id)):
                        continue

                    fetch_sources = []
                    try:
                        fs = source_settings.get("fetch_sources")
                        if isinstance(fs, list):
                            fetch_sources = [x for x in fs if isinstance(x, dict) and str(x.get("url") or "").strip()]
                    except Exception:
                        fetch_sources = []
                    if not fetch_sources:
                        fetch_sources = [{
                            "url": str(legacy_src_url or "").strip(),
                            "name": "",
                            "platform": str(src_platform or "").strip().lower() or None,
                            "enabled": True,
                        }]
                    else:
                        # اختيار قناة الجلب بشكل عشوائي في كل دورة (مع الحفاظ على نفس منطق جلب/اختيار الفيديو)
                        try:
                            import random
                            fetch_sources = list(fetch_sources)
                            random.shuffle(fetch_sources)
                        except Exception:
                            pass

                    try:
                        logger.info(
                            f"🔍 [AutoMod] Source group: {src_name} (fetch_channels={len(fetch_sources)}, target_channel={channel_id[:10]}...)"
                        )
                    except Exception:
                        pass

                    source_pending_review = get_pending_raw_review(source_id) if (require_raw_review and not preview_mode) else None
                    if source_pending_review and not approved_target_resume and not preview_mode:
                        logger.info(
                            f"⏸ [AutoMod] Source {src_name} is waiting for raw review decision (source_id={source_id[:20]}...)"
                        )
                        pending_raw_review_paused = True
                        results["waiting_raw_review"] = max(results["waiting_raw_review"], 1)
                        schedule_done = True
                        await _notify(
                            f"⏸ المصدر `{src_name[:30]}` لديه فيديو خام بانتظار قرارك بالفعل، لذلك سيتم إيقاف دورة الأتمتة بالكامل الآن."
                        )
                        break

                    current_vid_id = ""
                    current_vid_title = ""
                    current_dl_path = None
                    current_out_path = None
                    current_yt_url = ""
                    claimed_processing = False

                    try:
                        # مستويات البحث المتدرجة (نطاقات غير متداخلة لتحسين الكفاءة وتوسيع نطاق الاستخراج)
                        search_ranges = [
                            "1-50",
                            "51-250",
                            "251-1000",
                            "1001-5000",
                            "5001-20000"
                        ]

                        fetch_order = config.get("settings", {}).get("fetch_order", "newest")
                        videos = []
                        found_new_video = False

                        if direct_target_resume and schedule_force and source_id == str(target_source_id):
                            videos = [{
                                "id": str(target_video_id),
                                "title": str(target_video_title or "بدون عنوان"),
                                "url": str(target_video_url),
                                "video_type": str(target_video_type or ""),
                            }]
                            found_new_video = True
                            logger.info(
                                "⚡ [AutoMod] Directly resuming approved raw-review video %s from source %s without refetch discovery",
                                str(target_video_id)[:20],
                                src_name,
                            )
                            await _notify(
                                f"⚡ استئناف فوري للفيديو الموافق عليه مباشرة دون جلب فيديو جديد:\n"
                                f"   📺 `{str(target_video_title or 'بدون عنوان')[:60]}`"
                            )
                        else:
                            for fidx, fetch_item in enumerate(fetch_sources):
                                if found_new_video:
                                    break
                                if not isinstance(fetch_item, dict):
                                    continue
                                if not bool(fetch_item.get("enabled", True)):
                                    continue
                                fetch_url = str(fetch_item.get("url") or "").strip()
                                if not fetch_url:
                                    continue
                                fetch_name = str(fetch_item.get("name") or "").strip() or src_name
                                fetch_platform = (str(fetch_item.get("platform") or "").strip().lower() or str(src_platform or "").strip().lower() or "youtube")

                                for idx, s_range in enumerate(search_ranges):
                                    if found_new_video:
                                        break

                                    await _notify(
                                        f"🔎 *جلب فيديوهات من:* `{fetch_name}` (النطاق: {s_range}, الترتيب: {fetch_order})\n"
                                        f"   🔗 `{fetch_url[:60]}`"
                                    )

                                    # إضافة تأخير بسيط لتجنب التزامن الشديد
                                    if idx > 0:
                                        import random
                                        delay = random.uniform(3.0, 7.0)
                                        logger.debug(f"⏳ Normalizing fetch rhythm... waiting {delay:.1f}s")
                                        await asyncio.sleep(delay)

                                    batch_videos = await self.fetch_videos_from_source(fetch_url, items_range=s_range, platform=fetch_platform)
                                    logger.info(
                                        f"📦 [AutoMod] Fetch {fetch_name}: fetched {len(batch_videos)} videos (range={s_range})"
                                    )

                                    if not batch_videos:
                                        if idx == 0:
                                            await _notify(f"📭 لم يتم العثور على فيديوهات في النطاق الأول من `{fetch_name}`.")
                                            break  # ربما القناة فارغة أو هناك خطأ في الوصول
                                        await _notify(f"⏹️ لا توجد فيديوهات إضافية بعد النطاق {search_ranges[idx-1]}.")
                                        break

                                    # فحص الفيديوهات في هذا النطاق
                                    potential_videos = []
                                    published_count = 0
                                    locked_count = 0
                                    other_count = 0
                                    for v in batch_videos:
                                        v_id = v.get("id", "")
                                        if not v_id:
                                            logger.info(f"🔎 [AutoMod-Debug] Skipped video with NO ID: {v.get('title')}")
                                            continue

                                        if is_raw_review_blocked(source_id, v_id):
                                            logger.info(f"🔎 [AutoMod-Debug] Video {v_id} blocked by raw review.")
                                            other_count += 1
                                            continue
                                        if is_raw_review_skip_active(source_id, v_id):
                                            logger.info(f"🔎 [AutoMod-Debug] Video {v_id} skipped by raw review.")
                                            other_count += 1
                                            continue

                                        status, updated_at = self.db.get_video_process_state(v_id, channel_id)
                                        if status == "published":
                                            published_count += 1
                                            continue
                                        if status == "processing":
                                            processing_lock_minutes = _processing_lock_stale_minutes()
                                            if not updated_at:
                                                locked_count += 1
                                                continue
                                            try:
                                                if (datetime.now(timezone.utc) - updated_at) <= timedelta(minutes=processing_lock_minutes):
                                                    locked_count += 1
                                                    continue
                                            except Exception:
                                                locked_count += 1
                                                continue
                                        if status:
                                            logger.info(f"🔎 [AutoMod-Debug] Video {v_id} skipped because its status is '{status}' in DB.")
                                            other_count += 1
                                            # If status is failed or something, we shouldn't append it to potential_videos without considering retries
                                            # Wait, the original code had a bug here, it appended it!
                                            # We will just print the log and continue to truly skip it, OR maybe the original logic intended to append it?
                                            # The original code: if status: other_count += 1; potential_videos.append(v)
                                            # If it originally appended it, then `potential_videos` was NOT empty originally!!
                                            # Let's fix the bug: if status is truthy, we must continue, NOT append!
                                            continue
                                            
                                        logger.info(f"🔎 [AutoMod-Debug] Video {v_id} added to potential_videos! (status={status})")
                                        potential_videos.append(v)

                                if target_video_id and schedule_force and source_id == str(target_source_id):
                                    potential_videos = [
                                        item for item in potential_videos
                                        if str(item.get("id", "")) == str(target_video_id)
                                    ]
                                elif require_raw_review and potential_videos:
                                    approved_videos = [
                                        item for item in potential_videos
                                        if is_raw_review_approved(source_id, item.get("id", ""))
                                    ]
                                    if approved_videos:
                                        potential_videos = approved_videos
                                try:
                                    logger.info(
                                        f"🧾 [AutoMod] Source {src_name}: new={len(potential_videos)}, published={published_count}, locked={locked_count}, other={other_count} (channel={channel_id[:10]}...)"
                                    )
                                except Exception:
                                    pass
                            
                                if potential_videos:
                                    # وجدنا فيديوهات جديدة في هذا النطاق!
                                    found_new_video = True
                                
                                    # تطبيق ترتيب الجلب (Fetch Order) المكتشف في هذا النطاق
                                    if fetch_order == "oldest":
                                        # ترتيب من الأقدم للأحدث (نأخذ أقدم فيديو في النطاق المكتشف)
                                        try:
                                            potential_videos.sort(key=lambda x: x.get("upload_date") or "99999999")
                                        except Exception: pass
                                    elif fetch_order == "random":
                                        import random
                                        random.shuffle(potential_videos)
                                    else: # newest
                                        try:
                                            potential_videos.sort(key=lambda x: x.get("upload_date") or "00000000", reverse=True)
                                        except Exception: pass
                                
                                    videos = potential_videos
                                    await _notify(f"🎯 وجدنا *{len(potential_videos)}* فيديو جديد في النطاق {s_range} ({fetch_order}).")
                                    break
                                else:
                                    if idx < len(search_ranges) - 1:
                                        await _notify(f"🔄 جميع فيديوهات النطاق {s_range} تمت معالجتها. *جاري البحث في النطاق التالي...*")
                                    else:
                                        await _notify(f"📭 تم الوصول لأقصى عمق بحث ({depth}) ولم نجد فيديوهات جديدة.")
                                        logger.info(
                                            f"📭 [AutoMod] No new videos in any range for fetch {fetch_name} (channel={channel_id[:10]}...)"
                                        )

                                if found_new_video:
                                    # حفظ آخر fetch context للاستعمال اللاحق في التنزيل/المراجعة
                                    source["_am_fetch_url"] = fetch_url
                                    source["_am_fetch_name"] = fetch_name
                                    source["_am_fetch_platform"] = fetch_platform
                                    break

                        if not found_new_video:
                            continue

                        processed_in_this_source = 0

                        for video in videos:
                            vid_id = video.get("id", "")
                            vid_title = video.get("title", "بدون عنوان")[:60]
                            if not vid_id:
                                continue

                            # بما أننا قمنا بالتصفية مسبقاً في حلقة البحث العميق، الفيديوهات هنا هي "جديدة"
                            logger.info(
                                f"🎬 [AutoMod] Selected video {vid_id[:20]}... from {src_name} for channel {channel_id[:10]}..."
                            )
                            current_vid_id = vid_id
                            current_vid_title = vid_title
                            current_dl_path = None
                            current_out_path = None
                            current_yt_url = ""
                            claimed_processing = False
                            processing_touch_task: Optional[asyncio.Task] = None
                            processing_touch_stop: Optional[asyncio.Event] = None
                            await _notify(
                                f"🎬 *فيديو جديد مختار:* `{vid_title}`\n"
                                f"   🆔 `{vid_id[:20]}`"
                            )

                            # ========== فحص الموارد قبل التنزيل ==========
                            try:
                                from .disk_guard import should_allow_download
                                if not should_allow_download():
                                    await _notify("⚠️ المساحة غير كافية. تم تخطي التنزيل.")
                                    errors_log.append(f"مساحة غير كافية: {vid_title}")
                                    logger.warning(
                                        f"⛔ [AutoMod] Skipping download due to disk space (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )
                                    if not preview_mode:
                                        self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                    schedule_done = True
                                    break
                            except Exception:
                                pass

                            # تسجيل كقيد المعالجة
                            if preview_mode:
                                await _notify(
                                    f"🧪 وضع الاختبار للمصدر `{src_name[:30]}`: سيتم إنشاء فيديو نهائي تجريبي دون حجز حالة المعالجة أو تعديل سجل النشر الرسمي."
                                )
                            else:
                                claimed_processing = self.db.mark_video_processing(
                                    vid_id, channel_id, content_type, video.get("title", "")
                                )
                                if not claimed_processing:
                                    await _notify(f"⚠️ تعذر حجز حالة المعالجة للفيديو `{vid_title}`. تم الإيقاف الآمن لهذه المحاولة.")
                                    errors_log.append(f"فشل قفل المعالجة: {vid_title}")
                                    logger.error(
                                        f"❌ [AutoMod] Failed to acquire processing claim (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )
                                    results["failed"] += 1
                                    self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                    schedule_done = True
                                    break

                            effective_platform = str(source.get("_am_fetch_platform") or source.get("platform") or "youtube")
                            effective_source_url = str(source.get("_am_fetch_url") or source.get("source_url") or "")
                            vid_type = _infer_processing_video_type(
                                video,
                                effective_platform,
                                effective_source_url,
                                normalize_source_settings(source.get("settings")),
                            )
                            type_label = "شورتس" if vid_type == "shorts" else "طويل"
                            approved_resume_for_video = bool(
                                approved_target_resume
                                and schedule_force
                                and source_id == str(target_source_id)
                                and str(vid_id) == str(target_video_id)
                            )
                            dl_path = None
                            out_path = None

                            if approved_resume_for_video:
                                out_path = self._get_reusable_processed_output_path(vid_id, vid_type)
                                if out_path:
                                    current_out_path = out_path
                                    logger.info(
                                        "♻️ [AutoMod] Reusing existing processed artifact for approved video %s: %s",
                                        vid_id[:20],
                                        out_path,
                                    )
                                    await _notify(
                                        f"♻️ تم العثور على نسخة معالجة جاهزة للفيديو الموافق عليه، وسيتم استخدامها مباشرة:\n"
                                        f"   📺 `{vid_title}`"
                                    )

                            if not out_path:
                                reusable_raw_path = None
                                if approved_resume_for_video:
                                    reusable_raw_path = self._resolve_reusable_video_artifact(target_raw_video_path)
                                if reusable_raw_path:
                                    dl_path = reusable_raw_path
                                    current_dl_path = dl_path
                                    logger.info(
                                        "♻️ [AutoMod] Reusing preserved raw artifact for approved video %s: %s",
                                        vid_id[:20],
                                        dl_path,
                                    )
                                    await _notify(
                                        f"♻️ سيتم استكمال الفيديو الموافق عليه من الملف الخام المحفوظ دون إعادة تنزيل:\n"
                                        f"   📺 `{vid_title}`"
                                    )
                                else:
                                    # ========== تنزيل ==========
                                    await _notify(f"⬇️ *جاري التنزيل:* `{vid_title}`...")
                                    dl_dir = _ensure_runtime_dir(_project_local_path(".temp", "auto_mod_downloads"))

                                    # تحديد المدة القصوى بناءً على النوع المختار
                                    max_dur = 60 if effective_platform in ("youtube_shorts", "facebook_reels") else None
                                    dl_path = await self.download_video(video["url"], dl_dir, max_duration=max_dur)
                                    current_dl_path = dl_path

                                    if not dl_path:
                                        err_msg = f"❌ *فشل التنزيل:* `{vid_title}`"
                                        await _notify(err_msg)
                                        errors_log.append(f"تنزيل فاشل: {vid_title}")
                                        logger.warning(
                                            f"❌ [AutoMod] Download failed (video={vid_id[:20]}..., channel={channel_id[:10]}..., source={src_name})"
                                        )
                                        if not preview_mode:
                                            self.db.mark_video_failed(vid_id, channel_id, "Download failed")
                                        results["failed"] += 1
                                        try:
                                            from src.agent.error_tracker import get_error_tracker
                                            get_error_tracker().record_error("download", "download_failed", vid_title)
                                        except Exception:
                                            pass
                                        if not preview_mode:
                                            self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                        schedule_done = True
                                        break

                                    try:
                                        file_size_mb = ResilientFS.getsize(dl_path) / (1024 * 1024)
                                        await _notify(f"✅ تم التنزيل ({file_size_mb:.1f} MB)")
                                    except Exception:
                                        await _notify("✅ تم التنزيل.")

                                if require_raw_review and not preview_mode and not is_raw_review_approved(source_id, vid_id):
                                    from src.bot.raw_review import request_raw_video_review

                                    existing_pending = get_pending_raw_review(source_id)
                                    requested = await request_raw_video_review(
                                        source_id=source_id,
                                        channel_id=channel_id,
                                        source_name=src_name,
                                        source_url=effective_source_url,
                                        content_type=content_type,
                                        video=video,
                                        raw_video_path=dl_path,
                                        video_type=vid_type,
                                    )
                                    if claimed_processing:
                                        self.db.release_video_processing(vid_id, channel_id)
                                        claimed_processing = False
                                    if requested:
                                        current_dl_path = None
                                    else:
                                        self._cleanup_file(dl_path)
                                        current_dl_path = None
                                    schedule_done = True
                                    pending_raw_review_paused = bool(requested or existing_pending)
                                    if pending_raw_review_paused:
                                        results["waiting_raw_review"] += 1
                                    else:
                                        results["failed"] += 1

                                    if requested:
                                        await _notify(
                                            f"🛑 تم إرسال الفيديو الخام للمراجعة اليدوية قبل المعالجة:\n"
                                            f"   📺 `{vid_title}`\n"
                                            f"   🧪 المصدر: `{src_name[:30]}`"
                                        )
                                    elif existing_pending:
                                        await _notify(
                                            f"⏸ يوجد أصلًا فيديو خام بانتظار المراجعة لهذا المصدر، لذلك تم تجاهل الفيديو الجديد:\n"
                                            f"   📺 `{vid_title}`"
                                        )
                                    else:
                                        await _notify(
                                            f"⚠️ تعذر إرسال الفيديو الخام للمراجعة اليدوية، لذلك لن تتم المعالجة الآن:\n"
                                            f"   📺 `{vid_title}`"
                                        )
                                    break

                                await _notify(
                                    f"⚙️ *جاري المعالجة:* `{vid_title}`\n"
                                    f"   📐 النوع: `{type_label}`"
                                )

                                from src.agent.ffmpeg_utils import ffmpeg_bin
                                if not ffmpeg_bin():
                                     err_msg = "⚠️ *تنبيه:* FFmpeg غير موجود على النظام. لا يمكن معالجة الشورتس بدون FFmpeg. يرجى تثبيته لضمان عمل البوت."
                                     await _notify(err_msg)
                                     errors_log.append("FFmpeg missing")
                                     logger.warning(
                                         f"❌ [AutoMod] FFmpeg missing; cannot process (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                     )
                                     if not preview_mode:
                                         self.db.mark_video_failed(vid_id, channel_id, "FFmpeg missing")
                                     results["failed"] += 1
                                     self._cleanup_file(dl_path)
                                     if not preview_mode:
                                         self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                     schedule_done = True
                                     break

                                tail_trim_seconds = pick_source_tail_trim_seconds(source_settings)
                                if tail_trim_seconds > 0:
                                    await _notify(
                                        f"✂️ *قص ثابت للمصدر:* حذف `{tail_trim_seconds:g}` ثانية من نهاية الفيديو قبل المعالجة"
                                    )
                                    trimmed_dl_path = await self._apply_source_tail_trim(dl_path, vid_id, tail_trim_seconds)
                                    if not trimmed_dl_path:
                                        err_msg = f"❌ *فشل قص نهاية الفيديو:* `{vid_title}`"
                                        await _notify(err_msg)
                                        errors_log.append(f"قص نهاية فاشل: {vid_title}")
                                        logger.warning(
                                            f"❌ [AutoMod] Tail trim failed (video={vid_id[:20]}..., channel={channel_id[:10]}..., seconds={tail_trim_seconds})"
                                        )
                                        if not preview_mode:
                                            self.db.mark_video_failed(vid_id, channel_id, "Tail trim failed")
                                        results["failed"] += 1
                                        self._cleanup_file(dl_path)
                                        current_dl_path = None
                                        if not preview_mode:
                                            self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                        schedule_done = True
                                        break
                                    if trimmed_dl_path != dl_path:
                                        self._cleanup_file(dl_path)
                                        dl_path = trimmed_dl_path
                                        current_dl_path = trimmed_dl_path

                                if claimed_processing and not preview_mode:
                                    processing_touch_stop = asyncio.Event()

                                    async def _processing_touch_loop():
                                        try:
                                            raw_touch = (os.getenv("AUTO_MOD_PROCESSING_TOUCH_SECONDS", "45") or "45").strip()
                                            touch_seconds = int(float(raw_touch))
                                        except Exception:
                                            touch_seconds = 45
                                        touch_seconds = max(15, min(180, touch_seconds))
                                        while not processing_touch_stop.is_set():
                                            try:
                                                await asyncio.wait_for(processing_touch_stop.wait(), timeout=touch_seconds)
                                            except asyncio.TimeoutError:
                                                if not processing_touch_stop.is_set():
                                                    self.db.touch_video_processing(vid_id, channel_id)

                                    processing_touch_task = asyncio.create_task(_processing_touch_loop())

                                source_hflip = source_settings.get("hflip")
                                resolved_hflip = config.get("hflip_enabled", False) if source_hflip is None else bool(source_hflip)

                                out_path = await self.process_video(
                                    dl_path, vid_id,
                                    shorts_format=config.get("shorts_format", "crop"),
                                    enhance=config.get("enhance_enabled", False),
                                    add_cta=config.get("add_cta", True),
                                    hflip=resolved_hflip,
                                    video_type=vid_type,
                                    video_effects=pick_source_video_effects(source_settings),
                                    trim_end=0.0 if (vid_type == "shorts" and tail_trim_seconds > 0) else 1.0,
                                )
                                current_out_path = out_path
                                if processing_touch_stop:
                                    processing_touch_stop.set()
                                if processing_touch_task:
                                    with suppress(Exception):
                                        await asyncio.wait_for(processing_touch_task, timeout=3)

                                if not out_path:
                                    err_msg = f"❌ *فشل المعالجة:* `{vid_title}`"
                                    await _notify(err_msg)
                                    errors_log.append(f"معالجة فاشلة: {vid_title}")
                                    logger.warning(
                                        f"❌ [AutoMod] Processing failed (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )
                                    if not preview_mode:
                                        self.db.mark_video_failed(vid_id, channel_id, "Processing failed")
                                    results["failed"] += 1
                                    self._cleanup_file(dl_path)
                                    if not preview_mode:
                                        self.db.update_next_publish_after_attempt(channel_id, content_type, published=False)
                                    schedule_done = True
                                    break

                                await _notify("✅ تمت المعالجة بنجاح.")

                            # ========== نص مخصص (Custom Overlay Text) ==========
                            source_overlay = pick_source_overlay_config(source_settings) if vid_type == "shorts" else None
                            channel_overlay = None
                            if not source_overlay and vid_type == "shorts":
                                try:
                                    from src.bot.channel_manager import ChannelManager as _CM
                                    _ch = _CM().get_channel(channel_id)
                                    _overlay_texts = getattr(_ch, "custom_overlay_texts", None) or [] if _ch else []
                                    if _overlay_texts:
                                        channel_overlay = random.choice(_overlay_texts)
                                except Exception:
                                    channel_overlay = None

                            overlay_cfg = source_overlay or channel_overlay
                            if overlay_cfg and vid_type == "shorts":
                                try:
                                    _ov_text = (overlay_cfg.get("text") or "").strip()
                                    if _ov_text:
                                        await _notify(f"✏️ *جاري إضافة نص مخصص:* `{_ov_text[:40]}`...")
                                        from src.agent.mod_video_processor import ModVideoProcessor as _MVP
                                        _mvp_ov = _MVP(temp_dir=_project_local_path(".temp", "auto_mod"))
                                        _ov_out_dir = _project_local_path(".output", "auto_mod_overlay")
                                        ResilientFS.makedirs(_ov_out_dir, exist_ok=True)
                                        _ov_out = os.path.join(_ov_out_dir, f"{vid_id}_overlay.mp4")

                                        loop = asyncio.get_running_loop()
                                        import functools
                                        _ov_func = functools.partial(
                                            _mvp_ov.add_custom_overlay_text,
                                            input_path=out_path,
                                            output_path=_ov_out,
                                            text=_ov_text,
                                            timing=overlay_cfg.get("timing", "full"),
                                            duration=float(overlay_cfg.get("duration", 2.0)),
                                            screen_position=overlay_cfg.get("screen_position", "top"),
                                            intro_animation=overlay_cfg.get("intro_animation"),
                                            outro_animation=overlay_cfg.get("outro_animation"),
                                        )
                                        await loop.run_in_executor(None, _ov_func)
                                        if ResilientFS.exists(_ov_out):
                                            self._cleanup_file(out_path)
                                            out_path = _ov_out
                                            await _notify("✅ تم إضافة النص المخصص.")
                                        else:
                                            await _notify("⚠️ فشل إضافة النص المخصص، سيتم الرفع بدونه.")
                                except Exception as ov_err:
                                    logger.warning(f"Custom overlay text failed: {ov_err}")
                                    await _notify(f"⚠️ فشل النص المخصص: `{str(ov_err)[:80]}`")

                            # ========== فيس كام (Facecam overlay) ==========
                            facecam_cfg, facecam_clip = pick_source_facecam_clip(source_settings, channel_id)

                            if facecam_cfg.get("enabled"):
                                try:
                                    await _notify("🎬 *جاري إضافة فيس كام...*")
                                    if facecam_clip:
                                        from src.agent.config import load_config as _load_cfg
                                        from src.agent.renderer import render_with_pip

                                        render_cfg = _load_cfg()
                                        render_out_dir = _project_local_path(".output", "auto_mod_facecam")
                                        ResilientFS.makedirs(render_out_dir, exist_ok=True)

                                        fc_pos = facecam_cfg.get("position", "top_center")
                                        fc_shape = facecam_cfg.get("shape", "circle")
                                        fc_scale = facecam_cfg.get("scale", 0.28)
                                        fc_layout = facecam_cfg.get("layout", "top_center")

                                        loop = asyncio.get_running_loop()
                                        import functools
                                        render_func = functools.partial(
                                            render_with_pip,
                                            cfg=render_cfg,
                                            input_path=out_path,
                                            out_dir=render_out_dir,
                                            facecam_enabled=True,
                                            facecam_path=facecam_clip,
                                            facecam_layout=fc_layout,
                                            facecam_position=fc_pos,
                                            facecam_shape=fc_shape,
                                            facecam_scale=fc_scale,
                                        )
                                        fc_out = await loop.run_in_executor(None, render_func)
                                        if fc_out and ResilientFS.exists(fc_out):
                                            self._cleanup_file(out_path)
                                            out_path = fc_out
                                            await _notify("✅ تم إضافة الفيس كام.")
                                        else:
                                            await _notify("⚠️ فشل إضافة الفيس كام، سيتم الرفع بدونه.")
                                    else:
                                        await _notify(
                                            f"⚠️ لا توجد مقاطع فيس كام صالحة للمصدر `{source.get('source_name', '')[:20]}...`\n"
                                            "سيتم الرفع بدون فيس كام."
                                        )
                                except Exception as fc_err:
                                    logger.warning(f"Facecam overlay failed: {fc_err}")
                                    await _notify(f"⚠️ فشل الفيس كام: `{str(fc_err)[:80]}`")

                            if preview_mode:
                                await _notify(
                                    f"🧪 *اكتمل فيديو الاختبار بنجاح*\n"
                                    f"📺 `{vid_title}`\n"
                                    "لن يتم رفع هذا الفيديو إلى YouTube ولن يتم تعديل حالة النشر الرسمية."
                                )
                                results["previewed"] += 1
                                results["status"] = "preview_ready"
                                results["preview_video_path"] = out_path
                                results["preview_video_title"] = video.get("title", "")
                                results["preview_source_id"] = source_id
                                results["preview_source_name"] = src_name
                                results["preview_channel_id"] = channel_id
                            else:
                                # ========== رفع إلى YouTube ==========
                                await _notify(
                                    f"⬆️ *جاري الرفع إلى YouTube:* `{vid_title}`\n"
                                    f"   📺 القناة: `{channel_id[:20]}...`"
                                )
                                yt_url = await self._upload_to_youtube(
                                    out_path, channel_id, video.get("title", ""),
                                    source.get("source_name", ""),
                                    content_type, (vid_type == "shorts"),
                                    source_description=video.get("description", ""),
                                    source_settings=source_settings,
                                    source_video_metadata={
                                        "id": video.get("id", ""),
                                        "original_title": video.get("title", ""),
                                        "original_description": video.get("description", ""),
                                        "source_url": video.get("url", ""),
                                        "duration": video.get("duration"),
                                        "view_count": video.get("view_count"),
                                        "upload_date": video.get("upload_date"),
                                        "source_name": source.get("source_name", ""),
                                    },
                                )
                                current_yt_url = yt_url or ""

                                if yt_url:
                                    self.db.mark_video_published(vid_id, channel_id, yt_url)
                                    results["published"] += 1
                                    logger.info(
                                        f"✅ [AutoMod] Published (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )

                                    await _notify(
                                        f"🎉 *تم النشر بنجاح!*\n"
                                        f"📺 `{vid_title}`\n"
                                        f"🔗 {yt_url}"
                                    )
                                else:
                                    err_msg = f"❌ *فشل الرفع:* `{vid_title}`"
                                    await _notify(err_msg)
                                    errors_log.append(f"رفع فاشل: {vid_title}")
                                    logger.warning(
                                        f"❌ [AutoMod] Upload failed (video={vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )
                                    self.db.mark_video_failed(vid_id, channel_id, "Upload failed")
                                    results["failed"] += 1

                                self.db.update_next_publish_after_attempt(channel_id, content_type, published=bool(yt_url))
                            results["processed"] += 1

                            # تنظيف الملفات المؤقتة
                            self._cleanup_file(dl_path)
                            if not preview_mode:
                                self._cleanup_file(out_path)

                            # نشر فيديو واحد فقط لكل دورة لكل قناة
                            schedule_done = True
                            break

                    except Exception as e:
                        from src.agent.uploader import (
                            is_youtube_quota_error,
                            AuthenticationRequiredError,
                            youtube_channel_restriction_details,
                        )
                        if processing_touch_stop:
                            processing_touch_stop.set()
                        if processing_touch_task:
                            with suppress(Exception):
                                await asyncio.wait_for(processing_touch_task, timeout=3)

                        if claimed_processing and current_vid_id and not preview_mode:
                            try:
                                recovered_ok = self.db.mark_video_published(current_vid_id, channel_id, current_yt_url) if current_yt_url else self.db.mark_video_failed(
                                    current_vid_id,
                                    channel_id,
                                    f"Unexpected automation error: {str(e)[:400]}",
                                )
                                if not recovered_ok:
                                    logger.error(
                                        f"❌ [AutoMod] Failed to recover processing state after exception (video={current_vid_id[:20]}..., channel={channel_id[:10]}...)"
                                    )
                                self._cleanup_file(current_dl_path)
                                self._cleanup_file(current_out_path)
                                self.db.update_next_publish_after_attempt(channel_id, content_type, published=bool(current_yt_url))
                                schedule_done = True
                            except Exception as recovery_err:
                                logger.error(
                                    f"❌ [AutoMod] Recovery after source exception failed (video={current_vid_id[:20]}..., channel={channel_id[:10]}...): {recovery_err}"
                                )
                    
                        # ===== أخطاء حرجة → إيقاف جدول القناة المتأثرة فقط =====
                        is_critical = False
                        pause_reason = ""
                    
                        if isinstance(e, AuthenticationRequiredError):
                            is_critical = True
                            pause_reason = (
                                "🔐 *مشكلة مصادقة القناة!*\n\n"
                                f"📺 القناة: `{channel_id[:25]}...`\n"
                                f"📛 السبب: `{str(e)[:200]}`\n\n"
                                "💡 *الحل:*\n"
                                "1. افتح البوت واذهب إلى ⚙️ إعدادات القنوات\n"
                                "2. أعد ربط القناة المتأثرة بملف مصادقة جديد\n"
                                "3. أعد تشغيل الأتمتة يدوياً بعد الإصلاح"
                            )
                        elif is_youtube_quota_error(e):
                            is_critical = True
                            pause_reason = (
                                "📊 *نفدت حصة YouTube الأسبوعية!*\n\n"
                                f"📺 القناة: `{channel_id[:25]}...`\n"
                                f"📛 السبب: `{str(e)[:200]}`\n\n"
                                "💡 *الحل:*\n"
                                "1. انتظر حتى تتجدد حصة YouTube (عادةً بعد 24-48 ساعة)\n"
                                "2. أو استخدم ملف مصادقة لحساب/مشروع Google آخر\n"
                                "3. أعد تشغيل الأتمتة يدوياً بعد الإصلاح"
                            )
                    
                        if not is_critical:
                            try:
                                restricted, details = youtube_channel_restriction_details(e)
                            except Exception:
                                restricted, details = False, ""
                            if restricted:
                                is_critical = True
                                pause_reason = (
                                    "⛔ *تم تقييد القناة/الحساب من YouTube!*\n\n"
                                    f"📺 القناة: `{channel_id[:25]}...`\n"
                                    f"📛 السبب: `{details or str(e)[:200]}`\n\n"
                                    "💡 *ملاحظات:*\n"
                                    "- قد يكون هناك حظر/إنذار/قيود رفع من YouTube\n"
                                    "- راجع YouTube Studio > Channel status/features\n"
                                    "- بعد إزالة التقييد، أعد تفعيل جدول هذه القناة من إعدادات الأتمتة"
                                )

                        if is_critical and not preview_mode:
                            # ===== إيقاف جدول القناة المتأثرة فقط =====
                            try:
                                schedule["enabled"] = False
                                schedule["paused_reason"] = str(e)[:400]
                                schedule["paused_at"] = datetime.now(timezone.utc).isoformat()
                                self.db._save_existing_schedule(schedule, {
                                    "enabled": False,
                                    "paused_reason": str(e)[:400],
                                    "paused_at": datetime.now(timezone.utc).isoformat(),
                                })
                                logger.warning(
                                    f"🛑 [AutoMod] Schedule paused only for affected channel "
                                    f"(channel={channel_id[:20]}..., content_type={content_type})."
                                )
                            except Exception as db_err:
                                logger.error(f"Failed to pause affected schedule in DB: {db_err}")
                        
                            # إشعار المسؤول
                            auto_pause_msg = (
                                "🛑 *تم إيقاف هذا الوكيل فقط تلقائياً!*\n"
                                "━━━━━━━━━━━━━━━━━━━\n\n"
                                f"{pause_reason}\n\n"
                                "━━━━━━━━━━━━━━━━━━━\n"
                                "⏸ تم تعطيل جدول هذه القناة فقط مؤقتاً.\n"
                                "✅ بقية الوكلاء سيواصلون العمل بشكل طبيعي.\n"
                                "🔧 بعد إصلاح المصادقة/الحصة، أعد تفعيل جدول هذه القناة من إعدادات الأتمتة."
                            )
                            await _notify(auto_pause_msg)
                            errors_log.append(f"🛑 إيقاف وكيل واحد تلقائياً: {str(e)[:80]}")
                            schedule_done = True
                        
                            # إنهاء جدول هذه القناة فقط
                            break
                        else:
                            # أخطاء عادية — تسجيل ومتابعة
                            err_msg = f"💥 *خطأ غير متوقع* أثناء معالجة `{src_name}`:\n`{str(e)[:200]}`"
                            await _notify(err_msg)
                            errors_log.append(f"خطأ في {src_name}: {str(e)[:100]}")
                            try:
                                logger.error(
                                    f"Error processing source {str(source.get('_am_fetch_url') or source.get('source_url') or '')[:120]}: {e}"
                                )
                            except Exception:
                                logger.error(f"Error processing source: {e}")
                    if schedule_done or auto_paused:
                        break
                    
                if auto_paused or pending_raw_review_paused:
                    break
                if results["processed"] >= 1:
                    await _notify("⏳ تم الوصول للإجمالي الأقصى (فيديو واحد) في هذه الدورة حفاظاً على استقرار السيرفر.")
                    break

            # ========== ملخص الدورة ==========
            elapsed = time.time() - cycle_start
            elapsed_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
            if pending_raw_review_paused:
                results["status"] = "waiting_raw_review"
                results["message"] = "Waiting for raw review approval"

            summary = (
                "━━━━━━━━━━━━━━━━━━━\n"
                "📊 *ملخص دورة الجلب التلقائي*\n\n"
                f"⏱ المدة: `{elapsed_str}`\n"
                f"📦 معالج: *{results['processed']}*\n"
                f"✅ منشور: *{results['published']}*\n"
                f"❌ فاشل: *{results['failed']}*\n"
                f"🧪 بانتظار مراجعة خام: *{results['waiting_raw_review']}*\n"
                f"⏭ متخطى: *{results['skipped']}*\n"
            )

            if errors_log:
                summary += "\n🔴 *الأخطاء:*\n"
                for err in errors_log[:5]:
                    summary += f"  • {err}\n"
                if len(errors_log) > 5:
                    summary += f"  • ... و{len(errors_log) - 5} أخطاء أخرى\n"

            if not results["processed"] and not results["failed"] and not results["waiting_raw_review"]:
                summary += "\n💤 لا توجد فيديوهات جديدة للمعالجة."
            elif results["waiting_raw_review"]:
                summary += "\n⏸ تم إيقاف بقية الدورة حتى يتم اتخاذ قرار المراجعة الخام."
            elif preview_mode and results.get("previewed"):
                summary += "\n🧪 تم إنشاء فيديو اختبار نهائي دون أي نشر أو تعديل للحالة الرسمية."

            should_send_summary = (
                meta_notifications_enabled
                or bool(results["processed"] or results["published"] or results["failed"] or results["waiting_raw_review"])
                or bool(errors_log)
            )
            if should_send_summary:
                await _notify(summary)

            # Mandatory Render cooldown: Stay active but don't start a new cycle for 5 mins
            # This prevents "back-to-back" heavy processing which causes memory leaks/exhaustion
            logger.info("🎬 [AutoMod] Mandatory 5-minute cooldown starting...")
            return results
        finally:
            _mark_run_cycle_finished()
            _RUN_CYCLE_LOCK.release()

    async def run_test_render(self, source_id: str, *, notify_func=None) -> Dict[str, Any]:
        """إنشاء فيديو اختبار لمصدر محدد عبر نفس خط المعالجة الحقيقي دون نشر رسمي."""
        source = self.db._get_source_by_id(str(source_id))
        if not source:
            if notify_func:
                try:
                    await notify_func("⚠️ لم يتم العثور على المصدر المطلوب لتوليد فيديو الاختبار.")
                except Exception:
                    pass
            return {
                "status": "no_target_source",
                "message": "Target source not found",
                "processed": 0,
                "published": 0,
                "failed": 0,
                "skipped": 0,
                "waiting_raw_review": 0,
            }

        return await self.run_cycle(
            notify_func=notify_func,
            force=True,
            target_channel_id=source.get("channel_id"),
            target_content_type=source.get("content_type", "minecraft_mods"),
            target_source_id=str(source_id),
            preview_mode=True,
        )

    async def _upload_to_youtube(self, video_path: str, channel_id: str,
                                  title: str, source_name: str,
                                  content_type: str, is_shorts: bool = True,
                                  source_description: str = "",
                                  source_settings: Any = None,
                                  source_video_metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """رفع فيديو إلى YouTube عبر ChannelManager مع تجديد التوكن مسبقاً"""
        try:
            from src.agent.config import load_config
            from src.agent.uploader import upload_video_with_token, _creds_from_token_file
            from src.bot.channel_manager import ChannelManager
            from src.agent.ai import generate_ai_metadata
            from src.agent.error_tracker import get_error_tracker
            
            et = get_error_tracker()
            cfg = load_config()
            cm = ChannelManager()

            # الحصول على بيانات القناة
            channel = cm.get_channel(channel_id)
            if not channel:
                logger.error(f"Channel {channel_id} not found")
                et.record_error("upload", "channel_not_found", channel_id)
                return None

            token_path = channel.token_path
            if not token_path or not os.path.exists(token_path):
                logger.error(f"Token not found for channel {channel_id}")
                et.record_error("upload", "token_missing", channel_id)
                return None

            # === فحص التوكن مسبقاً وتجديده إذا لزم الأمر ===
            try:
                _creds_from_token_file(token_path)
                logger.debug(f"✅ Token pre-validated for {channel_id[:20]}")
            except Exception as tok_err:
                logger.warning(f"⚠️ Token pre-validation failed: {tok_err}")
                et.record_error("upload", "token_invalid", str(tok_err))
                raise  # سيتم التقاطها من run_cycle كخطأ حرج

            # استخدام لغة القناة بدل اللغة المُثبتة
            target_lang = getattr(channel, "language", "ar") or "ar"

            # توليد بيانات الفيديو محلياً قبل النشر
            ai_meta = generate_ai_metadata(
                cfg=cfg,
                source_title=title,
                source_description=source_description,
                content_type=content_type,
                target_lang=target_lang,
                is_shorts=is_shorts,
                channel_key=channel_id,
                video_path=video_path,
                source_context=source_video_metadata or {},
            )
            
            final_title, description, tags = _build_hashtag_only_upload_metadata(
                ai_meta,
                source_title=title,
                source_name=source_name,
                content_type=content_type,
                target_lang=target_lang,
                is_shorts=is_shorts,
                source_description=source_description,
                source_settings=source_settings,
            )
            
            # Use per-source privacy setting if configured, otherwise channel default
            privacy = source_settings.get("privacy") or getattr(channel, "privacy", "unlisted") or "unlisted"

            loop = asyncio.get_running_loop()
            import functools
            upload_func = functools.partial(
                upload_video_with_token,
                cfg=cfg,
                token_path=token_path,
                file_path=video_path,
                title=final_title,
                description=description,
                tags=tags,
                privacy=privacy,
            )
            video_id = await loop.run_in_executor(None, upload_func)

            if video_id:
                youtube_url = f"https://www.youtube.com/watch?v={video_id}"
                logger.info(f"✅ Auto-published: {youtube_url}")
                et.record_success("upload")
                return youtube_url
            et.record_error("upload", "no_video_id", "upload returned None")
            return None
        except Exception as e:
            from src.agent.uploader import is_youtube_quota_error, AuthenticationRequiredError
            from src.agent.error_tracker import get_error_tracker
            et = get_error_tracker()
            
            # أخطاء حرجة يجب إيصالها للمستدعي لإيقاف الأتمتة تلقائياً
            if isinstance(e, AuthenticationRequiredError):
                logger.error(f"🔐 Upload auth error (will auto-pause): {e}")
                et.record_error("upload", "auth", str(e))
                raise
            if is_youtube_quota_error(e):
                logger.error(f"📊 Upload quota error (will auto-pause): {e}")
                et.record_error("upload", "quota", str(e))
                raise
            # أخطاء عادية (شبكة، timeout...) — نبلع ونعيد None
            logger.error(f"Upload failed (non-critical): {e}")
            et.record_error("upload", "non_critical", str(e))
            return None

    def _is_publish_time(self, schedule: Dict) -> bool:
        """فحص إذا كان الوقت مناسبًا للنشر"""
        now = datetime.now(timezone.utc)

        # فحص next_publish_at
        next_pub = schedule.get("next_publish_at")
        next_dt = _parse_datetime_utc(next_pub)
        if next_dt and now < next_dt:
            return False
        # إذا لم يكن هناك next_publish_at، نعتبر أنه يحق له النشر الآن

        # فحص ساعات النشر
        hours = schedule.get("publish_hours") or {}
        try:
            start_hour = int(hours.get("start", 8))
        except Exception:
            start_hour = 8
        try:
            end_hour = int(hours.get("end", 22))
        except Exception:
            end_hour = 22
        start_hour = max(0, min(23, start_hour))
        end_hour = max(0, min(24, end_hour))
        current_hour = now.hour
        if start_hour <= end_hour:
            return start_hour <= current_hour < end_hour
        else:
            return current_hour >= start_hour or current_hour < end_hour

    def _reached_daily_limit(self, schedule: Dict) -> bool:
        """فحص الحد اليومي (بالاعتماد على توقيت UTC)"""
        try:
            daily_limit = int(schedule.get("daily_limit", 5) or 5)
        except Exception:
            daily_limit = 5
        if daily_limit <= 0 or daily_limit >= 999:
            return False

        channel_id = str(schedule.get("channel_id") or "").strip()
        if not channel_id:
            return False

        try:
            content_type = schedule.get("content_type", "minecraft_mods")
            total_today = self.db.count_published_today(channel_id, content_type)
            return total_today >= daily_limit
        except Exception:
            return False

    @staticmethod
    def _cleanup_file(path: str):
        """حذف ملف مؤقت"""
        try:
            if path:
                ResilientFS.remove(path)
        except Exception:
            pass


def _normalize_auto_fetch_loop_config(config: Any, default_interval_seconds: int) -> Dict[str, Any]:
    # Increased default fallback to 15 minutes (900s) on Render to ensure stability
    safe_default = max(300, int(default_interval_seconds or 900))
    if not isinstance(config, dict):
        return {
            "auto_fetch_enabled": True,
            "auto_fetch_interval_seconds": safe_default,
        }

    normalized = dict(config)
    try:
        interval = int(normalized.get("auto_fetch_interval_seconds", safe_default) or safe_default)
    except Exception:
        interval = safe_default

    normalized["auto_fetch_enabled"] = bool(normalized.get("auto_fetch_enabled", True))
    normalized["auto_fetch_interval_seconds"] = max(300, interval)
    return normalized


def _compute_loop_sleep_seconds(loop_started_monotonic: float, interval_seconds: int) -> int:
    """احسب المدة المتبقية قبل بداية الدورة التالية دون مضاعفة الانتظار بعد دورة طويلة."""
    safe_interval = max(5, int(interval_seconds or 60))
    try:
        elapsed = max(0.0, time.monotonic() - float(loop_started_monotonic))
    except Exception:
        elapsed = 0.0
    return max(0, int(round(safe_interval - elapsed)))

async def start_auto_fetch_loop(interval_seconds: int = 3600):
    """
    تشغيل حلقة الجلب التلقائي في الخلفية
    مع تكامل كامل مع أنظمة الحماية والمراقبة
    """
    from src.agent.auto_mod_fetcher import AutoModFetcher, AutoModDB, get_instance_id
    from src.agent.heartbeat import get_heartbeat_monitor
    from src.agent.disk_guard import cleanup_old_files, should_allow_download
    from src.agent.memory_guard import periodic_maintenance as mem_maintenance, should_defer_heavy_work
    from src.agent.error_tracker import get_error_tracker
    from src.agent.alert_system import get_alert_system
    import asyncio
    import logging
    
    logger = logging.getLogger("AutoModLoop")
    hb = get_heartbeat_monitor()
    et = get_error_tracker()
    
    instance_id = get_instance_id()
    logger.info(f"⏳ Auto-fetch loop started for instance: {instance_id} (Interval: {interval_seconds}s)")
    
    # تسجيل في HeartbeatMonitor
    hb.register("auto_fetch", max_silence_seconds=interval_seconds * 3 + 300)
    
    fetcher = AutoModFetcher(instance_id)
    db = AutoModDB(instance_id)
    consecutive_loop_errors = 0
    config = _normalize_auto_fetch_loop_config(None, interval_seconds)
    last_notify_probe_log = 0.0

    async def _loop_notify(msg: str):
        nonlocal last_notify_probe_log
        try:
            alert_system = get_alert_system()
            bot_app = alert_system.get_bot_app()
            admin_chat_id = alert_system.get_admin_chat_id()
            if not bot_app or not admin_chat_id:
                now = time.time()
                if now - last_notify_probe_log >= 300:
                    logger.warning("⚠️ Telegram automation notifications are disabled: admin chat is not configured yet.")
                    last_notify_probe_log = now
                return
            await bot_app.bot.send_message(
                chat_id=admin_chat_id,
                text=f"🤖 تحديث الأتمتة:\n\n{msg}",
            )
        except Exception as notify_exc:
            logger.warning(f"⚠️ Failed to send automation notification: {notify_exc}")
    
    # التأكد من وجود الإعدادات في Supabase للنسخة الحالية
    try:
        config = _normalize_auto_fetch_loop_config(db.get_config(use_cache=False), interval_seconds)
        if not config or config.get("instance_id") != instance_id:
            logger.info(f"📝 Initializing config for new instance: {instance_id}")
            db.save_config(config)
            
        # تنظيف أي فيديوهات علقت قيد المعالجة بسبب انهيار سابق
        stale_count = db.reset_stale_processing(
            stale_minutes=_processing_lock_stale_minutes(),
            force_reset_all=_should_force_reset_processing_on_boot(),
        )
        if stale_count > 0:
            logger.info(f"🧹 Cleaned up {stale_count} stale processing locks for instance {instance_id}")

    except Exception as e:
        logger.warning(f"Could not initialize config: {e}")

    while True:
        loop_started_monotonic = time.monotonic()
        hb.beat("auto_fetch")  # نبضة في كل دورة
        
        try:
            # === صيانة دورية ===
            cleanup_old_files()  # تنظيف القرص
            mem_maintenance()    # صيانة الذاكرة
            
            # === فحص الموارد قبل العمل الثقيل ===
            mem_defer, mem_reason, mem_retry = should_defer_heavy_work()
            if mem_defer:
                logger.warning(f"⏳ Deferring fetch cycle: {mem_reason} (retry in {mem_retry}s)")
                et.record_error("auto_fetch", "resource", mem_reason)
                await asyncio.sleep(min(mem_retry, interval_seconds))
                continue
            
            if not should_allow_download():
                logger.warning("⏳ Deferring fetch cycle: disk space critical")
                et.record_error("auto_fetch", "disk", "disk space critical")
                await asyncio.sleep(120)
                continue
            
            # === إعادة جلب الإعدادات ===
            config = _normalize_auto_fetch_loop_config(db.get_config(use_cache=False), interval_seconds)
            
            if config.get("auto_fetch_enabled"):
                # فحص نمط الأخطاء — إذا كان المكوّن "ميت"، نوقف مؤقتاً
                action = et.suggest_action("auto_fetch")
                if action == "disable":
                    logger.warning("🛑 ErrorTracker suggests disabling auto_fetch. Waiting 10 min...")
                    await asyncio.sleep(600)
                    et.record_success("auto_fetch")  # reset after wait
                    continue
                elif action == "backoff":
                    extra_wait = min(300, interval_seconds)
                    logger.warning(f"⏳ ErrorTracker suggests backoff. Extra wait: {extra_wait}s")
                    await asyncio.sleep(extra_wait)
                
                logger.info("🔄 [AutoMod] Starting scheduled cycle (Queue Mode)...")
                cycle_result = await fetcher.schedule_jobs(notify_func=_loop_notify)
                
                enqueued = cycle_result.get("enqueued", 0)
                if enqueued > 0:
                    logger.info(f"✅ [AutoMod] Enqueued {enqueued} jobs.")
                
                et.record_success("auto_fetch")
                consecutive_loop_errors = 0
            else:
                logger.debug("💤 [AutoMod] Auto-fetch is disabled in config.")
                
        except Exception as e:
            consecutive_loop_errors += 1
            et.record_error("auto_fetch", "loop_error", str(e))
            logger.error(f"❌ [AutoMod] Error in fetch loop ({consecutive_loop_errors}): {e}", exc_info=True)
            
            # إذا كانت الأخطاء متتالية كثيراً، ننتظر أطول
            if consecutive_loop_errors >= 5:
                extra = min(300, 60 * consecutive_loop_errors)
                logger.warning(f"⏳ Too many loop errors. Extra wait: {extra}s")
                await asyncio.sleep(extra)
            
        # استخراج الفاصل الزمني من الإعدادات (مع قابلية للتحديث الديناميكي)
        interval_seconds = _normalize_auto_fetch_loop_config(config, interval_seconds).get("auto_fetch_interval_seconds", 600)
        
        # Enforce a minimum wait of 5 minutes between cycles regardless of the interval setting
        # This is a safety buffer for Render's limited resources.
        sleep_duration = _compute_loop_sleep_seconds(loop_started_monotonic, interval_seconds)
        rendered_sleep = max(300, sleep_duration) 
        
        logger.info(f"💤 [AutoMod] Next cycle in {rendered_sleep}s (Schedule: {interval_seconds}s, Minimum Cooldown: 300s)")
        await asyncio.sleep(rendered_sleep)


if __name__ == "__main__":
    import sys
    
    # تحديد الفاصل الزمني من سطر الأوامر إن وجد
    interval = 3600
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            pass
            
    print(f"🚀 Starting AutoMod fetcher loop (interval: {interval}s)...")
    try:
        asyncio.run(start_auto_fetch_loop(interval))
    except KeyboardInterrupt:
        print("\n👋 Fetcher stopped by user.")
    except Exception as e:
        print(f"\n💥 Fatal error: {e}")
