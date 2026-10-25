from pathlib import Path

import pytest

from confluence_mirror.sync import SyncLockedError
from tests.fake_confluence import TOKEN, FakeAttachment, FakeConfluence, demo_space

from .helpers import client_for, rows, scalar, sync

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(rich_storage):
    f = FakeConfluence()
    demo_space(f, rich_storage)
    return f


def body_fetches(fake):
    """Requests that carried page bodies (bulk batches and single-page fallbacks)."""
    return fake.count(r"/rest/api/content(/\d+|/search)$", param=("expand", "body.storage"))


def test_full_sync_mirrors_space(conn, fake):
    report = sync(conn, fake)
    assert report.mode == "full" and report.status == "succeeded", report.errors
    assert report.counters.pages_listed == 5 and report.counters.pages_fetched == 5

    space = rows(
        conn,
        "SELECT space_key, name, description_plain, homepage_id, sync_enabled, "
        "last_successful_sync_at IS NOT NULL FROM source.spaces "
        "WHERE space_key = 'DEMO'",
    )[0]
    assert space == ("DEMO", "Demo Space", "Synthetic DEMO space", 1001, True, True)
    assert rows(conn, "SELECT property_key, value FROM source.space_properties") == [
        ("team", {"name": "Synthetic"})
    ]

    hierarchy = rows(
        conn, "SELECT page_id, parent_id, ancestor_ids, depth FROM source.pages ORDER BY page_id"
    )
    assert hierarchy == [
        (1001, None, [], 0),
        (1002, 1001, [1001], 1),
        (1003, 1002, [1001, 1002], 2),
        (1004, 1001, [1001], 1),
        (1005, 1001, [1001], 1),
    ]
    # Parent pages carry their own content (all three representations).
    root = rows(
        conn,
        "SELECT body_storage IS NOT NULL, body_view LIKE '<div class=''view''>%', "
        "body_export_view LIKE '<div class=''export''>%' FROM source.page_content "
        "WHERE page_id = 1001",
    )
    assert root == [(True, True, True)]
    assert scalar(conn, "SELECT count(*) FROM source.page_versions") == 5

    assert rows(conn, "SELECT name FROM source.page_labels WHERE page_id = 1001 ORDER BY name") == [
        ("overview",),
        ("root",),
    ]
    assert rows(conn, "SELECT property_key, value FROM source.page_properties") == [
        ("owner", {"team": "docs"})
    ]
    assert rows(
        conn, "SELECT attachment_id, page_id, title, media_type, file_size FROM source.attachments"
    ) == [("att5001", 1001, "diagram.png", "image/png", 4)]
    assert sorted(
        rows(
            conn,
            "SELECT page_id, operation, subject_type, subject_id FROM source.access_restrictions",
        )
    ) == [
        (1001, "update", "group", "doc-editors"),
        (1002, "read", "group", "team-a"),
        (1002, "read", "user", "key-alice"),
    ]

    run = rows(conn, "SELECT status, mode, pages_fetched, errors FROM sync.sync_runs")
    assert run == [("succeeded", "full", 5, [])]


def test_extraction_and_link_resolution(conn, fake):
    sync(conn, fake)
    macros = rows(
        conn,
        "SELECT macro_name, parent_macro_index FROM extracted.page_macros "
        "WHERE page_id = 1001 ORDER BY macro_index",
    )
    assert macros == [
        ("info", None),
        ("code", 0),
        ("details", None),
        ("status", 2),
        ("include", None),
    ]
    assert (
        scalar(
            conn,
            "SELECT data->>'language' FROM extracted.macro_extractions "
            "WHERE page_id = 1001 AND extraction_type = 'code'",
        )
        == "python"
    )
    assert scalar(conn, "SELECT records FROM extracted.page_tables WHERE page_id = 1003") == [
        {"K": "a", "V": "1"}
    ]
    links = dict(
        rows(
            conn,
            "SELECT target_title, target_page_id FROM extracted.page_links "
            "WHERE page_id = 1001 AND target_title IS NOT NULL "
            "AND target_filename IS NULL",
        )
    )
    # Same-space titles resolve; OTHER is not mirrored so "Elsewhere" stays unresolved.
    assert links == {
        "Child Page": 1002,
        "Root Page": 1001,
        "Shared Snippet": 1005,
        "Elsewhere": None,
    }
    # Child (synced before the parent it links to? no: parents first) links back to root.
    assert (
        scalar(conn, "SELECT target_page_id FROM extracted.page_links WHERE page_id = 1002") == 1001
    )
    in_mirror = rows(
        conn,
        "SELECT target_page_id, target_in_mirror FROM "
        "extracted.current_page_links WHERE page_id = 1001 AND source_element"
        " = 'a' AND target_page_id = 1003",
    )
    assert in_mirror == [(1003, True)]


