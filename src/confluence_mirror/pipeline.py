"""Extraction pipeline orchestration: scope -> classify -> plan -> extract -> validate.

Stages are explicit and their artifacts immutable. Network and model calls never run
inside a database transaction: a model request is dispatched, its result staged, and then
one short transaction commits the attempt, candidate revisions and item state together.
The model never approves anything; every candidate waits for human review.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import psycopg

from confluence_mirror import inspection as ins
from confluence_mirror import repository as repo
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.config import ConfexSettings
from confluence_mirror.extraction import classify as cls
from confluence_mirror.extraction import payload as pl
from confluence_mirror.extraction import templates as tpl
from confluence_mirror.extraction import validation as val
from confluence_mirror.extraction.candidates import build_candidates
from confluence_mirror.extraction.privacy import egress_decision
from confluence_mirror.extraction.provider import Outcome, cost_usd, make_provider
from confluence_mirror.extraction.registry import Profile, Registry, RegistryError
from confluence_mirror.extraction.rules import RULES_VERSION, RuleExtractor
from confluence_mirror.extraction.units import PLANNER_VERSION, coverage_problems, plan_units
from confluence_mirror.inspection import EXIT_UNAVAILABLE, EXIT_USAGE, ConfexError
from confluence_mirror.logging_utils import redact
from confluence_mirror.structure import PARSER_VERSION
from confluence_mirror.structure.ids import digest, opaque_id

log = logging.getLogger(__name__)

LEASE_SECONDS = 600
MAX_OUTPUT_TOKENS = 128_000


def versions(registry: Registry) -> dict[str, str]:
    return {
        "parser": PARSER_VERSION,
        "classifier": cls.CLASSIFIER_VERSION,
        "planner": PLANNER_VERSION,
        "prompt": pl.PROMPT_VERSION,
        "rules": RULES_VERSION,
        "validator": val.VALIDATOR_VERSION,
        "registry": registry.digest,
    }


def principal() -> str:
    return f"local:{os.environ.get('USER') or 'unknown'}@{socket.gethostname()}"


# -- scope ------------------------------------------------------------------------------------


@dataclass
class Target:
    table: dict
    artifact: dict
    page: dict
    blocked: str | None = None


def resolve_scope(
    conn, *, space_key: str | None, page_ids: list[int] | None, table_ids: list[str] | None
) -> tuple[list[Target], list[dict]]:
    """Selected table occurrences of current page versions; never fetches anything."""
    if sum(bool(x) for x in (space_key, page_ids, table_ids)) != 1:
        raise ConfexError(
            "ARGUMENT_CONFLICT", "give exactly one of --space, --page or --table", EXIT_USAGE
        )
    targets: list[Target] = []
    missing: list[dict] = []
    if table_ids:
        for tid in table_ids:
            occ = repo.table_occurrence(conn, tid)
            if occ is None:
                raise ins.not_found(f"table {tid} is not persisted; run `confex parse`")
            artifact = repo.load_artifact(conn, occ["artifact_id"])
            table = next(t for t in artifact["tables"] if t["table_id"] == tid)
            targets.append(Target(table, artifact, repo.get_page(conn, occ["page_id"])))
        conn.rollback()
        return targets, missing
    space_id = ins._space(conn, space_key)["space_id"] if space_key else None
    pages = repo.parse_targets(conn, space_id, page_ids, all_versions=False)
    if page_ids:
        for pid in page_ids:
            ins._page(conn, pid)
    for page_id, version in pages:
        page = repo.get_page(conn, page_id)
        artifacts = repo.latest_artifacts(conn, page_id, version)
        if not artifacts:
            missing.append(
                {
                    "page_id": page_id,
                    "version_number": version,
                    "reason": "not parsed",
                    "hint": f"confex parse --page {page_id}",
                }
            )
            continue
        sel = ins.selection(conn, page_id, version)
        by_id = {t["table_id"]: (rep, t) for rep, a in artifacts.items() for t in a["tables"]}
        for s in sel["selections"]:
            rep, table = by_id[s["table_id"]]
            targets.append(Target(table, artifacts[rep], page, s["block_reason"]))
    conn.rollback()
    return targets, missing


# -- budgets ----------------------------------------------------------------------------------


@dataclass
class Budget:
    limits: dict[str, Any]
    pricing: dict | None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    exhausted: str | None = None

    def check_configuration(self) -> list[str]:
        if self.limits.get("max_cost_usd") and not self.pricing:
            return ["a monetary budget is set but no dated price exists for the model"]
        return []

    def reserve(self, est_input: int, max_output: int) -> bool:
        lim = self.limits
        checks = [
            ("max_requests", self.requests + 1),
            ("max_input_tokens", self.input_tokens + est_input),
            ("max_output_tokens", self.output_tokens + max_output),
        ]
        for name, value in checks:
            if lim.get(name) is not None and value > lim[name]:
                self.exhausted = f"{name} ({lim[name]}) would be exceeded"
                return False
        if lim.get("max_cost_usd") and self.pricing:
            projected = self.cost + cost_usd(
                {"input_tokens": est_input, "output_tokens": max_output}, self.pricing
            )
            if projected > lim["max_cost_usd"]:
                self.exhausted = f"max_cost_usd ({lim['max_cost_usd']}) would be exceeded"
                return False
        self.requests += 1
        self.input_tokens += est_input
        self.output_tokens += max_output
        return True

    def settle(self, est_input: int, max_output: int, outcome: Outcome) -> float | None:
        """Replace the reservation with actual usage; unknown outcomes keep the reservation."""
        if outcome.status == "outcome_unknown" or not outcome.usage:
            self.cost_known = self.cost_known and self.pricing is not None
            if self.pricing:
                self.cost += cost_usd(
                    {"input_tokens": est_input, "output_tokens": max_output}, self.pricing
                )
            return None
        self.input_tokens += outcome.usage.get("input_tokens", 0) - est_input
        self.output_tokens += outcome.usage.get("output_tokens", 0) - max_output
        spent = cost_usd(outcome.usage, self.pricing)
        if spent is None:
            self.cost_known = False
        else:
            self.cost += spent
        return spent

    def as_dict(self) -> dict:
        return {
            "limits": self.limits,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost, 6) if self.cost_known else None,
            "cost_note": None if self.cost_known else "unknown: no dated price configured",
            "exhausted": self.exhausted,
        }


# -- the service --------------------------------------------------------------------------------


@dataclass
class Options:
    profile: str | None = None
    extractor: str = "auto"  # auto | rules | model
    force: bool = False
    model_classification: bool = False


@dataclass
class TemplateOptions:
    min_children: int = 2
    sample: int = 8
    dry_run: bool = False
    force: bool = False


@dataclass
class Report:
    run_id: str | None
    status: str
    counts: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "counts": self.counts,
            "items": self.items,
            "missing_prerequisites": self.missing,
            "budget": self.budget,
            "diagnostics": self.diagnostics,
        }


class Pipeline:
    def __init__(
        self,
        conn: psycopg.Connection,
        settings: ConfexSettings,
        registry: Registry,
        provider_factory: Callable = make_provider,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.registry = registry
        self.provider_factory = provider_factory
        self.model = settings.model.as_dict()
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._provider = None

    @property
    def config_digest(self) -> str:
        s = self.settings
        return digest(
            {
                "model": self.model,
                "privacy": s.privacy.digest,
                "extraction": [s.extraction.default_profile, s.extraction.extractor],
                "budgets": vars(s.budgets),
            }
        )[:16]

    def provider(self):
        if self._provider is None:
            self._provider = self.provider_factory(self.settings.model)
        return self._provider

    def close(self) -> None:
        if self._provider is not None and hasattr(self._provider, "close"):
            self._provider.close()

    # -- classification ------------------------------------------------------------------

    def egress(self, page: dict):
        return egress_decision(
            self.settings.privacy, self.settings.model.endpoint, page.get("space_key")
        )

    def classify(self, t: Target, opts: Options, run_id: str | None, persist: bool) -> dict:
        profile = opts.profile or None
        allowed = opts.model_classification and self.egress(t.page).allowed
        result = cls.classify_table(
            t.table, t.artifact, self.registry, explicit_profile=profile, model_allowed=allowed
        )
        if result["status"] == "needs_model" and allowed and persist:
            result = self._model_classify(t, result, run_id)
        if persist:
            with self.conn.transaction():
                prepo.insert_classification(
                    self.conn,
                    {
                        "classification_id": result["classification_id"],
                        "table_id": t.table["table_id"],
                        "artifact_id": t.artifact["artifact_id"],
                        "page_id": t.artifact["page_id"],
                        "version_number": t.artifact["version_number"],
                        "family": result["family"],
                        "status": result["status"],
                        "method": result["method"],
                        "profile": result["profile"],
                        "schema_ref": result["schema"],
                        "result": result,
                        "attempt_id": result.get("attempt_id"),
                    },
                )
            self.conn.commit()
        return result

    def _model_classify(self, t: Target, prior: dict, run_id: str | None) -> dict:
        layout = cls.Layout(t.table)
        rows = list(range(1, min(layout.rows, 6) + 1))
        unit = {
            "unit_id": "classify",
            "cell_ids": [
                c["cell_id"]
                for c in t.table["cells"]
                if c.get("origin") and c["origin"]["row"] in rows
            ],
            "header_rows": [],
            "selected_rows": rows,
            "delegated_tables": [],
            "parent_context": None,
            "chunk_index": 1,
            "chunk_count": 1,
        }
        evidence, _ = pl.build_payload(
            t.artifact,
            t.table,
            unit,
            {"row_roles": [], "column_bindings": []},
            self.settings.privacy,
            t.page,
        )
        evidence.pop("columns", None)
        candidates = prior.get("candidates") or None
        request = pl.classification_request(evidence, self.registry, self.model, candidates)
        attempt_id, outcome = self._call(request, t.table["table_id"], "classify", run_id, None, 1)
        if outcome.status != "succeeded":
            return {
                **prior,
                "status": "model_failed",
                "reason": f"model classification {outcome.status}",
                "attempt_id": attempt_id,
            }
        out = cls.from_model(t.table, t.artifact, self.registry, outcome.parsed, prior)
        out["attempt_id"] = attempt_id
        return out

    # -- model calls -----------------------------------------------------------------------

    def _call(
        self,
        request: dict,
        target_id: str,
        stage: str,
        run_id: str | None,
        parent: str | None,
        number: int,
    ) -> tuple[str, Outcome]:
        """Synchronous call used for classification and repair (rare, sequential)."""
        attempt_id = self._open_attempt(request, target_id, stage, run_id, parent, number)
        try:
            outcome = self.provider().send(request)
        except Exception as exc:  # adapter bug or provider unavailable
            outcome = Outcome("error", error=redact(f"{type(exc).__name__}: {exc}"))
        self._close_attempt(attempt_id, outcome, None)
        return attempt_id, outcome

    def _open_attempt(
        self,
        request: dict | None,
        target_id: str,
        stage: str,
        run_id: str | None,
        parent: str | None,
        number: int,
        extractor: str = "model",
    ) -> str:
        attempt_id = opaque_id("attempt", uuid.uuid4().hex)
        with self.conn.transaction():
            prepo.insert_attempt(
                self.conn,
                {
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "target_id": target_id,
                    "stage": stage,
                    "extractor": extractor,
                    "attempt_number": number,
                    "parent_attempt_id": parent,
                    "request_hash": digest(request) if request is not None else None,
                    "outcome": "outcome_unknown",  # until the response is committed
                },
            )
        self.conn.commit()
        return attempt_id

    def _close_attempt(self, attempt_id: str, outcome: Outcome, spent: float | None) -> None:
        retain = self.settings.privacy.retain_requests
        with self.conn.transaction():
            prepo.finish_attempt(
                self.conn,
                attempt_id,
                request_body=outcome.sent_body if retain else None,
                request_context=outcome.request_context,
                response_text=outcome.text if retain else None,
                response_meta=outcome.response_meta,
                outcome=outcome.status,
                usage=outcome.usage,
                cost_usd=spent,
                error=outcome.error,
            )
        self.conn.commit()

    # -- plan / execute ----------------------------------------------------------------------

    def _units(self, t: Target, c: dict) -> tuple[Profile, list[dict]]:
        profile = self.registry.profile(c["profile"])
        units = plan_units(t.artifact, t.table, c, profile, self.settings.privacy.digest)
        problems = coverage_problems(units, c, t.table["row_count"])
        if problems:  # a planner bug must never silently drop rows
            raise ValueError("; ".join(problems))
        return profile, units

    def _choose(
        self, opts: Options, c: dict, profile: Profile, page: dict
    ) -> tuple[str, str | None]:
        """(extractor, reason it cannot run or None)."""
        default = profile.get("extractor", {}).get("default", "rules")
        deterministic = c["method"] in ("template_rule", "explicit_profile")
        want = opts.extractor
        if want == "auto":
            want = "rules" if default == "rules" and deterministic else "model"
        if want == "model":
            if not profile.get("extractor", {}).get("allow_model"):
                return want, f"{profile.ref} does not allow model extraction"
            decision = self.egress(page)
            if not decision.allowed:
                return want, "EGRESS_DENIED: " + "; ".join(decision.reasons)
        return want, None

    def preview(self, t: Target, opts: Options, chunk: int) -> dict:
        """The exact unit, payload and request an extraction would use (no writes)."""
        stored = prepo.latest_classification(self.conn, t.table["table_id"])
        self.conn.rollback()
        if stored and stored["status"] == "routed" and not opts.profile:
            c, source = stored["result"], "persisted"
        else:
            c, source = self.classify(t, opts, None, persist=False), "deterministic_preview"
        if c["status"] != "routed":
            raise ConfexError(
                "PAYLOAD_NOT_READY",
                f"classification is {c['status']}: {c['reason']}",
                EXIT_UNAVAILABLE,
                {
                    "classification": {k: c.get(k) for k in ("status", "family", "candidates")},
                    "hint": "confex classify --table "
                    + t.table["table_id"]
                    + (" --extraction-profile PROFILE" if c["status"] == "ambiguous" else ""),
                },
            )
        profile, units = self._units(t, c)
        if not 1 <= chunk <= len(units):
            raise ins.not_found(f"chunk {chunk} does not exist (1..{len(units)})")
        unit = units[chunk - 1]
        evidence, redactions = pl.build_payload(
            t.artifact, t.table, unit, c, self.settings.privacy, t.page
        )
        request = pl.build_request(evidence, profile, self.model)
        persisted = prepo.get_unit(self.conn, unit["unit_id"])
        self.conn.rollback()
        extractor, blocked = self._choose(opts, c, profile, t.page)
        return {
            "preview_kind": "exact" if persisted else "prepared",
            "classification_source": source,
            "classification_id": c["classification_id"],
            "profile": profile.ref,
            "unit": unit,
            "extractor": extractor,
            "egress": {"allowed": blocked is None or extractor == "rules", "reason": blocked},
            "hashes": pl.hashes(evidence, request, unit, self.model),
            "redactions": sum(len(v) for v in redactions.values()),
            "estimated_input_tokens": pl.estimate_tokens(request),
            "estimate_note": "local estimate (characters / 3.5), not a provider count",
            "request": request,
        }

    def plan(self, targets: list[Target], opts: Options) -> dict:
        """What extraction would do. Reads the mirror only: no model call, no writes."""
        items, est_in, est_out, requests = [], 0, 0, 0
        for t in targets:
            item = {
                "table_id": t.table["table_id"],
                "page_id": t.artifact["page_id"],
                "representation": t.artifact["representation"],
            }
            if t.blocked:
                items.append({**item, "status": "blocked", "reason": t.blocked})
                continue
            c = self.classify(t, opts, None, persist=False)
            item.update(
                classification=c["status"],
                family=c["family"],
                profile=c["profile"],
                reason=c["reason"],
                candidates=c.get("candidates"),
            )
            if c["status"] != "routed":
                items.append({**item, "status": "skipped"})
                continue
            profile, units = self._units(t, c)
            extractor, blocked = self._choose(opts, c, profile, t.page)
            item.update(
                extractor=extractor,
                units=len(units),
                oversized=sum(1 for u in units if u["status"] == "oversized"),
            )
            if blocked:
                items.append({**item, "status": "blocked", "reason": blocked})
                continue
            if extractor == "model":
                for u in units:
                    ev, _ = pl.build_payload(
                        t.artifact, t.table, u, c, self.settings.privacy, t.page
                    )
                    est_in += pl.estimate_tokens(pl.build_request(ev, profile, self.model))
                    est_out += self.model["max_output_tokens"]
                    requests += 1
            items.append({**item, "status": "planned"})
        pricing = self.settings.pricing.get(self.model["model"])
        est_cost = cost_usd({"input_tokens": est_in, "output_tokens": est_out}, pricing)
        return {
            "items": items,
            "model_requests": requests,
            "estimated_input_tokens": est_in,
            "reserved_output_tokens": est_out,
            "estimated_cost_usd": est_cost,
            "cost_note": None if pricing else "unknown: no dated price configured for the model",
            "estimate_note": "local estimates; no provider token count was requested",
            "egress_policy": {
                "approved": self.settings.privacy.egress_approved,
                "approval_reference": self.settings.privacy.approval_reference or None,
            },
        }

    # -- template detection ---------------------------------------------------------------

    def detect_templates(
        self, *, space_key: str | None, root_id: int | None, opts: TemplateOptions
    ) -> dict:
        """Find parents whose children are all leaves; ask the model if they share a template.

        Candidates are found in the mirror. For each, the model proposes a template from a
        sample of the children and then checks every child against it. A dry run lists the
        candidates and estimated tokens, and calls nothing.
        """
        if (space_key is None) == (root_id is None):
            raise ConfexError(
                "ARGUMENT_CONFLICT", "give exactly one of --space or --root", EXIT_USAGE
            )
        space_id = ins._space(self.conn, space_key)["space_id"] if space_key else None
        if root_id is not None:
            ins._page(self.conn, root_id)
        candidates = prepo.template_candidates(
            self.conn, space_id=space_id, root_id=root_id, min_children=opts.min_children
        )
        self.conn.rollback()
        budget = Budget(
            {k: v for k, v in vars(self.settings.budgets).items() if k != "per_item_retries"},
            self.settings.pricing.get(self.model["model"]),
        )
        problems = budget.check_configuration()
        if problems:
            raise ConfexError("BUDGET_UNENFORCEABLE", "; ".join(problems), EXIT_USAGE)
        items = [self._detect_one(c, opts, budget) for c in candidates]
        counts: dict[str, int] = {}
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        return {
            "scope": {"space": space_key, "root": root_id, "min_children": opts.min_children},
            "dry_run": opts.dry_run,
            "candidates": len(candidates),
            "counts": counts,
            "items": items,
            "budget": budget.as_dict(),
        }

    def _detect_one(self, cand: dict, opts: TemplateOptions, budget: Budget) -> dict:
        parent = repo.get_page(self.conn, cand["page_id"])
        item: dict[str, Any] = {
            "parent_page_id": cand["page_id"],
            "title": cand["title"],
            "space_key": cand["space_key"],
            "children": len(cand["children"]),
        }
        summaries: dict[int, dict] = {}
        versions: dict[int, int] = {}
        for child in cand["children"]:
            if not child["has_content"]:
                continue
            try:
                resolved = ins.page_artifact(self.conn, child["page_id"], "auto")
            except ConfexError:
                continue
            summaries[child["page_id"]] = tpl.page_summary(
                resolved.artifact, child["title"], self.settings.privacy
            )
            versions[child["page_id"]] = child["version_number"]
        self.conn.rollback()
        ids = list(summaries)
        if len(ids) < opts.min_children:
            return {
                **item,
                "status": "skipped",
                "reason": f"only {len(ids)} child page(s) have stored bodies; sync the tree first",
            }
        sample = tpl.pick_sample(ids, opts.sample)
        inputs = {
            "detector": tpl.DETECTOR_VERSION,
            "prompt": tpl.TEMPLATE_PROMPT_VERSION,
            "model": self.model,
            "privacy": self.settings.privacy.digest,
            "parent": cand["page_id"],
            "children": [[i, versions[i]] for i in ids],
            "sample": sample,
        }
        input_digest = digest(inputs)
        detection_id = opaque_id("tmpl", input_digest + (uuid.uuid4().hex if opts.force else ""))
        stored = None if opts.force else prepo.get_template_detection(self.conn, detection_id)
        self.conn.rollback()
        if stored is not None:
            return {**item, **_detection_summary(stored), "reused": True}
        request = tpl.proposal_request(
            {
                "page_id": parent["page_id"],
                "title": tpl.clean_text(parent["title"], self.settings.privacy),
            },
            len(ids),
            [summaries[i] for i in sample],
            self.model,
        )
        est = pl.estimate_tokens(request)
        batches = [ids[i : i + tpl.CHECK_BATCH] for i in range(0, len(ids), tpl.CHECK_BATCH)]
        if opts.dry_run:
            check_est = sum(
                pl.estimate_tokens(tpl.check_request({}, [summaries[i] for i in b], self.model))
                for b in batches
            )
            return {
                **item,
                "status": "planned",
                "with_bodies": len(ids),
                "sample": sample,
                "requests": 1 + len(batches),
                "estimated_input_tokens": est + check_est,
                "estimate_note": "local estimate (characters / 3.5), excluding the template "
                "text the check requests will carry",
            }
        decision = self.egress(parent)
        if not decision.allowed:
            return {
                **item,
                "status": "blocked",
                "reason": "EGRESS_DENIED: " + "; ".join(decision.reasons),
            }
        row: dict[str, Any] = {
            "detection_id": detection_id,
            "parent_page_id": cand["page_id"],
            "space_key": cand["space_key"],
            "input_digest": input_digest,
            "detector_version": tpl.DETECTOR_VERSION,
            "prompt_version": tpl.TEMPLATE_PROMPT_VERSION,
            "model": self.model,
            "children": [{"page_id": i, "version_number": versions[i]} for i in ids],
            "sample": sample,
            "check_attempt_ids": [],
        }
        attempt, outcome = self._metered(request, detection_id, "template_propose", budget)
        if attempt is None:
            return {**item, "status": "budget_exhausted", "reason": budget.exhausted}
        row["propose_attempt_id"] = attempt
        if outcome.status != "succeeded":
            row.update(
                status="failed",
                issues=[f"proposal {outcome.status}: {outcome.error or ''}".strip()],
            )
            return {**item, **self._store_detection(row)}
        proposal, issues = tpl.clean_proposal(outcome.parsed, set(ids))
        pages: list[dict] = []
        if proposal.get("follows_template"):
            template = {k: proposal[k] for k in ("name", "description", "sections", "tables")}
            answers: list[dict | None] = []
            for batch in batches:
                check = tpl.check_request(template, [summaries[i] for i in batch], self.model)
                attempt, outcome = self._metered(check, detection_id, "template_check", budget)
                if attempt is not None:
                    row["check_attempt_ids"].append(attempt)
                if attempt is None or outcome.status != "succeeded":
                    reason = budget.exhausted if attempt is None else outcome.status
                    issues.append(f"check of {len(batch)} page(s) not done: {reason}")
                    answers.append(None)
                else:
                    answers.append(outcome.parsed)
            pages, check_issues = tpl.merge_checks(answers, batches)
            issues += check_issues
        result = tpl.verdict(proposal, pages)
        row.update(
            status=result["status"],
            conformance=result["conformance"],
            proposal=proposal,
            pages=pages,
            issues=issues,
        )
        return {**item, **self._store_detection(row)}

    def _metered(
        self, request: dict, target: str, stage: str, budget: Budget
    ) -> tuple[str | None, Outcome]:
        """One model call under the budget; (None, ...) when the budget refuses it."""
        est = pl.estimate_tokens(request)
        if budget.exhausted or not budget.reserve(est, request["max_tokens"]):
            return None, Outcome("budget_exhausted")
        attempt_id = self._open_attempt(request, target, stage, None, None, 1)
        try:
            outcome = self.provider().send(request)
        except Exception as exc:  # adapter bug or provider unavailable
            outcome = Outcome("error", error=redact(f"{type(exc).__name__}: {exc}"))
        self._close_attempt(attempt_id, outcome, budget.settle(est, request["max_tokens"], outcome))
        return attempt_id, outcome

    def _store_detection(self, row: dict) -> dict:
        with self.conn.transaction():
            prepo.insert_template_detection(self.conn, row)
        self.conn.commit()
        return _detection_summary(row)

    def classify_only(self, targets: list[Target], opts: Options) -> Report:
        run_id = self._start_run("classify", targets, opts, ["classify"])
        items = []
        for t in targets:
            if t.blocked:
                prepo.upsert_item(
                    self.conn,
                    run_id,
                    "classify",
                    t.table["table_id"],
                    "blocked",
                    detail={"reason": t.blocked},
                )
                items.append(
                    {"table_id": t.table["table_id"], "status": "blocked", "reason": t.blocked}
                )
                continue
            c = self.classify(t, opts, run_id, persist=True)
            status = (
                "succeeded"
                if c["status"] == "routed"
                else ("skipped" if c["status"] == "unsupported" else "blocked")
            )
            with self.conn.transaction():
                prepo.upsert_item(
                    self.conn,
                    run_id,
                    "classify",
                    t.table["table_id"],
                    status,
                    detail={"status": c["status"], "reason": c["reason"]},
                    output_ref=c["classification_id"],
                )
            self.conn.commit()
            items.append(
                {
                    "table_id": t.table["table_id"],
                    "classification_id": c["classification_id"],
                    "status": c["status"],
                    "family": c["family"],
                    "profile": c["profile"],
                    "reason": c["reason"],
                    "candidates": c.get("candidates"),
                }
            )
        return self._finish(run_id, items, Budget({}, None))

    def _start_run(
        self,
        command: str,
        targets: list[Target],
        opts: Options,
        stages: list[str],
        parent: str | None = None,
    ) -> str:
        run_id = opaque_id("run", uuid.uuid4().hex)
        manifest = [
            {
                "table_id": t.table["table_id"],
                "artifact_id": t.artifact["artifact_id"],
                "snapshot_id": t.artifact["snapshot_id"],
                "page_id": t.artifact["page_id"],
                "blocked": t.blocked,
            }
            for t in targets
        ]
        with self.conn.transaction():
            prepo.create_run(
                self.conn,
                {
                    "run_id": run_id,
                    "command": command,
                    "scope": {
                        "profile": opts.profile,
                        "extractor": opts.extractor,
                        "force": opts.force,
                    },
                    "requested_stages": stages,
                    "status": "running",
                    "config_digest": self.config_digest,
                    "versions": versions(self.registry),
                    "principal_scope": principal(),
                    "input_manifest": manifest,
                    "budget": vars(self.settings.budgets),
                    "parent_run_id": parent,
                },
            )
        self.conn.commit()
        return run_id

    def _finish(
        self, run_id: str, items: list[dict], budget: Budget, diagnostics: list | None = None
    ) -> Report:
        counts = prepo.stage_counts(self.conn, run_id)
        # The outcome is the outcome of the last stage that has items: extraction items for an
        # extract run, classification items when nothing reached extraction.
        final = next((counts[s] for s in ("extract", "classify") if s in counts), {})
        statuses = set(final)
        if not statuses or statuses <= {"succeeded", "skipped"}:
            status = "succeeded"
        elif "succeeded" in statuses:
            status = "partial"
        elif statuses <= {"blocked", "skipped"}:
            status = "blocked"
        else:
            status = "failed"
        if budget.exhausted and status in ("succeeded", "failed"):
            status = "partial"  # work remains; `confex runs resume` continues it
        diags = list(diagnostics or [])
        if budget.exhausted:
            diags.append(
                {
                    "code": "BUDGET_EXHAUSTED",
                    "severity": "warning",
                    "message": f"stopped scheduling: {budget.exhausted}",
                }
            )
        with self.conn.transaction():
            prepo.finish_run(
                self.conn,
                run_id,
                status,
                stage_counts=counts,
                usage=budget.as_dict(),
                diagnostics=diags,
            )
        self.conn.commit()
        return Report(run_id, status, counts, items, diags, budget.as_dict())

    def extract(
        self,
        targets: list[Target],
        opts: Options,
        *,
        command: str = "extract",
        parent_run: str | None = None,
        run_id: str | None = None,
        only_items: set[str] | None = None,
        execute: bool = True,
    ) -> Report:
        budget = Budget(
            {k: v for k, v in vars(self.settings.budgets).items() if k != "per_item_retries"},
            self.settings.pricing.get(self.model["model"]),
        )
        problems = budget.check_configuration()
        if problems:
            raise ConfexError("BUDGET_UNENFORCEABLE", "; ".join(problems), EXIT_USAGE)
        done_in_run: set[str] = set()
        if run_id is None:
            run_id = self._start_run(
                command, targets, opts, ["classify", "plan", "extract", "validate"], parent_run
            )
        else:  # resume: keep what this run already completed
            done_in_run = {
                i["item_id"] for i in prepo.run_items(self.conn, run_id, "succeeded", "extract")
            }
            with self.conn.transaction():
                prepo.update_run(self.conn, run_id, status="running")
            self.conn.commit()
        items: list[dict] = []
        jobs: list[tuple[Target, dict, Profile, dict, str]] = []
        for t in targets:
            tid = t.table["table_id"]
            if t.blocked:
                self._item(run_id, "classify", tid, "blocked", {"reason": t.blocked})
                items.append({"table_id": tid, "status": "blocked", "reason": t.blocked})
                continue
            c = self.classify(t, opts, run_id, persist=True)
            if c["status"] != "routed":
                status = "skipped" if c["status"] == "unsupported" else "blocked"
                self._item(
                    run_id,
                    "classify",
                    tid,
                    status,
                    {"status": c["status"], "reason": c["reason"]},
                    c["classification_id"],
                )
                items.append(
                    {
                        "table_id": tid,
                        "status": status,
                        "classification": c["status"],
                        "reason": c["reason"],
                        "candidates": c.get("candidates"),
                    }
                )
                continue
            self._item(
                run_id,
                "classify",
                tid,
                "succeeded",
                {"profile": c["profile"]},
                c["classification_id"],
            )
            profile, units = self._units(t, c)
            extractor, blocked = self._choose(opts, c, profile, t.page)
            for u in units:
                evidence, redactions = pl.build_payload(
                    t.artifact, t.table, u, c, self.settings.privacy, t.page
                )
                with self.conn.transaction():
                    prepo.insert_unit(
                        self.conn,
                        {
                            "unit_id": u["unit_id"],
                            "table_id": tid,
                            "artifact_id": t.artifact["artifact_id"],
                            "classification_id": c["classification_id"],
                            "page_id": t.artifact["page_id"],
                            "version_number": t.artifact["version_number"],
                            "profile": profile.ref,
                            "schema_ref": profile.schema.ref,
                            "chunk_index": u["chunk_index"],
                            "chunk_count": u["chunk_count"],
                            "status": u["status"],
                            "manifest": u,
                            "payload": evidence,
                            "payload_hash": digest(evidence),
                            "redaction_map": redactions,
                        },
                    )
                self.conn.commit()
                self._item(run_id, "plan", u["unit_id"], "succeeded", {"table_id": tid})
                if only_items is not None and u["unit_id"] not in only_items:
                    continue
                if u["unit_id"] in done_in_run:
                    items.append({"unit_id": u["unit_id"], "status": "done_in_run"})
                    continue
                if u["status"] == "oversized":
                    self._item(
                        run_id,
                        "extract",
                        u["unit_id"],
                        "blocked",
                        {"reason": "record group exceeds the chunk limit"},
                    )
                    items.append(
                        {"unit_id": u["unit_id"], "status": "blocked", "reason": "oversized"}
                    )
                    continue
                if blocked:
                    self._item(run_id, "extract", u["unit_id"], "blocked", {"reason": blocked})
                    items.append({"unit_id": u["unit_id"], "status": "blocked", "reason": blocked})
                    continue
                if not opts.force:
                    reused = prepo.succeeded_attempt_for_unit(self.conn, u["unit_id"])
                    self.conn.rollback()
                    if reused:
                        self._item(
                            run_id,
                            "extract",
                            u["unit_id"],
                            "succeeded",
                            {"reused_attempt": reused["attempt_id"]},
                            reused["attempt_id"],
                        )
                        items.append(
                            {
                                "unit_id": u["unit_id"],
                                "status": "reused",
                                "attempt_id": reused["attempt_id"],
                            }
                        )
                        continue
                self._item(run_id, "extract", u["unit_id"], "pending", {"extractor": extractor})
                jobs.append((t, u, profile, c, extractor))

        if not execute:  # --persist-plan: classifications and units only
            with self.conn.transaction():
                prepo.finish_run(
                    self.conn, run_id, "planned", stage_counts=prepo.stage_counts(self.conn, run_id)
                )
            self.conn.commit()
            return Report(run_id, "planned", prepo.stage_counts(self.conn, run_id), items)
        new_revisions = self._execute(run_id, jobs, budget, items)
        self._validate(new_revisions)
        return self._finish(run_id, items, budget)

    def _item(self, run_id, stage, item_id, status, detail=None, output_ref=None, attempts=0):
        with self.conn.transaction():
            prepo.upsert_item(
                self.conn,
                run_id,
                stage,
                item_id,
                status,
                detail=detail or {},
                output_ref=output_ref,
                attempts=attempts,
            )
        self.conn.commit()

    def _claim(self, run_id: str, unit_id: str) -> bool:
        with self.conn.transaction():
            ok = prepo.claim_item(self.conn, run_id, "extract", unit_id, self.owner, LEASE_SECONDS)
        self.conn.commit()
        return ok

    def _execute(self, run_id: str, jobs: list, budget: Budget, items: list) -> list[str]:
        new_revisions: list[str] = []
        model_jobs = []
        for t, u, profile, c, extractor in jobs:
            if not self._claim(run_id, u["unit_id"]):
                items.append(
                    {
                        "unit_id": u["unit_id"],
                        "status": "skipped",
                        "reason": "leased by another worker",
                    }
                )
                continue
            if extractor == "rules":
                attempt_id = self._open_attempt(
                    None, u["unit_id"], "extract", run_id, None, 1, extractor="rules"
                )
                result = RuleExtractor(t.artifact, t.table, u, c, profile).run()
                outcome = Outcome(
                    "succeeded", parsed=result, response_meta={"extractor": RULES_VERSION}
                )
                new_revisions += self._commit_result(
                    run_id, t, u, profile, c, attempt_id, outcome, None, "rules", items
                )
            else:
                model_jobs.append((t, u, profile, c))
        if model_jobs:
            new_revisions += self._execute_model(run_id, model_jobs, budget, items)
        return new_revisions

    def _execute_model(self, run_id: str, jobs: list, budget: Budget, items: list) -> list[str]:
        new_revisions: list[str] = []
        workers = max(1, self.settings.model.concurrency)
        provider = self.provider()
        pending: list[tuple[Future, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for t, u, profile, c in jobs:
                evidence, redactions = pl.build_payload(
                    t.artifact, t.table, u, c, self.settings.privacy, t.page
                )
                request = pl.build_request(evidence, profile, self.model)
                est = pl.estimate_tokens(request)
                if budget.exhausted or not budget.reserve(est, request["max_tokens"]):
                    self._item(
                        run_id,
                        "extract",
                        u["unit_id"],
                        "pending",
                        {"reason": "budget exhausted; resume to continue"},
                    )
                    items.append(
                        {
                            "unit_id": u["unit_id"],
                            "status": "not_scheduled",
                            "reason": budget.exhausted,
                        }
                    )
                    continue
                attempt_id = self._open_attempt(request, u["unit_id"], "extract", run_id, None, 1)
                fut = pool.submit(provider.send, request)
                pending.append((fut, (t, u, profile, c, attempt_id, request, est, redactions, 1)))
            # Results are committed on this thread only; workers never touch the database.
            while pending:
                fut, job = pending.pop(0)
                t, u, profile, c, attempt_id, request, est, redactions, number = job
                try:
                    outcome = fut.result()
                except Exception as exc:
                    outcome = Outcome("error", error=redact(f"{type(exc).__name__}: {exc}"))
                spent = budget.settle(est, request["max_tokens"], outcome)
                self._close_attempt(attempt_id, outcome, spent)
                retry = self._retry_request(outcome, request, number)
                if outcome.status == "succeeded":
                    revs = self._commit_result(
                        run_id,
                        t,
                        u,
                        profile,
                        c,
                        attempt_id,
                        outcome,
                        redactions,
                        "model",
                        items,
                        number=number,
                    )
                    if revs is None and number < 1 + self.settings.budgets.per_item_retries:
                        retry = self._repair_request(request, outcome, t, u, profile, redactions)
                    else:
                        new_revisions += revs or []
                        continue
                if retry is not None and number < 3:
                    est2 = pl.estimate_tokens(retry)
                    if budget.reserve(est2, retry["max_tokens"]):
                        stage = "repair" if outcome.status == "succeeded" else "extract"
                        a2 = self._open_attempt(
                            retry, u["unit_id"], stage, run_id, attempt_id, number + 1
                        )
                        pending.append(
                            (
                                pool.submit(provider.send, retry),
                                (t, u, profile, c, a2, retry, est2, redactions, number + 1),
                            )
                        )
                        continue
                self._item(
                    run_id,
                    "extract",
                    u["unit_id"],
                    "failed",
                    {"outcome": outcome.status, "error": outcome.error},
                    attempt_id,
                    attempts=number,
                )
                items.append(
                    {"unit_id": u["unit_id"], "status": "failed", "outcome": outcome.status}
                )
        return new_revisions

    def _retry_request(self, outcome: Outcome, request: dict, number: int) -> dict | None:
        """One bounded retry with a larger output budget after truncation; nothing else."""
        if outcome.status == "truncated" and number == 1:
            bigger = min(request["max_tokens"] * 2, MAX_OUTPUT_TOKENS)
            if bigger > request["max_tokens"]:
                return {**request, "max_tokens": bigger}
        return None

    def _check(
        self,
        t: Target,
        u: dict,
        profile: Profile,
        outcome: Outcome,
        redactions,
        origin: str,
        attempt_id: str,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """(result-level issues, candidates, all problems incl. evidence) for one result."""
        unit_issues = val.validate_result(outcome.parsed, u, profile)
        candidates = build_candidates(
            outcome.parsed,
            artifact=t.artifact,
            table=t.table,
            unit=u,
            profile=profile,
            origin=origin,
            attempt_id=attempt_id,
            redaction_map=redactions,
        )
        return (
            unit_issues,
            candidates,
            unit_issues + val.evidence_problems(candidates, t.artifact, t.table, u),
        )

    def _repair_request(
        self,
        request: dict,
        outcome: Outcome,
        t: Target,
        u: dict,
        profile: Profile,
        redactions=None,
    ) -> dict:
        _, _, problems = self._check(t, u, profile, outcome, redactions, "model", "repair")
        lines = "\n".join(f"- {p['code']}: {p['message']}" for p in problems[:30])
        repair = (
            "Your previous result (below) failed deterministic validation:\n"
            f"{lines}\n\nPrevious result:\n{outcome.text}\n\n"
            "Return a complete corrected result for the same evidence. Copy quotes exactly; "
            "do not add values the evidence does not contain."
        )
        return {**request, "messages": [*request["messages"], {"role": "user", "content": repair}]}

    def _commit_result(
        self,
        run_id,
        t: Target,
        u: dict,
        profile: Profile,
        c: dict,
        attempt_id: str,
        outcome: Outcome,
        redactions,
        origin: str,
        items: list,
        number: int = 1,
    ) -> list[str] | None:
        """Persist candidate revisions; None if the result fails repairable checks."""
        result = outcome.parsed
        unit_issues, candidates, problems = self._check(
            t, u, profile, outcome, redactions, origin, attempt_id
        )
        repairable = any(i["code"] in val.REPAIRABLE for i in problems)
        if origin == "model" and repairable and number < 1 + self.settings.budgets.per_item_retries:
            return None
        revision_ids = []
        with self.conn.transaction():
            for cand in candidates:
                last_n, last_id = prepo.latest_revision_number(self.conn, cand["record_key"])
                record = {
                    k: cand[k] for k in ("fields", "relationships", "source_rows", "local_id")
                }
                record["unit_issues"] = unit_issues
                row_cells = {
                    x["cell_id"]
                    for x in t.table["cells"]
                    if x.get("origin")
                    and any(
                        x["origin"]["row"] <= r < x["origin"]["row"] + x["rowspan"]
                        for r in cand["source_rows"]
                    )
                }
                record["ambiguities"] = [
                    a
                    for a in result.get("ambiguities", [])
                    if not a.get("cell_ids") or set(a["cell_ids"]) & row_cells
                ]
                prepo.insert_revision(
                    self.conn,
                    {
                        "revision_id": cand["revision_id"],
                        "record_key": cand["record_key"],
                        "parent_revision_id": last_id,
                        "revision_number": last_n + 1,
                        "origin": origin,
                        "unit_id": u["unit_id"],
                        "attempt_id": attempt_id,
                        "run_id": run_id,
                        "page_id": t.artifact["page_id"],
                        "version_number": t.artifact["version_number"],
                        "space_id": t.page.get("space_id"),
                        "table_id": t.table["table_id"],
                        "artifact_id": t.artifact["artifact_id"],
                        "snapshot_id": t.artifact["snapshot_id"],
                        "schema_ref": cand["schema"],
                        "profile": cand["profile"],
                        "record_kind": cand["record_kind"],
                        "record": record,
                        "content_hash": cand["content_hash"],
                        "dependency_hash": digest(
                            {
                                "unit": u["dependency_hash"],
                                "attempt": attempt_id,
                                "classification": c["classification_id"],
                            }
                        ),
                    },
                )
                revision_ids.append(cand["revision_id"])
            prepo.upsert_item(
                self.conn,
                run_id,
                "extract",
                u["unit_id"],
                "succeeded",
                detail={"revisions": len(revision_ids), "unit_issues": len(unit_issues)},
                output_ref=attempt_id,
                attempts=number,
            )
            if origin == "rules":
                prepo.finish_attempt(
                    self.conn,
                    attempt_id,
                    outcome="succeeded",
                    response_meta=outcome.response_meta,
                    usage={},
                )
        self.conn.commit()
        items.append(
            {
                "unit_id": u["unit_id"],
                "status": "succeeded",
                "revisions": len(revision_ids),
                "extractor": origin,
            }
        )
        return revision_ids

    # -- validation ------------------------------------------------------------------------

    def _validate(self, revision_ids: list[str]) -> list[dict]:
        revs = [prepo.get_revision(self.conn, r) for r in revision_ids]
        self.conn.rollback()
        return self.validate_revisions([r for r in revs if r])

    def validate_revisions(self, revs: list[dict]) -> list[dict]:
        profile_of = {r["revision_id"]: self.registry.profile(r["profile"]) for r in revs}
        cross, proposals = val.cross_record(revs, profile_of)
        out = []
        artifacts: dict[str, dict] = {}
        for r in revs:
            if r["artifact_id"] not in artifacts:
                artifacts[r["artifact_id"]] = repo.load_artifact(self.conn, r["artifact_id"])
            artifact = artifacts[r["artifact_id"]]
            table = next(t for t in artifact["tables"] if t["table_id"] == r["table_id"])
            unit = prepo.get_unit(self.conn, r["unit_id"]) if r["unit_id"] else None
            result = val.validate_revision(
                r,
                artifact=artifact,
                table=table,
                unit=unit["manifest"] if unit else None,
                profile=profile_of[r["revision_id"]],
                unit_issues=r["record"].get("unit_issues", []) + cross.get(r["revision_id"], []),
                current_version=r.get("current_version"),
            )
            out.append(result)
        self.conn.rollback()
        with self.conn.transaction():
            for result in out:
                prepo.insert_validation(
                    self.conn,
                    {
                        "validation_id": result["validation_id"],
                        "revision_id": result["revision_id"],
                        "validator_version": result["validator_version"],
                        "status": result["status"],
                        "issues": result["issues"],
                        "metrics": result["metrics"],
                    },
                )
            for p in proposals:
                prepo.insert_match_proposal(self.conn, p)
        self.conn.commit()
        return out


def _detection_summary(row: dict) -> dict:
    proposal = row.get("proposal") or {}
    pages = row.get("pages") or []
    return {
        "detection_id": row["detection_id"],
        "status": row["status"],
        "conformance": float(row["conformance"]) if row.get("conformance") is not None else None,
        "template_name": proposal.get("name"),
        "confidence": proposal.get("confidence"),
        "deviating": [p["page_id"] for p in pages if p["state"] == "deviates"],
        "unchecked": [p["page_id"] for p in pages if p["state"] == "unchecked"],
        "issues": len(row.get("issues") or []),
    }


def load_registry_for(settings: ConfexSettings) -> Registry:
    from confluence_mirror.extraction.registry import load_registry

    try:
        return load_registry(settings.extraction.registry_dirs)
    except RegistryError as exc:
        raise ConfexError("REGISTRY_INVALID", str(exc), EXIT_USAGE) from None
