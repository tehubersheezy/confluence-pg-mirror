"""db, parse, sync, spaces, pages and search commands."""

from __future__ import annotations

import argparse
import dataclasses
from typing import Any

from confluence_mirror import db, live
from confluence_mirror import inspection as ins
from confluence_mirror import repository as repo
from confluence_mirror.client import AuthenticationError, ConfluenceError
from confluence_mirror.confex.core import (
    Context,
    Result,
    collection_args,
    command,
    group,
    leaf,
    paginate,
    positive,
    representations_arg,
    safe,
)
from confluence_mirror.confex.render import diag_lines, items_table, kv, table, tree_lines
from confluence_mirror.config import PROPERTY_MODES
from confluence_mirror.inspection import ConfexError
from confluence_mirror.structure import REPRESENTATIONS
from confluence_mirror.sync import Scope, SpaceSync, SyncLockedError, reextract

REP_CHOICES = ("auto", *REPRESENTATIONS)


def register(sub: argparse._SubParsersAction) -> None:
    dbg = group(sub, "db", "schema status and migrations")
    leaf(dbg, "db.status", "applied and pending migrations")
    dm = leaf(dbg, "db.migrate", "apply pending migrations (the only schema-changing command)")
    dm.add_argument("--target", type=int, metavar="N", help="stop after migration N")

    rx = leaf(sub, "reextract", "rebuild extracted.* macros, tables and links (offline)")
    rx_scope = rx.add_mutually_exclusive_group()
    rx_scope.add_argument("--space", metavar="KEY")
    rx_scope.add_argument("--page", type=int, action="append", metavar="ID")
    rx.add_argument(
        "--all-versions", action="store_true", help="every stored version, not just current ones"
    )

    p = leaf(sub, "parse", "persist structural artifacts for stored bodies (offline)")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--space", metavar="KEY")
    scope.add_argument("--page", type=int, action="append", metavar="ID")
    p.add_argument(
        "--representations",
        type=representations_arg,
        default=["storage", "export_view"],
        help="comma-separated subset of storage,export_view,view, or 'all'",
    )
    p.add_argument("--all-versions", action="store_true")

    s = leaf(sub, "sync", "read Confluence and write the mirror (the only source writer)")
    scope = s.add_mutually_exclusive_group(required=True)
    scope.add_argument("--space", action="append", metavar="KEY", help="space key (repeatable)")
    scope.add_argument("--all", action="store_true", help="every space in [sync].spaces")
    scope.add_argument(
        "--page", type=int, action="append", metavar="ID", help="a page (repeatable; re-fetched)"
    )
    scope.add_argument("--root", type=int, metavar="ID", help="a page and all its descendants")
    scope.add_argument("--cql", metavar="QUERY", help="every page a CQL search returns")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--metadata-only", action="store_true", help="list pages; fetch no bodies")
    mode.add_argument("--changed-only", action="store_true", help="incremental space sync")
    mode.add_argument("--reconcile", action="store_true", help="full space re-fetch")
    s.add_argument("--representations", type=representations_arg)
    s.add_argument(
        "--properties",
        choices=PROPERTY_MODES,
        help="content properties for this run: known (default; no extra requests), "
        "all (one request per fetched page; finds new keys), none",
    )

    sp = group(sub, "spaces", "spaces")
    sl = leaf(sp, "spaces.list")
    sl.add_argument("--match")
    sl.add_argument("--type", dest="space_type")
    collection_args(sl)
    leaf(sp, "spaces.show").add_argument("key")
    st = leaf(sp, "spaces.tree")
    st.add_argument("key")
    st.add_argument("--depth", type=positive)
    st.add_argument("--match")
    st.add_argument("--counts", action="store_true")

    pg = group(sub, "pages", "page navigation, structure and metadata")
    leaf(pg, "pages.show").add_argument("page_id", type=int)
    for name in ("children", "siblings"):
        pc = leaf(pg, f"pages.{name}")
        pc.add_argument("page_id", type=int)
        collection_args(pc)
    leaf(pg, "pages.ancestors").add_argument("page_id", type=int)
    pt = leaf(pg, "pages.tree")
    pt.add_argument("--root", type=int, required=True)
    pt.add_argument("--depth", type=positive, default=3)
    pt.add_argument("--allow-scan", action="store_true", help="live: allow several requests")
    pt.add_argument("--max-requests", type=positive, default=100)
    for name in ("outline", "tables", "read"):
        po = leaf(pg, f"pages.{name}")
        po.add_argument("page_id", type=int)
        po.add_argument("--representation", "--source", choices=REP_CHOICES, default="auto")
        po.add_argument("--version", dest="page_version", type=int)
        if name == "outline":
            po.add_argument("--tables", action="store_true")
            po.add_argument("--macros", action="store_true")
        elif name == "tables":
            collection_args(po)
        else:
            po.add_argument("--mode", choices=("text", "raw"), default="text")
            po.add_argument("--section-id")
    leaf(pg, "pages.labels").add_argument("page_id", type=int)
    pp = leaf(pg, "pages.properties")
    pp.add_argument("page_id", type=int)
    pp.add_argument("--key")
    pa = leaf(pg, "pages.attachments")
    pa.add_argument("page_id", type=int)
    pa.add_argument("--include-deleted", action="store_true")
    leaf(pg, "pages.versions").add_argument("page_id", type=int)
    pm = leaf(pg, "pages.macros")
    pm.add_argument("page_id", type=int)
    pm.add_argument("--name")
    pm.add_argument("--version", dest="page_version", type=int)
    pl = leaf(pg, "pages.links")
    pl.add_argument("page_id", type=int)
    pl.add_argument("--direction", choices=("out", "in", "both"), default="out")
    pd = leaf(pg, "pages.diff")
    pd.add_argument("page_id", type=int)
    pd.add_argument("--from-snapshot")
    pd.add_argument("--to-snapshot")
    pd.add_argument("--from-version", type=int)
    pd.add_argument("--to-version", type=int)
    pd.add_argument("--representation", choices=REP_CHOICES, default="storage")

    se = leaf(sub, "search", "local substring search, or live CQL")
    se.add_argument("--space", metavar="KEY")
    se.add_argument("--title")
    se.add_argument("--text")
    se.add_argument("--label")
    se.add_argument("--has-tables", action="store_true")
    se.add_argument("--cql", help="native CQL; requires --data-source live")
    se.add_argument("--limit", type=positive, default=200)


