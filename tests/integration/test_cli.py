import json

import pytest

from confluence_mirror import cli
from confluence_mirror.client import ConfluenceClient
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
        'page_size = 3\n[sync]\nspaces = ["DEMO"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)
    monkeypatch.setenv("CONFLUENCE_PAT", TOKEN)

    class FakeTransportClient(ConfluenceClient):
        def __init__(self, settings, token, **kw):
            super().__init__(settings, token, transport=fake.transport, sleep=lambda s: None)

    monkeypatch.setattr(cli, "ConfluenceClient", FakeTransportClient)
    return fake, config


def test_end_to_end(env, raw_conn, capsys, caplog):
    fake, config = env
    assert cli.main(["-c", str(config), "sync", "--all"]) == cli.EXIT_USAGE  # not migrated
    assert cli.main(["-c", str(config), "db", "migrate"]) == 0
    assert cli.main(["-c", str(config), "check"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(config), "sync", "--all"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded" and report["pages_fetched"] == 5

    raw_conn.execute("DELETE FROM extracted.page_macros")
    raw_conn.commit()
    assert cli.main(["-c", str(config), "extract", "--space", "DEMO"]) == 0
    assert json.loads(capsys.readouterr().out)["extracted"] == 5
    assert scalar(raw_conn, "SELECT count(*) FROM extracted.page_macros") > 0

    assert cli.main(["-c", str(config), "runs", "--space", "DEMO"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "succeeded"
    assert TOKEN not in caplog.text


def test_missing_pat(env, monkeypatch):
    _, config = env
    monkeypatch.delenv("CONFLUENCE_PAT")
    assert cli.main(["-c", str(config), "check"]) == cli.EXIT_USAGE


def test_bad_pat_is_auth_exit(env, monkeypatch):
    _, config = env
    monkeypatch.setenv("CONFLUENCE_PAT", "wrong")
    assert cli.main(["-c", str(config), "check"]) == cli.EXIT_AUTH


def test_scoped_syncs(env, raw_conn, capsys):
    fake, config = env
    assert cli.main(["-c", str(config), "db", "migrate"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(config), "sync", "--root", "1002"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "pages" and report["pages_fetched"] == 2
    assert scalar(raw_conn, "SELECT count(*) FROM source.pages") == 2

    assert cli.main(["-c", str(config), "sync", "--cql", 'label = "root"']) == 0
    assert json.loads(capsys.readouterr().out)["pages_fetched"] == 1

    code = cli.main(["-c", str(config), "sync", "--page", "1004", "--page", "424242"])
    assert code == cli.EXIT_PARTIAL
    text, docs, decoder = capsys.readouterr().out.strip(), [], json.JSONDecoder()
    while text:
        doc, end = decoder.raw_decode(text)
        docs.append(doc)
        text = text[end:].strip()
    assert docs[0]["pages_fetched"] == 1 and docs[1]["missing"] == [424242]
    assert cli.main(["-c", str(config), "sync", "--root", "1002", "--incremental"]) == 1
    assert {r.method for r in fake.requests} == {"GET"}
