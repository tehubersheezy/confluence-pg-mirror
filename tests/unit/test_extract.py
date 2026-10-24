from confluence_mirror.extract import EXTRACTOR_VERSION, extract
from confluence_mirror.extract.links import LinkContext, classify_href
from confluence_mirror.extract.storage import parse_storage, text_of

BASE = "https://confluence.example.com"


def run(storage):
    return extract(storage, page_id=1001, space_key="DEMO", base_url=BASE)


def by_name(result):
    return {m["macro_name"]: m for m in result.macros}


def test_macros_nesting_parameters_and_bodies(rich_storage):
    result = run(rich_storage)
    names = [m["macro_name"] for m in result.macros]
    assert names == ["info", "code", "details", "status", "include"]
    macros = by_name(result)
    assert macros["info"]["macro_id"] == "m-info-1"
    assert macros["info"]["parameters"] == {"title": "Heads up"}
    assert macros["info"]["body_type"] == "rich-text"
    assert macros["code"]["parent_macro_index"] == macros["info"]["macro_index"]
    assert macros["code"]["depth"] == 1
    assert macros["code"]["body_type"] == "plain-text"
    assert macros["code"]["body_text"] == 'print("a < b & c")'
    # CDATA is preserved verbatim in the provenance markup.
    assert "<![CDATA[" in macros["code"]["source_markup"]
    assert "xmlns" not in macros["info"]["source_markup"]
    assert macros["status"]["element_path"].startswith("/ac:structured-macro[2]/")
    assert macros["include"]["parameters"][""]["refs"] == [
        {"type": "page", "content-title": "Shared Snippet"}
    ]
    # The nested macro is part of the outer body's storage, not lost.
    assert 'ac:name="code"' in macros["info"]["body_storage"]


def test_macro_extractions(rich_storage):
    result = run(rich_storage)
    got = {(e["extraction_type"]): e["data"] for e in result.macro_extractions}
    assert got["code"]["language"] == "python"
    assert got["panel"] == {
        "panel_type": "info",
        "title": "Heads up",
        "text": 'Outer body with a @u-123. print("a < b & c")',
    }
    assert got["page-properties"]["properties"] == [
        {"key": "Owner", "value": "@u-456"},
        {"key": "Status", "value": "DONE"},
    ]
    assert got["status"]["colour"] == "Green"
    assert got["include"]["target_title"] == "Shared Snippet"
    assert result.extractor_version == EXTRACTOR_VERSION


def test_tables(rich_storage):
    tables = run(rich_storage).tables
    assert len(tables) == 3
    props, data, nested = tables
    assert props["macro_index"] == 2 and props["header"] is None
    assert data["header"] == ["Name", "Value"]
    assert data["records"] == [{"Name": "alpha", "Value": "1"}, {"Name": "beta", "Value": "nested"}]
    assert data["row_count"] == 3  # nested table rows are not counted
    assert nested["parent_table_index"] == data["table_index"]
    assert nested["cells"] == [[{"text": "nested", "is_header": False, "colspan": 1, "rowspan": 1}]]


def test_table_spans_and_duplicate_headers():
    storage = (
        "<table><tr><th>A</th><th>A</th><th></th></tr>"
        "<tr><td colspan='2'>wide</td><td>x</td></tr></table>"
    )
    table = run(storage).tables[0]
    assert table["header"] == ["A", "A_2", "column_3"]
    assert table["column_count"] == 3
    assert table["records"] is None  # colspan makes alignment ambiguous


def test_links(rich_storage):
    links = run(rich_storage).links
    summary = [
        (
            lk["link_type"],
            lk["source_element"],
            lk["target_page_id"],
            lk["target_space_key"],
            lk["target_title"],
            lk["target_filename"],
        )
        for lk in links
    ]
    assert summary == [
        ("page", "ac:link", None, "DEMO", "Child Page", None),
        ("page", "ac:link", None, "OTHER", "Elsewhere", None),
        ("page", "a", 1003, None, None, None),
        ("page", "a", None, "DEMO", "Root Page", None),
        ("url", "a", None, None, None, None),
        ("anchor", "a", 1001, None, None, None),
        ("user", "ac:link", None, None, None, None),
        ("user", "ac:link", None, None, None, None),
        ("include", "macro:include", None, "DEMO", "Shared Snippet", None),
        ("attachment", "ac:image", 1001, "DEMO", None, "diagram.png"),
        ("attachment", "ac:link", None, "DEMO", "Child Page", "spec.pdf"),
    ]
    assert links[0]["link_text"] == "the child"
    assert links[3]["anchor"] == "top"
    assert links[6]["macro_index"] == 0 and links[6]["target_user_key"] == "u-123"
    assert [lk["link_index"] for lk in links] == list(range(len(links)))


def test_classify_href_variants():
    ctx = LinkContext(1, "DEMO", "https://confluence.example.com/wiki")
    assert classify_href("/wiki/spaces/OPS/pages/42/Title", ctx)["target_page_id"] == 42
    assert classify_href("/wiki/display/~jdoe", ctx)["link_type"] == "user"
    att = classify_href("/wiki/download/attachments/7/a%20b.pdf?version=2", ctx)
    assert (att["link_type"], att["target_page_id"], att["target_filename"]) == (
        "attachment",
        7,
        "a b.pdf",
    )
    t = classify_href("/wiki/pages/viewpage.action?spaceKey=OPS&title=Run+book", ctx)
    assert (t["target_space_key"], t["target_title"]) == ("OPS", "Run book")
    assert classify_href("https://other.example.org/display/DEMO/X", ctx)["link_type"] == "url"
    assert classify_href("mailto:someone@example.com", ctx)["link_type"] == "url"


def test_entities_and_text():
    root = parse_storage("<p>a&nbsp;b &mdash; c &amp; d &bogus;</p><p>next</p>")
    # &nbsp; becomes U+00A0 (whitespace, so normalised); unknown entities stay literal text.
    assert text_of(root) == "a b \u2014 c & d &bogus; next"


def test_malformed_markup_is_recovered():
    result = run("<p>unclosed <b>bold<p>next <ac:link><ri:page ri:content-title='X'/></ac:link>")
    assert any(lk["target_title"] == "X" for lk in result.links)


def test_external_entities_are_not_resolved(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    storage = f'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file://{secret}">]><p>&xxe;</p>'
    root = parse_storage(storage)
    assert "TOP-SECRET" not in text_of(root)


def test_legacy_macro_and_default_parameter():
    result = run(
        '<ac:macro ac:name="Anchor"><ac:default-parameter>here</ac:default-parameter></ac:macro>'
    )
    assert result.macros[0]["macro_name"] == "anchor"
    assert result.macros[0]["parameters"] == {"": "here"}
    assert result.macro_extractions[0]["data"] == {"name": "here"}


def test_empty_storage():
    result = run("")
    assert result.macros == result.tables == result.links == []
