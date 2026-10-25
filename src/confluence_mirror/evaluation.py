"""Offline evaluation against hand-labelled synthetic tables.

A dataset is JSON: ``{"dataset", "split", "cases": [{"case_id", "family", "profile",
"body", "expected": {"records": [...], "ambiguities": [...], "classification"?}}]}``.
Each case is parsed, classified, planned and extracted with the production code; the
report measures record precision/recall, field accuracy, evidence validity, row coverage
and ambiguity detection, by family and overall. Results are measured, never assumed: a
report states the dataset split it ran on.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from confluence_mirror.extraction import validation as val
from confluence_mirror.extraction.candidates import build_candidates
from confluence_mirror.extraction.classify import classify_table
from confluence_mirror.extraction.payload import build_payload, build_request
from confluence_mirror.extraction.privacy import PrivacyPolicy
from confluence_mirror.extraction.registry import Registry
from confluence_mirror.extraction.rules import RuleExtractor
from confluence_mirror.extraction.units import plan_units
from confluence_mirror.structure import parse_document

REPORT_SCHEMA = "confex.eval.v1"
TARGETS = {"record_precision": 0.98, "record_recall": 0.95}


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _predicted(candidates: list[dict]) -> list[dict]:
    out = []
    for c in candidates:
        values = {f["field"]: f["value"] for f in c["fields"] if f["value_state"] == "present"}
        out.append({"values": values, "fields": c["fields"]})
    return out


def run_case(
    case: dict,
    registry: Registry,
    *,
    extractor: str,
    provider=None,
    model: dict | None = None,
    policy: PrivacyPolicy | None = None,
) -> dict:
    policy = policy or PrivacyPolicy()
    artifact = parse_document(case["body"], page_id=0, version=1, representation="export_view")
    table = artifact["tables"][case.get("table_ordinal", 1) - 1]
    c = classify_table(table, artifact, registry, explicit_profile=case.get("profile"))
    expected = case["expected"]
    out: dict[str, Any] = {
        "case_id": case["case_id"],
        "family": case["family"],
        "classification": c["status"],
    }
    if expected.get("classification") and expected["classification"] != c["status"]:
        out["classification_error"] = f"expected {expected['classification']}, got {c['status']}"
    candidates, coverage_rows, selected, ambiguity_codes = [], 0, 0, set()
    usage = {"input_tokens": 0, "output_tokens": 0}
    if c["status"] == "routed":
        profile = registry.profile(c["profile"])
        for u in plan_units(artifact, table, c, profile, policy.digest):
            if extractor == "model":
                ev, redactions = build_payload(artifact, table, u, c, policy, {"title": "eval"})
                outcome = provider.send(build_request(ev, profile, model))
                for k in usage:
                    usage[k] += outcome.usage.get(k, 0)
                if outcome.status != "succeeded":
                    out.setdefault("errors", []).append(outcome.status)
                    continue
                result = outcome.parsed
            else:
                redactions = {}
                result = RuleExtractor(artifact, table, u, c, profile).run()
            selected += len(u["selected_rows"])
            coverage_rows += len(
                {x["row"] for x in result.get("row_coverage", [])} & set(u["selected_rows"])
            )
            ambiguity_codes |= {a["code"] for a in result.get("ambiguities", [])}
            cands = build_candidates(
                result,
                artifact=artifact,
                table=table,
                unit=u,
                profile=profile,
                origin=extractor,
                attempt_id="eval",
                redaction_map=redactions,
            )
            for cand in cands:
                issues = val.evidence_problems([cand], artifact, table, u)
                cand["_evidence_ok"] = not issues
            candidates += cands
        identity = profile.schema.data.get("identity_fields", [])[:1]
    else:
        identity = []
    pairs = [
        (p, c)
        for p, c in zip(_predicted(candidates), candidates, strict=True)
        if all(k in p["values"] for k in identity)
    ]
    predicted = [p for p, _ in pairs]
    expected_records = expected["records"]
    matched, field_total, field_ok = 0, 0, 0
    used = set()
    for exp in expected_records:
        key = {k: exp[k] for k in identity if k in exp}
        hit = next(
            (
                i
                for i, p in enumerate(predicted)
                if i not in used and all(_eq(p["values"].get(k), v) for k, v in key.items())
            ),
            None,
        )
        if hit is None:
            continue
        used.add(hit)
        matched += 1
        for k, v in exp.items():
            field_total += 1
            field_ok += _eq(predicted[hit]["values"].get(k), v)
    populated = sum(len(p["values"]) for p in predicted)
    evidence_ok = sum(len(p["values"]) for p, c in pairs if c.get("_evidence_ok"))
    expected_amb = set(expected.get("ambiguities", []))
    out.update(
        expected_records=len(expected_records),
        predicted_records=len(predicted),
        matched_records=matched,
        fields_checked=field_total,
        fields_correct=field_ok,
        populated_fields=populated,
        evidence_valid_fields=evidence_ok,
        selected_rows=selected,
        covered_rows=coverage_rows,
        expected_ambiguities=sorted(expected_amb),
        detected_ambiguities=sorted(ambiguity_codes & expected_amb),
        unexpected_ambiguities=sorted(ambiguity_codes - expected_amb),
        usage=usage,
    )
    return out


def _eq(a: Any, b: Any) -> bool:
    if isinstance(b, list):
        return isinstance(a, list) and [str(x).lower() for x in a] == [str(x).lower() for x in b]
    return a is not None and str(a).lower() == str(b).lower()


def _aggregate(cases: list[dict]) -> dict:
    s = defaultdict(int)
    for c in cases:
        for k in (
            "expected_records",
            "predicted_records",
            "matched_records",
            "fields_checked",
            "fields_correct",
            "populated_fields",
            "evidence_valid_fields",
            "selected_rows",
            "covered_rows",
        ):
            s[k] += c[k]
        s["expected_ambiguities"] += len(c["expected_ambiguities"])
        s["detected_ambiguities"] += len(c["detected_ambiguities"])
        s["classification_errors"] += 1 if c.get("classification_error") else 0
    return {
        "cases": len(cases),
        "record_precision": _ratio(s["matched_records"], s["predicted_records"]),
        "record_recall": _ratio(s["matched_records"], s["expected_records"]),
        "field_accuracy": _ratio(s["fields_correct"], s["fields_checked"]),
        "evidence_validity": _ratio(s["evidence_valid_fields"], s["populated_fields"]),
        "row_coverage": _ratio(s["covered_rows"], s["selected_rows"]),
        "ambiguity_recall": _ratio(s["detected_ambiguities"], s["expected_ambiguities"]),
        "classification_errors": s["classification_errors"],
    }


def run_dataset(
    dataset: dict,
    registry: Registry,
    *,
    extractor: str = "rules",
    provider=None,
    model: dict | None = None,
    policy: PrivacyPolicy | None = None,
    versions: dict | None = None,
) -> dict:
    cases = [
        run_case(c, registry, extractor=extractor, provider=provider, model=model, policy=policy)
        for c in dataset["cases"]
    ]
    by_family = defaultdict(list)
    for c in cases:
        by_family[c["family"]].append(c)
    overall = _aggregate(cases)
    meets = (
        all(overall.get(k) is not None and overall[k] >= v for k, v in TARGETS.items())
        and overall["classification_errors"] == 0
    )
    return {
        "schema_version": REPORT_SCHEMA,
        "dataset": dataset.get("dataset"),
        "split": dataset.get("split", "unspecified"),
        "extractor": extractor,
        "model": (model or {}).get("model") if extractor == "model" else None,
        "versions": versions or {},
        "created_at": datetime.now(UTC).isoformat(),
        "overall": overall,
        "by_family": {k: _aggregate(v) for k, v in sorted(by_family.items())},
        "cases": cases,
        "targets": TARGETS,
        "meets_targets": meets,
        "note": "measured on the stated split; a development split is not a held-out "
        "evaluation and does not establish production quality",
    }


def compare(a: dict, b: dict) -> dict:
    def delta(x: dict, y: dict) -> dict:
        return {
            k: (None if x.get(k) is None or y.get(k) is None else round(y[k] - x[k], 4))
            for k in (
                "record_precision",
                "record_recall",
                "field_accuracy",
                "evidence_validity",
                "row_coverage",
                "ambiguity_recall",
            )
        }

    ca = {c["case_id"]: c for c in a["cases"]}
    changed = []
    for c in b["cases"]:
        prev = ca.get(c["case_id"])
        if prev is None:
            changed.append({"case_id": c["case_id"], "change": "new"})
        elif (prev["matched_records"], prev["predicted_records"], prev["fields_correct"]) != (
            c["matched_records"],
            c["predicted_records"],
            c["fields_correct"],
        ):
            changed.append(
                {
                    "case_id": c["case_id"],
                    "change": "metrics",
                    "before": {
                        k: prev[k]
                        for k in ("matched_records", "predicted_records", "fields_correct")
                    },
                    "after": {
                        k: c[k] for k in ("matched_records", "predicted_records", "fields_correct")
                    },
                }
            )
    return {
        "baseline": {
            "dataset": a["dataset"],
            "extractor": a["extractor"],
            "created_at": a["created_at"],
        },
        "candidate": {
            "dataset": b["dataset"],
            "extractor": b["extractor"],
            "created_at": b["created_at"],
        },
        "overall_delta": delta(a["overall"], b["overall"]),
        "by_family_delta": {
            fam: delta(a["by_family"].get(fam, {}), m) for fam, m in b["by_family"].items()
        },
        "changed_cases": changed,
    }
