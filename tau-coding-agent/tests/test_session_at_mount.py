"""The TUI opens its session at mount, and an unused one leaves nothing on disk.

Issue #42: before any chat existed, ``/budget`` was called "not a command" and
Tab completion offered no extension commands, because extensions loaded only
when the first chat was created. The fix opens a deferred session at mount
(docs/SESSION-AT-MOUNT.md), so its hard requirement is tested here too: mounting
and quitting must write nothing under the tau home.
"""

from __future__ import annotations

import os
from pathlib import Path

from tau_agent_core.cursor import Cursor
from tau_coding_agent import chat_widgets, editor_widgets
from tau_coding_agent.backends import create_backend
from tau_coding_agent.session_picker import SessionPickerModal
from tau_coding_agent.session_store import FileSessionCatalog, Session

_BUDGET_EXT = """
def register(api):
    api.register_command(
        "budget", {"description": "show the budget", "handler": lambda args, ctx: "ok"}
    )
"""


_LIFECYCLE_EXT = """
import pathlib

OUT = pathlib.Path({out!r})


def register(api):
    def rec(kind):
        def handler(event, ctx):
            with OUT.open("a") as f:
                f.write(kind + ":" + event.get("reason", "") + "\\n")
        return handler

    api.on("session_start", rec("start"))
    api.on("session_shutdown", rec("shutdown"))
"""


def _lifecycle(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "lifecycle.log"
    ext = tmp_path / "lifecycle_ext.py"
    ext.write_text(_LIFECYCLE_EXT.format(out=str(out)))
    return out, ext


def _events(out: Path) -> list[str]:
    return out.read_text().splitlines() if out.exists() else []


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


async def _type(pilot, editor: chat_widgets.ChatInput, text: str) -> None:
    editor.text = text
    editor.move_cursor(editor.document.end)
    await pilot.pause()


async def test_quitting_an_untouched_startup_session_writes_nothing(make_app, tau_home):
    """Mount, open a second chat, quit: the session exists, the tau home is unchanged."""
    app = make_app(create_backend=create_backend)
    before = _files(tau_home)
    async with app.run_test() as pilot:
        session = app.current_session
        assert isinstance(session, Session)
        assert app._cursor is not None and app._cursor.log is session
        await app.action_new_chat()
        await pilot.pause()
        assert app.current_session is not session
        await pilot.press("ctrl+q")
    assert _files(tau_home) == before
    assert not (tau_home / "sessions").exists()


async def test_startup_screen_offers_extension_commands(make_app, tmp_path):
    """Before any chat, ``/budget`` is a command to the hint, to Tab and to the peek."""
    ext = tmp_path / "budget_ext.py"
    ext.write_text(_BUDGET_EXT)
    app = make_app(create_backend=create_backend, extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        editor = app.query_one(chat_widgets.ChatInput)
        popup = app.query_one(editor_widgets.CommandPopup)
        await _type(pilot, editor, "/budget")
        assert popup.display is True
        assert popup.has_class("command-popup-unknown") is False
        assert "show the budget" in popup.text

        await _type(pilot, editor, "/bud")
        await pilot.press("tab")
        assert editor.text.startswith("/budget")


async def test_the_first_append_writes_the_session_with_its_opening_chain(make_app, tau_home):
    """The deferred session lands on disk whole at its first write, ids unchanged."""
    app = make_app(create_backend=create_backend)
    async with app.run_test():
        session = app.current_session
        assert isinstance(session, Session) and session.path is None
        held = session.entries()
        await Cursor.newest(session).append_message({"role": "user", "content": "hi"})
        assert session.path is not None and session.path.exists()
        reloaded = Session.load(session.path)
        assert reloaded.id == session.id
        assert reloaded.entries()[: len(held)] == held
        assert reloaded.entries() == session.entries()


async def test_a_new_chat_shuts_down_the_backend_it_replaces(make_app, tmp_path):
    """Every ``session_start`` gets its ``session_shutdown``, across ctrl+N and quit."""
    out, ext = _lifecycle(tmp_path)
    app = make_app(create_backend=create_backend, extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await app.action_new_chat()
        await pilot.pause()
        assert _events(out) == ["start:startup", "shutdown:new", "start:startup"]
    assert _events(out) == ["start:startup", "shutdown:new", "start:startup", "shutdown:quit"]


async def test_resume_shuts_down_the_mount_session_it_replaces(make_app, tau_home, tmp_path):
    """Under ``--resume`` the mount session is shut down when the chosen one opens."""
    out, ext = _lifecycle(tmp_path)
    seeded = FileSessionCatalog(tau_home / "sessions").create(os.getcwd(), "m", "openai")
    await Cursor.newest(seeded).append_message({"role": "user", "content": "resume me"})
    app = make_app(create_backend=create_backend, extension_paths=[str(ext)], resume=True)
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.pause()
            screen = app.screen
            if isinstance(screen, SessionPickerModal) and not screen._loading:
                break
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause()
            if app.current_session is not None and app.current_session.id == seeded.id:
                break
        assert app.current_session is not None and app.current_session.id == seeded.id
        assert _events(out) == ["start:startup", "shutdown:resume", "start:startup"]
    assert _events(out) == [
        "start:startup",
        "shutdown:resume",
        "start:startup",
        "shutdown:quit",
    ]
