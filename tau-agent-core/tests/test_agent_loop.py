"""Tests for tau_agent_core.agent_loop — The core agent loop.

Tests verify the agent loop handles all 6 response types from
PHASE-2-SUBPHASE-1.md's "Done Criteria" and "Testing Strategy" sections:
1. Pure text response (no tool calls)
2. Single tool call (text -> tool call -> result -> text)
3. Multiple tool calls (sequential mode)
4. Multiple tool calls (parallel mode)
5. Multiple turns (LLM calls itself until done)
6. Early termination (one tool returns terminate=True)
7. Tool execution errors
8. Abort during tool execution
9. Abort during LLM streaming

Reference: PHASE-2-SUBPHASE-1.md, "Testing Strategy" section
Reference: SUBPHASE-0.0.md, "5. Agent Events" section
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tau_ai.abort import AbortSignal
from tau_ai.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_ai.types import (
    AssistantMessage,
    TextContent,
    ToolCall as TauToolCall,
    Usage,
    UserMessage,
)

from tau_agent_core.agent_loop import (
    AgentLoop,
    BlockedCall,
    ErrorCall,
)
from tau_agent_core.agent_loop_types import (
    AgentLoopConfig,
    PreparedToolCall,
)
from tau_agent_core.events import AgentEvent
from tau_agent_core.tools.base import (
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
    ToolDefinition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def async_emit(events: list, e: AgentEvent) -> None:
    """Async emit callback that appends to a list."""
    events.append(e)


def _make_text_assistant(content: str, **kwargs) -> AssistantMessage:
    """Create an AssistantMessage with only text content."""
    return AssistantMessage(
        content=[TextContent(text=content)],
        api="openai-completions",
        provider="openai",
        model=kwargs.get("model", "gpt-4o"),
        stop_reason=kwargs.get("stop_reason", "stop"),
        timestamp=int(time.time() * 1000),
        usage=kwargs.get("usage", Usage()),
    )


def _make_tool_call_assistant(
    tool_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    text_before: str = "",
    **kwargs,
) -> AssistantMessage:
    """Create an AssistantMessage containing a tool call."""
    blocks: list[Any] = []
    if text_before:
        blocks.append(TextContent(text=text_before))
    blocks.append(
        TauToolCall(
            type="toolCall",
            id=tool_id,
            name=tool_name,
            arguments=tool_args,
        )
    )
    return AssistantMessage(
        content=blocks,
        api="openai-completions",
        provider="openai",
        model=kwargs.get("model", "gpt-4o"),
        stop_reason=kwargs.get("stop_reason", "toolUse"),
        timestamp=int(time.time() * 1000),
        usage=kwargs.get("usage", Usage()),
    )


def _make_simple_tool(
    name: str,
    result: Any,
    delay: float = 0.01,
    **execute_kwargs,
) -> AgentTool:
    """Create a simple AgentTool for testing."""

    async def execute_impl(**kw):
        await asyncio.sleep(delay)
        return result

    tool_def = ToolDefinition(
        name=name,
        label=name.capitalize(),
        description=f"The {name} tool",
        parameters={
            "type": "object",
            "properties": {k: {"type": "string"} for k in execute_kwargs},
            "required": list(execute_kwargs.keys()),
        },
        execute=execute_impl,
        execution_mode="parallel",
    )
    return AgentTool(definition=tool_def)


def _make_failing_tool(name: str, error_msg: str = "tool error") -> AgentTool:
    """Create a tool that always raises an exception."""

    async def failing_execute(**kw):
        raise Exception(error_msg)

    tool_def = ToolDefinition(
        name=name,
        label=name.capitalize(),
        description=f"The {name} tool",
        parameters={"type": "object", "properties": {}, "required": []},
        execute=failing_execute,
        execution_mode="parallel",
    )
    return AgentTool(definition=tool_def)


def _make_mock_stream(events: list) -> MagicMock:
    """Create a mock stream that yields the given events.

    Returns a MagicMock that is also an async iterable, yielding the
    given events when iterated via ``async for``.  The ``.result()``
    coroutine returns the final message from the last DoneEvent.
    """

    class _MockStream:
        def __init__(self, evts: list):
            self._events = evts

        def __aiter__(self):
            return _EventIterator(self._events)

        async def result(self):
            for e in self._events:
                if isinstance(e, DoneEvent):
                    return e.final
            return None

    return _MockStream(events)


class _EventIterator:
    """Simple async iterator over a list of events."""

    def __init__(self, events: list):
        self._events = events
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


# ---------------------------------------------------------------------------
# Test 1: Text-only response
# ---------------------------------------------------------------------------


class TestTextOnlyResponse:
    """Test 1: Pure text response (no tool calls)."""

    @pytest.mark.asyncio
    async def test_text_only_response(self):
        """The agent loop returns text when the LLM produces no tool calls.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 1.
        """
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", system_prompt="test")
        loop = AgentLoop(config=config, emit=lambda e: async_emit(events, e))

        mock_stream = _make_mock_stream(
            [
                TextDeltaEvent(
                    delta="Hello",
                    partial=AssistantMessage(
                        content=[],
                        api="openai-completions",
                        provider="openai",
                        model="gpt-4o",
                        stop_reason="stop",
                        timestamp=0,
                    ),
                ),
                TextDeltaEvent(
                    delta=" world",
                    partial=AssistantMessage(
                        content=[TextContent(text="Hello")],
                        api="openai-completions",
                        provider="openai",
                        model="gpt-4o",
                        stop_reason="stop",
                        timestamp=0,
                    ),
                ),
                DoneEvent(
                    final=_make_text_assistant("Hello world"),
                    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
                ),
            ]
        )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", return_value=mock_stream
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(content=[TextContent(text="hi")], timestamp=0)
                ],
                context=[],
            )

        # Verify output
        assert len(messages) == 1  # One assistant message
        assert isinstance(messages[0], AssistantMessage)
        assert messages[0].role == "assistant"

        # Verify event sequence
        types = [e.type for e in events]
        assert "agent_start" in types
        assert "message_start" in types
        assert "message_update" in types
        assert "message_end" in types
        assert "turn_start" in types
        assert "turn_end" in types
        assert "agent_end" in types

        # Verify last event is agent_end
        assert events[-1].type == "agent_end"

    @pytest.mark.asyncio
    async def test_text_only_event_sequence(self):
        """Verify the exact event sequence for text-only response."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", system_prompt="test")
        loop = AgentLoop(config=config, emit=lambda e: async_emit(events, e))

        mock_stream = _make_mock_stream(
            [
                TextDeltaEvent(
                    delta="Hi",
                    partial=_make_text_assistant("", model="gpt-4o"),
                ),
                DoneEvent(
                    final=_make_text_assistant("Hi"),
                    usage=Usage(input_tokens=5, output_tokens=2, total_tokens=7),
                ),
            ]
        )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", return_value=mock_stream
        ):
            await loop.run(
                prompts=[
                    UserMessage(content=[TextContent(text="hello")], timestamp=0)
                ],
                context=[],
            )

        types = [e.type for e in events]
        # Check ordering: agent_start first, agent_end last
        assert types[0] == "agent_start"
        assert types[-1] == "agent_end"
        # message_start before message_end
        assert types.index("message_start") < types.index("message_end")
        # turn_start before turn_end
        assert types.index("turn_start") < types.index("turn_end")


