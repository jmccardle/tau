"""E10 §6 (S70) — ``/extensions enable|disable|reload`` runs through the app.

The read-only ``/extensions`` listing (S34) gains runtime management verbs (lifting
D-E5-6). Driven through the real ``Parley`` app via ``App.run_test()`` / Pilot with a
REAL ``TauBackend`` so the action lands on an actual session runner: the extension's
observable hook effect (a tool_result edit) proves disable genuinely stopped it, and a
teardown marker file proves ``session_shutdown`` fired.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S70.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from tau_coding_agent.app import ChatInput, MessageBox, Parley
from tau_coding_agent.backends import create_backend

# A file extension with a tool_result hook (appends a marker) and a session_shutdown
# teardown that writes a marker file — the file proves the S41 teardown fired on disable.
_EXT = '''
import pathlib

DOWN = pathlib.Path({down!r})


def register(api):
    def on_tool_result(event, ctx):
        content = event.get("content") or []
        text = content[0].get("text", "") if content else ""
        return {{"content": [{{"type": "text", "text": text + " +MARK"}}]}}

    api.on("tool_result", on_tool_result)
    api.on("session_shutdown", lambda e, c: DOWN.write_text(str(e.get("reason", ""))))
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

        chat_input = app.query_one("#chat-input", ChatInput)
        await app.on_input_submitted(Input.Submitted(chat_input, "/extensions disable probe_ext"))
        await pilot.pause()

        # Hook stopped firing + teardown ran (reason "disable").
        assert await _emit_tool_result(app) is None
        assert down.read_text() == "disable"

        # The listing (display-only chrome) marks it disabled and did NOT enter model input.
        listing = app._format_extensions_listing(
            app._extension_load_result, app._disabled_extension_paths()
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
            Input.Submitted(app.query_one("#chat-input", ChatInput), "/extensions disable probe_ext")
        )
        await pilot.pause()
        assert await _emit_tool_result(app) is None

        await app.on_input_submitted(
            Input.Submitted(app.query_one("#chat-input", ChatInput), "/extensions reload probe_ext")
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
            Input.Submitted(app.query_one("#chat-input", ChatInput), "/extensions frobnicate x")
        )
        await pilot.pause()

        # Not sent as a prompt.
        assert app.messages == before
        assert not any(
            isinstance(b, MessageBox) and "frobnicate" in getattr(b, "_content", "")
            for b in app.query(MessageBox)
        )
