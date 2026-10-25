import json
import socket

import pytest

from confluence_mirror.inspection import table_grid
from confluence_mirror.structure import ParseConfig, parse_document
from confluence_mirror.structure.grid import CellInput, Limits, RowInput, layout, parse_span

from ..conftest import FIXTURES


@pytest.fixture
def geometry() -> str:
    return (FIXTURES / "storage" / "geometry_page.xml").read_text(encoding="utf-8")


def parse(body, representation="storage", **kw):
    return parse_document(body, page_id=7, version=1, representation=representation, **kw)


def kinds(table):
    return [[s["kind"] for s in row] for row in table_grid(table)]


def headings(table):
    return [h["text"] for h in table["heading_path"]]


def codes(diags):
    return [d["code"] for d in diags]


def test_merged_cells_point_at_one_physical_cell(geometry):
    t = parse(geometry)["tables"][0]
    assert (t["row_count"], t["column_count"]) == (3, 3)
    assert kinds(t) == [["origin"] * 3, ["origin"] * 3, ["covered", "origin", "origin"]]
    covered = table_grid(t)[2][0]["cells"]
    owner = next(c for c in t["cells"] if c["cell_id"] == covered[0])
    assert owner["direct_text"] == "Example DB"
    assert (owner["origin"], owner["rowspan"]) == ({"row": 2, "col": 1}, 2)
    assert len(t["cells"]) == 8  # the covered slot is not a cell of its own
    assert t["geometry_status"] == "valid" and t["extraction_eligible"]


def test_nested_table_is_separate_and_referenced_from_its_cell(geometry):
    outer, inner = parse(geometry)["tables"][:2]
    assert outer["row_count"] == 3  # child rows never count towards the parent
    holder = next(c for c in outer["cells"] if c["cell_id"] == inner["parent_cell_id"])
    assert [x["kind"] for x in holder["content"]] == ["text", "table", "text"]
    assert holder["content"][1]["table_id"] == inner["table_id"]
    assert holder["direct_text"] == "see below end"  # excludes the child's text
    assert inner["parent_table_id"] == outer["table_id"] and inner["depth"] == 1
    assert outer["child_table_ids"] == [inner["table_id"]]


def test_heading_scopes_do_not_leak(geometry):
    tables = parse(geometry)["tables"]
    # Skipped levels (h1 -> h3) are kept as authored, not filled in.
    assert [h["level"] for h in tables[0]["heading_path"]] == [1, 3]
    # The h2 inside a nested cell does not change later top-level context.
    assert headings(tables[2]) == ["Inventory", "Groups"]
    # A heading in one layout column does not reach the other column.
    assert headings(tables[3]) == ["Inventory", "Groups", "Left column"]
    assert headings(tables[4]) == ["Inventory", "Groups"]
    assert tables[3]["containers"][0]["kind"] == "layout_cell"


def test_rowspan_zero_stops_at_each_row_group(geometry):
    t = parse(geometry)["tables"][2]
    assert [g["kind"] for g in t["row_groups"]] == ["thead", "tbody", "tfoot"]
    assert kinds(t) == [
        ["origin", "origin"],
        ["covered", "origin"],
        ["origin", "origin"],
        ["covered", "origin"],
        ["origin", "covered"],
    ]
    assert t["geometry_status"] == "valid"


def test_malformed_geometry_stays_inspectable(geometry):
    t = parse(geometry)["tables"][5]
    assert t["geometry_status"] == "invalid" and not t["extraction_eligible"]
    assert codes(t["diagnostics"]) == [
        "SPAN_OVERLAP",
        "INVALID_ROWSPAN",
        "SPAN_EXTENDS_BEYOND_ROWS",
    ]
    assert kinds(t)[1] == ["origin", "conflict"]
    conflict = table_grid(t)[1][1]["cells"]
    assert len(conflict) == 2  # every claimant is kept
    # The long rowspan is cut at the last authored row; no rows are invented.
    assert t["row_count"] == 3
    long_cell = t["cells"][-1]
    assert (long_cell["raw_rowspan"], long_cell["requested_rowspan"], long_cell["rowspan"]) == (
        "5",
        5,
        1,
    )


def test_code_macro_html_is_not_a_table(geometry):
    doc = parse(geometry)
    assert len(doc["tables"]) == 6
    # Even unescaped markup inside a plain-text body stays literal.
    body = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        "<table><tr><td>x</td></tr></table></ac:plain-text-body></ac:structured-macro>"
    )
    literal = parse(body)
    assert literal["tables"] == []
    assert codes(literal["diagnostics"]) == ["LITERAL_TABLE_MARKUP"]


