-- 0007: the extraction pipeline (schema "pipeline") and human review/export (schema
-- "review"). Source and parsed layers are untouched. Model responses, candidate revisions
-- and review events are immutable; run/item status and leases are the only mutable state.

CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE pipeline.runs (
    run_id           text PRIMARY KEY,                     -- run_<opaque>
    command          text NOT NULL,                        -- classify | extract | validate | run
    scope            jsonb NOT NULL,
    requested_stages jsonb NOT NULL,
    status           text NOT NULL
                     CHECK (status IN ('planned', 'running', 'succeeded', 'partial', 'failed',
                                       'cancelled', 'blocked')),
    config_digest    text NOT NULL,
    versions         jsonb NOT NULL,
    principal_scope  text NOT NULL,
    input_manifest   jsonb NOT NULL DEFAULT '[]'::jsonb,   -- frozen before extraction
    stage_counts     jsonb NOT NULL DEFAULT '{}'::jsonb,
    usage            jsonb NOT NULL DEFAULT '{}'::jsonb,
    budget           jsonb NOT NULL DEFAULT '{}'::jsonb,
    diagnostics      jsonb NOT NULL DEFAULT '[]'::jsonb,
    parent_run_id    text REFERENCES pipeline.runs (run_id) ON DELETE SET NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
CREATE INDEX runs_started_idx ON pipeline.runs (started_at DESC);

CREATE TABLE pipeline.run_items (
    run_id           text NOT NULL REFERENCES pipeline.runs (run_id) ON DELETE CASCADE,
    stage            text NOT NULL CHECK (stage IN ('classify', 'plan', 'extract', 'validate')),
    item_id          text NOT NULL,                        -- table_id or unit_id
    status           text NOT NULL
                     CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'blocked',
                                       'skipped')),
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts         integer NOT NULL DEFAULT 0,
    lease_owner      text,
    lease_expires_at timestamptz,
    output_ref       text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stage, item_id)
);
CREATE INDEX run_items_status_idx ON pipeline.run_items (run_id, status);

CREATE TABLE pipeline.attempts (
    attempt_id        text PRIMARY KEY,                    -- attempt_<opaque>
    run_id            text REFERENCES pipeline.runs (run_id) ON DELETE SET NULL,
    target_id         text NOT NULL,                       -- unit_id or table_id
    stage             text NOT NULL CHECK (stage IN ('classify', 'extract', 'repair')),
    extractor         text NOT NULL CHECK (extractor IN ('rules', 'model')),
    attempt_number    integer NOT NULL,
    parent_attempt_id text REFERENCES pipeline.attempts (attempt_id),
    -- The policy-approved body as captured before transport; NULL for rule extraction and
    -- under an explicitly chosen reduced-audit policy. Never holds credentials.
    request_body      jsonb,
    request_hash      text,
    request_context   jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_text     text,                                -- final output only; never thinking
    response_meta     jsonb NOT NULL DEFAULT '{}'::jsonb,
    outcome           text NOT NULL
                      CHECK (outcome IN ('succeeded', 'refusal', 'truncated', 'empty',
                                         'unsupported_shape', 'outcome_unknown', 'error',
                                         'policy_denied', 'budget_exhausted')),
    usage             jsonb NOT NULL DEFAULT '{}'::jsonb,
    cost_usd          numeric,
    error             text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);
CREATE INDEX attempts_target_idx ON pipeline.attempts (target_id, started_at);
CREATE INDEX attempts_run_idx ON pipeline.attempts (run_id);

