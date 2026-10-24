"""Command-line interface: ``confluence-mirror <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from confluence_mirror import __version__, db
from confluence_mirror.client import AuthenticationError, ConfluenceClient, ConfluenceError
from confluence_mirror.config import ConfigError, get_pat, load_dotenv, load_settings
from confluence_mirror.logging_utils import redact, setup_logging
from confluence_mirror.repository import recent_runs
from confluence_mirror.sync import SpaceSync, SyncLockedError, reextract

log = logging.getLogger("confluence_mirror")

EXIT_OK, EXIT_USAGE, EXIT_PARTIAL, EXIT_FAILED, EXIT_AUTH = 0, 1, 2, 3, 4


def _default_config() -> Path | None:
    for candidate in (Path("config.toml"), Path("config/config.toml")):
        if candidate.is_file():
            return candidate
    return None


def _json(obj) -> str:
    def default(o):
        if isinstance(o, datetime | date):
            return o.isoformat()
        raise TypeError(type(o).__name__)

    return json.dumps(obj, indent=2, default=default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="confluence-mirror", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="config TOML (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    dbp = sub.add_parser("db", help="database migrations")
    dbsub = dbp.add_subparsers(dest="db_command", required=True)
    mig = dbsub.add_parser("migrate", help="apply pending migrations")
    mig.add_argument("--target", type=int, help="stop after this migration version")
    dbsub.add_parser("status", help="show applied/pending migrations")

    sub.add_parser("check", help="validate config and test read-only access to Confluence")

    sp = sub.add_parser("sync", help="mirror one or more spaces")
    target = sp.add_mutually_exclusive_group(required=True)
    target.add_argument("--space", action="append", metavar="KEY", help="space key (repeatable)")
    target.add_argument("--all", action="store_true", help="every space in [sync].spaces")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument(
        "--full", action="store_const", dest="mode", const="full", help="re-fetch every page"
    )
    mode.add_argument(
        "--incremental",
        action="store_const",
        dest="mode",
        const="incremental",
        help="fetch only changes since the last successful sync",
    )
    sp.set_defaults(mode="auto")

    ex = sub.add_parser("extract", help="rebuild extracted data from stored bodies (offline)")
    ex.add_argument("--space", metavar="KEY")
    ex.add_argument("--page", type=int, action="append", metavar="ID")
    ex.add_argument(
        "--all-versions",
        action="store_true",
        help="re-extract every stored version, not just current ones",
    )

    runs = sub.add_parser("runs", help="list recent sync runs")
    runs.add_argument("--space", metavar="KEY")
    runs.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(-1 if args.quiet else args.verbose)
    load_dotenv()
    try:
        return _dispatch(args)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_USAGE
    except AuthenticationError as exc:
        log.error("authentication failed: %s", exc)
        return EXIT_AUTH
    except (ConfluenceError, db.MigrationError, SyncLockedError) as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
    except Exception as exc:
        log.error("unexpected error: %s", redact(f"{type(exc).__name__}: {exc}"))
        if args.verbose:
            log.exception("traceback")
        return EXIT_FAILED


def _dispatch(args: argparse.Namespace) -> int:
    offline = args.command in ("db", "runs", "extract")
    settings = load_settings(args.config or _default_config(), require_confluence=not offline)
    if args.command == "db":
        with db.connect(settings.dsn) as conn:
            if args.db_command == "migrate":
                applied = db.migrate(conn, args.target)
                for m in applied:
                    log.info("applied %04d_%s", m.version, m.name)
                if not applied:
                    log.info("database is up to date")
            else:
                print(_json(db.migration_status(conn)))
        return EXIT_OK

    if args.command == "runs":
        with db.connect(settings.dsn) as conn:
            print(_json(recent_runs(conn, args.space, args.limit)))
        return EXIT_OK

    if args.command == "extract":
        with db.connect(settings.dsn) as conn:
            base_url = settings.confluence.base_url if settings.confluence else None
            result = reextract(
                conn,
                base_url=base_url,
                space_key=args.space,
                page_ids=args.page,
                all_versions=args.all_versions,
            )
        print(_json(result))
        return EXIT_OK if result["failed"] == 0 else EXIT_PARTIAL

    token = get_pat()
    assert settings.confluence is not None
    with ConfluenceClient(settings.confluence, token) as client:
        if args.command == "check":
            user = client.current_user()
            print(
                _json(
                    {
                        "ok": True,
                        "base_url": settings.confluence.base_url,
                        "user": user.get("username") or user.get("userKey"),
                        "display_name": user.get("displayName"),
                    }
                )
            )
            return EXIT_OK

        spaces = args.space or list(settings.sync.spaces)
        if not spaces:
            raise ConfigError("no spaces given; use --space KEY or set [sync].spaces")
        worst = EXIT_OK
        with db.connect(settings.dsn) as conn:
            pending = [m for m in db.migration_status(conn) if not m["applied"]]
            if pending:
                raise ConfigError(
                    "database has pending migrations; run `confluence-mirror db migrate` first"
                )
            for space_key in spaces:
                try:
                    report = SpaceSync(conn, client, settings.sync).run(space_key, args.mode)
                except AuthenticationError:
                    raise
                except (ConfluenceError, SyncLockedError, ValueError) as exc:
                    log.error("sync of %s failed: %s", space_key, exc)
                    worst = max(worst, EXIT_FAILED)
                    continue
                summary = report.as_dict()
                summary["errors"] = summary["errors"][:20]
                print(_json(summary))
                if report.status != "succeeded":
                    worst = max(worst, EXIT_PARTIAL)
        return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
