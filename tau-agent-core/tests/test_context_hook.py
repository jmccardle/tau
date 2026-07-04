"""S14 — the ``context`` mutating hook (inject / replace before every LLM call).

Verifies the E2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E2, step S14) at the
``_stream_response`` seam (agent_loop.py, before the context dict is built):

- a handler returning ``{"messages": ...}`` REPLACES the message list that goes
  on the wire to the provider — so an injected ``<system-reminder>`` is visible
  on the payload actually sent (pi agent-loop.ts:283-285 ``transformContext``);
- the hook fires BEFORE EVERY LLM call, not once per turn: a turn that runs a
  tool re-enters ``_stream_response`` for the follow-up completion, and the
  reminder rides on BOTH wire payloads;
- the runner deep-copies (structuredClone-equivalent) before threading, and the
  hook operates on the conversation messages, leaving the separately-prepended
  system prompt intact.

These run the FULL loop; only the network boundary (``stream_simple``) is faked,
and the fake CAPTURES the ``messages`` it is handed so the test can assert on the
exact wire payload. The context handler is registered directly on the
session-owned ``ExtensionRunner`` (the wired dispatch surface for mutating hooks).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

REMINDER = "<system-reminder>stay in scope</system-reminder>"


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


def _message_text_blob(messages: list[Any]) -> str:
    """Flatten every text fragment of every message into one searchable string."""
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    out.append(str(block.get("text", "")))
                else:
                    out.append(str(getattr(block, "text", "")))
    return "\n".join(out)


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _make_session(*extensions, system_prompt: str = "") -> AgentSession:
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
        system_prompt=system_prompt,
    )


def _inject_reminder(event: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """A context handler that appends a ``<system-reminder>`` to the conversation."""
    messages = event["messages"]
    messages.append(
        {"role": "user", "content": [{"type": "text", "text": REMINDER}]}
    )
    return {"messages": messages}


async def test_injected_system_reminder_reaches_the_wire_payload() -> None:
    """A context handler's ``<system-reminder>`` is on the payload sent to the provider."""
    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        wire_payloads.append(list(context.get("messages", [])))
        final = _text_assistant("ok")
        return _Stream(
            [
                TextDeltaEvent(delta="ok", partial=final),
                DoneEvent(final=final, usage=Usage()),
            ]
        )

    session = _make_session()
    session._extension_runner.register_extension("mem:reminder").on("context", _inject_reminder)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("do the thing")

    # Exactly one LLM call, and the reminder rode on its wire payload.
    assert len(wire_payloads) == 1
    assert REMINDER in _message_text_blob(wire_payloads[0])


async def test_context_hook_fires_before_every_llm_call() -> None:
    """A tool round-trip makes two LLM calls; the reminder rides on BOTH payloads."""
    wire_payloads: list[list[Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "worker",
                "description": "a worker tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        if _has_tool_result(messages, "worker"):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", "worker", {})
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = _make_session(ext)
    session._extension_runner.register_extension("mem:reminder").on("context", _inject_reminder)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("use the worker")

    # Two provider round-trips (tool call, then follow-up), reminder on each.
    assert len(wire_payloads) == 2
    for payload in wire_payloads:
        assert REMINDER in _message_text_blob(payload)


async def test_no_context_handler_leaves_wire_payload_untouched() -> None:
    """Zero-extension fast path: no context handler → no reminder, no deep-copy work."""
    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        wire_payloads.append(list(context.get("messages", [])))
        final = _text_assistant("ok")
        return _Stream(
            [
                TextDeltaEvent(delta="ok", partial=final),
                DoneEvent(final=final, usage=Usage()),
            ]
        )

    session = _make_session()  # no extensions, no context handler

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("do the thing")

    assert len(wire_payloads) == 1
    assert REMINDER not in _message_text_blob(wire_payloads[0])


async def test_context_hook_preserves_prepended_system_prompt() -> None:
    """A handler replacing the whole list cannot clobber the separate system prompt."""
    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        wire_payloads.append(list(context.get("messages", [])))
        final = _text_assistant("ok")
        return _Stream(
            [
                TextDeltaEvent(delta="ok", partial=final),
                DoneEvent(final=final, usage=Usage()),
            ]
        )

    def replace_all(event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Wholesale replacement — the system prompt is NOT in event["messages"]
        # (pi keeps it separate), so it survives regardless.
        return {"messages": [{"role": "user", "content": [{"type": "text", "text": REMINDER}]}]}

    session = _make_session(system_prompt="SYSTEM RULES HERE")
    session._extension_runner.register_extension("mem:replace").on("context", replace_all)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("do the thing")

    payload = wire_payloads[0]
    first = payload[0]
    first_role = first.get("role") if isinstance(first, dict) else getattr(first, "role", None)
    # System prompt still leads the payload even after a full-list replacement.
    assert first_role == "system"
    assert "SYSTEM RULES HERE" in _message_text_blob([first])
    assert REMINDER in _message_text_blob(payload)
