"""Deterministic, rule-only extraction for tables an approved template matched (pure).

It returns the same result contract as the model (records, row_coverage, ambiguities,
relationships) with exact evidence spans, so both paths share normalization and
validation. It never zips independent lists, never expands ranges, and never fills a
blank cell from the row above unless a rowspan says so.
"""

from __future__ import annotations

import re
from typing import Any

from confluence_mirror.extraction.classify import Layout, header_key
from confluence_mirror.extraction.normalize import Member, split_list
from confluence_mirror.extraction.registry import Profile

RULES_VERSION = "rules.1"
_ROLE_DISPOSITION = {
    "header": "header",
    "repeated_header": "repeated_header",
    "section": "section_divider",
    "total": "total",
    "blank": "blank",
}


def _span(block_id: str, text: str, m: Member) -> dict:
    return {"block_id": block_id, "start": m.start, "end": m.end, "quote": text[m.start : m.end]}


class _Row:
    def __init__(self, extractor: RuleExtractor, r: int) -> None:
        self.x = extractor
        self.r = r
        self.ambiguities: list[dict] = []

    def cell_value(self, col: int, field: str) -> dict[str, Any]:
        """Field assertion skeleton for the owner of (row, col)."""
        cell = self.x.layout.owner(self.r, col)
        if cell is None:
            return {
                "state": "ambiguous",
                "members": [],
                "cell_id": None,
                "reason": "slot has conflicting owners",
            }
        texts = [c for c in cell.get("content", []) if c["kind"] == "text"]
        if not texts:
            return {"state": "absent", "members": [], "cell_id": cell["cell_id"], "reason": ""}
        if len(texts) > 1:
            self.ambiguities.append(
                {
                    "code": "MULTI_BLOCK_CELL",
                    "message": f"{field}: cell holds several text blocks around nested content",
                    "cell_ids": [cell["cell_id"]],
                }
            )
            return {
                "state": "ambiguous",
                "members": [],
                "cell_id": cell["cell_id"],
                "reason": "several text blocks",
            }
        bid = texts[0]["block_id"]
        text = self.x.blocks[bid]["evidence_text"]
        rule = self.x.profile.get("multivalue", {}).get(field, {})
        if rule.get("grammar") == "list":
            members, warnings = split_list(text, rule.get("separators", [","]))
        else:
            members, warnings = split_list(text, [])
            for sep in rule.get("flag_compound", []):
                if sep in text:
                    self.ambiguities.append(
                        {
                            "code": "COMPOUND_VALUE",
                            "message": f"{field}: {text!r} may combine several values; kept whole",
                            "cell_ids": [cell["cell_id"]],
                        }
                    )
                    break
        state = "present" if members else "absent"
        if "RANGE_NOT_EXPANDED" in warnings:
            state = "ambiguous"
            self.ambiguities.append(
                {
                    "code": "RANGE_NOT_EXPANDED",
                    "message": f"{field}: {text!r} looks like a range; it is not expanded",
                    "cell_ids": [cell["cell_id"]],
                }
            )
        return {
            "state": state,
            "members": [(bid, text, m) for m in members],
            "cell_id": cell["cell_id"],
            "reason": "",
            "covered": (cell["origin"]["row"], cell["origin"]["col"]) != (self.r, col),
        }


