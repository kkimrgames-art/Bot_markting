import os
import random
import logging
import shutil
from typing import Optional, List, Any, Dict
from dataclasses import dataclass
import re
import requests
from urllib.parse import urlparse, parse_qs, unquote

from .config import Config, load_config, get_project_root
from ..bot.persistence import load_state, save_state
import yt_dlp
import time

logger = logging.getLogger(__name__)
_PROJECT_ROOT = get_project_root()

# yt-dlp retry configuration for common errors
_YTDLP_PLAYER_CLIENTS = ["android", "web", "tv_embedded", "ios", "mweb"]
_YTDLP_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def _is_retryable_ytdlp_error(error_msg: str) -> tuple[bool, str]:
    """Check if the yt-dlp error is retryable and return (is_retryable, error_type)."""
    msg = error_msg.lower()
    if "403" in msg or "forbidden" in msg:
        return True, "403_forbidden"
    # Signature/cipher issues - expanded detection
    if any(kw in msg for kw in ["signature", "cipher", "nsig", "n_sig", "decrypt", "n-challenge", "n challenge"]):
        return True, "signature"
    if "sign in" in msg or "login" in msg or "age" in msg or "bot" in msg:
        if "bot" in msg or "confirm you’re not a bot" in msg or "robot" in msg:
            return True, "youtube_botcheck"
        return True, "signature"  # Often requires different player client
    if "requested format is not available" in msg or "no video formats" in msg or "format is not available" in msg:
        return True, "format_unavailable"
    if "unable to extract" in msg or "extractor error" in msg:
        return True, "extractor_error"
    if "http error" in msg or "connection" in msg or "timeout" in msg:
        return True, "network_error"
    if "sabr" in msg or "missing a url" in msg or "throttling" in msg or "152" in msg:
        return True, "format_unavailable"
    if "rate-limited" in msg or "rate limited" in msg or "too many requests" in msg:
        return True, "rate_limited"
    return False, ""


def _resolve_runtime_path(raw_path: Optional[str]) -> str:
    raw = (raw_path or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.abspath(os.path.join(_PROJECT_ROOT, raw))


def _build_retry_opts(base_opts: dict, attempt: int, error_type: str) -> dict:
    """Build modified yt-dlp options for retry attempts based on error type."""
    opts = dict(base_opts)
    
    # Rotate user agent
    ua_idx = attempt % len(_YTDLP_USER_AGENTS)
    opts.setdefault("http_headers", {})
    if isinstance(opts["http_headers"], dict):
        opts["http_headers"] = dict(opts["http_headers"])
        opts["http_headers"]["User-Agent"] = _YTDLP_USER_AGENTS[ua_idx]
    
    # Check for ffmpeg availability
    from .ffmpeg_utils import ffmpeg_bin
    has_ffmpeg = bool(ffmpeg_bin())

    # Configure extractor args for YouTube
    opts.setdefault("extractor_args", {})
    if "youtube" not in opts["extractor_args"]:
        opts["extractor_args"]["youtube"] = {}
    
    # Ensure JS runtime is always set in retries
    opts["js_runtimes"] = {"node": {}}
    
    if error_type in ["403_forbidden", "youtube_botcheck"]:
        # When facing a botcheck or 403, our cookies are likely poisoned or flagged. Drop them immediately.
        if "cookiefile" in opts:
            opts.pop("cookiefile", None)
            logger.info("🍪 Dropped 'cookiefile' from ydl_opts to bypass botcheck/403 block.")
        if "cookies" in opts:
            opts.pop("cookies", None)
        
        # Rotate through clients, prioritizing those that bypass PO Token/403
        clients = ["tv_embedded", "web_creator", "android", "ios", "mweb"]
        client_idx = attempt % len(clients)
        selected_client = clients[client_idx]
        
        # Reset extractor args to ensure clean state
        opts["extractor_args"] = {"youtube": {}}
        opts["extractor_args"]["youtube"]["player_client"] = [selected_client]
        opts["extractor_args"]["youtube"]["formats"] = ["missing_pot"]
        
        # Force specific n_client to match player_client if possible to avoid verification issues
        if selected_client in ["android", "web", "web_creator"]:
             opts["extractor_args"]["youtube"]["n_client"] = [selected_client]
        
        # Even in 403/botcheck retry, attempt to keep good quality
        if attempt > 0:
            if has_ffmpeg:
                opts["format"] = "bestvideo[height<=1080]+bestaudio/best[ext=mp4]/best"
            else:
                opts["format"] = "best[ext=mp4]/best"
            
        logger.info(f"🔄 {error_type} (attempt {attempt}): Switched YouTube client to {selected_client} without cookies")
        
        # Add a delay for 403 to avoid rate limits
        opts["sleep_interval"] = 5 + (attempt * 3)
    
    elif error_type == "signature":
        # For signature errors, try different player clients that handle challenges better
        clients = ["tv_embedded", "android", "ios", "mweb", "web_creator"]
        client_idx = attempt % len(clients)
        opts["extractor_args"]["youtube"]["player_client"] = [clients[client_idx]]
        # Force use of javascript if possible
        opts["extractor_args"]["youtube"]["n_client"] = ["android", "web"]
        opts["extractor_args"]["youtube"]["formats"] = ["missing_pot"]
    
    elif error_type == "format_unavailable":
        if has_ffmpeg:
            fallback_formats = [
                "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "bestvideo[height<=1080]+bestaudio/best[ext=mp4]/best",
                "bestvideo+bestaudio/best",
                "best[ext=mp4]/best",
                "best",
            ]
        else:
            fallback_formats = [
                "best[ext=mp4]/best",
                "best",
            ]
        fmt_idx = attempt % len(fallback_formats)
        opts["format"] = fallback_formats[fmt_idx]
        
        # Try different client combinations to bypass SABR/152
        client_pools = [
            ["tv_embedded"],
            ["ios"],
            ["mweb"],
            ["web_creator"],
            ["android"],
            ["web"],
        ]
        pool_idx = attempt % len(client_pools)
        opts["extractor_args"]["youtube"]["player_client"] = client_pools[pool_idx]
        opts["extractor_args"]["youtube"]["formats"] = ["missing_pot"]
        # Add a small delay for throttling
        opts["sleep_interval"] = 5 * attempt
    
    elif error_type == "rate_limited":
        opts["sleep_interval"] = 15 + (attempt * 10)
        opts["max_sleep_interval"] = 30 + (attempt * 15)
        opts["sleep_requests"] = 5 + attempt
    
    elif error_type == "network_error":
        opts["socket_timeout"] = 60 + (attempt * 10)
        opts["retries"] = 10
        opts["fragment_retries"] = 10
    
    return opts


def _download_with_retry(ydl_opts: dict, url: str, max_retries: int = 5) -> dict:
    """Attempt to download with automatic retry on common errors."""
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        except Exception as e:
            last_error = e
            error_msg = str(e)
            is_retryable, error_type = _is_retryable_ytdlp_error(error_msg)
            
            if not is_retryable or attempt >= max_retries:
                logger.error(f"yt-dlp error (attempt {attempt + 1}/{max_retries + 1}): {error_msg}")
                raise
            
            logger.warning(f"yt-dlp retryable error ({error_type}), attempt {attempt + 1}/{max_retries + 1}: {error_msg}")
            
            # Build retry options
            ydl_opts = _build_retry_opts(ydl_opts, attempt + 1, error_type)
            
            # Exponential backoff with jitter
            sleep_time = min(2 ** attempt + random.uniform(0, 1), 15)
            time.sleep(sleep_time)
    
    raise last_error


def _cleanup_temp_by_video_id(temp_dir: str, video_id: Optional[str]) -> None:
    if not (temp_dir and video_id):
        return
    try:
        import glob

        patterns = [
            os.path.join(temp_dir, f"{video_id}.*"),
            os.path.join(temp_dir, f"{video_id}_*.*"),
        ]
        for pat in patterns:
            for fp in glob.glob(pat):
                try:
                    os.remove(fp)
                except Exception:
                    pass
    except Exception:
        pass


def _resolution_to_height(resolution: Optional[str]) -> Optional[int]:
    if not resolution:
        return None
    r = str(resolution).strip().lower()
    try:
        if r.endswith("p") and r[:-1].isdigit():
            return int(r[:-1])
        if r.isdigit():
            return int(r)
    except Exception:
        return None
    return None


@dataclass
class DownloadResult:
    input_path: str
    title: Optional[str] = None
    id: Optional[str] = None
    source_url: Optional[str] = None


def _read_channels(channel_list_path: str, target_mode: Optional[str] = None) -> List[str]:
    """Read channels from the channel list file, optionally filtering by target_mode"""
    if not os.path.exists(channel_list_path):
        return []
    
    channels = []
    with open(channel_list_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Support inline tags: url #tag1 #tag2
            parts = line.split("#", 1)
            url = parts[0].strip()
            tags_str = parts[1].strip().lower() if len(parts) > 1 else ""
            tags = [t.strip() for t in tags_str.split() if t.strip()]
            
            if not url:
                continue
            
            if target_mode:
                tm = target_mode.lower().strip()
                # If the line has tags, it MUST match the target_mode
                if tags:
                    if tm in tags:
                        channels.append(url)
                    # if tags exist but tm not in tags, skip it
                else:
                    # No tags on this line -> include it for all modes (backward compatibility)
                    channels.append(url)
            else:
                # No target_mode provided -> return all non-commented URLs
                channels.append(url)
                
    return channels


def _fb_sources_path() -> str:
    # Allow override via env; default to spec/facebook_sources.txt
    return _resolve_runtime_path(os.getenv("FB_SOURCES_PATH", os.path.join("spec", "facebook_sources.txt")))


def _read_facebook_sources(target_mode: Optional[str] = None) -> List[str]:
    path = _fb_sources_path()
    try:
        if os.path.exists(path):
            channels = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    
                    # Support inline tags: url #tag1 #tag2
                    parts = line.split("#", 1)
                    url = parts[0].strip()
                    tags_str = parts[1].strip().lower() if len(parts) > 1 else ""
                    tags = [t.strip() for t in tags_str.split() if t.strip()]
                    
                    if not url:
                        continue
                    
                    if target_mode:
                        tm = target_mode.lower().strip()
                        if tags:
                            if tm in tags:
                                channels.append(url)
                        else:
                            # For FB sources, if no tags, we still include it if mode is games
                            # because FB sources are currently mostly games.
                            channels.append(url)
                    else:
                        channels.append(url)
            return channels
    except Exception:
        pass
    return []


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, allow_redirects: bool = True):
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = os.getenv("FB_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        req_headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1"
        })

        cookies = None
        if "facebook.com" in url:
            cpath = os.getenv("FB_COOKIES_PATH")
            if cpath:
                cpath = _resolve_runtime_path(cpath)
                if os.path.exists(cpath):
                    jar = requests.cookies.RequestsCookieJar()
                    try:
                        with open(cpath, "r", encoding="utf-8") as cf:
                            count = 0
                            for line in cf:
                                line = line.strip()
                                if not line or line.startswith("#"): continue
                                parts = line.split("\t")
                                if len(parts) < 7: continue
                                domain, flag, path, secure, expiry, name, value = parts[:7]
                                # Only apply facebook/messenger cookies
                                if "facebook.com" in domain or "messenger.com" in domain:
                                    jar.set(name, value, domain=domain, path=path, secure=(secure.upper() == "TRUE"))
                                    count += 1
                        cookies = jar
                        logger.debug(f"Loaded {count} cookies for {url}")
                    except Exception as e:
                        logger.error(f"Error loading cookies from {cpath}: {e}")
        
        resp = requests.get(url, headers=req_headers, timeout=timeout, allow_redirects=allow_redirects, cookies=cookies)
        logger.debug(f"GET {url} -> Status {resp.status_code}, Final URL: {resp.url}")
        return resp
    except Exception as e:
        logger.error(f"HTTP GET failed for {url}: {e}")
        return None


