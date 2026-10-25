"""tables list | show | tree | compare."""

from __future__ import annotations

import argparse
import json

from confluence_mirror import inspection as ins
from confluence_mirror import repository as repo
from confluence_mirror.confex.core import (
    Context,
    Result,
    actor,
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

    tt = leaf(tg, "tables.tree", "a table, its ancestors and nested descendants")
    tt.add_argument("selector")
    tt.add_argument("--representation", choices=REP_CHOICES, default="auto")
    tt.add_argument("--version", dest="page_version", type=int)
    tt.add_argument("--snapshot")
    tc = leaf(tg, "tables.compare", "structural comparison of two occurrences")
    tc.add_argument("selector")
    tc.add_argument("--other", required=True)
    tsel = leaf(tg, "tables.selection", "which occurrence extraction uses per authored table")
    tsel.add_argument("page_id", type=int)
    tsel.add_argument("--version", dest="page_version", type=int)
    tr = leaf(tg, "tables.resolve", "decide a tentative or conflicting correspondence")
    tr.add_argument("correspondence_id")
    tr.add_argument("--decision", choices=("confirmed", "distinct"), required=True)
    tr.add_argument("--reason", required=True)

    al = leaf(sub, "align", "compute cross-representation table correspondence (offline)")
    scope = al.add_mutually_exclusive_group(required=True)
    scope.add_argument("--space", metavar="KEY")
    scope.add_argument("--page", type=int, action="append", metavar="ID")


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
    if args.view == "payload":
        return _payload_view(args, ctx, resolved, tbl)
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
    if view == "payload":
        unit = data["unit"]
        out = head + kv(
            [
                ("preview", data["preview_kind"]),
                (
                    "classification",
                    f"{data['classification_id']} ({data['classification_source']})",
                ),
                ("profile", data["profile"]),
                ("unit", f"{unit['unit_id']} chunk {unit['chunk_index']}/{unit['chunk_count']}"),
                ("rows", unit["selected_rows"]),
                ("extractor", data["extractor"]),
                ("egress", data["egress"]),
                ("redactions", data["redactions"]),
                ("estimated_input_tokens", f"{data['estimated_input_tokens']} (local estimate)"),
                *data["hashes"].items(),
            ]
        )
        return out + json.dumps(data["request"], indent=2, ensure_ascii=False) + "\n"
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


def _plain_tree(data, args):
    lines = []
    for a in data["ancestors"]:
        lines.append(f"(parent) {a['table_id']} t{a['ordinal']:02d}")

    def walk(n, depth):
        lines.append(
            f"{'  ' * depth}{n['selector']} {n['table_id']} {n['rows']}x{n['columns']} "
            f"{n['geometry_status']}"
            + (f"  in {n['parent_cell_id']}" if n["parent_cell_id"] else "")
        )
        for c in n["children"]:
            walk(c, depth + 1)

    walk(data["tree"], 0)
    return "\n".join(lines) + "\n"


@command("tables.tree", plain=_plain_tree)
def tables_tree(args, ctx: Context) -> Result:
    conn = ctx.conn()
    resolved, tbl = ins.resolve_table(
        conn,
        args.selector,
        representation=args.representation,
        version=args.page_version,
        snapshot=args.snapshot,
    )
    return Result(ins.table_tree(conn, resolved, tbl), source=_source(resolved))


def _plain_compare(data, args):
    out = kv(
        [
            (k, data[k])
            for k in (
                "left",
                "right",
                "dimensions",
                "same_geometry",
                "same_text",
                "same_header",
                "same_heading_path",
                "similarity",
            )
        ]
    )
    stored = data["stored_correspondence"]
    out += f"stored correspondence: {stored['state'] if stored else 'none'}\n"
    for d in data["cell_differences"][:50]:
        out += f"  {d['coord']}: {safe(d['left'])} | {safe(d['right'])}\n"
    return out


@command("tables.compare", plain=_plain_compare)
def tables_compare(args, ctx: Context) -> Result:
    return Result(ins.compare_tables(ctx.conn(), args.selector, args.other))


def _plain_selection(data, args):
    rows = [
        [
            s["table_id"],
            s["representation"],
            len(s["alternates"]),
            "BLOCKED" if s["blocked"] else "ok",
            s["block_reason"] or s["reason"],
        ]
        for s in data["selections"]
    ]
    return table(["table_id", "representation", "alternates", "status", "reason"], rows)


@command("tables.selection", plain=_plain_selection)
def tables_selection(args, ctx: Context) -> Result:
    return Result(ins.selection(ctx.conn(), args.page_id, args.page_version))


@command("tables.resolve", writes=True)
def tables_resolve(args, ctx: Context) -> Result:
    return Result(
        ins.decide_correspondence(
            ctx.conn(), args.correspondence_id, args.decision, actor(), args.reason
        )
    )


def _plain_align(data, args):
    out = kv([(k, v) for k, v in data.items() if k != "items"])
    for item in data["items"]:
        detail = item.get("detail") or item.get("states")
        out += (
            f"page {item['page_id']} v{item['version_number']}: {item['status']} {safe(detail)}\n"
        )
    return out


@command("align", writes=True, plain=_plain_align)
def align_cmd(args, ctx: Context) -> Result:
    data = ins.align_pages(ctx.conn(), space_key=args.space, page_ids=args.page)
    return Result(data)


def _payload_view(args, ctx: Context, resolved: ins.Resolved, tbl: dict) -> Result:
    """The production serializer's unit, payload and request; no model call, no writes."""
    from confluence_mirror import pipeline as pipe
    from confluence_mirror.confex.extract import options, pipeline_for

    if args.stage != "extract":
        raise ins.ConfexError(
            "UNSUPPORTED_MODE", "only --stage extract payloads can be previewed", ins.EXIT_USAGE
        )
    p = pipeline_for(ctx)
    target = pipe.Target(
        tbl, resolved.artifact, repo.get_page(ctx.conn(), resolved.artifact["page_id"])
    )
    ns = argparse.Namespace(
        profile=args.profile, extractor=None, force=False, model_classification=False
    )
    data = p.preview(target, options(ns, ctx), args.chunk)
    data["identity"] = ins.identity(resolved, tbl)
    return Result(data, source=_source(resolved))
