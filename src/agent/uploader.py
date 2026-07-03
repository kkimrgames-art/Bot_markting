from __future__ import annotations

import json
import os
import time
import logging
import hashlib
import threading
import re
import subprocess
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
try:
    from googleapiclient.errors import HttpError
except Exception:  # pragma: no cover
    HttpError = None
from google.auth.transport.requests import Request

from .config import Config
from .ffmpeg_utils import ffprobe_bin

from ..bot.persistence import load_state, save_state, update_state

logger = logging.getLogger(__name__)


def _sanitize_video_tags(tags: Optional[list[str]]) -> list[str]:
    try:
        raw = tags or []
    except Exception:
        raw = []

    out: list[str] = []
    seen: set[str] = set()
    total_chars = 0

    for t in raw:
        if not isinstance(t, str):
            continue
        s = (t or "").strip()
        if not s:
            continue

        # YouTube tags should NOT include '#'
        s = s.lstrip("#")
        s = s.replace("#", "")

        # Normalize whitespace/newlines
        s = " ".join(s.replace("\n", " ").replace("\r", " ").split())
        s = s.strip(" ,;\t")
        if len(s) < 2:
            continue

        # Remove characters that commonly break tags
        s = re.sub(r"[\"<>]", "", s).strip()
        if not s:
            continue

        # YouTube tag limit is 30 chars per tag
        if len(s) > 30:
            s = s[:30].rstrip()
        if len(s) < 2:
            continue

        key = s.lower()
        if key in seen:
            continue
        seen.add(key)

        # Keep total tags char budget reasonable (YouTube has limits; prevent 400 invalidTags)
        if total_chars + len(s) > 450:
            break
        out.append(s)
        total_chars += len(s)
        if len(out) >= 25:
            break

    return out


_UPLOAD_IDEMPOTENCY_LOCK = threading.Lock()


def _upload_fingerprint(channel_key: str, file_path: str) -> str:
    try:
        abs_path = os.path.abspath(file_path or "")
    except Exception:
        abs_path = str(file_path or "")
    try:
        st = os.stat(file_path)
        size = int(getattr(st, "st_size", 0) or 0)
        mtime = int(getattr(st, "st_mtime", 0) or 0)
    except Exception:
        size = 0
        mtime = 0
    base = f"{(channel_key or '').strip()}|{abs_path}|{size}|{mtime}"
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def _reserve_upload_idempotency(cfg: Config, fingerprint: str) -> Tuple[bool, Optional[str], str]:
    now = time.time()
    try:
        done_window = int(os.getenv("UPLOAD_DEDUPE_WINDOW_SECONDS", "86400") or 86400)
    except Exception:
        done_window = 86400
    done_window = max(60, min(7 * 86400, done_window))

    try:
        inprog_window = int(os.getenv("UPLOAD_IN_PROGRESS_WINDOW_SECONDS", "7200") or 7200)
    except Exception:
        inprog_window = 7200
    inprog_window = max(30, min(12 * 3600, inprog_window))

    out = {"allow": True, "video_id": None, "state": "reserved"}

    def _upd(st):
        idem = st.setdefault("upload_idempotency", {})
        rec = idem.get(fingerprint) if isinstance(idem, dict) else None

        if isinstance(rec, dict):
            status = (rec.get("status") or "").strip().lower()
            ts = float(rec.get("ts") or 0.0)
            vid = (rec.get("video_id") or "").strip() or None

            if status == "done" and vid and (now - ts) <= done_window:
                out["allow"] = False
                out["video_id"] = vid
                out["state"] = "done"
                return

            if status == "uploading" and (now - ts) <= inprog_window:
                out["allow"] = False
                out["video_id"] = vid
                out["state"] = "uploading"
                return

        if not isinstance(idem, dict):
            idem = {}
            st["upload_idempotency"] = idem

        idem[fingerprint] = {"status": "uploading", "ts": now}
        out["allow"] = True
        out["video_id"] = None
        out["state"] = "reserved"

        try:
            max_items = int(os.getenv("UPLOAD_IDEMPOTENCY_MAX_ITEMS", "2000") or 2000)
        except Exception:
            max_items = 2000
        max_items = max(100, min(20000, max_items))
        if len(idem) > max_items:
            try:
                items = []
                for k, v in list(idem.items()):
                    if isinstance(v, dict):
                        items.append((k, float(v.get("ts") or 0.0)))
                items.sort(key=lambda x: x[1])
                drop = len(items) - max_items
                for i in range(max(0, drop)):
                    try:
                        del idem[items[i][0]]
                    except Exception:
                        continue
            except Exception:
                pass

    update_state(cfg, _upd)
    return bool(out["allow"]), out.get("video_id"), str(out.get("state") or "reserved")


