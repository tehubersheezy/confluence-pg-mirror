"""tables list | show | tree | compare."""

from __future__ import annotations

import argparse

from confluence_mirror import inspection as ins
from confluence_mirror import repository as repo
from confluence_mirror.confex.core import (
    Context,
    Result,
    collection_args,
    command,
    group,
    leaf,
    paginate,
    positive,
    safe,
)
from confluence_mirror.confex.nav import REP_CHOICES, plain_table_list
from confluence_mirror.confex.render import diag_lines, kv, preview, table
from confluence_mirror.structure import REPRESENTATIONS


def register(sub: argparse._SubParsersAction) -> None:
    tg = group(sub, "tables", "table occurrences")
    tl = leaf(tg, "tables.list")
    tl.add_argument("--space", metavar="KEY")
    tl.add_argument("--page", type=int)
    tl.add_argument("--nested", choices=("any", "only", "none"), default="any")
    tl.add_argument("--representation", choices=REPRESENTATIONS)
    collection_args(tl)
    ts = leaf(tg, "tables.show")
    ts.add_argument("selector", help="tbl_<id> or PAGE:tNN")
    ts.add_argument("--representation", "--source", choices=REP_CHOICES, default="auto")
    ts.add_argument("--version", dest="page_version", type=int)
    ts.add_argument("--snapshot")
    ts.add_argument("--view", choices=ins.VIEWS, default="overview")
    ts.add_argument("--cell", help="coordinate for --view cell, e.g. R3C1")
    ts.add_argument("--rows", help="inclusive range A:B, or all")
    ts.add_argument("--cols", help="inclusive range A:B, or all")
    ts.add_argument("--full", action="store_true", help="do not truncate text previews")
    ts.add_argument("--max-col-width", type=positive, default=32)
    ts.add_argument("--stage", choices=("classify", "extract"), default="extract")
    ts.add_argument("--chunk", type=int, default=1)
    ts.add_argument("--extraction-profile", dest="profile")


@command("tables.list", collection=True, plain=plain_table_list)
def tables_list(args, ctx: Context) -> Result:
    conn = ctx.conn()
    space_id = ins._space(conn, args.space)["space_id"] if args.space else None
    items = repo.list_table_occurrences(conn, space_id, args.page, args.nested, args.representation)
    data, completeness = paginate(
        items,
        args,
        {
            "scope": "persisted table occurrences of current versions",
            "s": args.space,
            "p": args.page,
            "n": args.nested,
            "r": args.representation,
        },
    )
    return Result(data, completeness=completeness)


def _source(resolved: ins.Resolved) -> dict:
    return {
        "snapshot_id": resolved.artifact["snapshot_id"],
        "artifact_id": resolved.artifact["artifact_id"],
        "observed_at": resolved.fetched_at,
    }


@command("tables.show", plain=lambda d, a: plain_table_view(d, a))
def tables_show(args, ctx: Context) -> Result:
    conn = ctx.conn()
    resolved, tbl = ins.resolve_table(
        conn,
        args.selector,
        representation=args.representation,
        version=args.page_version,
        snapshot=args.snapshot,
    )
    data = ins.table_view(
        conn,
        resolved,
        tbl,
        args.view,
        cell=args.cell,
        rows=args.rows,
        cols=args.cols,
        bounded=args.format == "plain",
    )
    completeness = {}
    if args.view == "grid":
        sel = data["selection"]
        completeness = {
            "scope": "selected_table",
            "state": "complete" if sel["complete"] else "partial",
            "has_more": False,
            "truncated": not sel["complete"],
            "reasons": [] if sel["complete"] else ["row/column selection or display bound"],
        }
    return Result(data, source=_source(resolved), completeness=completeness)


