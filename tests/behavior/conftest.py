"""The behavior suite runs twice (D21 section 9): once with the `Harness`'s clients local, once
remote, over a real Unix socket. Passing in both is the wire's exit test. A test of something a
wire cannot carry (an object dropped without `close`, an exception's `__cause__`) is marked
`local_only("why")`, and `tests/serve` has the wire's own test."""

from collections.abc import Iterator

import pytest
from harness import Harness


@pytest.fixture(autouse=True, params=["local", "remote"])
def harness_mode(request: pytest.FixtureRequest) -> Iterator[None]:
    skip = request.node.get_closest_marker("local_only")
    if request.param == "remote" and skip is not None:
        pytest.skip(f"local clients only: {skip.args[0]}")
    Harness.mode = request.param
    yield
    Harness.mode = "local"
