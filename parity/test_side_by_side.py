"""τ 0.15 and tau_next live in one interpreter.

Parity tests drive both implementations from the same process, so a name or
dependency collision, or 0.15 turning the GIL back on underneath us, must fail
here first.
"""

import importlib
import sys
import sysconfig
from importlib.metadata import version

import pytest

import tau_next

# Everything `ffwf-tau==0.15.0` installs. tau_jmfts is a separate, optional
# distribution and not part of the oracle.
TAU_015 = ["tau_llm", "tau_agent_core", "tau_coding_agent", "tau_meta"]


def import_015() -> None:
    for name in TAU_015:
        importlib.import_module(name)


def test_both_generations_import_together() -> None:
    import_015()
    assert version("ffwf-tau") == "0.15.0"
    assert tau_next.__version__.startswith("0.16.")


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="GIL build: nothing to re-enable"
)
def test_both_generations_keep_gil_disabled() -> None:
    import_015()
    assert not sys._is_gil_enabled()  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]  # 3.13+
