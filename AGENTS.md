# AGENTS.md

Guidance for coding agents and contributors working on this repository.

## Non-negotiables

1. **Read-only against Confluence.** `ConfluenceClient` sends only `GET`. Do not add
   POST/PUT/DELETE paths, even "harmless" ones. `tests/unit/test_client.py` asserts that the
   client has no write methods, and the fake server fails on any non-GET request.
2. **No secrets anywhere.** The PAT comes only from `CONFLUENCE_PAT` (environment or `.env`).
   `load_settings` rejects `pat`/`token`/`password` keys in the config file. Every secret
   goes through `logging_utils.register_secret`. Exception text that reaches logs or
   `sync_runs.errors` must pass through `redact()`.
3. **TLS stays verified.** No `verify=False` and no option to add one. Corporate CAs go
   through `ca_bundle` (added to certifi's roots) or `truststore`.
4. **No company data in Git.** Fixtures are synthetic (`example.com` hosts, made-up keys and
   IDs). Never commit a real page body (storage or rendered), URL, space key, username or
   token, even "anonymised".
5. **No Docker or admin requirements**, for runtime and for tests alike.

## Architecture

```
confex sync ──► sync.SpaceSync ──► client.ConfluenceClient   (HTTP, GET only)
                    │
                    ├──► models.py      (pure: API JSON → row dicts)
                    └──► repository.py  (all SQL; idempotent upserts)
confex/ (CLI) ──► inspection.py ─┬─► repository.py           (mirror reads; parse writes)
              ├──► pipeline.py ───┼─► repository_pipeline.py  (pipeline.* and review.* SQL)
              ├──► review.py ─────┘
              └──► live.py ──► client.ConfluenceClient       (--data-source live; never persists)
structure/   (pure: rendered HTML → document artifact, table geometry, links, IDs)
extraction/  (pure: registry, classify, units, payload, rules, candidates, validation, privacy,
              templates: page-template summaries, requests and verdicts)
extraction/provider.py  (the only module that talks to a model; imports the SDK lazily)
db.py: connections + versioned migrations (migrations/NNNN_name.sql)
```

- **confex** (`docs/confex.md`) is the only CLI (`python -m confluence_mirror` runs it too).
  Mirror commands never construct an HTTP client and run on a read-only connection. Parse artifacts, classifications, units, attempts, revisions and
  review events are immutable. Bump the stage's version constant (`PARSER_VERSION`,
  `CLASSIFIER_VERSION`, `PLANNER_VERSION`, `PROMPT_VERSION`, `RULES_VERSION`,
  `VALIDATOR_VERSION`) when its output changes; never update stored artifacts in place.
- **Model egress** goes through `extraction.privacy.egress_decision` and nothing else. Do
  not add a flag that bypasses it, and keep requests free of tools and sampling parameters.
  Prompts live in `extraction/prompts/`; examples in them must stay synthetic.
- **Approval is human only.** No code path may write an `approved` review event except
  `review.decide` called from `confex review approve`.

- **Layering.** `models` and `extract` do no I/O and are unit-tested exhaustively.
  `repository` and `repository_pipeline` own every SQL statement. `sync` orchestrates and
  owns transactions. Keep SQL out of `sync.py` and HTTP out of `repository.py`.
- **Rendered HTML only.** Everything derived reads `body.export_view` (falling back to
  `body.view`). Storage format is still fetched and stored in `page_content.body_storage`,
  but nothing parses it: it lacks what macros render (includes, page-properties reports,
  Jira tables) and is not what readers see. Don't add storage parsing back; the
  `REPRESENTATIONS` the parser accepts are `export_view` and `view`. Macros are recognised
  by Confluence's `data-macro-name` marker, links by their URL (`structure/links.py`).
- **Schemas separate provenance.** `source.*` holds what the API returned. `extracted.*`
  holds what we derived, and must be rebuildable from `source` alone (`confex parse`).
  `sync.*` holds bookkeeping. Never write derived values into `source`. The legacy
  storage-extraction tables (`extracted.page_macros`, `macro_extractions`, `page_tables`,
  `page_links`, `table_correspondence`; `page_content.extractor_version`/`extracted_at`)
  remain in the schema but are no longer written.
- **Discovery is listing-driven, not CQL-driven.** Each run lists the entire space with
  cheap metadata, then diffs against the mirror. This catches moves (which don't bump the
  version), deletions (which never appear in "modified since" queries) and restores, without
  depending on CQL date semantics or server time zones. CQL is used for attachment changes
  (with a generous overlap window), for scoped syncs (`Scope`: pages, tree, cql), and to
  fetch changed pages in `id in (...)` batches.
- **Bulk first.** Rate limits are the binding constraint. Listing uses `/content/scan`;
  bodies and metadata come in batches with expansions; per-page endpoints are fallbacks only
  for truncated expansions or a failed batch. Content properties are off by default
  (`page_properties = "none"`); the per-page `/property` call is opt-in
  (`--properties all`). Properties from a key-list expansion are upsert-only, because a
  missing key in an expansion does not prove deletion. Don't add a
  per-page request to the common path without a reason, and check
  `sync_runs.stats.http_requests` in tests when you touch it.
- **Transactions.** Network calls for a page (or its batch) happen first, then one short
  transaction per page writes it, so no transaction spans HTTP. Sync derives nothing, so a
  parser bug can never cost source rows.
