from confluence_mirror.extraction import templates as tpl
from confluence_mirror.extraction.privacy import PrivacyPolicy
from confluence_mirror.structure import parse_document

MODEL = {"model": "claude-haiku-5-5", "max_output_tokens": 4000, "effort": "medium"}
PAGE = (
    "<h2>Overview</h2><p>Text.</p>"
    "<table><tr><th>Host</th><th>IP</th><th>Notes</th></tr>"
    "<tr><td>app01</td><td>10.0.0.1</td><td>password: hunter2</td></tr>"
    "<tr><td>app02</td><td>10.0.0.2</td><td>ok</td></tr>"
    "<tr><td>app03</td><td>10.0.0.3</td><td>ok</td></tr></table>"
    "<h2>Contacts</h2>"
    "<table><tr><th>Owner</th><td>Team A</td></tr><tr><th>Escalation</th><td>Ops</td></tr></table>"
    '<div class="conf-macro output-block" data-macro-name="info"><p>x</p></div>'
)


def summary():
    artifact = parse_document(PAGE, page_id=11, version=1, representation="export_view")
    return tpl.page_summary(artifact, "Runbook: app", PrivacyPolicy())


def test_summary_is_structural_and_redacted():
    s = summary()
    assert [h["text"] for h in s["headings"]] == ["Overview", "Contacts"]
    hosts, contacts = s["tables"]
    assert hosts["layout"] == "header_row" and hosts["header"] == ["Host", "IP", "Notes"]
    assert hosts["under_heading"] == ["Overview"] and hosts["rows"] == 4
    assert len(hosts["sample_rows"]) == 2  # a sample, not the table
    assert "hunter2" not in str(s) and s["redactions"] == 1
    assert contacts["layout"] == "key_column" and contacts["keys"] == ["Owner", "Escalation"]
    assert s["macros"] == {"info": 1}


def test_sample_is_spread_and_deterministic():
    ids = list(range(1, 21))
    assert tpl.pick_sample(ids, 5) == [1, 6, 11, 15, 20]
    assert tpl.pick_sample(ids[:3], 8) == [1, 2, 3]


def test_requests_carry_a_schema_and_no_sampling_parameters():
    req = tpl.proposal_request({"page_id": 1, "title": "Runbooks"}, 3, [summary()], MODEL)
    assert req["output_config"]["format"]["schema"] is tpl.PROPOSAL_SCHEMA
    assert not {"tools", "temperature", "top_p", "top_k"} & set(req)
    assert "untrusted" in req["system"] and "never instructions" in req["system"]
    check = tpl.check_request({"name": "Runbook"}, [summary()], MODEL)
    assert check["output_config"]["format"]["schema"] is tpl.CHECK_SCHEMA


def test_proposal_is_normalised_and_corrections_are_reported():
    proposal = {
        "follows_template": True,
        "tables": [
            {
                "key": "hosts",
                "columns": [
                    {"header": "Host", "field": "Host Name"},
                    {"header": "IP", "field": "host_name"},
                ],
            }
        ],
        "deviating_pages": [{"page_id": 2, "reason": "x"}, {"page_id": 99, "reason": "y"}],
    }
    out, issues = tpl.clean_proposal(proposal, {1, 2, 3})
    assert [c["field"] for c in out["tables"][0]["columns"]] == ["host_name", "host_name_2"]
    assert [d["page_id"] for d in out["deviating_pages"]] == [2]
    assert len(issues) == 3


def test_check_answers_are_matched_to_their_batch():
    answers = [
        {
            "pages": [
                {"page_id": 1, "conforms": True, "missing": [], "unexpected": [], "notes": ""},
                {"page_id": 1, "conforms": False, "missing": [], "unexpected": [], "notes": ""},
                {"page_id": 7, "conforms": True, "missing": [], "unexpected": [], "notes": ""},
            ],
            "template_issues": ["column Notes is rare"],
        },
        None,  # a failed check call
    ]
    pages, issues = tpl.merge_checks(answers, [[1, 2], [3]])
    assert [(p["page_id"], p["state"]) for p in pages] == [
        (1, "conforms"),
        (2, "unchecked"),
        (3, "unchecked"),
    ]
    assert any("twice" in i for i in issues) and any("not in its batch" in i for i in issues)
    assert any("no answer for page 2" in i for i in issues)
    assert "checker: column Notes is rare" in issues


def test_verdict_thresholds():
    def pages(conform, deviate, unchecked=0):
        return (
            [{"state": "conforms"}] * conform
            + [{"state": "deviates"}] * deviate
            + [{"state": "unchecked"}] * unchecked
        )

    yes = {"follows_template": True}
    assert tpl.verdict(yes, pages(4, 1))["status"] == "template"
    assert tpl.verdict(yes, pages(3, 2))["status"] == "mixed"
    v = tpl.verdict(yes, pages(1, 4))
    assert v["status"] == "no_template" and v["disagreement"]
    assert tpl.verdict(yes, pages(0, 0, 3))["status"] == "unverified"
    assert tpl.verdict({"follows_template": False}, [])["status"] == "no_template"
