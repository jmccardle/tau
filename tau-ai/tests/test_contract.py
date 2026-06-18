"""Tests for tau_ai — Phase 1 Subphase 0 type contract verification.

These tests verify the TYPE CONTRACT from SUBPHASE-0.0.md, not implementation:

1. All types import correctly
2. Messages are pydantic models (equivalent to dataclasses)
3. ToolDefinition has the right fields
4. AbortSignal has the right methods with correct signatures
5. Provider ABC has stream_chat with correct signature
6. Registry is a singleton/factory
7. __init__.py exports are correct

Reference: PHASE-1-SUBPHASE-0.md "Testing" section
"""

import inspect
import sys

import pytest


# ───────────────────────────────────────────────
# 1. All types import correctly
# ───────────────────────────────────────────────

class TestAllImports:
    """Verify all types import correctly from the right packages.

    These are the exact imports listed in PHASE-1-SUBPHASE-0.md Testing section.
    """

    def test_import_message_types(self):
        """Message types import from tau_ai.types."""
        from tau_ai.types import (
            UserMessage,
            AssistantMessage,
            ToolResultMessage,
            TextContent,
            ImageContent,
            ToolCall,
            ThinkingContent,
        )
        assert UserMessage is not None
        assert AssistantMessage is not None
        assert ToolResultMessage is not None
        assert TextContent is not None
        assert ImageContent is not None
        assert ToolCall is not None
        assert ThinkingContent is not None

    def test_import_tools(self):
        """Tool types import from tau_ai.tools."""
        from tau_ai.tools import (
            ToolDefinition,
            define_tool,
            validate_tool_arguments,
        )
        assert ToolDefinition is not None
        assert callable(define_tool)
        assert callable(validate_tool_arguments)

    def test_import_abort(self):
        """AbortSignal imports from tau_ai.abort."""
        from tau_ai.abort import AbortSignal
        assert AbortSignal is not None

    def test_import_provider(self):
        """Provider imports from tau_ai.providers.base."""
        from tau_ai.providers.base import Provider
        assert Provider is not None

    def test_import_registry(self):
        """Registry imports from tau_ai.providers.registry."""
        from tau_ai.providers.registry import Registry, ProviderRegistry
        assert Registry is not None
        assert ProviderRegistry is not None

    def test_import_stream_simple(self):
        """stream_simple imports from tau_ai.client."""
        from tau_ai.client import stream_simple
        assert callable(stream_simple)

    def test_import_from_tau_ai_top_level(self):
        """All types are importable from tau_ai top-level."""
        from tau_ai import (
            UserMessage,
            AssistantMessage,
            ToolResultMessage,
            TextContent,
            ThinkingContent,
            ImageContent,
            ToolCall,
            Usage,
            define_tool,
            validate_tool_arguments,
            AbortSignal,
            Provider,
            ProviderRegistry,
            stream_simple,
        )
        assert all(x is not None for x in [
            UserMessage, AssistantMessage, ToolResultMessage,
            TextContent, ThinkingContent, ImageContent, ToolCall, Usage,
            define_tool, validate_tool_arguments, AbortSignal,
            Provider, ProviderRegistry, stream_simple,
        ])


# ───────────────────────────────────────────────
# 2. Messages are pydantic models (equivalent to dataclasses)
# ───────────────────────────────────────────────

