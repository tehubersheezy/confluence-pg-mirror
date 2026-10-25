"""Turn an extraction result (rules or model) into candidate record revisions (pure).

Code, not the model, performs normalization: every value is re-derived from its quoted
span with a named rule. Evidence references are enriched with their source coordinates
(snapshot, artifact, table, cell or heading) and, for model output, mapped back from
redacted to original offsets. A reference that cannot be mapped is kept and flagged; it
is never silently re-anchored.
"""

from __future__ import annotations

from typing import Any

from confluence_mirror.extraction.normalize import NORMALIZER_VERSION, normalize
from confluence_mirror.extraction.privacy import map_back
from confluence_mirror.extraction.registry import Profile
from confluence_mirror.structure.ids import digest, opaque_id

_BLOCK_KIND = {
    "cell_text": "cell",
    "heading": "heading",
    "caption": "caption",
    "paragraph": "paragraph",
    "list_item": "paragraph",
    "code": "paragraph",
    "quote": "paragraph",
}


def enrich_evidence(ref: dict, artifact: dict, table: dict, redaction_map: dict) -> dict:
    out = dict(ref)
    block = artifact["blocks"].get(ref.get("block_id"))
    out["source_instance_id"] = artifact.get("instance_id")
    out["snapshot_id"] = artifact["snapshot_id"]
    out["artifact_id"] = artifact["artifact_id"]
    out["table_id"] = table["table_id"]
    if block is None:
        out["kind"] = "unknown"
        out["resolution"] = "unknown_block"
        return out
    out["kind"] = _BLOCK_KIND.get(block["kind"], "paragraph")
    if block.get("cell_id"):
        out["cell_id"] = block["cell_id"]
    if block.get("section_id"):
        out["section_id"] = block["section_id"]
    mappings = redaction_map.get(ref["block_id"])
    if mappings:
        mapped = map_back(ref["start"], ref["end"], mappings)
        if mapped is None:
            out["resolution"] = "redacted_span"
            return out
        out["redacted_start"], out["redacted_end"] = ref["start"], ref["end"]
        out["start"], out["end"] = mapped
        out["resolution"] = "mapped_from_redacted"
    else:
        out["resolution"] = "direct"
    return out


def _normalize_field(assertion: dict, profile: Profile) -> dict:
    spec = profile.schema.fields.get(assertion["field"], {"type": "string"})
    value_map = (profile.get("value_maps") or {}).get(assertion["field"])
    values, rules, states = [], [], []
    for raw in assertion["values"]:
        n = normalize(spec["type"], raw, values=spec.get("values"), value_map=value_map)
        values.append(n.value)
        rules.append(n.rule)
        states.append(n.state)
    state = assertion["value_state"]
    issue_states = [s for s in states if s != "present"]
    if state == "present" and issue_states:
        state = "ambiguous" if "ambiguous" in issue_states else "unparsed"
    method = assertion["method"]
    if method == "quoted" and any(rules):
        method = "normalized"
    multivalue = bool(spec.get("multivalue"))
    return {
        "field": assertion["field"],
        "value": values if multivalue else (values[0] if values else None),
        "raw_value": assertion["values"]
        if multivalue
        else (assertion["values"][0] if assertion["values"] else None),
        "value_state": state if values or state != "present" else "absent",
        "method": method,
        "normalization": [r for r in rules if r] or None,
        "normalizer_version": NORMALIZER_VERSION,
        "reason": assertion.get("reason") or None,
    }


def build_candidates(
    result: dict,
    *,
    artifact: dict,
    table: dict,
    unit: dict,
    profile: Profile,
    origin: str,
    attempt_id: str,
    redaction_map: dict | None = None,
) -> list[dict[str, Any]]:
    redaction_map = redaction_map or {}
    schema = profile.schema
    identity = schema.data.get("identity_fields", [])
    rels_by_source: dict[str, list[dict]] = {}
    for rel in result.get("relationships", []):
        rels_by_source.setdefault(rel["source_local_id"], []).append(rel)
    out = []
    for rec in result.get("records", []):
        fields = []
        for a in rec["fields"]:
            f = _normalize_field(a, profile)
            f["evidence"] = [
                enrich_evidence(e, artifact, table, redaction_map) for e in a["evidence"]
            ]
            if any(e.get("resolution") == "redacted_span" for e in f["evidence"]):
                f["value_state"] = "redacted"
                f["value"] = [] if isinstance(f["value"], list) else None
            fields.append(f)
        by_field = {f["field"]: f for f in fields}
        key_values = [by_field.get(k, {}).get("raw_value") for k in identity]
        record_key = digest(
            {
                "table_id": table["table_id"],
                "record_kind": rec["record_kind"],
                "source_rows": sorted(rec["source_rows"]),
                "identity": key_values,
            }
        )[:24]
        relationships = [
            {
                "predicate": rel["predicate"],
                "target_local_id": rel.get("target_local_id") or None,
                "target_key": rel.get("target_key") or None,
                "evidence": [
                    enrich_evidence(e, artifact, table, redaction_map) for e in rel["evidence"]
                ],
            }
            for rel in rels_by_source.get(rec["local_id"], [])
        ]
        candidate = {
            "record_key": f"key_{record_key}",
            "local_id": rec["local_id"],
            "record_kind": rec["record_kind"],
            "schema": schema.ref,
            "profile": profile.ref,
            "source_rows": rec["source_rows"],
            "fields": fields,
            "relationships": relationships,
            "origin": origin,
            "unit_id": unit["unit_id"],
            "attempt_id": attempt_id,
            "table_id": table["table_id"],
            "artifact_id": artifact["artifact_id"],
            "snapshot_id": artifact["snapshot_id"],
            "page_id": artifact["page_id"],
            "version_number": artifact["version_number"],
        }
        candidate["content_hash"] = digest(
            {k: candidate[k] for k in ("record_kind", "schema", "fields", "relationships")}
        )
        candidate["revision_id"] = opaque_id(
            "rev", candidate["record_key"], candidate["content_hash"], attempt_id
        )
        out.append(candidate)
    return out
