"""Macro discovery and macro-specific structured extraction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lxml import etree

from confluence_mirror.extract.storage import (
    AC,
    ac_attr,
    element_path,
    inner_markup,
    qname,
    resource_ref,
    serialize,
    text_of,
)

MACRO_TAGS = (AC + "structured-macro", AC + "macro")
INCLUDE_MACROS = frozenset({"include", "excerpt-include"})


def is_macro(el: etree._Element) -> bool:
    return el.tag in MACRO_TAGS


def macro_name(el: etree._Element) -> str:
    return (ac_attr(el, "name") or "").strip().lower() or "unknown"


def nearest_macro(el: etree._Element) -> etree._Element | None:
    for ancestor in el.iterancestors():
        if ancestor.tag in MACRO_TAGS:
            return ancestor
    return None


def own_children(macro: etree._Element, local: str) -> list[etree._Element]:
    return [c for c in macro if c.tag == AC + local]


def parameters(macro: etree._Element) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for param in own_children(macro, "parameter"):
        name = ac_attr(param, "name") or ""
        # Top-level resource identifiers only; nested ones are reported as "container".
        refs = [
            resource_ref(c)
            for c in param.iter()
            if qname(c).startswith("ri:") and not qname(c.getparent()).startswith("ri:")
        ]
        text = "".join(param.itertext()).strip()
        params[name] = {"value": text, "refs": refs} if refs else text
    for param in own_children(macro, "default-parameter"):
        params[""] = "".join(param.itertext()).strip()
    return params


def body(macro: etree._Element) -> tuple[str | None, str | None, str | None]:
    """(body_type, body_storage, body_text) for a macro's own body."""
    rich = own_children(macro, "rich-text-body")
    if rich:
        return "rich-text", inner_markup(rich[0]), text_of(rich[0])
    plain = own_children(macro, "plain-text-body")
    if plain:
        return "plain-text", None, plain[0].text or ""
    return None, None, None


def macro_rows(root: etree._Element) -> tuple[list[dict], dict[etree._Element, int]]:
    elements = [el for el in root.iter(*MACRO_TAGS)]
    index = {el: i for i, el in enumerate(elements)}
    rows: list[dict] = []
    for el, i in index.items():
        parent = nearest_macro(el)
        depth = sum(1 for a in el.iterancestors() if a.tag in MACRO_TAGS)
        body_type, body_storage, body_text = body(el)
        rows.append(
            {
                "macro_index": i,
                "macro_name": macro_name(el),
                "macro_id": ac_attr(el, "macro-id"),
                "schema_version": ac_attr(el, "schema-version"),
                "parent_macro_index": index[parent] if parent is not None else None,
                "depth": depth,
                "element_path": element_path(el),
                "parameters": parameters(el),
                "body_type": body_type,
                "body_storage": body_storage,
                "body_text": body_text,
                "source_markup": serialize(el),
            }
        )
    return rows, index


# -- specialised interpretations ------------------------------------------------------

Extractor = Callable[[etree._Element, dict[str, Any]], list[tuple[str, dict]]]


def _param(params: dict[str, Any], name: str) -> Any:
    value = params.get(name)
    return value.get("value") if isinstance(value, dict) else value


def _first_ref(params: dict[str, Any], *names: str) -> dict | None:
    for name in names:
        value = params.get(name)
        if isinstance(value, dict) and value.get("refs"):
            return value["refs"][0]
    return None


def _code(el, params):
    _, _, text = body(el)
    return [
        (
            "code",
            {
                "language": _param(params, "language") or None,
                "title": _param(params, "title") or None,
                "linenumbers": _param(params, "linenumbers") == "true",
                "code": text or "",
            },
        )
    ]


def _panel(el, params):
    _, _, text = body(el)
    return [
        (
            "panel",
            {
                "panel_type": macro_name(el),
                "title": _param(params, "title") or None,
                "text": text or "",
            },
        )
    ]


def _jira(el, params):
    return [
        (
            "jira",
            {
                "key": _param(params, "key") or None,
                "jql": _param(params, "jqlQuery") or None,
                "server": _param(params, "server") or None,
                "server_id": _param(params, "serverId") or None,
            },
        )
    ]


def _include(el, params):
    ref = _first_ref(params, "", "page") or {}
    return [
        (
            "include",
            {
                "target_title": ref.get("content-title"),
                "target_space_key": ref.get("space-key"),
                "excerpt_name": _param(params, "name") or None,
            },
        )
    ]


def _excerpt(el, params):
    _, _, text = body(el)
    return [
        (
            "excerpt",
            {
                "name": _param(params, "name") or None,
                "text": text or "",
                "hidden": _param(params, "hidden") == "true",
            },
        )
    ]


def _status(el, params):
    return [
        (
            "status",
            {
                "title": _param(params, "title") or "",
                "colour": _param(params, "colour") or None,
                "subtle": _param(params, "subtle") == "true",
            },
        )
    ]


def _expand(el, params):
    _, _, text = body(el)
    return [("expand", {"title": _param(params, "title") or None, "text": text or ""})]


def _details(el, params):
    """Page Properties: key/value pairs from the (vertical or horizontal) table in the body."""
    rich = own_children(el, "rich-text-body")
    properties: list[dict[str, str]] = []
    if rich:
        for table in rich[0].iter("table"):
            for tr in table.iter("tr"):
                cells = [c for c in tr if c.tag in ("th", "td")]
                if len(cells) >= 2 and (cells[0].tag == "th" or len(cells) == 2):
                    properties.append(
                        {
                            "key": text_of(cells[0]),
                            "value": " | ".join(text_of(c) for c in cells[1:]),
                        }
                    )
            break
    return [
        (
            "page-properties",
            {
                "id": _param(params, "id") or None,
                "hidden": _param(params, "hidden") == "true",
                "properties": properties,
            },
        )
    ]


def _details_summary(el, params):
    return [
        (
            "page-properties-report",
            {
                "cql": _param(params, "cql") or None,
                "label": _param(params, "label") or None,
                "headings": _param(params, "headings") or None,
            },
        )
    ]


def _attachment_ref(el, params):
    ref = _first_ref(params, "name", "") or {}
    return [
        ("attachment-ref", {"filename": ref.get("filename"), "container": ref.get("container")})
    ]


def _anchor(el, params):
    return [("anchor", {"name": _param(params, "") or None})]


def _tree(el, params):
    return [
        ("navigation", {"kind": macro_name(el), "params": {k: _param(params, k) for k in params}})
    ]


EXTRACTORS: dict[str, Extractor] = {
    "code": _code,
    "noformat": _code,
    "info": _panel,
    "note": _panel,
    "warning": _panel,
    "tip": _panel,
    "panel": _panel,
    "jira": _jira,
    "include": _include,
    "excerpt-include": _include,
    "excerpt": _excerpt,
    "status": _status,
    "expand": _expand,
    "details": _details,
    "detailssummary": _details_summary,
    "view-file": _attachment_ref,
    "viewfile": _attachment_ref,
    "multimedia": _attachment_ref,
    "anchor": _anchor,
    "children": _tree,
    "toc": _tree,
    "pagetree": _tree,
}


def macro_extractions(index: dict[etree._Element, int], rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for el, i in index.items():
        extractor = EXTRACTORS.get(rows[i]["macro_name"])
        if extractor is None:
            continue
        for extraction_type, data in extractor(el, rows[i]["parameters"]):
            out.append({"macro_index": i, "extraction_type": extraction_type, "data": data})
    return out


__all__ = [
    "INCLUDE_MACROS",
    "is_macro",
    "macro_rows",
    "macro_extractions",
    "nearest_macro",
    "qname",
]
