"""Deterministic table classification and profile routing (pure; no model, no I/O).

Classification infers a semantic layout (header orientation, row roles, column-to-field
bindings) and routes a table to one approved extraction profile, or explains why it
cannot. It never invents schemas, and its output is a separate artifact from the
structural parse: a first all-``td`` row is a header only when a profile's header
vocabulary matches it, and that inference is flagged.

A table that no template matches is ``unsupported`` (layout, procedure and unknown tables
are valid outcomes), or ``needs_model`` when model classification may help.
"""

from __future__ import annotations

import re
from typing import Any

from confluence_mirror.extraction.registry import FAMILIES, Profile, Registry
from confluence_mirror.structure.grid import diagnostic, project
from confluence_mirror.structure.ids import digest, opaque_id

CLASSIFIER_VERSION = "classify.1"
_PUNCT_RE = re.compile(r"[\s:*#()\[\]]+")
_TOTAL_RE = re.compile(r"^(total|totals|sum|subtotal)\b", re.IGNORECASE)


def header_key(text: str) -> str:
    return _PUNCT_RE.sub(" ", text.lower()).strip()


def _grid(table: dict) -> list[list[dict]]:
    placements = [
        {"cell_id": c["cell_id"], **c["origin"], "rowspan": c["rowspan"], "colspan": c["colspan"]}
        for c in table["cells"]
        if c.get("origin")
    ]
    return project(placements, table["row_count"], table["column_count"])


class Layout:
    """Grid helpers shared by classification, unit planning and the rule extractor."""

    def __init__(self, table: dict) -> None:
        self.table = table
        self.cells = {c["cell_id"]: c for c in table["cells"]}
        self.grid = _grid(table) if table["geometry_status"] != "unsupported_due_to_limit" else []
        self.rows = len(self.grid)
        self.cols = table["column_count"]

    def owner(self, r: int, c: int) -> dict | None:
        slot = self.grid[r - 1][c - 1]
        return self.cells[slot["cells"][0]] if len(slot["cells"]) == 1 else None

    def text(self, r: int, c: int) -> str:
        cell = self.owner(r, c)
        return cell["direct_text"] if cell else ""

    def row_is_header(self, r: int) -> bool:
        owners = [self.owner(r, c) for c in range(1, self.cols + 1)]
        return bool(owners) and all(o is not None and o["tag"] == "th" for o in owners)

    def col_is_header(self, c: int, rows: range) -> bool:
        owners = [self.owner(r, c) for r in rows]
        return bool(owners) and all(o is not None and o["tag"] == "th" for o in owners)

    def nested_share(self) -> float:
        if not self.table["cells"]:
            return 0.0
        nested = sum(
            1
            for c in self.table["cells"]
            if any(x["kind"] == "table" for x in c.get("content", []))
        )
        return nested / len(self.table["cells"])


def _row_roles(layout: Layout, header_rows: list[int], header_texts: list[str]) -> list[dict]:
    roles = []
    for r in range(1, layout.rows + 1):
        if r in header_rows:
            roles.append({"row": r, "role": "header"})
            continue
        texts = [layout.text(r, c) for c in range(1, layout.cols + 1)]
        owners = {id(layout.owner(r, c)) for c in range(1, layout.cols + 1)}
        if not any(texts):
            role = "blank"
        elif layout.cols > 1 and len(owners) == 1:
            role = "section"  # one cell spans the whole row
        elif header_texts and [header_key(t) for t in texts] == header_texts:
            role = "repeated_header"
        elif texts and _TOTAL_RE.match(texts[0]):
            role = "total"
        else:
            role = "data"
        roles.append({"row": r, "role": role})
    return roles


