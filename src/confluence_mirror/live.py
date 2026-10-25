"""``--data-source live``: read-only, non-persistent views straight from Confluence.

Nothing here touches the database: live navigation never records observations or updates
the mirror (only ``sync`` writes source captures). Results mirror the shapes of the mirror
commands where the data allows, and say so when they cannot (e.g. no parsed structure).
"""

from __future__ import annotations

from typing import Any

from confluence_mirror import models
from confluence_mirror.client import ConfluenceClient, NotFoundError
from confluence_mirror.inspection import (
    EXIT_NOT_FOUND,
    EXIT_UNAVAILABLE,
    ConfexError,
    Resolved,
    read_text,
)
from confluence_mirror.structure import REPRESENTATIONS, parse_document

_PAGE_EXPAND = "version,ancestors,space,history,metadata.labels,extensions.position"


def _content(client: ConfluenceClient, page_id: int, expand: str = _PAGE_EXPAND) -> dict:
    try:
        return client.get_content(page_id, expand=expand)
    except NotFoundError:
        raise ConfexError(
            "NOT_FOUND",
            f"page {page_id} is not visible to this token (missing, or no permission)",
            EXIT_NOT_FOUND,
        ) from None


def _page_summary(item: dict) -> dict:
    position = (item.get("extensions") or {}).get("position")
    try:
        position = int(position) if position not in (None, "", "none") else None
    except (TypeError, ValueError):
        position = None
    return {
        "page_id": int(item["id"]),
        "title": item.get("title") or "",
        "position": position,
        "current_version": (item.get("version") or {}).get("number"),
    }


def _order(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda p: (p["position"] is None, p["position"] or 0, p["title"].lower(), p["page_id"]),
    )


def spaces_list(client: ConfluenceClient, match: str | None, space_type: str | None) -> list:
    out = []
    for s in client.list_spaces(space_type):
        row = models.space_row(s)
        if match and match.lower() not in f"{row['space_key']} {row['name'] or ''}".lower():
            continue
        out.append({k: row.get(k) for k in ("space_key", "name", "space_type", "status")})
    return sorted(out, key=lambda r: r["space_key"])


def space_show(client: ConfluenceClient, key: str) -> dict:
    try:
        row = models.space_row(client.get_space(key))
    except NotFoundError:
        raise ConfexError("NOT_FOUND", f"space {key} is not visible", EXIT_NOT_FOUND) from None
    return {k: v for k, v in row.items() if k not in ("raw", "space_id")} | {
        "space_id": str(row["space_id"])
    }


def page_show(client: ConfluenceClient, page_id: int) -> dict:
    content = _content(client, page_id)
    row = models.page_row(content)  # page expand includes space
    labels, complete = models.listing_labels(content)
    return {
        "page_id": row["page_id"],
        "title": row["title"],
        "status": row["status"],
        "space_key": (content.get("space") or {}).get("key"),
        "parent_id": row["parent_id"],
        "current_version": row["current_version"],
        "last_modified_at": row["last_modified_at"],
        "breadcrumbs": [
            {"page_id": int(a["id"]), "title": a.get("title")}
            for a in content.get("ancestors") or []
        ],
        "labels": [lbl["name"] for lbl in labels],
        "labels_complete": complete,
        "note": "live metadata; parsed structure is only available from the mirror",
    }


def page_children(client: ConfluenceClient, page_id: int) -> list[dict]:
    _content(client, page_id, expand="version")
    return _order([_page_summary(c) for c in client.child_pages(page_id)])


def page_ancestors(client: ConfluenceClient, page_id: int) -> dict:
    content = _content(client, page_id)
    return {
        "page": {"page_id": int(content["id"]), "title": content.get("title")},
        "ancestors": [
            {"page_id": int(a["id"]), "title": a.get("title")}
            for a in content.get("ancestors") or []
        ],
        "unresolved_parent_id": None,
        "diagnostics": [],
    }


