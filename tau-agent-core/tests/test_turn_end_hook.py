"""S43 (E6 §2) — the ``turn_end`` MUTATING hook (durable append + observer coexist).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S43 (unblocks trigger-compact,
checkpoint-annotate, status), decision D-E6-6.
pi source of truth: coding-agent/src/core/extensions/runner.ts + agent-session.ts
(pi's ``turn_end`` is NOTIFY-only, agent-session.ts:617); τ diverges by adding a
mutating return alongside the notify variant.

The mutating ``turn_end`` fires per loop turn, AFTER the notify ``turn_end``
``AgentEvent``, with ``{turn_index, usage, messages}``. A handler may

* RETURN ``{message}`` — the loop appends it as a durable ``customMessage`` node
  BEFORE the next turn: threaded into the running context (so the next turn's
  model sees it, custom→user on the wire) AND persisted as a ``customMessage``
  tree node (persisted == rendered == sent). Append-only, same power/limits as
  ``before_agent_start`` — it never rewrites the assistant/tool nodes above it; or
* RETURN NOTHING — a PURE OBSERVER, exactly pi's notify-grade ``turn_end``.

This suite pins:
  * ``api.on("turn_end", …)`` on a bucket-bound api lands in the runner (fires via
    ``emit_turn_end``), not the notify bus;
  * a returned message is a DURABLE append — a ``customMessage`` node on the
    persisted path, carrying ``role: "custom"`` + its ``customType``, surviving a
    reload (a fresh fold over the log's raw entries);
  * the event carries the finished turn's ``turn_index`` / real ``usage`` / the
    messages it produced;
  * the appended node is visible to the model on the NEXT turn (custom→user wire);
  * an observing handler (returns nothing) coexists with a mutating one — both run,
    only the mutating one appends;
  * the notify ``turn_end`` ``AgentEvent`` still reaches ``api.on("all", …)``
    observers, unchanged (the two channels coexist);
  * Fail-Early: a returned message without ``customType`` raises;
  * runner-level accumulation, load/registration order, and error surfacing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extensions.runner import ExtensionError, ExtensionRunner
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


def _text_assistant(text: str, usage: Usage | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=usage or Usage(),
    )


def _tool_call_assistant(call_id: str, usage: Usage | None = None) -> AssistantMessage:
    """An assistant with one (unregistered) ``write`` call → error result, loop continues."""
    return AssistantMessage(
        content=[
            ToolCall(
                type="toolCall",
                id=call_id,
                name="write",
                arguments={"path": "f.py", "content": "x"},
            )
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=usage or Usage(),
    )


def _text_stream(text: str, usage: Usage | None = None):
    async def fake(model, context, options=None):
        final = _text_assistant(text, usage)
        return _Stream(
            [TextDeltaEvent(delta=text, partial=final), DoneEvent(final=final, usage=Usage())]
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


def _session(*extensions) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=list(extensions),
    )


def _custom_nodes(messages: list[Any]) -> list[dict[str, Any]]:
    """Every ``role: "custom"`` message in a folded message list."""
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "custom"]


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _user_texts(messages: list[Any]) -> list[str]:
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
            btype = block.get("type") if isinstance(block, dict) else None
            text = block.get("text") if isinstance(block, dict) else None
            if btype == "text" and text is not None:
                out.append(text)
    return out


# ── runner-level unit tests (emit_turn_end) ──────────────────────────────────


async def test_turn_end_is_a_hook_event() -> None:
    """``turn_end`` is in HOOK_EVENTS so ``api.on`` routes it to the runner (S43)."""
    assert "turn_end" in ExtensionRunner.HOOK_EVENTS


async def test_emit_turn_end_accumulates_returned_messages_in_order() -> None:
    """Every returned ``message`` accumulates in load/registration order."""
    runner = ExtensionRunner()
    a = runner.register_extension("a")
    a.on("turn_end", lambda e, ctx: {"message": {"customType": "note", "content": "A1"}})
    a.on("turn_end", lambda e, ctx: {"message": {"customType": "note", "content": "A2"}})
    b = runner.register_extension("b")
    b.on("turn_end", lambda e, ctx: {"message": {"customType": "note", "content": "B1"}})

    injected = await runner.emit_turn_end(turn_index=0, usage=None, messages=[])

    assert [m["content"] for m in injected] == ["A1", "A2", "B1"]


async def test_emit_turn_end_observer_returns_nothing() -> None:
    """A handler returning ``None`` accumulates no message (pure observer)."""
    runner = ExtensionRunner()
    seen: list[dict] = []
    ext = runner.register_extension("obs")
    ext.on("turn_end", lambda e, ctx: seen.append(e))

    injected = await runner.emit_turn_end(
        turn_index=3, usage={"tokens": 42}, messages=[{"role": "assistant"}]
    )

    assert injected == []
    assert seen == [
        {
            "type": "turn_end",
            "turn_index": 3,
            "usage": {"tokens": 42},
            "messages": [{"role": "assistant"}],
        }
    ]


async def test_emit_turn_end_surfaces_handler_error_and_continues() -> None:
    """A throwing handler is surfaced via on_error; the next handler still runs."""
    runner = ExtensionRunner()
    errors: list[ExtensionError] = []
    runner.on_error(errors.append)

    def boom(event, ctx):
        raise RuntimeError("kaboom")

    a = runner.register_extension("bad")
    a.on("turn_end", boom)
    b = runner.register_extension("good")
    b.on("turn_end", lambda e, ctx: {"message": {"customType": "note", "content": "ok"}})

    injected = await runner.emit_turn_end(turn_index=0, usage=None, messages=[])

    assert [m["content"] for m in injected] == ["ok"]
    assert len(errors) == 1
    assert errors[0].event == "turn_end"
    assert errors[0].extension_path == "bad"
    assert "kaboom" in errors[0].error


# ── session integration ──────────────────────────────────────────────────────


async def test_api_on_turn_end_routes_to_runner_bucket() -> None:
    """``api.on("turn_end")`` binds to the runner bucket, not the notify bus."""
    seen: list[dict] = []

    def ext(api):
        api.on("turn_end", lambda event, ctx: seen.append(event))

    session = _session(ext)
    assert session._extension_runner.has_handlers("turn_end")
    injected = await session._extension_runner.emit_turn_end(turn_index=0, usage=None, messages=[])

    assert injected == []
    assert seen and seen[0]["type"] == "turn_end"


async def test_returned_message_is_durable_append() -> None:
    """A returned message becomes a ``customMessage`` node on the persisted path."""
    events: list[dict] = []

    def ext(api):
        def on_turn_end(event, ctx):
            events.append(event)
            return {"message": {"customType": "checkpoint", "content": "turn done"}}

        api.on("turn_end", on_turn_end)

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    # The event carried the finished turn's identity.
    assert len(events) == 1
    assert events[0]["turn_index"] == 0
    assert events[0]["usage"] is not None  # real per-completion usage (dict), never faked
    assert events[0]["messages"]  # the messages this turn produced (the assistant)

    # The append is a durable ``custom`` node on the active path.
    customs = _custom_nodes(session.messages)
    assert len(customs) == 1
    assert customs[0]["customType"] == "checkpoint"
    assert _text_of(customs[0]) == "turn done"

    # ... backed by a real ``customMessage`` tree entry (not a plain message node).
    kinds = [e.get("type") for e in session.session_log.entries()]
    assert kinds.count("customMessage") == 1


async def test_append_survives_reload() -> None:
    """Reload-invariance (à la S29): a fresh fold over raw entries keeps the node."""

    def ext(api):
        api.on(
            "turn_end",
            lambda e, ctx: {"message": {"customType": "annot", "content": "kept"}},
        )

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    reloaded = ConversationTree(
        session.session_log.entries(), session.session_log.cursor
    ).context_for()
    customs = _custom_nodes(reloaded)
    assert len(customs) == 1
    assert customs[0]["customType"] == "annot"
    assert _text_of(customs[0]) == "kept"


async def test_appended_node_is_visible_to_model_next_turn() -> None:
    """The durable append is threaded into the NEXT turn's wire (custom→user)."""
    wire_payloads: list[list[Any]] = []
    call_count = {"n": 0}

    async def fake(model, context, options=None):
        wire_payloads.append(list(context.get("messages", [])))
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:
            final = _tool_call_assistant("call_0")  # turn 0 → tool call → another turn
            return _Stream([DoneEvent(final=final, usage=Usage())])
        final = _text_assistant("done")  # turn 1 → text ends the loop
        return _Stream([DoneEvent(final=final, usage=Usage())])

    def ext(api):
        # Append only after the FIRST turn (turn_index 0), so turn 1 must see it.
        def on_turn_end(event, ctx):
            if event["turn_index"] == 0:
                return {"message": {"customType": "nudge", "content": "INJECTED-NUDGE"}}
            return None

        api.on("turn_end", on_turn_end)

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("go")

    assert len(wire_payloads) == 2  # two LLM round-trips
    # Turn 1's wire carries the injected node, remapped custom→user.
    assert "INJECTED-NUDGE" in _user_texts(wire_payloads[1])
    # Turn 0's wire did NOT (it was appended after turn 0 completed).
    assert "INJECTED-NUDGE" not in _user_texts(wire_payloads[0])


