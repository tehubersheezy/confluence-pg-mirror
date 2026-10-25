"""Repository-controlled schema and extraction-profile registry.

Schemas (``registry/schemas/<id>.v<version>.json``) define record kinds, typed fields and
allowed relationships. Profiles (``registry/profiles/<id>@<semver>.json``) define how a
table family maps onto one schema: header bindings, multivalue grammar, context
inheritance, chunking, privacy and review policy. Both are reviewable data, never
model-authored code. Extra directories can be added with ``[extraction] registry_dirs``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from confluence_mirror.structure.ids import digest

FIELD_TYPES = frozenset(
    {"string", "hostname", "ip_address", "port", "integer", "date", "email", "url", "enum"}
)
FAMILIES = (
    "inventory",
    "key_value",
    "relationship_mapping",
    "configuration_matrix",
    "ownership_mapping",
    "procedure",
    "layout",
    "mixed",
    "unknown",
)
_SCHEMA_FILE = re.compile(r"^([a-z][a-z0-9_]*)\.v(\d+)\.json$")
_PROFILE_FILE = re.compile(r"^([a-z][a-z0-9_]*)@(\d+\.\d+\.\d+)\.json$")
_PROFILE_KEYS = {
    "profile_id",
    "version",
    "description",
    "families",
    "representations",
    "schema",
    "template",
    "header_bindings",
    "value_maps",
    "record_granularity",
    "multivalue",
    "context_inheritance",
    "relationships",
    "required_evidence",
    "chunking",
    "privacy_policy",
    "review",
    "extractor",
}


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class Schema:
    schema_id: str
    version: str
    data: dict[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.schema_id}.v{self.version}"

    @property
    def fields(self) -> dict[str, dict]:
        return self.data["fields"]

    @property
    def digest(self) -> str:
        return digest(self.data)[:16]


@dataclass(frozen=True)
class Profile:
    profile_id: str
    version: str
    data: dict[str, Any]
    schema: Schema

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.version}"

    @property
    def digest(self) -> str:
        return digest(self.data)[:16]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class Registry:
    schemas: dict[str, Schema] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)

    def schema(self, ref: str) -> Schema:
        try:
            return self.schemas[ref]
        except KeyError:
            raise RegistryError(f"unknown schema {ref!r}") from None

    def profile(self, ref: str) -> Profile:
        if "@" not in ref:  # newest version of that profile id
            matches = sorted(
                (p for p in self.profiles.values() if p.profile_id == ref),
                key=lambda p: tuple(int(x) for x in p.version.split(".")),
            )
            if not matches:
                raise RegistryError(f"unknown extraction profile {ref!r}")
            return matches[-1]
        try:
            return self.profiles[ref]
        except KeyError:
            raise RegistryError(f"unknown extraction profile {ref!r}") from None

    @property
    def digest(self) -> str:
        return digest(
            {
                "schemas": {k: v.digest for k, v in sorted(self.schemas.items())},
                "profiles": {k: v.digest for k, v in sorted(self.profiles.items())},
            }
        )[:16]


def validate_schema(data: dict, name: str = "<schema>") -> list[str]:
    problems = []
    for key in ("schema_id", "version", "record_kinds", "fields"):
        if key not in data:
            problems.append(f"{name}: missing {key!r}")
    if problems:
        return problems
    if not data["record_kinds"]:
        problems.append(f"{name}: record_kinds is empty")
    for fname, spec in data["fields"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", fname):
            problems.append(f"{name}: bad field name {fname!r}")
        if spec.get("type") not in FIELD_TYPES:
            problems.append(f"{name}: field {fname} has unknown type {spec.get('type')!r}")
        if spec.get("type") == "enum" and not spec.get("values"):
            problems.append(f"{name}: enum field {fname} has no values")
    for fname in data.get("identity_fields", []):
        if fname not in data["fields"]:
            problems.append(f"{name}: identity field {fname} is not a field")
    for pred, spec in (data.get("relationships") or {}).items():
        if spec.get("source_kind") not in data["record_kinds"]:
            problems.append(f"{name}: relationship {pred} has an unknown source_kind")
    return problems


def validate_profile(data: dict, schemas: dict[str, Schema], name: str = "<profile>") -> list[str]:
    problems = [f"{name}: unknown key {k!r}" for k in data if k not in _PROFILE_KEYS]
    for key in ("profile_id", "version", "families", "schema", "template", "extractor"):
        if key not in data:
            problems.append(f"{name}: missing {key!r}")
    if problems:
        return problems
    schema = schemas.get(data["schema"])
    if schema is None:
        return [f"{name}: references unknown schema {data['schema']!r}"]
    fields = schema.fields
    for fam in data["families"]:
        if fam not in FAMILIES:
            problems.append(f"{name}: unknown family {fam!r}")
    for fname in data.get("header_bindings", {}):
        if fname not in fields:
            problems.append(f"{name}: header binding for unknown field {fname!r}")
    for fname in data["template"].get("required_fields", []):
        if fname not in fields:
            problems.append(f"{name}: template requires unknown field {fname!r}")
    for fname, rule in data.get("multivalue", {}).items():
        if fname not in fields:
            problems.append(f"{name}: multivalue rule for unknown field {fname!r}")
        if rule.get("grammar") not in ("list", "never"):
            problems.append(f"{name}: multivalue grammar for {fname} must be list or never")
        if (
            rule.get("grammar") == "list"
            and not fields.get(fname, {}).get("multivalue")
            and (fname != data.get("record_granularity", {}).get("explode_field"))
        ):
            problems.append(f"{name}: {fname} is split but neither multivalue nor exploded")
    for rule in data.get("context_inheritance", []):
        if rule.get("field") not in fields:
            problems.append(f"{name}: context inheritance for unknown field {rule.get('field')!r}")
        if rule.get("from") not in ("heading", "heading_text", "parent_row"):
            problems.append(f"{name}: context inheritance source {rule.get('from')!r} unknown")
        if rule.get("pattern"):
            try:
                re.compile(rule["pattern"])
            except re.error as exc:
                problems.append(f"{name}: bad inheritance pattern: {exc}")
    for pred in data.get("relationships", {}):
        if pred not in (schema.data.get("relationships") or {}):
            problems.append(f"{name}: relationship {pred!r} is not allowed by the schema")
    extractor = data["extractor"]
    if extractor.get("default") not in ("rules", "model"):
        problems.append(f"{name}: extractor.default must be rules or model")
    if extractor.get("effort", "medium") not in ("low", "medium", "high"):
        problems.append(f"{name}: extractor.effort must be low, medium or high")
    return problems


def _files(directory: Any) -> list[tuple[str, str]]:
    return sorted((p.name, p.read_text("utf-8")) for p in directory.iterdir() if p.is_file())


def load_registry(extra_dirs: list[str] | tuple[str, ...] = ()) -> Registry:
    base = resources.files("confluence_mirror.extraction") / "registry"
    sources = [(base / "schemas", base / "profiles")]
    for d in extra_dirs:
        root = Path(d).expanduser()
        sources.append((root / "schemas", root / "profiles"))
    registry = Registry()
    problems: list[str] = []
    for schema_dir, _ in sources:
        if not schema_dir.is_dir():
            continue
        for fname, text in _files(schema_dir):
            m = _SCHEMA_FILE.match(fname)
            if not m:
                continue
            data = json.loads(text)
            errs = validate_schema(data, fname)
            if (data.get("schema_id"), data.get("version")) != (m.group(1), m.group(2)):
                errs.append(f"{fname}: file name does not match schema_id/version")
            if errs:
                problems += errs
                continue
            schema = Schema(data["schema_id"], data["version"], data)
            if schema.ref in registry.schemas:
                problems.append(f"{fname}: duplicate schema {schema.ref}")
            registry.schemas[schema.ref] = schema
    for _, profile_dir in sources:
        if not profile_dir.is_dir():
            continue
        for fname, text in _files(profile_dir):
            m = _PROFILE_FILE.match(fname)
            if not m:
                continue
            data = json.loads(text)
            errs = validate_profile(data, registry.schemas, fname)
            if (data.get("profile_id"), data.get("version")) != (m.group(1), m.group(2)):
                errs.append(f"{fname}: file name does not match profile_id/version")
            if errs:
                problems += errs
                continue
            profile = Profile(
                data["profile_id"], data["version"], data, registry.schemas[data["schema"]]
            )
            if profile.ref in registry.profiles:
                problems.append(f"{fname}: duplicate profile {profile.ref}")
            registry.profiles[profile.ref] = profile
    if problems:
        raise RegistryError("; ".join(problems))
    return registry
