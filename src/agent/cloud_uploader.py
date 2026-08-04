#!/usr/bin/env python3
"""
Cloud Uploader - رفع نسخ من الفيديوهات لخدمات التخزين السحابي
يدعم: Google Drive, Supabase Storage, Claudeflare (S3-compatible)
يعيد رابط تحميل دائم (public URL).
"""
import os
import json
import logging
import mimetypes
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_invalid_scope_error(exc: Exception) -> bool:
    msg = str(exc)
    return "invalid_scope" in msg or "invalid_scope" in repr(getattr(exc, "error_details", ""))


# ==================== Google Drive ====================

def upload_to_google_drive(
    token_path: str,
    file_path: str,
    file_name: str = "",
    folder_id: str = "",
) -> Optional[Dict[str, str]]:
    """
    رفع ملف إلى Google Drive وإتاحته للعامة (رابط دائم).
    يعيد {"url": "...", "file_id": "...", "service": "google_drive"}
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from .uploader import SCOPES

        # تحميل التوكن
        if not os.path.exists(token_path):
            raise FileNotFoundError(f"Token not found: {token_path}")
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            else:
                raise RuntimeError(f"Token invalid: {token_path}")

        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # تحديد اسم الملف
        display_name = file_name or os.path.basename(file_path)
        mime_type = mimetypes.guess_type(file_path)[0] or "video/mp4"

        # بيانات الملف
        file_metadata = {"name": display_name}
        if folder_id:
            file_metadata["parents"] = [folder_id]

        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

        # رفع الملف
        uploaded = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webContentLink",
        ).execute()

        file_id = uploaded.get("id")
        web_link = uploaded.get("webContentLink")

        if not file_id:
            logger.error("Google Drive upload returned no file_id")
            return None

        # إتاحة الملف للعامة للحصول على رابط دائم
        try:
            service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
            ).execute()
        except Exception as perm_err:
            logger.warning(f"Failed to set Drive file public: {perm_err}")

        # الحصول على الرابط بعد الإتاحة
        if not web_link:
            try:
                f = service.files().get(fileId=file_id, fields="webContentLink").execute()
                web_link = f.get("webContentLink")
            except Exception:
                pass

        # بديل: رابط export إذا لم يتوفر webContentLink
        if not web_link:
            web_link = f"https://drive.google.com/uc?export=download&id={file_id}"

        logger.info(f"Drive upload OK: {display_name} -> {web_link[:80]}")
        return {
            "url": web_link,
            "file_id": file_id,
            "service": "google_drive",
        }

    except Exception as e:
        logger.error(f"Google Drive upload failed: {e}", exc_info=True)
        return None


def list_drive_folders(token_path: str) -> List[Dict]:
    """جلب قائمة المجلدات من Google Drive"""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from .uploader import SCOPES

        if not os.path.exists(token_path):
            return []
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        results = service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=50,
            fields="files(id, name)",
        ).execute()

        return [
            {"id": f.get("id"), "name": f.get("name", "بدون اسم")}
            for f in results.get("files", [])
        ]
    except Exception as e:
        if _is_invalid_scope_error(e):
            raise ValueError(
                "invalid_scope: توكن Google الحالي لا يحتوي على صلاحية Google Drive. "
                "يرجى حذف ملف التوكن ثم إعادة ربط القناة YouTube لمنح صلاحيات Drive."
            ) from e
        logger.error(f"Failed to list Drive folders: {e}")
        return []


# ==================== Supabase Storage ====================

def upload_to_supabase(
    bucket_name: str,
    file_path: str,
    file_name: str = "",
) -> Optional[Dict[str, str]]:
    """
    رفع ملف إلى Supabase Storage.
    يتطلب أن تكون الحاوية public أو ننشئ signed URL طويل الأمد.
    """
    try:
        from .supabase_client import (
            supabase_storage_upload,
            supabase_storage_create_signed_url,
            _get_supabase,
            USE_SUPABASE,
            SUPABASE_URL,
        )

        if not USE_SUPABASE:
            logger.warning("Supabase is not enabled")
            return None

        display_name = file_name or os.path.basename(file_path)
        # مسار فريد داخل الحاوية
        import uuid
        ext = os.path.splitext(display_name)[1]
        unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
        object_path = f"cloud_uploads/{unique_name}"

        # رفع الملف
        content_type = mimetypes.guess_type(file_path)[0] or "video/mp4"
        upload_result = supabase_storage_upload(
            bucket=bucket_name,
            object_path=object_path,
            file_path=file_path,
            content_type=content_type,
            upsert=True,
        )

        if not upload_result:
            logger.error(f"Supabase upload returned None for bucket={bucket_name}")
            return None

        # محاولة الحصول على رابط عام
        url = _try_get_public_url(bucket_name, object_path)

        # إذا لم يكن عاماً، نستخدم signed URL طويل الأمد (10 سنوات)
        if not url:
            url = supabase_storage_create_signed_url(
                bucket=bucket_name,
                object_path=object_path,
                expires_in=315360000,  # ~10 سنوات
            )

        if not url:
            # بديل أخير: بناء رابط مباشر (يعمل فقط للحاويات العامة)
            url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{object_path}"

        logger.info(f"Supabase upload OK: {display_name} -> {url[:80]}")
        return {
            "url": url,
            "object_path": object_path,
            "service": "supabase",
            "bucket": bucket_name,
        }

    except Exception as e:
        logger.error(f"Supabase storage upload failed: {e}", exc_info=True)
        return None


def _try_get_public_url(bucket: str, object_path: str) -> Optional[str]:
    """محاولة الحصول على رابط عام من Supabase"""
    try:
        from .supabase_client import _get_supabase, SUPABASE_URL, USE_SUPABASE
        if not USE_SUPABASE:
            return None
        client = _get_supabase()
        if not client:
            return None
        obj = str(object_path).strip().lstrip("/")
        resp = client.storage.from_(bucket).get_public_url(obj)
        if isinstance(resp, str) and resp.startswith("http"):
            return resp
        # بعض الإصدارات تعيد dict
        if isinstance(resp, dict):
            url = resp.get("publicUrl") or resp.get("publicURL") or resp.get("url")
            if url and url.startswith("http"):
                return url
    except Exception:
        pass
    return None


def list_supabase_buckets() -> List[Dict]:
    """جلب قائمة حاويات Supabase Storage"""
    try:
        from .supabase_client import _get_supabase, USE_SUPABASE
        if not USE_SUPABASE:
            return []
        client = _get_supabase()
        if not client:
            return []

        # استخدام REST API مباشرة لسرد الحاويات
        import httpx
        url = f"{client.supabase_url}/storage/v1/bucket"
        headers = {
            "apikey": client.supabase_key,
            "Authorization": f"Bearer {client.supabase_key}",
        }
        resp = httpx.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            buckets = resp.json()
            return [
                {"id": b.get("id"), "name": b.get("name"), "public": b.get("public", False)}
                for b in buckets
            ]
    except Exception as e:
        logger.error(f"Failed to list Supabase buckets: {e}")
    return []


# ==================== Claudeflare (S3-compatible) ====================

def upload_to_claudeflare(
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    file_path: str,
    file_name: str = "",
    region: str = "auto",
) -> Optional[Dict[str, str]]:
    """
    رفع ملف إلى Claudeflare (Cloudflare R2 / S3-compatible).
    يعيد رابط تحميل دائم.
    """
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        display_name = file_name or os.path.basename(file_path)
        ext = os.path.splitext(display_name)[1]
        import uuid
        unique_name = f"cloud_uploads/{uuid.uuid4().hex[:12]}{ext}"

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or "auto",
            config=BotoConfig(signature_version="s3v4"),
        )

        content_type = mimetypes.guess_type(file_path)[0] or "video/mp4"
        s3.upload_file(
            file_path,
            bucket,
            unique_name,
            ExtraArgs={"ContentType": content_type},
        )

        # محاولة إنشاء رابط عام
        url = ""
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": unique_name},
                ExpiresIn=315360000,  # ~10 سنوات
            )
        except Exception:
            pass

        if not url:
            # بناء رابط مباشر (يعمل إذا كانت الحاوية عامة)
            url = f"{endpoint.rstrip('/')}/{bucket}/{unique_name}"

        logger.info(f"Claudeflare upload OK: {display_name} -> {url[:80]}")
        return {
            "url": url,
            "key": unique_name,
            "service": "claudeflare",
            "bucket": bucket,
        }

    except ImportError:
        logger.error("boto3 is not installed. Run: pip install boto3")
        return None
    except Exception as e:
        logger.error(f"Claudeflare upload failed: {e}", exc_info=True)
        return None


# ==================== Orchestrator ====================

def upload_video_to_cloud(
    config: Dict[str, Any],
    video_path: str,
    video_title: str = "",
) -> Optional[Dict[str, str]]:
    """
    رفع فيديو إلى الخدمة السحابية حسب الإعدادات.
    يعيد {"url": "...", "service": "...", ...} أو None.
    """
    service = config.get("service", "")
    video_path = str(video_path)

    if not os.path.exists(video_path):
        logger.error(f"Video file not found: {video_path}")
        return None

    if service == "google_drive":
        token_path = config.get("token_path", "")
        if not token_path:
            logger.warning("Google Drive config missing token_path")
            return None
        return upload_to_google_drive(
            token_path=token_path,
            file_path=video_path,
            file_name=video_title or "",
            folder_id=config.get("drive_folder_id", ""),
        )

    elif service == "supabase":
        bucket = config.get("bucket_name", "")
        if not bucket:
            logger.warning("Supabase config missing bucket_name")
            return None
        return upload_to_supabase(
            bucket_name=bucket,
            file_path=video_path,
            file_name=video_title or "",
        )

    elif service == "claudeflare":
        endpoint = config.get("claudflare_endpoint", "")
        access_key = config.get("claudflare_access_key", "")
        secret_key = config.get("claudflare_secret_key", "")
        bucket = config.get("claudflare_bucket", "")
        if not all([endpoint, access_key, secret_key, bucket]):
            logger.warning("Claudeflare config missing required fields")
            return None
        return upload_to_claudeflare(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            file_path=video_path,
            file_name=video_title or "",
            region=config.get("claudflare_region", "auto"),
        )

    else:
        logger.warning(f"Unknown cloud service: {service}")
        return None