def _source(resolved: ins.Resolved) -> dict:
    return {
        "snapshot_id": resolved.artifact["snapshot_id"],
        "artifact_id": resolved.artifact["artifact_id"],
        "observed_at": resolved.fetched_at,
    }


# -- db / parse / sync ------------------------------------------------------------------------


def _plain_db_status(data, args):
    return table(
        ["version", "name", "applied", "checksum_ok"],
        [[m["version"], m["name"], m["applied"], m["checksum_ok"]] for m in data],
    )


@command("db.status", schema=False, plain=_plain_db_status)
def db_status(args, ctx: Context) -> Result:
    return Result(db.migration_status(ctx.conn()))


@command(
    "db.migrate",
    writes=True,
    schema=False,
    plain=lambda d, a: "".join(f"applied {m}\n" for m in d["applied"]) or "up to date\n",
)
def db_migrate(args, ctx: Context) -> Result:
    applied = db.migrate(ctx.conn(), args.target)
    return Result({"applied": [f"{m.version:04d}_{m.name}" for m in applied]})


def _plain_counts(data, args):
    if "runs" in data:
        return "\n".join(_plain_counts(r, args) for r in data["runs"])
    out = kv([(k, v) for k, v in data.items() if k != "errors"])
    for e in data.get("errors", []):
        out += f"error {safe(e)}\n"
    return out


@command("reextract", writes=True, plain=_plain_counts)
def reextract_cmd(args, ctx: Context) -> Result:
    confluence = ctx.settings.confluence
    data = reextract(
        ctx.conn(),
        base_url=confluence.base_url if confluence else None,
        space_key=args.space,
        page_ids=args.page,
        all_versions=args.all_versions,
    )
    return Result(data, exit_code=ins.EXIT_PARTIAL if data["failed"] else ins.EXIT_OK)


@command("parse", writes=True, plain=_plain_counts)
def parse_cmd(args, ctx: Context) -> Result:
    data = ins.parse_pages(
        ctx.conn(),
        space_key=args.space,
        page_ids=args.page,
        representations=args.representations,
        all_versions=args.all_versions,
    )
    return Result(data, exit_code=ins.EXIT_PARTIAL if data["failed"] else ins.EXIT_OK)


