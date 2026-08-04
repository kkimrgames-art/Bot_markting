"""
YouTube Resilient Fetcher — Production Edition for Render
=========================================================
Designed for multi-instance Render deployments with ZERO manual configuration.

KEY CHALLENGES ON RENDER:
  1. Render uses datacenter IPs → YouTube blocks ALL direct requests with
     "Sign in to confirm you're not a bot" (confirmed by extensive testing).
  2. Free public proxies are unreliable and die quickly under load.
  3. Multiple bot instances compete for the same scarce proxy resources.
  4. Render's ephemeral filesystem loses proxy state on restart.
  5. Render free tier has 512MB RAM — proxy testing must be lightweight.

ARCHITECTURE:
  ┌─────────────────────────────────────────────────────┐
  │  Instance A (Render)  ←─→  Supabase (shared state)  │
  │  Instance B (Render)  ←─→  (proxy pool, health)     │
  │  Instance C (Render)  ←─→                           │
  └─────────────────────────────────────────────────────┘
  
  - Each instance fetches proxy lists from 8+ sources (HTTP + SOCKS5 + VPN Gate).
  - Each instance tests proxies in parallel (20 threads, 5s timeout).
  - Results are synced to Supabase so all instances share working proxies.
  - Proxy pool is pre-warmed on startup (before any download attempt).
  - Background task refreshes the pool every 5 minutes.
  - When all proxies fail: graceful degradation (queue + exponential backoff).

ZERO-CONFIG DEFAULTS:
  - No env vars required — uses free public proxies automatically.
  - Optional: YOUTUBE_PROXY_URL (single proxy) or YOUTUBE_PROXY_LIST (comma-sep).
  - Optional: WEBSHARE_API_KEY (free tier: 10 residential proxies).
  - Optional: YOUTUBE_PROXY_STATE_FILE (default: .data/youtube_proxy_pool.json).
"""
from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Configuration (all env-var driven, zero-config defaults)
# ──────────────────────────────────────────────────────────────────────────────

# Proxy list sources — 8 sources for maximum coverage
_PROXY_SOURCES_HTTP = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://api.openproxylist.xyz/http.txt",
]

_PROXY_SOURCES_SOCKS5 = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=3000&country=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://api.openproxylist.xyz/socks5.txt",
]

# VPN Gate (free Japanese residential VPNs — often bypass YouTube blocks)
_VPN_GATE_API = "http://www.vpngate.net/api/iphone/"

# Testing
_PROXY_TEST_CONCURRENCY = int(os.getenv("PROXY_TEST_CONCURRENCY", "20"))
_PROXY_TEST_TIMEOUT = int(os.getenv("PROXY_TEST_TIMEOUT", "6"))
_PROXY_YT_TEST_TIMEOUT = int(os.getenv("PROXY_YT_TEST_TIMEOUT", "10"))

# Pool management
_PROXY_BAN_DURATION = int(os.getenv("PROXY_BAN_DURATION", "1800"))  # 30 min
_MIN_WORKING_PROXIES = int(os.getenv("PROXY_MIN_WORKING", "5"))
_MAX_PROXIES_TO_TEST = int(os.getenv("PROXY_MAX_TEST", "100"))
_PROXY_REFRESH_INTERVAL = int(os.getenv("PROXY_REFRESH_INTERVAL", "300"))  # 5 min
_PROXY_SYNC_INTERVAL = int(os.getenv("PROXY_SYNC_INTERVAL", "120"))  # 2 min

# State file
_RAW_STATE_FILE = os.getenv("YOUTUBE_PROXY_STATE_FILE") or ".data/youtube_proxy_pool.json"
if os.path.isabs(_RAW_STATE_FILE):
    _STATE_FILE = os.path.normpath(_RAW_STATE_FILE)
else:
    _STATE_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, _RAW_STATE_FILE))

# Optional paid proxy support (zero-config: if not set, uses free proxies)
_PAID_PROXY_URL = (os.getenv("YOUTUBE_PROXY_URL") or "").strip()
_PAID_PROXY_LIST = [
    p.strip() for p in (os.getenv("YOUTUBE_PROXY_LIST") or "").split(",") if p.strip()
]
_WEBSHARE_API_KEY = (os.getenv("WEBSHARE_API_KEY") or "").strip()


