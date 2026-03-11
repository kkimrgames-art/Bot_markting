import asyncio
from src.agent.auto_mod_fetcher import AutoModFetcher
import sys

async def main():
    try:
        instance_id = "local_8f229d9e"
        if len(sys.argv) > 1 and sys.argv[1].strip():
            instance_id = sys.argv[1].strip()
        f = AutoModFetcher(instance_id)

        if len(sys.argv) >= 3 and sys.argv[2].strip():
            url = sys.argv[2].strip()
            platform = sys.argv[3].strip() if len(sys.argv) >= 4 else "facebook_any"
            videos = await f.fetch_videos_from_source(url, items_range="1-10", platform=platform)
            print("Fetched:", len(videos))
            for v in (videos or [])[:5]:
                print("-", v.get("id"), (v.get("title") or "")[:60], (v.get("url") or "")[:80])
            return

        s = f.db.get_all_schedules()
        print('Schedules:', s)
        sources = sum([f.db.get_sources(c['channel_id'], c.get('content_type', 'minecraft_mods')) for c in s], [])
        print('Sources:', [(src.get('source_name'), src.get('platform')) for src in sources])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
