import sys
import os
import logging
import json
import yt_dlp

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from agent.auto_mod_fetcher import _build_yt_opts

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FB_DEBUG")

def debug_facebook_fetch():
    fb_url = "https://web.facebook.com/watch/?v=1059484835814402"
    
    ydl_opts = _build_yt_opts({
        "extract_flat": True,
        "nocheckcertificate": True,
    })
    ydl_opts.pop("cookiefile", None)
    ydl_opts.pop("source_address", None) # REMOVE FORCED IPv4
    
    logger.info(f"YDL Opts (No source_address): {json.dumps({k: str(v) for k, v in ydl_opts.items() if k != 'impersonate'}, indent=2)}")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("Extracting info...")
            info = ydl.extract_info(fb_url, download=False)
            if info:
                logger.info(f"✅ SUCCESS!")
                logger.info(f"Video ID: {info.get('id')} | Title: {info.get('title')}")
    except Exception as e:
        logger.error(f"❌ Failed: {e}")

if __name__ == "__main__":
    debug_facebook_fetch()
