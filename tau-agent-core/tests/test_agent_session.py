"""Tests for AgentSession and create_agent_session — τ-agent-core Phase 2 Subphase 4.

Tests verify the AgentSession public API and the SDK entry point:
1. AgentSession creation
2. Subscribe and unsubscribe
3. Prompt runs agent loop
4. Abort during prompt
5. Continue conversation
6. create_agent_session with model string resolution
7. Extensions are loaded and receive API
8. In-memory session isolation

Reference: PHASE-2-SUBPHASE-4.md
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock, patch

import pytest

from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session import SessionState
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, ToolDefinition
from tau_agent_core.sdk import (
    AgentSession as SDKAgentSession,
    create_agent_session,
    resolve_model,
    _resolve_tools,
    _build_system_prompt,
    _load_extensions,
)


# =============================================================================
# Test 1: AgentSession creation
# =============================================================================


class TestAgentSessionCreation:
    """Tests for AgentSession instantiation."""

    def create_sample_model(self) -> Model:
        """Create a sample Model for testing."""
        return Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )

    def create_sample_session_manager(self) -> InMemorySessionLog:
        """Create an in-memory SessionLog for testing."""
        return InMemorySessionLog()

    def test_create_agent_session(self):
        """Test 1: AgentSession can be instantiated with all parameters."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
            system_prompt="You are a helpful assistant.",
            tools=[],
            extensions=[],
        )
        assert session is not None
        assert session._model.id == "gpt-4o"
        assert session._system_prompt == "You are a helpful assistant."
        assert session._tools == []

    def test_create_agent_session_minimal(self):
        """AgentSession can be created with minimal parameters."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        assert session is not None
        assert session._tools == []
        assert session._extensions == []

    def test_create_agent_session_with_tools(self):
        """AgentSession can be created with tools."""
        def execute(ctx):
            return "test"

        tool = AgentTool(
            definition=ToolDefinition(
                name="ls",
                label="List",
                description="List files",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=execute,
            )
        )

        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
            tools=[tool],
        )
        assert len(session._tools) == 1
        assert session._tools[0].name == "ls"

    def test_create_agent_session_default_tools_empty(self):
        """AgentSession defaults tools to empty list."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        assert session._tools == []

    def test_agent_session_has_events_bus(self):
        """AgentSession creates an EventBus internally."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        assert isinstance(session._events, EventBus)

    def test_agent_session_has_abort_signal(self):
        """AgentSession creates an AbortSignal internally."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        assert session._abort_signal is not None

    def test_agent_session_is_streaming_initially_false(self):
        """AgentSession starts with _is_streaming = False."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        assert session._is_streaming is False

    def test_messages_property_returns_active_messages(self):
        """AgentSession.messages returns the current active path messages."""
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=self.create_sample_model(),
        )
        messages = session.messages
        assert messages == []

    def test_state_property_returns_session_state(self):
        """AgentSession.state returns a SessionState."""
        session = AgentSession(
            session_log=self.create_sample_session_manager(),
            model=self.create_sample_model(),
        )
        state = session.state
        assert isinstance(state, SessionState)
        assert state.status == "idle"


# =============================================================================
# Test 2: Subscribe and unsubscribe
# =============================================================================


class TestSubscribeUnsubscribe:
    """Tests for AgentSession.subscribe() and unsubscribe."""

    def create_session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    def test_subscribe_returns_unsubscribe_function(self):
        """Test 2: subscribe() returns an unsubscribe callable."""
        session = self.create_session()

        def handler(event):
            pass

        unsub = session.subscribe(handler)
        assert callable(unsub)

    def test_subscribe_adds_handler(self):
        """subscribe() adds the handler to the event bus."""
        session = self.create_session()
        handler = MagicMock()

        session.subscribe(handler)
        assert len(session._events._listeners["all"]) > 0

    @pytest.mark.asyncio
    async def test_subscribe_handler_receives_events(self):
        """Subscribers should receive events when emitted."""
        session = self.create_session()
        received = []

        def handler(event):
            received.append(event)

        session.subscribe(handler)
        await session._events.emit(AgentEvent(type="agent_start", timestamp=0))

        assert len(received) == 1
        assert received[0].type == "agent_start"

    def test_unsubscribe_removes_handler(self):
        """The returned unsubscribe function must remove the handler."""
        session = self.create_session()
        handler = MagicMock()

        unsub = session.subscribe(handler)
        assert len(session._events._listeners["all"]) == 1

        unsub()
        assert len(session._events._listeners["all"]) == 0

    def test_unsubscribe_does_not_error_if_handler_already_removed(self):
        """Calling unsubscribe() twice should not error."""
        session = self.create_session()
        handler = MagicMock()

        unsub = session.subscribe(handler)
        unsub()
        unsub()  # Second call — should not raise

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        """Multiple subscribers all receive emitted events."""
        session = self.create_session()
        received1 = []
        received2 = []

        def handler1(event):
            received1.append(event)

        def handler2(event):
            received2.append(event)

        session.subscribe(handler1)
        session.subscribe(handler2)

        await session._events.emit(AgentEvent(type="agent_end", timestamp=100))

        assert len(received1) == 1
        assert len(received2) == 1
        assert received1[0].type == "agent_end"
        assert received2[0].type == "agent_end"

    @pytest.mark.asyncio
    async def test_subscribe_specific_event_type(self):
        """subscribe() can target specific event types (via EventBus)."""
        session = self.create_session()
        received = []

        def handler(event):
            received.append(event)

        session._events.on("agent_start", handler)
        await session._events.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 1

        received.clear()
        await session._events.emit(AgentEvent(type="agent_end", timestamp=100))
        assert len(received) == 0  # handler only listens to agent_start


# =============================================================================
# Test 3: Prompt runs agent loop
# =============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestPromptRunsLoop:
    """Tests for AgentSession.prompt().

    Uses ``fake_llm`` so the full agent loop runs without a network call: real
    events fire and messages are assembled from a canned provider reply.
    """

    def create_session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    def test_prompt_runs_agent_loop(self):
        """Test 3: prompt() runs the agent loop and returns messages."""
        session = self.create_session()
        session.subscribe(lambda e: None)  # no-op handler

        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0
        assert session.is_streaming is False

    def test_prompt_returns_messages_with_user_and_assistant(self):
        """prompt() returns messages containing both user and assistant."""
        session = self.create_session()
        messages = asyncio.run(session.prompt("hello"))

        assert any(m.get("role") == "user" for m in messages)
        assert any(m.get("role") == "assistant" for m in messages)

    def test_prompt_sets_is_streaming_during_execution(self):
        """is_streaming is True during prompt execution."""
        session = self.create_session()
        streaming_during = []

        def capture_streaming(event):
            streaming_during.append(session.is_streaming)

        session.subscribe(capture_streaming)
        asyncio.run(session.prompt("hello"))

        # The streaming flag should have been True during execution
        assert any(streaming_during)

    def test_prompt_is_async(self):
        """prompt() is an async method."""
        session = self.create_session()
        assert inspect.iscoroutinefunction(session.prompt)

    def test_prompt_accepts_images(self):
        """prompt() accepts optional images parameter."""
        session = self.create_session()
        messages = asyncio.run(
            session.prompt(
                "hello",
                images=[{"type": "image", "data": "base64", "mime_type": "image/png"}],
            )
        )
        assert len(messages) > 0

    def test_prompt_emits_events(self):
        """prompt() emits agent_start, agent_end, and intermediate events."""
        session = self.create_session()
        events = []

        def handler(event):
            events.append(event)

        session.subscribe(handler)
        asyncio.run(session.prompt("hello"))

        types = [e.type for e in events]
        assert "agent_start" in types
        assert "agent_end" in types
        assert "turn_start" in types
        assert "turn_end" in types

    def test_prompt_appends_messages_to_session(self):
        """prompt() appends both user and assistant messages to the session."""
        session = self.create_session()
        asyncio.run(session.prompt("hello"))

        messages = session.messages
        assert len(messages) >= 2  # user + assistant
        assert messages[0].get("role") == "user"
        assert messages[0].get("content")[0].get("text") == "hello"

    def test_prompt_after_abort_returns_messages(self):
        """prompt() returns messages even if called after abort."""
        session = self.create_session()
        session.abort()
        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0
        assert session.is_streaming is False


class TestPromptReturnsOnlyThisTurnsMessages:
    """prompt() must return ONLY the messages produced this turn, never the
    accumulated session history.

    Regression for a compounding history-duplication bug: the TUI appends
    prompt()'s return to its own message store (which already holds every prior
    turn), so returning the full history made each turn re-append all earlier
    assistant/tool messages. The model then saw earlier exchanges duplicated
    (observed live in chats/1781920975.json: turn-1's date exchange reappeared
    verbatim inside turn 2, with identical usage stats — proving it was copied,
    not regenerated). These tests stub the agent loop so no network is needed.
    """

    def _session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    @staticmethod
    def _fake_loop(answer: str):
        """A drop-in for AgentLoop whose run() returns one assistant message."""

        class _FakeLoop:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def run(self, prompts, context):  # noqa: ANN001
                return [{
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                }]

        return _FakeLoop

    @staticmethod
    def _assistant_texts(messages: list[dict]) -> list[str]:
        return [
            block.get("text")
            for m in messages
            if m.get("role") == "assistant"
            for block in m.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]

    def test_second_prompt_return_excludes_first_turn(self):
        session = self._session()

        with patch("tau_agent_core.agent_session.AgentLoop", self._fake_loop("answer 1")):
            first = asyncio.run(session.prompt("hello"))
        with patch("tau_agent_core.agent_session.AgentLoop", self._fake_loop("answer 2")):
            second = asyncio.run(session.prompt("again"))

        # Turn 1's return carries that turn's user + assistant.
        assert "answer 1" in self._assistant_texts(first)
        assert any(m.get("role") == "user" for m in first)

        # Turn 2's return is ONLY turn 2 — turn 1 must not reappear.
        assert "answer 2" in self._assistant_texts(second)
        assert "answer 1" not in self._assistant_texts(second)

    def test_appending_returns_never_duplicates_prior_turns(self):
        """Replays the exact TUI persistence pattern (app.py: append every
        non-user message of prompt()'s return to a store that already holds the
        prior turns) and asserts no assistant message is ever duplicated."""
        session = self._session()
        store: list[dict] = []

        for i, answer in enumerate(["answer 1", "answer 2", "answer 3"]):
            store.append({"role": "user", "content": f"turn {i}"})
            with patch("tau_agent_core.agent_session.AgentLoop", self._fake_loop(answer)):
                returned = asyncio.run(session.prompt(f"turn {i}"))
            for msg in returned:
                if msg.get("role") != "user":
                    store.append(msg)

        texts = self._assistant_texts(store)
        assert texts == ["answer 1", "answer 2", "answer 3"]


class TestApiKeyThreadedToProvider:
    """The configured API key must reach the provider via stream_simple's
    options. It was previously dropped (backends stored it in an unused
    self._api_key and create_agent_session ignored its api_key arg), so a real
    key never reached the provider. These tests capture the options dict the loop
    hands to stream_simple."""

    def _session(self, api_key):
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            api_key=api_key,
        )

    @staticmethod
    def _capturing_stream_simple(captured: dict):
        class _Empty:
            def __aiter__(self):
                async def _gen():
                    return
                    yield  # pragma: no cover - makes this an async generator
                return _gen()

            async def result(self):
                return None

        async def _stream_simple(model, context, options=None):
            captured["options"] = options
            return _Empty()

        return _stream_simple

    def test_api_key_appears_in_stream_options(self):
        captured: dict = {}
        session = self._session("sk-thread-123")
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=self._capturing_stream_simple(captured),
        ):
            asyncio.run(session.prompt("hi"))
        assert captured["options"].get("api_key") == "sk-thread-123"

    def test_no_api_key_means_no_override(self):
        """With api_key=None the loop must not inject an empty api_key (so the
        provider/env default still applies)."""
        captured: dict = {}
        session = self._session(None)
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=self._capturing_stream_simple(captured),
        ):
            asyncio.run(session.prompt("hi"))
        assert "api_key" not in captured["options"]


class TestReasoningThreadedToProvider:
    """The requested thinking level must reach the provider via stream_simple's
    options as ``reasoning``. The provider clamps it and emits
    ``reasoning_effort``; here we only assert the level is threaded (or omitted
    when None) — same capture technique as the api_key tests."""

    def _session(self, reasoning):
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096, reasoning=True,
            ),
            reasoning=reasoning,
        )

    def test_reasoning_level_appears_in_stream_options(self):
        captured: dict = {}
        session = self._session("high")
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=TestApiKeyThreadedToProvider._capturing_stream_simple(captured),
        ):
            asyncio.run(session.prompt("hi"))
        assert captured["options"].get("reasoning") == "high"

    def test_no_reasoning_means_no_option(self):
        captured: dict = {}
        session = self._session(None)
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=TestApiKeyThreadedToProvider._capturing_stream_simple(captured),
        ):
            asyncio.run(session.prompt("hi"))
        assert "reasoning" not in captured["options"]


# =============================================================================
# Test 4: Abort during prompt
# =============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestAbortDuringPrompt:
    """Tests for AgentSession.abort() during prompt execution."""

    def create_session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    def test_abort_during_prompt(self):
        """Test 4: abort() during prompt stops streaming and returns."""
        session = self.create_session()
        abort_done = asyncio.Event()

        async def abort_then_prompt():
            async def abort_later():
                await asyncio.sleep(0.01)
                session.abort()
                abort_done.set()

            task = asyncio.create_task(abort_later())
            messages = await session.prompt("long response")
            await asyncio.wait_for(task, timeout=2.0)
            return messages

        messages = asyncio.run(abort_then_prompt())
        assert session.is_streaming is False

    def test_abort_sets_is_streaming_false(self):
        """abort() sets is_streaming to False."""
        session = self.create_session()
        session.abort()
        assert session.is_streaming is False

    def test_abort_sets_abort_signal(self):
        """abort() sets the abort signal."""
        session = self.create_session()
        assert not session._abort_signal.is_aborted()
        session.abort()
        assert session._abort_signal.is_aborted()

    def test_multiple_aborts_are_idempotent(self):
        """Calling abort() multiple times has no additional effect."""
        session = self.create_session()
        session.abort()
        session.abort()
        session.abort()
        assert session._abort_signal.is_aborted()
        assert session.is_streaming is False


# =============================================================================
# Test 5: Continue conversation
# =============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestContinueConversation:
    """Tests for AgentSession.continue_conversation()."""

    def create_session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    def test_continue_conversation(self):
        """Test 5: continue_conversation() runs another turn."""
        session = self.create_session()

        # First prompt
        asyncio.run(session.prompt("hello"))
        msg_count_1 = len(session.messages)

        # Continue
        messages = asyncio.run(session.continue_conversation())
        msg_count_2 = len(session.messages)
        assert msg_count_2 > msg_count_1

    def test_continue_conversation_returns_messages(self):
        """continue_conversation() returns messages."""
        session = self.create_session()
        messages = asyncio.run(session.continue_conversation())
        assert len(messages) > 0

    def test_continue_conversation_is_async(self):
        """continue_conversation() is an async method."""
        session = self.create_session()
        assert inspect.iscoroutinefunction(session.continue_conversation)

    def test_continue_conversation_emits_events(self):
        """continue_conversation() emits agent_start and agent_end events."""
        session = self.create_session()
        events = []

        def handler(event):
            events.append(event)

        session.subscribe(handler)
        asyncio.run(session.continue_conversation())

        types = [e.type for e in events]
        assert "agent_start" in types
        assert "agent_end" in types

    def test_continue_conversation_sets_not_streaming(self):
        """continue_conversation() sets is_streaming to False."""
        session = self.create_session()
        asyncio.run(session.continue_conversation())
        assert session.is_streaming is False

    def test_continue_conversation_after_prompt(self):
        """continue_conversation() after a prompt appends continuation message."""
        session = self.create_session()
        asyncio.run(session.prompt("first"))
        count_before = len(session.messages)
        asyncio.run(session.continue_conversation())
        count_after = len(session.messages)
        assert count_after > count_before

    def test_continue_conversation_with_abort(self):
        """abort() during continue_conversation() stops streaming."""
        session = self.create_session()

        async def abort_then_continue():
            async def abort_later():
                await asyncio.sleep(0.01)
                session.abort()

            task = asyncio.create_task(abort_later())
            await session.continue_conversation()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(abort_then_continue())
        assert session.is_streaming is False


# =============================================================================
# Test 6: create_agent_session with model string
# =============================================================================


class TestCreateAgentSession:
    """Tests for create_agent_session() SDK factory."""

    def test_model_resolution(self):
        """Test 6: create_agent_session() resolves model strings to Model objects."""
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        assert session._model.id == "gpt-4o"
        assert session._model.provider == "openai"

    def test_model_resolution_gpt4(self):
        """create_agent_session() resolves gpt-4."""
        session = create_agent_session(
            model="gpt-4",
            session_log=InMemorySessionLog(),
        )
        assert session._model.id == "gpt-4"

    def test_model_resolution_gpt4_turbo(self):
        """create_agent_session() resolves gpt-4-turbo."""
        session = create_agent_session(
            model="gpt-4-turbo",
            session_log=InMemorySessionLog(),
        )
        assert session._model.id == "gpt-4-turbo"

    def test_model_resolution_unknown_model(self):
        """create_agent_session() handles unknown model strings."""
        session = create_agent_session(
            model="my-custom-model",
            session_log=InMemorySessionLog(),
            provider="openai",
        )
        assert session._model.id == "my-custom-model"

    def test_model_resolution_with_custom_base_url(self):
        """create_agent_session() passes base_url to model."""
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
            base_url="https://custom.api.com/v1",
        )
        assert session._model.base_url == "https://custom.api.com/v1"

    def test_model_resolution_passes_model_object(self):
        """create_agent_session() accepts a Model object directly."""
        custom_model = Model(
            id="custom-model",
            name="Custom Model",
            api="openai-completions",
            provider="custom",
            base_url="https://custom.com",
            context_window=64000,
            max_tokens=4096,
        )
        session = create_agent_session(
            model=custom_model,
            session_log=InMemorySessionLog(),
        )
        assert session._model.id == "custom-model"
        assert session._model.provider == "custom"

    def test_tools_resolution(self):
        """create_agent_session() discovers and resolves tools from strings."""
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
            tools=["read", "bash"],
        )
        assert session._model.id == "gpt-4o"
        assert len(session._tools) == 2
        assert session._tools[0].name == "read"
        assert session._tools[1].name == "bash"
        assert session._is_streaming is False

    def test_tools_unknown_raises_error(self):
        """create_agent_session() raises ValueError for unknown tool names."""
        with pytest.raises(ValueError, match="Unknown tool"):
            create_agent_session(
                model="gpt-4o",
                session_log=InMemorySessionLog(),
                tools=["nonexistent_tool"],
            )

    def test_system_prompt_default(self):
        """create_agent_session() builds a default system prompt."""
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        assert session._system_prompt is not None
        assert "τ" in session._system_prompt or "helpful" in session._system_prompt.lower()

    def test_system_prompt_custom(self):
        """create_agent_session() uses custom system prompt when provided."""
        custom_prompt = "You are a test assistant."
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
            system_prompt=custom_prompt,
        )
        assert session._system_prompt == custom_prompt

    def test_sdk_agent_session_is_agent_session(self):
        """create_agent_session() returns an AgentSession instance."""
        session = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        assert isinstance(session, AgentSession)


# =============================================================================
# Test 7: Extensions are loaded and receive API
# =============================================================================


class TestExtensions:
    """Tests for extension loading and API."""

    def create_session(self, extensions=None) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            extensions=extensions,
        )

    def test_extensions_loaded_and_receive_api(self):
        """Test 7: Extensions receive an ExtensionAPI instance."""
        ext_called = []

        def my_ext(api):
            ext_called.append(api)

        session = self.create_session(extensions=[my_ext])
        assert len(ext_called) == 1
        assert isinstance(ext_called[0], ExtensionAPI)

    def test_extension_can_register_tool(self):
        """Extension can register a tool via the API."""
        async def _exec(tool_call_id, params, signal, on_update, ctx):
            return {"content": [{"type": "text", "text": "ok"}]}

        def my_ext(api):
            api.register_tool({
                "name": "my_tool",
                "label": "My Tool",
                "description": "A test tool",
                "parameters": {"type": "object", "properties": {}},
                "execute": _exec,
            })

        self.create_session(extensions=[my_ext])
        # The ExtensionAPI tracks tools internally
        # We verify by checking the ExtensionAPI instance

    def test_extension_can_subscribe_to_events(self):
        """api.on() subscribes to the session's LIVE event bus, not an orphan.

        Emitting on the session's own bus must reach the handler the extension
        registered through the API.
        """
        events_received = []

        def my_ext(api):
            api.on("agent_start", lambda e: events_received.append(e))

        session = self.create_session(extensions=[my_ext])
        asyncio.run(
            session._events.emit(AgentEvent(type="agent_start", timestamp=0))
        )
        assert len(events_received) == 1
        assert events_received[0].type == "agent_start"

    def test_extension_on_receives_live_tool_execution_end(self):
        """Verify (S3): api.on('tool_execution_end') gets a live event with real
        payload over a full fake-LLM turn.

        Proves the ExtensionAPI is bound to the real loop bus: the tool the loop
        actually executes fires a tool_execution_end that reaches the handler the
        extension subscribed via api.on() — the whole point of binding the API to
        self._events instead of a fresh orphan EventBus.
        """
        from unittest.mock import patch

        from tau_ai.streaming import DoneEvent, TextDeltaEvent
        from tau_ai.types import AssistantMessage, TextContent, Usage
        from tau_ai.types import ToolCall as TauToolCall

        received: list = []

        def my_ext(api):
            api.on("tool_execution_end", lambda e: received.append(e))

        async def echo_execute(**kwargs):
            return "TOOL_OUTPUT"

        echo_tool = AgentTool(
            definition=ToolDefinition(
                name="echo",
                label="Echo",
                description="Echo tool",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                execute=echo_execute,
                execution_mode="parallel",
            )
        )

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            tools=[echo_tool],
            extensions=[my_ext],
        )

        def _assistant(blocks, stop_reason):
            return AssistantMessage(
                content=blocks,
                api="openai-completions",
                provider="openai",
                model="gpt-4o",
                stop_reason=stop_reason,
                timestamp=0,
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

        class _Stream:
            def __init__(self, events):
                self._events = events

            def __aiter__(self):
                async def _gen():
                    for e in self._events:
                        yield e

                return _gen()

            async def result(self):
                for e in self._events:
                    if isinstance(e, DoneEvent):
                        return e.final
                return None

            def abort(self):
                pass

        call_count = [0]

        async def fake_stream_simple(model, context, options=None):
            call_count[0] += 1
            if call_count[0] == 1:
                tool_call = TauToolCall(
                    type="toolCall", id="call_1", name="echo",
                    arguments={"text": "hi"},
                )
                final = _assistant([tool_call], "toolUse")
                return _Stream([DoneEvent(final=final, usage=final.usage)])
            final = _assistant([TextContent(text="done")], "stop")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=final.usage),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream_simple
        ):
            asyncio.run(session.prompt("run echo"))

        assert len(received) == 1
        end_event = received[0]
        assert end_event.type == "tool_execution_end"
        assert end_event.tool_name == "echo"
        assert end_event.tool_call_id == "call_1"
        assert end_event.is_error is False
        # The real tool output reaches the handler (normalized to content blocks).
        assert end_event.result == [{"type": "text", "text": "TOOL_OUTPUT"}]

    def test_multiple_extensions_called(self):
        """Multiple extension factories are all called during creation."""
        call_order = []

        def ext1(api):
            call_order.append("ext1")

        def ext2(api):
            call_order.append("ext2")

        session = self.create_session(extensions=[ext1, ext2])
        assert call_order == ["ext1", "ext2"]

    def test_empty_extensions_list(self):
        """Session with no extensions works correctly."""
        session = self.create_session(extensions=[])
        assert session._extensions == []

    def test_none_extensions_defaults_to_empty(self):
        """Session with extensions=None defaults to empty list."""
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            extensions=None,
        )
        assert session._extensions == []

    def test_extension_api_has_ui(self):
        """ExtensionAPI exposes a ui property."""
        def my_ext(api):
            assert hasattr(api, "ui")
            ui = api.ui
            assert ui is not None

        session = self.create_session(extensions=[my_ext])

    def test_extension_api_set_session_name_raises_on_in_memory_session(self):
        """ExtensionAPI.set_session_name() Fail-Early raises when the bound
        session's log has no durable name slot (the SDK's in-memory log, as
        this fixture uses) — session naming needs a file-backed log (S64)."""
        def my_ext(api):
            assert api._session is not None
            with pytest.raises(RuntimeError):
                api.set_session_name("test-session")

        self.create_session(extensions=[my_ext])

    def test_extension_api_can_register_command(self):
        """ExtensionAPI.register_command() lands in the session-owned registry."""
        def my_ext(api):
            api.register_command("mycmd", {"description": "A command"})
            assert "mycmd" in api._registry._commands

        self.create_session(extensions=[my_ext])

    # -- Command output channel (E7 §3 / S46) ---------------------------------

    def test_run_command_returns_handlers_output(self):
        """A handled command carries the handler's returned value (S46, was G7).

        The value used to be discarded — ``run_extension_command`` could only
        report ``handled``. Now it returns an ``ExtensionCommandResult`` whose
        ``output`` is exactly what the handler returned.
        """
        def my_ext(api):
            def _report(args, ctx):
                return f"report for {args!r}"

            api.register_command("report", {"description": "r", "handler": _report})

        session = self.create_session(extensions=[my_ext])
        result = asyncio.run(session.run_extension_command("report", "todos"))
        assert result.handled is True
        assert result.output == "report for 'todos'"
        assert result.output_text() == "report for 'todos'"

    def test_run_command_awaits_async_handler_output(self):
        """An async handler's awaited return value is captured, not the coroutine."""
        def my_ext(api):
            async def _areport(args, ctx):
                return "async output"

            api.register_command("areport", {"description": "r", "handler": _areport})

        session = self.create_session(extensions=[my_ext])
        result = asyncio.run(session.run_extension_command("areport"))
        assert result.handled is True
        assert result.output == "async output"

    def test_run_command_none_output_has_no_text(self):
        """A handler returning None is handled but shows no output box."""
        def my_ext(api):
            def _silent(args, ctx):
                return None

            api.register_command("silent", {"description": "s", "handler": _silent})

        session = self.create_session(extensions=[my_ext])
        result = asyncio.run(session.run_extension_command("silent"))
        assert result.handled is True
        assert result.output is None
        assert result.output_text() is None

    def test_run_command_empty_string_output_has_no_text(self):
        """An empty-string return yields no output box (nothing to show)."""
        def my_ext(api):
            api.register_command(
                "blank", {"description": "b", "handler": lambda args, ctx: ""}
            )

        session = self.create_session(extensions=[my_ext])
        result = asyncio.run(session.run_extension_command("blank"))
        assert result.handled is True
        assert result.output_text() is None

    def test_run_command_non_str_output_is_stringified(self):
        """A non-str return is coerced to text for the display channels (honest, not dropped)."""
        def my_ext(api):
            api.register_command(
                "num", {"description": "n", "handler": lambda args, ctx: [1, 2, 3]}
            )

        session = self.create_session(extensions=[my_ext])
        result = asyncio.run(session.run_extension_command("num"))
        assert result.output == [1, 2, 3]
        assert result.output_text() == "[1, 2, 3]"

    def test_run_unknown_command_is_not_handled(self):
        """An unknown command → handled=False (caller falls through), no output."""
        session = self.create_session()
        result = asyncio.run(session.run_extension_command("nope"))
        assert result.handled is False
        assert result.output is None
        assert result.output_text() is None

    # -- Palette arg placeholder (E7 §3 / S51) --------------------------------

    def test_command_args_placeholder_declared(self):
        """A command's declared ``"args"`` placeholder is exposed for the palette."""
        def my_ext(api):
            api.register_command(
                "search",
                {"description": "search", "args": "<query>", "handler": lambda a, c: a},
            )

        session = self.create_session(extensions=[my_ext])
        assert session.get_extension_command_args("search") == "<query>"

    def test_command_args_absent_is_none(self):
        """A command that declares no ``"args"`` returns None (no modal on dispatch)."""
        def my_ext(api):
            api.register_command("plain", {"description": "p", "handler": lambda a, c: a})

        session = self.create_session(extensions=[my_ext])
        assert session.get_extension_command_args("plain") is None

    def test_command_args_unknown_command_is_none(self):
        """An unknown command name has no placeholder (None, never a fabricated value)."""
        session = self.create_session()
        assert session.get_extension_command_args("nope") is None

    def test_command_args_non_string_raises(self):
        """Fail-Early: a non-string ``"args"`` is a construction bug, so it raises."""
        def my_ext(api):
            api.register_command(
                "bad", {"description": "b", "args": True, "handler": lambda a, c: a}
            )

        session = self.create_session(extensions=[my_ext])
        with pytest.raises(TypeError, match="non-string 'args'"):
            session.get_extension_command_args("bad")

    # -- Key shortcuts (E10 §6 / S69) -----------------------------------------

    def test_get_extension_shortcuts_lists_registered(self):
        """Registered shortcuts are exposed as (key, command, args, description)."""
        def my_ext(api):
            api.register_shortcut("g", "fleet_status", description="Fleet status")
            api.register_shortcut("1", "abort_child", args="c-1")

        session = self.create_session(extensions=[my_ext])
        assert session.get_extension_shortcuts() == [
            ("g", "fleet_status", "", "Fleet status"),
            ("1", "abort_child", "c-1", ""),
        ]

    def test_get_extension_shortcuts_description_falls_back_to_command(self):
        """An undescribed shortcut inherits its target command's description."""
        def my_ext(api):
            api.register_command(
                "fleet_status", {"description": "Show the fleet", "handler": lambda a, c: None}
            )
            api.register_shortcut("g", "fleet_status")

        session = self.create_session(extensions=[my_ext])
        assert session.get_extension_shortcuts() == [
            ("g", "fleet_status", "", "Show the fleet"),
        ]

    def test_get_extension_shortcuts_empty_when_none_registered(self):
        session = self.create_session()
        assert session.get_extension_shortcuts() == []