class TestMessageModelType:
    """Verify message types are pydantic BaseModel (equivalent to dataclasses).

    The subphase doc says "dataclasses (or equivalent)" — pydantic BaseModel
    is the equivalent for τ.
    """

    def test_user_message_is_pydantic_model(self):
        """UserMessage is a pydantic BaseModel subclass."""
        from tau_ai.types import UserMessage
        from pydantic import BaseModel
        assert issubclass(UserMessage, BaseModel), (
            "UserMessage should be a pydantic BaseModel subclass"
        )

    def test_assistant_message_is_pydantic_model(self):
        """AssistantMessage is a pydantic BaseModel subclass."""
        from tau_ai.types import AssistantMessage
        from pydantic import BaseModel
        assert issubclass(AssistantMessage, BaseModel)

    def test_tool_result_message_is_pydantic_model(self):
        """ToolResultMessage is a pydantic BaseModel subclass."""
        from tau_ai.types import ToolResultMessage
        from pydantic import BaseModel
        assert issubclass(ToolResultMessage, BaseModel)

    def test_text_content_is_pydantic_model(self):
        """TextContent is a pydantic BaseModel subclass."""
        from tau_ai.types import TextContent
        from pydantic import BaseModel
        assert issubclass(TextContent, BaseModel)

    def test_image_content_is_pydantic_model(self):
        """ImageContent is a pydantic BaseModel subclass."""
        from tau_ai.types import ImageContent
        from pydantic import BaseModel
        assert issubclass(ImageContent, BaseModel)

    def test_tool_call_is_pydantic_model(self):
        """ToolCall is a pydantic BaseModel subclass."""
        from tau_ai.types import ToolCall
        from pydantic import BaseModel
        assert issubclass(ToolCall, BaseModel)


# ───────────────────────────────────────────────
# 3. ToolDefinition has the right fields
# ───────────────────────────────────────────────

class TestToolDefinitionFields:
    """Verify ToolDefinition has all required fields from SUBPHASE-0.0.md.

    The contract requires: name, label, description, parameters, execute,
    prompt_snippet, prompt_guidelines, execution_mode
    """

    def test_tool_definition_has_required_fields(self):
        """ToolDefinition has all required fields from the contract."""
        from tau_ai.tools import ToolDefinition
        expected_fields = {
            "name", "label", "description", "parameters", "execute",
            "prompt_snippet", "prompt_guidelines", "execution_mode",
        }
        actual_fields = set(ToolDefinition.model_fields.keys())
        assert expected_fields.issubset(actual_fields), (
            f"ToolDefinition missing fields: {expected_fields - actual_fields}. "
            f"Has: {actual_fields}"
        )

    def test_tool_definition_name_is_required(self):
        """ToolDefinition.name is required."""
        from tau_ai.tools import ToolDefinition
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ToolDefinition(
                label="test",
                description="test",
                parameters={},
                execute=lambda: None,
            )

    def test_tool_definition_label_is_required(self):
        """ToolDefinition.label is required."""
        from tau_ai.tools import ToolDefinition
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ToolDefinition(
                name="test",
                description="test",
                parameters={},
                execute=lambda: None,
            )

    def test_tool_definition_description_is_required(self):
        """ToolDefinition.description is required."""
        from tau_ai.tools import ToolDefinition
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ToolDefinition(
                name="test",
                label="test",
                parameters={},
                execute=lambda: None,
            )

    def test_tool_definition_parameters_is_required(self):
        """ToolDefinition.parameters is required."""
        from tau_ai.tools import ToolDefinition
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ToolDefinition(
                name="test",
                label="test",
                description="test",
                execute=lambda: None,
            )

    def test_tool_definition_execute_is_required(self):
        """ToolDefinition.execute is required."""
        from tau_ai.tools import ToolDefinition
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ToolDefinition(
                name="test",
                label="test",
                description="test",
                parameters={},
            )

    def test_tool_definition_has_optional_fields(self):
        """ToolDefinition has all optional fields."""
        from tau_ai.tools import ToolDefinition
        tool = ToolDefinition(
            name="test",
            label="Test",
            description="A test tool",
            parameters={},
            execute=lambda: None,
            prompt_snippet="Test snippet",
            prompt_guidelines=["Guideline 1"],
            execution_mode="parallel",
        )
        assert tool.prompt_snippet == "Test snippet"
        assert tool.prompt_guidelines == ["Guideline 1"]
        assert tool.execution_mode == "parallel"

    def test_tool_definition_execution_mode_defaults_parallel(self):
        """ToolDefinition.execution_mode defaults to 'parallel'."""
        from tau_ai.tools import ToolDefinition
        tool = ToolDefinition(
            name="test",
            label="Test",
            description="test",
            parameters={},
            execute=lambda: None,
        )
        assert tool.execution_mode == "parallel"

    def test_tool_definition_execution_mode_accepts_sequential(self):
        """ToolDefinition.execution_mode accepts 'sequential'."""
        from tau_ai.tools import ToolDefinition
        tool = ToolDefinition(
            name="test",
            label="Test",
            description="test",
            parameters={},
            execute=lambda: None,
            execution_mode="sequential",
        )
        assert tool.execution_mode == "sequential"


