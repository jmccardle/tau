"""A pytest plugin for the diagram build: names each test as the scenario the
core's transition log (`tau._core`, built with the `transitions` feature)
attributes the following position changes to.

The core keeps one global scenario name, so a change made by a thread that outlives its test
(a worker, a task still winding down) would be charged to whichever test runs next, and the
scenario count would depend on thread timing. After each test the name is therefore set to
`BETWEEN`, and `positions.py` drops what is recorded under it."""

import pytest

from tau import _core

BETWEEN = "py::(between tests)"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _core.transitions_scenario(f"py::{item.nodeid}")  # type: ignore[attr-defined]


def pytest_runtest_logfinish(nodeid: str, location: object) -> None:
    _core.transitions_scenario(BETWEEN)  # type: ignore[attr-defined]
