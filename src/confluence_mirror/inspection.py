"""Mirror navigation and table inspection services behind the ``confex`` CLI.

Everything here reads the mirror and the structural parser only: no HTTP client, no model
client, and (except :func:`parse_pages`) no writes. A table is inspected from its persisted
parse artifact when one exists for the exact capture; otherwise the stored body is parsed in
memory and the result is labeled ``ephemeral_parse`` and never persisted.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

from confluence_mirror import repository as repo
from confluence_mirror.logging_utils import redact
from confluence_mirror.structure import REPRESENTATIONS, ParseConfig, parse_document
from confluence_mirror.structure.document import artifact_identity
from confluence_mirror.structure.grid import project
from confluence_mirror.structure.ids import DEFAULT_INSTANCE_ID, opaque_id

log = logging.getLogger(__name__)

# Exit codes of the confex CLI (spec section 26.2).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_UNAVAILABLE = 4
EXIT_AMBIGUOUS = 5
EXIT_INTERNAL = 6
EXIT_VALIDATION = 7
EXIT_POLICY = 8
EXIT_PARTIAL = 9
EXIT_INTERRUPTED = 130

AUTO_PREFERENCE = ("export_view", "view")
VIEWS = ("overview", "grid", "physical", "cell", "context", "raw", "payload")
_ORDINAL_RE = re.compile(r"^(\d+):t(\d+)$")
_CELL_RE = re.compile(r"^R(\d+)C(\d+)$", re.IGNORECASE)


@dataclass
class ConfexError(Exception):
    code: str
    message: str
    exit_code: int
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def not_found(message: str, **details: Any) -> ConfexError:
    return ConfexError("NOT_FOUND", message, EXIT_NOT_FOUND, details)


# -- spaces ------------------------------------------------------------------------------


def _space(conn: psycopg.Connection, key: str) -> dict:
    space = repo.get_space(conn, key)
    if space is None:
        raise not_found(f"space {key} is not in the mirror", space_key=key)
    return space


def spaces_list(conn, match: str | None, space_type: str | None) -> list[dict]:
    out = []
    for s in repo.list_spaces(conn):
        if space_type and s["space_type"] != space_type:
            continue
        if match and match.lower() not in f"{s['space_key']} {s['name'] or ''}".lower():
            continue
        out.append(
            {
                **s,
                "counts_scope": "non-deleted mirrored pages; export-view tables of "
                "current versions parsed by `confex parse`",
            }
        )
    return out


def space_forest(
    conn, key: str, depth: int | None, match: str | None, counts: bool
) -> dict[str, Any]:
    """All known roots of a space as a forest; unresolved parents grouped separately."""
    space = _space(conn, key)
    pages = repo.space_pages(conn, space["space_id"])
    by_id = {p["page_id"]: p for p in pages}
    children: dict[int, list[dict]] = {}
    roots: list[dict] = []
    unresolved: list[dict] = []
    for p in pages:
        if p["parent_id"] is None:
            roots.append(p)  # the API reported no ancestors: a verified root
        elif p["parent_id"] in by_id:
            children.setdefault(p["parent_id"], []).append(p)
        else:
            unresolved.append(p)  # parent not mirrored (restricted, other space, or later)

    matched: set[int] | None = None
    if match:
        needle = match.lower()
        matched = {p["page_id"] for p in pages if needle in p["title"].lower()}

    reached: set[int] = set()
    sizes: dict[int, int] = {}

    def subtree_size(pid: int, seen: frozenset[int]) -> int:
        if pid in sizes:
            return sizes[pid]
        total = 0
        for c in children.get(pid, []):
            if c["page_id"] not in seen:
                total += 1 + subtree_size(c["page_id"], seen | {c["page_id"]})
        sizes[pid] = total
        return total

    def build(page: dict, d: int, seen: frozenset[int]) -> dict | None:
        pid = page["page_id"]
        reached.add(pid)
        kids = []
        hidden = 0
        for c in children.get(pid, []):
            if c["page_id"] in seen:
                continue
            if depth is not None and d >= depth:
                hidden += 1
                continue
            node = build(c, d + 1, seen | {c["page_id"]})
            if node is not None:
                kids.append(node)
        is_match = matched is None or pid in matched
        if matched is not None and not is_match and not kids:
            return None
        node = {
            "page_id": pid,
            "title": page["title"],
            "depth": d,
            "children": kids,
            "hidden_children": hidden,
        }
        if matched is not None:
            node["role"] = "match" if is_match else "context"
        if counts:
            node["descendants_known"] = subtree_size(pid, frozenset({pid}))
        return node

    forest = [n for r in roots if (n := build(r, 0, frozenset({r["page_id"]}))) is not None]
    orphans = [n for u in unresolved if (n := build(u, 0, frozenset({u["page_id"]}))) is not None]
    diagnostics = []
    in_cycle = [p["page_id"] for p in pages if p["page_id"] not in reached]
    if in_cycle:
        diagnostics.append(
            {
                "code": "HIERARCHY_CYCLE",
                "severity": "warning",
                "message": f"{len(in_cycle)} page(s) are only reachable through a parent cycle",
                "ref": {"page_ids": in_cycle[:50]},
            }
        )
    return {
        "space_key": key,
        "depth_limit": depth,
        "match": match,
        "roots": forest,
        "unresolved_parent": orphans,
        "known_pages": len(pages),
        "diagnostics": diagnostics,
    }


def space_show(conn, key: str) -> dict:
    space = _space(conn, key)
    forest = space_forest(conn, key, depth=0, match=None, counts=False)
    pages = repo.space_pages(conn, space["space_id"])
    return {
        **{k: v for k, v in space.items() if k != "space_id"},
        "known_pages": len(pages),
        "known_max_depth": max((p["depth"] for p in pages), default=None),
        "roots": [{"page_id": r["page_id"], "title": r["title"]} for r in forest["roots"]],
        "unresolved_parent_pages": len(forest["unresolved_parent"]),
        "warnings": (
            [] if space["last_successful_sync_at"] else ["space has no successful sync yet"]
        ),
    }


# -- pages ---------------------------------------------------------------------------------


def _page(conn, page_id: int) -> dict:
    page = repo.get_page(conn, page_id)
    if page is None:
        raise not_found(f"page {page_id} is not in the mirror", page_id=page_id)
    return page


def page_ancestors(conn, page_id: int) -> dict:
    _page(conn, page_id)
    chain = repo.page_ancestors(conn, page_id)
    diagnostics = []
    if chain[-1]["cycle"]:
        diagnostics.append(
            {"code": "HIERARCHY_CYCLE", "severity": "warning", "message": "parent cycle detected"}
        )
    top = chain[-1]
    unresolved = top["parent_id"] if top["parent_id"] is not None and not top["cycle"] else None
    if unresolved is not None:
        diagnostics.append(
            {
                "code": "SCOPE_INCOMPLETE",
                "severity": "warning",
                "message": f"ancestor {unresolved} is not in the mirror; the chain is partial",
            }
        )
    ancestors = [
        {"page_id": r["page_id"], "title": r["title"]}
        for r in reversed(chain[1:])
        if not r["cycle"]
    ]
    return {
        "page": {"page_id": chain[0]["page_id"], "title": chain[0]["title"]},
        "ancestors": ancestors,  # root first, parent last
        "unresolved_parent_id": unresolved,
        "diagnostics": diagnostics,
    }


def page_show(conn, page_id: int) -> dict:
    page = _page(conn, page_id)
    version = page["current_version"]
    content = repo.stored_bodies(conn, page_id)
    available = [r for r in REPRESENTATIONS if content["bodies"][r] is not None] if content else []
    artifacts = repo.page_artifacts(conn, page_id, version)
    counts = repo.page_summary_counts(conn, page_id)
    parsed = repo.latest_artifacts(conn, page_id, version)
    doc = next((parsed[r] for r in AUTO_PREFERENCE if r in parsed), None)
    counts["macros"] = sum(n["kind"] == "macro" for n in doc["nodes"]) if doc else None
    counts["links"] = len(doc.get("links", [])) if doc else None
    return {
        **{k: v for k, v in page.items() if k not in ("space_id", "ancestor_ids")},
        "breadcrumbs": page_ancestors(conn, page_id)["ancestors"],
        "representations_available": available,
        "body_fetched_at": content["fetched_at"] if content else None,
        "parse_artifacts": artifacts,
        "known_tables": {a["representation"]: a["table_count"] for a in artifacts} or None,
        **counts,
    }


def page_subtree(conn, root_id: int, depth: int) -> dict:
    _page(conn, root_id)
    rows = repo.page_subtree(conn, root_id, depth)
    nodes = {r["page_id"]: {**r, "children": []} for r in rows if not r["cycle"]}
    root = None
    for r in rows:
        if r["cycle"]:
            continue
        node = nodes[r["page_id"]]
        node["hidden_children"] = r["child_count"] if r["depth"] == depth else 0
        if r["page_id"] == root_id:
            root = node
        elif r["parent_id"] in nodes:
            nodes[r["parent_id"]]["children"].append(node)
    for node in nodes.values():
        for key in ("child_count", "cycle", "parent_id", "position"):
            node.pop(key, None)
    return {
        "root": root,
        "depth_limit": depth,
        "diagnostics": (
            [{"code": "HIERARCHY_CYCLE", "severity": "warning", "message": "cycle detected"}]
            if any(r["cycle"] for r in rows)
            else []
        ),
    }


# -- artifacts ------------------------------------------------------------------------------


@dataclass
class Resolved:
    artifact: dict
    parse_mode: str  # persisted | ephemeral_parse
    reason: str
    fetched_at: Any = None


def _artifact_for(
    conn, page_id: int, version: int, representation: str, body: str, config: ParseConfig
) -> tuple[dict, str]:
    ident = artifact_identity(page_id, version, representation, body, config=config)
    stored = repo.load_artifact(conn, ident["artifact_id"])
    if stored is not None:
        return stored, "persisted"
    return (
        parse_document(
            body, page_id=page_id, version=version, representation=representation, config=config
        ),
        "ephemeral_parse",
    )


def _content(conn, page_id: int, version: int | None) -> dict:
    page = _page(conn, page_id)
    content = repo.stored_bodies(conn, page_id, version)
    if content is None:
        raise ConfexError(
            "REPRESENTATION_UNAVAILABLE",
            f"no stored body for page {page_id} version {version or page['current_version']}",
            EXIT_UNAVAILABLE,
            {
                "available_versions": [
                    v["version_number"] for v in repo.content_versions(conn, page_id)
                ]
            },
        )
    return content


def _snapshot_content(conn, page_id: int, snapshot: str, config: ParseConfig) -> tuple[dict, str]:
    for v in repo.content_versions(conn, page_id):
        content = repo.stored_bodies(conn, page_id, v["version_number"])
        for rep in v["representations"]:
            ident = artifact_identity(
                page_id, v["version_number"], rep, content["bodies"][rep], config=config
            )
            if ident["snapshot_id"] == snapshot:
                return content, rep
    for old in repo.superseded_bodies(conn, page_id):
        rep = old["representation"]
        ident = artifact_identity(page_id, old["version_number"], rep, old["body"], config=config)
        if ident["snapshot_id"] == snapshot:
            bodies = {r: None for r in REPRESENTATIONS} | {rep: old["body"]}
            return {
                "page_id": page_id,
                "version_number": old["version_number"],
                "bodies": bodies,
                "fetched_at": old["captured_at"],
                "superseded": True,
            }, rep
    raise not_found(
        f"snapshot {snapshot} is not a stored capture of page {page_id}", snapshot_id=snapshot
    )


def page_artifact(
    conn,
    page_id: int,
    representation: str,
    version: int | None = None,
    config: ParseConfig | None = None,
) -> Resolved:
    """The document artifact for a rendered body; ``auto`` prefers export view, then view."""
    config = config or ParseConfig()
    content = _content(conn, page_id, version)
    bodies = content["bodies"]
    if representation == "auto":
        chosen = next((r for r in AUTO_PREFERENCE if bodies[r] is not None), None)
        if chosen is None:
            raise ConfexError("REPRESENTATION_UNAVAILABLE", "no body stored", EXIT_UNAVAILABLE)
        reason = f"auto: first available of {', '.join(AUTO_PREFERENCE)}"
    else:
        if bodies.get(representation) is None:
            raise ConfexError(
                "REPRESENTATION_UNAVAILABLE",
                f"representation {representation} is not stored for page {page_id} "
                f"version {content['version_number']}",
                EXIT_UNAVAILABLE,
                {"available": [r for r in REPRESENTATIONS if bodies[r] is not None]},
            )
        chosen, reason = representation, "requested"
    artifact, mode = _artifact_for(
        conn, page_id, content["version_number"], chosen, bodies[chosen], config
    )
    return Resolved(artifact, mode, reason, content["fetched_at"])


def resolve_table(
    conn,
    selector: str,
    *,
    representation: str = "auto",
    version: int | None = None,
    snapshot: str | None = None,
    config: ParseConfig | None = None,
) -> tuple[Resolved, dict]:
    config = config or ParseConfig()
    if selector.startswith("tbl_"):
        occ = repo.table_occurrence(conn, selector)
        if occ is None:
            raise not_found(
                f"table {selector} is not persisted; run `confex parse` or use PAGE:tNN",
                table_id=selector,
            )
        conflicts = []
        if representation not in ("auto", occ["representation"]):
            conflicts.append("--representation")
        if version is not None and version != occ["version_number"]:
            conflicts.append("--version")
        artifact = repo.load_artifact(conn, occ["artifact_id"])
        if snapshot is not None and snapshot != artifact["snapshot_id"]:
            conflicts.append("--snapshot")
        if conflicts:
            raise ConfexError(
                "ARGUMENT_CONFLICT",
                f"{', '.join(conflicts)} contradicts persistent table ID {selector}",
                EXIT_USAGE,
            )
        table = next(t for t in artifact["tables"] if t["table_id"] == selector)
        return Resolved(artifact, "persisted", "persistent table id"), table

    match = _ORDINAL_RE.match(selector)
    if not match:
        raise ConfexError(
            "INVALID_SELECTOR", f"expected tbl_<id> or PAGE:tNN, got {selector!r}", EXIT_USAGE
        )
    page_id, ordinal = int(match.group(1)), int(match.group(2))
    if snapshot is not None:
        if version is not None:
            raise ConfexError(
                "ARGUMENT_CONFLICT", "--snapshot and --version are exclusive", EXIT_USAGE
            )
        content, rep = _snapshot_content(conn, page_id, snapshot, config)
        if representation not in ("auto", rep):
            raise ConfexError(
                "ARGUMENT_CONFLICT", f"snapshot {snapshot} is a {rep} capture", EXIT_USAGE
            )
        candidates = [rep]
    else:
        content = _content(conn, page_id, version)
        if representation == "auto":
            candidates = [r for r in REPRESENTATIONS if content["bodies"][r] is not None]
        elif content["bodies"].get(representation) is None:
            raise ConfexError(
                "REPRESENTATION_UNAVAILABLE",
                f"representation {representation} is not stored for page {page_id} "
                f"version {content['version_number']}",
                EXIT_UNAVAILABLE,
                {"available": [r for r in REPRESENTATIONS if content["bodies"][r] is not None]},
            )
        else:
            candidates = [representation]

    found: list[tuple[Resolved, dict]] = []
    for rep in candidates:
        artifact, mode = _artifact_for(
            conn, page_id, content["version_number"], rep, content["bodies"][rep], config
        )
        table = next((t for t in artifact["tables"] if t["ordinal"] == ordinal), None)
        if table is not None:
            reason = "requested" if len(candidates) == 1 else "auto: only eligible occurrence"
            found.append((Resolved(artifact, mode, reason, content["fetched_at"]), table))
    if not found:
        raise not_found(
            f"page {page_id} version {content['version_number']} has no table t{ordinal:02d} "
            f"in {', '.join(candidates) or 'any stored representation'}",
            page_id=page_id,
        )
    if len(found) > 1:
        raise ConfexError(
            "AMBIGUOUS_SELECTOR",
            f"{selector} exists in several representations; ordinals are not matched across "
            "representations",
            EXIT_AMBIGUOUS,
            {
                "choices": [
                    {
                        "selector": f"{selector} --representation {r.artifact['representation']}",
                        "table_id": t["table_id"],
                        "snapshot_id": r.artifact["snapshot_id"],
                    }
                    for r, t in found
                ]
            },
        )
    return found[0]


# -- table views --------------------------------------------------------------------------


def _labels(table: dict) -> dict[str, str]:
    """Display-only short labels (c1, c2, ...) for physical cells in source order."""
    return {c["cell_id"]: f"c{i + 1}" for i, c in enumerate(table["cells"])}


def table_grid(table: dict) -> list[list[dict]]:
    placements = [
        {"cell_id": c["cell_id"], **c["origin"], "rowspan": c["rowspan"], "colspan": c["colspan"]}
        for c in table["cells"]
        if c.get("origin")
    ]
    return project(placements, table["row_count"], table["column_count"])


def parse_range(spec: str | None, total: int, default: int | None) -> tuple[int, int] | None:
    """Inclusive 1-based range; None means "everything"."""
    if spec is None:
        if default is None or total <= default:
            return None
        return (1, default)
    if spec == "all":
        return None
    try:
        if ":" in spec:
            lo_s, hi_s = spec.split(":", 1)
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else total
        else:
            lo = hi = int(spec)
    except ValueError:
        raise ConfexError(
            "INVALID_RANGE", f"bad range {spec!r}; use A:B or all", EXIT_USAGE
        ) from None
    if lo < 1 or hi < lo:
        raise ConfexError("INVALID_RANGE", f"bad range {spec!r}", EXIT_USAGE)
    return (lo, min(hi, total))


def _cell_text(artifact: dict, cell: dict) -> list[dict]:
    out = []
    for item in cell.get("content", []):
        if item["kind"] == "text":
            block = artifact["blocks"][item["block_id"]]
            out.append(
                {**block, "kind": "text", "block_kind": block["kind"], "block_id": item["block_id"]}
            )
        else:
            out.append(item)
    return out


def _block(artifact: dict, block_id: str | None) -> dict | None:
    if block_id is None:
        return None
    return {"block_id": block_id, **artifact["blocks"][block_id]}


def identity(resolved: Resolved, table: dict) -> dict:
    a = resolved.artifact
    return {
        "table_id": table["table_id"],
        "ordinal_selector": f"{a['page_id']}:t{table['ordinal']:02d}",
        "artifact_id": a["artifact_id"],
        "snapshot_id": a["snapshot_id"],
        "instance_id": a.get("instance_id", DEFAULT_INSTANCE_ID),
        "page_id": a["page_id"],
        "version_number": a["version_number"],
        "representation": a["representation"],
        "representation_reason": resolved.reason,
        "body_sha256": a["body_sha256"],
        "parser_version": a["parser_version"],
        "parse_mode": resolved.parse_mode,
    }


def table_view(
    conn,
    resolved: Resolved,
    table: dict,
    view: str,
    *,
    cell: str | None = None,
    rows: str | None = None,
    cols: str | None = None,
    bounded: bool = False,
) -> dict:
    """Data for one ``tables show`` view. ``bounded`` applies the terminal display limits."""
    a = resolved.artifact
    base = {"identity": identity(resolved, table)}
    labels = _labels(table)
    if view == "overview":
        return {
            **base,
            "heading_path": table["heading_path"],
            "containers": table["containers"],
            "dimensions": {"rows": table["row_count"], "columns": table["column_count"]},
            "depth": table["depth"],
            "parent_table_id": table["parent_table_id"],
            "parent_cell_id": table["parent_cell_id"],
            "child_table_ids": table["child_table_ids"],
            "row_groups": [
                {"id": g["id"], "kind": g["kind"], "rows": len(g["rows"])}
                for g in table["row_groups"]
            ],
            "row_order_policy": table["row_order_policy"],
            "geometry_status": table["geometry_status"],
            "extraction_eligible": table["extraction_eligible"],
            "artifact_status": a["status"],
            "diagnostics": table["diagnostics"],
        }
    if view == "grid":
        slots = table_grid(table)
        r_range = parse_range(rows, table["row_count"], 50 if bounded else None)
        c_range = parse_range(cols, table["column_count"], 12 if bounded else None)
        r_lo, r_hi = r_range or (1, table["row_count"])
        c_lo, c_hi = c_range or (1, table["column_count"])
        by_id = {c["cell_id"]: c for c in table["cells"]}
        out_rows = []
        for r in range(r_lo, r_hi + 1):
            line = []
            for c in range(c_lo, c_hi + 1):
                slot = slots[r - 1][c - 1]
                entry = {"coord": f"R{r}C{c}", "kind": slot["kind"], "cells": slot["cells"]}
                entry["labels"] = [labels[x] for x in slot["cells"]]
                if slot["kind"] == "origin":
                    owner = by_id[slot["cells"][0]]
                    entry["text"] = owner["direct_text"]
                    entry["nested"] = [
                        x["table_id"] for x in owner.get("content", []) if x["kind"] == "table"
                    ]
                line.append(entry)
            out_rows.append(line)
        return {
            **base,
            "geometry_status": table["geometry_status"],
            "dimensions": {"rows": table["row_count"], "columns": table["column_count"]},
            "selection": {
                "rows": [r_lo, r_hi],
                "cols": [c_lo, c_hi],
                "complete": r_range is None and c_range is None,
                "hidden_rows": table["row_count"] - (r_hi - r_lo + 1),
                "hidden_cols": table["column_count"] - (c_hi - c_lo + 1),
            },
            "grid": out_rows,
            "diagnostics": table["diagnostics"],
        }
    if view == "physical":
        by_row: dict[int, list[dict]] = {}
        for c in table["cells"]:
            by_row.setdefault(c["row_index"], []).append(
                {
                    "label": labels[c["cell_id"]],
                    **{k: v for k, v in c.items() if k != "content"},
                    "blocks": _cell_text(a, c),
                }
            )
        return {
            **base,
            "row_groups": table["row_groups"],
            "rows": [
                {
                    "row_index": r["row_index"],
                    "group_id": r["group_id"],
                    "cells": by_row.get(r["row_index"], []),
                }
                for r in table["rows"]
            ],
            "diagnostics": table["diagnostics"],
        }
    if view == "cell":
        if not cell:
            raise ConfexError("MISSING_ARGUMENT", "--view cell needs --cell RnCm", EXIT_USAGE)
        m = _CELL_RE.match(cell)
        if not m:
            raise ConfexError("INVALID_CELL", f"expected RnCm, got {cell!r}", EXIT_USAGE)
        r, c = int(m.group(1)), int(m.group(2))
        if not (1 <= r <= table["row_count"] and 1 <= c <= table["column_count"]):
            raise not_found(
                f"{cell.upper()} is outside the {table['row_count']}x{table['column_count']} grid"
            )
        slot = table_grid(table)[r - 1][c - 1]
        by_id = {x["cell_id"]: x for x in table["cells"]}
        owners = []
        for cid in slot["cells"]:
            owner = by_id[cid]
            owners.append(
                {
                    "label": labels[cid],
                    **{k: v for k, v in owner.items() if k != "content"},
                    "blocks": _cell_text(a, owner),
                    "span": f"R{owner['origin']['row']}C{owner['origin']['col']}:"
                    f"R{owner['origin']['row'] + owner['rowspan'] - 1}"
                    f"C{owner['origin']['col'] + owner['colspan'] - 1}",
                }
            )
        return {
            **base,
            "coord": f"R{r}C{c}",
            "kind": slot["kind"],
            "owners": owners,
            "heading_path": table["heading_path"],
            "diagnostics": [
                d
                for d in table["diagnostics"]
                if (d.get("ref") or {}).get("cell_id") in slot["cells"]
            ],
        }
    if view == "context":
        page = repo.get_page(conn, a["page_id"])
        ctx = table["context"]
        return {
            **base,
            "page": {
                "page_id": a["page_id"],
                "title": page["title"] if page else None,
                "space_key": page["space_key"] if page else None,
                "current_version": page["current_version"] if page else None,
            },
            "heading_path": table["heading_path"],
            "local_heading_count": table["local_heading_count"],
            "containers": table["containers"],
            "caption": _block(a, ctx["caption_block_id"]),
            "preceding": ctx["preceding"]
            and {**ctx["preceding"], "block": _block(a, ctx["preceding"]["block_id"])},
            "following": ctx["following"]
            and {**ctx["following"], "block": _block(a, ctx["following"]["block_id"])},
            "budget_chars": ctx["budget_chars"],
        }
    if view == "raw":
        return {
            **base,
            "kind": "dom_serialization",
            "byte_original": False,
            "note": "serialized from the parsed tree; not a byte-exact slice of the stored body",
            "markup": table["source_markup"],
        }
    raise ConfexError("UNSUPPORTED_MODE", f"unknown view {view!r}", EXIT_USAGE)


def outline(resolved: Resolved, include_tables: bool, include_macros: bool) -> list[dict]:
    kinds = (
        {"heading"}
        | ({"table"} if include_tables else set())
        | ({"macro"} if include_macros else set())
    )
    nodes = resolved.artifact["nodes"]
    by_id = {n["id"]: n for n in nodes}
    tables = {t["table_id"]: t for t in resolved.artifact["tables"]}
    cell_table = {c["cell_id"]: t["table_id"] for t in tables.values() for c in t["cells"]}
    out = []
    for n in nodes:
        if n["kind"] not in kinds:
            continue
        depth = 0
        parent = n["container_id"]
        while parent is not None:
            depth += 1
            holder = by_id.get(parent)
            if holder is None:  # a table cell: continue from its table node
                holder = by_id.get(cell_table.get(parent, ""))
            parent = holder["container_id"] if holder else None
        item = {k: n[k] for k in ("id", "kind", "order", "container_id", "cell_id")}
        item["container_depth"] = depth
        if n["kind"] == "heading":
            item.update(level=n["level"], text=n["text"])
        elif n["kind"] == "table":
            t = tables[n["id"]]
            item.update(
                ordinal=t["ordinal"],
                selector=f"{resolved.artifact['page_id']}:t{t['ordinal']:02d}",
                rows=t["row_count"],
                columns=t["column_count"],
                geometry_status=t["geometry_status"],
            )
        else:
            item["name"] = n["name"]
        out.append(item)
    return out


def table_summaries(resolved: Resolved) -> list[dict]:
    a = resolved.artifact
    return [
        {
            "selector": f"{a['page_id']}:t{t['ordinal']:02d}",
            "table_id": t["table_id"],
            "depth": t["depth"],
            "parent_table_id": t["parent_table_id"],
            "rows": t["row_count"],
            "columns": t["column_count"],
            "geometry_status": t["geometry_status"],
            "extraction_eligible": t["extraction_eligible"],
            "heading_path": [h["text"] for h in t["heading_path"]],
        }
        for t in a["tables"]
    ]


# -- parse (writes derived artifacts) -----------------------------------------------------


def parse_pages(
    conn,
    *,
    space_key: str | None,
    page_ids: list[int] | None,
    representations: list[str],
    all_versions: bool,
    config: ParseConfig | None = None,
) -> dict:
    """Persist structural artifacts for stored bodies (offline). One transaction per body."""
    config = config or ParseConfig()
    space_id = _space(conn, space_key)["space_id"] if space_key else None
    targets = repo.parse_targets(conn, space_id, page_ids, all_versions)
    conn.rollback()
    stats = {"parsed": 0, "reused": 0, "tables": 0, "failed": 0, "not_stored": 0, "by_status": {}}
    errors = []
    for page_id, version in targets:
        content = repo.stored_bodies(conn, page_id, version)
        conn.rollback()
        for rep in representations:
            body = content["bodies"][rep] if content else None
            if body is None:
                stats["not_stored"] += 1
                continue
            try:
                ident = artifact_identity(page_id, version, rep, body, config=config)
                with conn.transaction():
                    if repo.artifact_exists(conn, ident["artifact_id"]):
                        stats["reused"] += 1
                        continue
                    artifact = parse_document(
                        body, page_id=page_id, version=version, representation=rep, config=config
                    )
                    if repo.insert_parse_artifact(conn, artifact) and rep == "export_view":
                        repo.insert_search_blocks(conn, search_rows(artifact))
                conn.commit()
                stats["parsed"] += 1
                stats["tables"] += len(artifact["tables"])
                stats["by_status"][artifact["status"]] = (
                    stats["by_status"].get(artifact["status"], 0) + 1
                )
            except Exception as exc:  # one bad body must not stop the rest
                conn.rollback()
                stats["failed"] += 1
                message = redact(f"{type(exc).__name__}: {exc}")
                errors.append(
                    {
                        "page_id": page_id,
                        "version": version,
                        "representation": rep,
                        "error": message,
                    }
                )
                log.error("parse failed for page %s v%s %s: %s", page_id, version, rep, message)
    return {**stats, "targets": len(targets), "errors": errors[:50]}


def search_rows(artifact: dict) -> list[dict]:
    """Search-projection rows for an artifact: flow text, headings and cell text."""
    cell_table = {c["cell_id"]: t["table_id"] for t in artifact["tables"] for c in t["cells"]}
    section_of = {n["block_id"]: n["section_id"] for n in artifact["nodes"] if n.get("block_id")}
    rows = []
    for block_id, block in artifact["blocks"].items():
        if not block["normalized_text"] or block["kind"] in ("caption",):
            continue
        cell_id = block.get("cell_id")
        if block["kind"] != "cell_text" and block_id not in section_of:
            continue  # context-only blocks duplicate flow nodes
        rows.append(
            {
                "artifact_id": artifact["artifact_id"],
                "block_id": block_id,
                "page_id": artifact["page_id"],
                "version_number": artifact["version_number"],
                "block_kind": block["kind"],
                "section_id": section_of.get(block_id),
                "table_id": cell_table.get(cell_id) if cell_id else None,
                "cell_id": cell_id,
                "text": block["normalized_text"],
            }
        )
    return rows


# -- page metadata ---------------------------------------------------------------------


def page_labels(conn, page_id: int) -> list[dict]:
    _page(conn, page_id)
    return repo.page_labels(conn, page_id)


def page_properties(conn, page_id: int, key: str | None) -> list[dict]:
    _page(conn, page_id)
    rows = repo.page_properties(conn, page_id, key)
    if key and not rows:
        raise not_found(f"page {page_id} has no mirrored property {key!r}")
    return rows


def page_attachments(conn, page_id: int, include_deleted: bool) -> dict:
    _page(conn, page_id)
    return {
        "attachments": repo.page_attachments(conn, page_id, include_deleted),
        "note": "metadata only; attachment bodies are not downloaded or parsed",
    }


def page_versions(conn, page_id: int) -> dict:
    page = _page(conn, page_id)
    return {
        "current_version": page["current_version"],
        "versions": repo.page_version_rows(conn, page_id),
        "note": "versions without body_stored are known from metadata only",
    }


def _link_target(conn, link: dict) -> dict:
    """Resolve a classified page link against the mirror (by ID, else space and title)."""
    page = None
    if link.get("page_id") is not None:
        page = repo.get_page(conn, link["page_id"])
    elif link.get("space_key") and link.get("title"):
        page = repo.page_by_title(conn, link["space_key"], link["title"])
    if page is None:
        return {"target_page_id": link.get("page_id"), "resolution": "unresolved"}
    return {
        "target_page_id": page["page_id"],
        "target_title": page["title"],
        "target_space_key": page["space_key"],
        "resolution": "deleted" if page["is_deleted"] else "resolved",
    }


def page_links(conn, page_id: int, direction: str) -> dict:
    """Links in the rendered page (out) and links to it from parsed rendered pages (in)."""
    page = _page(conn, page_id)
    out: dict[str, Any] = {"direction": direction}
    if direction in ("out", "both"):
        resolved = page_artifact(conn, page_id, "auto")
        links = []
        for i, link in enumerate(resolved.artifact.get("links", []), 1):
            item = {"link_index": i, **link}
            if link["kind"] == "page":
                item.update(_link_target(conn, link))
            links.append(item)
        out["representation"] = resolved.artifact["representation"]
        out["outbound"] = links
    if direction in ("in", "both"):
        out["inbound"] = repo.inbound_links(conn, page_id, page["space_key"], page["title"])
        out["inbound_coverage"] = (
            "links in the current version of mirrored pages that `confex parse` has parsed; "
            "not a complete backlink index"
        )
    return out


# -- reading -------------------------------------------------------------------------------


def _section_nodes(artifact: dict, section_id: str | None) -> list[dict]:
    """Document-flow nodes of the whole page or of one section (with its subsections)."""
    nodes = [n for n in artifact["nodes"] if n["cell_id"] is None]
    if section_id is None:
        return nodes
    if section_id == _preamble_id(artifact):
        out = []
        for n in nodes:
            if n["kind"] == "heading" and n["container_id"] is None:
                break
            out.append(n)
        return out
    start = next((i for i, n in enumerate(nodes) if n["id"] == section_id), None)
    if start is None or nodes[start]["kind"] != "heading":
        raise not_found(f"section {section_id} is not in artifact {artifact['artifact_id']}")
    head = nodes[start]
    out = [head]
    for n in nodes[start + 1 :]:
        if (
            n["kind"] == "heading"
            and n["container_id"] == head["container_id"]
            and n["level"] <= head["level"]
        ):
            break
        out.append(n)
    return out


def read_text(resolved: Resolved, section_id: str | None) -> dict:
    """Page text in source order; tables and macros appear as references, not prose."""
    artifact = resolved.artifact
    tables = {t["table_id"]: t for t in artifact["tables"]}
    items = []
    for n in _section_nodes(artifact, section_id):
        item: dict[str, Any] = {"kind": n["kind"], "id": n["id"], "section_id": n["section_id"]}
        if n["kind"] == "heading":
            item.update(level=n["level"], text=n["text"])
        elif n["kind"] == "table":
            t = tables[n["id"]]
            item.update(
                selector=f"{artifact['page_id']}:t{t['ordinal']:02d}",
                rows=t["row_count"],
                columns=t["column_count"],
            )
        elif n["kind"] in ("macro", "layout_cell"):
            item["name"] = n.get("name")
        else:
            item["text"] = artifact["blocks"][n["block_id"]]["evidence_text"]
            item["block_id"] = n["block_id"]
        items.append(item)
    return {
        "representation": artifact["representation"],
        "representation_reason": resolved.reason,
        "parse_mode": resolved.parse_mode,
        "section_id": section_id,
        "preamble_section_id": _preamble_id(artifact),
        "blocks": items,
    }


def _preamble_id(artifact: dict) -> str:
    return opaque_id("sec", artifact["artifact_id"], "preamble")


def read_raw(conn, page_id: int, representation: str, version: int | None) -> dict:
    content = _content(conn, page_id, version)
    rep = representation
    if rep == "auto":
        rep = next((r for r in AUTO_PREFERENCE if content["bodies"][r] is not None), None)
    body = content["bodies"].get(rep) if rep else None
    if body is None:
        raise ConfexError(
            "REPRESENTATION_UNAVAILABLE",
            f"representation {representation} is not stored for page {page_id}",
            EXIT_UNAVAILABLE,
        )
    return {
        "representation": rep,
        "version_number": content["version_number"],
        "kind": "stored_body_value",
        "note": "the decoded REST body value as stored; not the HTTP transfer bytes",
        "body": body,
    }


# -- diff -----------------------------------------------------------------------------------


def _capture(
    conn,
    page_id: int,
    snapshot: str | None,
    version: int | None,
    representation: str,
    config: ParseConfig,
) -> Resolved:
    if snapshot:
        content, rep = _snapshot_content(conn, page_id, snapshot, config)
        artifact, mode = _artifact_for(
            conn, page_id, content["version_number"], rep, content["bodies"][rep], config
        )
        return Resolved(artifact, mode, "snapshot", content["fetched_at"])
    return page_artifact(conn, page_id, representation, version, config)


def _table_signature(t: dict) -> tuple:
    return (tuple(h["text"] for h in t["heading_path"]), t["column_count"], t["depth"])


def _table_texts(artifact: dict, t: dict) -> list[list[str]]:
    by_id = {c["cell_id"]: c for c in t["cells"]}
    grid = table_grid(t)
    return [
        [
            by_id[s["cells"][0]]["direct_text"] if s["kind"] == "origin" else f"<{s['kind']}>"
            for s in row
        ]
        for row in grid
    ]


def diff_pages(
    conn,
    page_id: int,
    *,
    from_snapshot: str | None = None,
    to_snapshot: str | None = None,
    from_version: int | None = None,
    to_version: int | None = None,
    representation: str = "auto",
    config: ParseConfig | None = None,
) -> dict:
    config = config or ParseConfig()
    if not (from_snapshot or from_version) or not (to_snapshot or to_version):
        raise ConfexError(
            "MISSING_ARGUMENT",
            "give --from-snapshot/--from-version and --to-snapshot/--to-version",
            EXIT_USAGE,
        )
    a = _capture(conn, page_id, from_snapshot, from_version, representation, config).artifact
    b = _capture(conn, page_id, to_snapshot, to_version, representation, config).artifact
    if a["representation"] != b["representation"]:
        raise ConfexError(
            "ARGUMENT_CONFLICT", "both captures must use the same representation", EXIT_USAGE
        )

    meta_a = repo.version_metadata(conn, page_id, a["version_number"]) or {}
    meta_b = repo.version_metadata(conn, page_id, b["version_number"]) or {}
    metadata = {
        k: {"from": meta_a.get(k), "to": meta_b.get(k)}
        for k in ("title", "parent_id")
        if meta_a.get(k) != meta_b.get(k)
    }

    def heads(art: dict) -> list[str]:
        return [
            f"h{n['level']} {n['text']}"
            for n in art["nodes"]
            if n["kind"] == "heading" and n["cell_id"] is None
        ]

    def flow(art: dict) -> list[str]:
        return [
            art["blocks"][n["block_id"]]["normalized_text"]
            for n in art["nodes"]
            if n.get("block_id") and n["kind"] != "heading" and n["cell_id"] is None
        ]

    def changes(x: list[str], y: list[str]) -> dict:
        added, removed = [], []
        for line in difflib.ndiff(x, y):
            if line.startswith("+ "):
                added.append(line[2:])
            elif line.startswith("- "):
                removed.append(line[2:])
        return {"added": added, "removed": removed}

    tables = []
    unmatched_b = list(b["tables"])
    for ta in a["tables"]:
        sig = _table_signature(ta)
        same = [tb for tb in unmatched_b if _table_signature(tb) == sig]
        if len(same) == 1:
            tb = same[0]
            unmatched_b.remove(tb)
            ga, gb = _table_texts(a, ta), _table_texts(b, tb)
            cell_changes = []
            if (ta["row_count"], ta["column_count"]) == (tb["row_count"], tb["column_count"]):
                for r, (ra, rb) in enumerate(zip(ga, gb, strict=True), start=1):
                    for c, (x, y) in enumerate(zip(ra, rb, strict=True), start=1):
                        if x != y:
                            cell_changes.append({"coord": f"R{r}C{c}", "from": x, "to": y})
            tables.append(
                {
                    "match": "candidate",
                    "method": "heading_path+columns+depth",
                    "from": ta["table_id"],
                    "to": tb["table_id"],
                    "dimensions": {
                        "from": [ta["row_count"], ta["column_count"]],
                        "to": [tb["row_count"], tb["column_count"]],
                    },
                    "cell_changes": cell_changes,
                }
            )
        else:
            tables.append(
                {
                    "match": "ambiguous" if same else "removed_or_changed",
                    "from": ta["table_id"],
                    "candidates": [t["table_id"] for t in same],
                }
            )
    tables += [{"match": "added_or_changed", "to": tb["table_id"]} for tb in unmatched_b]
    return {
        "from": {
            "snapshot_id": a["snapshot_id"],
            "version_number": a["version_number"],
            "artifact_id": a["artifact_id"],
        },
        "to": {
            "snapshot_id": b["snapshot_id"],
            "version_number": b["version_number"],
            "artifact_id": b["artifact_id"],
        },
        "representation": a["representation"],
        "identical_body": a["body_sha256"] == b["body_sha256"],
        "metadata": metadata,
        "headings": changes(heads(a), heads(b)),
        "blocks": changes(flow(a), flow(b)),
        "tables": tables,
        "note": "table matches are candidates, never established by ordinal",
    }


# -- search ----------------------------------------------------------------------------------


def search(
    conn,
    *,
    space_key: str | None,
    title: str | None,
    text: str | None,
    label: str | None,
    has_tables: bool,
    limit: int = 500,
) -> dict:
    if not any((title, text, label, has_tables)):
        raise ConfexError(
            "MISSING_ARGUMENT", "give --title, --text, --label or --has-tables", EXIT_USAGE
        )
    space_id = _space(conn, space_key)["space_id"] if space_key else None
    pages = repo.search_pages(
        conn, space_id=space_id, title=title, label=label, has_tables=has_tables, limit=limit
    )
    results: dict[int, dict] = {}
    for p in pages:
        matches = []
        if title:
            matches.append({"field": "title"})
        if label:
            matches.append({"field": "label", "value": label})
        if has_tables:
            matches.append({"field": "has_tables"})
        results[p["page_id"]] = {**p, "matches": matches}
    projection = None
    if text:
        ids = list(results) if (title or label or has_tables) else None
        if ids == []:
            results = {}
        else:
            hits = repo.search_text(conn, text=text, page_ids=ids, space_id=space_id, limit=limit)
            text_results: dict[int, dict] = {}
            for h in hits:
                entry = text_results.setdefault(
                    h["page_id"],
                    {
                        **{k: h[k] for k in ("page_id", "title", "space_key")},
                        "current_version": h["version_number"],
                        "matches": list(results.get(h["page_id"], {}).get("matches", [])),
                    },
                )
                entry["matches"].append(
                    {
                        "field": "text",
                        "block_id": h["block_id"],
                        "block_kind": h["block_kind"],
                        "section_id": h["section_id"],
                        "table_id": h["table_id"],
                        "cell_id": h["cell_id"],
                        "snippet": _snippet(h["text"], text),
                    }
                )
            results = text_results
        projection = repo.search_projection_state(conn, space_id)
    return {
        "method": "case-insensitive substring over mirror metadata and the parsed-text "
        "projection; not Confluence CQL ranking or tokenization",
        "results": sorted(results.values(), key=lambda r: (r["title"].lower(), r["page_id"])),
        "text_projection": projection,
    }


def _snippet(text: str, needle: str, width: int = 60) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return text[: width * 2]
    lo, hi = max(i - width, 0), min(i + len(needle) + width, len(text))
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def table_tree(conn, resolved: Resolved, table: dict) -> dict:
    tables = {t["table_id"]: t for t in resolved.artifact["tables"]}

    def node(t: dict) -> dict:
        return {
            "table_id": t["table_id"],
            "selector": f"{resolved.artifact['page_id']}:t{t['ordinal']:02d}",
            "depth": t["depth"],
            "parent_cell_id": t["parent_cell_id"],
            "rows": t["row_count"],
            "columns": t["column_count"],
            "geometry_status": t["geometry_status"],
            "children": [node(tables[c]) for c in t["child_table_ids"] if c in tables],
        }

    ancestors = []
    parent = table["parent_table_id"]
    while parent and parent in tables:
        p = tables[parent]
        ancestors.insert(0, {"table_id": p["table_id"], "ordinal": p["ordinal"]})
        parent = p["parent_table_id"]
    return {"identity": identity(resolved, table), "ancestors": ancestors, "tree": node(table)}
