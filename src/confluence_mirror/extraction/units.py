"""Extraction units: deterministic row-group chunks of one routed table (pure).

A unit is one table, or one chunk of its rows, plus exactly the cells, header cells and
context blocks needed to interpret those rows. Chunks follow record groups: rows tied
together by a rowspan are never split, and the origin cell of a span that covers a
selected row is included even when it starts outside the chunk. A group larger than the
chunk limit is ``oversized`` and is not silently truncated.
"""

from __future__ import annotations

from typing import Any

from confluence_mirror.extraction.classify import Layout
from confluence_mirror.extraction.registry import Profile
from confluence_mirror.structure.ids import digest, opaque_id

PLANNER_VERSION = "units.1"


def _record_groups(layout: Layout, rows: list[int]) -> list[list[int]]:
    """Consecutive rows joined by any cell whose rowspan covers more than one of them."""
    parent = {r: r for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            x = parent[x]
        return x

    selected = set(rows)
    for cell in layout.table["cells"]:
        o = cell.get("origin")
        if not o or cell["rowspan"] <= 1:
            continue
        covered = [r for r in range(o["row"], o["row"] + cell["rowspan"]) if r in selected]
        for r in covered[1:]:
            parent[find(r)] = find(covered[0])
    groups: dict[int, list[int]] = {}
    for r in rows:
        groups.setdefault(find(r), []).append(r)
    return sorted(groups.values())


def _parent_context(artifact: dict, table: dict) -> dict | None:
    if not table.get("parent_cell_id"):
        return None
    parent = next(
        (t for t in artifact["tables"] if t["table_id"] == table["parent_table_id"]), None
    )
    if parent is None:
        return None
    holder = next(c for c in parent["cells"] if c["cell_id"] == table["parent_cell_id"])
    layout = Layout(parent)
    row = holder["origin"]["row"] if holder.get("origin") else None
    if row is None:
        return None
    cells = []
    for c in range(1, layout.cols + 1):
        owner = layout.owner(row, c)
        header = layout.owner(1, c) if layout.row_is_header(1) else None
        if owner is None or owner["cell_id"] == holder["cell_id"]:
            continue
        cells.append(
            {
                "cell_id": owner["cell_id"],
                "col": c,
                "header_text": header["direct_text"] if header else None,
                "header_cell_id": header["cell_id"] if header else None,
            }
        )
    return {
        "table_id": parent["table_id"],
        "row": row,
        "holder_cell_id": holder["cell_id"],
        "cells": cells,
    }


def plan_units(
    artifact: dict,
    table: dict,
    classification: dict,
    profile: Profile,
    privacy_digest: str,
) -> list[dict[str, Any]]:
    layout = Layout(table)
    roles = {r["row"]: r["role"] for r in classification["row_roles"]}
    header_rows = classification.get("header_rows") or []
    body_rows = [r for r in range(1, layout.rows + 1) if r not in header_rows]
    data_rows = [r for r in body_rows if roles.get(r) == "data"]
    max_rows = int(profile.get("chunking", {}).get("max_rows", 40))
    groups = _record_groups(layout, data_rows)

    chunks: list[list[int]] = []
    current: list[int] = []
    oversized: list[list[int]] = []
    for g in groups:
        if len(g) > max_rows:
            if current:
                chunks.append(current)
                current = []
            oversized.append(g)
            chunks.append(g)
            continue
        if len(current) + len(g) > max_rows:
            chunks.append(current)
            current = []
        current += g
    if current or not chunks:
        chunks.append(current)

    # Chunks partition the body rows contiguously: a non-data row (section, total, blank)
    # belongs to the chunk whose data precedes it.
    starts = [min(rows) if rows else None for rows in chunks]
    units = []
    for i, rows in enumerate(chunks):
        nxt = next((s for s in starts[i + 1 :] if s is not None), None)
        selected = [
            r
            for r in body_rows
            if (i == 0 or starts[i] is None or r >= starts[i]) and (nxt is None or r < nxt)
        ]
        wanted = set(selected) | set(header_rows)
        cell_ids = []
        for cell in table["cells"]:
            o = cell.get("origin")
            if not o:
                continue
            span = range(o["row"], o["row"] + cell["rowspan"])
            if any(r in wanted for r in span):
                cell_ids.append(cell["cell_id"])
        delegated = [
            x["table_id"]
            for cid in cell_ids
            for x in layout.cells[cid].get("content", [])
            if x["kind"] == "table"
        ]
        manifest = {
            "planner_version": PLANNER_VERSION,
            "table_id": table["table_id"],
            "artifact_id": artifact["artifact_id"],
            "snapshot_id": artifact["snapshot_id"],
            "classification_id": classification["classification_id"],
            "profile": profile.ref,
            "profile_digest": profile.digest,
            "schema": profile.schema.ref,
            "schema_digest": profile.schema.digest,
            "privacy_digest": privacy_digest,
            "chunk_index": i + 1,
            "chunk_count": len(chunks),
            "header_rows": header_rows,
            "selected_rows": selected,
            "data_rows": rows,
            "cell_ids": cell_ids,
            "delegated_tables": delegated,
            "parent_context": _parent_context(artifact, table),
            "status": "oversized" if rows in oversized else "planned",
        }
        manifest["dependency_hash"] = digest(manifest)
        manifest["unit_id"] = opaque_id("unit", manifest["dependency_hash"])
        units.append(manifest)
    return units


def coverage_problems(units: list[dict], classification: dict, row_count: int) -> list[str]:
    """Gaps or unintended overlap between the units' selected rows."""
    header_rows = set(classification.get("header_rows") or [])
    seen: dict[int, int] = {}
    for u in units:
        for r in u["selected_rows"]:
            seen[r] = seen.get(r, 0) + 1
    problems = []
    for r in range(1, row_count + 1):
        if r in header_rows:
            continue
        if seen.get(r, 0) == 0:
            problems.append(f"row {r} is in no unit")
        elif seen[r] > 1:
            problems.append(f"row {r} is in {seen[r]} units")
    return problems