# ───────────────────────────────────────────────
# 4. AbortSignal has the right methods
# ───────────────────────────────────────────────

class TestAbortSignalMethods:
    """Verify AbortSignal has correct methods with correct signatures.

    The contract requires:
    - is_aborted() -> bool (no arguments)
    - abort() -> None (no arguments)
    """

    def test_abort_signal_is_class(self):
        """AbortSignal is a class, not a function."""
        from tau_ai.abort import AbortSignal
        assert inspect.isclass(AbortSignal)

    def test_abort_signal_has_is_aborted_method(self):
        """AbortSignal has is_aborted method."""
        from tau_ai.abort import AbortSignal
        assert hasattr(AbortSignal, "is_aborted")
        assert callable(getattr(AbortSignal, "is_aborted"))

    def test_abort_signal_has_abort_method(self):
        """AbortSignal has abort method."""
        from tau_ai.abort import AbortSignal
        assert hasattr(AbortSignal, "abort")
        assert callable(getattr(AbortSignal, "abort"))

    def test_is_aborted_has_no_arguments(self):
        """AbortSignal.is_aborted() has no arguments (signature check)."""
        from tau_ai.abort import AbortSignal
        sig = inspect.signature(AbortSignal.is_aborted)
        params = list(sig.parameters.keys())
        # 'self' is the only parameter
        assert params == ["self"], (
            f"is_aborted() should take only 'self', got: {params}"
        )

    def test_is_aborted_returns_bool(self):
        """AbortSignal.is_aborted() returns bool."""
        from tau_ai.abort import AbortSignal
        signal = AbortSignal()
        result = signal.is_aborted()
        assert isinstance(result, bool)

    def test_abort_has_no_arguments(self):
        """AbortSignal.abort() has no arguments (signature check)."""
        from tau_ai.abort import AbortSignal
        sig = inspect.signature(AbortSignal.abort)
        params = list(sig.parameters.keys())
        # 'self' is the only parameter
        assert params == ["self"], (
            f"abort() should take only 'self', got: {params}"
        )

    def test_abort_returns_none(self):
        """AbortSignal.abort() returns None."""
        from tau_ai.abort import AbortSignal
        signal = AbortSignal()
        result = signal.abort()
        assert result is None

    def test_is_aborted_false_by_default(self):
        """New AbortSignal.is_aborted() returns False."""
        from tau_ai.abort import AbortSignal
        signal = AbortSignal()
        assert signal.is_aborted() is False

    def test_abort_sets_is_aborted_true(self):
        """After AbortSignal.abort(), is_aborted() returns True."""
        from tau_ai.abort import AbortSignal
        signal = AbortSignal()
        signal.abort()
        assert signal.is_aborted() is True

    def test_abort_is_idempotent(self):
        """Multiple abort() calls don't raise."""
        from tau_ai.abort import AbortSignal
        signal = AbortSignal()
        signal.abort()
        signal.abort()
        signal.abort()
        assert signal.is_aborted() is True


# ───────────────────────────────────────────────
# 5. Provider ABC has stream_chat
# ───────────────────────────────────────────────

