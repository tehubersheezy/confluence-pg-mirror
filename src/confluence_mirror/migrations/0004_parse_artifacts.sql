-- 0004: structural parse artifacts written by `confex parse`. Additive only.
-- An artifact is immutable: artifact_id is derived from (snapshot, parser version, parse
-- config), and a snapshot from (page, version, representation, body SHA-256). Re-parsing an
-- unchanged body is therefore a no-op, and a changed body or parser yields a new artifact
-- instead of overwriting the old one.

CREATE TABLE extracted.parse_artifacts (
    artifact_id        text    PRIMARY KEY,               -- doc_<opaque>
    snapshot_id        text    NOT NULL,                  -- snap_<opaque>
    instance_id        text    NOT NULL,
    page_id            bigint  NOT NULL,
    version_number     integer NOT NULL,
    representation     text    NOT NULL
                       CHECK (representation IN ('storage', 'export_view', 'view')),
    body_sha256        text    NOT NULL,
    parser_version     text    NOT NULL,
    config_fingerprint text    NOT NULL,
    status             text    NOT NULL
                       CHECK (status IN ('parsed', 'recovered', 'unsupported_due_to_limit')),
    table_count        integer NOT NULL,
    -- Full document model: nodes, evidence blocks, tables with cells and placements.
    document           jsonb   NOT NULL,
    diagnostics        jsonb   NOT NULL DEFAULT '[]'::jsonb,
    parsed_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_content (page_id, version_number) ON DELETE CASCADE
);
CREATE INDEX parse_artifacts_page_idx
    ON extracted.parse_artifacts (page_id, version_number, representation);
CREATE INDEX parse_artifacts_snapshot_idx ON extracted.parse_artifacts (snapshot_id);

-- One row per table occurrence, so tables can be listed and resolved without loading
-- whole documents. Cells live in the artifact's document.
CREATE TABLE extracted.table_occurrences (
    table_id            text    PRIMARY KEY,              -- tbl_<opaque>
    artifact_id         text    NOT NULL
                        REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    page_id             bigint  NOT NULL,
    version_number      integer NOT NULL,
    representation      text    NOT NULL,
    ordinal             integer NOT NULL,                 -- 1-based, document order
    parent_table_id     text,
    parent_cell_id      text,
    depth               integer NOT NULL,
    element_path        text    NOT NULL,
    row_count           integer NOT NULL,
    column_count        integer NOT NULL,
    geometry_status     text    NOT NULL
                        CHECK (geometry_status IN ('valid', 'recovered', 'invalid',
                                                   'unsupported_due_to_limit')),
    extraction_eligible boolean NOT NULL,
    heading_path        jsonb   NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (artifact_id, ordinal)
);
CREATE INDEX table_occurrences_page_idx
    ON extracted.table_occurrences (page_id, version_number, representation);
CREATE INDEX table_occurrences_parent_idx ON extracted.table_occurrences (parent_table_id);
