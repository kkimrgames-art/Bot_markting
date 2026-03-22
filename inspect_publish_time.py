
import os
import sys
from datetime import datetime, timezone

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from agent.supabase_client import load_dotenv as load_supabase_dotenv
from agent.auto_mod_fetcher import AutoModFetcher, AutoModDB, get_instance_id

def inspect_schedules():
    load_supabase_dotenv()
    instance_id = get_instance_id()
    fetcher = AutoModFetcher(instance_id)
    db = AutoModDB(instance_id)
    
    schedules = db.get_all_schedules()
    print(f"Total schedules: {len(schedules)}")
    for s in schedules:
        print("-" * 50)
        channel_id = s.get("channel_id")
        content_type = s.get("content_type", "minecraft_mods")
        print(f"Enabled: {s.get('enabled')}")
        print(f"Channel ID: {channel_id}")
        print(f"Content Type: {content_type}")
        print(f"Next Publish At: {s.get('next_publish_at')}")
        print(f"Publish Hours: {s.get('publish_hours')}")
        print(f"Daily Limit: {s.get('daily_limit')}")
        
        # Test `_is_publish_time`
        now = datetime.now(timezone.utc)
        print(f"Current UTC hour: {now.hour}")
        
        is_time = fetcher._is_publish_time(s)
        print(f"_is_publish_time() = {is_time}")

        # Test daily limit
        reached_limit = fetcher._reached_daily_limit(s)
        total_today = db.count_published_today(channel_id, content_type)
        print(f"Total published today: {total_today}")
        print(f"_reached_daily_limit() = {reached_limit}")

        # Add to JobQueue mock test
        from agent.job_queue import JobQueue
        queue = JobQueue()
        print(f"is_agent_busy_or_queued(): {queue.is_agent_busy_or_queued(channel_id)}")

        # Check pending jobs
        print(f"Total pending jobs in queue: {queue.get_pending_count()}")

if __name__ == "__main__":
    inspect_schedules()