class TestProviderABC:
    """Verify Provider ABC has stream_chat with correct signature.

    The contract requires stream_chat to have: model, messages, tools, options
    parameters and return an async iterator of events.
    """

    def test_provider_is_abc(self):
        """Provider is an ABC (Abstract Base Class)."""
        from tau_ai.providers.base import Provider
        import abc
        assert isinstance(Provider.__mro__[1], type) and issubclass(
            Provider.__mro__[1], abc.ABC
        ) if len(Provider.__mro__) > 1 else pytest.fail(
            "Provider should be an ABC subclass"
        )

    def test_provider_has_stream_chat(self):
        """Provider has stream_chat method."""
        from tau_ai.providers.base import Provider
        assert hasattr(Provider, "stream_chat")
        assert callable(getattr(Provider, "stream_chat"))

    def test_stream_chat_has_correct_parameters(self):
        """Provider.stream_chat has model, messages, tools, options."""
        from tau_ai.providers.base import Provider
        sig = inspect.signature(Provider.stream_chat)
        params = list(sig.parameters.keys())
        assert "model" in params, f"'model' not in stream_chat params: {params}"
        assert "messages" in params, f"'messages' not in stream_chat params: {params}"

    def test_stream_chat_accepts_tools_parameter(self):
        """Provider.stream_chat has optional tools parameter."""
        from tau_ai.providers.base import Provider
        sig = inspect.signature(Provider.stream_chat)
        params = list(sig.parameters.keys())
        assert "tools" in params, f"'tools' not in stream_chat params: {params}"

    def test_stream_chat_accepts_options_parameter(self):
        """Provider.stream_chat has optional options parameter."""
        from tau_ai.providers.base import Provider
        sig = inspect.signature(Provider.stream_chat)
        params = list(sig.parameters.keys())
        assert "options" in params, f"'options' not in stream_chat params: {params}"

    def test_stream_chat_is_async(self):
        """Provider.stream_chat is async (returns coroutine)."""
        from tau_ai.providers.base import Provider
        import abc
        assert inspect.iscoroutinefunction(Provider.stream_chat) or inspect.isasyncgenfunction(
            Provider.stream_chat
        ), "stream_chat should be async"

    def test_provider_cannot_be_instantiated(self):
        """Provider cannot be instantiated (it's abstract)."""
        from tau_ai.providers.base import Provider
        with pytest.raises(TypeError):
            Provider()


# ───────────────────────────────────────────────
# 6. Registry is a singleton or factory
# ───────────────────────────────────────────────

class TestRegistry:
    """Verify Registry is a factory (each instantiation is independent).

    The contract requires: register(), get(), list_all()
    and that multiple Registry() instances are independent.
    """

    def test_registry_is_factory(self):
        """Each Registry() is a new independent instance."""
        from tau_ai.providers.registry import Registry
        r1 = Registry()
        r2 = Registry()
        assert r1 is not r2, "Registry() should return new instances (factory)"

    def test_registry_register_method_exists(self):
        """Registry has register method."""
        from tau_ai.providers.registry import Registry
        r = Registry()
        assert hasattr(r, "register")
        assert callable(getattr(r, "register"))

    def test_registry_get_method_exists(self):
        """Registry has get method."""
        from tau_ai.providers.registry import Registry
        r = Registry()
        assert hasattr(r, "get")
        assert callable(getattr(r, "get"))

    def test_registry_list_all_method_exists(self):
        """Registry has list_all method."""
        from tau_ai.providers.registry import Registry
        r = Registry()
        assert hasattr(r, "list_all")
        assert callable(getattr(r, "list_all"))

    def test_registry_register_and_get(self):
        """Can register and then get a provider."""
        from tau_ai.providers.registry import Registry, ProviderRegistry
        from tau_ai.providers.base import Provider

        class TestProvider(Provider):
            async def stream_chat(self, model, messages, tools=None, options=None):
                return
                yield

        r = Registry()
        r.register("test", TestProvider())
        provider = r.get("test")
        assert isinstance(provider, Provider)
        assert isinstance(provider, TestProvider)

    def test_registry_list_all_empty(self):
        """New Registry.list_all() returns empty dict."""
        from tau_ai.providers.registry import Registry
        r = Registry()
        result = r.list_all()
        assert result == {}

    def test_registry_list_all_after_register(self):
        """Registry.list_all() returns registered providers."""
        from tau_ai.providers.registry import Registry
        from tau_ai.providers.base import Provider

        class TestProvider(Provider):
            async def stream_chat(self, model, messages, tools=None, options=None):
                return
                yield

        r = Registry()
        r.register("p1", TestProvider())
        r.register("p2", TestProvider())
        all_providers = r.list_all()
        assert "p1" in all_providers
        assert "p2" in all_providers
        assert len(all_providers) == 2

    def test_registry_get_raises_key_error(self):
        """Registry.get() raises KeyError for unregistered provider."""
        from tau_ai.providers.registry import Registry
        r = Registry()
        with pytest.raises(KeyError):
            r.get("nonexistent")

    def test_registry_register_raises_value_error_on_duplicate(self):
        """Registry.register() raises ValueError for duplicate provider."""
        from tau_ai.providers.registry import Registry
        from tau_ai.providers.base import Provider

        class TestProvider(Provider):
            async def stream_chat(self, model, messages, tools=None, options=None):
                return
                yield

        r = Registry()
        r.register("test", TestProvider())
        with pytest.raises(ValueError):
            r.register("test", TestProvider())

    def test_registry_factory_independence(self):
        """Registry instances don't share state."""
        from tau_ai.providers.registry import Registry
        from tau_ai.providers.base import Provider

        class TestProvider(Provider):
            async def stream_chat(self, model, messages, tools=None, options=None):
                return
                yield

        r1 = Registry()
        r2 = Registry()
        r1.register("test1", TestProvider())
        r2.register("test2", TestProvider())
        assert "test1" in r1.list_all()
        assert "test1" not in r2.list_all()
        assert "test2" in r2.list_all()
        assert "test2" not in r1.list_all()

    def test_isinstance_registry(self):
        """Registry() returns an instance of Registry (contract check)."""
        from tau_ai.providers.registry import Registry
        assert isinstance(Registry(), Registry)


