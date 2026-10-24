from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rich_storage() -> str:
    return (FIXTURES / "storage" / "rich_page.xml").read_text(encoding="utf-8")
