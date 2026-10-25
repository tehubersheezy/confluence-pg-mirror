"""Pure functions mapping Confluence REST JSON to database row dicts (no I/O)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

# Expansions that are large or stored elsewhere; excluded from `raw` snapshots.
_RAW_EXCLUDE = ("body", "restrictions", "_expandable", "children", "descendants")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def user_ref(user: dict | None) -> tuple[str | None, str | None]:
    """Return (stable id, display name) for a Confluence user object."""
    if not isinstance(user, dict):
        return None, None
    ident = user.get("userKey") or user.get("username") or user.get("accountId")
    return ident, user.get("displayName") or user.get("username")


def raw_snapshot(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _RAW_EXCLUDE}


def sha256_text(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def space_row(space: dict) -> dict[str, Any]:
    description = ((space.get("description") or {}).get("plain") or {}).get("value")
    homepage = space.get("homepage") or {}
    return {
        "space_id": int(space["id"]),
        "space_key": space["key"],
        "name": space.get("name"),
        "space_type": space.get("type"),
        "status": space.get("status"),
        "description_plain": description,
        "homepage_id": int(homepage["id"]) if homepage.get("id") else None,
        "raw": raw_snapshot(space),
    }


def property_row(prop: dict) -> dict[str, Any]:
    return {
        "property_key": prop["key"],
        "property_id": str(prop["id"]) if prop.get("id") is not None else None,
        "value": prop.get("value"),
        "version_number": (prop.get("version") or {}).get("number"),
        "raw": raw_snapshot(prop),
    }


def page_row(content: dict) -> dict[str, Any]:
    ancestors = [int(a["id"]) for a in content.get("ancestors") or [] if a.get("id")]
    version = content.get("version") or {}
    history = content.get("history") or {}
    created_by, _ = user_ref(history.get("createdBy"))
    modified_by, _ = user_ref(version.get("by"))
    links = content.get("_links") or {}
    position = (content.get("extensions") or {}).get("position")
    try:
        position = int(position) if position not in (None, "", "none") else None
    except (TypeError, ValueError):
        position = None
    return {
        "page_id": int(content["id"]),
        "space_id": int(content["space"]["id"]),
        "content_type": content.get("type", "page"),
        "title": content.get("title") or "",
        "status": content.get("status", "current"),
        "parent_id": ancestors[-1] if ancestors else None,
        "ancestor_ids": ancestors,
        "depth": len(ancestors),
        "position": position,
        "current_version": int(version.get("number", 1)),
        "created_at": parse_ts(history.get("createdDate")),
        "created_by": created_by,
        "last_modified_at": parse_ts(version.get("when")),
        "last_modified_by": modified_by,
        "webui_path": links.get("webui"),
        "tinyui_path": links.get("tinyui"),
        "raw": raw_snapshot(content),
    }


def version_row(content: dict) -> dict[str, Any]:
    version = content.get("version") or {}
    by_id, by_name = user_ref(version.get("by"))
    ancestors = content.get("ancestors") or []
    return {
        "page_id": int(content["id"]),
        "version_number": int(version.get("number", 1)),
        "title": content.get("title") or "",
        "parent_id": int(ancestors[-1]["id"]) if ancestors else None,
        "space_id": int(content["space"]["id"]) if content.get("space") else None,
        "created_at": parse_ts(version.get("when")),
        "created_by": by_id,
        "created_by_display": by_name,
        "message": version.get("message") or None,
        "minor_edit": version.get("minorEdit"),
        "raw": raw_snapshot(version),
    }


def content_row(content: dict) -> dict[str, Any]:
    body = content.get("body") or {}

    def rep(name: str) -> str | None:
        value = (body.get(name) or {}).get("value")
        return value if isinstance(value, str) else None

    storage = rep("storage")
    return {
        "page_id": int(content["id"]),
        "version_number": int((content.get("version") or {}).get("number", 1)),
        "body_storage": storage,
        "body_view": rep("view"),
        "body_export_view": rep("export_view"),
        "storage_sha256": sha256_text(storage),
    }


def label_rows(labels: list[dict]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for label in labels:
        name = label.get("name") or label.get("label")
        if not name:
            continue
        prefix = label.get("prefix") or "global"
        rows[(prefix, name)] = {
            "prefix": prefix,
            "name": name,
            "label_id": str(label["id"]) if label.get("id") is not None else None,
        }
    return list(rows.values())


def _expanded(block: dict | None) -> tuple[list[dict], bool]:
    """Items of an expanded collection; second value is True only if it is complete."""
    block = block or {}
    results = block.get("results") or []
    links = block.get("_links") or {}
    size, limit = block.get("size"), block.get("limit")
    truncated = bool(links.get("next")) or (
        limit is not None and size is not None and size >= limit
    )
    return results, ("results" in block) and not truncated


def listing_labels(content: dict) -> tuple[list[dict], bool]:
    """Labels embedded in a listing (metadata.labels); second value is True if complete."""
    return _expanded((content.get("metadata") or {}).get("labels"))


def expanded_attachments(content: dict) -> tuple[list[dict], bool]:
    """Attachments embedded via ``children.attachment``; True if the list is complete."""
    return _expanded((content.get("children") or {}).get("attachment"))


def expanded_properties(content: dict, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Content properties embedded via ``metadata.properties.<key>`` expansions."""
    found = (content.get("metadata") or {}).get("properties") or {}
    return [property_row(found[k]) for k in keys if isinstance(found.get(k), dict)]


def restriction_rows(content: dict) -> tuple[list[dict[str, Any]], bool]:
    """Direct read/update restrictions; second value is False if not expanded/complete."""
    restrictions = content.get("restrictions")
    if not isinstance(restrictions, dict):
        return [], False
    rows: list[dict[str, Any]] = []
    complete = True
    for operation in ("read", "update"):
        op = (restrictions.get(operation) or {}).get("restrictions") or {}
        for subject_type, key in (("user", "user"), ("group", "group")):
            block = op.get(key) or {}
            if "results" not in block:
                complete = False
                continue
            if (block.get("_links") or {}).get("next"):
                complete = False
            for item in block["results"]:
                if subject_type == "user":
                    ident, name = user_ref(item)
                else:
                    ident = item.get("name")
                    name = item.get("name")
                if ident:
                    rows.append(
                        {
                            "operation": operation,
                            "subject_type": subject_type,
                            "subject_id": ident,
                            "subject_name": name,
                            "raw": raw_snapshot(item),
                        }
                    )
    return rows, complete


def attachment_row(att: dict, page_id: int) -> dict[str, Any]:
    extensions = att.get("extensions") or {}
    metadata = att.get("metadata") or {}
    version = att.get("version") or {}
    history = att.get("history") or {}
    created_by, _ = user_ref(history.get("createdBy") or version.get("by"))
    size = extensions.get("fileSize")
    return {
        "attachment_id": str(att["id"]),
        "page_id": page_id,
        "title": att.get("title") or "",
        "media_type": extensions.get("mediaType") or metadata.get("mediaType"),
        "file_size": int(size) if size is not None else None,
        "comment": extensions.get("comment") or metadata.get("comment"),
        "version_number": version.get("number"),
        "status": att.get("status"),
        "created_at": parse_ts(history.get("createdDate") or version.get("when")),
        "created_by": created_by,
        "download_path": (att.get("_links") or {}).get("download"),
        "raw": raw_snapshot(att),
    }
