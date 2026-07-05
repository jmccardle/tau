"""S42 (E6 §2) — the ``input`` mutating hook (pre-node transform / handled).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S42 (anchor G2).
pi source of truth: coding-agent/src/core/extensions/runner.ts (``emitInput``),
agent-session.ts (the prompt() call-site), examples/extensions/inline-bash.ts +
input-transform.ts.

The ``input`` hook fires at the TOP of ``AgentSession.prompt()`` — BEFORE the user
node exists — and either

* TRANSFORMS ``{prompt, images}`` (chaining across handlers): the transformed text
  is the SINGLE copy that gets persisted, rendered, and sent (no invariant
  violation, exactly the reasoning that made ``before_agent_start`` legal); or
* CONSUMES the input (``handled: True``): no turn starts, no user node is
  persisted, the provider is never called, and prompt() returns no messages.

This suite pins:
  * ``api.on("input", …)`` on a bucket-bound api lands in the runner (fires via
    ``emit_input``), not the notify bus;
  * a transform is DURABLE and PRE-NODE — the transformed text (and ONLY it, not
    the original) reaches the model and lands on the persisted path, surviving a
    reload (a fresh fold over the log's raw entries);
  * ``handled`` short-circuits the turn — the provider is never invoked and no
    user node is written;
  * transforms chain (later handler sees the earlier one's value);
  * the caller-echoed user turn (TUI passes full history) is stripped against the
    PRE-transform text, so the loop sees the transformed message exactly once.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extensions.runner import ExtensionError
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
        captured["calls"] = captured.get("calls", 0) + 1
        final = _text_assistant("done")
        return _Stream(
            [
                TextDeltaEvent(delta="done", partial=final),
                DoneEvent(final=final, usage=Usage()),
            ]
        )

    return fake


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(*extensions, system_prompt: str = "") -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt=system_prompt,
        extensions=list(extensions),
    )


def _user_texts(messages: list[Any]) -> list[str]:
    """Text of every user message in a message list (pydantic or dict)."""
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


async def test_api_on_input_routes_to_runner_bucket() -> None:
    """``api.on("input")`` binds to the runner bucket — firing via ``emit_input``
    (not the notify bus) reaches it."""
    seen: list[dict] = []

    def ext(api):
        api.on("input", lambda event, ctx: seen.append(event))

    session = _session(ext)
    result = await session._extension_runner.emit_input("hello", None)

    assert seen == [{"type": "input", "prompt": "hello", "images": None}]
    assert result == {"handled": False, "prompt": "hello", "images": None}


async def test_transform_is_pre_node_and_durable() -> None:
    """A transform replaces the prompt BEFORE the user node exists: the transformed
    text (and only it) reaches the model AND lands on the persisted path."""

    def ext(api):
        api.on(
            "input",
            lambda event, ctx: {"prompt": f"Respond briefly: {event['prompt']}"},
        )

    session = _session(ext)

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        await session.prompt("what is TypeScript")

    # The transformed text is what the model saw — the original never appears.
    wire_user_texts = _user_texts(captured["context"]["messages"])
    assert wire_user_texts == ["Respond briefly: what is TypeScript"]

    # ... and it is the SINGLE copy on the persisted path — exactly one user node,
    # carrying the transformed text (no separate original node).
    assert _user_texts(session.messages) == ["Respond briefly: what is TypeScript"]


async def test_transform_survives_reload() -> None:
    """Reload-invariance (à la S29): a fresh fold over the log's raw entries still
    carries the transformed user turn — no second history."""

    def ext(api):
        api.on("input", lambda event, ctx: {"prompt": event["prompt"].upper()})

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream({}),
    ):
        await session.prompt("hello world")

    # Simulate reload: rebuild the active path purely from persisted raw entries.
    reloaded = ConversationTree(
        session.session_log.entries(), session.session_log.cursor
    ).context_for()
    assert _user_texts(reloaded) == ["HELLO WORLD"]


async def test_handled_short_circuits_the_turn() -> None:
    """``handled: True`` consumes the input: no provider call, no user node, no
    returned messages."""
    ran: list[str] = []

    def ext(api):
        def on_input(event, ctx):
            ran.append(event["prompt"])
            return {"handled": True}

        api.on("input", on_input)

    session = _session(ext)

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        result = await session.prompt("ping")

    assert ran == ["ping"]  # the handler saw the input
    assert result == []  # no turn ran
    assert captured.get("calls", 0) == 0  # the provider was never invoked
    assert session.messages == []  # nothing persisted onto the path


async def test_transforms_chain_across_handlers() -> None:
    """Two handlers chain: the second sees the first's transformed value."""
    seen_by_second: dict[str, str] = {}

    def first(api):
        api.on("input", lambda event, ctx: {"prompt": event["prompt"] + " [1]"})

    def second(api):
        def on_input(event, ctx):
            seen_by_second["prompt"] = event["prompt"]
            return {"prompt": event["prompt"] + " [2]"}

        api.on("input", on_input)

    session = _session(first, second)
    result = await session._extension_runner.emit_input("go", None)

    assert seen_by_second["prompt"] == "go [1]"  # first's value was live to second
    assert result == {"handled": False, "prompt": "go [1] [2]", "images": None}


