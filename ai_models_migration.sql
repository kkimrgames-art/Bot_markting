-- ============================================================================
-- Consolidated AI + Quota + Preflight + Hibernation Migration
-- ============================================================================
-- This single SQL file creates ALL the tables needed by:
--   1. src/agent/ai_models_store.py     — per-provider custom model IDs
--   2. src/agent/ai_quota_tracker.py    — unified key+provider quota tracking
--   3. src/agent/preflight_checks.py    — pre-flight cooldown tracking
--   4. src/agent/hibernation_manager.py — DB-down hibernation state tracking
--
-- Run this once on your Supabase / PostgreSQL database.
-- Safe to re-run (uses CREATE TABLE IF NOT EXISTS + ON CONFLICT).
-- ============================================================================

BEGIN;

-- ===========================================================================
-- PART 1: AI MODELS TABLES (used by ai_models_store.py)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.ai_models_state (
    id           TEXT PRIMARY KEY DEFAULT 'main',
    models       JSONB   NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.ai_models_state (id, models, updated_at)
VALUES ('main', '{}'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.ai_models (
    id            BIGSERIAL PRIMARY KEY,
    provider      TEXT NOT NULL CHECK (provider IN ('gemini', 'openrouter', 'groq', 'clarifai', 'mistral')),
    model_id      TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    display_name  TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    added_at      TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (provider, model_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_models_provider         ON public.ai_models (provider);
CREATE INDEX IF NOT EXISTS idx_ai_models_provider_enabled ON public.ai_models (provider, enabled);
CREATE INDEX IF NOT EXISTS idx_ai_models_sort_order        ON public.ai_models (provider, sort_order);

CREATE OR REPLACE FUNCTION public.touch_ai_models_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_models_state_touch ON public.ai_models_state;
CREATE TRIGGER trg_ai_models_state_touch
    BEFORE UPDATE ON public.ai_models_state
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_ai_models_state_updated_at();

CREATE OR REPLACE FUNCTION public.touch_ai_models_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_models_touch ON public.ai_models;
CREATE TRIGGER trg_ai_models_touch
    BEFORE UPDATE ON public.ai_models
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_ai_models_updated_at();

CREATE OR REPLACE VIEW public.v_ai_enabled_models AS
SELECT provider, model_id, display_name, sort_order, added_at, updated_at
FROM public.ai_models
WHERE enabled = TRUE
ORDER BY provider ASC, sort_order ASC, added_at ASC;

INSERT INTO public.ai_models (provider, model_id, enabled, display_name, sort_order)
VALUES
    ('gemini', 'gemini-1.5-flash',          TRUE, 'Gemini 1.5 Flash',          1),
    ('gemini', 'gemini-1.5-flash-8b',       TRUE, 'Gemini 1.5 Flash 8B',       2),
    ('gemini', 'gemini-1.5-flash-latest',   TRUE, 'Gemini 1.5 Flash (latest)', 3),
    ('openrouter', 'meta-llama/llama-3.1-8b-instruct:free',         TRUE, 'Llama 3.1 8B (free)',         1),
    ('openrouter', 'mistralai/mistral-7b-instruct:free',            TRUE, 'Mistral 7B (free)',           2),
    ('openrouter', 'qwen/qwen-2.5-7b-instruct:free',                TRUE, 'Qwen 2.5 7B (free)',          3),
    ('openrouter', 'microsoft/phi-3-medium-128k-instruct:free',     TRUE, 'Phi-3 Medium (free)',         4),
    ('openrouter', 'huggingfaceh4/zephyr-7b-beta:free',             TRUE, 'Zephyr 7B Beta (free)',       5),
    ('openrouter', 'openchat/openchat-7b:free',                     TRUE, 'OpenChat 7B (free)',          6),
    ('groq', 'llama-3.1-8b-instant',    TRUE, 'Llama 3.1 8B Instant',    1),
    ('groq', 'llama-3.3-70b-versatile', TRUE, 'Llama 3.3 70B Versatile', 2),
    ('groq', 'gemma2-9b-it',            TRUE, 'Gemma 2 9B IT',           3),
    ('groq', 'mixtral-8x7b-32768',      TRUE, 'Mixtral 8x7B 32K',        4),
    ('clarifai', 'GPT-4o',            TRUE, 'GPT-4o',           1),
    ('clarifai', 'GLM_4_6',           TRUE, 'GLM 4.6',          2),
    ('clarifai', 'Kimi-K2-Thinking',  TRUE, 'Kimi K2 Thinking', 3),
    ('clarifai', 'MiniMax-M2',        TRUE, 'MiniMax M2',       4),
    ('mistral', 'mistral-large-latest', TRUE, 'Mistral Large (latest)', 1),
    ('mistral', 'mistral-small-latest',  TRUE, 'Mistral Small (latest)', 2),
    ('mistral', 'mistral-tiny',          TRUE, 'Mistral Tiny',           3)
ON CONFLICT (provider, model_id) DO NOTHING;


-- ===========================================================================
-- PART 2: AI QUOTA TRACKER TABLES (used by ai_quota_tracker.py)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.ai_quota_state (
    id           TEXT PRIMARY KEY DEFAULT 'main',
    keys         JSONB   NOT NULL DEFAULT '{}'::jsonb,
    providers    JSONB   NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.ai_quota_state (id, keys, providers, updated_at)
VALUES ('main', '{}'::jsonb, '{}'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.ai_quota_keys (
    id                          BIGSERIAL PRIMARY KEY,
    provider                    TEXT NOT NULL CHECK (provider IN ('gemini', 'openrouter', 'groq', 'clarifai', 'mistral')),
    api_key                     TEXT NOT NULL,
    is_blocked                  BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_until               TIMESTAMPTZ,
    quota_reset_at              TIMESTAMPTZ,
    last_error_category         TEXT,
    last_error_time             TIMESTAMPTZ,
    last_success_at             TIMESTAMPTZ,
    consecutive_quota_failures  INTEGER NOT NULL DEFAULT 0,
    total_requests              INTEGER NOT NULL DEFAULT 0,
    total_errors                INTEGER NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ DEFAULT now(),
    updated_at                  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (provider, api_key)
);

CREATE INDEX IF NOT EXISTS idx_ai_quota_keys_provider         ON public.ai_quota_keys (provider);
CREATE INDEX IF NOT EXISTS idx_ai_quota_keys_provider_blocked ON public.ai_quota_keys (provider, is_blocked);
CREATE INDEX IF NOT EXISTS idx_ai_quota_keys_blocked_until     ON public.ai_quota_keys (blocked_until);
CREATE INDEX IF NOT EXISTS idx_ai_quota_keys_quota_reset       ON public.ai_quota_keys (quota_reset_at);

CREATE TABLE IF NOT EXISTS public.ai_quota_providers (
    provider                    TEXT PRIMARY KEY CHECK (provider IN ('gemini', 'openrouter', 'groq', 'clarifai', 'mistral')),
    is_blocked                  BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_until               TIMESTAMPTZ,
    last_quota_exhausted_at     TIMESTAMPTZ,
    consecutive_failures        INTEGER NOT NULL DEFAULT 0,
    total_calls                 INTEGER NOT NULL DEFAULT 0,
    total_successes             INTEGER NOT NULL DEFAULT 0,
    updated_at                  TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.ai_quota_providers (provider, is_blocked, consecutive_failures, total_calls, total_successes)
VALUES
    ('gemini',     FALSE, 0, 0, 0),
    ('openrouter', FALSE, 0, 0, 0),
    ('groq',       FALSE, 0, 0, 0),
    ('clarifai',   FALSE, 0, 0, 0),
    ('mistral',    FALSE, 0, 0, 0)
ON CONFLICT (provider) DO NOTHING;

CREATE OR REPLACE FUNCTION public.touch_ai_quota_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_quota_state_touch ON public.ai_quota_state;
CREATE TRIGGER trg_ai_quota_state_touch
    BEFORE UPDATE ON public.ai_quota_state
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_ai_quota_state_updated_at();

DROP TRIGGER IF EXISTS trg_ai_quota_keys_touch ON public.ai_quota_keys;
CREATE TRIGGER trg_ai_quota_keys_touch
    BEFORE UPDATE ON public.ai_quota_keys
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_ai_quota_state_updated_at();

DROP TRIGGER IF EXISTS trg_ai_quota_providers_touch ON public.ai_quota_providers;
CREATE TRIGGER trg_ai_quota_providers_touch
    BEFORE UPDATE ON public.ai_quota_providers
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_ai_quota_state_updated_at();

CREATE OR REPLACE VIEW public.v_ai_blocked_keys AS
SELECT
    provider,
    api_key,
    SUBSTRING(api_key FROM GREATEST(LENGTH(api_key) - 7, 1)) AS masked_key_suffix,
    blocked_until,
    quota_reset_at,
    last_error_category,
    last_error_time,
    CASE
        WHEN blocked_until IS NULL THEN NULL
        ELSE EXTRACT(EPOCH FROM (blocked_until - now()))::INTEGER
    END AS seconds_until_unblock,
    CASE
        WHEN quota_reset_at IS NULL THEN NULL
        ELSE EXTRACT(EPOCH FROM (quota_reset_at - now()))::INTEGER
    END AS seconds_until_quota_reset
FROM public.ai_quota_keys
WHERE is_blocked = TRUE
ORDER BY provider, blocked_until;

CREATE OR REPLACE VIEW public.v_ai_provider_status AS
SELECT
    p.provider,
    p.is_blocked,
    p.blocked_until,
    p.last_quota_exhausted_at,
    p.consecutive_failures,
    p.total_calls,
    p.total_successes,
    CASE
        WHEN p.blocked_until IS NULL THEN 0
        ELSE EXTRACT(EPOCH FROM (p.blocked_until - now()))::INTEGER
    END AS seconds_until_unblock,
    (SELECT COUNT(*) FROM public.ai_quota_keys k WHERE k.provider = p.provider) AS total_keys,
    (SELECT COUNT(*) FROM public.ai_quota_keys k
     WHERE k.provider = p.provider AND k.is_blocked = FALSE) AS available_keys,
    (SELECT COUNT(*) FROM public.ai_quota_keys k
     WHERE k.provider = p.provider AND k.is_blocked = TRUE) AS blocked_keys
FROM public.ai_quota_providers p
ORDER BY p.provider;

CREATE OR REPLACE VIEW public.v_ai_provider_summary AS
SELECT
    p.provider,
    (SELECT COUNT(*) FROM public.ai_models m WHERE m.provider = p.provider) AS total_models,
    (SELECT COUNT(*) FROM public.ai_models m WHERE m.provider = p.provider AND m.enabled = TRUE) AS enabled_models,
    (SELECT COUNT(*) FROM public.ai_quota_keys k WHERE k.provider = p.provider) AS total_keys,
    (SELECT COUNT(*) FROM public.ai_quota_keys k
     WHERE k.provider = p.provider AND k.is_blocked = FALSE) AS available_keys,
    p.is_blocked AS provider_blocked,
    p.blocked_until,
    p.last_quota_exhausted_at,
    p.total_calls,
    p.total_successes
FROM public.ai_quota_providers p
ORDER BY p.provider;


-- ===========================================================================
-- PART 3: PREFLIGHT COOLDOWN TABLES (used by preflight_checks.py)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 3.1) Single-row JSON state for preflight cooldowns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.preflight_cooldowns_state (
    id           TEXT PRIMARY KEY DEFAULT 'main',
    entries      JSONB   NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.preflight_cooldowns_state (id, entries, updated_at)
VALUES ('main', '{}'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3.2) Detail table: one row per (channel_id, source_id) in cooldown
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.preflight_cooldowns (
    id                  BIGSERIAL PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    source_id           TEXT,
    reason              TEXT,
    failed_at           TIMESTAMPTZ DEFAULT now(),
    cooldown_until      TIMESTAMPTZ NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (channel_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_preflight_cd_channel   ON public.preflight_cooldowns (channel_id);
CREATE INDEX IF NOT EXISTS idx_preflight_cd_active     ON public.preflight_cooldowns (is_active);
CREATE INDEX IF NOT EXISTS idx_preflight_cd_until       ON public.preflight_cooldowns (cooldown_until);

-- ---------------------------------------------------------------------------
-- 3.3) updated_at trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_preflight_cd_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_preflight_cd_state_touch ON public.preflight_cooldowns_state;
CREATE TRIGGER trg_preflight_cd_state_touch
    BEFORE UPDATE ON public.preflight_cooldowns_state
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_preflight_cd_updated_at();

DROP TRIGGER IF EXISTS trg_preflight_cd_touch ON public.preflight_cooldowns;
CREATE TRIGGER trg_preflight_cd_touch
    BEFORE UPDATE ON public.preflight_cooldowns
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_preflight_cd_updated_at();

-- ---------------------------------------------------------------------------
-- 3.4) View: active preflight cooldowns
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_preflight_active_cooldowns AS
SELECT
    channel_id,
    source_id,
    reason,
    failed_at,
    cooldown_until,
    CASE
        WHEN cooldown_until <= now() THEN 0
        ELSE EXTRACT(EPOCH FROM (cooldown_until - now()))::INTEGER
    END AS seconds_remaining,
    CASE
        WHEN cooldown_until <= now() THEN 'expired'
        ELSE 'active'
    END AS status
FROM public.preflight_cooldowns
WHERE is_active = TRUE
ORDER BY cooldown_until ASC;

-- ---------------------------------------------------------------------------
-- 3.5) View: preflight summary (channels currently in cooldown grouped by reason)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_preflight_cooldown_summary AS
SELECT
    COUNT(*) AS total_active_cooldowns,
    COUNT(DISTINCT channel_id) AS channels_affected,
    COUNT(DISTINCT source_id) AS sources_affected,
    MIN(cooldown_until) AS earliest_expiry,
    MAX(cooldown_until) AS latest_expiry
FROM public.preflight_cooldowns
WHERE is_active = TRUE AND cooldown_until > now();


-- ===========================================================================
-- PART 4: HELPER FUNCTIONS
-- ===========================================================================

-- Function: manually unblock all blocked AI keys for a provider (or all)
CREATE OR REPLACE FUNCTION public.unblock_all_ai_keys(p_provider TEXT DEFAULT NULL)
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    IF p_provider IS NULL THEN
        UPDATE public.ai_quota_keys
        SET is_blocked = FALSE,
            blocked_until = NULL,
            quota_reset_at = NULL,
            consecutive_quota_failures = 0,
            last_error_category = NULL,
            last_error_time = NULL,
            updated_at = now()
        WHERE is_blocked = TRUE;
    ELSE
        UPDATE public.ai_quota_keys
        SET is_blocked = FALSE,
            blocked_until = NULL,
            quota_reset_at = NULL,
            consecutive_quota_failures = 0,
            last_error_category = NULL,
            last_error_time = NULL,
            updated_at = now()
        WHERE provider = p_provider AND is_blocked = TRUE;
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;

    IF p_provider IS NULL THEN
        UPDATE public.ai_quota_providers
        SET is_blocked = FALSE,
            blocked_until = NULL,
            consecutive_failures = 0,
            last_quota_exhausted_at = NULL,
            updated_at = now()
        WHERE is_blocked = TRUE;
    ELSE
        UPDATE public.ai_quota_providers
        SET is_blocked = FALSE,
            blocked_until = NULL,
            consecutive_failures = 0,
            last_quota_exhausted_at = NULL,
            updated_at = now()
        WHERE provider = p_provider AND is_blocked = TRUE;
    END IF;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- Function: cleanup expired AI quota blocks
CREATE OR REPLACE FUNCTION public.cleanup_expired_ai_quota()
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE public.ai_quota_keys
    SET is_blocked = FALSE,
        blocked_until = NULL,
        quota_reset_at = NULL,
        consecutive_quota_failures = 0,
        last_error_category = NULL,
        last_error_time = NULL,
        updated_at = now()
    WHERE (blocked_until IS NOT NULL AND blocked_until <= now())
       OR (quota_reset_at IS NOT NULL AND quota_reset_at <= now());
    GET DIAGNOSTICS v_count = ROW_COUNT;

    UPDATE public.ai_quota_providers
    SET is_blocked = FALSE,
        blocked_until = NULL,
        consecutive_failures = 0,
        updated_at = now()
    WHERE blocked_until IS NOT NULL AND blocked_until <= now();

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- Function: clear all preflight cooldowns (mark as inactive)
CREATE OR REPLACE FUNCTION public.clear_all_preflight_cooldowns()
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE public.preflight_cooldowns
    SET is_active = FALSE,
        updated_at = now()
    WHERE is_active = TRUE;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- Function: clear preflight cooldowns for a specific channel (and optionally source)
CREATE OR REPLACE FUNCTION public.clear_preflight_cooldown(p_channel_id TEXT, p_source_id TEXT DEFAULT NULL)
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    IF p_source_id IS NULL THEN
        UPDATE public.preflight_cooldowns
        SET is_active = FALSE,
            updated_at = now()
        WHERE channel_id = p_channel_id AND is_active = TRUE;
    ELSE
        UPDATE public.preflight_cooldowns
        SET is_active = FALSE,
            updated_at = now()
        WHERE channel_id = p_channel_id AND source_id = p_source_id AND is_active = TRUE;
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- Function: cleanup expired preflight cooldowns (mark as inactive)
CREATE OR REPLACE FUNCTION public.cleanup_expired_preflight_cooldowns()
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE public.preflight_cooldowns
    SET is_active = FALSE,
        updated_at = now()
    WHERE is_active = TRUE AND cooldown_until <= now();
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- ===========================================================================
-- PART 5: HIBERNATION STATE TABLES (used by hibernation_manager.py)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 5.1) Single-row JSON state for hibernation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.hibernation_state (
    id              TEXT PRIMARY KEY DEFAULT 'main',
    is_hibernating  BOOLEAN NOT NULL DEFAULT FALSE,
    meta            JSONB   NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.hibernation_state (id, is_hibernating, meta, updated_at)
VALUES ('main', FALSE, '{}'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5.2) Hibernation event log (history of enter/exit transitions)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.hibernation_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL CHECK (event_type IN ('enter', 'exit', 'force_wake', 'force_sleep', 'reset_counter')),
    reason          TEXT,
    failure_count   INTEGER,
    duration_seconds INTEGER,  -- populated on exit events (time spent hibernating)
    occurred_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hibernation_events_type ON public.hibernation_events (event_type);
CREATE INDEX IF NOT EXISTS idx_hibernation_events_time ON public.hibernation_events (occurred_at);

-- ---------------------------------------------------------------------------
-- 5.3) updated_at trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_hibernation_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_hibernation_state_touch ON public.hibernation_state;
CREATE TRIGGER trg_hibernation_state_touch
    BEFORE UPDATE ON public.hibernation_state
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_hibernation_state_updated_at();

-- ---------------------------------------------------------------------------
-- 5.4) View: current hibernation status
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_hibernation_status AS
SELECT
    id,
    is_hibernating,
    meta,
    updated_at,
    CASE
        WHEN NOT is_hibernating THEN 'active'
        ELSE 'hibernating'
    END AS status,
    CASE
        WHEN is_hibernating AND (meta->>'started_at') IS NOT NULL
        THEN EXTRACT(EPOCH FROM (now() - ((meta->>'started_at')::TIMESTAMPTZ)))::INTEGER
        ELSE NULL
    END AS hibernation_duration_seconds
FROM public.hibernation_state;

-- ---------------------------------------------------------------------------
-- 5.5) View: recent hibernation events (last 30 days)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_hibernation_recent_events AS
SELECT
    event_type,
    reason,
    failure_count,
    duration_seconds,
    occurred_at
FROM public.hibernation_events
WHERE occurred_at > now() - INTERVAL '30 days'
ORDER BY occurred_at DESC
LIMIT 100;

-- ---------------------------------------------------------------------------
-- 5.6) Helper: log a hibernation event
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.log_hibernation_event(
    p_event_type TEXT,
    p_reason TEXT DEFAULT NULL,
    p_failure_count INTEGER DEFAULT NULL,
    p_duration_seconds INTEGER DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO public.hibernation_events (event_type, reason, failure_count, duration_seconds)
    VALUES (p_event_type, p_reason, p_failure_count, p_duration_seconds);
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 5.7) Helper: force-wake the bot from SQL (admin action)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.force_wake_bot(p_reason TEXT DEFAULT 'Manual wake via SQL')
RETURNS BOOLEAN AS $$
DECLARE
    was_hibernating BOOLEAN;
    started_at TIMESTAMPTZ;
    duration_s INTEGER;
BEGIN
    SELECT is_hibernating, (meta->>'started_at')::TIMESTAMPTZ
    INTO was_hibernating, started_at
    FROM public.hibernation_state WHERE id = 'main';

    IF NOT was_hibernating THEN
        RETURN FALSE;
    END IF;

    duration_s := CASE
        WHEN started_at IS NOT NULL THEN EXTRACT(EPOCH FROM (now() - started_at))::INTEGER
        ELSE NULL
    END;

    UPDATE public.hibernation_state
    SET is_hibernating = FALSE,
        meta = jsonb_build_object(
            'last_exit_reason', p_reason,
            'last_exit_at', now()::TEXT,
            'last_started_at', COALESCE(started_at::TEXT, '')
        )
    WHERE id = 'main';

    PERFORM public.log_hibernation_event('force_wake', p_reason, NULL, duration_s);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 5.8) Helper: force-sleep the bot from SQL (admin action)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.force_sleep_bot(p_reason TEXT DEFAULT 'Manual hibernation via SQL')
RETURNS BOOLEAN AS $$
DECLARE
    was_hibernating BOOLEAN;
BEGIN
    SELECT is_hibernating INTO was_hibernating
    FROM public.hibernation_state WHERE id = 'main';

    IF was_hibernating THEN
        RETURN FALSE;
    END IF;

    UPDATE public.hibernation_state
    SET is_hibernating = TRUE,
        meta = jsonb_build_object(
            'reason', p_reason,
            'started_at', now()::TEXT,
            'forced', TRUE
        )
    WHERE id = 'main';

    PERFORM public.log_hibernation_event('force_sleep', p_reason);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;


-- ===========================================================================
-- PART 6: DAILY REPORTS + NOTIFICATION GATE TABLES
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 6.1) Single-row JSON state for daily report rotation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.daily_reports_state (
    id              TEXT PRIMARY KEY DEFAULT 'main',
    last_report_at  TIMESTAMPTZ,
    last_report_type TEXT,
    rotation_index  INTEGER NOT NULL DEFAULT 0,
    reports_sent    INTEGER NOT NULL DEFAULT 0,
    history         JSONB   NOT NULL DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.daily_reports_state (id, history, updated_at)
VALUES ('main', '[]'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 6.2) Daily reports history (one row per report sent)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.daily_reports_history (
    id              BIGSERIAL PRIMARY KEY,
    report_type     TEXT NOT NULL CHECK (report_type IN ('performance', 'health', 'activity', 'deep_dive')),
    sent_at         TIMESTAMPTZ DEFAULT now(),
    summary         TEXT,
    full_text       TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_reports_history_type ON public.daily_reports_history (report_type);
CREATE INDEX IF NOT EXISTS idx_daily_reports_history_time ON public.daily_reports_history (sent_at);

-- ---------------------------------------------------------------------------
-- 6.3) Notification log (all notifications sent through the gate)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.notification_log (
    id              BIGSERIAL PRIMARY KEY,
    category        TEXT NOT NULL CHECK (category IN ('critical', 'important', 'normal', 'verbose')),
    notification_key TEXT NOT NULL,
    title           TEXT,
    sent_at         TIMESTAMPTZ DEFAULT now(),
    was_throttled   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_notification_log_category ON public.notification_log (category);
CREATE INDEX IF NOT EXISTS idx_notification_log_key     ON public.notification_log (notification_key);
CREATE INDEX IF NOT EXISTS idx_notification_log_time    ON public.notification_log (sent_at);

-- ---------------------------------------------------------------------------
-- 6.4) updated_at trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_daily_reports_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_daily_reports_state_touch ON public.daily_reports_state;
CREATE TRIGGER trg_daily_reports_state_touch
    BEFORE UPDATE ON public.daily_reports_state
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_daily_reports_state_updated_at();

-- ---------------------------------------------------------------------------
-- 6.5) View: report counts by type (last 30 days)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_daily_report_counts AS
SELECT
    report_type,
    COUNT(*) AS count_30d,
    MAX(sent_at) AS last_sent_at,
    MIN(sent_at) AS first_sent_at