def _mark_upload_done_and_cleanup(cfg: Config, fingerprint: str, video_id: str, channel_id: str, file_path: str) -> None:
    now = time.time()
    try:
        abs_path = os.path.abspath(file_path or "")
    except Exception:
        abs_path = str(file_path or "")

    def _upd(st):
        idem = st.setdefault("upload_idempotency", {})
        if isinstance(idem, dict):
            idem[fingerprint] = {"status": "done", "ts": now, "video_id": video_id, "channel_id": channel_id, "file_path": abs_path}

        try:
            sched = st.get("scheduler") or {}
            waiting = (sched.get("waiting_videos") or [])
            if isinstance(waiting, list) and waiting:
                new_waiting = []
                for w in waiting:
                    try:
                        wp = (w.get("output_path") or "").strip()
                        if wp:
                            try:
                                wp_abs = os.path.abspath(wp)
                            except Exception:
                                wp_abs = wp
                        else:
                            wp_abs = ""
                        wch = (w.get("publish_channel_id") or "").strip()
                        if wp_abs and abs_path and wp_abs == abs_path and (not channel_id or not wch or wch == channel_id):
                            continue
                    except Exception:
                        pass
                    new_waiting.append(w)
                st.setdefault("scheduler", {})["waiting_videos"] = new_waiting
        except Exception:
            pass

    update_state(cfg, _upd)


def _clear_upload_idempotency(cfg: Config, fingerprint: str) -> None:
    def _upd(st):
        idem = st.get("upload_idempotency")
        if isinstance(idem, dict) and fingerprint in idem:
            try:
                del idem[fingerprint]
            except Exception:
                pass
    update_state(cfg, _upd)


def _shutdown_executor_nowait(executor: Optional[ThreadPoolExecutor]) -> None:
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    except Exception:
        pass


def _run_upload_with_timeout(
    worker: Callable[[], str],
    timeout_seconds: float,
    timeout_message: str,
    on_success: Optional[Callable[[str], None]] = None,
) -> str:
    executor: Optional[ThreadPoolExecutor] = None
    future = None

    def _wrapped() -> str:
        result = str(worker())
        if on_success is not None:
            try:
                on_success(result)
            except Exception as e:
                logger.warning(f"[Upload] post-success finalization failed: {e}")
        return result

    try:
        timeout_value = float(timeout_seconds)
    except Exception:
        timeout_value = float(DEFAULT_UPLOAD_TIMEOUT)
    if timeout_value <= 0:
        timeout_value = float(DEFAULT_UPLOAD_TIMEOUT)

    try:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_wrapped)
        return str(future.result(timeout=timeout_value))
    except FuturesTimeoutError:
        try:
            if future is not None:
                future.cancel()
        except Exception:
            pass
        raise TimeoutError(timeout_message)
    finally:
        _shutdown_executor_nowait(executor)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/blogger"]
TOKEN_PATH_DEFAULT = ".data/youtube_token.json"

# Upload timeout configuration (in seconds)
# Default: 20 minutes, configurable via YOUTUBE_UPLOAD_TIMEOUT_SECONDS env var
# Increased from 600 to 1200 to handle slow uploads without falsely timing out
DEFAULT_UPLOAD_TIMEOUT = 1200

# Retry configuration for transient errors
MAX_UPLOAD_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds


def is_youtube_quota_error(exc: Exception) -> bool:
    reasons = {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}

    # Structured HttpError parsing
    try:
        if HttpError is not None and isinstance(exc, HttpError):
            content = getattr(exc, "content", None)
            if content:
                try:
                    if isinstance(content, (bytes, bytearray)):
                        content_str = content.decode("utf-8", "ignore")
                    else:
                        content_str = str(content)
                    payload = json.loads(content_str)
                    errors = (payload.get("error") or {}).get("errors") or []
                    for e in errors:
                        r = (e or {}).get("reason")
                        if r in reasons:
                            return True
                except Exception:
                    pass

            try:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if int(status or 0) in {403, 429}:
                    msg = str(exc)
                    return any(r in msg for r in reasons)
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: string match
    msg = str(exc) or ""
    return any(r in msg for r in reasons)


