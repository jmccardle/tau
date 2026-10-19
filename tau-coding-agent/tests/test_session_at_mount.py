"""The TUI opens its session at mount, and an unused one leaves nothing on disk.

Issue #42: before any chat existed, ``/budget`` was called "not a command" and
Tab completion offered no extension commands, because extensions loaded only
when the first chat was created. The fix opens a deferred session at mount
(docs/SESSION-AT-MOUNT.md), so its hard requirement is tested here too: mounting
and quitting must write nothing under the tau home.
"""

from __future__ import annotations

from pathlib import Path

from tau_agent_core.cursor import Cursor
from tau_coding_agent import chat_widgets, editor_widgets
from tau_coding_agent.backends import create_backend
from tau_coding_agent.session_store import Session

_BUDGET_EXT = """
def register(api):
    api.register_command(
        "budget", {"description": "show the budget", "handler": lambda args, ctx: "ok"}
    )
"""


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
        assert isinstance(session, Session) and session.path is not None
        assert not session.path.exists()
        held = session.entries()
        await Cursor.newest(session).append_message({"role": "user", "content": "hi"})
        assert session.path.exists()
        reloaded = Session.load(session.path)
        assert reloaded.id == session.id
        assert reloaded.entries()[: len(held)] == held
        assert reloaded.entries() == session.entries()
