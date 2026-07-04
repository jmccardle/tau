"""S11 — the ``tool_call`` mutating hook (veto / in-place arg patch).

Verifies the E2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E2, step S11) at the
``_prepare_tool_call`` seam (agent_loop.py, at the ``PreparedToolCall`` return):

- a handler returning ``{"block": True, "reason": ...}`` short-circuits execution
  into an error tool result whose text is exactly ``reason`` (pi
  agent-loop.ts:597-602);
- a handler mutating ``event["input"]`` IN PLACE patches the args the tool
  actually executes with — with NO re-validation (pi parity, §7 decision E2-a);
- a THROWING handler is fail-CLOSED: it blocks execution (pi
  agent-session.ts:419-424) rather than letting the tool run unguarded.

These run the FULL loop; only the network boundary (``stream_simple``) is faked,
and the fake emits a real tool call so the hook fires on a real prepared call.
The tool_call handlers are registered directly on the session-owned
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
    """Fake stream_simple: emit one tool call, then a text stop once the tool ran.

    "Once the tool ran" is keyed on a toolResult for ``tool_name`` appearing in
    the context — which the loop appends both for a real execution AND for a
    blocked/error tool result. So a vetoed call still terminates the loop.
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


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            content = m["content"] if isinstance(m, dict) else m.content
            return content[0]["text"] if isinstance(content[0], dict) else content[0].text
    raise AssertionError(f"no toolResult for {tool_name}")


async def test_veto_blocks_execution_and_error_text_is_reason() -> None:
    """A ``{block: True, reason}`` handler stops the tool and yields reason text."""
    executed: list[dict[str, Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        executed.append(params)
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "guarded",
                "description": "a guarded tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )

    session = _make_session(ext)
    session._extension_runner.register_extension("mem:veto").on(
        "tool_call",
        lambda event, ctx: {"block": True, "reason": "denied by policy"},
    )

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("guarded", {}),
    ):
        messages = await session.prompt("call the guarded tool")

    # The tool never ran, and the toolResult text is exactly the reason.
    assert executed == []
    assert _tool_result_text(messages, "guarded") == "denied by policy"


async def test_in_place_arg_patch_reaches_the_tool() -> None:
    """Mutating ``event['input']`` patches the args the tool executes with."""
    executed: list[dict[str, Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        executed.append(params)
        return {"content": [{"type": "text", "text": "ok:" + params.get("q", "")}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "patchable",
                "description": "a patchable tool",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": [],
                },
                "execute": probe_execute,
            }
        )

    def patch_handler(event, ctx):
        # In-place mutation — no return value. The patched dict is what runs.
        event["input"]["q"] = "patched"
        return None

    session = _make_session(ext)
    session._extension_runner.register_extension("mem:patch").on("tool_call", patch_handler)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("patchable", {"q": "original"}),
    ):
        messages = await session.prompt("call the patchable tool")

    # The tool saw the mutated value, not the LLM's original argument.
    assert executed == [{"q": "patched"}]
    assert _tool_result_text(messages, "patchable") == "ok:patched"


async def test_throwing_tool_call_handler_blocks() -> None:
    """A tool_call handler that raises is fail-CLOSED: the tool does not run."""
    executed: list[dict[str, Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        executed.append(params)
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "risky",
                "description": "a risky tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )

    def boom(event, ctx):
        raise RuntimeError("handler exploded")

    session = _make_session(ext)
    session._extension_runner.register_extension("mem:boom").on("tool_call", boom)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("risky", {}),
    ):
        messages = await session.prompt("call the risky tool")

    # Fail-CLOSED: the tool never executed and the block surfaced as an error result.
    assert executed == []
    assert "handler exploded" in _tool_result_text(messages, "risky")
