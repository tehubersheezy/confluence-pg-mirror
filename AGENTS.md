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
   IDs). Never commit a real storage body, URL, space key, username or token, even
   "anonymised".
5. **No Docker or admin requirements**, for runtime and for tests alike.

## Architecture

```
cli.py ──► sync.SpaceSync ──► client.ConfluenceClient   (HTTP, GET only)
               │
               ├──► models.py      (pure: API JSON → row dicts)
               ├──► repository.py  (all SQL; idempotent upserts)
               └──► extract/       (pure: storage XHTML → macros/tables/links)
confex/ (CLI) ──► inspection.py ─┬─► repository.py           (mirror reads; parse/align writes)
              ├──► pipeline.py ───┼─► repository_pipeline.py  (pipeline.* and review.* SQL)
              ├──► review.py ─────┘
              └──► live.py ──► client.ConfluenceClient       (--data-source live; never persists)
structure/   (pure: body → document artifact, table geometry, IDs)
extraction/  (pure: registry, classify, units, payload, rules, candidates, validation, privacy)
extraction/provider.py  (the only module that talks to a model; imports the SDK lazily)
db.py: connections + versioned migrations (migrations/NNNN_name.sql)
```

- **confex** (`docs/confex.md`): mirror commands never construct an HTTP client and run on
  a read-only connection. Parse artifacts, classifications, units, attempts, revisions and
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
- **Schemas separate provenance.** `source.*` holds what the API returned. `extracted.*`
  holds what we derived, and must be rebuildable from `source` alone (`confluence-mirror
  extract`). `sync.*` holds bookkeeping. Never write derived values into `source`; the one
  exception is `page_content.extractor_version`/`extracted_at`, which records *that*
  extraction happened.
- **Discovery is listing-driven, not CQL-driven.** Each run lists the entire space with
  cheap metadata, then diffs against the mirror. This catches moves (which don't bump the
  version), deletions (which never appear in "modified since" queries) and restores, without
  depending on CQL date semantics or server time zones. CQL is used only as a supplement for
  attachment changes, with a generous overlap window.
- **Transactions.** Network calls for a page happen first, then one short transaction writes
  it, so no transaction spans HTTP. Extraction runs in a savepoint inside that transaction:
  an extractor bug loses the derived rows, never the source rows.
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
  version_number)`. All `extracted.*` rows carry the same key plus `element_path` and
  `extractor_version`. Bump `EXTRACTOR_VERSION` (`extract/pipeline.py`) whenever extractor
  output changes; the next sync re-extracts stored bodies whose recorded version differs.
- **Storage parsing.** Storage format is undeclared-namespace XML with HTML entities. We wrap
  it in a root declaring `ac`/`ri`/`at`, rewrite named entities to numeric ones, and parse
  with lxml (`recover=True`, `resolve_entities=False`, `no_network=True`,
  `strip_cdata=False`). Don't switch to an HTML parser: it lowercases and reshapes `ac:`
  markup and mishandles CDATA.
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
  `NotFoundError`, `ConfluenceError`). The CLI maps them to exit codes (`0` ok, `1` config,
  `2` partial, `3` failed, `4` auth).

## Testing

- `tests/unit/`: no database or network. Use `tests/fake_confluence.py` (an
  `httpx.MockTransport` speaking DC-style pagination) and fixtures in
  `tests/fixtures/storage/`.
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
- Restrictions expanded on content may be truncated. The sync falls back to
  `/restriction/byOperation` and records page IDs that were still incomplete in
  `sync_runs.stats.restrictions_incomplete`.