# ---------------------------------------------------------------------------
# Test 2: Single tool call (sequential)
# ---------------------------------------------------------------------------


class TestSingleToolCallSequential:
    """Test 2: Single tool call (sequential)."""

    @pytest.mark.asyncio
    async def test_single_tool_call_sequential(self):
        """The agent loop executes a single tool call in sequential mode.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 2.
        """
        events: list[AgentEvent] = []
        config = AgentLoopConfig(
            model="gpt-4o",
            system_prompt="test",
            tool_execution_mode="sequential",
        )
        ls_tool = _make_simple_tool(
            name="bash",
            result="file1.txt\nfile2.py",
            command="ls .",
        )
        loop = AgentLoop(config=config, emit=lambda e: async_emit(events, e), tools=[ls_tool])

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_001", "bash", {"command": "ls ."}
                            ),
                            usage=Usage(input_tokens=50, output_tokens=20, total_tokens=70),
                        ),
                    ]
                )
            else:
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="Done: ",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        TextDeltaEvent(
                            delta="ls output received.",
                            partial=_make_text_assistant("Done: ", model="gpt-4o"),
                        ),
                        DoneEvent(
                            final=_make_text_assistant("Done: ls output received."),
                            usage=Usage(input_tokens=60, output_tokens=15, total_tokens=75),
                        ),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(content=[TextContent(text="run ls")], timestamp=0)
                ],
                context=[],
            )

        # Verify tool was called
        tool_starts = [e for e in events if e.type == "tool_execution_start"]
        tool_ends = [e for e in events if e.type == "tool_execution_end"]
        assert len(tool_starts) == 1
        assert len(tool_ends) == 1
        assert tool_ends[0].tool_name == "bash"

        # Verify messages: assistant response (tool call), tool result,
        # assistant response (text)
        assert len(messages) >= 2

    @pytest.mark.asyncio
    async def test_single_tool_call_sequential_events(self):
        """Verify tool execution events in the correct sequence."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", tool_execution_mode="sequential")
        ls_tool = _make_simple_tool(name="ls", result="file1.txt", path=".")
        loop = AgentLoop(config=config, emit=lambda e: async_emit(events, e), tools=[ls_tool])

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_abc", "ls", {"path": "."}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            else:
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="Done.",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        DoneEvent(final=_make_text_assistant("Done."), usage=Usage()),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(content=[TextContent(text="ls")], timestamp=0)
                ],
                context=[],
            )

        types = [e.type for e in events]

        # Verify: turn_start -> tool_execution_start -> tool_execution_end
        # -> message_end -> turn_end -> agent_end
        turn_start_idx = types.index("turn_start")
        tool_start_idx = types.index("tool_execution_start")
        tool_end_idx = types.index("tool_execution_end")

        assert turn_start_idx < tool_start_idx < tool_end_idx
        assert events[tool_start_idx].tool_name == "ls"
        assert events[tool_start_idx].tool_call_id == "call_abc"


# ---------------------------------------------------------------------------
# Test 3: Multiple tool calls (parallel)
# ---------------------------------------------------------------------------


class TestParallelToolCalls:
    """Test 3: Multiple tool calls in parallel mode."""

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self):
        """Multiple tool calls are executed concurrently in parallel mode.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 3.
        """
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", tool_execution_mode="parallel")
        ls_tool = _make_simple_tool(name="ls", result="file1.txt", path=".")
        read_tool = _make_simple_tool(
            name="read_file",
            result="content of file",
            path="file1.txt",
        )
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool, read_tool],
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=AssistantMessage(
                                content=[
                                    TauToolCall(
                                        type="toolCall",
                                        id="call_001",
                                        name="ls",
                                        arguments={"path": "."},
                                    ),
                                    TauToolCall(
                                        type="toolCall",
                                        id="call_002",
                                        name="read_file",
                                        arguments={"path": "file1.txt"},
                                    ),
                                ],
                                api="openai-completions",
                                provider="openai",
                                model="gpt-4o",
                                stop_reason="toolUse",
                                timestamp=int(time.time() * 1000),
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            else:
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="Results gathered.",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        DoneEvent(
                            final=_make_text_assistant("Results gathered."),
                            usage=Usage(),
                        ),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="list and read")], timestamp=0
                    )
                ],
                context=[],
            )

        # Both tools should have been called
        tool_starts = [e for e in events if e.type == "tool_execution_start"]
        assert len(tool_starts) == 2  # both tools called

        tool_names = {e.tool_name for e in tool_starts}
        assert tool_names == {"ls", "read_file"}

        # In parallel mode, both starts happen before their respective ends
        tool_ends = [e for e in events if e.type == "tool_execution_end"]
        assert len(tool_ends) == 2


# ---------------------------------------------------------------------------
# Test 4: Early termination
# ---------------------------------------------------------------------------


class TestEarlyTermination:
    """Test 4: Early termination when a tool returns terminate=True."""

    @pytest.mark.asyncio
    async def test_early_termination(self):
        """The agent loop stops after a tool returns terminate=True.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 4.
        """
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")

        # Create a tool that signals termination
        class TerminateAgentToolResult(AgentToolResult):
            terminate: bool = True

        async def terminate_execute(**kw):
            return TerminateAgentToolResult(
                tool_name="terminate",
                content=[{"type": "text", "text": "Terminating..."}],
                terminate=True,
            )

        term_tool = AgentTool(
            definition=ToolDefinition(
                name="terminate",
                label="Terminate",
                description="Terminates the agent loop",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=terminate_execute,
                execution_mode="parallel",
            )
        )

        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e), tools=[term_tool]
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_term", "terminate", {}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            # Should NOT reach here — loop should terminate
            raise RuntimeError("Agent loop should not make a second LLM call")

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="terminate")], timestamp=0
                    )
                ],
                context=[],
            )

        # Only 1 LLM call should have been made
        assert call_count[0] == 1

        # Should have exactly 1 turn_end (before termination)
        turn_ends = [e for e in events if e.type == "turn_end"]
        assert len(turn_ends) == 1

    @pytest.mark.asyncio
    async def test_early_termination_stops_tool_execution(self):
        """Early termination stops further tool calls in a batch."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(
            model="gpt-4o",
            tool_execution_mode="sequential",
        )

        class TerminateAgentToolResult(AgentToolResult):
            terminate: bool = True

        async def terminate_execute(**kw):
            return TerminateAgentToolResult(
                tool_name="terminate",
                content=[{"type": "text", "text": "done"}],
                terminate=True,
            )

        term_tool = AgentTool(
            definition=ToolDefinition(
                name="terminate",
                label="Terminate",
                description="Terminate",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=terminate_execute,
                execution_mode="sequential",
            )
        )

        called = []

        async def never_called_execute(**kw):
            called.append(True)
            return "should not be called"

        never_tool = AgentTool(
            definition=ToolDefinition(
                name="never",
                label="Never",
                description="Should not be called",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=never_called_execute,
                execution_mode="sequential",
            )
        )

        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[term_tool, never_tool],
        )

        async def mock_stream_func(model, context, options):
            return _make_mock_stream(
                [
                    DoneEvent(
                        final=AssistantMessage(
                            content=[
                                TauToolCall(
                                    type="toolCall",
                                    id="call_001",
                                    name="terminate",
                                    arguments={},
                                ),
                                TauToolCall(
                                    type="toolCall",
                                    id="call_002",
                                    name="never",
                                    arguments={},
                                ),
                            ],
                            api="openai-completions",
                            provider="openai",
                            model="gpt-4o",
                            stop_reason="toolUse",
                            timestamp=int(time.time() * 1000),
                        ),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="terminate")], timestamp=0
                    )
                ],
                context=[],
            )

        assert len(called) == 0


