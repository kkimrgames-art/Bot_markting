import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.agent import uploader as uploader_module
from src.agent.config import get_project_root, load_config
from src.agent.uploader import InvalidUploadVideoError, upload_video_with_token


class TestProjectPathAnchoring(unittest.TestCase):
    def test_load_config_anchors_relative_paths_to_project_root(self):
        root = get_project_root()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as outside_cwd:
            try:
                os.chdir(outside_cwd)
                with patch("src.agent.config.load_dotenv"), patch.dict(
                    os.environ,
                    {
                        "CHANNEL_LIST_PATH": "spec/custom_channels.txt",
                        "OUTPUT_DIR": "outputs_custom",
                        "TEMP_DIR": ".temp_custom",
                        "REACTIONS_DIR": "reactions_custom",
                        "BACKGROUND_DIR": "background_custom",
                        "TELEGRAM_DB_PATH": ".data_custom/tg_state.db",
                    },
                    clear=False,
                ):
                    cfg = load_config(force_reload=True)

                self.assertEqual(cfg.CHANNEL_LIST_PATH, os.path.join(root, "spec", "custom_channels.txt"))
                self.assertEqual(cfg.OUTPUT_DIR, os.path.join(root, "outputs_custom"))
                self.assertEqual(cfg.TEMP_DIR, os.path.join(root, ".temp_custom"))
                self.assertEqual(cfg.REACTIONS_DIR, os.path.join(root, "reactions_custom"))
                self.assertEqual(cfg.BACKGROUND_DIR, os.path.join(root, "background_custom"))
                self.assertEqual(cfg.TELEGRAM_DB_PATH, os.path.join(root, ".data_custom", "tg_state.db"))
            finally:
                os.chdir(original_cwd)
                load_config(force_reload=True)


class TestFinalUploadValidation(unittest.TestCase):
    def test_upload_video_with_token_rejects_zero_duration_artifact_before_youtube_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            video_path = os.path.join(tempdir, "broken.mp4")
            token_path = os.path.join(tempdir, "token.json")
            with open(video_path, "wb") as f:
                f.write(b"broken-but-non-empty")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write("{}")

            cfg = SimpleNamespace(TELEGRAM_DB_PATH=os.path.join(tempdir, "tg_state.db"))
            probe_result = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"format": {"duration": "0.0"}, "streams": [{"codec_type": "video"}]}),
                stderr="",
            )

            with patch.object(uploader_module, "ffprobe_bin", return_value="ffprobe"), \
                 patch.object(uploader_module.subprocess, "run", return_value=probe_result) as mock_run, \
                 patch.object(uploader_module, "build") as mock_build:
                with self.assertRaises(InvalidUploadVideoError) as ctx:
                    upload_video_with_token(cfg, token_path, video_path, "title", "desc", [])

            self.assertIn("مدة", str(ctx.exception))
            self.assertTrue(mock_run.called)
            self.assertFalse(mock_build.called)


if __name__ == "__main__":
    unittest.main()