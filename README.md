# confluence-pg-mirror

Mirror Atlassian **Confluence Data Center** spaces into a local **PostgreSQL** database,
read-only, one space at a time. You get page hierarchy, every body representation, labels,
properties, attachments, and restrictions. On top of that come structured extractions of
macros, tables, and page-to-page links, each traceable to the exact markup it came from.

It runs on a locked-down work Mac: no Docker and no administrator rights. You need
PostgreSQL, Python ≥ 3.11 and a personal access token (PAT).

## What it does

- **Space-based sync**: discovers every page in a space through the REST API's pagination,
  parents first, and keeps the full parent/child tree (`parent_id`, `ancestor_ids`, `depth`,
  `position`).
- **All body representations**: `body.storage`, `body.view` and `body.export_view` for every
  page, including parent pages.
- **Metadata**: labels, content properties, space properties, attachment metadata (binaries
  optional), and direct read/update restrictions.
- **Extraction**: macros with their parameters, IDs, nesting and bodies; macro-specific
  interpretations (code, panels, Jira, include/excerpt, page properties, status, …); tables
  as cells and header-keyed records; links (page, attachment, user, space, URL, anchor,
  include). Every derived row records the page version, element path and extractor version.
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

Source data and derived data live in separate schemas. Everything in `extracted` can be
rebuilt from `source` offline (`confluence-mirror extract`).

| Schema | Table | Contents |
|---|---|---|
| `source` | `spaces` | space metadata, sync cursor |
| | `space_properties` | space properties (JSONB) |
| | `pages` | current state of each page: hierarchy, version, deletion/move tracking |
| | `page_versions` | each version observed (title, parent, author, message) |
| | `page_content` | storage / view / export_view bodies, one row per (page, version) |
| | `page_labels` | labels (prefix, name) |
| | `page_properties` | content properties (JSONB) |
| | `attachments` | attachment metadata, soft-deleted, optional local copy + SHA-256 |
| | `access_restrictions` | direct read/update restrictions per user/group |
| `extracted` | `page_macros` | every macro: name, `ac:macro-id`, parameters, nesting, body |
| | `macro_extractions` | macro-specific structured data (JSONB) |
| | `page_tables` | tables: cells with spans, header, records |
| | `page_links` | outgoing links with resolved `target_page_id` |
| | `parse_artifacts` | `confex` structural parses: document model per body capture (JSONB) |
| | `table_occurrences` | `confex` table index: geometry status, nesting, heading path |
| `sync` | `sync_runs` | one row per run: mode, status, counters, per-page errors |

Views `extracted.current_page_content`, `current_page_macros`, `current_page_tables` and
`current_page_links` restrict to the latest version of non-deleted pages.

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

- **Postgres.app.** If it is already installed (or you can install apps), start a server
  from the app and add its tools to your `PATH`:
  `export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"`.
  Postgres.app normally lives in `/Applications`. If you can't write there, use the next
  option.
- **User-space binaries** from conda-forge via
  [micromamba](https://mamba.readthedocs.io/), entirely under your home directory:

  ```bash
  "${SHELL}" <(curl -L micro.mamba.pm/install.sh)
  micromamba create -y -n pg -c conda-forge postgresql=16
  micromamba activate pg
  ```

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
confluence-mirror db migrate             # create / upgrade the schema
confluence-mirror check                  # validates config + a read-only API call
confluence-mirror sync --space DEMO      # first run is a full import
confluence-mirror sync --space DEMO      # later runs are incremental
confluence-mirror sync --all             # every space in [sync].spaces
confluence-mirror sync --space DEMO --full
confluence-mirror runs --space DEMO      # recent sync runs as JSON
confluence-mirror extract --space DEMO   # rebuild extracted.* offline
```

Exit codes: `0` success, `1` configuration error, `2` partial (some pages failed; details in
`sync.sync_runs.errors`), `3` failure, `4` authentication failure.

### 6. Explore and inspect tables (`confex`)

`confex` reads the mirror offline: it opens the database read-only and makes no network
calls. Global options go before the subcommand.

```bash
confex spaces tree DEMO --depth 3 --counts
confex pages ancestors 12345
confex parse --space DEMO                   # persist structural artifacts (offline)
confex pages outline 12345 --representation storage --tables --macros
confex pages tables 12345 --representation storage
confex tables show 12345:t02 --representation storage --view grid
confex tables show 12345:t02 --representation storage --view cell --cell R3C1
confex --format json tables show tbl_<id> --view physical
```

Views are `overview`, `grid`, `physical`, `cell`, `context` and `raw`. Merged cells appear as
`covered:cN` slots that point at one physical cell. Nested tables are separate occurrences
that are referenced from their parent cell. Malformed spans stay inspectable as `conflict`
slots, with diagnostics. Exit codes: `0` ok, `2` usage, `3` not found, `4` unavailable,
`5` ambiguous selector, `6` internal, `9` partial. See [docs/confex.md](docs/confex.md) for
what is implemented and how the spec maps onto this schema.

To schedule a sync, use `launchd` (user agents in `~/Library/LaunchAgents` need no admin) or
`cron`.

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

Restrictions and content properties change without a version bump. Incremental runs refresh
them only for changed pages, unless `refresh_unchanged_metadata = true` (slower). A
`--full` run always refreshes everything.

## Example queries

```sql
-- Page tree of a space
SELECT repeat('  ', depth) || title FROM source.pages p
JOIN source.spaces s USING (space_id)
WHERE s.space_key = 'DEMO' AND NOT is_deleted ORDER BY ancestor_ids || page_id;

-- Broken internal links (target title not found in the mirror)
SELECT page_id, target_space_key, target_title FROM extracted.current_page_links
WHERE link_type = 'page' AND target_page_id IS NULL;

-- All "Page Properties" values
SELECT page_id, prop->>'key' AS key, prop->>'value' AS value
FROM extracted.macro_extractions, jsonb_array_elements(data->'properties') prop
WHERE extraction_type = 'page-properties';

-- Code blocks by language
SELECT data->>'language', count(*) FROM extracted.macro_extractions
WHERE extraction_type = 'code' GROUP BY 1;
```

## Security

- Read-only: the HTTP client can only send `GET`, and the test fake fails on anything else.
- The PAT never goes to disk via this tool. It is registered with a logging filter that
  redacts it everywhere, `Authorization` values are masked, and errors stored in
  `sync_runs` pass through the same redaction.
- TLS verification is always on. The client refuses pagination links pointing at another
  host, so the token is only ever sent to the configured instance.
- Storage XML is parsed with entity resolution and network access disabled (no XXE).
- Mirrored content stays in your local database. Nothing is sent to any other service.

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
Confluence in `tests/fake_confluence.py` and storage fixtures in `tests/fixtures/`.

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
