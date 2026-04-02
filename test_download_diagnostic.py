"""
تشخيص مشكلة تنزيل الفيديوهات من فيسبوك
يختبر yt-dlp مباشرة لمعرفة أين يتم حفظ الملفات فعلياً
"""
import os
import sys
import tempfile
import json

# Add project root
project_root = os.path.abspath(os.path.dirname(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def test_ytdlp_output_path():
    """Test where yt-dlp actually writes files vs where we expect them"""
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp not installed")
        return

    test_url = "https://www.facebook.com/watch?v=2936476116713963"
    
    # Create a temp directory similar to what the bot uses
    test_dir = os.path.join(project_root, ".temp", "test_download_diagnostic")
    os.makedirs(test_dir, exist_ok=True)
    
    output_template = os.path.join(test_dir, "%(id)s.%(ext)s")
    
    print(f"📁 Test directory: {test_dir}")
    print(f"📝 Output template: {output_template}")
    print(f"🔗 Test URL: {test_url}")
    print()
    
    # Track what yt-dlp does
    progress_info = {"filenames": [], "finished_files": []}
    
    def progress_hook(d):
        status = d.get("status", "")
        filename = d.get("filename", "")
        if filename and filename not in progress_info["filenames"]:
            progress_info["filenames"].append(filename)
            print(f"  📦 Progress hook filename: {filename}")
        if status == "finished":
            progress_info["finished_files"].append(filename)
            print(f"  ✅ Finished: {filename} ({d.get('downloaded_bytes', '?')} bytes)")
    
    # Facebook-optimized format (same as the bot uses)
    fmt = (
        "best[ext=mp4][height<=1920]/"
        "best[ext=mp4]/"
        "hd/sd/"
        "best/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo+bestaudio/"
        "b"
    )
    
    opts = {
        "quiet": False,
        "no_warnings": False,
        "format": fmt,
        "outtmpl": output_template,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "keepvideo": True,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [progress_hook],
        "no_check_certificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.facebook.com",
            "Referer": "https://www.facebook.com/",
        },
    }
    
    # Check if ffmpeg is available
    ffmpeg_path = None
    try:
        from src.agent.ffmpeg_utils import ffmpeg_bin
        ffmpeg_path = ffmpeg_bin()
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path
            print(f"🔧 FFmpeg found: {ffmpeg_path}")
        else:
            print("⚠️ FFmpeg not found")
            opts.pop("merge_output_format", None)
    except Exception as e:
        print(f"⚠️ FFmpeg check failed: {e}")
        opts.pop("merge_output_format", None)
    
    print()
    print("=" * 60)
    print("🔄 Starting yt-dlp extract_info (download=True)...")
    print("=" * 60)
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(test_url, download=True)
            
            if info:
                print()
                print("=" * 60)
                print("📊 EXTRACT_INFO RESULTS:")
                print("=" * 60)
                print(f"  id: {info.get('id')}")
                print(f"  title: {info.get('title', '?')[:60]}")
                print(f"  format: {info.get('format', '?')}")
                print(f"  format_id: {info.get('format_id', '?')}")
                print(f"  ext: {info.get('ext', '?')}")
                print(f"  vcodec: {info.get('vcodec', '?')}")
                print(f"  acodec: {info.get('acodec', '?')}")
                print(f"  height: {info.get('height', '?')}")
                print(f"  width: {info.get('width', '?')}")
                print(f"  duration: {info.get('duration', '?')}")
                
                # Check prepare_filename
                prepared_filename = ydl.prepare_filename(info)
                print(f"\n  prepare_filename: {prepared_filename}")
                print(f"  prepare_filename exists: {os.path.exists(prepared_filename)}")
                
                # Check with .mp4 extension
                base = os.path.splitext(prepared_filename)[0]
                for ext in [".mp4", ".mkv", ".webm", ".m4a"]:
                    candidate = base + ext
                    exists = os.path.exists(candidate)
                    if exists:
                        size = os.path.getsize(candidate)
                        print(f"  {ext} candidate: {candidate} ✅ EXISTS ({size} bytes)")
                    else:
                        print(f"  {ext} candidate: {candidate} ❌ NOT FOUND")
                
                # Check requested_downloads
                requested = info.get("requested_downloads") or []
                if requested:
                    print(f"\n  requested_downloads ({len(requested)} items):")
                    for idx, rd in enumerate(requested):
                        rd_filepath = rd.get("filepath") or rd.get("filename") or "?"
                        print(f"    [{idx}] filepath: {rd_filepath}")
                        print(f"         exists: {os.path.exists(rd_filepath) if rd_filepath != '?' else 'N/A'}")
                        print(f"         ext: {rd.get('ext', '?')}")
                
                print()
            else:
                print("❌ extract_info returned None")
    except Exception as e:
        print(f"❌ yt-dlp error: {e}")
    
    print()
    print("=" * 60)
    print("📁 ACTUAL DIRECTORY CONTENTS:")
    print("=" * 60)
    
    # List all files in test_dir
    if os.path.exists(test_dir):
        all_files = os.listdir(test_dir)
        print(f"  Directory: {test_dir}")
        print(f"  Files ({len(all_files)}):")
        for f in sorted(all_files):
            fpath = os.path.join(test_dir, f)
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath)
                print(f"    📄 {f} ({size:,} bytes)")
            else:
                print(f"    📁 {f}/")
    else:
        print(f"  ❌ Directory does not exist: {test_dir}")
    
    # Also check parent directories
    parent = os.path.dirname(test_dir)
    if os.path.exists(parent):
        print(f"\n  Parent directory: {parent}")
        parent_files = os.listdir(parent)
        print(f"  Files ({len(parent_files)}):")
        for f in sorted(parent_files):
            fpath = os.path.join(parent, f)
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath)
                print(f"    📄 {f} ({size:,} bytes)")
            else:
                print(f"    📁 {f}/")
    
    # Check progress hook data
    print()
    print("=" * 60)
    print("📊 PROGRESS HOOK DATA:")
    print("=" * 60)
    print(f"  All filenames seen: {progress_info['filenames']}")
    print(f"  Finished files: {progress_info['finished_files']}")
    
    # Check if any of the progress hook filenames exist
    for fn in progress_info["filenames"]:
        if fn and os.path.exists(fn):
            print(f"  ✅ Progress file exists: {fn} ({os.path.getsize(fn):,} bytes)")
        elif fn:
            print(f"  ❌ Progress file NOT found: {fn}")
            # Check directory of this file
            fn_dir = os.path.dirname(fn)
            if fn_dir and os.path.exists(fn_dir) and fn_dir != test_dir:
                print(f"     📁 Its directory ({fn_dir}) contents: {os.listdir(fn_dir)}")


