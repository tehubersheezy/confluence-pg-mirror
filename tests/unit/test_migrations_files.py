import re

from confluence_mirror.db import available_migrations

TABLES = [
    "spaces",
    "space_properties",
    "pages",
    "page_content",
    "page_versions",
    "page_labels",
    "page_properties",
    "page_macros",
    "macro_extractions",
    "page_tables",
    "page_links",
    "attachments",
    "access_restrictions",
    "sync_runs",
]
# Additive confex tables (0004+), by schema. The fourteen mirror tables above stay as they are.
ADDITIONS = {
    "parse_artifacts": "extracted",
    "table_occurrences": "extracted",
    "search_blocks": "extracted",
    "table_correspondence": "extracted",
    "superseded_bodies": "source",
    "correspondence_decisions": "review",
    **{
        t: "pipeline"
        for t in (
            "runs",
            "run_items",
            "attempts",
            "classifications",
            "units",
            "candidate_revisions",
            "validation_results",
            "entity_match_proposals",
            "template_detections",
        )
    },
    "review_events": "review",
    "exports": "review",
    "export_items": "review",
}


def test_migrations_are_ordered_and_contiguous():
    versions = [m.version for m in available_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_mirror_tables_plus_confex_additions_are_defined():
    sql = "\n".join(m.sql for m in available_migrations())
    created = re.findall(r"CREATE TABLE (\w+)\.(\w+)", sql)
    schemas = {name: schema for schema, name in created}
    assert len(schemas) == len(created)  # no table defined twice
    assert sorted(schemas) == sorted(TABLES + list(ADDITIONS))
    assert {
        schemas[t] for t in ("page_macros", "macro_extractions", "page_tables", "page_links")
    } == {"extracted"}
    assert schemas["sync_runs"] == "sync"
    assert {name: schemas[name] for name in ADDITIONS} == ADDITIONS
