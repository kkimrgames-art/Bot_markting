
import httpx
import json

url = "https://umpaypezrlnvxpghrawq.supabase.co/rest/v1/auto_mod_processed?limit=1"
key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVtcGF5cGV6cmxudnhwZ2hyYXdxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njg1OTAwNDAsImV4cCI6MjA4NDE2NjA0MH0.xlplRfA0xCrTubDSzSgH2heYFYwpbnYM5C_N-L6WBBQ"

headers = {
    "apikey": key,
    "Authorization": f"Bearer {key}"
}

try:
    response = httpx.get(url, headers=headers, timeout=10)
    if response.status_code == 200:
        data = response.json()
        if data:
            print(json.dumps(data[0], indent=2))
        else:
            print("No data found in table.")
    else:
        print(f"Error: {response.status_code} - {response.text}")
except Exception as e:
    print(f"Exception: {e}")
