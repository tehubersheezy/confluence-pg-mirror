"""Structural parsing of body captures: document scope, tables, cells and geometry.

Pure code (no database or network I/O). Output is JSON-native so artifacts can be stored
and reloaded unchanged; see :func:`parse_document`.
"""

from confluence_mirror.structure.document import (
    PARSER_VERSION,
    REPRESENTATIONS,
    ParseConfig,
    artifact_identity,
    parse_document,
)

__all__ = [
    "PARSER_VERSION",
    "REPRESENTATIONS",
    "ParseConfig",
    "artifact_identity",
    "parse_document",
]
