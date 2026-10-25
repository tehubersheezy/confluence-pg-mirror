"""All SQL writes and reads used by the sync and extraction pipelines.

Every write is an idempotent upsert keyed on Confluence identifiers, so replaying a sync
(or a page) converges on the same state. "Set" children of a page (labels, properties,
restrictions) are reconciled: rows seen in this fetch are upserted, rows not seen are
removed. Attachments are soft-deleted instead, so their history stays queryable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from confluence_mirror import __version__

_JSON_COLUMNS = frozenset(
    {
        "raw",
        "value",
        "parameters",
        "data",
        "header",
        "cells",
        "records",
        "errors",
        "stats",
        "document",
        "diagnostics",
        "heading_path",
        "evidence",
    }
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


_BODY_COLUMNS = {"storage": "body_storage", "export_view": "body_export_view", "view": "body_view"}


def upsert_content(conn: psycopg.Connection, row: dict, run_id: int) -> list[str]:
    """Store the latest capture of a (page, version); returns representations whose body
    changed without a version bump. The replaced body is archived in superseded_bodies.
    A representation that was not fetched (None) keeps its stored body."""
    key = (row["page_id"], row["version_number"])
    old = conn.execute(
        "SELECT body_storage, body_export_view, body_view, fetched_at FROM source.page_content "
        "WHERE page_id = %s AND version_number = %s",
        key,
    ).fetchone()
    changed: list[str] = []
    if old is not None:
        for (rep, column), previous in zip(_BODY_COLUMNS.items(), old[:3], strict=True):
            new = row.get(column)
            if previous is not None and new is not None and new != previous:
                changed.append(rep)
                conn.execute(
                    "INSERT INTO source.superseded_bodies (page_id, version_number, "
                    "representation, body_sha256, body, captured_at, sync_run_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (*key, rep, sha256_hex(previous), previous, old[3], run_id),
                )
    _upsert(
        conn,
        "source.page_content",
        dict(row, sync_run_id=run_id),
        ["page_id", "version_number"],
        update=[c for c in row if c not in _BODY_COLUMNS.values() and c != "storage_sha256"]
        + ["sync_run_id"],
        extra_update=", ".join(
            [
                f"{c} = COALESCE(EXCLUDED.{c}, page_content.{c})"
                for c in (*_BODY_COLUMNS.values(), "storage_sha256")
            ]
            + ["fetched_at = now()"]
        ),
    )
    return changed


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def superseded_bodies(conn, page_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT version_number, representation, body_sha256, body, captured_at, superseded_at "
        "FROM source.superseded_bodies WHERE page_id = %s ORDER BY captured_at",
        (page_id,),
    )
    return _dicts(cur)


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


# -- structural parse artifacts (confex) ----------------------------------------------------


def stored_bodies(conn, page_id: int, version: int | None = None) -> dict | None:
    """Bodies of one stored (page, version); the page's current version when ``version`` is
    None. Returns None when no content row exists."""
    row = conn.execute(
        "SELECT c.page_id, c.version_number, c.body_storage, c.body_export_view, c.body_view, "
        "c.fetched_at FROM source.page_content c JOIN source.pages p ON p.page_id = c.page_id "
        "WHERE c.page_id = %(page)s "
        "AND c.version_number = COALESCE(%(version)s::integer, p.current_version)",
        {"page": page_id, "version": version},
    ).fetchone()
    if row is None:
        return None
    return {
        "page_id": row[0],
        "version_number": row[1],
        "bodies": {"storage": row[2], "export_view": row[3], "view": row[4]},
        "fetched_at": row[5],
    }


def content_versions(conn, page_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT version_number, body_storage IS NOT NULL, body_export_view IS NOT NULL, "
        "body_view IS NOT NULL, fetched_at FROM source.page_content WHERE page_id = %s "
        "ORDER BY version_number",
        (page_id,),
    )
    return [
        {
            "version_number": v,
            "representations": [
                name
                for name, present in zip(("storage", "export_view", "view"), flags, strict=True)
                if present
            ],
            "fetched_at": fetched,
        }
        for v, *flags, fetched in cur.fetchall()
    ]


def parse_targets(
    conn, space_id: int | None, page_ids: Sequence[int] | None, all_versions: bool
) -> list[tuple[int, int]]:
    return [(p, v) for p, v, _ in page_versions_to_extract(conn, space_id, page_ids, all_versions)]


def artifact_exists(conn, artifact_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM extracted.parse_artifacts WHERE artifact_id = %s", (artifact_id,)
    ).fetchone()
    return row is not None


def insert_parse_artifact(conn, artifact: dict) -> bool:
    """Store an artifact and its table index; False if it already existed (immutable)."""
    cur = conn.execute(
        "INSERT INTO extracted.parse_artifacts (artifact_id, snapshot_id, instance_id, page_id, "
        "version_number, representation, body_sha256, parser_version, config_fingerprint, "
        "status, table_count, document, diagnostics) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (artifact_id) DO NOTHING",
        (
            artifact["artifact_id"],
            artifact["snapshot_id"],
            artifact["instance_id"],
            artifact["page_id"],
            artifact["version_number"],
            artifact["representation"],
            artifact["body_sha256"],
            artifact["parser_version"],
            artifact["config_fingerprint"],
            artifact["status"],
            len(artifact["tables"]),
            Jsonb(artifact),
            Jsonb(artifact["diagnostics"]),
        ),
    )
    if cur.rowcount == 0:
        return False
    for table in artifact["tables"]:
        _insert(
            conn,
            "extracted.table_occurrences",
            {
                "table_id": table["table_id"],
                "artifact_id": artifact["artifact_id"],
                "page_id": artifact["page_id"],
                "version_number": artifact["version_number"],
                "representation": artifact["representation"],
                **{
                    k: table[k]
                    for k in (
                        "ordinal",
                        "parent_table_id",
                        "parent_cell_id",
                        "depth",
                        "element_path",
                        "row_count",
                        "column_count",
                        "geometry_status",
                        "extraction_eligible",
                        "heading_path",
                    )
                },
            },
        )
    return True


def load_artifact(conn, artifact_id: str) -> dict | None:
    row = conn.execute(
        "SELECT document FROM extracted.parse_artifacts WHERE artifact_id = %s", (artifact_id,)
    ).fetchone()
    return row[0] if row else None


def table_occurrence(conn, table_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT table_id, artifact_id, page_id, version_number, representation, ordinal "
        "FROM extracted.table_occurrences WHERE table_id = %s",
        (table_id,),
    )
    return _one_dict(cur)


def list_table_occurrences(
    conn, space_id: int | None, page_id: int | None, nested: str, representation: str | None
) -> list[dict]:
    """Table occurrences of current, non-deleted page versions."""
    cur = conn.execute(
        "SELECT t.table_id, t.page_id, p.title, s.space_key, t.version_number, t.representation, "
        "t.ordinal, t.depth, t.parent_table_id, t.row_count, t.column_count, "
        "t.geometry_status, t.extraction_eligible, t.heading_path, t.artifact_id "
        "FROM extracted.table_occurrences t "
        "JOIN source.pages p ON p.page_id = t.page_id AND p.current_version = t.version_number "
        "JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE NOT p.is_deleted "
        "AND (%(space)s::bigint IS NULL OR p.space_id = %(space)s) "
        "AND (%(page)s::bigint IS NULL OR t.page_id = %(page)s) "
        "AND (%(rep)s::text IS NULL OR t.representation = %(rep)s) "
        "AND (%(nested)s = 'any' OR (%(nested)s = 'only') = (t.depth > 0)) "
        "ORDER BY s.space_key, t.page_id, t.representation, t.ordinal, t.table_id",
        {"space": space_id, "page": page_id, "rep": representation, "nested": nested},
    )
    return _dicts(cur)


# -- mirror navigation (read-only) -----------------------------------------------------------

# Deterministic sibling order: source position when known, then a normalized title, then ID.
_PAGE_ORDER = "position NULLS LAST, lower(title), page_id"


def _dicts(cur) -> list[dict]:
    names = [d.name for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _one_dict(cur) -> dict | None:
    found = _dicts(cur)
    return found[0] if found else None


def list_spaces(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT s.space_key, s.name, s.space_type, s.status, s.sync_enabled, "
        "s.last_successful_sync_at, s.last_full_sync_at, "
        "(SELECT count(*) FROM source.pages p WHERE p.space_id = s.space_id "
        " AND NOT p.is_deleted) AS mirrored_pages, "
        "(SELECT count(*) FROM extracted.table_occurrences t JOIN source.pages p "
        " ON p.page_id = t.page_id AND p.current_version = t.version_number "
        " WHERE p.space_id = s.space_id AND NOT p.is_deleted "
        " AND t.representation = 'storage') AS parsed_storage_tables "
        "FROM source.spaces s ORDER BY s.space_key"
    )
    return _dicts(cur)


def get_space(conn, space_key: str) -> dict | None:
    cur = conn.execute(
        "SELECT space_id, space_key, name, space_type, status, description_plain, homepage_id, "
        "sync_enabled, last_successful_sync_at, last_full_sync_at, last_seen_at "
        "FROM source.spaces WHERE space_key = %s",
        (space_key,),
    )
    return _one_dict(cur)


def space_pages(conn, space_id: int) -> list[dict]:
    """Metadata of every non-deleted page in a space (no bodies)."""
    cur = conn.execute(
        "SELECT page_id, title, parent_id, position, depth, content_type "
        f"FROM source.pages WHERE space_id = %s AND NOT is_deleted ORDER BY {_PAGE_ORDER}",
        (space_id,),
    )
    return _dicts(cur)


def get_page(conn, page_id: int) -> dict | None:
    cur = conn.execute(
        "SELECT p.page_id, p.title, p.status, p.content_type, p.parent_id, p.ancestor_ids, "
        "p.depth, p.position, p.current_version, p.last_modified_at, p.last_modified_by, "
        "p.webui_path, p.last_seen_at, p.is_deleted, p.deleted_at, p.deletion_reason, "
        "p.moved_at, s.space_key, s.space_id "
        "FROM source.pages p JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE p.page_id = %s",
        (page_id,),
    )
    return _one_dict(cur)


def page_children(conn, page_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT page_id, title, position, current_version FROM source.pages "
        f"WHERE parent_id = %s AND NOT is_deleted ORDER BY {_PAGE_ORDER}",
        (page_id,),
    )
    return _dicts(cur)


def page_siblings(conn, page_id: int) -> list[dict]:
    """Pages sharing the selected page's parent (or, for a root, the space's other roots)."""
    cur = conn.execute(
        "SELECT q.page_id, q.title, q.position FROM source.pages p "
        "JOIN source.pages q ON q.space_id = p.space_id "
        " AND q.parent_id IS NOT DISTINCT FROM p.parent_id "
        "WHERE p.page_id = %s AND NOT q.is_deleted "
        "ORDER BY q.position NULLS LAST, lower(q.title), q.page_id",
        (page_id,),
    )
    return _dicts(cur)


def page_ancestors(conn, page_id: int, max_depth: int = 1000) -> list[dict]:
    """Chain from the page up to the furthest mirrored ancestor, nearest first.

    Each row reports ``cycle`` when the walk revisits a page; the walk stops there.
    """
    cur = conn.execute(
        "WITH RECURSIVE up (page_id, parent_id, title, n, path, cycle) AS ("
        "  SELECT p.page_id, p.parent_id, p.title, 0, ARRAY[p.page_id], false "
        "  FROM source.pages p WHERE p.page_id = %(page)s "
        "  UNION ALL "
        "  SELECT q.page_id, q.parent_id, q.title, up.n + 1, up.path || q.page_id, "
        "         q.page_id = ANY(up.path) "
        "  FROM up JOIN source.pages q ON q.page_id = up.parent_id "
        "  WHERE NOT up.cycle AND up.n < %(max)s"
        ") SELECT page_id, parent_id, title, n AS distance, cycle FROM up ORDER BY n",
        {"page": page_id, "max": max_depth},
    )
    return _dicts(cur)


def page_subtree(conn, root_id: int, max_depth: int) -> list[dict]:
    """The root and its known descendants up to ``max_depth`` edges, with cycle detection."""
    cur = conn.execute(
        "WITH RECURSIVE down (page_id, parent_id, title, position, depth, path, cycle) AS ("
        "  SELECT p.page_id, p.parent_id, p.title, p.position, 0, ARRAY[p.page_id], false "
        "  FROM source.pages p WHERE p.page_id = %(root)s "
        "  UNION ALL "
        "  SELECT q.page_id, q.parent_id, q.title, q.position, down.depth + 1, "
        "         down.path || q.page_id, q.page_id = ANY(down.path) "
        "  FROM down JOIN source.pages q ON q.parent_id = down.page_id AND NOT q.is_deleted "
        "  WHERE NOT down.cycle AND down.depth < %(max)s"
        ") SELECT d.page_id, d.parent_id, d.title, d.position, d.depth, d.cycle, "
        "  (SELECT count(*) FROM source.pages c WHERE c.parent_id = d.page_id "
        "   AND NOT c.is_deleted) AS child_count "
        "FROM down d ORDER BY d.depth, d.position NULLS LAST, lower(d.title), d.page_id",
        {"root": root_id, "max": max_depth},
    )
    return _dicts(cur)


def page_summary_counts(conn, page_id: int, version: int) -> dict:
    row = conn.execute(
        "SELECT "
        "(SELECT count(*) FROM extracted.page_macros WHERE page_id = %(p)s "
        " AND version_number = %(v)s), "
        "(SELECT count(*) FROM extracted.page_links WHERE page_id = %(p)s "
        " AND version_number = %(v)s), "
        "(SELECT count(*) FROM source.attachments WHERE page_id = %(p)s AND NOT is_deleted), "
        "(SELECT count(*) FROM source.access_restrictions WHERE page_id = %(p)s), "
        "(SELECT count(*) FROM source.pages WHERE parent_id = %(p)s AND NOT is_deleted)",
        {"p": page_id, "v": version},
    ).fetchone()
    labels = conn.execute(
        "SELECT prefix, name FROM source.page_labels WHERE page_id = %s ORDER BY prefix, name",
        (page_id,),
    ).fetchall()
    return {
        "macros": row[0],
        "links": row[1],
        "attachments": row[2],
        "direct_restrictions": row[3],
        "children": row[4],
        "labels": [f"{p}:{n}" if p != "global" else n for p, n in labels],
    }


def page_artifacts(conn, page_id: int, version: int) -> list[dict]:
    cur = conn.execute(
        "SELECT artifact_id, snapshot_id, representation, status, table_count, parser_version, "
        "parsed_at FROM extracted.parse_artifacts WHERE page_id = %s AND version_number = %s "
        "ORDER BY representation, parsed_at",
        (page_id, version),
    )
    return _dicts(cur)


def page_labels(conn, page_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT prefix, name, label_id, first_seen_at, last_seen_at FROM source.page_labels "
        "WHERE page_id = %s ORDER BY prefix, name",
        (page_id,),
    )
    return _dicts(cur)


def page_properties(conn, page_id: int, key: str | None = None) -> list[dict]:
    cur = conn.execute(
        "SELECT property_key, property_id, value, version_number, last_seen_at "
        "FROM source.page_properties WHERE page_id = %s "
        "AND (%s::text IS NULL OR property_key = %s) ORDER BY property_key",
        (page_id, key, key),
    )
    return _dicts(cur)


def page_attachments(conn, page_id: int, include_deleted: bool) -> list[dict]:
    cur = conn.execute(
        "SELECT attachment_id, title, media_type, file_size, version_number, status, "
        "created_at, created_by, comment, is_deleted, deleted_at, last_seen_at "
        "FROM source.attachments WHERE page_id = %s AND (%s OR NOT is_deleted) "
        "ORDER BY lower(title), attachment_id",
        (page_id, include_deleted),
    )
    return _dicts(cur)


def page_version_rows(conn, page_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT v.version_number, v.title, v.parent_id, v.created_at, v.created_by, "
        "v.message, v.minor_edit, v.recorded_at, "
        "(c.page_id IS NOT NULL) AS body_stored, c.fetched_at AS body_fetched_at, "
        "(SELECT count(*) FROM source.superseded_bodies b WHERE b.page_id = v.page_id "
        " AND b.version_number = v.version_number) AS superseded_captures "
        "FROM source.page_versions v LEFT JOIN source.page_content c "
        "ON c.page_id = v.page_id AND c.version_number = v.version_number "
        "WHERE v.page_id = %s ORDER BY v.version_number",
        (page_id,),
    )
    return _dicts(cur)


def version_metadata(conn, page_id: int, version: int) -> dict | None:
    cur = conn.execute(
        "SELECT version_number, title, parent_id, created_at, created_by, message "
        "FROM source.page_versions WHERE page_id = %s AND version_number = %s",
        (page_id, version),
    )
    return _one_dict(cur)


def page_macro_rows(conn, page_id: int, version: int, name: str | None) -> list[dict]:
    cur = conn.execute(
        "SELECT macro_index, macro_name, macro_id, parent_macro_index, depth, element_path, "
        "parameters, body_type, body_text FROM extracted.page_macros "
        "WHERE page_id = %s AND version_number = %s AND (%s::text IS NULL OR macro_name = %s) "
        "ORDER BY macro_index",
        (page_id, version, name, name),
    )
    return _dicts(cur)


def outbound_links(conn, page_id: int, version: int) -> list[dict]:
    cur = conn.execute(
        "SELECT l.link_index, l.link_type, l.source_element, l.element_path, l.target_page_id, "
        "l.target_space_key, l.target_title, l.target_filename, l.target_user_key, "
        "l.target_url, l.anchor, l.link_text, "
        "(tp.page_id IS NOT NULL AND NOT tp.is_deleted) AS target_in_mirror "
        "FROM extracted.page_links l LEFT JOIN source.pages tp ON tp.page_id = l.target_page_id "
        "WHERE l.page_id = %s AND l.version_number = %s ORDER BY l.link_index",
        (page_id, version),
    )
    return _dicts(cur)


def inbound_links(conn, page_id: int) -> list[dict]:
    """Links to the page from the current version of other mirrored pages."""
    cur = conn.execute(
        "SELECT l.page_id AS source_page_id, p.title AS source_title, s.space_key, "
        "l.link_index, l.link_type, l.anchor, l.link_text "
        "FROM extracted.page_links l "
        "JOIN source.pages p ON p.page_id = l.page_id AND p.current_version = l.version_number "
        "JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE l.target_page_id = %s AND NOT p.is_deleted AND l.page_id <> %s "
        "ORDER BY l.page_id, l.link_index",
        (page_id, page_id),
    )
    return _dicts(cur)


def insert_search_blocks(conn, rows: list[dict]) -> None:
    for row in rows:
        conn.execute(
            "INSERT INTO extracted.search_blocks (artifact_id, block_id, page_id, version_number, "
            "block_kind, section_id, table_id, cell_id, text) "
            "VALUES (%(artifact_id)s, %(block_id)s, %(page_id)s, %(version_number)s, "
            "%(block_kind)s, %(section_id)s, %(table_id)s, %(cell_id)s, %(text)s) "
            "ON CONFLICT DO NOTHING",
            row,
        )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_pages(
    conn,
    *,
    space_id: int | None,
    title: str | None,
    label: str | None,
    has_tables: bool,
    limit: int,
) -> list[dict]:
    """Current, non-deleted pages matching every given metadata filter (substring, ILIKE)."""
    cur = conn.execute(
        "SELECT p.page_id, p.title, s.space_key, p.current_version FROM source.pages p "
        "JOIN source.spaces s ON s.space_id = p.space_id WHERE NOT p.is_deleted "
        "AND (%(space)s::bigint IS NULL OR p.space_id = %(space)s) "
        "AND (%(title)s::text IS NULL OR p.title ILIKE %(title)s) "
        "AND (%(label)s::text IS NULL OR EXISTS (SELECT 1 FROM source.page_labels l "
        "     WHERE l.page_id = p.page_id AND l.name = %(label)s)) "
        "AND (NOT %(tables)s OR EXISTS (SELECT 1 FROM extracted.table_occurrences t "
        "     WHERE t.page_id = p.page_id AND t.version_number = p.current_version)) "
        "ORDER BY lower(p.title), p.page_id LIMIT %(limit)s",
        {
            "space": space_id,
            "title": _like(title) if title else None,
            "label": label,
            "tables": has_tables,
            "limit": limit,
        },
    )
    return _dicts(cur)


def search_text(
    conn, *, text: str, page_ids: list[int] | None, space_id: int | None, limit: int
) -> list[dict]:
    """Search-projection blocks of current versions containing ``text`` (ILIKE)."""
    cur = conn.execute(
        "SELECT b.page_id, p.title, s.space_key, b.version_number, b.block_id, b.block_kind, "
        "b.section_id, b.table_id, b.cell_id, b.text, b.indexed_at "
        "FROM extracted.search_blocks b "
        "JOIN source.pages p ON p.page_id = b.page_id AND p.current_version = b.version_number "
        "JOIN source.spaces s ON s.space_id = p.space_id "
        "WHERE NOT p.is_deleted AND b.text ILIKE %(text)s "
        "AND (%(space)s::bigint IS NULL OR p.space_id = %(space)s) "
        "AND (%(ids)s::bigint[] IS NULL OR b.page_id = ANY(%(ids)s)) "
        "ORDER BY lower(p.title), b.page_id, b.block_id LIMIT %(limit)s",
        {"text": _like(text), "space": space_id, "ids": page_ids, "limit": limit},
    )
    return _dicts(cur)


def search_projection_state(conn, space_id: int | None) -> dict:
    row = conn.execute(
        "SELECT count(DISTINCT p.page_id), count(DISTINCT b.page_id), max(b.indexed_at) "
        "FROM source.pages p LEFT JOIN extracted.search_blocks b "
        "ON b.page_id = p.page_id AND b.version_number = p.current_version "
        "WHERE NOT p.is_deleted AND (%s::bigint IS NULL OR p.space_id = %s)",
        (space_id, space_id),
    ).fetchone()
    return {"pages": row[0], "pages_indexed": row[1], "indexed_at": row[2]}


# -- correspondence -------------------------------------------------------------------------


def latest_artifacts(conn, page_id: int, version: int) -> dict[str, dict]:
    """The most recently parsed artifact per representation for one (page, version)."""
    cur = conn.execute(
        "SELECT DISTINCT ON (representation) representation, document "
        "FROM extracted.parse_artifacts WHERE page_id = %s AND version_number = %s "
        "ORDER BY representation, parsed_at DESC",
        (page_id, version),
    )
    return {rep: doc for rep, doc in cur.fetchall()}


def insert_correspondence(conn, rows: list[dict]) -> int:
    added = 0
    for row in rows:
        data = {k: v for k, v in row.items() if k not in ("left_ordinal", "right_ordinal")}
        cur = conn.execute(
            sql.SQL(
                "INSERT INTO extracted.table_correspondence ({c}) VALUES ({v}) "
                "ON CONFLICT (correspondence_id) DO NOTHING"
            ).format(
                c=sql.SQL(", ").join(map(sql.Identifier, data)),
                v=sql.SQL(", ").join(sql.Placeholder() * len(data)),
            ),
            [_adapt(k, v) for k, v in data.items()],
        )
        added += cur.rowcount
    return added


def correspondence_rows(conn, page_id: int, version: int, artifact_ids: list[str]) -> list[dict]:
    cur = conn.execute(
        "SELECT c.*, d.decision, d.actor AS decided_by, d.reason AS decision_reason, "
        "d.decided_at FROM extracted.table_correspondence c "
        "LEFT JOIN LATERAL (SELECT decision, actor, reason, decided_at "
        "  FROM review.correspondence_decisions x WHERE x.correspondence_id = c.correspondence_id "
        "  ORDER BY decided_at DESC, decision_id DESC LIMIT 1) d ON true "
        "WHERE c.page_id = %s AND c.version_number = %s "
        "AND c.left_artifact_id = ANY(%s) AND c.right_artifact_id = ANY(%s) "
        "ORDER BY c.left_representation, c.right_representation, c.correspondence_id",
        (page_id, version, artifact_ids, artifact_ids),
    )
    return _dicts(cur)


def correspondence_for_table(conn, table_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT c.correspondence_id, c.left_table_id, c.right_table_id, c.left_representation, "
        "c.right_representation, c.state, c.method, c.evidence, d.decision "
        "FROM extracted.table_correspondence c "
        "LEFT JOIN LATERAL (SELECT decision FROM review.correspondence_decisions x "
        "  WHERE x.correspondence_id = c.correspondence_id "
        "  ORDER BY decided_at DESC, decision_id DESC LIMIT 1) d ON true "
        "WHERE c.left_table_id = %s OR c.right_table_id = %s ORDER BY c.correspondence_id",
        (table_id, table_id),
    )
    return _dicts(cur)


def get_correspondence(conn, correspondence_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM extracted.table_correspondence WHERE correspondence_id = %s",
        (correspondence_id,),
    )
    return _one_dict(cur)


def add_correspondence_decision(
    conn, correspondence_id: str, decision: str, actor: str, reason: str
) -> int:
    row = conn.execute(
        "INSERT INTO review.correspondence_decisions (correspondence_id, decision, actor, reason) "
        "VALUES (%s, %s, %s, %s) RETURNING decision_id",
        (correspondence_id, decision, actor, reason),
    ).fetchone()
    return row[0]
