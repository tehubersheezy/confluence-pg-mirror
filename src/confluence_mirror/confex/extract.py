"""profiles, schemas, classify, plan, extract and validate commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from confluence_mirror import inspection as ins
from confluence_mirror import pipeline as pipe
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.confex.core import Context, Result, command, group, leaf, safe
from confluence_mirror.confex.render import kv, table
from confluence_mirror.extraction import provider as provider_mod
from confluence_mirror.extraction.registry import validate_profile, validate_schema
from confluence_mirror.inspection import ConfexError

EXTRACTORS = ("auto", "rules", "model")


def scope_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--space", metavar="KEY")
    g.add_argument("--page", type=int, action="append", metavar="ID")
    g.add_argument("--table", action="append", metavar="TABLE_ID")
    p.add_argument("--extraction-profile", dest="profile", metavar="PROFILE[@VERSION]")


def register(sub: argparse._SubParsersAction) -> None:
    pg = group(sub, "profiles", "approved extraction profiles")
    leaf(pg, "profiles.list")
    leaf(pg, "profiles.show").add_argument("profile")
    sg = group(sub, "schemas", "registered domain schemas")
    leaf(sg, "schemas.list")
    leaf(sg, "schemas.show").add_argument("schema")
    leaf(sg, "schemas.validate", "check a schema or profile file").add_argument("path", type=Path)

    c = leaf(sub, "classify", "classify tables and route them to profiles")
    scope_args(c)
    c.add_argument(
        "--model", action="store_true", help="allow model classification where policy permits it"
    )
    p = leaf(sub, "plan", "what extraction would do; no model calls and no writes")
    scope_args(p)
    p.add_argument("--extractor", choices=EXTRACTORS, default=None)
    e = leaf(sub, "extract", "classify, plan, extract and validate; creates review work")
    scope_args(e)
    e.add_argument("--extractor", choices=EXTRACTORS, default=None)
    e.add_argument("--model-classification", action="store_true")
    e.add_argument("--plan-only", action="store_true", help="like `confex plan`")
    e.add_argument(
        "--persist-plan",
        action="store_true",
        help="store classifications and units without extracting",
    )
    e.add_argument(
        "--force", action="store_true", help="new attempts even where a compatible result exists"
    )
    v = leaf(sub, "validate", "re-run deterministic validation (no extraction)")
    vg = v.add_mutually_exclusive_group(required=True)
    vg.add_argument("--run", metavar="RUN_ID")
    vg.add_argument("--space", metavar="KEY")
    vg.add_argument("--page", type=int)


def pipeline_for(ctx: Context) -> pipe.Pipeline:
    settings = ctx.settings.confex
    return pipe.Pipeline(
        ctx.conn(),
        settings,
        pipe.load_registry_for(settings),
        provider_factory=lambda s: provider_mod.make_provider(s),
    )


def options(args, ctx: Context) -> pipe.Options:
    ext = ctx.settings.confex.extraction
    return pipe.Options(
        profile=args.profile or ext.default_profile,
        extractor=getattr(args, "extractor", None) or ext.extractor,
        force=getattr(args, "force", False),
        model_classification=getattr(args, "model_classification", False)
        or getattr(args, "model", False),
    )


def _targets(args, ctx: Context):
    return pipe.resolve_scope(
        ctx.conn(), space_key=args.space, page_ids=args.page, table_ids=args.table
    )


# -- registry -------------------------------------------------------------------------------


@command(
    "profiles.list",
    plain=lambda d, a: table(
        ["profile", "schema", "families", "extractor", "description"],
        [[x["profile"], x["schema"], x["families"], x["extractor"], x["description"]] for x in d],
    ),
)
def profiles_list(args, ctx: Context) -> Result:
    reg = pipe.load_registry_for(ctx.settings.confex)
    return Result(
        [
            {
                "profile": p.ref,
                "schema": p.schema.ref,
                "families": p.get("families"),
                "extractor": p.get("extractor", {}).get("default"),
                "description": p.get("description"),
                "digest": p.digest,
            }
            for p in sorted(reg.profiles.values(), key=lambda p: p.ref)
        ]
    )


@command("profiles.show", plain=lambda d, a: json.dumps(d, indent=2) + "\n")
def profiles_show(args, ctx: Context) -> Result:
    reg = pipe.load_registry_for(ctx.settings.confex)
    try:
        p = reg.profile(args.profile)
    except Exception as exc:
        raise ins.not_found(str(exc)) from None
    return Result({"profile": p.ref, "digest": p.digest, **p.data})


@command(
    "schemas.list",
    plain=lambda d, a: table(
        ["schema", "record_kinds", "fields"],
        [[x["schema"], x["record_kinds"], x["fields"]] for x in d],
    ),
)
def schemas_list(args, ctx: Context) -> Result:
    reg = pipe.load_registry_for(ctx.settings.confex)
    return Result(
        [
            {
                "schema": s.ref,
                "record_kinds": s.data["record_kinds"],
                "fields": sorted(s.fields),
                "digest": s.digest,
            }
            for s in sorted(reg.schemas.values(), key=lambda s: s.ref)
        ]
    )


@command("schemas.show", plain=lambda d, a: json.dumps(d, indent=2) + "\n")
def schemas_show(args, ctx: Context) -> Result:
    reg = pipe.load_registry_for(ctx.settings.confex)
    try:
        s = reg.schema(args.schema)
    except Exception as exc:
        raise ins.not_found(str(exc)) from None
    return Result({"schema": s.ref, "digest": s.digest, **s.data})


@command("schemas.validate", schema=False)
def schemas_validate(args, ctx: Context) -> Result:
    try:
        data = json.loads(args.path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfexError(
            "INVALID_ARGUMENT", f"cannot read {args.path}: {exc}", ins.EXIT_USAGE
        ) from None
    reg = pipe.load_registry_for(ctx.settings.confex)
    if "profile_id" in data:
        problems = validate_profile(data, reg.schemas, args.path.name)
        kind = "profile"
    else:
        problems = validate_schema(data, args.path.name)
        kind = "schema"
    return Result(
        {"path": str(args.path), "kind": kind, "valid": not problems, "problems": problems},
        exit_code=ins.EXIT_OK if not problems else ins.EXIT_VALIDATION,
    )


# -- pipeline -------------------------------------------------------------------------------


def _plain_report(data, args):
    out = kv([(k, data[k]) for k in ("run_id", "status", "counts") if k in data])
    if data.get("budget"):
        out += kv([("budget", data["budget"])])
    for item in data.get("items", []):
        ident = item.get("table_id") or item.get("unit_id")
        rest = {k: v for k, v in item.items() if k not in ("table_id", "unit_id")}
        out += f"  {ident}  {safe(rest)}\n"
    for m in data.get("missing_prerequisites", []):
        out += f"  missing: page {m['page_id']} {m['reason']} ({m['hint']})\n"
    for d in data.get("diagnostics", []):
        out += f"! {d['code']}: {safe(d['message'])}\n"
    return out


def _exit(report: pipe.Report) -> int:
    if report.status in ("succeeded", "planned"):
        return ins.EXIT_OK
    if report.status == "partial":
        return ins.EXIT_PARTIAL
    if report.status == "blocked":
        egress = any("EGRESS_DENIED" in str(i.get("reason", "")) for i in report.items)
        return ins.EXIT_POLICY if egress else ins.EXIT_UNAVAILABLE
    return ins.EXIT_INTERNAL


@command("classify", writes=True, plain=_plain_report)
def classify_cmd(args, ctx: Context) -> Result:
    p = pipeline_for(ctx)
    try:
        targets, missing = _targets(args, ctx)
        report = p.classify_only(targets, options(args, ctx))
    finally:
        p.close()
    report.missing = missing
    return Result(report.as_dict(), exit_code=_exit(report))


def _plain_plan(data, args):
    rows = [
        [
            i["table_id"],
            i["status"],
            i.get("classification"),
            i.get("profile"),
            i.get("extractor"),
            i.get("units"),
            i.get("reason"),
        ]
        for i in data["items"]
    ]
    out = (
        table(
            ["table_id", "status", "classification", "profile", "extractor", "units", "reason"],
            rows,
        )
        if rows
        else "(nothing selected)\n"
    )
    out += kv(
        [
            (k, data[k])
            for k in (
                "model_requests",
                "estimated_input_tokens",
                "reserved_output_tokens",
                "estimated_cost_usd",
                "cost_note",
                "egress_policy",
            )
        ]
    )
    return out


@command("plan", plain=_plain_plan)
def plan_cmd(args, ctx: Context) -> Result:
    p = pipeline_for(ctx)
    targets, missing = _targets(args, ctx)
    data = p.plan(targets, options(args, ctx))
    data["missing_prerequisites"] = missing
    return Result(data)


@command(
    "extract",
    writes=True,
    plain=lambda d, a: _plain_plan(d, a) if "model_requests" in d else _plain_report(d, a),
)
def extract_cmd(args, ctx: Context) -> Result:
    p = pipeline_for(ctx)
    try:
        targets, missing = _targets(args, ctx)
        opts = options(args, ctx)
        if args.plan_only:
            data = p.plan(targets, opts)
            data["missing_prerequisites"] = missing
            return Result(data)
        report = p.extract(targets, opts, execute=not args.persist_plan)
    finally:
        p.close()
    report.missing = missing
    return Result(report.as_dict(), exit_code=_exit(report))


def _plain_validation(data, args):
    rows = [
        [
            v["revision_id"],
            v["status"],
            len(v["issues"]),
            ", ".join(sorted({i["code"] for i in v["issues"]})),
        ]
        for v in data["results"]
    ]
    out = table(["revision", "status", "issues", "codes"], rows) if rows else "(no revisions)\n"
    return out + kv([("summary", data["summary"])])


@command("validate", writes=True, plain=_plain_validation)
def validate_cmd(args, ctx: Context) -> Result:
    p = pipeline_for(ctx)
    conn = ctx.conn()
    if args.run:
        if prepo.get_run(conn, args.run) is None:
            raise ins.not_found(f"run {args.run} does not exist")
        revs = prepo.revisions_for_run(conn, args.run)
    else:
        revs = prepo.records(conn, space=args.space, page=args.page)
    conn.rollback()
    full = [prepo.get_revision(conn, r["revision_id"]) for r in revs]
    conn.rollback()
    results = p.validate_revisions([r for r in full if r])
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    bad = summary.get("failed", 0) + summary.get("blocked", 0)
    return Result(
        {"results": results, "summary": summary},
        exit_code=ins.EXIT_VALIDATION if bad else ins.EXIT_OK,
    )
