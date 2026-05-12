-- Persist missing bot/channel features discovered during storage audit.

BEGIN;

ALTER TABLE public.bot_state
    ADD COLUMN IF NOT EXISTS pending_videos TEXT,
    ADD COLUMN IF NOT EXISTS raw_review TEXT,
    ADD COLUMN IF NOT EXISTS downloader TEXT,
    ADD COLUMN IF NOT EXISTS scheduler TEXT,
    ADD COLUMN IF NOT EXISTS source_rate_limits TEXT,
    ADD COLUMN IF NOT EXISTS gdrive_poll TEXT,
    ADD COLUMN IF NOT EXISTS channel_content_mode TEXT,
    ADD COLUMN IF NOT EXISTS awaiting TEXT,
    ADD COLUMN IF NOT EXISTS last_output TEXT,
    ADD COLUMN IF NOT EXISTS telegram_notifications TEXT,
    ADD COLUMN IF NOT EXISTS facecam_missing_notified TEXT;

ALTER TABLE public.channel_configs
    ADD COLUMN IF NOT EXISTS custom_overlay_texts TEXT DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS extra_data TEXT DEFAULT '{}';

ALTER TABLE public.publish_channels
    ADD COLUMN IF NOT EXISTS custom_overlay_texts TEXT DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS extra_data TEXT DEFAULT '{}';

COMMIT;
