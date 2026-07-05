"""Tests for tau_agent_core.extension_types — ExtensionAPI, ExtensionContext, ExtensionUI.

Tests verify:
- ExtensionAPI exposes all documented methods
- ExtensionAPI.on() registers event handlers on the event bus
- ExtensionAPI.register_tool() stores tool definitions with _source="extension"
- ExtensionAPI.get_all_tools() returns registered tools
- ExtensionAPI.append_entry() persists through registry
- ExtensionContext provides required properties via constructor
- ExtensionUI methods are no-ops (headless mode)
- ExtensionUI.confirm() returns True by default
- ExtensionUI.select() returns first item or None
- ExtensionUI.input() returns default value

Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section
Reference: PHASE-3-SUBPHASE-0.md, Extension API Surface contract
"""

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
    HeadlessDialogError,
    form_headless_value,
    validate_form_spec,
)
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.events import EventBus


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI — method existence and basic properties
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPIInit:
    """Tests for ExtensionAPI initialization (backward compatible)."""

    def test_create_extension_api_no_args(self):
        """ExtensionAPI can be instantiated with no arguments (backward compat)."""
        api = ExtensionAPI()
        assert api is not None

    def test_create_extension_api_with_args(self):
        """ExtensionAPI can be instantiated with explicit arguments."""
        reg = ExtensionRegistry()
        bus = EventBus()
        ctx = ExtensionContext(cwd="/tmp")
        api = ExtensionAPI(registry=reg, event_bus=bus, context=ctx, session=None)
        assert api is not None

    def test_extension_api_has_internal_registry(self):
        """ExtensionAPI stores its registry."""
        api = ExtensionAPI()
        assert hasattr(api, "_registry")

    def test_extension_api_has_internal_event_bus(self):
        """ExtensionAPI stores its event bus."""
        api = ExtensionAPI()
        assert hasattr(api, "_event_bus")

    def test_extension_api_has_internal_context(self):
        """ExtensionAPI stores its context."""
        api = ExtensionAPI()
        assert hasattr(api, "_context")


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI.on() — event subscription
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPIEvents:
    """Tests for ExtensionAPI.on() event registration via event bus."""

    @pytest.mark.asyncio
    async def test_register_handler(self):
        """ExtensionAPI.on() registers a handler on the event bus."""
        api = ExtensionAPI()
        received: list = []

        def handler(event):
            received.append(event)

        api.on("test_event", handler)
        # The handler is stored on the event bus, not on the API
        assert len(api._event_bus._listeners.get("test_event", [])) == 1

    @pytest.mark.asyncio
    async def test_register_handler_for_all(self):
        """ExtensionAPI.on('all') subscribes to 'all' channel."""
        api = ExtensionAPI()
        bus = api._event_bus
        api.on("all", lambda e: None)
        assert len(bus._listeners.get("all", [])) == 1

    @pytest.mark.asyncio
    async def test_register_handler_for_specific_event(self):
        """ExtensionAPI.on('agent_start') subscribes to that specific event."""
        api = ExtensionAPI()
        bus = api._event_bus
        api.on("agent_start", lambda e: None)
        assert len(bus._listeners.get("agent_start", [])) == 1

    @pytest.mark.asyncio
    async def test_on_returns_unsubscribe_function(self):
        """ExtensionAPI.on() returns an unsubscribe function."""
        api = ExtensionAPI()
        unsub = api.on("agent_start", lambda e: None)
        assert callable(unsub)
        unsub()  # should not raise


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI.tool methods
# ──────────────────────────────────────────────────────────────────────────────


async def _noop_exec(tool_call_id, params, signal, on_update, ctx):
    """A pi-shaped no-op execute for register_tool() tests."""
    return {"content": [{"type": "text", "text": "ok"}]}


