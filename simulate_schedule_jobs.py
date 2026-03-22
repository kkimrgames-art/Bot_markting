
import asyncio
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from agent.supabase_client import load_dotenv as load_supabase_dotenv
from agent.auto_mod_fetcher import AutoModFetcher, get_instance_id
from agent.job_queue import JobQueue

async def main():
    load_supabase_dotenv()
    instance_id = get_instance_id()
    fetcher = AutoModFetcher(instance_id)
    
    print(f"Instantiated JobQueue: {JobQueue()}")
    
    async def _notify(msg):
        print(f"NOTIFY: {msg}")
        
    print("Running fetcher.schedule_jobs()...")
    result = await fetcher.schedule_jobs(notify_func=_notify)
    print("Result of schedule_jobs:", result)

if __name__ == "__main__":
    asyncio.run(main())
