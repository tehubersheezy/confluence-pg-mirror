"""Model egress policy and redaction (pure).

No source text goes to a model unless every gate passes: an organizational approval
recorded in configuration, an approved endpoint, a permitted source scope and a retention
policy. A CLI flag cannot override a denial. Redaction runs before token estimation and
before transmission, replaces matches with stable placeholders, and keeps a local span
mapping so evidence cited against redacted text can be mapped back, or marked
unresolvable when it touches a redacted span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from confluence_mirror.structure.ids import digest

DEFAULT_REDACTIONS: tuple[tuple[str, str], ...] = (
    # The value after a credential-looking key, e.g. "password: hunter2".
    (
        "credential",
        r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*(\S+)",
    ),
    ("aws_access_key", r"\b(AKIA[0-9A-Z]{16})\b"),
    (
        "private_key",
        r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)",
    ),
    ("bearer_token", r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"),
)


@dataclass(frozen=True)
class PrivacyPolicy:
    name: str = "default"
    egress_approved: bool = False
    approval_reference: str = ""
    allowed_spaces: tuple[str, ...] = ()
    allowed_endpoints: tuple[str, ...] = ("https://api.anthropic.com",)
    retain_requests: bool = True
    reduced_audit: bool = False  # allow extraction without retained requests (not full v1 lineage)
    redactions: tuple[tuple[str, str], ...] = DEFAULT_REDACTIONS
    extra_redactions: tuple[tuple[str, str], ...] = ()

    @property
    def rules(self) -> tuple[tuple[str, str], ...]:
        return self.redactions + self.extra_redactions

    @property
    def digest(self) -> str:
        return digest(
            {
                "name": self.name,
                "rules": [list(r) for r in self.rules],
                "retain": self.retain_requests,
            }
        )[:16]


@dataclass
class EgressDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def egress_decision(policy: PrivacyPolicy, endpoint: str, space_key: str | None) -> EgressDecision:
    reasons = []
    if not policy.egress_approved:
        reasons.append("model egress is not approved ([privacy] egress_approved = false)")
    if not policy.approval_reference.strip():
        reasons.append("no organizational approval reference is recorded")
    origin = _origin(endpoint)
    if origin not in {_origin(e) for e in policy.allowed_endpoints}:
        reasons.append(f"endpoint {origin} is not in [privacy] allowed_endpoints")
    if policy.allowed_spaces and space_key not in policy.allowed_spaces:
        reasons.append(f"space {space_key} is not in [privacy] allowed_spaces")
    if not policy.retain_requests and not policy.reduced_audit:
        reasons.append(
            "request retention is off; enable retain_requests or explicitly opt into reduced_audit"
        )
    return EgressDecision(not reasons, reasons)


def _origin(url: str) -> str:
    parts = urlsplit(url.strip().rstrip("/"))
    return f"{parts.scheme}://{parts.netloc}".lower()


def redact(
    text: str, rules: tuple[tuple[str, str], ...], counter: dict[str, int]
) -> tuple[str, list[dict[str, Any]]]:
    """Redact the capture group (or whole match) of each rule.

    Returns ``(redacted_text, mappings)``; each mapping records the placeholder, its span in
    the redacted text and the original span. ``counter`` numbers placeholders per rule, so
    they are stable within one payload.
    """
    spans: list[tuple[int, int, str]] = []
    for name, pattern in rules:
        for m in re.finditer(pattern, text):
            start, end = (m.start(1), m.end(1)) if m.groups() else (m.start(), m.end())
            if any(not (end <= s or start >= e) for s, e, _ in spans):
                continue
            spans.append((start, end, name))
    spans.sort()
    out, mappings, pos, shift = [], [], 0, 0
    for start, end, name in spans:
        counter[name] = counter.get(name, 0) + 1
        placeholder = f"[REDACTED:{name}:{counter[name]}]"
        out.append(text[pos:start])
        r_start = start + shift
        out.append(placeholder)
        mappings.append(
            {
                "placeholder": placeholder,
                "rule": name,
                "redacted_start": r_start,
                "redacted_end": r_start + len(placeholder),
                "original_start": start,
                "original_end": end,
            }
        )
        shift += len(placeholder) - (end - start)
        pos = end
    out.append(text[pos:])
    return "".join(out), mappings


def map_back(start: int, end: int, mappings: list[dict]) -> tuple[int, int] | None:
    """Translate a span of redacted text to the original; None if it touches a redaction."""
    shift = 0
    for m in mappings:
        if end <= m["redacted_start"]:
            break
        if start < m["redacted_end"]:
            return None
        shift += (m["original_end"] - m["original_start"]) - (
            m["redacted_end"] - m["redacted_start"]
        )
    return start + shift, end + shift
