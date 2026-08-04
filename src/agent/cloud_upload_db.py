#!/usr/bin/env python3
"""
Cloud Upload DB - طبقة التخزين لإعدادات رفع الفيديوهات السحابية
يدعم Supabase مع Fallback محلي (JSON)
"""
import os
import json
import uuid
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

from .config import get_project_root
from .supabase_client import (
    supabase_upsert, supabase_select, supabase_select_one,
    supabase_delete, USE_SUPABASE,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(get_project_root())
_LOCAL_PATH = _PROJECT_ROOT / ".data" / "cloud_upload_data.json"
_LOCK = threading.RLock()


def _load_local() -> Dict:
    if not _LOCAL_PATH.exists():
        return {"configs": {}}
    try:
        with open(_LOCAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"configs": {}}


def _save_local(data: Dict):
    _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ==================== Cloud Upload Configs ====================

def get_cloud_configs(source_id: str = None) -> List[Dict]:
    """جلب إعدادات الرفع السحابي مع فلتر اختياري"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                filters = {"source_id": source_id} if source_id else None
                rows = supabase_select("cloud_upload_configs", filters) or []
                if rows:
                    return rows
            except Exception as e:
                logger.warning(f"Supabase cloud_upload_configs select failed: {e}")

        local = _load_local()
        configs = list(local.get("configs", {}).values())
        if source_id:
            configs = [c for c in configs if c.get("source_id") == source_id]
        return configs


def get_cloud_config(config_id: str) -> Optional[Dict]:
    """جلب إعداد سحابي واحد"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                row = supabase_select_one("cloud_upload_configs", "id", config_id)
                if row:
                    return row
            except Exception as e:
                logger.warning(f"Supabase cloud_upload_configs select_one failed: {e}")

        local = _load_local()
        return local.get("configs", {}).get(config_id)


def save_cloud_config(data: Dict) -> bool:
    """حفظ/تحديث إعداد رفع سحابي"""
    with _LOCK:
        config_id = data.get("id") or str(uuid.uuid4())
        data["id"] = config_id
        data["updated_at"] = _now_iso()
        if "created_at" not in data or not data["created_at"]:
            data["created_at"] = _now_iso()

        # حقول افتراضية
        data.setdefault("source_id", "")
        data.setdefault("channel_id", "")
        data.setdefault("service", "")  # google_drive | supabase | claudeflare
        data.setdefault("service_label", "")
        data.setdefault("token_path", "")  # للـ Google Drive
        data.setdefault("drive_folder_id", "")  # للـ Google Drive
        data.setdefault("bucket_name", "")  # للـ Supabase
        data.setdefault("claudflare_endpoint", "")  # للـ Claudeflare
        data.setdefault("claudflare_access_key", "")
        data.setdefault("claudflare_secret_key", "")
        data.setdefault("claudflare_bucket", "")
        data.setdefault("claudflare_region", "")
        data.setdefault("enabled", True)
        data.setdefault("shorten_link", False)
        data.setdefault("shorten_api_token", "")
        data.setdefault("link_to_blogger", False)
        data.setdefault("blogger_link_position", "bottom")  # top | middle | bottom

        if USE_SUPABASE:
            try:
                ok = supabase_upsert("cloud_upload_configs", data, key_field="id")
                if ok:
                    return True
            except Exception as e:
                logger.warning(f"Supabase cloud_upload_configs upsert failed: {e}")

        local = _load_local()
        local.setdefault("configs", {})[config_id] = data
        _save_local(local)
        return True


def delete_cloud_config(config_id: str) -> bool:
    """حذف إعداد رفع سحابي"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                supabase_delete("cloud_upload_configs", "id", config_id)
            except Exception as e:
                logger.warning(f"Supabase cloud_upload_configs delete failed: {e}")

        local = _load_local()
        local.get("configs", {}).pop(config_id, None)
        _save_local(local)
        return True


def get_active_cloud_configs_for_source(source_id: str) -> List[Dict]:
    """جلب الإعدادات النشطة لمصدر معين"""
    configs = get_cloud_configs(source_id=source_id)
    return [c for c in configs if c.get("enabled", True)]
