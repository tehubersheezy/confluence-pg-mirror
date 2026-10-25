"""Records, human review and export.

Review events are append-only and bound to an exact revision, its dependency hash, the
policy version and the source capture. A correction is a new immutable revision that
needs its own validation and approval. Export selects explicit approved revisions and
writes an evidence-bearing package atomically; it never writes to a target system.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from confluence_mirror import repository as repo
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.extraction.normalize import normalize
from confluence_mirror.extraction.registry import Registry
from confluence_mirror.extraction.validation import VALIDATOR_VERSION
from confluence_mirror.inspection import (
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfexError,
)
from confluence_mirror.structure.ids import digest, opaque_id

EXPORT_SCHEMA = "confex.export.v1"
SECURITY_CLASSIFICATION = (
    "internal: candidate documented data extracted from Confluence; not authoritative "
    "configuration items"
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def policy_version(registry: Registry, profile_ref: str, privacy_digest: str) -> str:
    profile = registry.profile(profile_ref)
    return digest([profile.ref, profile.digest, privacy_digest, VALIDATOR_VERSION])[:16]


# -- state ---------------------------------------------------------------------------------


def state(row: dict) -> dict[str, str]:
    """Independent validation, review and freshness states of a record's latest revision."""
    if row.get("current_version") is None:
        freshness = "unknown"
    elif row.get("is_deleted") or row["current_version"] != row["version_number"]:
        freshness = "stale"
    else:
        freshness = "current"
    decision = row.get("review_decision")
    if decision is None:
        review = "pending"
    elif row.get("reviewed_revision_id") != row["revision_id"]:
        # The latest event concerns an older revision; a correction points here.
        review = "pending" if decision == "corrected" else "superseded"
    elif decision == "approved" and row.get("reviewed_dependency_hash") != row["dependency_hash"]:
        review = "superseded"
    else:
        review = decision
    return {
        "validation": row.get("validation_status") or "not_run",
        "review": review,
        "freshness": freshness,
    }


def _identity(row: dict) -> dict:
    return {f["field"]: f["value"] for f in row["record"]["fields"]}


def list_records(
    conn, *, space=None, run=None, schema=None, page=None, review_state: str | None = None
) -> list[dict]:
    out = []
    for row in prepo.records(conn, space=space, run=run, schema=schema, page=page):
        st = state(row)
        if review_state and st["review"] != review_state:
            continue
        out.append(
            {
                "revision_id": row["revision_id"],
                "record_key": row["record_key"],
                "revision_number": row["revision_number"],
                "record_kind": row["record_kind"],
                "schema": row["schema_ref"],
                "space_key": row["space_key"],
                "page_id": row["page_id"],
                "table_id": row["table_id"],
                "values": {
                    f["field"]: f["value"]
                    for f in row["record"]["fields"]
                    if f["value_state"] == "present"
                },
                "issue_codes": sorted({i["code"] for i in row.get("issues") or []}),
                **st,
            }
        )
    return out


def _revision(conn, revision_id: str) -> dict:
    rev = prepo.get_revision(conn, revision_id)
    if rev is None:
        raise ConfexError("NOT_FOUND", f"revision {revision_id} does not exist", EXIT_NOT_FOUND)
    return rev


def _coord(table: dict, cell_id: str | None) -> str | None:
    for c in table["cells"]:
        if c["cell_id"] == cell_id and c.get("origin"):
            return f"R{c['origin']['row']}C{c['origin']['col']}"
    return None