# ---------------------------------------------------------------------------
# Test 5: Tool error handling
# ---------------------------------------------------------------------------


class TestToolErrorHandling:
    """Test 5: Tool execution errors are caught and reported."""

    @pytest.mark.asyncio
    async def test_tool_error(self):
        """Tool execution errors produce is_error=True events.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 5.
        """
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        fail_tool = _make_failing_tool("failing_tool", "simulated failure")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e), tools=[fail_tool]
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_err", "failing_tool", {}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            # After error, LLM recovers
            return _make_mock_stream(
                [
                    TextDeltaEvent(
                        delta="Error handled.",
                        partial=_make_text_assistant("", model="gpt-4o"),
                    ),
                    DoneEvent(
                        final=_make_text_assistant("Error handled."),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="fail")], timestamp=0
                    )
                ],
                context=[],
            )

        error_events = [
            e for e in events
            if e.type == "tool_execution_end" and e.is_error
        ]
        assert len(error_events) == 1
        assert error_events[0].tool_name == "failing_tool"
        assert "simulated failure" in str(error_events[0].result)

    @pytest.mark.asyncio
    async def test_tool_error_in_parallel_mode(self):
        """Tool errors in parallel mode are handled correctly."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(
            model="gpt-4o",
            tool_execution_mode="parallel",
        )
        good_tool = _make_simple_tool(name="good_tool", result="ok")
        bad_tool = _make_failing_tool("bad_tool", "boom")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[good_tool, bad_tool],
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=AssistantMessage(
                                content=[
                                    TauToolCall(
                                        type="toolCall",
                                        id="call_001",
                                        name="good_tool",
                                        arguments={},
                                    ),
                                    TauToolCall(
                                        type="toolCall",
                                        id="call_002",
                                        name="bad_tool",
                                        arguments={},
                                    ),
                                ],
                                api="openai-completions",
                                provider="openai",
                                model="gpt-4o",
                                stop_reason="toolUse",
                                timestamp=int(time.time() * 1000),
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            return _make_mock_stream(
                [
                    TextDeltaEvent(
                        delta="Recovery",
                        partial=_make_text_assistant("", model="gpt-4o"),
                    ),
                    DoneEvent(
                        final=_make_text_assistant("Recovery"), usage=Usage()
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="test")], timestamp=0
                    )
                ],
                context=[],
            )

        tool_ends = [e for e in events if e.type == "tool_execution_end"]
        assert len(tool_ends) == 2  # Both tools reported

        error_ends = [e for e in tool_ends if e.is_error]
        assert len(error_ends) == 1
        assert error_ends[0].tool_name == "bad_tool"


# ---------------------------------------------------------------------------
# Test 6: Abort during tool execution
# ---------------------------------------------------------------------------


class TestAbortDuringToolExecution:
    """Test 6: Abort signal stops tool execution."""

    @pytest.mark.asyncio
    async def test_abort_during_tool(self):
        """The agent loop respects abort_signal during tool execution.

        Reference: PHASE-2-SUBPHASE-1.md, Testing Strategy, Test 6.
        """
        signal = AbortSignal()
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")

        # Tool that checks abort signal
        async def slow_execute(**kw):
            if signal.is_aborted():
                return "aborted"
            await asyncio.sleep(0.05)
            return "done"

        slow_tool = AgentTool(
            definition=ToolDefinition(
                name="slow_tool",
                label="Slow Tool",
                description="A slow tool",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=slow_execute,
                execution_mode="parallel",
            )
        )

        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[slow_tool],
            abort_signal=signal,
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_slow", "slow_tool", {}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            raise RuntimeError("Should not make second LLM call after abort")

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            task = asyncio.create_task(
                loop.run(
                    prompts=[
                        UserMessage(
                            content=[TextContent(text="slow")], timestamp=0
                        )
                    ],
                    context=[],
                )
            )

            # Wait a bit for the tool to start
            await asyncio.sleep(0.05)
            signal.abort()

            # Wait for task to complete or cancel
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert signal.is_aborted()

    @pytest.mark.asyncio
    async def test_abort_during_streaming(self):
        """The agent loop respects abort_signal during LLM streaming."""
        signal = AbortSignal()
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            abort_signal=signal,
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="This is a long",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                    ]
                )
            raise RuntimeError("Should not make second LLM call")

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            task = asyncio.create_task(
                loop.run(
                    prompts=[
                        UserMessage(
                            content=[TextContent(text="hello")], timestamp=0
                        )
                    ],
                    context=[],
                )
            )

            # Abort while the stream is happening
            await asyncio.sleep(0.05)
            signal.abort()

            await asyncio.sleep(0.05)

        assert signal.is_aborted()


# ---------------------------------------------------------------------------
# Test 7: Multiple turns
# ---------------------------------------------------------------------------


class TestMultipleTurns:
    """Test 7: Multiple turns — the LLM calls itself until done."""

    @pytest.mark.asyncio
    async def test_multiple_turns(self):
        """The agent loop can make multiple LLM calls until done."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", max_turns=10)
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool],
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_001", "ls", {"path": "."}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            elif call_count[0] == 2:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_002", "ls", {"path": ".."}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            else:
                # Third call: text only, done
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="All done!",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        DoneEvent(
                            final=_make_text_assistant("All done!"),
                            usage=Usage(),
                        ),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="explore")], timestamp=0
                    )
                ],
                context=[],
            )

        # Should have made 3 LLM calls
        assert call_count[0] == 3

        # Should have 3 turn_starts
        turn_starts = [e for e in events if e.type == "turn_start"]
        assert len(turn_starts) == 3


