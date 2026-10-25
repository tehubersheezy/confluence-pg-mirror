"""Deterministic value normalization and list splitting (pure; no lookups, no network).

Every normalizer is named, so a field assertion records exactly which rule produced its
value from its quoted span, and validation can re-run the rule to check it.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

NORMALIZER_VERSION = "norm.1"

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9\-_]{0,62})(?:\.[A-Za-z0-9\-_]{1,63})*$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_RE = re.compile(r"^([A-Za-z_\-]*?)(\d+)\s*[-–]\s*\1?(\d+)$")


@dataclass(frozen=True)
class Normalized:
    value: str | int | None
    rule: str | None
    state: str  # present | unparsed | ambiguous
    issue: str | None = None


def normalize(
    field_type: str,
    text: str,
    *,
    values: list[str] | None = None,
    value_map: dict[str, str] | None = None,
) -> Normalized:
    raw = " ".join(text.split())
    if field_type == "string":
        rule = "string.collapse_whitespace" if raw != text else None
        return Normalized(raw, rule, "present")
    if field_type == "hostname":
        if not _HOST_RE.match(raw):
            return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")
        return Normalized(
            raw.lower(), "hostname.lowercase" if raw != raw.lower() else None, "present"
        )
    if field_type == "ip_address":
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")
        out = addr.compressed
        return Normalized(out, "ip.compressed" if out != raw else None, "present")
    if field_type in ("port", "integer"):
        if not re.fullmatch(r"\d+", raw):
            return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")
        n = int(raw)
        if field_type == "port" and not 1 <= n <= 65535:
            return Normalized(raw, None, "unparsed", "VALUE_OUT_OF_RANGE")
        return Normalized(n, f"{field_type}.parse", "present")
    if field_type == "date":
        if _ISO_DATE_RE.match(raw):
            return Normalized(raw, None, "present")
        # Locale-dependent forms (03/04/2026) stay ambiguous rather than guessed.
        return Normalized(raw, None, "ambiguous", "DATE_FORMAT_AMBIGUOUS")
    if field_type == "email":
        if not _EMAIL_RE.match(raw):
            return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")
        return Normalized(raw.lower(), "email.lowercase" if raw != raw.lower() else None, "present")
    if field_type == "url":
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")
        return Normalized(raw, None, "present")
    if field_type == "enum":
        key = raw.lower()
        if value_map and key in value_map:
            return Normalized(value_map[key], "enum.value_map", "present")
        for v in values or []:
            if v.lower() == key:
                return Normalized(v, "enum.casefold" if v != raw else None, "present")
        return Normalized(raw, None, "unparsed", "ENUM_VALUE_UNKNOWN")
    return Normalized(raw, None, "unparsed", "NORMALIZATION_UNSUPPORTED")


@dataclass(frozen=True)
class Member:
    text: str
    start: int  # code-point offsets into the source evidence text, half-open
    end: int


def split_list(text: str, separators: list[str]) -> tuple[list[Member], list[str]]:
    """Split a cell's evidence text into ordered members with exact offsets.

    Returns ``(members, warnings)``. Ranges like ``db01-db10`` are never expanded; they are
    returned whole with a ``RANGE_NOT_EXPANDED`` warning.
    """
    if not separators:
        stripped = text.strip()
        start = text.find(stripped) if stripped else 0
        return ([Member(stripped, start, start + len(stripped))] if stripped else []), []
    pattern = "|".join(re.escape(s) if s != " " else r"\s+" for s in separators)
    members: list[Member] = []
    warnings: list[str] = []
    pos = 0
    for m in re.finditer(pattern, text):
        members.append(_member(text, pos, m.start()))
        pos = m.end()
    members.append(_member(text, pos, len(text)))
    members = [m for m in members if m.text]
    for m in members:
        if _RANGE_RE.match(m.text):
            warnings.append("RANGE_NOT_EXPANDED")
    return members, warnings


def _member(text: str, start: int, end: int) -> Member:
    chunk = text[start:end]
    lead = len(chunk) - len(chunk.lstrip())
    stripped = chunk.strip()
    return Member(stripped, start + lead, start + lead + len(stripped))