def evidence_view(conn, revision_id: str) -> dict:
    rev = _revision(conn, revision_id)
    artifact = repo.load_artifact(conn, rev["artifact_id"])
    table = next(t for t in artifact["tables"] if t["table_id"] == rev["table_id"])
    items = []
    for f in rev["record"]["fields"]:
        for ev in f.get("evidence", []):
            entry = {"field": f["field"], **ev}
            block = artifact["blocks"].get(ev.get("block_id"))
            if block is not None:
                entry["block_text"] = block["evidence_text"]
            coord = _coord(table, ev.get("cell_id"))
            if coord:
                entry["coord"] = coord
                entry["open"] = f"confex tables show {rev['table_id']} --view cell --cell {coord}"
            elif ev.get("kind") in ("heading", "paragraph", "caption"):
                entry["open"] = f"confex tables show {rev['table_id']} --view context"
            items.append(entry)
    for rel in rev["record"].get("relationships", []):
        for ev in rel["evidence"]:
            items.append({"relationship": rel["predicate"], "target_key": rel["target_key"], **ev})
    return {
        "revision_id": revision_id,
        "source": {
            "page_id": rev["page_id"],
            "page_title": rev["page_title"],
            "space_key": rev["space_key"],
            "version_number": rev["version_number"],
            "snapshot_id": rev["snapshot_id"],
            "artifact_id": rev["artifact_id"],
            "table_id": rev["table_id"],
        },
        "evidence": items,
    }


def show(conn, revision_id: str) -> dict:
    rev = _revision(conn, revision_id)
    rows = [
        r for r in prepo.records(conn, page=rev["page_id"]) if r["record_key"] == rev["record_key"]
    ]
    latest = rows[0] if rows else None
    validation = prepo.latest_validation(conn, revision_id)
    st = (
        state(latest)
        if latest and latest["revision_id"] == revision_id
        else {
            "validation": validation["status"] if validation else "not_run",
            "review": "superseded",
            "freshness": "stale" if rev["current_version"] != rev["version_number"] else "current",
        }
    )
    return {
        "revision_id": revision_id,
        "record_key": rev["record_key"],
        "revision_number": rev["revision_number"],
        "origin": rev["origin"],
        "record_kind": rev["record_kind"],
        "schema": rev["schema_ref"],
        "profile": rev["profile"],
        "fields": rev["record"]["fields"],
        "relationships": rev["record"].get("relationships", []),
        "source_rows": rev["record"].get("source_rows"),
        "ambiguities": rev["record"].get("ambiguities", []),
        "validation": validation,
        "states": st,
        "history": prepo.revision_history(conn, rev["record_key"]),
        "review_events": prepo.review_events(conn, rev["record_key"]),
        "evidence": evidence_view(conn, revision_id)["evidence"],
        "next": [
            f"confex records evidence {revision_id}",
            f"confex review approve {revision_id} --reason TEXT",
            f"confex review reject {revision_id} --reason TEXT",
            f"confex review correct {revision_id} --patch FILE --reason TEXT",
        ],
    }


def diff(conn, revision_id: str, other: str | None) -> dict:
    rev = _revision(conn, revision_id)
    other_id = other or rev["parent_revision_id"]
    if other_id is None:
        raise ConfexError("NOT_FOUND", f"{revision_id} has no parent; pass --other", EXIT_NOT_FOUND)
    base = _revision(conn, other_id)
    a = {f["field"]: f for f in base["record"]["fields"]}
    b = {f["field"]: f for f in rev["record"]["fields"]}
    changes = []
    for name in sorted(set(a) | set(b)):
        fa, fb = a.get(name), b.get(name)
        if (
            fa is None
            or fb is None
            or (fa["value"], fa["value_state"], fa["method"])
            != (fb["value"], fb["value_state"], fb["method"])
        ):
            changes.append(
                {
                    "field": name,
                    "from": fa and {k: fa[k] for k in ("value", "value_state", "method")},
                    "to": fb and {k: fb[k] for k in ("value", "value_state", "method")},
                }
            )
    return {"from": other_id, "to": revision_id, "changes": changes}


# -- decisions --------------------------------------------------------------------------------


def _latest_for(conn, record_key: str) -> tuple[int, str | None]:
    return prepo.latest_revision_number(conn, record_key)


