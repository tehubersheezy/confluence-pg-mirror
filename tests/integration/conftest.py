"""PostgreSQL for integration tests, without Docker or admin rights.

Order of preference:
1. ``CONFLUENCE_MIRROR_TEST_DSN`` — an existing database you own (its schemas are dropped
   and recreated per test, so never point this at real data).
2. A throwaway cluster created with ``initdb``/``pg_ctl`` found on PATH, in Postgres.app,
   or under /usr/lib/postgresql. It listens only on a Unix socket in a temp directory.
3. Otherwise the integration tests are skipped (or fail, with CPM_REQUIRE_POSTGRES=1).
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest

from confluence_mirror import db

_CANDIDATE_BIN_DIRS = [
    "/Applications/Postgres.app/Contents/Versions/latest/bin",
    *sorted(glob.glob("/Applications/Postgres.app/Contents/Versions/*/bin"), reverse=True),
    *sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True),
    *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True),
]


def _find_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in _CANDIDATE_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _unavailable(reason: str):
    if os.environ.get("CPM_REQUIRE_POSTGRES") == "1":
        pytest.fail(f"PostgreSQL required (CPM_REQUIRE_POSTGRES=1): {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def pg_dsn():
    env_dsn = os.environ.get("CONFLUENCE_MIRROR_TEST_DSN")
    if env_dsn:
        yield env_dsn
        return
    initdb, pg_ctl = _find_bin("initdb"), _find_bin("pg_ctl")
    if not initdb or not pg_ctl:
        _unavailable("no PostgreSQL: set CONFLUENCE_MIRROR_TEST_DSN or put initdb on PATH")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _unavailable("initdb refuses to run as root; set CONFLUENCE_MIRROR_TEST_DSN")
    # Short base dir: Unix socket paths are limited to ~100 bytes (macOS TMPDIR is long).
    # mkdtemp creates a private (0700), randomly named directory.
    short_tmp = "/tmp" if Path("/tmp").is_dir() else None  # noqa: S108
    base = Path(tempfile.mkdtemp(prefix="cpm-", dir=short_tmp))
    data, sock = base / "data", base / "s"
    sock.mkdir()
    port = _free_port()
    subprocess.run(
        [initdb, "-D", str(data), "-U", "mirror", "--auth=trust", "-E", "UTF8", "--no-locale"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-l",
            str(base / "log"),
            "-w",
            "-o",
            f"-k {sock} -p {port} -c listen_addresses=''",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://mirror@/postgres?host={sock}&port={port}"
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


def reset_schema(conn: psycopg.Connection) -> None:
    for schema in ("pipeline", "review", "extracted", "source", "sync"):
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.commit()


@pytest.fixture
def conn(pg_dsn):
    connection = db.connect(pg_dsn)
    reset_schema(connection)
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def raw_conn(pg_dsn):
    """A connection to an empty (unmigrated) database."""
    connection = db.connect(pg_dsn)
    reset_schema(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