def sync_scope(args, *, full: bool) -> Scope:
    """The bulk sync selection named by ``--page``/``--root``/``--cql``."""
    if args.page:  # named pages are always re-fetched, as a repair tool
        return Scope("pages", page_ids=tuple(args.page), full=True)
    if getattr(args, "root", None) is not None:
        return Scope("tree", root=args.root, full=full)
    return Scope("cql", cql=getattr(args, "cql", None), full=full)


@command("sync", writes=True, plain=_plain_counts)
def sync_cmd(args, ctx: Context) -> Result:
    settings = ctx.settings.sync
    if args.representations:
        if "storage" not in args.representations:
            raise ConfexError(
                "INVALID_ARGUMENT", "--representations must include storage", ins.EXIT_USAGE
            )
        settings = dataclasses.replace(settings, representations=tuple(args.representations))
    if args.properties:
        settings = dataclasses.replace(settings, page_properties=args.properties)
    conn, client = ctx.conn(), ctx.client()
    syncer = SpaceSync(conn, client, settings)
    reports: list = []
    if args.space or args.all:
        spaces = args.space or list(ctx.settings.sync.spaces)
        if not spaces:
            raise ConfexError(
                "INVALID_ARGUMENT", "no spaces given and [sync].spaces is empty", ins.EXIT_USAGE
            )
        mode = "incremental" if args.changed_only else "full" if args.reconcile else "auto"
        for space_key in spaces:
            try:
                if args.metadata_only:
                    reports.append(syncer.run_metadata(space_key))
                else:
                    reports.append(syncer.run(space_key, mode))
            except AuthenticationError:
                raise
            except (ConfluenceError, SyncLockedError) as exc:
                # One space failing (lock held, space gone) must not stop the others.
                conn.rollback()
                reports.append(
                    {"space_key": space_key, "status": "failed", "errors": [{"error": str(exc)}]}
                )
    else:
        if args.metadata_only or args.changed_only:
            raise ConfexError(
                "ARGUMENT_CONFLICT",
                "--page/--root/--cql take only --reconcile",
                ins.EXIT_USAGE,
            )
        scope = sync_scope(args, full=bool(args.reconcile))
        found, missing = syncer.run_scope(scope)
        reports.extend(found)
        if missing:
            reports.append(
                {
                    "status": "partial",
                    "errors": [
                        {"page_id": p, "error": "not visible (404); not treated as deleted"}
                        for p in missing
                    ],
                }
            )
        if not reports:
            reports.append({"status": "succeeded", "note": "the selection matched no pages"})
    out: list[dict[str, Any]] = []
    for r in reports:
        d = r if isinstance(r, dict) else r.as_dict()
        d["errors"] = d.get("errors", [])[:20]
        out.append(d)
    worst = ins.EXIT_OK if all(d["status"] == "succeeded" for d in out) else ins.EXIT_PARTIAL
    data = out[0] if len(out) == 1 else {"runs": out}
    return Result(data, exit_code=worst)


# -- spaces -----------------------------------------------------------------------------------


def _plain_spaces(data, args):
    if data and "mirrored_pages" not in data[0]:
        return table(
            ["key", "name", "type", "status"],
            [[s["space_key"], s["name"], s["space_type"], s["status"]] for s in data],
        )
    return table(
        ["key", "name", "type", "status", "pages", "tables(storage)", "last sync"],
        [
            [
                s["space_key"],
                s["name"],
                s["space_type"],
                s["status"],
                s["mirrored_pages"],
                s["parsed_storage_tables"],
                s["last_successful_sync_at"] or "never",
            ]
            for s in data
        ],
    )


@command("spaces.list", live=True, collection=True, plain=_plain_spaces)
def spaces_list(args, ctx: Context) -> Result:
    if ctx.live:
        items = live.spaces_list(ctx.client(), args.match, args.space_type)
    else:
        items = ins.spaces_list(ctx.conn(), args.match, args.space_type)
    data, completeness = paginate(
        items, args, {"scope": "spaces", "m": args.match, "t": args.space_type, "l": ctx.live}
    )
    return Result(data, completeness=completeness)


@command("spaces.show", live=True)
def spaces_show(args, ctx: Context) -> Result:
    if ctx.live:
        return Result(live.space_show(ctx.client(), args.key))
    return Result(ins.space_show(ctx.conn(), args.key))


def _plain_space_tree(data, args):
    lines = tree_lines(data["roots"])
    if data["unresolved_parent"]:
        lines.append("(unresolved parent)")
        lines += tree_lines(data["unresolved_parent"], 1)
    return "\n".join(lines) + "\n" + diag_lines(data["diagnostics"])