class TestExtensionAPITools:
    """Tests for ExtensionAPI tool methods via registry."""

    def test_register_tool(self):
        """ExtensionAPI.register_tool() stores tool with _source='extension'."""
        api = ExtensionAPI()
        tool_def = {
            "name": "ls",
            "description": "List files",
            "parameters": {},
            "execute": _noop_exec,
        }
        api.register_tool(tool_def)
        tools = api._registry.get_all_tools()
        assert len(tools) == 1
        assert tools[0].name == "ls"

    def test_register_tool_sets_source(self):
        """ExtensionAPI.register_tool() sets _source='extension' on the tool."""
        api = ExtensionAPI()
        api.register_tool(
            {"name": "my_tool", "description": "desc", "parameters": {}, "execute": _noop_exec}
        )
        tools = api._registry.get_all_tools()
        assert tools[0].source == "extension"

    def test_register_tool_does_not_mutate_original(self):
        """ExtensionAPI.register_tool() does not mutate the caller's dict."""
        api = ExtensionAPI()
        tool_def = {"name": "tool", "description": "desc", "parameters": {}, "execute": _noop_exec}
        api.register_tool(tool_def)
        assert "_source" not in tool_def

    def test_register_tool_missing_execute_raises(self):
        """register_tool() raises when a required pi ToolDefinition key is missing."""
        api = ExtensionAPI()
        with pytest.raises(ValueError, match="missing required key 'execute'"):
            api.register_tool({"name": "x", "description": "d", "parameters": {}})

    def test_register_tool_non_dict_parameters_raises(self):
        """register_tool() raises when 'parameters' is not a JSON-schema dict."""
        api = ExtensionAPI()
        with pytest.raises(TypeError, match="'parameters' must be a JSON-schema dict"):
            api.register_tool(
                {"name": "x", "description": "d", "parameters": "nope", "execute": _noop_exec}
            )

    def test_get_all_tools_empty(self):
        """ExtensionAPI.get_all_tools() returns empty list initially."""
        api = ExtensionAPI()
        assert api.get_all_tools() == []

    def test_get_all_tools_after_register(self):
        """ExtensionAPI.get_all_tools() returns registered tools."""
        api = ExtensionAPI()
        api.register_tool(
            {"name": "ls", "description": "List", "parameters": {}, "execute": _noop_exec}
        )
        api.register_tool(
            {"name": "grep", "description": "Search", "parameters": {}, "execute": _noop_exec}
        )
        tools = api.get_all_tools()
        assert len(tools) == 2
        names = [t.name for t in tools]
        assert "ls" in names
        assert "grep" in names

    def test_set_active_tools(self):
        """ExtensionAPI.set_active_tools() forwards to registry."""
        api = ExtensionAPI()
        api.register_tool(
            {"name": "ls", "description": "List", "parameters": {}, "execute": _noop_exec}
        )
        api.register_tool(
            {"name": "grep", "description": "Search", "parameters": {}, "execute": _noop_exec}
        )
        api.set_active_tools(["ls", "grep"])
        active = api._registry.get_active_tools()
        assert set(active.keys()) == {"ls", "grep"}

    def test_register_multiple_tools(self):
        """ExtensionAPI.register_tool() can register multiple tools."""
        api = ExtensionAPI()
        for i in range(5):
            api.register_tool(
                {
                    "name": f"tool_{i}",
                    "description": f"tool {i}",
                    "parameters": {},
                    "execute": _noop_exec,
                }
            )
        assert len(api.get_all_tools()) == 5


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI command methods
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPICommands:
    """Tests for ExtensionAPI command registration via registry."""

    def test_register_command(self):
        """ExtensionAPI.register_command() stores a command in the registry."""
        api = ExtensionAPI()
        cmd = {"action": "help"}
        api.register_command("help", cmd)
        assert "help" in api._registry._commands
        assert api._registry._commands["help"] == cmd

    def test_register_multiple_commands(self):
        """ExtensionAPI.register_command() can register multiple commands."""
        api = ExtensionAPI()
        api.register_command("help", {"action": "help"})
        api.register_command("status", {"action": "status"})
        assert "help" in api._registry._commands
        assert "status" in api._registry._commands

    def test_register_command_overwrites(self):
        """ExtensionAPI.register_command() overwrites existing command."""
        api = ExtensionAPI()
        api.register_command("help", {"action": "old"})
        api.register_command("help", {"action": "new"})
        assert api._registry._commands["help"]["action"] == "new"


# ──────────────────────────────────────────────────────────────────────────────
# register_flag / get_flag deleted (E6 §2 / S38)
# ──────────────────────────────────────────────────────────────────────────────