def _expand_facebook_candidates(u: str, fb_ua: Optional[str]) -> List[str]:
    """Expand a Facebook share/redirect URL into likely canonical post/reel/video URLs."""
    urls: List[str] = [u]
    visited = set()
    headers = {"User-Agent": fb_ua} if fb_ua else {}

    try:
        def scrape_and_collect(page_url: str, depth: int = 0):
            if depth > 1 or page_url in visited:
                return
            visited.add(page_url)

            # Proactive variants for profile/page URLs (don't scrape these variants yet, just add them as candidates)
            if not any(x in page_url for x in ["/reels", "/videos", "/watch", "/reel/"]):
                base_clean = page_url.split("?")[0].rstrip("/")
                if "/profile.php" not in base_clean and "/people/" not in base_clean and len(base_clean) > 25:
                    urls.append(f"{base_clean}/reels/")
                    urls.append(f"{base_clean}/videos/")
                elif "/profile.php" in page_url:
                    qs_p = parse_qs(urlparse(page_url).query)
                    pid = qs_p.get("id", [None])[0]
                    if pid:
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=reels_tab")
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=owner_reels")
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=videos")
                elif "/people/" in base_clean:
                    # Handle /people/{name}/{numeric_id}/ URL format
                    people_id_match = re.search(r'/people/[^/]+/(\d{10,20})/?', base_clean)
                    if people_id_match:
                        pid = people_id_match.group(1)
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=reels_tab")
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=owner_reels")
                        urls.append(f"https://www.facebook.com/profile.php?id={pid}&sk=videos")
                        urls.append(f"{base_clean}/reels/")
                        urls.append(f"{base_clean}/videos/")

            # Perform HTTP GET to find redirects and content
            resp = _http_get(page_url, headers=headers, timeout=10, allow_redirects=True)
            if resp is None:
                return
            
            final_url = getattr(resp, "url", None)
            if final_url and final_url not in urls:
                urls.append(final_url)
                # If we redirected and haven't reached depth limit, scrape the final one
                if final_url != page_url:
                    scrape_and_collect(final_url, depth + 1)

            text = resp.text or ""
            
            # Find direct Reel/Video patterns (numeric IDs: 10-20 digits)
            reel_patterns = [
                r'/reel/(\d{10,20})', 
                r'href="/reel/(\d{10,20})',
                r'facebook\.com/reel/(\d{10,20})',
                r'/videos/(\d{10,20})',
                r'facebook\.com/[^/]+/videos/(\d{10,20})',
                r'"videoID":"(\d{10,20})"',
                r'"video_id":"(\d{10,20})"',
                r'fb://reel/(\d{10,20})',
                r'"target_id":"(\d{10,20})"',
                r'original_video_id["\']:\s*["\'](\d{10,20})["\']'
            ]
            
            for pat in reel_patterns:
                for match in re.findall(pat, text):
                    # Found a numeric ID, add common formats
                    urls.append(f"https://www.facebook.com/reel/{match}/")
                    urls.append(f"https://www.facebook.com/watch/?v={match}")

            # Find Profile/People/Page ID patterns (numeric IDs, usually smaller or different context)
            profile_patterns = [
                r'/people/[^/]+/(\d{10,20})/',
                r'facebook\.com/profile\.php\?id=(\d{10,20})',
                r'fb://profile/(\d{10,20})'
            ]
            for pat in profile_patterns:
                for match in re.findall(pat, text):
                    if match not in urls:
                        urls.append(f"https://www.facebook.com/profile.php?id={match}&sk=reels_tab")
                        urls.append(f"https://www.facebook.com/profile.php?id={match}&sk=videos")

            # Try to resolve vanity URL part (e.g. facebook.com/PAGE_NAME)
            # Look for titles or metadata that might have the vanity slug
            slug_match = re.search(r'facebook\.com/([^/?#]+)/', final_url or "")
            if slug_match:
                slug = slug_match.group(1)
                if slug not in ["profile.php", "people", "groups", "sharer"]:
                    urls.append(f"https://www.facebook.com/{slug}/reels/")
                    urls.append(f"https://www.facebook.com/{slug}/videos/")

        # Start scraping from the original URL
        scrape_and_collect(u)
        
    except Exception:
        pass

    # Clean up and normalize results
    out_reels: List[str] = []
    out_sections: List[str] = []
    out_others: List[str] = []
    seen = set()
    
    for x in urls:
        if not x: continue
        # Normalize: ensure it's a full URL and prefer www.facebook.com
        if x.startswith("/"): x = "https://www.facebook.com" + x
        x = x.replace("web.facebook.com", "www.facebook.com").replace("m.facebook.com", "www.facebook.com")
        
        # Filter out obvious non-video pages
        if "/people/" in x and "?sk=" not in x and "/videos" not in x and "/reels" not in x:
            continue
        
        # Strip trailing slashes and normalize query for seen set
        clean_key = x.rstrip("/").split("#")[0]
        if clean_key in seen: continue
        seen.add(clean_key)
        
        if "/reel/" in x or "/watch/" in x or "video_id" in x:
            out_reels.append(x)
        elif "/reels/" in x or "/videos/" in x or "sk=" in x:
            out_sections.append(x)
        else:
            out_others.append(x)
    
    # Prioritize: reels -> sections -> profiles
    return out_reels + out_sections + out_others


