
import os
import sys
import json
import httpx

url = "https://umpaypezrlnvxpghrawq.supabase.co"
key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVtcGF5cGV6cmxudnhwZ2hyYXdxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njg1OTAwNDAsImV4cCI6MjA4NDE2NjA0MH0.xlplRfA0xCrTubDSzSgH2heYFYwpbnYM5C_N-L6WBBQ"

video_ids = ["Gm7mLd3PUd8", "Co1T1-JAkgs", "Eqw8d2kigIQ", "4AyOOLBE8yI", "Wvwt5uDKShk"]

def check_supabase():
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}"
    }
    
    print(f"Checking {len(video_ids)} video IDs in auto_mod_processed...")
    
    for vid in video_ids:
        rest_url = f"{url}/rest/v1/auto_mod_processed?source_video_id=eq.{vid}"
        response = httpx.get(rest_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if data:
                for entry in data:
                    print(f"Match: ID={vid}, Channel={entry.get('channel_id')}, Status={entry.get('status')}, Instance={entry.get('instance_id')}")
            else:
                print(f"No match: ID={vid} (New video!)")
        else:
            print(f"Error checking {vid}: {response.status_code} - {response.text}")

if __name__ == "__main__":
    check_supabase()
