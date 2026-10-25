"""Structural parse of one body capture into a document artifact (pure; no I/O).

The artifact is a JSON-native dict so it can be stored as JSONB and reloaded unchanged:

- ``nodes``: headings, tables, macros and layout cells in source order, each with the
  container it sits in. Heading stacks are kept per structural scope (document flow,
  layout cell, table cell, macro body), so a heading inside a cell or one layout column
  never changes the context of content outside it.
- ``blocks``: evidence blocks referenced by headings, captions, context and cells. Each
  has an immutable ``evidence_text`` (offsets address it) and a ``normalized_text`` for
  display and search.
- ``tables``: every table occurrence with its row groups, physical cells and their logical
  placement (see :mod:`confluence_mirror.structure.grid`). Nested tables are separate
  occurrences that point back at their parent cell; a parent never counts their rows.

No semantic interpretation happens here: header rows are not inferred and no records are
produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import lxml.html
from lxml import etree

from confluence_mirror.extract.storage import (
    ROOT_TAG,
    ac_attr,
    element_path,
    parse_storage_report,
    qname,
    resource_ref,
    ri_attr,
    serialize,
)
from confluence_mirror.structure import grid
from confluence_mirror.structure.ids import (
    DEFAULT_INSTANCE_ID,
    digest,
    opaque_id,
    sha256_text,
    snapshot_id,
)

# Bump when artifact output changes; it is part of every artifact fingerprint.
PARSER_VERSION = "structure.2"
REPRESENTATIONS = ("storage", "export_view", "view")
EVIDENCE_TEXT_POLICY = "evidence-text.v1"

_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_MACROS = ("ac:structured-macro", "ac:macro")
_GROUP_TAGS = ("thead", "tbody", "tfoot")
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "pre",
        "blockquote",
        "hr",
        *_HEADINGS,
        "ac:structured-macro",
        "ac:macro",
        "ac:rich-text-body",
        "ac:task",
        "ac:layout-section",
        "ac:layout-cell",
    }
)
_SKIP_TAGS = frozenset(
    {
        "ac:parameter",
        "ac:default-parameter",
        "ac:task-id",
        "ac:task-status",
        "ac:task-uuid",
        "ac:placeholder",
        "script",
        "style",
    }
)
_CONTEXT_TAGS = frozenset({"p", "ul", "ol", "div", "blockquote", "pre"})
# Flow blocks recorded as text nodes, so a page can be read back in source order.
_FLOW_KINDS = {
    "p": "paragraph",
    "li": "list_item",
    "pre": "code",
    "blockquote": "quote",
    "div": "paragraph",
}
_STRUCTURAL = frozenset(
    {
        "table",
        "ul",
        "ol",
        "p",
        "div",
        "pre",
        "blockquote",
        "li",
        *_HEADINGS,
        *_MACROS,
        "ac:layout",
        "ac:layout-section",
        "ac:layout-cell",
    }
)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParseConfig:
    limits: grid.Limits = field(default_factory=grid.Limits)
    context_chars: int = 1000

    def fingerprint(self) -> str:
        return digest(
            {
                "limits": self.limits.as_dict(),
                "context_chars": self.context_chars,
                "evidence_text": EVIDENCE_TEXT_POLICY,
            }
        )[:16]


def artifact_identity(
    page_id: int,
    version: int,
    representation: str,
    body: str,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    config: ParseConfig | None = None,
) -> dict[str, str]:
    """The IDs an artifact for this capture would have, without parsing it."""
    config = config or ParseConfig()
    body_sha = sha256_text(body)
    snap = snapshot_id(instance_id, page_id, version, representation, body_sha)
    fp = config.fingerprint()
    return {
        "snapshot_id": snap,
        "body_sha256": body_sha,
        "config_fingerprint": fp,
        "artifact_id": opaque_id("doc", snap, PARSER_VERSION, fp),
    }


# -- text -------------------------------------------------------------------------------


class _Text:
    """Evidence-text builder.

    Policy ``evidence-text.v1``: whitespace runs inside text nodes collapse to one space
    (as HTML renders them), except inside ``pre`` and plain-text macro bodies, which are
    kept verbatim; ``br`` and block boundaries become one newline; spaces around newlines
    and at both ends are trimmed.
    """

    def __init__(self) -> None:
        self.out = ""

    def text(self, s: str | None, verbatim: bool = False) -> None:
        if not s:
            return
        if not verbatim:
            s = _WS_RE.sub(" ", s)
            if not self.out or self.out.endswith("\n"):
                s = s.lstrip(" ")
        self.out += s

    def newline(self) -> None:
        self.out = self.out.rstrip(" ")
        if self.out and not self.out.endswith("\n"):
            self.out += "\n"

    def value(self) -> str:
        return self.out.strip(" \n")


def _render(el: etree._Element, buf: _Text, *, stop_at_tables: bool, out: list | None) -> None:
    """Append the visible text of ``el``'s content (not ``el.text``/tail handling of el)."""
    verbatim = qname(el) == "pre"
    buf.text(el.text, verbatim)
    for child in el:
        if not isinstance(child.tag, str):
            buf.text(child.tail, verbatim)
            continue
        name = qname(child)
        if name == "table" and stop_at_tables and out is not None:
            out.append(("text", buf.value()))
            buf.out = ""
            out.append(("table", child))
        elif name in _SKIP_TAGS:
            pass
        elif name == "br":
            buf.newline()
        elif name in _MACROS and ac_attr(child, "name") == "status":
            title = next(
                (
                    p.text
                    for p in child
                    if qname(p) == "ac:parameter" and ac_attr(p, "name") == "title"
                ),
                None,
            )
            buf.text(title)
        elif name == "ac:plain-text-body":
            buf.newline()
            buf.text(child.text, verbatim=True)
            buf.newline()
        elif name == "ac:link" and not any(
            qname(c) in ("ac:link-body", "ac:plain-text-link-body") for c in child
        ):
            for ref in child:
                if qname(ref).startswith("ri:"):
                    buf.text(
                        ri_attr(ref, "content-title")
                        or ri_attr(ref, "filename")
                        or ri_attr(ref, "space-key")
                        or "@" + (ri_attr(ref, "userkey") or ri_attr(ref, "username") or "user")
                    )
                    break
        else:
            block = name in _BLOCK_TAGS or name in ("td", "th", "table")
            if block:
                buf.newline()
            _render(child, buf, stop_at_tables=stop_at_tables, out=out)
            if block:
                buf.newline()
        buf.text(child.tail, verbatim)


