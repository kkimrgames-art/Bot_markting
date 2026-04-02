-- Table: facecam_storage_index
CREATE TABLE IF NOT EXISTS public.facecam_storage_index (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            object_path TEXT NOT NULL,
            name TEXT,
            size_bytes BIGINT,
            created_at TIMESTAMPTZ DEFAULT now()
        );

-- Table: source_cool_downs
CREATE TABLE IF NOT EXISTS public.source_cool_downs (
            source_url TEXT PRIMARY KEY,
            expires_at TIMESTAMPTZ,
            reason TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
