"""Configuration loading.

Values come from a TOML file, overridden by environment variables. The PAT is only ever
read from the environment (``CONFLUENCE_PAT``), optionally populated from a local ``.env``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PAT_ENV = "CONFLUENCE_PAT"


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


@dataclass(frozen=True)
class Settings:
    confluence: ConfluenceSettings | None
    dsn: str
    sync: SyncSettings = field(default_factory=SyncSettings)


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
    return Settings(confluence=confluence, dsn=dsn, sync=sync)
