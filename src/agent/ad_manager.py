"""
Video Ad Manager
================
Manages advertisement videos that can be inserted into processed videos.

Features:
  - Per-source ad configuration (each source can have its own ad)
  - Ad position: "end" (last N seconds) or "middle" (midpoint)
  - Ad timing: 2, 5, or 10 seconds (or custom)
  - "Continue after ad" option:
      ON  = ad plays, then remaining video continues
      OFF = ad plays, then video ends (truncated)
  - Ad video upload via Telegram
  - Persistent storage (local + Supabase)
  - ffmpeg-based insertion (scale ad to match video dimensions)

Storage:
  - Ad video files: .data/ads/{source_id}.mp4
  - Ad settings: .data/ads/ads_config.json + Supabase sync
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# ─── Paths ───
_ADS_DIR = Path(os.getenv("ADS_DIR", ".data/ads"))
_ADS_CONFIG_FILE = _ADS_DIR / "ads_config.json"


def _ensure_dirs():
    _ADS_DIR.mkdir(parents=True, exist_ok=True)


def _get_ad_video_path(source_id: str) -> Path:
    """Get the file path for a source's ad video."""
    _ensure_dirs()
    # Sanitize source_id for filesystem
    safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_id)
    return _ADS_DIR / f"{safe_id}.mp4"


def _load_config() -> Dict[str, Dict[str, Any]]:
    """Load ad configuration from local JSON file."""
    if not _ADS_CONFIG_FILE.exists():
        return {}
    try:
        with open(_ADS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.warning(f"Failed to load ads config: {e}")
        return {}


def _save_config(config: Dict[str, Dict[str, Any]]) -> None:
    """Save ad configuration to local JSON + Supabase."""
    _ensure_dirs()
    try:
        with open(_ADS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save ads config: {e}")

    # Sync to Supabase (best-effort)
    try:
        from .supabase_client import USE_SUPABASE, is_online, supabase_upsert
        if USE_SUPABASE and is_online():
            supabase_upsert(
                "video_ads_config",
                {
                    "id": "main",
                    "config": json.dumps(config, ensure_ascii=False),
                    "updated_at": datetime.now().isoformat(),
                },
                "id",
            )
    except Exception:
        pass


# ─── Public API ───

def get_ad_config(source_id: str) -> Dict[str, Any]:
    """Get ad configuration for a source.

    Returns:
        Dict with keys: enabled, position, timing, continue_after_ad, has_video
    """
    with _LOCK:
        config = _load_config()
        entry = config.get(source_id, {})
        ad_path = _get_ad_video_path(source_id)
        return {
            "enabled": bool(entry.get("enabled", False)),
            "position": str(entry.get("position", "end")),  # "end" or "middle"
            "timing": float(entry.get("timing", 5.0)),  # seconds: 2, 5, 10, or custom
            "continue_after_ad": bool(entry.get("continue_after_ad", True)),
            "has_video": ad_path.exists(),
            "video_path": str(ad_path) if ad_path.exists() else None,
            "added_at": entry.get("added_at"),
        }


def set_ad_config(
    source_id: str,
    *,
    enabled: Optional[bool] = None,
    position: Optional[str] = None,
    timing: Optional[float] = None,
    continue_after_ad: Optional[bool] = None,
) -> bool:
    """Update ad configuration for a source. Only updates provided fields.

    Args:
        source_id: the source ID (or channel_id for channel-wide ads)
        enabled: True/False to enable/disable
        position: "end" or "middle"
        timing: ad duration in seconds (2, 5, 10, or custom)
        continue_after_ad: if True, video continues after ad; if False, video ends

    Returns:
        True if config was updated.
    """
    with _LOCK:
        config = _load_config()
        entry = config.get(source_id, {})

        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if position is not None:
            pos = str(position).strip().lower()
            if pos not in ("end", "middle"):
                pos = "end"
            entry["position"] = pos
        if timing is not None:
            try:
                t = float(timing)
                entry["timing"] = max(1.0, min(60.0, t))  # clamp 1-60s
            except Exception:
                pass
        if continue_after_ad is not None:
            entry["continue_after_ad"] = bool(continue_after_ad)

        entry["updated_at"] = datetime.now().isoformat()
        if "added_at" not in entry:
            entry["added_at"] = entry["updated_at"]

        config[source_id] = entry
        _save_config(config)
        return True


def save_ad_video(source_id: str, video_file_path: str) -> bool:
    """Save an uploaded ad video for a source.

    Args:
        source_id: the source ID.
        video_file_path: path to the uploaded video file (will be copied).

    Returns:
        True if saved successfully.
    """
    with _LOCK:
        if not video_file_path or not os.path.exists(video_file_path):
            return False
        dest = _get_ad_video_path(source_id)
        try:
            shutil.copy2(video_file_path, str(dest))
            logger.info(f"✅ Ad video saved for source {source_id[:20]}... -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to save ad video: {e}")
            return False


def delete_ad_video(source_id: str) -> bool:
    """Delete the ad video for a source."""
    with _LOCK:
        ad_path = _get_ad_video_path(source_id)
        if ad_path.exists():
            try:
                ad_path.unlink()
                logger.info(f"🗑️ Ad video deleted for source {source_id[:20]}...")
            except Exception:
                pass
        # Also disable in config
        config = _load_config()
        if source_id in config:
            config[source_id]["enabled"] = False
            config[source_id]["has_video"] = False
            _save_config(config)
        return True


def list_all_ads() -> List[Dict[str, Any]]:
    """List all configured ads (for UI display)."""
    with _LOCK:
        config = _load_config()
        result = []
        for source_id, entry in config.items():
            ad_path = _get_ad_video_path(source_id)
            result.append({
                "source_id": source_id,
                "enabled": bool(entry.get("enabled", False)),
                "position": entry.get("position", "end"),
                "timing": entry.get("timing", 5.0),
                "continue_after_ad": entry.get("continue_after_ad", True),
                "has_video": ad_path.exists(),
                "added_at": entry.get("added_at"),
                "updated_at": entry.get("updated_at"),
            })
        return result


def get_status_text() -> str:
    """Return Arabic status text for Telegram UI."""
    with _LOCK:
        ads = list_all_ads()
    if not ads:
        return (
            "📺 <b>إدارة الإعلانات</b>\n\n"
            "📭 لا توجد إعلانات مُضافة بعد.\n\n"
            "💡 <i>يمكنك إضافة إعلان لأي مصدر من قائمة المصادر.</i>"
        )

    text = "📺 <b>إدارة الإعلانات</b>\n\n"
    text += f"📊 إجمالي الإعلانات: <b>{len(ads)}</b>\n\n"

    enabled_count = sum(1 for a in ads if a["enabled"])
    text += f"✅ مفعّل: <b>{enabled_count}</b>\n"
    text += f"❌ معطّل: <b>{len(ads) - enabled_count}</b>\n\n"

    for ad in ads[:10]:
        icon = "✅" if ad["enabled"] else "❌"
        pos_text = "آخر الفيديو" if ad["position"] == "end" else "منتصف الفيديو"
        cont_text = "نعم" if ad["continue_after_ad"] else "لا"
        video_icon = "🎬" if ad["has_video"] else "⚠️"
        text += (
            f"{icon} {video_icon} <code>{ad['source_id'][:25]}...</code>\n"
            f"   📍 {pos_text} | ⏱ {ad['timing']}s | 🔄 تكملة: {cont_text}\n"
        )

    if len(ads) > 10:
        text += f"\n<i>...و {len(ads) - 10} إعلان آخر</i>\n"

    return text