def evidence_text(el: etree._Element) -> str:
    buf = _Text()
    _render(el, buf, stop_at_tables=False, out=None)
    return buf.value()


def _segments(cell: etree._Element) -> list[tuple[str, Any]]:
    """Cell content split at nested tables: ``("text", str)`` and ``("table", element)``."""
    out: list[tuple[str, Any]] = []
    buf = _Text()
    _render(cell, buf, stop_at_tables=True, out=out)
    out.append(("text", buf.value()))
    return [(kind, v) for kind, v in out if kind == "table" or v]


def normalize(text: str) -> str:
    return " ".join(text.split())


# -- parsing ------------------------------------------------------------------------------


def _parse_html(body: str) -> tuple[etree._Element, list[tuple[int, str]]]:
    parser = lxml.html.HTMLParser(no_network=True, remove_comments=False)
    if not body.strip():
        return etree.Element(ROOT_TAG), []
    root = lxml.html.fragment_fromstring(body, create_parent=ROOT_TAG, parser=parser)
    return root, [(e.line, e.type_name) for e in parser.error_log]


def _nearest(el: etree._Element, names: tuple[str, ...]) -> etree._Element | None:
    for ancestor in el.iterancestors():
        if isinstance(ancestor.tag, str) and qname(ancestor) in names:
            return ancestor
    return None


def _attrs(el: etree._Element) -> dict[str, str]:
    out = {}
    for key, value in el.attrib.items():
        if key.startswith("{"):
            ns, _, local = key[1:].partition("}")
            prefix = {"http://atlassian.com/content": "ac"}.get(ns, "ri")
            key = f"{prefix}:{local}"
        out[key] = value
    return out


