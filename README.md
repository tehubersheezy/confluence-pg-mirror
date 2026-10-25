# confluence-pg-mirror

Mirror Atlassian **Confluence Data Center** spaces into a local **PostgreSQL** database,
read-only, one space at a time. You get page hierarchy, every body representation, labels,
properties, attachments, and restrictions. Everything derived (document structure, tables,
links, extracted records) comes from the *rendered* pages, the HTML readers see, and is
traceable to the exact element it came from.

Everything goes through one command, `confex`: database setup, sync, structural
inspection of pages and tables, and evidence-backed extraction with human review.

It runs on a locked-down work Mac: no Docker, no conda and no administrator rights. You
need PostgreSQL, Python ≥ 3.11 and a personal access token (PAT).

## What it does

- **Space-based sync**: discovers every page in a space through the REST API's pagination,
  parents first, and keeps the full parent/child tree (`parent_id`, `ancestor_ids`, `depth`,
  `position`).
- **All body representations**: `body.export_view` and `body.view` (rendered HTML, which
  everything downstream reads) and `body.storage` (mirrored as returned, never parsed:
  it lacks what macros render, such as includes, page-properties reports and Jira tables).
- **Metadata**: labels, attachment metadata (binaries optional), direct read/update
  restrictions, and optionally content and space properties.
- **Structure**: `confex parse` turns rendered pages into a document model: headings and
  sections, tables with exact cell geometry (merged and nested cells), rendered macros and
  every link, classified by URL (page, attachment, user, external, anchor).
- **Bulk requests, gentle on rate limits**: the space is listed through `/content/scan`
  (200 pages a request, cursor-paged), and changed pages are fetched 50 at a time with
  bodies, labels, attachments and restrictions in one CQL `id in (...)` request. A
  client-side limit (`max_requests_per_second`) spaces requests out. Each run records its
  request counts in `sync_runs.stats.http_requests`.
- **Scoped syncs**: one page tree (`--root`), a CQL search (`--cql`) or named pages
  (`--page`), listed and fetched in bulk, without touching the rest of the space.
- **Initial and incremental sync**: incremental mode fetches only new or changed pages. It
  also detects moves, trash, purges, permission loss and pages moved to another space, and
  picks up metadata-only changes (labels, new attachments).
- **Resilient**: retries with backoff (honouring `Retry-After`). Each page commits in its own
  transaction, so one bad page is recorded and retried on the next run without blocking the
  rest. A deletion guard protects against a bad listing wiping the mirror, and an advisory
  lock stops two syncs of the same space running at once.
- **Idempotent**: every write is an upsert keyed on Confluence IDs, so re-running a sync
  converges on the same state.

## Database layout

Source data and derived data live in separate schemas. Everything `confex` derives can be
rebuilt from the rendered bodies in `source` offline (`confex parse`).

| Schema | Table | Contents |
|---|---|---|
| `source` | `spaces` | space metadata, sync cursor |
| | `space_properties` | space properties (JSONB) |
| | `pages` | current state of each page: hierarchy, version, deletion/move tracking |
| | `page_versions` | each version observed (title, parent, author, message) |
| | `page_content` | export_view / view / storage bodies, one row per (page, version); storage is never parsed |
| | `page_labels` | labels (prefix, name) |
| | `page_properties` | content properties (JSONB) |
| | `attachments` | attachment metadata, soft-deleted, optional local copy + SHA-256 |
| | `access_restrictions` | direct read/update restrictions per user/group |
| `extracted` | `parse_artifacts` | structural parses of rendered bodies: document model, tables, links (JSONB) |
| | `table_occurrences` | table index: geometry status, nesting, heading path |
| | `search_blocks` | text projection for local search |
| `pipeline` | `runs`, `run_items`, `attempts`, `classifications`, `units`, `candidate_revisions`, `validation_results`, `entity_match_proposals`, `template_detections` | `confex` extraction runs, immutable candidates, and model-detected page templates |
| `review` | `review_events`, `exports`, `export_items` | append-only human decisions and export audit |
| `sync` | `sync_runs` | one row per run: mode, status, counters, per-page errors |

Tables from earlier versions stay in place but are no longer written:
`extracted.page_macros`, `macro_extractions`, `page_tables` and `page_links` (and their
`current_*` views) held a storage-format extraction, and `extracted.table_correspondence` /
`review.correspondence_decisions` matched tables across formats. Parse artifacts made from
storage bodies are kept as history and ignored.

Migrations are versioned SQL files in `src/confluence_mirror/migrations/`, applied in order
and checksummed in `sync.schema_migrations`.

## Setup on macOS (no admin rights)

### 1. Python