def _bind_first_row(layout: Layout, profile: Profile, header_rows: list[int]) -> dict[str, Any]:
    bottom = header_rows[-1]
    synonyms: dict[str, str] = {}
    for fname, words in profile.get("header_bindings", {}).items():
        for w in words:
            synonyms.setdefault(header_key(w), fname)
    bindings, used, dupes = [], {}, []
    for c in range(1, layout.cols + 1):
        cell = layout.owner(bottom, c)
        text = cell["direct_text"] if cell else ""
        field = synonyms.get(header_key(text))
        if field and field in used:
            dupes.append(field)
        if field:
            used[field] = c
        bindings.append(
            {
                "col": c,
                "field": field,
                "header_text": text,
                "header_cell_id": cell["cell_id"] if cell else None,
                "match": "synonym" if field else None,
            }
        )
    return {"bindings": bindings, "fields": used, "duplicates": dupes}


def match_profile(table: dict, profile: Profile) -> dict[str, Any]:
    """Check one profile's template against a table. Never consults a model."""
    layout = Layout(table)
    template = profile.get("template", {})
    out: dict[str, Any] = {"profile": profile.ref, "matched": False, "warnings": []}
    if table["geometry_status"] != "valid":
        out["reason"] = f"geometry is {table['geometry_status']}"
        return out
    if layout.rows == 0:
        out["reason"] = "empty table"
        return out
    if template.get("header") == "first_row":
        header_rows = []
        for r in range(1, layout.rows + 1):
            if layout.row_is_header(r):
                header_rows.append(r)
            else:
                break
        inferred = False
        if not header_rows:
            header_rows, inferred = [1], True
        bound = _bind_first_row(layout, profile, header_rows)
        required = template.get("required_fields", [])
        missing = [f for f in required if f not in bound["fields"]]
        n_bound = len(bound["fields"])
        if bound["duplicates"]:
            out["reason"] = f"several columns bind to {sorted(set(bound['duplicates']))}"
            return out
        if missing:
            out["reason"] = f"required field(s) not found in the header: {missing}"
            return out
        if n_bound < template.get("min_bound_columns", 1):
            out["reason"] = f"only {n_bound} column(s) bind to the schema"
            return out
        if inferred and n_bound < layout.cols:
            out["reason"] = "first row is not marked as a header and does not fully match"
            return out
        header_texts = [
            header_key(layout.text(header_rows[-1], c)) for c in range(1, layout.cols + 1)
        ]
        if inferred:
            out["warnings"].append(
                diagnostic(
                    "HEADER_INFERRED",
                    "warning",
                    "row 1 has no th cells; it is treated as a header because every cell "
                    "matches the profile's header vocabulary",
                    {"table_id": table["table_id"]},
                    stage="classify",
                )
            )
        out.update(
            matched=True,
            orientation="row",
            header_rows=header_rows,
            header_source="inferred" if inferred else "th",
            column_bindings=bound["bindings"],
            row_roles=_row_roles(layout, header_rows, header_texts),
            score=n_bound,
            unbound_columns=[b["col"] for b in bound["bindings"] if not b["field"]],
        )
        return out
    if template.get("header") == "first_column":
        rows = range(1, layout.rows + 1)
        if layout.cols < 2 or not layout.col_is_header(1, rows):
            out["reason"] = "first column is not all header cells"
            return out
        if any(layout.row_is_header(r) for r in rows):
            out["reason"] = "a full header row exists; not a key/value layout"
            return out
        if layout.cols > 2:
            out["warnings"].append(
                diagnostic(
                    "EXTRA_COLUMNS",
                    "warning",
                    f"{layout.cols - 2} column(s) after the value column are not extracted",
                    {"table_id": table["table_id"]},
                    stage="classify",
                )
            )
        out.update(
            matched=True,
            orientation="column",
            header_rows=[],
            header_source="th",
            column_bindings=[
                {
                    "col": 1,
                    "field": "key",
                    "header_text": None,
                    "header_cell_id": None,
                    "match": "orientation",
                },
                {
                    "col": 2,
                    "field": "value",
                    "header_text": None,
                    "header_cell_id": None,
                    "match": "orientation",
                },
            ],
            row_roles=[{"row": r, "role": "data"} for r in rows],
            score=2,
            unbound_columns=list(range(3, layout.cols + 1)),
        )
        return out
    out["reason"] = f"unsupported template header mode {template.get('header')!r}"
    return out


