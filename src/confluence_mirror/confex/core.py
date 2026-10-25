"""Command registry, execution context, output envelope and the ``main`` entry point."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from confluence_mirror import __version__, db
from confluence_mirror import inspection as ins
from confluence_mirror.client import AuthenticationError, ConfluenceClient, ConfluenceError
from confluence_mirror.config import ConfigError, Settings, get_pat, load_dotenv, load_settings
from confluence_mirror.inspection import ConfexError
from confluence_mirror.logging_utils import redact, setup_logging
from confluence_mirror.structure.ids import DEFAULT_INSTANCE_ID, digest

log = logging.getLogger("confluence_mirror.confex")

SCHEMA_VERSION = "confex.cli.v1"
DEFAULT_LIMIT = 50


# -- registry ------------------------------------------------------------------------------


@dataclass
class Result:
    data: Any
    source: dict = field(default_factory=dict)
    completeness: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    exit_code: int = ins.EXIT_OK


@dataclass(frozen=True)
class Spec:
    handler: Callable[[argparse.Namespace, Context], Result]
    live: bool = False  # supported with --data-source live
    collection: bool = False  # accepts --format jsonl
    writes: bool = False  # needs a writable connection
    schema: bool = True  # requires the schema to be fully migrated
    plain: Callable[[Any, argparse.Namespace], str] | None = None


REGISTRY: dict[str, Spec] = {}


def command(name: str, **kw: Any) -> Callable:
    def deco(fn: Callable) -> Callable:
        REGISTRY[name] = Spec(fn, **kw)
        return fn

    return deco


def group(sub: argparse._SubParsersAction, name: str, help: str) -> argparse._SubParsersAction:
    return sub.add_parser(name, help=help).add_subparsers(dest=f"{name}_command", required=True)


def leaf(sub: argparse._SubParsersAction, cmd: str, help: str | None = None):
    parser = sub.add_parser(cmd.split(".")[-1], help=help)
    parser.set_defaults(cmd=cmd)
    return parser


def positive(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def collection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=positive, default=DEFAULT_LIMIT)
    parser.add_argument("--cursor")
    parser.add_argument("--all", action="store_true", help="no page limit (still bounded)")


def representations_arg(value: str) -> list[str]:
    from confluence_mirror.structure import REPRESENTATIONS

    reps = (
        list(REPRESENTATIONS)
        if value == "all"
        else [r.strip() for r in value.split(",") if r.strip()]
    )
    bad = set(reps) - set(REPRESENTATIONS)
    if bad or not reps:
        raise argparse.ArgumentTypeError(f"unknown representation(s): {', '.join(sorted(bad))}")
    return reps


# -- context ---------------------------------------------------------------------------------


def _default_config() -> Path | None:
    for candidate in (Path("config.toml"), Path("config/config.toml")):
        if candidate.is_file():
            return candidate
    return None


def make_client(settings, token: str) -> ConfluenceClient:
    """Indirection so tests can supply a fake transport."""
    return ConfluenceClient(settings, token)


class Context:
    """Lazily opened resources for one command. Mirror commands never open a client."""

    def __init__(self, args: argparse.Namespace, spec: Spec) -> None:
        self.args = args
        self.spec = spec
        self._settings: Settings | None = None
        self._conn: psycopg.Connection | None = None
        self._client: ConfluenceClient | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(
                self.args.config or _default_config(),
                require_confluence=False,
                require_dsn=self.args.data_source != "live",
            )
        return self._settings

    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            conn = db.connect(self.settings.dsn)
            try:
                if self.spec.schema:
                    schema_ready(conn)
                if not self.spec.writes:
                    conn.read_only = True  # navigation and inspection never write
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def client(self) -> ConfluenceClient:
        if self._client is None:
            if self.settings.confluence is None:
                raise ConfigError("confluence.base_url is required for this command")
            self._client = make_client(self.settings.confluence, get_pat())
        return self._client

    @property
    def live(self) -> bool:
        return self.args.data_source == "live"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        if self._client is not None:
            self._client.close()


def schema_ready(conn: psycopg.Connection) -> None:
    status = db.migration_status(conn)
    if any(not m["applied"] or not m["checksum_ok"] for m in status):
        raise ConfexError(
            "SCHEMA_INCOMPATIBLE",
            "the mirror schema is not at this version; run `confex db migrate` explicitly",
            ins.EXIT_UNAVAILABLE,
            {"pending": [m["version"] for m in status if not m["applied"]]},
        )


# -- output ------------------------------------------------------------------------------------


def json_default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    raise TypeError(type(o).__name__)


def dumps(value: Any, **kw: Any) -> str:
    return json.dumps(value, default=json_default, ensure_ascii=False, **kw)


def envelope(
    command: str,
    data: Any,
    *,
    source: dict | None = None,
    completeness: dict | None = None,
    diagnostics: list | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error" if error else "ok",
        "source": {"mode": "mirror", "instance_id": DEFAULT_INSTANCE_ID, **(source or {})},
        "data": data,
        "completeness": completeness
        or {
            "scope": command,
            "state": "complete",
            "has_more": False,
            "truncated": False,
            "reasons": [],
        },
        "diagnostics": diagnostics or [],
        "error": error,
    }


def write_text(text: str, path: Path | None, overwrite: bool) -> None:
    """stdout, or an exclusive temp file renamed atomically into place (mode 0600)."""
    if path is None:
        sys.stdout.write(text)
        return
    if path.exists() and not overwrite:
        raise ConfexError(
            "OUTPUT_EXISTS", f"{path} exists; pass --overwrite to replace it", ins.EXIT_USAGE
        )
    fd, tmp = tempfile.mkstemp(dir=path.parent or Path("."), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def safe(text: Any) -> str:
    """Terminal-safe text: control characters (incl. ESC) are shown escaped."""
    s = "" if text is None else str(text)
    return "".join(
        ch if ch == "\n" or unicodedata.category(ch)[0] != "C" else f"\\x{ord(ch):02x}" for ch in s
    )


def paginate(items: list, args: argparse.Namespace, scope: dict) -> tuple[list, dict]:
    key = digest(scope)[:16]
    offset = 0
    if getattr(args, "cursor", None):
        try:
            raw = json.loads(base64.urlsafe_b64decode(args.cursor.encode()).decode())
            offset, cursor_key = int(raw["o"]), raw["k"]
        except (ValueError, KeyError, TypeError):
            raise ConfexError("INVALID_CURSOR", "cursor is malformed", ins.EXIT_USAGE) from None
        if cursor_key != key:
            raise ConfexError(
                "INVALID_CURSOR", "cursor belongs to a different query", ins.EXIT_USAGE
            )
    limit = None if getattr(args, "all", False) else getattr(args, "limit", DEFAULT_LIMIT)
    page = items[offset:] if limit is None else items[offset : offset + limit]
    end = offset + len(page)
    has_more = end < len(items)
    cursor = (
        base64.urlsafe_b64encode(json.dumps({"o": end, "k": key}).encode()).decode()
        if has_more
        else None
    )
    return page, {
        "scope": scope.get("scope", "collection"),
        "state": "complete" if not has_more else "partial",
        "has_more": has_more,
        "truncated": False,
        "returned": len(page),
        "known_total": len(items),
        "next_cursor": cursor,
        "reasons": [] if not has_more else ["page limit reached; pass --cursor to continue"],
    }


def emit(args: argparse.Namespace, spec: Spec, result: Result) -> None:
    from confluence_mirror.confex.render import render_plain

    cmd = args.cmd
    source = {"mode": "live"} if args.data_source == "live" else {}
    source |= result.source
    if args.format == "json":
        env = envelope(
            cmd,
            result.data,
            source=source,
            completeness=result.completeness or None,
            diagnostics=result.diagnostics,
        )
        text = dumps(env, indent=2) + "\n"
    elif args.format == "jsonl":
        if not spec.collection:
            raise ConfexError(
                "UNSUPPORTED_MODE", f"--format jsonl is not accepted by {cmd}", ins.EXIT_USAGE
            )
        lines = [
            {
                "type": "meta",
                "schema_version": SCHEMA_VERSION,
                "command": cmd,
                "source": {"mode": "mirror", "instance_id": DEFAULT_INSTANCE_ID, **source},
            }
        ]
        lines += [{"type": "item", "data": item} for item in result.data]
        lines.append(
            {
                "type": "summary",
                "status": "ok",
                "completeness": result.completeness,
                "diagnostics": result.diagnostics,
            }
        )
        text = "".join(dumps(x) + "\n" for x in lines)
    else:
        renderer = spec.plain or render_plain
        text = renderer(result.data, args)
        if result.completeness.get("has_more"):
            text += f"-- more: --cursor {result.completeness['next_cursor']}\n"
    write_text(text, args.output, args.overwrite)


def emit_error(args: argparse.Namespace | None, exc: ConfexError) -> None:
    log.error("%s", exc)
    if args is None or args.format == "plain":
        for choice in exc.details.get("choices", []):
            sys.stderr.write(f"  candidate: {choice['selector']}  ({choice['table_id']})\n")
        if exc.details.get("hint"):
            sys.stderr.write(f"  hint: {exc.details['hint']}\n")
        return
    cmd = getattr(args, "cmd", "confex")
    error = {"code": exc.code, "message": exc.message, "details": exc.details}
    if args.format == "jsonl":
        text = dumps({"type": "summary", "status": "error", "error": error}) + "\n"
    else:
        env = envelope(
            cmd,
            None,
            source={"mode": args.data_source},
            error=error,
            completeness={
                "scope": cmd,
                "state": "failed",
                "has_more": False,
                "truncated": False,
                "reasons": [exc.code],
            },
        )
        text = dumps(env, indent=2) + "\n"
    sys.stdout.write(text)


# -- entry point --------------------------------------------------------------------------------


def build_parser(modules: list) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="confex",
        description="Explore a Confluence mirror, inspect document and table structure, and "
        "run the evidence-backed extraction pipeline. Global options go before the subcommand.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", type=Path, help="config TOML (default: ./config.toml)")
    p.add_argument("--data-source", choices=("mirror", "live"), default="mirror")
    p.add_argument(
        "--format",
        choices=("plain", "json", "jsonl"),
        default="plain",
        help="output: plain text, one JSON envelope, or JSONL (collections only)",
    )
    p.add_argument("--output", type=Path, help="write output to this file instead of stdout")
    p.add_argument("--overwrite", action="store_true", help="allow --output to replace a file")
    p.add_argument("--no-color", action="store_true", help="accepted; output is never colored")
    p.add_argument("--verbose", "-v", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)
    for module in modules:
        module.register(sub)
    return p


def run_main(modules: list, argv: list[str] | None = None) -> int:
    parser = build_parser(modules)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return ins.EXIT_USAGE if exc.code not in (0, None) else 0
    setup_logging(args.verbose)
    load_dotenv()
    spec = REGISTRY[args.cmd]
    try:
        if args.data_source == "live" and not spec.live:
            raise ConfexError(
                "CAPABILITY_UNAVAILABLE",
                f"{args.cmd} is mirror-only; sync the scope and use the mirror",
                ins.EXIT_UNAVAILABLE,
                {"hint": "confex sync --page ID, then confex parse --page ID"},
            )
        ctx = Context(args, spec)
        try:
            result = spec.handler(args, ctx)
        finally:
            ctx.close()
        emit(args, spec, result)
        return result.exit_code
    except ConfexError as exc:
        emit_error(args, exc)
        return exc.exit_code
    except ConfigError as exc:
        emit_error(args, ConfexError("CONFIG_INVALID", str(exc), ins.EXIT_USAGE))
        return ins.EXIT_USAGE
    except AuthenticationError as exc:
        emit_error(args, ConfexError("AUTHENTICATION_FAILED", str(exc), ins.EXIT_POLICY))
        return ins.EXIT_POLICY
    except ConfluenceError as exc:
        emit_error(args, ConfexError("SOURCE_ERROR", str(exc), ins.EXIT_INTERNAL))
        return ins.EXIT_INTERNAL
    except db.MigrationError as exc:
        emit_error(args, ConfexError("SCHEMA_INCOMPATIBLE", str(exc), ins.EXIT_UNAVAILABLE))
        return ins.EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        log.error("interrupted")
        return ins.EXIT_INTERRUPTED
    except psycopg.Error as exc:
        emit_error(args, ConfexError("DATABASE_ERROR", redact(str(exc)), ins.EXIT_INTERNAL))
        return ins.EXIT_INTERNAL
    except Exception as exc:
        log.error("unexpected error: %s", redact(f"{type(exc).__name__}: {exc}"))
        if args.verbose:
            log.exception("traceback")
        return ins.EXIT_INTERNAL
