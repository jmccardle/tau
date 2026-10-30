from pathlib import Path

import pytest
from world import World


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home, home / ".tau", {"HOME": str(home)})
