-- 0006: cross-representation table correspondence (derived) and the "review" schema for
-- human decisions, which are not rebuildable from source and therefore live outside
-- "extracted". Review rows are append-only.

CREATE SCHEMA IF NOT EXISTS review;

CREATE TABLE extracted.table_correspondence (
    correspondence_id   text    PRIMARY KEY,               -- corr_<opaque>
    page_id             bigint  NOT NULL,
    version_number      integer NOT NULL,
    left_artifact_id    text    NOT NULL
                        REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    right_artifact_id   text    NOT NULL
                        REFERENCES extracted.parse_artifacts (artifact_id) ON DELETE CASCADE,
    left_representation  text   NOT NULL,
    right_representation text   NOT NULL,
    left_table_id       text,                              -- NULL: right-only occurrence
    right_table_id      text,                              -- NULL: no counterpart
    state               text    NOT NULL
                        CHECK (state IN ('confirmed', 'tentative', 'unmatched', 'conflicting')),
    method              text    NOT NULL,
    evidence            jsonb   NOT NULL DEFAULT '{}'::jsonb,
    algorithm_version   text    NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (left_table_id IS NOT NULL OR right_table_id IS NOT NULL)
);
CREATE INDEX table_correspondence_page_idx
    ON extracted.table_correspondence (page_id, version_number);
CREATE INDEX table_correspondence_left_idx ON extracted.table_correspondence (left_table_id);
CREATE INDEX table_correspondence_right_idx ON extracted.table_correspondence (right_table_id);

-- A human resolution of a tentative or conflicting correspondence. The latest decision per
-- correspondence wins; earlier ones remain as history.
CREATE TABLE review.correspondence_decisions (
    decision_id        bigserial PRIMARY KEY,
    correspondence_id  text   NOT NULL
                       REFERENCES extracted.table_correspondence (correspondence_id)
                       ON DELETE CASCADE,
    decision           text   NOT NULL CHECK (decision IN ('confirmed', 'distinct')),
    actor              text   NOT NULL,
    reason             text   NOT NULL,
    decided_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX correspondence_decisions_corr_idx
    ON review.correspondence_decisions (correspondence_id, decided_at DESC);
