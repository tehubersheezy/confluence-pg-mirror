"""Classify links in rendered Confluence HTML by their URL (pure; no lookups).

Rendered pages link to other pages by ID (``viewpage.action?pageId=``,
``/spaces/KEY/pages/ID/...``) or by space and title (``/display/KEY/Title``), to
attachments (``/download/attachments/PAGE/file``) and to user profiles. Anything else is
an external URL, a mail link or an in-page anchor. Hosts are not checked: the paths are
Confluence's own, and a lookup in the mirror decides whether a page target exists.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit

_SPACES_PAGE = re.compile(r"/spaces/([^/]+)/pages/(\d+)(?:/|$)")
_DISPLAY_USER = re.compile(r"/display/~([^/]+)/?$")
_DISPLAY_PAGE = re.compile(r"/display/([^/~][^/]*)/([^/]+)/?$")
_ATTACHMENT = re.compile(r"/download/attachments/(\d+)/([^/?]+)")


def classify_href(href: str) -> dict[str, Any]:
    href = (href or "").strip()
    if href.startswith("#"):
        return {"kind": "anchor", "anchor": unquote(href[1:])}
    parts = urlsplit(href)
    out: dict[str, Any] = {}
    if parts.fragment:
        out["anchor"] = unquote(parts.fragment)
    if parts.scheme == "mailto":
        return {"kind": "email", **out}
    query = parse_qs(parts.query)
    raw_path = parts.path
    if raw_path.endswith("viewpage.action") and query.get("pageId", [""])[0].isdigit():
        return {"kind": "page", "page_id": int(query["pageId"][0]), **out}
    if raw_path.endswith("viewuserprofile.action") and query.get("username"):
        return {"kind": "user", "username": query["username"][0], **out}
    if m := _SPACES_PAGE.search(raw_path):
        return {"kind": "page", "page_id": int(m.group(2)), "space_key": unquote(m.group(1)), **out}
    if m := _DISPLAY_USER.search(raw_path):
        return {"kind": "user", "username": unquote(m.group(1)), **out}
    if m := _DISPLAY_PAGE.search(raw_path):
        return {
            "kind": "page",
            "space_key": unquote(m.group(1)),
            "title": unquote_plus(m.group(2)),
            **out,
        }
    if m := _ATTACHMENT.search(raw_path):
        return {
            "kind": "attachment",
            "page_id": int(m.group(1)),
            "filename": unquote(m.group(2)),
            **out,
        }
    if parts.scheme in ("http", "https"):
        return {"kind": "external", "host": parts.netloc.lower(), **out}
    return {"kind": "other", **out}
