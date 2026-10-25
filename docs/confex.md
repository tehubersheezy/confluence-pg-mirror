# Confex: implementation status and schema mapping

`confex` is the exploration and structural-inspection CLI described in the *Confex Complete
Spec v1* (not committed: the spec is a private planning document). This file is the M0
deliverable the spec asks for: how its logical data contracts map onto the existing mirror,
which decisions differ from the spec and why, and what is built so far.

## What exists today

The first vertical slice the spec names (section 29): sync a page, parse complex and nested
tables, inspect every cell. It does this offline, with no model call.

| Area | Status |
|---|---|
| Structural parser (`structure/`) | Storage (namespace-aware XML) and rendered HTML adapters; scoped heading stacks; physical cells, row groups, span geometry (`origin`/`covered`/`empty`/`conflict`); nested tables as separate occurrences; evidence blocks; nearby context; resource limits; deterministic IDs. |
| Persistence | Migration `0004`: `extracted.parse_artifacts`, `extracted.table_occurrences`. Additive; 0001–0003 are unchanged. |
| `confex db status\|migrate` | Same migration runner as `confluence-mirror`. |
| `confex parse` | Persists artifacts for stored bodies. Re-running it is a no-op (immutable, content-addressed IDs). |
| `confex spaces list\|show\|tree` | Forest of all verified roots, an unresolved-parent group, `--depth`, `--match` with context ancestors, `--counts`, cycle diagnostics. |
| `confex pages show\|children\|ancestors\|siblings\|tree\|outline\|tables` | Recursive CTEs with cycle detection; deterministic order (position, then title, then ID). |
| `confex tables list\|show` | Views `overview`, `grid`, `physical`, `cell`, `context`, `raw`; `payload` answers `PAYLOAD_NOT_READY`. Selectors `tbl_<id>`, `PAGE:tNN` with `--representation`, `--version`, `--snapshot`. |
| Output | `plain`, `json` (the `confex.cli.v1` envelope), `jsonl` for collections; `--output` with atomic write, mode 0600, no overwrite unless `--overwrite`. Terminal output escapes control characters. |

Not built yet (spec milestones M1 remainder, M4–M6): live data source, CQL/local search,
links/labels/properties/attachments/versions/diff subcommands, cross-representation
correspondence (reported as `not_computed`), classification, extraction profiles and
schemas, the model adapter, validation, review, export, run manifests/budgets, evaluation.

## Decisions that differ from the spec

The repository's `AGENTS.md` rules take precedence where they conflict with the spec.

| Spec says | Implemented | Why |
|---|---|---|
| Typer + Rich CLI, Pydantic, SQLAlchemy | `argparse`, plain dicts/dataclasses, `psycopg` | `AGENTS.md`: runtime dependencies stay `httpx`, `psycopg`, `lxml`. Adding Rich would only add colour. `--format rich` is not offered; `plain` is the terminal format. |
| Separate `confex/` package tree | Modules inside `confluence_mirror` (`structure/`, `inspection.py`, `confex.py`) | Same package, same layering: `structure` is pure, `repository` owns SQL, `inspection` orchestrates, `confex` presents. |
| Python 3.12+ | Python ≥ 3.11 | Matches the existing package and CI matrix. |
| `--profile NAME` | `-c/--config PATH` | The existing config is one TOML file; named profiles can come later. |
| Exit codes 0/2–9/130 | Implemented for `confex` | `confluence-mirror` keeps its existing 0–4 codes. |
| `raw` view shows the exact source slice when offsets exist | Always labeled `dom_serialization`, `byte_original: false` | lxml exposes no reliable source offsets, so the view never claims to be byte-exact. |
| Grid stored as an artifact | Cells store origin and effective spans, and the grid is projected on read | Avoids storing up to 250,000 slots per table. The projection is deterministic. |
| HTML adapter for rendered bodies | `lxml.html` (part of `lxml`) | No new dependency; network and entity loading stay off. |

## Mapping of spec entities to the mirror (section 23)

| Spec entity | Mirror today | Gap / plan |
|---|---|---|
| `spaces`, `space_properties`, `pages`, `page_versions`, `page_labels`, `page_properties`, `attachments`, `access_restrictions`, `sync_runs` | Same names in `source.*` / `sync.*` | Kept as is. |
| `page_content` | `source.page_content`, one row per (page, version) with three body columns | **Gap:** a re-fetch of the same version overwrites the bodies, and only storage has a stored SHA-256. The spec wants immutable captures, each with its own observation time. Plan: an additive `source.content_snapshots` table (page, version, representation, sha256, principal scope, first/last observed) in a later migration. |
| `page_macros`, `macro_extractions`, `page_links`, `page_tables` | `extracted.*`, from the existing extractor | Kept. `page_tables` is the legacy header-keyed projection; confex tables live in `table_occurrences`. |
| Source instances | Single instance; IDs use namespace `default` | Add `instance_id` to the config and to the source keys when a second instance is needed. Opaque IDs already include it. |
| Immutable content snapshots | `snap_<id>` derived from (instance, page, version, representation, body SHA-256) and computed on read | See the `page_content` gap. |
| Metadata observations / scope coverage | `pages.last_seen_at`, `is_deleted`, `deletion_reason`, `sync_runs.stats` | Per-resource completeness states (`complete`/`partial`/…) are not recorded yet. |
| Parse artifacts | `extracted.parse_artifacts` (new) | Done: the document JSONB holds nodes, blocks and tables. |
| Document nodes | Inside `parse_artifacts.document.nodes` | Relational nodes only if queries need them. |
| Physical table rows/cells | Inside the document; table index in `extracted.table_occurrences` (new) | A cell-level index (spec 23.4 "cells by table/origin") is deferred. |
| Table correspondence; runs/items/attempts; classifications/units; candidate revisions/facts; evidence links; validation; review events; export manifests | None | Built with M4–M6, each as an additive migration. |

## Identity recipe (`ids.v1`)

`opaque_id(prefix, *parts) = prefix + "_" + sha256(canonical_json(["ids.v1", prefix, *parts]))[:24]`.

| ID | Inputs |
|---|---|
| `snap_` | instance, page ID, version, representation, body SHA-256 |
| `doc_` | snapshot ID, parser version, parse-config fingerprint (limits, context budget, evidence-text policy) |
| `tbl_`, `sec_`, `mac_`, `lay_`, `blk_` | artifact ID, element path (plus a segment index for cell text blocks) |
| `cell_` | table ID, source row index, source cell index |

Bump `PARSER_VERSION` in `structure/document.py` whenever artifact output changes. Old
artifacts stay in the table, and new parses get new IDs.

## Evidence text (`evidence-text.v1`)

Whitespace runs inside text nodes collapse to one space, as HTML renders them. `pre` and
plain-text macro bodies are kept verbatim. `<br>` and block boundaries become one newline.
Spaces around newlines and at both ends are trimmed. Offsets are 0-based, half-open Unicode
code points into `evidence_text`. `normalized_text` (all whitespace collapsed) is for display
and search only.
