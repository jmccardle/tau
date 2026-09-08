"""E10 §6 (S70) — ``/extensions enable|disable|reload`` runs through the app.

The read-only ``/extensions`` listing (S34) gains runtime management verbs (lifting
D-E5-6). Driven through the real ``TauApp`` app via ``App.run_test()`` / Pilot with a
REAL ``TauBackend`` so the action lands on an actual session runner: the extension's
observable hook effect (a tool_result edit) proves disable genuinely stopped it, and a
teardown marker file proves ``session_shutdown`` fired.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S70.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from tau_coding_agent.backends import create_backend
from tau_coding_agent import chat_widgets

_EXT = """
import pathlib

DOWN = pathlib.Path({down!r})


def register(api):
    def on_tool_result(event, ctx):
        content = event.get("content") or []
        text = content[0].get("text", "") if content else ""
        return {{"content": [{{"type": "text", "text": text + " +MARK"}}]}}

    api.on("tool_result", on_tool_result)
    api.on("session_shutdown", lambda e, c: DOWN.write_text(str(e.get("reason", ""))))
"""


@pytest.fixture
def app(make_app):
    """A TauApp wired to REAL TauBackends (TauBackend has no network in __init__)."""
    return make_app(create_backend=create_backend)


async def _emit_tool_result(app) -> str | None:
    runner = app.current_backend.agent_session._extension_runner
    patched = await runner.emit_tool_result(
        {"type": "tool_result", "content": [{"type": "text", "text": "base"}]}
    )
    return None if patched is None else patched["content"][0]["text"]


async def test_slash_disable_stops_hook_and_fires_teardown(app, tmp_path):
    """``/extensions disable <name>`` detaches the hook and runs the S41 teardown."""
    down = tmp_path / "down.txt"
    ext = tmp_path / "probe_ext.py"
    ext.write_text(_EXT.format(down=str(down)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # Hook is live before disable.
        assert await _emit_tool_result(app) == "base +MARK"

        chat_input = app.query_one("#chat-input", chat_widgets.ChatInput)
        await app.on_input_submitted(Input.Submitted(chat_input, "/extensions disable probe_ext"))
        await pilot.pause()

        # Hook stopped firing + teardown ran (reason "disable").
        assert await _emit_tool_result(app) is None
        assert down.read_text() == "disable"

        # The listing (display-only chrome) marks it disabled and did NOT enter model input.
        listing = app._format_extensions_listing(
            app.current_backend.get_extension_state(), app._disabled_extension_paths()
        )
        assert "_(disabled)_" in listing
        assert all("probe_ext" not in str(m.get("content", "")) for m in app.messages)


async def test_slash_reload_rebinds(app, tmp_path):
    """``/extensions reload <name>`` re-imports the file and re-binds the hook."""
    down = tmp_path / "down.txt"
    ext = tmp_path / "probe_ext.py"
    ext.write_text(_EXT.format(down=str(down)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        await app.on_input_submitted(
            Input.Submitted(
                app.query_one("#chat-input", chat_widgets.ChatInput),
                "/extensions disable probe_ext",
            )
        )
        await pilot.pause()
        assert await _emit_tool_result(app) is None

        await app.on_input_submitted(
            Input.Submitted(
                app.query_one("#chat-input", chat_widgets.ChatInput), "/extensions reload probe_ext"
            )
        )
        await pilot.pause()

        # Re-bound: the hook fires again after reload.
        assert await _emit_tool_result(app) == "base +MARK"


async def test_slash_unknown_verb_is_reported_not_sent(app, tmp_path):
    """An unknown verb notifies and never reaches the model (display-only chrome)."""
    ext = tmp_path / "probe_ext.py"
    ext.write_text(_EXT.format(down=str(tmp_path / "down.txt")))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        before = list(app.messages)
        await app.on_input_submitted(
            Input.Submitted(
                app.query_one("#chat-input", chat_widgets.ChatInput), "/extensions frobnicate x"
            )
        )
        await pilot.pause()

        # Not sent as a prompt.
        assert app.messages == before
        assert not any(
            isinstance(b, chat_widgets.MessageBox) and "frobnicate" in getattr(b, "_content", "")
            for b in app.query(chat_widgets.MessageBox)
        )


_TOOL_EXT = """
def register(api):
    api.register_tool(
        {{
            "name": {tool!r},
            "description": "a probe tool",
            "parameters": {{"type": "object", "properties": {{}}}},
            "execute": lambda args, ctx: {{"output": "ok"}},
        }}
    )
"""


async def test_the_listing_reflects_a_reload_rather_than_the_load_time_snapshot(app, tmp_path):
    """The listing is a live read, so re-importing a changed file changes what it says.

    The app used to cache the ``LoadExtensionsResult`` the loader returned and render
    the listing from it, and nothing refreshed that cache on reload — so an extension
    whose registered tools changed kept showing its old ones until restart. The listing
    now reads ``get_extension_state``, which is rebuilt from the session's live
    ``_loaded_extensions``, and ``reload_extension`` replaces the entry there.
    """
    ext = tmp_path / "swap_ext.py"
    ext.write_text(_TOOL_EXT.format(tool="alpha"))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        before = app._format_extensions_listing(app.current_backend.get_extension_state())
        assert "alpha" in before

        ext.write_text(_TOOL_EXT.format(tool="beta"))
        await app.action_run_extension_flow("reload_extension", str(ext))
        await pilot.pause()

        after = app._format_extensions_listing(app.current_backend.get_extension_state())
        assert "beta" in after
        assert "alpha" not in after
