"""The TUI suite runs in both Harness modes (D23 section 9): the app's client is local, then
remote over a real Unix socket. Passing in both holds the app to the contract over the wire."""

from collections.abc import Iterator

import pytest
from harness import Harness


@pytest.fixture(autouse=True, params=["local", "remote"])
def harness_mode(request: pytest.FixtureRequest) -> Iterator[None]:
    Harness.mode = request.param
    yield
    Harness.mode = "local"