# ---------------------------------------------------------------------------
# Test 8: AgentLoop.run_continue()
# ---------------------------------------------------------------------------


class TestRunContinue:
    """Test: AgentLoop.run_continue() works."""

    @pytest.mark.asyncio
    async def test_run_continue(self):
        """run_continue runs another turn with existing context."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e)
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            return _make_mock_stream(
                [
                    TextDeltaEvent(
                        delta=f"Turn {call_count[0]}",
                        partial=_make_text_assistant("", model="gpt-4o"),
                    ),
                    DoneEvent(
                        final=_make_text_assistant(f"Turn {call_count[0]}"),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            # First run
            messages1 = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="hello")], timestamp=0
                    )
                ],
                context=[],
            )
            assert len(messages1) == 1

            # run_continue
            messages2 = await loop.run_continue(context=messages1)
            assert len(messages2) == 1  # one new assistant message
            assert call_count[0] == 2  # second LLM call made


# ---------------------------------------------------------------------------
# Test 9: Token usage tracking
# ---------------------------------------------------------------------------


class TestTokenUsageTracking:
    """Test: Token usage is tracked and emitted."""

    @pytest.mark.asyncio
    async def test_token_usage_in_done_event(self):
        """Token usage from the LLM response is captured."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e)
        )

        usage = Usage(input_tokens=100, output_tokens=50, total_tokens=150)

        mock_stream = _make_mock_stream(
            [
                DoneEvent(
                    final=_make_text_assistant("Hello", usage=usage),
                    usage=usage,
                ),
            ]
        )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", return_value=mock_stream
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="hi")], timestamp=0
                    )
                ],
                context=[],
            )

        assert len(messages) == 1
        assert messages[0].usage.output_tokens == 50
        assert messages[0].usage.input_tokens == 100