FROM public.daily_reports_history
WHERE sent_at > now() - INTERVAL '30 days'
GROUP BY report_type
ORDER BY count_30d DESC;

-- ---------------------------------------------------------------------------
-- 6.6) View: notification stats (last 7 days)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_notification_stats_7d AS
SELECT
    category,
    COUNT(*) AS total_sent,
    SUM(CASE WHEN was_throttled THEN 1 ELSE 0 END) AS throttled_count,
    SUM(CASE WHEN NOT was_throttled THEN 1 ELSE 0 END) AS delivered_count,
    COUNT(DISTINCT notification_key) AS unique_keys
FROM public.notification_log
WHERE sent_at > now() - INTERVAL '7 days'
GROUP BY category
ORDER BY total_sent DESC;

-- ---------------------------------------------------------------------------
-- 6.7) View: most frequent notifications (last 7 days)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_top_notifications_7d AS
SELECT
    category,
    notification_key,
    COUNT(*) AS sent_count,
    MAX(title) AS sample_title,
    MAX(sent_at) AS last_sent
FROM public.notification_log
WHERE sent_at > now() - INTERVAL '7 days' AND NOT was_throttled
GROUP BY category, notification_key
ORDER BY sent_count DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 6.8) Helper: log a sent report
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.log_daily_report(
    p_report_type TEXT,
    p_summary TEXT DEFAULT NULL,
    p_full_text TEXT DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO public.daily_reports_history (report_type, summary, full_text)
    VALUES (p_report_type, p_summary, p_full_text);

    -- Update state
    UPDATE public.daily_reports_state
    SET last_report_at = now(),
        last_report_type = p_report_type,
        reports_sent = reports_sent + 1
    WHERE id = 'main';
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 6.9) Helper: log a notification (sent or throttled)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.log_notification(
    p_category TEXT,
    p_notification_key TEXT,
    p_title TEXT DEFAULT NULL,
    p_was_throttled BOOLEAN DEFAULT FALSE
) RETURNS VOID AS $$
BEGIN
    INSERT INTO public.notification_log (category, notification_key, title, was_throttled)
    VALUES (p_category, p_notification_key, p_title, p_was_throttled);
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 6.10) Helper: cleanup old notification logs (keep 30 days)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.cleanup_old_notification_logs()
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    DELETE FROM public.notification_log
    WHERE sent_at < now() - INTERVAL '30 days';
    GET DIAGNOSTICS v_count = ROW_COUNT;

    DELETE FROM public.daily_reports_history
    WHERE sent_at < now() - INTERVAL '90 days';

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- ===========================================================================
-- PART 7: YOUTUBE PROXY POOL (shared across Render instances)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.proxy_pool_state (
    id              TEXT PRIMARY KEY DEFAULT 'main',
    proxies         JSONB   NOT NULL DEFAULT '{}'::jsonb,
    working_count   INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.proxy_pool_state (id, proxies, working_count, updated_at)
VALUES ('main', '{}'::jsonb, 0, now())
ON CONFLICT (id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_proxy_pool_working ON public.proxy_pool_state (working_count);

-- View: proxy pool status
CREATE OR REPLACE VIEW public.v_proxy_pool_status AS
SELECT
    id,
    working_count,
    updated_at,
    CASE
        WHEN working_count = 0 THEN 'empty'
        WHEN working_count < 5 THEN 'low'
        ELSE 'healthy'
    END AS health,
    EXTRACT(EPOCH FROM (now() - updated_at))::INTEGER AS seconds_since_update
FROM public.proxy_pool_state;


-- ===========================================================================
-- PART 8: VIDEO ADS (ad insertion into processed videos)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.video_ads_config (
    id              TEXT PRIMARY KEY DEFAULT 'main',
    config          JSONB   NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

INSERT INTO public.video_ads_config (id, config, updated_at)
VALUES ('main', '{}'::jsonb, now())
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.video_ads (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    position            TEXT NOT NULL DEFAULT 'end' CHECK (position IN ('end', 'middle')),
    timing              REAL NOT NULL DEFAULT 5.0,
    continue_after_ad   BOOLEAN NOT NULL DEFAULT TRUE,
    has_video           BOOLEAN NOT NULL DEFAULT FALSE,
    video_filename      TEXT,
    added_at            TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_id)
);

CREATE INDEX IF NOT EXISTS idx_video_ads_source ON public.video_ads (source_id);
CREATE INDEX IF NOT EXISTS idx_video_ads_enabled ON public.video_ads (enabled);

CREATE OR REPLACE FUNCTION public.touch_video_ads_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_video_ads_touch ON public.video_ads;
CREATE TRIGGER trg_video_ads_touch
    BEFORE UPDATE ON public.video_ads
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_video_ads_updated_at();

CREATE OR REPLACE VIEW public.v_video_ads_summary AS
SELECT
    COUNT(*) AS total_ads,
    SUM(CASE WHEN enabled THEN 1 ELSE 0 END) AS enabled_ads,
    SUM(CASE WHEN has_video THEN 1 ELSE 0 END) AS ads_with_video,
    SUM(CASE WHEN position = 'end' THEN 1 ELSE 0 END) AS end_position,
    SUM(CASE WHEN position = 'middle' THEN 1 ELSE 0 END) AS middle_position,
    SUM(CASE WHEN continue_after_ad THEN 1 ELSE 0 END) AS continue_after
FROM public.video_ads;


COMMIT;

-- ============================================================================
-- Post-install verification queries (run these manually to confirm):
--
--   -- Total tables created:
--   SELECT tablename FROM pg_tables WHERE schemaname='public'
--   AND (tablename LIKE 'ai_%' OR tablename LIKE 'preflight_%' OR tablename LIKE 'hibernation_%');
--
--   -- Models per provider:
--   SELECT provider, COUNT(*) AS total, SUM(CASE WHEN enabled THEN 1 ELSE 0 END) AS enabled
--   FROM public.ai_models GROUP BY provider ORDER BY provider;
--
--   -- View enabled models in order:
--   SELECT * FROM public.v_ai_enabled_models;
--
--   -- View AI provider status summary (models + keys + blocked state):
--   SELECT * FROM public.v_ai_provider_summary;
--
--   -- View currently blocked AI keys with time remaining:
--   SELECT * FROM public.v_ai_blocked_keys;
--
--   -- View current AI quota JSON state:
--   SELECT id, keys, providers, updated_at FROM public.ai_quota_state WHERE id='main';
--
--   -- View active preflight cooldowns:
--   SELECT * FROM public.v_preflight_active_cooldowns;
--
--   -- View preflight cooldown summary:
--   SELECT * FROM public.v_preflight_cooldown_summary;
--
--   -- View current preflight JSON state:
--   SELECT id, entries, updated_at FROM public.preflight_cooldowns_state WHERE id='main';
--
--   -- View current hibernation status:
--   SELECT * FROM public.v_hibernation_status;
--
--   -- View recent hibernation events (last 30 days):
--   SELECT * FROM public.v_hibernation_recent_events;
--
--   -- View hibernation event counts by type:
--   SELECT event_type, COUNT(*) AS count, MAX(occurred_at) AS last_event
--   FROM public.hibernation_events
--   WHERE occurred_at > now() - INTERVAL '7 days'
--   GROUP BY event_type ORDER BY count DESC;
--
--   -- Manually add a new model:
--   INSERT INTO public.ai_models (provider, model_id, enabled, display_name, sort_order)
--   VALUES ('openrouter', 'deepseek/deepseek-r1:free', TRUE, 'DeepSeek R1 (free)', 0)
--   ON CONFLICT (provider, model_id) DO UPDATE SET enabled = TRUE, updated_at = now();
--
--   -- Disable a model:
--   UPDATE public.ai_models SET enabled = FALSE WHERE provider='groq' AND model_id='mixtral-8x7b-32768';
--
--   -- Reorder: move a model to position 1 (highest priority)
--   UPDATE public.ai_models SET sort_order = 0 WHERE provider='groq' AND model_id='llama-3.3-70b-versatile';
--
--   -- Unblock all AI keys for a specific provider:
--   SELECT public.unblock_all_ai_keys('groq');
--
--   -- Unblock all AI keys for all providers:
--   SELECT public.unblock_all_ai_keys(NULL);
--
--   -- Cleanup expired AI quota blocks:
--   SELECT public.cleanup_expired_ai_quota();
--
--   -- Clear all preflight cooldowns:
--   SELECT public.clear_all_preflight_cooldowns();
--
--   -- Clear preflight cooldown for a specific channel:
--   SELECT public.clear_preflight_cooldown('channel-id-here');
--
--   -- Clear preflight cooldown for a specific channel+source:
--   SELECT public.clear_preflight_cooldown('channel-id-here', 'source-id-here');
--
--   -- Cleanup expired preflight cooldowns:
--   SELECT public.cleanup_expired_preflight_cooldowns();
--
--   -- Force-wake the bot from SQL (if stuck in hibernation):
--   SELECT public.force_wake_bot('Manual wake from SQL admin');
--
--   -- Force-sleep the bot from SQL (emergency stop):
--   SELECT public.force_sleep_bot('Emergency hibernation');
--
--   -- Log a manual hibernation event:
--   SELECT public.log_hibernation_event('reset_counter', 'Manual counter reset by admin');
--
--   -- View daily report counts by type (last 30 days):
--   SELECT * FROM public.v_daily_report_counts;
--
--   -- View notification stats by category (last 7 days):
--   SELECT * FROM public.v_notification_stats_7d;
--
--   -- View top 20 most frequent notifications (last 7 days):
--   SELECT * FROM public.v_top_notifications_7d;
--
--   -- View recent daily reports:
--   SELECT report_type, sent_at, LEFT(summary, 80) AS summary_preview
--   FROM public.daily_reports_history
--   ORDER BY sent_at DESC LIMIT 10;
--
--   -- View recent notifications:
--   SELECT category, notification_key, title, sent_at, was_throttled
--   FROM public.notification_log
--   ORDER BY sent_at DESC LIMIT 20;
--
--   -- Cleanup old notification logs (keep 30 days):
--   SELECT public.cleanup_old_notification_logs();
--
--   -- Manually log a daily report:
--   SELECT public.log_daily_report('performance', 'Manual report', 'Full text...');
--
--   -- Schedule weekly cleanup (optional, using pg_cron extension):
--   -- SELECT cron.schedule('cleanup-notifications', '0 3 * * 0', 'SELECT public.cleanup_old_notification_logs();');
--
--   -- Schedule hourly cleanups (optional, using pg_cron extension):
--   -- CREATE EXTENSION IF NOT EXISTS pg_cron;
--   -- SELECT cron.schedule('cleanup-ai-quota', '0 * * * *', 'SELECT public.cleanup_expired_ai_quota();');
--   -- SELECT cron.schedule('cleanup-preflight', '5 * * * *', 'SELECT public.cleanup_expired_preflight_cooldowns();');
-- ============================================================================
