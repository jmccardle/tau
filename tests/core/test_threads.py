"""Free-threading: importing tau_next must not re-enable the GIL, and one
Session must be safe to call from many threads at once.

τ 0.15 ships on free-threaded 3.14t/3.15t; the rewrite may not regress that.
The same check with τ 0.15 imported alongside is in parity/.
"""

import random
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tau_next import Answer, Author, Body, Busy, Config, ItemData, Job, Session, WrongAnswer

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


EXIT_WHILE_STREAMING = """
import asyncio, sys, threading, time
from tau_next import BashTool

seen = []
tool = BashTool(".")

def stream() -> None:
    asyncio.run(tool.call(
        '{"command": "for i in $(seq 100); do echo tick; sleep 0.02; done", "timeout": 60000}',
        progress=seen.append,
    ))

threading.Thread(target=stream, daemon=True).start()
while not seen:
    time.sleep(0.01)
sys.exit(0)  # the command still runs and its output still arrives
"""


def test_exiting_while_bash_streams_progress_ends_cleanly() -> None:
    """The progress thread must not attach to an interpreter that is finalizing."""
    for _ in range(5):
        done = subprocess.run(
            [sys.executable, "-I", "-c", EXIT_WHILE_STREAMING],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert (done.returncode, done.stderr) == (0, "")


@pytest.mark.parametrize("on_disk", [False, True], ids=["memory", "directory"])
def test_threads_drive_one_cursor_without_deadlock_or_corruption(
    tmp_path: Path, on_disk: bool
) -> None:
    """Many threads run the job loop, move, and abort one cursor at once. A call
    that loses the race gets a typed refusal; none hangs, and what is left is a
    consistent session that reloads as it stands."""
    directory = tmp_path / "s" if on_disk else None
    session = Session.create(Config("{}"), "sys", 0, directory)
    cursor = session.cursors()[0]
    root = session.cursor(cursor).at
    lost_race = (Busy, WrongAnswer)

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        for n in range(CALLS * 5):
            try:
                match rng.randrange(5):
                    case 0:
                        session.input(cursor, "term", f"hi {n}")
                    case 1:
                        if isinstance(session.next_job(cursor), Job.Hooks):
                            session.answer(cursor, Answer.Hooks(None, None), n)
                    case 2:
                        session.go_to(cursor, root, "term", n)
                    case 3:
                        session.abort(cursor)
                    case _:
                        session.cursor(cursor).moves  # noqa: B018 - a read under the same lock
            except lost_race:
                pass

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        for done in [pool.submit(hammer, seed) for seed in range(THREADS)]:
            done.result(timeout=120)
    for _ in range(1000):  # settle: hooks answered, any sample or tool run aborted
        match session.next_job(cursor):
            case Job.Rest():
                break
            case Job.Hooks():
                session.answer(cursor, Answer.Hooks(None, None), 0)
            case _:
                session.abort(cursor)
    assert not session.cursor(cursor).busy
    nodes = len(session.tree)
    assert all(session.tree.walk(i)[-1].id == i for i in range(nodes))
    at = session.cursor(cursor).at
    if directory is not None:
        session.close()
        loaded = Session.load(directory)
        assert (loaded.cursor(cursor).at, len(loaded.tree)) == (at, nodes)


# `stop` once held the tracer lock while it waited for the file with the GIL
# released; a thread holding the GIL in `trace.span` then waited on that lock.
TRACE_WHILE_SPANNING = """
import sys, tempfile, threading
from pathlib import Path
from tau_next import Config, Session, trace

session = Session.create(Config("{}"), "sys", 0)

running = True
errors = []

def spin() -> None:
    while running:
        try:
            with trace.span("driver", "spin"):
                session.cursors()  # native spans keep the writer thread busy
        except AttributeError:
            pass  # the span's list is dropped by a stop() mid-span: python-side, harmless
        except BaseException as e:
            errors.append(e)
            return

sys.setswitchinterval(1e-6)
spinners = [threading.Thread(target=spin, daemon=True) for _ in range(8)]
for thread in spinners:
    thread.start()
with tempfile.TemporaryDirectory() as tmp:
    for n in range(200):
        trace.start(Path(tmp) / f"{n}.json")
        for _ in range(300):
            session.cursors()  # a file with content takes the writer thread a while
        trace.stop()
running = False
for thread in spinners:
    thread.join()
sys.exit(1 if errors else 0)
"""


def test_stopping_traces_while_other_threads_span_does_not_freeze() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-c", TRACE_WHILE_SPANNING],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, "")