def plain_table_view(data: dict, args: argparse.Namespace) -> str:
    ident = data["identity"]
    head = (
        f"{ident['table_id']}  {ident['ordinal_selector']}  {ident['representation']} "
        f"v{ident['version_number']}  {ident['snapshot_id']}  ({ident['parse_mode']})\n"
    )
    view = args.view
    width = args.max_col_width
    if view == "grid":
        sel = data["selection"]
        c_lo, c_hi = sel["cols"]
        headers = [""] + [f"C{c}" for c in range(c_lo, c_hi + 1)]
        rows = []
        for line in data["grid"]:
            coord = line[0]["coord"].split("C")[0] if line else ""
            cells = []
            for slot in line:
                if slot["kind"] == "origin":
                    text = slot["text"]
                    if slot["nested"]:
                        text = (text + " " if text else "") + " ".join(
                            f"nested:{t}" for t in slot["nested"]
                        )
                    cells.append(preview(text, width, args.full))
                elif slot["kind"] == "covered":
                    cells.append(f"covered:{slot['labels'][0]}")
                elif slot["kind"] == "conflict":
                    cells.append(f"conflict:{','.join(slot['labels'])}")
                else:
                    cells.append("")
            rows.append([coord, *cells])
        out = head + table(headers, rows)
        if sel["hidden_rows"] or sel["hidden_cols"]:
            out += (
                f"-- showing rows {sel['rows'][0]}-{sel['rows'][1]}, cols {c_lo}-{c_hi}; "
                f"hidden: {sel['hidden_rows']} rows, {sel['hidden_cols']} cols "
                "(use --rows/--cols)\n"
            )
        return out + diag_lines(data["diagnostics"])
    if view == "physical":
        lines = [head.rstrip()]
        for row in data["rows"]:
            lines.append(f"row {row['row_index']} ({row['group_id']})")
            for c in row["cells"]:
                o = c["origin"]
                origin = f"R{o['row']}C{o['col']}" if o else "unplaced"
                spans = f"rowspan={c['raw_rowspan'] or '-'} colspan={c['raw_colspan'] or '-'}"
                lines.append(
                    f"  {c['label']} <{c['tag']}> {origin} {spans} "
                    f"eff={c['rowspan']}x{c['colspan']}  "
                    f"{preview(c['direct_text'], width, args.full)}"
                )
                for b in c["blocks"]:
                    if b["kind"] == "table":
                        lines.append(f"      nested {b['table_id']}")
        return "\n".join(lines) + "\n" + diag_lines(data["diagnostics"])
    if view == "cell":
        lines = [head.rstrip(), f"{data['coord']}: {data['kind']}"]
        for o in data["owners"]:
            lines.append(f"  -> {o['label']} {o['cell_id']} <{o['tag']}> span {o['span']}")
            for b in o["blocks"]:
                if b["kind"] == "text":
                    lines.append(f"     [{b['block_id']}] {safe(b['evidence_text'])}")
                else:
                    lines.append(f"     nested {b['table_id']}")
        if data["heading_path"]:
            lines.append("  headings: " + " > ".join(safe(h["text"]) for h in data["heading_path"]))
        return "\n".join(lines) + "\n" + diag_lines(data["diagnostics"])
    if view == "context":
        lines = [
            head.rstrip(),
            f"page: {safe(data['page']['title'])} [{data['page']['page_id']}] "
            f"space {data['page']['space_key']}",
            "headings: "
            + (
                " > ".join(f"h{h['level']} {safe(h['text'])}" for h in data["heading_path"])
                or "(none)"
            ),
        ]
        for c in data["containers"]:
            lines.append(f"container: {c['kind']} {c.get('name') or ''} {c['id']}".rstrip())
        for label in ("caption", "preceding", "following"):
            item = data[label]
            block = item.get("block") if isinstance(item, dict) and "reason" in item else item
            if block:
                lines.append(f"{label}: {safe(block['evidence_text'])}")
            elif isinstance(item, dict):
                lines.append(f"{label}: (skipped: {item['reason']})")
        return "\n".join(lines) + "\n"
    if view == "raw":
        return head + f"-- {data['note']}\n" + safe(data["markup"]) + "\n"
    rows = [(k, v) for k, v in data.items() if k not in ("identity", "diagnostics")]
    return (
        head
        + kv(
            [
                (
                    k,
                    v,
                )
                for k, v in rows
            ]
        )
        + diag_lines(data["diagnostics"])
    )
