"""templates detect | list | show: model-based page-template detection over page trees."""

from __future__ import annotations

import argparse
import json

from confluence_mirror import inspection as ins
from confluence_mirror import pipeline as pipe
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.confex.core import Context, Result, command, group, leaf, positive
from confluence_mirror.confex.extract import pipeline_for
from confluence_mirror.confex.render import kv, table


def register(sub: argparse._SubParsersAction) -> None:
    tg = group(sub, "templates", "find page templates in page trees (model-assisted)")
    d = leaf(
        tg,
        "templates.detect",
        "for each parent whose children are all leaves, ask the model whether the children "
        "follow one template, then have it check every child against that template",
    )
    scope = d.add_mutually_exclusive_group(required=True)
    scope.add_argument("--space", metavar="KEY")
    scope.add_argument("--root", type=int, metavar="ID", help="a page and its descendants")
    d.add_argument(
        "--min-children", type=positive, default=2, help="smallest family to consider (2)"
    )
    d.add_argument("--sample", type=positive, default=8, help="children shown to the proposer (8)")
    d.add_argument("--dry-run", action="store_true", help="list candidates; call no model")
    d.add_argument("--force", action="store_true", help="ask again even if inputs are unchanged")
    tl = leaf(tg, "templates.list", "latest detection per parent page")
    tl_scope = tl.add_mutually_exclusive_group()
    tl_scope.add_argument("--space", metavar="KEY")
    tl_scope.add_argument("--page", type=int, metavar="ID", help="a parent page")
    tl.add_argument("--limit", type=positive, default=50)
    ts = leaf(tg, "templates.show", "one detection: the proposed template and per-page checks")
    ts.add_argument("detection", help="tmpl_<id>, or a parent page ID for its latest detection")


def _plain_detect(data, args):
    rows = [
        [
            i["parent_page_id"],
            i["title"],
            i["children"],
            i["status"],
            i.get("template_name") or "",
            "" if i.get("conformance") is None else f"{i['conformance']:.0%}",
            i.get("detection_id") or i.get("reason") or "",
        ]
        for i in data["items"]
    ]
    out = table(["parent", "title", "children", "status", "template", "conform", "detail"], rows)
    out += kv([("candidates", data["candidates"]), ("counts", data["counts"])])
    if not data["dry_run"]:
        out += kv([("model requests", data["budget"]["requests"])])
    return out


@command("templates.detect", writes=True, plain=_plain_detect)
def templates_detect(args, ctx: Context) -> Result:
    p = pipeline_for(ctx)
    try:
        data = p.detect_templates(
            space_key=args.space,
            root_id=args.root,
            opts=pipe.TemplateOptions(
                min_children=max(args.min_children, 2),
                sample=max(args.sample, 2),
                dry_run=args.dry_run,
                force=args.force,
            ),
        )
    finally:
        p.close()
    statuses = data["counts"]
    if statuses and set(statuses) == {"blocked"}:
        code = ins.EXIT_POLICY  # nothing could be sent: egress is not approved
    elif any(s in statuses for s in ("failed", "blocked", "budget_exhausted")):
        code = ins.EXIT_PARTIAL
    else:
        code = ins.EXIT_OK
    return Result(data, exit_code=code)


@command(
    "templates.list",
    collection=True,
    plain=lambda d, a: table(
        ["detection", "parent", "space", "status", "template", "children", "conform", "at"],
        [
            [
                r["detection_id"],
                r["parent_page_id"],
                r["space_key"],
                r["status"],
                r["template_name"] or "",
                r["child_count"],
                "" if r["conformance"] is None else f"{float(r['conformance']):.0%}",
                r["created_at"],
            ]
            for r in d
        ],
    ),
)
def templates_list(args, ctx: Context) -> Result:
    return Result(
        prepo.template_detections(
            ctx.conn(), space_key=args.space, parent_page_id=args.page, limit=args.limit
        )
    )


def _plain_show(data, args):
    out = kv(
        [
            (k, data[k])
            for k in ("detection_id", "parent_page_id", "space_key", "status", "conformance")
        ]
    )
    proposal = data.get("proposal") or {}
    if proposal:
        out += kv(
            [
                ("template", proposal.get("name")),
                ("confidence", proposal.get("confidence")),
                ("description", proposal.get("description")),
                ("sections", ", ".join(proposal.get("sections", []))),
            ]
        )
        for t in proposal.get("tables", []):
            cols = ", ".join(f"{c['header']}→{c['field']}:{c['type']}" for c in t["columns"])
            out += f"table {t['key']} ({t['layout']}, under {t['located_under']!r}): {cols}\n"
    for p in data.get("pages", []):
        extra = "; ".join(
            x
            for x in (
                "missing " + ", ".join(p["missing"]) if p.get("missing") else "",
                "unexpected " + ", ".join(p["unexpected"]) if p.get("unexpected") else "",
            )
            if x
        )
        out += f"page {p['page_id']}: {p['state']}{'  ' + extra if extra else ''}\n"
    for issue in data.get("issues", []):
        out += f"! {issue}\n"
    return out


@command("templates.show", plain=_plain_show)
def templates_show(args, ctx: Context) -> Result:
    conn = ctx.conn()
    ref = args.detection
    if ref.isdigit():
        latest = prepo.template_detections(conn, space_key=None, parent_page_id=int(ref), limit=1)
        if not latest:
            raise ins.not_found(f"no template detection for page {ref}")
        ref = latest[0]["detection_id"]
    row = prepo.get_template_detection(conn, ref)
    if row is None:
        raise ins.not_found(f"template detection {ref} does not exist")
    if row.get("conformance") is not None:
        row["conformance"] = float(row["conformance"])
    return Result(json.loads(json.dumps(row, default=str)))
