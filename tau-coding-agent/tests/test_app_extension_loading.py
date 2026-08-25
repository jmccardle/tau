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
from tau_coding_agent.backends import create_backend, resolve_tool_names

# A file extension registering a tool_result hook — presence on the backend's
# live runner proves register(api) ran against THIS session (not a standalone one).
_TOOL_RESULT_EXT = """
def register(api):
    api.on("tool_result", lambda event, ctx: {"content": event.get("content")})
"""


@pytest.fixture
def app(make_app):
    """A Parley wired to REAL TauBackends (TauBackend has no network in __init__)."""
    return make_app(create_backend=create_backend)


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
        async def load_extensions(
            self,
            explicit_paths=None,
            *,
            discover=True,
            user_dir=None,
            extensions_config=None,
            collect_explicit_errors=False,
        ):
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
    app._no_tools = "builtin"
    mc = app._apply_run_config({"backend": "openai", "model": "m", "tools": ["read"]})
    assert mc["tools"] == []
    # The policy rides alongside, so TauBackend can tell this apart from --no-tools.
    assert mc["no_tools"] == "builtin"


def test_apply_run_config_no_tools_marks_the_run_all(app):
    """--no-tools empties the built-ins AND declares "all", the only difference."""
    app._no_tools = "all"
    mc = app._apply_run_config({"backend": "openai", "model": "m", "tools": ["read"]})
    assert mc["tools"] == []
    assert mc["no_tools"] == "all"


def test_apply_run_config_threads_no_context_files(app):
    """-nc reaches the model config TauBackend builds its prompt from (0.9.3 §1),
    and a mid-session ``/model`` switch re-applies it like the tool flags."""
    app._no_context_files = True
    first = app._apply_run_config({"backend": "openai", "model": "a"})
    switched = app._apply_run_config({"backend": "openai", "model": "b"})
    assert first["no_context_files"] is True
    assert switched["no_context_files"] is True


def test_apply_run_config_without_nc_leaves_the_entry_alone(app):
    """Absence never writes False: a config entry may set it on its own."""
    app._no_context_files = False
    mc = app._apply_run_config({"backend": "openai", "model": "a", "no_context_files": True})
    assert mc["no_context_files"] is True


def test_model_switch_under_no_tools_does_not_restore_tools(app):
    """The mid-session ``/model`` switch must not hand the tools back.

    ``_apply_run_config`` runs at EVERY ``create_backend``, so switching to a
    completely different model entry — one that declares its own tool list —
    re-applies the run's policy to it. Before ``--no-tools`` became run-level it
    lived on ONE entry (``resolve_model_config`` wrote ``tools: []`` there), and
    the entry the switch selected still had every tool.
    """
    app._no_tools = "all"
    first = app._apply_run_config({"backend": "openai", "model": "a", "tools": ["read"]})
    switched = app._apply_run_config(
        {"backend": "openai", "model": "b", "tools": ["read", "write", "bash"]}
    )
    assert first["tools"] == [] and first["no_tools"] == "all"
    assert switched["tools"] == [] and switched["no_tools"] == "all"
    assert resolve_tool_names(switched) == []


def test_model_switch_under_a_tool_allowlist_keeps_the_allowlist(app):
    """``--tools`` is the same class of run-level policy as ``--no-tools``.

    It had the identical defect: it took effect only by triggering
    ``resolve_model_config``, which wrote the allowlist into the ONE entry the
    process started on, so a ``/model`` switch selected an entry that still had
    every tool it declared. The allowlist now rides run-level and is re-applied
    to whichever entry the switch chose.
    """
    app._tool_allowlist = ["read"]
    first = app._apply_run_config({"backend": "openai", "model": "a", "tools": ["read", "bash"]})
    switched = app._apply_run_config(
        {"backend": "openai", "model": "b", "tools": ["read", "write", "bash"]}
    )
    assert first["tools"] == ["read"]
    assert switched["tools"] == ["read"]
    assert resolve_tool_names(switched) == ["read"]