# ---------------------------------------------------------------------------
# Test 10: Tool arguments validation
# ---------------------------------------------------------------------------


class TestToolArgumentValidation:
    """Test: Tool arguments are validated before execution."""

    @pytest.mark.asyncio
    async def test_invalid_tool_args_are_blocked(self):
        """Invalid tool arguments result in BlockedCall and error event."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")

        ls_tool = _make_simple_tool(
            name="ls",
            result="files",
            path=".",
        )

        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool],
        )

        async def mock_stream_func(model, context, options):
            # First call: invalid tool call (args missing required "path")
            # Subsequent calls: text response (loop terminates)
            call_count = getattr(mock_stream_func, "_count", 0)
            mock_stream_func._count = call_count + 1
            if call_count == 0:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_bad",
                                "ls",
                                {"wrong_arg": "value"},  # Missing required "path"
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            else:
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="Done.",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        DoneEvent(
                            final=_make_text_assistant("Done."),
                            usage=Usage(),
                        ),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="ls")], timestamp=0
                    )
                ],
                context=[],
            )

        # Should have a tool_execution_end with is_error=True
        error_ends = [
            e for e in events
            if e.type == "tool_execution_end" and e.is_error
        ]
        assert len(error_ends) == 1
        assert error_ends[0].tool_name == "ls"


# ---------------------------------------------------------------------------
# Test 11: _prepare_tool_call helper
# ---------------------------------------------------------------------------


class TestPrepareToolCall:
    """Tests for the _prepare_tool_call helper method."""

    @pytest.mark.asyncio
    async def test_prepare_valid_tool_call(self):
        """Valid tool calls are prepared successfully."""
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(config=config, tools=[ls_tool])

        tc = TauToolCall(
            type="toolCall",
            id="call_001",
            name="ls",
            arguments={"path": "."},
        )
        result = await loop._prepare_tool_call(tc)

        assert isinstance(result, PreparedToolCall)
        assert result.id == "call_001"
        assert result.name == "ls"
        assert result.arguments == {"path": "."}

    @pytest.mark.asyncio
    async def test_prepare_invalid_tool_call(self):
        """Invalid tool arguments result in BlockedCall."""
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(config=config, tools=[ls_tool])

        tc = TauToolCall(
            type="toolCall",
            id="call_bad",
            name="ls",
            arguments={"wrong_key": "value"},
        )
        result = await loop._prepare_tool_call(tc)

        assert isinstance(result, BlockedCall)
        assert result.call.name == "ls"

    @pytest.mark.asyncio
    async def test_prepare_unknown_tool(self):
        """Unknown tools don't fail during prepare (execution will fail)."""
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(config=config)

        tc = TauToolCall(
            type="toolCall",
            id="call_001",
            name="unknown_tool",
            arguments={"path": "."},
        )
        result = await loop._prepare_tool_call(tc)

        assert isinstance(result, PreparedToolCall)
        assert result.name == "unknown_tool"


