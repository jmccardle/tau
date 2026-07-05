"""E5 §5 (S35) — extension-registered slash commands are LISTED and RUNNABLE.

``registry._commands`` used to be write-only: an extension's ``register_command``
stored a command that nobody read, so it was invisible in the palette and could
never run (E5 §0, the second orphan). This surfaces them on BOTH ends:

- :meth:`Parley.get_system_commands` yields a palette entry per registered command
  (listed), and
- :meth:`Parley.on_input_submitted` dispatches a matching ``/name args`` to the
  command's handler before the text reaches the model (runnable), mirrored by the
  palette callback :meth:`Parley._dispatch_extension_command`.

Driven through the real app via ``App.run_test()`` / Pilot with a REAL
``TauBackend`` (its ``__init__`` does no network), so the command is asserted on an
actual session registry — the handler's observable side effect (a marker file it
writes) proves it genuinely ran, not that a name was merely stored.

Reference: docs/EXTENSIONS-E5-WIRING.md §5 (E5.4 / S35).
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from tau_coding_agent.app import Parley, ChatInput
from tau_coding_agent.backends import create_backend

# A file extension registering a slash command whose handler writes a marker file
# capturing the args it received — the file's existence + contents prove the
# handler actually ran through the real dispatch (not a stored-but-inert name).
_COMMAND_EXT = '''
import pathlib

MARKER = pathlib.Path({marker!r})


def register(api):
    def _greet(args, ctx):
        MARKER.write_text("ran:" + args)

    api.register_command("greet", {{"description": "greet the user", "handler": _greet}})
'''


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    # Isolate the module-global session-event listener list (see
    # test_app_extension_loading.py for the rationale — avoids a cross-test leak).
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_extension_command_listed_and_runnable(app, tmp_path):
    """A registered command appears in the palette AND runs when invoked."""
    marker = tmp_path / "greet_marker.txt"
    ext = tmp_path / "cmd_ext.py"
    ext.write_text(_COMMAND_EXT.format(marker=str(marker)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # The command reached the session registry (bound to THIS backend).
        assert app.current_backend.get_extension_commands() == [("greet", "greet the user")]

        # (1) LISTED — a palette entry with the command's title/help.
        titles = {cmd.title: cmd for cmd in app.get_system_commands(app.screen)}
        assert "/greet" in titles
        assert titles["/greet"].help == "greet the user"

        # (2) RUNNABLE via slash input — dispatch parses `/greet <args>` and runs
        # the handler before any model call. The marker proves it executed with args.
        chat_input = app.query_one("#chat-input", ChatInput)
        await app.on_input_submitted(Input.Submitted(chat_input, "/greet hello world"))
        await pilot.pause()
        assert marker.read_text() == "ran:hello world"

        # (3) RUNNABLE via the palette callback (no args line → empty args).
        marker.unlink()
        await titles["/greet"].callback()
        await pilot.pause()
        assert marker.read_text() == "ran:"


async def test_unknown_slash_command_falls_through(app, tmp_path):
    """An unknown ``/…`` is NOT swallowed by command dispatch (returns to prompt path).

    ``run_extension_command`` returns False for an unregistered name, so the app
    must fall through rather than silently drop the text. We stub the generation
    worker to capture that fall-through instead of hitting the model.
    """
    ext = tmp_path / "cmd_ext.py"
    ext.write_text(_COMMAND_EXT.format(marker=str(tmp_path / "unused.txt")))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    generated: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # Replace the streaming worker so the fall-through doesn't call a provider.
        app._generate_response = lambda: generated.append(app.messages[-1]["content"])  # type: ignore[method-assign]

        chat_input = app.query_one("#chat-input", ChatInput)
        await app.on_input_submitted(Input.Submitted(chat_input, "/nope not-a-command"))
        await pilot.pause()

        # Unknown command fell through to the prompt path (the text was queued to send).
        assert generated == ["/nope not-a-command"]


async def test_command_without_handler_raises(app, tmp_path):
    """A command registered without a callable handler raises on run (Fail-Early)."""
    ext = tmp_path / "bad_cmd_ext.py"
    ext.write_text(
        "def register(api):\n"
        "    api.register_command('inert', {'description': 'no handler'})\n"
    )
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # Listed (best-effort chrome) but NOT runnable — invoking raises rather
        # than silently no-op'ing on a registered-but-inert command.
        assert ("inert", "no handler") in app.current_backend.get_extension_commands()
        with pytest.raises(RuntimeError, match="no callable 'handler'"):
            await app.current_backend.run_extension_command("inert")
