"""Configuration loading.

Values come from a TOML file, overridden by environment variables. The PAT is only ever
read from the environment (``CONFLUENCE_PAT``), optionally populated from a local ``.env``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from confluence_mirror.extraction.privacy import PrivacyPolicy
from confluence_mirror.extraction.provider import ModelSettings

PAT_ENV = "CONFLUENCE_PAT"
_SECRET_KEYS = ("api_key", "key", "token", "password", "secret")


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class ConfluenceSettings:
    base_url: str
    timeout: float = 60.0
    page_size: int = 50
    max_retries: int = 5
    backoff_base: float = 1.0
    ca_bundle: str | None = None
    use_system_truststore: bool = False
    # Client-side ceiling on requests per second (0 disables). Server 429s are still
    # honoured through Retry-After.
    max_requests_per_second: float = 5.0


@dataclass(frozen=True)
class SyncSettings:
    spaces: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ("page",)
    max_delete_fraction: float = 0.5
    incremental_overlap_minutes: int = 1440
    refresh_unchanged_metadata: bool = False
    attachment_dir: str | None = None
    # Body representations fetched per page; storage is always required.
    representations: tuple[str, ...] = ("storage", "export_view", "view")
    # Space listing: "scan" (/content/scan, DC 7.18+), "legacy" (/content offset paging) or
    # "auto" (scan, falling back to legacy when the server lacks it).
    listing: str = "auto"
    listing_page_size: int = 200
    # Pages per bulk fetch (CQL id in (...)); the server caps body expansions at 50.
    body_batch_size: int = 50
    # Content properties: "all" (one request per fetched page), "none", or a list of keys
    # fetched in bulk through metadata.properties.<key> expansions.
    page_properties: str | tuple[str, ...] = "all"


@dataclass(frozen=True)
class BudgetSettings:
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    per_item_retries: int = 1  # semantic repair attempts per unit


@dataclass(frozen=True)
class ExtractionSettings:
    default_profile: str | None = None
    registry_dirs: tuple[str, ...] = ()
    extractor: str = "auto"  # auto | rules | model


@dataclass(frozen=True)
class ConfexSettings:
    model: ModelSettings = field(default_factory=ModelSettings)
    privacy: PrivacyPolicy = field(default_factory=PrivacyPolicy)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    budgets: BudgetSettings = field(default_factory=BudgetSettings)
    pricing: dict[str, dict] = field(default_factory=dict)  # model id -> dated prices


@dataclass(frozen=True)
class Settings:
    confluence: ConfluenceSettings | None
    dsn: str
    sync: SyncSettings = field(default_factory=SyncSettings)
    confex: ConfexSettings = field(default_factory=ConfexSettings)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Populate os.environ from a simple KEY=VALUE file without overriding existing values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value


def get_pat(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    token = env.get(PAT_ENV, "").strip()
    if not token:
        raise ConfigError(f"{PAT_ENV} is not set (export it or put it in a git-ignored .env file)")
    return token


def _validate_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        raise ConfigError("confluence.base_url must use https:// (plain http only for localhost)")
    if not parts.hostname:
        raise ConfigError("confluence.base_url has no host")
    if parts.query or parts.fragment:
        raise ConfigError("confluence.base_url must not contain a query or fragment")
    return url


def load_settings(
    path: Path | None,
    env: dict[str, str] | None = None,
    *,
    require_confluence: bool = True,
    require_dsn: bool = True,
) -> Settings:
    env = os.environ if env is None else env
    data: dict = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        with path.open("rb") as fh:
            data = tomllib.load(fh)

    conf = dict(data.get("confluence", {}))
    for forbidden in ("pat", "token", "password"):
        if forbidden in conf:
            raise ConfigError(
                f"confluence.{forbidden} must not be stored in the config file; "
                f"use the {PAT_ENV} environment variable"
            )
    if conf.get("verify") is False or conf.get("verify_ssl") is False:
        raise ConfigError("disabling TLS verification is not supported; configure ca_bundle")
    conf.pop("verify", None)
    conf.pop("verify_ssl", None)
    if env.get("CONFLUENCE_BASE_URL"):
        conf["base_url"] = env["CONFLUENCE_BASE_URL"]
    if env.get("CONFLUENCE_CA_BUNDLE"):
        conf["ca_bundle"] = env["CONFLUENCE_CA_BUNDLE"]
    known = set(ConfluenceSettings.__dataclass_fields__)
    unknown = set(conf) - known
    if unknown:
        raise ConfigError(f"unknown [confluence] keys: {', '.join(sorted(unknown))}")
    confluence: ConfluenceSettings | None = None
    if conf.get("base_url"):
        conf["base_url"] = _validate_base_url(conf["base_url"])
        if conf.get("ca_bundle") and not Path(conf["ca_bundle"]).expanduser().is_file():
            raise ConfigError(f"ca_bundle not found: {conf['ca_bundle']}")
        confluence = ConfluenceSettings(**conf)
        if not 1 <= confluence.page_size <= 1000:
            raise ConfigError("confluence.page_size must be between 1 and 1000")
        if confluence.max_requests_per_second < 0:
            raise ConfigError("confluence.max_requests_per_second must be >= 0")
    elif require_confluence:
        raise ConfigError("confluence.base_url is required")

    db = data.get("database", {})
    dsn = env.get("CONFLUENCE_MIRROR_DSN") or db.get("dsn") or ""
    if not dsn and require_dsn:
        raise ConfigError("database.dsn (or CONFLUENCE_MIRROR_DSN) is required")

    sync_raw = dict(data.get("sync", {}))
    unknown = set(sync_raw) - set(SyncSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown [sync] keys: {', '.join(sorted(unknown))}")
    for key in ("spaces", "content_types", "representations"):
        if key in sync_raw:
            sync_raw[key] = tuple(sync_raw[key])
    if isinstance(sync_raw.get("page_properties"), list):
        sync_raw["page_properties"] = tuple(sync_raw["page_properties"])
    sync = SyncSettings(**sync_raw)
    bad_types = set(sync.content_types) - {"page", "blogpost"}
    if bad_types:
        raise ConfigError(f"unsupported content_types: {', '.join(sorted(bad_types))}")
    bad_reps = set(sync.representations) - {"storage", "export_view", "view"}
    if bad_reps or "storage" not in sync.representations:
        raise ConfigError(
            "sync.representations must include storage and only name storage, export_view, view"
        )
    if not 0 < sync.max_delete_fraction <= 1:
        raise ConfigError("sync.max_delete_fraction must be in (0, 1]")
    if sync.listing not in ("auto", "scan", "legacy"):
        raise ConfigError("sync.listing must be auto, scan or legacy")
    if not 1 <= sync.body_batch_size <= 50:
        raise ConfigError("sync.body_batch_size must be between 1 and 50 (server cap)")
    if not 1 <= sync.listing_page_size <= 1000:
        raise ConfigError("sync.listing_page_size must be between 1 and 1000")
    if isinstance(sync.page_properties, str) and sync.page_properties not in ("all", "none"):
        raise ConfigError('sync.page_properties must be "all", "none" or a list of keys')
    if isinstance(sync.page_properties, tuple) and not all(
        isinstance(k, str) and k and "," not in k for k in sync.page_properties
    ):
        raise ConfigError("sync.page_properties keys must be non-empty strings without commas")
    return Settings(confluence=confluence, dsn=dsn, sync=sync, confex=_confex_settings(data))


def _section(data: dict, name: str, cls: type) -> dict:
    raw = dict(data.get(name, {}))
    for key in _SECRET_KEYS:
        if key in raw:
            raise ConfigError(
                f"[{name}].{key} must not be stored in the config file; reference an "
                "environment variable instead (e.g. [model] api_key_env)"
            )
    unknown = set(raw) - set(cls.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown [{name}] keys: {', '.join(sorted(unknown))}")
    return raw


def _confex_settings(data: dict) -> ConfexSettings:
    model_raw = _section(data, "model", ModelSettings)
    model = ModelSettings(**model_raw)
    if model.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("model.effort must be low, medium, high, xhigh or max")
    if not model.endpoint.startswith("https://"):
        raise ConfigError("model.endpoint must use https://")
    if not 1 <= model.concurrency <= 16:
        raise ConfigError("model.concurrency must be between 1 and 16")

    priv_raw = dict(data.get("privacy", {}))
    patterns = priv_raw.pop("redact_patterns", [])
    priv_raw = _section({"privacy": priv_raw}, "privacy", PrivacyPolicy)
    for key in ("allowed_spaces", "allowed_endpoints"):
        if key in priv_raw:
            priv_raw[key] = tuple(priv_raw[key])
    extra = []
    for item in patterns:
        try:
            re.compile(item["pattern"])
        except (KeyError, TypeError, re.error) as exc:
            raise ConfigError(f"privacy.redact_patterns entry is invalid: {exc}") from None
        extra.append((str(item.get("name", "custom")), item["pattern"]))
    priv_raw.pop("redactions", None)
    privacy = PrivacyPolicy(**priv_raw, extra_redactions=tuple(extra))

    ext_raw = _section(data, "extraction", ExtractionSettings)
    if "registry_dirs" in ext_raw:
        ext_raw["registry_dirs"] = tuple(ext_raw["registry_dirs"])
    extraction = ExtractionSettings(**ext_raw)
    if extraction.extractor not in ("auto", "rules", "model"):
        raise ConfigError("extraction.extractor must be auto, rules or model")

    budgets = BudgetSettings(**_section(data, "budgets", BudgetSettings))
    pricing = {}
    for model_id, price in (data.get("pricing") or {}).items():
        missing = {"input_per_mtok", "output_per_mtok", "as_of"} - set(price)
        if missing:
            raise ConfigError(f"[pricing.{model_id}] needs {', '.join(sorted(missing))}")
        pricing[model_id] = dict(price)
    return ConfexSettings(model, privacy, extraction, budgets, pricing)
