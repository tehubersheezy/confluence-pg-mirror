"""Bulk discovery and fetching: request budgets, scan, fallbacks and scoped syncs."""

import pytest

from confluence_mirror.config import SyncSettings
from confluence_mirror.sync import Scope, SpaceSync
from tests.fake_confluence import FakeAttachment, FakeConfluence, demo_space

from .helpers import client_for, rows, scalar, sync

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(rich_storage):
    f = FakeConfluence()
    demo_space(f, rich_storage)
    return f


def big_space(n: int = 120) -> FakeConfluence:
    f = FakeConfluence()
    f.add_space("BIG", 97001, "Big Space", homepage=50000)
    f.add_page(50000, "BIG", "Home", None, "<p>home</p>", properties={"owner": {"team": "x"}})
    for i in range(1, n):
        f.add_page(
            50000 + i, "BIG", f"Page {i}", 50000, f"<p>page {i}</p>", position=i, labels=["bulk"]
        )
    return f


def requests_by_kind(fake):
    out = {}
    for r in fake.requests:
        path = r.url.path
        kind = "single" if path.rstrip("/").split("/")[-1].isdigit() else path.rsplit("/", 1)[-1]
        out[kind] = out.get(kind, 0) + 1
    return out


def test_full_sync_uses_a_handful_of_requests(conn):
    f = big_space(120)
    report = sync(conn, f, space="BIG", page_properties=("owner",))
    assert report.status == "succeeded", report.errors
    assert report.counters.pages_fetched == 120
    kinds = requests_by_kind(f)
    # helpers.client_for uses page_size=2; listing_page_size=200 makes the scan one page.
    assert kinds["scan"] == 1
    assert kinds["search"] == 3  # ceil(120 / 50) body batches
    assert "single" not in kinds
    assert kinds.get("property", 0) == 1  # the space's properties; pages' come in the batches
    assert len(f.requests) <= 8  # space, space properties, scan, 3 batches
    assert report.stats["listing"] == {"page": "scan"}
    assert report.stats["http_requests"]["/rest/api/content/search"] == 3
    # Properties named in page_properties come from the bulk expansion.
    assert rows(conn, "SELECT property_key FROM source.page_properties") == [("owner",)]
    assert scalar(conn, "SELECT count(*) FROM source.page_labels") == 119
    # An unchanged second run lists and fetches nothing else.
    before = len(f.requests)
    report = sync(conn, f, space="BIG", page_properties=("owner",))
    assert report.mode == "incremental" and report.counters.pages_unchanged == 120
    assert requests_by_kind(f).get("search", 0) - kinds["search"] <= 1  # attachment CQL only
    assert len(f.requests) - before <= 4


def test_properties_all_costs_one_request_per_page(conn):
    f = big_space(10)
    sync(conn, f, space="BIG")
    assert requests_by_kind(f)["property"] == 1 + 10  # space properties + one per page


@pytest.mark.parametrize("style", ["cursor", "link"])
def test_scan_follows_cursors_and_links(conn, style):
    f = big_space(23)
    f.scan_style = style
    report = sync(conn, f, space="BIG", listing_page_size=5, page_properties="none")
    assert report.counters.pages_listed == 23 and report.status == "succeeded"
    assert requests_by_kind(f)["scan"] == 5


def test_servers_without_scan_fall_back_to_offset_listing(conn, fake):
    fake.scan_style = None
    report = sync(conn, fake)
    assert report.status == "succeeded" and report.stats["listing"] == {"page": "legacy"}
    with pytest.raises(Exception, match="404"):
        sync(conn, fake, listing="scan")


def test_truncated_expansions_fall_back_per_page(conn, fake):
    fake.expansion_limit = 3
    fake.pages[1002].attachments = [
        FakeAttachment(f"att9{i:02d}", f"file{i}.txt") for i in range(5)
    ]
    fake.pages[1002].labels = [f"l{i}" for i in range(4)]
    report = sync(conn, fake)
    assert report.status == "succeeded", report.errors
    assert scalar(conn, "SELECT count(*) FROM source.attachments WHERE page_id = 1002") == 5
    assert scalar(conn, "SELECT count(*) FROM source.page_labels WHERE page_id = 1002") == 4
    assert report.stats["attachment_fallbacks"] == 1
    assert report.stats["label_fallbacks"] >= 1


def test_page_missing_from_a_batch_is_fetched_alone(conn, fake):
    real = fake._cql

    def lossy(cql, expand):
        return [c for c in real(cql, expand) if not (cql.startswith("id in") and c["id"] == "1003")]

    fake._cql = lossy
    report = sync(conn, fake)
    assert report.status == "succeeded"
    assert report.stats["single_fetches"] == 1
    assert scalar(conn, "SELECT count(*) FROM source.page_content WHERE page_id = 1003") == 1


def test_unchanged_metadata_refresh_is_bulk(conn, fake):
    sync(conn, fake)
    fake.pages[1004].attachments.append(FakeAttachment("att7001", "late.txt"))
    before = len(fake.requests)
    report = sync(conn, fake, refresh_unchanged_metadata=True)
    assert report.status == "succeeded"
    new = fake.requests[before:]
    assert not [r for r in new if r.url.path.endswith(("/label", "/child/attachment"))]
    assert scalar(conn, "SELECT count(*) FROM source.attachments WHERE page_id = 1004") == 1


def test_tree_cql_and_page_scopes(conn, fake):
    syncer = SpaceSync(conn, client_for(fake), SyncSettings())
    reports, missing = syncer.run_scope(Scope("tree", root=1002))
    assert missing == [] and len(reports) == 1
    assert reports[0].mode == "pages" and reports[0].counters.pages_fetched == 2
    assert {r[0] for r in rows(conn, "SELECT page_id FROM source.pages")} == {1002, 1003}
    assert reports[0].stats["scope"] == {"kind": "tree", "root": 1002, "full": False}
    # Scoped syncs never advance the cursor or look for deletions.
    assert scalar(conn, "SELECT last_successful_sync_at FROM source.spaces") is None

    reports, _ = syncer.run_scope(Scope("tree", root=1002))
    assert reports[0].counters.pages_unchanged == 2 and reports[0].counters.pages_fetched == 0
    reports, _ = syncer.run_scope(Scope("tree", root=1002, full=True))
    assert reports[0].counters.pages_fetched == 2

    reports, _ = syncer.run_scope(Scope("cql", cql='label = "root"'))
    assert reports[0].counters.pages_fetched == 1
    assert scalar(conn, "SELECT count(*) FROM source.pages WHERE page_id = 1001") == 1

    reports, missing = syncer.run_scope(Scope("pages", page_ids=(1004, 2001, 424242)))
    assert missing == [424242]
    assert sorted(r.space_key for r in reports) == ["DEMO", "OTHER"]
    reports, missing = syncer.run_scope(Scope("tree", root=424242))
    assert reports == [] and missing == [424242]
