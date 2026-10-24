"""All SQL writes and reads used by the sync and extraction pipelines.

Every write is an idempotent upsert keyed on Confluence identifiers, so replaying a sync
(or a page) converges on the same state. "Set" children of a page (labels, properties,
restrictions) are reconciled: rows seen in this fetch are upserted, rows not seen are
removed. Attachments are soft-deleted instead, so their history stays queryable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from confluence_mirror import __version__

_JSON_COLUMNS = frozenset(
    {"raw", "value", "parameters", "data", "header", "cells", "records", "errors", "stats"}
)


def _adapt(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS and value is not None:
        return Jsonb(value)
    return value


def _upsert(
    conn: psycopg.Connection,
    table: str,
    row: dict[str, Any],
    conflict: Sequence[str],
    *,
    update: Iterable[str] | None = None,
    extra_update: str = "",
) -> None:
    schema, name = table.split(".")
    columns = list(row)
    update_cols = [c for c in (update if update is not None else columns) if c not in conflict]
    assignments = [sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in update_cols]
    if extra_update:
        assignments.append(sql.SQL(extra_update))
    action = (
        (sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(assignments))
        if assignments
        else sql.SQL("DO NOTHING")
    )
    query = (
        sql.SQL("INSERT INTO {t} ({cols}) VALUES ({vals}) ON CONFLICT ({conf}) ").format(
            t=sql.Identifier(schema, name),
            cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            conf=sql.SQL(", ").join(map(sql.Identifier, conflict)),
        )
        + action
    )
    conn.execute(query, [_adapt(c, row[c]) for c in columns])


# -- sync runs ------------------------------------------------------------------------


def abandon_stale_runs(conn: psycopg.Connection, space_key: str) -> int:
    """Mark leftover 'running' runs as abandoned. Call only while holding the space lock."""
    cur = conn.execute(
        "UPDATE sync.sync_runs SET status = 'abandoned', finished_at = now() "
        "WHERE space_key = %s AND status = 'running'",
        (space_key,),
    )
    return cur.rowcount


def start_run(conn: psycopg.Connection, space_key: str, mode: str, cursor_from: Any = None) -> int:
    row = conn.execute(
        "INSERT INTO sync.sync_runs (space_key, mode, cursor_from, tool_version) "
        "VALUES (%s, %s, %s, %s) RETURNING sync_run_id, started_at",
        (space_key, mode, cursor_from, __version__),
    ).fetchone()
    return row[0]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    counters: dict[str, int],
    errors: list[dict],
    stats: dict,
) -> None:
    allowed = {
        "pages_listed",
        "pages_fetched",
        "pages_unchanged",
        "pages_moved",
        "pages_deleted",
        "pages_failed",
        "attachments_seen",
    }
    sets = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in counters if k in allowed]
    values = [v for k, v in counters.items() if k in allowed]
    query = (
        sql.SQL(
            "UPDATE sync.sync_runs SET status = %s, finished_at = now(), "
            "cursor_to = started_at, errors = %s, stats = %s"
        )
        + (sql.SQL(", ") + sql.SQL(", ").join(sets) if sets else sql.SQL(""))
        + sql.SQL(" WHERE sync_run_id = %s")
    )
    conn.execute(query, [status, Jsonb(errors), Jsonb(stats), *values, run_id])


def run_started_at(conn: psycopg.Connection, run_id: int):
    return conn.execute(
        "SELECT started_at FROM sync.sync_runs WHERE sync_run_id = %s", (run_id,)
    ).fetchone()[0]


def recent_runs(conn: psycopg.Connection, space_key: str | None, limit: int) -> list[dict]:
    cur = conn.execute(
        "SELECT sync_run_id, space_key, mode, status, started_at, finished_at, pages_listed, "
        "pages_fetched, pages_unchanged, pages_moved, pages_deleted, pages_failed, "
        "attachments_seen, jsonb_array_length(errors) AS error_count "
        "FROM sync.sync_runs WHERE (%s::text IS NULL OR space_key = %s) "
        "ORDER BY sync_run_id DESC LIMIT %s",
        (space_key, space_key, limit),
    )
    names = [d.name for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


# -- spaces ---------------------------------------------------------------------------


def upsert_space(conn: psycopg.Connection, row: dict, run_id: int) -> None:
    data = dict(row, sync_enabled=True, last_sync_run_id=run_id)
    _upsert(conn, "source.spaces", data, ["space_id"], extra_update="last_seen_at = now()")


def ensure_space_stub(conn: psycopg.Connection, space: dict) -> int:
    """Record a space seen only as a page's new home (not synced itself)."""
    space_id = int(space["id"])
    conn.execute(
        "INSERT INTO source.spaces (space_id, space_key, name, raw) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (space_id) DO UPDATE SET last_seen_at = now()",
        (
            space_id,
            space["key"],
            space.get("name"),
            Jsonb({"id": space["id"], "key": space["key"]}),
        ),
    )
    return space_id


