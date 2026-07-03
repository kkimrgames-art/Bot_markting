"""
Google Drive Manager - إدارة Google Drive للفيديوهات الجاهزة
يتعامل مع OAuth2 وجلب الفيديوهات من مجلد محدد
"""
import os
import json
import time
import logging
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import load_config, get_project_root
from ..bot.shared_state import oauth_callback_results

logger = logging.getLogger(__name__)

# المجلد المحدد للفيديوهات الجاهزة
GDRIVE_READY_VIDEOS_FOLDER_ID = "1mk4yijB5Mx7vYP_Yuj_vwFsEKq2KDRPF"

# صلاحيات Google Drive
GDRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

# مسار ملف client_secret في جذر المشروع
_CLIENT_SECRET_FILENAME = "client_secret_2_634906081340-7t16q0d78f4e3ssg7nketcj5qia3580c.apps.googleusercontent.com.json"

# مسار ملف التوكن المحلي
_TOKEN_CACHE_DIR = Path(get_project_root()) / ".data" / "gdrive"
_TOKEN_CACHE_PATH = _TOKEN_CACHE_DIR / "token.json"

# جدول Supabase لتخزين بيانات المصادقة
GDRIVE_AUTH_TABLE = "gdrive_auth"

# أنواع الفيديوهات المدعومة
SUPPORTED_VIDEO_MIMETYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
    "video/3gpp",
    "video/mpeg",
}


def _get_client_secrets_path() -> str:
    """البحث عن ملف client_secret في جذر المشروع"""
    root = Path(get_project_root())
    path = root / _CLIENT_SECRET_FILENAME
    if path.exists():
        return str(path)
    # بحث بديل: أي ملف يبدأ بـ client_secret_2
    for f in root.glob("client_secret_2*.json"):
        return str(f)
    return ""


