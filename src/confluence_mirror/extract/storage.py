"""Parsing Confluence storage format (XHTML with ``ac:``/``ri:`` elements) safely.

Storage format is XML without namespace declarations and with HTML named entities, so the
body is wrapped in a root element that declares the Atlassian namespaces and named
entities are rewritten to numeric references. The parser never resolves entities or touches
the network (no XXE), and ``recover=True`` tolerates the occasional malformed page.
"""

from __future__ import annotations

import re
from html.entities import html5
from typing import Any

from lxml import etree

AC_NS = "http://atlassian.com/content"
RI_NS = "http://atlassian.com/resource/identifier"
AT_NS = "http://atlassian.com/template"
PREFIXES = {AC_NS: "ac", RI_NS: "ri", AT_NS: "at"}
AC = f"{{{AC_NS}}}"
RI = f"{{{RI_NS}}}"
ROOT_TAG = "cpm-root"

_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_NS_DECL_RE = re.compile(r' xmlns:(?:ac|ri|at)="[^"]*"')


def _entity(match: re.Match[str]) -> str:
    name = match.group(1)
    if name in _XML_ENTITIES:
        return match.group(0)
    char = html5.get(name + ";")
    if char is None:
        return "&amp;" + name + ";"
    return "".join(f"&#{ord(c)};" for c in char)


def parse_storage(storage: str) -> etree._Element:
    text = _ENTITY_RE.sub(_entity, storage or "")
    wrapped = (
        f'<{ROOT_TAG} xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}" xmlns:at="{AT_NS}">{text}</{ROOT_TAG}>'
    )
    parser = etree.XMLParser(
        recover=True,
        resolve_entities=False,
        no_network=True,
        huge_tree=True,
        load_dtd=False,
        strip_cdata=False,
    )
    root = etree.fromstring(wrapped.encode("utf-8"), parser)
    if root is None:  # pragma: no cover - recover mode returns a root for any input
        root = etree.Element(ROOT_TAG)
    return root


def qname(el: etree._Element) -> str:
    """Prefixed tag name as it appears in storage format, e.g. ``ac:structured-macro``."""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        prefix = PREFIXES.get(ns)
        return f"{prefix}:{local}" if prefix else local
    return tag


def ri_attr(el: etree._Element, name: str) -> str | None:
    return el.get(RI + name)


def ac_attr(el: etree._Element, name: str) -> str | None:
    return el.get(AC + name)


def element_path(el: etree._Element) -> str:
    """Stable XPath-like path of ``el`` below the synthetic root, e.g. ``/p[2]/ac:link[1]``."""
    parts: list[str] = []
    node = el
    while node is not None and node.tag != ROOT_TAG:
        parent = node.getparent()
        index = 1
        if parent is not None:
            for sibling in parent:
                if sibling is node:
                    break
                if sibling.tag == node.tag:
                    index += 1
        parts.append(f"{qname(node)}[{index}]")
        node = parent
    return "/" + "/".join(reversed(parts))


def serialize(el: etree._Element, *, with_tail: bool = False) -> str:
    text = etree.tostring(el, encoding="unicode", with_tail=with_tail)
    return _NS_DECL_RE.sub("", text)


def inner_markup(el: etree._Element) -> str:
    return (_escape(el.text or "")) + "".join(serialize(child, with_tail=True) for child in el)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_text(text: str) -> str:
    return " ".join(text.split())


_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "td",
        "th",
        "tr",
        "table",
        "tbody",
        "thead",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "blockquote",
        "ac:structured-macro",
        "ac:macro",
        "ac:rich-text-body",
        "ac:plain-text-body",
        "ac:task",
        "ac:task-body",
        "ac:layout-section",
        "ac:layout-cell",
    }
)
# Content that is configuration, not prose.
_SKIP_TAGS = frozenset(
    {
        "ac:parameter",
        "ac:default-parameter",
        "ac:task-id",
        "ac:task-status",
        "ac:task-uuid",
        "ac:placeholder",
    }
)


def _render(el: etree._Element, out: list[str]) -> None:
    name = qname(el)
    if name in _SKIP_TAGS or not isinstance(el.tag, str):
        return
    block = name in _BLOCK_TAGS
    if block:
        out.append(" ")
    if name in ("ac:structured-macro", "ac:macro") and (ac_attr(el, "name") or "") == "status":
        title = next(
            (p.text for p in el if qname(p) == "ac:parameter" and ac_attr(p, "name") == "title"),
            None,
        )
        out.append(title or "")
    elif name == "ac:link" and not any(
        qname(c) in ("ac:link-body", "ac:plain-text-link-body") for c in el
    ):
        # A bare link renders as its target, as Confluence displays it.
        for child in el:
            if qname(child).startswith("ri:"):
                out.append(
                    ri_attr(child, "content-title")
                    or ri_attr(child, "filename")
                    or ri_attr(child, "space-key")
                    or "@" + (ri_attr(child, "userkey") or ri_attr(child, "username") or "user")
                )
                break
    else:
        if el.text:
            out.append(el.text)
        for child in el:
            _render(child, out)
            if child.tail:
                out.append(child.tail)
    if block:
        out.append(" ")


def text_of(el: etree._Element) -> str:
    """Readable plain text: block boundaries become spaces, macro parameters are skipped."""
    out: list[str] = []
    if el.text:
        out.append(el.text)
    for child in el:
        _render(child, out)
        if child.tail:
            out.append(child.tail)
    return normalize_text("".join(out))


def resource_ref(el: etree._Element) -> dict[str, Any] | None:
    """Describe an ``ri:*`` resource identifier element."""
    kind = qname(el)
    if not kind.startswith("ri:"):
        return None
    ref: dict[str, Any] = {"type": kind[3:]}
    for key, value in el.attrib.items():
        if key.startswith(RI):
            ref[key[len(RI) :]] = value
    nested = [resource_ref(child) for child in el if qname(child).startswith("ri:")]
    if nested:
        ref["container"] = nested[0]
    return ref
