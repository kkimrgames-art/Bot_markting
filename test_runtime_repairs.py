import os
import asyncio
import unittest
import tempfile
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.agent.auto_mod_fetcher import AutoModDB, AutoModFetcher, _RUN_CYCLE_LOCK, _build_hashtag_only_upload_metadata, _build_ytdlp_runtime_diagnostics, _build_yt_opts, _compute_loop_sleep_seconds, _create_runtime_dir_keepalive, _download_profile_overrides, _infer_processing_video_type, _normalize_auto_fetch_loop_config, _normalize_youtube_watch_url, _release_runtime_dir_keepalive, _resolve_any_cookiefile, _resolve_cobalt_api_settings, _resolve_cookiefile_details, normalize_source_settings, pick_source_facecam_clip, pick_source_facecam_config, pick_source_overlay_config, pick_source_tail_trim_seconds, pick_source_video_effects
from src.agent.disk_guard import cleanup_old_files
from src.agent.ffmpeg_utils import _candidate_ffmpeg_dirs
from src.agent.local_metadata import extract_source_metadata_context
from src.agent.mod_video_processor import ModVideoProcessor
from src.agent.renderer import render_with_pip
from src.agent.supabase_client import _repair_upsert_payload
from src.bot import persistence
from src.bot.handlers.auto_mod_handlers import (
    AM_ADD_SOURCE_CUSTOMIZE,
    AM_ADD_SOURCE_FACECAM,
    AM_ADD_SOURCE_URL,
    AM_CONFIG,
    AM_EDIT_SOURCE_CHANNEL,
    AM_MENU,
    AM_SOURCES,
    AM_STATUS,
    add_source_choose_overlay_animation_duration,
    add_source_choose_video_effect_duration,
    add_source_facecam_upload_receive,
    add_source_name,
    add_source_url,
    auto_mod_menu,
    delete_source,
    edit_source_facecam_manage,
    edit_source_overlay_animation_duration,
    edit_source_video_effect_duration,
    edit_source_tail_trim_value,
    get_auto_mod_conversation_handler,
    _build_facecam_menu_keyboard,
    _download_facecam_clip,
    run_now,
    sources_menu,
    test_render_menu,
    test_render_run,
)
from src.bot.handlers.edit_handlers import facecam_upload_receive


class RuntimeRepairTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _callback_patterns(handlers):
        patterns = []
        for handler in handlers:
            pattern = getattr(handler, "pattern", None)
            if pattern is None:
                continue
            patterns.append(getattr(pattern, "pattern", pattern))
        return patterns

    @staticmethod
    def _handler_callbacks(handlers):
        callbacks = []
        for handler in handlers:
            callback = getattr(handler, "callback", None)
            if callback is None:
                continue
            callbacks.append(getattr(callback, "__name__", str(callback)))
        return callbacks

    @staticmethod
    def _mock_ydl_context(*, info=None, error=None, filename=None):
        ctx = MagicMock()
        ydl = ctx.__enter__.return_value
        if error is not None:
            ydl.extract_info.side_effect = error
        else:
            ydl.extract_info.return_value = info
        ydl.prepare_filename.return_value = filename or os.path.join(".temp", "vid.webm")
        return ctx

    def test_repair_upsert_payload_fills_youtube_api_key_from_local_file(self):
        payload = _repair_upsert_payload(
            None,
            "youtube_api_keys",
            {"key_id": "ec9099267576", "quota_used": 0, "is_active": True},
            "key_id",
        )
        self.assertEqual(payload["key_id"], "ec9099267576")
        self.assertIn("api_key", payload)
        self.assertTrue(payload["api_key"].startswith("AIza"))

    def test_toggle_source_upserts_full_row(self):
        db = AutoModDB("inst-1")
        full_source = {
            "id": "src-1",
            "instance_id": "inst-1",
            "channel_id": "ch-1",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube",
            "enabled": True,
        }
        with patch.object(db, "get_sources", return_value=[full_source]), \
             patch("src.agent.supabase_client.supabase_upsert") as upsert:
            self.assertTrue(db.toggle_source("src-1", False))
            payload = upsert.call_args.args[1]
            self.assertEqual(payload["id"], "src-1")
            self.assertEqual(payload["instance_id"], "inst-1")
            self.assertEqual(payload["channel_id"], "ch-1")
            self.assertEqual(payload["source_url"], "https://youtube.com/@demo")
            self.assertFalse(payload["enabled"])

    def test_ffmpeg_candidates_include_legacy_workspace_tools_dir(self):
        candidates = _candidate_ffmpeg_dirs()
        legacy = os.path.normpath(os.path.abspath(os.path.join(os.getcwd(), ".tools", "ffmpeg")))
        self.assertIn(legacy, candidates)

    def test_auto_mod_navigation_buttons_work_from_multiple_states(self):
        conv = get_auto_mod_conversation_handler()
        expected_patterns = {
            "^am_sources$",
            "^am_schedule$",
            "^am_status$",
            "^am_config$",
            "^am_view_containers$",
            "^am_toggle$",
            "^am_run_now$",
            "^am_test_render$",
            "^list_channels:",
        }

        for state in (AM_SOURCES, AM_ADD_SOURCE_URL, AM_STATUS, AM_CONFIG):
            patterns = set(self._callback_patterns(conv.states[state]))
            self.assertTrue(expected_patterns.issubset(patterns), f"missing shared nav handlers in state {state}")

    def test_sources_menu_shows_source_feature_summaries_per_source(self):
        db = MagicMock()
        db.get_sources.return_value = [{
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "content_type": "minecraft_mods",
            "source_url": "https://youtube.com/@demo",
            "channel_id": "channel-1234567890",
            "settings": {
                "facecam": {
                    "enabled": True,
                    "position": "bottom_center",
                    "clips": [
                        {"id": "clip-1", "path": "a.mp4", "enabled": True},
                        {"id": "clip-2", "path": "b.mp4", "enabled": True},
                    ],
                },
                "shorts_overlay": {
                    "enabled": True,
                    "texts": ["one", "two"],
                    "selection_mode": "random",
                },
                "video_effects": {
                    "intro": {"enabled": True, "type": "blur", "duration": 1.5},
                    "outro": {"enabled": True, "type": "black_blur", "duration": 2.0},
                },
            },
        }]
        chat = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(callback_query=None, effective_chat=chat)
        context = SimpleNamespace(user_data={})

        with patch("src.bot.handlers.auto_mod_handlers._get_db", return_value=db):
            state = asyncio.run(sources_menu(update, context))

        self.assertEqual(state, AM_SOURCES)
        message_text = chat.send_message.await_args.args[0]
        self.assertIn("🎬 Facecam:", message_text)
        self.assertIn("✅ دائري أسفل الفيديو / 2 مقطع", message_text)
        self.assertIn("📝 النص:", message_text)
        self.assertIn("✅ 2 نص / عشوائي", message_text)
        self.assertIn("✨ البداية:", message_text)
        self.assertIn("✅ Blur عادي / 1.5ث", message_text)
        self.assertIn("🏁 النهاية:", message_text)
        self.assertIn("✅ Black Blur / 2ث", message_text)

    def test_auto_mod_menu_shows_test_render_button_next_to_run_now(self):
        db = MagicMock()
        db.get_config.return_value = {"auto_fetch_enabled": True, "default_content_type": "minecraft_mods"}
        db.get_stats.return_value = {"total_channels": 1, "total_sources": 2, "total_schedules": 1, "published": 5, "failed": 1}
        chat = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(callback_query=None, effective_chat=chat)
        context = SimpleNamespace(user_data={})

        with patch("src.bot.handlers.auto_mod_handlers._get_db", return_value=db):
            state = asyncio.run(auto_mod_menu(update, context))

        self.assertEqual(state, AM_MENU)
        markup = chat.send_message.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("am_run_now", callbacks)
        self.assertIn("am_test_render", callbacks)

    def test_test_render_menu_lists_available_sources(self):
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})
        db = MagicMock()
        db.get_sources.return_value = [
            {"id": "src-1", "enabled": True, "source_name": "Alpha Source"},
            {"id": "src-2", "enabled": False, "source_name": "Beta Source"},
        ]

        with patch("src.bot.handlers.auto_mod_handlers._get_db", return_value=db):
            state = asyncio.run(test_render_menu(update, context))

        self.assertEqual(state, AM_MENU)
        message_text = query.edit_message_text.await_args.args[0]
        self.assertIn("اختبار وتطوير", message_text)
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("am_test_render_src:src-1", callbacks)
        self.assertIn("am_test_render_src:src-2", callbacks)

    def test_add_source_customize_state_registers_tail_trim_callback(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_ADD_SOURCE_CUSTOMIZE]))
        self.assertIn("^am_src_trim:", patterns)

    def test_add_source_customize_state_registers_video_effect_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_ADD_SOURCE_CUSTOMIZE]))
        self.assertIn("^am_src_fx_menu:", patterns)
        self.assertIn("^am_src_fx_kind:", patterns)
        self.assertIn("^am_src_fx_dur:", patterns)

    def test_add_source_customize_state_registers_overlay_animation_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_ADD_SOURCE_CUSTOMIZE]))
        self.assertIn("^am_src_ov_anim_kind:", patterns)
        self.assertIn("^am_src_ov_anim_dur:", patterns)

    def test_add_source_facecam_state_registers_facecam_manager_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_ADD_SOURCE_FACECAM]))
        callbacks = set(self._handler_callbacks(conv.states[AM_ADD_SOURCE_FACECAM]))
        self.assertIn("^am_src_fc_manage:", patterns)
        self.assertIn("^am_src_fc_done$", patterns)
        self.assertIn("add_source_facecam_upload_receive", callbacks)

    def test_edit_source_state_registers_tail_trim_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_EDIT_SOURCE_CHANNEL]))
        self.assertIn("^am_edit_trim_menu$", patterns)
        self.assertIn("^am_edit_trim:", patterns)

    def test_edit_source_state_registers_video_effect_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_EDIT_SOURCE_CHANNEL]))
        self.assertIn("^am_edit_fx_menu:", patterns)
        self.assertIn("^am_edit_fx_kind:", patterns)
        self.assertIn("^am_edit_fx_dur:", patterns)

    def test_edit_source_state_registers_overlay_animation_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_EDIT_SOURCE_CHANNEL]))
        self.assertIn("^am_edit_ov_anim_menu:", patterns)
        self.assertIn("^am_edit_ov_anim_kind:", patterns)
        self.assertIn("^am_edit_ov_anim_dur:", patterns)

    def test_edit_source_state_registers_facecam_manager_callbacks(self):
        conv = get_auto_mod_conversation_handler()
        patterns = set(self._callback_patterns(conv.states[AM_EDIT_SOURCE_CHANNEL]))
        callbacks = set(self._handler_callbacks(conv.states[AM_EDIT_SOURCE_CHANNEL]))
        self.assertIn("^am_edit_fc_manage:", patterns)
        self.assertIn("edit_source_facecam_upload_receive", callbacks)

    def test_normalize_source_settings_parses_tail_trim_config(self):
        normalized = normalize_source_settings({"tail_trim": {"enabled": True, "seconds": "2.5"}})
        self.assertEqual(normalized["tail_trim"], {"enabled": True, "seconds": 2.5})
        self.assertEqual(pick_source_tail_trim_seconds(normalized), 2.5)

        normalized_disabled = normalize_source_settings({"tail_trim": {"enabled": True, "seconds": 0}})
        self.assertEqual(normalized_disabled["tail_trim"], {"enabled": False, "seconds": 0.0})
        self.assertEqual(pick_source_tail_trim_seconds(normalized_disabled), 0.0)

    def test_normalize_source_settings_parses_video_effects_config(self):
        normalized = normalize_source_settings({
            "video_effects": {
                "intro": {"enabled": True, "type": "black", "duration": "2.5"},
                "outro": "blur",
            }
        })

        self.assertEqual(normalized["video_effects"]["intro"], {"enabled": True, "type": "black_blur", "duration": 2.5})
        self.assertEqual(normalized["video_effects"]["outro"], {"enabled": True, "type": "blur", "duration": 1.0})
        self.assertEqual(pick_source_video_effects(normalized), {
            "intro": {"enabled": True, "type": "black_blur", "duration": 2.5},
            "outro": {"enabled": True, "type": "blur", "duration": 1.0},
        })

        normalized_disabled = normalize_source_settings({"video_effects": {"intro": {"enabled": True, "type": "none", "duration": 2}}})
        self.assertEqual(normalized_disabled["video_effects"]["intro"], {"enabled": False, "type": "none", "duration": 0.0})

    def test_normalize_source_settings_parses_overlay_animation_config(self):
        normalized = normalize_source_settings({
            "shorts_overlay": {
                "enabled": True,
                "texts": ["أهلاً"],
                "intro_animation": {"enabled": True, "type": "blur", "duration": "0.8"},
                "outro_animation": "fade",
            }
        })

        self.assertEqual(normalized["shorts_overlay"]["intro_animation"], {"enabled": True, "type": "blur", "duration": 0.8})
        self.assertEqual(normalized["shorts_overlay"]["outro_animation"], {"enabled": True, "type": "fade", "duration": 0.6})
        self.assertEqual(pick_source_overlay_config(normalized)["intro_animation"], {"enabled": True, "type": "blur", "duration": 0.8})

        normalized_disabled = normalize_source_settings({
            "shorts_overlay": {"enabled": True, "texts": ["Hi"], "intro_animation": {"enabled": True, "type": "none", "duration": 1}}
        })
        self.assertEqual(normalized_disabled["shorts_overlay"]["intro_animation"], {"enabled": False, "type": "none", "duration": 0.0})

    def test_normalize_source_settings_parses_facecam_config(self):
        normalized = normalize_source_settings({
            "facecam": {
                "enabled": True,
                "position": "bottom",
                "shape": "rect",
                "scale": "0.4",
                "clips": [
                    {"id": "clip-1", "path": "/tmp/facecam-1.mp4", "name": "cam 1", "enabled": True},
                    "/tmp/facecam-2.mov",
                ],
            }
        })

        self.assertEqual(normalized["facecam"]["position"], "bottom_center")
        self.assertEqual(normalized["facecam"]["shape"], "square")
        self.assertEqual(normalized["facecam"]["scale"], 0.4)
        self.assertEqual(normalized["facecam"]["layout"], "custom")
        self.assertEqual(len(normalized["facecam"]["clips"]), 2)
        self.assertTrue(normalized["facecam_enabled"])
        self.assertEqual(normalized["facecam_position"], "bottom_center")
        self.assertEqual(pick_source_facecam_config(normalized)["clips"][0]["id"], "clip-1")

    def test_normalize_source_settings_resolves_small_corner_facecam_layouts(self):
        expected = {
            "small_circle_top_left": "top_left",
            "small_circle_top_right": "top_right",
            "small_circle_bottom_right": "bottom_right",
            "small_circle_bottom_left": "bottom_left",
        }

        for layout, position in expected.items():
            with self.subTest(layout=layout):
                normalized = normalize_source_settings({
                    "facecam": {
                        "enabled": True,
                        "layout": layout,
                        "clips": [{"id": "clip-1", "path": "/tmp/facecam-1.mp4", "enabled": True}],
                    }
                })

                self.assertEqual(normalized["facecam"]["layout"], layout)
                self.assertEqual(normalized["facecam"]["position"], position)
                self.assertEqual(normalized["facecam"]["shape"], "circle")
                self.assertEqual(normalized["facecam"]["scale"], 0.18)
                self.assertEqual(normalized["facecam_layout"], layout)

    def test_facecam_menu_keyboard_offers_all_small_corner_layouts(self):
        markup = _build_facecam_menu_keyboard({"facecam": {"enabled": True, "position": "top_center", "clips": []}}, mode="add")
        labels = [button.text for row in markup.inline_keyboard for button in row]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

        self.assertIn("↖️ دائرة صغيرة أعلى اليسار", labels)
        self.assertIn("↗️ دائرة صغيرة أعلى اليمين", labels)
        self.assertIn("↘️ دائرة صغيرة أسفل اليمين", labels)
        self.assertIn("↙️ دائرة صغيرة أسفل اليسار", labels)
        self.assertIn("am_src_fc_manage:pos:small_circle_top_left", callbacks)
        self.assertIn("am_src_fc_manage:pos:small_circle_top_right", callbacks)
        self.assertIn("am_src_fc_manage:pos:small_circle_bottom_right", callbacks)
        self.assertIn("am_src_fc_manage:pos:small_circle_bottom_left", callbacks)

    def test_pick_source_facecam_clip_prefers_enabled_source_clips(self):
        clip_a = os.path.join(self.tempdir.name, "facecam-a.mp4")
        clip_b = os.path.join(self.tempdir.name, "facecam-b.mov")
        Path(clip_a).write_bytes(b"a")
        Path(clip_b).write_bytes(b"b")

        with patch("src.agent.auto_mod_fetcher.random.choice", side_effect=lambda items: items[-1]) as chooser:
            config, chosen = pick_source_facecam_clip({
                "facecam": {
                    "enabled": True,
                    "position": "top_center",
                    "clips": [
                        {"id": "a", "path": clip_a, "enabled": True},
                        {"id": "b", "path": clip_b, "enabled": True},
                        {"id": "c", "path": os.path.join(self.tempdir.name, "missing.mp4"), "enabled": True},
                    ],
                }
            })

        self.assertTrue(config["enabled"])
        self.assertEqual(chosen, clip_b)
        self.assertEqual(chooser.call_args.args[0], [clip_a, clip_b])

    def test_pick_source_facecam_clip_returns_empty_when_no_valid_clips_exist(self):
        config, chosen = pick_source_facecam_clip({
            "facecam": {
                "enabled": True,
                "clips": [
                    {"id": "bad", "path": os.path.join(self.tempdir.name, "missing.txt"), "enabled": True},
                    {"id": "off", "path": os.path.join(self.tempdir.name, "missing.mp4"), "enabled": False},
                ],
            }
        })

        self.assertTrue(config["enabled"])
        self.assertEqual(chosen, "")

    def test_add_source_url_rejects_multiple_links_in_one_message(self):
        update = SimpleNamespace(message=SimpleNamespace(text="https://one.example https://two.example", reply_text=AsyncMock()))
        context = SimpleNamespace(user_data={"am_new_source": {"awaiting_url": True}})

        state = asyncio.run(add_source_url(update, context))

        self.assertEqual(state, AM_ADD_SOURCE_URL)
        self.assertNotIn("source_url", context.user_data["am_new_source"])
        update.message.reply_text.assert_awaited()

    def test_edit_source_tail_trim_value_updates_source_settings(self):
        query = SimpleNamespace(data="am_edit_trim:3.5", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_edit_source_id": "src-1"})

        with patch("src.bot.handlers.auto_mod_handlers._update_edit_source_settings", AsyncMock(return_value=True)) as update_settings, \
             patch("src.bot.handlers.auto_mod_handlers._show_edit_source_menu", AsyncMock(return_value=AM_EDIT_SOURCE_CHANNEL)) as show_menu:
            state = asyncio.run(edit_source_tail_trim_value(update, context))

        self.assertEqual(state, AM_EDIT_SOURCE_CHANNEL)
        update_settings.assert_awaited_once_with(context, {"tail_trim": {"enabled": True, "seconds": 3.5}})
        show_menu.assert_awaited_once_with(update, context)

    def test_add_source_choose_video_effect_duration_updates_draft_settings(self):
        query = SimpleNamespace(data="am_src_fx_dur:intro:black_blur:1.5", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_new_source": {"source_settings": {}}})

        with patch("src.bot.handlers.auto_mod_handlers._ask_source_video_effect_kind", AsyncMock(return_value=AM_ADD_SOURCE_CUSTOMIZE)) as ask_effect:
            state = asyncio.run(add_source_choose_video_effect_duration(update, context))

        self.assertEqual(state, AM_ADD_SOURCE_CUSTOMIZE)
        self.assertEqual(context.user_data["am_new_source"]["source_settings"]["video_effects"]["intro"], {
            "enabled": True,
            "type": "black_blur",
            "duration": 1.5,
        })
        self.assertTrue(context.user_data["am_new_source"]["intro_effect_configured"])
        ask_effect.assert_awaited_once_with(update, context, "outro")

    def test_edit_source_video_effect_duration_updates_source_settings(self):
        query = SimpleNamespace(data="am_edit_fx_dur:outro:black_blur:2.0", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_edit_source_id": "src-1"})

        with patch("src.bot.handlers.auto_mod_handlers._update_edit_source_settings", AsyncMock(return_value=True)) as update_settings, \
             patch("src.bot.handlers.auto_mod_handlers._show_edit_source_menu", AsyncMock(return_value=AM_EDIT_SOURCE_CHANNEL)) as show_menu:
            state = asyncio.run(edit_source_video_effect_duration(update, context))

        self.assertEqual(state, AM_EDIT_SOURCE_CHANNEL)
        update_settings.assert_awaited_once_with(context, {
            "video_effects": {"outro": {"enabled": True, "type": "black_blur", "duration": 2.0}}
        })
        show_menu.assert_awaited_once_with(update, context)

    def test_add_source_choose_overlay_animation_duration_updates_draft_settings(self):
        query = SimpleNamespace(data="am_src_ov_anim_dur:intro:blur:0.8", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_new_source": {"source_settings": {}}})

        with patch("src.bot.handlers.auto_mod_handlers._ask_source_overlay_animation_kind", AsyncMock(return_value=AM_ADD_SOURCE_CUSTOMIZE)) as ask_anim:
            state = asyncio.run(add_source_choose_overlay_animation_duration(update, context))

        self.assertEqual(state, AM_ADD_SOURCE_CUSTOMIZE)
        self.assertEqual(context.user_data["am_new_source"]["source_settings"]["shorts_overlay"]["intro_animation"], {
            "enabled": True,
            "type": "blur",
            "duration": 0.8,
        })
        ask_anim.assert_awaited_once_with(update, context, "outro")

    def test_edit_source_overlay_animation_duration_updates_source_settings(self):
        query = SimpleNamespace(data="am_edit_ov_anim_dur:outro:fade:0.5", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_edit_source_id": "src-1"})

        with patch("src.bot.handlers.auto_mod_handlers._update_edit_source_settings", AsyncMock(return_value=True)) as update_settings, \
             patch("src.bot.handlers.auto_mod_handlers._show_overlay_editor", AsyncMock(return_value=AM_EDIT_SOURCE_CHANNEL)) as show_menu:
            state = asyncio.run(edit_source_overlay_animation_duration(update, context))

        self.assertEqual(state, AM_EDIT_SOURCE_CHANNEL)
        update_settings.assert_awaited_once_with(context, {
            "shorts_overlay": {"outro_animation": {"enabled": True, "type": "fade", "duration": 0.5}}
        })
        show_menu.assert_awaited_once_with(update, context)

    def test_add_source_facecam_upload_receive_appends_clip_to_draft_settings(self):
        clip_entry = {
            "id": "clip-1",
            "path": ".data/facecam_sources/src-1/clip-1.mp4",
            "name": "clip-1.mp4",
            "enabled": True,
            "created_at": "2026-03-10T00:00:00+00:00",
        }
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(user_data={
            "am_facecam_upload_mode": "add",
            "am_new_source": {
                "source_id": "src-1",
                "source_settings": {"facecam": {"enabled": True, "layout": "small_circle_top_right", "position": "top_right", "shape": "circle", "scale": 0.18, "clips": []}},
            },
        })

        with patch("src.bot.handlers.auto_mod_handlers._download_facecam_clip", AsyncMock(return_value=(clip_entry, None))), \
             patch("src.bot.handlers.auto_mod_handlers._show_add_facecam_manager", AsyncMock(return_value=AM_ADD_SOURCE_FACECAM)) as show_manager:
            state = asyncio.run(add_source_facecam_upload_receive(update, context))

        self.assertEqual(state, AM_ADD_SOURCE_FACECAM)
        facecam = context.user_data["am_new_source"]["source_settings"]["facecam"]
        self.assertEqual(facecam["clips"], [clip_entry])
        self.assertEqual(facecam["layout"], "small_circle_top_right")
        self.assertEqual(context.user_data["am_new_source"]["facecam_settings"], {"facecam": facecam})
        show_manager.assert_awaited_once()

    def test_download_facecam_clip_converts_photo_upload_to_mp4(self):
        photo = SimpleNamespace(file_id="photo-file", file_size=1024)
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        update = SimpleNamespace(message=SimpleNamespace(photo=[photo], video=None, document=None))
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))

        with patch("src.agent.config.load_config", return_value=SimpleNamespace(LOCAL_BOT_API_URL="http://local")), \
             patch("src.bot.handlers.auto_mod_handlers._project_local_path", side_effect=lambda *parts: os.path.join(self.tempdir.name, *parts)), \
             patch("src.bot.handlers.auto_mod_handlers.convert_still_image_to_loop_video", return_value=True) as convert_image:
            clip_entry, error_message = asyncio.run(_download_facecam_clip(update, context, "src-photo"))

        self.assertIsNone(error_message)
        self.assertIsNotNone(clip_entry)
        self.assertTrue(str(clip_entry["path"]).endswith(".mp4"))
        self.assertEqual(clip_entry["name"], "facecam.mp4")
        context.bot.get_file.assert_awaited_once_with("photo-file")
        download_path = tg_file.download_to_drive.await_args.args[0]
        self.assertTrue(download_path.endswith(".jpg"))
        convert_args = convert_image.call_args.args
        self.assertTrue(convert_args[0].endswith(".jpg"))
        self.assertTrue(convert_args[1].endswith(".mp4"))

    def test_download_facecam_clip_converts_image_document_to_mp4(self):
        document = SimpleNamespace(file_id="doc-file", file_size=2048, file_name="avatar.png", mime_type="image/png")
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        update = SimpleNamespace(message=SimpleNamespace(photo=None, video=None, document=document))
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))

        with patch("src.agent.config.load_config", return_value=SimpleNamespace(LOCAL_BOT_API_URL="http://local")), \
             patch("src.bot.handlers.auto_mod_handlers._project_local_path", side_effect=lambda *parts: os.path.join(self.tempdir.name, *parts)), \
             patch("src.bot.handlers.auto_mod_handlers.convert_still_image_to_loop_video", return_value=True) as convert_image:
            clip_entry, error_message = asyncio.run(_download_facecam_clip(update, context, "src-doc"))

        self.assertIsNone(error_message)
        self.assertIsNotNone(clip_entry)
        self.assertTrue(str(clip_entry["path"]).endswith(".mp4"))
        self.assertEqual(clip_entry["name"], "avatar.mp4")
        context.bot.get_file.assert_awaited_once_with("doc-file")
        download_path = tg_file.download_to_drive.await_args.args[0]
        self.assertTrue(download_path.endswith(".png"))
        convert_args = convert_image.call_args.args
        self.assertTrue(convert_args[0].endswith(".png"))
        self.assertTrue(convert_args[1].endswith(".mp4"))

    def test_legacy_facecam_upload_receive_converts_photo_to_mp4(self):
        photo = SimpleNamespace(file_id="legacy-photo", file_size=1024)
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(
                photo=[photo],
                video=None,
                document=None,
                reply_text=AsyncMock(),
            )
        )
        context = SimpleNamespace(
            user_data={"channel_id": "channel-1"},
            bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)),
        )
        state_store = {"facecam_clips_by_channel": {}}

        with patch("src.bot.handlers.edit_handlers.load_config", return_value=SimpleNamespace(LOCAL_BOT_API_URL="http://local")), \
             patch("src.agent.config.load_config", return_value=SimpleNamespace(LOCAL_BOT_API_URL="http://local")), \
             patch("src.bot.handlers.edit_handlers.load_state", return_value=state_store), \
             patch("src.bot.handlers.edit_handlers.save_state") as save_state, \
             patch("src.bot.handlers.edit_handlers.convert_still_image_to_loop_video", return_value=True) as convert_image, \
             patch("src.bot.handlers.edit_handlers.uuid.uuid4", return_value="legacy-id"):
            state = asyncio.run(facecam_upload_receive(update, context))

        self.assertEqual(state, -1)
        context.bot.get_file.assert_awaited_once_with("legacy-photo")
        download_path = tg_file.download_to_drive.await_args.args[0]
        self.assertTrue(download_path.endswith("legacy-id_src.jpg"))
        convert_args = convert_image.call_args.args
        self.assertTrue(convert_args[0].endswith("legacy-id_src.jpg"))
        self.assertTrue(convert_args[1].endswith("legacy-id.mp4"))
        clip_entry = state_store["facecam_clips_by_channel"]["channel-1"][0]
        self.assertEqual(clip_entry["name"], "facecam.mp4")
        self.assertTrue(clip_entry["path"].endswith("legacy-id.mp4"))
        save_state.assert_called_once()
        update.message.reply_text.assert_awaited_with("✅ تم رفع المقطع/الصورة بنجاح وتفعيلها لهذه القناة.")

    def test_edit_source_facecam_manage_deletes_clip_from_source_settings(self):
        clip_one = {"id": "clip-1", "path": ".data/facecam_sources/src-1/clip-1.mp4", "enabled": True}
        clip_two = {"id": "clip-2", "path": ".data/facecam_sources/src-1/clip-2.mp4", "enabled": True}
        query = SimpleNamespace(data="am_edit_fc_manage:del:clip-1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"am_edit_source_id": "src-1"})

        with patch("src.bot.handlers.auto_mod_handlers._get_edit_source", AsyncMock(return_value={"id": "src-1", "settings": {"facecam": {"enabled": True, "clips": [clip_one, clip_two]}}})), \
             patch("src.bot.handlers.auto_mod_handlers._delete_facecam_clip_file") as delete_file, \
             patch("src.bot.handlers.auto_mod_handlers._update_edit_source_settings", AsyncMock(return_value=True)) as update_settings, \
             patch("src.bot.handlers.auto_mod_handlers._show_edit_facecam_manager", AsyncMock(return_value=AM_EDIT_SOURCE_CHANNEL)) as show_manager:
            state = asyncio.run(edit_source_facecam_manage(update, context))

        self.assertEqual(state, AM_EDIT_SOURCE_CHANNEL)
        removed_clip = delete_file.call_args.args[0]
        self.assertEqual(removed_clip["id"], "clip-1")
        self.assertEqual(removed_clip["path"], clip_one["path"])
        kept_clips = update_settings.await_args.args[1]["facecam"]["clips"]
        self.assertEqual(len(kept_clips), 1)
        self.assertEqual(kept_clips[0]["id"], "clip-2")
        self.assertEqual(kept_clips[0]["path"], clip_two["path"])
        show_manager.assert_awaited_once()

    def test_delete_source_removes_source_facecam_storage_directory(self):
        source_dir = Path(self.tempdir.name) / ".data" / "facecam_sources" / "src-1"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "clip-1.mp4").write_bytes(b"clip")
        query = SimpleNamespace(data="am_delete_src:src-1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})
        db = MagicMock()
        db.get_sources.return_value = [{
            "id": "src-1",
            "settings": {"facecam": {"clips": [{"id": "clip-1", "path": ".data/facecam_sources/src-1/clip-1.mp4", "enabled": True}]}}
        }]
        db.remove_source.return_value = True

        with patch("src.bot.handlers.auto_mod_handlers._get_db", return_value=db), \
             patch("src.bot.handlers.auto_mod_handlers._project_local_path", side_effect=lambda *parts: str(Path(self.tempdir.name, *parts))), \
             patch("src.bot.handlers.auto_mod_handlers.sources_menu", AsyncMock(return_value=AM_SOURCES)) as show_sources:
            state = asyncio.run(delete_source(update, context))

        self.assertEqual(state, AM_SOURCES)
        db.remove_source.assert_called_once_with("src-1")
        self.assertFalse(source_dir.exists())
        show_sources.assert_awaited_once()

    def test_delete_channel_cleans_facecam_state_publish_entries_and_token_file(self):
        from src.bot.channel_manager import ChannelManager

        ChannelManager._instance = None
        channels_dir = Path(self.tempdir.name) / "channels"
        channels_dir.mkdir(parents=True, exist_ok=True)
        channel_file = channels_dir / "channel-1.json"
        channel_file.write_text(
            '{"channel_id":"channel-1","channel_name":"Demo","youtube_channel_id":"yt-1","platform":"youtube","enabled":true,"content_type":"minecraft","privacy":"unlisted","publish_interval":3600,"language":"ar","created_at":"2024-01-01T00:00:00"}',
            encoding="utf-8",
        )

        clip_path = Path(self.tempdir.name) / "facecam-a.mp4"
        other_clip_path = Path(self.tempdir.name) / "facecam-b.mp4"
        clip_path.write_bytes(b"a")
        other_clip_path.write_bytes(b"b")
        token_dir = Path(self.tempdir.name) / "youtube_tokens"
        token_dir.mkdir(parents=True, exist_ok=True)
        token_path = token_dir / "yt-1.json"
        token_path.write_text("{}", encoding="utf-8")

        state_store = {
            "facecam_clips_by_channel": {
                "channel-1": [{"path": str(clip_path)}],
                "channel-2": [{"path": str(other_clip_path)}],
            },
            "publish_channels": [
                {"internal_id": "channel-1", "channel_id": "yt-1", "title": "Demo"},
                {"internal_id": "channel-2", "channel_id": "yt-2", "title": "Other"},
            ],
        }
        cfg = SimpleNamespace(TELEGRAM_DB_PATH=str(Path(self.tempdir.name) / "bot_state.db"))

        try:
            with patch("src.bot.channel_manager.load_config", return_value=cfg), \
                 patch("src.bot.channel_manager.load_state", return_value=state_store), \
                 patch("src.bot.channel_manager.save_state") as save_state:
                manager = ChannelManager(data_dir=str(channels_dir))
                deleted = manager.delete_channel("channel-1")
        finally:
            ChannelManager._instance = None

        self.assertTrue(deleted)
        self.assertFalse(channel_file.exists())
        self.assertFalse(clip_path.exists())
        self.assertTrue(other_clip_path.exists())
        self.assertFalse(token_path.exists())
        self.assertNotIn("channel-1", state_store["facecam_clips_by_channel"])
        self.assertEqual(state_store["publish_channels"], [{"internal_id": "channel-2", "channel_id": "yt-2", "title": "Other"}])
        save_state.assert_called_once()

    def test_add_source_name_passes_source_facecam_settings_and_source_id_to_db(self):
        update = SimpleNamespace(message=SimpleNamespace(text="Demo Source", reply_text=AsyncMock()))
        context = SimpleNamespace(user_data={
            "am_new_source": {
                "channel_id": "ch-1",
                "source_url": "https://youtube.com/@demo",
                "content_type": "minecraft_mods",
                "platform": "youtube_shorts",
                "source_id": "src-draft-1",
                "source_settings": {"facecam": {"enabled": True, "position": "bottom_center", "clips": [{"id": "clip-1", "path": "clip.mp4", "enabled": True}]}} ,
                "facecam_settings": {"facecam": {"enabled": True, "position": "bottom_center", "clips": [{"id": "clip-1", "path": "clip.mp4", "enabled": True}]}} ,
            }
        })
        db = MagicMock()
        db.add_source.return_value = True

        with patch("src.bot.handlers.auto_mod_handlers._get_db", return_value=db):
            state = asyncio.run(add_source_name(update, context))

        self.assertEqual(state, AM_SOURCES)
        self.assertEqual(db.add_source.call_args.kwargs["source_id"], "src-draft-1")
        self.assertEqual(db.add_source.call_args.kwargs["facecam_settings"]["facecam"]["position"], "bottom_center")
        self.assertTrue(db.add_source.call_args.kwargs["source_settings"]["facecam"]["enabled"])
        update.message.reply_text.assert_awaited_once()

    def test_run_cycle_rejects_overlapping_execution(self):
        fetcher = AutoModFetcher("inst-lock-test")
        acquired = _RUN_CYCLE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = asyncio.run(fetcher.run_cycle(force=True))
        finally:
            if acquired and _RUN_CYCLE_LOCK.locked():
                _RUN_CYCLE_LOCK.release()

        self.assertEqual(result.get("status"), "busy")

    def test_download_sync_recreates_output_dir_before_attempts(self):
        fetcher = AutoModFetcher("inst-download-dir")
        output_dir = os.path.join(self.tempdir.name, "auto_mod_downloads")

        with patch("src.agent.auto_mod_fetcher.time.sleep"), \
             patch("src.agent.auto_mod_fetcher.ResilientFS.makedirs") as makedirs, \
             patch.object(fetcher, "_download_sync_attempt", side_effect=[None, None, None]):
            result = fetcher._download_sync("https://example.com/video", output_dir)

        self.assertIsNone(result)
        self.assertGreaterEqual(makedirs.call_count, 1)
        makedirs.assert_any_call(os.path.abspath(output_dir), exist_ok=True)

    def test_download_profile_overrides_prioritize_android_vr_then_web_then_compatibility(self):
        with patch.dict(os.environ, {"YTDLP_HIGH_QUALITY_FIRST": "1"}, clear=True):
            profiles = _download_profile_overrides("/usr/bin/ffmpeg", cookies_enabled=False)

        self.assertEqual([profile["label"] for profile in profiles], [
            "high_quality_android_vr",
            "high_quality_web",
            "compatibility_mobile",
        ])
        self.assertEqual(
            profiles[0]["extractor_args"]["youtube"]["player_client"],
            ["android_vr", "android", "mweb"],
        )

    def test_download_profile_overrides_with_cookies_prioritize_authenticated_defaults(self):
        with patch.dict(os.environ, {"YTDLP_HIGH_QUALITY_FIRST": "1"}, clear=True):
            profiles = _download_profile_overrides("/usr/bin/ffmpeg", cookies_enabled=True)

        self.assertEqual([profile["label"] for profile in profiles], [
            "authenticated_default",
            "authenticated_web",
            "compatibility_mobile",
        ])
        self.assertNotIn("extractor_args", profiles[0])
        self.assertEqual(
            profiles[1]["extractor_args"]["youtube"]["player_client"],
            ["web", "web_safari", "mweb"],
        )

    def test_download_sync_attempt_falls_back_from_android_vr_to_web_profile(self):
        fetcher = AutoModFetcher("inst-download-quality-fallback")
        output_dir = os.path.join(self.tempdir.name, "downloads")
        webm_path = os.path.join(output_dir, "vid1.webm")
        mp4_path = os.path.join(output_dir, "vid1.mp4")

        first_ctx = self._mock_ydl_context(error=Exception("Requested format is not available"))
        second_ctx = self._mock_ydl_context(
            info={"id": "vid1", "title": "Demo", "ext": "webm", "height": 1080, "format_id": "137+140"},
            filename=webm_path,
        )

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.dict(os.environ, {"YTDLP_HIGH_QUALITY_FIRST": "1"}, clear=True), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "", "source": "none", "available": False, "path_label": "none", "issues": []}), \
             patch("yt_dlp.YoutubeDL", side_effect=[first_ctx, second_ctx]) as mock_ydl, \
             patch("src.agent.auto_mod_fetcher.ResilientFS.exists", side_effect=lambda path: str(path) == mp4_path):
            result = fetcher._download_sync_attempt("https://www.youtube.com/watch?v=vid1", output_dir)

        self.assertEqual(result, mp4_path)
        self.assertEqual(mock_ydl.call_count, 2)
        self.assertIn("format_unavailable", "\n".join(captured.output))

        first_opts = mock_ydl.call_args_list[0].args[0]
        second_opts = mock_ydl.call_args_list[1].args[0]
        self.assertEqual(first_opts["extractor_args"]["youtube"]["player_client"], ["android_vr", "android", "mweb"])
        self.assertEqual(second_opts["extractor_args"]["youtube"]["player_client"], ["web", "android", "mweb"])

    def test_download_sync_attempt_without_ffmpeg_keeps_single_compatibility_profile(self):
        fetcher = AutoModFetcher("inst-download-no-ffmpeg")
        output_dir = os.path.join(self.tempdir.name, "downloads_no_ffmpeg")
        mp4_path = os.path.join(output_dir, "vid2.mp4")
        only_ctx = self._mock_ydl_context(
            info={"id": "vid2", "title": "Demo", "ext": "mp4", "height": 720, "format_id": "22"},
            filename=mp4_path,
        )

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.dict(os.environ, {"YTDLP_HIGH_QUALITY_FIRST": "1"}, clear=True), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", side_effect=RuntimeError("missing ffmpeg")), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "", "source": "none", "available": False, "path_label": "none", "issues": []}), \
             patch("yt_dlp.YoutubeDL", return_value=only_ctx) as mock_ydl, \
             patch("src.agent.auto_mod_fetcher.ResilientFS.exists", side_effect=lambda path: str(path) == mp4_path):
            result = fetcher._download_sync_attempt("https://www.youtube.com/watch?v=vid2", output_dir)

        self.assertEqual(result, mp4_path)
        self.assertEqual(mock_ydl.call_count, 1)
        self.assertIn("single compatibility profile only", "\n".join(captured.output))
        used_opts = mock_ydl.call_args.args[0]
        self.assertEqual(used_opts["format"], "best[ext=mp4]/best/b")
        self.assertEqual(used_opts["extractor_args"]["youtube"]["player_client"], ["ios", "android", "mweb"])
        self.assertEqual(used_opts["postprocessors"], [])

    def test_normalize_youtube_watch_url_converts_shorts_to_canonical_watch_url(self):
        normalized = _normalize_youtube_watch_url("https://www.youtube.com/shorts/abc123xyz89?feature=share")
        self.assertEqual(normalized, "https://www.youtube.com/watch?v=abc123xyz89")

    def test_download_sync_attempt_normalizes_shorts_url_before_ytdlp(self):
        fetcher = AutoModFetcher("inst-download-normalize-shorts")
        output_dir = os.path.join(self.tempdir.name, "downloads_normalized")
        mp4_path = os.path.join(output_dir, "abc123xyz89.mp4")
        only_ctx = self._mock_ydl_context(
            info={"id": "abc123xyz89", "title": "Demo", "ext": "mp4", "height": 720, "format_id": "22"},
            filename=mp4_path,
        )

        with patch.dict(os.environ, {"YTDLP_HIGH_QUALITY_FIRST": "1"}, clear=True), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", side_effect=RuntimeError("missing ffmpeg")), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "", "source": "none", "available": False, "path_label": "none", "issues": []}), \
             patch("yt_dlp.YoutubeDL", return_value=only_ctx), \
             patch("src.agent.auto_mod_fetcher.ResilientFS.exists", side_effect=lambda path: str(path) == mp4_path):
            result = fetcher._download_sync_attempt("https://www.youtube.com/shorts/abc123xyz89?feature=share", output_dir)

        self.assertEqual(result, mp4_path)
        only_ctx.__enter__.return_value.extract_info.assert_called_once_with(
            "https://www.youtube.com/watch?v=abc123xyz89",
            download=True,
        )

    def test_build_yt_opts_with_cookies_leaves_authenticated_client_selection_to_yt_dlp(self):
        with patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "env_path:YTDLP_COOKIES_PATH", "available": True, "path_label": "cookies.txt", "issues": []}), \
             patch("src.agent.config.load_config", return_value=SimpleNamespace(YTDLP_FORCE_IPV4=True)):
            opts = _build_yt_opts()

        self.assertEqual(opts["cookiefile"], "/tmp/cookies.txt")
        self.assertEqual(opts["extractor_args"]["youtube"], {})

    def test_download_sync_logs_botcheck_reason_before_cobalt_fallback(self):
        fetcher = AutoModFetcher("inst-download-botcheck")
        output_dir = os.path.join(self.tempdir.name, "downloads_botcheck")

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.object(fetcher, "_download_sync_attempt", side_effect=Exception("Sign in to confirm you're not a bot")), \
             patch.object(fetcher, "_download_via_cobalt", return_value=None), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "project_file:www.youtube.com_cookies.txt", "available": True, "path_label": "cookies.txt", "issues": []}):
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid3", output_dir)

        self.assertIsNone(result)
        combined_logs = "\n".join(captured.output)
        self.assertIn("youtube_botcheck", combined_logs)
        self.assertIn("Attempting fallback via Cobalt API", combined_logs)

    def test_download_sync_prefers_remote_worker_on_render_when_configured(self):
        fetcher = AutoModFetcher("inst-download-remote-first")
        output_dir = os.path.join(self.tempdir.name, "downloads_remote_first")
        expected = os.path.join(output_dir, "vid6_remote.mp4")

        with patch.dict(os.environ, {
            "RENDER": "true",
            "DOWNLOADER_WORKER_URL": "https://worker.example.com",
        }, clear=True), \
             patch.object(fetcher, "_download_via_remote_worker", return_value=expected) as remote_mock, \
             patch.object(fetcher, "_download_sync_attempt") as local_mock:
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid6", output_dir)

        self.assertEqual(result, expected)
        remote_mock.assert_called_once_with(
            "https://www.youtube.com/watch?v=vid6",
            output_dir,
            max_duration=None,
        )
        local_mock.assert_not_called()

    def test_download_sync_botcheck_tries_remote_worker_before_cobalt_when_not_preferred(self):
        fetcher = AutoModFetcher("inst-download-remote-botcheck")
        output_dir = os.path.join(self.tempdir.name, "downloads_remote_botcheck")
        expected = os.path.join(output_dir, "vid7_remote.mp4")

        with patch.dict(os.environ, {
            "DOWNLOADER_WORKER_URL": "https://worker.example.com",
            "DOWNLOADER_WORKER_PREFER_REMOTE": "0",
        }, clear=True), \
             patch.object(fetcher, "_download_sync_attempt", side_effect=Exception("Sign in to confirm you're not a bot")), \
             patch.object(fetcher, "_download_via_remote_worker", return_value=expected) as remote_mock, \
             patch.object(fetcher, "_download_via_cobalt") as cobalt_mock, \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "project_file:www.youtube.com_cookies.txt", "available": True, "path_label": "cookies.txt", "issues": []}):
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid7", output_dir)

        self.assertEqual(result, expected)
        remote_mock.assert_called_once_with(
            "https://www.youtube.com/watch?v=vid7",
            output_dir,
            max_duration=None,
        )
        cobalt_mock.assert_not_called()

    def test_download_sync_botcheck_falls_back_to_cobalt_after_remote_worker_failure(self):
        fetcher = AutoModFetcher("inst-download-remote-then-cobalt")
        output_dir = os.path.join(self.tempdir.name, "downloads_remote_then_cobalt")
        expected = os.path.join(output_dir, "vid8_cobalt.mp4")

        with patch.dict(os.environ, {
            "DOWNLOADER_WORKER_URL": "https://worker.example.com",
            "DOWNLOADER_WORKER_PREFER_REMOTE": "0",
        }, clear=True), \
             patch.object(fetcher, "_download_sync_attempt", side_effect=Exception("Sign in to confirm you're not a bot")), \
             patch.object(fetcher, "_download_via_remote_worker", return_value=None) as remote_mock, \
             patch.object(fetcher, "_download_via_cobalt", return_value=expected) as cobalt_mock, \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "project_file:www.youtube.com_cookies.txt", "available": True, "path_label": "cookies.txt", "issues": []}):
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid8", output_dir)

        self.assertEqual(result, expected)
        remote_mock.assert_called_once_with(
            "https://www.youtube.com/watch?v=vid8",
            output_dir,
            max_duration=None,
        )
        cobalt_mock.assert_called_once_with("https://www.youtube.com/watch?v=vid8", output_dir)

    def test_resolve_any_cookiefile_writes_env_cookie_text_to_runtime_file(self):
        cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\tdemo\n"

        with patch.dict(os.environ, {"YTDLP_COOKIES_TEXT": cookie_text}, clear=True), \
             patch("src.agent.auto_mod_fetcher.project_root", self.tempdir.name):
            resolved = _resolve_any_cookiefile()

        self.assertTrue(resolved.endswith("yt_dlp_cookies.txt"))
        self.assertTrue(os.path.isfile(resolved))
        with open(resolved, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), cookie_text.strip())

    def test_resolve_cookiefile_details_tracks_missing_env_path_and_falls_back_to_project_file(self):
        cookie_path = os.path.join(self.tempdir.name, "www.youtube.com_cookies.txt")
        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")

        with patch.dict(os.environ, {"YTDLP_COOKIES_PATH": "missing/cookies.txt"}, clear=True), \
             patch("src.agent.auto_mod_fetcher.project_root", self.tempdir.name):
            details = _resolve_cookiefile_details()

        self.assertTrue(details["available"])
        self.assertEqual(details["source"], "project_file:www.youtube.com_cookies.txt")
        self.assertIn("YTDLP_COOKIES_PATH=missing", details["issues"][0])

    def test_build_ytdlp_runtime_diagnostics_redacts_proxy_credentials(self):
        cookies_info = {
            "path": "/tmp/yt_dlp_cookies.txt",
            "source": "env_text:YTDLP_COOKIES_TEXT",
            "available": True,
            "path_label": ".data/yt_dlp_cookies.txt",
            "issues": [],
        }

        with patch.dict(os.environ, {
            "RENDER": "true",
            "YTDLP_PROXY": "http://user:secret@example.com:8080",
            "YOUTUBE_PO_TOKEN": "demo-token",
            "YTDLP_IMPERSONATE": "chrome",
            "COBALT_API_URL": "https://api.cobalt.tools/",
            "COBALT_API_TOKEN": "secret-cobalt",
            "DOWNLOADER_WORKER_URL": "https://worker.example.com",
        }, clear=True):
            text = _build_ytdlp_runtime_diagnostics("unit_test", cookies_info=cookies_info, profile_labels=["authenticated_default"])

        self.assertIn("runtime=render", text)
        self.assertIn("proxy=on:http", text)
        self.assertNotIn("secret@example.com", text)
        self.assertNotIn("secret-cobalt", text)
        self.assertIn("cookies=env_text:YTDLP_COOKIES_TEXT:.data/yt_dlp_cookies.txt", text)
        self.assertIn("cobalt_auth_scheme=api-key", text)
        self.assertIn("remote_worker=on:worker.example.com", text)
        self.assertIn("remote_prefer=yes", text)
        self.assertIn("profiles=authenticated_default", text)


    def test_resolve_cobalt_api_settings_distinguishes_jwt_from_api_key(self):
        with patch.dict(os.environ, {
            "COBALT_API_URL": "https://cobalt.example.com/",
            "COBALT_API_TOKEN": "api-key-demo",
        }, clear=True):
            api_url, auth_scheme, auth_token = _resolve_cobalt_api_settings()

        self.assertEqual(api_url, "https://cobalt.example.com/")
        self.assertEqual(auth_scheme, "Api-Key")
        self.assertEqual(auth_token, "api-key-demo")

        with patch.dict(os.environ, {
            "COBALT_API_URL": "https://cobalt.example.com/",
            "COBALT_API_JWT": "jwt-demo",
            "COBALT_API_TOKEN": "api-key-demo",
        }, clear=True):
            api_url, auth_scheme, auth_token = _resolve_cobalt_api_settings()

        self.assertEqual(api_url, "https://cobalt.example.com/")
        self.assertEqual(auth_scheme, "Bearer")
        self.assertEqual(auth_token, "jwt-demo")

    def test_download_sync_skips_cobalt_when_fallback_was_disabled(self):
        fetcher = AutoModFetcher("inst-download-botcheck-disabled")
        output_dir = os.path.join(self.tempdir.name, "downloads_botcheck_disabled")

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.object(fetcher, "_download_sync_attempt", side_effect=Exception("Sign in to confirm you're not a bot")), \
             patch.object(fetcher, "_download_via_cobalt") as cobalt_mock, \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", True), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "project_file:www.youtube.com_cookies.txt", "available": True, "path_label": "cookies.txt", "issues": []}):
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid4", output_dir)

        self.assertIsNone(result)
        cobalt_mock.assert_not_called()
        combined_logs = "\n".join(captured.output)
        self.assertNotIn("Attempting fallback via Cobalt API", combined_logs)

    def test_download_via_cobalt_disables_future_attempts_after_jwt_missing_response(self):
        fetcher = AutoModFetcher("inst-download-cobalt-jwt")
        output_dir = os.path.join(self.tempdir.name, "downloads_cobalt_jwt")
        response = SimpleNamespace(
            status_code=400,
            text='{"status":"error","error":{"code":"error.api.auth.jwt.missing"}}',
        )

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.dict(os.environ, {"COBALT_API_URL": "https://jwt-required.example/"}, clear=True), \
             patch("httpx.post", return_value=response) as post_mock, \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", False), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED_HOSTS", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_DISABLE_HINT_SHOWN", False):
            first = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)
            second = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(post_mock.call_count, 1)
        self.assertIn("requires Bearer JWT auth", "\n".join(captured.output))

    def test_download_via_cobalt_uses_api_key_authorization_for_token_env(self):
        fetcher = AutoModFetcher("inst-download-cobalt-api-key")
        output_dir = os.path.join(self.tempdir.name, "downloads_cobalt_api_key")
        response = SimpleNamespace(status_code=500, text="server error")

        with patch.dict(os.environ, {
            "COBALT_API_URL": "https://cobalt.example.com/",
            "COBALT_API_TOKEN": "api-key-demo",
        }, clear=True), \
             patch("httpx.post", return_value=response) as post_mock, \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", False):
            result = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)

        self.assertIsNone(result)
        headers = post_mock.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Api-Key api-key-demo")

    def test_download_via_cobalt_403_sets_host_scoped_cooldown_not_global_disable(self):
        fetcher = AutoModFetcher("inst-download-cobalt-cooldown")
        output_dir = os.path.join(self.tempdir.name, "downloads_cobalt_cooldown")
        response = SimpleNamespace(status_code=403, text="forbidden")

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch("httpx.post", return_value=response) as post_mock, \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", False), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED_HOSTS", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST", {}):
            with patch.dict(os.environ, {"COBALT_API_URL": "https://host-one.example/"}, clear=True):
                first = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)
                second = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)

            with patch.dict(os.environ, {"COBALT_API_URL": "https://host-two.example/"}, clear=True):
                third = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid5", output_dir)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        self.assertEqual(post_mock.call_count, 2)
        combined_logs = "\n".join(captured.output)
        self.assertIn("temporarily disabled for host=host-one.example", combined_logs)

    def test_download_sync_skips_cobalt_when_current_host_is_in_cooldown(self):
        fetcher = AutoModFetcher("inst-download-cobalt-cooldown-sync")
        output_dir = os.path.join(self.tempdir.name, "downloads_cobalt_cooldown_sync")
        future_ts = datetime.now(timezone.utc).timestamp() + 120

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.dict(os.environ, {"COBALT_API_URL": "https://cooldown.example/"}, clear=True), \
             patch.object(fetcher, "_download_sync_attempt", side_effect=Exception("Sign in to confirm you're not a bot")), \
             patch.object(fetcher, "_download_via_cobalt") as cobalt_mock, \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", False), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED_HOSTS", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST", {"cooldown.example": future_ts}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST", {"cooldown.example": "temporary challenge"}), \
             patch("src.agent.auto_mod_fetcher._resolve_cookiefile_details", return_value={"path": "/tmp/cookies.txt", "source": "project_file:www.youtube.com_cookies.txt", "available": True, "path_label": "cookies.txt", "issues": []}):
            result = fetcher._download_sync("https://www.youtube.com/watch?v=vid4", output_dir)

        self.assertIsNone(result)
        cobalt_mock.assert_not_called()
        self.assertIn("Skipping Cobalt API fallback for host=cooldown.example", "\n".join(captured.output))

    def test_download_via_cobalt_logs_missing_auth_without_leaking_secret(self):
        fetcher = AutoModFetcher("inst-download-cobalt-missing-auth")
        output_dir = os.path.join(self.tempdir.name, "downloads_cobalt_missing_auth")
        response = SimpleNamespace(
            status_code=400,
            text='{"status":"error","error":{"code":"error.api.auth.jwt.missing"}}',
        )

        with self.assertLogs("src.agent.auto_mod_fetcher", level="WARNING") as captured, \
             patch.dict(os.environ, {"COBALT_API_URL": "https://api.cobalt.tools/"}, clear=True), \
             patch("httpx.post", return_value=response), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED", False), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_DISABLED_HOSTS", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_UNTIL_BY_HOST", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_FALLBACK_COOLDOWN_REASON_BY_HOST", {}), \
             patch("src.agent.auto_mod_fetcher._COBALT_DISABLE_HINT_SHOWN", False), \
             patch("src.agent.auto_mod_fetcher._COBALT_MISSING_AUTH_HINT_SHOWN", False):
            result = fetcher._download_via_cobalt("https://www.youtube.com/watch?v=vid6", output_dir)

        self.assertIsNone(result)
        combined_logs = "\n".join(captured.output)
        self.assertIn("without auth token", combined_logs)
        self.assertIn("api.cobalt.tools", combined_logs)

    def test_infer_processing_video_type_prefers_shorts_url_when_duration_missing(self):
        video = {
            "id": "vid-shorts-url",
            "url": "https://youtube.com/shorts/abc123",
            "duration": None,
        }

        inferred = _infer_processing_video_type(video, "youtube_any", "https://youtube.com/@demo")

        self.assertEqual(inferred, "shorts")

    def test_build_hashtag_only_upload_metadata_keeps_hashtag_only_output_without_forced_brand_or_generic_tags(self):
        final_title, final_description, tags = _build_hashtag_only_upload_metadata(
            {
                "title": "Amazing Mod Clip",
                "description": "Watch now! #Minecraft #Mods #Modetaris #سلام_عليكم",
                "tags": ["best mod", "viral short"],
            },
            source_title="Amazing Mod Clip",
            source_name="ModeTaris Source",
            content_type="minecraft_mods",
            target_lang="en",
            is_shorts=True,
            source_description="Plain text source description #gaming",
            source_settings={
                "extra_description": {
                    "enabled": True,
                    "texts": ["Subscribe now #ExtraTag"],
                    "selection_mode": "fixed",
                }
            },
        )

        self.assertTrue(final_title)
        self.assertTrue(all(part.startswith("#") for part in final_title.split()))
        self.assertTrue(all(part.startswith("#") for part in final_description.split()))
        self.assertNotIn("Watch now", final_description)
        self.assertNotIn("#Modetaris", final_description.split())
        self.assertNotIn("#modetaris", final_description.split())
        self.assertNotIn("#سلام_عليكم", final_description.split())
        self.assertNotIn("#gaming", final_description.split())
        self.assertIn("#shorts", final_title.split())
        self.assertTrue(tags)
        self.assertTrue(all(not part.startswith("#") for part in tags))

    def test_extract_source_metadata_context_prefers_specific_source_terms(self):
        context = extract_source_metadata_context(
            hint_title="Minecraft secret house with hidden redstone door",
            source_description="Survival build tutorial for an underground base entrance with a trapdoor reveal.",
            lang="en",
            content_type="minecraft_builds",
            source_name="ModeTaris Source",
        )

        hashtags = {tag.lower() for tag in context.get("hashtags", [])}
        joined = " ".join(hashtags)
        self.assertTrue(any(term in joined for term in ["redstone", "secret", "trapdoor", "house"]))
        self.assertNotIn("#gaming", hashtags)

    def test_extract_source_metadata_context_blocks_generic_greetings_when_not_topic(self):
        context = extract_source_metadata_context(
            hint_title="Minecraft hidden redstone door tutorial",
            source_description="السلام عليكم يا شباب اليوم عندنا شرح سريع #سلام_عليكم #ماين_كرافت #ريدستون",
            lang="ar",
            content_type="minecraft_builds",
            source_name="Builder Source",
        )

        combined = " ".join(list(context.get("hashtags", [])) + list(context.get("keywords", []))).lower()
        self.assertNotIn("سلام", combined)
        self.assertNotIn("عليكم", combined)
        self.assertTrue(any(term in combined for term in ["ريدستون", "redstone", "ماين", "كرافت"]))

    def test_extract_source_metadata_context_allows_greeting_when_it_is_real_topic(self):
        context = extract_source_metadata_context(
            hint_title="معنى السلام عليكم ولماذا نقولها",
            source_description="شرح معنى السلام عليكم واستخدامها الصحيح في التحية.",
            lang="ar",
            content_type="language_explainer",
            source_name="Arabic Source",
        )

        combined = " ".join(list(context.get("hashtags", [])) + list(context.get("keywords", []))).lower()
        self.assertIn("سلام", combined)
        self.assertIn("عليكم", combined)

    def test_build_hashtag_only_upload_metadata_avoids_unrelated_gaming_tags_for_non_gaming_source(self):
        final_title, final_description, tags = _build_hashtag_only_upload_metadata(
            {
                "title": "Soft chocolate cookies recipe",
                "description": "#dessert #cookies #سلام_عليكم #Modetaris #gaming",
                "tags": ["chocolate cookies", "easy baking"],
                "hashtags": ["#dessert", "#cookies"],
                "source_context": {
                    "original_title": "Soft chocolate cookies recipe",
                    "original_description": "Easy oven dessert with cocoa butter and baking tips.",
                    "duration": 42,
                },
            },
            source_title="Soft chocolate cookies recipe",
            source_name="Kitchen Source",
            content_type="cooking_tips",
            target_lang="en",
            is_shorts=True,
            source_description="Easy oven dessert with cocoa butter and baking tips.",
        )

        combined = f"{final_title} {final_description}".lower()
        self.assertNotIn("#gaming", combined)
        self.assertNotIn("#minecraft", combined)
        self.assertNotIn("#mods", combined)
        self.assertNotIn("#modetaris", combined)
        self.assertNotIn("#سلام_عليكم", combined)
        self.assertTrue(any(term in combined for term in ["cookie", "cookies", "dessert", "cocoa", "baking"]))
        self.assertIn("#shorts", final_title.split())
        self.assertTrue(all(not part.startswith("#") for part in tags))

    def test_runtime_dir_keepalive_creates_and_releases_marker(self):
        output_dir = os.path.join(self.tempdir.name, "auto_mod_downloads")

        resolved, marker_path = _create_runtime_dir_keepalive(output_dir)
        self.assertEqual(resolved, os.path.abspath(output_dir))
        self.assertTrue(os.path.isdir(resolved))
        self.assertTrue(os.path.isfile(marker_path))

        _release_runtime_dir_keepalive(marker_path)
        self.assertFalse(os.path.exists(marker_path))

    def test_disk_cleanup_keeps_auto_mod_download_dir(self):
        temp_root = os.path.join(self.tempdir.name, ".temp")
        protected_dir = os.path.join(temp_root, "auto_mod_downloads")
        os.makedirs(protected_dir, exist_ok=True)

        with patch("src.agent.disk_guard.CLEANUP_DIRS", [temp_root]), \
             patch("src.agent.disk_guard._protected_empty_dirs", return_value={os.path.normcase(os.path.abspath(protected_dir))}):
            cleanup_old_files(max_age_hours=1)

        self.assertTrue(os.path.isdir(protected_dir))

    def test_disk_cleanup_keeps_auto_mod_shorts_output_dir(self):
        output_root = os.path.join(self.tempdir.name, ".output")
        protected_dir = os.path.join(output_root, "auto_mod_shorts")
        os.makedirs(protected_dir, exist_ok=True)

        with patch("src.agent.disk_guard._PROJECT_ROOT", self.tempdir.name), \
             patch("src.agent.disk_guard.CLEANUP_DIRS", [output_root]):
            cleanup_old_files(max_age_hours=1)

        self.assertTrue(os.path.isdir(protected_dir))

    def test_encode_final_shorts_recreates_missing_output_dir_before_ffmpeg(self):
        processor = ModVideoProcessor(temp_dir=self.tempdir.name)
        input_path = os.path.join(self.tempdir.name, "input.mp4")
        output_path = os.path.join(self.tempdir.name, ".output", "auto_mod_shorts", "vid_mod.mp4")
        Path(input_path).write_bytes(b"input-video")

        def fake_run(cmd, capture_output, timeout):
            out_file = cmd[-1]
            self.assertTrue(os.path.isdir(os.path.dirname(out_file)))
            Path(out_file).write_bytes(b"encoded-video")
            return SimpleNamespace(returncode=0, stderr=b"")

        with patch("src.agent.mod_video_processor.ffmpeg_bin", return_value="ffmpeg"), \
             patch("src.agent.mod_video_processor._get_shorts_encoder_settings", return_value={"encoder": "libx264", "preset": "medium", "crf": "20", "threads": 1, "extra_args": [], "audio_bitrate": "128k", "audio_sample_rate": 48000}), \
             patch.object(processor, "_has_audio", return_value=False), \
             patch.object(processor, "_validate_video_file", return_value=None), \
             patch("src.agent.mod_video_processor.subprocess.run", side_effect=fake_run):
            ok = processor._encode_final_shorts(input_path, output_path, target_fps=30.0)

        self.assertTrue(ok)

    def test_process_video_forwards_video_effects_to_mod_video_processor(self):
        fetcher = AutoModFetcher("inst-process-effects")
        input_path = os.path.join(self.tempdir.name, "input-effects.mp4")
        Path(input_path).write_bytes(b"input-video")
        expected_output = os.path.join(self.tempdir.name, "processed-effects.mp4")
        effects = {
            "intro": {"enabled": True, "type": "blur", "duration": 1.0},
            "outro": {"enabled": True, "type": "black_blur", "duration": 2.0},
        }
        processor_instance = MagicMock()
        processor_instance.process_mod_video.return_value = (expected_output, {"status": "ok"})

        class _FakeLoop:
            async def run_in_executor(self, executor, func):
                return func()

        with patch("src.agent.auto_mod_fetcher.asyncio.get_running_loop", return_value=_FakeLoop()), \
             patch("src.agent.auto_mod_fetcher.ResilientFS.makedirs"), \
             patch("src.agent.mod_video_processor.ModVideoProcessor", return_value=processor_instance):
            out_path = asyncio.run(fetcher.process_video(input_path, "vid-effects", video_type="shorts", video_effects=effects))

        self.assertEqual(out_path, expected_output)
        self.assertEqual(processor_instance.process_mod_video.call_args.kwargs["video_effects"], effects)

    def test_process_mod_video_uses_explicit_source_video_effects_instead_of_legacy_defaults(self):
        processor = ModVideoProcessor(temp_dir=self.tempdir.name)
        input_path = os.path.join(self.tempdir.name, "processor-input.mp4")
        output_dir = os.path.join(self.tempdir.name, "out")
        Path(input_path).write_bytes(b"input-video")
        effects = {
            "intro": {"enabled": True, "type": "blur", "duration": 1.0},
            "outro": {"enabled": True, "type": "black_blur", "duration": 1.5},
        }

        def fake_convert_to_shorts(src, dst, width, height, shorts_format="crop"):
            Path(dst).write_bytes(b"resized-video")

        def fake_apply_effects(src, dst, explicit_effects):
            self.assertEqual(explicit_effects, effects)
            Path(dst).write_bytes(b"effected-video")

        def fake_encode_final(src, dst, target_fps=None):
            Path(dst).write_bytes(b"final-video")
            return True

        with patch.dict(os.environ, {"SHORTS_EFFECTS_APPLY_DURING_PROCESSING": "1"}, clear=False), \
             patch("src.agent.mod_video_processor.ffmpeg_bin", return_value="ffmpeg"), \
             patch("src.agent.mod_video_processor.ffprobe_bin", return_value="ffprobe"), \
             patch.object(processor, "_get_video_duration", return_value=12.0), \
             patch.object(processor, "_get_video_dimensions", return_value=(1920, 1080)), \
             patch.object(processor, "_get_video_fps", return_value=30.0), \
             patch.object(processor, "_convert_to_shorts", side_effect=fake_convert_to_shorts), \
             patch.object(processor, "_apply_configured_intro_outro_effects", side_effect=fake_apply_effects) as apply_effects, \
             patch.object(processor, "add_simple_intro_outro_effects") as legacy_intro_outro, \
             patch.object(processor, "_apply_outro_blur_black") as legacy_outro, \
             patch.object(processor, "_encode_final_shorts", side_effect=fake_encode_final):
            out_path, info = processor.process_mod_video(
                input_video=input_path,
                output_dir=output_dir,
                video_id="vid-explicit-effects",
                trim_start=0.0,
                trim_end=0.0,
                convert_to_shorts=True,
                video_effects=effects,
            )

        self.assertTrue(out_path.endswith("vid-explicit-effects_mod.mp4"))
        self.assertEqual(info["final_path"], out_path)
        apply_effects.assert_called_once()
        legacy_intro_outro.assert_not_called()
        legacy_outro.assert_not_called()
        self.assertTrue(os.path.isfile(out_path))

    def test_run_cycle_pauses_when_any_raw_review_is_pending(self):
        fetcher = AutoModFetcher("inst-pending-pause")
        notifications = []

        async def notify(msg):
            notifications.append(msg)

        with patch.object(fetcher.db, "get_config", return_value={"auto_fetch_enabled": True}), \
             patch.object(fetcher.db, "get_all_schedules", return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}]), \
             patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=True), \
             patch.object(fetcher.db, "get_sources") as get_sources:
            result = asyncio.run(fetcher.run_cycle(notify_func=notify, force=True))

        self.assertEqual(result.get("status"), "waiting_raw_review")
        self.assertEqual(result.get("waiting_raw_review"), 1)
        get_sources.assert_not_called()
        self.assertTrue(any("مراجعة فيديو خام" in msg for msg in notifications))

    def test_run_cycle_does_not_continue_to_next_schedule_after_requesting_raw_review(self):
        fetcher = AutoModFetcher("inst-raw-review-stop")
        seen_channels = []

        schedules = [
            {"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"},
            {"enabled": True, "channel_id": "ch-2", "content_type": "minecraft_mods"},
        ]
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://example.com/source",
            "platform": "youtube_shorts",
            "settings": {"require_raw_review": True},
        }
        video = {"id": "vid-1", "title": "Video 1", "url": "https://example.com/video.mp4", "duration": 30}

        def get_sources(channel_id, content_type):
            seen_channels.append(channel_id)
            return [source]

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=schedules)
        fetcher.db.get_sources = MagicMock(side_effect=get_sources)
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=os.path.join(self.tempdir.name, "raw.mp4"))), \
             patch("src.bot.raw_review.request_raw_video_review", AsyncMock(return_value=True)):
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 0)
        self.assertEqual(result.get("status"), "waiting_raw_review")
        self.assertEqual(result.get("waiting_raw_review"), 1)
        self.assertEqual(result.get("skipped"), 0)
        self.assertEqual(seen_channels, ["ch-1"])
        fetcher.db.release_video_processing.assert_called_once_with("vid-1", "ch-1")

    def test_run_cycle_processes_approved_raw_review_video_without_re_requesting_review(self):
        fetcher = AutoModFetcher("inst-approved-review")
        cfg = SimpleNamespace(
            TELEGRAM_DB_PATH=os.path.join(self.tempdir.name, "tg_state.db"),
            RUN_DAILY_AT="10:00",
            RUN_ONLY_ON_WIFI=False,
            RUN_ONLY_WHILE_CHARGING=False,
            AUDIO_MODE="light",
            TELEGRAM_ALLOWED_USER_IDS=[12345],
        )
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://example.com/source",
            "platform": "youtube_shorts",
            "settings": {"require_raw_review": True},
        }
        video = {"id": "vid-1", "title": "Video 1", "url": "https://example.com/video.mp4", "duration": 30}
        raw_path = os.path.join(self.tempdir.name, "raw-approved.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-approved.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        detect_patch = patch("src.bot.persistence._detect_and_add_publish_channels", lambda state, cfg: None)
        sqlite_default_patch = patch("src.agent.sqlite_storage._DEFAULT_DB_PATH", Path(self.tempdir.name) / "default_state.db")
        detect_patch.start()
        sqlite_default_patch.start()
        persistence._CACHED_STATE = None
        persistence._CACHED_STATE_TS = 0.0
        persistence._CACHED_STATE_KEY = None

        try:
            persistence.set_pending_raw_review(
                "src-1",
                {"token": "tok-approved", "video_id": "vid-1", "video_title": "Video 1", "source_name": "Demo Source"},
                cfg=cfg,
            )
            persistence.approve_pending_raw_review("tok-approved", decided_by=12345, cfg=cfg)

            with patch("src.bot.persistence.load_config", return_value=cfg), \
                 patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
                 patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
                 patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
                 patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)), \
                 patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/demo")), \
                 patch("src.bot.raw_review.request_raw_video_review", AsyncMock(return_value=True)) as request_review:
                result = asyncio.run(fetcher.run_cycle(force=True))
        finally:
            detect_patch.stop()
            sqlite_default_patch.stop()
            persistence._CACHED_STATE = None
            persistence._CACHED_STATE_TS = 0.0
            persistence._CACHED_STATE_KEY = None

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        request_review.assert_not_awaited()
        fetcher.db.mark_video_published.assert_called_once_with("vid-1", "ch-1", "https://youtu.be/demo")
        fetcher.db.release_video_processing.assert_not_called()

    def test_run_cycle_reuses_preserved_raw_artifact_after_approval(self):
        fetcher = AutoModFetcher("inst-approved-raw-reuse")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://example.com/source",
            "platform": "youtube_shorts",
            "settings": {"require_raw_review": True},
        }
        raw_path = os.path.join(self.tempdir.name, "raw-preserved.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-approved.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=True), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock()) as fetch_videos, \
             patch.object(fetcher, "download_video", AsyncMock()) as download_video, \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)) as process_video, \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/reused-raw")):
            result = asyncio.run(
                fetcher.run_cycle(
                    force=True,
                    target_channel_id="ch-1",
                    target_content_type="minecraft_mods",
                    target_source_id="src-1",
                    target_video_id="vid-approved",
                    target_video_url="https://example.com/approved.mp4",
                    target_video_title="Approved Video",
                    target_video_type="shorts",
                    target_raw_video_path=raw_path,
                )
            )

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        fetch_videos.assert_not_awaited()
        download_video.assert_not_awaited()
        self.assertEqual(process_video.await_args.args[0], raw_path)

    def test_run_cycle_reuses_existing_processed_artifact_after_approval(self):
        fetcher = AutoModFetcher("inst-approved-processed-reuse")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://example.com/source",
            "platform": "youtube_shorts",
            "settings": {"require_raw_review": True},
        }
        processed_path = os.path.join(self.tempdir.name, "processed-ready.mp4")
        Path(processed_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=True), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock()) as fetch_videos, \
             patch.object(fetcher, "download_video", AsyncMock()) as download_video, \
             patch.object(fetcher, "process_video", AsyncMock()) as process_video, \
             patch.object(fetcher, "_get_reusable_processed_output_path", return_value=processed_path), \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/reused-processed")):
            result = asyncio.run(
                fetcher.run_cycle(
                    force=True,
                    target_channel_id="ch-1",
                    target_content_type="minecraft_mods",
                    target_source_id="src-1",
                    target_video_id="vid-approved",
                    target_video_url="https://example.com/approved.mp4",
                    target_video_title="Approved Video",
                    target_video_type="shorts",
                    target_raw_video_path=os.path.join(self.tempdir.name, "missing-raw.mp4"),
                )
            )

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        fetch_videos.assert_not_awaited()
        download_video.assert_not_awaited()
        process_video.assert_not_awaited()

    def test_run_cycle_treats_shorts_url_without_duration_as_shorts(self):
        fetcher = AutoModFetcher("inst-shorts-url")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_any",
            "settings": {},
        }
        video = {
            "id": "vid-shorts-url",
            "title": "Short URL Video",
            "url": "https://youtube.com/shorts/abc123",
            "duration": None,
            "description": "desc",
        }
        raw_path = os.path.join(self.tempdir.name, "raw-shorts-url.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-shorts-url.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)) as process_video, \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/shorts-demo")) as upload_video:
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        self.assertEqual(process_video.await_args.kwargs["video_type"], "shorts")
        self.assertTrue(upload_video.await_args.args[5])

    def test_run_cycle_applies_source_tail_trim_before_processing(self):
        fetcher = AutoModFetcher("inst-tail-trim")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_shorts",
            "settings": {"tail_trim": {"enabled": True, "seconds": 2.5}},
        }
        video = {
            "id": "vid-tail-trim",
            "title": "Trimmed Video",
            "url": "https://youtube.com/shorts/trim123",
            "duration": 30,
            "description": "desc",
        }
        raw_path = os.path.join(self.tempdir.name, "raw-tail-trim.mp4")
        trimmed_path = os.path.join(self.tempdir.name, "raw-tail-trim.trimmed.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-tail-trim.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(trimmed_path).write_bytes(b"trimmed-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "_apply_source_tail_trim", AsyncMock(return_value=trimmed_path)) as trim_video, \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)) as process_video, \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/trimmed")):
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        trim_video.assert_awaited_once_with(raw_path, "vid-tail-trim", 2.5)
        self.assertEqual(process_video.await_args.args[0], trimmed_path)
        self.assertEqual(process_video.await_args.kwargs["trim_end"], 0.0)

    def test_run_cycle_forwards_source_video_effects_to_processing(self):
        fetcher = AutoModFetcher("inst-video-effects")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_shorts",
            "settings": {
                "video_effects": {
                    "intro": {"enabled": True, "type": "blur", "duration": 1.5},
                    "outro": {"enabled": True, "type": "black_blur", "duration": 2.0},
                }
            },
        }
        video = {
            "id": "vid-effects",
            "title": "Effects Video",
            "url": "https://youtube.com/shorts/fx123",
            "duration": 30,
            "description": "desc",
        }
        raw_path = os.path.join(self.tempdir.name, "raw-effects.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-effects.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)) as process_video, \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/effects")):
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        self.assertEqual(process_video.await_args.kwargs["video_effects"], {
            "intro": {"enabled": True, "type": "blur", "duration": 1.5},
            "outro": {"enabled": True, "type": "black_blur", "duration": 2.0},
        })

    def test_run_cycle_forwards_source_overlay_animations_to_overlay_renderer(self):
        fetcher = AutoModFetcher("inst-overlay-animations")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_shorts",
            "settings": {
                "shorts_overlay": {
                    "enabled": True,
                    "texts": ["Overlay text"],
                    "selection_mode": "fixed",
                    "timing": "full",
                    "duration": 2.0,
                    "screen_position": "top",
                    "intro_animation": {"enabled": True, "type": "blur", "duration": 0.8},
                    "outro_animation": {"enabled": True, "type": "fade", "duration": 0.5},
                }
            },
        }
        video = {
            "id": "vid-overlay-anim",
            "title": "Overlay Video",
            "url": "https://youtube.com/shorts/ov123",
            "duration": 30,
            "description": "desc",
        }
        raw_path = os.path.join(self.tempdir.name, "raw-overlay.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-overlay.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()
        overlay_processor = MagicMock()

        def fake_overlay(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"overlay-video")

        overlay_processor.add_custom_overlay_text.side_effect = fake_overlay

        class _FakeLoop:
            async def run_in_executor(self, executor, func):
                return func()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("src.agent.auto_mod_fetcher.asyncio.get_running_loop", return_value=_FakeLoop()), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)), \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/overlay")), \
             patch("src.agent.mod_video_processor.ModVideoProcessor", return_value=overlay_processor):
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        overlay_kwargs = overlay_processor.add_custom_overlay_text.call_args.kwargs
        self.assertEqual(overlay_kwargs["intro_animation"], {"enabled": True, "type": "blur", "duration": 0.8})
        self.assertEqual(overlay_kwargs["outro_animation"], {"enabled": True, "type": "fade", "duration": 0.5})
        self.assertTrue(overlay_kwargs["output_path"].endswith("vid-overlay-anim_overlay.mp4"))

    def test_run_cycle_forwards_source_facecam_to_renderer(self):
        fetcher = AutoModFetcher("inst-facecam")
        clip_path = os.path.join(self.tempdir.name, "facecam.mp4")
        raw_path = os.path.join(self.tempdir.name, "raw-facecam.mp4")
        processed_path = os.path.join(self.tempdir.name, "processed-facecam.mp4")
        rendered_path = os.path.join(self.tempdir.name, "rendered-facecam.mp4")
        Path(clip_path).write_bytes(b"facecam")
        Path(raw_path).write_bytes(b"raw-video")
        Path(processed_path).write_bytes(b"processed-video")
        Path(rendered_path).write_bytes(b"rendered-video")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_shorts",
            "settings": {
                "facecam": {
                    "enabled": True,
                    "layout": "small_circle_top_right",
                    "position": "top_right",
                    "shape": "circle",
                    "scale": 0.18,
                    "clips": [{"id": "clip-1", "path": clip_path, "enabled": True}],
                }
            },
        }
        video = {
            "id": "vid-facecam",
            "title": "Facecam Video",
            "url": "https://youtube.com/shorts/fc123",
            "duration": 30,
            "description": "desc",
        }

        class _FakeLoop:
            async def run_in_executor(self, executor, func):
                return func()

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("src.agent.auto_mod_fetcher.asyncio.get_running_loop", return_value=_FakeLoop()), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=processed_path)), \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/facecam")), \
             patch("src.agent.config.load_config", return_value={}), \
             patch("src.agent.renderer.render_with_pip", return_value=rendered_path) as render_with_pip:
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        render_kwargs = render_with_pip.call_args.kwargs
        self.assertTrue(render_kwargs["facecam_enabled"])
        self.assertEqual(render_kwargs["facecam_path"], clip_path)
        self.assertEqual(render_kwargs["facecam_layout"], "small_circle_top_right")
        self.assertEqual(render_kwargs["facecam_position"], "top_right")
        self.assertEqual(render_kwargs["facecam_shape"], "circle")
        self.assertEqual(render_kwargs["facecam_scale"], 0.18)

    def test_run_test_render_reuses_pipeline_without_publish_side_effects(self):
        fetcher = AutoModFetcher("inst-preview-render")
        clip_path = os.path.join(self.tempdir.name, "preview-facecam.mp4")
        raw_path = os.path.join(self.tempdir.name, "preview-raw.mp4")
        processed_path = os.path.join(self.tempdir.name, "preview-processed.mp4")
        rendered_path = os.path.join(self.tempdir.name, "preview-rendered.mp4")
        Path(clip_path).write_bytes(b"facecam")
        Path(raw_path).write_bytes(b"raw-video")
        Path(processed_path).write_bytes(b"processed-video")
        Path(rendered_path).write_bytes(b"rendered-video")

        source = {
            "id": "src-1",
            "channel_id": "ch-1",
            "content_type": "minecraft_mods",
            "enabled": False,
            "source_name": "Preview Source",
            "source_url": "https://youtube.com/@preview",
            "platform": "youtube_shorts",
            "settings": {
                "require_raw_review": True,
                "shorts_overlay": {
                    "enabled": True,
                    "texts": ["Preview Overlay"],
                    "selection_mode": "fixed",
                    "timing": "full",
                    "duration": 2.0,
                    "screen_position": "top",
                },
                "video_effects": {
                    "intro": {"enabled": True, "type": "blur", "duration": 1.5},
                },
                "facecam": {
                    "enabled": True,
                    "layout": "small_circle_top_right",
                    "position": "top_right",
                    "shape": "circle",
                    "scale": 0.18,
                    "clips": [{"id": "clip-1", "path": clip_path, "enabled": True}],
                },
            },
        }
        video = {
            "id": "vid-preview",
            "title": "Preview Video",
            "url": "https://youtube.com/shorts/preview123",
            "duration": 30,
            "description": "desc",
        }

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": False, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[])
        fetcher.db._get_source_by_id = MagicMock(return_value=source)
        fetcher.db.get_sources = MagicMock()
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()
        fetcher.db.save_config = MagicMock()

        overlay_processor = MagicMock()

        def fake_overlay(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"overlay-video")

        overlay_processor.add_custom_overlay_text.side_effect = fake_overlay

        class _FakeLoop:
            async def run_in_executor(self, executor, func):
                return func()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=True), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value={"video_id": "pending"}), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.agent.auto_mod_fetcher.asyncio.get_running_loop", return_value=_FakeLoop()), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("src.agent.config.load_config", return_value={}), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=processed_path)) as process_video, \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/should-not-happen")) as upload_video, \
             patch("src.agent.mod_video_processor.ModVideoProcessor", return_value=overlay_processor), \
             patch("src.agent.renderer.render_with_pip", return_value=rendered_path) as render_with_pip, \
             patch("src.bot.raw_review.request_raw_video_review", AsyncMock(return_value=True)) as request_review:
            result = asyncio.run(fetcher.run_test_render("src-1"))

        self.assertEqual(result.get("status"), "preview_ready")
        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 0)
        self.assertEqual(result.get("preview_video_path"), rendered_path)
        self.assertEqual(result.get("preview_source_name"), "Preview Source")
        self.assertEqual(process_video.await_args.kwargs["video_effects"], {
            "intro": {"enabled": True, "type": "blur", "duration": 1.5},
            "outro": {"enabled": False, "type": "none", "duration": 0.0},
        })
        overlay_processor.add_custom_overlay_text.assert_called_once()
        render_with_pip.assert_called_once()
        upload_video.assert_not_awaited()
        request_review.assert_not_awaited()
        fetcher.db.get_sources.assert_not_called()
        fetcher.db.mark_video_processing.assert_not_called()
        fetcher.db.release_video_processing.assert_not_called()
        fetcher.db.mark_video_published.assert_not_called()
        fetcher.db.mark_video_failed.assert_not_called()
        fetcher.db.update_next_publish_after_attempt.assert_not_called()
        fetcher.db.save_config.assert_not_called()

    def test_render_with_pip_uses_compact_top_right_facecam_layout(self):
        input_path = os.path.join(self.tempdir.name, "main.mp4")
        facecam_path = os.path.join(self.tempdir.name, "facecam.mp4")
        Path(input_path).write_bytes(b"main")
        Path(facecam_path).write_bytes(b"facecam")
        captured = {}
        cfg = SimpleNamespace(
            TEMP_DIR=self.tempdir.name,
            REACTIONS_DIR=self.tempdir.name,
            BACKGROUND_REMOVAL_ENABLED=False,
            BACKGROUND_DIR=self.tempdir.name,
            PIP_SCALE=0.7,
            PIP_MARGIN=6,
            PIP_POSITION="bottom_right",
            FFMPEG_THREADS=1,
            GLOBAL_FONT_AR=None,
            GLOBAL_FONT_EN=None,
        )

        def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=10):
            cmd_text = " ".join(cmd)
            if "stream=codec_type" in cmd_text:
                return SimpleNamespace(stdout="audio\n", stderr="", returncode=0)
            if "stream=width,height" in cmd_text:
                return SimpleNamespace(stdout="1080x1920\n", stderr="", returncode=0)
            if "format=duration" in cmd_text:
                return SimpleNamespace(stdout="6.0\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        def fake_run_ffmpeg(cmd):
            captured["cmd"] = cmd
            return True

        with patch("src.agent.renderer.validate_input_file", return_value=True), \
             patch("src.agent.renderer.validate_output_file", return_value=True), \
             patch("src.agent.renderer._detect_and_remove_borders", side_effect=lambda path, temp_dir: path), \
             patch("src.agent.renderer.run_ffmpeg_command", side_effect=fake_run_ffmpeg), \
             patch("src.agent.renderer.subprocess.run", side_effect=fake_subprocess_run):
            out_path = render_with_pip(
                cfg=cfg,
                input_path=input_path,
                out_dir=self.tempdir.name,
                facecam_enabled=True,
                facecam_path=facecam_path,
                facecam_layout="small_circle_top_right",
                facecam_position="top_right",
                facecam_shape="circle",
                facecam_scale=0.18,
            )

        self.assertTrue(out_path.endswith("_pip.mp4"))
        filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        self.assertIn("scale=194:-2:flags=lanczos", filter_complex)
        self.assertIn("overlay=x=main_w-overlay_w-23:y=23", filter_complex)
        self.assertIn("geq=r='r(X,Y)'", filter_complex)

    def test_render_with_pip_uses_compact_bottom_left_facecam_layout(self):
        input_path = os.path.join(self.tempdir.name, "main-bottom-left.mp4")
        facecam_path = os.path.join(self.tempdir.name, "facecam-bottom-left.mp4")
        Path(input_path).write_bytes(b"main")
        Path(facecam_path).write_bytes(b"facecam")
        captured = {}
        cfg = SimpleNamespace(
            TEMP_DIR=self.tempdir.name,
            REACTIONS_DIR=self.tempdir.name,
            BACKGROUND_REMOVAL_ENABLED=False,
            BACKGROUND_DIR=self.tempdir.name,
            PIP_SCALE=0.7,
            PIP_MARGIN=6,
            PIP_POSITION="bottom_right",
            FFMPEG_THREADS=1,
            GLOBAL_FONT_AR=None,
            GLOBAL_FONT_EN=None,
        )

        def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=10):
            cmd_text = " ".join(cmd)
            if "stream=codec_type" in cmd_text:
                return SimpleNamespace(stdout="audio\n", stderr="", returncode=0)
            if "stream=width,height" in cmd_text:
                return SimpleNamespace(stdout="1080x1920\n", stderr="", returncode=0)
            if "format=duration" in cmd_text:
                return SimpleNamespace(stdout="6.0\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        def fake_run_ffmpeg(cmd):
            captured["cmd"] = cmd
            return True

        with patch("src.agent.renderer.validate_input_file", return_value=True), \
             patch("src.agent.renderer.validate_output_file", return_value=True), \
             patch("src.agent.renderer._detect_and_remove_borders", side_effect=lambda path, temp_dir: path), \
             patch("src.agent.renderer.run_ffmpeg_command", side_effect=fake_run_ffmpeg), \
             patch("src.agent.renderer.subprocess.run", side_effect=fake_subprocess_run):
            out_path = render_with_pip(
                cfg=cfg,
                input_path=input_path,
                out_dir=self.tempdir.name,
                facecam_enabled=True,
                facecam_path=facecam_path,
                facecam_layout="small_circle_bottom_left",
                facecam_position="bottom_left",
                facecam_shape="circle",
                facecam_scale=0.18,
            )

        self.assertTrue(out_path.endswith("_pip.mp4"))
        filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        self.assertIn("scale=194:-2:flags=lanczos", filter_complex)
        self.assertIn("overlay=x=23:y=main_h-overlay_h-23", filter_complex)
        self.assertIn("geq=r='r(X,Y)'", filter_complex)

    def test_run_cycle_skips_facecam_render_when_source_has_no_valid_clips(self):
        fetcher = AutoModFetcher("inst-facecam-empty")
        raw_path = os.path.join(self.tempdir.name, "raw-facecam-empty.mp4")
        processed_path = os.path.join(self.tempdir.name, "processed-facecam-empty.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(processed_path).write_bytes(b"processed-video")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://youtube.com/@demo",
            "platform": "youtube_shorts",
            "settings": {
                "facecam": {
                    "enabled": True,
                    "clips": [{"id": "clip-1", "path": os.path.join(self.tempdir.name, "missing.mp4"), "enabled": True}],
                }
            },
        }
        video = {
            "id": "vid-facecam-empty",
            "title": "Facecam Video",
            "url": "https://youtube.com/shorts/fc999",
            "duration": 30,
            "description": "desc",
        }

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch("src.bot.channel_manager.ChannelManager.get_channel", return_value=None), \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[video])), \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=processed_path)), \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/no-facecam")), \
             patch("src.agent.renderer.render_with_pip") as render_with_pip:
            result = asyncio.run(fetcher.run_cycle(force=True))

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        render_with_pip.assert_not_called()

    def test_add_custom_overlay_text_uses_animated_image_overlay_when_requested(self):
        processor = ModVideoProcessor(temp_dir=self.tempdir.name)
        input_path = os.path.join(self.tempdir.name, "overlay-input.mp4")
        output_path = os.path.join(self.tempdir.name, "overlay-output.mp4")
        Path(input_path).write_bytes(b"input-video")
        captured = {}

        def fake_run(cmd, capture_output=True, text=True, timeout=300):
            captured["cmd"] = cmd
            Path(output_path).write_bytes(b"output-video")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("src.agent.mod_video_processor.ffmpeg_bin", return_value="ffmpeg"), \
             patch.object(processor, "_get_video_duration", return_value=12.0), \
             patch.object(processor, "_get_video_dimensions", return_value=(1080, 1920)), \
             patch.object(processor, "_get_video_fps", return_value=30.0), \
             patch.object(processor, "_get_best_font", return_value="C:/Windows/Fonts/arial.ttf"), \
             patch("src.agent.mod_video_processor.subprocess.run", side_effect=fake_run):
            processor.add_custom_overlay_text(
                input_path=input_path,
                output_path=output_path,
                text="Animated overlay",
                timing="start",
                duration=2.0,
                screen_position="top",
                intro_animation={"enabled": True, "type": "blur", "duration": 0.8},
                outro_animation={"enabled": True, "type": "fade", "duration": 0.5},
            )

        self.assertTrue(os.path.exists(output_path))
        cmd = captured["cmd"]
        self.assertIn("-filter_complex", cmd)
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("fade=t=in", filter_complex)
        self.assertIn("fade=t=out", filter_complex)
        self.assertIn("boxblur=", filter_complex)

    def test_targeted_approved_run_processes_exact_approved_video_without_refetching_source(self):
        fetcher = AutoModFetcher("inst-approved-target-video")
        source = {
            "id": "src-1",
            "enabled": True,
            "source_name": "Demo Source",
            "source_url": "https://example.com/source",
            "platform": "youtube_shorts",
            "settings": {"require_raw_review": True},
        }
        other_video = {"id": "vid-other", "title": "Other Video", "url": "https://example.com/other.mp4", "duration": 30}
        approved_video = {"id": "vid-approved", "title": "Approved Video", "url": "https://example.com/approved.mp4", "duration": 30}
        raw_path = os.path.join(self.tempdir.name, "raw-target-approved.mp4")
        out_path = os.path.join(self.tempdir.name, "processed-target-approved.mp4")
        Path(raw_path).write_bytes(b"raw-video")
        Path(out_path).write_bytes(b"processed-video")

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=[{"enabled": True, "channel_id": "ch-1", "content_type": "minecraft_mods"}])
        fetcher.db.get_sources = MagicMock(return_value=[source])
        fetcher.db.get_video_process_state = MagicMock(return_value=(None, None))
        fetcher.db.mark_video_processing = MagicMock(return_value=True)
        fetcher.db.release_video_processing = MagicMock()
        fetcher.db.mark_video_published = MagicMock()
        fetcher.db.mark_video_failed = MagicMock()
        fetcher.db.update_next_publish_after_attempt = MagicMock()

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch("src.agent.auto_mod_fetcher.get_pending_raw_review", return_value=None), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_approved", side_effect=lambda _sid, vid: vid == "vid-approved"), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_blocked", return_value=False), \
             patch("src.agent.auto_mod_fetcher.is_raw_review_skip_active", return_value=False), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(side_effect=[[other_video], [approved_video]])) as fetch_videos, \
             patch.object(fetcher, "download_video", AsyncMock(return_value=raw_path)) as download_video, \
             patch("src.agent.ffmpeg_utils.ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch.object(fetcher, "process_video", AsyncMock(return_value=out_path)), \
             patch.object(fetcher, "_upload_to_youtube", AsyncMock(return_value="https://youtu.be/approved")), \
             patch("src.bot.raw_review.request_raw_video_review", AsyncMock(return_value=True)) as request_review:
            result = asyncio.run(
                fetcher.run_cycle(
                    force=True,
                    target_channel_id="ch-1",
                    target_content_type="minecraft_mods",
                    target_source_id="src-1",
                    target_video_id="vid-approved",
                    target_video_url=approved_video["url"],
                    target_video_title=approved_video["title"],
                    target_video_type="shorts",
                )
            )

        self.assertEqual(result.get("processed"), 1)
        self.assertEqual(result.get("published"), 1)
        fetch_videos.assert_not_awaited()
        self.assertEqual(download_video.await_args.args[0], approved_video["url"])
        request_review.assert_not_awaited()

    def test_run_now_reports_waiting_raw_review_instead_of_skipped_completion(self):
        query = SimpleNamespace(
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=12345))
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        fake_fetcher = SimpleNamespace(
            run_cycle=AsyncMock(
                return_value={
                    "status": "waiting_raw_review",
                    "processed": 0,
                    "published": 0,
                    "failed": 0,
                    "skipped": 0,
                    "waiting_raw_review": 1,
                }
            )
        )

        with patch("src.bot.handlers.auto_mod_handlers.AutoModFetcher", return_value=fake_fetcher):
            state = asyncio.run(run_now(update, context))

        self.assertEqual(state, AM_MENU)
        reply_text = query.message.reply_text.await_args.args[0]
        self.assertIn("بانتظار مراجعة خام", reply_text)
        self.assertIn("لن يتم جلب فيديو جديد", reply_text)

    def test_test_render_run_sends_preview_video_in_telegram_without_publishing(self):
        preview_path = os.path.join(self.tempdir.name, "telegram-preview.mp4")
        Path(preview_path).write_bytes(b"preview-video")
        query = SimpleNamespace(
            data="am_test_render_src:src-1",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=12345))
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_video=AsyncMock()))
        run_test_render = AsyncMock(
            return_value={
                "status": "preview_ready",
                "processed": 1,
                "published": 0,
                "preview_video_path": preview_path,
                "preview_video_title": "Preview Video",
                "preview_source_name": "Preview Source",
            }
        )

        class FakeFetcher:
            _cleanup_file = staticmethod(AutoModFetcher._cleanup_file)

            async def run_test_render(self, *args, **kwargs):
                return await run_test_render(*args, **kwargs)

        with patch("src.bot.handlers.auto_mod_handlers.AutoModFetcher", FakeFetcher):
            state = asyncio.run(test_render_run(update, context))

        self.assertEqual(state, AM_MENU)
        run_test_render.assert_awaited_once()
        self.assertEqual(run_test_render.await_args.args[0], "src-1")
        context.bot.send_video.assert_awaited_once()
        reply_text = query.message.reply_text.await_args.args[0]
        self.assertIn("تم إنشاء فيديو الاختبار", reply_text)
        self.assertIn("لم يتم رفع الفيديو إلى YouTube", reply_text)
        self.assertFalse(os.path.exists(preview_path))

    def test_targeted_force_run_only_touches_matching_schedule_and_source(self):
        fetcher = AutoModFetcher("inst-target-force")
        calls = []
        schedules = [
            {
                "enabled": True,
                "channel_id": "ch-1",
                "content_type": "minecraft_mods",
                "next_publish_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            },
            {
                "enabled": True,
                "channel_id": "ch-2",
                "content_type": "minecraft_mods",
                "next_publish_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            },
        ]

        def get_sources(channel_id, content_type):
            calls.append((channel_id, content_type))
            return [
                {"id": "src-other", "enabled": True, "source_name": "Other", "source_url": "https://example.com/other", "platform": "youtube_shorts", "settings": {}},
                {"id": "src-target", "enabled": True, "source_name": "Target", "source_url": "https://example.com/target", "platform": "youtube_shorts", "settings": {}},
            ]

        fetcher.db.get_config = MagicMock(return_value={"auto_fetch_enabled": True, "settings": {"fetch_order": "newest"}})
        fetcher.db.get_all_schedules = MagicMock(return_value=schedules)
        fetcher.db.get_sources = MagicMock(side_effect=get_sources)

        with patch("src.agent.auto_mod_fetcher.has_pending_raw_reviews", return_value=False), \
             patch.object(fetcher, "fetch_videos_from_source", AsyncMock(return_value=[])) as fetch_videos:
            result = asyncio.run(
                fetcher.run_cycle(
                    force=True,
                    target_channel_id="ch-2",
                    target_content_type="minecraft_mods",
                    target_source_id="src-target",
                )
            )

        self.assertEqual(result.get("processed"), 0)
        self.assertEqual(calls, [("ch-2", "minecraft_mods")])
        fetch_videos.assert_awaited_once_with("https://example.com/target", items_range="1-50", platform="youtube_shorts")

    def test_short_interval_still_respects_publish_hours(self):
        fetcher = AutoModFetcher("inst-hours")
        now = datetime.now(timezone.utc)
        schedule = {
            "publish_interval_minutes": 5,
            "next_publish_at": (now - timedelta(minutes=1)).isoformat(),
            "publish_hours": {
                "start": (now.hour + 1) % 24,
                "end": (now.hour + 2) % 24,
            },
        }

        self.assertFalse(fetcher._is_publish_time(schedule))

    def test_daily_limit_uses_actual_published_records(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db = AutoModDB("inst-daily-limit")
            self.assertTrue(db.save_schedule("ch-1", "minecraft_mods", interval_minutes=30, daily_limit=1))
            self.assertTrue(db.mark_video_processing("vid-1", "ch-1", "minecraft_mods", "Video 1"))
            self.assertTrue(db.mark_video_published("vid-1", "ch-1", youtube_url="https://youtu.be/demo"))

            fetcher = AutoModFetcher("inst-daily-limit")
            schedule = fetcher.db.get_schedule("ch-1", "minecraft_mods")
            self.assertTrue(fetcher._reached_daily_limit(schedule))

    def test_failed_attempt_does_not_pull_next_publish_backward_in_local_mode(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        future_next = datetime.now(timezone.utc) + timedelta(hours=3)

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db = AutoModDB("inst-next-publish")
            self.assertTrue(db.save_schedule("ch-1", "minecraft_mods", interval_minutes=30, daily_limit=2))

            schedule = db.get_schedule("ch-1", "minecraft_mods")
            self.assertTrue(db._save_existing_schedule(schedule, {"next_publish_at": future_next.isoformat()}))
            self.assertTrue(db.update_next_publish_after_attempt("ch-1", "minecraft_mods", published=False))

            reloaded = AutoModDB("inst-next-publish").get_schedule("ch-1", "minecraft_mods")

        persisted_next = datetime.fromisoformat(reloaded["next_publish_at"])
        self.assertGreaterEqual(persisted_next, future_next - timedelta(seconds=1))

    def test_loop_sleep_only_waits_remaining_interval(self):
        with patch("src.agent.auto_mod_fetcher.time.monotonic", return_value=130.4):
            self.assertEqual(_compute_loop_sleep_seconds(100.0, 60), 30)

        with patch("src.agent.auto_mod_fetcher.time.monotonic", return_value=170.0):
            self.assertEqual(_compute_loop_sleep_seconds(100.0, 60), 0)

    def test_auto_fetch_loop_config_normalization_survives_missing_or_invalid_config(self):
        normalized_missing = _normalize_auto_fetch_loop_config(None, 60)
        self.assertTrue(normalized_missing["auto_fetch_enabled"])
        self.assertEqual(normalized_missing["auto_fetch_interval_seconds"], 60)

        normalized_invalid = _normalize_auto_fetch_loop_config({"auto_fetch_interval_seconds": "bad"}, 45)
        self.assertTrue(normalized_invalid["auto_fetch_enabled"])
        self.assertEqual(normalized_invalid["auto_fetch_interval_seconds"], 45)

    def test_local_only_sources_and_schedule_survive_new_db_instance(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db1 = AutoModDB("inst-local")
            self.assertTrue(db1.add_source("ch-1", "https://youtube.com/@demo", "Demo"))
            self.assertTrue(db1.save_schedule("ch-1", "minecraft_mods", interval_minutes=30, daily_limit=2))

            db2 = AutoModDB("inst-local")
            sources = db2.get_sources("ch-1", "minecraft_mods")
            schedule = db2.get_schedule("ch-1", "minecraft_mods")

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_url"], "https://youtube.com/@demo")
        self.assertIsNotNone(schedule)
        self.assertEqual(schedule["daily_limit"], 2)

    def test_local_only_processed_state_survives_new_db_instance(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db1 = AutoModDB("inst-local")
            self.assertTrue(db1.mark_video_processing("vid-1", "ch-1"))

            db2 = AutoModDB("inst-local")
            self.assertTrue(db2.is_video_locked("vid-1", "ch-1"))
            self.assertTrue(db2.mark_video_published("vid-1", "ch-1", youtube_url="https://youtu.be/x"))

            db3 = AutoModDB("inst-local")
            record = db3.get_video_process_record("vid-1", "ch-1")

        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["youtube_url"], "https://youtu.be/x")

    def test_reset_stale_processing_keeps_recent_processing_locks(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db = AutoModDB("inst-stale-recent")
            self.assertTrue(db.mark_video_processing("vid-keep", "ch-1"))
            cleaned = db.reset_stale_processing(stale_minutes=90)
            self.assertEqual(cleaned, 0)
            self.assertTrue(db.is_video_locked("vid-keep", "ch-1", stale_minutes=90))

    def test_reset_stale_processing_removes_old_processing_locks(self):
        def _table_path(table):
            return os.path.join(self.tempdir.name, f"{table}.json")

        with patch("src.agent.auto_mod_fetcher._auto_mod_local_table_path", side_effect=_table_path), \
             patch("src.agent.supabase_client.USE_SUPABASE", False):
            db = AutoModDB("inst-stale-old")
            self.assertTrue(db.mark_video_processing("vid-old", "ch-1"))

            processed_path = _table_path("auto_mod_processed")
            with open(processed_path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            rows[0]["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
            with open(processed_path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=2)

            cleaned = db.reset_stale_processing(stale_minutes=30)
            self.assertEqual(cleaned, 1)
            self.assertFalse(db.is_video_locked("vid-old", "ch-1", stale_minutes=30))

    # ────────────────────────────────────────────────
    # FFmpeg Low-Resource Settings Tests
    # ────────────────────────────────────────────────

    def test_shorts_x264_settings_ultrafast_on_render(self):
        """When RENDER=1, preset must be ultrafast and CRF >= 26."""
        mvp = ModVideoProcessor(temp_dir=self.tempdir.name)
        with patch.dict(os.environ, {"RENDER": "1"}, clear=False):
            threads, preset, crf = mvp._shorts_x264_settings()
        self.assertEqual(preset, "ultrafast")
        self.assertGreaterEqual(crf, 26)
        self.assertGreater(threads, 0)

    def test_shorts_x264_settings_ultrafast_on_low_resource(self):
        """When LOW_RESOURCE_MODE=1, preset must be ultrafast and CRF >= 26."""
        mvp = ModVideoProcessor(temp_dir=self.tempdir.name)
        with patch.dict(os.environ, {"LOW_RESOURCE_MODE": "1", "RENDER": ""}, clear=False):
            threads, preset, crf = mvp._shorts_x264_settings()
        self.assertEqual(preset, "ultrafast")
        self.assertGreaterEqual(crf, 26)

    def test_shorts_x264_settings_uses_env_overrides(self):
        """Custom env vars override the base defaults."""
        mvp = ModVideoProcessor(temp_dir=self.tempdir.name)
        with patch.dict(os.environ, {
            "RENDER": "1",
            "SHORTS_X264_PRESET": "superfast",
            "SHORTS_X264_CRF": "30",
        }, clear=False):
            threads, preset, crf = mvp._shorts_x264_settings()
        self.assertEqual(preset, "superfast")
        self.assertEqual(crf, 30)

    def test_process_mod_video_accepts_progress_callback(self):
        """process_mod_video signature must accept progress_callback kwarg."""
        import inspect
        sig = inspect.signature(ModVideoProcessor.process_mod_video)
        self.assertIn("progress_callback", sig.parameters)


if __name__ == "__main__":
    unittest.main()