CREATE TABLE pipeline.classifications (
    classification_id text PRIMARY KEY,                    -- cls_<opaque>
    table_id          text NOT NULL,
    artifact_id       text NOT NULL
                      REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    page_id           bigint NOT NULL,
    version_number    integer NOT NULL,
    family            text NOT NULL,
    status            text NOT NULL,
    method            text NOT NULL,
    profile           text,
    schema_ref        text,
    result            jsonb NOT NULL,
    attempt_id        text REFERENCES pipeline.attempts (attempt_id),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX classifications_table_idx ON pipeline.classifications (table_id, created_at DESC);

CREATE TABLE pipeline.units (
    unit_id           text PRIMARY KEY,                    -- unit_<opaque>
    table_id          text NOT NULL,
    artifact_id       text NOT NULL
                      REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    classification_id text NOT NULL REFERENCES pipeline.classifications (classification_id),
    page_id           bigint NOT NULL,
    version_number    integer NOT NULL,
    profile           text NOT NULL,
    schema_ref        text NOT NULL,
    chunk_index       integer NOT NULL,
    chunk_count       integer NOT NULL,
    status            text NOT NULL CHECK (status IN ('planned', 'oversized', 'blocked')),
    manifest          jsonb NOT NULL,
    payload           jsonb NOT NULL,                      -- redacted evidence object
    payload_hash      text NOT NULL,
    redaction_map     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- local only; never sent
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX units_table_idx ON pipeline.units (table_id);

CREATE TABLE pipeline.candidate_revisions (
    revision_id        text PRIMARY KEY,                   -- rev_<opaque>
    record_key         text NOT NULL,                      -- stable across revisions
    parent_revision_id text REFERENCES pipeline.candidate_revisions (revision_id),
    revision_number    integer NOT NULL,
    origin             text NOT NULL CHECK (origin IN ('rules', 'model', 'human_correction')),
    unit_id            text REFERENCES pipeline.units (unit_id),
    attempt_id         text REFERENCES pipeline.attempts (attempt_id),
    run_id             text REFERENCES pipeline.runs (run_id) ON DELETE SET NULL,
    page_id            bigint NOT NULL,
    version_number     integer NOT NULL,
    space_id           bigint,
    table_id           text NOT NULL,
    artifact_id        text NOT NULL,
    snapshot_id        text NOT NULL,
    schema_ref         text NOT NULL,
    profile            text NOT NULL,
    record_kind        text NOT NULL,
    record             jsonb NOT NULL,                     -- fields, relationships, source rows
    content_hash       text NOT NULL,
    dependency_hash    text NOT NULL,
    patch              jsonb,
    created_by         text,
    reason             text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (record_key, revision_number)
);
CREATE INDEX candidate_revisions_page_idx ON pipeline.candidate_revisions (page_id);
CREATE INDEX candidate_revisions_schema_idx ON pipeline.candidate_revisions (schema_ref);
CREATE INDEX candidate_revisions_run_idx ON pipeline.candidate_revisions (run_id);

CREATE TABLE pipeline.validation_results (
    validation_id     text PRIMARY KEY,                    -- val_<opaque>
    revision_id       text NOT NULL
                      REFERENCES pipeline.candidate_revisions (revision_id) ON DELETE CASCADE,
    validator_version text NOT NULL,
    status            text NOT NULL CHECK (status IN ('passed', 'warnings', 'failed', 'blocked')),
    issues            jsonb NOT NULL,
    metrics           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX validation_results_revision_idx
    ON pipeline.validation_results (revision_id, created_at DESC);

-- Candidate identity matches across records; proposals only, never merges.
CREATE TABLE pipeline.entity_match_proposals (
    match_id     text PRIMARY KEY,                         -- match_<opaque>
    schema_ref   text NOT NULL,
    record_kind  text NOT NULL,
    match_key    jsonb NOT NULL,
    scope        jsonb NOT NULL,
    record_keys  jsonb NOT NULL,
    state        text NOT NULL CHECK (state IN ('proposed', 'conflicting')),
    evidence     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE review.review_events (
    event_id              bigserial PRIMARY KEY,
    record_key            text NOT NULL,
    sequence              integer NOT NULL,                -- optimistic concurrency per record
    revision_id           text NOT NULL REFERENCES pipeline.candidate_revisions (revision_id),
    decision              text NOT NULL CHECK (decision IN ('approved', 'rejected', 'corrected')),
    corrected_revision_id text REFERENCES pipeline.candidate_revisions (revision_id),
    actor                 text NOT NULL,
    reason                text NOT NULL,
    dependency_hash       text NOT NULL,
    policy_version        text NOT NULL,
    snapshot_id           text NOT NULL,
    validation_id         text REFERENCES pipeline.validation_results (validation_id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (record_key, sequence)
);
CREATE INDEX review_events_revision_idx ON review.review_events (revision_id);

CREATE TABLE review.exports (
    export_id     text PRIMARY KEY,                        -- exp_<opaque>
    actor         text NOT NULL,
    scope         jsonb NOT NULL,
    export_format text NOT NULL CHECK (export_format IN ('json', 'jsonl', 'csv')),
    projection    text NOT NULL,
    path          text NOT NULL,
    content_key   text NOT NULL,                           -- digest of the selected revisions
    record_count  integer NOT NULL,
    excluded      jsonb NOT NULL,
    manifest      jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE review.export_items (
    export_id   text NOT NULL REFERENCES review.exports (export_id) ON DELETE CASCADE,
    revision_id text NOT NULL REFERENCES pipeline.candidate_revisions (revision_id),
    PRIMARY KEY (export_id, revision_id)
);
