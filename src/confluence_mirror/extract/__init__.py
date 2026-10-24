"""Derived-data extraction from page storage bodies (pure; no database or network I/O)."""

from confluence_mirror.extract.pipeline import EXTRACTOR_VERSION, ExtractionResult, extract

__all__ = ["EXTRACTOR_VERSION", "ExtractionResult", "extract"]
