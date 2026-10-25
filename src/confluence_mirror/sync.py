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
    ConfluenceClient,
    ConfluenceError,
    ForbiddenError,
    NotFoundError,
    content_expand,
)
from confluence_mirror.config import SyncSettings
from confluence_mirror.extract import EXTRACTOR_VERSION, extract
from confluence_mirror.logging_utils import redact

log = logging.getLogger(__name__)

MAX_RECORDED_ERRORS = 500
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Global keys are alphanumeric; personal spaces are "~username".
SPACE_KEY_RE = re.compile(r"^~?[A-Za-z0-9_.\-@]{1,255}$")


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
        self._expand = content_expand(settings.representations)

    # -- entry point ----------------------------------------------------------------

    def run(self, space_key: str, mode: str = "auto") -> SyncReport:
        if mode not in ("auto", "full", "incremental"):
            raise ValueError(f"unknown mode {mode}")
        return self._locked(space_key, lambda: self._run_locked(space_key, mode))

    def run_pages(self, space_key: str, page_ids: list[int]) -> SyncReport:
        """Fetch selected pages of one space (mode ``pages``).

        No deletion phase and no cursor advance: a page that cannot be fetched is an error
        for this run, never evidence of deletion.
        """
        return self._locked(space_key, lambda: self._run_scoped(space_key, "pages", page_ids))

    def run_metadata(self, space_key: str) -> SyncReport:
        """List the space and record page metadata only (mode ``metadata``); no bodies."""
        return self._locked(space_key, lambda: self._run_scoped(space_key, "metadata", None))

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
        try:
            return body()
        finally:
            self.conn.rollback()
            self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
            self.conn.commit()

    def _run_scoped(self, space_key: str, mode: str, page_ids: list[int] | None) -> SyncReport:
        run_id = repo.start_run(self.conn, space_key, mode, None)
        self.conn.commit()
        report = SyncReport(run_id, space_key, mode, "running", Counters())
        try:
            space = models.space_row(self.client.get_space(space_key))
            repo.upsert_space(self.conn, space, run_id)
            self.conn.commit()
            if mode == "pages":
                for page_id in page_ids or []:
                    try:
                        self._fetch_page(page_id, space["space_id"], report)
                    except Exception as exc:
                        self.conn.rollback()
                        report.counters.pages_failed += 1
                        self._error(report, page_id, "page", exc)
                repo.resolve_links(self.conn, space["space_id"])
            else:
                for content_type in self.settings.content_types:
                    for item in self.client.list_content(space_key, content_type):
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
        props = [models.property_row(p) for p in self.client.space_properties(report.space_key)]
        repo.replace_space_properties(self.conn, space_id, props, run_id)
        self.conn.commit()

        fetched: set[int] = set()
        for content_type in self.settings.content_types:
            listing = list(self.client.list_content(report.space_key, content_type))
            report.counters.pages_listed += len(listing)
            # Parents first, so hierarchy and same-space links fill in top-down.
            pairs = sorted(
                ((item, models.page_row(item)) for item in listing),
                key=lambda p: (p[1]["depth"], p[1]["position"] or 0, p[1]["page_id"]),
            )
            known = repo.known_pages(self.conn, space_id, content_type)
            self.conn.rollback()
            seen: set[int] = set()
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
                try:
                    if needs_fetch:
                        self._fetch_page(page_id, space_id, report)
                        fetched.add(page_id)
                    else:
                        self._refresh_unchanged(item, listed, report)
                        if prior.extractor_version != EXTRACTOR_VERSION:
                            self._reextract_stored(page_id, prior.current_version, report)
                except Exception as exc:  # one page must not stop the space
                    self.conn.rollback()
                    report.counters.pages_failed += 1
                    self._error(report, page_id, "page", exc)
            self._handle_missing(report, space_id, known, seen)

        if not full and cursor is not None:
            self._refresh_changed_attachments(report, cursor, fetched)

        resolved = repo.resolve_links(self.conn, space_id)
        report.stats["links_resolved"] = resolved
        if not report.errors:
            repo.mark_space_synced(self.conn, space_id, run_id, full)
        self.conn.commit()

    def _fetch_page(self, page_id: int, space_id: int, report: SyncReport) -> None:
        run_id = report.sync_run_id
        # Network first, then one short transaction, so no transaction spans HTTP calls.
        content = self.client.get_content(page_id, expand=self._expand)
        labels = models.label_rows(list(self.client.labels(page_id)))
        props = [models.property_row(p) for p in self.client.content_properties(page_id)]
        attachments = [models.attachment_row(a, page_id) for a in self.client.attachments(page_id)]
        restrictions, complete = models.restriction_rows(content)
        if not complete:
            restrictions, complete = models.restriction_rows(
                {"restrictions": self.client.restrictions(page_id)}
            )
            if not complete:
                report.stats.setdefault("restrictions_incomplete", []).append(page_id)

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
            repo.replace_page_properties(self.conn, page_id, props, run_id)
            repo.replace_restrictions(self.conn, page_id, restrictions, run_id)
            repo.sync_attachments(self.conn, page_id, attachments, run_id)
            self._extract(page_id, page["current_version"], content, report)
        self.conn.commit()
        report.counters.pages_fetched += 1
        report.counters.attachments_seen += len(attachments)
        if self.settings.attachment_dir:
            self._download_attachments(page_id, attachments, report)

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

    def _refresh_unchanged(self, item: dict, listed: dict, report: SyncReport) -> None:
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
        if not complete or self.settings.refresh_unchanged_metadata:
            self._refresh_metadata(
                page_id,
                report,
                labels=not complete,
                everything=self.settings.refresh_unchanged_metadata,
            )
        report.counters.pages_unchanged += 1

    def _refresh_metadata(
        self, page_id: int, report: SyncReport, *, labels: bool, everything: bool
    ) -> None:
        run_id = report.sync_run_id
        label_rows = (
            models.label_rows(list(self.client.labels(page_id))) if labels or everything else None
        )
        props = attachments = restrictions = None
        if everything:
            props = [models.property_row(p) for p in self.client.content_properties(page_id)]
            attachments = [
                models.attachment_row(a, page_id) for a in self.client.attachments(page_id)
            ]
            restrictions, _ = models.restriction_rows(
                {"restrictions": self.client.restrictions(page_id)}
            )
        with self.conn.transaction():
            if label_rows is not None:
                repo.replace_labels(self.conn, page_id, label_rows, run_id)
            if props is not None:
                repo.replace_page_properties(self.conn, page_id, props, run_id)
            if restrictions is not None:
                repo.replace_restrictions(self.conn, page_id, restrictions, run_id)
            if attachments is not None:
                repo.sync_attachments(self.conn, page_id, attachments, run_id)
        self.conn.commit()
        if attachments is not None:
            report.counters.attachments_seen += len(attachments)

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
            f'space = "{report.space_key}" and type = attachment '
            f'and lastmodified >= "{since:%Y-%m-%d %H:%M}"'
        )
        try:
            pages: set[int] = set()
            for att in self.client.search(cql, expand="container"):
                container = att.get("container") or {}
                if container.get("id") and str(container["id"]).isdigit():
                    pages.add(int(container["id"]))
        except ConfluenceError as exc:
            self._error(report, None, "attachment-search", exc)
            return
        for page_id in sorted(pages - fetched):
            exists = self.conn.execute(
                "SELECT 1 FROM source.pages WHERE page_id = %s", (page_id,)
            ).fetchone()
            self.conn.rollback()
            if not exists:
                continue
            try:
                attachments = [
                    models.attachment_row(a, page_id) for a in self.client.attachments(page_id)
                ]
                with self.conn.transaction():
                    repo.sync_attachments(self.conn, page_id, attachments, report.sync_run_id)
                self.conn.commit()
                report.counters.attachments_seen += len(attachments)
                if self.settings.attachment_dir:
                    self._download_attachments(page_id, attachments, report)
            except Exception as exc:
                self.conn.rollback()
                self._error(report, page_id, "attachments", exc)

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