@command("spaces.tree", plain=_plain_space_tree)
def spaces_tree(args, ctx: Context) -> Result:
    data = ins.space_forest(ctx.conn(), args.key, args.depth, args.match, args.counts)
    return Result(data, diagnostics=data["diagnostics"])


# -- pages ------------------------------------------------------------------------------------


@command("pages.show", live=True)
def pages_show(args, ctx: Context) -> Result:
    if ctx.live:
        return Result(live.page_show(ctx.client(), args.page_id))
    return Result(ins.page_show(ctx.conn(), args.page_id))


def _plain_page_list(data, args):
    return table(
        ["page_id", "title", "position", ""],
        [
            [
                p["page_id"],
                p["title"],
                p.get("position"),
                "<- selected" if p.get("selected") else "",
            ]
            for p in data
        ],
    )


@command("pages.children", live=True, collection=True, plain=_plain_page_list)
def pages_children(args, ctx: Context) -> Result:
    if ctx.live:
        items = live.page_children(ctx.client(), args.page_id)
    else:
        conn = ctx.conn()
        ins._page(conn, args.page_id)
        items = repo.page_children(conn, args.page_id)
    data, completeness = paginate(
        items, args, {"scope": "children", "p": args.page_id, "l": ctx.live}
    )
    return Result(data, completeness=completeness)


@command("pages.siblings", collection=True, plain=_plain_page_list)
def pages_siblings(args, ctx: Context) -> Result:
    conn = ctx.conn()
    ins._page(conn, args.page_id)
    items = [
        {**s, "selected": s["page_id"] == args.page_id}
        for s in repo.page_siblings(conn, args.page_id)
    ]
    data, completeness = paginate(items, args, {"scope": "siblings", "p": args.page_id})
    return Result(data, completeness=completeness)


def _plain_ancestors(data, args):
    out = ""
    if data["unresolved_parent_id"] is not None:
        out += f"(unresolved parent {data['unresolved_parent_id']})\n"
    chain = [*data["ancestors"], {**data["page"], "selected": True}]
    out += "".join(
        f"{'  ' * i}{safe(p['title'])} [{p['page_id']}]"
        f"{'  <- selected' if p.get('selected') else ''}\n"
        for i, p in enumerate(chain)
    )
    return out + diag_lines(data["diagnostics"])


@command("pages.ancestors", live=True, plain=_plain_ancestors)
def pages_ancestors(args, ctx: Context) -> Result:
    if ctx.live:
        data = live.page_ancestors(ctx.client(), args.page_id)
    else:
        data = ins.page_ancestors(ctx.conn(), args.page_id)
    return Result(data, diagnostics=data["diagnostics"])


def _plain_page_tree(data, args):
    out = "\n".join(tree_lines([data["root"]] if data["root"] else [])) + "\n"
    return out + diag_lines(data["diagnostics"])


@command("pages.tree", live=True, plain=_plain_page_tree)
def pages_tree(args, ctx: Context) -> Result:
    if ctx.live:
        if args.depth > 0 and not args.allow_scan:
            raise ConfexError(
                "SCAN_NOT_ALLOWED",
                "a live tree needs several requests; pass --allow-scan (bounded by --max-requests)",
                ins.EXIT_USAGE,
            )
        data = live.page_tree(ctx.client(), args.root, args.depth, args.max_requests)
    else:
        data = ins.page_subtree(ctx.conn(), args.root, args.depth)
    return Result(data, diagnostics=data["diagnostics"])


def _plain_outline(data, args):
    lines = []
    for n in data["outline"]:
        pad = "  " * n["container_depth"]
        if n["kind"] == "heading":
            lines.append(f"{pad}{'#' * n['level']} {safe(n['text'])}")
        elif n["kind"] == "table":
            lines.append(
                f"{pad}[table {n['selector']} {n['rows']}x{n['columns']} {n['geometry_status']}]"
            )
        else:
            lines.append(f"{pad}[macro {safe(n['name'])}]")
    return "\n".join(lines) + "\n"


@command("pages.outline", plain=_plain_outline)
def pages_outline(args, ctx: Context) -> Result:
    resolved = ins.page_artifact(ctx.conn(), args.page_id, args.representation, args.page_version)
    data = {
        "representation": resolved.artifact["representation"],
        "representation_reason": resolved.reason,
        "parse_mode": resolved.parse_mode,
        "outline": ins.outline(resolved, args.tables, args.macros),
    }
    return Result(data, source=_source(resolved), diagnostics=resolved.artifact["diagnostics"])


