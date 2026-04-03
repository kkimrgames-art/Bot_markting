
import os
import sys
import asyncio
import unittest
import shutil
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add src to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from src.agent.auto_mod_fetcher import AutoModFetcher
from src.agent.mod_video_processor import ModVideoProcessor

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
        mock_build_yt_opts.side_effect = lambda extra, **kwargs: {**(extra or {}), "impersonate": "chrome"}
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
        self.assertTrue(used_opts.get("outtmpl", "").endswith(os.path.join(".temp", "downloads_test", "%(id)s.%(ext)s")))
        self.assertNotIn("impersonate", used_opts)
        print("✅ Download ydl_opts regression verified.")


class TestModVideoProcessor(unittest.TestCase):

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in [
                "LOW_RESOURCE_MODE",
                "AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL",
                "SHORTS_TARGET_WIDTH",
                "SHORTS_TARGET_HEIGHT",
            ]
        }
        self.work_dir = Path(tempfile.mkdtemp(prefix="mod_video_processor_test_"))

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_convert_to_shorts_retries_with_lighter_fallback_after_timeout(self):
        input_path = Path(project_root) / ".temp" / "smoke_tests" / "landscape_input.mp4"
        self.assertTrue(input_path.exists(), f"Missing smoke input: {input_path}")

        os.environ["LOW_RESOURCE_MODE"] = "1"
        os.environ["AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL"] = "0"
        os.environ["SHORTS_TARGET_WIDTH"] = "720"
        os.environ["SHORTS_TARGET_HEIGHT"] = "1280"

        processor = ModVideoProcessor(temp_dir=str(self.work_dir / "temp"))
        output_path = self.work_dir / "retry_case.mp4"
        calls = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, **kwargs):
            calls.append((list(cmd), timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, stderr=b"primary timeout")
            tmp_output = Path(cmd[-1])
            tmp_output.parent.mkdir(parents=True, exist_ok=True)
            tmp_output.write_bytes(b"fake-mp4-data")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch.object(processor, "_get_video_duration", return_value=60.0), \
             patch.object(processor, "_get_video_fps", return_value=30.0), \
             patch.object(processor, "_has_audio", return_value=True), \
             patch.object(processor, "_validate_video_file", return_value=None), \
             patch("src.agent.mod_video_processor.ffmpeg_bin", return_value="ffmpeg"), \
             patch("src.agent.mod_video_processor.subprocess.run", side_effect=fake_run):
            processor._convert_to_shorts(
                input_path=str(input_path),
                output_path=str(output_path),
                orig_width=720,
                orig_height=1010,
                shorts_format="crop",
                hflip=True,
            )

        self.assertTrue(output_path.exists(), output_path)
        self.assertGreaterEqual(len(calls), 2)
        second_cmd = calls[1][0]
        vf_value = second_cmd[second_cmd.index("-vf") + 1]
        self.assertTrue("scale=540:960" in vf_value or "scale=360:640" in vf_value, vf_value)
        self.assertNotIn("-af", second_cmd)
        print("✅ Shorts timeout fallback regression verified.")

    def test_process_mod_video_smoke_low_resource_hflip(self):
        input_path = Path(project_root) / ".temp" / "smoke_tests" / "landscape_input.mp4"
        self.assertTrue(input_path.exists(), f"Missing smoke input: {input_path}")

        os.environ["LOW_RESOURCE_MODE"] = "1"
        os.environ["AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL"] = "0"
        os.environ["SHORTS_TARGET_WIDTH"] = "720"
        os.environ["SHORTS_TARGET_HEIGHT"] = "1280"

        processor = ModVideoProcessor(temp_dir=str(self.work_dir / "temp_smoke"))
        output_dir = self.work_dir / "out"

        output_path, info = processor.process_mod_video(
            input_video=str(input_path),
            output_dir=str(output_dir),
            video_id="smoke_landscape",
            trim_start=0.2,
            trim_end=0.2,
            add_cta=False,
            convert_to_shorts=True,
            shorts_format="crop",
            hflip=True,
        )

        self.assertTrue(os.path.exists(output_path), output_path)
        self.assertGreater(os.path.getsize(output_path), 0)
        self.assertEqual(info.get("final_size"), "720x1280")
        print("✅ Low-resource smoke processing verified.")

if __name__ == "__main__":
    unittest.main()