def _family(table: dict) -> str:
    layout = Layout(table)
    if layout.rows == 0 or (layout.rows == 1 and layout.cols == 1):
        return "layout"
    if layout.nested_share() >= 0.5:
        return "layout"
    first = (
        [header_key(layout.text(1, c)) for c in range(1, layout.cols + 1)] if layout.rows else []
    )
    if any(t in ("step", "steps", "#", "action") for t in first):
        return "procedure"
    if layout.cols >= 2 and layout.col_is_header(1, range(1, layout.rows + 1)):
        return "key_value"
    if layout.row_is_header(1) and layout.rows >= 2:
        return "inventory"
    return "unknown"


def classify_table(
    table: dict,
    artifact: dict,
    registry: Registry,
    *,
    explicit_profile: str | None = None,
    model_allowed: bool = False,
) -> dict[str, Any]:
    """Classification artifact for one table (deterministic)."""
    base = {
        "classifier_version": CLASSIFIER_VERSION,
        "table_id": table["table_id"],
        "artifact_id": artifact["artifact_id"],
        "registry_digest": registry.digest,
        "explicit_profile": explicit_profile,
    }
    if table["geometry_status"] != "valid":
        return _result(
            base,
            family="unknown",
            status="blocked",
            method="structural_check",
            reason=f"geometry is {table['geometry_status']}; automatic extraction is blocked",
        )
    if artifact["representation"] not in ("storage", "export_view", "view"):
        raise ValueError(artifact["representation"])

    if explicit_profile:
        profile = registry.profile(explicit_profile)
        if artifact["representation"] not in profile.get("representations", []):
            return _result(
                base,
                family="unknown",
                status="blocked",
                method="explicit_profile",
                reason=f"{profile.ref} does not accept {artifact['representation']} tables",
            )
        m = match_profile(table, profile)
        if m["matched"]:
            return _routed(base, profile, m, "explicit_profile")
        return _result(
            base,
            family=_family(table),
            status="needs_model" if model_allowed and _model_ok(profile) else "incompatible",
            method="explicit_profile",
            reason=f"{profile.ref} is not structurally compatible: {m['reason']}",
            candidates=[profile.ref],
        )

    matches = []
    rejected = []
    for profile in registry.profiles.values():
        if artifact["representation"] not in profile.get("representations", []):
            continue
        m = match_profile(table, profile)
        (matches if m["matched"] else rejected).append((profile, m))
    if matches:
        best = max(m["score"] for _, m in matches)
        top = [(p, m) for p, m in matches if m["score"] == best]
        if len(top) == 1:
            return _routed(base, top[0][0], top[0][1], "template_rule")
        return _result(
            base,
            family=top[0][0].get("families")[0],
            status="ambiguous",
            method="template_rule",
            reason="several profiles match equally well; choose one with --extraction-profile",
            candidates=sorted(p.ref for p, _ in top),
        )
    family = _family(table)
    if family in ("layout", "procedure"):
        return _result(
            base,
            family=family,
            status="unsupported",
            method="heuristic",
            reason=f"{family} tables are not extracted; nested tables are classified separately",
        )
    return _result(
        base,
        family=family,
        status="needs_model" if model_allowed else "unsupported",
        method="heuristic",
        reason="no approved profile template matches"
        + ("" if model_allowed else "; model classification is not permitted by policy"),
        candidates=[],
        rejected={p.ref: m["reason"] for p, m in rejected},
    )


def _model_ok(profile: Profile) -> bool:
    return bool(profile.get("extractor", {}).get("allow_model"))


def _routed(base: dict, profile: Profile, m: dict, method: str) -> dict:
    return _result(
        base,
        family=profile.get("families")[0],
        status="routed",
        method=method,
        reason=f"template of {profile.ref} matched",
        profile=profile.ref,
        schema=profile.schema.ref,
        orientation=m["orientation"],
        header_rows=m["header_rows"],
        header_source=m["header_source"],
        column_bindings=m["column_bindings"],
        row_roles=m["row_roles"],
        unbound_columns=m["unbound_columns"],
        warnings=m["warnings"],
    )