# ---------------------------------------------------------------------------
# Test 12: _execute_tool helper
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """Tests for the _execute_tool helper method."""

    @pytest.mark.asyncio
    async def test_execute_known_tool(self):
        """Known tools are executed and return results."""
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="file1.txt", path=".")
        loop = AgentLoop(config=config, tools=[ls_tool])

        prepared = PreparedToolCall(
            id="call_001", name="ls", arguments={"path": "."}
        )
        result = await loop._execute_tool(prepared)

        assert isinstance(result, AgentToolResult)
        assert result.tool_name == "ls"
        assert not result.is_error
        assert "file1.txt" in str(result.content)

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """Unknown tools return an error result."""
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(config=config)

        prepared = PreparedToolCall(
            id="call_001", name="nonexistent", arguments={}
        )
        result = await loop._execute_tool(prepared)

        assert isinstance(result, AgentToolResult)
        assert result.is_error
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_failing_tool(self):
        """Tools that raise exceptions return error results."""
        config = AgentLoopConfig(model="gpt-4o")
        fail_tool = _make_failing_tool("bad_tool", "something broke")
        loop = AgentLoop(config=config, tools=[fail_tool])

        prepared = PreparedToolCall(
            id="call_001", name="bad_tool", arguments={}
        )
        result = await loop._execute_tool(prepared)

        assert isinstance(result, AgentToolResult)
        assert result.is_error
        assert result.error_message == "something broke"


# ---------------------------------------------------------------------------
# Test 13: Event emission completeness
# ---------------------------------------------------------------------------