def space_state(conn: psycopg.Connection, space_key: str) -> dict | None:
    cur = conn.execute(
        "SELECT space_id, last_successful_sync_at, last_full_sync_at FROM source.spaces "
        "WHERE space_key = %s",
        (space_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"space_id": row[0], "last_successful_sync_at": row[1], "last_full_sync_at": row[2]}


def mark_space_synced(conn: psycopg.Connection, space_id: int, run_id: int, full: bool) -> None:
    conn.execute(
        "UPDATE source.spaces s SET last_successful_sync_at = r.started_at, "
        "last_full_sync_at = CASE WHEN %s THEN r.started_at ELSE s.last_full_sync_at END "
        "FROM sync.sync_runs r WHERE r.sync_run_id = %s AND s.space_id = %s",
        (full, run_id, space_id),
    )


def replace_space_properties(
    conn: psycopg.Connection, space_id: int, rows: list[dict], run_id: int
) -> None:
    for row in rows:
        _upsert(
            conn,
            "source.space_properties",
            dict(row, space_id=space_id, last_sync_run_id=run_id),
            ["space_id", "property_key"],
            extra_update="last_seen_at = now()",
        )
    conn.execute(
        "DELETE FROM source.space_properties WHERE space_id = %s AND NOT (property_key = ANY(%s))",
        (space_id, [r["property_key"] for r in rows]),
    )


# -- pages ----------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownPage:
    page_id: int
    current_version: int
    parent_id: int | None
    title: str
    is_deleted: bool
    has_content: bool
    extractor_version: str | None


def known_pages(conn: psycopg.Connection, space_id: int, content_type: str) -> dict[int, KnownPage]:
    cur = conn.execute(
        "SELECT p.page_id, p.current_version, p.parent_id, p.title, p.is_deleted, "
        "c.page_id IS NOT NULL, c.extractor_version "
        "FROM source.pages p LEFT JOIN source.page_content c "
        "  ON c.page_id = p.page_id AND c.version_number = p.current_version "
        "WHERE p.space_id = %s AND p.content_type = %s",
        (space_id, content_type),
    )
    return {r[0]: KnownPage(*r) for r in cur.fetchall()}


def has_content(conn: psycopg.Connection, page_id: int, version: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM source.page_content WHERE page_id = %s AND version_number = %s",
            (page_id, version),
        ).fetchone()
        is not None
    )


_MOVE_TRACKING = (
    "previous_parent_id = CASE WHEN source.pages.parent_id IS DISTINCT FROM EXCLUDED.parent_id "
    "THEN source.pages.parent_id ELSE source.pages.previous_parent_id END, "
    "moved_at = CASE WHEN source.pages.parent_id IS DISTINCT FROM EXCLUDED.parent_id "
    "THEN now() ELSE source.pages.moved_at END, "
    "last_seen_at = now(), is_deleted = false, deleted_at = NULL, deletion_reason = NULL"
)


def upsert_page(conn: psycopg.Connection, row: dict, run_id: int) -> bool:
    """Insert or update a page; returns True if its parent changed (a move)."""
    previous = conn.execute(
        "SELECT parent_id FROM source.pages WHERE page_id = %s FOR UPDATE", (row["page_id"],)
    ).fetchone()
    data = dict(row, last_sync_run_id=run_id)
    _upsert(conn, "source.pages", data, ["page_id"], extra_update=_MOVE_TRACKING)
    return previous is not None and previous[0] != row["parent_id"]