- **Failure semantics.** A page-level failure is recorded in `sync_runs.errors` and the run
  ends `partial`. A failure before the listing (space fetch, listing) ends `failed`. The
  space cursor (`last_successful_sync_at`) advances only on `succeeded`. Because a failed
  page keeps its old version in the DB, the next run re-fetches it with no special retry
  queue.
- **Deletion is soft.** Pages get `is_deleted` + `deletion_reason`, and attachments get
  `is_deleted`. Versions and content are retained. Hard deletes are only for reconciled
  "set" children (labels, properties, restrictions), where history is not meaningful.
- **The deletion guard** (`max_delete_fraction`) skips the deletion phase when an implausible
  share of pages vanishes from a listing, e.g. a PAT that lost permissions.
- **Version awareness.** `page_versions`/`page_content` are keyed `(page_id,
  version_number)`. Parse artifacts are keyed by a snapshot of one rendered body and the
  `PARSER_VERSION`; bump it (`structure/document.py`) whenever artifact output changes, and
  `confex parse` writes new artifacts instead of touching old ones.
- **Migrations.** Plain SQL files, applied in order inside a transaction each, and
  checksummed. **Never edit a migration that has shipped**; add `NNNN+1_description.sql`.
  (Editing is acceptable only before the first release, as with 0001–0003 today.) Every
  table needs a primary key that makes upserts natural; prefer composite natural keys over
  surrogate IDs.
- **No parent FK on `pages.parent_id`/`spaces.homepage_id`**, on purpose. Parents can be
  unreadable or arrive later in a listing.

## Coding conventions

- Python ≥ 3.11, `from __future__ import annotations`, type hints on public functions.
- `ruff format` + `ruff check` (config in `pyproject.toml`, line length 100). CI enforces both.
- Runtime dependencies are deliberately few: `httpx`, `psycopg[binary]`, `lxml`. Adding one
  needs a reason that holds on a locked-down Mac (pure wheels, no compilers, no conda). The
  Anthropic SDK is the optional `model` extra and is imported only by `extraction/provider.py`.
- Use `argparse`, not click/typer, and a hand-rolled migration runner, not Alembic. Both
  keep the dependency surface small; revisit only if they become limiting.
- SQL: compose identifiers with `psycopg.sql`, pass values as parameters, never f-string
  values into SQL. JSON columns go through `Jsonb` (see `repository._JSON_COLUMNS`).
- Logging: module-level `log = logging.getLogger(__name__)`; `%s` formatting, not f-strings
  in log calls; never log response bodies of authenticated calls at INFO.
- Errors: raise the client's typed errors (`AuthenticationError`, `ForbiddenError`,
  `NotFoundError`, `ConfluenceError`) or `ConfexError` with a stable code. `confex.core`
  maps them to exit codes (`docs/confex.md#exit-codes`: `2` config/usage, `6` source error,
  `8` auth, `9` partial, …).

## Testing

- `tests/unit/`: no database or network. Use `tests/fake_confluence.py` (an
  `httpx.MockTransport` speaking DC-style pagination; pages take a `rendered=` HTML body
  for export view) and fixtures in `tests/fixtures/render/` (rendered HTML; the storage
  fixtures only exercise mirroring).
- `tests/integration/`: real PostgreSQL. The fixture uses `CONFLUENCE_MIRROR_TEST_DSN`, or
  boots a throwaway cluster from `initdb`/`pg_ctl` on a private Unix socket. CI sets
  `CPM_REQUIRE_POSTGRES=1` so missing Postgres fails instead of skipping.
- Every behavior change to sync semantics needs an integration test that drives the fake
  through the scenario (edit, move, trash, failure, …) and asserts on table contents.
- Run `ruff check src tests && ruff format --check src tests && pytest` before committing.
- GitHub Actions are pinned by commit SHA with a trailing `# vX.Y.Z` comment, and must run
  on Node 24 (Node 20 was removed from runners on 2026-09-23). Dependabot keeps the pins
  current; when bumping by hand, check the action's `action.yml` says `using: node24`.

## Assumptions about the Confluence DC API (verify against your instance)

- Pagination: `results` + `_links.next`, relative to `_links.base`, which includes the
  context path. The client also accepts context-prefixed and same-origin absolute links.
- `GET /rest/api/content/{id}?status=any` returns trashed content. Some releases answer 400,
  so `SpaceSync._probe` falls back to `status=trashed`, then no status.
- `extensions.position` is an integer or the string `"none"`.
- Attachment IDs are strings (`att123`); page and space IDs are numeric.
- `/rest/api/content/scan` exists on DC 7.18+ (pages) and 8.3+ (blog posts). It pages by
  `cursor` (`nextCursor`, or `_links.next` on some builds) and has no offset. Servers that
  answer 400/404/405/501 get offset listing on `/rest/api/content`.
- CQL search caps each page silently: 50 results with a body expansion, 200 with other
  expansions, 1000 with none. Batches therefore request at most 50 bodies.
- Embedded collections (`metadata.labels`, `children.attachment`) are truncated past their
  limit; `models._expanded` reports that and the sync falls back to the per-page endpoint.
- Restrictions expanded on content may be truncated. The sync falls back to
  `/restriction/byOperation` and records page IDs that were still incomplete in
  `sync_runs.stats.restrictions_incomplete`.
