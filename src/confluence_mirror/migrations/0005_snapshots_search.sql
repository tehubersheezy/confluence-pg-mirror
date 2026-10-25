-- 0005: keep superseded body captures, add the local search projection, and allow
-- page-scoped and metadata-only sync runs. Additive except for widening one CHECK.

-- A re-fetch of the same page version can return a different body (rendered macros such
-- as Include Page change without a version bump). page_content keeps the latest capture;
-- the one it replaces is archived here, so every snapshot stays reproducible.
CREATE TABLE source.superseded_bodies (
    page_id         bigint  NOT NULL,
    version_number  integer NOT NULL,
    representation  text    NOT NULL
                    CHECK (representation IN ('storage', 'export_view', 'view')),
    body_sha256     text    NOT NULL,
    body            text    NOT NULL,
    captured_at     timestamptz NOT NULL,             -- fetched_at of the replaced capture
    superseded_at   timestamptz NOT NULL DEFAULT now(),
    sync_run_id     bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, version_number, representation, body_sha256),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_content (page_id, version_number) ON DELETE CASCADE
);

-- Text projection of parsed storage artifacts for `confex search --text`. Rebuilt by
-- `confex parse`; never written by sync.
CREATE TABLE extracted.search_blocks (
    artifact_id     text    NOT NULL
                    REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    block_id        text    NOT NULL,
    page_id         bigint  NOT NULL,
    version_number  integer NOT NULL,
    block_kind      text    NOT NULL,
    section_id      text,
    table_id        text,
    cell_id         text,
    text            text    NOT NULL,
    indexed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, block_id)
);
CREATE INDEX search_blocks_page_idx ON extracted.search_blocks (page_id, version_number);
CREATE INDEX search_blocks_fts_idx ON extracted.search_blocks
    USING gin (to_tsvector('simple', text));

ALTER TABLE sync.sync_runs DROP CONSTRAINT sync_runs_mode_check;
ALTER TABLE sync.sync_runs ADD CONSTRAINT sync_runs_mode_check
    CHECK (mode IN ('full', 'incremental', 'extract', 'pages', 'metadata'));