def test_no_tools_beats_a_tool_allowlist(app):
    """Both flags given: ``--no-tools`` is the stronger claim and must win.

    ``_apply_run_config`` writes the allowlist before the suppression for this
    reason; the other order would let ``-t`` hand back tools ``-nt`` withheld.
    Mirrors resolve_model_config's if/elif on the headless path.
    """
    app._tool_allowlist = ["read", "bash"]
    app._no_tools = "all"
    mc = app._apply_run_config({"backend": "openai", "model": "a", "tools": ["read", "bash"]})
    assert mc["tools"] == []
    assert mc["no_tools"] == "all"


def test_apply_run_config_exclude_tools_rides_as_denylist(app):
    """--exclude-tools rides as an exclude_tools denylist TauBackend applies."""
    app._exclude_tools = ["bash"]
    mc = app._apply_run_config({"backend": "openai", "model": "m", "tools": ["read", "bash"]})
    assert mc["exclude_tools"] == ["bash"]


def test_apply_run_config_no_flags_returns_unchanged(app):
    """A bare tau (no run flags, no config defaults) leaves the object untouched.

    The config-level defaults this method also folds in — ``system_prompt`` here,
    ``reasoning_replay`` elsewhere — are cleared first. They are not run flags,
    and each has its own test; what this one pins is that the FLAGS alone never
    cause a copy.
    """
    app.config.pop("system_prompt", None)
    original = {"backend": "openai", "model": "m", "tools": ["read"]}
    assert app._apply_run_config(original) is original


def test_apply_run_config_folds_the_config_system_prompt_onto_the_entry(app):
    """The top-level ``system_prompt`` reaches the ENTRY, not the session message.

    This is the seam that lets a configured prompt COMPOSE with the project
    context files and the tool list: ``TauBackend`` reads it off the entry as
    ``custom_prompt`` and builds around it. It used to be written straight into
    the session's first message, which takes precedence over the built prompt —
    so setting it cost the user their AGENTS.md context and the tool list.
    """
    app.config["system_prompt"] = "MY PROMPT"
    mc = app._apply_run_config({"backend": "openai", "model": "m"})
    assert mc["system_prompt"] == "MY PROMPT"


def test_apply_run_config_entry_prompt_wins_over_the_config_default(app):
    """A model entry naming its own prompt is not overwritten by the global one.

    Same precedence ``reasoning_replay`` has: per-model wins, else the top-level
    default. Writing the global over it would make a per-model prompt unusable.
    """
    app.config["system_prompt"] = "GLOBAL"
    mc = app._apply_run_config({"backend": "openai", "model": "m", "system_prompt": "ENTRY"})
    assert mc["system_prompt"] == "ENTRY"


def test_apply_run_config_carries_append_sections_for_the_backend(app):
    """``--append-system-prompt`` rides as its own key rather than pre-folded.

    ``TauBackend`` applies it to whichever base text it resolves, so the sections
    land ahead of the project context and the tool list (pi's placement) and one
    code path decides that for the TUI, print mode and RPC mode alike.
    """
    app._append_system_prompt = ["EXTRA RULE"]
    mc = app._apply_run_config({"backend": "openai", "model": "m"})
    assert mc["append_system_prompt"] == ["EXTRA RULE"]


def test_apply_run_config_bus_grants_the_h8_capability(app):
    """``--bus`` reaches the model_config TauBackend builds its session from.

    Until this was threaded, ``bus_available`` was a ``create_agent_session``
    parameter nothing in this package set, so every TOUCHES_BUS extension was
    refused in the TUI and ``nats_bus`` was loadable only from a script.
    """
    app._bus_available = True
    mc = app._apply_run_config({"backend": "openai", "model": "m"})
    assert mc["bus_available"] is True


def test_apply_run_config_without_bus_does_not_revoke_a_configured_one(app):
    """No ``--bus`` must not overwrite a model entry that granted it itself.

    The flag GRANTS the capability; its absence is "no opinion", not a denial.
    Writing False here would make ``"bus_available": true`` in config.json
    unusable without also passing a flag every single run.
    """
    app._bus_available = False
    mc = app._apply_run_config({"backend": "openai", "model": "m", "bus_available": True})
    assert mc["bus_available"] is True


