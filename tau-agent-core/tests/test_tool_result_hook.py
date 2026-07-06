"""S12 — the ``tool_result`` mutating hook (field-patch the shared result event).

Verifies the E2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E2, step S12) at the
``_apply_after_hooks`` seam (agent_loop.py, called after every tool execution in
both the sequential and parallel paths):

- a handler returning ``{"content": ...}`` / ``{"is_error": ...}`` replaces those
  fields on the tool result the loop feeds back to the LLM (whole-value replace,
  no deep merge; pi runner.ts:826-837 + agent-loop.ts:697-701);
- the event is cloned once and field-patched across handlers, so a *second*
  handler sees the *first* handler's patch (later sees earlier);
- a handler that sets nothing passes the result through unchanged.

These run the FULL loop; only the network boundary (``stream_simple``) is faked,
and the fake emits a real tool call so the hook fires on a real executed result.
The tool_result handlers are registered directly on the session-owned
``ExtensionRunner`` (the ``api.on`` -> runner routing for mutating hooks lands in
its own step; the runner is the wired dispatch surface here).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

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
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
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
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


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


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    m = _tool_result(messages, tool_name)
    content = m["content"] if isinstance(m, dict) else m.content
    return content[0]["text"] if isinstance(content[0], dict) else content[0].text


def _tool_result_is_error(messages: list[Any], tool_name: str) -> bool:
    m = _tool_result(messages, tool_name)
    return bool(m["is_error"] if isinstance(m, dict) else m.is_error)


def _tool_ext(name: str):
    """An extension registering a tool that returns a fixed, non-error result."""

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "original"}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": name,
                "description": "a probed tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )

    return ext


async def test_patch_replaces_content_and_is_error() -> None:
    """A ``{content, is_error}`` handler replaces both fields on the tool result."""
    session = _make_session(_tool_ext("probed"))

    def patcher(event, ctx):
        assert event["type"] == "tool_result"
        # The event carries the tool's real content before any patch.
        assert event["content"] == [{"type": "text", "text": "original"}]
        return {
            "content": [{"type": "text", "text": "patched"}],
            "is_error": True,
        }

    session._extension_runner.register_extension("mem:patch").on("tool_result", patcher)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed", {}),
    ):
        messages = await session.prompt("call the probed tool")

    assert _tool_result_text(messages, "probed") == "patched"
    assert _tool_result_is_error(messages, "probed") is True


async def test_patch_chained_across_two_handlers() -> None:
    """The event is field-patched across handlers: the second sees the first's patch."""
    seen_by_second: list[Any] = []

    session = _make_session(_tool_ext("probed"))

    def first(event, ctx):
        return {"content": [{"type": "text", "text": "first"}]}

    def second(event, ctx):
        # The shared event was already patched by `first` — the later handler
        # sees the earlier handler's write.
        seen_by_second.append(event["content"])
        return {"content": [{"type": "text", "text": "second"}]}

    ext = session._extension_runner.register_extension("mem:chain")
    ext.on("tool_result", first)
    ext.on("tool_result", second)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed", {}),
    ):
        messages = await session.prompt("call the probed tool")

    # The second handler observed the first handler's patched content...
    assert seen_by_second == [[{"type": "text", "text": "first"}]]
    # ...and its own patch (last write) is what reaches the LLM.
    assert _tool_result_text(messages, "probed") == "second"


async def test_no_patch_passes_result_through_unchanged() -> None:
    """A handler that sets nothing leaves the tool result untouched."""
    session = _make_session(_tool_ext("probed"))

    def noop(event, ctx):
        return None

    session._extension_runner.register_extension("mem:noop").on("tool_result", noop)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("probed", {}),
    ):
        messages = await session.prompt("call the probed tool")

    assert _tool_result_text(messages, "probed") == "original"
    assert _tool_result_is_error(messages, "probed") is False