class TestFlagsRemoved:
    """The dead ``register_flag`` / ``get_flag`` API was deleted in S38 (G6).

    The ``value`` was never populated (superseded by S40 per-extension config), so
    the methods and the ``_flags`` store are gone from both the API and the
    registry surface.
    """

    def test_extension_api_has_no_register_flag(self):
        assert not hasattr(ExtensionAPI(), "register_flag")

    def test_extension_api_has_no_get_flag(self):
        assert not hasattr(ExtensionAPI(), "get_flag")

    def test_extension_api_has_no_flags_store(self):
        assert not hasattr(ExtensionAPI(), "_flags")


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI.append_entry
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPIAppendEntry:
    """Tests for ExtensionAPI.append_entry() — now DURABLE (E6 §2 / S39).

    Persists onto the session tree as a ``customEntry`` node instead of the old
    RAM-only registry ``_entry_store`` (removed with G4). The full durable /
    reload-invariant / off-the-wire proof lives in ``test_append_entry_durable.py``;
    these tests cover the API-surface contract (delegation + Fail-Early raise).
    """

    def test_append_entry_exists(self):
        """ExtensionAPI has append_entry method."""
        api = ExtensionAPI()
        assert hasattr(api, "append_entry")

    def test_append_entry_raises_without_session(self):
        """Fail-Early: no session bound → raise, not a silent RAM store (G4)."""
        api = ExtensionAPI()
        with pytest.raises(RuntimeError):
            api.append_entry("notification", {"text": "test"})

    def test_append_entry_delegates_to_session(self):
        """append_entry() forwards {custom_type, data} to _append_custom_entry."""
        mock_session = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.append_entry("notification", {"text": "test"})
        mock_session._append_custom_entry.assert_called_once_with("notification", {"text": "test"})

    def test_append_multiple_entries_delegate(self):
        """Each append_entry() call is a separate durable append."""
        mock_session = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.append_entry("counter", {"value": 1})
        api.append_entry("counter", {"value": 2})
        assert mock_session._append_custom_entry.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI session methods
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPISession:
    """Tests for ExtensionAPI session methods."""

    def test_set_session_name_raises_without_session(self):
        """ExtensionAPI.set_session_name() Fail-Early raises without a bound
        session (S64: the old ``_session_name``-attribute check was a silent
        no-op on every real session; a bare API has nowhere durable to land
        the name either, so it raises like its sibling durable-write ops)."""
        api = ExtensionAPI()
        with pytest.raises(RuntimeError):
            api.set_session_name("My Session")

    def test_set_session_name_with_session(self):
        """ExtensionAPI.set_session_name() forwards to the session log's
        append_session_info (S64)."""
        mock_session = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.set_session_name("new_name")
        mock_session._session_log.append_session_info.assert_called_once_with("new_name")

    def test_get_session_name_raises_without_session(self):
        """ExtensionAPI.get_session_name() Fail-Early raises without a bound
        session (no durable name to read)."""
        api = ExtensionAPI()
        with pytest.raises(RuntimeError):
            api.get_session_name()

    def test_get_session_name_reads_the_session_log(self):
        """ExtensionAPI.get_session_name() reads the session log's ``.name``,
        returning ``None`` for a falsy (unset) name rather than the raw value."""
        mock_session = MagicMock()
        mock_session._session_log.name = None
        api = ExtensionAPI(session=mock_session)
        assert api.get_session_name() is None

        mock_session._session_log.name = "existing-name"
        assert api.get_session_name() == "existing-name"

    def test_send_user_message_raises_without_queue(self):
        """send_user_message() raises (not silent) until the E3-ctx queue exists.

        ``ExtensionAPI()`` has no session, so there is no ``_queue_message``;
        Fail-Early requires a raise rather than dropping the message silently.
        """
        api = ExtensionAPI()
        with pytest.raises(RuntimeError):
            api.send_user_message("Hello")

    def test_send_user_message_defaults_to_follow_up(self):
        """send_user_message() defaults deliver_as to 'followUp' (not 'steer')."""
        mock_session = MagicMock()
        mock_session._queue_message = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.send_user_message("Hello")
        mock_session._queue_message.assert_called_once_with("Hello", deliver_as="followUp")

    def test_send_user_message_with_session(self):
        """ExtensionAPI.send_user_message() queues message on session."""
        mock_session = MagicMock()
        mock_session._queue_message = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.send_user_message("Hello", deliver_as="nextTurn")
        mock_session._queue_message.assert_called_once_with("Hello", deliver_as="nextTurn")

    def test_send_user_message_rejects_bad_deliver_as(self):
        """send_user_message() validates deliver_as against {followUp, nextTurn}."""
        mock_session = MagicMock()
        mock_session._queue_message = MagicMock()
        api = ExtensionAPI(session=mock_session)
        with pytest.raises(ValueError):
            api.send_user_message("Hello", deliver_as="steer")
        mock_session._queue_message.assert_not_called()

    def test_send_message_raises_without_session(self):
        """ExtensionAPI.send_message() raises without a session (Fail-Early, S38).

        The old behaviour silently no-op'd on a nonexistent method; a message with
        nowhere durable to land is a construction bug, not a no-op.
        """
        api = ExtensionAPI()
        with pytest.raises(RuntimeError):
            api.send_message({"customType": "note", "content": "Hello"}, {})

    def test_send_message_with_session(self):
        """ExtensionAPI.send_message() appends custom message on session."""
        mock_session = MagicMock()
        mock_session._append_custom_message = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.send_message({"customType": "note", "content": "Hello"}, {"source": "extension"})
        mock_session._append_custom_message.assert_called_once_with(
            {"customType": "note", "content": "Hello"}, {"source": "extension"}
        )

    def test_send_message_default_options_forwarded_as_empty_dict(self):
        """Omitting options forwards ``{}`` (display-only default is applied downstream)."""
        mock_session = MagicMock()
        mock_session._append_custom_message = MagicMock()
        api = ExtensionAPI(session=mock_session)
        api.send_message({"customType": "note", "content": "Hi"})
        mock_session._append_custom_message.assert_called_once_with(
            {"customType": "note", "content": "Hi"}, {}
        )


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI.ui property
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPIProperty:
    """Tests for ExtensionAPI.ui property."""

    def test_ui_returns_extension_ui(self):
        """ExtensionAPI.ui returns an ExtensionUI instance."""
        api = ExtensionAPI()
        ui = api.ui
        assert isinstance(ui, ExtensionUI)

    @pytest.mark.asyncio
    async def test_ui_raises_in_headless_without_policy(self):
        """ExtensionAPI.ui dialogs RAISE headless with no policy (S48, Fail-Early)."""
        from tau_agent_core.extension_types import HeadlessDialogError

        api = ExtensionAPI()
        ui = api.ui
        with pytest.raises(HeadlessDialogError):
            await ui.confirm("title", "msg")
        with pytest.raises(HeadlessDialogError):
            await ui.select("title", ["a"])
        with pytest.raises(HeadlessDialogError):
            await ui.input("title", default="default")

    @pytest.mark.asyncio
    async def test_ui_honors_headless_policy(self):
        """ExtensionAPI.ui dialogs auto-answer once a headless policy is set (S48)."""
        api = ExtensionAPI()
        api.context.set_headless_ui_defaults(
            {"confirm": "yes", "select": "first", "input": "default"}
        )
        ui = api.ui
        assert await ui.confirm("title", "msg") is True
        assert await ui.select("title", ["a", "b"]) == "a"
        assert await ui.input("title", default="default") == "default"

    def test_ui_returns_same_instance(self):
        """ExtensionAPI.ui returns the context's ui (cached per context)."""
        api = ExtensionAPI()
        ui1 = api.ui
        ui2 = api.ui
        assert ui1 is ui2

    def test_context_property_exists(self):
        """ExtensionAPI has context property returning ExtensionContext."""
        api = ExtensionAPI()
        ctx = api.context
        assert isinstance(ctx, ExtensionContext)