def test_context_and_unicode_evidence(geometry):
    doc = parse(geometry)
    ctx = doc["tables"][0]["context"]
    before = doc["blocks"][ctx["preceding"]["block_id"]]["evidence_text"]
    after = doc["blocks"][ctx["following"]["block_id"]]["evidence_text"]
    assert before == "Synthetic hosts for the example service."
    assert after == "Values are illustrative 🙂 only."
    # Offsets are code points: the emoji is one position.
    assert after.index("🙂") == 24 and after[25:] == " only."
    # Context inside a cell stays inside the cell.
    inner_ctx = doc["tables"][1]["context"]
    assert doc["blocks"][inner_ctx["preceding"]["block_id"]]["evidence_text"] == "see below"


def test_identities_are_deterministic_and_content_bound(geometry):
    a, b = parse(geometry), parse(geometry)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    changed = parse(geometry.replace("db02", "db03"))
    assert changed["snapshot_id"] != a["snapshot_id"]
    assert changed["artifact_id"] != a["artifact_id"]
    assert changed["tables"][0]["table_id"] != a["tables"][0]["table_id"]
    assert all(t["table_id"].startswith("tbl_") for t in a["tables"])
    other_rep = parse(geometry, representation="export_view")
    assert other_rep["tables"][0]["table_id"] != a["tables"][0]["table_id"]


def test_export_view_uses_html_adapter():
    body = (
        '<div class="table-wrap"><table class="confluenceTable"><tbody>'
        "<tr><th>K</th><th>V</th></tr><tr><td>a</td><td>1<br/>2</td></tr></tbody></table></div>"
        "<p>after</p>"
    )
    doc = parse(body, representation="export_view")
    t = doc["tables"][0]
    assert kinds(t) == [["origin", "origin"], ["origin", "origin"]]
    cell = t["cells"][3]
    assert doc["blocks"][cell["content"][0]["block_id"]]["evidence_text"] == "1\n2"
    # The table-wrap div is climbed, so the following paragraph is found.
    assert doc["blocks"][t["context"]["following"]["block_id"]]["evidence_text"] == "after"


def test_recovery_is_reported():
    doc = parse("<table><tr><td>x</p></td></tr></table>")
    assert doc["status"] == "recovered"
    assert "PARSER_RECOVERY_APPLIED" in codes(doc["diagnostics"])


def test_resource_limits_are_not_truncated_results():
    rows = "".join(f"<tr><td>{i}</td></tr>" for i in range(5))
    config = ParseConfig(limits=Limits(max_rows=3))
    t = parse(f"<table>{rows}</table>", config=config)["tables"][0]
    assert t["geometry_status"] == "unsupported_due_to_limit"
    assert "RESOURCE_LIMIT_EXCEEDED" in codes(t["diagnostics"])
    big = parse("<p>" + "x" * 50 + "</p>", config=ParseConfig(limits=Limits(max_body_bytes=10)))
    assert big["status"] == "unsupported_due_to_limit" and big["tables"] == []


def test_nesting_depth_limit():
    body = "<table><tr><td>" * 3 + "x" + "</td></tr></table>" * 3
    tables = parse(body, config=ParseConfig(limits=Limits(max_depth=2)))["tables"]
    assert [t["geometry_status"] for t in tables] == [
        "valid",
        "valid",
        "unsupported_due_to_limit",
    ]


def test_wide_colspan_hits_column_limit():
    result = layout([RowInput("g", [CellInput("c1", colspan="300")])], Limits(max_cols=200))
    assert result.status == "unsupported_due_to_limit"


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        (None, "rowspan", (1, False)),
        ("3", "rowspan", (3, False)),
        ("0", "rowspan", (0, False)),
        ("0", "colspan", (1, True)),
        ("-1", "rowspan", (1, True)),
        ("2px", "colspan", (1, True)),
        ("5000", "colspan", (1000, True)),
    ],
)
def test_parse_span(raw, kind, expected):
    value, problem = parse_span(raw, kind)
    assert (value, problem is not None) == expected


def test_ragged_rows_get_empty_slots():
    result = parse("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>")
    t = result["tables"][0]
    assert kinds(t)[1] == ["origin", "empty"]
    assert "NON_RECTANGULAR_GRID" in codes(t["diagnostics"])
    assert t["geometry_status"] == "valid"


def test_parsing_opens_no_sockets(geometry, monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("parser tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    parse(geometry)
    parse('<!DOCTYPE x SYSTEM "http://example.com/x.dtd"><p>&ext;</p>', "export_view")


def test_structure_imports_no_clients():
    import confluence_mirror.inspection as inspection
    import confluence_mirror.structure.document as document

    for module in (inspection, document):
        names = set(vars(module))
        assert "httpx" not in names and "ConfluenceClient" not in names
