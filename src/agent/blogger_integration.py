#!/usr/bin/env python3
"""
Blogger Integration - التعامل مع Blogger API v3
يستخدم نفس بيانات OAuth المستخدمة لقنوات YouTube
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


def _get_blogger_service(creds):
    """إنشاء خدمة Blogger API"""
    from googleapiclient.discovery import build
    return build("blogger", "v3", credentials=creds, cache_discovery=False)


def _creds_from_token_path(token_path: str):
    """تحميل بيانات OAuth من ملف التوكن
    نستخدم نفس SCOPES المعرّفة في uploader.py (تشمل youtube.upload + blogger)
    لضمان تجديد التوكن بشكل صحيح.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from .uploader import SCOPES

    import os
    if not os.path.exists(token_path):
        raise FileNotFoundError(f"Token file not found: {token_path}")

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                logger.debug(f"Refreshed blogger token: {token_path}")
            except Exception as e:
                logger.error(f"Failed to refresh blogger token: {e}")
                raise
        else:
            raise RuntimeError(f"Blogger token invalid and cannot be refreshed: {token_path}")

    return creds


def list_blogs(creds) -> List[Dict[str, Any]]:
    """جلب قائمة المدونات المتاحة للحساب"""
    try:
        service = _get_blogger_service(creds)
        result = service.blogs().listByUser(userId="self").execute()
        blogs = result.get("items", [])
        return [
            {
                "id": b.get("id"),
                "name": b.get("name", "بدون اسم"),
                "url": b.get("url", ""),
                "description": b.get("description", "")[:100],
            }
            for b in blogs
        ]
    except Exception as e:
        logger.error(f"Failed to list blogs: {e}")
        raise


def create_post(creds, blog_id: str, title: str, content: str, labels: List[str] = None) -> Dict[str, Any]:
    """نشر مقال جديد على بلوجر"""
    try:
        service = _get_blogger_service(creds)
        body = {
            "kind": "blogger#post",
            "title": title,
            "content": content,
        }
        if labels:
            body["labels"] = labels

        result = service.posts().insert(
            blogId=blog_id,
            body=body,
            isDraft=False,
        ).execute()

        return {
            "post_id": result.get("id"),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "status": "published",
        }
    except Exception as e:
        logger.error(f"Failed to create blog post: {e}")
        raise


def get_post(creds, blog_id: str, post_id: str) -> Dict[str, Any]:
    """جلب بيانات مقال"""
    try:
        service = _get_blogger_service(creds)
        result = service.posts().get(blogId=blog_id, postId=post_id).execute()
        return {
            "post_id": result.get("id"),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "content": result.get("content", "")[:200],
            "status": result.get("status", ""),
        }
    except Exception as e:
        logger.error(f"Failed to get blog post: {e}")
        raise


def list_posts(creds, blog_id: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """جلب آخر المقالات من مدونة"""
    try:
        service = _get_blogger_service(creds)
        result = service.posts().list(
            blogId=blog_id,
            maxResults=max_results,
            status="live",
        ).execute()
        posts = result.get("items", [])
        return [
            {
                "post_id": p.get("id"),
                "url": p.get("url", ""),
                "title": p.get("title", ""),
                "published": p.get("published", ""),
            }
            for p in posts
        ]
    except Exception as e:
        logger.error(f"Failed to list blog posts: {e}")
        raise


def delete_post(creds, blog_id: str, post_id: str) -> bool:
    """حذف مقال"""
    try:
        service = _get_blogger_service(creds)
        service.posts().delete(blogId=blog_id, postId=post_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to delete blog post: {e}")
        raise


# ==================== المساعدات ====================

def get_channel_token_paths() -> Dict[str, str]:
    """
    جلب جميع مسارات توكنات القنوات المتاحة.
    يعيد dict: {channel_id: token_path}
    يبحث في .data/youtube_tokens/ وفي channel_configs
    """
    from .config import get_project_root
    from .supabase_storage import list_channel_configs
    
    token_map = {}
    project_root = Path(get_project_root())
    
    # 1. البحث في مجلد التوكنات
    token_dir = project_root / ".data" / "youtube_tokens"
    if token_dir.exists():
        for f in token_dir.glob("*.json"):
            try:
                with open(f, "r") as fp:
                    token_data = json.load(fp)
                # محاولة استخراج channel_id من التوكن
                sub = token_data.get("sub", "")
                if sub:
                    token_map[f"yt_{sub}"] = str(f)
                else:
                    token_map[f"file_{f.stem}"] = str(f)
            except Exception:
                token_map[f"file_{f.stem}"] = str(f)
    
    # 2. البحث في channel_configs (للحصول على token_path)
    try:
        configs = list_channel_configs()
        for cfg in (configs or []):
            ch_id = cfg.get("channel_id", "")
            tp = cfg.get("token_path", "")
            if ch_id and tp:
                token_map[ch_id] = tp
    except Exception as e:
        logger.debug(f"Failed to load channel configs for token paths: {e}")
    
    return token_map


def get_blogs_for_token(token_path: str) -> List[Dict]:
    """جلب المدونات المتاحة لتوكن معين"""
    try:
        creds = _creds_from_token_path(token_path)
        blogs = list_blogs(creds)
        return blogs
    except Exception as e:
        logger.error(f"Failed to get blogs for token {token_path}: {e}")
        return []


def publish_article_to_blogger(
    token_path: str,
    blog_id: str,
    title: str,
    content: str,
    labels: List[str] = None,
) -> Dict:
    """
    نشر مقال على بلوجر باستخدام توكن معين.
    يعيد dict يحتوي على post_id و url.
    """
    creds = _creds_from_token_path(token_path)
    result = create_post(creds, blog_id, title, content, labels)
    return result