class TestEventEmission:
    """Tests for complete event emission."""

    @pytest.mark.asyncio
    async def test_agent_start_emitted(self):
        """agent_start is always emitted first."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e)
        )

        mock_stream = _make_mock_stream(
            [
                DoneEvent(
                    final=_make_text_assistant("ok"),
                    usage=Usage(),
                ),
            ]
        )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", return_value=mock_stream
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="hi")], timestamp=0
                    )
                ],
                context=[],
            )

        assert events[0].type == "agent_start"

    @pytest.mark.asyncio
    async def test_agent_end_emitted(self):
        """agent_end is always emitted last."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e)
        )

        mock_stream = _make_mock_stream(
            [
                DoneEvent(
                    final=_make_text_assistant("ok"),
                    usage=Usage(),
                ),
            ]
        )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", return_value=mock_stream
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="hi")], timestamp=0
                    )
                ],
                context=[],
            )

        assert events[-1].type == "agent_end"
        assert events[-1].messages is not None

    @pytest.mark.asyncio
    async def test_turn_events_for_each_turn(self):
        """Each turn produces turn_start and turn_end."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool],
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                "call_001", "ls", {"path": "."}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            return _make_mock_stream(
                [
                    TextDeltaEvent(
                        delta="done",
                        partial=_make_text_assistant("", model="gpt-4o"),
                    ),
                    DoneEvent(final=_make_text_assistant("done"), usage=Usage()),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="go")], timestamp=0
                    )
                ],
                context=[],
            )

        turn_starts = [e for e in events if e.type == "turn_start"]
        turn_ends = [e for e in events if e.type == "turn_end"]
        assert len(turn_starts) == len(turn_ends) == 2


# ---------------------------------------------------------------------------
# Test 14: Max turns limit
# ---------------------------------------------------------------------------


class TestMaxTurnsLimit:
    """Test: Max turns limits the number of iterations."""

    @pytest.mark.asyncio
    async def test_max_turns_stops_loop(self):
        """The agent loop stops when max_turns is reached."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o", max_turns=2)
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool],
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            # Always return a tool call — the loop would run forever
            return _make_mock_stream(
                [
                    DoneEvent(
                        final=_make_tool_call_assistant(
                            f"call_{call_count[0]}", "ls", {"path": "."}
                        ),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="go")], timestamp=0
                    )
                ],
                context=[],
            )

        # Should stop at max_turns=2
        assert call_count[0] == 2

        turn_starts = [e for e in events if e.type == "turn_start"]
        assert len(turn_starts) == 2


# ---------------------------------------------------------------------------
# Test 15: Tool call ID tracking
# ---------------------------------------------------------------------------


