"""The behavior suite runs three times (D21 section 9): with the `Harness`'s clients local, then
remote over a real Unix socket (passing in both is the wire's exit test), then local on a loop
with `asyncio.eager_task_factory`, which Textual installs: a task starts running inside
`create_task`. A test of something a wire cannot carry (an object dropped without `close`, an
exception's `__cause__`) is marked `local_only("why")`, and `tests/serve` has the wire's own
test."""

from collections.abc import Iterator

import pytest
from harness import Harness


@pytest.fixture(autouse=True, params=["local", "remote", "eager"])
def harness_mode(request: pytest.FixtureRequest) -> Iterator[None]:
    skip = request.node.get_closest_marker("local_only")
    if request.param == "remote" and skip is not None:
        pytest.skip(f"local clients only: {skip.args[0]}")
    Harness.mode = "remote" if request.param == "remote" else "local"
    Harness.eager = request.param == "eager"
    yield
    Harness.mode = "local"
    Harness.eager = False