def _is_layout_cell(el: etree._Element, representation: str) -> bool:
    if representation == "storage":
        return qname(el) == "ac:layout-cell"
    return el.tag == "div" and "cell" in (el.get("class") or "").split() and el.get("data-type")


@dataclass
class _Scope:
    kind: str  # document | layout_cell | table_cell | macro_body
    container_id: str | None = None
    inherited: list[dict] = field(default_factory=list)
    stack: list[dict] = field(default_factory=list)
    containers: list[dict] = field(default_factory=list)
    table_id: str | None = None
    cell_id: str | None = None
    depth: int = 0

    def path(self) -> list[dict]:
        return self.inherited + self.stack

    def child(self, kind: str, container: dict, **kw: Any) -> _Scope:
        return _Scope(
            kind,
            container["id"],
            self.path(),
            [],
            [*self.containers, container],
            kw.get("table_id", self.table_id),
            kw.get("cell_id", self.cell_id),
            kw.get("depth", self.depth),
        )


class _Builder:
    def __init__(self, artifact_id: str, representation: str, config: ParseConfig) -> None:
        self.artifact_id = artifact_id
        self.representation = representation
        self.config = config
        self.nodes: list[dict] = []
        self.blocks: dict[str, dict] = {}
        self.tables: dict[str, dict] = {}
        self.table_ids: dict[etree._Element, str] = {}
        self.ordinals: dict[str, int] = {}
        self.diagnostics: list[dict] = []
        # Content before the first heading is addressable as its own section.
        self.preamble_id = opaque_id("sec", artifact_id, "preamble")

    def block(self, kind: str, el: etree._Element, text: str, suffix: str = "") -> str:
        path = element_path(el) + suffix
        block_id = opaque_id("blk", self.artifact_id, path)
        self.blocks[block_id] = {
            "kind": kind,
            "element_path": path,
            "evidence_text": text,
            "normalized_text": normalize(text),
        }
        return block_id

    def node(self, kind: str, node_id: str, el: etree._Element, scope: _Scope, **extra) -> dict:
        path = scope.path()
        node = {
            "id": node_id,
            "kind": kind,
            "order": len(self.nodes),
            "container_id": scope.container_id,
            "cell_id": scope.cell_id,
            "section_id": path[-1]["id"] if path else self.preamble_id,
            "element_path": element_path(el),
            **extra,
        }
        self.nodes.append(node)
        return node

    # -- walk --

    def collect_tables(self, root: etree._Element) -> None:
        literal = 0
        for table in root.iter("table"):
            if _nearest(table, ("ac:plain-text-body",)) is not None:
                literal += 1
                continue
            table_id = opaque_id("tbl", self.artifact_id, element_path(table))
            self.table_ids[table] = table_id
            self.ordinals[table_id] = len(self.ordinals) + 1
        if literal:
            self.diagnostics.append(
                grid.diagnostic(
                    "LITERAL_TABLE_MARKUP",
                    "info",
                    f"{literal} table(s) inside plain-text macro bodies kept as literal text",
                )
            )

    def walk(self, el: etree._Element, scope: _Scope) -> None:
        for child in el:
            if not isinstance(child.tag, str):
                continue
            name = qname(child)
            if name in _HEADINGS:
                level = _HEADINGS[name]
                text = evidence_text(child)
                sec_id = opaque_id("sec", self.artifact_id, element_path(child))
                block_id = self.block("heading", child, text)
                while scope.stack and scope.stack[-1]["level"] >= level:
                    scope.stack.pop()
                ref = {"id": sec_id, "level": level, "text": normalize(text), "scope": scope.kind}
                scope.stack.append(ref)
                self.node(
                    "heading",
                    sec_id,
                    child,
                    scope,
                    level=level,
                    text=ref["text"],
                    block_id=block_id,
                )
                self.blocks[block_id]["section_id"] = sec_id
            elif name == "table" and child in self.table_ids:
                self.table(child, scope)
            elif name in _MACROS:
                macro_id = opaque_id("mac", self.artifact_id, element_path(child))
                macro_name = ac_attr(child, "name") or ""
                self.node("macro", macro_id, child, scope, name=macro_name)
                container = {"kind": "macro", "id": macro_id, "name": macro_name}
                for body in child:
                    if isinstance(body.tag, str) and qname(body) == "ac:rich-text-body":
                        self.walk(body, scope.child("macro_body", container))
            elif _is_layout_cell(child, self.representation):
                cell_node_id = opaque_id("lay", self.artifact_id, element_path(child))
                self.node("layout_cell", cell_node_id, child, scope)
                container = {"kind": "layout_cell", "id": cell_node_id}
                self.walk(child, scope.child("layout_cell", container))
            elif name in _FLOW_KINDS:
                self.flow(child, name, scope)
            elif name != "ac:plain-text-body":
                self.walk(child, scope)

    def flow(self, el: etree._Element, name: str, scope: _Scope) -> None:
        nested = any(
            isinstance(d.tag, str) and qname(d) in _STRUCTURAL for d in el.iterdescendants()
        )
        if not nested:
            text = evidence_text(el)
            if text:
                block_id = self.block(_FLOW_KINDS[name], el, text)
                self.node(_FLOW_KINDS[name], block_id, el, scope, block_id=block_id, text=text)
            return
        lead = _WS_RE.sub(" ", el.text or "").strip()
        if lead:  # text before the first nested block, e.g. a list item owning a sub-list
            block_id = self.block(_FLOW_KINDS[name], el, lead, "#lead")
            self.node(_FLOW_KINDS[name], block_id, el, scope, block_id=block_id, text=lead)
        self.walk(el, scope)

    def table(self, table: etree._Element, scope: _Scope, orphan_parent: str | None = None) -> None:
        table_id = self.table_ids[table]
        if table_id in self.tables:
            return
        depth = scope.depth + (1 if orphan_parent else 0)
        parent_table_id = orphan_parent or scope.table_id
        diags: list[dict] = []
        if orphan_parent:
            diags.append(
                grid.diagnostic(
                    "ORPHAN_NESTED_TABLE",
                    "warning",
                    "table is nested in another table outside any cell",
                    {"table_id": table_id},
                )
            )
        self.node("table", table_id, table, scope, ordinal=self.ordinals[table_id])

        caption_el = next(
            (c for c in table if isinstance(c.tag, str) and qname(c) == "caption"), None
        )
        caption = (
            self.block("caption", caption_el, evidence_text(caption_el))
            if caption_el is not None
            else None
        )

        rows_el = [tr for tr in table.iter("tr") if _nearest(tr, ("table",)) is table]
        groups: list[dict] = []
        group_of: dict[etree._Element, dict] = {}
        rows: list[dict] = []
        grid_rows: list[grid.RowInput] = []
        cells: list[dict] = []
        cell_elements: list[tuple[etree._Element, dict]] = []
        previous: dict | None = None
        for r, tr in enumerate(rows_el):
            # The nearest group-or-table ancestor is a group of this table or the table.
            group_el = _nearest(tr, (*_GROUP_TAGS, "table"))
            explicit = group_el is not None and group_el is not table
            if explicit and group_el in group_of:
                group = group_of[group_el]
            elif not explicit and previous is not None and previous["kind"] == "implicit":
                group = previous  # consecutive rows outside any group share an implicit one
            else:
                kind = qname(group_el) if explicit else "implicit"
                group = {"id": f"g{len(groups) + 1}", "kind": kind, "rows": []}
                groups.append(group)
                if explicit:
                    group_of[group_el] = group
            previous = group
            group["rows"].append(r + 1)
            row_cells = [c for c in tr.iter("td", "th") if _nearest(c, ("tr",)) is tr]
            ids = []
            inputs = []
            for i, cell in enumerate(row_cells):
                cell_id = opaque_id("cell", table_id, r, i)
                ids.append(cell_id)
                inputs.append(grid.CellInput(cell_id, cell.get("rowspan"), cell.get("colspan")))
                record = {
                    "cell_id": cell_id,
                    "row_index": r + 1,
                    "cell_index": i + 1,
                    "tag": cell.tag,
                    "attributes": _attrs(cell),
                    "raw_rowspan": cell.get("rowspan"),
                    "raw_colspan": cell.get("colspan"),
                    "header_metadata": {
                        k: cell.get(k) for k in ("scope", "headers", "id", "abbr") if cell.get(k)
                    },
                    "element_path": element_path(cell),
                }
                cells.append(record)
                cell_elements.append((cell, record))
            rows.append({"row_index": r + 1, "group_id": group["id"], "cell_ids": ids})
            grid_rows.append(grid.RowInput(group["id"], inputs))

        limits = self.config.limits
        if depth + 1 > limits.max_depth:
            result = grid.Layout(
                "unsupported_due_to_limit",
                len(rows),
                0,
                [],
                [
                    grid.diagnostic(
                        "RESOURCE_LIMIT_EXCEEDED",
                        "blocker",
                        f"nesting depth {depth + 1} exceeds the limit of {limits.max_depth}",
                    )
                ],
            )
        else:
            result = grid.layout(grid_rows, limits)
        diags.extend(result.diagnostics)
        placed = {p.cell_id: p for p in result.placements}
        for record in cells:
            p = placed.get(record["cell_id"])
            record.update(
                {
                    "origin": {"row": p.row, "col": p.col} if p else None,
                    "requested_rowspan": p.requested_rowspan if p else None,
                    "requested_colspan": p.requested_colspan if p else None,
                    "rowspan": p.rowspan if p else None,
                    "colspan": p.colspan if p else None,
                }
            )

        self.tables[table_id] = {
            "table_id": table_id,
            "ordinal": self.ordinals[table_id],
            "element_path": element_path(table),
            "depth": depth,
            "parent_table_id": parent_table_id,
            "parent_cell_id": None if orphan_parent else scope.cell_id,
            "attributes": _attrs(table),
            "heading_path": scope.path(),
            "local_heading_count": len(scope.stack),
            "containers": scope.containers,
            "context": self.context(table, caption),
            "row_order_policy": "source_dom",
            "row_groups": groups,
            "rows": rows,
            "cells": cells,
            "row_count": result.row_count,
            "column_count": result.column_count,
            "geometry_status": result.status,
            "extraction_eligible": result.status == "valid",
            "child_table_ids": [],
            "diagnostics": diags,
            "source_markup": serialize(table),
        }

        for cell, record in cell_elements:
            record["content"] = []
            for i, (kind, value) in enumerate(_segments(cell)):
                if kind == "text":
                    block_id = self.block("cell_text", cell, value, f"#seg{i + 1}")
                    self.blocks[block_id]["cell_id"] = record["cell_id"]
                    record["content"].append({"kind": "text", "block_id": block_id})
                elif value in self.table_ids:
                    record["content"].append({"kind": "table", "table_id": self.table_ids[value]})
            record["direct_text"] = normalize(
                " ".join(
                    self.blocks[c["block_id"]]["evidence_text"]
                    for c in record["content"]
                    if c["kind"] == "text"
                )
            )
            record["links"] = _links(cell)
            container = {"kind": "table_cell", "id": record["cell_id"], "table_id": table_id}
            self.walk(
                cell,
                scope.child(
                    "table_cell",
                    container,
                    table_id=table_id,
                    cell_id=record["cell_id"],
                    depth=depth + 1,
                ),
            )
            self.tables[table_id]["child_table_ids"].extend(
                c["table_id"] for c in record["content"] if c["kind"] == "table"
            )

    def context(self, table: etree._Element, caption: str | None) -> dict:
        anchor = table
        parent = anchor.getparent()
        while (
            parent is not None
            and parent.tag == "div"
            and len([c for c in parent if isinstance(c.tag, str)]) == 1
        ):
            anchor, parent = parent, parent.getparent()
        return {
            "caption_block_id": caption,
            "preceding": self._neighbour(anchor, preceding=True),
            "following": self._neighbour(anchor, preceding=False),
            "budget_chars": self.config.context_chars,
        }

    def _neighbour(self, anchor: etree._Element, *, preceding: bool) -> dict | None:
        for sib in anchor.itersiblings(preceding=preceding):
            if not isinstance(sib.tag, str):
                continue
            name = qname(sib)
            if name in _HEADINGS or name == "table" or next(sib.iter("table"), None) is not None:
                return None
            if name not in _CONTEXT_TAGS:
                if name in _MACROS or name.startswith("ac:layout"):
                    return None  # container boundary
                continue
            text = evidence_text(sib)
            if not text:
                continue
            if len(text) > self.config.context_chars:
                return {"block_id": None, "reason": "exceeds_context_budget"}
            kind = "preceding_block" if preceding else "following_block"
            return {"block_id": self.block("paragraph", sib, text), "reason": kind}
        return None