def _result(base: dict, **kw: Any) -> dict:
    out = {
        **base,
        "family": kw.pop("family"),
        "status": kw.pop("status"),
        "method": kw.pop("method"),
        "reason": kw.pop("reason"),
        "profile": kw.pop("profile", None),
        "schema": kw.pop("schema", None),
        "warnings": kw.pop("warnings", []),
        **kw,
    }
    out["classification_id"] = opaque_id("cls", digest(out))
    return out


def from_model(
    table: dict, artifact: dict, registry: Registry, answer: dict, prior: dict
) -> dict[str, Any]:
    """Turn a model's classification into an artifact, after deterministic checks.

    The model may only choose a registered profile and that profile's fields; required
    fields must be bound and coordinates must exist. Anything else is rejected, not fixed.
    """
    base = {
        k: prior[k]
        for k in (
            "classifier_version",
            "table_id",
            "artifact_id",
            "registry_digest",
            "explicit_profile",
        )
    }
    family = answer.get("family") if answer.get("family") in FAMILIES else "unknown"
    ref = answer.get("profile")
    if not ref or ref == "none":
        return _result(
            base,
            family=family,
            status="unsupported",
            method="model",
            reason="model found no fitting profile: " + str(answer.get("reason", "")),
        )
    try:
        profile = registry.profile(ref)
    except Exception:
        return _result(
            base,
            family=family,
            status="model_invalid",
            method="model",
            reason=f"model chose unregistered profile {ref!r}",
        )
    layout = Layout(table)
    header_rows = sorted({r for r in answer.get("header_rows", []) if isinstance(r, int)})
    problems = []
    if any(not 1 <= r <= layout.rows for r in header_rows):
        problems.append("header row out of range")
    fields_seen: dict[str, int] = {}
    bindings = []
    for b in answer.get("column_bindings", []):
        col, fname = b.get("col"), b.get("field")
        if not isinstance(col, int) or not 1 <= col <= layout.cols:
            problems.append(f"column {col} out of range")
            continue
        if fname == "none":
            fname = None
        elif fname not in profile.schema.fields:
            problems.append(f"field {fname!r} is not in {profile.schema.ref}")
            continue
        if fname and fname in fields_seen:
            problems.append(f"field {fname} bound twice")
        if fname:
            fields_seen[fname] = col
        header = layout.owner(header_rows[-1], col) if header_rows else None
        bindings.append(
            {
                "col": col,
                "field": fname,
                "header_text": header["direct_text"] if header else None,
                "header_cell_id": header["cell_id"] if header else None,
                "match": "model" if fname else None,
            }
        )
    missing = [
        f for f in profile.get("template", {}).get("required_fields", []) if f not in fields_seen
    ]
    if missing:
        problems.append(f"required field(s) unbound: {missing}")
    if problems:
        return _result(
            base,
            family=family,
            status="model_invalid",
            method="model",
            reason="; ".join(problems),
            candidates=[profile.ref],
        )
    header_texts = (
        [header_key(layout.text(header_rows[-1], c)) for c in range(1, layout.cols + 1)]
        if header_rows
        else []
    )
    return _result(
        base,
        family=family,
        status="routed",
        method="model",
        reason=f"model routed to {profile.ref}: {answer.get('reason', '')}",
        profile=profile.ref,
        schema=profile.schema.ref,
        orientation="row",
        header_rows=header_rows,
        header_source="model",
        column_bindings=sorted(bindings, key=lambda b: b["col"]),
        row_roles=_row_roles(layout, header_rows, header_texts),
        unbound_columns=[b["col"] for b in bindings if not b["field"]],
        warnings=[
            diagnostic(
                "MODEL_CLASSIFIED",
                "warning",
                "column bindings were proposed by the model; review them",
                {"table_id": table["table_id"]},
                stage="classify",
            )
        ],
    )
