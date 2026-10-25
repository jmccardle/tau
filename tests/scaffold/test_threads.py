"""Free-threading: importing tau_next must not re-enable the GIL, and the
native module must be safe to call from many threads at once.

τ 0.15 ships on free-threaded 3.14t/3.15t; the rewrite may not regress that.
"""

import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

import pytest

from tau_next import Probe, echo

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_keeps_gil_disabled() -> None:
    assert not sys._is_gil_enabled()  # pyright: ignore[reportAttributeAccessIssue]  # 3.13+


def test_concurrent_calls_share_one_probe() -> None:
    shared = Probe(1, "shared", [str(i) for i in range(64)])
    expected = shared.digest()

    def hammer(_: int) -> bool:
        return all(echo(shared).digest() == expected for _ in range(2_000))

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(hammer, range(64)))
