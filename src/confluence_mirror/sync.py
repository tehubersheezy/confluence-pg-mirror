"""Space-at-a-time sync orchestration.

Discovery always lists every current page of the space with lightweight metadata (version,
ancestors, labels). Comparing that listing with the mirror drives everything else:

* new page, newer version, or missing body  -> full fetch (bodies, labels, properties,
  restrictions, attachments) and re-extraction
* same version                              -> hierarchy/title/labels refreshed from the
  listing; moves are detected from ancestors
* known page absent from the listing        -> checked individually: trashed, purged, no
  longer visible, or moved to another space

"Full" mode re-fetches every page; "incremental" fetches only what changed, plus pages whose
attachments changed since the last successful sync (via CQL). Each page is written in its
own transaction, so one failing page is recorded in sync_runs and retried next run (its
stored version is unchanged) without blocking the rest of the space.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg

from confluence_mirror import models
from confluence_mirror import repository as repo
from confluence_mirror.client import (
    CHILD_ATTACHMENTS_EXPAND,
    CONTENT_EXPAND_LIST,
    AuthenticationError,
    ConfluenceClient,
    ConfluenceError,
    ForbiddenError,
    NotFoundError,
    bulk_expand,
    cql_string,
)
from confluence_mirror.config import SyncSettings
from confluence_mirror.extract import EXTRACTOR_VERSION, extract
from confluence_mirror.logging_utils import redact

log = logging.getLogger(__name__)

MAX_RECORDED_ERRORS = 500
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Global keys are alphanumeric; personal spaces are "~username".
SPACE_KEY_RE = re.compile(r"^~?[A-Za-z0-9_.\-@]{1,255}$")


@dataclass(frozen=True)
class Scope:
    """A page selection to sync: explicit pages, a page tree, or a CQL result."""

    kind: str  # pages | tree | cql
    page_ids: tuple[int, ...] = ()
    root: int | None = None
    cql: str | None = None
    full: bool = False  # also re-fetch pages whose version is unchanged

    def __post_init__(self) -> None:
        if self.kind == "pages" and not self.page_ids:
            raise ValueError("a pages scope needs page IDs")
        if self.kind == "tree" and self.root is None:
            raise ValueError("a tree scope needs a root page ID")
        if self.kind == "cql" and not (self.cql or "").strip():
            raise ValueError("a cql scope needs a query")
        if self.kind not in ("pages", "tree", "cql"):
            raise ValueError(f"unknown scope {self.kind!r}")

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "full": self.full}
        if self.kind == "pages":
            out["pages"] = len(self.page_ids)
        elif self.kind == "tree":
            out["root"] = self.root
        else:
            out["cql"] = self.cql
        return out


def _delta(now: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {k: v - before.get(k, 0) for k, v in sorted(now.items()) if v > before.get(k, 0)}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class SyncLockedError(Exception):
    """Another process is syncing the same space."""


@dataclass
class Counters:
    pages_listed: int = 0
    pages_fetched: int = 0
    pages_unchanged: int = 0
    pages_moved: int = 0
    pages_deleted: int = 0
    pages_failed: int = 0
    attachments_seen: int = 0


@dataclass
class SyncReport:
    sync_run_id: int
    space_key: str
    mode: str
    status: str
    counters: Counters
    errors: list[dict] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "sync_run_id": self.sync_run_id,
            "space_key": self.space_key,
            "mode": self.mode,
            "status": self.status,
            **asdict(self.counters),
            "errors": self.errors,
            "stats": self.stats,
        }


class SpaceSync:
    def __init__(
        self, conn: psycopg.Connection, client: ConfluenceClient, settings: SyncSettings
    ) -> None:
        self.conn = conn
        self.client = client
        self.settings = settings
        self._counts_at_start: tuple[dict[str, int], dict[str, int]] = ({}, {})
        keys = settings.page_properties if isinstance(settings.page_properties, tuple) else ()
        self._property_keys: tuple[str, ...] = keys
        self._bulk = bulk_expand(settings.representations, keys)
        restrictions = ",".join(p for p in self._bulk.split(",") if p.startswith("restrictions."))
        props = ",".join(f"metadata.properties.{k}" for k in keys)
        # Everything except bodies, for metadata refreshes of unchanged pages.
        self._meta = ",".join(
            x for x in ("metadata.labels", CHILD_ATTACHMENTS_EXPAND, restrictions, props) if x
        )

    # -- entry point ----------------------------------------------------------------

    def run(self, space_key: str, mode: str = "auto") -> SyncReport:
        if mode not in ("auto", "full", "incremental"):
            raise ValueError(f"unknown mode {mode}")
        return self._locked(space_key, lambda: self._run_locked(space_key, mode))

    def run_metadata(self, space_key: str) -> SyncReport:
        """List the space and record page metadata only (mode ``metadata``); no bodies."""
        return self._locked(space_key, lambda: self._run_metadata(space_key))

    def run_scope(self, scope: Scope) -> tuple[list[SyncReport], list[int]]:
        """Sync a page selection, a page tree or a CQL result (mode ``pages``).

        The selection is listed in bulk (metadata only), grouped by space, and each space's
        new or changed pages are fetched in batches. There is no deletion phase and the
        space cursor does not move: a page outside the selection is simply not looked at.
        Returns the per-space reports and the requested page IDs that were not visible.
        """
        items, missing = self._scope_listing(scope)
        by_space: dict[str, list[dict]] = {}
        for item in items:
            by_space.setdefault(item["space"]["key"], []).append(item)
        reports = [
            self._locked(key, lambda key=key: self._run_listed(key, by_space[key], scope))
            for key in sorted(by_space)
        ]
        return reports, missing

    def _scope_listing(self, scope: Scope) -> tuple[list[dict], list[int]]:
        limit = min(self.settings.listing_page_size, 200)  # CQL cap with expansions
        found: dict[int, dict] = {}
        if scope.kind == "pages":
            ids = list(dict.fromkeys(scope.page_ids))
            for batch in _chunks(ids, limit):
                for item in self.client.content_by_ids(batch, CONTENT_EXPAND_LIST, limit=limit):
                    found[int(item["id"])] = item
            return list(found.values()), [i for i in ids if i not in found]
        if scope.kind == "tree":
            queries = [f"id = {int(scope.root)}", f"ancestor = {int(scope.root)} and type = page"]
        elif scope.kind == "cql":
            queries = [f"({scope.cql}) and type = {t}" for t in self.settings.content_types]
        else:  # pragma: no cover - Scope validates kind
            raise ValueError(scope.kind)
        for cql in queries:
            for item in self.client.search(cql, CONTENT_EXPAND_LIST, limit=limit):
                if item.get("type") in self.settings.content_types:
                    found[int(item["id"])] = item
        missing = [scope.root] if scope.kind == "tree" and scope.root not in found else []
        return list(found.values()), missing

    def _run_listed(self, space_key: str, items: list[dict], scope: Scope) -> SyncReport:
        run_id = repo.start_run(self.conn, space_key, "pages", None)
        self.conn.commit()
        report = SyncReport(run_id, space_key, "pages", "running", Counters())
        report.stats["scope"] = scope.describe()
        try:
            space = models.space_row(self.client.get_space(space_key))
            repo.upsert_space(self.conn, space, run_id)
            self.conn.commit()
            known: dict[int, repo.KnownPage] = {}
            for content_type in {i.get("type", "page") for i in items}:
                known.update(repo.known_pages(self.conn, space["space_id"], content_type))
            self.conn.rollback()
            report.counters.pages_listed += len(items)
            self._apply_listing(report, space["space_id"], items, known, full=scope.full)
            repo.resolve_links(self.conn, space["space_id"])
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            report.status = "failed"
            self._error(report, None, "run", exc)
            self._finish(report)
            raise
        report.status = "succeeded" if not report.errors else "partial"
        self._finish(report)
        return report

    def _run_metadata(self, space_key: str) -> SyncReport:
        run_id = repo.start_run(self.conn, space_key, "metadata", None)
        self.conn.commit()
        report = SyncReport(run_id, space_key, "metadata", "running", Counters())
        try:
            space = models.space_row(self.client.get_space(space_key))
            repo.upsert_space(self.conn, space, run_id)
            self.conn.commit()
            for content_type in self.settings.content_types:
                for item in self._space_listing(space_key, content_type, report):
                    report.counters.pages_listed += 1
                    listed = models.page_row(item)
                    with self.conn.transaction():
                        if repo.update_page_from_listing(self.conn, listed, run_id):
                            report.counters.pages_moved += 1
                        labels, complete = models.listing_labels(item)
                        if complete:
                            repo.replace_labels(
                                self.conn, listed["page_id"], models.label_rows(labels), run_id
                            )
            report.stats["deletions_evaluated"] = False
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            report.status = "failed"
            self._error(report, None, "run", exc)
            self._finish(report)
            raise
        report.status = "succeeded" if not report.errors else "partial"
        self._finish(report)
        return report

    def _locked(self, space_key: str, body):
        if not SPACE_KEY_RE.match(space_key):
            raise ValueError(f"invalid space key: {space_key!r}")
        lock_key = f"confluence-mirror:space:{space_key}"
        locked = self.conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,)
        ).fetchone()[0]
        self.conn.commit()
        if not locked:
            raise SyncLockedError(f"space {space_key} is already being synced")
        # Request counts in the run's stats cover this space only.
        self._counts_at_start = (dict(self.client.request_counts), dict(self.client.retry_counts))
        try:
            return body()
        finally:
            self.conn.rollback()
            self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
            self.conn.commit()

    def _run_locked(self, space_key: str, mode: str) -> SyncReport:
        abandoned = repo.abandon_stale_runs(self.conn, space_key)
        if abandoned:
            log.warning("marked %d interrupted run(s) for %s as abandoned", abandoned, space_key)
        state = repo.space_state(self.conn, space_key)
        cursor = state["last_successful_sync_at"] if state else None
        if mode == "auto":
            mode = "incremental" if cursor else "full"
        elif mode == "incremental" and cursor is None:
            log.info("no previous successful sync of %s; running a full sync", space_key)
            mode = "full"
        run_id = repo.start_run(
            self.conn, space_key, mode, cursor if mode == "incremental" else None
        )
        self.conn.commit()
        report = SyncReport(run_id, space_key, mode, "running", Counters())
        try:
            self._sync(report, cursor if mode == "incremental" else None)
        except Exception as exc:
            self.conn.rollback()
            report.status = "failed"
            self._error(report, None, "run", exc)
            self._finish(report)
            raise
        report.status = "succeeded" if not report.errors else "partial"
        self._finish(report)
        return report

    def _finish(self, report: SyncReport) -> None:
        before, retries_before = self._counts_at_start
        report.stats["http_requests"] = _delta(self.client.request_counts, before)
        retries = _delta(self.client.retry_counts, retries_before)
        if retries:
            report.stats["http_retries"] = retries
            report.stats["requests_per_second"] = self.client.rate
        repo.finish_run(
            self.conn,
            report.sync_run_id,
            report.status,
            asdict(report.counters),
            report.errors[:MAX_RECORDED_ERRORS],
            report.stats,
        )
        self.conn.commit()

    def _error(self, report: SyncReport, page_id: int | None, stage: str, exc: Exception) -> None:
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("%s%s failed: %s", stage, f" page {page_id}" if page_id else "", message)
        entry: dict[str, Any] = {"stage": stage, "error": message}
        if page_id is not None:
            entry["page_id"] = page_id
        status = getattr(exc, "status", None)
        if status:
            entry["http_status"] = status
        report.errors.append(entry)

    # -- phases -------------------------------------------------------------------------

    def _sync(self, report: SyncReport, cursor) -> None:
        run_id = report.sync_run_id
        full = report.mode == "full"
        space_json = self.client.get_space(report.space_key)
        space = models.space_row(space_json)
        space_id = space["space_id"]
        repo.upsert_space(self.conn, space, run_id)
        if self.settings.page_properties == "all":
            props = [models.property_row(p) for p in self.client.space_properties(report.space_key)]
            repo.replace_space_properties(self.conn, space_id, props, run_id)
        self.conn.commit()

        fetched: set[int] = set()
        for content_type in self.settings.content_types:
            listing = list(self._space_listing(report.space_key, content_type, report))
            report.counters.pages_listed += len(listing)
            known = repo.known_pages(self.conn, space_id, content_type)
            self.conn.rollback()
            seen, done = self._apply_listing(report, space_id, listing, known, full=full)
            fetched |= done
            self._handle_missing(report, space_id, known, seen)

        if not full and cursor is not None:
            self._refresh_changed_attachments(report, cursor, fetched)

        resolved = repo.resolve_links(self.conn, space_id)
        report.stats["links_resolved"] = resolved
        if not report.errors:
            repo.mark_space_synced(self.conn, space_id, run_id, full)
        self.conn.commit()

    # -- listing -----------------------------------------------------------------------

    def _space_listing(self, space_key: str, content_type: str, report: SyncReport):
        """Every current item of a space with cheap metadata: scan if possible."""
        mode = self.settings.listing
        limit = self.settings.listing_page_size
        if mode in ("auto", "scan"):
            try:
                items = list(
                    self.client.scan_content(
                        space_key, content_type, CONTENT_EXPAND_LIST, limit=limit
                    )
                )
                report.stats.setdefault("listing", {})[content_type] = "scan"
                return items
            except AuthenticationError:
                raise
            except ConfluenceError as exc:
                # 400/404/405/501: no scan on this server (or not for this type). Anything
                # else is a real failure and must not silently switch strategy.
                if mode == "scan" or exc.status not in (400, 404, 405, 501):
                    raise
                log.info(
                    "scan unavailable for %s %s (HTTP %s); using offset listing",
                    space_key,
                    content_type,
                    exc.status,
                )
        report.stats.setdefault("listing", {})[content_type] = "legacy"
        return list(self.client.list_content(space_key, content_type, limit=limit))

    def _apply_listing(
        self,
        report: SyncReport,
        space_id: int,
        listing: list[dict],
        known: dict[int, repo.KnownPage],
        *,
        full: bool,
    ) -> tuple[set[int], set[int]]:
        """Refresh unchanged pages from the listing and bulk-fetch the rest.

        Returns (page IDs seen, page IDs fetched).
        """
        # Parents first, so hierarchy and same-space links fill in top-down.
        pairs = sorted(
            ((item, models.page_row(item)) for item in listing),
            key=lambda p: (p[1]["depth"], p[1]["position"] or 0, p[1]["page_id"]),
        )
        seen: set[int] = set()
        to_fetch: list[int] = []
        to_refresh: list[int] = []
        for item, listed in pairs:
            page_id = listed["page_id"]
            seen.add(page_id)
            prior = known.get(page_id)
            needs_fetch = (
                full
                or prior is None
                or prior.is_deleted
                or prior.current_version != listed["current_version"]
                or not prior.has_content
            )
            if needs_fetch:
                to_fetch.append(page_id)
                continue
            try:
                if self._refresh_unchanged(item, listed, report):
                    to_refresh.append(page_id)
                if prior.extractor_version != EXTRACTOR_VERSION:
                    self._reextract_stored(page_id, prior.current_version, report)
            except Exception as exc:  # one page must not stop the space
                self.conn.rollback()
                report.counters.pages_failed += 1
                self._error(report, page_id, "page", exc)
        fetched = self._fetch_pages(to_fetch, space_id, report)
        if to_refresh:
            self._refresh_resources(to_refresh, report, everything=True)
        return seen, fetched

    # -- bulk fetch ----------------------------------------------------------------------

    def _fetch_pages(self, page_ids: list[int], space_id: int, report: SyncReport) -> set[int]:
        """Fetch pages in batches (``id in (...)``) and store each in its own transaction.

        A failed batch falls back to page-by-page requests, so one bad page still fails
        alone. A page missing from a batch result is fetched individually.
        """
        stored: set[int] = set()
        size = self.settings.body_batch_size
        for batch in _chunks(page_ids, size):
            contents: dict[int, dict] = {}
            try:
                for c in self.client.content_by_ids(batch, self._bulk, limit=size):
                    contents[int(c["id"])] = c
                report.stats["bulk_batches"] = report.stats.get("bulk_batches", 0) + 1
            except AuthenticationError:
                raise
            except ConfluenceError as exc:
                log.warning(
                    "bulk fetch of %d page(s) failed (%s); fetching one by one", len(batch), exc
                )
                report.stats["bulk_fallbacks"] = report.stats.get("bulk_fallbacks", 0) + 1
            for page_id in batch:
                try:
                    content = contents.get(page_id)
                    if content is None:
                        content = self.client.get_content(page_id, expand=self._bulk)
                        report.stats["single_fetches"] = report.stats.get("single_fetches", 0) + 1
                    self._store_page(content, space_id, report)
                    stored.add(page_id)
                except AuthenticationError:
                    raise
                except Exception as exc:  # one page must not stop the batch
                    self.conn.rollback()
                    report.counters.pages_failed += 1
                    self._error(report, page_id, "page", exc)
        return stored

    def _labels(self, content: dict, page_id: int, report: SyncReport) -> list[dict]:
        labels, complete = models.listing_labels(content)
        if not complete:
            labels = list(self.client.labels(page_id))
            report.stats["label_fallbacks"] = report.stats.get("label_fallbacks", 0) + 1
        return models.label_rows(labels)

    def _attachments(self, content: dict, page_id: int, report: SyncReport) -> list[dict]:
        items, complete = models.expanded_attachments(content)
        if not complete:
            items = list(self.client.attachments(page_id))
            report.stats["attachment_fallbacks"] = report.stats.get("attachment_fallbacks", 0) + 1
        return [models.attachment_row(a, page_id) for a in items]

    def _properties(self, content: dict, page_id: int) -> tuple[list[dict], bool] | None:
        """Rows, and whether they are the page's complete set (only for "all").

        None means "leave stored properties alone". Expanded keys are only ever added or
        updated: an absent key may mean the server did not expand it, not that it is gone.
        """
        policy = self.settings.page_properties
        if policy == "none":
            return None
        if policy == "all":
            rows = [models.property_row(p) for p in self.client.content_properties(page_id)]
            return rows, True
        if not self._property_keys:
            return None
        return models.expanded_properties(content, self._property_keys), False

    def _write_properties(self, page_id: int, props, run_id: int) -> None:
        if props is None:
            return
        rows, complete = props
        if complete:
            repo.replace_page_properties(self.conn, page_id, rows, run_id)
        else:
            repo.upsert_page_properties(self.conn, page_id, rows, run_id)

    def _restrictions(self, content: dict, page_id: int, report: SyncReport) -> list[dict]:
        restrictions, complete = models.restriction_rows(content)
        if not complete:
            restrictions, complete = models.restriction_rows(
                {"restrictions": self.client.restrictions(page_id)}
            )
            if not complete:
                report.stats.setdefault("restrictions_incomplete", []).append(page_id)
        return restrictions

    def _store_page(self, content: dict, space_id: int, report: SyncReport) -> None:
        run_id = report.sync_run_id
        page_id = int(content["id"])
        # Fallback requests (truncated expansions) first, then one short transaction.
        labels = self._labels(content, page_id, report)
        attachments = self._attachments(content, page_id, report)
        props = self._properties(content, page_id)
        restrictions = self._restrictions(content, page_id, report)
        page = models.page_row(content)
        if page["space_id"] != space_id:
            repo.ensure_space_stub(self.conn, content["space"])
        with self.conn.transaction():
            if repo.upsert_page(self.conn, page, run_id):
                report.counters.pages_moved += 1
            repo.insert_version(self.conn, models.version_row(content), run_id)
            changed = repo.upsert_content(self.conn, models.content_row(content), run_id)
            if changed:  # same version, different body: archived, never silently lost
                report.stats.setdefault("same_version_body_changed", []).append(
                    {"page_id": page_id, "representations": changed}
                )
            repo.replace_labels(self.conn, page_id, labels, run_id)
            self._write_properties(page_id, props, run_id)
            repo.replace_restrictions(self.conn, page_id, restrictions, run_id)
            repo.sync_attachments(self.conn, page_id, attachments, run_id)
            self._extract(page_id, page["current_version"], content, report)
        self.conn.commit()
        report.counters.pages_fetched += 1
        report.counters.attachments_seen += len(attachments)
        if self.settings.attachment_dir:
            self._download_attachments(page_id, attachments, report)

    def _refresh_resources(
        self, page_ids: list[int], report: SyncReport, *, everything: bool
    ) -> None:
        """Labels, attachments, restrictions (and properties) of unchanged pages, in bulk.

        With ``everything=False`` only attachments are refreshed.
        """
        run_id = report.sync_run_id
        limit = 200  # server cap for non-body expansions
        expand = self._meta if everything else CHILD_ATTACHMENTS_EXPAND
        for batch in _chunks(page_ids, limit):
            try:
                contents = {
                    int(c["id"]): c for c in self.client.content_by_ids(batch, expand, limit=limit)
                }
                report.stats["bulk_batches"] = report.stats.get("bulk_batches", 0) + 1
            except AuthenticationError:
                raise
            except ConfluenceError as exc:
                self._error(report, None, "metadata-refresh", exc)
                continue
            for page_id in batch:
                content = contents.get(page_id)
                if content is None:
                    continue  # no longer visible; the next listing decides what happened
                try:
                    attachments = self._attachments(content, page_id, report)
                    labels = self._labels(content, page_id, report) if everything else None
                    props = self._properties(content, page_id) if everything else None
                    restrictions = (
                        self._restrictions(content, page_id, report) if everything else None
                    )
                    with self.conn.transaction():
                        if labels is not None:
                            repo.replace_labels(self.conn, page_id, labels, run_id)
                        self._write_properties(page_id, props, run_id)
                        if restrictions is not None:
                            repo.replace_restrictions(self.conn, page_id, restrictions, run_id)
                        repo.sync_attachments(self.conn, page_id, attachments, run_id)
                    self.conn.commit()
                    report.counters.attachments_seen += len(attachments)
                    if self.settings.attachment_dir:
                        self._download_attachments(page_id, attachments, report)
                except Exception as exc:
                    self.conn.rollback()
                    self._error(report, page_id, "metadata-refresh", exc)

    def _extract(self, page_id: int, version: int, content: dict, report: SyncReport) -> None:
        storage = models.content_row(content)["body_storage"]
        space_key = (content.get("space") or {}).get("key")
        try:
            with self.conn.transaction():  # savepoint: source rows survive an extractor bug
                result = extract(
                    storage, page_id=page_id, space_key=space_key, base_url=self.client.base_url
                )
                repo.replace_extraction(self.conn, page_id, version, result, report.sync_run_id)
        except Exception as exc:
            self._error(report, page_id, "extract", exc)

    def _reextract_stored(self, page_id: int, version: int, report: SyncReport) -> None:
        """Re-derive extracted rows from the stored body (no network)."""
        storage = repo.storage_body(self.conn, page_id, version)
        try:
            with self.conn.transaction():
                result = extract(
                    storage,
                    page_id=page_id,
                    space_key=report.space_key,
                    base_url=self.client.base_url,
                )
                repo.replace_extraction(self.conn, page_id, version, result, report.sync_run_id)
            self.conn.commit()
            report.stats["reextracted"] = report.stats.get("reextracted", 0) + 1
        except Exception as exc:
            self.conn.rollback()
            self._error(report, page_id, "extract", exc)

    def _refresh_unchanged(self, item: dict, listed: dict, report: SyncReport) -> bool:
        """Apply listing metadata to an unchanged page; True if its other resources need a
        refresh (labels truncated in the listing, or refresh_unchanged_metadata)."""
        page_id = listed["page_id"]
        run_id = report.sync_run_id
        with self.conn.transaction():
            if repo.update_page_from_listing(self.conn, listed, run_id):
                report.counters.pages_moved += 1
            labels, complete = models.listing_labels(item)
            if complete:
                rows = models.label_rows(labels)
                if {(r["prefix"], r["name"]) for r in rows} != repo.page_label_set(
                    self.conn, page_id
                ):
                    repo.replace_labels(self.conn, page_id, rows, run_id)
        self.conn.commit()
        report.counters.pages_unchanged += 1
        return not complete or self.settings.refresh_unchanged_metadata

    def _handle_missing(
        self, report: SyncReport, space_id: int, known: dict[int, repo.KnownPage], seen: set[int]
    ) -> None:
        active = [p for p in known.values() if not p.is_deleted]
        missing = [p.page_id for p in active if p.page_id not in seen]
        if not missing:
            return
        if len(missing) / len(active) > self.settings.max_delete_fraction:
            report.errors.append(
                {
                    "stage": "deletions",
                    "error": f"{len(missing)} of {len(active)} known pages missing from listing "
                    f"exceeds max_delete_fraction={self.settings.max_delete_fraction}; "
                    "deletion phase skipped",
                }
            )
            return
        for page_id in missing:
            try:
                self._check_missing(page_id, space_id, report)
            except Exception as exc:
                self.conn.rollback()
                self._error(report, page_id, "deletion-check", exc)

    def _check_missing(self, page_id: int, space_id: int, report: SyncReport) -> None:
        run_id = report.sync_run_id
        try:
            content = self._probe(page_id)
        except NotFoundError:
            repo.mark_page_deleted(self.conn, page_id, "purged", run_id)
            report.counters.pages_deleted += 1
        except ForbiddenError:
            repo.mark_page_deleted(self.conn, page_id, "not_visible", run_id)
            report.counters.pages_deleted += 1
        else:
            status = content.get("status")
            new_space = content.get("space") or {}
            if status == "trashed":
                repo.mark_page_deleted(self.conn, page_id, "trashed", run_id, status="trashed")
                report.counters.pages_deleted += 1
            elif new_space.get("id") and int(new_space["id"]) != space_id:
                repo.move_page_to_space(
                    self.conn, page_id, repo.ensure_space_stub(self.conn, new_space), run_id
                )
                report.counters.pages_moved += 1
            else:
                report.stats.setdefault("missing_but_current", []).append(page_id)
        self.conn.commit()

    def _probe(self, page_id: int) -> dict:
        """Fetch a page in any status. Older DC releases reject ``status=any`` with 400, so
        fall back to asking for trashed content, then for current content."""
        last: ConfluenceError | None = None
        for status in ("any", "trashed", None):
            try:
                return self.client.get_content(
                    page_id, expand="space,version,ancestors", status=status
                )
            except NotFoundError as exc:
                if status != "trashed":  # "any" covers trashed; None is the last resort
                    raise
                last = exc
            except ConfluenceError as exc:
                if exc.status != 400:
                    raise
                last = exc
        raise last  # pragma: no cover - loop always returns or raises

    def _refresh_changed_attachments(self, report: SyncReport, cursor, fetched: set[int]) -> None:
        since = cursor - timedelta(minutes=self.settings.incremental_overlap_minutes)
        cql = (
            f"space = {cql_string(report.space_key)} and type = attachment "
            f'and lastmodified >= "{since:%Y-%m-%d %H:%M}"'
        )
        try:
            pages: set[int] = set()
            for att in self.client.search(cql, expand="container", limit=200):
                container = att.get("container") or {}
                if container.get("id") and str(container["id"]).isdigit():
                    pages.add(int(container["id"]))
        except ConfluenceError as exc:
            self._error(report, None, "attachment-search", exc)
            return
        candidates = sorted(pages - fetched)
        known = set()
        for batch in _chunks(candidates, 1000):
            rows = self.conn.execute(
                "SELECT page_id FROM source.pages WHERE page_id = ANY(%s)", (batch,)
            ).fetchall()
            known |= {r[0] for r in rows}
        self.conn.rollback()
        if known:
            self._refresh_resources(sorted(known), report, everything=False)

    def _download_attachments(
        self, page_id: int, attachments: list[dict], report: SyncReport
    ) -> None:
        root = Path(self.settings.attachment_dir).expanduser()
        for att in attachments:
            if not att.get("download_path"):
                continue
            version, path = repo.attachment_download_state(self.conn, att["attachment_id"])
            self.conn.rollback()
            if version == att["version_number"] and path and Path(path).is_file():
                continue
            name = _SAFE_NAME_RE.sub("_", att["title"]).strip("._") or "file"
            att_dir = _SAFE_NAME_RE.sub("_", att["attachment_id"]).strip("._") or "attachment"
            dest = root / str(page_id) / att_dir / name
            try:
                digest = self.client.download(att["download_path"], dest)
                repo.record_attachment_download(
                    self.conn, att["attachment_id"], att["version_number"], str(dest), digest
                )
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                self._error(report, page_id, "attachment-download", exc)


def reextract(
    conn: psycopg.Connection,
    *,
    base_url: str | None,
    space_key: str | None = None,
    page_ids: list[int] | None = None,
    all_versions: bool = False,
) -> dict[str, int]:
    """Rebuild extracted.* from stored source bodies, without any network access."""
    space_id = None
    if space_key:
        state = repo.space_state(conn, space_key)
        if state is None:
            raise ValueError(f"space {space_key} is not in the mirror")
        space_id = state["space_id"]
    targets = repo.page_versions_to_extract(conn, space_id, page_ids, all_versions)
    conn.rollback()
    done = failed = 0
    for page_id, version, key in targets:
        try:
            with conn.transaction():
                result = extract(
                    repo.storage_body(conn, page_id, version),
                    page_id=page_id,
                    space_key=key,
                    base_url=base_url,
                )
                repo.replace_extraction(conn, page_id, version, result, None)
            conn.commit()
            done += 1
        except Exception as exc:
            conn.rollback()
            failed += 1
            log.error("extraction failed for page %s v%s: %s", page_id, version, redact(str(exc)))
    resolved = repo.resolve_links(conn, space_id)
    conn.commit()
    return {"extracted": done, "failed": failed, "links_resolved": resolved}
