import json

import pytest

from confluence_mirror import confex, inspection
from confluence_mirror.client import ConfluenceClient
from confluence_mirror.confex import core
from tests.fake_confluence import TOKEN, FakeConfluence, demo_space

from ..conftest import FIXTURES
from .helpers import rows, scalar, sync

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(rich_storage):
    f = FakeConfluence()
    demo_space(f, rich_storage)
    geometry = (FIXTURES / "storage" / "geometry_page.xml").read_text(encoding="utf-8")
    f.add_page(1006, "DEMO", "Servers", 1002, geometry, position=5, labels=["inventory"])
    return f


@pytest.fixture
def env(conn, fake, pg_dsn, monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[confluence]\nbase_url = "https://confluence.example.com"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)
    monkeypatch.setenv("CONFLUENCE_PAT", TOKEN)

    def client(settings, token):
        return ConfluenceClient(settings, token, transport=fake.transport, sleep=lambda s: None)

    monkeypatch.setattr(core, "make_client", client)
    return fake


def run(capsys, *argv):
    code = confex.main(["--format", "json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def requests_made(fake):
    return len(fake.requests)


def test_page_metadata_commands(env, conn, capsys):
    assert sync(conn, env).status == "succeeded"
    before = requests_made(env)

    code, e = run(capsys, "pages", "labels", "1001")
    assert [x["name"] for x in e["data"]] == ["overview", "root"]
    code, e = run(capsys, "pages", "properties", "1001", "--key", "owner")
    assert e["data"][0]["value"] == {"team": "docs"}
    code, e = run(capsys, "pages", "properties", "1001", "--key", "absent")
    assert code == inspection.EXIT_NOT_FOUND
    code, e = run(capsys, "pages", "attachments", "1001")
    assert e["data"]["attachments"][0]["title"] == "diagram.png"
    code, e = run(capsys, "pages", "versions", "1001")
    assert e["data"]["versions"][0]["body_stored"] is True
    code, e = run(capsys, "pages", "macros", "1001", "--name", "info")
    assert [m["macro_name"] for m in e["data"]["macros"]] == ["info"]
    code, e = run(capsys, "pages", "links", "1002", "--direction", "both")
    assert e["data"]["outbound"][0]["target_page_id"] == 1001
    assert {x["source_page_id"] for x in e["data"]["inbound"]} == {1001}
    assert "not a complete backlink index" in e["data"]["inbound_coverage"]

    assert requests_made(env) == before  # mirror commands make no requests


def test_read_sections_and_raw(env, conn, capsys):
    sync(conn, env)
    code, e = run(capsys, "pages", "read", "1006", "--representation", "storage")
    blocks = e["data"]["blocks"]
    kinds = [b["kind"] for b in blocks]
    assert kinds[:4] == ["heading", "heading", "paragraph", "table"]
    # Nested-table and cell content is referenced, not flattened into prose.
    assert not any(b.get("text") == "see below" for b in blocks)
    groups = next(b for b in blocks if b.get("text") == "Groups")
    code, e = run(
        capsys, "pages", "read", "1006", "--representation", "storage", "--section-id", groups["id"]
    )
    section = e["data"]["blocks"]
    assert section[0]["text"] == "Groups" and section[1]["kind"] == "table"
    assert all(b.get("text") != "Servers" for b in section)

    code, e = run(capsys, "pages", "read", "1001", "--representation", "storage")
    preamble = e["data"]["preamble_section_id"]
    code, e = run(
        capsys, "pages", "read", "1001", "--representation", "storage", "--section-id", preamble
    )
    assert e["data"]["blocks"] == []  # the root page starts with a heading

    code, e = run(capsys, "pages", "read", "1003", "--mode", "raw", "--representation", "storage")
    assert e["data"]["body"].startswith("<table>")

    assert confex.main(["pages", "read", "1006", "--representation", "storage"]) == 0
    text = capsys.readouterr().out
    assert "# Inventory" in text and "[table 1006:t01 3x3]" in text


def test_diff_between_versions(env, conn, capsys):
    sync(conn, env)
    body = (
        env.pages[1006]
        .body.replace("db02", "db09")
        .replace("<h2>Groups</h2>", "<h2>Groups</h2><p>New note</p>")
    )
    env.edit(1006, body=body, title="Servers (edited)")
    sync(conn, env)
    code, e = run(capsys, "pages", "diff", "1006", "--from-version", "1", "--to-version", "2")
    assert code == 0
    d = e["data"]
    assert d["metadata"]["title"] == {"from": "Servers", "to": "Servers (edited)"}
    assert d["blocks"]["added"] == ["New note"]
    first = d["tables"][0]
    assert first["match"] == "candidate"
    assert first["cell_changes"] == [{"coord": "R2C3", "from": "db01, db02", "to": "db01, db09"}]


def test_same_version_body_change_is_archived(env, conn, capsys):
    sync(conn, env)
    old = env.pages[1004].body
    env.pages[1004].body = "<p>Rendered differently</p>"  # no version bump
    report = sync(conn, env, mode="full")
    assert report.stats["same_version_body_changed"][0]["page_id"] == 1004
    archived = rows(
        conn,
        "SELECT representation, body FROM source.superseded_bodies WHERE page_id = 1004 "
        "ORDER BY representation",
    )
    assert ("storage", old) in archived
    # The old capture is still addressable by its snapshot ID.
    from confluence_mirror.structure import artifact_identity

    snap = artifact_identity(1004, 1, "storage", old)["snapshot_id"]
    code, e = run(capsys, "pages", "diff", "1004", "--from-snapshot", snap, "--to-version", "1")
    assert code == 0 and e["data"]["blocks"]["added"] == ["Rendered differently"]


def test_local_search(env, conn, capsys):
    sync(conn, env)
    assert confex.main(["parse", "--space", "DEMO"]) == 0
    capsys.readouterr()
    code, e = run(capsys, "search", "--space", "DEMO", "--text", "db01")
    hit = e["data"]["results"][0]
    assert hit["page_id"] == 1006
    match = hit["matches"][0]
    assert match["table_id"].startswith("tbl_") and match["cell_id"].startswith("cell_")
    assert e["data"]["text_projection"]["pages_indexed"] == 6
    code, e = run(capsys, "search", "--title", "page", "--label", "child")
    assert [r["page_id"] for r in e["data"]["results"]] == [1002]
    code, e = run(capsys, "search", "--has-tables", "--text", "illustrative")
    assert [r["page_id"] for r in e["data"]["results"]] == [1006]
    code, e = run(capsys, "search", "--text", "100%_")  # LIKE metacharacters are literal
    assert e["data"]["results"] == []
    code, e = run(capsys, "search", "--cql", "type = page")
    assert code == inspection.EXIT_UNAVAILABLE
    code, e = run(capsys, "--data-source", "live", "search", "--cql", "x", "--title", "y")
    assert code == inspection.EXIT_USAGE


def test_live_mode_reads_without_persisting(env, conn, capsys):
    code, e = run(capsys, "--data-source", "live", "spaces", "list")
    assert code == 0 and e["source"]["mode"] == "live"
    assert [s["space_key"] for s in e["data"]] == ["DEMO", "OTHER"]
    code, e = run(capsys, "--data-source", "live", "pages", "children", "1001")
    assert [p["title"] for p in e["data"]] == ["Child Page", "Sibling Page", "Shared Snippet"]
    code, e = run(capsys, "--data-source", "live", "pages", "ancestors", "1006")
    assert [a["page_id"] for a in e["data"]["ancestors"]] == [1001, 1002]
    code, e = run(capsys, "--data-source", "live", "pages", "read", "1006")
    assert e["data"]["parse_mode"] == "ephemeral_parse"
    code, e = run(capsys, "--data-source", "live", "pages", "tree", "--root", "1001")
    assert code == inspection.EXIT_USAGE  # needs --allow-scan
    code, e = run(
        capsys,
        "--data-source",
        "live",
        "pages",
        "tree",
        "--root",
        "1001",
        "--allow-scan",
        "--max-requests",
        "2",
    )
    assert e["data"]["truncated"] and e["diagnostics"][0]["code"] == "SCOPE_INCOMPLETE"
    code, e = run(
        capsys, "--data-source", "live", "search", "--cql", "ancestor = 1001", "--space", "DEMO"
    )
    assert {r["page_id"] for r in e["data"]["results"]} == {1002, 1003, 1004, 1005, 1006}
    code, e = run(capsys, "--data-source", "live", "pages", "show", "424242")
    assert code == inspection.EXIT_NOT_FOUND
    # Nothing was written to the mirror.
    assert scalar(conn, "SELECT count(*) FROM source.pages") == 0
    assert scalar(conn, "SELECT count(*) FROM sync.sync_runs") == 0


def test_sync_command_scopes(env, conn, capsys):
    code, e = run(capsys, "sync", "--space", "DEMO", "--metadata-only")
    assert code == 0 and e["data"]["mode"] == "metadata"
    assert scalar(conn, "SELECT count(*) FROM source.pages") == 6
    assert scalar(conn, "SELECT count(*) FROM source.page_content") == 0
    assert scalar(conn, "SELECT last_successful_sync_at FROM source.spaces") is None

    code, e = run(
        capsys,
        "sync",
        "--page",
        "1006",
        "--page",
        "999",
        "--representations",
        "storage,export_view",
    )
    assert code == inspection.EXIT_PARTIAL
    runs = e["data"]["runs"]
    assert runs[0]["mode"] == "pages" and runs[0]["pages_fetched"] == 1
    assert "not treated as deleted" in runs[1]["errors"][0]["error"]
    body = rows(
        conn,
        "SELECT body_storage IS NOT NULL, body_export_view IS NOT NULL, body_view IS NULL "
        "FROM source.page_content WHERE page_id = 1006",
    )
    assert body == [(True, True, True)]
    view_requests = [r for r in env.requests if "body.view" in r.url.params.get("expand", "")]
    assert view_requests == []

    code, e = run(capsys, "sync", "--space", "DEMO")
    assert code == 0 and e["data"]["mode"] == "full"
    code, e = run(capsys, "sync", "--space", "DEMO", "--changed-only")
    assert e["data"]["mode"] == "incremental" and e["data"]["pages_unchanged"] == 6
    assert {r.method for r in env.requests} == {"GET"}
