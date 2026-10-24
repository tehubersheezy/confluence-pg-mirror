"""Database connection and versioned migrations.

Migrations are plain SQL files named ``NNNN_description.sql`` shipped inside the package.
Each is applied once, in order, inside its own transaction, and recorded with its SHA-256
in ``sync.schema_migrations``; an applied file whose checksum later changes is an error
(fix forward with a new migration instead of editing history).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

import psycopg

MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x434D4952  # advisory lock id for migrations ("CMIR")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=False, application_name="confluence-pg-mirror")


def available_migrations() -> list[Migration]:
    found: list[Migration] = []
    for entry in resources.files("confluence_mirror.migrations").iterdir():
        match = MIGRATION_RE.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), match.group(2), entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version numbers")
    return found


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS sync")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sync.schema_migrations (
               version    integer PRIMARY KEY,
               name       text NOT NULL,
               checksum   text NOT NULL,
               applied_at timestamptz NOT NULL DEFAULT now())"""
    )


def applied_migrations(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    exists = conn.execute("SELECT to_regclass('sync.schema_migrations')").fetchone()[0]
    if exists is None:
        return {}
    rows = conn.execute("SELECT version, name, checksum FROM sync.schema_migrations").fetchall()
    return {v: (n, c) for v, n, c in rows}


def migrate(conn: psycopg.Connection, target: int | None = None) -> list[Migration]:
    """Apply pending migrations up to ``target``; returns those applied."""
    applied_now: list[Migration] = []
    # Serialise concurrent migrators across the whole run (session-level lock).
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    conn.commit()
    try:
        with conn.transaction():
            _ensure_table(conn)
        done = applied_migrations(conn)
        for migration in available_migrations():
            if target is not None and migration.version > target:
                break
            if migration.version in done:
                if done[migration.version][1] != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} was modified after "
                        "being applied; add a new migration instead"
                    )
                continue
            with conn.transaction():
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO sync.schema_migrations (version, name, checksum) "
                    "VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            conn.commit()
            applied_now.append(migration)
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        conn.commit()
    return applied_now


def migration_status(conn: psycopg.Connection) -> list[dict]:
    done = applied_migrations(conn)
    conn.rollback()
    return [
        {
            "version": m.version,
            "name": m.name,
            "applied": m.version in done,
            "checksum_ok": (m.version not in done) or done[m.version][1] == m.checksum,
        }
        for m in available_migrations()
    ]