def test_ytdlp_info_only():
    """Test extract_info without download to see what format is selected"""
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp not installed")
        return

    test_url = "https://www.facebook.com/watch?v=2936476116713963"
    
    print()
    print("=" * 60)
    print("🔍 INFO-ONLY TEST (no download):")
    print("=" * 60)
    
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_check_certificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
    }
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
            if info:
                print(f"  id: {info.get('id')}")
                print(f"  title: {info.get('title', '?')[:60]}")
                print(f"  ext: {info.get('ext', '?')}")
                print(f"  format: {info.get('format', '?')}")
                print(f"  format_id: {info.get('format_id', '?')}")
                
                # Check available formats
                formats = info.get("formats") or []
                print(f"\n  Available formats ({len(formats)}):")
                
                combined_formats = []
                dash_video = []
                dash_audio = []
                
                for f in formats:
                    fid = f.get("format_id", "?")
                    ext = f.get("ext", "?")
                    vcodec = f.get("vcodec", "none")
                    acodec = f.get("acodec", "none")
                    height = f.get("height", "?")
                    width = f.get("width", "?")
                    filesize = f.get("filesize") or f.get("filesize_approx") or 0
                    
                    has_video = vcodec and vcodec != "none"
                    has_audio = acodec and acodec != "none"
                    
                    if has_video and has_audio:
                        combined_formats.append(f)
                        print(f"    🎬 COMBINED: {fid} | {ext} | {width}x{height} | v={vcodec} a={acodec} | {filesize:,} bytes")
                    elif has_video:
                        dash_video.append(f)
                        print(f"    📹 VIDEO:    {fid} | {ext} | {width}x{height} | v={vcodec} | {filesize:,} bytes")
                    elif has_audio:
                        dash_audio.append(f)
                        print(f"    🔊 AUDIO:    {fid} | {ext} | a={acodec} | {filesize:,} bytes")
                
                print(f"\n  Summary: {len(combined_formats)} combined, {len(dash_video)} video-only, {len(dash_audio)} audio-only")
                
                if not combined_formats:
                    print("\n  ⚠️ NO COMBINED FORMATS AVAILABLE!")
                    print("  This means yt-dlp MUST merge video+audio using FFmpeg.")
                    print("  If FFmpeg merge fails silently, no output file will be created.")
            else:
                print("❌ extract_info returned None")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    print("🔬 Facebook Video Download Diagnostic")
    print("=" * 60)
    
    # First, check info only
    test_ytdlp_info_only()
    
    # Then test actual download
    test_ytdlp_output_path()
    
    print()
    print("=" * 60)
    print("🏁 Diagnostic complete!")