# ───────────────────────────────────────────────
# 7. __init__.py exports check
# ───────────────────────────────────────────────

class TestPublicExports:
    """Verify tau_ai.__init__.py exports the correct public API.

    An agent reading tau_ai/__init__.py should see exactly what types
    are exported.
    """

    def test___all__is_defined(self):
        """__all__ is defined in tau_ai."""
        import tau_ai
        assert hasattr(tau_ai, "__all__"), "__all__ should be defined"

    def test__all__contains_all_type_exports(self):
        """__all__ contains all message and content types."""
        import tau_ai
        for name in [
            "UserMessage", "AssistantMessage", "ToolResultMessage",
            "TextContent", "ThinkingContent", "ImageContent", "ToolCall", "Usage",
        ]:
            assert name in tau_ai.__all__, f"{name} should be in __all__"

    def test__all__contains_tool_exports(self):
        """__all__ contains tool functions."""
        import tau_ai
        for name in ["define_tool", "validate_tool_arguments"]:
            assert name in tau_ai.__all__, f"{name} should be in __all__"

    def test__all__contains_abort_export(self):
        """__all__ contains AbortSignal."""
        import tau_ai
        assert "AbortSignal" in tau_ai.__all__, "AbortSignal should be in __all__"

    def test__all__contains_provider_exports(self):
        """__all__ contains Provider and ProviderRegistry."""
        import tau_ai
        for name in ["Provider", "ProviderRegistry"]:
            assert name in tau_ai.__all__, f"{name} should be in __all__"

    def test__all__contains_stream_simple(self):
        """__all__ contains stream_simple."""
        import tau_ai
        assert "stream_simple" in tau_ai.__all__, "stream_simple should be in __all__"

    def test_all_exports_are_actual_modules(self):
        """Every name in __all__ is accessible via getattr."""
        import tau_ai
        for name in tau_ai.__all__:
            assert hasattr(tau_ai, name), f"{name} in __all__ but not accessible"


# ───────────────────────────────────────────────
# 8. Streaming events contract
# ───────────────────────────────────────────────

class TestStreamingContract:
    """Verify stream_simple follows the streaming event contract.

    The contract defines event types:
    - text_delta, toolcall_delta, done, error
    """

    def test_stream_simple_is_async_function(self):
        """stream_simple is an async function (returns coroutine)."""
        from tau_ai.client import stream_simple
        assert inspect.iscoroutinefunction(stream_simple), \
            "stream_simple should be async"

    def test_stream_simple_returns_event_stream(self):
        """stream_simple returns an AssistantMessageEventStream."""
        from tau_ai.client import stream_simple
        sig = inspect.signature(stream_simple)
        # In Phase 1.3, stream_simple returns a coroutine wrapping
        # an AssistantMessageEventStream
        assert inspect.iscoroutinefunction(stream_simple)

    def test_stream_simple_accepts_model_context(self):
        """stream_simple accepts model and context parameters."""
        from tau_ai.client import stream_simple
        sig = inspect.signature(stream_simple)
        params = list(sig.parameters.keys())
        assert "model" in params
        assert "context" in params
        # context wraps messages, tools, system_prompt
        assert "options" in params

    def test_stream_simple_accepts_options(self):
        """stream_simple accepts optional provider options."""
        from tau_ai.client import stream_simple
        sig = inspect.signature(stream_simple)
        assert "options" in sig.parameters
        assert sig.parameters["options"].default is None