def _find_redirect_uri() -> str:
    """تحديد redirect URI المناسب"""
    # 1. من البيئة
    env_uri = (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    if env_uri:
        if not env_uri.endswith("/oauth2/callback"):
            env_uri = env_uri.rstrip("/") + "/oauth2/callback"
        return env_uri

    # 2. من Render
    external_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if external_url:
        return f"{external_url}/oauth2/callback"

    # 3. الافتراضي (ngrok)
    return "https://your-static.ngrok.app/oauth2/callback"


def _load_token_cache() -> Optional[Credentials]:
    """تحميل التوكن من الكاش المحلي"""
    try:
        if _TOKEN_CACHE_PATH.exists():
            with open(_TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            creds = Credentials(
                token=data.get("token"),
                refresh_token=data.get("refresh_token"),
                token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=data.get("client_id"),
                client_secret=data.get("client_secret"),
                scopes=data.get("scopes"),
            )
            return creds
    except Exception as e:
        logger.debug(f"Failed to load token cache: {e}")
    return None


def _save_token_cache(creds: Credentials) -> None:
    """حفظ التوكن في الكاش المحلي"""
    try:
        _TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
            "saved_at": datetime.utcnow().isoformat(),
        }
        with open(_TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("✅ Google Drive token cached locally.")
    except Exception as e:
        logger.warning(f"⚠️ Failed to cache token: {e}")


async def _save_token_to_supabase(creds: Credentials) -> bool:
    """حفظ التوكن في Supabase (جدول gdrive_auth)"""
    try:
        from .supabase_client import supabase_upsert, USE_SUPABASE
        if not USE_SUPABASE:
            logger.warning("Supabase not available, token saved locally only.")
            return False

        payload = {
            "id": "gdrive_main",
            "token": creds.token or "",
            "refresh_token": creds.refresh_token or "",
            "token_uri": creds.token_uri or "https://oauth2.googleapis.com/token",
            "client_id": creds.client_id or "",
            "client_secret": creds.client_secret or "",
            "scopes": json.dumps(list(creds.scopes or [])),
            "updated_at": datetime.utcnow().isoformat(),
        }
        await asyncio.to_thread(supabase_upsert, GDRIVE_AUTH_TABLE, payload, on_conflict="id")
        logger.info("✅ Google Drive token saved to Supabase.")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Failed to save token to Supabase: {e}")
        return False


async def _load_token_from_supabase() -> Optional[Credentials]:
    """تحميل التوكن من Supabase"""
    try:
        from .supabase_client import supabase_select_one, USE_SUPABASE
        if not USE_SUPABASE:
            return None

        result = await asyncio.to_thread(
            supabase_select_one, GDRIVE_AUTH_TABLE, "id", "gdrive_main"
        )
        if not result:
            return None

        creds = Credentials(
            token=result.get("token", ""),
            refresh_token=result.get("refresh_token", ""),
            token_uri=result.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=result.get("client_id", ""),
            client_secret=result.get("client_secret", ""),
            scopes=json.loads(result.get("scopes", "[]")),
        )
        logger.info("✅ Google Drive token loaded from Supabase.")
        return creds
    except Exception as e:
        logger.warning(f"⚠️ Failed to load Google Drive token from Supabase: {e}")
        return None


def _refresh_credentials(creds: Credentials) -> Credentials:
    """تجديد التوكن إذا انتهت صلاحيته"""
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("🔄 Google Drive token refreshed.")
            _save_token_cache(creds)
            return creds
        except Exception as e:
            logger.error(f"❌ Failed to refresh Google Drive token: {e}")
            raise
    return creds


async def get_credentials() -> Optional[Credentials]:
    """
    الحصول على بيانات اعتماد Google Drive صالحة.
    يتحقق بالترتيب: كاش محلي → Supabase → None
    """
    # 1. الكاش المحلي
    creds = _load_token_cache()
    if creds and creds.valid:
        return creds
    if creds and creds.refresh_token:
        try:
            creds = _refresh_credentials(creds)
            if creds.valid:
                return creds
        except Exception:
            pass

    # 2. Supabase
    creds = await _load_token_from_supabase()
    if creds and creds.valid:
        _save_token_cache(creds)
        return creds
    if creds and creds.refresh_token:
        try:
            creds = _refresh_credentials(creds)
            if creds.valid:
                _save_token_cache(creds)
                return creds
        except Exception:
            pass

    return None


async def save_credentials(creds: Credentials) -> None:
    """حفظ بيانات الاعتماد في الكاش المحلي و Supabase"""
    _save_token_cache(creds)
    await _save_token_to_supabase(creds)


def create_auth_flow():
    """
    إنشاء تدفق OAuth2 للمصادقة مع Google Drive.
    Returns: (flow, auth_url, redirect_uri)
    """
    from google_auth_oauthlib.flow import Flow

    client_secrets_path = _get_client_secrets_path()
    if not client_secrets_path:
        raise FileNotFoundError("client_secret JSON file not found in project root.")

    redirect_uri = _find_redirect_uri()

    # قراءة ملف client_secret و تعديل redirect_uris
    with open(client_secrets_path, "r", encoding="utf-8") as f:
        client_data = json.load(f)

    client_type = "web" if "web" in client_data else "installed"
    client_config = dict(client_data.get(client_type, {}))
    client_config["redirect_uris"] = [redirect_uri]

    flow = Flow.from_client_config(
        {client_type: client_config},
        scopes=GDRIVE_SCOPES,
        redirect_uri=redirect_uri,
    )

    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )

    return flow, auth_url, redirect_uri


def exchange_code(flow, authorization_response: str) -> Credentials:
    """تبادل كود المصادقة مع التوكن"""
    if "http://" in authorization_response and "https://" in flow.redirect_uri:
        authorization_response = authorization_response.replace("http://", "https://", 1)

    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Warning:
        pass
    finally:
        os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)

    return flow.credentials


def get_drive_service(creds: Credentials):
    """إنشاء خدمة Google Drive API"""
    return build("drive", "v3", credentials=creds, static_discovery=False)