def _links(cell: etree._Element) -> list[dict]:
    out = []
    for el in cell.iter():
        if not isinstance(el.tag, str) or _nearest(el, ("td", "th")) is not cell:
            continue
        name = qname(el)
        if name == "a" and el.get("href"):
            out.append({"kind": "href", "target": el.get("href")})
        elif name == "ac:link":
            ref = next(
                (resource_ref(c) for c in el if isinstance(c.tag, str) and qname(c)[:3] == "ri:"),
                None,
            )
            item: dict[str, Any] = {"kind": "resource", "target": ref}
            if ac_attr(el, "anchor"):
                item["anchor"] = ac_attr(el, "anchor")
            out.append(item)
    return out


def parse_document(
    body: str | None,
    *,
    page_id: int,
    version: int,
    representation: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    config: ParseConfig | None = None,
) -> dict[str, Any]:
    if representation not in REPRESENTATIONS:
        raise ValueError(f"unknown representation {representation!r}")
    config = config or ParseConfig()
    body = body or ""
    identity = artifact_identity(
        page_id, version, representation, body, instance_id=instance_id, config=config
    )
    artifact: dict[str, Any] = {
        **identity,
        "instance_id": instance_id,
        "page_id": page_id,
        "version_number": version,
        "representation": representation,
        "parser_version": PARSER_VERSION,
        "status": "parsed",
        "diagnostics": [],
        "nodes": [],
        "blocks": {},
        "tables": [],
    }
    size = len(body.encode("utf-8"))
    if size > config.limits.max_body_bytes:
        artifact["status"] = "unsupported_due_to_limit"
        artifact["diagnostics"].append(
            grid.diagnostic(
                "RESOURCE_LIMIT_EXCEEDED",
                "blocker",
                f"body of {size} bytes exceeds the limit of {config.limits.max_body_bytes}",
            )
        )
        return artifact

    if representation == "storage":
        root, recoveries = parse_storage_report(body)
    else:
        root, recoveries = _parse_html(body)
    builder = _Builder(identity["artifact_id"], representation, config)
    if recoveries:
        artifact["status"] = "recovered"
        builder.diagnostics.append(
            grid.diagnostic(
                "PARSER_RECOVERY_APPLIED",
                "warning",
                f"parser repaired {len(recoveries)} problem(s) in the markup",
                {"repairs": [{"line": line, "type": kind} for line, kind in recoveries[:20]]},
            )
        )
    builder.collect_tables(root)
    builder.walk(root, _Scope("document"))
    # Tables the scoped walk could not reach: nested outside a cell, or inside elements the
    # walk does not descend into (headings, macro parameters).
    for table, table_id in builder.table_ids.items():
        if table_id not in builder.tables:
            owner = _nearest(table, ("table", "td", "th"))
            if owner is not None and owner.tag == "table":
                builder.table(table, _Scope("document"), orphan_parent=builder.table_ids[owner])
            else:
                builder.table(table, _Scope("document"))
                builder.tables[table_id]["diagnostics"].append(
                    grid.diagnostic(
                        "CONTEXT_UNAVAILABLE",
                        "warning",
                        "table sits where document scope could not be resolved",
                        {"table_id": table_id},
                    )
                )
    artifact["diagnostics"] = builder.diagnostics
    artifact["nodes"] = builder.nodes
    artifact["blocks"] = builder.blocks
    artifact["tables"] = sorted(builder.tables.values(), key=lambda t: t["ordinal"])
    return artifact
