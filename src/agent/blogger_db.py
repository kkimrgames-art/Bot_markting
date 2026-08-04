#!/usr/bin/env python3
"""
Blogger DB - طبقة التخزين لبيانات ناشر مقالات البلوجر
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
_LOCAL_BLOGGER_PATH = _PROJECT_ROOT / ".data" / "blogger_data.json"
_LOCK = threading.RLock()


# ==================== التخزين المحلي (Fallback) ====================

def _load_local() -> Dict:
    """تحميل البيانات المحلية"""
    if not _LOCAL_BLOGGER_PATH.exists():
        return {"links": {}, "articles": []}
    try:
        with open(_LOCAL_BLOGGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"links": {}, "articles": []}


def _save_local(data: Dict):
    """حفظ البيانات محلياً"""
    _LOCAL_BLOGGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_BLOGGER_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ==================== Blogger Links (ربط المصادر بالمدونات) ====================

def get_blogger_links(source_id: str = None, channel_id: str = None) -> List[Dict]:
    """جلب روابط البلوجر مع فلتر اختياري"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                filters = {}
                if source_id:
                    filters["source_id"] = source_id
                if channel_id:
                    filters["channel_id"] = channel_id
                rows = supabase_select("blogger_links", filters if filters else None) or []
                if rows:
                    return rows
            except Exception as e:
                logger.warning(f"Supabase blogger_links select failed: {e}")

        # Fallback محلي
        local = _load_local()
        links = list(local.get("links", {}).values())
        if source_id:
            links = [l for l in links if l.get("source_id") == source_id]
        if channel_id:
            links = [l for l in links if l.get("channel_id") == channel_id]
        return links


def get_blogger_link(link_id: str) -> Optional[Dict]:
    """جلب رابط بلوجر واحد"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                row = supabase_select_one("blogger_links", "id", link_id)
                if row:
                    return row
            except Exception as e:
                logger.warning(f"Supabase blogger_links select_one failed: {e}")

        local = _load_local()
        return local.get("links", {}).get(link_id)


def save_blogger_link(data: Dict) -> bool:
    """حفظ/تحديث رابط بلوجر"""
    with _LOCK:
        link_id = data.get("id") or str(uuid.uuid4())
        data["id"] = link_id
        data["updated_at"] = _now_iso()
        if "created_at" not in data or not data["created_at"]:
            data["created_at"] = _now_iso()

        # ضمان الحقول المطلوبة
        data.setdefault("source_id", "")
        data.setdefault("channel_id", "")
        data.setdefault("blog_id", "")
        data.setdefault("blog_name", "")
        data.setdefault("blog_url", "")
        data.setdefault("enabled", True)
        data.setdefault("link_title", "🔗 اقرأ المزيد على المدونة")
        data.setdefault("ai_mode", "ai_prompt")
        data.setdefault("ai_prompt", "")
        data.setdefault("templates_order", "sequential")
        data.setdefault("templates", "[]")
        data.setdefault("template_index", 0)
        data.setdefault("article_language", "ar")
        data.setdefault("token_path", "")

        if USE_SUPABASE:
            try:
                ok = supabase_upsert("blogger_links", data, key_field="id")
                if ok:
                    return True
            except Exception as e:
                logger.warning(f"Supabase blogger_links upsert failed: {e}")

        # Fallback محلي
        local = _load_local()
        local.setdefault("links", {})[link_id] = data
        _save_local(local)
        return True


def delete_blogger_link(link_id: str) -> bool:
    """حذف رابط بلوجر"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                supabase_delete("blogger_links", "id", link_id)
            except Exception as e:
                logger.warning(f"Supabase blogger_links delete failed: {e}")

        local = _load_local()
        local.get("links", {}).pop(link_id, None)
        # حذف المقالات المرتبطة
        local["articles"] = [a for a in local.get("articles", []) if a.get("link_id") != link_id]
        _save_local(local)
        return True


def get_active_blogger_links_for_source(source_id: str) -> List[Dict]:
    """جلب الروابط النشطة لمصدر معين"""
    links = get_blogger_links(source_id=source_id)
    return [l for l in links if l.get("enabled", True)]


# ==================== Blogger Articles (المقالات المنشورة) ====================

def save_blogger_article(data: Dict) -> bool:
    """حفظ سجل مقال منشور"""
    with _LOCK:
        article_id = data.get("id") or str(uuid.uuid4())
        data["id"] = article_id
        data["created_at"] = data.get("created_at") or _now_iso()

        if USE_SUPABASE:
            try:
                supabase_upsert("blogger_articles", data, key_field="id")
            except Exception as e:
                logger.warning(f"Supabase blogger_articles upsert failed: {e}")

        local = _load_local()
        local.setdefault("articles", []).append(data)
        _save_local(local)
        return True


def get_blogger_articles(link_id: str = None, limit: int = 20) -> List[Dict]:
    """جلب سجل المقالات"""
    with _LOCK:
        if USE_SUPABASE:
            try:
                filters = {"link_id": link_id} if link_id else None
                rows = supabase_select("blogger_articles", filters) or []
                return rows[:limit]
            except Exception as e:
                logger.warning(f"Supabase blogger_articles select failed: {e}")

        local = _load_local()
        articles = local.get("articles", [])
        if link_id:
            articles = [a for a in articles if a.get("link_id") == link_id]
        return articles[-limit:]


def increment_template_index(link_id: str):
    """زيادة مؤشر القالب التالي (للوضع الترتيبي)"""
    link = get_blogger_link(link_id)
    if not link:
        return
    templates = link.get("templates", "[]")
    try:
        templates_list = json.loads(templates) if isinstance(templates, str) else templates
        count = len(templates_list)
    except Exception:
        count = 0

    current = link.get("template_index", 0) or 0
    next_idx = (current + 1) % max(count, 1)
    link["template_index"] = next_idx
    save_blogger_link(link)


# ==================== إنشاء الجداول تلقائياً ====================

def ensure_tables_exist():
    """محاولة إنشاء الجداول في Supabase إذا لم تكن موجودة (يحتاج DB password)"""
    if not USE_SUPABASE:
        # إنشاء ملف محلي فارغ
        if not _LOCAL_BLOGGER_PATH.exists():
            _save_local({"links": {}, "articles": []})
            logger.info("✅ Created local blogger data file")
        return

    # محاولة قراءة من الجدول - إذا فشل يعني غير موجود
    try:
        supabase_select("blogger_links")
        logger.info("✅ blogger_links table exists")
    except Exception:
        logger.warning(
            "⚠️ جدول blogger_links غير موجود في Supabase.\n"
            "   شغّل SQL التالي في Supabase Dashboard > SQL Editor:\n"
            "   الملف: scripts/create_blogger_tables.sql"
        )
