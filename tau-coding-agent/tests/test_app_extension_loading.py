"""E5 §2.2 (S27) — the TUI loads file extensions into each backend it creates.

``TauBackend.__init__`` left ``AgentSession`` with no extensions, and the app
never called the loader, so the interactive path loaded ZERO extensions (E5 §0).
This closes it: after every ``create_backend`` (new-chat / resume) the app runs
``_load_backend_extensions``, binding the run-level ``-e`` paths + discovery into
that backend's live session. Errors surface as TUI notices, never stderr (which
would corrupt the Textual screen — S25).

Driven through the real app via ``App.run_test()`` / Pilot, with a REAL
``TauBackend`` (its ``__init__`` does no network — it only builds the model +
resolves tools), so the hook binding is asserted on an actual session runner.

Reference: docs/EXTENSIONS-E5-WIRING.md §2.2, S27.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.app import Parley
from tau_coding_agent.backends import create_backend

# A file extension registering a tool_result hook — presence on the backend's
# live runner proves register(api) ran against THIS session (not a standalone one).
_TOOL_RESULT_EXT = """
def register(api):
    api.on("tool_result", lambda event, ctx: {"content": event.get("content")})
"""


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    # Use the real backend factory (TauBackend has no network in __init__).
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    # A real backend gives the app a real AgentSession, so _bind_backend_session
    # registers a MODULE-GLOBAL session-event listener (subscribe_session_events).
    # The app never unsubscribes on shutdown (harmless in a one-shot process), so
    # isolate the global list to a fresh one here — monkeypatch restores the
    # original (listener-free) list on teardown, discarding this app's leak so a
    # later test_session_store session-emit can't fire into a dead listener.
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_new_chat_binds_file_extension_to_backend(app, tmp_path):
    """A run-level ``-e`` extension's hook binds to the new-chat backend's session."""
    ext = tmp_path / "probe_ext.py"
    ext.write_text(_TOOL_RESULT_EXT)
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        runner = app.current_backend.agent_session._extension_runner
        assert runner.has_handlers("tool_result") is True
        # Bucket labelled by the extension's file path.
        paths = [b.path for b in runner._extensions if b.handlers]
        assert paths == [str(ext)]


async def test_no_extensions_binds_nothing(app):
    """With no ``-e`` paths and discovery off, the backend loads no hooks."""
    app._extension_paths = []
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        runner = app.current_backend.agent_session._extension_runner
        assert runner.has_handlers("tool_result") is False


async def test_discovered_failure_surfaces_notice(app, monkeypatch):
    """A *discovered* load error surfaces as a warning notice (never stderr)."""
    from tau_agent_core.sdk import ExtensionLoadError, LoadExtensionsResult

    class _ErroringBackend:
        async def load_extensions(self, explicit_paths=None, *, discover=True, user_dir=None):
            return LoadExtensionsResult(
                errors=[ExtensionLoadError(path="/x/broken.py", error="boom")]
            )

    notices: list[tuple[str, str]] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _ErroringBackend()
        monkeypatch.setattr(
            app, "notify", lambda msg, **kw: notices.append((msg, kw.get("severity", "")))
        )
        await app._load_backend_extensions()
        await pilot.pause()

    assert any("broken.py" in msg and sev == "warning" for msg, sev in notices)


def test_apply_run_config_no_builtin_tools_drops_builtins(app):
    """--no-builtin-tools sets tools=[] (extension tools survive the later merge)."""
    app._no_builtin_tools = True
    mc = app._apply_run_config({"backend": "openai", "model": "m", "tools": ["read"]})
    assert mc["tools"] == []


def test_apply_run_config_exclude_tools_rides_as_denylist(app):
    """--exclude-tools rides as an exclude_tools denylist TauBackend applies."""
    app._exclude_tools = ["bash"]
    mc = app._apply_run_config({"backend": "openai", "model": "m", "tools": ["read", "bash"]})
    assert mc["exclude_tools"] == ["bash"]


def test_apply_run_config_no_flags_returns_unchanged(app):
    """A bare tau (no run flags) leaves the model_config object untouched."""
    original = {"backend": "openai", "model": "m", "tools": ["read"]}
    assert app._apply_run_config(original) is original


async def test_new_chat_appends_system_prompt(app, tmp_path):
    """--append-system-prompt augments the base prompt on a NEW session (S28)."""
    app._append_system_prompt = ["EXTRA RULE"]

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        sys_msg = app.messages[0]
        assert sys_msg["role"] == "system"
        assert "sys" in sys_msg["content"]  # base prompt from the fixture config
        assert "EXTRA RULE" in sys_msg["content"]


async def test_explicit_failure_surfaces_error_notice(app, monkeypatch):
    """An explicit ``-e`` failure (loader RAISES) is caught into an error notice."""

    class _RaisingBackend:
        async def load_extensions(self, explicit_paths=None, *, discover=True, user_dir=None):
            raise RuntimeError("boom during import")

    notices: list[tuple[str, str]] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _RaisingBackend()
        monkeypatch.setattr(
            app, "notify", lambda msg, **kw: notices.append((msg, kw.get("severity", "")))
        )
        await app._load_backend_extensions()
        await pilot.pause()

    assert any("boom" in msg and sev == "error" for msg, sev in notices)


# ── /extensions palette listing (E5 §5 / S34) ─────────────────────────────────

# A file extension registering a tool, a command, and a hook — everything the
# /extensions listing must surface for one loaded extension.
_FULL_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    api.register_tool({
        "name": "probe",
        "description": "a probe tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
    api.register_command("hello", {"description": "say hi"})
    api.on("tool_result", lambda event, ctx: None)
"""


