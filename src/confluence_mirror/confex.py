"""``confex``: explore the mirror and inspect document and table structure, offline.

Global options go before the subcommand::

    confex --format json tables show 12345:t02 --representation storage --view grid

Navigation and inspection open the database read-only and make no network calls. Only
``db migrate`` changes the schema and only ``parse`` writes (derived artifacts).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

from confluence_mirror import __version__, db
from confluence_mirror import inspection as ins
from confluence_mirror.config import ConfigError, load_dotenv, load_settings
from confluence_mirror.inspection import ConfexError
from confluence_mirror.logging_utils import redact, setup_logging
from confluence_mirror.structure import REPRESENTATIONS
from confluence_mirror.structure.ids import DEFAULT_INSTANCE_ID, digest

log = logging.getLogger("confluence_mirror.confex")

SCHEMA_VERSION = "confex.cli.v1"
COLLECTION_COMMANDS = {
    "spaces.list",
    "pages.children",
    "pages.siblings",
    "pages.tables",
    "tables.list",
}
DEFAULT_LIMIT = 50


def _default_config() -> Path | None:
    for candidate in (Path("config.toml"), Path("config/config.toml")):
        if candidate.is_file():
            return candidate
    return None


def _positive(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="confex", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", type=Path, help="config TOML (default: ./config.toml)")
    p.add_argument("--data-source", choices=("mirror", "live"), default="mirror")
    p.add_argument(
        "--format",
        choices=("plain", "json", "jsonl"),
        default="plain",
        help="output: plain text, one JSON envelope, or JSONL (collections only)",
    )
    p.add_argument("--output", type=Path, help="write output to this file instead of stdout")
    p.add_argument("--overwrite", action="store_true", help="allow --output to replace a file")
    p.add_argument("--no-color", action="store_true", help="accepted; output is never colored")
    p.add_argument("--verbose", "-v", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)

    dbp = sub.add_parser("db", help="schema status and migrations")
    dbsub = dbp.add_subparsers(dest="sub", required=True)
    dbsub.add_parser("status", help="applied and pending migrations")
    dbsub.add_parser("migrate", help="apply pending migrations (the only schema change)")

    parse = sub.add_parser("parse", help="persist structural artifacts for stored bodies")
    scope = parse.add_mutually_exclusive_group(required=True)
    scope.add_argument("--space", metavar="KEY")
    scope.add_argument("--page", type=int, action="append", metavar="ID")
    parse.add_argument(
        "--representations",
        default="storage,export_view",
        help="comma-separated subset of storage,export_view,view, or 'all'",
    )
    parse.add_argument("--all-versions", action="store_true")

    def collection(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
        parser.add_argument("--cursor")
        parser.add_argument("--all", action="store_true", help="no page limit")

    spaces = sub.add_parser("spaces", help="mirrored spaces").add_subparsers(
        dest="sub", required=True
    )
    sl = spaces.add_parser("list")
    sl.add_argument("--match")
    sl.add_argument("--type", dest="space_type")
    collection(sl)
    spaces.add_parser("show").add_argument("key")
    st = spaces.add_parser("tree")
    st.add_argument("key")
    st.add_argument("--depth", type=_positive)
    st.add_argument("--match")
    st.add_argument("--counts", action="store_true")

    pages = sub.add_parser("pages", help="page navigation and structure").add_subparsers(
        dest="sub", required=True
    )
    pages.add_parser("show").add_argument("page_id", type=int)
    for name in ("children", "siblings"):
        pc = pages.add_parser(name)
        pc.add_argument("page_id", type=int)
        collection(pc)
    pages.add_parser("ancestors").add_argument("page_id", type=int)
    pt = pages.add_parser("tree")
    pt.add_argument("--root", type=int, required=True)
    pt.add_argument("--depth", type=_positive, default=3)
    for name in ("outline", "tables"):
        po = pages.add_parser(name)
        po.add_argument("page_id", type=int)
        po.add_argument("--representation", choices=("auto", *REPRESENTATIONS), default="auto")
        po.add_argument("--version", dest="page_version", type=int)
        if name == "outline":
            po.add_argument("--tables", action="store_true")
            po.add_argument("--macros", action="store_true")
        else:
            collection(po)

    tables = sub.add_parser("tables", help="table occurrences").add_subparsers(
        dest="sub", required=True
    )
    tl = tables.add_parser("list")
    tl.add_argument("--space", metavar="KEY")
    tl.add_argument("--page", type=int)
    tl.add_argument("--nested", choices=("any", "only", "none"), default="any")
    tl.add_argument("--representation", choices=REPRESENTATIONS)
    collection(tl)
    ts = tables.add_parser("show")
    ts.add_argument("selector", help="tbl_<id> or PAGE:tNN")
    ts.add_argument(
        "--representation", "--source", choices=("auto", *REPRESENTATIONS), default="auto"
    )
    ts.add_argument("--version", dest="page_version", type=int)
    ts.add_argument("--snapshot")
    ts.add_argument("--view", choices=ins.VIEWS, default="overview")
    ts.add_argument("--cell", help="coordinate for --view cell, e.g. R3C1")
    ts.add_argument("--rows", help="inclusive range A:B, or all")
    ts.add_argument("--cols", help="inclusive range A:B, or all")
    ts.add_argument("--full", action="store_true", help="do not truncate text previews")
    ts.add_argument("--max-col-width", type=_positive, default=32)
    ts.add_argument("--stage")
    ts.add_argument("--chunk", type=int)
    return p


# -- envelope and output ------------------------------------------------------------------


def _default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def envelope(
    command: str,
    data: Any,
    *,
    source: dict | None = None,
    completeness: dict | None = None,
    diagnostics: list | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error" if error else "ok",
        "source": {"mode": "mirror", "instance_id": DEFAULT_INSTANCE_ID, **(source or {})},
        "data": data,
        "completeness": completeness
        or {
            "scope": command,
            "state": "complete",
            "has_more": False,
            "truncated": False,
            "reasons": [],
        },
        "diagnostics": diagnostics or [],
        "error": error,
    }


def _write(text: str, args: argparse.Namespace) -> None:
    if args.output is None:
        sys.stdout.write(text)
        return
    path: Path = args.output
    if path.exists() and not args.overwrite:
        raise ConfexError(
            "OUTPUT_EXISTS", f"{path} exists; pass --overwrite to replace it", ins.EXIT_USAGE
        )
    fd, tmp = tempfile.mkstemp(dir=path.parent or Path("."), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def safe(text: Any) -> str:
    """Terminal-safe text: control characters (incl. ESC) are shown escaped."""
    s = "" if text is None else str(text)
    return "".join(
        ch if ch == "\n" or unicodedata.category(ch)[0] != "C" else f"\\x{ord(ch):02x}" for ch in s
    )


def _preview(text: str, width: int, full: bool) -> str:
    one = safe(text).replace("\n", " ⏎ ")
    if full or len(one) <= width:
        return one
    return one[: max(width - 1, 1)] + "…"


# -- pagination ---------------------------------------------------------------------------


def paginate(items: list, args: argparse.Namespace, scope: dict) -> tuple[list, dict]:
    key = digest(scope)[:16]
    offset = 0
    if getattr(args, "cursor", None):
        try:
            raw = json.loads(base64.urlsafe_b64decode(args.cursor.encode()).decode())
            offset, cursor_key = int(raw["o"]), raw["k"]
        except (ValueError, KeyError, TypeError):
            raise ConfexError("INVALID_CURSOR", "cursor is malformed", ins.EXIT_USAGE) from None
        if cursor_key != key:
            raise ConfexError(
                "INVALID_CURSOR", "cursor belongs to a different query", ins.EXIT_USAGE
            )
    limit = None if getattr(args, "all", False) else getattr(args, "limit", DEFAULT_LIMIT)
    page = items[offset:] if limit is None else items[offset : offset + limit]
    end = offset + len(page)
    has_more = end < len(items)
    cursor = (
        base64.urlsafe_b64encode(json.dumps({"o": end, "k": key}).encode()).decode()
        if has_more
        else None
    )
    return page, {
        "scope": scope.get("scope", "collection"),
        "state": "complete" if not has_more else "partial",
        "has_more": has_more,
        "truncated": False,
        "returned": len(page),
        "known_total": len(items),
        "next_cursor": cursor,
        "reasons": [] if not has_more else ["page limit reached; pass --cursor to continue"],
    }


# -- plain renderers ----------------------------------------------------------------------


def _kv(rows: list[tuple[str, Any]]) -> str:
    width = max((len(k) for k, _ in rows), default=0)
    return "".join(f"{k.ljust(width)}  {safe(v)}\n" for k, v in rows)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[safe(c).replace("\n", " ") for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()
    out = [line]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(out) + "\n"


def _tree_lines(nodes: list[dict], indent: int = 0) -> list[str]:
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
        out += _tree_lines(n.get("children", []), indent + 1)
    return out


def _diag_lines(diagnostics: list[dict]) -> str:
    return "".join(f"! {d['severity']} {d['code']}: {safe(d['message'])}\n" for d in diagnostics)


def render_plain(command: str, data: Any, args: argparse.Namespace, completeness: dict) -> str:
    out = ""
    if command == "db.status":
        out = _table(
            ["version", "name", "applied", "checksum_ok"],
            [[m["version"], m["name"], m["applied"], m["checksum_ok"]] for m in data],
        )
    elif command == "db.migrate":
        out = "".join(f"applied {m}\n" for m in data["applied"]) or "database is up to date\n"
    elif command == "parse":
        out = _kv([(k, v) for k, v in data.items() if k != "errors"])
        out += "".join(
            f"error page {e['page_id']} v{e['version']} {e['representation']}: {safe(e['error'])}\n"
            for e in data["errors"]
        )
    elif command == "spaces.list":
        out = _table(
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
    elif command == "spaces.tree":
        lines = _tree_lines(data["roots"])
        if data["unresolved_parent"]:
            lines.append("(unresolved parent)")
            lines += _tree_lines(data["unresolved_parent"], 1)
        out = "\n".join(lines) + "\n" + _diag_lines(data["diagnostics"])
    elif command == "pages.tree":
        out = "\n".join(_tree_lines([data["root"]] if data["root"] else [])) + "\n"
        out += _diag_lines(data["diagnostics"])
    elif command == "pages.ancestors":
        chain = [*data["ancestors"], {**data["page"], "selected": True}]
        if data["unresolved_parent_id"] is not None:
            out += f"(unresolved parent {data['unresolved_parent_id']})\n"
        out += "".join(
            f"{'  ' * i}{safe(p['title'])} [{p['page_id']}]"
            f"{'  <- selected' if p.get('selected') else ''}\n"
            for i, p in enumerate(chain)
        )
        out += _diag_lines(data["diagnostics"])
    elif command in ("pages.children", "pages.siblings"):
        out = _table(
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
    elif command in ("pages.tables", "tables.list"):
        out = _table(
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
    elif command == "pages.outline":
        lines = []
        for n in data["outline"]:
            pad = "  " * n["container_depth"]
            if n["kind"] == "heading":
                lines.append(f"{pad}{'#' * n['level']} {safe(n['text'])}")
            elif n["kind"] == "table":
                lines.append(
                    f"{pad}[table {n['selector']} {n['rows']}x{n['columns']} "
                    f"{n['geometry_status']}]"
                )
            else:
                lines.append(f"{pad}[macro {safe(n['name'])}]")
        out = "\n".join(lines) + "\n"
    elif command == "tables.show":
        out = _render_table_view(data, args)
    elif isinstance(data, dict):
        out = _kv(
            [
                (
                    k,
                    json.dumps(v, default=_default, ensure_ascii=False)
                    if isinstance(v, dict | list)
                    else v,
                )
                for k, v in data.items()
            ]
        )
    if completeness.get("has_more"):
        out += f"-- more: --cursor {completeness['next_cursor']}\n"
    return out


def _render_table_view(data: dict, args: argparse.Namespace) -> str:
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
                    cells.append(_preview(text, width, args.full))
                elif slot["kind"] == "covered":
                    cells.append(f"covered:{slot['labels'][0]}")
                elif slot["kind"] == "conflict":
                    cells.append(f"conflict:{','.join(slot['labels'])}")
                else:
                    cells.append("")
            rows.append([coord, *cells])
        out = head + _table(headers, rows)
        if sel["hidden_rows"] or sel["hidden_cols"]:
            out += (
                f"-- showing rows {sel['rows'][0]}-{sel['rows'][1]}, cols {c_lo}-{c_hi}; "
                f"hidden: {sel['hidden_rows']} rows, {sel['hidden_cols']} cols "
                "(use --rows/--cols)\n"
            )
        return out + _diag_lines(data["diagnostics"])
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
                    f"{_preview(c['direct_text'], width, args.full)}"
                )
                for b in c["blocks"]:
                    if b["kind"] == "table":
                        lines.append(f"      nested {b['table_id']}")
        return "\n".join(lines) + "\n" + _diag_lines(data["diagnostics"])
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
        return "\n".join(lines) + "\n" + _diag_lines(data["diagnostics"])
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
        + _kv(
            [
                (
                    k,
                    json.dumps(v, default=_default, ensure_ascii=False)
                    if isinstance(v, dict | list)
                    else v,
                )
                for k, v in rows
            ]
        )
        + _diag_lines(data["diagnostics"])
    )


# -- dispatch -----------------------------------------------------------------------------


def _schema_ready(conn: psycopg.Connection) -> None:
    status = db.migration_status(conn)
    if any(not m["applied"] or not m["checksum_ok"] for m in status):
        raise ConfexError(
            "SCHEMA_INCOMPATIBLE",
            "the mirror schema is not at this version; run `confex db migrate` explicitly",
            ins.EXIT_UNAVAILABLE,
            {"pending": [m["version"] for m in status if not m["applied"]]},
        )


def _run(args: argparse.Namespace, conn: psycopg.Connection) -> tuple[str, Any, dict, dict, list]:
    """Returns (command, data, source, completeness, diagnostics)."""
    cmd = f"{args.command}.{args.sub}" if getattr(args, "sub", None) else args.command
    source: dict = {}
    completeness: dict = {}
    diagnostics: list = []
    if cmd == "db.status":
        return cmd, db.migration_status(conn), source, completeness, diagnostics
    if cmd == "db.migrate":
        applied = db.migrate(conn)
        return cmd, {"applied": [f"{m.version:04d}_{m.name}" for m in applied]}, source, {}, []

    _schema_ready(conn)
    if cmd == "parse":
        reps = (
            list(REPRESENTATIONS)
            if args.representations == "all"
            else [r.strip() for r in args.representations.split(",") if r.strip()]
        )
        bad = set(reps) - set(REPRESENTATIONS)
        if bad or not reps:
            raise ConfexError(
                "INVALID_ARGUMENT", f"unknown representation(s): {sorted(bad)}", ins.EXIT_USAGE
            )
        data = ins.parse_pages(
            conn,
            space_key=args.space,
            page_ids=args.page,
            representations=reps,
            all_versions=args.all_versions,
        )
        return cmd, data, source, completeness, diagnostics

    conn.read_only = True  # navigation and inspection never write
    if cmd == "spaces.list":
        items = ins.spaces_list(conn, args.match, args.space_type)
        data, completeness = paginate(
            items, args, {"scope": "spaces", "m": args.match, "t": args.space_type}
        )
    elif cmd == "spaces.show":
        data = ins.space_show(conn, args.key)
    elif cmd == "spaces.tree":
        data = ins.space_forest(conn, args.key, args.depth, args.match, args.counts)
        diagnostics = data["diagnostics"]
    elif cmd == "pages.show":
        data = ins.page_show(conn, args.page_id)
    elif cmd == "pages.children":
        ins._page(conn, args.page_id)
        items = ins.repo.page_children(conn, args.page_id)
        data, completeness = paginate(items, args, {"scope": "children", "p": args.page_id})
    elif cmd == "pages.siblings":
        ins._page(conn, args.page_id)
        items = [
            {**s, "selected": s["page_id"] == args.page_id}
            for s in ins.repo.page_siblings(conn, args.page_id)
        ]
        data, completeness = paginate(items, args, {"scope": "siblings", "p": args.page_id})
    elif cmd == "pages.ancestors":
        data = ins.page_ancestors(conn, args.page_id)
        diagnostics = data["diagnostics"]
    elif cmd == "pages.tree":
        data = ins.page_subtree(conn, args.root, args.depth)
        diagnostics = data["diagnostics"]
    elif cmd in ("pages.outline", "pages.tables"):
        resolved = ins.page_artifact(conn, args.page_id, args.representation, args.page_version)
        source = _source(resolved)
        diagnostics = resolved.artifact["diagnostics"]
        if cmd == "pages.outline":
            data = {
                "representation": resolved.artifact["representation"],
                "representation_reason": resolved.reason,
                "parse_mode": resolved.parse_mode,
                "outline": ins.outline(resolved, args.tables, args.macros),
            }
        else:
            data, completeness = paginate(
                ins.table_summaries(resolved),
                args,
                {"scope": "page_tables", "a": resolved.artifact["artifact_id"]},
            )
    elif cmd == "tables.list":
        space_id = ins._space(conn, args.space)["space_id"] if args.space else None
        items = ins.repo.list_table_occurrences(
            conn, space_id, args.page, args.nested, args.representation
        )
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
    elif cmd == "tables.show":
        resolved, table = ins.resolve_table(
            conn,
            args.selector,
            representation=args.representation,
            version=args.page_version,
            snapshot=args.snapshot,
        )
        bounded = args.format == "plain"
        data = ins.table_view(
            conn,
            resolved,
            table,
            args.view,
            cell=args.cell,
            rows=args.rows,
            cols=args.cols,
            bounded=bounded,
        )
        source = _source(resolved)
        if args.view == "grid":
            sel = data["selection"]
            completeness = {
                "scope": "selected_table",
                "state": "complete" if sel["complete"] else "partial",
                "has_more": False,
                "truncated": not sel["complete"],
                "reasons": [] if sel["complete"] else ["row/column selection or display bound"],
            }
    else:  # pragma: no cover - argparse restricts commands
        raise ConfexError("UNSUPPORTED_MODE", f"unknown command {cmd}", ins.EXIT_USAGE)
    return cmd, data, source, completeness, diagnostics


def _source(resolved: ins.Resolved) -> dict:
    return {
        "snapshot_id": resolved.artifact["snapshot_id"],
        "artifact_id": resolved.artifact["artifact_id"],
        "observed_at": resolved.fetched_at,
    }


def _emit(
    args: argparse.Namespace,
    cmd: str,
    data: Any,
    source: dict,
    completeness: dict,
    diagnostics: list,
) -> None:
    if args.format == "json":
        env = envelope(
            cmd, data, source=source, completeness=completeness or None, diagnostics=diagnostics
        )
        _write(json.dumps(env, indent=2, default=_default, ensure_ascii=False) + "\n", args)
    elif args.format == "jsonl":
        if cmd not in COLLECTION_COMMANDS:
            raise ConfexError(
                "UNSUPPORTED_MODE", f"--format jsonl is not accepted by {cmd}", ins.EXIT_USAGE
            )
        lines = [
            {
                "type": "meta",
                "schema_version": SCHEMA_VERSION,
                "command": cmd,
                "source": {"mode": "mirror", "instance_id": DEFAULT_INSTANCE_ID, **source},
            }
        ]
        lines += [{"type": "item", "data": item} for item in data]
        lines.append(
            {
                "type": "summary",
                "status": "ok",
                "completeness": completeness,
                "diagnostics": diagnostics,
            }
        )
        _write(
            "".join(json.dumps(x, default=_default, ensure_ascii=False) + "\n" for x in lines), args
        )
    else:
        _write(render_plain(cmd, data, args, completeness or {}), args)


def _emit_error(args: argparse.Namespace | None, exc: ConfexError) -> None:
    log.error("%s", exc)
    if args is None or args.format == "plain":
        for choice in exc.details.get("choices", []):
            sys.stderr.write(f"  candidate: {choice['selector']}  ({choice['table_id']})\n")
        return
    cmd = f"{args.command}.{args.sub}" if getattr(args, "sub", None) else args.command
    error = {"code": exc.code, "message": exc.message, "details": exc.details}
    env = envelope(
        cmd,
        None,
        error=error,
        completeness={
            "scope": cmd,
            "state": "failed",
            "has_more": False,
            "truncated": False,
            "reasons": [exc.code],
        },
    )
    text = json.dumps(env, indent=2, default=_default, ensure_ascii=False) + "\n"
    if args.format == "jsonl":
        text = (
            json.dumps({"type": "summary", "status": "error", "error": error}, default=_default)
            + "\n"
        )
    sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return ins.EXIT_USAGE if exc.code not in (0, None) else 0
    setup_logging(args.verbose)
    load_dotenv()
    try:
        if args.data_source == "live":
            raise ConfexError(
                "CAPABILITY_UNAVAILABLE",
                "live mode is not implemented in this release; sync, then use the mirror",
                ins.EXIT_UNAVAILABLE,
            )
        settings = load_settings(args.config or _default_config(), require_confluence=False)
        with db.connect(settings.dsn) as conn:
            cmd, data, source, completeness, diagnostics = _run(args, conn)
        _emit(args, cmd, data, source, completeness, diagnostics)
        if cmd == "parse" and data["failed"]:
            return ins.EXIT_PARTIAL
        return ins.EXIT_OK
    except ConfexError as exc:
        _emit_error(args, exc)
        return exc.exit_code
    except ConfigError as exc:
        _emit_error(args, ConfexError("CONFIG_INVALID", str(exc), ins.EXIT_USAGE))
        return ins.EXIT_USAGE
    except db.MigrationError as exc:
        _emit_error(args, ConfexError("SCHEMA_INCOMPATIBLE", str(exc), ins.EXIT_UNAVAILABLE))
        return ins.EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        log.error("interrupted")
        return ins.EXIT_INTERRUPTED
    except psycopg.Error as exc:
        _emit_error(args, ConfexError("DATABASE_ERROR", redact(str(exc)), ins.EXIT_INTERNAL))
        return ins.EXIT_INTERNAL
    except Exception as exc:
        log.error("unexpected error: %s", redact(f"{type(exc).__name__}: {exc}"))
        if args.verbose:
            log.exception("traceback")
        return ins.EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
