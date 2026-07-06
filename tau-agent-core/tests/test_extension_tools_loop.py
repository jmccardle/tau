"""S4 — registered extension tools reach the agent loop.

Verifies the E1.2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E1.2, step S4):

- a tool registered via ``api.register_tool(...)`` (pi ``ToolDefinition`` shape,
  ``execute(tool_call_id, params, signal, on_update, ctx)``) is resolved into an
  ``AgentTool`` and merged into the per-turn tools list, so the LLM can call it
  and it executes for real through ``AgentLoop``;
- because the loop is rebuilt every ``prompt()``, a SECOND ``register_tool`` made
  mid-session is live on the very next turn.

These run the FULL loop; only the network boundary (``stream_simple``) is faked,
and the fake emits a real tool call so the registered tool actually executes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    """Minimal async stream matching the stream_simple contract."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    """True if the context already holds a toolResult for ``tool_name``.

    Keyed on the specific tool so a prior turn's toolResult (from another tool,
    still in the session history) does not short-circuit this turn.
    """
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    """Fake stream_simple: emit one tool call, then a text stop once the tool ran.

    The first LLM call (no toolResult yet in the context) returns a tool call to
    ``tool_name``; after the loop appends the toolResult, the next call returns a
    plain text stop so the loop terminates.
    """

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _make_session(*extensions) -> AgentSession:
    from tau_ai.types import Model

    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        extensions=list(extensions),
    )


async def test_registered_tool_executes_through_loop() -> None:
    """A tool registered by an extension is callable and runs through the loop."""
    calls: list[dict[str, Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        # pi ToolDefinition.execute signature: params carries the LLM args, ctx is
        # the bound ExtensionContext.
        assert ctx is not None
        calls.append(params)
        return {"content": [{"type": "text", "text": "probed:" + params.get("q", "")}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "fake_probe",
                "description": "record the query and echo it",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": [],
                },
                "execute": probe_execute,
            }
        )

    session = _make_session(ext)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("fake_probe", {"q": "hi"}),
    ):
        messages = await session.prompt("call the probe")

    # The extension tool actually executed with the LLM-supplied args.
    assert calls == [{"q": "hi"}]

    # Its result reached the transcript as a toolResult message.
    tool_results = [
        m
        for m in messages
        if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "toolResult"
    ]
    assert len(tool_results) == 1
    text = tool_results[0]["content"][0]["text"]
    assert text == "probed:hi"


async def test_second_register_tool_is_live_next_turn() -> None:
    """A register_tool made after the first turn is callable on the next prompt."""
    first_calls: list[dict[str, Any]] = []
    second_calls: list[dict[str, Any]] = []
    captured_api: dict[str, Any] = {}

    async def first_execute(tool_call_id, params, signal, on_update, ctx):
        first_calls.append(params)
        return {"content": [{"type": "text", "text": "first"}]}

    async def second_execute(tool_call_id, params, signal, on_update, ctx):
        second_calls.append(params)
        return {"content": [{"type": "text", "text": "second"}]}

    def ext(api) -> None:
        captured_api["api"] = api
        api.register_tool(
            {
                "name": "first_tool",
                "description": "first",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": first_execute,
            }
        )

    session = _make_session(ext)

    # Turn 1: only first_tool exists; the LLM calls it.
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("first_tool", {}),
    ):
        await session.prompt("use the first tool")
    assert len(first_calls) == 1

    # Register a SECOND tool mid-session (after turn 1). The loop is rebuilt per
    # prompt(), so it must be live on the next turn without re-constructing anything.
    captured_api["api"].register_tool(
        {
            "name": "second_tool",
            "description": "second",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": second_execute,
        }
    )

    # Turn 2: the LLM calls the newly-registered tool.
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("second_tool", {}),
    ):
        await session.prompt("use the second tool")
    assert len(second_calls) == 1


async def test_get_context_usage_real_over_seeded_session() -> None:
    """S6: ctx.get_context_usage() returns real, non-zero pi ``ContextUsage``.

    Runs a real ``prompt()`` turn (only the network boundary faked) whose final
    assistant carries a real ``Usage``; the bound ``ExtensionContext`` then reads
    the SAME ``estimate_context_tokens`` that drives auto-compaction and returns
    ``{tokens, context_window, percent}`` — no more ``{"total_tokens": 0}`` stub.
    """

    async def _fake_text_with_usage(model, context, options=None):
        final = AssistantMessage(
            content=[TextContent(text="hello there")],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",
            timestamp=0,
            usage=Usage(input_tokens=500, output_tokens=100),
        )
        return _Stream(
            [
                TextDeltaEvent(delta="hello there", partial=final),
                DoneEvent(final=final, usage=Usage(input_tokens=500, output_tokens=100)),
            ]
        )

    session = _make_session()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_text_with_usage,
    ):
        await session.prompt("hi")

    usage = session._extension_api.context.get_context_usage()
    assert usage is not None
    # pi ContextUsage shape (snake_case): {tokens, context_window, percent}.
    assert set(usage) == {"tokens", "context_window", "percent"}
    assert usage["context_window"] == 128000
    # Real, non-zero: the persisted assistant usage anchors the estimate.
    assert usage["tokens"] > 0
    assert usage["percent"] == (usage["tokens"] / 128000) * 100
