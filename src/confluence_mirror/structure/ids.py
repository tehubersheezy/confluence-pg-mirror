"""Deterministic opaque identifiers and canonical hashing.

Identities are derived from canonical JSON of their inputs plus a versioned recipe, so
re-parsing the same capture with the same parser yields the same IDs, and inspecting a
table never mints new random ones. Consumers must treat the suffixes as opaque.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ID_RECIPE = "ids.v1"
# The mirror holds one Confluence instance today; IDs still carry the namespace so that a
# second instance can be added without colliding identities.
DEFAULT_INSTANCE_ID = "default"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(canonical_json(value))


def opaque_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{digest([ID_RECIPE, prefix, *parts])[:24]}"


def snapshot_id(
    instance_id: str, page_id: int, version: int, representation: str, body_sha256: str
) -> str:
    return opaque_id("snap", instance_id, str(page_id), version, representation, body_sha256)