def decide(
    conn: psycopg.Connection,
    registry: Registry,
    privacy_digest: str,
    revision_id: str,
    decision: str,
    actor: str,
    reason: str,
    expect_sequence: int | None = None,
    corrected_revision_id: str | None = None,
) -> dict:
    if not reason.strip():
        raise ConfexError("MISSING_ARGUMENT", "--reason is required", EXIT_USAGE)
    rev = _revision(conn, revision_id)
    _, latest_id = _latest_for(conn, rev["record_key"])
    if latest_id != revision_id and decision != "corrected":
        raise ConfexError(
            "APPROVAL_STALE",
            f"{revision_id} is not the latest revision of its record ({latest_id} is)",
            EXIT_VALIDATION,
            {"latest_revision_id": latest_id},
        )
    validation = prepo.latest_validation(conn, revision_id)
    if decision == "approved":
        if validation is None or validation["status"] not in ("passed", "warnings"):
            status = validation["status"] if validation else "not_run"
            raise ConfexError(
                "EXPORT_NOT_ELIGIBLE",
                f"validation is {status}; resolve the issues (or correct the record) first",
                EXIT_VALIDATION,
                {"issues": [i["code"] for i in (validation or {}).get("issues", [])]},
            )
        if rev["current_version"] is not None and rev["current_version"] != rev["version_number"]:
            raise ConfexError(
                "APPROVAL_STALE",
                f"the page moved on to version {rev['current_version']}; re-extract first",
                EXIT_VALIDATION,
            )
    last = prepo.last_review_sequence(conn, rev["record_key"])
    if expect_sequence is not None and expect_sequence != last:
        raise ConfexError(
            "CONCURRENT_REVIEW",
            f"expected review sequence {expect_sequence}, found {last}",
            EXIT_USAGE,
        )
    try:
        with conn.transaction():
            event_id = prepo.insert_review_event(
                conn,
                {
                    "record_key": rev["record_key"],
                    "sequence": last + 1,
                    "revision_id": revision_id,
                    "decision": decision,
                    "corrected_revision_id": corrected_revision_id,
                    "actor": actor,
                    "reason": reason,
                    "dependency_hash": rev["dependency_hash"],
                    "policy_version": policy_version(registry, rev["profile"], privacy_digest),
                    "snapshot_id": rev["snapshot_id"],
                    "validation_id": validation["validation_id"] if validation else None,
                },
            )
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        raise ConfexError(
            "CONCURRENT_REVIEW",
            "another review event was recorded first; re-read and retry",
            EXIT_USAGE,
        ) from None
    conn.commit()
    return {
        "event_id": event_id,
        "revision_id": revision_id,
        "decision": decision,
        "sequence": last + 1,
        "actor": actor,
    }