def test_apply_run_config_threads_the_turn_ceiling(app):
    """``--max-turns`` reaches the model config, and survives a ``/model`` switch.

    The ceiling is a statement about the run, not about the entry the run happens
    to be pointed at, so it is re-applied at every ``create_backend`` like the
    tool flags.
    """
    app._max_turns = 12
    first = app._apply_run_config({"backend": "openai", "model": "a"})
    switched = app._apply_run_config({"backend": "openai", "model": "b"})
    assert first["max_turns"] == 12
    assert switched["max_turns"] == 12


def test_apply_run_config_falls_back_to_the_config_ceiling(app):
    """No flag → config.json's top-level ``max_turns``; a model entry beats it."""
    app._max_turns = None
    app.config["max_turns"] = 30
    assert app._apply_run_config({"backend": "openai", "model": "a"})["max_turns"] == 30
    entry = {"backend": "openai", "model": "a", "max_turns": 5}
    assert app._apply_run_config(entry)["max_turns"] == 5
    # …and the flag beats both.
    app._max_turns = 7
    assert app._apply_run_config(entry)["max_turns"] == 7


def test_apply_run_config_without_a_ceiling_states_none(app):
    """Silence everywhere leaves the key absent, so ``AgentLoopConfig``'s default
    (no ceiling) stands. Writing a number here would resurrect the unreachable 50
    this replaced, just in a different file."""
    app._max_turns = None
    app.config.pop("max_turns", None)
    mc = app._apply_run_config({"backend": "openai", "model": "m"})
    assert "max_turns" not in mc


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
    """A non-per-extension load failure (loader RAISES) hits the outer-except backstop.

    Per-extension explicit ``-e`` failures are now collected (see
    ``test_partial_load_keeps_good_and_lists_errors``); the outer ``except`` remains
    a backstop for a load that raises wholesale (e.g. config resolution).
    """

    class _RaisingBackend:
        async def load_extensions(
            self,
            explicit_paths=None,
            *,
            discover=True,
            user_dir=None,
            extensions_config=None,
            collect_explicit_errors=False,
        ):
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


async def test_partial_load_keeps_good_and_lists_errors(app, tmp_path):
    """A broken explicit ``-e`` alongside a good one no longer empties /extensions.

    Reproduces + closes the split-brain (docs/EXTENSIONS-DEMO-ROADMAP.md): the loader
    used to RAISE past the partial result on an explicit failure, leaving
    ``_extension_load_result`` empty while the good extension's tools/commands stayed
    bound to the live registry. The TUI now passes ``collect_explicit_errors=True``,
    so the good extension is kept in ``result.extensions`` AND the failure lands in
    ``result.errors`` — and the /extensions listing shows both.
    """
    from tau_coding_agent.app import MessageBox

    good = tmp_path / "full_ext.py"
    good.write_text(_FULL_EXT)
    broken = tmp_path / "broken_ext.py"
    broken.write_text("def register(api):\n    raise RuntimeError('boom during register')\n")
    app._extension_paths = [str(good), str(broken)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # Result carries the good extension AND the failure (no longer split-brained).
        loaded_paths = [e.path for e in app._extension_load_result.extensions]
        assert loaded_paths == [str(good)]
        error_paths = [e.path for e in app._extension_load_result.errors]
        assert str(broken) in error_paths[0]
        # The good extension's command really is bound to the live registry.
        commands = dict(app.current_backend.get_extension_commands())
        assert "hello" in commands

        # The /extensions listing shows the good extension AND a Load errors section.
        app.action_show_extensions()
        await pilot.pause()
        boxes = [b for b in app.query(MessageBox) if b.role == "system"]
        listing = boxes[-1]._content
        assert "full_ext" in listing
        assert "hello" in listing  # bound command
        assert "Load errors" in listing
        assert "broken_ext" in listing
        assert "boom during register" in listing


def test_format_extensions_listing_surfaces_load_errors(tmp_path):
    """The listing text carries both a loaded extension AND any load errors (S34)."""
    import asyncio

    from tau_agent_core.sdk import ExtensionLoadError, _load_extensions

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

    assert Parley._format_extensions_listing(LoadExtensionsResult()) == "No extensions loaded."
