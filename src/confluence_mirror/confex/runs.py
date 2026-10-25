"""runs list | show | items | request | resume | retry."""

from __future__ import annotations

import argparse
import json

from confluence_mirror import inspection as ins
from confluence_mirror import pipeline as pipe
from confluence_mirror import repository as repo
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.confex.core import Context, Result, command, group, leaf, positive
from confluence_mirror.confex.extract import _exit, _plain_report, options, pipeline_for
from confluence_mirror.confex.render import kv, table
from confluence_mirror.structure.ids import digest


def register(sub: argparse._SubParsersAction) -> None:
    rg = group(sub, "runs", "pipeline runs, items and attempts")
    rl = leaf(rg, "runs.list")
    rl.add_argument("--limit", type=positive, default=20)
    leaf(rg, "runs.show").add_argument("run_id")
    ri = leaf(rg, "runs.items")
    ri.add_argument("run_id")
    ri.add_argument("--status")
    ri.add_argument("--stage", choices=("classify", "plan", "extract", "validate"))
    leaf(rg, "runs.request", "the retained request body of one attempt").add_argument("attempt_id")
    rr = leaf(rg, "runs.resume", "continue a run; completed work is reused")
    rr.add_argument("run_id")
    rt = leaf(rg, "runs.retry", "new attempts for a run's failed items (new run)")
    rt.add_argument("run_id")
    rt.add_argument("--failed-only", action="store_true", default=True)


def _get(conn, run_id: str) -> dict:
    run = prepo.get_run(conn, run_id)
    if run is None:
        raise ins.not_found(f"run {run_id} does not exist")
    return run


@command(
    "runs.list",
    plain=lambda d, a: table(
        ["run_id", "command", "status", "started_at", "finished_at"],
        [[r["run_id"], r["command"], r["status"], r["started_at"], r["finished_at"]] for r in d],
    ),
)
def runs_list(args, ctx: Context) -> Result:
    return Result(prepo.list_runs(ctx.conn(), args.limit))


def _plain_run(data, args):
    out = kv(
        [
            (k, data[k])
            for k in (
                "run_id",
                "command",
                "status",
                "started_at",
                "finished_at",
                "stage_counts",
                "usage",
                "parent_run_id",
            )
        ]
    )
    out += f"inputs: {len(data['input_manifest'])} table occurrence(s)\n"
    for d in data["diagnostics"]:
        out += f"! {d['code']}: {d['message']}\n"
    out += f"next: {data['next']}\n"
    return out


@command("runs.show", plain=_plain_run)
def runs_show(args, ctx: Context) -> Result:
    run = _get(ctx.conn(), args.run_id)
    counts = run["stage_counts"] or {}
    open_items = sum(
        n for s in counts.values() for k, n in s.items() if k in ("pending", "running", "failed")
    )
    run["next"] = (
        f"confex runs resume {run['run_id']}"
        if run["status"] in ("partial", "running") and open_items
        else f"confex records list --run {run['run_id']}"
    )
    return Result(run)


@command(
    "runs.items",
    plain=lambda d, a: table(
        ["stage", "item_id", "status", "attempts", "detail"],
        [[i["stage"], i["item_id"], i["status"], i["attempts"], i["detail"]] for i in d],
    ),
)
def runs_items(args, ctx: Context) -> Result:
    _get(ctx.conn(), args.run_id)
    return Result(prepo.run_items(ctx.conn(), args.run_id, args.status, args.stage))


@command("runs.request", plain=lambda d, a: json.dumps(d, indent=2, default=str) + "\n")
def runs_request(args, ctx: Context) -> Result:
    attempt = prepo.get_attempt(ctx.conn(), args.attempt_id)
    if attempt is None:
        raise ins.not_found(f"attempt {args.attempt_id} does not exist")
    body = attempt["request_body"]
    if body is None:
        reason = (
            "rule extraction sends no request"
            if attempt["extractor"] == "rules"
            else "the request was not retained (reduced-audit policy) or never sent"
        )
        raise ins.ConfexError(
            "REQUEST_NOT_RETAINED", reason, ins.EXIT_UNAVAILABLE, {"attempt_id": args.attempt_id}
        )
    return Result(
        {
            "attempt_id": attempt["attempt_id"],
            "run_id": attempt["run_id"],
            "target_id": attempt["target_id"],
            "stage": attempt["stage"],
            "attempt_number": attempt["attempt_number"],
            "parent_attempt_id": attempt["parent_attempt_id"],
            "outcome": attempt["outcome"],
            "request_hash": attempt["request_hash"],
            "sent_body_hash": digest(body),
            "request_context": attempt["request_context"],
            "note": "the body as the SDK put it on the wire; authentication headers excluded",
            "request": body,
        }
    )


def _targets_from_manifest(conn, run: dict) -> list[pipe.Target]:
    """The frozen inputs of a run; a page changed since then is not substituted."""
    targets = []
    for entry in run["input_manifest"]:
        artifact = repo.load_artifact(conn, entry["artifact_id"])
        if artifact is None:
            continue
        table_ = next(t for t in artifact["tables"] if t["table_id"] == entry["table_id"])
        targets.append(
            pipe.Target(
                table_, artifact, repo.get_page(conn, entry["page_id"]), entry.get("blocked")
            )
        )
    conn.rollback()
    return targets


def _run_options(args, ctx: Context, run: dict, force: bool) -> pipe.Options:
    scope = run["scope"] or {}
    ns = argparse.Namespace(
        profile=scope.get("profile"),
        extractor=scope.get("extractor"),
        force=force,
        model_classification=False,
    )
    return options(ns, ctx)


@command("runs.resume", writes=True, plain=_plain_report)
def runs_resume(args, ctx: Context) -> Result:
    conn = ctx.conn()
    run = _get(conn, args.run_id)
    if run["status"] in ("succeeded",):
        return Result(
            {
                "run_id": run["run_id"],
                "status": run["status"],
                "counts": run["stage_counts"],
                "items": [],
                "note": "nothing to resume",
            }
        )
    p = pipeline_for(ctx)
    try:
        report = p.extract(
            _targets_from_manifest(conn, run),
            _run_options(args, ctx, run, force=bool((run["scope"] or {}).get("force"))),
            run_id=run["run_id"],
        )
    finally:
        p.close()
    return Result(report.as_dict(), exit_code=_exit(report))


@command("runs.retry", writes=True, plain=_plain_report)
def runs_retry(args, ctx: Context) -> Result:
    conn = ctx.conn()
    run = _get(conn, args.run_id)
    failed = {i["item_id"] for i in prepo.run_items(conn, run["run_id"], "failed", "extract")}
    conn.rollback()
    if not failed:
        return Result(
            {
                "run_id": None,
                "status": "succeeded",
                "counts": {},
                "items": [],
                "note": "no failed extraction items",
            }
        )
    p = pipeline_for(ctx)
    try:
        report = p.extract(
            _targets_from_manifest(conn, run),
            _run_options(args, ctx, run, force=True),
            command="retry",
            parent_run=run["run_id"],
            only_items=failed,
        )
    finally:
        p.close()
    return Result(report.as_dict(), exit_code=_exit(report))