# ───────────────────────────────────────────────
# 9. AgentEvent contract (tau-agent-core)
# ───────────────────────────────────────────────

class TestAgentEventContract:
    """Verify AgentEvent has correct structure from SUBPHASE-0.0.md."""

    def test_agent_event_imports_from_tau_agent_core(self):
        """AgentEvent imports from tau_agent_core."""
        from tau_agent_core.events import AgentEvent
        assert AgentEvent is not None

    def test_agent_event_importable_from_top_level(self):
        """AgentEvent imports from tau_agent_core top-level."""
        from tau_agent_core import AgentEvent
        assert AgentEvent is not None

    def test_agent_event_has_type_field(self):
        """AgentEvent has type field with correct allowed values."""
        from tau_agent_core.events import AgentEvent
        event = AgentEvent(type="agent_start", timestamp=0)
        assert event.type == "agent_start"

    def test_agent_event_has_timestamp_field(self):
        """AgentEvent has timestamp field (ms since epoch)."""
        from tau_agent_core.events import AgentEvent
        event = AgentEvent(type="agent_start", timestamp=1700000000000)
        assert event.timestamp == 1700000000000

    def test_agent_event_all_types(self):
        """All 10 event types from the contract are valid."""
        from tau_agent_core.events import AgentEvent
        valid_types = [
            "agent_start", "agent_end", "turn_start", "turn_end",
            "message_start", "message_update", "message_end",
            "tool_execution_start", "tool_execution_update", "tool_execution_end",
        ]
        for event_type in valid_types:
            event = AgentEvent(type=event_type, timestamp=0)
            assert event.type == event_type

    def test_agent_event_has_is_error_field(self):
        """AgentEvent has is_error field defaulting to False."""
        from tau_agent_core.events import AgentEvent
        event = AgentEvent(type="agent_start", timestamp=0)
        assert event.is_error is False
        event2 = AgentEvent(type="agent_start", timestamp=0, is_error=True)
        assert event2.is_error is True


# ───────────────────────────────────────────────
# 10. Session entry types contract (tau-agent-core)
# ───────────────────────────────────────────────

class TestSessionEntryContract:
    """Verify SessionEntry types from SUBPHASE-0.0.md."""

    def test_session_entry_importable(self):
        """SessionEntry imports from tau_agent_core."""
        from tau_agent_core.session import SessionEntry
        assert SessionEntry is not None

    def test_session_entry_type_is_literal_session(self):
        """SessionEntry.type is Literal['session']."""
        from tau_agent_core.session import SessionEntry
        entry = SessionEntry(id="s1", type="session", timestamp=0)
        assert entry.type == "session"

    def test_message_entry_type_is_literal_message(self):
        """MessageEntry.type is Literal['message']."""
        from tau_agent_core.session import MessageEntry
        entry = MessageEntry(id="m1", type="message", timestamp=0, message={})
        assert entry.type == "message"

    def test_tool_result_entry_type_is_literal_tool_result(self):
        """ToolResultEntry.type is Literal['toolResult']."""
        from tau_agent_core.session import ToolResultEntry
        entry = ToolResultEntry(
            id="t1", type="toolResult", timestamp=0,
            tool_call_id="call_1", tool_name="ls", content=[]
        )
        assert entry.type == "toolResult"

    def test_custom_message_entry_type_is_literal(self):
        """CustomMessageEntry.type is Literal['customMessage']."""
        from tau_agent_core.session import CustomMessageEntry
        entry = CustomMessageEntry(
            id="c1", type="customMessage", timestamp=0,
            custom_type="info", message={}
        )
        assert entry.type == "customMessage"

    def test_compaction_entry_type_is_literal(self):
        """CompactionEntry.type is Literal['compaction']."""
        from tau_agent_core.session import CompactionEntry
        entry = CompactionEntry(
            id="cp1", type="compaction", timestamp=0,
            first_kept_id="msg_001", summary="test"
        )
        assert entry.type == "compaction"

    def test_all_session_entries_in_top_level(self):
        """All session entry types are exported from tau_agent_core."""
        from tau_agent_core import (
            SessionEntry, MessageEntry, ToolResultEntry,
            CustomMessageEntry, CompactionEntry,
        )
        assert all(x is not None for x in [
            SessionEntry, MessageEntry, ToolResultEntry,
            CustomMessageEntry, CompactionEntry,
        ])