The system `python3` is usually too old. [uv](https://docs.astral.sh/uv/) installs into
your home directory:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs to ~/.local/bin
uv python install 3.13
```

### 2. PostgreSQL

Pick whichever your machine allows:

- **[Postgres.app](https://postgresapp.com/).** Use it from `/Applications` if it is
  already there. Otherwise drag it from the disk image into `~/Applications`, which needs
  no admin rights (supported since Postgres.app 2.6). Then put its tools on your `PATH`:
  `export PATH="$HOME/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"`
  (or `/Applications/...`).
- **The binaries zip** from the PostgreSQL macOS download page
  ([postgresql.org/download/macosx](https://www.postgresql.org/download/macosx/), "zip
  archive of the binaries"). Unzip it under your home directory and add its `bin` to `PATH`.
- **Already installed**: any `initdb`/`pg_ctl` on your `PATH` (PostgreSQL 14 or newer).

Then create a private cluster and database. It is reachable only from your machine:

```bash
initdb -D ~/pgdata/confluence-mirror -U "$USER" --auth-local=peer --auth-host=scram-sha-256
pg_ctl -D ~/pgdata/confluence-mirror -l ~/pgdata/confluence-mirror.log \
       -o "-c listen_addresses=localhost" start
createdb confluence_mirror
```

(With Postgres.app, just `createdb confluence_mirror`.)

### 3. Install the mirror

```bash
git clone <this repository> && cd confluence-pg-mirror
uv venv --python 3.13 .venv && source .venv/bin/activate
uv pip install -e '.[dev,truststore]'
```

### 4. Configure

```bash
cp config/config.example.toml config.toml   # git-ignored
cp .env.example .env                        # git-ignored
chmod 600 .env
```

- In `config.toml`, set `confluence.base_url` (include the context path if your instance
  has one, e.g. `https://confluence.example.com/confluence`), `database.dsn` and
  `sync.spaces`.
- In `.env`, set `CONFLUENCE_PAT` to the token from Confluence → Profile → Settings →
  Personal Access Tokens. The token is read **only** from the environment or `.env`; the
  tool refuses a config file that contains one.
- **Corporate TLS.** Verification can't be disabled. If your company re-signs TLS with an
  internal CA, either set `use_system_truststore = true` (uses the macOS Keychain; needs the
  `truststore` extra installed above) or point `ca_bundle` at a PEM file containing the root
  CA.

### 5. Run

```bash
confex db migrate                     # create / upgrade the schema (the only schema change)
confex doctor --check-db --check-confluence   # config, schema and a read-only API call
confex sync --space DEMO              # first run is a full import, later runs incremental
confex sync --all                     # every space in [sync].spaces (for launchd/cron)
confex sync --space DEMO --reconcile  # re-fetch every page
confex sync --root 12345              # one page tree (new or changed pages only)
confex sync --cql 'space = DEMO and label = "runbook"'
confex sync --page 12345              # named pages, always re-fetched
confex runs sync --space DEMO         # recent sync runs
```

Exit codes: `0` success, `2` usage or configuration error, `4` schema not migrated, `6`
source or internal error, `8` authentication denied, `9` partial (some pages or spaces
failed; details in `sync.sync_runs.errors`). The full list is in
[docs/confex.md](docs/confex.md#exit-codes). Add `--format json` (before the subcommand) for
machine-readable output.

### 6. Explore, inspect and extract

Navigation and inspection open the database read-only and make no network calls. Global
options go before the subcommand.

```bash
confex spaces tree DEMO --depth 3 --counts
confex parse --space DEMO                      # structural artifacts (offline)
confex pages outline 12345 --tables --macros
confex pages links 12345 --direction both
confex tables show 12345:t02 --view grid
confex tables show 12345:t02 --view cell --cell R3C1

confex templates detect --space DEMO --dry-run # parents whose children are all leaves
confex templates detect --space DEMO           # model: do the children share a template?
confex extract --page 12345 --plan-only        # no writes, no model calls
confex extract --page 12345                    # deterministic rules where a template matches
confex review queue --space DEMO
confex review approve rev_<id> --reason "Checked source cells"
confex export --space DEMO --approved-only --output ./approved
confex run --root 12345                        # sync → parse → extract → validate
```

Merged cells appear as `covered:cN` slots that point at one physical cell. Nested tables
are separate occurrences, referenced from their parent cell. Malformed spans stay
inspectable as `conflict` slots, with diagnostics.

Extraction with Claude (Haiku 5.5 by default) is optional. It needs
`pip install -e '.[model]'`, an `ANTHROPIC_API_KEY`, and an explicit organizational
approval recorded in `[privacy]` (see `config/config.example.toml`). Until then nothing
leaves the machine, and rule-based extraction still works. Every candidate needs human
approval before export.

See [docs/confex.md](docs/confex.md) for the full command set, safety model and measured
results.

To schedule a sync, run `confex sync --all` from `launchd` (user agents in
`~/Library/LaunchAgents` need no admin) or `cron`.

## How incremental sync works

Every run first lists the whole space with lightweight metadata (version, ancestors,
labels). It is compared to the mirror:

| Situation | Action |
|---|---|
| new page, or version changed | full fetch (bodies, labels, properties, restrictions, attachments) + extraction |
| version unchanged | hierarchy, title and labels refreshed from the listing (moves detected here) |
| known page missing from the listing | probed by ID: `trashed`, `purged` (404), `not_visible` (403), or `moved_out` to another space |
| attachment changed since the last successful sync | attachments re-listed for that page (CQL `type = attachment and lastmodified >= …`, with a configurable overlap window) |
| stored extraction outdated or failed | re-extracted from the stored body, no network |

Old versions are kept: `page_versions` and `page_content` grow by one row per version seen,
and extraction rows are per version. The sync cursor only advances on a fully successful
run. Failed pages keep their old stored version, so the next run retries them automatically.

Scoped syncs (`--root`, `--cql`, `--page`) run in mode `pages`: they list only the selection,
fetch what is new or changed (`--reconcile` re-fetches it all), and never mark pages deleted
or advance the space cursor.

### Request budget and rate limits

| Step | Requests |
|---|---|
| list a space of N pages | about N / 200 (`/content/scan`; offset `/content` paging on servers without it) |
| fetch C new or changed pages | about C / 50 (`id in (...)` with every expansion) |
| content properties, `page_properties = "all"` | one more per fetched page, plus one per space |
| labels or attachments beyond the expansion limit | one more per affected page |

Content properties are off by default (`page_properties = "none"`): Confluence has no bulk
listing for them, so fetching them costs a request per page. Stored properties are left as
they were. To fetch them once, or on a schedule of your choosing:

```bash
confex sync --space DEMO --reconcile --properties all
```

A key list (`page_properties = ["owner"]`) fetches just those keys inside the batch
requests at no extra cost, if your server honours `metadata.properties.<key>` expansions in
search results. Those keys are only ever added or updated, never deleted.

Requests are spaced to `max_requests_per_second` (default 5; `0` turns the limit off). A
429 response is retried after `Retry-After`, and the client halves its rate for the rest
of the run (down to 0.25/s). Each run records its requests in `sync_runs.stats`:
`http_requests` per endpoint, plus `http_retries` by status and the final
`requests_per_second` when anything was retried.

Restrictions and content properties change without a version bump. Incremental runs refresh
them only for changed pages, unless `refresh_unchanged_metadata = true` (slower). A
`--reconcile` run always refreshes everything.

## Example queries

```sql
-- Page tree of a space
SELECT repeat('  ', depth) || title FROM source.pages p
JOIN source.spaces s USING (space_id)
WHERE s.space_key = 'DEMO' AND NOT is_deleted ORDER BY ancestor_ids || page_id;

-- Links by kind in parsed rendered pages (run `confex parse` first)
SELECT l->>'kind' AS kind, count(*)
FROM extracted.parse_artifacts a, jsonb_array_elements(a.document->'links') l
WHERE a.representation = 'export_view' GROUP BY 1;

-- Pages and their table counts in the latest parse
SELECT page_id, version_number, table_count FROM extracted.parse_artifacts
WHERE representation = 'export_view' ORDER BY table_count DESC LIMIT 20;
```

## Security

- Read-only: the HTTP client can only send `GET`, and the test fake fails on anything else.
- The PAT never goes to disk via this tool. It is registered with a logging filter that
  redacts it everywhere, `Authorization` values are masked, and errors stored in
  `sync_runs` pass through the same redaction.
- TLS verification is always on. The client refuses pagination links pointing at another
  host, so the token is only ever sent to the configured instance.
- Rendered HTML is parsed with network access disabled; storage XML is never parsed.
- Mirrored content stays in your local database. Nothing is sent to any other service
  unless model extraction is explicitly approved in `[privacy]`.

## Development

```bash
uv pip install -e '.[dev]'
ruff check src tests && ruff format --check src tests
pytest                     # unit + integration
pytest tests/unit          # no database needed
```

The integration tests need PostgreSQL and find it automatically. If `initdb`/`pg_ctl` are
on your `PATH`, in Postgres.app, or under `/usr/lib/postgresql`, they start a throwaway
cluster on a private Unix socket and delete it afterwards. Alternatively, set
`CONFLUENCE_MIRROR_TEST_DSN` to a scratch database you own; its schemas are dropped and
recreated, so never point it at real data. All test data is synthetic: an in-memory fake
Confluence in `tests/fake_confluence.py` and rendered-HTML fixtures in `tests/fixtures/render/`.

See [AGENTS.md](AGENTS.md) for architecture decisions and conventions, and
[docs/agent-setup-prompt.md](docs/agent-setup-prompt.md) for a step-by-step prompt that lets a
coding agent do the macOS setup for you.

## Limitations

- Only *direct* page restrictions are stored. Inherited restrictions are the ancestors' rows
  (join through `ancestor_ids`).
- Version history is what the mirror has observed since it started. Earlier versions are not
  backfilled.
- Comments, inline tasks and blog-post-specific metadata are not mirrored. Blog posts can be
  enabled with `content_types = ["page", "blogpost"]`.
- No write-back to Confluence, by design.

## License

MIT
