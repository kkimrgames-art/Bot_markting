
import os
import sys
import asyncio
import unittest
from unittest.mock import MagicMock, patch

# Add src to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from src.agent.auto_mod_fetcher import AutoModFetcher

class TestYouTubeFixes(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        self.fetcher = AutoModFetcher(instance_id="test_instance")
        # Ensure we start fresh
        os.environ.pop("YTDLP_SKIP_IMPERSONATE", None)

    @patch("yt_dlp.YoutubeDL")
    async def test_impersonate_retry_logic(self, mock_ydl):
        """Test that we catch the impersonate error and retry without it"""
        
        # Setup mock to fail on first call and succeed on second
        error_msg = 'Impersonate target "chrome" is not available. Use --list-impersonate-targets to see available targets.'
        
        # First call raises Exception, second call returns data
        instance = mock_ydl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            Exception(error_msg),
            {"entries": [{"id": "vid1", "title": "Test Video", "duration": 100}]}
        ]
        
        # Run fetch
        videos = await self.fetcher.fetch_videos_from_source("https://youtube.com/test", platform="youtube")
        
        # Assertions
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["id"], "vid1")
        self.assertEqual(os.environ.get("YTDLP_SKIP_IMPERSONATE"), "1")
        
        # Verify it was called twice
        # Once with impersonate, once without
        calls = instance.extract_info.call_count
        self.assertEqual(calls, 2)
        print("\n✅ Impersonate retry logic verified.")

    @patch("yt_dlp.YoutubeDL")
    async def test_no_skip_logic(self, mock_ydl):
        """Test that videos are NOT skipped regardless of duration"""
        
        instance = mock_ydl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "entries": [
                {"id": "short", "title": "Short Video", "duration": 30},
                {"id": "long", "title": "Long Video", "duration": 3600}, # 1 hour
                {"id": "v-long", "title": "Very Long Video", "duration": 10000}
            ]
        }
        
        # Run fetch for shorts platform
        videos = await self.fetcher.fetch_videos_from_source("https://youtube.com/shorts", platform="youtube_shorts")
        
        # Verify all videos are returned (previously 'long' and 'v-long' would be skipped)
        self.assertEqual(len(videos), 3)
        print(f"✅ No-skip logic verified. Found {len(videos)} videos (including long ones in shorts channel).")

    @patch("src.agent.auto_mod_fetcher._build_yt_opts")
    @patch("src.utils.resilient_fs.ResilientFS.exists")
    @patch("yt_dlp.YoutubeDL")
    def test_download_sync_attempt_builds_ydl_opts_before_download(self, mock_ydl, mock_exists, mock_build_yt_opts):
        """Regression test: download path must build ydl_opts before using YoutubeDL."""
        os.environ["YTDLP_SKIP_IMPERSONATE"] = "1"
        mock_build_yt_opts.side_effect = lambda extra: {**(extra or {}), "impersonate": "chrome"}
        mock_exists.side_effect = lambda path: str(path).endswith(".mp4")

        instance = mock_ydl.return_value.__enter__.return_value
        instance.extract_info.return_value = {"id": "vid1", "title": "Test Video", "ext": "webm"}
        instance.prepare_filename.return_value = os.path.join(".temp", "vid1.webm")

        result = self.fetcher._download_sync_attempt(
            "https://www.youtube.com/watch?v=vid1",
            os.path.join(".temp", "downloads_test"),
            max_duration=60,
        )

        self.assertEqual(result, os.path.join(".temp", "vid1.mp4"))
        self.assertTrue(mock_build_yt_opts.called)
        self.assertEqual(mock_ydl.call_count, 1)
        used_opts = mock_ydl.call_args[0][0]
        self.assertEqual(used_opts.get("outtmpl"), os.path.join(".temp", "downloads_test", "%(id)s.%(ext)s"))
        self.assertNotIn("impersonate", used_opts)
        print("✅ Download ydl_opts regression verified.")

if __name__ == "__main__":
    unittest.main()
