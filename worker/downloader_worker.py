"""Minimal YouTube downloader worker optimised for Koyeb free-tier.

Koyeb free-tier limits (2024/2025):
  RAM: 512 MB | SSD: 2 GB | Bandwidth: 100 GB/month | vCPU: 0.1

Safety features:
  • One download at a time (asyncio.Semaphore)
  • Max file size 50 MB (sufficient for Shorts)
  • Max video duration 120 s
  • Disk-space check before every download
  • In-memory monthly bandwidth tracker (resets on restart)
  • Simple rate limiter (10 req / min)
  • Temp files cleaned after every request
"""

import asyncio
import base64
import logging
import os
import random
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiohttp import web
import yt_dlp

logger = logging.getLogger("downloader_worker")

# ──────────────────────────────────────────────────────────────
# Configuration (all tuneable via env vars)
# ──────────────────────────────────────────────────────────────
HOST = os.getenv("WORKER_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("WORKER_PORT", "8080")))
TOKEN = (os.getenv("DOWNLOADER_WORKER_TOKEN") or "").strip()

# Use /tmp – Koyeb cleans it automatically on scale-to-zero
TEMP_ROOT = Path(os.getenv("DOWNLOADER_WORKER_TEMP_DIR", "/tmp/worker_dl"))

# ── Resource limits ──────────────────────────────────────────
MAX_FILE_SIZE_MB = int(os.getenv("WORKER_MAX_FILE_MB", "200"))
MAX_DURATION_SEC = int(os.getenv("WORKER_MAX_DURATION", "120"))
MONTHLY_BW_LIMIT_GB = int(os.getenv("WORKER_BW_LIMIT_GB", "80"))
MIN_DISK_FREE_MB = int(os.getenv("WORKER_MIN_DISK_MB", "200"))
RATE_LIMIT_PER_MIN = int(os.getenv("WORKER_RATE_LIMIT", "10"))
STREAM_CHUNK_KB = int(os.getenv("WORKER_CHUNK_KB", "256"))
DOWNLOAD_TIMEOUT_SEC = int(os.getenv("WORKER_DOWNLOAD_TIMEOUT_SEC", "420"))

# ── Internal state (reset on restart – fine for free tier) ───
_download_semaphore = asyncio.Semaphore(1)
_bw_used_bytes: int = 0
_bw_month: int = 0  # month number when counter was last reset
_request_times: list[float] = []
_total_requests: int = 0
_total_downloads_ok: int = 0
_total_downloads_fail: int = 0


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _extract_youtube_id(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return (parsed.path or "/").strip("/").split("/")[0]
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        parts = [p for p in (parsed.path or "").split("/") if p]
        lower_parts = [p.lower() for p in parts]
        if "shorts" in lower_parts:
            idx = lower_parts.index("shorts")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return parse_qs(parsed.query or "").get("v", [""])[0]
    return ""


def _normalize_youtube_url(url: str) -> str:
    vid = _extract_youtube_id(url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else str(url or "").strip()


def _resolve_cookiefile() -> str:
    env_candidates = [
        os.getenv("YTDLP_COOKIES_PATH"),
        os.getenv("YT_COOKIES_PATH"),
        os.getenv("COOKIES_PATH"),
    ]
    for raw in env_candidates:
        raw_path = (raw or "").strip()
        if raw_path and os.path.exists(raw_path):
            return raw_path

    for candidate in [
        "www.youtube.com_cookies.txt",
        "youtube.com_cookies.txt",
        "youtube_cookies.txt",
        "cookies.txt",
        ".data/yt_dlp_cookies.txt",
        ".data/yt_cookies.txt",
    ]:
        try:
            resolved = os.path.abspath(candidate)
            if os.path.exists(resolved):
                return resolved
        except Exception:
            pass

    cookie_b64 = (os.getenv("YTDLP_COOKIES_B64") or "").strip()
    cookie_text = (os.getenv("YTDLP_COOKIES_TEXT") or "").strip()
    if not cookie_b64 and not cookie_text:
        return ""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    out = TEMP_ROOT / "yt_dlp_worker_cookies.txt"
    content = (
        base64.b64decode(cookie_b64.encode()).decode("utf-8", errors="replace")
        if cookie_b64
        else cookie_text
    )
    out.write_text(content.strip(), encoding="utf-8")
    return str(out)


def _check_disk_space() -> tuple[bool, int]:
    """Return (ok, free_mb)."""
    try:
        st = shutil.disk_usage(TEMP_ROOT if TEMP_ROOT.exists() else "/tmp")
        free_mb = st.free // (1024 * 1024)
        return free_mb >= MIN_DISK_FREE_MB, free_mb
    except OSError:
        return True, -1  # can't check → allow


def _check_bandwidth() -> tuple[bool, float]:
    """Return (ok, remaining_gb)."""
    global _bw_used_bytes, _bw_month
    import datetime

    now_month = datetime.datetime.now(datetime.timezone.utc).month
    if now_month != _bw_month:
        _bw_used_bytes = 0
        _bw_month = now_month

    limit = MONTHLY_BW_LIMIT_GB * 1024 * 1024 * 1024
    remaining = max(0, limit - _bw_used_bytes) / (1024 * 1024 * 1024)
    return _bw_used_bytes < limit, round(remaining, 2)


def _check_rate_limit() -> bool:
    """Return True if request is allowed."""
    now = time.monotonic()
    cutoff = now - 60
    # Remove old entries
    while _request_times and _request_times[0] < cutoff:
        _request_times.pop(0)
    if len(_request_times) >= RATE_LIMIT_PER_MIN:
        return False
    _request_times.append(now)
    return True


def _cleanup_temp():
    """Remove any leftover files in TEMP_ROOT (belt-and-suspenders)."""
    try:
        if TEMP_ROOT.exists():
            for item in TEMP_ROOT.iterdir():
                if item.name == "yt_dlp_worker_cookies.txt":
                    continue
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────
# yt-dlp download (runs in thread)
# ──────────────────────────────────────────────────────────────
def _duration_filter(max_dur: int):
    def _filter(info_dict, *, incomplete=False):
        dur = info_dict.get("duration")
        if max_dur and dur and int(dur) > max_dur:
            return f"duration {dur}s exceeds limit {max_dur}s"
        return None
    return _filter


def _classify_ytdlp_error(exc: Exception) -> str:
    # AssertionError with no message is thrown when impersonate target
    # string is not auto-converted to ImpersonateTarget (yt-dlp 2026.x+)
    if isinstance(exc, AssertionError):
        return "impersonate_unavailable"
    msg = str(exc or "").lower()
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "http error 429" in msg or "too many requests" in msg:
        return "rate_limited"
    if "http error 403" in msg or "forbidden" in msg:
        return "forbidden"
    if "requested format is not available" in msg or "requested formats are not available" in msg:
        return "format_unavailable"
    if "sign in to confirm" in msg or "bot" in msg and "detected" in msg:
        return "botcheck"
    if "impersonate target" in msg and "not available" in msg:
        return "impersonate_unavailable"
    return "other"


def _download_profiles(ffmpeg_path: str, max_bytes: int, cookies_enabled: bool, prefer_anonymous: bool = False) -> list[dict]:
    high_quality = _env_flag("WORKER_HIGH_QUALITY_FIRST", True)
    single_file_profile = {
        "label": "single_file_mp4",
        "format": "best[ext=mp4]/best/b",
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "mweb"]}},
    }
    anonymous_single_file_profile = {
        "label": "anonymous_single_file_mp4",
        "format": "best[ext=mp4]/best/b",
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "mweb"]}},
        "strip_cookies": True,
    }
    authenticated_default_profile = {
        "label": "authenticated_default",
        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "web_creator", "mweb"]}},
    }
    authenticated_web_profile = {
        "label": "authenticated_web",
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb"]}},
    }

    compatibility = {
        "label": "compatibility_mobile",
        "format": (
            f"bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<=1080]+bestaudio/"
            f"best[height<=1080][ext=mp4]/best[height<=1080]/best"
        ),
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "mweb"]}},
    }
    anonymous_compatibility = {
        "label": "anonymous_compatibility_mobile",
        "format": (
            f"bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<=1080]+bestaudio/"
            f"best[height<=1080][ext=mp4]/best[height<=1080]/best"
        ),
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "mweb"]}},
        "strip_cookies": True,
    }

    if not ffmpeg_path:
        anonymous_profiles = [anonymous_single_file_profile]
        if not cookies_enabled:
            return anonymous_profiles
        if prefer_anonymous:
            return anonymous_profiles + [authenticated_default_profile, authenticated_web_profile]
        return [authenticated_default_profile, authenticated_web_profile] + anonymous_profiles

    if not high_quality:
        anonymous_profiles = [anonymous_compatibility]
        if not cookies_enabled:
            return anonymous_profiles
        if prefer_anonymous:
            return anonymous_profiles + [authenticated_default_profile, authenticated_web_profile]
        return [authenticated_default_profile, authenticated_web_profile] + anonymous_profiles

    anonymous_profiles = [
        {
            "label": "anonymous_hq_android_vr",
            "format": (
                "bestvideo[height<=1440][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080]+bestaudio/"
                "best[height<=1080][ext=mp4]/best[height<=1080]/best"
            ),
            "extractor_args": {"youtube": {"player_client": ["android_vr", "android", "mweb"]}},
            "strip_cookies": True,
        },
        {
            "label": "anonymous_hq_web",
            "format": (
                "bestvideo[height<=1440][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080]+bestaudio/"
                "best[height<=1080][ext=mp4]/best[height<=1080]/best"
            ),
            "extractor_args": {"youtube": {"player_client": ["web", "android", "mweb"]}},
            "strip_cookies": True,
        },
        anonymous_compatibility,
    ]
    if not cookies_enabled:
        return anonymous_profiles

    if prefer_anonymous:
        return anonymous_profiles + [authenticated_default_profile, authenticated_web_profile]

    return [authenticated_default_profile, authenticated_web_profile] + anonymous_profiles