# ──────────────────────────────────────────────────────────────────────────────
# ui.form — declarative form spec (E10 §6 / S66)
# ──────────────────────────────────────────────────────────────────────────────


_FULL_FORM_SPEC = {
    "title": "New task",
    "fields": [
        {"name": "desc", "kind": "text", "label": "Description", "default": "draft"},
        {"name": "prio", "kind": "select", "options": ["low", "high"], "default": "high"},
        {"name": "tags", "kind": "multiselect", "options": ["a", "b", "c"], "default": ["b"]},
        {"name": "urgent", "kind": "confirm", "default": True},
        {"name": "points", "kind": "number", "default": 3},
    ],
}


class TestValidateFormSpec:
    """validate_form_spec normalizes a good spec and Fail-Early rejects bad ones."""

    def test_normalizes_title_and_fields(self):
        title, fields = validate_form_spec(_FULL_FORM_SPEC)
        assert title == "New task"
        assert [f["name"] for f in fields] == ["desc", "prio", "tags", "urgent", "points"]
        # label defaults to name when absent (the select field declared none).
        assert next(f for f in fields if f["name"] == "prio")["label"] == "prio"
        # options preserved for select/multiselect.
        assert next(f for f in fields if f["name"] == "tags")["options"] == ["a", "b", "c"]

    def test_title_defaults_to_form(self):
        title, _ = validate_form_spec({"fields": [{"name": "x", "kind": "text"}]})
        assert title == "Form"

    def test_non_dict_spec_raises(self):
        with pytest.raises(ValueError, match="spec must be a dict"):
            validate_form_spec(["not", "a", "dict"])

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_form_spec({"fields": []})

    def test_missing_fields_raises(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_form_spec({"title": "x"})

    def test_field_missing_name_raises(self):
        with pytest.raises(ValueError, match="non-empty string 'name'"):
            validate_form_spec({"fields": [{"kind": "text"}]})

    def test_duplicate_name_raises(self):
        spec = {"fields": [{"name": "x", "kind": "text"}, {"name": "x", "kind": "number"}]}
        with pytest.raises(ValueError, match="duplicate field name"):
            validate_form_spec(spec)

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown kind"):
            validate_form_spec({"fields": [{"name": "x", "kind": "slider"}]})

    def test_select_without_options_raises(self):
        with pytest.raises(ValueError, match="needs a non-empty 'options' list"):
            validate_form_spec({"fields": [{"name": "x", "kind": "select"}]})

    def test_multiselect_non_string_options_raises(self):
        spec = {"fields": [{"name": "x", "kind": "multiselect", "options": [1, 2]}]}
        with pytest.raises(ValueError, match="'options' must all be strings"):
            validate_form_spec(spec)

    def test_non_string_label_raises(self):
        spec = {"fields": [{"name": "x", "kind": "text", "label": 5}]}
        with pytest.raises(ValueError, match="label must be a string"):
            validate_form_spec(spec)


class TestFormHeadlessValue:
    """form_headless_value returns the declared default or the kind's empty value."""

    def test_declared_default_wins(self):
        _, fields = validate_form_spec(_FULL_FORM_SPEC)
        by_name = {f["name"]: f for f in fields}
        assert form_headless_value(by_name["desc"]) == "draft"
        assert form_headless_value(by_name["prio"]) == "high"
        assert form_headless_value(by_name["tags"]) == ["b"]
        assert form_headless_value(by_name["urgent"]) is True
        assert form_headless_value(by_name["points"]) == 3

    def test_natural_empty_per_kind_without_default(self):
        spec = {
            "fields": [
                {"name": "t", "kind": "text"},
                {"name": "n", "kind": "number"},
                {"name": "c", "kind": "confirm"},
                {"name": "m", "kind": "multiselect", "options": ["a", "b"]},
                {"name": "s", "kind": "select", "options": ["a", "b"]},
            ]
        }
        _, fields = validate_form_spec(spec)
        by_name = {f["name"]: f for f in fields}
        assert form_headless_value(by_name["t"]) == ""
        assert form_headless_value(by_name["n"]) == 0
        assert form_headless_value(by_name["c"]) is False
        assert form_headless_value(by_name["m"]) == []
        # select has no empty value — falls back to its first (concrete) option.
        assert form_headless_value(by_name["s"]) == "a"


class TestExtensionUIForm:
    """ExtensionUI.form headless routing: raise / policy defaults / json record."""

    @pytest.mark.asyncio
    async def test_form_raises_headless_without_policy(self):
        # Fail-Early: no form policy → raise, NEVER silently auto-fill.
        ui = ExtensionUI(mode="headless")
        with pytest.raises(HeadlessDialogError):
            await ui.form(_FULL_FORM_SPEC)

    @pytest.mark.asyncio
    async def test_form_defaults_policy_returns_declared_defaults(self):
        ui = ExtensionUI(mode="headless")
        ui.set_headless_defaults({"form": "defaults"})
        answers = await ui.form(_FULL_FORM_SPEC)
        assert answers == {
            "desc": "draft",
            "prio": "high",
            "tags": ["b"],
            "urgent": True,
            "points": 3,
        }

    @pytest.mark.asyncio
    async def test_form_validates_before_policy(self):
        # A malformed spec fails up front regardless of policy (no UI shown).
        ui = ExtensionUI(mode="headless")
        ui.set_headless_defaults({"form": "defaults"})
        with pytest.raises(ValueError, match="unknown kind"):
            await ui.form({"fields": [{"name": "x", "kind": "nope"}]})

    @pytest.mark.asyncio
    async def test_form_emits_json_record_then_resolves(self):
        ui = ExtensionUI(mode="headless")
        ui.set_headless_defaults({"form": "defaults"})
        records: list[dict] = []
        ui.set_record_sink(records.append)
        answers = await ui.form(_FULL_FORM_SPEC)
        assert answers["prio"] == "high"
        assert len(records) == 1
        rec = records[0]
        assert rec["type"] == "extension"
        assert rec["kind"] == "form"
        assert rec["extension"] is None
        assert rec["title"] == "New task"
        assert [f["name"] for f in rec["fields"]] == ["desc", "prio", "tags", "urgent", "points"]

    @pytest.mark.asyncio
    async def test_form_emits_record_even_when_it_will_raise(self):
        # The request is visible on the stream before the Fail-Early raise.
        ui = ExtensionUI(mode="headless")
        records: list[dict] = []
        ui.set_record_sink(records.append)
        with pytest.raises(HeadlessDialogError):
            await ui.form(_FULL_FORM_SPEC)
        assert len(records) == 1
        assert records[0]["kind"] == "form"

    @pytest.mark.asyncio
    async def test_form_ui_defaults_rejects_bad_form_token(self):
        # Only "defaults" is a valid form answer (validated like every method).
        ui = ExtensionUI(mode="headless")
        with pytest.raises(ValueError, match="form="):
            ui.set_headless_defaults({"form": "yes"})

    @pytest.mark.asyncio
    async def test_form_delegates_in_tui_mode(self):
        # TUI mode routes to the delegate (a human fills it); no policy needed.
        ui = ExtensionUI(mode="tui")

        class _Delegate:
            async def form(self, spec):
                return {"desc": "typed", "prio": "low"}

        ui._tui_delegate = _Delegate()
        answers = await ui.form(_FULL_FORM_SPEC)
        assert answers == {"desc": "typed", "prio": "low"}


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionContext — constructor and properties
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionContext:
    """Tests for ExtensionContext constructor and properties."""

    def test_create_context_defaults(self):
        """ExtensionContext can be instantiated with no arguments."""
        ctx = ExtensionContext()
        assert ctx is not None

    def test_context_cwd(self):
        """ExtensionContext.cwd returns the cwd argument."""
        ctx = ExtensionContext(cwd="/tmp/test")
        assert ctx.cwd == "/tmp/test"

    def test_context_cwd_default(self):
        """ExtensionContext.cwd defaults to '.'."""
        ctx = ExtensionContext()
        assert ctx.cwd == "."

    def test_context_session_manager(self):
        """ExtensionContext.session_manager returns the argument."""
        ctx = ExtensionContext(session_manager="manager")
        assert ctx.session_manager == "manager"

    def test_context_session_manager_default(self):
        """ExtensionContext.session_manager defaults to None."""
        ctx = ExtensionContext()
        assert ctx.session_manager is None

    def test_context_signal(self):
        """ExtensionContext.signal returns the argument."""
        mock_signal = MagicMock()
        ctx = ExtensionContext(signal=mock_signal)
        assert ctx.signal is mock_signal

    def test_context_signal_default(self):
        """ExtensionContext.signal defaults to None."""
        ctx = ExtensionContext()
        assert ctx.signal is None

    def test_context_is_idle(self):
        """ExtensionContext.is_idle returns the argument."""
        ctx = ExtensionContext(is_idle=False)
        assert ctx.is_idle is False

    def test_context_is_idle_default(self):
        """ExtensionContext.is_idle defaults to True."""
        ctx = ExtensionContext()
        assert ctx.is_idle is True

    def test_context_has_ui(self):
        """ExtensionContext has an internal ExtensionUI."""
        ctx = ExtensionContext()
        assert ctx._ui is not None
        assert isinstance(ctx._ui, ExtensionUI)

    def test_context_abort_no_signal(self):
        """ExtensionContext.abort() is a no-op when signal is None."""
        ctx = ExtensionContext()
        ctx.abort()  # should not raise

    def test_context_abort_with_signal(self):
        """ExtensionContext.abort() calls signal.abort() when signal is present."""
        mock_signal = MagicMock()
        ctx = ExtensionContext(signal=mock_signal)
        ctx.abort()
        mock_signal.abort.assert_called_once()

    def test_context_shutdown_no_manager(self):
        """ExtensionContext.shutdown() is a no-op when session_manager is None."""
        ctx = ExtensionContext()
        ctx.shutdown()  # should not raise

    def test_context_shutdown_with_manager(self):
        """ExtensionContext.shutdown() calls session_manager.shutdown()."""
        mock_manager = MagicMock()
        ctx = ExtensionContext(session_manager=mock_manager)
        ctx.shutdown()
        mock_manager.shutdown.assert_called_once()

    def test_context_get_context_usage_raises_without_session(self):
        """get_context_usage() raises when no session is bound (Fail-Early)."""
        ctx = ExtensionContext()
        with pytest.raises(RuntimeError):
            ctx.get_context_usage()

    def test_context_set_ui_delegate(self):
        """ExtensionContext.set_ui_delegate() sets the TUI delegate."""
        mock_delegate = MagicMock()
        ctx = ExtensionContext()
        ctx.set_ui_delegate(mock_delegate)
        assert ctx._ui._mode == "tui"
        assert ctx._ui._tui_delegate is mock_delegate


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionUI — headless mode (no-op behavior)
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionUI:
    """Tests for ExtensionUI (headless dialog policy).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface"; E7 §3 / S48. In
    headless mode a blocking dialog raises by default (no human to ask) and
    auto-answers only under an explicit ``--ui-defaults`` policy.
    """

    @pytest.mark.asyncio
    async def test_confirm_raises_without_policy(self):
        """ExtensionUI.confirm() raises headless with no policy (S48)."""
        ui = ExtensionUI()
        with pytest.raises(HeadlessDialogError):
            await ui.confirm("Title", "Message")

    @pytest.mark.asyncio
    async def test_confirm_yes_and_no(self):
        """ExtensionUI.confirm() maps yes/true→True, no/false→False (S48)."""
        assert await ExtensionUI(headless_policy={"confirm": "yes"}).confirm("t", "m") is True
        assert await ExtensionUI(headless_policy={"confirm": "true"}).confirm("t", "m") is True
        assert await ExtensionUI(headless_policy={"confirm": "no"}).confirm("t", "m") is False
        assert await ExtensionUI(headless_policy={"confirm": "false"}).confirm("t", "m") is False

    @pytest.mark.asyncio
    async def test_select_raises_without_policy(self):
        """ExtensionUI.select() raises headless with no policy (S48)."""
        ui = ExtensionUI()
        with pytest.raises(HeadlessDialogError):
            await ui.select("Title", ["Option 1", "Option 2"])

    @pytest.mark.asyncio
    async def test_select_returns_first_item_with_policy(self):
        """ExtensionUI.select() returns first item under select=first (S48)."""
        ui = ExtensionUI(headless_policy={"select": "first"})
        result = await ui.select("Title", ["Option 1", "Option 2"])
        assert result == "Option 1"

    @pytest.mark.asyncio
    async def test_select_returns_none_for_empty_list_with_policy(self):
        """ExtensionUI.select() returns None for empty list under select=first (S48)."""
        ui = ExtensionUI(headless_policy={"select": "first"})
        result = await ui.select("Title", [])
        assert result is None

    @pytest.mark.asyncio
    async def test_input_raises_without_policy(self):
        """ExtensionUI.input() raises headless with no policy (S48)."""
        ui = ExtensionUI()
        with pytest.raises(HeadlessDialogError):
            await ui.input("Title", default="default_value")

    @pytest.mark.asyncio
    async def test_input_returns_default_with_policy(self):
        """ExtensionUI.input() returns default value under input=default (S48)."""
        ui = ExtensionUI(headless_policy={"input": "default"})
        result = await ui.input("Title", default="default_value")
        assert result == "default_value"

    @pytest.mark.asyncio
    async def test_input_returns_empty_string_without_default(self):
        """ExtensionUI.input() returns empty string when no default (input=default)."""
        ui = ExtensionUI(headless_policy={"input": "default"})
        result = await ui.input("Title")
        assert result == ""

    def test_set_headless_defaults_rejects_unknown_method(self):
        """set_headless_defaults raises on an unknown dialog method (Fail-Early)."""
        ui = ExtensionUI()
        with pytest.raises(ValueError):
            ui.set_headless_defaults({"bogus": "yes"})

    def test_set_headless_defaults_rejects_unknown_token(self):
        """set_headless_defaults raises on an invalid answer token (Fail-Early)."""
        ui = ExtensionUI()
        with pytest.raises(ValueError):
            ui.set_headless_defaults({"confirm": "maybe"})
        with pytest.raises(ValueError):
            ui.set_headless_defaults({"select": "last"})

    def test_notify_noop(self):
        """ExtensionUI.notify() is a no-op in headless mode (prints to stderr)."""
        ui = ExtensionUI()
        # Should not raise
        ui.notify("Test message")
        ui.notify("Test message", level="info")
        ui.notify("Test message", level="warning")
        ui.notify("Test message", level="error")

    def test_notify_accepts_level(self):
        """ExtensionUI.notify() accepts level parameter."""
        ui = ExtensionUI()
        ui.notify("Test", level="info")
        ui.notify("Test", level="warning")
        ui.notify("Test", level="error")

    def test_notify_prints_to_stderr(self):
        """ExtensionUI.notify() in headless mode prints to stderr."""
        ui = ExtensionUI()
        with patch("sys.stderr", new=io.StringIO()) as mock_stderr:
            ui.notify("Hello from extension", "info")
            output = mock_stderr.getvalue()
            assert "[τ] info: Hello from extension" in output

    def test_notify_default_level(self):
        """ExtensionUI.notify() defaults to 'info' level."""
        ui = ExtensionUI()
        with patch("sys.stderr", new=io.StringIO()) as mock_stderr:
            ui.notify("Test message")
            output = mock_stderr.getvalue()
            assert "info:" in output

    @pytest.mark.asyncio
    async def test_confirm_returns_async_bool(self):
        """ExtensionUI.confirm() is async and returns bool (under a policy)."""
        ui = ExtensionUI(headless_policy={"confirm": "yes"})
        result = await ui.confirm("Title", "Message")
        assert isinstance(result, bool)
        assert result is True

    @pytest.mark.asyncio
    async def test_select_returns_async_str_or_none(self):
        """ExtensionUI.select() is async and returns str or None (under a policy)."""
        ui = ExtensionUI(headless_policy={"select": "first"})
        result = await ui.select("Title", ["a", "b"])
        assert isinstance(result, str)
        assert result == "a"

    @pytest.mark.asyncio
    async def test_input_returns_async_str(self):
        """ExtensionUI.input() is async and returns str (under a policy)."""
        ui = ExtensionUI(headless_policy={"input": "default"})
        result = await ui.input("Title", default="def")
        assert isinstance(result, str)
        assert result == "def"

    def test_init_with_tui_mode(self):
        """ExtensionUI can be initialized with mode='tui'."""
        ui = ExtensionUI(mode="tui")
        assert ui._mode == "tui"

    def test_init_with_headless_mode(self):
        """ExtensionUI can be initialized with mode='headless'."""
        ui = ExtensionUI(mode="headless")
        assert ui._mode == "headless"

    def test_init_mode_defaults_to_headless(self):
        """ExtensionUI mode defaults to 'headless'."""
        ui = ExtensionUI()
        assert ui._mode == "headless"


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionUI — TUI delegation
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionUITUI:
    """Tests for ExtensionUI TUI delegation mode."""

    @pytest.mark.asyncio
    async def test_tui_confirm_delegates(self):
        """ExtensionUI.confirm() delegates to TUI delegate in TUI mode."""
        class MockDelegate:
            async def confirm(self, title, message):
                return False

        ui = ExtensionUI(mode="headless")
        ui._mode = "tui"
        ui._tui_delegate = MockDelegate()
        result = await ui.confirm("Title", "Message")
        assert result is False

    @pytest.mark.asyncio
    async def test_tui_select_delegates(self):
        """ExtensionUI.select() delegates to TUI delegate in TUI mode."""
        class MockDelegate:
            async def select(self, title, items):
                return items[1]  # return second item

        ui = ExtensionUI(mode="headless")
        ui._mode = "tui"
        ui._tui_delegate = MockDelegate()
        result = await ui.select("Title", ["a", "b", "c"])
        assert result == "b"

    @pytest.mark.asyncio
    async def test_tui_input_delegates(self):
        """ExtensionUI.input() delegates to TUI delegate in TUI mode."""
        class MockDelegate:
            async def input(self, title, default):
                return "user_typed"

        ui = ExtensionUI(mode="headless")
        ui._mode = "tui"
        ui._tui_delegate = MockDelegate()
        result = await ui.input("Title", default="default")
        assert result == "user_typed"

    def test_tui_notify_delegates(self):
        """ExtensionUI.notify() delegates to TUI delegate in TUI mode."""
        class MockDelegate:
            def notify(self, message, level):
                self.last_notify = (message, level)

        delegate = MockDelegate()
        ui = ExtensionUI(mode="headless")
        ui._mode = "tui"
        ui._tui_delegate = delegate
        ui.notify("Hello", "warning")
        assert delegate.last_notify == ("Hello", "warning")

    @pytest.mark.asyncio
    async def test_tui_mode_without_delegate_uses_headless_policy(self):
        """TUI mode without a delegate falls through to the headless policy (S48)."""
        ui = ExtensionUI(mode="tui")
        # No delegate set — falls through to headless behavior.
        assert ui._mode == "tui"
        assert ui._tui_delegate is None
        # With no policy the fall-through raises (Fail-Early, no silent auto-answer).
        with pytest.raises(HeadlessDialogError):
            await ui.confirm("T", "M")
        # With a policy it honors it.
        ui.set_headless_defaults({"confirm": "yes"})
        assert await ui.confirm("T", "M") is True

    def test_set_ui_delegate_enables_tui_mode(self):
        """ExtensionUI.set_ui_delegate() sets mode to TUI and delegate."""
        ui = ExtensionUI(mode="headless")
        assert ui._mode == "headless"
        mock_delegate = MagicMock()
        ui._tui_delegate = mock_delegate
        ui._mode = "tui"  # Simulate set_ui_delegate()
        assert ui._mode == "tui"
        assert ui._tui_delegate is mock_delegate


# ──────────────────────────────────────────────────────────────────────────────
# ExtensionAPI — integration tests
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionAPIIntegration:
    """Integration tests for ExtensionAPI combining multiple features."""

    def test_api_with_registry_event_bus_context_session(self):
        """All constructor args work together."""
        reg = ExtensionRegistry()
        bus = EventBus()
        ctx = ExtensionContext(cwd="/test")
        session = MagicMock()
        api = ExtensionAPI(
            registry=reg,
            event_bus=bus,
            context=ctx,
            session=session,
        )
        assert api._registry is reg
        assert api._event_bus is bus
        assert api._context is ctx
        assert api._session is session

    def test_tool_registration_event_subscription_and_entry_persistence(self):
        """Tool registration, event subscription, and entry persistence work together."""
        api = ExtensionAPI()
        received = []

        # Register event handler
        api.on("agent_start", lambda e: received.append(e))

        # Register tool
        api.register_tool({
            "name": "test_tool",
            "description": "test desc",
            "parameters": {"type": "object"},
            "execute": lambda: None,
        })
        tools = api.get_all_tools()
        assert len(tools) == 1
        assert tools[0].source == "extension"

        # Append entry — now DURABLE (S39): delegates to the bound session, not a
        # RAM registry store (removed with G4).
        mock_session = MagicMock()
        session_api = ExtensionAPI(session=mock_session)
        session_api.append_entry("counter", {"value": 42})
        mock_session._append_custom_entry.assert_called_once_with("counter", {"value": 42})

    def test_ui_property_reflects_context_ui(self):
        """ExtensionAPI.ui returns the context's ExtensionUI."""
        ctx = ExtensionContext()
        ctx.set_ui_delegate(MagicMock())
        api = ExtensionAPI(context=ctx)
        # The API's ui should reflect the context's TUI-enabled ui
        assert api.ui._mode == "tui"

    def test_extension_works_in_both_modes(self):
        """The same ExtensionAPI setup works in both TUI and headless modes."""
        # Headless
        api_headless = ExtensionAPI()
        ui_h = api_headless.ui
        assert ui_h._mode == "headless"
        assert api_headless.ui._tui_delegate is None

        # TUI (simulated)
        class MockTUI:
            async def confirm(self, title, message):
                return True
            async def select(self, title, items):
                return items[0] if items else None
            async def input(self, title, default):
                return default
            def notify(self, message, level):
                pass

        ctx = ExtensionContext()
        api_tui = ExtensionAPI(context=ctx)
        ctx.set_ui_delegate(MockTUI())
        assert ctx._ui._mode == "tui"
        assert ctx._ui._tui_delegate is not None


# ──────────────────────────────────────────────────────────────────────────────
# Import tests
# ──────────────────────────────────────────────────────────────────────────────


class TestExtensionTypesImport:
    """Tests for module-level imports."""

    def test_import_from_module(self):
        """All types import from extension_types module."""
        from tau_agent_core.extension_types import (
            ExtensionAPI,
            ExtensionContext,
            ExtensionUI,
        )
        assert ExtensionAPI is not None
        assert ExtensionContext is not None
        assert ExtensionUI is not None

    def test_import_from_package_root(self):
        """All types import from tau_agent_core package root."""
        from tau_agent_core import (
            ExtensionAPI,
            ExtensionContext,
            ExtensionUI,
        )
        assert ExtensionAPI is not None
        assert ExtensionContext is not None
        assert ExtensionUI is not None

    def test_types_are_correct_classes(self):
        """Imported types are the correct classes."""
        from tau_agent_core import (
            ExtensionAPI as API,
            ExtensionContext as Context,
            ExtensionUI as UI,
        )
        from tau_agent_core.extension_types import (
            ExtensionAPI,
            ExtensionContext,
            ExtensionUI,
        )
        assert API is ExtensionAPI
        assert Context is ExtensionContext
        assert UI is ExtensionUI
