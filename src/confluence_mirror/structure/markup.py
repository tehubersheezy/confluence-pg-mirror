"""Element helpers for parsed rendered HTML (export view, view)."""

from __future__ import annotations

from lxml import etree

# Synthetic root wrapping every parsed body, so fragments have one parent.
ROOT_TAG = "cpm-root"


def qname(el: etree._Element) -> str:
    """The element's tag name; empty for comments and processing instructions."""
    return el.tag if isinstance(el.tag, str) else ""


def element_path(el: etree._Element) -> str:
    """Stable XPath-like path of ``el`` below the synthetic root, e.g. ``/div[2]/table[1]``."""
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
    return etree.tostring(el, encoding="unicode", with_tail=with_tail, method="html")


_INLINE = frozenset({"span", "a", "img", "code", "strong", "em", "b", "i", "time", "abbr"})


def macro_name(el: etree._Element) -> str | None:
    """The block macro an element renders, from Confluence's ``data-macro-name`` marker.

    Inline macros (a status lozenge, a user mention) are part of the surrounding text and
    are not containers, so they are not reported here.
    """
    if not isinstance(el.tag, str) or el.tag in _INLINE:
        return None
    return el.get("data-macro-name")
