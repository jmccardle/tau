"""A tool's ``details`` reaches a head instead of being computed and discarded.

Seven built-in tools set ``result_dict["details"]`` — a path, a line range, a
match count, a diff. Before this, ``_execute_tool`` read ``content``,
``is_error`` and ``terminate`` off that dict and nothing else, so the value died
at the loop boundary and ``_apply_after_hooks`` handed every ``tool_result``
handler a hard-coded ``None``.

The three places it has to arrive, one test each: the persisted ``toolResult``
message (what a reload and ``get_messages`` return), the
``tool_execution_end`` event (what a live head subscribes to), and the
``tool_result`` hook event (what an extension may read and patch).

Reference: docs/VSCODE-HEAD.md §6.1.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

DETAILS = {"path": "/tmp/x.py", "lines_read": 12, "truncated": False}


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(),
    )


def _tool_call_assistant(call_id: str, name: str) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments={})],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=_TS,
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
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str):
    """Fake stream_simple: emit one tool call, then a text stop once the tool ran."""

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
        final = _tool_call_assistant("call_1", tool_name)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _detail_ext(name: str, details: dict[str, Any] | None):
    """An extension registering a tool that returns the built-in tools' dict shape."""

    async def execute(tool_call_id, params, signal, on_update, ctx):
        result: dict[str, Any] = {"content": [{"type": "text", "text": "ok"}]}
        if details is not None:
            result["details"] = details
        return result

    def ext(api) -> None:
        api.register_tool(
            {
                "name": name,
                "description": "a tool that reports details",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": execute,
            }
        )

    return ext


def _make_session(*extensions) -> AgentSession:
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


def _tool_result(messages: list[Any], tool_name: str) -> Any:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return m
    raise AssertionError(f"no toolResult for {tool_name}")


def _details_of(messages: list[Any], tool_name: str) -> Any:
    m = _tool_result(messages, tool_name)
    return m["details"] if isinstance(m, dict) else m.details


async def test_details_reach_the_tool_result_message() -> None:
    """The dict a tool returns carries ``details`` onto the persisted message."""
    session = _make_session(_detail_ext("probed", DETAILS))

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed"),
    ):
        messages = await session.prompt("call the probed tool")

    assert _details_of(messages, "probed") == DETAILS


async def test_a_tool_declaring_no_details_reports_none() -> None:
    """No details is ``None``, not an empty dict — absent and empty differ."""
    session = _make_session(_detail_ext("bare", None))

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bare"),
    ):
        messages = await session.prompt("call the bare tool")

    assert _details_of(messages, "bare") is None


async def test_details_reach_the_tool_execution_end_event() -> None:
    """A live head subscribed to the bus sees the same value the message holds."""
    session = _make_session(_detail_ext("probed", DETAILS))
    seen: list[AgentEvent] = []

    def collect(event: AgentEvent) -> None:
        if event.type == "tool_execution_end":
            seen.append(event)

    session.subscribe(collect)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed"),
    ):
        await session.prompt("call the probed tool")

    assert [e.details for e in seen] == [DETAILS]


async def test_a_hook_reads_the_real_details_and_can_patch_them() -> None:
    """``tool_result`` handlers were handed a hard-coded ``None``; now they are not."""
    session = _make_session(_detail_ext("probed", DETAILS))
    observed: list[Any] = []

    def patcher(event, ctx):
        observed.append(event["details"])
        return {"details": {"path": "/tmp/rewritten.py"}}

    session._extension_runner.register_extension("mem:details").on("tool_result", patcher)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed"),
    ):
        messages = await session.prompt("call the probed tool")

    assert observed == [DETAILS]
    assert _details_of(messages, "probed") == {"path": "/tmp/rewritten.py"}
