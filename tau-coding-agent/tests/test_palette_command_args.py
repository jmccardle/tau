"""E7 §3 (S51) — palette entries for ``args``-declaring commands collect the arg string.

A command may declare ``"args": "<placeholder>"`` to signal it expects a free-form
argument string (parity with typing ``/name args`` at the prompt). The command palette
has no argument line, so its entry for such a command opens the S47
:class:`ExtensionInputModal` to collect the arg string BEFORE dispatch; a command with
no ``args`` dispatches directly.

Driven through the real app via ``App.run_test()`` / Pilot with a REAL ``TauBackend``,
matching ``test_app_extension_commands`` / ``test_extension_dialogs``: the handler's
observable side effect (a marker file capturing the args it received) proves the arg
string reached the handler exactly as typed.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S51.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from tau_coding_agent.app import Parley
from tau_coding_agent.backends import create_backend

# A command declaring an ``args`` placeholder; its handler writes a marker capturing
# the args it was dispatched with — proving the collected string reached the handler.
_ARGS_EXT = '''
import pathlib

MARKER = pathlib.Path({marker!r})


def register(api):
    def _search(args, ctx):
        MARKER.write_text("ran:" + args)

    api.register_command(
        "search",
        {{"description": "search the corpus", "args": "<query>", "handler": _search}},
    )
'''

# A command WITHOUT ``args`` — its palette entry must dispatch directly, no modal.
_PLAIN_EXT = '''
import pathlib

MARKER = pathlib.Path({marker!r})


def register(api):
    def _now(args, ctx):
        MARKER.write_text("ran:" + args)

    api.register_command("now", {{"description": "print now", "handler": _now}})
'''


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_args_command_exposes_placeholder(app, tmp_path):
    """The declared ``args`` placeholder is readable through the backend (S51)."""
    ext = tmp_path / "args_ext.py"
    ext.write_text(_ARGS_EXT.format(marker=str(tmp_path / "unused.txt")))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        assert app.current_backend.get_extension_command_args("search") == "<query>"
        # An unknown command name has no placeholder (None, never fabricated).
        assert app.current_backend.get_extension_command_args("nope") is None


async def test_palette_args_command_opens_modal_and_dispatches(app, tmp_path):
    """Palette entry for an ``args`` command opens the input modal, then dispatches it."""
    marker = tmp_path / "search_marker.txt"
    ext = tmp_path / "args_ext.py"
    ext.write_text(_ARGS_EXT.format(marker=str(marker)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # The palette lists the command; its callback routes through the arg prompt.
        titles = {cmd.title: cmd for cmd in app.get_system_commands(app.screen)}
        assert "/search" in titles

        # Invoking the palette callback opens the S47 input modal (worker context).
        worker = titles["/search"].callback()
        await pilot.pause()
        field = app.screen.query_one("#ext-input-field", Input)
        field.value = "pi ports"
        await pilot.pause()
        await pilot.press("enter")
        await worker.wait()
        await pilot.pause()

        # The typed arg string reached the handler exactly (parity with /search args).
        assert marker.read_text() == "ran:pi ports"


async def test_palette_args_command_cancel_does_not_dispatch(app, tmp_path):
    """Fail-Early: cancelling the arg modal does NOT run the command on a fabricated arg."""
    marker = tmp_path / "search_marker.txt"
    ext = tmp_path / "args_ext.py"
    ext.write_text(_ARGS_EXT.format(marker=str(marker)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        titles = {cmd.title: cmd for cmd in app.get_system_commands(app.screen)}
        worker = titles["/search"].callback()
        await pilot.pause()
        # The modal is up; cancel it (Cancel button → dismiss(None)). The command
        # must not dispatch on an arg the user never confirmed.
        assert app.screen.query_one("#ext-input-field", Input) is not None
        await pilot.click("#ext-input-cancel")
        await worker.wait()
        await pilot.pause()

        assert not marker.exists()


async def test_palette_plain_command_dispatches_without_modal(app, tmp_path):
    """A command with no ``args`` dispatches directly from the palette (no input modal)."""
    marker = tmp_path / "now_marker.txt"
    ext = tmp_path / "plain_ext.py"
    ext.write_text(_PLAIN_EXT.format(marker=str(marker)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        assert app.current_backend.get_extension_command_args("now") is None

        titles = {cmd.title: cmd for cmd in app.get_system_commands(app.screen)}
        await titles["/now"].callback()
        await pilot.pause()

        # No input modal was pushed (dispatch was direct); the handler ran with the
        # empty arg string, exactly as the pre-S51 palette dispatch did.
        assert len(app.screen.query("#ext-input-field")) == 0
        assert marker.read_text() == "ran:"