def plain_table_list(data, args):
    return table(
        ["selector", "table_id", "depth", "rows", "cols", "geometry", "heading path"],
        [
            [
                t.get("selector") or f"{t['page_id']}:t{t['ordinal']:02d}",
                t["table_id"],
                t["depth"],
                t.get("rows", t.get("row_count")),
                t.get("columns", t.get("column_count")),
                t["geometry_status"],
                " > ".join(h if isinstance(h, str) else h["text"] for h in t["heading_path"]),
            ]
            for t in data
        ],
    )


@command("pages.tables", collection=True, plain=plain_table_list)
def pages_tables(args, ctx: Context) -> Result:
    resolved = ins.page_artifact(ctx.conn(), args.page_id, args.representation, args.page_version)
    data, completeness = paginate(
        ins.table_summaries(resolved),
        args,
        {"scope": "page_tables", "a": resolved.artifact["artifact_id"]},
    )
    return Result(
        data,
        source=_source(resolved),
        completeness=completeness,
        diagnostics=resolved.artifact["diagnostics"],
    )


def _plain_read(data, args):
    if "body" in data:
        return safe(data["body"]) + "\n"
    out = []
    for b in data["blocks"]:
        kind = b["kind"]
        if kind == "heading":
            out.append(f"{'#' * b['level']} {safe(b['text'])}")
        elif kind == "table":
            out.append(f"[table {b['selector']} {b['rows']}x{b['columns']}]")
        elif kind in ("macro", "layout_cell"):
            if kind == "macro":
                out.append(f"[macro {safe(b['name'])}]")
        elif kind == "list_item":
            out.append(f"- {safe(b['text'])}")
        elif kind == "code":
            out.append("```\n" + safe(b["text"]) + "\n```")
        else:
            out.append(safe(b["text"]))
    return "\n\n".join(out) + "\n"


@command("pages.read", live=True, plain=_plain_read)
def pages_read(args, ctx: Context) -> Result:
    if ctx.live:
        data = live.page_read(
            ctx.client(), args.page_id, args.representation, args.mode, args.section_id
        )
        return Result(data)
    conn = ctx.conn()
    if args.mode == "raw":
        return Result(ins.read_raw(conn, args.page_id, args.representation, args.page_version))
    resolved = ins.page_artifact(conn, args.page_id, args.representation, args.page_version)
    return Result(ins.read_text(resolved, args.section_id), source=_source(resolved))


@command(
    "pages.labels",
    live=True,
    plain=lambda d, a: table(["prefix", "name"], [[x["prefix"], x["name"]] for x in d]),
)
def pages_labels(args, ctx: Context) -> Result:
    if ctx.live:
        return Result(live.page_labels(ctx.client(), args.page_id))
    return Result(ins.page_labels(ctx.conn(), args.page_id))


@command(
    "pages.properties",
    live=True,
    plain=lambda d, a: table(
        ["key", "version", "value"],
        [[x["property_key"], x["version_number"], x["value"]] for x in d],
    ),
)
def pages_properties(args, ctx: Context) -> Result:
    if ctx.live:
        return Result(live.page_properties(ctx.client(), args.page_id, args.key))
    return Result(ins.page_properties(ctx.conn(), args.page_id, args.key))


@command(
    "pages.attachments",
    live=True,
    plain=items_table(
        "attachments", ["attachment_id", "title", "media_type", "file_size", "version_number"]
    ),
)
def pages_attachments(args, ctx: Context) -> Result:
    if ctx.live:
        return Result(live.page_attachments(ctx.client(), args.page_id))
    return Result(ins.page_attachments(ctx.conn(), args.page_id, args.include_deleted))


@command(
    "pages.versions",
    plain=items_table(
        "versions",
        ["version_number", "title", "created_at", "body_stored", "superseded_captures"],
    ),
)
def pages_versions(args, ctx: Context) -> Result:
    return Result(ins.page_versions(ctx.conn(), args.page_id))


@command(
    "pages.macros",
    plain=items_table("macros", ["macro_index", "macro_name", "macro_id", "depth", "parameters"]),
)
def pages_macros(args, ctx: Context) -> Result:
    return Result(ins.page_macros(ctx.conn(), args.page_id, args.name, args.page_version))


