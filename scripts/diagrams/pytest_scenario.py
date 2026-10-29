"""A pytest plugin for the diagram build: names each test as the scenario the
core's transition log (`tau_next._core`, built with the `transitions` feature)
attributes the following position changes to."""

import pytest

from tau_next import _core


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _core.transitions_scenario(f"py::{item.nodeid}")  # type: ignore[attr-defined]
