"""Free-threading: importing tau_next must not re-enable the GIL, and one
Session must be safe to call from many threads at once.

τ 0.15 ships on free-threaded 3.14t/3.15t; the rewrite may not regress that.
The same check with τ 0.15 imported alongside is in parity/.
"""

import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from drive import run, say

from tau_next import Author, Body, Config, Session

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
THREADS = 16
TURNS = 20


def gil_enabled() -> bool:
    return sys._is_gil_enabled()  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]  # 3.13+


@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_keeps_gil_disabled() -> None:
    assert not gil_enabled()


@pytest.mark.parametrize("on_disk", [False, True], ids=["memory", "directory"])
def test_threads_share_one_session(tmp_path: Path, on_disk: bool) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s" if on_disk else None)
    root = session.at(session.cursors()[0])
    cursors = [session.open(root, Author.Core(), 0) for _ in range(THREADS)]

    def turns(cursor: int) -> int:
        run(session, cursor)  # the open's own wake
        for n in range(TURNS):
            session.send(cursor, "term", f"hi {n}")
            run(session, cursor, iter([say(f"re {n}")]), n)
            session.cursors()
            session.walk(session.at(cursor))
        return len(session.walk(session.at(cursor)))

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        lengths = list(pool.map(turns, cursors))
    assert lengths == [2 + 2 * TURNS] * THREADS
    for cursor in cursors:
        walk = session.walk(session.at(cursor))
        texts = [n.body.text for n in walk if isinstance(n.body, Body.User)]
        assert texts == [f"hi {n}" for n in range(TURNS)]