def test_resync_is_idempotent(conn, fake):
    sync(conn, fake)
    tables = [
        "source.pages",
        "source.page_versions",
        "source.page_content",
        "source.page_labels",
        "source.page_properties",
        "source.attachments",
        "source.access_restrictions",
        "extracted.page_macros",
        "extracted.macro_extractions",
        "extracted.page_tables",
        "extracted.page_links",
    ]
    before = {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in tables}
    report = sync(conn, fake, mode="full")
    assert report.status == "succeeded"
    assert {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in tables} == before


def test_incremental_fetches_only_changes_and_keeps_history(conn, fake):
    sync(conn, fake)
    fetched_before = body_fetches(fake)
    fake.edit(
        1004,
        body='<p>Now links <ac:link><ri:page ri:content-title="Child Page"/></ac:link></p>',
        title="Sibling Renamed",
    )
    report = sync(conn, fake)
    assert report.mode == "incremental" and report.status == "succeeded", report.errors
    assert report.counters.pages_fetched == 1 and report.counters.pages_unchanged == 4
    assert body_fetches(fake) - fetched_before == 1

    assert rows(
        conn,
        "SELECT version_number, title FROM source.page_versions WHERE page_id = 1004 ORDER BY 1",
    ) == [(1, "Sibling Page"), (2, "Sibling Renamed")]
    assert scalar(conn, "SELECT count(*) FROM source.page_content WHERE page_id = 1004") == 2
    assert scalar(conn, "SELECT current_version FROM source.pages WHERE page_id = 1004") == 2
    # Extraction is per version; the current-view shows only v2's link.
    assert rows(
        conn,
        "SELECT version_number, target_page_id FROM extracted.current_page_links "
        "WHERE page_id = 1004",
    ) == [(2, 1002)]
    assert (
        scalar(
            conn,
            "SELECT cursor_from IS NOT NULL FROM sync.sync_runs ORDER BY sync_run_id DESC LIMIT 1",
        )
        is True
    )


def test_label_change_without_version_bump(conn, fake):
    sync(conn, fake)
    fake.pages[1002].labels = ["child", "reviewed"]
    sync(conn, fake)
    assert rows(conn, "SELECT name FROM source.page_labels WHERE page_id = 1002 ORDER BY 1") == [
        ("child",),
        ("reviewed",),
    ]
    fake.pages[1002].labels = []
    sync(conn, fake)
    assert scalar(conn, "SELECT count(*) FROM source.page_labels WHERE page_id = 1002") == 0


def test_move_within_space(conn, fake):
    sync(conn, fake)
    fake.pages[1003].parent_id = 1004  # moves don't bump the version
    report = sync(conn, fake)
    assert report.counters.pages_moved == 1 and report.counters.pages_fetched == 0
    assert rows(
        conn,
        "SELECT parent_id, previous_parent_id, ancestor_ids, moved_at IS NOT NULL "
        "FROM source.pages WHERE page_id = 1003",
    ) == [(1004, 1002, [1001, 1004], True)]


def test_deletions_trash_purge_permission_and_move_out(conn, fake):
    sync(conn, fake)
    fake.pages[1003].status = "trashed"
    fake.pages[1004].status = "purged"
    fake.pages[1005].space_key = "OTHER"
    report = sync(conn, fake, max_delete_fraction=0.9)
    assert report.status == "succeeded", report.errors
    assert report.counters.pages_deleted == 2 and report.counters.pages_moved == 1
    state = dict(
        (r[0], r[1:])
        for r in rows(
            conn, "SELECT page_id, is_deleted, deletion_reason, space_id FROM source.pages"
        )
    )
    assert state[1003] == (True, "trashed", 98001)
    assert state[1004] == (True, "purged", 98001)
    assert state[1005] == (True, "moved_out", 98002)
    assert state[1001] == (False, None, 98001)
    # History is retained; the current views hide deleted pages.
    assert scalar(conn, "SELECT count(*) FROM source.page_content WHERE page_id = 1003") == 1
    assert (
        scalar(conn, "SELECT count(*) FROM extracted.current_page_tables WHERE page_id = 1003") == 0
    )
    # Syncing the target space picks the moved page up again.
    sync(conn, fake, space="OTHER")
    assert rows(conn, "SELECT is_deleted, space_id FROM source.pages WHERE page_id = 1005") == [
        (False, 98002)
    ]


