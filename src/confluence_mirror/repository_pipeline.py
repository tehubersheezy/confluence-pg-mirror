"""SQL for the extraction pipeline (schema ``pipeline``) and review/export (``review``).

Same rules as :mod:`repository`: values are parameters, identifiers are composed with
``psycopg.sql``, JSON goes through ``Jsonb``. Immutable artifacts are inserted with
``ON CONFLICT DO NOTHING`` so a resumed or repeated commit is idempotent.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from confluence_mirror.repository import _dicts, _one_dict

_JSON = frozenset(
    {
        "scope",
        "requested_stages",
        "versions",
        "input_manifest",
        "stage_counts",
        "usage",
        "budget",
        "diagnostics",
        "detail",
        "request_body",
        "request_context",
        "response_meta",
        "result",
        "manifest",
        "payload",
        "redaction_map",
        "record",
        "patch",
        "issues",
        "metrics",
        "match_key",
        "record_keys",
        "evidence",
        "excluded",
    }
)


def _ins(conn, table: str, row: dict, conflict: str | None = None) -> int:
    schema, name = table.split(".")
    cols = list(row)
    query = sql.SQL("INSERT INTO {t} ({c}) VALUES ({v})").format(
        t=sql.Identifier(schema, name),
        c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        v=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    if conflict:
        query += sql.SQL(" ON CONFLICT ({k}) DO NOTHING").format(k=sql.Identifier(conflict))
    values = [Jsonb(row[c]) if c in _JSON and row[c] is not None else row[c] for c in cols]
    return conn.execute(query, values).rowcount


# -- runs ---------------------------------------------------------------------------------


def create_run(conn, row: dict) -> None:
    _ins(conn, "pipeline.runs", row)


def update_run(conn, run_id: str, **fields: Any) -> None:
    sets = [sql.SQL("{c} = %s").format(c=sql.Identifier(k)) for k in fields]
    values = [Jsonb(v) if k in _JSON and v is not None else v for k, v in fields.items()]
    conn.execute(
        sql.SQL("UPDATE pipeline.runs SET {s} WHERE run_id = %s").format(
            s=sql.SQL(", ").join(sets)
        ),
        [*values, run_id],
    )


def finish_run(conn, run_id: str, status: str, **fields: Any) -> None:
    update_run(conn, run_id, status=status, **fields)
    conn.execute("UPDATE pipeline.runs SET finished_at = now() WHERE run_id = %s", (run_id,))


def get_run(conn, run_id: str) -> dict | None:
    return _one_dict(conn.execute("SELECT * FROM pipeline.runs WHERE run_id = %s", (run_id,)))


def list_runs(conn, limit: int) -> list[dict]:
    return _dicts(
        conn.execute(
            "SELECT run_id, command, status, scope, stage_counts, usage, started_at, finished_at, "
            "parent_run_id FROM pipeline.runs ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
    )


def upsert_item(conn, run_id: str, stage: str, item_id: str, status: str, **fields: Any) -> None:
    detail = fields.pop("detail", {})
    conn.execute(
        "INSERT INTO pipeline.run_items (run_id, stage, item_id, status, detail, attempts, "
        "output_ref) VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id, stage, item_id) DO UPDATE SET status = EXCLUDED.status, "
        "detail = EXCLUDED.detail, attempts = pipeline.run_items.attempts + EXCLUDED.attempts, "
        "output_ref = COALESCE(EXCLUDED.output_ref, pipeline.run_items.output_ref), "
        "lease_owner = NULL, lease_expires_at = NULL, updated_at = now()",
        (
            run_id,
            stage,
            item_id,
            status,
            Jsonb(detail),
            fields.get("attempts", 0),
            fields.get("output_ref"),
        ),
    )


def run_items(conn, run_id: str, status: str | None = None, stage: str | None = None) -> list[dict]:
    return _dicts(
        conn.execute(
            "SELECT * FROM pipeline.run_items WHERE run_id = %s "
            "AND (%s::text IS NULL OR status = %s) AND (%s::text IS NULL OR stage = %s) "
            "ORDER BY stage, item_id",
            (run_id, status, status, stage, stage),
        )
    )


def claim_item(conn, run_id: str, stage: str, item_id: str, owner: str, seconds: int) -> bool:
    """Lease an item unless another live lease holds it (expired leases are reclaimable)."""
    cur = conn.execute(
        "UPDATE pipeline.run_items SET status = 'running', lease_owner = %s, "
        "lease_expires_at = now() + make_interval(secs => %s), updated_at = now() "
        "WHERE run_id = %s AND stage = %s AND item_id = %s "
        "AND (lease_owner IS NULL OR lease_expires_at < now())",
        (owner, seconds, run_id, stage, item_id),
    )
    return cur.rowcount == 1


def stage_counts(conn, run_id: str) -> dict:
    rows = conn.execute(
        "SELECT stage, status, count(*) FROM pipeline.run_items WHERE run_id = %s "
        "GROUP BY stage, status",
        (run_id,),
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for stage, status, n in rows:
        out.setdefault(stage, {})[status] = n
    return out


# -- classifications / units / attempts ----------------------------------------------------


def insert_classification(conn, row: dict) -> None:
    _ins(conn, "pipeline.classifications", row, "classification_id")


def latest_classification(conn, table_id: str) -> dict | None:
    return _one_dict(
        conn.execute(
            "SELECT * FROM pipeline.classifications WHERE table_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (table_id,),
        )
    )


def insert_unit(conn, row: dict) -> None:
    _ins(conn, "pipeline.units", row, "unit_id")


def get_unit(conn, unit_id: str) -> dict | None:
    return _one_dict(conn.execute("SELECT * FROM pipeline.units WHERE unit_id = %s", (unit_id,)))


def insert_attempt(conn, row: dict) -> None:
    _ins(conn, "pipeline.attempts", row)


def finish_attempt(conn, attempt_id: str, **fields: Any) -> None:
    sets = [sql.SQL("{c} = %s").format(c=sql.Identifier(k)) for k in fields]
    values = [Jsonb(v) if k in _JSON and v is not None else v for k, v in fields.items()]
    conn.execute(
        sql.SQL(
            "UPDATE pipeline.attempts SET {s}, finished_at = now() WHERE attempt_id = %s"
        ).format(s=sql.SQL(", ").join(sets)),
        [*values, attempt_id],
    )


def get_attempt(conn, attempt_id: str) -> dict | None:
    return _one_dict(
        conn.execute("SELECT * FROM pipeline.attempts WHERE attempt_id = %s", (attempt_id,))
    )


def succeeded_attempt_for_unit(conn, unit_id: str) -> dict | None:
    return _one_dict(
        conn.execute(
            "SELECT a.* FROM pipeline.attempts a WHERE a.target_id = %s "
            "AND a.outcome = 'succeeded' AND EXISTS (SELECT 1 FROM pipeline.candidate_revisions r "
            "WHERE r.attempt_id = a.attempt_id) ORDER BY a.started_at DESC LIMIT 1",
            (unit_id,),
        )
    )


# -- revisions / validation ------------------------------------------------------------------


def latest_revision_number(conn, record_key: str) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT revision_number, revision_id FROM pipeline.candidate_revisions "
        "WHERE record_key = %s ORDER BY revision_number DESC LIMIT 1",
        (record_key,),
    ).fetchone()
    return (row[0], row[1]) if row else (0, None)


def insert_revision(conn, row: dict) -> int:
    return _ins(conn, "pipeline.candidate_revisions", row, "revision_id")


def get_revision(conn, revision_id: str) -> dict | None:
    return _one_dict(
        conn.execute(
            "SELECT r.*, p.title AS page_title, s.space_key, p.current_version "
            "FROM pipeline.candidate_revisions r LEFT JOIN source.pages p ON p.page_id = r.page_id "
            "LEFT JOIN source.spaces s ON s.space_id = r.space_id WHERE r.revision_id = %s",
            (revision_id,),
        )
    )


def revision_history(conn, record_key: str) -> list[dict]:
    return _dicts(
        conn.execute(
            "SELECT revision_id, revision_number, parent_revision_id, origin, created_at, "
            "created_by, reason FROM pipeline.candidate_revisions WHERE record_key = %s "
            "ORDER BY revision_number",
            (record_key,),
        )
    )


def insert_validation(conn, row: dict) -> None:
    _ins(conn, "pipeline.validation_results", row, "validation_id")


def latest_validation(conn, revision_id: str) -> dict | None:
    return _one_dict(
        conn.execute(
            "SELECT * FROM pipeline.validation_results WHERE revision_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (revision_id,),
        )
    )


def insert_match_proposal(conn, row: dict) -> None:
    _ins(conn, "pipeline.entity_match_proposals", row, "match_id")


# A record's state is computed from its newest revision, latest validation and latest
# review event. "superseded" means a newer revision exists for the record key.
_RECORDS = """
WITH latest AS (
  SELECT DISTINCT ON (record_key) * FROM pipeline.candidate_revisions
  ORDER BY record_key, revision_number DESC
)
SELECT r.revision_id, r.record_key, r.revision_number, r.origin, r.page_id, r.version_number,
       r.table_id, r.snapshot_id, r.schema_ref, r.profile, r.record_kind, r.record, r.run_id,
       r.created_at, r.dependency_hash, r.content_hash, s.space_key, p.title AS page_title,
       p.current_version, p.is_deleted,
       v.validation_id, v.status AS validation_status, v.issues,
       e.decision AS review_decision, e.revision_id AS reviewed_revision_id, e.actor AS reviewer,
       e.created_at AS reviewed_at, e.dependency_hash AS reviewed_dependency_hash,
       e.sequence AS review_sequence
