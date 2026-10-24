"""Run every extractor over one storage body."""

from __future__ import annotations

from dataclasses import dataclass, field

from confluence_mirror.extract.links import LinkContext, link_rows
from confluence_mirror.extract.macros import macro_extractions, macro_rows
from confluence_mirror.extract.storage import parse_storage
from confluence_mirror.extract.tables import table_rows

# Bump when extractor output changes so rows can be re-derived selectively.
EXTRACTOR_VERSION = "1.0"


@dataclass
class ExtractionResult:
    macros: list[dict] = field(default_factory=list)
    macro_extractions: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    extractor_version: str = EXTRACTOR_VERSION


def extract(
    storage: str | None,
    *,
    page_id: int | None = None,
    space_key: str | None = None,
    base_url: str | None = None,
) -> ExtractionResult:
    if not storage:
        return ExtractionResult()
    root = parse_storage(storage)
    macros, index = macro_rows(root)
    return ExtractionResult(
        macros=macros,
        macro_extractions=macro_extractions(index, macros),
        tables=table_rows(root, index),
        links=link_rows(root, index, LinkContext(page_id, space_key, base_url)),
    )
