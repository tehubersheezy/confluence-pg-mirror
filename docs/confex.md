# confex

`confex` is the exploration, structural-inspection and evidence-backed extraction CLI
described in the *Confex Complete Spec v1*. The spec is a private planning document and is
not committed. It lives in this repository next to `confluence-mirror` and uses the same
mirror database.

> Preserve what Confluence supplied. Compute structure in code. Ask the model to interpret
> meaning. Validate evidence. Require approval before treating a candidate as accepted data.

## Install

There is nothing to install beyond pip packages: no conda, Docker or admin rights.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # mirror, navigation, parsing, inspection, rule extraction
pip install -e '.[model]'        # optional: Claude extraction (official Anthropic SDK)
confex config validate
confex doctor --check-db
```

The `model` extra adds `anthropic` and its dependencies. These are pure-Python wheels plus
prebuilt `pydantic-core`/`jiter` wheels for macOS, so no compiler is needed. Only
`extraction/provider.py` imports it, and only when a model call is about to happen.

## Status by milestone

| Milestone | Delivered | Not yet / limits |
|---|---|---|
| **M0** contract | This document: schema mapping, identity recipe, config groups, output envelope `confex.cli.v1`, synthetic fixtures | Lockfile: the repo pins ranges in `pyproject.toml`. Generate a hashed `requirements.lock` for your approved interpreter with `pip-compile --generate-hashes`. |
| **M1** mirror and navigation | `sync` (`--space`, `--page`, `--metadata-only`, `--changed-only`, `--reconcile`, `--representations`); `spaces`, `pages show/children/ancestors/siblings/tree/outline/tables/read/labels/properties/attachments/versions/macros/links/diff`; `search` (local substring + projection; `--cql` live); `--data-source live` for metadata, read, bounded tree, CQL; same-version body changes archived | Live mode covers metadata and reads; parsed structure is mirror-only, as the spec requires. Local search is substring, not ranked. |
| **M2** structural parser | Storage and HTML adapters; scoped headings; physical cells, row groups, `origin/covered/empty/conflict` geometry; `rowspan=0`; group-crossing, overlap and over-long spans flagged; nested tables; evidence blocks; context; limits | Exact source offsets for `raw` (lxml exposes none, so `raw` is a labeled DOM serialization). |
| **M3** inspection | `tables show` (overview, grid, physical, cell, context, raw, payload), `tables list/tree/compare/selection/resolve`, `align` (cross-representation correspondence and extraction selection) | No rendering-order (CSS) projection; rows are in source DOM order. |
| **M4** extraction | Registry (5 schemas, 5 profiles), deterministic classification and optional model classification, record-group units, one canonical payload/request serializer, rule extractor, Claude Haiku 5.5 adapter, egress gate, redaction, `classify/plan/extract`, `runs request` | No remote token counting (local estimates only, labeled as such). Model classification is used only where policy allows. |
| **M5** review and export | `validate`; `records list/show/evidence/diff`; `review queue/show/approve/reject/correct`; entity-match proposals; `export` (JSON/JSONL packages, flat CSV) | No enterprise identity or signed review events; the actor is the local OS user. |
| **M6** hardening | `runs list/show/items/request/resume/retry`, leases, budgets with dated pricing, `run` orchestration with `--from-stage/--to-stage/--dry-run`, `doctor`, `config show/validate`, `eval run/report/compare`, benchmark script | The bundled eval set is a small synthetic **development** set. A held-out set labelled on your own templates is still needed before production use (see below). |
| Later (spec) | — | TUI (`browse`), `investigate` agent, ServiceNow publishing, Confluence write-back: not implemented, by design. |

## Workflow

```bash
confex sync --space ITOPS --metadata-only
confex spaces tree ITOPS --depth 3
confex sync --page 12345 --representations storage,export_view
confex parse --page 12345 && confex align --page 12345
confex pages outline 12345 --representation storage --tables --macros
confex tables show 12345:t02 --representation storage --view grid
confex tables show 12345:t02 --representation storage --view cell --cell R3C1

confex classify --page 12345                      # deterministic routing; no model
confex extract --page 12345 --plan-only           # what would happen; no writes
confex tables show tbl_<id> --view payload        # the exact request an extraction sends
confex extract --page 12345                       # rules where a template matched
confex extract --page 12345 --extractor model     # Claude, only if [privacy] approves egress
confex review queue --space ITOPS
confex records evidence rev_<id>
confex review approve rev_<id> --reason "Checked source cells and context"
confex export --space ITOPS --approved-only --export-format jsonl --output ./approved