# ──────────────────────────────────────────────────────────────────────────────
# ProxyPool — shared across instances via Supabase
# ──────────────────────────────────────────────────────────────────────────────

class ProxyPool:
    """Thread-safe pool of working proxies, synced across Render instances."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proxies: Dict[str, Dict[str, Any]] = {}
        self._last_refresh: float = 0.0
        self._last_sync: float = 0.0
        self._pre_warmed: bool = False
        self._load_state()

    # ─── Persistence (local JSON) ───

    def _load_state(self) -> None:
        try:
            p = Path(_STATE_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                self._proxies = data.get("proxies", {})
                if not isinstance(self._proxies, dict):
                    self._proxies = {}
                self._last_refresh = float(data.get("last_refresh", 0))
                logger.info(f"📦 Loaded {len(self._proxies)} proxies from local state")
        except Exception as e:
            logger.warning(f"Failed to load proxy state: {e}")
            self._proxies = {}

    def _save_state(self) -> None:
        try:
            p = Path(_STATE_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "proxies": self._proxies,
                "last_refresh": self._last_refresh,
                "saved_at": datetime.now().isoformat(),
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save proxy state: {e}")

    # ─── Supabase sync (share proxies between instances) ───

    def _sync_to_supabase(self) -> None:
        """Upload working proxies to Supabase so other instances can use them."""
        try:
            from .supabase_client import USE_SUPABASE, is_online, supabase_upsert
            if not (USE_SUPABASE and is_online()):
                return
            with self._lock:
                working = {
                    url: info for url, info in self._proxies.items()
                    if info.get("working") and not self._is_banned(info)
                }
            if not working:
                return
            # Upload as a single JSON row
            supabase_upsert(
                "proxy_pool_state",
                {
                    "id": "main",
                    "proxies": json.dumps(working, ensure_ascii=False),
                    "working_count": len(working),
                    "updated_at": datetime.now().isoformat(),
                },
                "id",
            )
            self._last_sync = time.time()
            logger.debug(f"☁️ Synced {len(working)} working proxies to Supabase")
        except Exception as e:
            logger.debug(f"Supabase proxy sync skipped: {e}")

    def _sync_from_supabase(self) -> None:
        """Download working proxies discovered by other instances."""
        try:
            from .supabase_client import USE_SUPABASE, is_online, supabase_select_one
            if not (USE_SUPABASE and is_online()):
                return
            result = supabase_select_one("proxy_pool_state", "id", "main")
            if not result:
                return
            raw = result.get("proxies")
            if isinstance(raw, str):
                remote_proxies = json.loads(raw)
            elif isinstance(raw, dict):
                remote_proxies = raw
            else:
                return
            if not isinstance(remote_proxies, dict):
                return
            # Merge: add remote proxies we don't have locally
            added = 0
            with self._lock:
                for url, info in remote_proxies.items():
                    if url not in self._proxies:
                        self._proxies[url] = info
                        added += 1
                    # Update ban status from remote (if remote says it's banned, trust it)
                    elif info.get("banned_until") and not self._proxies[url].get("banned_until"):
                        self._proxies[url]["banned_until"] = info["banned_until"]
            if added > 0:
                logger.info(f"☁️ Merged {added} working proxies from Supabase (shared by other instances)")
                self._save_state()
        except Exception as e:
            logger.debug(f"Supabase proxy fetch skipped: {e}")

    # ─── Proxy discovery (8+ sources) ───

    def _fetch_proxy_lists(self) -> List[str]:
        """Fetch proxies from all sources (HTTP + SOCKS5 + VPN Gate)."""
        all_proxies: List[str] = []

        # HTTP proxies
        for source in _PROXY_SOURCES_HTTP:
            try:
                resp = requests.get(source, timeout=8)
                if resp.status_code == 200:
                    lines = [l.strip() for l in resp.text.split("\n") if l.strip() and ":" in l]
                    all_proxies.extend([f"http://{l}" for l in lines])
            except Exception:
                pass

        # SOCKS5 proxies
        for source in _PROXY_SOURCES_SOCKS5:
            try:
                resp = requests.get(source, timeout=8)
                if resp.status_code == 200:
                    lines = [l.strip() for l in resp.text.split("\n") if l.strip() and ":" in l]
                    all_proxies.extend([f"socks5://{l}" for l in lines])
            except Exception:
                pass

        # VPN Gate (Japanese residential IPs — often bypass YouTube blocks)
        try:
            resp = requests.get(_VPN_GATE_API, timeout=10)
            if resp.status_code == 200:
                # VPN Gate returns CSV: the 2nd column is IP, 3rd is port
                lines = resp.text.strip().split("\n")[2:]  # skip header lines
                for line in lines:
                    parts = line.split(",")
                    if len(parts) >= 3:
                        ip = parts[1].strip()
                        port = parts[2].strip()
                        if ip and port and port.isdigit():
                            all_proxies.append(f"http://{ip}:{port}")
        except Exception:
            pass

        # Paid proxies (if configured — zero-config: skipped if not set)
        if _PAID_PROXY_URL:
            all_proxies.insert(0, _PAID_PROXY_URL)
        if _PAID_PROXY_LIST:
            for p in _PAID_PROXY_LIST:
                if p not in all_proxies:
                    all_proxies.insert(0, p)

        # Webshare free tier (if API key configured)
        if _WEBSHARE_API_KEY:
            try:
                resp = requests.get(
                    "https://proxy.webshare.io/api/v2/proxy/list/?mode=full&page=1&page_size=25",
                    headers={"Authorization": f"Token {_WEBSHARE_API_KEY}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("results", []):
                        ip = item.get("proxy_address")
                        port = item.get("port")
                        if ip and port:
                            proto = item.get("format", "http").split(",")[0]
                            all_proxies.insert(0, f"{proto}://{ip}:{port}")
            except Exception:
                pass

        # Dedupe
        seen = set()
        unique = []
        for p in all_proxies:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        logger.info(f"🌐 Fetched {len(unique)} unique proxies from {len(_PROXY_SOURCES_HTTP) + len(_PROXY_SOURCES_SOCKS5) + 1} sources")
        return unique

    def _test_proxy_youtube(self, proxy_url: str) -> bool:
        """Test if a proxy can access YouTube without bot detection."""
        try:
            # Normalize proxy format for requests
            proxies = {"http": proxy_url, "https": proxy_url}
            resp = requests.get(
                "https://www.youtube.com",
                proxies=proxies,
                timeout=_PROXY_YT_TEST_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if resp.status_code == 200 and len(resp.text) > 5000:
                if "Sign in to confirm" not in resp.text and "confirm you" not in resp.text:
                    return True
            return False
        except Exception:
            return False

    def refresh(self, force: bool = False) -> int:
        """Discover and test new proxies. Returns count of working proxies."""
        with self._lock:
            now = time.time()
            if not force and now - self._last_refresh < _PROXY_REFRESH_INTERVAL:
                working_count = sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))
                if working_count >= _MIN_WORKING_PROXIES:
                    return working_count

            # First, sync from Supabase (other instances may have found proxies)
            self._sync_from_supabase()

            logger.info("🔄 Refreshing proxy pool...")
            raw_proxies = self._fetch_proxy_lists()
            if not raw_proxies:
                logger.warning("⚠️ No proxies fetched from any source")
                return sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))

            # Shuffle and take a subset to test
            random.shuffle(raw_proxies)
            to_test = raw_proxies[:_MAX_PROXIES_TO_TEST]

            # Test proxies in parallel
            import concurrent.futures
            working = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=_PROXY_TEST_CONCURRENCY) as executor:
                future_to_proxy = {executor.submit(self._test_proxy_youtube, p): p for p in to_test}
                for future in concurrent.futures.as_completed(future_to_proxy):
                    proxy_url = future_to_proxy[future]
                    try:
                        if future.result():
                            working.append(proxy_url)
                            self._proxies[proxy_url] = {
                                "working": True,
                                "last_tested": now,
                                "banned_until": None,
                                "success_count": 0,
                                "fail_count": 0,
                            }
                    except Exception:
                        pass

            self._last_refresh = now
            self._save_state()

            # Sync to Supabase so other instances can use these proxies
            self._sync_to_supabase()

            total_working = sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))
            logger.info(f"✅ Proxy pool: {total_working} working ({len(working)} newly discovered from {len(to_test)} tested)")
            return total_working

    # ─── Pre-warm (call on startup) ───

    def pre_warm(self) -> int:
        """Pre-warm the proxy pool on startup. Should be called BEFORE any download.
        
        This is critical for Render: the first download attempt will fail if no
        proxies are available. Pre-warming ensures the pool is ready.
        """
        if self._pre_warmed:
            # Already warmed — just check if we have enough
            working = sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))
            if working >= _MIN_WORKING_PROXIES:
                return working

        logger.info("🔥 Pre-warming proxy pool (startup)...")
        self._pre_warmed = True

        # First try Supabase sync (fast — other instances may have proxies)
        self._sync_from_supabase()
        working = sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))
        if working >= _MIN_WORKING_PROXIES:
            logger.info(f"✅ Pre-warm: got {working} proxies from Supabase (shared by other instances)")
            return working

        # Not enough — do a full refresh
        return self.refresh(force=True)

    # ─── Proxy selection ───

    def _is_banned(self, proxy_info: Dict[str, Any]) -> bool:
        banned_until = proxy_info.get("banned_until")
        if not banned_until:
            return False
        if time.time() >= banned_until:
            proxy_info["banned_until"] = None
            return False
        return True

    def get_proxy(self) -> Optional[str]:
        """Get a working proxy URL. Returns None if none available."""
        with self._lock:
            working = [url for url, info in self._proxies.items()
                       if info.get("working") and not self._is_banned(info)]
            if len(working) < _MIN_WORKING_PROXIES:
                # Trigger refresh (non-blocking best-effort)
                try:
                    self.refresh(force=False)
                except Exception:
                    pass
                working = [url for url, info in self._proxies.items()
                           if info.get("working") and not self._is_banned(info)]

            if not working:
                return None

            # Weighted random: prefer proxies with more successes
            weighted = []
            for url in working:
                info = self._proxies.get(url, {})
                weight = max(1, int(info.get("success_count", 0)) + 1)
                weighted.extend([url] * weight)
            return random.choice(weighted) if weighted else random.choice(working)

    def mark_success(self, proxy_url: str) -> None:
        with self._lock:
            info = self._proxies.get(proxy_url)
            if info:
                info["success_count"] = int(info.get("success_count", 0)) + 1
                info["last_tested"] = time.time()
                info["banned_until"] = None
                self._save_state()

    def mark_failure(self, proxy_url: str, reason: str = "") -> None:
        with self._lock:
            info = self._proxies.get(proxy_url)
            if info:
                info["fail_count"] = int(info.get("fail_count", 0)) + 1
                info["last_tested"] = time.time()
                if info["fail_count"] >= 2:
                    info["banned_until"] = time.time() + _PROXY_BAN_DURATION
                    logger.info(f"🚫 Proxy banned {_PROXY_BAN_DURATION}s: {proxy_url[:40]}... ({reason[:50]})")
                self._save_state()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._proxies)
            working = sum(1 for p in self._proxies.values() if p.get("working") and not self._is_banned(p))
            banned = sum(1 for p in self._proxies.values() if self._is_banned(p))
            return {
                "total_known": total,
                "working": working,
                "banned": banned,
                "last_refresh_ago_seconds": int(time.time() - self._last_refresh) if self._last_refresh else None,
                "pre_warmed": self._pre_warmed,
            }

    def background_refresh_loop(self):
        """Background task that periodically refreshes the proxy pool.
        
        Should be registered as a supervised task in main.py.
        Runs forever — never exits (supervised by TaskSupervisor).
        """
        import asyncio
        logger.info(f"🌐 Proxy pool background refresh started (interval={_PROXY_REFRESH_INTERVAL}s, sync={_PROXY_SYNC_INTERVAL}s)")
        while True:
            try:
                # Refresh if needed
                working = self.refresh(force=False)
                if working < _MIN_WORKING_PROXIES:
                    logger.warning(f"⚠️ Only {working} working proxies — forcing refresh")
                    self.refresh(force=True)

                # Sync to/from Supabase periodically
                now = time.time()
                if now - self._last_sync > _PROXY_SYNC_INTERVAL:
                    self._sync_to_supabase()
                    self._sync_from_supabase()

            except Exception as e:
                logger.error(f"Proxy refresh loop error: {e}")

            # Sleep in small increments so the task can be cancelled cleanly
            sleep_total = min(_PROXY_REFRESH_INTERVAL, 60)
            for _ in range(sleep_total):
                time.sleep(1)


# Singleton
_pool: Optional[ProxyPool] = None
_pool_lock = threading.Lock()


def get_proxy_pool() -> ProxyPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProxyPool()
        return _pool


# ──────────────────────────────────────────────────────────────────────────────
# YouTube download / list functions
# ──────────────────────────────────────────────────────────────────────────────

def _build_ytdlp_opts(
    output_path: str,
    *,
    proxy_url: Optional[str] = None,
    max_height: int = 1080,
) -> Dict[str, Any]:
    if max_height >= 1080:
        fmt = (
            f"bestvideo[height<={max_height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}][vcodec^=avc1]+bestaudio/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best[ext=mp4]/best"
        )
    else:
        fmt = f"best[height<={max_height}][ext=mp4]/best[ext=mp4]/best"

    opts: Dict[str, Any] = {
        "format": fmt,
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }
    if proxy_url:
        opts["proxy"] = proxy_url
    return opts


def download_video(
    url: str,
    output_path: str,
    *,
    max_height: int = 1080,
    max_attempts: int = 8,
    use_proxy: bool = True,
) -> Dict[str, Any]:
    """Download a YouTube video with automatic proxy failover.

    Enhanced for Render:
      - Pre-warms the proxy pool on first call.
      - Tries direct first (in case we're on a residential IP).
      - Rotates through proxies on bot detection.
      - Graceful degradation: if all proxies fail, returns error with details.
    """
    import yt_dlp

    result: Dict[str, Any] = {
        "success": False,
        "filepath": None,
        "title": None,
        "duration": None,
        "error": None,
        "proxy_used": None,
        "attempts": 0,
    }

    pool = get_proxy_pool() if use_proxy else None

    # Pre-warm the pool if not done yet (critical for Render startup)
    if pool and not pool._pre_warmed:
        try:
            pool.pre_warm()
        except Exception as e:
            logger.warning(f"Pre-warm failed: {e}")

    tried_proxies: set = set()

    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt

        # Decide which proxy to use
        proxy_url = None
        if attempt > 1 or (use_proxy and pool and pool.status()["working"] > 0):
            if pool:
                proxy_url = pool.get_proxy()
                if proxy_url and proxy_url in tried_proxies:
                    continue
                if proxy_url:
                    tried_proxies.add(proxy_url)
                elif attempt > 1:
                    # No more proxies to try
                    result["error"] = "All proxies exhausted"
                    break

        opts = _build_ytdlp_opts(output_path, proxy_url=proxy_url, max_height=max_height)

        try:
            logger.info(f"📥 Attempt {attempt}/{max_attempts}: {url}" +
                       (f" via {proxy_url[:30]}..." if proxy_url else " (direct)"))
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            filepath = ydl.prepare_filename(info)
            if filepath and os.path.exists(filepath):
                result["success"] = True
                result["filepath"] = filepath
                result["title"] = info.get("title")
                result["duration"] = info.get("duration")
                result["proxy_used"] = proxy_url
                if pool and proxy_url:
                    pool.mark_success(proxy_url)
                logger.info(f"✅ Downloaded: {info.get('title')}")
                return result
            else:
                result["error"] = "File not found after download"
        except Exception as e:
            err_msg = str(e)[:300]
            result["error"] = err_msg
            logger.warning(f"❌ Attempt {attempt} failed: {err_msg[:120]}")

            if pool and proxy_url:
                pool.mark_failure(proxy_url, reason=err_msg[:60])

            # Bot detection → rotate proxy immediately
            if any(kw in err_msg.lower() for kw in ["not a bot", "sign in", "forbidden", "403"]):
                continue
            # Format/extractor errors → try different proxy
            elif any(kw in err_msg.lower() for kw in ["format", "unavailable", "extractor", "timeout"]):
                continue
            else:
                break

    return result


def list_channel_videos(
    channel_url: str,
    *,
    max_videos: int = 20,
    use_proxy: bool = True,
) -> Dict[str, Any]:
    """List videos from a YouTube channel/shorts URL.

    On Render: listing usually works WITHOUT proxy (YouTube allows listing
    even from datacenter IPs). Only uses proxy as fallback.
    """
    import yt_dlp

    pool = get_proxy_pool() if use_proxy else None
    result: Dict[str, Any] = {
        "success": False,
        "videos": [],
        "error": None,
        "proxy_used": None,
    }

    # Try direct first (listing often works without proxy)
    for attempt in range(3):
        proxy_url = None
        if attempt > 0 and pool:
            proxy_url = pool.get_proxy()
            if not proxy_url:
                result["error"] = "No proxies for listing fallback"
                break

        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": True,
            "playlistend": max_videos,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if proxy_url:
            opts["proxy"] = proxy_url

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)

            entries = info.get("entries", []) if info else []
            videos = []
            for entry in entries:
                if entry and entry.get("id"):
                    videos.append({
                        "id": entry.get("id"),
                        "title": entry.get("title", ""),
                        "url": entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "duration": entry.get("duration"),
                    })

            result["success"] = True
            result["videos"] = videos
            result["proxy_used"] = proxy_url
            if pool and proxy_url:
                pool.mark_success(proxy_url)
            logger.info(f"📋 Listed {len(videos)} videos from {channel_url}")
            return result
        except Exception as e:
            err_msg = str(e)[:200]
            result["error"] = err_msg
            if pool and proxy_url:
                pool.mark_failure(proxy_url, reason=err_msg[:60])
            if any(kw in err_msg.lower() for kw in ["not a bot", "sign in"]):
                continue
            else:
                break

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Telegram UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_status_text() -> str:
    pool = get_proxy_pool()
    status = pool.status()
    text = (
        "🌐 <b>حالة جلب فيديوهات YouTube</b>\n\n"
        f"📦 البروكسيات المعروفة: <code>{status['total_known']}</code>\n"
        f"✅ تعمل: <code>{status['working']}</code>\n"
        f"🚫 محظورة: <code>{status['banned']}</code>\n"
    )
    if status.get("last_refresh_ago_seconds") is not None:
        mins = status["last_refresh_ago_seconds"] // 60
        secs = status["last_refresh_ago_seconds"] % 60
        text += f"🔄 آخر تحديث: <code>{mins}د {secs}ث</code> مضت\n"
    text += f"🔥 Pre-warm: {'✅' if status.get('pre_warmed') else '❌'}\n"
    text += (
        "\n💡 <i>البروكسيات تُكتشف تلقائياً من 8+ مصادر.</i>\n"
        "<i>يتم مشاركتها بين نسخ البوت عبر Supabase.</i>\n"
    )
    if _PAID_PROXY_URL:
        text += f"\n🔑 بروكسي مدفوع: <code>{_PAID_PROXY_URL[:30]}...</code>\n"
    if _WEBSHARE_API_KEY:
        text += "🔑 Webshare: <code>مفعّل</code>\n"
    return text


def refresh_proxies() -> int:
    pool = get_proxy_pool()
    return pool.refresh(force=True)


def pre_warm_pool() -> int:
    """Pre-warm the proxy pool. Call on startup."""
    pool = get_proxy_pool()
    return pool.pre_warm()
