"""
أدوات مساعدة لعملية المصادقة عبر ملف JSON
"""
import os
import json
import logging
import threading
import ipaddress
import wsgiref.simple_server
import wsgiref.util
from urllib.parse import urlparse, parse_qs
from typing import Optional, Tuple, Callable

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
try:
    from pyngrok import ngrok, conf
    HAS_NGROK = True
except ImportError:
    HAS_NGROK = False

# 🆕 السماح ببروتوكول HTTP للمصادقة المحلية (ضروري لـ localhost)


def _maybe_allow_insecure_transport(redirect_uri: str) -> None:
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return
    if parsed.scheme != "http":
        return
    hostname = parsed.hostname or ""
    allow = hostname in {"localhost", "127.0.0.1"}
    if not allow:
        try:
            ip = ipaddress.ip_address(hostname)
            allow = ip.is_private or ip.is_loopback
        except ValueError:
            allow = False
    if not allow:
        # If using ngrok via http (not https) or other tunneling, we might need this
        # But usually Ngrok provides https. 
        pass
        # raise RuntimeError(f"(insecure_transport) OAuth 2 MUST utilize https. redirect_uri={redirect_uri}")
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/blogger"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
ALL_SCOPES = SCOPES

class OAuthCallbackServer:
    """خادم محلي بسيط لاستقبال رد كود المصادقة"""
    
    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.server = None
        self.auth_code = None
        self.error = None
        self.last_uri = None
        self.authorized_uri = None  # 🆕 حفظ الرابط الذي يحتوي على الكود فعلياً
        self._stop_event = threading.Event()
        self.use_shared_state = False  # 🆕 إذا كان صحيحاً، ينتظر من shared_state بدلاً من السيرفر المحلي
        
    def _app(self, environ, start_response):
        """تطبيق WSGI لمعالجة الطلب"""
        uri = wsgiref.util.request_uri(environ)
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        
        if 'code' in query or 'error' in query:
            self.authorized_uri = uri
            logger.info(f"Captured authorized URI: {uri}")
            
        if 'code' in query:
            self.auth_code = query['code'][0]
            status = '200 OK'
            headers = [('Content-type', 'text/html; charset=utf-8')]
            start_response(status, headers)
            return [b"<h1>Authentication Successful!</h1><p>You can close this window now. The bot has received the code and is processing it.</p>"]
        elif 'error' in query:
            self.error = query['error'][0]
            status = '200 OK'
            headers = [('Content-type', 'text/html; charset=utf-8')]
            start_response(status, headers)
            return [f"<h1>Authentication Failed</h1><p>Error: {self.error}</p>".encode('utf-8')]
        
        # 🆕 للمساعدة في التشخيص
        logger.debug(f"Request received but ignored for code: {uri}")
        
        status = '404 Not Found'
        start_response(status, [('Content-type', 'text/plain')])
        return [b"Not Found"]

    def start(self) -> int:
        """بدء الخادم وإرجاع المنفذ"""
        # استخدام 127.0.0.1 صراحة لتجنب مشاكل IPv6
        self.server = wsgiref.simple_server.make_server(self.host, self.port, self._app)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever)
        thread.daemon = True
        thread.start()
        return self.port
        
    def wait_for_response(self, timeout: int = 300) -> Optional[str]:
        """انتظار الكود أو الخطأ وإرجاع الرابط الكامل"""
        import time
        from .shared_state import oauth_callback_results
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            # التحقق من الحالة المحلية (السيرفر الخاص)
            if self.auth_code or self.error:
                res = self.authorized_uri
                self.stop()
                return res
            
            # التحقق من الحالة المشتركة (إذا تم التفعيل)
            if self.use_shared_state:
                if 'latest' in oauth_callback_results:
                    res = oauth_callback_results.pop('latest')
                    logger.info(f"✅ Found callback in shared state: {res}")
                    self.stop()
                    return res
                    
            # تقليص وقت الانتظار لاستجابة أسرع
            time.sleep(0.1)
        return None

    def stop(self):
        """إيقاف الخادم"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def _extract_redirect_uris(client_secrets_file: str) -> Tuple[Optional[str], list[str]]:
    try:
        with open(client_secrets_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, []

    payload = data.get("installed") or data.get("web") or {}
    redirect_uris = payload.get("redirect_uris") or []
    if not isinstance(redirect_uris, list):
        redirect_uris = []
    client_type = "installed" if "installed" in data else "web" if "web" in data else None
    return client_type, redirect_uris


def _build_client_config_with_redirect(client_secrets_file: str, redirect_uri: str) -> Optional[dict]:
    try:
        with open(client_secrets_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    payload = data.get("installed") or data.get("web") or None
    if not isinstance(payload, dict):
        return None

    client_type = "installed" if "installed" in data else "web"
    updated = dict(payload)
    updated["redirect_uris"] = [redirect_uri]
    return {client_type: updated}

def start_ngrok_tunnel(port: int) -> Optional[str]:
    """Start an Ngrok tunnel to the specified port and return the public URL"""
    if not HAS_NGROK:
        logger.warning("pyngrok not installed. Skipping Ngrok tunnel.")
        return None
        
    try:
        # Disconnect existing tunnels to be safe
        ngrok.kill()
        
        # Open a HTTP tunnel on the default port 8080
        # http_tunnel = ngrok.connect(port)
        # Using bind_tls=True to force https
        public_url = ngrok.connect(port, bind_tls=True).public_url
        logger.info(f"Ngrok Tunnel Started: {public_url} -> http://localhost:{port}")
        return public_url
    except Exception as e:
        logger.error(f"Failed to start Ngrok tunnel: {e}")
        return None

def create_flow_from_file_scopes(
    client_secrets_file: str,
    scopes: list[str],
    *,
    include_granted_scopes: bool = False,
) -> Tuple[Flow, str, OAuthCallbackServer]:
    """إنشاء Flow وبدء الخادم وإرجاع رابط المصادقة"""
    
    # 1. إعداد الخادم ومحاولة استخدام المنفذ المحدد في الإعدادات
    from src.agent.config import load_config
    cfg = load_config()
    
    target_port = 8080
    target_uri = (cfg.GOOGLE_REDIRECT_URI or os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    bind_host = "127.0.0.1"

    client_type, allowed_redirects = _extract_redirect_uris(client_secrets_file)
    if target_uri and allowed_redirects and target_uri not in allowed_redirects:
        # If USE_NGROK is active, we might ignore this mismatch as we will override it anyway
        pass
    
    # Determine port first
    server = None
    ports_to_try = [cfg.GOOGLE_OAUTH_PORT] + [p for p in range(8080, 8091) if p != cfg.GOOGLE_OAUTH_PORT]
    
    for port in ports_to_try:
        try:
            server = OAuthCallbackServer(port=port, host=bind_host)
            server.start()
            logger.info(f"ابدأ خادم المصادقة على {bind_host}:{server.port}")
            break
        except Exception as e:
            logger.debug(f"المنفذ {port} مشغول: {e}")
            continue
            
    if not server:
        server = OAuthCallbackServer(port=0, host=bind_host)
        server.start()
        logger.warning(f"استخدام منفذ عشوائي للمصادقة: {server.port}")

    # Determine redirect URI (must match Google Console EXACTLY).
    # Prefer explicit GOOGLE_REDIRECT_URI if set. Otherwise, use ngrok (optional).
    redirect_uri = ""
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    
    if target_uri:
        # Normalize: ensure callback path exists if it looks like a base domain
        try:
            parsed = urlparse(target_uri)
            if parsed.scheme and parsed.netloc:
                if parsed.path and parsed.path != "/":
                    redirect_uri = target_uri
                else:
                    base = target_uri.rstrip("/")
                    redirect_uri = f"{base}/oauth2/callback"
            else:
                redirect_uri = target_uri # Manual entry or something else
        except Exception:
            redirect_uri = target_uri
    elif external_url:
        # Auto-detect Render URL
        redirect_uri = f"{external_url}/oauth2/callback"
        logger.info(f"✨ Auto-detected Render Redirect URI: {redirect_uri}")

    if not redirect_uri:
        use_ngrok = os.environ.get("USE_NGROK", "").strip().lower() in {"1", "true", "yes", "on"}
        ngrok_url = start_ngrok_tunnel(server.port) if use_ngrok else None
        if ngrok_url:
            redirect_uri = f"{ngrok_url.rstrip('/')}/oauth2/callback"
            logger.info(f"================================================================")
            logger.info(f"🚀 Ngrok Tunnel Active: {ngrok_url}")
            logger.info(f"⚠️  IMPORTANT: You MUST add this Redirect URI to Google Cloud Console:")
            logger.info(f"   {redirect_uri}")
            logger.info(f"================================================================")
        else:
            redirect_uri = f"http://localhost:{server.port}/oauth2/callback"
    
    # If the redirect URI is external (not localhost) and we have a main web server running,
    # we can tell the server object to wait for shared state instead of its own wsgiref server.
    if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        logger.info("🌐 Using external redirect. Will wait for shared state callback.")
        server.use_shared_state = True
    
    _maybe_allow_insecure_transport(redirect_uri)
    
    # 2. إعداد Flow
    # We must use the dynamic redirect_uri, so we usually need to build client config dynamically
    # because the file's redirect_uris likely won't contain the random ngrok url.
    
    client_config = _build_client_config_with_redirect(client_secrets_file, redirect_uri)
    if client_config:
        flow = Flow.from_client_config(
            client_config,
            scopes=scopes,
            redirect_uri=redirect_uri
        )
    else:
        flow = Flow.from_client_secrets_file(
            client_secrets_file,
            scopes=scopes,
            redirect_uri=redirect_uri
        )
    
    # 3. توليد رابط المصادقة
    auth_url, _ = flow.authorization_url(
        prompt='consent',
        access_type='offline',
        include_granted_scopes='true' if include_granted_scopes else 'false',
    )
    
    return flow, auth_url, server


def create_flow_from_file(client_secrets_file: str) -> Tuple[Flow, str, OAuthCallbackServer]:
    # نستخدم ALL_SCOPES لتجنب خطأ "Scope has changed" عند إعادة المصادقة
    # إذا كان المستخدم قد منح drive.readonly سابقاً
    return create_flow_from_file_scopes(client_secrets_file, ALL_SCOPES)

def exchange_code_and_get_creds(flow: Flow, authorization_response: str) -> Credentials:
    """تبادل الكود (عبر الرابط الكامل لضمان تطابق state) والحصول على Credentials"""
    # Fix for http vs https mismatch if ngrok is used
    if "http://" in authorization_response and "https://" in flow.redirect_uri:
        authorization_response = authorization_response.replace("http://", "https://", 1)

    # السماح بتغيير الصلاحيات عند تبادل التوكن
    # يحدث عند استخدام include_granted_scopes='true' وإضافة صلاحيات جديدة مثل drive.readonly
    os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Warning as w:
        # oauthlib قد يرسل Warning بدلاً من Exception عند RELAX_TOKEN_SCOPE
        logger.info(f"Token scope warning (ignored): {w}")
    finally:
        # تنظيف المتغير البيئي بعد الاستخدام
        os.environ.pop('OAUTHLIB_RELAX_TOKEN_SCOPE', None)
    return flow.credentials

def get_channel_info_from_creds(creds: Credentials) -> dict:
    """الحصول على معلومات القناة من Credentials"""
    youtube = build("youtube", "v3", credentials=creds, static_discovery=False)
    response = youtube.channels().list(part="snippet,contentDetails,statistics", mine=True).execute()
    
    items = response.get("items", [])
    if not items:
        # ربما حساب بدون قناة؟
        return {
            "id": "unknown_" + os.urandom(4).hex(),
            "title": "Unknown Channel (No Channel Found)",
            "privacy": "unlisted"
        }
        
    item = items[0]
    return {
        "id": item["id"],
        "title": item["snippet"]["title"],
        "thumbnail": item["snippet"]["thumbnails"]["default"]["url"],
        "uploads_playlist": item["contentDetails"]["relatedPlaylists"]["uploads"]
    }


def start_auth_flow(client_secrets_file: str) -> Tuple[str, OAuthCallbackServer, Flow]:
    """
    بدء عملية المصادقة وإرجاع الرابط والخادم والتدفق.
    """
    # استخدام الدالة الموحدة لضمان التطابق مع عملية رفع الملف الجديد
    flow, auth_url, server = create_flow_from_file(client_secrets_file)
    return auth_url, server, flow


def start_auth_flow_scopes(
    client_secrets_file: str,
    scopes: list[str],
    *,
    include_granted_scopes: bool = False,
) -> Tuple[str, OAuthCallbackServer, Flow]:
    flow, auth_url, server = create_flow_from_file_scopes(
        client_secrets_file,
        scopes,
        include_granted_scopes=include_granted_scopes,
    )
    return auth_url, server, flow


# ==================== تدفق المصادقة اليدوي (للأجهزة البعيدة) ====================

OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

def create_manual_auth_flow(client_secrets_file: str) -> Tuple[Flow, str]:
    """
    إنشاء تدفق مصادقة يدوي يعمل من أي جهاز.
    بعد المصادقة، يظهر للمستخدم كود (أو رابط به كود) يرسله للبوت.
    
    Returns:
        (flow, auth_url) - التدفق ورابط المصادقة
    """
    client_type, redirect_uris = _extract_redirect_uris(client_secrets_file)
    
    if client_type == "web":
        # تطبيقات الويب لا تدعم OOB
        redirect_uri = os.environ.get('GOOGLE_REDIRECT_URI')
        if not redirect_uri and redirect_uris:
            redirect_uri = redirect_uris[0]
        if not redirect_uri:
            redirect_uri = "http://localhost:8080/oauth2/callback"
    else:
        redirect_uri = OOB_REDIRECT_URI

    _maybe_allow_insecure_transport(redirect_uri)
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
    
    client_config = _build_client_config_with_redirect(client_secrets_file, redirect_uri)
    
    if client_config:
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
    else:
        flow = Flow.from_client_secrets_file(
            client_secrets_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
    
    auth_url, _ = flow.authorization_url(
        prompt='consent',
        access_type='offline',
        include_granted_scopes='false'
    )
    
    logger.info(f"تم إنشاء رابط مصادقة يدوي: {auth_url[:80]}...")
    return flow, auth_url


def exchange_manual_code(flow: Flow, code: str) -> Credentials:
    """
    تبادل الكود اليدوي (أو الرابط الكامل) والحصول على Credentials.
    
    Args:
        flow: تدفق OAuth
        code: الكود الذي أدخله المستخدم، أو الرابط الكامل الذي تم تحويله إليه
    
    Returns:
        Credentials
    """
    if code.startswith("http://") or code.startswith("https://"):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(code)
        query = parse_qs(parsed.query)
        if 'code' in query:
            code = query['code'][0]

    flow.fetch_token(code=code)
    return flow.credentials
