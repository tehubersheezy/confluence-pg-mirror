"""Cross-representation table correspondence and extraction selection (pure; no I/O).

The same authored table usually appears in storage, export view and view. Correspondence
says which occurrences are the same table; it never deletes or merges occurrences, and
identical text alone is not enough (the same table can legitimately appear twice).

States: ``confirmed`` (one-to-one on content and geometry, unambiguous), ``tentative``
(partial evidence, or a pairing that rests on order among identical tables), ``unmatched``
(no counterpart) and ``conflicting`` (several equally good counterparts).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from confluence_mirror.structure.grid import project
from confluence_mirror.structure.ids import digest, opaque_id

ALGORITHM_VERSION = "corr.1"
SIMILARITY_THRESHOLD = 0.8
# Selection preference for authored tables; render-only tables come from export view.
PREFERENCE = ("storage", "export_view", "view")


def _grid(table: dict) -> list[list[dict]]:
    placements = [
        {"cell_id": c["cell_id"], **c["origin"], "rowspan": c["rowspan"], "colspan": c["colspan"]}
        for c in table["cells"]
        if c.get("origin")
    ]
    return project(placements, table["row_count"], table["column_count"])


def features(table: dict) -> dict[str, Any]:
    by_id = {c["cell_id"]: c for c in table["cells"]}
    grid = _grid(table)
    texts = [
        [by_id[s["cells"][0]]["direct_text"] if s["kind"] == "origin" else "" for s in row]
        for row in grid
    ]
    kinds = [[s["kind"] for s in row] for row in grid]
    return {
        "table_id": table["table_id"],
        "ordinal": table["ordinal"],
        "depth": table["depth"],
        "dims": (table["row_count"], table["column_count"]),
        "geometry": digest([table["row_count"], table["column_count"], kinds])[:16],
        "text": digest(texts)[:16],
        "header": digest(texts[0] if texts else [])[:16],
        "heading_path": [h["text"] for h in table["heading_path"]],
        "cells": [t for row in texts for t in row],
    }


def _similarity(a: dict, b: dict) -> float:
    if a["dims"] != b["dims"] or not a["cells"]:
        return 0.0
    same = sum(1 for x, y in zip(a["cells"], b["cells"], strict=True) if x == y)
    return same / len(a["cells"])


def match_tables(left: dict, right: dict) -> list[dict]:
    """Correspondence rows between two artifacts of one (page, version)."""
    lf = [features(t) for t in left["tables"]]
    rf = [features(t) for t in right["tables"]]
    out: list[dict] = []
    used_right: set[str] = set()

    # 1. Exact content + geometry. Group identical tables so duplicates are never paired by
    #    text alone.
    groups: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
    for f in lf:
        groups[(f["text"], f["geometry"], f["depth"])][0].append(f)
    for f in rf:
        groups[(f["text"], f["geometry"], f["depth"])][1].append(f)
    matched_left: set[str] = set()
    for (_text, _geo, _depth), (ls, rs) in groups.items():
        if not ls or not rs:
            continue
        if len(ls) == 1 and len(rs) == 1:
            out.append(_row(left, right, ls[0], rs[0], "confirmed", "content+geometry", 1.0))
            matched_left.add(ls[0]["table_id"])
            used_right.add(rs[0]["table_id"])
            continue
        # Identical tables: heading path may tell them apart.
        remaining_l, remaining_r = list(ls), list(rs)
        for fl in ls:
            same_heading = [fr for fr in remaining_r if fr["heading_path"] == fl["heading_path"]]
            peers = [x for x in remaining_l if x["heading_path"] == fl["heading_path"]]
            if len(same_heading) == 1 and len(peers) == 1:
                fr = same_heading[0]
                out.append(_row(left, right, fl, fr, "confirmed", "content+geometry+heading", 1.0))
                remaining_l.remove(fl)
                remaining_r.remove(fr)
                matched_left.add(fl["table_id"])
                used_right.add(fr["table_id"])
        if remaining_l and len(remaining_l) == len(remaining_r):
            for fl, fr in zip(remaining_l, remaining_r, strict=True):
                out.append(_row(left, right, fl, fr, "tentative", "content+geometry+order", 1.0))
                matched_left.add(fl["table_id"])
                used_right.add(fr["table_id"])
        else:
            for fl in remaining_l:
                out.append(
                    _row(
                        left,
                        right,
                        fl,
                        None,
                        "conflicting",
                        "content+geometry",
                        1.0,
                        candidates=[fr["table_id"] for fr in remaining_r],
                    )
                )
                matched_left.add(fl["table_id"])
            used_right.update(fr["table_id"] for fr in remaining_r)

    # 2. Same geometry with mostly equal cell text (rendered macros, user names, etc.).
    pending_left: list[dict] = []
    for fl in lf:
        if fl["table_id"] in matched_left:
            continue
        scored = [
            (fr, _similarity(fl, fr))
            for fr in rf
            if fr["table_id"] not in used_right and fr["geometry"] == fl["geometry"]
        ]
        scored = [(fr, s) for fr, s in scored if s >= SIMILARITY_THRESHOLD]
        best = max((s for _, s in scored), default=0.0)
        top = [fr for fr, s in scored if s == best]
        if len(top) == 1:
            out.append(_row(left, right, fl, top[0], "tentative", "geometry+similar_text", best))
            used_right.add(top[0]["table_id"])
        elif len(top) > 1:
            out.append(
                _row(
                    left,
                    right,
                    fl,
                    None,
                    "conflicting",
                    "geometry+similar_text",
                    best,
                    candidates=[fr["table_id"] for fr in top],
                )
            )
        else:
            pending_left.append(fl)

    # 3. Same geometry and heading path, unique on both sides (macro output that renders
    #    differently, e.g. user mentions). Never more than tentative.
    for fl in pending_left:
        same = [
            fr
            for fr in rf
            if fr["table_id"] not in used_right
            and fr["geometry"] == fl["geometry"]
            and fr["heading_path"] == fl["heading_path"]
        ]
        peers = [
            x
            for x in pending_left
            if x["geometry"] == fl["geometry"] and x["heading_path"] == fl["heading_path"]
        ]
        if len(same) == 1 and len(peers) == 1:
            out.append(
                _row(
                    left,
                    right,
                    fl,
                    same[0],
                    "tentative",
                    "geometry+heading",
                    _similarity(fl, same[0]),
                )
            )
            used_right.add(same[0]["table_id"])
        else:
            out.append(_row(left, right, fl, None, "unmatched", "none", 0.0))
    for fr in rf:
        if fr["table_id"] not in used_right:
            out.append(_row(left, right, None, fr, "unmatched", "none", 0.0))
    return sorted(out, key=lambda r: (r["left_ordinal"] or 10**9, r["right_ordinal"] or 10**9))


def _row(
    left: dict,
    right: dict,
    fl: dict | None,
    fr: dict | None,
    state: str,
    method: str,
    score: float,
    candidates: list[str] | None = None,
) -> dict:
    evidence: dict[str, Any] = {"score": round(score, 4)}
    if fl and fr:
        evidence.update(
            {
                "same_text": fl["text"] == fr["text"],
                "same_geometry": fl["geometry"] == fr["geometry"],
                "same_header": fl["header"] == fr["header"],
                "same_heading_path": fl["heading_path"] == fr["heading_path"],
                "ordinals": [fl["ordinal"], fr["ordinal"]],
            }
        )
    if candidates:
        evidence["candidates"] = candidates
    left_id = fl["table_id"] if fl else None
    right_id = fr["table_id"] if fr else None
    return {
        "correspondence_id": opaque_id(
            "corr", left["artifact_id"], right["artifact_id"], left_id, right_id, ALGORITHM_VERSION
        ),
        "page_id": left["page_id"],
        "version_number": left["version_number"],
        "left_artifact_id": left["artifact_id"],
        "right_artifact_id": right["artifact_id"],
        "left_representation": left["representation"],
        "right_representation": right["representation"],
        "left_table_id": left_id,
        "right_table_id": right_id,
        "left_ordinal": fl["ordinal"] if fl else None,
        "right_ordinal": fr["ordinal"] if fr else None,
        "state": state,
        "method": method,
        "evidence": evidence,
        "algorithm_version": ALGORITHM_VERSION,
    }


def select_for_extraction(
    artifacts: dict[str, dict], rows: list[dict], decisions: dict[str, str] | None = None
) -> list[dict]:
    """One selection per authored table across representations.

    ``artifacts`` maps representation -> artifact of the same (page, version); ``rows`` are
    correspondences between them; ``decisions`` maps correspondence_id -> ``confirmed`` or
    ``distinct`` for human-resolved tentative/conflicting rows.
    """
    decisions = decisions or {}
    # Union-find over occurrences that are (effectively) the same table.
    parent: dict[str, str] = {}
    rep_of: dict[str, str] = {}
    for rep, art in artifacts.items():
        for t in art["tables"]:
            parent[t["table_id"]] = t["table_id"]
            rep_of[t["table_id"]] = rep

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    blocked: dict[str, str] = {}
    for r in rows:
        left, right = r["left_table_id"], r["right_table_id"]
        if left not in parent or (right is not None and right not in parent):
            continue
        decision = decisions.get(r["correspondence_id"])
        state = r["state"]
        if decision == "confirmed":
            state = "confirmed"
        elif decision == "distinct":
            continue
        if state == "confirmed" and right is not None:
            parent[find(left)] = find(right)
        elif state in ("tentative", "conflicting"):
            reason = f"correspondence {r['correspondence_id']} is {state}"
            blocked[left] = reason
            if right is not None:
                blocked[right] = reason
            for c in r["evidence"].get("candidates", []):
                if c in parent:
                    blocked[c] = reason

    clusters: dict[str, list[str]] = defaultdict(list)
    for table_id in parent:
        clusters[find(table_id)].append(table_id)
    selections = []
    for members in clusters.values():
        members.sort(key=lambda t: PREFERENCE.index(rep_of[t]))
        chosen = members[0]
        reps = {rep_of[m] for m in members}
        if rep_of[chosen] == "storage":
            reason = "authored table: storage preferred"
        elif "storage" not in reps and artifacts.get("storage") is not None:
            reason = "render-only table (no storage counterpart): export view preferred"
        else:
            reason = f"{rep_of[chosen]} is the best available representation"
        block = next((blocked[m] for m in members if m in blocked), None)
        selections.append(
            {
                "table_id": chosen,
                "representation": rep_of[chosen],
                "reason": reason,
                "alternates": [{"table_id": m, "representation": rep_of[m]} for m in members[1:]],
                "blocked": block is not None,
                "block_reason": block,
            }
        )
    order = {
        t["table_id"]: (PREFERENCE.index(rep), t["ordinal"])
        for rep, art in artifacts.items()
        for t in art["tables"]
    }
    return sorted(selections, key=lambda s: order[s["table_id"]])


def compare(a: dict, b: dict) -> dict:
    """Side-by-side structural comparison of two table occurrences (any artifacts)."""
    fa, fb = features(a), features(b)
    diffs = []
    if fa["dims"] == fb["dims"]:
        cols = fa["dims"][1]
        for i, (x, y) in enumerate(zip(fa["cells"], fb["cells"], strict=True)):
            if x != y:
                diffs.append({"coord": f"R{i // cols + 1}C{i % cols + 1}", "left": x, "right": y})
    return {
        "left": a["table_id"],
        "right": b["table_id"],
        "dimensions": {"left": list(fa["dims"]), "right": list(fb["dims"])},
        "same_geometry": fa["geometry"] == fb["geometry"],
        "same_text": fa["text"] == fb["text"],
        "same_header": fa["header"] == fb["header"],
        "same_heading_path": fa["heading_path"] == fb["heading_path"],
        "heading_paths": {"left": fa["heading_path"], "right": fb["heading_path"]},
        "similarity": round(_similarity(fa, fb), 4),
        "cell_differences": diffs,
    }
