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
# Additive confex tables (0004); the fourteen agreed mirror tables above stay as they are.
CONFEX_TABLES = ["parse_artifacts", "table_occurrences"]


def test_migrations_are_ordered_and_contiguous():
    versions = [m.version for m in available_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_mirror_tables_plus_confex_additions_are_defined():
    sql = "\n".join(m.sql for m in available_migrations())
    created = re.findall(r"CREATE TABLE (\w+)\.(\w+)", sql)
    assert sorted(name for _, name in created) == sorted(TABLES + CONFEX_TABLES)
    schemas = {name: schema for schema, name in created}
    assert {
        schemas[t]
        for t in ("page_macros", "macro_extractions", "page_tables", "page_links", *CONFEX_TABLES)
    } == {"extracted"}
    assert schemas["sync_runs"] == "sync"
