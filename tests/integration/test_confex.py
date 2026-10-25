import json

import pytest

from confluence_mirror import confex, inspection
from tests.fake_confluence import demo_space

from ..conftest import FIXTURES
from .helpers import scalar, sync

pytestmark = pytest.mark.integration


@pytest.fixture
def mirror(conn, rich_storage, pg_dsn, monkeypatch, tmp_path):
    """A synced DEMO space plus a page with merged and nested tables (id 1006)."""
    from tests.fake_confluence import FakeConfluence

    fake = FakeConfluence()
    demo_space(fake, rich_storage)
    geometry = (FIXTURES / "storage" / "geometry_page.xml").read_text(encoding="utf-8")
    fake.add_page(1006, "DEMO", "Servers", 1002, geometry, position=5)
    report = sync(conn, fake)
    assert report.status == "succeeded", report.errors
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)

    # Navigation and inspection must never reach Confluence.
    def no_network(*a, **kw):
        raise AssertionError("confex opened an HTTP client")

    monkeypatch.setattr("httpx.Client.__init__", no_network)
    return fake


def run(capsys, *argv):
    code = confex.main(["--format", "json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_navigation(mirror, capsys):
    code, env = run(capsys, "spaces", "list")
    assert code == 0 and env["schema_version"] == "confex.cli.v1"
    demo = next(s for s in env["data"] if s["space_key"] == "DEMO")
    assert demo["mirrored_pages"] == 6

    code, env = run(capsys, "spaces", "tree", "DEMO", "--counts")
    root = env["data"]["roots"][0]
    assert root["title"] == "Root Page" and root["descendants_known"] == 5
    # Sibling order follows source position, then title.
    assert [c["title"] for c in root["children"]] == [
        "Child Page",
        "Sibling Page",
        "Shared Snippet",
    ]

    code, env = run(capsys, "spaces", "tree", "DEMO", "--match", "servers")
    path = env["data"]["roots"][0]
    assert path["role"] == "context"
    assert path["children"][0]["children"][0] == {
        "page_id": 1006,
        "title": "Servers",
        "depth": 2,
        "children": [],
        "hidden_children": 0,
        "role": "match",
    }

    code, env = run(capsys, "spaces", "tree", "DEMO", "--depth", "0")
    assert env["data"]["roots"][0]["children"] == []
    assert env["data"]["roots"][0]["hidden_children"] == 3

    code, env = run(capsys, "pages", "ancestors", "1006")
    assert [a["page_id"] for a in env["data"]["ancestors"]] == [1001, 1002]

    code, env = run(capsys, "pages", "siblings", "1002")
    assert [(s["page_id"], s["selected"]) for s in env["data"]][0] == (1002, True)

    code, env = run(capsys, "pages", "children", "1001", "--limit", "2")
    assert env["completeness"]["has_more"] and env["completeness"]["known_total"] == 3
    cursor = env["completeness"]["next_cursor"]
    code, env = run(capsys, "pages", "children", "1001", "--limit", "2", "--cursor", cursor)
    assert [p["title"] for p in env["data"]] == ["Shared Snippet"]
    code, env = run(capsys, "pages", "children", "1002", "--cursor", cursor)
    assert code == inspection.EXIT_USAGE and env["error"]["code"] == "INVALID_CURSOR"

    code, env = run(capsys, "pages", "tree", "--root", "1001", "--depth", "1")
    assert env["data"]["root"]["children"][0]["hidden_children"] == 2

    code, env = run(capsys, "pages", "show", "1006")
    page = env["data"]
    assert page["breadcrumbs"][-1]["title"] == "Child Page"
    assert page["representations_available"] == ["storage", "export_view", "view"]
    assert page["parse_artifacts"] == [] and page["known_tables"] is None

    code, env = run(capsys, "pages", "show", "99999")
    assert code == inspection.EXIT_NOT_FOUND and env["status"] == "error"


def test_table_inspection_without_persisted_parse(mirror, capsys, conn):
    code, env = run(capsys, "tables", "show", "1006:t01")
    assert code == inspection.EXIT_AMBIGUOUS
    choices = env["error"]["details"]["choices"]
    assert len(choices) == 3  # storage, export_view and view are never silently equated

    code, env = run(capsys, "tables", "show", "1006:t01", "--representation", "storage")
    assert code == 0
    data = env["data"]
    assert data["identity"]["parse_mode"] == "ephemeral_parse"
    assert data["heading_path"][-1]["text"] == "Servers"
    assert data["dimensions"] == {"rows": 3, "columns": 3}
    assert env["source"]["snapshot_id"].startswith("snap_")

    code, env = run(
        capsys,
        "tables",
        "show",
        "1006:t01",
        "--representation",
        "storage",
        "--view",
        "cell",
        "--cell",
        "R3C1",
    )
    assert env["data"]["kind"] == "covered"
    owner = env["data"]["owners"][0]
    assert owner["span"] == "R2C1:R3C1"
    assert owner["blocks"][0]["evidence_text"] == "Example DB"

    code, env = run(
        capsys,
        "tables",
        "show",
        "1006:t01",
        "--representation",
        "storage",
        "--view",
        "grid",
        "--rows",
        "2:3",
    )
    assert env["completeness"]["state"] == "partial"
    assert [s["kind"] for s in env["data"]["grid"][1]] == ["covered", "origin", "origin"]
    assert env["data"]["grid"][1][2]["nested"]

    code, env = run(
        capsys, "tables", "show", "1006:t06", "--representation", "storage", "--view", "overview"
    )
    assert env["data"]["geometry_status"] == "invalid"
    assert "SPAN_OVERLAP" in [d["code"] for d in env["data"]["diagnostics"]]

    code, env = run(
        capsys, "tables", "show", "1006:t01", "--representation", "storage", "--view", "payload"
    )
    assert code == inspection.EXIT_UNAVAILABLE and env["error"]["code"] == "PAYLOAD_NOT_READY"

    code, env = run(capsys, "tables", "show", "1006:t09", "--representation", "storage")
    assert code == inspection.EXIT_NOT_FOUND

    # Inspection wrote nothing.
    assert scalar(conn, "SELECT count(*) FROM extracted.parse_artifacts") == 0


def test_parse_then_inspect_by_persistent_id(mirror, capsys, conn):
    code, env = run(capsys, "parse", "--space", "DEMO")
    assert code == 0 and env["data"]["parsed"] == 12 and env["data"]["failed"] == 0
    code, env = run(capsys, "parse", "--space", "DEMO")
    assert env["data"]["reused"] == 12 and env["data"]["parsed"] == 0  # immutable artifacts

    code, env = run(capsys, "tables", "list", "--space", "DEMO", "--representation", "storage")
    by_page = [(t["page_id"], t["ordinal"]) for t in env["data"]]
    assert (1006, 1) in by_page and (1003, 1) in by_page
    code, env = run(
        capsys,
        "tables",
        "list",
        "--page",
        "1006",
        "--representation",
        "storage",
        "--nested",
        "only",
    )
    assert [t["ordinal"] for t in env["data"]] == [2]

    code, env = run(capsys, "pages", "tables", "1006", "--representation", "storage")
    first = env["data"][0]
    code, env = run(capsys, "tables", "show", first["table_id"], "--view", "physical")
    assert code == 0 and env["data"]["identity"]["parse_mode"] == "persisted"
    assert env["data"]["rows"][1]["cells"][0]["raw_rowspan"] == "2"

    code, env = run(capsys, "tables", "show", first["table_id"], "--representation", "view")
    assert code == inspection.EXIT_USAGE and env["error"]["code"] == "ARGUMENT_CONFLICT"

    code, env = run(capsys, "tables", "show", "1006:t01", "--representation", "storage")
    assert env["data"]["identity"]["parse_mode"] == "persisted"
    snapshot = env["source"]["snapshot_id"]
    code, env = run(capsys, "tables", "show", "1006:t01", "--snapshot", snapshot)
    assert code == 0 and env["data"]["identity"]["table_id"] == first["table_id"]

    code, env = run(capsys, "pages", "outline", "1006", "--representation", "storage", "--tables")
    outline = env["data"]["outline"]
    assert [n["text"] for n in outline if n["kind"] == "heading"][:2] == ["Inventory", "Servers"]
    nested = next(n for n in outline if n.get("selector") == "1006:t02")
    assert nested["container_depth"] == 1

    code, env = run(capsys, "pages", "show", "1006")
    assert env["data"]["known_tables"] == {"export_view": 6, "storage": 6}


def test_plain_output_is_terminal_safe(mirror, capsys, conn):
    conn.execute("UPDATE source.pages SET title = %s WHERE page_id = 1004", ("Evil \x1b[31mred",))
    conn.commit()
    assert confex.main(["pages", "children", "1001"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\\x1b[31mred" in out

    assert (
        confex.main(["tables", "show", "1006:t01", "--representation", "storage", "--view", "grid"])
        == 0
    )
    out = capsys.readouterr().out
    assert "covered:c4" in out and "nested:tbl_" in out

    # A mixed cell: paragraph, nested-table reference, paragraph.
    assert (
        confex.main(
            [
                "tables",
                "show",
                "1006:t01",
                "--representation",
                "storage",
                "--view",
                "cell",
                "--cell",
                "R3C3",
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[3].endswith("see below") and "nested tbl_" in lines[4]
    assert lines[5].endswith("end")


def test_output_file_and_schema_gate(mirror, capsys, tmp_path, conn):
    target = tmp_path / "spaces.json"
    assert confex.main(["--format", "json", "--output", str(target), "spaces", "list"]) == 0
    assert json.loads(target.read_text())["command"] == "spaces.list"
    assert (target.stat().st_mode & 0o777) == 0o600
    code = confex.main(["--format", "json", "--output", str(target), "spaces", "list"])
    assert code == inspection.EXIT_USAGE
    capsys.readouterr()

    # An unmigrated database is refused, never migrated implicitly.
    conn.execute("DROP TABLE extracted.table_occurrences")
    conn.execute("DELETE FROM sync.schema_migrations WHERE version = 4")
    conn.commit()
    code, env = run(capsys, "spaces", "list")
    assert code == inspection.EXIT_UNAVAILABLE and env["error"]["code"] == "SCHEMA_INCOMPATIBLE"
    code, env = run(capsys, "--data-source", "live", "tables", "list")
    assert code == inspection.EXIT_UNAVAILABLE and env["error"]["code"] == "CAPABILITY_UNAVAILABLE"


def test_jsonl_collections(mirror, capsys):
    assert confex.main(["--format", "jsonl", "spaces", "list"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["type"] == "meta" and lines[-1]["type"] == "summary"
    assert {x["type"] for x in lines[1:-1]} == {"item"}
    assert confex.main(["--format", "jsonl", "pages", "show", "1001"]) == inspection.EXIT_USAGE