def correct(
    conn, registry: Registry, revision_id: str, patch: dict, actor: str, reason: str
) -> dict:
    """A new revision from a reviewer patch; it must be validated and approved separately.

    Patch format::

        {"fields": {"<field>": {"value": "...", "evidence": [{"block_id": ..., "start": n,
                                                              "end": m}]}
                   "<field>": {"value": "...", "human_asserted": true}},
         "remove_fields": ["<field>"]}

    A changed value either cites evidence that actually contains it, or is recorded as a
    ``human_asserted`` fact with actor and reason; a quote that does not support the value
    is rejected.
    """
    if not reason.strip():
        raise ConfexError("MISSING_ARGUMENT", "--reason is required", EXIT_USAGE)
    rev = _revision(conn, revision_id)
    n, latest_id = _latest_for(conn, rev["record_key"])
    if latest_id != revision_id:
        raise ConfexError(
            "APPROVAL_STALE",
            f"{revision_id} is not the latest revision",
            EXIT_VALIDATION,
            {"latest_revision_id": latest_id},
        )
    profile = registry.profile(rev["profile"])
    artifact = repo.load_artifact(conn, rev["artifact_id"])
    fields = {f["field"]: dict(f) for f in rev["record"]["fields"]}
    now = datetime.now(UTC).isoformat()
    for name in patch.get("remove_fields", []):
        fields.pop(name, None)
    for name, change in (patch.get("fields") or {}).items():
        spec = profile.schema.fields.get(name)
        if spec is None:
            raise ConfexError(
                "SCHEMA_INVALID", f"{name} is not a field of {profile.schema.ref}", EXIT_USAGE
            )
        value = change.get("value")
        if change.get("human_asserted"):
            n_ = normalize(spec["type"], str(value), values=spec.get("values"))
            fields[name] = {
                "field": name,
                "value": [n_.value] if spec.get("multivalue") else n_.value,
                "raw_value": [str(value)] if spec.get("multivalue") else str(value),
                "value_state": "present",
                "method": "human_asserted",
                "normalization": [n_.rule] if n_.rule else None,
                "reason": reason,
                "assertion": {"actor": actor, "reason": reason, "asserted_at": now},
                "evidence": [{"kind": "human_assertion", "actor": actor, "reason": reason}],
            }
            continue
        evidence = []
        quotes = []
        for ref in change.get("evidence") or []:
            block = artifact["blocks"].get(ref.get("block_id"))
            if block is None:
                raise ConfexError(
                    "EVIDENCE_REFERENCE_INVALID",
                    f"block {ref.get('block_id')} is not in the source artifact",
                    EXIT_USAGE,
                )
            quote = block["evidence_text"][ref["start"] : ref["end"]]
            quotes.append(quote)
            evidence.append(
                {
                    "block_id": ref["block_id"],
                    "start": ref["start"],
                    "end": ref["end"],
                    "quote": quote,
                    "kind": "cell" if block.get("cell_id") else block["kind"],
                    "cell_id": block.get("cell_id"),
                    "snapshot_id": artifact["snapshot_id"],
                    "artifact_id": artifact["artifact_id"],
                    "table_id": rev["table_id"],
                    "resolution": "reviewer_cited",
                }
            )
        if not evidence:
            raise ConfexError(
                "MISSING_EVIDENCE",
                f'{name}: cite evidence or set "human_asserted": true',
                EXIT_USAGE,
            )
        values = value if isinstance(value, list) else [value]
        normalized = [normalize(spec["type"], q, values=spec.get("values")) for q in quotes]
        if [x.value for x in normalized] != [
            normalize(spec["type"], str(v), values=spec.get("values")).value for v in values
        ]:
            raise ConfexError(
                "EVIDENCE_QUOTE_MISMATCH",
                f"{name}: the cited text {quotes} does not support {values}",
                EXIT_USAGE,
            )
        fields[name] = {
            "field": name,
            "value": [x.value for x in normalized]
            if spec.get("multivalue")
            else normalized[0].value,
            "raw_value": quotes if spec.get("multivalue") else quotes[0],
            "value_state": "present",
            "method": "normalized" if any(x.rule for x in normalized) else "quoted",
            "normalization": [x.rule for x in normalized if x.rule] or None,
            "reason": reason,
            "evidence": evidence,
        }
    record = {**rev["record"], "fields": list(fields.values()), "unit_issues": []}
    content_hash = digest(
        {
            "record_kind": rev["record_kind"],
            "schema": rev["schema_ref"],
            "fields": record["fields"],
            "relationships": record.get("relationships", []),
        }
    )
    new_id = opaque_id("rev", rev["record_key"], content_hash, "correction", revision_id, now)
    with conn.transaction():
        prepo.insert_revision(
            conn,
            {
                "revision_id": new_id,
                "record_key": rev["record_key"],
                "parent_revision_id": revision_id,
                "revision_number": n + 1,
                "origin": "human_correction",
                "unit_id": rev["unit_id"],
                "attempt_id": rev["attempt_id"],
                "run_id": rev["run_id"],
                "page_id": rev["page_id"],
                "version_number": rev["version_number"],
                "space_id": rev["space_id"],
                "table_id": rev["table_id"],
                "artifact_id": rev["artifact_id"],
                "snapshot_id": rev["snapshot_id"],
                "schema_ref": rev["schema_ref"],
                "profile": rev["profile"],
                "record_kind": rev["record_kind"],
                "record": record,
                "content_hash": content_hash,
                "dependency_hash": digest({"parent": rev["dependency_hash"], "patch": patch}),
                "patch": patch,
                "created_by": actor,
                "reason": reason,
            },
        )
    conn.commit()
    return {"revision_id": new_id, "parent_revision_id": revision_id, "revision_number": n + 1}


# -- export ---------------------------------------------------------------------------------------


def eligibility(row: dict) -> str | None:
    """None if the record's latest revision may be exported, else the reason it may not."""
    st = state(row)
    if st["review"] != "approved":
        return f"review_{st['review']}"
    if st["validation"] not in ("passed", "warnings"):
        return f"validation_{st['validation']}"
    if st["freshness"] != "current":
        return f"source_{st['freshness']}"
    return None


