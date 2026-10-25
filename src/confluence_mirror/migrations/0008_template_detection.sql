-- 0008: model-based page-template detection (`confex templates detect`). Additive only.
-- A detection covers one parent page whose children are all leaves: the model proposes a
-- template from a sample of the children, then checks every child against it. Rows are
-- immutable: detection_id is derived from the inputs (children and their versions, model,
-- prompt and detector versions), so re-running on unchanged pages reuses the stored answer.

ALTER TABLE pipeline.attempts DROP CONSTRAINT attempts_stage_check;
ALTER TABLE pipeline.attempts ADD CONSTRAINT attempts_stage_check
    CHECK (stage IN ('classify', 'extract', 'repair', 'template_propose', 'template_check'));

CREATE TABLE pipeline.template_detections (
    detection_id       text PRIMARY KEY,                    -- tmpl_<opaque>
    parent_page_id     bigint NOT NULL,
    space_key          text NOT NULL,
    input_digest       text NOT NULL,
    detector_version   text NOT NULL,
    prompt_version     text NOT NULL,
    model              jsonb NOT NULL,
    children           jsonb NOT NULL,                      -- [{page_id, version_number}]
    sample             jsonb NOT NULL,                      -- page IDs shown to the proposer
    status             text NOT NULL
                       CHECK (status IN ('template', 'mixed', 'no_template', 'unverified',
                                         'failed')),
    conformance        numeric,                             -- share of checked children
    proposal           jsonb,                               -- the proposer's template
    pages              jsonb NOT NULL DEFAULT '[]'::jsonb,  -- per-child check results
    issues             jsonb NOT NULL DEFAULT '[]'::jsonb,  -- corrections and checker notes
    propose_attempt_id text,
    check_attempt_ids  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX template_detections_parent_idx
    ON pipeline.template_detections (parent_page_id, created_at DESC);
CREATE INDEX template_detections_space_idx
    ON pipeline.template_detections (space_key, created_at DESC);