confex run --space ITOPS --changed-only           # sync -> parse -> align -> extract -> validate
confex runs show run_<id> && confex runs resume run_<id>
confex runs request attempt_<id>                  # the request body actually sent
```

`run` never approves candidates, writes export files or publishes anything.

## Pipeline and safety model

| Stage | What runs | Model? | Writes |
|---|---|---|---|
| `sync` | Confluence REST, GET only | no | `source.*` |
| `parse` | structural parser | no | `extracted.parse_artifacts`, `table_occurrences`, `search_blocks` |
| `align` | correspondence | no | `extracted.table_correspondence` |
| `classify` | template routing; model only if allowed | optional | `pipeline.classifications` |
| plan | record-group units, redacted payloads | no | `pipeline.units` |
| `extract` | rules, or Claude | rules: no | `pipeline.attempts`, `candidate_revisions` |
| `validate` | deterministic checks | no | `pipeline.validation_results` |
| `review` | human decisions | no | `review.review_events` |
| `export` | approved, validated, current revisions | no | files + `review.exports` |

- **Egress gate.** A model call needs `[privacy] egress_approved = true`, an
  `approval_reference`, an allow-listed endpoint, an allowed space and retained requests (or
  an explicit `reduced_audit`). No flag overrides a denial; denied work is `blocked` (exit 8).
- **Redaction** runs before estimation and sending. Placeholders are stable, and the span
  mapping stays local. A value cited inside redacted text is marked `redacted`; it is never
  re-anchored.
- **Requests** carry no tools, no sampling parameters and no thinking configuration. Claude
  Haiku 5.5 thinks adaptively by default, and `effort` sets depth. `max_output_tokens`
  leaves room for thinking. Only the final text is kept: thinking blocks are dropped. The
  body is captured as the SDK puts it on the wire, without authentication headers.
- **Outcomes.** `refusal`, `truncated`, `empty`, `unsupported_shape` and `outcome_unknown`
  (a timeout, possibly billed) are explicit. One bounded retry is made for truncation (twice
  the output budget). One repair attempt is made when validation finds a false quote, a
  coverage gap or a schema error. Each attempt is its own record.
- **Budgets.** Request, token and cost ceilings are reserved before dispatch. A cost ceiling
  without a dated price is refused rather than guessed. An exhausted budget leaves the run
  `partial`, and `runs resume` continues it.
- **Validation** checks schema, structure, exact quotes at exact offsets, normalization
  re-runs, context-inheritance scope, row coverage, identity and cross-record duplicates.
  `model_inferred` values block export.
- **Review.** Events are append-only, bound to revision, dependency hash, policy version and
  snapshot, with optimistic concurrency (`--expect-sequence`). A correction is a new
  revision that cites evidence or is `human_asserted`, and must be approved separately. A
  page edit makes approvals stale.
- **Export** writes to an exclusive temporary directory, fsyncs and renames atomically,
  using mode 0600. The manifest carries file checksums and a stable `content_key`. CSV is a
  flat projection of one schema: arrays are JSON-encoded and formula prefixes neutralized.

## Exit codes

`0` ok · `2` usage/config · `3` not found · `4` unavailable (missing prerequisite,
capability, `PAYLOAD_NOT_READY`) · `5` ambiguous selector · `6` internal/execution failure ·
`7` validation failed / not export-eligible · `8` policy or authentication denial ·
`9` partial · `130` interrupted.

## Decisions that differ from the spec

`AGENTS.md` rules take precedence where they conflict with the spec.

| Spec says | Implemented | Why |
|---|---|---|
| Typer + Rich, Pydantic, SQLAlchemy | `argparse`, dataclasses/dicts, `psycopg` | Keeps runtime deps at `httpx`, `psycopg`, `lxml`. `--format rich` is not offered. |
| Pinned Anthropic client SDK | `anthropic>=2.3,<3` as the optional `model` extra | The SDK is used, as the spec asks, but only for extraction. Mirror installs stay small. |
| `confex/` package tree with many top-level packages | `confluence_mirror.{structure,extraction,confex}` plus `inspection`, `pipeline`, `review` services | Same layering; `repository*.py` own all SQL. |
| Python 3.12+ | Python ≥ 3.11 | Matches the existing package and CI. |
| `--profile NAME` | `-c/--config PATH` | One TOML file today. |
| `raw` as exact slice | labeled `dom_serialization` | lxml exposes no reliable offsets. |
| Physical cells and a grid artifact | Cells store origin and spans, and the grid is projected on read | Avoids storing up to 250,000 slots per table. |

## Schema mapping (spec section 23)

| Spec entity | Where |
|---|---|
| spaces, space_properties, pages, page_versions, page_labels, page_properties, attachments, access_restrictions, sync_runs | unchanged (`source.*`, `sync.*`) |
| page_content / immutable snapshots | `source.page_content` (latest capture) + `source.superseded_bodies` (replaced captures); `snap_` IDs from content |
| page_macros, macro_extractions, page_links, page_tables | unchanged legacy extractor output (`extracted.*`) |
| Parse artifacts, document nodes, cells | `extracted.parse_artifacts` (document JSONB), `extracted.table_occurrences` |
| Local search projection | `extracted.search_blocks` |
| Table correspondence | `extracted.table_correspondence`; decisions in `review.correspondence_decisions` |
| Runs, items, attempts, leases | `pipeline.runs`, `pipeline.run_items`, `pipeline.attempts` |
| Classifications, units | `pipeline.classifications`, `pipeline.units` |
| Candidate revisions, facts, relationships, evidence | `pipeline.candidate_revisions` (record JSONB) |
| Validation results | `pipeline.validation_results` |
| Entity reconciliation | `pipeline.entity_match_proposals` (proposals only; nothing is merged) |
| Review events | `review.review_events` |
| Export manifests and items | `review.exports`, `review.export_items` |
| Source instances | single instance today (`default`); IDs already include the namespace |

All of these come from additive migrations 0004–0007; besides adding tables, 0005 widens
one CHECK (`sync.sync_runs.mode` gains `pages` and `metadata`). `confex db migrate` is the
only schema-changing command. Every other command fails with `SCHEMA_INCOMPATIBLE`
(exit 4) on an older schema.

## Identities and evidence

`opaque_id(prefix, *parts) = prefix + "_" + sha256(canonical_json(["ids.v1", prefix, *parts]))[:24]`.

| ID | Inputs |
|---|---|
| `snap_` | instance, page, version, representation, body SHA-256 |
| `doc_` | snapshot, `PARSER_VERSION`, parse-config fingerprint |
| `tbl_`, `sec_`, `mac_`, `lay_`, `blk_` | artifact, element path (segment index for cell text) |
| `cell_` | table, source row index, source cell index |
| `corr_` | both artifacts, both tables, algorithm version |
| `cls_`, `unit_`, `val_` | digest of the artifact's own content (classification, unit manifest, validation) |
| `rev_` | record key, content hash, attempt |
| `run_`, `attempt_`, `exp_` | random (execution records, not artifacts) |

Evidence text (`evidence-text.v1`) collapses whitespace inside text nodes as HTML renders
it. `pre` and plain-text macro bodies are kept verbatim. Block boundaries and `<br>` become
newlines. Offsets are 0-based, half-open Unicode code points into `evidence_text`.

Bump `structure.document.PARSER_VERSION`, `classify.CLASSIFIER_VERSION`,
`units.PLANNER_VERSION`, `payload.PROMPT_VERSION`, `rules.RULES_VERSION` or
`validation.VALIDATOR_VERSION` whenever that stage's output changes. Stored artifacts are
never updated in place.

## Measured results

Benchmark (`scripts/benchmark.py`, synthetic data, best of 3). Measured on the development
container: Linux x86_64, Python 3.13, PostgreSQL 16. Re-run it on your Mac before relying
on it.

| Measurement | Result | Spec target |
|---|---|---|
| `tables show --view grid` on a persisted 501×50 table (CLI, incl. startup) | 0.64 s | < 1 s cached inspection |
| grid view in-process / cell view in-process | 0.07 s / 0.09 s | — |
| `spaces tree --counts` over 1,000 pages | 0.02 s | no N+1 body reads (metadata only) |
| `pages tree --depth 10` over 1,000 pages | 0.02 s | — |
| first parse of the 501×50 table | 2.9 s | (one-off; persisted afterwards) |

Evaluation (`confex eval run --dataset tests/fixtures/evaluation/synthetic_v1.json`): with
the rule extractor on the synthetic **development** split, precision, recall, field
accuracy, evidence validity, row coverage and ambiguity recall are all 1.0 over 8 cases. The
rules were written with that set in view, so this shows regressions and nothing more. The
spec's release targets (≥ 98 % precision, ≥ 95 % recall) must be measured on a separately
labelled, held-out set of your own templates, ideally about 50 tables across families,
split by template. The same harness runs it, and `--extractor model` measures Claude once
egress is approved.