def youtube_channel_restriction_details(exc: Exception) -> Tuple[bool, str]:
    try:
        if HttpError is not None and isinstance(exc, HttpError):
            status = None
            try:
                status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
            except Exception:
                status = None

            content_str = ""
            try:
                content = getattr(exc, "content", None)
                if content:
                    if isinstance(content, (bytes, bytearray)):
                        content_str = content.decode("utf-8", "ignore")
                    else:
                        content_str = str(content)
            except Exception:
                content_str = ""

            payload = None
            try:
                payload = json.loads(content_str) if content_str else None
            except Exception:
                payload = None

            reasons = set()
            message = ""
            try:
                errors = (payload.get("error") or {}).get("errors") or [] if isinstance(payload, dict) else []
                for e in errors:
                    if isinstance(e, dict) and e.get("reason"):
                        reasons.add(str(e.get("reason")))
                    if isinstance(e, dict) and not message and e.get("message"):
                        message = str(e.get("message"))
                if not message and isinstance(payload, dict):
                    message = str((payload.get("error") or {}).get("message") or "")
            except Exception:
                reasons = set()

            msg_low = (message or str(exc) or "").lower()
            known_reasons = {
                "youtubeSignupRequired",
                "forbidden",
                "accountSuspended",
                "channelSuspended",
                "accountDisabled",
                "uploadBlocked",
                "uploadLimitExceeded",
                "uploadRestricted",
                "videoRejected",
                "policyViolation",
                "copyright",
                "termsOfService",
            }

            if status in {400, 401, 403, 409, 423, 429}:
                if reasons.intersection(known_reasons):
                    reason = sorted(reasons.intersection(known_reasons))[0]
                    detail = message or str(exc)
                    return True, f"{reason}: {detail}"[:400]

                if any(s in msg_low for s in [
                    "suspend",
                    "terminated",
                    "disabled",
                    "forbidden",
                    "not eligible",
                    "not allowed",
                    "restricted",
                    "blocked",
                    "this account",
                    "this channel",
                    "community",
                    "guidelines",
                    "copyright",
                    "terms",
                    "policy",
                ]):
                    return True, (message or str(exc) or "restricted")[:400]
    except Exception:
        pass
    return False, ""


def is_retryable_error(exc: Exception) -> bool:
    # HttpError handling
    try:
        if HttpError is not None and isinstance(exc, HttpError):
            try:
                status = getattr(getattr(exc, "resp", None), "status", None)
                code = int(status or 0)
                if code in {408, 425, 429, 500, 502, 503, 504}:
                    return True
            except Exception:
                pass

            # Sometimes the reason is embedded in the response content
            try:
                content = getattr(exc, "content", None)
                if content:
                    if isinstance(content, (bytes, bytearray)):
                        content_str = content.decode("utf-8", "ignore")
                    else:
                        content_str = str(content)
                    low = content_str.lower()
                    if any(s in low for s in ["backenderror", "internalerror", "ratelimit", "quota", "timeout", "timed out"]):
                        return True
            except Exception:
                pass
    except Exception:
        pass

    # Generic network/IO transient errors
    msg = (str(exc) or "").lower()
    if any(s in msg for s in [
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "connection error",
        "remote end closed",
        "unable to find the server",
        "name resolution",
        "dns",
        "network is unreachable",
        "no route to host",
        "503",
        "504",
        "429",
    ]):
        return True

    # Common Python exception types
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    return False


def _sanitize_video_title(title: Optional[str], file_path: str) -> str:
    t = (title or "").strip()
    if t:
        t = " ".join(t.replace("\n", " ").replace("\r", " ").split())
    if not t:
        base = os.path.splitext(os.path.basename(file_path or ""))[0].strip()
        t = base or "Short"
    if len(t) > 95:
        t = t[:95].rstrip()
    return t


def _maybe_allow_insecure_transport(redirect_uri: str) -> None:
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return
    if parsed.scheme != "http":
        return
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError(f"(insecure_transport) OAuth 2 MUST utilize https. redirect_uri={redirect_uri}")
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


class AuthenticationRequiredError(Exception):
    """Exception raised when authentication is invalid or expired and requires user interaction."""
    def __init__(self, message: str, token_path: str, channel_id: str = None):
        super().__init__(message)
        self.token_path = token_path
        self.channel_id = channel_id


class InvalidUploadVideoError(ValueError):
    """Raised when the final video artifact is not safe to publish."""


def _token_path(cfg: Config) -> str:
    base = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "youtube_token.json")


def _tokens_dir(cfg: Config) -> str:
    base = os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"
    path = os.path.join(base, "youtube_tokens")
    os.makedirs(path, exist_ok=True)
    return path


def _project_root() -> str:
    # src/agent/uploader.py -> project_root = dirname(dirname(dirname(this)))
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def _resolve_runtime_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.abspath(os.path.join(_project_root(), raw))


def _invalid_upload(reason: str) -> None:
    logger.error(f"🚫 رفض رفع الفيديو النهائي: {reason}")
    raise InvalidUploadVideoError(reason)


