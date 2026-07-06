"""S13 — the ``before_agent_start`` mutating hook (chain + accumulate).

Verifies the E2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E2, step S13) fired in
``AgentSession.prompt()`` just before ``loop.run()`` (pi agent-session.ts:1101-1125):

- ``system_prompt`` CHAINS across handlers — each handler sees the running value
  (last wins) — and the chained prompt reaches the provider for THIS turn;
- ``message``s ACCUMULATE across handlers and are injected as custom messages
  after the user turn, reading as ``user`` messages on the wire (pi messages.ts
  custom→user).

These run the FULL loop; only the network boundary (``stream_simple``) is faked,
and the fake captures the exact context dict the provider would receive so we can
assert the injected messages + chained system prompt on the wire. Handlers are
registered directly on the session-owned ``ExtensionRunner`` (the ``api.on`` ->
runner routing for mutating hooks lands in its own step; the runner is the wired
dispatch surface here), matching the S11/S12 hook tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


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


def _capturing_stream(captured: dict[str, Any]):
    """Fake stream_simple that records the context dict then answers with text."""

    async def fake(model, context, options=None):
        captured["context"] = context
        final = _text_assistant("done")
        return _Stream(
            [
                TextDeltaEvent(delta="done", partial=final),
                DoneEvent(final=final, usage=Usage()),
            ]
        )

    return fake


def _make_session(system_prompt: str = "") -> AgentSession:
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
        system_prompt=system_prompt,
    )


def _system_text(messages: list[Any]) -> str:
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content", ""))
    raise AssertionError("no system message on the wire")


def _user_texts(messages: list[Any]) -> list[str]:
    """Text of every user message on the wire (pydantic UserMessage or dict)."""
    out: list[str] = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text" and text is not None:
                out.append(text)
    return out


async def test_two_handlers_chain_system_prompt_and_accumulate_messages() -> None:
    """Two handlers chain the system prompt and each contribute one custom message."""
    session = _make_session(system_prompt="BASE")

    seen_by_second: dict[str, Any] = {}

    def first(event, ctx):
        return {
            "message": {"customType": "first", "content": "msg-A"},
            "system_prompt": event["system_prompt"] + "\nA",
        }

    def second(event, ctx):
        # The running system prompt is live to this later handler (chaining).
        seen_by_second["system_prompt"] = event["system_prompt"]
        return {
            "message": {"customType": "second", "content": "msg-B"},
            "system_prompt": event["system_prompt"] + "\nB",
        }

    # Load order: ext "first" before ext "second".
    session._extension_runner.register_extension("mem:first").on("before_agent_start", first)
    session._extension_runner.register_extension("mem:second").on("before_agent_start", second)

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        await session.prompt("hello")

    messages = captured["context"]["messages"]

    # system_prompt chained (last wins), and the second handler saw the first's
    # value live (BASE\nA), proving the running value threads forward.
    assert seen_by_second["system_prompt"] == "BASE\nA"
    assert _system_text(messages) == "BASE\nA\nB"

    # Both accumulated messages reached the model, in accumulation order, after
    # the user turn.
    user_texts = _user_texts(messages)
    assert user_texts == ["hello", "msg-A", "msg-B"]


async def test_no_before_agent_start_handlers_leaves_base_prompt_and_no_injection() -> None:
    """Zero-extension fast path: base system prompt, only the user message."""
    session = _make_session(system_prompt="BASE")

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        await session.prompt("hello")

    messages = captured["context"]["messages"]
    assert _system_text(messages) == "BASE"
    assert _user_texts(messages) == ["hello"]
