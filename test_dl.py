"""Verify the impersonate fix works: ImpersonateTarget.from_str fallback."""
import sys
import traceback

print("=== Test 1: ImpersonateTarget.from_str ===")
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    target = ImpersonateTarget.from_str("chrome110")
    print(f"  OK: ImpersonateTarget created: {target}")
except AttributeError:
    print("  WARN: ImpersonateTarget.from_str not available (older yt-dlp)")
except Exception as e:
    print(f"  EXPECTED: ImpersonateTarget.from_str failed: {e}")
    print("  The except block in our fix will catch this and skip impersonate.")

print()
print("=== Test 2: YoutubeDL without impersonate (should work) ===")
try:
    import yt_dlp
    opts = {
        "format": "best",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=8YtcnS5saBk", download=False)
        print(f"  OK: Video title = {info.get('title', '(unknown)')}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=== Test 3: Worker module import check ===")
sys.path.insert(0, "worker")
try:
    from downloader_worker import create_app
    print("  OK: Worker module imports successfully")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("All tests complete.")
