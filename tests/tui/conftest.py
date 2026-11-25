"""The TUI suite runs in both Harness modes (D23 section 9): the app's client is local, then
remote over a real Unix socket (on Windows, which has none: over the HTTP edge). Passing in both
holds the app to the contract over the wire."""

import sys
from collections.abc import Iterator

import pytest
from harness import Harness


@pytest.fixture(autouse=True, params=["local", "remote"])
def harness_mode(request: pytest.FixtureRequest) -> Iterator[None]:
    wired = "http" if sys.platform == "win32" else "remote"
    Harness.mode = wired if request.param == "remote" else "local"
    yield
    Harness.mode = "local"