def test_restored_page_is_undeleted(conn, fake):
    sync(conn, fake)
    fake.pages[1004].status = "trashed"
    sync(conn, fake)
    fake.pages[1004].status = "current"
    sync(conn, fake)
    assert scalar(conn, "SELECT is_deleted FROM source.pages WHERE page_id = 1004") is False


def test_deletion_guard(conn, fake):
    sync(conn, fake)
    fake.hide_from_listing = {1002, 1003, 1004, 1005}
    report = sync(conn, fake)
    assert report.status == "partial"
    assert report.errors[0]["stage"] == "deletions"
    assert scalar(conn, "SELECT count(*) FROM source.pages WHERE is_deleted") == 0


def test_page_failure_is_isolated_and_retried(conn, fake):
    # The bulk batch fails, so pages are fetched one by one, and one of those fails too.
    fake.fail(r"/rest/api/content/search$", 500, 500, 500)
    fake.fail(r"/rest/api/content/1003$", 500, 500, 500, 500, 500, 500)
    report = sync(conn, fake)
    assert report.status == "partial"
    assert report.counters.pages_failed == 1 and report.counters.pages_fetched == 4
    assert report.errors[0]["page_id"] == 1003 and report.errors[0]["http_status"] == 500
    assert scalar(conn, "SELECT count(*) FROM source.pages WHERE page_id = 1003") == 0
    assert (
        scalar(conn, "SELECT last_successful_sync_at FROM source.spaces WHERE space_key = 'DEMO'")
        is None
    )
    # Failure cleared: a partial run never advanced the cursor, so this is another full run.
    fake.failures.clear()
    report = sync(conn, fake)
    assert report.status == "succeeded" and report.mode == "full"
    assert scalar(conn, "SELECT count(*) FROM source.pages WHERE page_id = 1003") == 1


def test_transient_errors_are_retried(conn, fake):
    fake.fail(r"/rest/api/content/1002/property$", 503)
    report = sync(conn, fake)
    assert report.status == "succeeded"


def test_space_level_failure_marks_run_failed(conn, fake):
    fake.fail(r"/rest/api/space/DEMO$", 404)
    with pytest.raises(Exception, match="404"):
        sync(conn, fake)
    assert rows(conn, "SELECT status, jsonb_array_length(errors) FROM sync.sync_runs") == [
        ("failed", 1)
    ]


def test_incremental_picks_up_new_attachments_via_cql(conn, fake):
    sync(conn, fake)
    fake.pages[1004].attachments.append(
        FakeAttachment("att5002", "notes.txt", "text/plain", b"hello", modified=fake.tick())
    )
    report = sync(conn, fake)
    assert report.counters.pages_fetched == 0
    assert (
        scalar(conn, "SELECT page_id FROM source.attachments WHERE attachment_id = 'att5002'")
        == 1004
    )
    fake.pages[1004].attachments.clear()
    fake.edit(1004)
    sync(conn, fake)
    assert (
        scalar(conn, "SELECT is_deleted FROM source.attachments WHERE attachment_id = 'att5002'")
        is True
    )


def test_attachment_download(conn, fake, tmp_path):
    sync(conn, fake, attachment_dir=str(tmp_path))
    path, digest, version = rows(
        conn, "SELECT local_path, content_sha256, downloaded_version FROM source.attachments"
    )[0]
    assert Path(path).read_bytes() == b"\x89PNG" and len(digest) == 64 and version == 1
    downloads = fake.count(r"^/download/")
    sync(conn, fake, mode="full", attachment_dir=str(tmp_path))
    assert fake.count(r"^/download/") == downloads  # unchanged version not re-downloaded