class RuleExtractor:
    def __init__(
        self, artifact: dict, table: dict, unit: dict, classification: dict, profile: Profile
    ) -> None:
        self.artifact = artifact
        self.table = table
        self.unit = unit
        self.classification = classification
        self.profile = profile
        self.schema = profile.schema
        self.layout = Layout(table)
        self.blocks = artifact["blocks"]
        self.heading_blocks = {
            n["id"]: n.get("block_id") for n in artifact["nodes"] if n["kind"] == "heading"
        }

    # -- inheritance --------------------------------------------------------------------

    def _inherit(self, field: str) -> dict | None:
        for rule in self.profile.get("context_inheritance", []):
            if rule["field"] != field:
                continue
            if rule["from"] in ("heading", "heading_text"):
                for h in reversed(self.table["heading_path"]):
                    bid = self.heading_blocks.get(h["id"])
                    if not bid:
                        continue
                    text = self.blocks[bid]["evidence_text"]
                    if rule["from"] == "heading_text":
                        return {"bid": bid, "text": text, "start": 0, "end": len(text)}
                    m = re.search(rule["pattern"], text)
                    if m:
                        g = 1 if m.groups() else 0
                        return {"bid": bid, "text": text, "start": m.start(g), "end": m.end(g)}
            elif rule["from"] == "parent_row":
                pc = self.unit.get("parent_context")
                if not pc:
                    continue
                synonyms = {
                    header_key(w): f
                    for f, words in self.profile.get("header_bindings", {}).items()
                    for w in words
                }
                for c in pc["cells"]:
                    if synonyms.get(header_key(c["header_text"] or "")) not in rule.get(
                        "parent_fields", [field]
                    ):
                        continue
                    ptable = next(
                        t for t in self.artifact["tables"] if t["table_id"] == pc["table_id"]
                    )
                    pcell = next(x for x in ptable["cells"] if x["cell_id"] == c["cell_id"])
                    texts = [x for x in pcell.get("content", []) if x["kind"] == "text"]
                    if len(texts) == 1:
                        bid = texts[0]["block_id"]
                        text = self.blocks[bid]["evidence_text"]
                        return {"bid": bid, "text": text, "start": 0, "end": len(text)}
        return None

    # -- extraction ---------------------------------------------------------------------

    def _assert(self, field: str, value: dict) -> dict:
        members = value["members"]
        out = {
            "field": field,
            "values": [text[m.start : m.end] for _, text, m in members],
            "value_state": value["state"],
            "method": "quoted",
            "evidence": [_span(bid, text, m) for bid, text, m in members],
            "reason": value.get("reason", ""),
        }
        if value.get("covered"):
            out["reason"] = "value from a merged cell covering this row"
        return out

    def _inherited(self, field: str) -> dict | None:
        found = self._inherit(field)
        if not found:
            return None
        quote = found["text"][found["start"] : found["end"]]
        return {
            "field": field,
            "values": [quote],
            "value_state": "present",
            "method": "inherited_context",
            "evidence": [
                {
                    "block_id": found["bid"],
                    "start": found["start"],
                    "end": found["end"],
                    "quote": quote,
                }
            ],
            "reason": "inherited under the profile's context rule",
        }

    def run(self) -> dict[str, Any]:
        roles = {r["row"]: r["role"] for r in self.classification["row_roles"]}
        bindings = [b for b in self.classification["column_bindings"] if b["field"]]
        explode = self.profile.get("record_granularity", {}).get("explode_field")
        required = [f for f, spec in self.schema.fields.items() if spec.get("required")]
        kind = self.schema.data["record_kinds"][0]
        records, coverage, ambiguities, relationships = [], [], [], []
        for r in self.unit["selected_rows"]:
            role = roles.get(r, "data")
            if role != "data":
                coverage.append(
                    {
                        "row": r,
                        "disposition": _ROLE_DISPOSITION.get(role, "skipped"),
                        "local_ids": [],
                        "reason": "",
                    }
                )
                continue
            row = _Row(self, r)
            values = {b["field"]: row.cell_value(b["col"], b["field"]) for b in bindings}
            ambiguities += row.ambiguities
            for field in self.schema.fields:
                if field not in values or values[field]["state"] == "absent":
                    inherited = self._inherited(field)
                    if inherited:
                        values[field] = {"inherited": inherited}
            missing = [
                f for f in required if values.get(f, {"state": "absent"}).get("state") == "absent"
            ]
            if missing:
                coverage.append(
                    {
                        "row": r,
                        "disposition": "skipped",
                        "local_ids": [],
                        "reason": f"required field(s) empty: {', '.join(missing)}",
                    }
                )
                continue
            fan = values.get(explode, {}).get("members", []) if explode else []
            groups = [[m] for m in fan] if len(fan) > 1 else [None]
            ids = []
            for i, group in enumerate(groups):
                local_id = f"r{r}" + (f".{i + 1}" if len(groups) > 1 else "")
                fields = []
                for field in self.schema.fields:
                    v = values.get(field)
                    if v is None:
                        continue
                    if "inherited" in v:
                        fields.append(v["inherited"])
                        continue
                    if field == explode and group is not None:
                        fields.append(self._assert(field, {**v, "members": group}))
                    elif (
                        group is not None
                        and len(v["members"]) > 1
                        and (self.schema.fields[field].get("multivalue"))
                    ):
                        # Several hosts and several values in another cell: never zip.
                        fields.append(
                            {
                                **self._assert(field, v),
                                "values": [],
                                "value_state": "ambiguous",
                                "reason": f"{len(v['members'])} values cannot be paired with "
                                f"{len(groups)} {explode} values without positional evidence",
                            }
                        )
                        if i == 0:
                            ambiguities.append(
                                {
                                    "code": "MULTIVALUE_ALIGNMENT_AMBIGUOUS",
                                    "message": f"row {r}: {field} has {len(v['members'])} "
                                    f"values for {len(groups)} {explode} values",
                                    "cell_ids": [v["cell_id"]],
                                }
                            )
                    elif len(v["members"]) > 1 and not self.schema.fields[field].get("multivalue"):
                        fields.append(
                            {
                                **self._assert(field, v),
                                "value_state": "ambiguous",
                                "reason": "several values for a single-valued field",
                            }
                        )
                    else:
                        fields.append(self._assert(field, v))
                records.append(
                    {
                        "local_id": local_id,
                        "record_kind": kind,
                        "source_rows": [r],
                        "fields": fields,
                    }
                )
                ids.append(local_id)
                for pred, rel in self.profile.get("relationships", {}).items():
                    src = next((f for f in fields if f["field"] == rel["from_field"]), None)
                    if src and src["value_state"] == "present":
                        for value, ev in zip(src["values"], src["evidence"], strict=False):
                            relationships.append(
                                {
                                    "predicate": pred,
                                    "source_local_id": local_id,
                                    "target_local_id": "",
                                    "target_key": value,
                                    "evidence": [ev],
                                }
                            )
            coverage.append({"row": r, "disposition": "record", "local_ids": ids, "reason": ""})
        result: dict[str, Any] = {
            "records": records,
            "row_coverage": coverage,
            "ambiguities": ambiguities,
        }
        if self.profile.get("relationships"):
            result["relationships"] = relationships
        return result
