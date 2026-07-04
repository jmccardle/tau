"""S10 — thread the extension hook dispatcher into the agent loop.

Verifies the E2 wiring (docs/EXTENSIONS-IMPLEMENTATION.md §E2 "How the loop
reaches the dispatcher", step S10):

- ``AgentSession`` owns ONE return-collecting ``ExtensionRunner`` and injects it
  into every ``AgentLoop`` it builds via ``hook_dispatcher=`` — the loop that
  actually runs a ``prompt()`` turn holds the session's runner as its
  ``_hook_dispatcher`` (reachable from the loop);
- the zero-extension fast path: ``loop.has_hook_handlers(event)`` is ``False``
  for every mutating hook when no extension registered one, and flips to
  ``True`` once a handler is registered on the (shared) runner;
- a standalone ``AgentLoop`` with no injected dispatcher reports no handlers
  rather than raising — the four hook call-sites (S11-S14) can safely gate on it.

The session tests run the FULL loop; only the network boundary (``stream_simple``)
is faked. S10 only threads the dispatcher in — the four call-sites land in
S11-S14 — so these assert reachability + the fast path, not hook side effects.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extensions.runner import ExtensionRunner
from tau_agent_core.session_log import InMemorySessionLog

HOOK_EVENTS = ("tool_call", "tool_result", "before_agent_start")


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


async def _fake_text(model, context, options=None) -> _Stream:
    final = AssistantMessage(
        content=[TextContent(text="ok")],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )
    return _Stream(
        [
            TextDeltaEvent(delta="ok", partial=final),
            DoneEvent(final=final, usage=Usage()),
        ]
    )


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


def _spy_loop(captured: dict[str, Any]) -> type[AgentLoop]:
    """An AgentLoop subclass that records the instance the session builds."""

    class _SpyLoop(AgentLoop):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured["loop"] = self
            captured["hook_dispatcher_kwarg"] = kwargs.get("hook_dispatcher")

    return _SpyLoop


async def test_dispatcher_reachable_from_loop_and_fast_path_no_extensions() -> None:
    """The loop that runs a turn holds the session's runner; no handlers → fast path."""
    session = _make_session()
    captured: dict[str, Any] = {}

    with (
        patch("tau_agent_core.agent_session.AgentLoop", _spy_loop(captured)),
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text),
    ):
        await session.prompt("hi")

    loop = captured["loop"]
    # The session injected its single runner into the loop it actually ran.
    assert isinstance(session._extension_runner, ExtensionRunner)
    assert captured["hook_dispatcher_kwarg"] is session._extension_runner
    assert loop._hook_dispatcher is session._extension_runner

    # Zero-extension fast path: no mutating-hook handlers anywhere.
    for event in HOOK_EVENTS:
        assert loop.has_hook_handlers(event) is False


async def test_has_hook_handlers_flips_true_when_runner_has_handler() -> None:
    """A handler on the session's (shared) runner is visible through the loop."""
    session = _make_session()
    # Register a mutating-hook handler directly on the session-owned runner (the
    # api.on -> runner routing lands in a later step; S10 only threads the runner
    # in). Because the runner is shared, the loop must observe it.
    session._extension_runner.register_extension("mem:probe").on(
        "tool_call", lambda event, ctx: None
    )

    captured: dict[str, Any] = {}
    with (
        patch("tau_agent_core.agent_session.AgentLoop", _spy_loop(captured)),
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text),
    ):
        await session.prompt("hi")

    loop = captured["loop"]
    assert loop._hook_dispatcher is session._extension_runner
    assert loop.has_hook_handlers("tool_call") is True
    # Only the registered event is hot; the others stay on the fast path.
    assert loop.has_hook_handlers("tool_result") is False
    assert loop.has_hook_handlers("before_agent_start") is False


def test_standalone_loop_without_dispatcher_reports_no_handlers() -> None:
    """A loop built with no injected dispatcher reports no handlers (never raises)."""
    loop = AgentLoop(config=AgentLoopConfig(system_prompt=""))
    assert loop._hook_dispatcher is None
    for event in HOOK_EVENTS:
        assert loop.has_hook_handlers(event) is False
