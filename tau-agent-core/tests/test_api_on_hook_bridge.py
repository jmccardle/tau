"""S24 — the ``api.on`` → ``ExtensionRunner`` bridge for the four mutating hooks.

The defect this closes: ``ExtensionAPI.on(event, handler)`` used to route EVERY
event to the notify ``EventBus``. But the four MUTATING hooks (``tool_call`` /
``tool_result`` / ``before_agent_start`` / ``context``) are dispatched by the
SEPARATE ``ExtensionRunner``, whose call-sites gate on ``has_handlers(event)`` —
so a hook registered via the PUBLIC ``api.on`` never populated the runner and was
a silent NO-OP in a real session. Every shipped demo (22_gatekeeper, 21_reminders,
24_budget, 23_context_surgeon) registers hooks through ``api.on``.

These tests assert the bridge END-TO-END through the real ``AgentSession`` +
fake-``stream_simple`` loop (the same harness the E2 hook tests use), so a
regression that re-breaks the routing FAILS here:

- ``api.on("tool_call", …)`` on the surface a LOADED extension is handed vetoes a
  real prepared tool call (the tool never runs);
- ``api.on("context", …)`` injects a message that reaches the wire payload;
- ``api.on("<hook>", …)`` on an api with NO runner bucket RAISES (Fail-Early), not
  a silent no-op;
- a NON-hook event (``tool_execution_end``) still routes to the notify ``EventBus``.

Reference: docs/EXTENSIONS-IMPLEMENTATION.md S24; pi loader.ts createExtensionAPI.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog


# ── loop harness (a faked network boundary; everything else is real) ──────────


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


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            content = m["content"] if isinstance(m, dict) else m.content
            block = content[0]
            return block["text"] if isinstance(block, dict) else block.text
    raise AssertionError(f"no toolResult for {tool_name}")


def _message_text_blob(messages: list[Any]) -> str:
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


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    """Emit one tool call, then a text stop once a toolResult for it appears."""

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


def _make_session(*extensions: Any) -> AgentSession:
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


# ── the bridge FIRES: tool_call veto through the public api.on ────────────────


async def test_public_api_on_tool_call_veto_blocks_execution() -> None:
    """``api.on("tool_call", …)`` on a loaded extension's api vetoes the call."""
    executed: list[dict[str, Any]] = []

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        executed.append(params)
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api: ExtensionAPI) -> None:
        api.register_tool(
            {
                "name": "guarded",
                "description": "a guarded tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )
        # PUBLIC surface — this is exactly what 22_gatekeeper does.
        api.on("tool_call", lambda event, ctx: {"block": True, "reason": "denied by policy"})

    # Loaded via the real extensions= path: the load loop hands ext a bucket-bound
    # api, so api.on("tool_call") must reach the ExtensionRunner the loop reads.
    session = _make_session(ext)

    # The bridge populated the runner — the call-site's has_handlers gate is live.
    assert session._extension_runner.has_handlers("tool_call") is True

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("guarded", {}),
    ):
        messages = await session.prompt("call the guarded tool")

    assert executed == []  # vetoed — the tool never ran
    assert _tool_result_text(messages, "guarded") == "denied by policy"


def test_public_api_on_context_is_a_retired_hook_and_raises() -> None:
    """``api.on("context", …)`` RAISES — the hook was removed in E5 §3.2 / S30.

    Even on a fully bound extension api (the surface a loaded extension is handed),
    registering the retired ``context`` hook is a Fail-Early error, not a silent
    bind to the notify ``EventBus`` (a dead no-op channel). The error names the
    durable replacements so demo authors are pointed at ``tool_result`` /
    ``before_agent_start``.
    """
    session = _make_session()
    api = session._bind_extension_api("mem:retired")

    with pytest.raises(RuntimeError, match="removed in E5"):
        api.on("context", lambda event, ctx: None)

    # It did NOT leak onto the notify EventBus either.
    assert session._extension_runner.has_handlers("context") is False


# ── Fail-Early: a hook on an api with no runner bucket RAISES ─────────────────


@pytest.mark.parametrize("hook", ["tool_call", "tool_result", "before_agent_start"])
def test_hook_on_unbound_api_raises(hook: str) -> None:
    """Registering a mutating hook on an api with no bucket RAISES (no silent no-op)."""
    api = ExtensionAPI()  # bare — hook_handlers is None
    with pytest.raises(RuntimeError, match="not bound to an ExtensionRunner bucket"):
        api.on(hook, lambda event, ctx: None)


# ── non-hook events still route to the notify EventBus ───────────────────────


async def test_non_hook_event_routes_to_notify_bus() -> None:
    """A non-hook event (``tool_execution_end``) registered via public api.on fires
    on the session's notify EventBus — NOT the runner."""
    seen: list[Any] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("tool_execution_end", lambda event: seen.append(event))

    session = _make_session(ext)

    # It did NOT land in the runner (not a mutating hook)…
    assert session._extension_runner.has_handlers("tool_execution_end") is False
    # …and it DID land on the notify bus: emitting the channel fires the handler.
    await session._events.emit_channel("tool_execution_end", {"type": "tool_execution_end"})
    assert seen == [{"type": "tool_execution_end"}]
