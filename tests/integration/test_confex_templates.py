"""Model-based template detection over a page tree, with a scripted model."""

import json
import re

import pytest

from confluence_mirror import confex, inspection
from confluence_mirror.extraction import provider as provider_mod
from confluence_mirror.extraction.provider import Outcome
from tests.fake_confluence import FakeConfluence

from .helpers import rows, scalar, sync

pytestmark = pytest.mark.integration

APPROVED = '[privacy]\negress_approved = true\napproval_reference = "SEC-TEST-1"\n'


def runbook(host: str, note: str = "ok") -> str:
    return (
        "<h2>Overview</h2><p>Service runbook.</p>"
        "<table><tr><th>Host</th><th>IP</th><th>Notes</th></tr>"
        f"<tr><td>{host}</td><td>10.0.0.1</td><td>{note}</td></tr></table>"
        "<h2>Contacts</h2><table><tr><th>Owner</th><td>Team A</td></tr></table>"
    )


def tree() -> FakeConfluence:
    f = FakeConfluence()
    f.add_space("TPL", 96001, "Templates", homepage=9000)
    f.add_page(9000, "TPL", "Runbooks", None, "<p>index</p>")
    for i in range(1, 6):
        body = runbook(f"app0{i}", "password: hunter2" if i == 1 else "ok")
        if i == 5:
            body = "<h2>Notes</h2><p>Free text, no tables.</p>"
        f.add_page(9000 + i, "TPL", f"Runbook {i}", 9000, body, position=i)
    # A parent whose child has its own child: not a template family.
    f.add_page(9100, "TPL", "Guides", 9000, "<p>guides</p>", position=9)
    f.add_page(9101, "TPL", "Guide A", 9100, runbook("x"), position=1)
    f.add_page(9102, "TPL", "Guide A detail", 9101, runbook("y"), position=1)
    f.add_page(9103, "TPL", "Guide B", 9100, runbook("z"), position=2)
    return f


class TemplateModel:
    """Proposes a runbook template; the checker rejects pages without the hosts table."""

    def __init__(self):
        self.requests = []
        self.script = []

    def send(self, request):
        self.requests.append(request)
        content = request["messages"][0]["content"]
        payload = json.loads(re.search(r"<pages>\n(.*)\n</pages>", content, re.S).group(1))
        behaviour = self.script.pop(0) if self.script else "good"
        if behaviour == "refuse":
            return Outcome("refusal", usage={"input_tokens": 10}, sent_body=request)
        schema = request["output_config"]["format"]["schema"]["properties"]
        if "follows_template" in schema:
            answer = {
                "follows_template": True,
                "confidence": "high",
                "name": "Service runbook",
                "description": "One service: hosts, then contacts.",
                "sections": ["Overview", "Contacts"],
                "tables": [
                    {
                        "key": "hosts",
                        "located_under": "Overview",
                        "layout": "header_row",
                        "purpose": "hosts of the service",
                        "columns": [
                            {
                                "header": "Host",
                                "field": "hostname",
                                "type": "hostname",
                                "description": "",
                            },
                            {
                                "header": "IP",
                                "field": "IP Address",
                                "type": "ip_address",
                                "description": "",
                            },
                            {
                                "header": "Notes",
                                "field": "notes",
                                "type": "string",
                                "description": "",
                            },
                        ],
                    }
                ],
                "deviating_pages": [],
                "reason": "same sections and tables",
            }
        else:
            answer = {
                "pages": [
                    {
                        "page_id": c["page_id"],
                        "conforms": bool(c["tables"]),
                        "missing": [] if c["tables"] else ["hosts table"],
                        "unexpected": [],
                        "notes": "",
                    }
                    for c in payload["children"]
                ]
                + (
                    [
                        {
                            "page_id": 4242,
                            "conforms": True,
                            "missing": [],
                            "unexpected": [],
                            "notes": "",
                        }
                    ]
                    if behaviour == "stray"
                    else []
                ),
                "template_issues": [],
            }
        return Outcome(
            "succeeded",
            text=json.dumps(answer),
            parsed=answer,
            usage={"input_tokens": 500, "output_tokens": 200},
            response_meta={"returned_model": request["model"], "stop_reason": "end_turn"},
            sent_body=request,
        )


