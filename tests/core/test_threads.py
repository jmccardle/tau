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

from tau_next import Author, Body, Config, ItemData, Session

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
THREADS = 16
CALLS = 20


def gil_enabled() -> bool:
    return sys._is_gil_enabled()  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]  # 3.13+


@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_keeps_gil_disabled() -> None:
    assert not gil_enabled()


@pytest.mark.parametrize("on_disk", [False, True], ids=["memory", "directory"])
def test_threads_share_one_session(tmp_path: Path, on_disk: bool) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s" if on_disk else None)
    root = session.cursor(session.cursors()[0]).at
    cursors = [session.open(root, Author.Core(), 0) for _ in range(THREADS)]

    def calls(cursor: int) -> None:
        for n in range(CALLS):
            session.input(cursor, "term", f"hi {n}")
            session.add_item(cursor, ItemData.Extension("timer", f"tick {n}"))
            session.set_label(f"cursor {cursor}", cursor)
            session.cursors()
            session.tree.walk(session.cursor(cursor).at)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(calls, cursors))
    for cursor in cursors:
        items = [i.data for i in session.cursor(cursor).items]
        assert items == [
            data
            for n in range(CALLS)
            for data in (
                ItemData.Input("term", f"hi {n}"),
                ItemData.Extension("timer", f"tick {n}"),
            )
        ]
        assert session.label(f"cursor {cursor}") == cursor
    assert len(session.tree.walk(root)) == 2, "no thread wrote a node"
    assert isinstance(session.tree.node(root).body, Body.System)
