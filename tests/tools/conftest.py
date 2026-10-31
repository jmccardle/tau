"""Fixtures for the base tools."""

from pathlib import Path

import pytest


@pytest.fixture
def overflow(tmp_path: Path) -> Path:
    """A directory for the overflow files, apart from the files the test works on."""
    return tmp_path / "overflow"
