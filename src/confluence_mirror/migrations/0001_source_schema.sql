-- 0001: schemas, sync bookkeeping, and raw source tables mirrored from Confluence.
-- Source data (schema "source") is what the REST API returned, unchanged in meaning.
-- Derived data lives in schema "extracted" (0002) and is always rebuildable from source.

CREATE SCHEMA IF NOT EXISTS sync;
CREATE SCHEMA IF NOT EXISTS source;
CREATE SCHEMA IF NOT EXISTS extracted;

-- 14. sync_runs -------------------------------------------------------------
CREATE TABLE sync.sync_runs (
    sync_run_id      bigserial PRIMARY KEY,
    space_key        text        NOT NULL,
    mode             text        NOT NULL CHECK (mode IN ('full', 'incremental', 'extract')),
    status           text        NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'abandoned')),
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    -- Incremental window actually used (cursor_from NULL for full syncs).
    cursor_from      timestamptz,
    cursor_to        timestamptz,
    pages_listed     integer     NOT NULL DEFAULT 0,
    pages_fetched    integer     NOT NULL DEFAULT 0,
    pages_unchanged  integer     NOT NULL DEFAULT 0,
    pages_moved      integer     NOT NULL DEFAULT 0,
    pages_deleted    integer     NOT NULL DEFAULT 0,
    pages_failed     integer     NOT NULL DEFAULT 0,
    attachments_seen integer     NOT NULL DEFAULT 0,
    -- Per-item failures: [{"page_id": ..., "stage": ..., "error": ...}] (never credentials).
    errors           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    stats            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    tool_version     text        NOT NULL
);
CREATE INDEX sync_runs_space_started_idx ON sync.sync_runs (space_key, started_at DESC);
CREATE INDEX sync_runs_running_idx ON sync.sync_runs (status) WHERE status = 'running';

-- 1. spaces ------------------------------------------------------------------
CREATE TABLE source.spaces (
    space_id               bigint PRIMARY KEY,
    space_key              text        NOT NULL UNIQUE,
    name                   text,
    space_type             text,
    status                 text,
    description_plain      text,
    -- No FK: the homepage may not be mirrored yet (or ever, if restricted).
    homepage_id            bigint,
    -- TRUE for spaces explicitly synced; FALSE for spaces only seen as a move target.
    sync_enabled           boolean     NOT NULL DEFAULT false,
    raw                    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id       bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    last_full_sync_at      timestamptz,
    -- Start time of the last succeeded sync; incremental windows begin here.
    last_successful_sync_at timestamptz
);

-- 2. space_properties ----------------------------------------------------------
CREATE TABLE source.space_properties (
    space_id         bigint NOT NULL REFERENCES source.spaces (space_id) ON DELETE CASCADE,
    property_key     text   NOT NULL,
    property_id      text,
    value            jsonb,
    version_number   integer,
    raw              jsonb  NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (space_id, property_key)
);

-- 3. pages (current state; one row per Confluence content id) --------------------
CREATE TABLE source.pages (
    page_id            bigint PRIMARY KEY,
    space_id           bigint  NOT NULL REFERENCES source.spaces (space_id),
    content_type       text    NOT NULL DEFAULT 'page',
    title              text    NOT NULL,
    status             text    NOT NULL,
    -- Hierarchy. No FK on parent_id: parents can arrive later in a listing or be unreadable.
    parent_id          bigint,
    ancestor_ids       bigint[] NOT NULL DEFAULT '{}',
    depth              integer  NOT NULL DEFAULT 0,
    position           integer,
    current_version    integer  NOT NULL,
    created_at         timestamptz,
    created_by         text,
    last_modified_at   timestamptz,
    last_modified_by   text,
    webui_path         text,
    tinyui_path        text,
    raw                jsonb    NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id   bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    previous_parent_id bigint,
    moved_at           timestamptz,
    is_deleted         boolean  NOT NULL DEFAULT false,
    deleted_at         timestamptz,
    -- 'trashed' | 'purged' | 'not_visible' | 'moved_out'
    deletion_reason    text
);
CREATE INDEX pages_space_idx       ON source.pages (space_id) WHERE NOT is_deleted;
CREATE INDEX pages_parent_idx      ON source.pages (parent_id);
CREATE INDEX pages_space_title_idx ON source.pages (space_id, title);
CREATE INDEX pages_ancestors_idx   ON source.pages USING gin (ancestor_ids);
CREATE INDEX pages_modified_idx    ON source.pages (last_modified_at);

