"""Choosing and switching sessions in a terminal of its own (D23 section 5): `tau -r` shows the
picker first; Ctrl+O and Ctrl+N leave the screen and `tau` replaces itself (the same process id)
with `tau` started for the other session. A real process, a throwaway TAU_HOME, the mock model."""

import time
from pathlib import Path

from ptykit import Screen, lock_holder
from world import World, tau

from tau import list_sessions

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"
NEW = "20261031-101010-4f5a6b"
DOWN, CTRL_O, CTRL_N = "\x1b[B", "\x0f", "\x0e"


def work_of(world: World) -> Path:
    work = world.home / "work"
    work.mkdir(exist_ok=True)
    world.user_config(MOCK)
    return work


def ids(world: World) -> list[str]:
    return sorted(r.id for r in list_sessions(world.tau / "sessions"))


def wait_for_ids(world: World, wanted: list[str]) -> None:
    deadline = time.monotonic() + 20
    while ids(world) != wanted:
        assert time.monotonic() < deadline, f"sessions are {ids(world)}, not {wanted}"
        time.sleep(0.05)


def test_resume_shows_the_picker_first_and_opens_the_session_picked(served: World) -> None:
    work = work_of(served)
    served.session(OLD, cwd=work, says=["the old question"], age=900)
    served.session(NEW, cwd=work, name="the newer one", age=60)
    screen = Screen(served, "-r", cwd=work)
    screen.wait_for("Resume a session")
    screen.wait_for("the newer one")
    screen.wait_for("the old question")
    screen.type(DOWN + "\r")  # the second row, the older session
    screen.wait_for(OLD)  # the header of the screen that opened
    screen.type("hi\r")
    screen.wait_for("mock: hi")
    assert screen.quit() == 0
    assert ids(served) == [OLD, NEW]


def test_resume_marks_the_session_a_server_holds_and_attaches_to_it(served: World) -> None:
    work = work_of(served)
    served.session(OLD, cwd=work, says=["the old question"], age=900)
    started = tau(served, "serve", "-d", "--session", OLD, cwd=work)
    assert started.code == 0, started.err
    screen = Screen(served, "-r", cwd=work)
    screen.wait_for("served")
    screen.type("\r")
    screen.wait_for("idle", on_screen=True)  # the chat screen, not the picker's own line naming OLD
    screen.wait_for(OLD, on_screen=True)
    screen.type("over the wire\r")
    screen.wait_for("mock: over the wire")
    assert screen.quit() == 0


def test_ctrl_n_replaces_the_process_with_a_new_session_and_keeps_the_old(served: World) -> None:
    work = work_of(served)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    screen.type("first words\r")
    screen.wait_for("mock: first words")
    (first,) = ids(served)
    pid = screen.pid
    screen.type(CTRL_N)
    deadline = time.monotonic() + 20
    while len(ids(served)) < 2:
        screen.wait_for("idle")
        assert time.monotonic() < deadline, "no second session"
        time.sleep(0.05)
    (second,) = (i for i in ids(served) if i != first)
    screen.wait_for(second)
    log = served.tau / "sessions" / second / "log"
    assert lock_holder(log) == pid, "the process that held the first session now holds the new one"
    assert lock_holder(served.tau / "sessions" / first / "log") is None, "and let go of the first"
    assert screen.quit() == 0
    assert ids(served) == [first], "the new session was never used, so it is gone"


def test_ctrl_n_on_a_session_never_used_replaces_it(served: World) -> None:
    work = work_of(served)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    (first,) = ids(served)
    screen.type(CTRL_N)
    deadline = time.monotonic() + 20
    while ids(served) in ([first], []):
        screen.wait_for("idle")
        assert time.monotonic() < deadline, "the session was not replaced"
        time.sleep(0.05)
    (second,) = ids(served)
    assert second != first
    screen.wait_for(second)
    assert screen.quit() == 0
    assert ids(served) == []


def test_ctrl_o_picks_another_session_and_the_process_becomes_it(served: World) -> None:
    work = work_of(served)
    served.session(OLD, cwd=work, says=["the old question"], age=900)
    served.session(NEW, cwd=work, name="the newer one", age=60)
    screen = Screen(served, "--session", OLD, cwd=work)
    screen.wait_for(OLD)
    screen.type(CTRL_O)
    screen.wait_for("Open a session")
    screen.wait_for("the newer one")
    screen.type("\r")  # the first row: the newer session
    screen.wait_for(NEW)
    screen.type("hello\r")
    screen.wait_for("mock: hello")
    assert screen.quit() == 0


def test_ctrl_o_on_the_current_unused_session_changes_nothing(served: World) -> None:
    work = work_of(served)
    screen = Screen(served, cwd=work)
    screen.wait_for("idle")
    (first,) = ids(served)
    screen.type(CTRL_O)
    screen.wait_for("Open a session")
    screen.wait_for("current")
    screen.type("\r")  # the only row: this session
    time.sleep(1)
    assert ids(served) == [first], "the session was not closed, and so not removed"
    assert lock_holder(served.tau / "sessions" / first / "log") == screen.pid
    screen.type("still here\r")
    screen.wait_for("mock: still here")
    assert screen.quit() == 0


def test_ctrl_o_marks_a_session_another_tau_holds_and_does_not_leave_for_it(
    served: World,
) -> None:
    work = work_of(served)
    served.session(OLD, cwd=work, says=["the old question"], age=900)
    holder = Screen(served, "--session", OLD, cwd=work)
    holder.wait_for(OLD)
    screen = Screen(served, cwd=work)
    screen.wait_for("idle")
    screen.type("hello\r")
    screen.wait_for("mock: hello")
    screen.type(CTRL_O)
    screen.wait_for("in use")
    screen.type(DOWN + "\r")  # the row of the session the other tau holds
    screen.wait_for("cannot be opened")
    screen.type("\x1b")
    time.sleep(0.5)  # Esc alone, not the start of a key sequence
    screen.type("more\r")
    screen.wait_for("mock: more")
    assert screen.quit() == 0
    assert holder.quit() == 0