def _select_next_channel(channel_list_path: str, state: dict) -> str:
    """Select the next channel to use, rotating through all channels"""
    # Always sync channels from file to state first
    file_channels = _read_channels(channel_list_path)
    if not file_channels:
        raise ValueError("No channels found in spec/channels.txt")
    
    # Sync file channels with state channels
    state_channels = state.get("channels", [])
    # Add any new channels from file that aren't in state
    for ch in file_channels:
        if ch not in state_channels:
            state_channels.append(ch)
    # Remove any channels from state that aren't in file
    state_channels = [ch for ch in state_channels if ch in file_channels]
    state["channels"] = state_channels
    
    # Use state channels for processing
    channels = state_channels
    
    # Filter channels based on enabled status
    enabled_channels = state.get("enabled_channels", {})
    # If enabled_channels is empty, all channels are considered enabled by default
    if enabled_channels:
        filtered_channels = [ch for ch in channels if enabled_channels.get(ch, True)]
        if not filtered_channels:
            # If no channels are enabled, use all channels
            filtered_channels = channels
    else:
        # If no enabled_channels dict exists, all channels are enabled
        filtered_channels = channels
    
    if not filtered_channels:
        raise ValueError("No enabled channels found in spec/channels.txt")
    
    # Get the index of the last used channel among filtered channels
    last_channel_index = state.get("downloader", {}).get("last_channel_index", -1)
    
    # Select the next channel (rotate)
    next_index = (last_channel_index + 1) % len(filtered_channels)
    
    # Update state with the new index
    state.setdefault("downloader", {})["last_channel_index"] = next_index
    save_state(state, load_config())

    # Return the selected channel URL
    return filtered_channels[next_index]


def _music_meta_reject(info_dict: Dict[Any, Any], enabled: bool) -> Optional[str]:
    """Return a reason string to reject likely music videos based on metadata, or None to accept.

    Enhanced heuristics for better music detection:
    - categories include 'Music'
    - presence of 'artist', 'track', 'album' fields
    - uploader contains 'vevo', 'topic', 'records', 'music'
    - title hints: multiple music-related phrases
    - description contains music indicators
    - tags contain music-related keywords
    """
    if not enabled:
        return None

    try:
        # Category check
        cats = info_dict.get("categories") or []
        if isinstance(cats, list) and any(str(c).strip().lower() == "music" for c in cats):
            return "Category Music detected"

        # Artist/track/album fields
        if (info_dict.get("artist") or info_dict.get("track") or info_dict.get("album")):
            return "Artist/track/album metadata present"

        # Uploader patterns (more comprehensive)
        upl = (info_dict.get("uploader") or info_dict.get("uploader_id") or "")
        upl_l = str(upl).lower()
        music_uploader_patterns = [
            "vevo", "- topic", "records", "music", "official",
            "entertainment", "label", "productions"
        ]
        for pattern in music_uploader_patterns:
            if pattern in upl_l:
                return f"Uploader indicates music channel: {pattern}"

        # Title patterns (comprehensive)
        title = (info_dict.get("title") or info_dict.get("alt_title") or "").lower()
        music_phrases = [
            "official music video",
            "lyric video",
            "audio (official)",
            "official audio",
            "music video",
            "lyrics",
            "(official video)",
            "(official)",
            "[official]",
            "official mv",
            "visualizer",
            "audio only",
            "full song",
            "ft.",  # featuring
            "feat.",
            "prod. by",
            "produced by",
        ]
        for p in music_phrases:
            if p in title:
                return f"Title indicates music: {p}"
        
        # Check for common music title patterns (Artist - Song Title)
        if " - " in title and len(title.split(" - ")) == 2:
            parts = title.split(" - ")
            # If both parts are relatively short (typical for artist - song format)
            if len(parts[0]) < 50 and len(parts[1]) < 50:
                # Additional check: no common video words
                non_music_words = ["react", "review", "tutorial", "how to", "guide", "tips"]
                if not any(word in title for word in non_music_words):
                    return "Title format suggests music: Artist - Song"
        
        # Description check
        desc = (info_dict.get("description") or "").lower()
        music_desc_keywords = [
            "stream", "spotify", "apple music", "itunes", "download",
            "available now", "out now", "new album", "new single"
        ]
        for keyword in music_desc_keywords:
            if keyword in desc:
                return f"Description indicates music: {keyword}"
        
        # Tags check
        tags = info_dict.get("tags") or []
        if isinstance(tags, list):
            music_tags = [
                "music", "song", "official", "audio", "lyrics",
                "music video", "mv", "single", "album"
            ]
            tags_lower = [str(t).lower() for t in tags]
            music_tag_count = sum(1 for tag in tags_lower if any(mt in tag for mt in music_tags))
            # If more than 2 music-related tags, likely music
            if music_tag_count >= 2:
                return f"Multiple music tags detected: {music_tag_count}"

        return None
    except Exception:
        return None


def _is_video_processed(video_id: str, state: dict) -> bool:
    """Check if a video has already been processed"""
    processed_videos = state.get("downloader", {}).get("processed_videos", [])
    return video_id in processed_videos


def _mark_video_as_processed(video_id: str, state: dict, channel_url: Optional[str] = None) -> None:
    """Mark a video as processed to avoid reprocessing
    
    Args:
        video_id: The video ID to mark as processed
        state: The state dictionary
        channel_url: Optional channel URL to track per-channel history
    """
    # Global processed videos list
    processed_videos = state.setdefault("downloader", {}).setdefault("processed_videos", [])
    
    # Add the video ID to the global list
    if video_id not in processed_videos:
        processed_videos.append(video_id)
        
        # Keep only the last 500 processed videos (increased from 100)
        if len(processed_videos) > 500:
            processed_videos = processed_videos[-500:]
        
        # Always save the updated list
        state["downloader"]["processed_videos"] = processed_videos
    
    # Per-channel tracking for better rotation
    if channel_url:
        channel_history = state.setdefault("downloader", {}).setdefault("channel_history", {})
        channel_videos = channel_history.setdefault(channel_url, [])
        
        if video_id not in channel_videos:
            channel_videos.append(video_id)
            
            # Keep last 200 videos per channel
            if len(channel_videos) > 200:
                channel_videos = channel_videos[-200:]
                channel_history[channel_url] = channel_videos
    
    save_state(state, load_config())
    
    # Sync to Supabase
    try:
        from ..agent.supabase_storage import save_processed_video, save_channel_history
        save_processed_video(video_id)
        if channel_url:
            save_channel_history(channel_url, video_id)
    except Exception as e:
        logger.warning(f"Failed to sync processed video to Supabase: {e}")


def _is_facebook_video(info: Dict[str, Any]) -> bool:
    """Determine if the extracted yt-dlp info corresponds to a video on Facebook."""
    if not info:
        return False
    
    # Check if we have video/duration info
    if info.get('duration') or info.get('vcodec') or info.get('acodec'):
        return True
        
    # Check for direct video formats
    formats = info.get('formats', [])
    if any(f.get('vcodec') != 'none' for f in formats):
        return True
        
    # Check extractor/type
    extractor = info.get('extractor_key', '').lower()
    if 'facebook' in extractor:
        # If it's a playlist/entry, check entries
        if info.get('_type') == 'playlist':
            return True
        # If it has a URL ending in .mp4 in metadata
        if info.get('url') and ('.mp4' in info['url'] or '.m3u8' in info['url']):
            return True
            
    return False


