# Agent setup prompt: confluence-pg-mirror on macOS

Paste everything below the line into a coding agent running in a terminal on the user's Mac
(for example Claude Code). It is written so a small model can follow it step by step.

---

You are setting up **confluence-pg-mirror** on this Mac. It copies Confluence Data Center
spaces into a local PostgreSQL database, read-only. Work through the steps below **in order**.
After each step, run its **Check** and compare the result with **Expect**. If the result
does not match, follow **If not**; if that says STOP, stop and report to the user.

## Rules (never break these)

1. **No admin rights.** Never use `sudo`, never write outside the user's home directory,
   and never install with a `.pkg` installer.
2. **No Docker and no conda** (no Anaconda, Miniconda, micromamba or conda-forge).
3. **Never see the token.** The Confluence personal access token (PAT) is typed by the user
   into `.env` themselves. Never ask for it in chat, never print `.env`
   (`cat`, `less`, `head`, `grep` without `-c`), never echo `$CONFLUENCE_PAT`, and never
   put it in a command line or a file other than `.env`.
4. **Never weaken TLS.** Never use `curl -k`/`--insecure`, `verify=False`,
   `PYTHONHTTPSVERIFY=0`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `GIT_SSL_NO_VERIFY` or similar.
   If a download fails with a certificate error, STOP and report it.
5. **Never write to Confluence.** The tool is read-only; do not call the Confluence API
   yourself with anything except what these steps run.
6. **No company content leaves the machine.** Do not paste page content, URLs or space
   keys into any web service. Do not commit `config.toml`, `.env` or data to Git.
7. **Do not improvise.** If something fails in a way these steps do not cover, STOP and
   report the exact command and its last 20 lines of output. Do not try other fixes.

Use `zsh`/`bash` syntax. Use full paths when this prompt gives them.

## Step 1 — Preflight

Run:
```bash
uname -sm; sw_vers -productVersion; echo "$HOME"; command -v git curl
```
**Expect:** `Darwin arm64` or `Darwin x86_64`, a macOS version, and paths for `git` and
`curl`.
**If not:** if `git` is missing, run `xcode-select --install` only if the user agrees
(it opens a dialog), otherwise STOP.

## Step 2 — Get the code

Run:
```bash
mkdir -p "$HOME/src"
if [ -d "$HOME/src/confluence-pg-mirror/.git" ]; then
  git -C "$HOME/src/confluence-pg-mirror" pull --ff-only
else
  git clone https://github.com/tehubersheezy/confluence-pg-mirror "$HOME/src/confluence-pg-mirror"
fi
cd "$HOME/src/confluence-pg-mirror"
```
**Check:** `ls pyproject.toml config/config.example.toml .env.example`
**Expect:** all three files listed.
**If not:** STOP (network or proxy problem; report the git error).

Every later step runs from `$HOME/src/confluence-pg-mirror`.

## Step 3 — Python 3.11+ via uv

Run:
```bash
command -v uv || ls "$HOME/.local/bin/uv" 2>/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv venv --python 3.13 .venv
.venv/bin/python --version
```
**Expect:** `uv x.y.z` and `Python 3.13.x`.
**If not:** check whether `python3 --version` prints 3.11 or newer. If it does, run
`python3 -m venv .venv` instead and continue, using `.venv/bin/pip install` where later
steps say `uv pip install`. Otherwise STOP.

## Step 4 — Install the package

Run:
```bash
uv pip install --python .venv/bin/python -e '.[dev,truststore]'
.venv/bin/confex --version
```
**Expect:** `confex 0.1.0` (or newer).
**If not:** STOP.

## Step 5 — PostgreSQL binaries

Find PostgreSQL in this order, and stop at the first that works. Record the directory as
`PGBIN`.

a. Postgres.app, system-wide or in the user's own Applications folder:
```bash
ls /Applications/Postgres.app/Contents/Versions/latest/bin/initdb \
   "$HOME/Applications/Postgres.app/Contents/Versions/latest/bin/initdb" 2>/dev/null
```
If found: `PGBIN` is the directory of the first path listed.