@pytest.fixture
def env(conn, pg_dsn, monkeypatch, tmp_path):
    fake = tree()
    assert sync(conn, fake, space="TPL").status == "succeeded"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_MIRROR_DSN", pg_dsn)
    model = TemplateModel()
    monkeypatch.setattr(provider_mod, "make_provider", lambda settings: model)
    (tmp_path / "config.toml").write_text("")
    return model, tmp_path


def run(capsys, *argv):
    code = confex.main(["--format", "json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_candidates_are_parents_of_leaves_only(env, capsys):
    model, _ = env
    code, e = run(capsys, "templates", "detect", "--space", "TPL", "--dry-run")
    assert code == 0, e
    items = {i["parent_page_id"]: i for i in e["data"]["items"]}
    # 9000 has a grandchild (via 9100), 9100 has one too (9102): neither qualifies.
    # 9101 has a single child, below --min-children.
    assert set(items) == set()
    code, e = run(capsys, "templates", "detect", "--root", "9101", "--min-children", "1")
    assert e["data"]["candidates"] == 0  # min-children is at least 2


def test_detect_proposes_then_checks_every_child(env, conn, capsys):
    model, tmp = env
    # Make 9000 a family of leaves: move the Guides subtree's grandchild out of the way.
    conn.execute(
        "UPDATE source.pages SET is_deleted = true WHERE page_id IN (9100, 9101, 9102, 9103)"
    )
    conn.commit()

    code, e = run(capsys, "templates", "detect", "--space", "TPL", "--dry-run")
    item = e["data"]["items"][0]
    assert item["parent_page_id"] == 9000 and item["status"] == "planned"
    assert item["children"] == 5 and item["requests"] == 2 and model.requests == []

    code, e = run(capsys, "templates", "detect", "--space", "TPL")
    assert code == inspection.EXIT_POLICY and e["data"]["items"][0]["status"] == "blocked"
    assert model.requests == []  # no approval, nothing sent

    (tmp / "config.toml").write_text(APPROVED)
    code, e = run(capsys, "templates", "detect", "--space", "TPL")
    assert code == 0, e
    item = e["data"]["items"][0]
    assert item["status"] == "template" and item["conformance"] == 0.8
    assert item["template_name"] == "Service runbook" and item["deviating"] == [9005]
    propose, check = model.requests
    assert "follows_template" in propose["output_config"]["format"]["schema"]["properties"]
    assert "hunter2" not in json.dumps(model.requests)  # redacted before sending
    checked = json.loads(
        re.search(r"<pages>\n(.*)\n</pages>", check["messages"][0]["content"], re.S).group(1)
    )
    assert [c["page_id"] for c in checked["children"]] == [9001, 9002, 9003, 9004, 9005]

    det = item["detection_id"]
    assert rows(conn, "SELECT stage, outcome FROM pipeline.attempts ORDER BY started_at") == [
        ("template_propose", "succeeded"),
        ("template_check", "succeeded"),
    ]
    code, e = run(capsys, "templates", "show", "9000")
    assert e["data"]["detection_id"] == det
    fields = [c["field"] for c in e["data"]["proposal"]["tables"][0]["columns"]]
    assert fields == ["hostname", "ip_address", "notes"]  # normalised, and reported
    assert any("renamed" in i for i in e["data"]["issues"])
    assert {p["page_id"]: p["state"] for p in e["data"]["pages"]}[9005] == "deviates"

    # Unchanged inputs: the stored answer is reused, no new calls.
    code, e = run(capsys, "templates", "detect", "--space", "TPL")
    assert e["data"]["items"][0]["reused"] and len(model.requests) == 2
    code, e = run(capsys, "templates", "list", "--space", "TPL")
    assert [r["detection_id"] for r in e["data"]] == [det]

    # --force asks again; a checker that answers for a stranger is noted, not trusted.
    model.script = ["good", "stray"]
    code, e = run(capsys, "templates", "detect", "--space", "TPL", "--force")
    item = e["data"]["items"][0]
    assert item["detection_id"] != det and item["status"] == "template" and item["issues"] == 2
    assert scalar(conn, "SELECT count(*) FROM pipeline.template_detections") == 2

    # A refused proposal is stored as failed and reported as partial.
    model.script = ["refuse"]
    code, e = run(capsys, "templates", "detect", "--space", "TPL", "--force")
    assert code == inspection.EXIT_PARTIAL and e["data"]["items"][0]["status"] == "failed"
