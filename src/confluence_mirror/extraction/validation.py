"""Deterministic validation of extraction results and candidate revisions (pure).

Layers, in order: result/schema conformance, structure, evidence (exact quotes at exact
offsets), derivation (normalization re-run, inheritance permitted), coverage (every
selected row accounted for once), domain (required identity, parse failures), and
cross-record (duplicates, conflicting values). A model-generated confidence is never a
gate; issue codes are. Validation success is not approval.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from confluence_mirror.extraction.normalize import normalize
from confluence_mirror.extraction.payload import DISPOSITIONS, VALUE_STATES
from confluence_mirror.extraction.registry import Profile
from confluence_mirror.structure.ids import digest, opaque_id

VALIDATOR_VERSION = "validate.1"
REPAIRABLE = {
    "SCHEMA_INVALID",
    "EVIDENCE_QUOTE_MISMATCH",
    "EVIDENCE_REFERENCE_INVALID",
    "COVERAGE_GAP",
    "MISSING_EVIDENCE",
}
_INHERIT_KINDS = {"heading": {"heading"}, "heading_text": {"heading"}, "parent_row": {"cell"}}


def issue(
    code: str, severity: str, message: str, remediation: str | None = None, **ref: Any
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "stage": "validate",
        "message": message,
        "ref": ref or None,
        "remediation": remediation,
    }


def validate_result(result: dict, unit: dict, profile: Profile) -> list[dict]:
    """Unit-level checks on the raw result: schema conformance and row coverage."""
    issues = []
    schema = profile.schema
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        return [issue("SCHEMA_INVALID", "blocker", "result has no records array")]
    local_ids = set()
    for rec in result["records"]:
        if rec.get("record_kind") not in schema.data["record_kinds"]:
            issues.append(
                issue(
                    "SCHEMA_INVALID", "blocker", f"unknown record kind {rec.get('record_kind')!r}"
                )
            )
        if rec.get("local_id") in local_ids:
            issues.append(
                issue("SCHEMA_INVALID", "blocker", f"duplicate local_id {rec.get('local_id')!r}")
            )
        local_ids.add(rec.get("local_id"))
        for f in rec.get("fields", []):
            if f.get("field") not in schema.fields:
                issues.append(
                    issue(
                        "SCHEMA_INVALID",
                        "blocker",
                        f"unknown field {f.get('field')!r}",
                        local_id=rec["local_id"],
                    )
                )
            if f.get("value_state") not in VALUE_STATES:
                issues.append(
                    issue(
                        "SCHEMA_INVALID", "blocker", f"unknown value_state {f.get('value_state')!r}"
                    )
                )
        for r in rec.get("source_rows", []):
            if r not in unit["selected_rows"]:
                issues.append(
                    issue(
                        "EVIDENCE_REFERENCE_INVALID",
                        "blocker",
                        f"record {rec['local_id']} cites row {r} outside the unit",
                    )
                )
    for rel in result.get("relationships", []):
        if rel.get("predicate") not in profile.get("relationships", {}):
            issues.append(
                issue(
                    "SCHEMA_INVALID",
                    "blocker",
                    f"relationship predicate {rel.get('predicate')!r} not allowed",
                )
            )
        if rel.get("source_local_id") not in local_ids:
            issues.append(
                issue(
                    "SCHEMA_INVALID",
                    "blocker",
                    f"relationship source {rel.get('source_local_id')!r} unknown",
                )
            )

    seen: dict[int, int] = defaultdict(int)
    for cov in result.get("row_coverage", []):
        seen[cov.get("row")] += 1
        if cov.get("disposition") not in DISPOSITIONS:
            issues.append(
                issue("SCHEMA_INVALID", "blocker", f"row {cov.get('row')}: unknown disposition")
            )
        if cov.get("row") not in unit["selected_rows"]:
            issues.append(
                issue(
                    "COVERAGE_GAP",
                    "blocker",
                    f"coverage names row {cov.get('row')}, which is not selected",
                )
            )
        for lid in cov.get("local_ids", []):
            if lid not in local_ids:
                issues.append(
                    issue(
                        "COVERAGE_GAP",
                        "blocker",
                        f"row {cov.get('row')} points at unknown record {lid}",
                    )
                )
        if cov.get("disposition") == "record" and not cov.get("local_ids"):
            issues.append(
                issue(
                    "COVERAGE_GAP",
                    "blocker",
                    f"row {cov.get('row')} is marked record but names none",
                )
            )
    for r in unit["selected_rows"]:
        if seen.get(r, 0) == 0:
            issues.append(
                issue(
                    "COVERAGE_GAP",
                    "blocker",
                    f"selected row {r} is unaccounted for",
                    "re-run extraction; every row needs a disposition",
                    row=r,
                )
            )
        elif seen[r] > 1:
            issues.append(
                issue("COVERAGE_GAP", "blocker", f"row {r} has {seen[r]} coverage entries", row=r)
            )
    covered_records = {
        lid for c in result.get("row_coverage", []) for lid in c.get("local_ids", [])
    }
    for lid in local_ids - covered_records:
        issues.append(
            issue("COVERAGE_GAP", "blocker", f"record {lid} is not tied to any covered row")
        )
    return issues


def _allowed_blocks(artifact: dict, table: dict, unit: dict) -> set[str]:
    blocks = set()
    cells = set(unit["cell_ids"])
    pc = unit.get("parent_context")
    if pc:
        cells |= {c["cell_id"] for c in pc["cells"]}
    for bid, b in artifact["blocks"].items():
        if b.get("cell_id") in cells:
            blocks.add(bid)
    heading_block = {
        n["id"]: n.get("block_id") for n in artifact["nodes"] if n["kind"] == "heading"
    }
    for h in table["heading_path"]:
        if heading_block.get(h["id"]):
            blocks.add(heading_block[h["id"]])
    ctx = table["context"]
    for ref in (
        ctx.get("caption_block_id"),
        (ctx.get("preceding") or {}).get("block_id"),
        (ctx.get("following") or {}).get("block_id"),
    ):
        if ref:
            blocks.add(ref)
    return blocks


def validate_revision(
    revision: dict,
    *,
    artifact: dict,
    table: dict,
    unit: dict | None,
    profile: Profile,
    unit_issues: list[dict] = (),
    current_version: int | None = None,
) -> dict[str, Any]:
    """Validation result for one revision: status, issues and quality metrics."""
    record = revision["record"]
    schema = profile.schema
    issues: list[dict] = list(unit_issues)
    allowed = _allowed_blocks(artifact, table, unit) if unit else None
    inherit_rules = {r["field"]: r for r in profile.get("context_inheritance", [])}
    identity = schema.data.get("identity_fields", [])
    blocking = set(profile.get("review", {}).get("export_blocking_issues", []))
    supported = 0
    populated = 0

    if table["geometry_status"] != "valid":
        issues.append(issue("INVALID_GEOMETRY", "blocker", "source table geometry is not valid"))
    by_field = {f["field"]: f for f in record["fields"]}
    for fname, spec in schema.fields.items():
        if spec.get("required") and by_field.get(fname, {}).get("value_state") != "present":
            state = by_field.get(fname, {}).get("value_state", "absent")
            sev = "blocker" if fname in identity else "error"
            code = "AMBIGUOUS_IDENTITY" if state == "ambiguous" else "MISSING_REQUIRED_FIELD"
            issues.append(issue(code, sev, f"required field {fname} is {state}", field=fname))

    for f in record["fields"]:
        fname = f["field"]
        spec = schema.fields.get(fname)
        if spec is None:
            issues.append(issue("SCHEMA_INVALID", "blocker", f"unknown field {fname}"))
            continue
        values = (
            f["value"]
            if isinstance(f["value"], list)
            else ([f["value"]] if f["value"] is not None else [])
        )
        raws = (
            f["raw_value"]
            if isinstance(f["raw_value"], list)
            else ([f["raw_value"]] if f["raw_value"] is not None else [])
        )
        if f["value_state"] == "present" and values:
            populated += 1
        if f["method"] == "human_asserted":
            if not f.get("assertion"):
                issues.append(
                    issue(
                        "HUMAN_ASSERTION_INVALID",
                        "blocker",
                        f"{fname}: human assertion lacks actor/reason",
                        field=fname,
                    )
                )
            continue
        if f["value_state"] == "present" and values and not f["evidence"]:
            issues.append(
                issue(
                    "MISSING_EVIDENCE",
                    "blocker",
                    f"{fname} has no evidence",
                    "every populated value needs a quoted span",
                    field=fname,
                )
            )
        ok = True
        for ev in f["evidence"]:
            ok &= _check_evidence(ev, artifact, allowed, issues, fname)
        if f["value_state"] == "present" and ok and f["evidence"]:
            quotes = [ev.get("quote") for ev in f["evidence"]]
            if len(raws) == len(quotes) and raws != quotes:
                issues.append(
                    issue(
                        "DERIVATION_MISMATCH",
                        "blocker",
                        f"{fname}: raw value does not equal its quoted span",
                        field=fname,
                    )
                )
                ok = False
            for raw, value in zip(raws, values, strict=False):
                n = normalize(
                    spec["type"],
                    raw,
                    values=spec.get("values"),
                    value_map=(profile.get("value_maps") or {}).get(fname),
                )
                if n.value != value:
                    issues.append(
                        issue(
                            "DERIVATION_MISMATCH",
                            "blocker",
                            f"{fname}: {value!r} is not the registered normalization of {raw!r}",
                            field=fname,
                        )
                    )
                    ok = False
            if ok:
                supported += 1
        if f["method"] == "inherited_context":
            rule = inherit_rules.get(fname)
            kinds = {ev.get("kind") for ev in f["evidence"]}
            if rule is None or not kinds <= _INHERIT_KINDS.get(rule["from"], set()):
                issues.append(
                    issue(
                        "INHERITANCE_NOT_PERMITTED",
                        "blocker",
                        f"{fname}: context inheritance is not permitted here",
                        field=fname,
                    )
                )
        if f["method"] == "model_inferred":
            sev = "blocker" if "model_inferred" in blocking else "warning"
            issues.append(
                issue(
                    "MODEL_INFERRED",
                    sev,
                    f"{fname} was inferred, not stated; review required",
                    field=fname,
                )
            )
        if f["value_state"] == "unparsed":
            sev = "blocker" if fname in identity else "warning"
            issues.append(
                issue(
                    "NORMALIZATION_UNSUPPORTED",
                    sev,
                    f"{fname}: {raws} does not parse as {spec['type']}",
                    field=fname,
                )
            )
        if f["value_state"] == "ambiguous" and fname not in identity:
            issues.append(
                issue(
                    "VALUE_AMBIGUOUS",
                    "warning",
                    f"{fname} is ambiguous: {f.get('reason') or 'see ambiguities'}",
                    field=fname,
                )
            )
        if f["value_state"] == "redacted":
            issues.append(
                issue(
                    "VALUE_REDACTED",
                    "warning",
                    f"{fname} touches redacted text and cannot be resolved",
                    field=fname,
                )
            )

    for rel in record.get("relationships", []):
        if rel["predicate"] not in profile.get("relationships", {}):
            issues.append(
                issue(
                    "SCHEMA_INVALID", "blocker", f"relationship {rel['predicate']} is not allowed"
                )
            )
        if not rel["evidence"]:
            issues.append(
                issue(
                    "MISSING_EVIDENCE",
                    "blocker",
                    f"relationship {rel['predicate']} has no directional evidence",
                )
            )
        for ev in rel["evidence"]:
            _check_evidence(ev, artifact, allowed, issues, rel["predicate"])

    if current_version is not None and current_version != revision["version_number"]:
        issues.append(
            issue(
                "STALE_SOURCE",
                "warning",
                f"extracted from version {revision['version_number']}; the page is "
                f"now at version {current_version}",
            )
        )

    status = _status(issues)
    metrics = {
        "populated_fields": populated,
        "evidence_supported_fields": supported,
        "issues": len(issues),
        "blockers": sum(1 for i in issues if i["severity"] == "blocker"),
    }
    out = {
        "revision_id": revision["revision_id"],
        "validator_version": VALIDATOR_VERSION,
        "status": status,
        "issues": issues,
        "metrics": metrics,
    }
    out["validation_id"] = opaque_id("val", digest(out))
    return out


def _check_evidence(
    ev: dict, artifact: dict, allowed: set[str] | None, issues: list, where: str
) -> bool:
    block = artifact["blocks"].get(ev.get("block_id"))
    if block is None:
        issues.append(
            issue(
                "EVIDENCE_REFERENCE_INVALID",
                "blocker",
                f"{where}: block {ev.get('block_id')} does not exist",
                block_id=ev.get("block_id"),
            )
        )
        return False
    if allowed is not None and ev["block_id"] not in allowed:
        issues.append(
            issue(
                "EVIDENCE_REFERENCE_INVALID",
                "blocker",
                f"{where}: block {ev['block_id']} is outside the supplied evidence",
                block_id=ev["block_id"],
            )
        )
        return False
    if ev.get("resolution") == "redacted_span":
        return False
    text = block["evidence_text"]
    start, end = ev.get("start"), ev.get("end")
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text)):
        issues.append(
            issue(
                "EVIDENCE_QUOTE_MISMATCH",
                "blocker",
                f"{where}: offsets [{start}, {end}) are out of range",
                block_id=ev["block_id"],
            )
        )
        return False
    if text[start:end] != ev.get("quote"):
        issues.append(
            issue(
                "EVIDENCE_QUOTE_MISMATCH",
                "blocker",
                f"{where}: quote does not match the text at [{start}, {end})",
                "re-run with repair; the model must copy text exactly",
                block_id=ev["block_id"],
            )
        )
        return False
    return True


def _status(issues: list[dict]) -> str:
    sev = {i["severity"] for i in issues}
    if "blocker" in sev:
        return "blocked"
    if "error" in sev:
        return "failed"
    if "warning" in sev:
        return "warnings"
    return "passed"


def cross_record(
    revisions: list[dict], profile_of: dict[str, Profile]
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Duplicate and conflicting candidates; returns (issues per revision, match proposals)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for rev in revisions:
        profile = profile_of[rev["revision_id"]]
        identity = profile.schema.data.get("identity_fields", [])
        fields = {f["field"]: f for f in rev["record"]["fields"]}
        key = tuple(
            (k, str(fields.get(k, {}).get("value")).lower() if fields.get(k) else None)
            for k in identity
        )
        if any(v is None or v == "none" for _, v in key):
            continue
        # Scope: same schema and same space; names are never merged across spaces.
        groups[(rev["schema_ref"], rev.get("space_id"), key)].append(rev)
    per_rev: dict[str, list[dict]] = defaultdict(list)
    proposals = []
    for (schema_ref, space_id, key), revs in groups.items():
        if len(revs) < 2:
            continue
        values = defaultdict(set)
        for rev in revs:
            for f in rev["record"]["fields"]:
                if f["value_state"] == "present":
                    values[f["field"]].add(str(f["value"]))
        conflicts = sorted(k for k, v in values.items() if len(v) > 1)
        state = "conflicting" if conflicts else "proposed"
        for rev in revs:
            per_rev[rev["revision_id"]].append(
                issue(
                    "ENTITY_MATCH_AMBIGUOUS" if conflicts else "DUPLICATE_CANDIDATE",
                    "warning",
                    f"{len(revs)} candidates share identity {dict(key)}"
                    + (f"; conflicting {', '.join(conflicts)}" if conflicts else ""),
                )
            )
        proposal = {
            "schema_ref": schema_ref,
            "record_kind": revs[0]["record_kind"],
            "match_key": dict(key),
            "scope": {"space_id": space_id},
            "record_keys": sorted({r["record_key"] for r in revs}),
            "state": state,
            "evidence": {"conflicting_fields": conflicts},
        }
        proposal["match_id"] = opaque_id("match", digest(proposal))
        proposals.append(proposal)
    return per_rev, proposals


def evidence_problems(
    candidates: list[dict], artifact: dict, table: dict, unit: dict
) -> list[dict]:
    """Quote and reference checks over freshly built candidates (before they are stored)."""
    allowed = _allowed_blocks(artifact, table, unit)
    issues: list[dict] = []
    for cand in candidates:
        for f in cand["fields"]:
            if f["method"] == "human_asserted":
                continue
            if f["value_state"] == "present" and f["value"] not in (None, []) and not f["evidence"]:
                issues.append(
                    issue(
                        "MISSING_EVIDENCE",
                        "blocker",
                        f"{cand['local_id']}.{f['field']} has no evidence",
                    )
                )
            for ev in f["evidence"]:
                _check_evidence(ev, artifact, allowed, issues, f"{cand['local_id']}.{f['field']}")
    return issues