async def list_videos_in_folder(
    folder_id: str = GDRIVE_READY_VIDEOS_FOLDER_ID,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    """
    جلب قائمة الفيديوهات من مجلد Google Drive مرتبة من الأحدث للأقدم.
    """
    creds = await get_credentials()
    if not creds:
        raise RuntimeError("Google Drive not authenticated. Please authenticate first.")

    service = get_drive_service(creds)

    # البحث عن الفيديوهات في المجلد
    query = (
        f"'{folder_id}' in parents "
        f"and mimeType contains 'video/' "
        f"and trashed = false"
    )

    results = []
    page_token = None

    while len(results) < max_results:
        try:
            response = await asyncio.to_thread(
                service.files().list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, thumbnailLink, webContentLink, webViewLink)",
                    pageToken=page_token,
                    pageSize=min(100, max_results - len(results)),
                    orderBy="createdTime desc",
                ).execute
            )
        except Exception as e:
            logger.error(f"❌ Failed to list Drive files: {e}")
            break

        files = response.get("files", [])
        for f in files:
            mime = f.get("mimeType", "")
            if mime not in SUPPORTED_VIDEO_MIMETYPES and not mime.startswith("video/"):
                continue
            results.append({
                "id": f["id"],
                "name": f.get("name", "Untitled"),
                "mime_type": mime,
                "size": int(f.get("size", 0)),
                "created_time": f.get("createdTime", ""),
                "modified_time": f.get("modifiedTime", ""),
                "thumbnail": f.get("thumbnailLink", ""),
                "download_link": f.get("webContentLink", ""),
                "view_link": f.get("webViewLink", ""),
            })

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


async def download_video(
    file_id: str,
    destination_path: str,
    progress_callback=None,
) -> str:
    """
    تحميل فيديو من Google Drive إلى مسار محلي.
    Returns: المسار المحلي للملف المحمل.
    """
    creds = await get_credentials()
    if not creds:
        raise RuntimeError("Google Drive not authenticated.")

    service = get_drive_service(creds)

    # جلب معلومات الملف
    file_info = await asyncio.to_thread(
        service.files().get(fileId=file_id, fields="name, mimeType, size").execute
    )

    filename = file_info.get("name", f"{file_id}.mp4")
    os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)

    # تحميل الملف
    request = service.files().get_media(fileId=file_id)

    from googleapiclient.http import MediaIoBaseDownload
    import io

    with open(destination_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if progress_callback and status:
                progress_callback(status.progress())

    logger.info(f"✅ Downloaded: {filename} -> {destination_path}")
    return destination_path


async def get_video_thumbnail(file_id: str) -> Optional[str]:
    """تحميل الصورة المصغرة لفيديو من Google Drive"""
    creds = await get_credentials()
    if not creds:
        return None

    service = get_drive_service(creds)
    try:
        response = await asyncio.to_thread(
            service.files().get(fileId=file_id, fields="thumbnailLink").execute
        )
        return response.get("thumbnailLink")
    except Exception as e:
        logger.debug(f"Failed to get thumbnail: {e}")
        return None


async def ensure_auth_table_exists() -> bool:
    """التأكد من وجود جدول gdrive_auth في Supabase"""
    try:
        from .supabase_client import USE_SUPABASE
        if not USE_SUPABASE:
            logger.info("Supabase not available, skipping table check.")
            return True

        # محاولة إدراج سجل فارغ للتأكد من أن الجدول موجود
        # إذا لم يكن موجوداً، سنحتاج لإنشائه يدوياً
        from .supabase_client import supabase_select_one
        result = await asyncio.to_thread(
            supabase_select_one, GDRIVE_AUTH_TABLE, "id", "gdrive_main"
        )
        # النتيجة قد تكون None إذا لم يكن هناك سجل، لكن هذا يعني أن الجدول موجود
        logger.info("✅ gdrive_auth table verified.")
        return True
    except Exception as e:
        logger.warning(f"⚠️ gdrive_auth table check failed: {e}")
        logger.info("📋 Please create the gdrive_auth table in Supabase. See setup_gdrive_auth.sql.")
        return False


# SQL لإنشاء الجدول
CREATE_GDRIVE_AUTH_TABLE_SQL = """
-- إنشاء جدول مصادقة Google Drive
CREATE TABLE IF NOT EXISTS gdrive_auth (
    id TEXT PRIMARY KEY DEFAULT 'gdrive_main',
    token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    token_uri TEXT NOT NULL DEFAULT 'https://oauth2.googleapis.com/token',
    client_id TEXT NOT NULL DEFAULT '',
    client_secret TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS policy (Supabase)
ALTER TABLE gdrive_auth ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow all for authenticated" ON gdrive_auth
    FOR ALL
    USING (true)
    WITH CHECK (true);
"""
