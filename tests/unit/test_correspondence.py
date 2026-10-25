from confluence_mirror import correspondence as corr
from confluence_mirror.structure import parse_document

T1 = "<table><tr><th>K</th><th>V</th></tr><tr><td>a</td><td>1</td></tr></table>"
T2 = (
    "<table><tr><th>Host</th></tr>"
    + "".join(f"<tr><td>db0{i}</td></tr>" for i in range(1, 6))
    + "</table>"
)


def art(body, rep, version=1):
    return parse_document(body, page_id=9, version=version, representation=rep)


def states(rows):
    return [(r["left_ordinal"], r["right_ordinal"], r["state"], r["method"]) for r in rows]


def test_one_to_one_is_confirmed_and_order_is_not_trusted():
    storage = art(f"<h2>A</h2>{T1}<h2>B</h2>{T2}", "storage")
    # Rendered output with the tables in the other order: matching must use content.
    rendered = art(f"<div><h2>B</h2>{T2}<h2>A</h2>{T1}</div>", "export_view")
    rows = corr.match_tables(storage, rendered)
    assert states(rows) == [
        (1, 2, "confirmed", "content+geometry"),
        (2, 1, "confirmed", "content+geometry"),
    ]


def test_identical_tables_need_more_than_text():
    storage = art(f"<h2>A</h2>{T1}<h2>B</h2>{T1}", "storage")
    by_heading = corr.match_tables(storage, art(f"<h2>A</h2>{T1}<h2>B</h2>{T1}", "export_view"))
    assert [r["method"] for r in by_heading] == ["content+geometry+heading"] * 2
    # Without distinguishing headings only order pairs them: tentative, never confirmed.
    same = art(f"{T1}{T1}", "storage")
    rows = corr.match_tables(same, art(f"{T1}{T1}", "export_view"))
    assert {r["state"] for r in rows} == {"tentative"}
    # Two storage copies but one rendered copy: conflicting.
    rows = corr.match_tables(same, art(T1, "export_view"))
    assert {r["state"] for r in rows} == {"conflicting"}
    assert rows[0]["evidence"]["candidates"]


def test_similar_text_is_tentative_and_render_only_is_unmatched():
    storage = art(T2, "storage")
    rendered = art(
        T2.replace("db02", "db02 (primary)") + "<table><tr><td>macro output</td></tr></table>",
        "export_view",
    )
    rows = corr.match_tables(storage, rendered)
    assert states(rows) == [
        (1, 1, "tentative", "geometry+similar_text"),
        (None, 2, "unmatched", "none"),
    ]


def test_selection_prefers_storage_and_blocks_tentative():
    storage = art(f"<h2>A</h2>{T1}{T2}", "storage")
    rendered = art(
        f"<h2>A</h2>{T1}{T2.replace('db02', 'db02*')}"
        "<table><tr><td>only rendered</td></tr></table>",
        "export_view",
    )
    arts = {"storage": storage, "export_view": rendered}
    rows = corr.match_tables(storage, rendered)
    sel = corr.select_for_extraction(arts, rows)
    by_table = {s["table_id"]: s for s in sel}
    t1, t2 = storage["tables"][0]["table_id"], storage["tables"][1]["table_id"]
    assert by_table[t1]["representation"] == "storage" and not by_table[t1]["blocked"]
    assert by_table[t1]["alternates"][0]["representation"] == "export_view"
    assert by_table[t2]["blocked"]  # tentative counterpart: no automatic double counting
    render_only = rendered["tables"][2]["table_id"]
    assert by_table[render_only]["representation"] == "export_view"
    assert "render-only" in by_table[render_only]["reason"]
    assert len(sel) == 4  # t2 and its tentative counterpart stay separate, both blocked
    tentative = next(r for r in rows if r["state"] == "tentative")
    resolved = corr.select_for_extraction(arts, rows, {tentative["correspondence_id"]: "confirmed"})
    assert len(resolved) == 3 and not any(s["blocked"] for s in resolved)
    distinct = corr.select_for_extraction(arts, rows, {tentative["correspondence_id"]: "distinct"})
    assert len(distinct) == 4 and not any(s["blocked"] for s in distinct)


def test_ids_are_deterministic():
    a, b = art(T1, "storage"), art(T1, "export_view")
    assert corr.match_tables(a, b) == corr.match_tables(a, b)


def test_compare_reports_cell_differences():
    a = art(T2, "storage")["tables"][0]
    b = art(T2.replace("db01", "db07"), "storage")["tables"][0]
    out = corr.compare(a, b)
    assert out["same_geometry"] and not out["same_text"]
    assert out["cell_differences"] == [{"coord": "R2C1", "left": "db01", "right": "db07"}]
