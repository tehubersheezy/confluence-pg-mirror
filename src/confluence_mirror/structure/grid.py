"""Table geometry: place physical cells on logical coordinates (pure; no I/O).

The physical model is what the source says: rows, in row groups, holding cells with span
attributes. The logical model projects those spans onto 1-based ``(row, col)`` slots. A slot
is ``origin`` (a cell starts here), ``covered`` (another coordinate's cell owns it),
``empty`` (no cell covers it) or ``conflict`` (malformed spans claim it more than once).

Nothing here invents data: a span reaching past its row group is cut at the group boundary
and the geometry is marked ``invalid``; an overlap keeps every claimant; a non-numeric span
is read as 1 and the geometry is marked ``recovered``. Only ``valid`` geometry is eligible
for automatic extraction.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

# HTML table model caps (WHATWG "processing model"); values above them are clamped.
_MAX_COLSPAN = 1000
_MAX_ROWSPAN = 65534
_SPAN_RE = re.compile(r"^\s*(\d+)\s*$")

STATUS_ORDER = ("valid", "recovered", "invalid", "unsupported_due_to_limit")


@dataclass(frozen=True)
class Limits:
    """Application safeguards, checked before any dense structure is allocated."""

    max_rows: int = 5000
    max_cols: int = 200
    max_slots: int = 250_000
    max_depth: int = 16
    max_body_bytes: int = 10 * 1024 * 1024

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def diagnostic(
    code: str, severity: str, message: str, ref: dict[str, Any] | None = None, stage: str = "parse"
) -> dict[str, Any]:
    return {"code": code, "severity": severity, "stage": stage, "message": message, "ref": ref}


def worse(a: str, b: str) -> str:
    return a if STATUS_ORDER.index(a) >= STATUS_ORDER.index(b) else b


def parse_span(raw: str | None, kind: str) -> tuple[int, str | None]:
    """Return ``(value, problem)``; ``problem`` is None for a well-formed attribute.

    ``rowspan="0"`` is valid HTML (span to the end of the row group) and returns 0.
    """
    if raw is None:
        return 1, None
    match = _SPAN_RE.match(raw)
    if not match:
        return 1, f"non-numeric {kind} {raw!r} read as 1"
    value = int(match.group(1))
    if kind == "colspan":
        if value == 0:
            return 1, "colspan 0 read as 1"
        if value > _MAX_COLSPAN:
            return _MAX_COLSPAN, f"colspan {value} clamped to {_MAX_COLSPAN}"
    elif value > _MAX_ROWSPAN:
        return _MAX_ROWSPAN, f"rowspan {value} clamped to {_MAX_ROWSPAN}"
    return value, None


@dataclass
class CellInput:
    cell_id: str
    rowspan: str | None = None
    colspan: str | None = None


@dataclass
class RowInput:
    group_id: str
    cells: list[CellInput]


@dataclass
class Placement:
    cell_id: str
    row: int  # 1-based origin
    col: int
    requested_rowspan: int  # after numeric parsing; 0 means "to end of row group"
    requested_colspan: int
    rowspan: int  # effective coverage actually projected
    colspan: int


@dataclass
class Layout:
    status: str
    row_count: int
    column_count: int
    placements: list[Placement]
    diagnostics: list[dict[str, Any]]


def layout(rows: list[RowInput], limits: Limits | None = None) -> Layout:
    limits = limits or Limits()
    diags: list[dict[str, Any]] = []
    status = "valid"
    n = len(rows)
    if n > limits.max_rows:
        return Layout(
            "unsupported_due_to_limit",
            n,
            0,
            [],
            [
                diagnostic(
                    "RESOURCE_LIMIT_EXCEEDED",
                    "blocker",
                    f"{n} rows exceeds the limit of {limits.max_rows}",
                )
            ],
        )

    # Exclusive end index of each row's group; groups are contiguous runs of the same id.
    group_end = [0] * n
    i = 0
    while i < n:
        j = i
        while j < n and rows[j].group_id == rows[i].group_id:
            j += 1
        for k in range(i, j):
            group_end[k] = j
        i = j

    occupied: dict[tuple[int, int], list[str]] = {}
    placements: list[Placement] = []
    for r, row in enumerate(rows):
        c = 0
        for cell in row.cells:
            while (r, c) in occupied:
                c += 1
            ref = {"cell_id": cell.cell_id}
            rs_req, rs_problem = parse_span(cell.rowspan, "rowspan")
            cs_req, cs_problem = parse_span(cell.colspan, "colspan")
            for problem, code in ((rs_problem, "INVALID_ROWSPAN"), (cs_problem, "INVALID_COLSPAN")):
                if problem:
                    diags.append(diagnostic(code, "warning", problem, ref))
                    status = worse(status, "recovered")
            available = group_end[r] - r
            if rs_req == 0:
                rs = available
            elif rs_req > available:
                rs = available
                if group_end[r] == n:
                    code = "SPAN_EXTENDS_BEYOND_ROWS"
                    message = f"rowspan {rs_req} extends past the last authored row"
                else:
                    code = "SPAN_CROSSES_ROW_GROUP"
                    message = f"rowspan {rs_req} crosses the end of its row group"
                diags.append(diagnostic(code, "blocker", message, ref))
                status = worse(status, "invalid")
            else:
                rs = rs_req
            if c + cs_req > limits.max_cols:
                return Layout(
                    "unsupported_due_to_limit",
                    n,
                    c + cs_req,
                    [],
                    [
                        *diags,
                        diagnostic(
                            "RESOURCE_LIMIT_EXCEEDED",
                            "blocker",
                            f"{c + cs_req} logical columns exceeds the limit of {limits.max_cols}",
                            ref,
                        ),
                    ],
                )
            rect = [(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs_req)]
            clash = sorted({owner for slot in rect for owner in occupied.get(slot, [])})
            if clash:
                diags.append(
                    diagnostic(
                        "SPAN_OVERLAP",
                        "blocker",
                        f"cell overlaps slots already owned by {', '.join(clash)}",
                        ref,
                    )
                )
                status = worse(status, "invalid")
            for slot in rect:
                occupied.setdefault(slot, []).append(cell.cell_id)
            placements.append(Placement(cell.cell_id, r + 1, c + 1, rs_req, cs_req, rs, cs_req))
            c += cs_req

    width = max((cc + 1 for (_, cc) in occupied), default=0)
    if n * width > limits.max_slots:
        return Layout(
            "unsupported_due_to_limit",
            n,
            width,
            [],
            [
                *diags,
                diagnostic(
                    "RESOURCE_LIMIT_EXCEEDED",
                    "blocker",
                    f"{n}x{width} logical slots exceeds the limit of {limits.max_slots}",
                ),
            ],
        )
    extents = [0] * n
    for rr, cc in occupied:
        extents[rr] = max(extents[rr], cc + 1)
    if len(set(extents)) > 1:
        diags.append(
            diagnostic(
                "NON_RECTANGULAR_GRID",
                "warning",
                "rows cover different numbers of columns; uncovered slots are empty",
            )
        )
    return Layout(status, n, width, placements, diags)


def project(
    placements: list[dict[str, Any]], row_count: int, column_count: int
) -> list[list[dict[str, Any]]]:
    """Logical slots for stored placements (``row``/``col``/``rowspan``/``colspan`` dicts)."""
    claims: dict[tuple[int, int], list[tuple[str, bool]]] = {}
    for p in placements:
        if p.get("row") is None:
            continue
        for r in range(p["row"], p["row"] + p["rowspan"]):
            for c in range(p["col"], p["col"] + p["colspan"]):
                is_origin = (r, c) == (p["row"], p["col"])
                claims.setdefault((r, c), []).append((p["cell_id"], is_origin))
    grid: list[list[dict[str, Any]]] = []
    for r in range(1, row_count + 1):
        line = []
        for c in range(1, column_count + 1):
            owners = claims.get((r, c), [])
            if not owners:
                kind = "empty"
            elif len(owners) > 1:
                kind = "conflict"
            else:
                kind = "origin" if owners[0][1] else "covered"
            line.append({"kind": kind, "cells": [cid for cid, _ in owners]})
        grid.append(line)
    return grid