def _plain_links(data, args):
    out = ""
    if "outbound" in data:
        out += "outbound\n" + table(
            ["type", "target", "resolution", "text"],
            [
                [
                    x["link_type"],
                    x["target_page_id"]
                    or x["target_title"]
                    or x["target_url"]
                    or x["target_filename"]
                    or x["anchor"],
                    x["resolution"],
                    x["link_text"],
                ]
                for x in data["outbound"]
            ],
        )
    if "inbound" in data:
        out += "inbound\n" + table(
            ["from", "title", "type"],
            [[x["source_page_id"], x["source_title"], x["link_type"]] for x in data["inbound"]],
        )
        out += f"-- {data['inbound_coverage']}\n"
    return out


@command("pages.links", plain=_plain_links)
def pages_links(args, ctx: Context) -> Result:
    return Result(ins.page_links(ctx.conn(), args.page_id, args.direction))


def _plain_diff(data, args):
    lines = [
        f"{data['from']['snapshot_id']} (v{data['from']['version_number']}) -> "
        f"{data['to']['snapshot_id']} (v{data['to']['version_number']})"
    ]
    if data["identical_body"]:
        lines.append("bodies are identical")
    for key, change in data["metadata"].items():
        lines.append(f"metadata {key}: {safe(change['from'])} -> {safe(change['to'])}")
    for section in ("headings", "blocks"):
        for text in data[section]["removed"]:
            lines.append(f"- {section[:-1]}: {safe(text)}")
        for text in data[section]["added"]:
            lines.append(f"+ {section[:-1]}: {safe(text)}")
    for t in data["tables"]:
        if t["match"] == "candidate":
            lines.append(
                f"~ table {t['from']} -> {t['to']} (candidate): {len(t['cell_changes'])} cell "
                "change(s)"
            )
            for c in t["cell_changes"]:
                lines.append(f"    {c['coord']}: {safe(c['from'])} -> {safe(c['to'])}")
        else:
            lines.append(f"? table {t.get('from') or t.get('to')}: {t['match']}")
    return "\n".join(lines) + "\n"


@command("pages.diff", plain=_plain_diff)
def pages_diff(args, ctx: Context) -> Result:
    return Result(
        ins.diff_pages(
            ctx.conn(),
            args.page_id,
            from_snapshot=args.from_snapshot,
            to_snapshot=args.to_snapshot,
            from_version=args.from_version,
            to_version=args.to_version,
            representation=args.representation,
        )
    )


# -- search -------------------------------------------------------------------------------------


def _plain_search(data, args):
    rows = []
    for r in data["results"]:
        for m in r["matches"] or [{"field": "-"}]:
            rows.append(
                [
                    r["page_id"],
                    r["title"],
                    r.get("space_key"),
                    m["field"],
                    m.get("table_id") or m.get("section_id") or "",
                    m.get("snippet", ""),
                ]
            )
    out = (
        table(["page_id", "title", "space", "field", "where", "snippet"], rows)
        if rows
        else ("(no matches)\n")
    )
    return out + f"-- {data['method']}\n"


@command("search", live=True, plain=_plain_search)
def search_cmd(args, ctx: Context) -> Result:
    local = any((args.title, args.text, args.label, args.has_tables))
    if args.cql:
        if local:
            raise ConfexError(
                "ARGUMENT_CONFLICT",
                "--cql cannot be combined with local filters (--title/--text/--label/"
                "--has-tables); results would not mean the same thing",
                ins.EXIT_USAGE,
            )
        if not ctx.live:
            raise ConfexError(
                "UNSUPPORTED_MODE",
                "native CQL needs --data-source live; the mirror does not emulate CQL",
                ins.EXIT_UNAVAILABLE,
            )
        cql = (
            args.cql if not args.space else f'({args.cql}) AND space = "{_cql_escape(args.space)}"'
        )
        data = live.search_cql(ctx.client(), cql, args.limit)
        return Result(
            {**data, "results": [{**r, "matches": [{"field": "cql"}]} for r in data["results"]]}
        )
    if ctx.live:
        raise ConfexError(
            "UNSUPPORTED_MODE",
            "local search filters are mirror-only; use --cql with --data-source live",
            ins.EXIT_UNAVAILABLE,
        )
    data = ins.search(
        ctx.conn(),
        space_key=args.space,
        title=args.title,
        text=args.text,
        label=args.label,
        has_tables=args.has_tables,
        limit=args.limit,
    )
    return Result(data)


def _cql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