def update_page_from_listing(conn: psycopg.Connection, row: dict, run_id: int) -> bool:
    """Refresh hierarchy/visibility for a page whose version did not change."""
    previous = conn.execute(
        "SELECT parent_id FROM source.pages WHERE page_id = %s FOR UPDATE", (row["page_id"],)
    ).fetchone()
    keep = [
        "page_id",
        "space_id",
        "title",
        "status",
        "parent_id",
        "ancestor_ids",
        "depth",
        "position",
        "webui_path",
        "tinyui_path",
    ]
    data = {k: row[k] for k in keep}
    data["last_sync_run_id"] = run_id
    _upsert(
        conn,
        "source.pages",
        dict(data, current_version=row["current_version"]),
        ["page_id"],
        update=[*keep, "last_sync_run_id"],
        extra_update=_MOVE_TRACKING,
    )
    return previous is not None and previous[0] != row["parent_id"]


def insert_version(conn: psycopg.Connection, row: dict, run_id: int) -> None:
    _upsert(
        conn,
        "source.page_versions",
        dict(row, sync_run_id=run_id),
        ["page_id", "version_number"],
        update=[],
    )


def upsert_content(conn: psycopg.Connection, row: dict, run_id: int) -> None:
    _upsert(
        conn,
        "source.page_content",
        dict(row, sync_run_id=run_id),
        ["page_id", "version_number"],
        extra_update="fetched_at = now()",
    )


def mark_page_deleted(
    conn: psycopg.Connection, page_id: int, reason: str, run_id: int, status: str | None = None
) -> None:
    conn.execute(
        "UPDATE source.pages SET is_deleted = true, deleted_at = COALESCE(deleted_at, now()), "
        "deletion_reason = %s, status = COALESCE(%s, status), last_sync_run_id = %s "
        "WHERE page_id = %s",
        (reason, status, run_id, page_id),
    )


def move_page_to_space(conn: psycopg.Connection, page_id: int, space_id: int, run_id: int) -> None:
    conn.execute(
        "UPDATE source.pages SET space_id = %s, is_deleted = true, deleted_at = now(), "
        "deletion_reason = 'moved_out', moved_at = now(), last_sync_run_id = %s "
        "WHERE page_id = %s",
        (space_id, run_id, page_id),
    )


def _reconcile(
    conn: psycopg.Connection,
    table: str,
    page_id: int,
    rows: list[dict],
    key: Sequence[str],
    run_id: int,
) -> None:
    for row in rows:
        _upsert(
            conn,
            table,
            dict(row, page_id=page_id, last_sync_run_id=run_id),
            ["page_id", *key],
            extra_update="last_seen_at = now()",
        )
    schema, name = table.split(".")
    columns = sql.SQL(", ").join(map(sql.Identifier, key))
    arrays = sql.SQL(", ").join(sql.SQL("%s::text[]") for _ in key)
    conn.execute(
        sql.SQL(
            "DELETE FROM {t} WHERE page_id = %s AND ({cols}) NOT IN "
            "(SELECT * FROM unnest({arrays}))"
        ).format(t=sql.Identifier(schema, name), cols=columns, arrays=arrays),
        (page_id, *[[r[k] for r in rows] for k in key]),
    )


def replace_labels(conn, page_id: int, rows: list[dict], run_id: int) -> None:
    _reconcile(conn, "source.page_labels", page_id, rows, ["prefix", "name"], run_id)


def replace_page_properties(conn, page_id: int, rows: list[dict], run_id: int) -> None:
    _reconcile(conn, "source.page_properties", page_id, rows, ["property_key"], run_id)


def replace_restrictions(conn, page_id: int, rows: list[dict], run_id: int) -> None:
    _reconcile(
        conn,
        "source.access_restrictions",
        page_id,
        rows,
        ["operation", "subject_type", "subject_id"],
        run_id,
    )


def page_label_set(conn, page_id: int) -> set[tuple[str, str]]:
    cur = conn.execute("SELECT prefix, name FROM source.page_labels WHERE page_id = %s", (page_id,))
    return {(p, n) for p, n in cur.fetchall()}


def sync_attachments(conn, page_id: int, rows: list[dict], run_id: int) -> None:
    for row in rows:
        _upsert(
            conn,
            "source.attachments",
            dict(row, last_sync_run_id=run_id),
            ["attachment_id"],
            extra_update="last_seen_at = now(), is_deleted = false, deleted_at = NULL",
        )
    conn.execute(
        "UPDATE source.attachments SET is_deleted = true, deleted_at = now(), "
        "last_sync_run_id = %s WHERE page_id = %s AND NOT is_deleted "
        "AND NOT (attachment_id = ANY(%s))",
        (run_id, page_id, [r["attachment_id"] for r in rows]),
    )


