"""records, review and export commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from confluence_mirror import inspection as ins
from confluence_mirror import pipeline as pipe
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror import review as rv
from confluence_mirror.confex.core import (
    Context,
    Result,
    actor,
    collection_args,
    command,
    group,
    leaf,
    paginate,
    safe,
)
from confluence_mirror.confex.extract import pipeline_for
from confluence_mirror.confex.render import kv, table
from confluence_mirror.inspection import ConfexError

REVIEW_STATES = ("pending", "approved", "rejected", "corrected", "superseded")


def _filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--space", metavar="KEY")
    p.add_argument("--run", metavar="RUN_ID")
    p.add_argument("--schema", metavar="SCHEMA_REF")
    p.add_argument("--page", type=int)


def register(sub: argparse._SubParsersAction) -> None:
    rg = group(sub, "records", "candidate records and their evidence")
    rl = leaf(rg, "records.list")
    _filters(rl)
    rl.add_argument("--state", choices=REVIEW_STATES)
    collection_args(rl)
    leaf(rg, "records.show").add_argument("revision_id")
    leaf(rg, "records.evidence").add_argument("revision_id")
    rd = leaf(rg, "records.diff")
    rd.add_argument("revision_id")
    rd.add_argument("--other", help="compare with this revision (default: the parent)")

    vg = group(sub, "review", "human review of candidate revisions")
    q = leaf(vg, "review.queue")
    _filters(q)
    collection_args(q)
    leaf(vg, "review.show").add_argument("revision_id")
    for name in ("approve", "reject"):
        d = leaf(vg, f"review.{name}")
        d.add_argument("revision_id")
        d.add_argument("--reason", required=True)
        d.add_argument(
            "--expect-sequence",
            type=int,
            help="fail if another review event was recorded since this sequence",
        )
    c = leaf(vg, "review.correct")
    c.add_argument("revision_id")
    c.add_argument("--patch", type=Path, required=True)
    c.add_argument("--reason", required=True)

    e = leaf(sub, "export", "write approved records as an evidence-bearing package")
    _filters(e)
    e.add_argument("--revision", action="append", metavar="REVISION_ID")
    e.add_argument("--approved-only", action="store_true")
    e.add_argument("--export-format", choices=("json", "jsonl", "csv"), default="jsonl")
    e.add_argument(
        "--output",
        dest="export_output",
        type=Path,
        required=True,
        help="directory (json/jsonl) or file (csv)",
    )
    e.add_argument("--overwrite", dest="export_overwrite", action="store_true")


def _plain_records(data, args):
    return (
        table(
            ["revision_id", "kind", "values", "validation", "review", "freshness", "issues"],
            [
                [
                    r["revision_id"],
                    r["record_kind"],
                    r["values"],
                    r["validation"],
                    r["review"],
                    r["freshness"],
                    ",".join(r["issue_codes"]),
                ]
                for r in data
            ],
        )
        if data
        else "(no records)\n"
    )


@command("records.list", collection=True, plain=_plain_records)
def records_list(args, ctx: Context) -> Result:
    items = rv.list_records(
        ctx.conn(),
        space=args.space,
        run=args.run,
        schema=args.schema,
        page=args.page,
        review_state=args.state,
    )
    data, completeness = paginate(
        items,
        args,
        {
            "scope": "records",
            "s": args.space,
            "r": args.run,
            "c": args.schema,
            "p": args.page,
            "st": args.state,
        },
    )
    return Result(data, completeness=completeness)


def _plain_show(data, args):
    out = kv(
        [
            (k, data[k])
            for k in (
                "revision_id",
                "record_key",
                "revision_number",
                "origin",
                "record_kind",
                "schema",
                "profile",
                "states",
            )
        ]
    )
    for f in data["fields"]:
        out += (
            f"  {f['field']}: {safe(f['value'])}  [{f['value_state']}, {f['method']}"
            + (f", {','.join(f['normalization'])}" if f.get("normalization") else "")
            + "]\n"
        )
    v = data["validation"]
    if v:
        out += f"validation: {v['status']}\n"
        for i in v["issues"]:
            out += f"  ! {i['severity']} {i['code']}: {safe(i['message'])}\n"
    for a in data["ambiguities"]:
        out += f"  ? {a['code']}: {safe(a['message'])}\n"
    out += "next:\n" + "".join(f"  {n}\n" for n in data["next"])
    return out


@command("records.show", plain=_plain_show)
def records_show(args, ctx: Context) -> Result:
    return Result(rv.show(ctx.conn(), args.revision_id))


def _plain_evidence(data, args):
    out = kv(list(data["source"].items()))
    for e in data["evidence"]:
        label = e.get("field") or e.get("relationship")
        if e.get("kind") == "human_assertion":
            out += f"  {label}: human assertion by {e['actor']}: {safe(e['reason'])}\n"
            continue
        out += (
            f"  {label}: {safe(e.get('quote'))!r} {e.get('kind')} {e.get('coord') or ''} "
            f"[{e.get('start')}, {e.get('end')}) in {e.get('block_id')}\n"
        )
        if e.get("open"):
            out += f"      {e['open']}\n"
    return out


@command("records.evidence", plain=_plain_evidence)
def records_evidence(args, ctx: Context) -> Result:
    return Result(rv.evidence_view(ctx.conn(), args.revision_id))


@command("records.diff")
def records_diff(args, ctx: Context) -> Result:
    return Result(rv.diff(ctx.conn(), args.revision_id, args.other))


@command("review.queue", collection=True, plain=_plain_records)
def review_queue(args, ctx: Context) -> Result:
    items = rv.list_records(
        ctx.conn(),
        space=args.space,
        run=args.run,
        schema=args.schema,
        page=args.page,
        review_state="pending",
    )
    data, completeness = paginate(
        items,
        args,
        {"scope": "review_queue", "s": args.space, "r": args.run, "c": args.schema, "p": args.page},
    )
    return Result(data, completeness=completeness)


@command("review.show", plain=_plain_show)
def review_show(args, ctx: Context) -> Result:
    return Result(rv.show(ctx.conn(), args.revision_id))


def _decide(args, ctx: Context, decision: str) -> Result:
    settings = ctx.settings.confex
    registry = pipe.load_registry_for(settings)
    return Result(
        rv.decide(
            ctx.conn(),
            registry,
            settings.privacy.digest,
            args.revision_id,
            decision,
            actor(),
            args.reason,
            args.expect_sequence,
        )
    )


@command("review.approve", writes=True)
def review_approve(args, ctx: Context) -> Result:
    return _decide(args, ctx, "approved")


@command("review.reject", writes=True)
def review_reject(args, ctx: Context) -> Result:
    return _decide(args, ctx, "rejected")


@command("review.correct", writes=True)
def review_correct(args, ctx: Context) -> Result:
    try:
        patch = json.loads(args.patch.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfexError("INVALID_ARGUMENT", f"cannot read patch: {exc}", ins.EXIT_USAGE) from None
    settings = ctx.settings.confex
    conn = ctx.conn()
    registry = pipe.load_registry_for(settings)
    who = actor()
    created = rv.correct(conn, registry, args.revision_id, patch, who, args.reason)
    rv.decide(
        conn,
        registry,
        settings.privacy.digest,
        args.revision_id,
        "corrected",
        who,
        args.reason,
        corrected_revision_id=created["revision_id"],
    )
    p = pipeline_for(ctx)
    new = prepo.get_revision(conn, created["revision_id"])
    conn.rollback()
    validation = p.validate_revisions([new])[0]
    return Result(
        {
            **created,
            "validation": {"status": validation["status"], "issues": validation["issues"]},
            "next": f"confex review approve {created['revision_id']} --reason TEXT",
        }
    )


@command("export", writes=True, plain=lambda d, a: kv(list(d.items())))
def export_cmd(args, ctx: Context) -> Result:
    settings = ctx.settings.confex
    data = rv.export(
        ctx.conn(),
        pipe.load_registry_for(settings),
        space=args.space,
        run=args.run,
        schema=args.schema,
        revision_ids=args.revision,
        approved_only=args.approved_only,
        export_format=args.export_format,
        output=args.export_output,
        overwrite=args.export_overwrite,
        actor=actor(),
    )
    return Result(data)
