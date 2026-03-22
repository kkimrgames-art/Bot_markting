import asyncio
import os
import sys
import json
from datetime import datetime, timezone

# Add src to path
sys.path.append(os.getcwd())

from src.agent.auto_mod_fetcher import AutoModDB, get_instance_id
from src.agent.supabase_client import USE_SUPABASE, is_online

async def diagnose():
    print(f"--- Diagnostic Report ({datetime.now(timezone.utc).isoformat()}) ---")
    
    instance_id = get_instance_id()
    print(f"Instance ID: {instance_id}")
    
    print(f"USE_SUPABASE (env): {os.environ.get('USE_SUPABASE')}")
    print(f"USE_SUPABASE (code): {USE_SUPABASE}")
    print(f"Is Online: {is_online()}")
    
    db = AutoModDB(instance_id)
    
    print("\nFetching Schedules...")
    schedules = db.get_all_schedules()
    print(f"Total Schedules Found: {len(schedules)}")
    
    for idx, sch in enumerate(schedules):
        print(f"\nSchedule #{idx+1}:")
        print(f"  Channel ID: {sch.get('channel_id')}")
        print(f"  Content Type: {sch.get('content_type')}")
        print(f"  Enabled: {sch.get('enabled')}")
        print(f"  Next Publish At: {sch.get('next_publish_at')}")
        print(f"  Instance ID: {sch.get('instance_id')}")
        
    print("\nFetching Sources...")
    # get_sources requires channel_id and content_type
    if schedules:
        s = schedules[0]
        sources = db.get_sources(s['channel_id'], s['content_type'])
        print(f"Sources for {s['channel_id'][:10]}...: {len(sources)}")
        for src in sources:
            print(f"  - {src.get('source_name')} ({src.get('source_url')}) [Enabled: {src.get('enabled')}]")
    else:
        print("No schedules found, skipping source check.")

if __name__ == "__main__":
    asyncio.run(diagnose())
