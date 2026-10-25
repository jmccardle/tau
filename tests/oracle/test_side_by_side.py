"""Phase 0 exit: τ 0.15 and tau_next import into one interpreter.

The Phase 2 behavior suite runs both implementations from the same process,
so a name or dependency collision must fail here first.
"""

import importlib
from importlib.metadata import version

import pytest

import tau_next

TAU_015 = ["tau_llm", "tau_agent_core", "tau_coding_agent"]


@pytest.mark.oracle
def test_both_generations_import_together() -> None:
    for name in TAU_015:
        importlib.import_module(name)
    assert version("ffwf-tau") == "0.15.0"
    assert tau_next.__version__.startswith("0.16.")
