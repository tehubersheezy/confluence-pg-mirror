"""Table extraction: cells with spans, header detection, and header-keyed records."""

from __future__ import annotations

from lxml import etree

from confluence_mirror.extract.macros import nearest_macro
from confluence_mirror.extract.storage import element_path, serialize, text_of


def _owner_table(el: etree._Element) -> etree._Element | None:
    for ancestor in el.iterancestors():
        if ancestor.tag == "table":
            return ancestor
    return None


def _span(cell: etree._Element, name: str) -> int:
    try:
        return max(int(cell.get(name, "1")), 1)
    except ValueError:
        return 1


def _unique_headers(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, name in enumerate(names):
        key = name or f"column_{i + 1}"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        out.append(key)
    return out


def table_rows(root: etree._Element, macro_index: dict[etree._Element, int]) -> list[dict]:
    tables = list(root.iter("table"))
    index = {t: i for i, t in enumerate(tables)}
    out: list[dict] = []
    for table, i in index.items():
        rows = [tr for tr in table.iter("tr") if _owner_table(tr) is table]
        cells: list[list[dict]] = []
        for tr in rows:
            cells.append(
                [
                    {
                        "text": text_of(c),
                        "is_header": c.tag == "th",
                        "colspan": _span(c, "colspan"),
                        "rowspan": _span(c, "rowspan"),
                    }
                    for c in tr
                    if c.tag in ("th", "td")
                ]
            )
        column_count = max((sum(c["colspan"] for c in r) for r in cells), default=0)
        header = records = None
        if cells and cells[0] and all(c["is_header"] for c in cells[0]) and len(cells) > 1:
            header = _unique_headers([c["text"] for c in cells[0]])
            records = []
            for row in cells[1:]:
                if any(c["colspan"] > 1 for c in row):
                    records = None  # spans make column alignment ambiguous; keep cells only
                    break
                records.append({header[j]: c["text"] for j, c in enumerate(row) if j < len(header)})
        parent_table = _owner_table(table)
        macro = nearest_macro(table)
        out.append(
            {
                "table_index": i,
                "parent_table_index": index.get(parent_table) if parent_table is not None else None,
                "macro_index": macro_index.get(macro) if macro is not None else None,
                "element_path": element_path(table),
                "row_count": len(cells),
                "column_count": column_count,
                "header": header,
                "cells": cells,
                "records": records,
                "source_markup": serialize(table),
            }
        )
    return out