FROM latest r
LEFT JOIN source.pages p ON p.page_id = r.page_id
LEFT JOIN source.spaces s ON s.space_id = r.space_id
LEFT JOIN LATERAL (SELECT * FROM pipeline.validation_results x WHERE x.revision_id = r.revision_id
                   ORDER BY created_at DESC LIMIT 1) v ON true
LEFT JOIN LATERAL (SELECT * FROM review.review_events x WHERE x.record_key = r.record_key
                   ORDER BY sequence DESC LIMIT 1) e ON true
WHERE (%(space)s::text IS NULL OR s.space_key = %(space)s)
  AND (%(run)s::text IS NULL OR r.run_id = %(run)s)
  AND (%(schema)s::text IS NULL OR r.schema_ref = %(schema)s)
  AND (%(page)s::bigint IS NULL OR r.page_id = %(page)s)
ORDER BY s.space_key, r.page_id, r.table_id, r.record_key
"""


def records(
    conn,
    *,
    space: str | None = None,
    run: str | None = None,
    schema: str | None = None,
    page: int | None = None,
) -> list[dict]:
    return _dicts(
        conn.execute(_RECORDS, {"space": space, "run": run, "schema": schema, "page": page})
    )


def revisions_for_run(conn, run_id: str) -> list[dict]:
    return _dicts(
        conn.execute(
            "SELECT * FROM pipeline.candidate_revisions WHERE run_id = %s ORDER BY record_key",
            (run_id,),
        )
    )


# -- review ------------------------------------------------------------------------------------


def last_review_sequence(conn, record_key: str) -> int:
    row = conn.execute(
        "SELECT max(sequence) FROM review.review_events WHERE record_key = %s", (record_key,)
    ).fetchone()
    return row[0] or 0


def insert_review_event(conn, row: dict) -> int:
    return conn.execute(
        "INSERT INTO review.review_events (record_key, sequence, revision_id, decision, "
        "corrected_revision_id, actor, reason, dependency_hash, policy_version, snapshot_id, "
        "validation_id) VALUES (%(record_key)s, %(sequence)s, %(revision_id)s, %(decision)s, "
        "%(corrected_revision_id)s, %(actor)s, %(reason)s, %(dependency_hash)s, "
        "%(policy_version)s, %(snapshot_id)s, %(validation_id)s) RETURNING event_id",
        row,
    ).fetchone()[0]


def review_events(conn, record_key: str) -> list[dict]:
    return _dicts(
        conn.execute(
            "SELECT * FROM review.review_events WHERE record_key = %s ORDER BY sequence",
            (record_key,),
        )
    )


# -- export --------------------------------------------------------------------------------------


def insert_export(conn, row: dict, revision_ids: list[str]) -> None:
    _ins(conn, "review.exports", row)
    for rid in revision_ids:
        conn.execute(
            "INSERT INTO review.export_items (export_id, revision_id) VALUES (%s, %s)",
            (row["export_id"], rid),
        )
