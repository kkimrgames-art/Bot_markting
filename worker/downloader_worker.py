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
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiohttp import web
import yt_dlp

# ──────────────────────────────────────────────────────────────
# Configuration (all tuneable via env vars)
# ──────────────────────────────────────────────────────────────
HOST = os.getenv("WORKER_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("WORKER_PORT", "8080")))
TOKEN = (os.getenv("DOWNLOADER_WORKER_TOKEN") or "").strip()

# Use /tmp – Koyeb cleans it automatically on scale-to-zero
TEMP_ROOT = Path(os.getenv("DOWNLOADER_WORKER_TEMP_DIR", "/tmp/worker_dl"))

# ── Resource limits ──────────────────────────────────────────
MAX_FILE_SIZE_MB = int(os.getenv("WORKER_MAX_FILE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("WORKER_MAX_DURATION", "120"))
MONTHLY_BW_LIMIT_GB = int(os.getenv("WORKER_BW_LIMIT_GB", "80"))
MIN_DISK_FREE_MB = int(os.getenv("WORKER_MIN_DISK_MB", "200"))
RATE_LIMIT_PER_MIN = int(os.getenv("WORKER_RATE_LIMIT", "10"))
STREAM_CHUNK_KB = int(os.getenv("WORKER_CHUNK_KB", "256"))

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
    raw_path = (os.getenv("YTDLP_COOKIES_PATH") or "").strip()
    if raw_path and os.path.exists(raw_path):
        return raw_path
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


def _download_video(url: str, work_dir: Path) -> tuple[str, dict]:
    ffmpeg_path = shutil.which("ffmpeg")
    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    cookiefile = _resolve_cookiefile()
    proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    po_token = (os.getenv("YOUTUBE_PO_TOKEN") or "").strip()

    yt_args: dict = {}
    if po_token:
        yt_args["po_token"] = [po_token]
    if not cookiefile:
        yt_args["player_client"] = ["android", "mweb", "web"]

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    opts: dict = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "continuedl": False,  # always fresh – avoids partial-file buildup
        "retries": 2,
        "nopart": True,
        "match_filter": _duration_filter(MAX_DURATION_SEC),
        "max_filesize": max_bytes,
        "format": (
            f"best[ext=mp4][filesize<{max_bytes}]/"
            f"best[filesize<{max_bytes}]/"
            "best[ext=mp4]/best/b"
        ) if not ffmpeg_path else (
            f"bestvideo[ext=mp4][filesize<{max_bytes}]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][filesize<{max_bytes}]/"
            "best[ext=mp4]/best/b"
        ),
        "extractor_args": {"youtube": yt_args},
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    if ffmpeg_path:
        opts["merge_output_format"] = "mp4"
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if proxy:
        opts["proxy"] = proxy
    if _env_flag("YTDLP_FORCE_IPV4", True):
        opts["source_address"] = "0.0.0.0"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_normalize_youtube_url(url), download=True)
        prepared = ydl.prepare_filename(info)

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
                file_path, info = await asyncio.to_thread(
                    _download_video, url, Path(temp_dir)
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
            return web.json_response({"error": str(exc)}, status=502)
        finally:
            _cleanup_temp()
    finally:
        _download_semaphore.release()


# ──────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────
def create_app() -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)  # 2MB max request
    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/status", status)
    app.router.add_post("/download", download)
    return app


if __name__ == "__main__":
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()
    print(f"[Worker] Starting on {HOST}:{PORT}")
    print(f"[Worker] Limits: {MAX_FILE_SIZE_MB}MB file, "
          f"{MAX_DURATION_SEC}s duration, "
          f"{MONTHLY_BW_LIMIT_GB}GB/month BW, "
          f"{RATE_LIMIT_PER_MIN} req/min")
    print(f"[Worker] FFmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}")
    web.run_app(create_app(), host=HOST, port=PORT)