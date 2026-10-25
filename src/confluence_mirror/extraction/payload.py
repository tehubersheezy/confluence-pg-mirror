"""The one canonical serializer for extraction payloads and model requests (pure).

``tables show --view payload`` and ``extract`` both call :func:`build_payload` and
:func:`build_request`, so a preview and an execution of the same unit produce the same
bytes and the same hashes:

- ``payload_hash``: the canonical, redacted evidence object;
- ``request_hash``: the complete application request body (no credentials, no transport IDs);
- ``dependency_hash``: every source, policy, profile and model input.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from confluence_mirror.extraction.classify import Layout
from confluence_mirror.extraction.privacy import PrivacyPolicy, redact
from confluence_mirror.extraction.registry import FAMILIES, Profile, Registry
from confluence_mirror.structure.ids import canonical_json, digest

PROMPT_VERSION = "prompt.1"
VALUE_STATES = [
    "present",
    "absent",
    "unknown",
    "not_applicable",
    "redacted",
    "ambiguous",
    "unparsed",
]
DISPOSITIONS = [
    "record",
    "header",
    "repeated_header",
    "section_divider",
    "total",
    "blank",
    "delegated_child",
    "skipped",
    "ambiguous",
]
EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "block_id": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "quote": {"type": "string"},
    },
    "required": ["block_id", "start", "end", "quote"],
    "additionalProperties": False,
}


def prompt(name: str) -> str:
    return (resources.files("confluence_mirror.extraction") / "prompts" / name).read_text("utf-8")


def _coord(cell: dict) -> tuple[str, str]:
    o = cell["origin"]
    end = f"R{o['row'] + cell['rowspan'] - 1}C{o['col'] + cell['colspan'] - 1}"
    return f"R{o['row']}C{o['col']}", f"R{o['row']}C{o['col']}:{end}"


class _Redactor:
    def __init__(self, policy: PrivacyPolicy) -> None:
        self.rules = policy.rules
        self.counter: dict[str, int] = {}
        self.maps: dict[str, list[dict]] = {}

    def block(self, block_id: str, text: str) -> dict:
        redacted, mappings = redact(text, self.rules, self.counter)
        if mappings:
            self.maps[block_id] = mappings
        return {"block_id": block_id, "text": redacted}


def build_payload(
    artifact: dict,
    table: dict,
    unit: dict,
    classification: dict,
    policy: PrivacyPolicy,
    page_meta: dict,
) -> tuple[dict, dict]:
    """Return ``(evidence, redaction_map)``. The map stays local; it is never sent."""
    red = _Redactor(policy)
    blocks = artifact["blocks"]
    layout = Layout(table)
    roles = {r["row"]: r["role"] for r in classification.get("row_roles", [])}
    header_rows = set(unit["header_rows"])
    heading_block = {
        n["id"]: n.get("block_id") for n in artifact["nodes"] if n["kind"] == "heading"
    }

    context_blocks = []
    for h in table["heading_path"]:
        bid = heading_block.get(h["id"])
        if bid:
            context_blocks.append(
                {
                    "kind": "heading",
                    "level": h["level"],
                    **red.block(bid, blocks[bid]["evidence_text"]),
                }
            )
    ctx = table["context"]
    for kind, ref in (
        ("caption", ctx.get("caption_block_id")),
        ("preceding_paragraph", (ctx.get("preceding") or {}).get("block_id")),
        ("following_paragraph", (ctx.get("following") or {}).get("block_id")),
    ):
        if ref:
            context_blocks.append({"kind": kind, **red.block(ref, blocks[ref]["evidence_text"])})

    cells = []
    for cid in unit["cell_ids"]:
        cell = layout.cells[cid]
        coord, span = _coord(cell)
        row = cell["origin"]["row"]
        cells.append(
            {
                "cell_id": cid,
                "coord": coord,
                "span": span,
                "tag": cell["tag"],
                "row_role": "header" if row in header_rows else roles.get(row, "data"),
                "blocks": [
                    red.block(x["block_id"], blocks[x["block_id"]]["evidence_text"])
                    for x in cell.get("content", [])
                    if x["kind"] == "text"
                ],
                "nested_tables": [
                    x["table_id"] for x in cell.get("content", []) if x["kind"] == "table"
                ],
            }
        )

    parent = None
    pc = unit.get("parent_context")
    if pc:
        ptable = next(t for t in artifact["tables"] if t["table_id"] == pc["table_id"])
        pcells = {c["cell_id"]: c for c in ptable["cells"]}
        parent = {
            "table_id": pc["table_id"],
            "row": pc["row"],
            "cells": [
                {
                    "cell_id": c["cell_id"],
                    "header": c["header_text"],
                    "blocks": [
                        red.block(x["block_id"], blocks[x["block_id"]]["evidence_text"])
                        for x in pcells[c["cell_id"]].get("content", [])
                        if x["kind"] == "text"
                    ],
                }
                for c in pc["cells"]
            ],
        }

    evidence = {
        "source": {
            "instance_id": artifact.get("instance_id"),
            "page_id": str(artifact["page_id"]),
            "version": artifact["version_number"],
            "snapshot_id": artifact["snapshot_id"],
            "representation": artifact["representation"],
            "artifact_id": artifact["artifact_id"],
            "table_id": table["table_id"],
        },
        "page": {"title": page_meta.get("title"), "space_key": page_meta.get("space_key")},
        "context": context_blocks,
        "parent_context": parent,
        "table": {
            "rows": table["row_count"],
            "columns": table["column_count"],
            "orientation": classification.get("orientation"),
            "header_rows": unit["header_rows"],
            "chunk": f"{unit['chunk_index']}/{unit['chunk_count']}",
        },
        "columns": [
            {"col": b["col"], "field": b["field"], "header_text": b["header_text"]}
            for b in classification.get("column_bindings", [])
        ],
        "cells": cells,
        "selected_rows": unit["selected_rows"],
        "delegated_tables": unit["delegated_tables"],
    }
    return evidence, red.maps


def result_schema(profile: Profile) -> dict:
    schema = profile.schema
    predicates = sorted(profile.get("relationships", {}))
    field_schema = {
        "type": "object",
        "properties": {
            "field": {"type": "string", "enum": sorted(schema.fields)},
            "values": {"type": "array", "items": {"type": "string"}},
            "value_state": {"type": "string", "enum": VALUE_STATES},
            "method": {"type": "string", "enum": ["quoted", "inherited_context", "model_inferred"]},
            "evidence": {"type": "array", "items": EVIDENCE_SCHEMA},
            "reason": {"type": "string"},
        },
        "required": ["field", "values", "value_state", "method", "evidence", "reason"],
        "additionalProperties": False,
    }
    properties: dict[str, Any] = {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "local_id": {"type": "string"},
                    "record_kind": {"type": "string", "enum": schema.data["record_kinds"]},
                    "source_rows": {"type": "array", "items": {"type": "integer"}},
                    "fields": {"type": "array", "items": field_schema},
                },
                "required": ["local_id", "record_kind", "source_rows", "fields"],
                "additionalProperties": False,
            },
        },
        "row_coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row": {"type": "integer"},
                    "disposition": {"type": "string", "enum": DISPOSITIONS},
                    "local_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["row", "disposition", "local_ids", "reason"],
                "additionalProperties": False,
            },
        },
        "ambiguities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "cell_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["code", "message", "cell_ids"],
                "additionalProperties": False,
            },
        },
    }
    required = ["records", "row_coverage", "ambiguities"]
    if predicates:
        properties["relationships"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "predicate": {"type": "string", "enum": predicates},
                    "source_local_id": {"type": "string"},
                    "target_local_id": {"type": "string"},
                    "target_key": {"type": "string"},
                    "evidence": {"type": "array", "items": EVIDENCE_SCHEMA},
                },
                "required": [
                    "predicate",
                    "source_local_id",
                    "target_local_id",
                    "target_key",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
        required.append("relationships")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_request(evidence: dict, profile: Profile, model: dict) -> dict:
    """The full Messages API request body for one extraction unit."""
    schema = profile.schema
    instructions = prompt("extract_instructions.txt").format(
        schema_ref=schema.ref,
        profile_ref=profile.ref,
        fields=canonical_json(
            {
                k: {"type": v["type"], "multivalue": bool(v.get("multivalue"))}
                for k, v in sorted(schema.fields.items())
            }
        ),
        record_kinds=", ".join(schema.data["record_kinds"]),
        multivalue=canonical_json(profile.get("multivalue", {})),
        inheritance=canonical_json(profile.get("context_inheritance", [])),
        selected_rows=evidence["selected_rows"],
    )
    effort = profile.get("extractor", {}).get("effort") or model["effort"]
    return {
        "model": model["model"],
        "max_tokens": model["max_output_tokens"],
        "system": prompt("extract_system.txt"),
        "messages": [
            {
                "role": "user",
                "content": f"{instructions}\n<evidence>\n{canonical_json(evidence)}\n</evidence>",
            }
        ],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": result_schema(profile)},
        },
    }


def classification_request(
    evidence: dict, registry: Registry, model: dict, profiles: list[str] | None = None
) -> dict:
    refs = sorted(profiles or registry.profiles)
    fields = sorted({f for r in refs for f in registry.profile(r).schema.fields})
    schema = {
        "type": "object",
        "properties": {
            "family": {"type": "string", "enum": list(FAMILIES)},
            "profile": {"type": "string", "enum": [*refs, "none"]},
            "header_rows": {"type": "array", "items": {"type": "integer"}},
            "column_bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "col": {"type": "integer"},
                        "field": {"type": "string", "enum": [*fields, "none"]},
                    },
                    "required": ["col", "field"],
                    "additionalProperties": False,
                },
            },
            "reason": {"type": "string"},
        },
        "required": ["family", "profile", "header_rows", "column_bindings", "reason"],
        "additionalProperties": False,
    }
    catalog = {
        r: {
            "description": registry.profile(r).get("description"),
            "families": registry.profile(r).get("families"),
            "fields": sorted(registry.profile(r).schema.fields),
        }
        for r in refs
    }
    return {
        "model": model["model"],
        "max_tokens": model["max_output_tokens"],
        "system": prompt("classify_system.txt"),
        "messages": [
            {
                "role": "user",
                "content": "Registered profiles:\n"
                + canonical_json(catalog)
                + f"\n<evidence>\n{canonical_json(evidence)}\n</evidence>",
            }
        ],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    }


def hashes(evidence: dict, request: dict, unit: dict, model: dict) -> dict[str, str]:
    return {
        "payload_hash": digest(evidence),
        "request_hash": digest(request),
        "dependency_hash": digest(
            {
                "unit": unit["dependency_hash"],
                "prompt_version": PROMPT_VERSION,
                "model": {
                    k: model[k] for k in ("provider", "model", "effort", "max_output_tokens")
                },
            }
        ),
    }


def estimate_tokens(request: dict) -> int:
    """Local estimate (about 3.5 characters per token); labeled as an estimate by callers."""
    return max(1, int(len(canonical_json(request)) / 3.5))
