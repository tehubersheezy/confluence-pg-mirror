"""Plain-text rendering. Every string from the mirror passes through :func:`safe`."""

from __future__ import annotations

import argparse
from typing import Any

from confluence_mirror.confex.core import dumps, safe


def kv(rows: list[tuple[str, Any]]) -> str:
    width = max((len(k) for k, _ in rows), default=0)
    return "".join(f"{k.ljust(width)}  {safe(_scalar(v))}\n" for k, v in rows)


def _scalar(v: Any) -> Any:
    return dumps(v) if isinstance(v, dict | list) else v


def table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[safe(_scalar(c)).replace("\n", " ") for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()
    out = [line]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(out) + "\n"


def preview(text: str, width: int, full: bool) -> str:
    one = safe(text).replace("\n", " ⏎ ")
    if full or len(one) <= width:
        return one
    return one[: max(width - 1, 1)] + "…"


def tree_lines(nodes: list[dict], indent: int = 0) -> list[str]:
    out = []
    for n in nodes:
        extra = []
        if n.get("role") == "context":
            extra.append("context")
        if n.get("descendants_known") is not None:
            extra.append(f"{n['descendants_known']} descendants")
        if n.get("hidden_children"):
            extra.append(f"+{n['hidden_children']} hidden")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        out.append(f"{'  ' * indent}{safe(n['title'])} [{n['page_id']}]{suffix}")
        out += tree_lines(n.get("children", []), indent + 1)
    return out


def diag_lines(diagnostics: list[dict]) -> str:
    return "".join(f"! {d['severity']} {d['code']}: {safe(d['message'])}\n" for d in diagnostics)


def render_plain(data: Any, args: argparse.Namespace) -> str:
    """Generic fallback: a list of dicts becomes a table, a dict becomes key/value lines."""
    if isinstance(data, list):
        if not data:
            return "(none)\n"
        if all(isinstance(x, dict) for x in data):
            headers = list(data[0])[:8]
            return table(headers, [[x.get(h) for h in headers] for x in data])
        return "".join(f"{safe(x)}\n" for x in data)
    if isinstance(data, dict):
        return kv(list(data.items()))
    return f"{safe(data)}\n"


def items_table(key: str, headers: list[str]):
    """Renderer for ``{key: [rows]}`` payloads that prints chosen columns."""

    def render(data: dict, args: argparse.Namespace) -> str:
        rows = data[key] if isinstance(data, dict) else data
        out = table(headers, [[r.get(h) for h in headers] for r in rows]) if rows else "(none)\n"
        extras = {k: v for k, v in data.items() if k != key} if isinstance(data, dict) else {}
        notes = [f"-- {safe(v)}" for k, v in extras.items() if k in ("note", "inbound_coverage")]
        return out + ("\n".join(notes) + "\n" if notes else "")

    return render
