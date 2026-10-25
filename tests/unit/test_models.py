from datetime import UTC, datetime

from confluence_mirror import models
from tests.fake_confluence import FakeConfluence, demo_space


def fake_content(expand="body.storage,body.view,body.export_view,restrictions"):
    fake = FakeConfluence()
    demo_space(fake, "<p>root</p>")
    return fake, fake.content_json(fake.pages[1003], expand)


def test_parse_ts():
    assert models.parse_ts("2024-01-02T03:04:05.000Z") == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert models.parse_ts("2024-01-02T03:04:05.000+02:00").utcoffset().total_seconds() == 7200
    assert models.parse_ts(None) is None
    assert models.parse_ts("garbage") is None


def test_page_row_hierarchy():
    _, content = fake_content()
    row = models.page_row(content)
    assert row["page_id"] == 1003
    assert row["parent_id"] == 1002
    assert row["ancestor_ids"] == [1001, 1002]
    assert row["depth"] == 2
    assert row["position"] == 0
    assert row["created_by"] == "key-author"
    assert "body" not in row["raw"] and "restrictions" not in row["raw"]


def test_root_page_has_no_parent_and_no_position():
    fake, _ = fake_content()
    row = models.page_row(fake.content_json(fake.pages[1001], ""))
    assert row["parent_id"] is None and row["depth"] == 0 and row["position"] is None


def test_content_row_bodies_and_hash():
    _, content = fake_content()
    row = models.content_row(content)
    assert row["body_storage"].startswith("<table>")
    assert row["body_view"].startswith("<div class='view'>")
    assert row["body_export_view"].startswith("<div class='export'>")
    assert row["storage_sha256"] == models.sha256_text(row["body_storage"])


def test_restrictions():
    fake, _ = fake_content()
    rows, complete = models.restriction_rows(fake.content_json(fake.pages[1002], "restrictions"))
    assert complete
    assert {(r["operation"], r["subject_type"], r["subject_id"]) for r in rows} == {
        ("read", "user", "key-alice"),
        ("read", "group", "team-a"),
    }
    assert models.restriction_rows({"id": "1"}) == ([], False)


def test_listing_labels_truncation():
    labels, complete = models.listing_labels(
        {"metadata": {"labels": {"results": [{"name": "a"}], "size": 1, "limit": 200}}}
    )
    assert complete and labels == [{"name": "a"}]
    _, complete = models.listing_labels(
        {"metadata": {"labels": {"results": [{"name": "a"}], "size": 1, "limit": 1}}}
    )
    assert not complete
    assert models.listing_labels({}) == ([], False)


def test_label_rows_dedupe():
    rows = models.label_rows(
        [
            {"prefix": "global", "name": "x", "id": 1},
            {"name": "x"},
            {"prefix": "my", "name": "x"},
            {},
        ]
    )
    assert sorted((r["prefix"], r["name"]) for r in rows) == [("global", "x"), ("my", "x")]


def test_attachment_row():
    fake, _ = fake_content()
    page = fake.pages[1001]
    row = models.attachment_row(fake.attachment_json(page, page.attachments[0]), 1001)
    assert row["attachment_id"] == "att5001"
    assert row["media_type"] == "image/png"
    assert row["file_size"] == 4
    assert row["download_path"].startswith("/download/attachments/1001/diagram.png")
