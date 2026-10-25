"""Page-template detection over a page tree (pure: no I/O, no model calls).

A *template family* is a parent page whose children are all leaves (no grandchildren) and
were written to one pattern: the same sections, the same tables with the same columns.
Finding the candidates is structural; deciding whether the children really share a
template is the model's job, in two passes:

1. **propose**: the model sees structural summaries of a sample of the children and says
   whether they follow one template, and if so describes it (sections, tables, columns
   with field names and types);
2. **check**: the model checks *every* child against that proposal and says, page by page,
   whether it conforms and what is missing or unexpected.

This module builds the summaries and both requests, and turns the two answers into a
verdict. Summaries hold structure plus a little sample text, redacted with the privacy
rules before anything is sent.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from confluence_mirror.extraction.classify import Layout
from confluence_mirror.extraction.payload import prompt
from confluence_mirror.extraction.privacy import PrivacyPolicy, redact
from confluence_mirror.structure.ids import canonical_json

DETECTOR_VERSION = "templates.1"
TEMPLATE_PROMPT_VERSION = "template-prompt.1"
FIELD_TYPES = ["string", "hostname", "ip_address", "port", "integer", "date", "email", "url"]
CHECK_BATCH = 10
# A template holds when at least this share of the checked children conforms.
CONFORMANCE_THRESHOLD = 0.8
MIXED_THRESHOLD = 0.5

_FIELD_RE = re.compile(r"[^a-z0-9]+")


# -- summaries ------------------------------------------------------------------------------


class _Redactor:
    def __init__(self, policy: PrivacyPolicy) -> None:
        self.rules = policy.rules
        self.counter: dict[str, int] = {}
        self.count = 0

    def __call__(self, text: str | None, limit: int | None = None) -> str:
        text = (text or "").strip()
        if limit is not None and len(text) > limit:
            text = text[: limit - 1] + "…"
        out, mappings = redact(text, self.rules, self.counter)
        self.count += len(mappings)
        return out


def _table_summary(table: dict, clean: _Redactor, sample_rows: int, cell_chars: int) -> dict:
    out: dict[str, Any] = {
        "ordinal": table["ordinal"],
        "under_heading": [clean(h["text"], 120) for h in table["heading_path"]][-2:],
        "rows": table["row_count"],
        "columns": table["column_count"],
    }
    if table["geometry_status"] == "unsupported_due_to_limit":
        out["note"] = "too large to summarise"
        return out
    layout = Layout(table)
    if not layout.rows:
        return out
    header_rows = []
    for r in range(1, layout.rows + 1):
        if not layout.row_is_header(r):
            break
        header_rows.append(r)
    rows = range(1, layout.rows + 1)
    if header_rows:
        out["layout"] = "header_row"
        out["header"] = [
            clean(layout.text(header_rows[-1], c), cell_chars) for c in range(1, layout.cols + 1)
        ]
        data = [r for r in rows if r not in header_rows]
    elif layout.cols >= 2 and layout.col_is_header(1, rows):
        out["layout"] = "key_column"
        out["keys"] = [clean(layout.text(r, 1), cell_chars) for r in rows][:30]
        data = []
    else:
        out["layout"] = "no_header_cells"
        data = list(rows)
    out["sample_rows"] = [
        [clean(layout.text(r, c), cell_chars) for c in range(1, layout.cols + 1)]
        for r in data[:sample_rows]
    ]
    return out


def page_summary(
    artifact: dict,
    title: str,
    policy: PrivacyPolicy,
    *,
    max_tables: int = 8,
    sample_rows: int = 2,
    cell_chars: int = 60,
) -> dict:
    """A compact, redacted structural summary of one page for the template prompts."""
    clean = _Redactor(policy)
    headings = [
        {"level": n["level"], "text": clean(n["text"], 120)}
        for n in artifact["nodes"]
        if n["kind"] == "heading" and n.get("cell_id") is None
    ][:40]
    macros = Counter(n["name"] for n in artifact["nodes"] if n["kind"] == "macro")
    top = [t for t in artifact["tables"] if t["depth"] == 0]
    return {
        "page_id": artifact["page_id"],
        "title": clean(title, 200),
        "headings": headings,
        "tables": [_table_summary(t, clean, sample_rows, cell_chars) for t in top[:max_tables]],
        "more_tables": max(len(top) - max_tables, 0),
        "nested_tables": len(artifact["tables"]) - len(top),
        "macros": dict(sorted(macros.items())),
        "redactions": clean.count,
    }


def clean_text(text: str, policy: PrivacyPolicy, limit: int = 200) -> str:
    """One redacted string (for text sent outside a page summary, such as a parent title)."""
    return _Redactor(policy)(text, limit)


def pick_sample(page_ids: list[int], size: int) -> list[int]:
    """Up to ``size`` IDs spread evenly over the list (deterministic)."""
    if len(page_ids) <= size:
        return list(page_ids)
    step = (len(page_ids) - 1) / (size - 1) if size > 1 else 0
    return [page_ids[round(i * step)] for i in range(size)]


# -- requests -------------------------------------------------------------------------------


def _strict(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}

PROPOSAL_SCHEMA = _strict(
    {
        "follows_template": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "name": _STR,
        "description": _STR,
        "sections": _STRS,
        "tables": {
            "type": "array",
            "items": _strict(
                {
                    "key": _STR,
                    "located_under": _STR,
                    "layout": {"type": "string", "enum": ["header_row", "key_column", "other"]},
                    "purpose": _STR,
                    "columns": {
                        "type": "array",
                        "items": _strict(
                            {
                                "header": _STR,
                                "field": _STR,
                                "type": {"type": "string", "enum": FIELD_TYPES},
                                "description": _STR,
                            }
                        ),
                    },
                }
            ),
        },
        "deviating_pages": {
            "type": "array",
            "items": _strict({"page_id": {"type": "integer"}, "reason": _STR}),
        },
        "reason": _STR,
    }
)

CHECK_SCHEMA = _strict(
    {
        "pages": {
            "type": "array",
            "items": _strict(
                {
                    "page_id": {"type": "integer"},
                    "conforms": {"type": "boolean"},
                    "missing": _STRS,
                    "unexpected": _STRS,
                    "notes": _STR,
                }
            ),
        },
        "template_issues": _STRS,
    }
)


def _request(model: dict, instructions: str, payload: dict, schema: dict) -> dict:
    return {
        "model": model["model"],
        "max_tokens": model["max_output_tokens"],
        "system": prompt("template_system.txt"),
        "messages": [
            {
                "role": "user",
                "content": f"{instructions}\n<pages>\n{canonical_json(payload)}\n</pages>",
            }
        ],
        "output_config": {
            "effort": model["effort"],
            "format": {"type": "json_schema", "schema": schema},
        },
    }


def proposal_request(parent: dict, child_count: int, sample: list[dict], model: dict) -> dict:
    """Ask whether the sampled children of ``parent`` follow one template, and describe it."""
    payload = {
        "parent": {"page_id": parent["page_id"], "title": parent["title"]},
        "children_total": child_count,
        "children_shown": len(sample),
        "children": sample,
    }
    return _request(model, prompt("template_propose.txt"), payload, PROPOSAL_SCHEMA)


def check_request(template: dict, batch: list[dict], model: dict) -> dict:
    """Ask, page by page, whether each child conforms to a proposed template."""
    payload = {"template": template, "children": batch}
    return _request(model, prompt("template_check.txt"), payload, CHECK_SCHEMA)


# -- verdict ----------------------------------------------------------------------------------


def field_name(text: str) -> str:
    name = _FIELD_RE.sub("_", (text or "").lower()).strip("_")
    if not name or not name[0].isalpha():
        name = f"f_{name}" if name else "field"
    return name[:63]


def clean_proposal(proposal: dict, child_ids: set[int]) -> tuple[dict, list[str]]:
    """Normalise a proposal and list what had to be corrected (never silently)."""
    issues: list[str] = []
    out = dict(proposal)
    tables = []
    for t in proposal.get("tables", []):
        seen: dict[str, int] = {}
        cols = []
        for c in t.get("columns", []):
            name = field_name(c.get("field") or c.get("header"))
            if name != c.get("field"):
                issues.append(f"field {c.get('field')!r} renamed to {name!r}")
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name}_{seen[name]}"
                issues.append(f"duplicate field in table {t.get('key')!r} renamed to {name!r}")
            cols.append({**c, "field": name})
        tables.append({**t, "columns": cols})
    out["tables"] = tables
    deviating = []
    for d in proposal.get("deviating_pages", []):
        if d.get("page_id") in child_ids:
            deviating.append(d)
        else:
            issues.append(f"deviating page {d.get('page_id')} is not a child; ignored")
    out["deviating_pages"] = deviating
    return out, issues


def merge_checks(answers: list[dict], batches: list[list[int]]) -> tuple[list[dict], list[str]]:
    """Per-child conformance from the check answers; unanswered children are ``unchecked``."""
    pages: list[dict] = []
    issues: list[str] = []
    for answer, batch in zip(answers, batches, strict=True):
        wanted = set(batch)
        got: dict[int, dict] = {}
        for p in (answer or {}).get("pages", []):
            pid = p.get("page_id")
            if pid not in wanted:
                issues.append(f"check answered for page {pid}, which was not in its batch")
            elif pid in got:
                issues.append(f"check answered twice for page {pid}; first answer kept")
            else:
                got[pid] = p
        for pid in batch:
            if pid in got:
                p = got[pid]
                pages.append(
                    {
                        "page_id": pid,
                        "state": "conforms" if p["conforms"] else "deviates",
                        "missing": p.get("missing", []),
                        "unexpected": p.get("unexpected", []),
                        "notes": p.get("notes", ""),
                    }
                )
            else:
                pages.append({"page_id": pid, "state": "unchecked"})
                if answer is not None:
                    issues.append(f"check gave no answer for page {pid}")
        for issue in (answer or {}).get("template_issues", []):
            issues.append(f"checker: {issue}")
    return pages, issues


def verdict(proposal: dict, pages: list[dict]) -> dict[str, Any]:
    """Status from the proposal and the per-child check.

    ``template`` needs the proposer to see a template and the checker to confirm it for at
    least ``CONFORMANCE_THRESHOLD`` of the checked children; ``mixed`` is a template that
    only part of the family follows; ``no_template`` is either the proposer saying so or
    the checker rejecting most children (a disagreement, reported as such).
    """
    if not proposal.get("follows_template"):
        return {"status": "no_template", "conformance": None, "disagreement": False}
    checked = [p for p in pages if p["state"] != "unchecked"]
    if not checked:
        return {"status": "unverified", "conformance": None, "disagreement": False}
    share = sum(p["state"] == "conforms" for p in checked) / len(checked)
    if share >= CONFORMANCE_THRESHOLD:
        status = "template"
    elif share >= MIXED_THRESHOLD:
        status = "mixed"
    else:
        status = "no_template"
    return {
        "status": status,
        "conformance": round(share, 3),
        "checked": len(checked),
        "unchecked": len(pages) - len(checked),
        "disagreement": status == "no_template",
    }