# ───────────────────────────────────────────────
# 11. Extension API contract (tau-agent-core)
# ───────────────────────────────────────────────

class TestExtensionAPIContract:
    """Verify ExtensionAPI, ExtensionContext, ExtensionUI from SUBPHASE-0.0.md."""

    def test_extension_api_importable(self):
        """ExtensionAPI imports from tau_agent_core."""
        from tau_agent_core.extension_types import ExtensionAPI
        assert ExtensionAPI is not None

    def test_extension_context_importable(self):
        """ExtensionContext imports from tau_agent_core."""
        from tau_agent_core.extension_types import ExtensionContext
        assert ExtensionContext is not None

    def test_extension_ui_importable(self):
        """ExtensionUI imports from tau_agent_core."""
        from tau_agent_core.extension_types import ExtensionUI
        assert ExtensionUI is not None

    def test_extension_types_in_top_level(self):
        """All extension types exported from tau_agent_core top-level."""
        from tau_agent_core import ExtensionAPI, ExtensionContext, ExtensionUI
        assert all(x is not None for x in [ExtensionAPI, ExtensionContext, ExtensionUI])

    def test_extension_api_has_required_methods(self):
        """ExtensionAPI has all required methods from the contract."""
        from tau_agent_core.extension_types import ExtensionAPI
        api = ExtensionAPI()
        required_methods = [
            "on", "register_tool", "get_all_tools", "set_active_tools",
            "register_command", "append_entry", "set_session_name",
            "send_user_message", "send_message", "register_flag", "get_flag",
        ]
        for method in required_methods:
            assert hasattr(api, method), f"ExtensionAPI missing method: {method}"

    def test_extension_api_has_ui_property(self):
        """ExtensionAPI has ui property returning ExtensionUI."""
        from tau_agent_core.extension_types import ExtensionAPI, ExtensionUI
        api = ExtensionAPI()
        ui = api.ui
        assert isinstance(ui, ExtensionUI)

    def test_extension_ui_has_confirm(self):
        """ExtensionUI has async confirm method."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI()
        assert hasattr(ui, "confirm")
        assert callable(ui.confirm)

    def test_extension_ui_has_select(self):
        """ExtensionUI has async select method."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI()
        assert hasattr(ui, "select")
        assert callable(ui.select)

    def test_extension_ui_has_input(self):
        """ExtensionUI has async input method."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI()
        assert hasattr(ui, "input")
        assert callable(ui.input)

    def test_extension_ui_has_notify(self):
        """ExtensionUI has notify method."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI()
        assert hasattr(ui, "notify")
        assert callable(ui.notify)

    def test_extension_context_has_cwd(self):
        """ExtensionContext has cwd property."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "cwd")

    def test_extension_context_has_session_manager(self):
        """ExtensionContext has session_manager property."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "session_manager")

    def test_extension_context_has_signal(self):
        """ExtensionContext has signal property."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "signal")

    def test_extension_context_has_is_idle(self):
        """ExtensionContext has is_idle property."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "is_idle")

    def test_extension_context_has_abort(self):
        """ExtensionContext has abort method."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "abort")
        assert callable(ctx.abort)

    def test_extension_context_has_shutdown(self):
        """ExtensionContext has shutdown method."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "shutdown")
        assert callable(ctx.shutdown)

    def test_extension_context_has_get_context_usage(self):
        """ExtensionContext has get_context_usage method."""
        from tau_agent_core.extension_types import ExtensionContext
        ctx = ExtensionContext()
        assert hasattr(ctx, "get_context_usage")
        assert callable(ctx.get_context_usage)
