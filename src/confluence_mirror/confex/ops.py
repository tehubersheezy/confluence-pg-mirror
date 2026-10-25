"""doctor, config, run and eval commands."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from confluence_mirror import db, evaluation
from confluence_mirror import inspection as ins
from confluence_mirror import pipeline as pipe
from confluence_mirror import repository as repo
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.confex.core import Context, Result, command, group, leaf, safe
from confluence_mirror.confex.extract import _exit, options, pipeline_for
from confluence_mirror.confex.nav import sync_scope
from confluence_mirror.confex.render import kv, table
from confluence_mirror.config import PAT_ENV, ConfigError
from confluence_mirror.extraction import provider as provider_mod
from confluence_mirror.extraction.privacy import egress_decision
from confluence_mirror.inspection import ConfexError
from confluence_mirror.sync import SpaceSync

STAGES = ("sync", "parse", "extract")


def register(sub: argparse._SubParsersAction) -> None:
    d = leaf(sub, "doctor", "check configuration; network checks only when asked")
    d.add_argument("--check-db", action="store_true")
    d.add_argument("--check-confluence", action="store_true")
    d.add_argument(
        "--check-model",
        action="store_true",
        help="contact the model endpoint; no document content is sent",
    )
    cg = group(sub, "config", "effective configuration")
    leaf(cg, "config.show")
    leaf(cg, "config.validate")

    r = leaf(sub, "run", "sync -> parse -> extract -> validate; creates review work")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--space", metavar="KEY")
    g.add_argument("--page", type=int, action="append", metavar="ID")
    g.add_argument("--root", type=int, metavar="ID", help="the root and its known descendants")
    g.add_argument("--table", action="append", metavar="TABLE_ID")
    r.add_argument("--extraction-profile", dest="profile")
    r.add_argument("--extractor", choices=("auto", "rules", "model"))
    r.add_argument(
        "--changed-only", action="store_true", help="incremental sync; unchanged units are reused"
    )
    r.add_argument("--from-stage", choices=STAGES, default="sync")
    r.add_argument("--to-stage", choices=STAGES, default="extract")
    r.add_argument(
        "--dry-run", action="store_true", help="plan only: no sync, no writes, no model calls"
    )

    eg = group(sub, "eval", "offline evaluation on labelled synthetic tables")
    er = leaf(eg, "eval.run")
    er.add_argument("--dataset", type=Path, required=True)
    er.add_argument("--extractor", choices=("rules", "model"), default="rules")
    er.add_argument("--report", dest="report_path", type=Path, help="write the report here")
    leaf(eg, "eval.report").add_argument("report_path", type=Path)
    ec = leaf(eg, "eval.compare")
    ec.add_argument("baseline", type=Path)
    ec.add_argument("candidate", type=Path)


# -- doctor / config ---------------------------------------------------------------------------


def _redact_dsn(dsn: str) -> str:
    if "://" in dsn:
        parts = urlsplit(dsn)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
            return parts._replace(netloc=netloc).geturl()
        return dsn
    return " ".join("password=***" if t.startswith("password=") else t for t in dsn.split())


def _effective(ctx: Context) -> dict:
    s = ctx.settings
    c = s.confex
    return {
        "config_file": str(ctx.args.config) if ctx.args.config else "(default search)",
        "confluence": dataclasses.asdict(s.confluence) if s.confluence else None,
        "pat": {"env": PAT_ENV, "set": bool(os.environ.get(PAT_ENV))},
        "database": {"dsn": _redact_dsn(s.dsn) if s.dsn else None},
        "sync": dataclasses.asdict(s.sync),
        "model": {
            **dataclasses.asdict(c.model),
            "api_key": {
                "env": c.model.api_key_env,
                "set": bool(os.environ.get(c.model.api_key_env)),
            },
        },
        "privacy": {
            "egress_approved": c.privacy.egress_approved,
            "approval_reference": c.privacy.approval_reference or None,
            "allowed_spaces": list(c.privacy.allowed_spaces),
            "allowed_endpoints": list(c.privacy.allowed_endpoints),
            "retain_requests": c.privacy.retain_requests,
            "reduced_audit": c.privacy.reduced_audit,
            "redaction_rules": [name for name, _ in c.privacy.rules],
            "digest": c.privacy.digest,
        },
        "extraction": dataclasses.asdict(c.extraction),
        "budgets": dataclasses.asdict(c.budgets),
        "pricing": c.pricing,
    }


@command("config.show", schema=False, plain=lambda d, a: json.dumps(d, indent=2) + "\n")
def config_show(args, ctx: Context) -> Result:
    return Result(_effective(ctx))


@command("config.validate", schema=False)
def config_validate(args, ctx: Context) -> Result:
    problems = []
    try:
        _effective(ctx)
        pipe.load_registry_for(ctx.settings.confex)
    except (ConfigError, ConfexError) as exc:
        problems.append(str(exc))
    return Result(
        {"valid": not problems, "problems": problems},
        exit_code=ins.EXIT_OK if not problems else ins.EXIT_USAGE,
    )


def _plain_doctor(data, args):
    rows = [
        [
            c["check"],
            "ok" if c["ok"] else ("skipped" if c["ok"] is None else "FAIL"),
            c.get("detail", ""),
        ]
        for c in data["checks"]
    ]
    return table(["check", "status", "detail"], rows)


@command("doctor", schema=False, plain=_plain_doctor)
def doctor(args, ctx: Context) -> Result:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": ok, "detail": safe(detail)})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        settings = ctx.settings
        add("config", True, "loaded")
    except ConfigError as exc:
        add("config", False, str(exc))
        return Result({"checks": checks}, exit_code=ins.EXIT_USAGE)
    try:
        reg = pipe.load_registry_for(settings.confex)
        add("registry", True, f"{len(reg.schemas)} schemas, {len(reg.profiles)} profiles")
    except ConfexError as exc:
        add("registry", False, exc.message)
    sdk = importlib.util.find_spec("anthropic") is not None
    add(
        "model SDK",
        sdk or None,
        "installed"
        if sdk
        else "not installed (pip install 'confluence-pg-mirror[model]'); rules extraction works",
    )
    decision = egress_decision(settings.confex.privacy, settings.confex.model.endpoint, None)
    add(
        "model egress",
        decision.allowed or None,
        "approved" if decision.allowed else "; ".join(decision.reasons),
    )
    price = settings.confex.pricing.get(settings.confex.model.model)
    add(
        "pricing",
        bool(price) or None,
        f"as of {price['as_of']}" if price else "no dated price: costs report as unknown",
    )

    if args.check_db:
        try:
            with db.connect(settings.dsn) as conn:
                status = db.migration_status(conn)
                pending = [m["version"] for m in status if not m["applied"]]
                version = conn.execute("SHOW server_version").fetchone()[0]
            add(
                "database",
                not pending,
                f"PostgreSQL {version}; "
                + (f"pending migrations {pending}" if pending else "schema up to date"),
            )
        except Exception as exc:
            add("database", False, str(exc).splitlines()[0])
    else:
        add("database", None, "pass --check-db")
    if args.check_confluence:
        try:
            user = ctx.client().current_user()
            add(
                "confluence",
                True,
                f"authenticated as {user.get('username') or user.get('userKey')}",
            )
        except Exception as exc:
            add("confluence", False, str(exc))
    else:
        add("confluence", None, "pass --check-confluence")
    if args.check_model:
        try:
            p = provider_mod.make_provider(settings.confex.model)
            res = p.check()
            add("model", res["ok"], res.get("model") or res.get("error"))
        except Exception as exc:
            add("model", False, str(exc))
    else:
        add("model", None, "pass --check-model (no document content is sent)")
    failed = any(c["ok"] is False for c in checks)
    return Result({"checks": checks}, exit_code=ins.EXIT_UNAVAILABLE if failed else ins.EXIT_OK)


# -- run ------------------------------------------------------------------------------------------


def _page_ids(conn, args) -> list[int] | None:
    if args.root is not None:
        ins._page(conn, args.root)
        ids = [r["page_id"] for r in repo.page_subtree(conn, args.root, 10_000) if not r["cycle"]]
        conn.rollback()
        return ids
    return args.page


def _plain_run(data, args):
    out = ""
    for st in data["stages"]:
        out += f"{st['stage']}: {st['status']}  {safe(st.get('summary', ''))}\n"
    out += kv([(k, data[k]) for k in ("awaiting_review", "next") if k in data])
    return out


@command("run", writes=True, plain=_plain_run)
def run_cmd(args, ctx: Context) -> Result:
    order = list(STAGES)
    lo, hi = order.index(args.from_stage), order.index(args.to_stage)
    if lo > hi:
        raise ConfexError("ARGUMENT_CONFLICT", "--from-stage is after --to-stage", ins.EXIT_USAGE)
    stages = order[lo : hi + 1]
    if args.table and any(s in stages for s in ("sync", "parse")):
        if args.from_stage != "sync":
            raise ConfexError(
                "ARGUMENT_CONFLICT",
                "a --table scope starts at extract (its artifact already exists)",
                ins.EXIT_USAGE,
            )
        stages = ["extract"] if "extract" in stages else []
    conn = ctx.conn()
    if args.dry_run:
        stage_rows = [{"stage": s, "status": "planned"} for s in stages]
        try:
            page_ids = _page_ids(conn, args)
            targets, missing = pipe.resolve_scope(
                conn, space_key=args.space, page_ids=page_ids, table_ids=args.table
            )
        except ConfexError as exc:
            if exc.code != "NOT_FOUND" or "sync" not in stages:
                raise
            conn.rollback()
            return Result(
                {
                    "dry_run": True,
                    "would_run": stages,
                    "plan": None,
                    "note": f"{exc.message}; sync would run first, so nothing can be "
                    "planned from the mirror yet",
                    "stages": stage_rows,
                }
            )
        plan = pipeline_for(ctx).plan(targets, options(args, ctx))
        return Result(
            {
                "dry_run": True,
                "would_run": stages,
                "plan": plan,
                "missing_prerequisites": missing,
                "stages": stage_rows,
            }
        )
    results = []
    report = None
    # A --root sync discovers the tree first; the other stages then use what it mirrored.
    synced_tree = args.root is not None and "sync" in stages
    page_ids = None if args.space or synced_tree else _page_ids(conn, args)
    for stage in stages:
        if stage == "sync":
            syncer = SpaceSync(conn, ctx.client(), ctx.settings.sync)
            if args.space:
                rep = syncer.run(args.space, "incremental" if args.changed_only else "auto")
                results.append(
                    {
                        "stage": "sync",
                        "status": rep.status,
                        "summary": {
                            k: v for k, v in rep.as_dict().items() if k.startswith("pages_")
                        },
                    }
                )
            else:
                found, missing = syncer.run_scope(sync_scope(args, full=False))
                statuses = [r.status for r in found] + ["partial"] * bool(missing)
                results.append(
                    {
                        "stage": "sync",
                        "status": "succeeded"
                        if all(s == "succeeded" for s in statuses)
                        else "partial",
                        "summary": {"runs": len(found), "missing": missing},
                    }
                )
                if args.root is not None:  # the tree may have grown
                    page_ids = _page_ids(conn, args)
        elif stage == "parse":
            data = ins.parse_pages(
                conn,
                space_key=args.space,
                page_ids=page_ids,
                representations=["export_view"],
                all_versions=False,
            )
            results.append(
                {
                    "stage": "parse",
                    "status": "succeeded" if not data["failed"] else "partial",
                    "summary": {k: data[k] for k in ("parsed", "reused", "failed")},
                }
            )
        else:
            targets, missing = pipe.resolve_scope(
                conn, space_key=args.space, page_ids=page_ids, table_ids=args.table
            )
            if missing and "parse" not in stages:
                raise ConfexError(
                    "MISSING_PREREQUISITE",
                    f"{len(missing)} page(s) have no parse artifacts; start at --from-stage parse",
                    ins.EXIT_UNAVAILABLE,
                    {"missing": missing},
                )
            p = pipeline_for(ctx)
            try:
                report = p.extract(targets, options(args, ctx), command="run")
            finally:
                p.close()
            results.append(
                {
                    "stage": "extract",
                    "status": report.status,
                    "summary": report.counts,
                    "run_id": report.run_id,
                }
            )
    data = {"stages": results}
    if report is not None:
        awaiting = prepo.records(conn, run=report.run_id)
        conn.rollback()
        data["run_id"] = report.run_id
        data["awaiting_review"] = len(awaiting)
        data["next"] = (
            f"confex review queue --run {report.run_id}"
            if report.status in ("succeeded",)
            else f"confex runs show {report.run_id}"
        )
    worst = ins.EXIT_OK
    if report is not None:
        worst = _exit(report)
    elif any(r["status"] != "succeeded" for r in results):
        worst = ins.EXIT_PARTIAL
    return Result(data, exit_code=worst)


# -- eval ----------------------------------------------------------------------------------------


def _plain_eval(data, args):
    out = kv([(k, data.get(k)) for k in ("dataset", "split", "extractor", "meets_targets")])
    out += "overall: " + json.dumps(data["overall"]) + "\n"
    for fam, m in data["by_family"].items():
        out += (
            f"  {fam}: precision={m['record_precision']} recall={m['record_recall']} "
            f"fields={m['field_accuracy']} evidence={m['evidence_validity']}\n"
        )
    out += f"-- {data['note']}\n"
    return out


@command("eval.run", schema=False, plain=_plain_eval)
def eval_run(args, ctx: Context) -> Result:
    try:
        dataset = json.loads(args.dataset.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfexError(
            "INVALID_ARGUMENT", f"cannot read dataset: {exc}", ins.EXIT_USAGE
        ) from None
    settings = ctx.settings.confex
    registry = pipe.load_registry_for(settings)
    provider = None
    if args.extractor == "model":
        decision = egress_decision(settings.privacy, settings.model.endpoint, None)
        if not decision.allowed:
            raise ConfexError("EGRESS_DENIED", "; ".join(decision.reasons), ins.EXIT_POLICY)
        provider = provider_mod.make_provider(settings.model)
    report = evaluation.run_dataset(
        dataset,
        registry,
        extractor=args.extractor,
        provider=provider,
        model=settings.model.as_dict(),
        policy=settings.privacy,
        versions=pipe.versions(registry),
    )
    if args.report_path:
        from confluence_mirror.confex.core import dumps, write_text

        write_text(dumps(report, indent=2) + "\n", args.report_path, overwrite=True)
    return Result(report, exit_code=ins.EXIT_OK if report["meets_targets"] else ins.EXIT_VALIDATION)


@command("eval.report", schema=False, plain=_plain_eval)
def eval_report(args, ctx: Context) -> Result:
    return Result(json.loads(args.report_path.read_text("utf-8")))


@command("eval.compare", schema=False, plain=lambda d, a: json.dumps(d, indent=2) + "\n")
def eval_compare(args, ctx: Context) -> Result:
    a = json.loads(args.baseline.read_text("utf-8"))
    b = json.loads(args.candidate.read_text("utf-8"))
    return Result(evaluation.compare(a, b))
