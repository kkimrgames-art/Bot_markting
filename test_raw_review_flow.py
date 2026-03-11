import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.agent.auto_mod_fetcher import merge_source_settings, normalize_source_settings
from src.bot import persistence
from src.bot.raw_review import (
    _run_approved_review_now,
    handle_raw_review_callback,
    request_raw_video_review,
)


class TestSourceRawReviewSettings(unittest.TestCase):
    def test_normalize_and_merge_keep_require_raw_review_flag(self):
        normalized = normalize_source_settings({"require_raw_review": "true"})
        self.assertTrue(normalized.get("require_raw_review"))

        merged = merge_source_settings(
            {"require_raw_review": False},
            {"require_raw_review": True, "extra_description": {"enabled": False}},
        )
        self.assertTrue(merged.get("require_raw_review"))


class TestRawReviewPersistence(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(
            TELEGRAM_DB_PATH=os.path.join(self.tempdir.name, "tg_state.db"),
            RUN_DAILY_AT="10:00",
            RUN_ONLY_ON_WIFI=False,
            RUN_ONLY_WHILE_CHARGING=False,
            AUDIO_MODE="light",
            TELEGRAM_ALLOWED_USER_IDS=[12345],
        )
        self.detect_patch = patch("src.bot.persistence._detect_and_add_publish_channels", lambda state, cfg: None)
        self.sqlite_default_patch = patch("src.agent.sqlite_storage._DEFAULT_DB_PATH", Path(self.tempdir.name) / "default_state.db")
        self.detect_patch.start()
        self.sqlite_default_patch.start()
        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None

    def tearDown(self):
        self.detect_patch.stop()
        self.sqlite_default_patch.stop()
        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None
        self.tempdir.cleanup()

    def test_pending_approve_skip_block_roundtrip(self):
        persistence.set_pending_raw_review("src-1", {"token": "tok-a", "video_id": "vid-1"}, cfg=self.cfg)
        self.assertIsNotNone(persistence.get_pending_raw_review("src-1", cfg=self.cfg))
        self.assertTrue(persistence.has_pending_raw_reviews(cfg=self.cfg))

        approved, source_id = persistence.approve_pending_raw_review("tok-a", decided_by=12345, cfg=self.cfg)
        self.assertEqual(source_id, "src-1")
        self.assertEqual(approved.get("decision"), "approved")
        self.assertTrue(persistence.is_raw_review_approved("src-1", "vid-1", cfg=self.cfg))
        self.assertFalse(persistence.has_pending_raw_reviews(cfg=self.cfg))

        persistence.set_pending_raw_review("src-1", {"token": "tok-b", "video_id": "vid-2"}, cfg=self.cfg)
        skipped, _ = persistence.skip_pending_raw_review("tok-b", decided_by=12345, cfg=self.cfg, skip_cooldown_seconds=3600)
        self.assertEqual(skipped.get("decision"), "skipped")
        self.assertTrue(persistence.is_raw_review_skip_active("src-1", "vid-2", cfg=self.cfg))

        persistence.set_pending_raw_review("src-1", {"token": "tok-c", "video_id": "vid-3"}, cfg=self.cfg)
        blocked, _ = persistence.block_pending_raw_review("tok-c", decided_by=12345, cfg=self.cfg)
        self.assertEqual(blocked.get("decision"), "blocked")
        self.assertTrue(persistence.is_raw_review_blocked("src-1", "vid-3", cfg=self.cfg))

    def test_state_is_isolated_per_configured_telegram_db_path(self):
        other_cfg = SimpleNamespace(
            TELEGRAM_DB_PATH=os.path.join(self.tempdir.name, "tg_state_other.db"),
            RUN_DAILY_AT="10:00",
            RUN_ONLY_ON_WIFI=False,
            RUN_ONLY_WHILE_CHARGING=False,
            AUDIO_MODE="light",
            TELEGRAM_ALLOWED_USER_IDS=[12345],
        )

        persistence.set_pending_raw_review("src-isolated", {"token": "tok-z", "video_id": "vid-z"}, cfg=self.cfg)

        self.assertIsNone(persistence.get_pending_raw_review("src-isolated", cfg=other_cfg))

        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None

        self.assertIsNone(persistence.get_pending_raw_review("src-isolated", cfg=other_cfg))
        self.assertFalse(persistence.has_pending_raw_reviews(cfg=other_cfg))
        self.assertIsNotNone(persistence.get_pending_raw_review("src-isolated", cfg=self.cfg))
        self.assertTrue(persistence.has_pending_raw_reviews(cfg=self.cfg))


class TestRawReviewTelegramFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(
            TELEGRAM_DB_PATH=os.path.join(self.tempdir.name, "tg_state.db"),
            RUN_DAILY_AT="10:00",
            RUN_ONLY_ON_WIFI=False,
            RUN_ONLY_WHILE_CHARGING=False,
            AUDIO_MODE="light",
            TELEGRAM_ALLOWED_USER_IDS=[12345],
        )
        self.detect_patch = patch("src.bot.persistence._detect_and_add_publish_channels", lambda state, cfg: None)
        self.sqlite_default_patch = patch("src.agent.sqlite_storage._DEFAULT_DB_PATH", Path(self.tempdir.name) / "default_state.db")
        self.detect_patch.start()
        self.sqlite_default_patch.start()
        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None

    async def asyncTearDown(self):
        self.detect_patch.stop()
        self.sqlite_default_patch.stop()
        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None
        self.tempdir.cleanup()

    async def test_request_then_approve_callback(self):
        raw_path = os.path.join(self.tempdir.name, "raw.mp4")
        with open(raw_path, "wb") as f:
            f.write(b"raw-video")

        fake_bot = SimpleNamespace(send_video=AsyncMock(), send_document=AsyncMock())
        fake_app = SimpleNamespace(bot=fake_bot)
        fake_alert = SimpleNamespace(get_bot_app=lambda: fake_app, get_admin_chat_id=lambda: 12345)

        with patch("src.bot.raw_review.get_alert_system", return_value=fake_alert), \
             patch("src.bot.raw_review.load_config", return_value=self.cfg):
            sent = await request_raw_video_review(
                source_id="src-11",
                channel_id="ch-1",
                source_name="Demo Source",
                source_url="https://example.com/source",
                content_type="minecraft_mods",
                video={"id": "vid-9", "title": "Raw Clip", "url": "https://example.com/v.mp4"},
                raw_video_path=raw_path,
                video_type="shorts",
            )

            self.assertTrue(sent)
            pending = persistence.get_pending_raw_review("src-11", cfg=self.cfg)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.get("raw_video_path"), os.path.abspath(raw_path))
            token = pending.get("token")

            fake_query = SimpleNamespace(
                data=f"rawrev:approve:{token}",
                answer=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                message=SimpleNamespace(reply_text=AsyncMock()),
            )
            update = SimpleNamespace(callback_query=fake_query, effective_user=SimpleNamespace(id=12345))
            context = SimpleNamespace(application=SimpleNamespace(create_task=MagicMock()))

            with patch("src.bot.raw_review._schedule_approved_review_processing") as schedule_resume:
                await handle_raw_review_callback(update, context)

        self.assertTrue(persistence.is_raw_review_approved("src-11", "vid-9", cfg=self.cfg))
        self.assertIsNone(persistence.get_pending_raw_review("src-11", cfg=self.cfg))
        schedule_resume.assert_called_once()
        fake_query.message.reply_text.assert_awaited_once()
        fake_query.answer.assert_awaited_once()

    async def test_run_approved_review_now_retries_until_cycle_lock_is_free(self):
        fake_fetcher = SimpleNamespace(
            run_cycle=AsyncMock(side_effect=[
                {"status": "busy"},
                {"status": "ok", "processed": 1, "published": 1},
            ])
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        fake_alert = SimpleNamespace(get_admin_chat_id=lambda: 12345)
        context = SimpleNamespace(bot=fake_bot)

        with patch("src.agent.auto_mod_fetcher.AutoModFetcher", return_value=fake_fetcher), \
             patch("src.bot.raw_review.get_alert_system", return_value=fake_alert), \
             patch("src.bot.raw_review.asyncio.sleep", new=AsyncMock()):
            await _run_approved_review_now(
                {
                    "source_name": "Demo Source",
                    "video_title": "Raw Clip",
                    "video_id": "vid-9",
                    "video_url": "https://example.com/raw.mp4",
                    "video_type": "shorts",
                    "channel_id": "ch-1",
                    "content_type": "minecraft_mods",
                    "source_id": "src-1",
                    "raw_video_path": "/tmp/raw-approved.mp4",
                },
                context,
                max_attempts=2,
                retry_delay_seconds=0,
            )

        self.assertEqual(fake_fetcher.run_cycle.await_count, 2)
        first_call = fake_fetcher.run_cycle.await_args_list[0]
        self.assertEqual(first_call.kwargs.get("target_channel_id"), "ch-1")
        self.assertEqual(first_call.kwargs.get("target_content_type"), "minecraft_mods")
        self.assertEqual(first_call.kwargs.get("target_source_id"), "src-1")
        self.assertEqual(first_call.kwargs.get("target_video_id"), "vid-9")
        self.assertEqual(first_call.kwargs.get("target_video_url"), "https://example.com/raw.mp4")
        self.assertEqual(first_call.kwargs.get("target_video_title"), "Raw Clip")
        self.assertEqual(first_call.kwargs.get("target_video_type"), "shorts")
        self.assertEqual(first_call.kwargs.get("target_raw_video_path"), "/tmp/raw-approved.mp4")
        self.assertEqual(fake_bot.send_message.await_count, 0)

    async def test_run_approved_review_now_notifies_when_another_pending_review_blocks_immediate_resume(self):
        fake_fetcher = SimpleNamespace(
            run_cycle=AsyncMock(return_value={"status": "waiting_raw_review", "waiting_raw_review": 1})
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        fake_alert = SimpleNamespace(get_admin_chat_id=lambda: 12345)
        context = SimpleNamespace(bot=fake_bot)

        with patch("src.agent.auto_mod_fetcher.AutoModFetcher", return_value=fake_fetcher), \
             patch("src.bot.raw_review.get_alert_system", return_value=fake_alert):
            await _run_approved_review_now(
                {
                    "source_name": "Demo Source",
                    "video_title": "Raw Clip",
                    "video_id": "vid-9",
                    "video_url": "https://example.com/raw.mp4",
                    "video_type": "shorts",
                    "channel_id": "ch-1",
                    "content_type": "minecraft_mods",
                    "source_id": "src-1",
                },
                context,
                max_attempts=1,
                retry_delay_seconds=0,
            )

        fake_bot.send_message.assert_awaited_once()
        self.assertIn("لا يمكن متابعة المعالجة الآن", fake_bot.send_message.await_args.kwargs["text"])

    async def test_request_does_not_overwrite_existing_pending_for_same_source(self):
        raw_path = os.path.join(self.tempdir.name, "raw-2.mp4")
        with open(raw_path, "wb") as f:
            f.write(b"raw-video")

        fake_bot = SimpleNamespace(send_video=AsyncMock(), send_document=AsyncMock())
        fake_app = SimpleNamespace(bot=fake_bot)
        fake_alert = SimpleNamespace(get_bot_app=lambda: fake_app, get_admin_chat_id=lambda: 12345)

        with patch("src.bot.raw_review.get_alert_system", return_value=fake_alert), \
             patch("src.bot.raw_review.load_config", return_value=self.cfg):
            first = await request_raw_video_review(
                source_id="src-10",
                channel_id="ch-1",
                source_name="Demo Source",
                source_url="https://example.com/source",
                content_type="minecraft_mods",
                video={"id": "vid-9", "title": "Raw Clip", "url": "https://example.com/v.mp4"},
                raw_video_path=raw_path,
                video_type="shorts",
            )
            pending_before = persistence.get_pending_raw_review("src-10", cfg=self.cfg)

            second = await request_raw_video_review(
                source_id="src-10",
                channel_id="ch-1",
                source_name="Demo Source",
                source_url="https://example.com/source",
                content_type="minecraft_mods",
                video={"id": "vid-10", "title": "Other Clip", "url": "https://example.com/v2.mp4"},
                raw_video_path=raw_path,
                video_type="shorts",
            )

        pending_after = persistence.get_pending_raw_review("src-10", cfg=self.cfg)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(fake_bot.send_video.await_count, 1)
        self.assertEqual(pending_after.get("token"), pending_before.get("token"))
        self.assertEqual(pending_after.get("video_id"), "vid-9")


if __name__ == "__main__":
    unittest.main()