async def test_extensions_command_lists_loaded_extension(app, tmp_path):
    """/extensions renders a system box with the loaded extension's name/path/…."""
    from tau_coding_agent.app import ChatDisplay, ChatInput, MessageBox

    ext = tmp_path / "full_ext.py"
    ext.write_text(_FULL_EXT)
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # Drive the real slash path (text → interception → listing), exactly as a
        # user typing /extensions: proves the dispatch wiring, not just the action.
        input_widget = app.query_one("#chat-input", ChatInput)
        input_widget.text = "/extensions"
        input_widget.action_submit()
        await pilot.pause()

        boxes = [b for b in app.query(MessageBox) if b.role == "system"]
        assert boxes, "no system box rendered for /extensions"
        listing = boxes[-1]._content
        assert "full_ext" in listing  # name
        assert str(ext) in listing  # path
        assert "probe" in listing  # registered tool
        assert "hello" in listing  # registered command
        assert "tool_result" in listing  # registered hook

        # Read-only: the listing is UI chrome, NOT a conversation node — it must not
        # leak into the working message list the model is sent (invariant, E5 §1).
        assert not any(m.get("content") == listing for m in app.messages)
        # And the ChatDisplay is where it lives.
        assert app.query_one(ChatDisplay) is not None


def test_format_extensions_listing_surfaces_load_errors(tmp_path):
    """The listing text carries both a loaded extension AND any load errors (S34)."""
    import asyncio

    from tau_agent_core.sdk import ExtensionLoadError, _load_extensions

    from tau_coding_agent.app import Parley

    ext = tmp_path / "full_ext.py"
    ext.write_text(_FULL_EXT)

    result = asyncio.run(_load_extensions([str(ext)], discover=False))
    # A discovered failure the loader would have collected alongside the good one.
    result.errors.append(ExtensionLoadError(path="/x/broken.py", error="boom during import"))

    text = Parley._format_extensions_listing(result)
    assert "full_ext" in text
    assert "probe" in text  # tool
    assert "hello" in text  # command
    assert "Load errors" in text
    assert "/x/broken.py" in text
    assert "boom during import" in text


def test_format_extensions_listing_empty_when_nothing_loaded():
    """With no extensions and no errors the listing says so (honest empty state)."""
    from tau_agent_core.sdk import LoadExtensionsResult

    from tau_coding_agent.app import Parley

    assert Parley._format_extensions_listing(LoadExtensionsResult()) == "No extensions loaded."