def _export_record(row: dict) -> dict:
    fields = row["record"]["fields"]
    return {
        "revision_id": row["revision_id"],
        "record_key": row["record_key"],
        "revision_number": row["revision_number"],
        "schema": row["schema_ref"],
        "profile": row["profile"],
        "record_kind": row["record_kind"],
        "fields": [
            {
                k: f.get(k)
                for k in (
                    "field",
                    "value",
                    "raw_value",
                    "value_state",
                    "method",
                    "normalization",
                    "evidence",
                    "assertion",
                )
                if k in f
            }
            for f in fields
        ],
        "relationships": row["record"].get("relationships", []),
        "source": {
            "space_key": row["space_key"],
            "page_id": str(row["page_id"]),
            "page_title": row["page_title"],
            "version_number": row["version_number"],
            "snapshot_id": row["snapshot_id"],
            "table_id": row["table_id"],
            "source_rows": row["record"].get("source_rows"),
        },
        "validation": {
            "validation_id": row["validation_id"],
            "status": row["validation_status"],
            "issue_codes": sorted({i["code"] for i in row.get("issues") or []}),
        },
        "review": {
            "decision": row["review_decision"],
            "actor": row["reviewer"],
            "reviewed_at": row["reviewed_at"],
            "sequence": row["review_sequence"],
        },
        "origin": row["origin"],
        "candidate_entity_keys": {
            f["field"]: f["value"] for f in fields if f["value_state"] == "present"
        },
    }