def attachment_download_state(conn, attachment_id: str) -> tuple[int | None, str | None]:
    row = conn.execute(
        "SELECT downloaded_version, local_path FROM source.attachments WHERE attachment_id = %s",
        (attachment_id,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def record_attachment_download(
    conn, attachment_id: str, version: int | None, path: str, sha256: str
) -> None:
    conn.execute(
        "UPDATE source.attachments SET downloaded_version = %s, local_path = %s, "
        "content_sha256 = %s WHERE attachment_id = %s",
        (version, path, sha256, attachment_id),
    )


# -- extraction ---------------------------------------------------------------------------


def page_versions_to_extract(
    conn, space_id: int | None, page_ids: Sequence[int] | None, all_versions: bool
) -> list[tuple[int, int, str | None]]:
    query = (
        "SELECT c.page_id, c.version_number, s.space_key FROM source.page_content c "
        "JOIN source.pages p ON p.page_id = c.page_id "
        "JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE (%(space)s::bigint IS NULL OR p.space_id = %(space)s) "
        "AND (%(ids)s::bigint[] IS NULL OR c.page_id = ANY(%(ids)s)) "
    )
    if not all_versions:
        query += "AND c.version_number = p.current_version AND NOT p.is_deleted "
    query += "ORDER BY c.page_id, c.version_number"
    cur = conn.execute(query, {"space": space_id, "ids": list(page_ids) if page_ids else None})
    return cur.fetchall()


def storage_body(conn, page_id: int, version: int) -> str | None:
    row = conn.execute(
        "SELECT body_storage FROM source.page_content WHERE page_id = %s AND version_number = %s",
        (page_id, version),
    ).fetchone()
    return row[0] if row else None


def replace_extraction(conn, page_id: int, version: int, result: Any, run_id: int | None) -> None:
    """Replace all derived rows for one (page, version) with a fresh extraction result."""
    key = {"page_id": page_id, "version_number": version}
    for table in ("page_links", "page_tables", "macro_extractions", "page_macros"):
        conn.execute(
            sql.SQL("DELETE FROM {} WHERE page_id = %s AND version_number = %s").format(
                sql.Identifier("extracted", table)
            ),
            (page_id, version),
        )
    ev = result.extractor_version
    for macro in result.macros:
        _insert(
            conn,
            "extracted.page_macros",
            {**key, **macro, "extractor_version": ev, "sync_run_id": run_id},
        )
    for item in result.macro_extractions:
        _insert(conn, "extracted.macro_extractions", {**key, **item, "extractor_version": ev})
    for table in result.tables:
        _insert(conn, "extracted.page_tables", {**key, **table, "extractor_version": ev})
    for link in result.links:
        _insert(conn, "extracted.page_links", {**key, **link, "extractor_version": ev})
    conn.execute(
        "UPDATE source.page_content SET extractor_version = %s, extracted_at = now() "
        "WHERE page_id = %s AND version_number = %s",
        (ev, page_id, version),
    )


def _insert(conn, table: str, row: dict) -> None:
    schema, name = table.split(".")
    columns = list(row)
    conn.execute(
        sql.SQL("INSERT INTO {t} ({c}) VALUES ({v})").format(
            t=sql.Identifier(schema, name),
            c=sql.SQL(", ").join(map(sql.Identifier, columns)),
            v=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        ),
        [_adapt(c, row[c]) for c in columns],
    )


def resolve_links(conn, space_id: int | None = None) -> int:
    """Fill target_page_id for links that name a page by (space key, title)."""
    cur = conn.execute(
        "UPDATE extracted.page_links l SET target_page_id = p.page_id "
        "FROM source.pages p JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE l.target_page_id IS NULL AND l.target_title IS NOT NULL "
        "AND l.link_type IN ('page', 'blogpost', 'include') "
        "AND p.title = l.target_title AND s.space_key = l.target_space_key "
        "AND NOT p.is_deleted "
        "AND (%(space)s::bigint IS NULL OR l.page_id IN "
        "     (SELECT page_id FROM source.pages WHERE space_id = %(space)s))",
        {"space": space_id},
    )
    return cur.rowcount
