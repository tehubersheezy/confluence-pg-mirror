"""The mirror lifecycle through confex: migrate, doctor, sync, reextract, sync runs."""

import json

import pytest

from confluence_mirror import confex, db, inspection
from confluence_mirror.client import ConfluenceClient
from confluence_mirror.confex import core
from tests.fake_confluence import TOKEN, FakeConfluence, demo_space

from .helpers import scalar

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path, monkeypatch, pg_dsn, raw_conn, rich_storage):
    fake = FakeConfluence()
    demo_space(fake, rich_storage)
    config = tmp_path / "config.toml"
    config.write_text(
        '[confluence]\nbase_url = "https://confluence.example.com"\n'
        'page_size = 3\n[sync]\nspaces = ["DEMO", "OTHER"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)
    monkeypatch.setenv("CONFLUENCE_PAT", TOKEN)
    monkeypatch.setattr(
        core,
        "make_client",
        lambda s, t: ConfluenceClient(s, t, transport=fake.transport, sleep=lambda x: None),
    )
    return fake


def run(capsys, *argv):
    code = confex.main(["--format", "json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_mirror_lifecycle(env, raw_conn, capsys, caplog):
    code, e = run(capsys, "sync", "--all")
    assert code == inspection.EXIT_UNAVAILABLE  # not migrated: no implicit schema change
    assert e["error"]["code"] == "SCHEMA_INCOMPATIBLE"

    code, e = run(capsys, "db", "migrate", "--target", "1")
    assert e["data"]["applied"] == ["0001_source_schema"]
    code, e = run(capsys, "db", "migrate")
    assert code == 0 and len(e["data"]["applied"]) == len(db.available_migrations()) - 1

    code, e = run(capsys, "doctor", "--check-db", "--check-confluence")
    checks = {c["check"]: c["ok"] for c in e["data"]["checks"]}
    assert checks["database"] and checks["confluence"]

    code, e = run(capsys, "sync", "--all")
    assert code == 0, e
    assert [r["space_key"] for r in e["data"]["runs"]] == ["DEMO", "OTHER"]
    assert all(r["status"] == "succeeded" for r in e["data"]["runs"])

    raw_conn.execute("DELETE FROM extracted.page_macros")
    raw_conn.commit()
    code, e = run(capsys, "reextract", "--space", "DEMO")
    assert code == 0 and e["data"]["extracted"] == 5
    assert scalar(raw_conn, "SELECT count(*) FROM extracted.page_macros") > 0

    code, e = run(capsys, "runs", "sync", "--space", "DEMO")
    assert e["data"][0]["status"] == "succeeded" and e["data"][0]["space_key"] == "DEMO"
    assert {r.method for r in env.requests} == {"GET"}
    assert TOKEN not in caplog.text


def test_one_locked_space_does_not_stop_the_others(env, raw_conn, pg_dsn, capsys):
    run(capsys, "db", "migrate")
    other = db.connect(pg_dsn)
    try:
        other.execute("SELECT pg_advisory_lock(hashtext('confluence-mirror:space:DEMO'))")
        code, e = run(capsys, "sync", "--space", "DEMO", "--space", "OTHER")
    finally:
        other.close()
    assert code == inspection.EXIT_PARTIAL
    status = {r["space_key"]: r["status"] for r in e["data"]["runs"]}
    assert status == {"DEMO": "failed", "OTHER": "succeeded"}
    assert "already being synced" in e["data"]["runs"][0]["errors"][0]["error"]


def test_scoped_syncs(env, raw_conn, capsys):
    run(capsys, "db", "migrate")
    code, e = run(capsys, "sync", "--root", "1002")
    assert code == 0 and e["data"]["mode"] == "pages" and e["data"]["pages_fetched"] == 2
    assert scalar(raw_conn, "SELECT count(*) FROM source.pages") == 2

    code, e = run(capsys, "sync", "--page", "1004", "--page", "424242")
    assert code == inspection.EXIT_PARTIAL
    runs = e["data"]["runs"]
    assert runs[0]["pages_fetched"] == 1
    assert runs[1]["errors"][0]["page_id"] == 424242


def test_credentials(env, monkeypatch, capsys):
    run(capsys, "db", "migrate")
    monkeypatch.delenv("CONFLUENCE_PAT")
    code, e = run(capsys, "sync", "--space", "DEMO")
    assert code == inspection.EXIT_USAGE and e["error"]["code"] == "CONFIG_INVALID"
    monkeypatch.setenv("CONFLUENCE_PAT", "wrong")
    code, e = run(capsys, "sync", "--space", "DEMO")
    assert code == inspection.EXIT_POLICY and e["error"]["code"] == "AUTHENTICATION_FAILED"
    code, e = run(capsys, "doctor", "--check-confluence")
    assert {c["check"]: c["ok"] for c in e["data"]["checks"]}["confluence"] is False


def test_properties_override(env, raw_conn, capsys):
    run(capsys, "db", "migrate")
    code, e = run(capsys, "sync", "--space", "DEMO")
    assert code == 0 and "property_keys" in e["data"]["stats"]
    assert scalar(raw_conn, "SELECT count(*) FROM source.page_properties") == 0
    code, e = run(capsys, "sync", "--space", "DEMO", "--reconcile", "--properties", "all")
    assert code == 0
    assert scalar(raw_conn, "SELECT count(*) FROM source.page_properties") == 1
    assert e["data"]["stats"]["http_requests"]["/rest/api/content/{id}/property"] == 5
