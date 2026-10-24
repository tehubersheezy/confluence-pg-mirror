import pytest

from confluence_mirror import db
from tests.unit.test_migrations_files import TABLES

from .helpers import rows, scalar

pytestmark = pytest.mark.integration


def test_migrate_creates_all_tables_and_is_idempotent(raw_conn):
    applied = db.migrate(raw_conn)
    assert [m.version for m in applied] == [m.version for m in db.available_migrations()]
    found = {
        r[0]
        for r in rows(
            raw_conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema IN ('source', 'extracted', 'sync') AND table_type = 'BASE TABLE'",
        )
    }
    assert set(TABLES) <= found
    assert db.migrate(raw_conn) == []
    assert all(s["applied"] and s["checksum_ok"] for s in db.migration_status(raw_conn))


def test_partial_target_then_rest(raw_conn):
    assert [m.version for m in db.migrate(raw_conn, target=1)] == [1]
    assert scalar(raw_conn, "SELECT to_regclass('extracted.page_links')") is None
    assert [m.version for m in db.migrate(raw_conn)][0] == 2


def test_modified_migration_is_detected(raw_conn):
    db.migrate(raw_conn)
    raw_conn.execute("UPDATE sync.schema_migrations SET checksum = 'tampered' WHERE version = 1")
    raw_conn.commit()
    with pytest.raises(db.MigrationError, match="modified"):
        db.migrate(raw_conn)


def test_foreign_keys_enforced(conn):
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO source.pages (page_id, space_id, title, status, current_version)"
            " VALUES (1, 999, 't', 'current', 1)"
        )
    conn.rollback()