-- 5. page_versions (immutable per (page, version); history as observed) ------------
CREATE TABLE source.page_versions (
    page_id         bigint  NOT NULL REFERENCES source.pages (page_id) ON DELETE CASCADE,
    version_number  integer NOT NULL,
    title           text    NOT NULL,
    parent_id       bigint,
    space_id        bigint,
    created_at      timestamptz,
    created_by      text,
    created_by_display text,
    message         text,
    minor_edit      boolean,
    raw             jsonb   NOT NULL DEFAULT '{}'::jsonb,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    sync_run_id     bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, version_number)
);

-- 4. page_content (bodies per version) ----------------------------------------------
CREATE TABLE source.page_content (
    page_id          bigint  NOT NULL,
    version_number   integer NOT NULL,
    body_storage     text,
    body_view        text,
    body_export_view text,
    storage_sha256   text,
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    -- Set when extracted.* rows were (re)built from this body; a NULL or outdated value
    -- makes the next sync re-extract offline (failed extraction, or an extractor upgrade).
    extractor_version text,
    extracted_at     timestamptz,
    sync_run_id      bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, version_number),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_versions (page_id, version_number) ON DELETE CASCADE
);

-- 6. page_labels ----------------------------------------------------------------
CREATE TABLE source.page_labels (
    page_id          bigint NOT NULL REFERENCES source.pages (page_id) ON DELETE CASCADE,
    prefix           text   NOT NULL DEFAULT 'global',
    name             text   NOT NULL,
    label_id         text,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, prefix, name)
);
CREATE INDEX page_labels_name_idx ON source.page_labels (name);

-- 7. page_properties (content properties) -------------------------------------------
CREATE TABLE source.page_properties (
    page_id          bigint NOT NULL REFERENCES source.pages (page_id) ON DELETE CASCADE,
    property_key     text   NOT NULL,
    property_id      text,
    value            jsonb,
    version_number   integer,
    raw              jsonb  NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, property_key)
);
CREATE INDEX page_properties_value_idx ON source.page_properties USING gin (value jsonb_path_ops);

-- 12. attachments -----------------------------------------------------------------
CREATE TABLE source.attachments (
    attachment_id    text   PRIMARY KEY,           -- e.g. 'att123456'
    page_id          bigint NOT NULL REFERENCES source.pages (page_id) ON DELETE CASCADE,
    title            text   NOT NULL,
    media_type       text,
    file_size        bigint,
    comment          text,
    version_number   integer,
    status           text,
    created_at       timestamptz,
    created_by       text,
    download_path    text,
    -- Populated only when attachment downloading is enabled.
    local_path       text,
    content_sha256   text,
    downloaded_version integer,
    raw              jsonb  NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    is_deleted       boolean NOT NULL DEFAULT false,
    deleted_at       timestamptz
);
CREATE INDEX attachments_page_idx  ON source.attachments (page_id) WHERE NOT is_deleted;
CREATE INDEX attachments_title_idx ON source.attachments (page_id, title);

-- 13. access_restrictions (direct restrictions on a page; inherited ones live on ancestors)
CREATE TABLE source.access_restrictions (
    page_id          bigint NOT NULL REFERENCES source.pages (page_id) ON DELETE CASCADE,
    operation        text   NOT NULL CHECK (operation IN ('read', 'update')),
    subject_type     text   NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id       text   NOT NULL,             -- userKey or group name
    subject_name     text,                        -- username / display name
    raw              jsonb  NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, operation, subject_type, subject_id)
);
CREATE INDEX access_restrictions_subject_idx ON source.access_restrictions (subject_type, subject_id);
