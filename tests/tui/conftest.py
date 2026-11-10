"""The TUI suite runs in both Harness modes (D23 section 9): the app's client is local, then
remote over a real Unix socket. Passing in both holds the app to the contract over the wire."""

import sys
from collections.abc import Iterator

import pytest
from harness import Harness


@pytest.fixture(autouse=True, params=["local", "remote"])
def harness_mode(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.param == "remote" and sys.platform == "win32":
        pytest.skip("Windows: no wire to run remote over (tau serve is not ported, D21)")
    Harness.mode = request.param
    yield
    Harness.mode = "local"
