"""Free-threading: importing tau_next (alone, and beside τ 0.15) must not
re-enable the GIL, and the native module must be safe to call from many
threads at once.

τ 0.15 ships on free-threaded 3.14t/3.15t; the rewrite may not regress that.
"""

import importlib
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

import pytest

from tau_next import Probe, echo

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled() -> bool:
    return sys._is_gil_enabled()  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]  # 3.13+


@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_keeps_gil_disabled() -> None:
    assert not gil_enabled()


@pytest.mark.oracle
@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_beside_015_keeps_gil_disabled() -> None:
    for name in ["tau_llm", "tau_agent_core", "tau_coding_agent", "tau_meta"]:
        importlib.import_module(name)
    assert not gil_enabled()


def test_concurrent_calls_share_one_probe() -> None:
    shared = Probe(1, "shared", [str(i) for i in range(64)])
    expected = shared.digest()

    def hammer(_: int) -> bool:
        return all(echo(shared).digest() == expected for _ in range(2_000))

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(hammer, range(64)))
