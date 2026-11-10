"""`tau` itself, in a pseudo-terminal: the launch from `tau.cli.main` to the screen and back (D23
section 1). A real process, so the whole path runs: the terminal check, the theme, `open_client`,
`run_async` on the driver's loop, `Opened.close`. The mock model, a throwaway TAU_HOME."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import json
import time

from ptykit import Screen
from world import World, tau

from tau import Body, Session, list_sessions

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"


def test_tau_opens_the_screen_answers_and_leaves(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    screen.type("hello there\r")
    screen.wait_for("mock: hello there")
    assert screen.quit() == 0


def test_tau_attaches_to_a_server_on_its_own_socket(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    served.session(OLD, cwd=work)
    custom = served.home / "mine.sock"
    started = tau(served, "serve", "-d", "--session", OLD, "--socket", str(custom), cwd=work)
    assert started.code == 0, started.err
    screen = Screen(served, "--session", OLD, cwd=work)
    screen.wait_for(OLD)
    screen.type("over the wire\r")
    screen.wait_for("mock: over the wire")
    assert screen.quit() == 0


def test_the_cursor_keys_reach_the_app_in_a_terminal(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    screen.type("edit me\r")
    screen.wait_for("mock: edit me")
    screen.type("\x07")  # Ctrl+G: the tree
    screen.wait_for("o open here")
    screen.wait_for("user: edit me")
    screen.type("\x1b")  # Esc leaves it
    time.sleep(0.3)
    screen.type("\x1a")  # Ctrl+Z, which a terminal would take to stop the process
    time.sleep(0.5)
    screen.type("\x1b[1;3C")  # Alt+Right: one cursor, a toast
    screen.wait_for("there is only one cursor")
    screen.type("!\r")  # the edited message goes out as a new branch
    screen.wait_for("mock: edit me!")
    assert screen.quit() == 0
    (info,) = list_sessions(served.tau / "sessions")
    session = Session.load(info.path)
    users = [
        n
        for n in (session.tree.node(i) for i in range(len(session.tree)))
        if isinstance(n.body, Body.User)
    ]
    assert [(n.parent, n.body.text) for n in users if isinstance(n.body, Body.User)] == [
        (1, "edit me"),
        (1, "edit me!"),
    ]
    session.close()


def test_a_theme_picked_in_the_palette_is_kept_for_the_next_tau(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    screen.type("\x10")  # Ctrl+P: the command palette
    screen.wait_for("Theme")
    screen.type("theme")
    time.sleep(0.5)
    screen.type("\r")
    screen.wait_for("type to filter")
    screen.type("latte\r")
    screen.wait_for("saved for every session")
    assert screen.quit() == 0
    kept = json.loads((served.tau / "config.json").read_text(encoding="utf-8"))
    assert kept["tui"] == {"theme": "latte"}, "and the file's other keys are still there"
    assert kept["default_model"] == "m"
    again = Screen(served, "-m", "m", cwd=work)
    again.wait_for("idle")
    assert b"239;241;245" in again.seen, "the second tau wears latte (its background colour)"
    assert again.quit() == 0


def test_the_screen_starts_with_session_settings_and_says_what_the_config_lacks(
    served: World,
) -> None:
    served.user_config({**MOCK, "load": {"deleted": True}})
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    env = {"TAU_SETTINGS": json.dumps({"load": {"retry": False}})}
    screen = Screen(served, "-m", "m", "--verbose", cwd=work, env=env)
    screen.wait_for("idle")
    shown = screen.text()
    assert "warning: load.deleted: no extension source has the key 'deleted'" in shown
    assert "tau: extension step_limit" in shown
    assert "tau: retry (" not in shown
    assert screen.quit() == 0
    plain = Screen(served, "-m", "m", "--verbose", cwd=work)
    plain.wait_for("idle")
    assert "tau: retry (" in plain.text(), "without the variable, retry is on"
    assert plain.quit() == 0
