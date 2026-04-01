import sys
import os
import logging
import json

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from agent.auto_mod_fetcher import AutoModFetcher

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FB_FINAL_TEST")

def test_facebook_page_to_reels():
    fetcher = AutoModFetcher()
    
    # Test normalization: Page URL should become Page URL + /reels
    base_url = "https://www.facebook.com/Meta"
    platform = "facebook_reels"
    
    logger.info(f"Testing normalization for: {base_url} (Platform: {platform})")
    
    # We'll use the internal _normalize_facebook_source_url just to check
    from agent.auto_mod_fetcher import _normalize_facebook_source_url
    norm_url = _normalize_facebook_source_url(base_url, platform)
    logger.info(f"Normalized URL: {norm_url}")
    
    if norm_url == base_url + "/reels":
        logger.info("✅ Normalization successful!")
    else:
        logger.error(f"❌ Normalization failed. Got: {norm_url}")

    # Now let's try a REAL fetch with the original watch link but using the fetcher's logic
    watch_url = "https://web.facebook.com/watch/?v=1059484835814402"
    logger.info(f"Testing real fetch for: {watch_url}")
    videos = fetcher._fetch_sync(watch_url, items_range="1", platform=platform)
    
    if videos and len(videos) > 0:
        logger.info(f"✅ SUCCESS! Fetched: {videos[0]['title']}")
    else:
        logger.error("❌ Failed to fetch video.")

if __name__ == "__main__":
    test_facebook_page_to_reels()