async def test_observer_coexists_with_mutator() -> None:
    """A pure observer and a mutating handler both fire; only the mutator appends."""
    observed: list[dict] = []

    def observer(api):
        api.on("turn_end", lambda event, ctx: observed.append(event))

    def mutator(api):
        api.on(
            "turn_end",
            lambda e, ctx: {"message": {"customType": "note", "content": "appended"}},
        )

    session = _session(observer, mutator)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    assert len(observed) == 1  # the observer saw the turn
    customs = _custom_nodes(session.messages)
    assert len(customs) == 1  # exactly one append (the mutator's), not two
    assert _text_of(customs[0]) == "appended"


async def test_pure_observer_appends_nothing() -> None:
    """A handler that returns nothing leaves the path untouched (notify-grade)."""

    def ext(api):
        api.on("turn_end", lambda event, ctx: None)

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    assert _custom_nodes(session.messages) == []
    kinds = [e.get("type") for e in session.session_log.entries()]
    assert "customMessage" not in kinds


async def test_notify_turn_end_still_reaches_all_observers() -> None:
    """The notify ``turn_end`` ``AgentEvent`` is unchanged — ``api.on("all")`` gets it."""
    types_seen: list[str] = []

    def ext(api):
        api.on("all", lambda event: types_seen.append(event.type))

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    assert "turn_end" in types_seen  # the notify channel still fires for observers


async def test_returned_message_without_custom_type_raises() -> None:
    """Fail-Early: a returned message missing ``customType`` raises (no fabrication)."""

    def ext(api):
        api.on("turn_end", lambda e, ctx: {"message": {"content": "orphan"}})

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        with pytest.raises(ValueError, match="customType"):
            await session.prompt("go")
