"""Suite-wide enforcement of CLAUDE.md rule 4 (fakes at the effect boundary)."""

from pathlib import Path

import pytest
from world import World


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    patched = [i.nodeid for i in items if "monkeypatch" in getattr(i, "fixturenames", ())]
    if patched:
        raise pytest.UsageError(
            "rule 4: `monkeypatch` is patching; use FakeProvider/FakeTool instead:\n  "
            + "\n  ".join(patched)
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home, home / ".tau", {"HOME": str(home)})
