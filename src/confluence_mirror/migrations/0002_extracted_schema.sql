-- 0002: data extracted from page_content.body_storage. Every row is keyed by the
-- (page_id, version_number) it was derived from, plus an element path and the extractor
-- version, so any value can be traced back to the exact source markup.

-- 8. page_macros ------------------------------------------------------------------
CREATE TABLE extracted.page_macros (
    page_id            bigint  NOT NULL,
    version_number     integer NOT NULL,
    macro_index        integer NOT NULL,          -- document order, 0-based
    macro_name         text    NOT NULL,
    macro_id           text,                      -- ac:macro-id
    schema_version     text,
    parent_macro_index integer,                   -- enclosing macro (nesting)
    depth              integer NOT NULL DEFAULT 0,
    element_path       text    NOT NULL,          -- provenance within body_storage
    parameters         jsonb   NOT NULL DEFAULT '{}'::jsonb,
    body_type          text CHECK (body_type IN ('rich-text', 'plain-text') OR body_type IS NULL),
    body_storage       text,                      -- inner markup of the macro body
    body_text          text,                      -- plain text of the body
    source_markup      text    NOT NULL,          -- the macro element as it appeared
    extractor_version  text    NOT NULL,
    extracted_at       timestamptz NOT NULL DEFAULT now(),
    sync_run_id        bigint REFERENCES sync.sync_runs (sync_run_id) ON DELETE SET NULL,
    PRIMARY KEY (page_id, version_number, macro_index),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_content (page_id, version_number) ON DELETE CASCADE,
    FOREIGN KEY (page_id, version_number, parent_macro_index)
        REFERENCES extracted.page_macros (page_id, version_number, macro_index)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX page_macros_name_idx ON extracted.page_macros (macro_name);
CREATE INDEX page_macros_params_idx ON extracted.page_macros USING gin (parameters jsonb_path_ops);

-- 9. macro_extractions (macro-specific structured interpretation) --------------------
CREATE TABLE extracted.macro_extractions (
    page_id           bigint  NOT NULL,
    version_number    integer NOT NULL,
    macro_index       integer NOT NULL,
    extraction_type   text    NOT NULL,           -- e.g. 'code', 'panel', 'jira', 'page-properties'
    data              jsonb   NOT NULL,
    extractor_version text    NOT NULL,
    extracted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, version_number, macro_index, extraction_type),
    FOREIGN KEY (page_id, version_number, macro_index)
        REFERENCES extracted.page_macros (page_id, version_number, macro_index) ON DELETE CASCADE
);
CREATE INDEX macro_extractions_type_idx ON extracted.macro_extractions (extraction_type);
CREATE INDEX macro_extractions_data_idx ON extracted.macro_extractions USING gin (data jsonb_path_ops);

-- 10. page_tables -----------------------------------------------------------------
CREATE TABLE extracted.page_tables (
    page_id            bigint  NOT NULL,
    version_number     integer NOT NULL,
    table_index        integer NOT NULL,
    parent_table_index integer,                   -- nested tables
    macro_index        integer,                   -- enclosing macro, if any
    element_path       text    NOT NULL,
    row_count          integer NOT NULL,
    column_count       integer NOT NULL,
    header             jsonb,                     -- ["col", ...] when a header row exists
    cells              jsonb   NOT NULL,          -- [[{"text","is_header","colspan","rowspan"}]]
    records            jsonb,                     -- [{"col": "value"}] when a header row exists
    source_markup      text    NOT NULL,
    extractor_version  text    NOT NULL,
    extracted_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, version_number, table_index),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_content (page_id, version_number) ON DELETE CASCADE
);

-- 11. page_links ------------------------------------------------------------------
CREATE TABLE extracted.page_links (
    page_id           bigint  NOT NULL,
    version_number    integer NOT NULL,
    link_index        integer NOT NULL,
    link_type         text    NOT NULL
                      CHECK (link_type IN ('page', 'blogpost', 'attachment', 'space', 'user',
                                           'url', 'anchor', 'include')),
    source_element    text    NOT NULL,           -- 'ac:link', 'a', 'ac:image', 'macro:include', ...
    element_path      text    NOT NULL,
    macro_index       integer,
    target_page_id    bigint,                     -- resolved page (may be outside the mirror)
    target_space_key  text,
    target_title      text,
    target_filename   text,
    target_user_key   text,
    target_url        text,
    anchor            text,
    link_text         text,
    extractor_version text    NOT NULL,
    extracted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, version_number, link_index),
    FOREIGN KEY (page_id, version_number)
        REFERENCES source.page_content (page_id, version_number) ON DELETE CASCADE
);
CREATE INDEX page_links_target_idx ON extracted.page_links (target_page_id);
CREATE INDEX page_links_unresolved_idx ON extracted.page_links (target_space_key, target_title)
    WHERE target_page_id IS NULL AND target_title IS NOT NULL;