def _csv_cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if value is None:
        text = ""
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text  # neutralize spreadsheet formula injection
    return text


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def export(
    conn,
    registry: Registry,
    *,
    space: str | None,
    run: str | None,
    schema: str | None,
    revision_ids: list[str] | None,
    approved_only: bool,
    export_format: str,
    output: Path,
    overwrite: bool,
    actor: str,
) -> dict:
    if not approved_only and not revision_ids:
        raise ConfexError(
            "ARGUMENT_CONFLICT",
            "pass --approved-only to select approved records in scope, or name revisions with "
            "--revision",
            EXIT_USAGE,
        )
    rows = prepo.records(conn, space=space, run=run, schema=schema)
    excluded: dict[str, int] = {}
    selected = []
    if revision_ids:
        by_id = {r["revision_id"]: r for r in rows}
        for rid in revision_ids:
            row = by_id.get(rid)
            if row is None:
                raise ConfexError(
                    "EXPORT_NOT_ELIGIBLE",
                    f"{rid} is not the latest revision of a record in scope",
                    EXIT_VALIDATION,
                )
            why = eligibility(row)
            if why:
                raise ConfexError(
                    "EXPORT_NOT_ELIGIBLE", f"{rid} cannot be exported: {why}", EXIT_VALIDATION
                )
            selected.append(row)
    else:
        for row in rows:
            why = eligibility(row)
            if why:
                excluded[why] = excluded.get(why, 0) + 1
            else:
                selected.append(row)
    if export_format == "csv":
        schemas = {r["schema_ref"] for r in selected}
        if len(schemas) > 1 or (not schema and selected):
            raise ConfexError(
                "ARGUMENT_CONFLICT",
                "CSV is a flat projection of one schema; pass --schema",
                EXIT_USAGE,
            )
    if output.exists() and not overwrite:
        raise ConfexError("OUTPUT_EXISTS", f"{output} exists; pass --overwrite", EXIT_USAGE)

    records = [_export_record(r) for r in selected]
    content_key = digest(
        {
            "revisions": sorted((r["revision_id"], r["content_hash"]) for r in selected),
            "format": export_format,
            "schema": EXPORT_SCHEMA,
        }
    )
    created = datetime.now(UTC)
    export_id = opaque_id("exp", content_key, created.isoformat())
    files: dict[str, bytes] = {}
    if export_format == "csv":
        fields = (
            sorted(registry.schema(next(iter({r["schema_ref"] for r in selected}))).fields)
            if selected
            else []
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["revision_id", "record_key", "page_id", "snapshot_id", "table_id", *fields]
        )
        for rec in records:
            vals = {f["field"]: f["value"] for f in rec["fields"]}
            writer.writerow(
                [
                    _csv_cell(rec["revision_id"]),
                    _csv_cell(rec["record_key"]),
                    _csv_cell(rec["source"]["page_id"]),
                    _csv_cell(rec["source"]["snapshot_id"]),
                    _csv_cell(rec["source"]["table_id"]),
                    *[_csv_cell(vals.get(f)) for f in fields],
                ]
            )
        files[output.name] = buf.getvalue().encode("utf-8")
    else:
        if export_format == "jsonl":
            body = "".join(
                json.dumps(r, default=_json_default, ensure_ascii=False) + "\n" for r in records
            )
            files["records.jsonl"] = body.encode("utf-8")
        else:
            files["records.json"] = json.dumps(
                records, default=_json_default, indent=2, ensure_ascii=False
            ).encode("utf-8")
        evidence_index = {
            r["revision_id"]: [
                {"field": f["field"], **e} for f in r["fields"] for e in f.get("evidence") or []
            ]
            for r in records
        }
        files["evidence_index.json"] = json.dumps(
            evidence_index, indent=2, default=_json_default
        ).encode("utf-8")
        files["validation_summary.json"] = json.dumps(
            {r["revision_id"]: r["validation"] for r in records}, indent=2
        ).encode("utf-8")
        files["review_summary.json"] = json.dumps(
            {r["revision_id"]: r["review"] for r in records}, indent=2, default=_json_default
        ).encode("utf-8")
    manifest = {
        "schema_version": EXPORT_SCHEMA,
        "export_id": export_id,
        "created_at": created.isoformat(),
        "created_by": actor,
        "scope": {
            "space": space,
            "run": run,
            "schema": schema,
            "revisions": revision_ids,
            "approved_only": approved_only,
        },
        "export_format": export_format,
        "record_count": len(records),
        "excluded": excluded,
        "schemas": sorted({r["schema"] for r in records}),
        "selected_revisions": [
            {"revision_id": r["revision_id"], "record_key": r["record_key"]} for r in records
        ],
        "content_key": content_key,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "security_classification": SECURITY_CLASSIFICATION,
        "lineage": "record -> field evidence -> table cell/heading -> snapshot -> page",
        "completeness": {
            "state": "complete" if not excluded else "partial",
            "note": "only approved, validated, current revisions are included",
        },
    }
    if export_format == "csv":
        files[output.name + ".manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
        _write_files(output.parent, files, overwrite)
    else:
        files["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
        _write_dir(output, files, overwrite)
    with conn.transaction():
        prepo.insert_export(
            conn,
            {
                "export_id": export_id,
                "actor": actor,
                "scope": manifest["scope"],
                "export_format": export_format,
                "projection": "flat:" + (schema or "") if export_format == "csv" else "full",
                "path": str(output),
                "content_key": content_key,
                "record_count": len(records),
                "excluded": excluded,
                "manifest": manifest,
            },
            [r["revision_id"] for r in records],
        )
    conn.commit()
    return {
        "export_id": export_id,
        "path": str(output),
        "record_count": len(records),
        "excluded": excluded,
        "content_key": content_key,
        "files": manifest["files"],
    }


def _write_dir(target: Path, files: dict[str, bytes], overwrite: bool) -> None:
    """Write into an exclusive temporary directory, fsync, then rename into place."""
    target = target.resolve()
    tmp = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        os.chmod(tmp, 0o700)
        for name, data in files.items():
            path = tmp / name
            with open(path, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(path, 0o600)
        if target.exists():
            if not overwrite:
                raise ConfexError("OUTPUT_EXISTS", f"{target} exists", EXIT_USAGE)
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        os.replace(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _write_files(directory: Path, files: dict[str, bytes], overwrite: bool) -> None:
    written = []
    try:
        for name, data in files.items():
            path = directory / name
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            if path.exists() and not overwrite:
                Path(tmp).unlink()
                raise ConfexError("OUTPUT_EXISTS", f"{path} exists", EXIT_USAGE)
            os.replace(tmp, path)
            written.append(path)
    except BaseException:
        if not overwrite:
            for p in written:
                p.unlink(missing_ok=True)
        raise