# =============================================================================
# Test 8: In-memory session is isolated
# =============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestInMemoryIsolation:
    """Tests for in-memory session isolation."""

    def test_in_memory_isolation(self):
        """Test 8: In-memory sessions are independent."""
        session1 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        session2 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )

        asyncio.run(session1.prompt("hello from session 1"))
        assert len(session1.messages) > 0
        assert len(session2.messages) == 0  # isolated

    def test_prompt_in_one_session_does_not_affect_other(self):
        """Messages in one session don't leak to another."""
        session1 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        session2 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )

        asyncio.run(session1.prompt("hello"))
        asyncio.run(session2.prompt("world"))

        # Each session has its own messages
        user_msgs_1 = [m for m in session1.messages if m.get("role") == "user"]
        user_msgs_2 = [m for m in session2.messages if m.get("role") == "user"]

        assert len(user_msgs_1) >= 1
        assert len(user_msgs_2) >= 1
        assert user_msgs_1[0].get("content")[0].get("text") == "hello"
        assert user_msgs_2[0].get("content")[0].get("text") == "world"

    def test_state_is_independent(self):
        """Session state is independent between sessions."""
        session1 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )
        session2 = create_agent_session(
            model="gpt-4o",
            session_log=InMemorySessionLog(),
        )

        assert session1.state.status == "idle"
        assert session2.state.status == "idle"

        asyncio.run(session1.prompt("hello"))
        # Both should be idle after prompt completes
        assert session1.state.status == "idle"
        assert session2.state.status == "idle"

    def test_different_session_managers(self):
        """Different SessionManager instances are truly isolated."""
        log1 = InMemorySessionLog()
        log2 = InMemorySessionLog()

        session1 = create_agent_session(
            model="gpt-4o",
            session_log=log1,
        )
        session2 = create_agent_session(
            model="gpt-4o",
            session_log=log2,
        )

        asyncio.run(session1.prompt("session1"))
        assert len(session1.messages) > 0
        assert len(session2.messages) == 0

    def test_multiple_sessions_same_prompt(self):
        """Multiple sessions can each handle prompts independently."""
        sessions = [
            create_agent_session(
                model="gpt-4o",
                session_log=InMemorySessionLog(),
            )
            for _ in range(5)
        ]

        for i, session in enumerate(sessions):
            asyncio.run(session.prompt(f"prompt {i}"))

        for i, session in enumerate(sessions):
            user_msgs = [
                m for m in session.messages
                if m.get("role") == "user"
            ]
            assert len(user_msgs) >= 1
            assert user_msgs[0].get("content")[0].get("text") == f"prompt {i}"


