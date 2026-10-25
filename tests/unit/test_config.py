import os
import re

import pytest

from confluence_mirror.config import ConfigError, get_pat, load_dotenv, load_settings


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


BASE = """
[confluence]
base_url = "https://confluence.example.com/wiki/"
[database]
dsn = "postgresql:///mirror"
[sync]
spaces = ["DEMO", "OPS"]
"""


def test_loads_and_normalises(tmp_path):
    s = load_settings(write(tmp_path, BASE), env={})
    assert s.confluence.base_url == "https://confluence.example.com/wiki"
    assert s.sync.spaces == ("DEMO", "OPS")
    assert s.sync.content_types == ("page",)
    assert s.dsn == "postgresql:///mirror"


def test_env_overrides(tmp_path):
    s = load_settings(
        write(tmp_path, BASE),
        env={
            "CONFLUENCE_BASE_URL": "https://other.example.com",
            "CONFLUENCE_MIRROR_DSN": "postgresql:///other",
        },
    )
    assert s.confluence.base_url == "https://other.example.com"
    assert s.dsn == "postgresql:///other"


@pytest.mark.parametrize("line", ['pat = "x"', 'token = "x"', 'password = "x"'])
def test_secrets_in_file_rejected(tmp_path, line):
    text = BASE.replace("[database]", f"{line}\n[database]")
    with pytest.raises(ConfigError, match="must not be stored"):
        load_settings(write(tmp_path, text), env={})


@pytest.mark.parametrize("line", ["verify = false", "verify_ssl = false"])
def test_disabling_tls_rejected(tmp_path, line):
    text = BASE.replace("[database]", f"{line}\n[database]")
    with pytest.raises(ConfigError, match="TLS"):
        load_settings(write(tmp_path, text), env={})


def test_plain_http_rejected_except_localhost(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        load_settings(write(tmp_path, BASE.replace("https://", "http://")), env={})
    local = BASE.replace("https://confluence.example.com/wiki/", "http://localhost:8090")
    assert load_settings(write(tmp_path, local), env={}).confluence.base_url.startswith("http:")


def test_unknown_keys_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown"):
        load_settings(write(tmp_path, BASE + "typo_key = 1\n"), env={})


def test_bad_content_type(tmp_path):
    text = BASE + 'content_types = ["page", "comment"]\n'
    with pytest.raises(ConfigError, match="content_types"):
        load_settings(write(tmp_path, text), env={})


def test_missing_ca_bundle(tmp_path):
    text = BASE.replace("[database]", 'ca_bundle = "/nonexistent/ca.pem"\n[database]')
    with pytest.raises(ConfigError, match="ca_bundle"):
        load_settings(write(tmp_path, text), env={})


def test_offline_commands_do_not_need_base_url(tmp_path):
    text = '[database]\ndsn = "postgresql:///m"\n'
    assert load_settings(write(tmp_path, text), env={}, require_confluence=False).confluence is None
    with pytest.raises(ConfigError, match="base_url"):
        load_settings(write(tmp_path, text), env={})


def test_get_pat():
    assert get_pat({"CONFLUENCE_PAT": " abc "}) == "abc"
    with pytest.raises(ConfigError):
        get_pat({})


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nCPM_TEST_A="from-file"\nexport CPM_TEST_B=b\nCPM_TEST_C=\n')
    monkeypatch.setenv("CPM_TEST_A", "from-env")
    monkeypatch.delenv("CPM_TEST_B", raising=False)
    load_dotenv(env_file)
    assert os.environ["CPM_TEST_A"] == "from-env"
    assert os.environ["CPM_TEST_B"] == "b"
    assert "CPM_TEST_C" not in os.environ
    monkeypatch.delenv("CPM_TEST_B")


def test_shipped_template_is_valid():
    from pathlib import Path

    template = Path(__file__).parents[2] / "config" / "config.example.toml"
    settings = load_settings(template, env={})
    assert settings.sync.spaces == ("DEMO",)
    assert settings.confluence.use_system_truststore is False


def _confex(tmp_path, text):
    path = tmp_path / "c.toml"
    path.write_text(text)
    return load_settings(
        path, {"CONFLUENCE_MIRROR_DSN": "postgresql:///x"}, require_confluence=False
    ).confex


def test_confex_sections(tmp_path):
    c = _confex(
        tmp_path,
        '[model]\neffort = "low"\n[privacy]\negress_approved = true\n'
        'approval_reference = "SEC-1"\nallowed_spaces = ["DEMO"]\n'
        'redact_patterns = [{name = "ticket", pattern = "TCK-[0-9]+"}]\n'
        "[budgets]\nmax_requests = 5\n"
        '[pricing."claude-haiku-5-5"]\ninput_per_mtok = 0.1\noutput_per_mtok = 0.5\n'
        'as_of = "2026-10-06"\n',
    )
    assert c.model.model == "claude-haiku-5-5" and c.model.effort == "low"
    assert c.privacy.allowed_spaces == ("DEMO",)
    assert ("ticket", "TCK-[0-9]+") in c.privacy.rules
    assert c.budgets.max_requests == 5
    assert c.pricing["claude-haiku-5-5"]["as_of"] == "2026-10-06"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[model]\napi_key = "sk-ant-x"\n', "must not be stored"),
        ('[model]\nendpoint = "http://api.anthropic.com"\n', "https"),
        ('[privacy]\nredact_patterns = [{name = "x", pattern = "("}]\n', "redact_patterns"),
        ('[pricing."m"]\ninput_per_mtok = 1\n', "needs"),
        ("[budgets]\nmonthly = 3\n", "unknown [budgets]"),
        ('[extraction]\nextractor = "magic"\n', "extractor"),
    ],
)
def test_confex_sections_reject(tmp_path, text, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        _confex(tmp_path, text)


def test_bulk_sync_settings(tmp_path):
    s = load_settings(
        write(
            tmp_path,
            BASE.replace("[sync]", '[sync]\nlisting = "scan"\npage_properties = ["owner"]') + "\n",
        ),
        env={},
    )
    assert s.sync.listing == "scan" and s.sync.page_properties == ("owner",)
    assert s.sync.body_batch_size == 50 and s.confluence.max_requests_per_second == 5.0


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('listing = "cql"', "sync.listing"),
        ("body_batch_size = 51", "body_batch_size"),
        ("listing_page_size = 0", "listing_page_size"),
        ('page_properties = "some"', "page_properties"),
        ('page_properties = ["a,b"]', "page_properties"),
    ],
)
def test_bulk_sync_settings_rejected(tmp_path, line, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, BASE.replace("[sync]", f"[sync]\n{line}")), env={})


def test_negative_rate_rejected(tmp_path):
    text = BASE.replace("[confluence]", "[confluence]\nmax_requests_per_second = -1")
    with pytest.raises(ConfigError, match="max_requests_per_second"):
        load_settings(write(tmp_path, text), env={})


def test_page_properties_default_is_known(tmp_path):
    s = load_settings(write(tmp_path, BASE), env={})
    assert s.sync.page_properties == "known"
    text = BASE.replace("[sync]", '[sync]\npage_properties = "all"')
    assert load_settings(write(tmp_path, text), env={}).sync.page_properties == "all"