def _probe_upload_video(file_path: str) -> dict:
    probe = ffprobe_bin()
    if not probe:
        _invalid_upload("تعذر التحقق من الفيديو النهائي قبل الرفع لأن ffprobe غير متوفر")

    try:
        result = subprocess.run(
            [probe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", file_path],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as e:
        _invalid_upload(f"فشل تشغيل ffprobe لفحص الفيديو النهائي: {e}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        detail = detail[:300] if detail else "خطأ غير معروف"
        _invalid_upload(f"فشل ffprobe أثناء فحص الفيديو النهائي: {detail}")

    try:
        return json.loads(result.stdout or "{}")
    except Exception as e:
        _invalid_upload(f"أعاد ffprobe مخرجات غير صالحة: {e}")


def _validate_uploadable_video(file_path: str) -> str:
    path = _resolve_runtime_path(file_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"الملف غير موجود: {path}")
    if not os.path.isfile(path):
        _invalid_upload(f"المسار ليس ملف فيديو صالحًا: {path}")

    try:
        size_bytes = int(os.path.getsize(path) or 0)
    except Exception:
        size_bytes = 0
    if size_bytes <= 0:
        _invalid_upload(f"الملف النهائي فارغ: {path}")

    metadata = _probe_upload_video(path)
    streams = metadata.get("streams") or []
    has_video_stream = any((s.get("codec_type") or "").strip().lower() == "video" for s in streams)
    if not has_video_stream:
        _invalid_upload("الملف النهائي لا يحتوي على video stream صالح")

    duration_raw = ((metadata.get("format") or {}).get("duration") or "0").strip()
    try:
        duration = float(duration_raw)
    except Exception:
        duration = 0.0
    if duration <= 0.05:
        _invalid_upload(f"مدة الملف النهائي غير صالحة للرفع ({duration:.3f} ثانية)")

    return path


def _find_client_secrets_file(cfg: Config) -> Optional[str]:
    # 1) Explicit env var
    env_path = _resolve_runtime_path(os.getenv("GOOGLE_CLIENT_SECRETS_FILE") or "")
    if env_path and os.path.exists(env_path):
        return env_path
    # 2) Project root: client_secret*.json
    root = _project_root()
    try:
        for name in os.listdir(root):
            if name.lower().startswith("client_secret") and name.lower().endswith(".json"):
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    return path
    except Exception:
        pass
    # 3) .data/client_secret.json
    guess = os.path.join(os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data", "client_secret.json")
    if os.path.exists(guess):
        return guess
    return None


def _client_config_from_env(cfg: Config) -> dict:
    if not cfg.GOOGLE_CLIENT_ID or not cfg.GOOGLE_CLIENT_SECRET:
        raise RuntimeError("يجب ضبط GOOGLE_CLIENT_ID و GOOGLE_CLIENT_SECRET في .env لتفعيل رفع يوتيوب")
    redirect_uri = cfg.GOOGLE_REDIRECT_URI or "http://localhost:8080/"
    return {
        "installed": {
            "client_id": cfg.GOOGLE_CLIENT_ID,
            "client_secret": cfg.GOOGLE_CLIENT_SECRET,
            "redirect_uris": [redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "project_id": "yt-upload-agent"
        }
    }


def get_credentials(cfg: Config, interactive: bool = False) -> Credentials:
    token_path = _token_path(cfg)
    creds: Optional[Credentials] = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except ValueError as e:
            # إذا تغيرت الصلاحيات، نحذف التوكن القديم لإجبار المصادقة من جديد
            if "Scope has changed" in str(e):
                os.remove(token_path)
                creds = None
            else:
                raise e
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except Exception as e:
                # Refresh failed
                if os.path.exists(token_path):
                    os.remove(token_path)
                creds = None
        
        # If still no valid creds
        if not creds or not creds.valid:
            if not interactive:
                # If non-interactive, raise error to be handled by caller (bot notification)
                channel_id = os.path.splitext(os.path.basename(token_path))[0]
                raise AuthenticationRequiredError("Authentication required (interactive mode disabled).", token_path, channel_id)

            secrets_file = _find_client_secrets_file(cfg)
            if secrets_file and os.path.exists(secrets_file):
                flow = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
            else:
                flow = InstalledAppFlow.from_client_config(_client_config_from_env(cfg), SCOPES)
            
            port = 8080
            try:
                import urllib.parse as up
                uri_str = cfg.GOOGLE_REDIRECT_URI or "http://localhost:8080/"
                _maybe_allow_insecure_transport(uri_str)
                pr = up.urlparse(uri_str)
                if pr.port:
                    port = pr.port
                logger.info(f"إعداد خادم المصادقة المحلي على المنفذ: {port}")
                logger.info(f"رابط إعادة التوجيه المستخدم: {uri_str}")
            except Exception:
                pass
            
            try:
                creds = flow.run_local_server(port=port, prompt="consent", open_browser=True)
            except Exception as e:
                logger.error(f"فشل تشغيل خادم المصادقة المحلي: {e}")
                logger.error("تأكد من أن المنفذ غير مستخدم من قبل تطبيق آخر.")
                raise e
                
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
    return creds


def upload_video(cfg: Config, file_path: str, title: str, description: str, tags: list[str], privacy: str = "unlisted", publish_at: str = None) -> str:
    """
    رفع فيديو إلى YouTube مع دعم الجدولة
    
    Args:
        cfg: إعدادات التطبيق
        file_path: مسار ملف الفيديو
        title: عنون الفيديو
        description: وصف الفيديو
        tags: الوسوم
        privacy: حالة الخصوصية (public/unlisted/private)
        publish_at: موعد النشر المجدول بصيغة ISO 8601 (مثال: 2026-01-07T18:00:00.000Z)
                   إذا تم تحديده، سيُرفع الفيديو كـ private ثم يُنشر تلقائياً في الموعد
    
    Returns:
        معرف الفيديو على YouTube
    """
    file_path = _validate_uploadable_video(file_path)

    channel_id = "default"
    fp = _upload_fingerprint(channel_id, file_path)

    with _UPLOAD_IDEMPOTENCY_LOCK:
        allow, existing_vid, state = _reserve_upload_idempotency(cfg, fp)
    if not allow:
        if state == "done" and existing_vid:
            logger.warning(f"⏭️ Skipping duplicate upload (already uploaded): channel={channel_id} file={os.path.basename(file_path)} vid={existing_vid}")
            return existing_vid
        if state == "uploading":
            # Wait briefly for the in-progress upload to finish, then re-check once.
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    st = load_state(cfg)
                    rec = (st.get("upload_idempotency") or {}).get(fp) if isinstance(st.get("upload_idempotency"), dict) else None
                    if isinstance(rec, dict) and (rec.get("status") == "done") and rec.get("video_id"):
                        return str(rec.get("video_id"))
                except Exception:
                    pass
                time.sleep(5)
            raise RuntimeError("upload_in_progress")

    title = _sanitize_video_title(title, file_path)
    tags = _sanitize_video_tags(tags)
    creds = get_credentials(cfg)

    def _is_invalid_tags_error(exc: Exception) -> bool:
        try:
            if HttpError is not None and isinstance(exc, HttpError):
                content = getattr(exc, "content", None)
                if content:
                    try:
                        if isinstance(content, (bytes, bytearray)):
                            content_str = content.decode("utf-8", "ignore")
                        else:
                            content_str = str(content)
                        payload = json.loads(content_str)
                        errors = (payload.get("error") or {}).get("errors") or []
                        for e in errors:
                            if (e or {}).get("reason") == "invalidTags":
                                return True
                    except Exception:
                        pass
        except Exception:
            pass
        msg = (str(exc) or "")
        return ("invalidtags" in msg.lower()) or ("invalid video keywords" in msg.lower())

    try:
        timeout_seconds = int(os.getenv("YOUTUBE_UPLOAD_TIMEOUT_SECONDS", str(DEFAULT_UPLOAD_TIMEOUT)))
    except (ValueError, TypeError):
        timeout_seconds = DEFAULT_UPLOAD_TIMEOUT
    try:
        size_mb = float(os.path.getsize(file_path) or 0) / (1024.0 * 1024.0)
        timeout_seconds = max(int(timeout_seconds), min(int(6 * 3600), max(DEFAULT_UPLOAD_TIMEOUT, int(size_mb * 12))))
    except Exception:
        pass
    try:
        max_timeout = int(os.getenv("YOUTUBE_UPLOAD_TIMEOUT_MAX_SECONDS", "3600") or 3600)
    except Exception:
        max_timeout = 3600
    try:
        if max_timeout and int(max_timeout) > 0:
            timeout_seconds = min(int(timeout_seconds), int(max_timeout))
    except Exception:
        pass

    last_error = None
    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            logger.info(f"محاولة رفع الفيديو (محاولة {attempt + 1}/{MAX_UPLOAD_RETRIES}): {os.path.basename(file_path)}")

            def _do_upload():
                youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

                effective_privacy = "private" if publish_at else privacy
                body = {
                    "snippet": {
                        "title": title,
                        "description": description,
                        "tags": tags or [],
                        "categoryId": "22",
                    },
                    "status": {
                        "privacyStatus": effective_privacy,
                        "selfDeclaredMadeForKids": False,
                    },
                }
                if publish_at:
                    body["status"]["publishAt"] = publish_at

                try:
                    chunk_mb = int(os.getenv("YOUTUBE_UPLOAD_CHUNK_SIZE_MB", "8") or 8)
                except Exception:
                    chunk_mb = 8
                chunk_mb = max(1, min(64, int(chunk_mb)))
                chunksize = chunk_mb * 1024 * 1024
                unit = 256 * 1024
                chunksize = max(unit, (chunksize // unit) * unit)

                media = MediaFileUpload(file_path, chunksize=chunksize, resumable=True)
                request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

                response = None
                chunk_num = 0
                last_ts = time.time()
                while response is None:
                    status, response = request.next_chunk()
                    chunk_num += 1
                    now_ts = time.time()
                    try:
                        dt = float(now_ts - last_ts)
                        last_ts = now_ts
                    except Exception:
                        dt = None
                    if status:
                        pct = int(status.progress() * 100)
                        if dt is not None and dt >= float(os.getenv("YOUTUBE_UPLOAD_SLOW_CHUNK_SECONDS", "90") or 90):
                            logger.warning(f"[Upload] chunk بطيء: {dt:.1f}s عند {pct}% (chunk {chunk_num})")
                        else:
                            logger.info(f"[Upload] تقدم الرفع: {pct}% (chunk {chunk_num})")
                if not response or "id" not in response:
                    raise RuntimeError("فشل رفع الفيديو: استجابة غير متوقعة")
                logger.info(f"[Upload] اكتمل الرفع بنجاح: video_id={response['id']}")
                return response["id"]

            result = _run_upload_with_timeout(
                _do_upload,
                timeout_seconds=timeout_seconds,
                timeout_message=f"انتهت مهلة رفع الفيديو بعد {timeout_seconds} ثانية",
                on_success=lambda video_id: _mark_upload_done_and_cleanup(cfg, fp, video_id, channel_id, file_path),
            )
            logger.info(f"تم رفع الفيديو بنجاح: {result}")
            return result

        except AuthenticationRequiredError:
            raise
        except TimeoutError as e:
            last_error = e
            try:
                if attempt < MAX_UPLOAD_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Timeout في الرفع: {e}. إعادة المحاولة بعد {delay} ثانية...")
                    time.sleep(delay)
                    continue
            except Exception:
                pass
            raise
        except Exception as e:
            last_error = e
            if _is_invalid_tags_error(e) and tags:
                try:
                    logger.warning("[Upload] invalidTags from YouTube. Retrying once without tags.")
                except Exception:
                    pass
                tags = []
                continue
            if is_retryable_error(e) and attempt < MAX_UPLOAD_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"خطأ مؤقت في الرفع: {e}. إعادة المحاولة بعد {delay} ثانية...")
                time.sleep(delay)
                continue
            try:
                _clear_upload_idempotency(cfg, fp)
            except Exception:
                pass
            raise

    try:
        _clear_upload_idempotency(cfg, fp)
    except Exception:
        pass
    if last_error is not None:
        raise last_error
    raise RuntimeError("فشل رفع الفيديو")


def _get_channel_info(creds: Credentials) -> Tuple[str, str]:
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    ch = yt.channels().list(part="snippet", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        raise RuntimeError("لم يتم العثور على قناة YouTube في هذا الحساب")
    item = items[0]
    return item["id"], item["snippet"].get("title") or "My Channel"


def oauth_add_account(cfg: Config) -> dict:
    """تشغيل OAuth لإضافة حساب نشر جديد. يعيد dict يحوي channel_id/title ومسار التوكن ومحتواه."""
    creds = get_credentials(cfg)
    ch_id, ch_title = _get_channel_info(creds)
    # احفظ التوكن في ملف مخصص لكل قناة
    dirp = _tokens_dir(cfg)
    token_path = os.path.join(dirp, f"{ch_id}.json")
    token_json = creds.to_json()
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(token_json)
    return {
        "channel_id": ch_id, 
        "title": ch_title, 
        "token_path": token_path,
        "token_content": json.loads(token_json)
    }

def _creds_from_token_file(token_path: str) -> Credentials:
    channel_id = os.path.splitext(os.path.basename(token_path))[0]
    
    # محاولة استعادة التوكن من قاعدة البيانات إذا غاب الملف
    if not os.path.exists(token_path):
        try:
            from ..bot.channel_manager import ChannelManager
            cm = ChannelManager()
            # البحث عن القناة بالمعرف المستخرج من اسم الملف أو المعرف الفعلي
            # ملاحظة: channel_id هنا قد يكون youtube_channel_id
            channels, _ = cm.list_channels(limit=1000)
            ch = next((c for c in channels if c.youtube_channel_id == channel_id or c.channel_id == channel_id), None)
            
            if ch and ch.platform_credentials:
                os.makedirs(os.path.dirname(token_path), exist_ok=True)
                with open(token_path, "w", encoding="utf-8") as f:
                    creds = ch.platform_credentials
                    if isinstance(creds, dict):
                        json.dump(creds, f)
                    else:
                        # If it's a string (JSON string from DB), write it directly
                        f.write(str(creds))

                logger.info(f"🔄 Recovered missing token file from DB for: {channel_id}")
        except Exception as e:
            logger.debug(f"Failed to recover token for {channel_id}: {e}")

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception as e:
        # Token file corrupted or missing after failed recovery
        if os.path.exists(token_path):
            try:
                os.remove(token_path)
            except: 
                pass
        raise AuthenticationRequiredError(f"تلف ملف المصادقة أو مفقود. يرجى إعادة ربط القناة.", token_path, channel_id) from e

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                
                # تحديث التوكن في قاعدة البيانات بعد التجديد
                try:
                    from ..bot.channel_manager import ChannelManager
                    cm = ChannelManager()
                    channels, _ = cm.list_channels(limit=1000)
                    ch = next((c for c in channels if c.youtube_channel_id == channel_id or c.channel_id == channel_id), None)
                    if ch:
                        cm._save_channel(ch)
                except Exception: pass
            except Exception as e:
                # Refresh failed
                raise AuthenticationRequiredError(f"فشل تحديث التوكن (انتهت الصلاحية). يرجى إعادة المصادقة.", token_path, channel_id) from e
        else:
            # No refresh token or other issue
            raise AuthenticationRequiredError(f"التوكن غير صالح ولا يمكن تحديثه.", token_path, channel_id)
            
    return creds


def upload_video_with_token(cfg: Config, token_path: str, file_path: str, title: str, description: str, tags: list[str], privacy: str = "unlisted", publish_at: str = None) -> str:
    """
    رفع فيديو إلى YouTube باستخدام توكن مخصص مع دعم الجدولة والـ timeout
    
    Args:
        cfg: إعدادات التطبيق
        token_path: مسار ملف توكن المصادقة
        file_path: مسار ملف الفيديو
        title: عنون الفيديو
        description: وصف الفيديو
        tags: الوسوم
        privacy: حالة الخصوصية (public/unlisted/private)
        publish_at: موعد النشر المجدول بصيغة ISO 8601 (مثال: 2026-01-07T18:00:00.000Z)
                   إذا تم تحديده، سيُرفع الفيديو كـ private ثم يُنشر تلقائياً في الموعد
    
    Returns:
        معرف الفيديو على YouTube
    
    Raises:
        TimeoutError: إذا تجاوز الرفع المدة المحددة
        FileNotFoundError: إذا كان الملف غير موجود
    """
    token_path = _resolve_runtime_path(token_path)
    file_path = _validate_uploadable_video(file_path)
    
    title = _sanitize_video_title(title, file_path)
    tags = _sanitize_video_tags(tags)

    def _is_invalid_tags_error(exc: Exception) -> bool:
        try:
            if HttpError is not None and isinstance(exc, HttpError):
                content = getattr(exc, "content", None)
                if content:
                    try:
                        if isinstance(content, (bytes, bytearray)):
                            content_str = content.decode("utf-8", "ignore")
                        else:
                            content_str = str(content)
                        payload = json.loads(content_str)
                        errors = (payload.get("error") or {}).get("errors") or []
                        for e in errors:
                            if (e or {}).get("reason") == "invalidTags":
                                return True
                    except Exception:
                        pass
        except Exception:
            pass
        msg = (str(exc) or "")
        return ("invalidtags" in msg.lower()) or ("invalid video keywords" in msg.lower())

    channel_id = os.path.splitext(os.path.basename(token_path or ""))[0].strip() if token_path else ""
    fp = _upload_fingerprint(channel_id, file_path)

    with _UPLOAD_IDEMPOTENCY_LOCK:
        allow, existing_vid, state = _reserve_upload_idempotency(cfg, fp)
    if not allow:
        if state == "done" and existing_vid:
            logger.warning(f"⏭️ Skipping duplicate upload (already uploaded): channel={channel_id} file={os.path.basename(file_path)} vid={existing_vid}")
            return existing_vid
        if state == "uploading":
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    st = load_state(cfg)
                    rec = (st.get("upload_idempotency") or {}).get(fp) if isinstance(st.get("upload_idempotency"), dict) else None
                    if isinstance(rec, dict) and (rec.get("status") == "done") and rec.get("video_id"):
                        return str(rec.get("video_id"))
                except Exception:
                    pass
                time.sleep(5)
            raise RuntimeError("upload_in_progress")
    
    # Get timeout from environment or use default
    try:
        timeout_seconds = int(os.getenv("YOUTUBE_UPLOAD_TIMEOUT_SECONDS", str(DEFAULT_UPLOAD_TIMEOUT)))
    except (ValueError, TypeError):
        timeout_seconds = DEFAULT_UPLOAD_TIMEOUT

    # Auto-scale timeout based on file size (helps slow connections without endless retries)
    try:
        size_mb = float(os.path.getsize(file_path) or 0) / (1024.0 * 1024.0)
        # ~12 seconds per MB (≈5-6 Mbps) with generous cap; can be overridden via env.
        timeout_seconds = max(int(timeout_seconds), min(int(6 * 3600), max(DEFAULT_UPLOAD_TIMEOUT, int(size_mb * 12))))
    except Exception:
        pass

    try:
        max_timeout = int(os.getenv("YOUTUBE_UPLOAD_TIMEOUT_MAX_SECONDS", "3600") or 3600)
    except Exception:
        max_timeout = 3600
    try:
        if max_timeout and int(max_timeout) > 0:
            timeout_seconds = min(int(timeout_seconds), int(max_timeout))
    except Exception:
        pass
    
    last_error = None
    
    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            logger.info(f"محاولة رفع الفيديو (محاولة {attempt + 1}/{MAX_UPLOAD_RETRIES}): {os.path.basename(file_path)}")
            
            def _do_upload():
                creds = _creds_from_token_file(token_path)
                youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

                # 🔒 التحقق من أن التوكن ينتمي للقناة المتوقعة
                if channel_id:
                    try:
                        ch_resp = youtube.channels().list(part="id", mine=True).execute()
                        token_channels = [item.get("id", "") for item in (ch_resp.get("items") or [])]
                        if token_channels and channel_id not in token_channels:
                            raise RuntimeError(
                                f"❌ توكن ينتمي لقناة مختلفة ({token_channels[0][:20]}...) بدلاً من ({channel_id[:20]}...). "
                                f"يُرجى إعادة ربط القناة بالتوكن الصحيح."
                            )
                    except RuntimeError:
                        raise
                    except Exception as verify_err:
                        # لا نمنع النشر إذا فشل التحقق
                        logger.debug(f"Could not verify token channel ownership: {verify_err}")
                
                # إذا كان هناك موعد نشر مجدول، يجب أن تكون الخصوصية private
                effective_privacy = "private" if publish_at else privacy
                
                body = {
                    "snippet": {
                        "title": title,
                        "description": description,
                        "tags": tags or [],
                        "categoryId": "22",
                    },
                    "status": {
                        "privacyStatus": effective_privacy,
                        "selfDeclaredMadeForKids": False,
                    },
                }
                
                # إضافة موعد النشر المجدول إذا تم تحديده
                if publish_at:
                    body["status"]["publishAt"] = publish_at
                
                # Use chunked uploads for better reliability on slower networks
                try:
                    chunk_mb = int(os.getenv("YOUTUBE_UPLOAD_CHUNK_SIZE_MB", "8") or 8)
                except Exception:
                    chunk_mb = 8
                chunk_mb = max(1, min(64, int(chunk_mb)))
                chunksize = chunk_mb * 1024 * 1024
                # YouTube API requires chunk size to be a multiple of 256KB
                unit = 256 * 1024
                chunksize = max(unit, (chunksize // unit) * unit)
                media = MediaFileUpload(file_path, chunksize=chunksize, resumable=True)
                request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
                response = None
                chunk_num = 0
                while response is None:
                    status, response = request.next_chunk()
                    chunk_num += 1
                    if status:
                        pct = int(status.progress() * 100)
                        logger.info(f"[Upload] تقدم الرفع: {pct}% (chunk {chunk_num})")
                if not response or "id" not in response:
                    raise RuntimeError("فشل رفع الفيديو: استجابة غير متوقعة")
                logger.info(f"[Upload] اكتمل الرفع بنجاح: video_id={response['id']}")
                return response["id"]
            
            # Execute upload with timeout
            try:
                result = _run_upload_with_timeout(
                    _do_upload,
                    timeout_seconds=timeout_seconds,
                    timeout_message=f"انتهت مهلة رفع الفيديو بعد {timeout_seconds} ثانية - قد يكون الفيديو قد رُفع بنجاح، تحقق من القناة",
                    on_success=lambda video_id: _mark_upload_done_and_cleanup(cfg, fp, video_id, channel_id, file_path),
                )
                logger.info(f"تم رفع الفيديو بنجاح: {result}")
                return result
            except TimeoutError:
                logger.warning(f"انتهت مهلة الرفع ({timeout_seconds} ثانية) - سيتم التحقق من YouTube")
                # ✅ تحقق إذا تم الرفع بنجاح رغم انتهاء المهلة
                try:
                    # انتظر قليلاً ثم تحقق من سجل الـ idempotency
                    import time as _time
                    _time.sleep(5)
                    st = load_state(cfg)
                    rec = (st.get("upload_idempotency") or {}).get(fp) if isinstance(st.get("upload_idempotency"), dict) else None
                    if isinstance(rec, dict) and rec.get("status") == "done" and rec.get("video_id"):
                        vid_id = str(rec.get("video_id"))
                        logger.info(f"✅ الفيديو تم رفعه بنجاح رغم انتهاء المهلة: {vid_id}")
                        return vid_id
                except Exception:
                    pass
                # إذا لم نعثر على الفيديو، نسجل خطأ ولكن لا نمسح الـ idempotency فوراً
                # لأن الرفع قد يكون مستمراً في الخلفية
                logger.error(f"لم يتم العثور على الفيديو بعد انتهاء المهلة")
                raise
                    
        except AuthenticationRequiredError:
            # Don't retry authentication errors
            raise
        except TimeoutError:
            # Don't retry timeout errors
            raise
        except Exception as e:
            last_error = e
            if _is_invalid_tags_error(e) and tags:
                try:
                    logger.warning("[Upload] invalidTags from YouTube. Retrying once without tags.")
                except Exception:
                    pass
                tags = []
                continue
            if is_retryable_error(e) and attempt < MAX_UPLOAD_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"خطأ مؤقت في الرفع: {e}. إعادة المحاولة بعد {delay} ثانية...")
                time.sleep(delay)
                continue
            else:
                try:
                    _clear_upload_idempotency(cfg, fp)
                except Exception:
                    pass
                raise
    
    # If we get here, all retries failed
    if last_error:
        try:
            _clear_upload_idempotency(cfg, fp)
        except Exception:
            pass
        raise last_error
    try:
        _clear_upload_idempotency(cfg, fp)
    except Exception:
        pass
    raise RuntimeError("فشل رفع الفيديو بعد جميع المحاولات")