def download_one(url_or_feed: Optional[str], temp_dir: str, channel_list_path: str, max_height: Optional[int] = None, target_mode: Optional[str] = None) -> DownloadResult:
    os.makedirs(temp_dir, exist_ok=True)

    # Load configuration and state
    cfg = load_config()
    state = load_state(cfg)
    
    # Determine selected sources to try
    sources_to_try = []
    if url_or_feed:
        sources_to_try = [url_or_feed]
    else:
        # Randomize channel selection to avoid always starting from the first channel.
        all_channels = _read_channels(channel_list_path, target_mode=target_mode)
        if not all_channels:
            raise ValueError(f"No channels found in {channel_list_path} for mode: {target_mode}")

        enabled_channels = state.get("enabled_channels", {}) or {}
        if enabled_channels:
            selectable_channels = [ch for ch in all_channels if enabled_channels.get(ch, True)]
            if not selectable_channels:
                selectable_channels = list(all_channels)
        else:
            selectable_channels = list(all_channels)

        st_dl = state.setdefault("downloader", {})
        recent_channels = st_dl.get("recent_channels", []) or []
        try:
            recent_set = set(str(x) for x in recent_channels)
        except Exception:
            recent_set = set()

        # Prefer channels not used recently; if that exhausts, fall back to all.
        fresh = [ch for ch in selectable_channels if ch not in recent_set]
        pool = fresh if fresh else list(selectable_channels)

        random.shuffle(pool)

        # Try up to 10 channels or all if less
        limit = min(len(pool), 10)
        sources_to_try = pool[:limit]

        try:
            logger.info("Channel candidates (randomized): %s", " | ".join(str(x) for x in sources_to_try))
        except Exception:
            pass

        # Persist the order hint: remember the first candidate as "recent" even if it fails,
        # so consecutive runs don't keep trying the same first channel.
        try:
            for ch in sources_to_try[:3]:
                if ch and ch not in recent_channels:
                    recent_channels.append(ch)
            st_dl["recent_channels"] = recent_channels[-30:]
            save_state(state, load_config())
        except Exception:
            pass
    
    selected_result = None
    last_error = None
    
    # Get settings
    max_duration = state.get("max_duration", 60)  # Default to 60 seconds for Shorts
    mus = (state.get("music", {}) or {})
    meta_music_filter_enabled = bool(getattr(cfg, "MUSIC_DETECTION_ENABLED", False) and mus.get("metadata_filter", True) and mus.get("enabled", True))
    
    # We will update these per-source inside the loop
    expected_handle = None
    apply_meta_filter = False

    def _is_youtube_url(u: Optional[str]) -> bool:
        try:
            if not u:
                return False
            s = str(u)
            return ("youtube.com" in s) or ("youtu.be" in s)
        except Exception:
            return False

    def _is_youtube_shorts(info_dict: Dict[Any, Any]) -> bool:
        try:
            for k in ("webpage_url", "original_url", "url"):
                u = info_dict.get(k)
                if not u:
                    continue
                s = str(u)
                if ("youtube.com" in s or "youtu.be" in s) and ("/shorts/" in s):
                    return True
        except Exception:
            return False
        return False

    def duration_filter(info_dict: Dict[Any, Any]) -> Optional[str]:
        """Strict Shorts-only policy:
        - Must have a duration.
        - Duration must be <= 60 seconds (Standard YouTube Shorts limit).
        - If YouTube, must be a Shorts URL (/shorts/) or duration <= 60s.
        """
        try:
            dur = info_dict.get('duration')
            if dur is None:
                return "Missing duration"
            
            # Global limit for ALL videos: strictly under 60 seconds
            if float(dur) > 60:
                return f"Video is too long for Shorts ({dur}s > 60s)"

            # Special check for YouTube to ensure it's categorized as Shorts if possible
            is_yt = False
            try:
                u = info_dict.get("webpage_url") or info_dict.get("original_url") or info_dict.get("url")
                if _is_youtube_url(u):
                    is_yt = True
            except Exception:
                pass

            if is_yt:
                # If YouTube, we prefer /shorts/ URLs, but allow others if < 60s
                # (Some shorts are uploaded as normal videos but are still shorts)
                pass

        except Exception:
            return "Invalid duration"
        return None

    def composite_filter(info_dict: Dict[Any, Any]) -> Optional[str]:
        # If targeting a specific YouTube handle, ensure entries belong to that channel
        if expected_handle and isinstance(info_dict, dict):
            try:
                ch_url = (info_dict.get("uploader_url") or info_dict.get("channel_url") or "").lower()
                ch_name = (info_dict.get("uploader") or info_dict.get("channel") or "").lower()
                # Strong check via channel URL containing the handle
                if ch_url:
                    if f"/@{expected_handle}" not in ch_url:
                        return "Different channel (handle mismatch)"
                elif ch_name:
                    # Fallback: compare normalized names (best-effort)
                    if expected_handle not in ch_name.replace(" ", ""):
                        return "Different channel (name mismatch)"
            except Exception:
                pass
        # Duration first
        reason = duration_filter(info_dict)
        if reason:
            return reason
        # Metadata-based music filter (only for playlist/feed selection)
        if apply_meta_filter:
            reason = _music_meta_reject(info_dict, enabled=meta_music_filter_enabled)
            if reason:
                return f"Rejected (music): {reason}"
        return None

    # Proxy from state/env and prefer IPv4 to reduce DNS issues
    proxy_url = (
        (state.get("proxy", {}) or {}).get("url")
        or os.getenv("YTDLP_PROXY_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
    )

    # ===== تحديد أقصى ارتفاع للتحميل =====
    # YouTube يوفر 1080p+ فقط كـ adaptive streams (فيديو+صوت منفصلين)
    # يجب استخدام FFmpeg لدمجهما. لا نستخدم format 18 (360p) أبداً.
    is_aotu = str(max_height).lower() == "aotu" if max_height else False
    
    if is_aotu:
        # أفضل جودة متاحة بدون قيود
        ydl_format = (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/"
            "best"
        )
    else:
        max_res_env = (os.getenv("MAX_RESOLUTION") or "1080p").strip().lower()
        if "2160" in max_res_env or "4k" in max_res_env:
            env_max_h = 2160
        elif "1440" in max_res_env:
            env_max_h = 1440
        elif "1080" in max_res_env:
            env_max_h = 1080
        else:
            env_max_h = 720

        try:
            requested_h = int(max_height) if max_height else None
        except Exception:
            requested_h = None

        max_h = env_max_h
        if requested_h:
            max_h = max(env_max_h, requested_h)

        # أولوية الجودة: أعلى جودة ممكنة مع FFmpeg merge
        ydl_format = (
            f"bestvideo[height<={max_h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_h}][vcodec^=avc1]+bestaudio/"
            f"bestvideo[height<={max_h}]+bestaudio/"
            f"best[height<={max_h}][ext=mp4]/"
            f"best[height<={max_h}]/"
            f"bestvideo+bestaudio/"
            f"best"
        )

    from .ffmpeg_utils import ffmpeg_bin
    ffmpeg_path = ffmpeg_bin()
    
    # Critical Check: If ffmpeg is missing, we MUST use a single-file format (mp4).
    # Requesting separate video+audio without ffmpeg causes 'merging' errors and often fails.
    if not ffmpeg_path:
        logger.warning("⚠️ FFmpeg not found! Forcing single-file format (best[ext=mp4]) to ensure download success.")
        ydl_format = "best[ext=mp4]/best"
    
    ydl_opts = {
        "ffmpeg_location": ffmpeg_path,
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "format": ydl_format,
        "noplaylist": True,
        "merge_output_format": "mp4" if ffmpeg_path else None,
        "playlistend": 20,
        "quiet": False,
        "no_warnings": False,
        "match_filter": composite_filter,
        "force_ipv4": cfg.YTDLP_FORCE_IPV4,
        "extract_flat": "in_playlist",
        "no_check_certificate": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "ignoreerrors": True,
        "max_filesize": 50 * 1024 * 1024,  # 50 MB max
        "check_formats": "selected",
        # Enable JavaScript runtimes for challenge solving (fixes n-challenge)
        "js_runtimes": {"node": {}},
        # Default extractor args for better compatibility
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "android", "web"],
                "n_client": ["tv_embedded", "web"],
                "formats": ["missing_pot"],
            }
        },
        "prefer_free_formats": False,
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": True,
    }
    if getattr(cfg, "YTDLP_FORCE_IPV4", True):
        ydl_opts["source_address"] = "0.0.0.0"

    try:
        ydl_opts["ffmpeg_location"] = ffmpeg_bin()
    except Exception:
        pass
    if proxy_url:
        ydl_opts["proxy"] = proxy_url

    fb_cookies = os.getenv("FB_COOKIES_PATH")
    yt_cookies = os.getenv("YT_COOKIES_PATH")
    fb_cookies_path = None
    yt_cookies_path = None
    if fb_cookies:
        p = _resolve_runtime_path(fb_cookies)
        if os.path.exists(p): fb_cookies_path = p
    if yt_cookies:
        p = _resolve_runtime_path(yt_cookies)
        if os.path.exists(p): yt_cookies_path = p

    fb_ua = os.getenv("FB_USER_AGENT")
    facebook_headers = {"User-Agent": fb_ua} if fb_ua else None

    for current_source in sources_to_try:
        try:
            selected_url = current_source
            logger.info(f"Targeting source: {selected_url}")

            # Compute effective content mode (per-channel overrides global)
            channel_modes = state.get("channel_content_mode", {})
            per_channel_mode = (channel_modes.get(selected_url) or "").strip().lower()

            # Fix: If not found in legacy map, check modern publish_channels list
            if not per_channel_mode:
                for pc in state.get("publish_channels", []):
                    # Match by YouTube channel ID (handle or UC*)
                    if (pc.get("channel_id") == selected_url) or (f"https://www.youtube.com/@{pc.get('channel_id')}" in selected_url) or (f"channel/{pc.get('channel_id')}" in selected_url):
                        per_channel_mode = (pc.get("content_type") or "").strip().lower()
                        if per_channel_mode:
                            break

            global_mode = (state.get("content_mode") or os.getenv("CONTENT_MODE") or "").strip().lower()
            effective_mode = per_channel_mode if per_channel_mode else global_mode

            fb_sources = _read_facebook_sources(target_mode=target_mode)
            # Only use Facebook for Games content, NOT Minecraft 🆕
            # target_mode comes from core.py and represents the requested publish channel type
            final_mode = target_mode if target_mode else effective_mode
            use_fb_mode = (final_mode == "games") and bool(fb_sources)
            
            # Common gaming keywords for filtering
            gaming_keywords = [
                "game", "play", "gaming", "لعبة", "لعب", "جيم", "gameplay", "mobile", "android", 
                "funny", "short", "viral", "reel", "fun", "meme", "trending", "attitude", "video"
            ]
            
            # Decide initial URL used for info discovery
            url = selected_url
            expected_handle: Optional[str] = None
            try:
                m_handle = re.search(r"youtube\.com/@([^/\?]+)", url, re.IGNORECASE)
                if m_handle:
                    expected_handle = m_handle.group(1).strip().lower()
            except Exception:
                expected_handle = None

            if use_fb_mode:
                url = random.choice(fb_sources)
                logger.info(f"Routing to Facebook source: {url}")
            else:
                logger.info(f"Using selected URL: {url}")
    
            # Apply metadata-based filter only when rotating through generic feeds (no explicit handle),
            # not for explicit single URLs and not for exact @handle pages to avoid over-filtering.
            apply_meta_filter = (not bool(url_or_feed)) and not bool(expected_handle)

            def _candidate_urls(u: str) -> List[str]:
                if not u:
                    return []
                
                # Check if it's a direct video URL (has watch?v= or /shorts/VIDEO_ID)
                if "watch?v=" in u or "/shorts/" in u:
                    return [u]
                
                base = u.split("?")[0]
                m_handle = re.match(r"^https?://(?:www\.)?youtube\.com/@([^/]+)(?:/(shorts|videos))?$", base, re.IGNORECASE)
                if m_handle:
                    handle = m_handle.group(1) or ""
                    root = f"https://youtube.com/@{handle}"
                    return [
                        f"{root}/shorts",  # Direct shorts playlist (most reliable)
                        f"ytsearchdate50:{handle} short",  # Alternative search term
                        f"ytsearchdate50:{handle}",  # Channel search without "shorts"
                        base  # Original URL fallback
                    ]
                
                if "facebook.com" in base:
                    try:
                        return _expand_facebook_candidates(u, fb_ua)
                    except Exception:
                        return [u]
                
                return [u] # Return full URL instead of stripped base

            candidates = _candidate_urls(url)
            logger.info(f"Total candidates to try: {len(candidates)}")

            last_cand_error = None
            for cand in candidates:
                try:
                    logger.info(f"Attempting to extract info from: {cand}")
                    # Clone options per-candidate to scope headers only when needed
                    per_opts = dict(ydl_opts)
                    if ("facebook.com" in cand) or ("fb.watch" in cand) or ("mbasic.facebook.com" in cand) or ("m.facebook.com" in cand):
                        if facebook_headers:
                            per_opts["http_headers"] = facebook_headers
                        if fb_cookies_path:
                            per_opts["cookiefile"] = fb_cookies_path
                    elif ("youtube.com" in cand) or ("youtu.be" in cand):
                        if yt_cookies_path:
                            per_opts["cookiefile"] = yt_cookies_path
                    else:
                        per_opts.pop("http_headers", None)
                    with yt_dlp.YoutubeDL(per_opts) as ydl_info:  # type: ignore
                        try:
                            info = ydl_info.extract_info(cand, download=False)
                        except yt_dlp.utils.DownloadError as e:
                            err_str = str(e).lower()
                            is_retryable, error_type = _is_retryable_ytdlp_error(err_str)
                            
                            if is_retryable and ("youtube.com" in cand or "youtu.be" in cand):
                                # Try with different player clients
                                logger.warning(f"Retrying extract_info with alternative player client ({error_type})")
                                retry_opts = _build_retry_opts(per_opts, 1, error_type)
                                retry_opts["extract_flat"] = "in_playlist"
                                try:
                                    with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                                        info = ydl_retry.extract_info(cand, download=False)
                                except Exception:
                                    logger.warning(f"Retry also failed for {cand}")
                                    continue
                            else:
                                if "sign" in err_str and ("java" in err_str or "js" in err_str):
                                    logger.error("❌ فشل فك التشفير (Signature). جارٍ تجربة player clients بديلة...")
                                logger.warning(f"DownloadError for {cand}: {e}")
                                continue
                        if info:
                            logger.info(f"Successfully extracted info from candidate: {cand}")
                        else:
                            logger.warning(f"Extraction returned no data for candidate: {cand}")
                        
                        # Additional filtering for Facebook: ensure it's a video, not an image
                        if "facebook.com" in cand and info:
                            # Check if it's actually a video
                            if not _is_facebook_video(info):
                                logger.warning(f"Skipping non-video content (image/post): {cand}")
                                continue
                        
                        # Fallback: if this is a handle URL and we got no entries, use ytsearchdate
                        need_search_fallback = False
                        if not info:
                            need_search_fallback = True
                        elif info.get("_type") == "playlist" and not (info.get("entries") or []):
                            need_search_fallback = True

                        if need_search_fallback:
                            m = re.search(r"youtube\.com/@([^/]+)", cand, re.IGNORECASE)
                            if m:
                                handle = m.group(1)
                                queries = [
                                    f"ytsearchdate50:{handle} shorts",
                                    f"ytsearchdate50:{handle}"
                                ]
                                for q in queries:
                                    try:
                                        qinfo = ydl_info.extract_info(q, download=False)
                                        if qinfo and qinfo.get("_type") == "playlist" and (qinfo.get("entries") or []):
                                            info = qinfo
                                            break
                                    except Exception:
                                        continue
                    
                        # When it's a channel/playlist, it will return a dict with entries
                        if info and info.get("_type") == "playlist":
                            # Use a stable channel key: prefer @handle when available, else channel UC id, else candidate URL
                            if expected_handle:
                                chan_key = f"@{expected_handle}"
                            else:
                                chan_key = (info.get("id") if info.get("id") and info.get("id").startswith("UC") else None) or cand
                            entries = info.get("entries") or []
                            
                            # For Facebook reels, prioritize recent gaming content
                            if use_fb_mode and effective_mode == "games" and "facebook.com" in cand:
                                # Sort by upload date if available to get newer content
                                try:
                                    entries.sort(key=lambda x: (x.get("upload_date") or "0"), reverse=True)
                                except Exception:
                                    pass
                            
                            # Filter entries by channel, duration, music metadata, content mode, processed status, and recent skips
                            valid_entries = []
                            recent_skipped = (state.get("downloader", {}) or {}).get("recent_skipped", [])
                            processed_list = (state.get("downloader", {}) or {}).get("processed_videos", [])
                            for entry in entries:
                                if entry and entry.get("id"):
                                    # Resolve a robust video id for comparisons
                                    vid_entry = (
                                        entry.get("id")
                                        or entry.get("video_id")
                                        or (entry.get("webpage_url") or entry.get("url") or "").split("v=")[-1].split("&")[0]
                                    )
                                    # Avoid immediately re-selecting recently skipped or already processed videos
                                    try:
                                        if vid_entry and vid_entry in recent_skipped:
                                            continue
                                    except Exception:
                                        pass
                                    # Enforce channel when expected_handle is provided
                                    if expected_handle:
                                        ch_url = (entry.get("uploader_url") or entry.get("channel_url") or "").lower()
                                        ch_name = (entry.get("uploader") or entry.get("channel") or "").lower()
                                        if ch_url:
                                            if f"/@{expected_handle}" not in ch_url:
                                                continue
                                        elif ch_name:
                                            if expected_handle not in ch_name.replace(" ", ""):
                                                continue
                                    # Skip processed videos if enabled
                                    if cfg.SKIP_PROCESSED_VIDEOS and vid_entry and _is_video_processed(vid_entry, state):
                                        logger.debug(f"Skipping processed video: {entry['id']}")
                                        continue
                                    # Check duration
                                    dur_ok = (not max_duration) or (not entry.get("duration")) or (entry.get("duration") <= max_duration)
                                    if not dur_ok:
                                        continue
                                    # Filter Facebook content more leniently
                                    if use_fb_mode:
                                        t = (entry.get("title") or "")
                                        d = (entry.get("description") or "")
                                        tags = entry.get("tags") or []
                                        txt = f"{t}\n{d}\n{' '.join(str(x) for x in tags)}".lower()
                                        
                                        if effective_mode == "minecraft":
                                            mc_match = ("minecraft" in txt) or ("ماينكرافت" in txt)
                                            if not mc_match and not any(kw in txt for kw in gaming_keywords):
                                                continue
                                        elif effective_mode == "games":
                                            is_gaming = any(kw in txt for kw in gaming_keywords)
                                            if not is_gaming and len(valid_entries) >= 5:
                                                continue
                                    # Check music metadata
                                    if _music_meta_reject(entry, enabled=meta_music_filter_enabled):
                                        continue
                                    valid_entries.append(entry)

                            # Aggressive cleanup: drop entries already processed or recently skipped
                            if valid_entries:
                                cleaned: list[dict] = []
                                for e in valid_entries:
                                    try:
                                        vid0 = e.get("id") or e.get("video_id") or (e.get("webpage_url") or e.get("url") or "").split("v=")[-1].split("&")[0]
                                    except Exception:
                                        vid0 = e.get("id")
                                    if vid0 and (vid0 in processed_list or vid0 in recent_skipped):
                                        continue
                                    cleaned.append(e)
                                valid_entries = cleaned
                            
                            # If no valid entries and we're not skipping processed videos, try relaxed filter
                            if not valid_entries and not cfg.SKIP_PROCESSED_VIDEOS:
                                for entry in entries:
                                    if not entry:
                                        continue
                                    # Enforce channel in fallback too when a handle is expected
                                    if expected_handle:
                                        ch_url = (entry.get("uploader_url") or entry.get("channel_url") or "").lower()
                                        ch_name = (entry.get("uploader") or entry.get("channel") or "").lower()
                                        if ch_url:
                                            if f"/@{expected_handle}" not in ch_url:
                                                continue
                                        elif ch_name:
                                            if expected_handle not in ch_name.replace(" ", ""):
                                                continue
                                    # Duration check
                                    if max_duration and entry.get("duration") and entry.get("duration") > max_duration:
                                        continue
                                    # Music metadata check
                                    if _music_meta_reject(entry, enabled=meta_music_filter_enabled):
                                        continue
                                    valid_entries.append(entry)
                            
                            # Select a valid entry or fall back when none unprocessed available
                            reuse_allowed = False
                            if valid_entries:
                                selected_pool = valid_entries
                                # Deterministic rotation per channel/playlist
                                chan_idx_map = state.setdefault("downloader", {}).setdefault("channel_video_index", {})
                                if cfg.SINGLE_VIDEO_MODE:
                                    rotation_start = 0
                                else:
                                    rotation_start = int(chan_idx_map.get(chan_key, -1)) + 1
                                n = len(valid_entries)
                                chosen = None
                                # Prefer unprocessed entries scanning from rotation_start
                                for step in range(n):
                                    idx = (rotation_start + step) % n
                                    e = valid_entries[idx]
                                    vid = e.get("id")
                                    if not (cfg.SKIP_PROCESSED_VIDEOS and vid and _is_video_processed(vid, state)):
                                        chosen = e
                                        chan_idx_map[chan_key] = idx
                                        save_state(state, load_config())
                                        break
                                if chosen is None:
                                    if cfg.SKIP_PROCESSED_VIDEOS:
                                        chosen = valid_entries[rotation_start % n]
                                        reuse_allowed = True
                                        chan_idx_map[chan_key] = rotation_start % n
                                        save_state(state, load_config())
                                    else:
                                        chosen = valid_entries[rotation_start % n]
                                        chan_idx_map[chan_key] = rotation_start % n
                                        save_state(state, load_config())
                                info = chosen
                                try:
                                    logger.info(
                                        "Selected entry from %s: id=%s title=%s",
                                        str(chan_key),
                                        str((info or {}).get("id")),
                                        str((info or {}).get("title")),
                                    )
                                except Exception:
                                    pass
                            elif entries:
                                selected_pool = entries
                                if cfg.SKIP_PROCESSED_VIDEOS:
                                    # Avoid always picking the first entry when falling back.
                                    try:
                                        entries_shuf = [e for e in entries if e]
                                        random.shuffle(entries_shuf)
                                        info = entries_shuf[0] if entries_shuf else entries[0]
                                    except Exception:
                                        info = entries[0]
                                    reuse_allowed = True
                                else:
                                    if cfg.SINGLE_VIDEO_MODE:
                                        try:
                                            entries_shuf = [e for e in entries if e]
                                            random.shuffle(entries_shuf)
                                            info = entries_shuf[0] if entries_shuf else entries[0]
                                        except Exception:
                                            info = entries[0]
                                    else:
                                        try:
                                            entries_shuf = [e for e in entries if e]
                                            random.shuffle(entries_shuf)
                                            info = entries_shuf[0] if entries_shuf else entries[0]
                                        except Exception:
                                            info = entries[0]
                            else:
                                raise RuntimeError("No valid videos found in channel/playlist")
                        
                        # Lenient validation for non-playlist videos in FB mode
                        if info and use_fb_mode and info.get("_type") != "playlist":
                            t = (info.get("title") or "")
                            d = (info.get("description") or "")
                            tags = info.get("tags") or []
                            txt = f"{t}\n{d}\n{' '.join(str(x) for x in tags)}".lower()
                            
                            if effective_mode == "minecraft":
                                mc_match = ("minecraft" in txt) or ("ماينكرافت" in txt)
                                if not mc_match and not any(kw in txt for kw in gaming_keywords):
                                    raise RuntimeError("FB content not related to Minecraft or Gaming; skipping")
                            elif effective_mode == "games":
                                if not any(kw in txt for kw in gaming_keywords):
                                    logger.warning(f"FB video lacks gaming keywords but proceeding (Games mode): {cand}")

                        # For single video (non-playlist) ensure it matches the expected YouTube handle
                        if info and info.get("_type") != "playlist" and expected_handle:
                            try:
                                ch_url = (info.get("uploader_url") or info.get("channel_url") or "").lower()
                                ch_name = (info.get("uploader") or info.get("channel") or "").lower()
                                ok_channel = False
                                if ch_url and f"/@{expected_handle}" in ch_url:
                                    ok_channel = True
                                elif ch_name and (expected_handle in ch_name.replace(" ", "")):
                                    ok_channel = True
                                if not ok_channel:
                                    raise RuntimeError("Different channel (single video handle mismatch)")
                            except Exception:
                                # If we cannot verify, proceed; later validations may catch
                                pass

                        # Apply duration and metadata-based music filter for single videos too
                        if info and info.get("_type") != "playlist" and apply_meta_filter:
                            reason = duration_filter(info) or _music_meta_reject(info, enabled=meta_music_filter_enabled)
                            if reason:
                                raise RuntimeError(f"Rejected (single video): {reason}")

                        # Check if this video has already been processed
                        if info and info.get("id") and cfg.SKIP_PROCESSED_VIDEOS and _is_video_processed(info["id"], state):
                            if url_or_feed and (url_or_feed.startswith("https://www.youtube.com/watch?v=") or url_or_feed.startswith("https://youtu.be/")):
                                logger.warning(f"Video {info['id']} already processed, proceeding due to explicit single video URL")
                            else:
                                logger.info(f"Video {info['id']} already processed in source, trying next candidate/source")
                                continue # Try next candidate in this source
                        
                        # Download the selected video using a clean downloader (no match_filter)
                        if info:
                            attempt_entry = info
                            tried_ids: set[str] = set()
                            for _attempt in range(5):
                                # Get the URL to download
                                download_url = attempt_entry.get("webpage_url") or attempt_entry.get("url") or cand
                                # If we used extract_flat, YouTube entries may be ids; normalize to a full URL
                                try:
                                    if download_url and isinstance(download_url, str) and not download_url.startswith("http"):
                                        vid_guess = attempt_entry.get("id") or attempt_entry.get("video_id") or download_url
                                        if vid_guess and isinstance(vid_guess, str):
                                            download_url = f"https://www.youtube.com/watch?v={vid_guess}"
                                except Exception:
                                    pass
                                youtube_url = attempt_entry.get("webpage_url") or cand  # Store the actual YouTube URL
                                ydl_dl_opts = dict(ydl_opts)
                                ydl_dl_opts.pop("match_filter", None)
                                # For safety, ensure single video download
                                ydl_dl_opts["noplaylist"] = True
                                ydl_dl_opts.pop("extract_flat", None)
                                # Apply cookies/headers per-site for the actual download
                                if ("facebook.com" in download_url) or ("fb.watch" in download_url) or ("mbasic.facebook.com" in download_url) or ("m.facebook.com" in download_url):
                                    if facebook_headers:
                                        ydl_dl_opts["http_headers"] = facebook_headers
                                    if fb_cookies_path:
                                        ydl_dl_opts["cookiefile"] = fb_cookies_path
                                else:
                                    ydl_dl_opts.pop("http_headers", None)
                                    if ("youtube.com" in download_url) or ("youtu.be" in download_url):
                                        if yt_cookies_path:
                                            ydl_dl_opts["cookiefile"] = yt_cookies_path
                                    else:
                                        ydl_dl_opts.pop("cookiefile", None)

                                try:
                                    # Use retry-enabled download for better error handling
                                    is_youtube_url = ("youtube.com" in download_url) or ("youtu.be" in download_url)
                                    if is_youtube_url:
                                        # YouTube: use retry logic with player client rotation
                                        download_info = _download_with_retry(ydl_dl_opts, download_url, max_retries=3)
                                        info = download_info
                                    else:
                                        # Non-YouTube: standard download
                                        with yt_dlp.YoutubeDL(ydl_dl_opts) as ydl_dl:
                                            download_info = ydl_dl.extract_info(download_url, download=True)
                                            info = download_info

                                    dur = None
                                    try:
                                        dur = info.get("duration") if isinstance(info, dict) else None
                                    except Exception:
                                        dur = None
                                    # Reject if duration missing or exceeds max_duration
                                    try:
                                        too_long = False
                                        unknown = False
                                        if max_duration:
                                            if dur is None:
                                                unknown = True
                                            else:
                                                too_long = float(dur) > float(max_duration)
                                        if unknown or too_long:
                                            vid_too_long = None
                                            try:
                                                vid_too_long = info.get("id") or attempt_entry.get("id")
                                            except Exception:
                                                vid_too_long = None

                                            _cleanup_temp_by_video_id(temp_dir, str(vid_too_long) if vid_too_long else None)

                                            try:
                                                st_dl = state.setdefault("downloader", {})
                                                rs = st_dl.setdefault("recent_skipped", [])
                                                if vid_too_long and str(vid_too_long) not in rs:
                                                    rs.append(str(vid_too_long))
                                                    st_dl["recent_skipped"] = rs[-200:]
                                                save_state(state, load_config())
                                            except Exception:
                                                pass

                                            if selected_pool:
                                                tried_ids.add(str(vid_too_long) if vid_too_long else "")
                                                next_entry = None
                                                for ent in selected_pool:
                                                    try:
                                                        vid_ent = ent.get("id") or ent.get("video_id")
                                                        if vid_ent and str(vid_ent) not in tried_ids:
                                                            next_entry = ent
                                                            break
                                                    except Exception:
                                                        continue
                                                if next_entry is not None:
                                                    attempt_entry = next_entry
                                                    continue

                                            if unknown:
                                                raise RuntimeError("Video duration unknown; skipping non-Shorts candidate")
                                            else:
                                                raise RuntimeError(f"Video too long for Shorts ({dur}s > {max_duration}s)")
                                    except Exception:
                                        pass
                                    break
                                except Exception as e:
                                    msg = str(e).lower()
                                    is_youtube = ("youtube.com" in download_url) or ("youtu.be" in download_url)
                                    retryable = is_youtube and (
                                        ("only images" in msg)
                                        or ("requested format is not available" in msg)
                                        or ("challenge solving" in msg)
                                        or ("ejs" in msg)
                                        or ("sabr" in msg)
                                    )

                                    vid_fail = None
                                    try:
                                        vid_fail = (
                                            attempt_entry.get("id")
                                            or attempt_entry.get("video_id")
                                            or (attempt_entry.get("webpage_url") or attempt_entry.get("url") or "").split("/shorts/")[-1].split("?")[0].split("&")[0]
                                        )
                                    except Exception:
                                        vid_fail = attempt_entry.get("id")
                                    if vid_fail:
                                        tried_ids.add(str(vid_fail))

                                    if retryable and selected_pool:
                                        try:
                                            st_dl = state.setdefault("downloader", {})
                                            rs = st_dl.setdefault("recent_skipped", [])
                                            if vid_fail and vid_fail not in rs:
                                                rs.append(vid_fail)
                                                st_dl["recent_skipped"] = rs[-200:]
                                                save_state(state, load_config())
                                        except Exception:
                                            pass

                                        next_entry = None
                                        for ent in selected_pool:
                                            try:
                                                vid_ent = ent.get("id") or ent.get("video_id")
                                                if vid_ent and str(vid_ent) not in tried_ids:
                                                    next_entry = ent
                                                    break
                                            except Exception:
                                                continue
                                        if next_entry is not None:
                                            attempt_entry = next_entry
                                            continue

                                    raise

                            # Log chosen format and max available height (diagnostic)
                            try:
                                fmts = info.get("formats") or []
                                max_av_h = 0
                                for f in fmts:
                                    try:
                                        h = int(f.get("height") or 0)
                                        if h > max_av_h:
                                            max_av_h = h
                                    except Exception:
                                        continue
                                logger.info(
                                    "yt-dlp selected format_id=%s %sx%s ext=%s vcodec=%s acodec=%s max_available_height=%s",
                                    info.get("format_id"),
                                    info.get("width"),
                                    info.get("height"),
                                    info.get("ext"),
                                    info.get("vcodec"),
                                    info.get("acodec"),
                                    max_av_h,
                                )
                            except Exception:
                                pass
                        else:
                            youtube_url = cand  # Fallback to candidate URL

                        # Resolve expected output path robustly
                        path: Optional[str] = None
                        # First try direct filepath from info (newer yt-dlp)
                        if info:
                            # requested_downloads may contain concrete filepaths
                            reqs = info.get("requested_downloads") or []
                            # Prefer downloads that have a video stream (avoid picking audio-only files)
                            try:
                                from .ffmpeg_utils import get_video_stream_summary
                            except Exception:
                                get_video_stream_summary = None
                            best_video_cand = None
                            for r in reqs:
                                f_path_cand = r.get("filepath") or r.get("_filename") or r.get("filename")
                                if not (f_path_cand and os.path.exists(f_path_cand)):
                                    continue
                                if get_video_stream_summary is None:
                                    best_video_cand = f_path_cand
                                    break
                                try:
                                    s_v = get_video_stream_summary(f_path_cand) or {}
                                    w_v = int(s_v.get("width") or 0)
                                    h_v = int(s_v.get("height") or 0)
                                    if w_v > 0 and h_v > 0:
                                        best_video_cand = f_path_cand
                                        break
                                except Exception:
                                    continue
                            if best_video_cand:
                                path = best_video_cand
                            # Fallback to _filename on root info
                            if not path:
                                f_path_cand = info.get("_filename") or info.get("filename")
                                if f_path_cand and os.path.exists(f_path_cand):
                                    path = f_path_cand
                        # Final fallbacks by id/ext and scanning temp_dir
                        if not path and info:
                            vid0 = info.get("id")
                            if vid0:
                                # Try common extensions
                                for ext_try in [
                                    (info.get("ext") if info else None) or "mp4",
                                    "mkv","webm","mov","m4v","mp3","m4a","wav"
                                ]:
                                    f_path_cand = os.path.join(temp_dir, f"{vid0}.{ext_try}")
                                    if os.path.exists(f_path_cand):
                                        path = f_path_cand
                                        break
                                # As a last resort, scan for any file starting with id (e.g., id.f299.mp4)
                                if not path:
                                    try:
                                        possible_paths = [
                                            os.path.join(temp_dir, fn)
                                            for fn in os.listdir(temp_dir)
                                            if fn.startswith(f"{vid0}.")
                                        ]
                                        if possible_paths:
                                            # pick the most recently modified
                                            possible_paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                                            path = possible_paths[0]
                                    except Exception:
                                        pass

                        if not path:
                            # Likely skipped or failed for this candidate; try next
                            raise RuntimeError("Download skipped or failed; no new file produced")

                        # Final safety: ensure the resolved file has a video stream (not audio-only)
                        try:
                            from .ffmpeg_utils import get_video_stream_summary
                            s_v = get_video_stream_summary(path) or {}
                            w_v = int(s_v.get("width") or 0)
                            h_v = int(s_v.get("height") or 0)
                            if w_v <= 0 or h_v <= 0:
                                # Try to find another file for same id that DOES have video
                                vid0 = None
                                try:
                                    vid0 = info.get("id") if info else None
                                except Exception:
                                    vid0 = None
                                if vid0:
                                    try:
                                        alts = [
                                            os.path.join(temp_dir, fn)
                                            for fn in os.listdir(temp_dir)
                                            if fn.startswith(f"{vid0}.")
                                        ]
                                        alts.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                                        for alt in alts:
                                            try:
                                                ss_v = get_video_stream_summary(alt) or {}
                                                ww_v = int(ss_v.get("width") or 0)
                                                hh_v = int(ss_v.get("height") or 0)
                                                if ww_v > 0 and hh_v > 0:
                                                    path = alt
                                                    break
                                            except Exception:
                                                continue
                                    except Exception:
                                        pass
                                # Re-check
                                s2_v = get_video_stream_summary(path) or {}
                                w2_v = int(s2_v.get("width") or 0)
                                h2_v = int(s2_v.get("height") or 0)
                                if w2_v <= 0 or h2_v <= 0:
                                    raise RuntimeError("Downloaded file has no video stream")
                        except Exception as e:
                            raise

                        try:
                            if max_duration:
                                from .ffmpeg_utils import get_file_info
                                finfo = get_file_info(path) or {}
                                fmt_v = (finfo.get("format") or {}) if isinstance(finfo, dict) else {}
                                dur_s_v = None
                                try:
                                    dur_s_v = float(fmt_v.get("duration")) if fmt_v.get("duration") is not None else None
                                except Exception:
                                    dur_s_v = None
                                if dur_s_v is not None and dur_s_v > float(max_duration):
                                    vid0 = None
                                    try:
                                        vid0 = info.get("id") if info else None
                                    except Exception:
                                        vid0 = None
                                    _cleanup_temp_by_video_id(temp_dir, str(vid0) if vid0 else None)
                                    try:
                                        os.remove(path)
                                    except Exception:
                                        pass
                                    raise RuntimeError(f"Downloaded video is too long for Shorts ({dur_s_v}s > {max_duration}s)")
                        except Exception:
                            raise
                        
                        # Mark video as processed using the filename (robust against playlist dicts)
                        vid_id = None
                        # Update rotation index in state if successful and in rotation mode
                        if not url_or_feed:
                            try:
                                all_ch = _read_channels(channel_list_path, target_mode=target_mode)
                                if selected_url in all_ch:
                                    state.setdefault("downloader", {})["last_channel_index"] = all_ch.index(selected_url)
                                    save_state(state, cfg)
                            except Exception:
                                pass

                        return DownloadResult(
                            input_path=path,
                            title=info.get("title") if info else None,
                            id=info.get("id") if info else None,
                            source_url=youtube_url if info else cand,
                        )
                    
                except Exception as e:
                    last_cand_error = e
                    logger.warning(f"Candidate failed: {cand} -> {e}")
                    continue

        except Exception as e:
            last_error = e
            logger.warning(f"Source failed or exhausted: {current_source} -> {e}")
            continue

    # =========================================================================
    # FALLBACK: Generic Search if all specific sources failed or are exhausted
    # =========================================================================
    if not url_or_feed:
        logger.info("All specific sources exhausted. Triggering broad generic search fallback...")
        try:
            # Determine search query based on content mode
            global_mode = (state.get("content_mode") or os.getenv("CONTENT_MODE") or "").strip().lower()
            query_term = global_mode if global_mode else "minecraft"
            # Add "shorts" to ensure we get short content
            search_query = f"ytsearchdate100:{query_term} shorts"
            
            logger.info(f"Generic Search Query: {search_query}")
            
            # Re-run information extraction for the search query
            with yt_dlp.YoutubeDL(ydl_opts) as ydl_gen:
                info_gen = ydl_gen.extract_info(search_query, download=False)
                if info_gen and info_gen.get("_type") == "playlist":
                    entries_gen = info_gen.get("entries") or []
                    for entry in entries_gen:
                        if not entry: continue
                        vid_gen = entry.get("id")
                        if vid_gen and cfg.SKIP_PROCESSED_VIDEOS and _is_video_processed(vid_gen, state):
                            continue
                        
                        # We found a potential video!
                        try:
                            logger.info(f"Generic search found candidate: {vid_gen}. downloading...")
                            download_url = f"https://www.youtube.com/watch?v={vid_gen}"
                            
                            # Standard download attempt for this search result
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl_dl:
                                d_info = ydl_dl.extract_info(download_url, download=True)
                                if d_info:
                                    # Resolve path
                                    reqs = d_info.get("requested_downloads") or []
                                    final_path = None
                                    for r in reqs:
                                        p = r.get("filepath") or r.get("_filename")
                                        if p and os.path.exists(p):
                                            final_path = p
                                            break
                                    if not final_path:
                                        final_path = d_info.get("_filename")
                                    
                                    if final_path and os.path.exists(final_path):
                                        _mark_video_as_processed(vid_gen, state, channel_url=download_url)
                                        return DownloadResult(
                                            input_path=final_path,
                                            title=d_info.get("title"),
                                            id=vid_gen,
                                            source_url=download_url
                                        )
                        except Exception as e_gen:
                            logger.warning(f"Generic search entry failed: {vid_gen} -> {e_gen}")
                            continue

        except Exception as e_final:
            logger.error(f"Generic search fallback failed: {e_final}")

    # If all candidates failed
    error_msg = str(last_error) if last_error else "No valid sources or candidates available"
    logger.error(f"Failed to find or download any suitable video: {error_msg}")
    raise RuntimeError(f"Failed to find or download any suitable video: {error_msg}")