def page_labels(client: ConfluenceClient, page_id: int) -> list[dict]:
    return [
        {"prefix": lbl.get("prefix", "global"), "name": lbl.get("name"), "label_id": lbl.get("id")}
        for lbl in client.labels(page_id)
    ]


def page_properties(client: ConfluenceClient, page_id: int, key: str | None) -> list[dict]:
    rows = [models.property_row(p) for p in client.content_properties(page_id)]
    out = [
        {k: r.get(k) for k in ("property_key", "property_id", "value", "version_number")}
        for r in rows
        if key is None or r["property_key"] == key
    ]
    if key and not out:
        raise ConfexError("NOT_FOUND", f"page {page_id} has no property {key!r}", EXIT_NOT_FOUND)
    return out


def page_attachments(client: ConfluenceClient, page_id: int) -> dict:
    rows = [models.attachment_row(a, page_id) for a in client.attachments(page_id)]
    keep = ("attachment_id", "title", "media_type", "file_size", "version_number", "status")
    return {
        "attachments": [{k: r.get(k) for k in keep} for r in rows],
        "note": "metadata only; attachment bodies are not downloaded or parsed",
    }


def page_read(
    client: ConfluenceClient, page_id: int, representation: str, mode: str, section_id: str | None
) -> dict:
    rep = "storage" if representation == "auto" else representation
    if rep not in REPRESENTATIONS:
        raise ConfexError("UNSUPPORTED_MODE", f"unknown representation {rep}", EXIT_UNAVAILABLE)
    content = _content(client, page_id, expand=f"version,body.{rep}")
    body = ((content.get("body") or {}).get(rep) or {}).get("value")
    if not isinstance(body, str):
        raise ConfexError(
            "REPRESENTATION_UNAVAILABLE",
            f"the server returned no {rep} body for page {page_id}",
            EXIT_UNAVAILABLE,
        )
    version = int((content.get("version") or {}).get("number", 1))
    if mode == "raw":
        return {
            "representation": rep,
            "version_number": version,
            "kind": "live_body_value",
            "note": "fetched live; not stored in the mirror",
            "body": body,
        }
    artifact = parse_document(body, page_id=page_id, version=version, representation=rep)
    reason = "auto: storage" if representation == "auto" else "requested"
    return read_text(Resolved(artifact, "ephemeral_parse", reason), section_id)


def page_tree(client: ConfluenceClient, root_id: int, depth: int, max_requests: int) -> dict:
    """Bounded breadth-first scan of immediate children (metadata only)."""
    root = _content(client, root_id, expand="version,extensions.position")
    requests = 1
    node: dict[str, Any] = {**_page_summary(root), "depth": 0, "children": []}
    frontier = [node]
    truncated = False
    while frontier:
        nxt = []
        for parent in frontier:
            if parent["depth"] >= depth:
                continue
            if requests >= max_requests:
                truncated = True
                break
            requests += 1
            for child in _order([_page_summary(c) for c in client.child_pages(parent["page_id"])]):
                entry = {**child, "depth": parent["depth"] + 1, "children": []}
                parent["children"].append(entry)
                nxt.append(entry)
        frontier = nxt if not truncated else []
    return {
        "root": node,
        "depth_limit": depth,
        "requests": requests,
        "truncated": truncated,
        "diagnostics": (
            [
                {
                    "code": "SCOPE_INCOMPLETE",
                    "severity": "warning",
                    "message": f"scan stopped after {max_requests} requests",
                }
            ]
            if truncated
            else []
        ),
    }


def search_cql(client: ConfluenceClient, cql: str, limit: int) -> dict:
    results = []
    for item in client.search(cql, expand="space,version"):
        results.append(
            {
                "page_id": int(item["id"]),
                "type": item.get("type"),
                "title": item.get("title"),
                "space_key": (item.get("space") or {}).get("key"),
                "current_version": (item.get("version") or {}).get("number"),
            }
        )
        if len(results) >= limit:
            break
    return {
        "method": "Confluence CQL via /rest/api/content/search (live)",
        "cql": cql,
        "results": results,
        "truncated": len(results) >= limit,
    }
