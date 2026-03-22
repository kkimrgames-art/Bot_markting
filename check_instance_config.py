
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from agent.supabase_client import load_dotenv as load_supabase_dotenv
from agent.auto_mod_fetcher import AutoModDB, get_instance_id

def check_config():
    load_supabase_dotenv()
    instance_id = get_instance_id()
    print(f"Current Instance ID: {instance_id}")
    
    db = AutoModDB(instance_id)
    config = db.get_config(use_cache=False)
    print("\nInstance Config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    
    if not config.get("auto_fetch_enabled"):
        print("\nWARNING: auto_fetch_enabled is FALSE for this instance!")
    else:
        print("\nSUCCESS: auto_fetch_enabled is TRUE for this instance.")

if __name__ == "__main__":
    check_config()