b. Already on PATH: `command -v initdb` → `PGBIN` is its directory.

c. Otherwise, ask the user to install Postgres.app without admin rights, word for word:

> Please download Postgres.app from https://postgresapp.com/downloads.html, open the disk
> image, and drag Postgres.app into the **Applications folder in your home directory**
> (`~/Applications`; create it in Finder if it doesn't exist), not the system one. You
> don't need to start it. Tell me when you're done.

Wait for the user, then repeat check a. If it is still not found, STOP.

**Check:** `"$PGBIN/initdb" --version && "$PGBIN/pg_ctl" --version`
**Expect:** both print `PostgreSQL 14` or newer.
**If not:** STOP.

## Step 6 — Run the test suite

The tests use only synthetic data and start their own throwaway database, so they do not
touch anything real.

Run:
```bash
PATH="$PGBIN:$PATH" CPM_REQUIRE_POSTGRES=1 .venv/bin/pytest -q
```
**Expect:** a final line like `N passed` with **0 failed** and **0 errors**.
**If not:** STOP and report the summary lines and the first failure. Do not edit code or
tests.

## Step 7 — Create the mirror database

Use a private cluster on port **5433** (so it never collides with another PostgreSQL on
5432), listening only on this machine.

Run:
```bash
export PGDATA="$HOME/pgdata/confluence-mirror"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  mkdir -p "$HOME/pgdata"
  "$PGBIN/initdb" -D "$PGDATA" -U "$USER" -E UTF8 --auth-local=peer --auth-host=scram-sha-256
fi
"$PGBIN/pg_ctl" -D "$PGDATA" -l "$HOME/pgdata/confluence-mirror.log" -w \
  -o "-p 5433 -k /tmp -c listen_addresses=localhost" start || true
"$PGBIN/pg_isready" -h /tmp -p 5433
"$PGBIN/psql" -h /tmp -p 5433 -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='confluence_mirror'" | grep -q 1 \
  || "$PGBIN/createdb" -h /tmp -p 5433 confluence_mirror
```
**Check:** `"$PGBIN/psql" -h /tmp -p 5433 -d confluence_mirror -tAc "SELECT current_user"`
**Expect:** the user's macOS username.
**If not:** if `pg_ctl` said another server is running on the port, run
`"$PGBIN/pg_ctl" -D "$PGDATA" status`. If that shows our server is running, continue.
Otherwise STOP and report the last 20 lines of `$HOME/pgdata/confluence-mirror.log`.

The database connection string is: `postgresql:///confluence_mirror?host=/tmp&port=5433`

## Step 8 — Configure (non-secret settings)

Ask the user **two questions** and wait for the answers:
1. "What is your Confluence base URL? Include the context path if there is one, for
   example `https://confluence.yourcompany.com` or `https://wiki.yourcompany.com/confluence`."
2. "Which space key should we mirror first? Pick a small space for the first test."

Then run (only if `config.toml` does not exist yet):
```bash
[ -f config.toml ] || cp config/config.example.toml config.toml
```
Edit `config.toml` with a file-editing tool so that:
- `[confluence] base_url` = the URL from answer 1 (no trailing slash);
- `[database] dsn` = `"postgresql:///confluence_mirror?host=/tmp&port=5433"`;
- `[sync] spaces` = `["<KEY from answer 2>"]`.

Change nothing else.

**Check:** `.venv/bin/python -c "from confluence_mirror.config import load_settings; from pathlib import Path; s=load_settings(Path('config.toml'), require_confluence=True); print(s.confluence.base_url, s.sync.spaces)"`
**Expect:** the URL and the space key.
**If not:** fix the edit per the error message (for example, the URL must start with
`https://`). If the error is about anything else, STOP.

## Step 9 — The token (user does this, not you)

Run:
```bash
[ -f .env ] || cp .env.example .env
chmod 600 .env
```
Tell the user, word for word:

> Please open `~/src/confluence-pg-mirror/.env` in a text editor and put your Confluence
> personal access token after `CONFLUENCE_PAT=` (no quotes, no spaces). You create one in
> Confluence under your avatar → Settings → Personal Access Tokens; read access is enough.
> Save the file and tell me when you're done. Don't paste the token here.

Wait for the user. Then check **without printing the token**:
```bash
grep -c '^CONFLUENCE_PAT=..*' .env
```
**Expect:** `1`.
**If not:** tell the user the token line is still empty and wait again.

## Step 10 — Create the schema and test the connection

Run:
```bash
.venv/bin/confex db migrate
.venv/bin/confex db status
.venv/bin/confex doctor --check-db --check-confluence; echo "exit=$?"
```
**Expect:** `db status` shows every migration as applied; `doctor` shows `ok` for
`config`, `database` and `confluence` (with "authenticated as" the user's Confluence
username), and `exit=0`. `skipped` rows (model checks) are fine.

**If not**, read the `confluence` row's detail:
- It mentions authentication or `401`: tell the user the token was rejected and ask them
  to make a new one and update `.env`. Return to the check in Step 9.
- It mentions `CERTIFICATE_VERIFY_FAILED` or `certificate`: the company uses its own
  certificate authority. In `config.toml` set `use_system_truststore = true` under
  `[confluence]`, then run `doctor` again. If it still fails, ask the user for the path to
  their company's root CA file (PEM). Set `ca_bundle = "<path>"` instead of
  `use_system_truststore`, then run `doctor` again. If it still fails, STOP.
- The `config` row fails: read the message, fix `config.toml` if the message names a
  setting you set in Step 8, and run again. Otherwise STOP.
- A network error (DNS, timeout, connection refused): the user may need to be on the
  company network or VPN. Ask them, wait, and retry once. Then STOP.

## Step 11 — First sync

Run:
```bash
.venv/bin/confex sync --all; echo "exit=$?"
```
This can take a while for big spaces. Do not interrupt it.

**Expect:** a summary with `status: succeeded` and `exit=0`.
**If not:**
- `exit=9` / `status: partial`: some pages failed. That is acceptable for a first run.
  Note `pages_failed` and the first 3 `error` lines for the report. Do not retry.
- `exit=8`: the token was rejected or lacks access. Report it.
- Any other exit code: report the error message.

Then show the user what was mirrored:
```bash
"$PGBIN/psql" -h /tmp -p 5433 -d confluence_mirror -c "
SELECT (SELECT count(*) FROM source.pages WHERE NOT is_deleted) AS pages,
       (SELECT count(*) FROM source.attachments WHERE NOT is_deleted) AS attachments,
       (SELECT count(*) FROM source.page_content) AS bodies;"
```
Report the numbers only, not page titles or content.

## Step 12 — Final report

Reply to the user with exactly these sections:

1. **Result**: `succeeded`, `partial` or `stopped at step N`.
2. **Installed**: the Python version, the PostgreSQL source (Postgres.app or PATH) and its
   version, and the test result line from Step 6.
3. **Mirror**: the counts from Step 11 and the sync status.
4. **Problems**: anything that failed, with the exact error. Never include the token.
5. **How to use it later** (copy this block, filling in `PGBIN`):
   ```bash
   cd ~/src/confluence-pg-mirror
   # after a reboot, start the database:
   <PGBIN>/pg_ctl -D ~/pgdata/confluence-mirror -l ~/pgdata/confluence-mirror.log \
     -o "-p 5433 -k /tmp -c listen_addresses=localhost" start
   .venv/bin/confex sync --all        # incremental after the first run
   .venv/bin/confex runs sync         # history of sync runs
   <PGBIN>/psql -h /tmp -p 5433 -d confluence_mirror
   ```

Do not set up scheduled syncs, add more spaces or change code unless the user asks.