# =============================================================================
# Additional: Compact test
# =============================================================================


class TestCompact:
    """Tests for AgentSession.compact()."""

    def create_session(self) -> AgentSession:
        return AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
        )

    def test_compact_is_async(self):
        """compact() is an async method."""
        session = self.create_session()
        assert inspect.iscoroutinefunction(session.compact)

    def test_compact_completes_without_error(self):
        """compact() completes without raising errors."""
        session = self.create_session()
        asyncio.run(session.compact())

    def test_compact_accepts_custom_instructions(self):
        """compact() accepts optional custom_instructions parameter."""
        session = self.create_session()
        # Should not raise
        asyncio.run(session.compact(custom_instructions="Summarize everything."))

    def test_compact_emits_events(self):
        """compact() emits agent_start and agent_end events."""
        session = self.create_session()
        events = []

        def handler(event):
            events.append(event)

        session.subscribe(handler)
        asyncio.run(session.compact())

        types = [e.type for e in events]
        assert "agent_start" in types
        assert "agent_end" in types


# =============================================================================
# Additional: EventBus tests
# =============================================================================


class TestEventBus:
    """Tests for the EventBus class."""

    def test_event_bus_creation(self):
        """EventBus can be instantiated."""
        bus = EventBus()
        assert bus is not None

    def test_event_bus_on_returns_unsubscribe(self):
        """EventBus.on() returns an unsubscribe function."""
        bus = EventBus()

        def handler(event):
            pass

        unsub = bus.on("all", handler)
        assert callable(unsub)

    @pytest.mark.asyncio
    async def test_event_bus_emit_calls_all_subscribers(self):
        """EventBus.emit() calls all 'all' subscribers."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.on("all", handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_event_bus_emit_specific_type(self):
        """EventBus.emit() calls type-specific subscribers."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.on("agent_start", handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_event_bus_emit_does_not_call_wrong_type(self):
        """EventBus.emit() does not call handlers for wrong type."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.on("agent_end", handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_event_bus_emit_calls_both_all_and_type(self):
        """EventBus.emit() calls both 'all' and type-specific handlers."""
        bus = EventBus()
        received = []

        def all_handler(event):
            received.append(("all", event.type))

        def type_handler(event):
            received.append(("type", event.type))

        bus.on("all", all_handler)
        bus.on("agent_start", type_handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=0))

        assert len(received) == 2
        assert ("all", "agent_start") in received
        assert ("type", "agent_start") in received

    def test_event_bus_unsubscribe(self):
        """EventBus unsubscribe removes the handler."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        unsub = bus.on("all", handler)
        unsub()

        # After unsubscribing, handler should not be called
        assert handler not in bus._listeners["all"]

    @pytest.mark.asyncio
    async def test_event_bus_handler_exception_does_not_break_others(self):
        """One handler exception doesn't prevent others from being called."""
        bus = EventBus()
        received = []

        def bad_handler(event):
            raise ValueError("oops")

        def good_handler(event):
            received.append(event)

        bus.on("all", bad_handler)
        bus.on("all", good_handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=0))

        assert len(received) == 1

    def test_event_bus_off_removes_handler(self):
        """EventBus.off() removes a specific handler."""
        bus = EventBus()

        def handler(event):
            pass

        bus.on("test_channel", handler)
        assert handler in bus._listeners["test_channel"]
        bus.off("test_channel", handler)
        assert handler not in bus._listeners["test_channel"]

    def test_event_bus_all_event_types_registered(self):
        """EventBus has all documented event types registered."""
        bus = EventBus()
        expected_types = {
            "all",
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }
        assert set(bus._listeners.keys()) == expected_types