def test_concurrent_sync_of_same_space_is_refused(conn, fake, pg_dsn):
    from confluence_mirror import db

    other = db.connect(pg_dsn)
    try:
        other.execute("SELECT pg_advisory_lock(hashtext('confluence-mirror:space:DEMO'))")
        with pytest.raises(SyncLockedError):
            sync(conn, fake)
    finally:
        other.close()


def test_abandoned_runs_are_marked(conn, fake):
    conn.execute(
        "INSERT INTO sync.sync_runs (space_key, mode, tool_version) VALUES ('DEMO', 'full', 'x')"
    )
    conn.commit()
    sync(conn, fake)
    assert rows(conn, "SELECT status FROM sync.sync_runs ORDER BY sync_run_id") == [
        ("abandoned",),
        ("succeeded",),
    ]


def test_blogposts_optional(conn, fake):
    fake.add_page(3001, "DEMO", "News", None, "<p>blog</p>", type="blogpost")
    sync(conn, fake, content_types=("page", "blogpost"))
    assert scalar(conn, "SELECT content_type FROM source.pages WHERE page_id = 3001") == "blogpost"


def test_context_path_instance(conn, rich_storage):
    fake = FakeConfluence("https://confluence.example.com/wiki")
    demo_space(fake, rich_storage)
    assert sync(conn, fake).status == "succeeded"
    assert scalar(conn, "SELECT count(*) FROM source.pages") == 5


def test_no_writes_and_no_token_in_database(conn, fake):
    sync(conn, fake)
    assert {r.method for r in fake.requests} == {"GET"}
    dump = "".join(
        str(r)
        for t in (
            "source.spaces",
            "source.pages",
            "source.page_versions",
            "source.attachments",
            "sync.sync_runs",
        )
        for r in rows(conn, f"SELECT * FROM {t}")
    )
    assert TOKEN not in dump


def test_client_helper_sends_bearer(fake):
    client_for(fake).current_user()
    assert fake.requests[-1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_stale_extraction_is_rebuilt_offline(conn, fake):
    sync(conn, fake)
    conn.execute("UPDATE source.page_content SET extractor_version = '0.0' WHERE page_id = 1003")
    conn.execute("DELETE FROM extracted.page_tables WHERE page_id = 1003")
    conn.commit()
    fetched = body_fetches(fake)
    report = sync(conn, fake)
    assert report.stats["reextracted"] == 1
    assert body_fetches(fake) == fetched  # rebuilt from the stored body
    assert scalar(conn, "SELECT count(*) FROM extracted.page_tables WHERE page_id = 1003") == 1


def test_extractor_failure_keeps_source_and_is_retried(conn, fake, monkeypatch):
    from confluence_mirror import sync as sync_module

    real = sync_module.extract

    def broken(storage, **kw):
        if kw.get("page_id") == 1004:
            raise RuntimeError("extractor bug")
        return real(storage, **kw)

    monkeypatch.setattr(sync_module, "extract", broken)
    report = sync(conn, fake)
    assert report.status == "partial"
    assert report.errors == [
        {"stage": "extract", "error": "RuntimeError: extractor bug", "page_id": 1004}
    ]
    # Source data was committed even though extraction failed.
    assert (
        scalar(conn, "SELECT extractor_version FROM source.page_content WHERE page_id = 1004")
        is None
    )
    monkeypatch.setattr(sync_module, "extract", real)
    report = sync(conn, fake)
    assert report.status == "succeeded"
    assert (
        scalar(
            conn,
            "SELECT extractor_version IS NOT NULL FROM source.page_content WHERE page_id = 1004",
        )
        is True
    )


def test_status_any_rejected_falls_back(conn, fake):
    sync(conn, fake)
    fake.pages[1003].status = "trashed"
    real = fake.handle

    def reject_any(request):
        if request.url.params.get("status") == "any":
            return __import__("httpx").Response(400, json={"message": "bad status"})
        return real(request)

    fake.transport.handler = reject_any
    report = sync(conn, fake)
    assert report.status == "succeeded", report.errors
    assert (
        scalar(conn, "SELECT deletion_reason FROM source.pages WHERE page_id = 1003") == "trashed"
    )


def test_invalid_space_key_rejected(conn, fake):
    with pytest.raises(ValueError, match="invalid space key"):
        sync(conn, fake, space='DEMO" or space = "X')