def _download_video(
    url: str,
    work_dir: Path,
    custom_cookiefile: str = "",
    custom_po_token: str = "",
    custom_max_duration: int | None = None,
    prefer_anonymous: bool = False,
) -> tuple[str, dict]:
    ffmpeg_path = shutil.which("ffmpeg")
    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    cookiefile = custom_cookiefile or _resolve_cookiefile()
    proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    po_token = custom_po_token or (os.getenv("YOUTUBE_PO_TOKEN") or "").strip()
    max_duration = max(1, int(custom_max_duration)) if custom_max_duration else MAX_DURATION_SEC

    yt_args: dict = {}
    if po_token:
        yt_args["po_token"] = [po_token]
    if not cookiefile:
        yt_args["player_client"] = ["android", "mweb", "web"]

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    profiles = _download_profiles(
        ffmpeg_path,
        max_bytes=max_bytes,
        cookies_enabled=bool(cookiefile),
        prefer_anonymous=prefer_anonymous,
    )
    ytdlp_url = _normalize_youtube_url(url)
    last_error: Exception | None = None
    max_attempts = max(1, int(os.getenv("WORKER_YTDLP_ATTEMPTS", "3")))

    for attempt in range(1, max_attempts + 1):
        for profile_idx, profile in enumerate(profiles, start=1):
            yt_profile_args = dict(yt_args)
            profile_extractors = (profile.get("extractor_args") or {}).get("youtube") or {}
            yt_profile_args.update(profile_extractors)
            profile_cookiefile = "" if profile.get("strip_cookies") else cookiefile
            opts: dict = {
                "outtmpl": outtmpl,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "continuedl": False,
                "retries": 5,
                "fragment_retries": 5,
                "nopart": True,
                "match_filter": _duration_filter(max_duration),
                "max_filesize": max_bytes,
                "format": profile.get("format"),
                "extractor_args": {"youtube": yt_profile_args},
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": "https://www.youtube.com",
                    "Referer": "https://www.youtube.com/",
                    "Sec-Fetch-Mode": "navigate",
                },
                "no_check_certificate": True,
                "socket_timeout": 30,
                "check_formats": "selected",
                "prefer_free_formats": False,
                "youtube_include_dash_manifest": True,
                "youtube_include_hls_manifest": True,
                "sleep_interval": 2,
                "max_sleep_interval": 6,
                "sleep_requests": 1,
            }
            if ffmpeg_path:
                opts["merge_output_format"] = "mp4"
                opts["ffmpeg_location"] = ffmpeg_path
            if profile_cookiefile:
                opts["cookiefile"] = profile_cookiefile
            if proxy:
                opts["proxy"] = proxy
            if _env_flag("YTDLP_FORCE_IPV4", True):
                opts["source_address"] = "0.0.0.0"

            impersonate = (os.getenv("YTDLP_IMPERSONATE") or "chrome110").strip()
            if impersonate and impersonate.lower() not in ("off", "none", "false") and os.getenv("YTDLP_SKIP_IMPERSONATE") != "1":
                try:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    target = ImpersonateTarget.from_str(impersonate)
                    opts["impersonate"] = target
                except Exception:
                    logger.info("impersonate target %r not available, skipping", impersonate)

            try:
                logger.info(
                    "yt-dlp attempt %s/%s profile=%s ffmpeg=%s cookies=%s",
                    attempt,
                    max_attempts,
                    profile.get("label") or f"profile_{profile_idx}",
                    "on" if ffmpeg_path else "off",
                    "on" if profile_cookiefile else "off",
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(ytdlp_url, download=True)
                        prepared = ydl.prepare_filename(info)
                except AssertionError:
                    # impersonate target incompatible — retry without it
                    opts.pop("impersonate", None)
                    os.environ["YTDLP_SKIP_IMPERSONATE"] = "1"
                    logger.warning("impersonate caused AssertionError, retrying without it")
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(ytdlp_url, download=True)
                        prepared = ydl.prepare_filename(info)
                break
            except Exception as exc:
                last_error = exc
                reason = _classify_ytdlp_error(exc)
                logger.warning(
                    "yt-dlp profile failed: attempt=%s/%s profile=%s reason=%s error=%s",
                    attempt,
                    max_attempts,
                    profile.get("label") or f"profile_{profile_idx}",
                    reason,
                    repr(exc),
                )
                if reason == "impersonate_unavailable":
                    os.environ["YTDLP_SKIP_IMPERSONATE"] = "1"
                if reason == "botcheck":
                    logger.warning("Botcheck detected. Cookiefile might be poisoned. Dropping cookies for subsequent fallbacks.")
                    cookiefile = ""
                if profile_idx < len(profiles):
                    continue
                delay = min(10.0, (2 ** (attempt - 1)) + random.uniform(0.0, 0.8))
                time.sleep(delay)
        else:
            continue
        break
    else:
        detail = str(last_error) if str(last_error or "").strip() else repr(last_error)
        raise RuntimeError(f"yt-dlp failed after {max_attempts} attempts: {detail}")

    base = os.path.splitext(prepared)[0]
    for candidate in [prepared, f"{base}.mp4", f"{base}.mkv", f"{base}.webm"]:
        if os.path.exists(candidate):
            # Final size check
            fsize = os.path.getsize(candidate)
            if fsize > max_bytes:
                os.unlink(candidate)
                raise ValueError(
                    f"Downloaded file {fsize / 1024 / 1024:.1f}MB "
                    f"exceeds limit {MAX_FILE_SIZE_MB}MB"
                )
            return candidate, info
    raise FileNotFoundError("yt-dlp completed but no output file was found")


# ──────────────────────────────────────────────────────────────
# HTTP handlers
# ──────────────────────────────────────────────────────────────
async def root(_: web.Request) -> web.Response:
    return web.json_response({
        "service": "automod-downloader-worker",
        "status": "healthy",
        "version": "2.0-koyeb-safe",
        "endpoints": ["/download", "/healthz", "/status"],
    })


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def status(_: web.Request) -> web.Response:
    """Enhanced status with resource usage info."""
    disk_ok, disk_free_mb = _check_disk_space()
    bw_ok, bw_remaining_gb = _check_bandwidth()
    bw_used_gb = round(_bw_used_bytes / (1024 ** 3), 3)

    # Memory usage (if available)
    mem_mb = -1
    try:
        import resource as _res
        mem_mb = round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        try:
            # Linux /proc fallback
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        mem_mb = round(int(line.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass

    return web.json_response({
        "ok": True,
        "limits": {
            "max_file_mb": MAX_FILE_SIZE_MB,
            "max_duration_sec": MAX_DURATION_SEC,
            "monthly_bw_limit_gb": MONTHLY_BW_LIMIT_GB,
            "rate_limit_per_min": RATE_LIMIT_PER_MIN,
        },
        "resources": {
            "disk_free_mb": disk_free_mb,
            "disk_ok": disk_ok,
            "bandwidth_used_gb": bw_used_gb,
            "bandwidth_remaining_gb": bw_remaining_gb,
            "bandwidth_ok": bw_ok,
            "memory_rss_mb": mem_mb,
        },
        "stats": {
            "total_requests": _total_requests,
            "downloads_ok": _total_downloads_ok,
            "downloads_fail": _total_downloads_fail,
        },
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "token_configured": bool(TOKEN),
    })


async def download(request: web.Request) -> web.StreamResponse:
    global _bw_used_bytes, _total_requests, _total_downloads_ok, _total_downloads_fail
    _total_requests += 1

    # ── Auth ──
    if TOKEN:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth != f"Bearer {TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)

    # ── Rate limit ──
    if not _check_rate_limit():
        return web.json_response(
            {"error": "rate limit exceeded – max {}/min".format(RATE_LIMIT_PER_MIN)},
            status=429,
        )

    # ── Bandwidth check ──
    bw_ok, bw_rem = _check_bandwidth()
    if not bw_ok:
        return web.json_response(
            {"error": "monthly bandwidth limit reached ({} GB)".format(MONTHLY_BW_LIMIT_GB)},
            status=503,
        )

    # ── Disk check ──
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        _cleanup_temp()
        disk_ok, disk_free = _check_disk_space()
        if not disk_ok:
            return web.json_response(
                {"error": f"disk space low ({disk_free}MB free, need {MIN_DISK_FREE_MB}MB)"},
                status=503,
            )

    # ── Parse request ──
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    url = str(payload.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "url is required"}, status=400)
    
    cookies_text = str(payload.get("cookies_text") or "").strip()
    po_token = str(payload.get("po_token") or "").strip()
    max_duration_raw = payload.get("max_duration")
    max_duration = None
    try:
        if max_duration_raw is not None and str(max_duration_raw).strip():
            max_duration = max(1, int(float(str(max_duration_raw).strip())))
    except Exception:
        max_duration = None
    prefer_anonymous_raw = payload.get("prefer_anonymous")
    if prefer_anonymous_raw is None:
        prefer_anonymous = _env_flag("WORKER_PREFER_ANONYMOUS_YOUTUBE", False)
    else:
        prefer_anonymous = str(prefer_anonymous_raw).strip().lower() in {"1", "true", "yes", "on"}
    skip_cookies = str(payload.get("skip_cookies") or "").strip().lower() in {"1", "true", "yes", "on"}

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    # ── Download (one at a time, queue with timeout) ──
    # Instead of rejecting, wait up to 120s for the semaphore.
    # This lets multiple agents queue up instead of failing.
    QUEUE_TIMEOUT = 120  # seconds to wait for semaphore
    try:
        await asyncio.wait_for(_download_semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": f"download queue full – waited {QUEUE_TIMEOUT}s, try again later"},
            status=503,
        )

    try:  # replaces "async with _download_semaphore:"
        try:
            with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp_dir:
                custom_cookiefile = ""
                if cookies_text and not skip_cookies:
                    cfile_path = Path(temp_dir) / "request_cookies.txt"
                    cfile_path.write_text(cookies_text, encoding="utf-8")
                    custom_cookiefile = str(cfile_path)

                file_path, info = await asyncio.wait_for(
                    asyncio.to_thread(
                        _download_video,
                        url,
                        Path(temp_dir),
                        custom_cookiefile,
                        po_token,
                        max_duration,
                        prefer_anonymous,
                    ),
                    timeout=max(60, DOWNLOAD_TIMEOUT_SEC),
                )

                file_size = os.path.getsize(file_path)
                chunk_size = STREAM_CHUNK_KB * 1024

                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                    "X-Downloader-Filename": os.path.basename(file_path),
                    "X-Downloader-Video-Id": str(info.get("id") or ""),
                    "X-Downloader-File-Size-MB": str(round(file_size / 1024 / 1024, 2)),
                    "X-BW-Remaining-GB": str(round(
                        max(0, MONTHLY_BW_LIMIT_GB * 1024**3 - _bw_used_bytes - file_size) / 1024**3, 2
                    )),
                }

                response = web.StreamResponse(status=200, headers=headers)
                await response.prepare(request)

                with open(file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(chunk_size)
                        if not chunk:
                            break
                        await response.write(chunk)
                        _bw_used_bytes += len(chunk)

                await response.write_eof()
                _total_downloads_ok += 1
                return response

        except Exception as exc:
            _total_downloads_fail += 1
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            _cleanup_temp()
    finally:
        _download_semaphore.release()


# ──────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────
def create_app() -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/status", status)
    app.router.add_post("/download", download)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()
    print(f"[Worker] Starting on {HOST}:{PORT}")
    print(f"[Worker] Limits: {MAX_FILE_SIZE_MB}MB file, "
          f"{MAX_DURATION_SEC}s duration, "
          f"{MONTHLY_BW_LIMIT_GB}GB/month BW, "
          f"{RATE_LIMIT_PER_MIN} req/min")
    print(f"[Worker] Download timeout: {DOWNLOAD_TIMEOUT_SEC}s")
    print(f"[Worker] FFmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}")
    web.run_app(create_app(), host=HOST, port=PORT)