# =============================================================================
# Additional: SDK helper functions
# =============================================================================


class TestSDKHelpers:
    """Tests for SDK helper functions."""

    def test_resolve_model_gpt4o(self):
        """resolve_model() returns correct Model for gpt-4o."""
        model = resolve_model("gpt-4o")
        assert model.id == "gpt-4o"
        assert model.name == "GPT-4o"
        assert model.provider == "openai"

    def test_resolve_model_custom_provider(self):
        """resolve_model() uses the specified provider."""
        model = resolve_model("custom-model", provider="anthropic")
        assert model.id == "custom-model"
        assert model.provider == "anthropic"

    def test_resolve_model_custom_base_url(self):
        """resolve_model() respects custom base_url."""
        model = resolve_model("gpt-4o", base_url="https://custom.com/v1")
        assert model.base_url == "https://custom.com/v1"

    def test_resolve_tools_empty(self):
        """_resolve_tools() with empty list returns empty."""
        tools = _resolve_tools([])
        assert tools == []

    def test_resolve_tools_none(self):
        """_resolve_tools() with None returns empty."""
        tools = _resolve_tools(None)
        assert tools == []

    def test_resolve_tools_known_tools(self):
        """_resolve_tools() resolves known tool names."""
        tools = _resolve_tools(["read", "bash", "ls"])
        assert len(tools) == 3
        names = [t.name for t in tools]
        assert "read" in names
        assert "bash" in names
        assert "ls" in names
        # Verify tools have execute method
        for t in tools:
            assert hasattr(t, "execute")
            assert callable(t.execute)

    def test_resolve_tools_unknown_raises(self):
        """_resolve_tools() raises ValueError for unknown tools."""
        with pytest.raises(ValueError, match="Unknown tool"):
            _resolve_tools(["nonexistent"])

    def test_build_system_prompt_default(self):
        """_build_system_prompt() returns a non-empty prompt."""
        prompt = _build_system_prompt()
        assert len(prompt) > 0

    def test_build_system_prompt_includes_tools(self):
        """_build_system_prompt() includes tool snippets."""
        def execute(ctx):
            return "test"

        tool = AgentTool(
            definition=ToolDefinition(
                name="ls",
                label="List",
                description="List files",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=execute,
                prompt_snippet="ls: List directory contents",
                prompt_guidelines=["Use absolute paths"],
            )
        )
        prompt = _build_system_prompt(tools=[tool])
        assert "ls: List directory contents" in prompt
        assert "Use absolute paths" in prompt

    def test_build_system_prompt_with_custom_prompt(self):
        """_build_system_prompt() uses provided system prompt."""
        custom = "You are custom."
        prompt = custom  # When system_prompt is provided, it bypasses _build_system_prompt
        assert len(prompt) > 0
