
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from agent.supabase_client import load_dotenv as load_supabase_dotenv
from agent.auto_mod_fetcher import AutoModFetcher, AutoModDB, get_instance_id

# Need these for raw review checks (they are global functions usually)
try:
    from bot.raw_review import is_raw_review_blocked, is_raw_review_skip_active
except ImportError:
    # If not found directly, try agent imports if defined there
    def is_raw_review_blocked(s, v): return False
    def is_raw_review_skip_active(s, v): return False

async def main():
    load_supabase_dotenv()
    instance_id = get_instance_id()
    fetcher = AutoModFetcher(instance_id)
    db = AutoModDB(instance_id)
    
    url = "https://youtube.com/@uniquepotato/shorts"
    channel_id = "ae639843-cca5-4c10-ae23-c9a6b4ba903b"
    source_id = f"{channel_id}:{url}" # Approximate source_id

    print(f"Fetching 50 videos from {url}...")
    batch_videos = await fetcher.fetch_videos_from_source(url, items_range="1-50", platform="youtube")
    print(f"Fetched {len(batch_videos)} videos.")

    if not batch_videos:
        print("No videos returned from yt-dlp. Is the channel URL correct?")
        return

    potential_videos = []
    published_count = 0
    locked_count = 0
    other_count = 0
    
    status_tally = {}

    for v in batch_videos:
        v_id = v.get("id", "")
        if not v_id:
            print(f"Video {v.get('title')} has no ID!")
            continue

        if is_raw_review_blocked(source_id, v_id):
            status_tally["raw_review_blocked"] = status_tally.get("raw_review_blocked", 0) + 1
            other_count += 1
            continue
        if is_raw_review_skip_active(source_id, v_id):
            status_tally["raw_review_skipped"] = status_tally.get("raw_review_skipped", 0) + 1
            other_count += 1
            continue

        status, updated_at = db.get_video_process_state(v_id, channel_id)
        
        status_tally[str(status)] = status_tally.get(str(status), 0) + 1
        
        if status == "published":
            published_count += 1
            continue
        if status == "processing":
            locked_count += 1
            continue
        if status:
            other_count += 1
            continue
            
        potential_videos.append(v)

    print("\n--- Summary ---")
    print(f"Potential Videos (NEW): {len(potential_videos)}")
    print(f"Published Count: {published_count}")
    print(f"Locked (Processing) Count: {locked_count}")
    print(f"Other Count (Failed/Skipped): {other_count}")
    print("\nStatus Details breakdown:")
    for stat, count in status_tally.items():
        print(f"  - '{stat}': {count}")
        
    if len(potential_videos) == 0:
        print("\nAll videos were skipped! This explains why it keeps digging.")

if __name__ == "__main__":
    asyncio.run(main())