class TestToolCallIdTracking:
    """Test: Tool call IDs are properly tracked through the pipeline."""

    @pytest.mark.asyncio
    async def test_tool_call_id_preserved(self):
        """Tool call IDs are preserved from LLM response through execution."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[ls_tool],
        )

        expected_id = "call_xyz789"
        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_mock_stream(
                    [
                        DoneEvent(
                            final=_make_tool_call_assistant(
                                expected_id, "ls", {"path": "."}
                            ),
                            usage=Usage(),
                        ),
                    ]
                )
            else:
                # After tool result, return text
                return _make_mock_stream(
                    [
                        TextDeltaEvent(
                            delta="Done.",
                            partial=_make_text_assistant("", model="gpt-4o"),
                        ),
                        DoneEvent(
                            final=_make_text_assistant("Done."),
                            usage=Usage(),
                        ),
                    ]
                )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="ls")], timestamp=0
                    )
                ],
                context=[],
            )

        # Check tool_execution_start has the correct tool_call_id
        start_events = [
            e for e in events if e.type == "tool_execution_start"
        ]
        assert len(start_events) == 1
        assert start_events[0].tool_call_id == expected_id

        # Check tool_execution_end has the same tool_call_id
        end_events = [
            e for e in events if e.type == "tool_execution_end"
        ]
        assert len(end_events) == 1
        assert end_events[0].tool_call_id == expected_id


# ---------------------------------------------------------------------------
# Test 16: _to_llm_tool conversion
# ---------------------------------------------------------------------------


class TestToLLMTool:
    """Tests for the _to_llm_tool helper method."""

    def test_convert_to_llm_tool(self):
        """AgentTool converts to OpenAI-format tool dict."""
        config = AgentLoopConfig(model="gpt-4o")
        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop = AgentLoop(config=config, tools=[ls_tool])

        result = loop._to_llm_tool(ls_tool)

        assert result["type"] == "function"
        assert result["function"]["name"] == "ls"
        assert result["function"]["description"] == "The ls tool"
        assert "path" in result["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Test 17: add_tool method
# ---------------------------------------------------------------------------


class TestAddTool:
    """Tests for the add_tool method."""

    def test_add_tool_to_empty_loop(self):
        """Tools can be added to a loop with no initial tools."""
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(config=config)

        ls_tool = _make_simple_tool(name="ls", result="files", path=".")
        loop.add_tool(ls_tool)

        assert "ls" in loop._tools
        assert loop._tools["ls"] is ls_tool

    def test_add_multiple_tools(self):
        """Multiple tools can be added."""
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(config=config)

        loop.add_tool(
            _make_simple_tool(name="ls", result="files", path=".")
        )
        loop.add_tool(
            _make_simple_tool(name="read", result="content", path=".")
        )

        assert "ls" in loop._tools
        assert "read" in loop._tools


# ---------------------------------------------------------------------------
# Test 18: BlockedCall and ErrorCall types
# ---------------------------------------------------------------------------


class TestBlockedCallAndErrorCall:
    """Tests for internal BlockedCall and ErrorCall types."""

    def test_blocked_call(self):
        """BlockedCall holds a call and error message."""
        call = PreparedToolCall(id="c1", name="ls", arguments={})
        blocked = BlockedCall(call, "Validation failed")
        assert blocked.call.id == "c1"
        assert blocked.error == "Validation failed"

    def test_error_call(self):
        """ErrorCall holds a call and error message."""
        call = PreparedToolCall(id="c2", name="ls", arguments={})
        error_call = ErrorCall(call, "Unexpected error")
        assert error_call.call.id == "c2"
        assert error_call.error == "Unexpected error"


# ---------------------------------------------------------------------------
# Test 19: Sequential mode stops on first terminate
# ---------------------------------------------------------------------------


class TestSequentialTermination:
    """Test: Sequential mode stops tool execution on terminate."""

    @pytest.mark.asyncio
    async def test_sequential_stops_on_terminate(self):
        """In sequential mode, tools after a terminate signal are skipped."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(
            model="gpt-4o",
            tool_execution_mode="sequential",
        )

        class TerminateAgentToolResult(AgentToolResult):
            terminate: bool = True

        async def term_execute(**kw):
            return TerminateAgentToolResult(
                tool_name="terminate",
                content=[{"type": "text", "text": "terminating"}],
                terminate=True,
            )

        term_tool = AgentTool(
            definition=ToolDefinition(
                name="terminate",
                label="Terminate",
                description="Terminate",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                execute=term_execute,
                execution_mode="sequential",
            )
        )

        # Create a tool that should NOT be called
        called = []

        async def never_called_execute(**kw):
            called.append(True)
            return "should not be called"

        never_tool = AgentTool(
            definition=ToolDefinition(
                name="never",
                label="Never",
                description="Should not be called",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                execute=never_called_execute,
                execution_mode="sequential",
            )
        )

        loop = AgentLoop(
            config=config,
            emit=lambda e: async_emit(events, e),
            tools=[term_tool, never_tool],
        )

        # LLM calls both tools — but sequential mode should stop after terminate
        async def mock_stream_func(model, context, options):
            return _make_mock_stream(
                [
                    DoneEvent(
                        final=AssistantMessage(
                            content=[
                                TauToolCall(
                                    type="toolCall",
                                    id="call_001",
                                    name="terminate",
                                    arguments={},
                                ),
                                TauToolCall(
                                    type="toolCall",
                                    id="call_002",
                                    name="never",
                                    arguments={},
                                ),
                            ],
                            api="openai-completions",
                            provider="openai",
                            model="gpt-4o",
                            stop_reason="toolUse",
                            timestamp=int(time.time() * 1000),
                        ),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="go")], timestamp=0
                    )
                ],
                context=[],
            )

        # The "never" tool should not have been called
        assert len(called) == 0


# ---------------------------------------------------------------------------
# Test 20: run_continue with existing context
# ---------------------------------------------------------------------------


class TestRunContinueContext:
    """Test: run_continue uses existing context correctly."""

    @pytest.mark.asyncio
    async def test_run_continue_accumulates_messages(self):
        """run_continue continues from existing message history."""
        events: list[AgentEvent] = []
        config = AgentLoopConfig(model="gpt-4o")
        loop = AgentLoop(
            config=config, emit=lambda e: async_emit(events, e)
        )

        call_count = [0]

        async def mock_stream_func(model, context, options):
            call_count[0] += 1
            return _make_mock_stream(
                [
                    DoneEvent(
                        final=_make_text_assistant(f"Turn {call_count[0]}"),
                        usage=Usage(),
                    ),
                ]
            )

        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func
        ):
            # Initial run
            messages = await loop.run(
                prompts=[
                    UserMessage(
                        content=[TextContent(text="first")], timestamp=0
                    )
                ],
                context=[],
            )
            assert len(messages) == 1

            # run_continue
            messages2 = await loop.run_continue(context=messages)
            assert len(messages2) == 1  # one new assistant message
            assert call_count[0] == 2
