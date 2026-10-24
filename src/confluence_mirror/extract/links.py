"""Link extraction: ``ac:link``, ``<a href>``, ``ac:image`` and include-macro targets."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit

from lxml import etree

from confluence_mirror.extract.macros import INCLUDE_MACROS, macro_name, nearest_macro
from confluence_mirror.extract.storage import (
    AC,
    RI,
    ac_attr,
    element_path,
    qname,
    ri_attr,
    text_of,
)

_SPACES_PAGES_RE = re.compile(r"^/spaces/([^/]+)/pages/(\d+)")
_DISPLAY_RE = re.compile(r"^/display/([^/]+)/([^/?#]+)")
_USER_RE = re.compile(r"^/display/~([^/?#]+)/?$")
_DOWNLOAD_RE = re.compile(r"^/download/(?:attachments|thumbnails)/(\d+)/([^/?#]+)")


class LinkContext:
    def __init__(self, page_id: int | None, space_key: str | None, base_url: str | None):
        self.page_id = page_id
        self.space_key = space_key
        parts = urlsplit(base_url or "")
        self.host = parts.hostname
        self.context_path = parts.path.rstrip("/")


def _empty(link_type: str, source_element: str, el: etree._Element) -> dict:
    return {
        "link_type": link_type,
        "source_element": source_element,
        "element_path": element_path(el),
        "macro_index": None,
        "target_page_id": None,
        "target_space_key": None,
        "target_title": None,
        "target_filename": None,
        "target_user_key": None,
        "target_url": None,
        "anchor": None,
        "link_text": None,
    }


def _apply_resource(link: dict, res: etree._Element, ctx: LinkContext) -> None:
    kind = qname(res)
    if kind in ("ri:page", "ri:blog-post"):
        link["link_type"] = "page" if kind == "ri:page" else "blogpost"
        link["target_title"] = ri_attr(res, "content-title")
        link["target_space_key"] = ri_attr(res, "space-key") or ctx.space_key
        if ri_attr(res, "content-id"):
            link["target_page_id"] = int(ri_attr(res, "content-id"))
    elif kind == "ri:attachment":
        link["link_type"] = "attachment"
        link["target_filename"] = ri_attr(res, "filename")
        container = next((c for c in res if qname(c) in ("ri:page", "ri:blog-post")), None)
        if container is not None:
            link["target_title"] = ri_attr(container, "content-title")
            link["target_space_key"] = ri_attr(container, "space-key") or ctx.space_key
        else:
            link["target_page_id"] = ctx.page_id
            link["target_space_key"] = ctx.space_key
    elif kind == "ri:user":
        link["link_type"] = "user"
        link["target_user_key"] = (
            ri_attr(res, "userkey") or ri_attr(res, "username") or ri_attr(res, "account-id")
        )
    elif kind == "ri:space":
        link["link_type"] = "space"
        link["target_space_key"] = ri_attr(res, "space-key")
    elif kind == "ri:url":
        link["link_type"] = "url"
        link["target_url"] = ri_attr(res, "value")
    elif kind in ("ri:content-entity", "ri:shortcut"):
        link["link_type"] = "page"
        cid = ri_attr(res, "content-id")
        if cid and cid.isdigit():
            link["target_page_id"] = int(cid)


def _ac_link(el: etree._Element, ctx: LinkContext) -> dict:
    link = _empty("anchor", "ac:link", el)
    link["anchor"] = ac_attr(el, "anchor")
    resource = next((c for c in el if qname(c).startswith("ri:")), None)
    if resource is not None:
        _apply_resource(link, resource, ctx)
    elif link["anchor"]:
        link["target_page_id"] = ctx.page_id
    for child in el:
        if child.tag == AC + "plain-text-link-body":
            link["link_text"] = child.text or ""
        elif child.tag == AC + "link-body":
            link["link_text"] = text_of(child)
    return link


def classify_href(href: str, ctx: LinkContext) -> dict:
    """Map an href to link fields (link_type and targets)."""
    out: dict = {"link_type": "url", "target_url": href}
    if href.startswith("#"):
        return {
            "link_type": "anchor",
            "anchor": unquote(href[1:]) or None,
            "target_page_id": ctx.page_id,
        }
    parts = urlsplit(href)
    internal = (not parts.scheme and not parts.netloc) or (
        parts.scheme in ("http", "https") and ctx.host and parts.hostname == ctx.host
    )
    if not internal or parts.scheme == "mailto":
        return out
    path = parts.path
    if ctx.context_path and path.startswith(ctx.context_path + "/"):
        path = path[len(ctx.context_path) :]
    anchor = unquote(parts.fragment) or None
    query = parse_qs(parts.query)
    if path.endswith("/pages/viewpage.action") and query.get("pageId", [""])[0].isdigit():
        return {
            **out,
            "link_type": "page",
            "target_page_id": int(query["pageId"][0]),
            "anchor": anchor,
        }
    if path.endswith("/pages/viewpage.action") and "title" in query:
        return {
            **out,
            "link_type": "page",
            "target_title": query["title"][0],
            "target_space_key": query.get("spaceKey", [ctx.space_key])[0],
            "anchor": anchor,
        }
    if m := _SPACES_PAGES_RE.match(path):
        return {
            **out,
            "link_type": "page",
            "target_page_id": int(m.group(2)),
            "target_space_key": m.group(1),
            "anchor": anchor,
        }
    if m := _USER_RE.match(path):
        return {**out, "link_type": "user", "target_user_key": unquote(m.group(1))}
    if m := _DISPLAY_RE.match(path):
        return {
            **out,
            "link_type": "page",
            "target_space_key": m.group(1),
            "target_title": unquote_plus(m.group(2)),
            "anchor": anchor,
        }
    if m := _DOWNLOAD_RE.match(path):
        return {
            **out,
            "link_type": "attachment",
            "target_page_id": int(m.group(1)),
            "target_filename": unquote(m.group(2)),
        }
    return out


def _a_href(el: etree._Element, ctx: LinkContext) -> dict | None:
    href = (el.get("href") or "").strip()
    if not href:
        return None
    link = _empty("url", "a", el)
    link.update(classify_href(href, ctx))
    link["link_text"] = text_of(el)
    return link


def _image(el: etree._Element, ctx: LinkContext) -> dict | None:
    resource = next((c for c in el if qname(c) in ("ri:attachment", "ri:url")), None)
    if resource is None:
        return None
    link = _empty("attachment", "ac:image", el)
    _apply_resource(link, resource, ctx)
    link["link_text"] = ac_attr(el, "alt") or ac_attr(el, "title")
    return link


def _include_param_macro(el: etree._Element) -> etree._Element | None:
    """Return the include macro if ``el`` sits directly in one of its parameters."""
    parent = el.getparent()
    if parent is None or parent.tag != AC + "parameter":
        return None
    macro = parent.getparent()
    if (
        macro is not None
        and macro.tag in (AC + "structured-macro", AC + "macro")
        and macro_name(macro) in INCLUDE_MACROS
    ):
        return macro
    return None


def link_rows(
    root: etree._Element, macro_index: dict[etree._Element, int], ctx: LinkContext
) -> list[dict]:
    out: list[dict] = []
    for el in root.iter():
        tag = el.tag
        link = None
        if tag == AC + "link":
            link = _ac_link(el, ctx)
            include = _include_param_macro(el)
            if include is not None:
                link["link_type"] = "include"
                link["source_element"] = f"macro:{macro_name(include)}"
        elif tag == RI + "page" and _include_param_macro(el) is not None:
            link = _empty("include", f"macro:{macro_name(_include_param_macro(el))}", el)
            _apply_resource(link, el, ctx)
            link["link_type"] = "include"
        elif tag == "a":
            link = _a_href(el, ctx)
        elif tag == AC + "image":
            link = _image(el, ctx)
        if link is None:
            continue
        macro = nearest_macro(el)
        link["macro_index"] = macro_index.get(macro) if macro is not None else None
        link["link_index"] = len(out)
        out.append(link)
    return out
