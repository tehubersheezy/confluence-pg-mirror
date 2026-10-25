import json
import logging

import psycopg
import pytest

from confluence_mirror import confex, db, inspection
from confluence_mirror import repository as repo
from confluence_mirror import repository_pipeline as prepo
from confluence_mirror.client import ConfluenceClient
from confluence_mirror.confex import core
from confluence_mirror.extraction import provider as provider_mod
from confluence_mirror.structure import parse_document
from tests.fake_confluence import TOKEN, FakeConfluence, demo_space

from ..conftest import FIXTURES
from .helpers import scalar

pytestmark = pytest.mark.integration

HOSTS = (
    "<h2>Production</h2><table><tr><th>Host</th><th>IP</th></tr>"
    "<tr><td>app01</td><td>10.0.0.1</td></tr><tr><td>app02</td><td>10.0.0.2</td></tr></table>"
)


@pytest.fixture
def env(conn, rich_storage, pg_dsn, monkeypatch, tmp_path):
    fake = FakeConfluence()
    demo_space(fake, rich_storage)
    fake.add_page(1007, "DEMO", "Hosts", 1001, HOSTS, position=6)
    config = tmp_path / "config.toml"
    config.write_text('[confluence]\nbase_url = "https://confluence.example.com"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)
    monkeypatch.setenv("CONFLUENCE_PAT", TOKEN)
    monkeypatch.setattr(
        core,
        "make_client",
        lambda s, t: ConfluenceClient(s, t, transport=fake.transport, sleep=lambda x: None),
    )
    return fake, config


def run(capsys, *argv):
    code = confex.main(["--format", "json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_run_drives_every_stage_and_never_approves(env, conn, capsys, caplog):
    fake, _ = env
    caplog.set_level(logging.DEBUG)
    code, e = run(capsys, "run", "--space", "DEMO", "--from-stage", "extract")
    assert code == inspection.EXIT_NOT_FOUND  # nothing synced yet: no guessing
    assert scalar(conn, "SELECT count(*) FROM pipeline.candidate_revisions") == 0

    code, e = run(capsys, "run", "--space", "DEMO", "--dry-run")
    assert e["data"]["would_run"] == ["sync", "parse", "align", "extract"]
    assert scalar(conn, "SELECT count(*) FROM source.pages") == 0  # a dry run syncs nothing

    code, e = run(capsys, "run", "--space", "DEMO")
    assert code == 0, e
    stages = {s["stage"]: s["status"] for s in e["data"]["stages"]}
    assert stages == {
        "sync": "succeeded",
        "parse": "succeeded",
        "align": "succeeded",
        "extract": "succeeded",
    }
    assert e["data"]["awaiting_review"] >= 2
    assert scalar(conn, "SELECT count(*) FROM review.review_events") == 0
    assert scalar(conn, "SELECT count(*) FROM review.exports") == 0

    code, e = run(capsys, "run", "--space", "DEMO", "--changed-only")
    assert code == 0
    run_id = e["data"]["run_id"]
    items = prepo.run_items(conn, run_id, stage="extract")
    conn.rollback()
    assert items and all(i["detail"].get("reused_attempt") for i in items)

    code, e = run(capsys, "run", "--root", "1002", "--from-stage", "sync", "--to-stage", "parse")
    assert {s["stage"] for s in e["data"]["stages"]} == {"sync", "parse"}
    assert {r.method for r in fake.requests} == {"GET"}
    assert TOKEN not in caplog.text


def test_missing_prerequisites_fail_instead_of_guessing(env, conn, capsys):
    fake, _ = env
    run(capsys, "sync", "--space", "DEMO")
    code, e = run(capsys, "run", "--page", "1007", "--from-stage", "extract")
    assert code == inspection.EXIT_UNAVAILABLE and e["error"]["code"] == "MISSING_PREREQUISITE"
    code, e = run(capsys, "run", "--table", "tbl_x", "--from-stage", "parse")
    assert code == inspection.EXIT_USAGE


def test_doctor_and_config(env, monkeypatch, capsys, tmp_path, pg_dsn):
    _, config = env
    code, e = run(capsys, "doctor")
    checks = {c["check"]: c["ok"] for c in e["data"]["checks"]}
    assert checks["config"] and checks["registry"] and checks["database"] is None
    code, e = run(capsys, "doctor", "--check-db", "--check-confluence")
    checks = {c["check"]: c for c in e["data"]["checks"]}
    assert checks["confluence"]["ok"] and "mirror-bot" in checks["confluence"]["detail"]

    class FakeCheck:
        def check(self):
            return {"ok": True, "model": "claude-haiku-5-5"}

    monkeypatch.setattr(provider_mod, "make_provider", lambda s: FakeCheck())
    code, e = run(capsys, "doctor", "--check-model")
    assert {c["check"]: c["ok"] for c in e["data"]["checks"]}["model"] is True

    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", "postgresql://u:hunter2@db.example.com/x")
    code, e = run(capsys, "config", "show")
    text = json.dumps(e["data"])
    assert "hunter2" not in text and TOKEN not in text and "***" in text
    assert e["data"]["pat"] == {"env": "CONFLUENCE_PAT", "set": True}
    config.write_text('[model]\napi_key = "sk-ant-inline"\n')
    code, e = run(capsys, "config", "validate")
    assert code == inspection.EXIT_USAGE


def test_eval_commands(env, capsys, tmp_path):
    dataset = FIXTURES / "evaluation" / "synthetic_v1.json"
    report = tmp_path / "r1.json"
    code, e = run(capsys, "eval", "run", "--dataset", str(dataset), "--report", str(report))
    assert code == 0 and e["data"]["meets_targets"]
    code, e = run(capsys, "eval", "report", str(report))
    assert e["data"]["dataset"] == "synthetic_v1"
    code, e = run(capsys, "eval", "compare", str(report), str(report))
    assert e["data"]["changed_cases"] == []
    code, e = run(capsys, "eval", "run", "--dataset", str(dataset), "--extractor", "model")
    assert code == inspection.EXIT_POLICY  # synthetic or not, egress needs approval


def test_leases_are_exclusive_until_they_expire(conn):
    conn.execute(
        "INSERT INTO pipeline.runs (run_id, command, scope, requested_stages, status, "
        "config_digest, versions, principal_scope) VALUES ('run_t', 'extract', '{}', '[]', "
        "'running', 'x', '{}', 'local:test')"
    )
    prepo.upsert_item(conn, "run_t", "extract", "unit_a", "pending")
    conn.commit()
    assert prepo.claim_item(conn, "run_t", "extract", "unit_a", "worker-1", 600)
    assert not prepo.claim_item(conn, "run_t", "extract", "unit_a", "worker-2", 600)
    conn.execute("UPDATE pipeline.run_items SET lease_expires_at = now() - interval '1 second'")
    assert prepo.claim_item(conn, "run_t", "extract", "unit_a", "worker-2", 600)
    conn.rollback()


def test_concurrent_identical_artifact_commits(pg_dsn, conn, rich_storage):
    from .helpers import sync

    fake = FakeConfluence()
    demo_space(fake, rich_storage)
    sync(conn, fake)
    body = repo.storage_body(conn, 1001, 1)
    conn.rollback()
    artifact = parse_document(body, page_id=1001, version=1, representation="storage")
    other = db.connect(pg_dsn)
    try:
        with conn.transaction():
            assert repo.insert_parse_artifact(conn, artifact)
            # A second writer with the same artifact blocks on the key, then does nothing.
            with other.transaction():
                other.execute("SET LOCAL lock_timeout = '200ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    repo.insert_parse_artifact(other, artifact)
        other.rollback()
        with other.transaction():
            assert not repo.insert_parse_artifact(other, artifact)
    finally:
        other.close()
    assert scalar(conn, "SELECT count(*) FROM extracted.parse_artifacts") == 1


def test_navigation_connection_is_read_only(env, pg_dsn, capsys):
    from confluence_mirror.confex.core import REGISTRY, Context

    run(capsys, "sync", "--space", "DEMO")
    args = confex.core.build_parser(confex.MODULES).parse_args(["pages", "show", "1001"])
    ctx = Context(args, REGISTRY["pages.show"])
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ctx.conn().execute("UPDATE source.pages SET title = 'x'")
    finally:
        ctx.close()


def test_export_manifest_holds_no_secrets(env, conn, capsys, tmp_path):
    run(capsys, "run", "--space", "DEMO")
    rev = next(r for r in run(capsys, "records", "list", "--page", "1007")[1]["data"])
    run(capsys, "review", "approve", rev["revision_id"], "--reason", "ok")
    out = tmp_path / "pkg"
    code, e = run(capsys, "export", "--space", "DEMO", "--approved-only", "--output", str(out))
    assert code == 0
    for f in out.iterdir():
        assert TOKEN not in f.read_text()


def test_tree_scoped_run_and_sync_discover_pages(env, conn, capsys):
    code, e = run(capsys, "run", "--root", "1002", "--to-stage", "parse")
    assert code == 0, e
    stages = {s["stage"]: s for s in e["data"]["stages"]}
    assert stages["sync"]["status"] == "succeeded" and stages["parse"]["status"] == "succeeded"
    assert {r[0] for r in conn.execute("SELECT page_id FROM source.pages")} == {1002, 1003}
    code, e = run(capsys, "sync", "--cql", 'title ~ "Hosts"')
    assert code == 0 and e["data"]["pages_fetched"] == 1 and e["data"]["mode"] == "pages"
    code, e = run(capsys, "sync", "--root", "1002", "--changed-only")
    assert code == inspection.EXIT_USAGE