async def test_handled_stops_chain_immediately() -> None:
    """A ``handled`` return stops dispatch — a later handler never runs."""
    later_ran: list[str] = []

    def first(api):
        api.on("input", lambda event, ctx: {"handled": True})

    def second(api):
        api.on("input", lambda event, ctx: later_ran.append(event["prompt"]))

    session = _session(first, second)
    result = await session._extension_runner.emit_input("x", None)

    assert result["handled"] is True
    assert later_ran == []


async def test_input_handler_error_surfaced_not_swallowed() -> None:
    """A throwing input handler is reported via the runner's on_error and dispatch
    continues to the next handler (Fail-Early: no silent drop)."""
    errors: list[ExtensionError] = []
    second_ran: list[str] = []

    def first(api):
        def boom(event, ctx):
            raise RuntimeError("expansion failed")

        api.on("input", boom)

    def second(api):
        api.on("input", lambda event, ctx: second_ran.append(event["prompt"]))

    session = _session(first, second)
    session._extension_runner.on_error(errors.append)

    result = await session._extension_runner.emit_input("hi", None)

    assert len(errors) == 1
    assert errors[0].event == "input"
    assert "expansion failed" in errors[0].error
    assert second_ran == ["hi"]  # dispatch continued past the failure
    assert result == {"handled": False, "prompt": "hi", "images": None}


async def test_caller_echoed_turn_stripped_against_pre_transform_text() -> None:
    """The TUI passes the full history ending with the ORIGINAL user text. After a
    transform the loop must still see the (transformed) user turn exactly once —
    the echo is stripped against the pre-transform text, not duplicated."""

    def ext(api):
        api.on("input", lambda event, ctx: {"prompt": event["prompt"] + " (edited)"})

    session = _session(ext)

    # Emulate the backend: context ends with the caller's echo of the original.
    context = [{"role": "user", "content": [{"type": "text", "text": "original"}]}]

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        await session.prompt("original", context=context)

    # Exactly one user turn on the wire — the transformed one, no leftover echo.
    assert _user_texts(captured["context"]["messages"]) == ["original (edited)"]


async def test_zero_handler_fast_path_leaves_prompt_untouched() -> None:
    """With no input handler the prompt is unchanged (has_handlers fast path)."""

    def ext(api):
        # Registers an unrelated hook only — no input handler.
        api.on("tool_result", lambda event, ctx: None)

    session = _session(ext)

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        await session.prompt("verbatim")

    assert _user_texts(captured["context"]["messages"]) == ["verbatim"]
