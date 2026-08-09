"""tau-003 — pinning the tempo-table corrections (SIM_SPEC_v2.md §12.4 / §16.5).

§12.4's tempo table maps two behaviours onto the extension hooks that turned out,
on inspection, to be wrong done literally (§16.5 corrections 1 and 2). This suite
pins the two fixes plus their supporting invariants:

Change 1 — declared discourse position (§12.4's "phasic" row, corrected). A
``before_agent_start`` handler's returned ``message`` may declare
``"position": "before_user"`` to thread AHEAD of the user's utterance instead of
behind it. The unqualified inference "attached before deliberation" therefore
"precedes the utterance" was wrong; attachment (one model call, results present)
held, precedence did not. The default (`"after_user"`, or the key absent
entirely) reproduces pi's order byte for byte —
``[user, ...nextTurn, ...custom]`` — so a message written before this change
keeps behaving exactly as it did. An unrecognised ``position`` value raises
(Fail-Early): a reflex surface threaded into the wrong discourse position still
runs and still passes, which is precisely the corrupted-measurement failure mode
the declared key exists to remove.

Change 2 — the ``user_turn_end`` hook (§12.4's "consolidative" row, corrected).
§16.5 correction 2 records that mapping a once-per-utterance consolidation onto
the pre-existing ``turn_end`` hook is wrong: ``turn_end`` fires once per
AGENT-LOOP turn (once per assistant completion), so an utterance resolved in N
tool round-trips runs the "once per utterance" logic N+1 times. ``user_turn_end``
fires exactly once per ``AgentSession.prompt()`` call, at its tail — after the
loop, every followUp re-entry, and auto-compaction — which is the boundary
§16.5 names. The two hooks' cadences are pinned side by side in the same test so
the factor between them is visible, not just individually asserted.

These tests drive the REAL ``AgentSession.prompt()`` with only the network
boundary faked (``tau_agent_core.agent_loop.stream_simple``), capturing the exact
message list handed to the (fake) provider wherever ordering is under test —
proving what the model would see, not what the session merely intended.

Reference: SIM_SPEC_v2.md §12.4 (the tempo table), §16.5 (the two cadence
corrections); tau_agent_core.extensions.runner (MESSAGE_POSITION_*, HOOK_EVENTS,
emit_before_agent_start, emit_user_turn_end); tau_agent_core.agent_session
(AgentSession._run_one_turn, AgentSession._run_user_turn_end).
Conventions copied from tau-agent-core/tests/test_turn_end_hook.py,
test_before_agent_start_hook.py, and test_api_on_hook_bridge.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.extensions.runner import (
    FIRING_UNIT_AGENT_LOOP_TURN,
    FIRING_UNIT_USER_TURN,
    ExtensionError,
    ExtensionRunner,
)
from tau_agent_core.session_log import InMemorySessionLog

# ── shared harness (network boundary faked; everything else real) ────────────


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


def _capturing_stream(captured: dict[str, Any]):
    """Fake stream_simple that records the context dict then answers with text."""

    async def fake(model, context, options=None):
        captured["context"] = context
        final = _text_assistant("done")
        return _Stream(
            [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
        )

    return fake


def _n_tool_calls_then_text(n_tool_calls: int, final_text: str = "final answer"):
    """Fake stream_simple: N tool-call completions, then one text completion.

    Total assistant completions = n_tool_calls + 1 — the N+1 cadence the
    turn_end/user_turn_end tests pin.
    """
    counter = {"n": 0}

    async def fake(model, context, options=None):
        n = counter["n"]
        counter["n"] += 1
        if n < n_tool_calls:
            final: AssistantMessage = _tool_call_assistant(f"call_{n}")
        else:
            final = _text_assistant(final_text)
        return _Stream([DoneEvent(final=final, usage=Usage())])

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


def _make_session(**kwargs: Any) -> AgentSession:
    """A bare session for direct ``_extension_runner.register_extension`` wiring."""
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), **kwargs)


def _session(*extensions: Any) -> AgentSession:
    """A session loaded with factory-style extensions (``def ext(api): ...``)."""
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions)
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
    """Text of every ``user`` message on the wire (pydantic UserMessage or dict).

    ``before_agent_start`` custom nodes serialize custom->user on the wire, so
    they show up here alongside the genuine user turn and queued nextTurn
    messages — exactly the surface the model sees, in delivery order.
    """
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
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if btype == "text" and text is not None:
                out.append(text)
    return out


def _discourse_texts(messages: list[Any]) -> list[str]:
    """Text of every ``user`` OR ``custom`` node in a RAW (pre-wire) fold.

    ``session.messages`` / a reloaded ``ConversationTree`` fold store an
    extension-injected node with its durable ``role: "custom"`` — the
    custom->user remap is a WIRE-time conversion (``convert_to_llm``), not
    something the raw fold applies. So comparing the raw fold's discourse
    order against the wire's ``_user_texts`` needs both roles folded into one
    text sequence, in path order.
    """
    out: list[str] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "custom"):
            continue
        out.append(_text_of(m))
    return out


# ── Change 1: declared discourse position ─────────────────────────────────────


async def test_before_user_and_after_user_ordering_on_the_wire() -> None:
    """The full [before, user, nextTurn, after] order reaches the provider.

    Two ``before_agent_start`` handlers each contribute one message: one
    declares ``position: "before_user"``, the other declares nothing (defaults
    to ``after_user``). A queued ``nextTurn`` message sits between the user
    turn and the after_user injection, matching pi's
    ``[user, ...nextTurn, ...custom]`` order with the before_user prefix ahead
    of it. Captured at the ``stream_simple`` boundary — the deepest point
    before the (fake) network — so this proves what the MODEL sees.
    """
    session = _make_session()

    def before_handler(event, ctx):
        return {
            "message": {
                "customType": "phasic",
                "content": "BEFORE-MSG",
                "position": "before_user",
            }
        }

    def after_handler(event, ctx):
        return {"message": {"customType": "reflex", "content": "AFTER-MSG"}}

    session._extension_runner.register_extension("mem:before").on(
        "before_agent_start", before_handler
    )
    session._extension_runner.register_extension("mem:after").on(
        "before_agent_start", after_handler
    )
    session._queue_message("QUEUED-MSG", deliver_as="nextTurn")

    captured: dict[str, Any] = {}
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_capturing_stream(captured)):
        await session.prompt("USER-TEXT")

    messages = captured["context"]["messages"]
    assert _user_texts(messages) == ["BEFORE-MSG", "USER-TEXT", "QUEUED-MSG", "AFTER-MSG"]


async def test_persisted_order_matches_model_visible_order() -> None:
    """The session log / ``session.messages`` fold reproduces the wire order.

    The reload-fork invariant: a fresh fold over the raw log entries (what a
    reload would rebuild) must walk the path in the exact order the model saw
    it, or a reload replays a DIFFERENT conversation than the one that
    actually happened.
    """
    session = _make_session()

    session._extension_runner.register_extension("mem:before").on(
        "before_agent_start",
        lambda e, ctx: {
            "message": {"customType": "phasic", "content": "BEFORE-MSG", "position": "before_user"}
        },
    )
    session._extension_runner.register_extension("mem:after").on(
        "before_agent_start",
        lambda e, ctx: {"message": {"customType": "reflex", "content": "AFTER-MSG"}},
    )
    session._queue_message("QUEUED-MSG", deliver_as="nextTurn")

    captured: dict[str, Any] = {}
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_capturing_stream(captured)):
        await session.prompt("USER-TEXT")

    wire_order = _user_texts(captured["context"]["messages"])

    # session.messages: the live fold via ConversationTree.context_for(). The
    # raw fold keeps injected nodes at their durable ``role: "custom"`` (the
    # custom->user remap is a WIRE-time conversion), so compare via
    # _discourse_texts, which folds both "user" and "custom" nodes into one
    # path-order sequence.
    assert _discourse_texts(session.messages) == wire_order

    # A fresh fold over the raw log entries too — the reload path, not the
    # live cached one.
    reloaded = ConversationTree(
        session.session_log.entries(), session.session_log.cursor
    ).context_for()
    assert _discourse_texts(reloaded) == wire_order
    assert wire_order == ["BEFORE-MSG", "USER-TEXT", "QUEUED-MSG", "AFTER-MSG"]


async def test_unrecognised_position_raises() -> None:
    """Fail-Early: a ``position`` outside the legal set raises, not defaults.

    The message names the offending extension's path and the legal list, so a
    demo author sees exactly what to fix.
    """
    session = _make_session()
    session._extension_runner.register_extension("mem:bad").on(
        "before_agent_start",
        lambda e, ctx: {
            "message": {"customType": "x", "content": "y", "position": "middle_of_the_turn"}
        },
    )

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        with pytest.raises(ValueError) as excinfo:
            await session.prompt("go")

    message = str(excinfo.value)
    assert "mem:bad" in message
    assert "middle_of_the_turn" in message
    assert "before_user" in message
    assert "after_user" in message


async def test_default_position_reproduces_pi_order() -> None:
    """No ``position`` key at all → pi's exact ``[user, ...nextTurn, ...custom]``.

    A pi-parity pin: an extension that never heard of ``position`` keeps
    behaving exactly as it did before this change.
    """
    session = _make_session()
    session._extension_runner.register_extension("mem:after").on(
        "before_agent_start",
        lambda e, ctx: {"message": {"customType": "reflex", "content": "AFTER-MSG"}},
    )
    session._queue_message("QUEUED-MSG", deliver_as="nextTurn")

    captured: dict[str, Any] = {}
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_capturing_stream(captured)):
        await session.prompt("USER-TEXT")

    messages = captured["context"]["messages"]
    assert _user_texts(messages) == ["USER-TEXT", "QUEUED-MSG", "AFTER-MSG"]


# ── Change 2: user_turn_end cadence ────────────────────────────────────────────


async def test_turn_end_and_user_turn_end_cadence_differ() -> None:
    """turn_end fires N+1 times across N tool round-trips; user_turn_end fires once.

    N=3 tool round-trips + 1 text-only answer = 4 assistant completions. The
    mutating ``turn_end`` hook's per-loop-turn cadence is CORRECT-AS-IS (pi
    parity); ``user_turn_end`` is the once-per-prompt() sibling. Both counts
    are asserted in the SAME test, against the SAME prompt() call, so the
    factor between them is visible rather than merely two isolated assertions.
    """
    turn_end_events: list[dict[str, Any]] = []
    user_turn_end_events: list[dict[str, Any]] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("turn_end", lambda event, ctx: turn_end_events.append(event))
        api.on("user_turn_end", lambda event, ctx: user_turn_end_events.append(event))

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_n_tool_calls_then_text(3),
    ):
        await session.prompt("go")

    assert len(turn_end_events) == 4  # N+1: pi-parity per-completion cadence
    assert len(user_turn_end_events) == 1  # once per prompt()
    assert len(turn_end_events) != len(user_turn_end_events)

    # Each event states its own firing unit, so a handler holding a bare event
    # dict can say what it counted instead of inferring the cadence from
    # documentation about a different hook (§9 rule 1; the ``Trace.arm`` shape).
    assert {e["firing_unit"] for e in turn_end_events} == {FIRING_UNIT_AGENT_LOOP_TURN}
    assert [e["firing_unit"] for e in user_turn_end_events] == [FIRING_UNIT_USER_TURN]
    assert FIRING_UNIT_AGENT_LOOP_TURN != FIRING_UNIT_USER_TURN


async def test_turn_end_firing_unit_is_carried_on_the_event_not_inferred() -> None:
    """Both turn-boundary hooks name their own unit ON the event they hand over.

    §12.4's table maps a tempo onto a hook name; the hook name alone does not
    say what it counts, and ``turn_end`` counts something different from
    ``user_turn_end``. Carrying the unit as a field means a consolidation
    handler that ended up on the wrong hook can DETECT it at runtime instead of
    silently producing a number wrong by a factor — the same reason freeze v1.1
    moved ``arm`` onto ``Trace`` rather than leaving it in the directory name.
    """
    turn_end_events: list[dict[str, Any]] = []
    user_turn_end_events: list[dict[str, Any]] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("turn_end", lambda event, ctx: turn_end_events.append(event))
        api.on("user_turn_end", lambda event, ctx: user_turn_end_events.append(event))

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.prompt("go")

    assert turn_end_events[0]["firing_unit"] == FIRING_UNIT_AGENT_LOOP_TURN
    assert user_turn_end_events[0]["firing_unit"] == FIRING_UNIT_USER_TURN


async def test_user_turn_end_durable_append_and_visible_next_prompt() -> None:
    """A returned ``user_turn_end`` message is a durable append, visible next turn.

    Lands as a ``role: "custom"`` node in ``prompt()``'s return value AND the
    session log this turn, then reaches the model (custom->user on the wire)
    on the NEXT ``prompt()`` call.
    """

    nudged_once: list[bool] = []

    def ext(api: ExtensionAPI) -> None:
        def on_user_turn_end(event, ctx):
            if nudged_once:
                return None
            nudged_once.append(True)
            return {"message": {"customType": "consolidated", "content": "NUDGE"}}

        api.on("user_turn_end", on_user_turn_end)

    session = _session(ext)
    wire_payloads: list[list[Any]] = []
    call_count = {"n": 0}

    async def fake(model, context, options=None):
        wire_payloads.append(list(context.get("messages", [])))
        n = call_count["n"]
        call_count["n"] += 1
        final = _text_assistant(f"reply-{n}")
        return _Stream([DoneEvent(final=final, usage=Usage())])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        turn_messages_1 = await session.prompt("go")

        # ... visible in prompt()'s own return value for turn 1, and persisted
        # as a real customMessage tree entry, BEFORE the second prompt fires
        # (which would append nothing further — the handler is one-shot).
        customs_returned = _custom_nodes(turn_messages_1)
        assert len(customs_returned) == 1
        assert customs_returned[0]["customType"] == "consolidated"
        assert _text_of(customs_returned[0]) == "NUDGE"
        kinds = [e.get("type") for e in session.session_log.entries()]
        assert kinds.count("customMessage") == 1

        await session.prompt("go-again")

    # ... and reachable to the model on the NEXT prompt's wire, not the first.
    assert len(wire_payloads) == 2
    assert "NUDGE" not in _user_texts(wire_payloads[0])
    assert "NUDGE" in _user_texts(wire_payloads[1])


async def test_user_turn_end_receives_loop_turns_and_messages() -> None:
    """The event carries the right ``loop_turns`` count and this turn's messages."""
    captured_event: dict[str, Any] = {}

    def ext(api: ExtensionAPI) -> None:
        def on_user_turn_end(event, ctx):
            captured_event.update(event)
            return None

        api.on("user_turn_end", on_user_turn_end)

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_n_tool_calls_then_text(2),
    ):
        turn_messages = await session.prompt("go")

    assert captured_event["type"] == "user_turn_end"
    # 2 tool round-trips + 1 text completion = 3 assistant completions.
    assert captured_event["loop_turns"] == 3
    assert sum(1 for m in captured_event["messages"] if m.get("role") == "assistant") == 3
    # No handler returned a message, so nothing was appended after the fact —
    # the event's messages ARE this turn's final messages.
    assert captured_event["messages"] == turn_messages


async def test_user_turn_end_missing_custom_type_raises() -> None:
    """Fail-Early: a returned message missing ``customType`` raises."""

    def ext(api: ExtensionAPI) -> None:
        api.on("user_turn_end", lambda event, ctx: {"message": {"content": "orphan"}})

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        with pytest.raises(ValueError, match="user_turn_end message is missing"):
            await session.prompt("go")


async def test_user_turn_end_does_not_fire_when_input_handled() -> None:
    """No turn started (``input`` returned ``handled: True``) → no turn ended."""
    seen: list[dict[str, Any]] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("input", lambda event, ctx: {"handled": True})
        api.on("user_turn_end", lambda event, ctx: seen.append(event))

    session = _session(ext)
    result = await session.prompt("go")

    assert result == []
    assert seen == []


async def test_user_turn_end_does_not_fire_for_continue_conversation() -> None:
    """``continue_conversation()`` produces loop turns without a user turn."""
    seen: list[dict[str, Any]] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("user_turn_end", lambda event, ctx: seen.append(event))

    session = _session(ext)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("hi")):
        await session.continue_conversation()

    assert seen == []


async def test_user_turn_end_fires_once_across_a_followup_drain() -> None:
    """One firing even when a followUp re-entry runs inside the same prompt().

    The followUp queued during the first loop turn re-enters ``_run_one_turn``
    within the SAME ``prompt()`` call (S20), producing a second assistant
    completion. ``user_turn_end`` still fires exactly once, at the tail of the
    whole ``prompt()`` — but ``loop_turns`` is now 2, not 1, which is the
    visible trace of the drain happening under the single firing.
    """
    user_turn_end_events: list[dict[str, Any]] = []

    def ext(api: ExtensionAPI) -> None:
        api.on("user_turn_end", lambda event, ctx: user_turn_end_events.append(event))

    session = _session(ext)
    # Queue a followUp BEFORE prompt() starts: prompt() drains "nextTurn"
    # messages before the first _run_one_turn call but only drains "followUp"
    # messages AFTER it (_end_of_prompt_drain, S20) — so this one re-enters
    # the loop for a second assistant completion, WITHIN the same prompt().
    session._queue_message("FOLLOWUP-TEXT", deliver_as="followUp")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_text_stream("reply")):
        await session.prompt("go")

    assert len(user_turn_end_events) == 1
    assert user_turn_end_events[0]["loop_turns"] == 2


# ── Change 2: runner-level (HOOK_EVENTS + api.on bridge + error surfacing) ────


def test_user_turn_end_is_a_hook_event() -> None:
    """``user_turn_end`` is in HOOK_EVENTS so ``api.on`` routes it to the runner."""
    assert "user_turn_end" in ExtensionRunner.HOOK_EVENTS


async def test_api_on_user_turn_end_routes_to_runner_bucket() -> None:
    """``api.on("user_turn_end", …)`` on a bucket-bound api lands in the runner,
    not the notify ``EventBus``."""
    session = _make_session()
    api = session._bind_extension_api("mem:consolidator")

    seen: list[dict[str, Any]] = []
    api.on("user_turn_end", lambda event, ctx: seen.append(event))

    assert session._extension_runner.has_handlers("user_turn_end") is True

    injected = await session._extension_runner.emit_user_turn_end(loop_turns=1, messages=[])

    assert injected == []
    assert seen and seen[0]["type"] == "user_turn_end"


async def test_emit_user_turn_end_surfaces_handler_error_and_continues() -> None:
    """A throwing handler is surfaced via on_error; the next handler still runs."""
    runner = ExtensionRunner()
    errors: list[ExtensionError] = []
    runner.on_error(errors.append)

    def boom(event, ctx):
        raise RuntimeError("kaboom")

    bad = runner.register_extension("bad")
    bad.on("user_turn_end", boom)
    good = runner.register_extension("good")
    good.on(
        "user_turn_end",
        lambda e, ctx: {"message": {"customType": "note", "content": "ok"}},
    )

    injected = await runner.emit_user_turn_end(loop_turns=1, messages=[])

    assert [m["content"] for m in injected] == ["ok"]
    assert len(errors) == 1
    assert errors[0].event == "user_turn_end"
    assert errors[0].extension_path == "bad"
    assert "kaboom" in errors[0].error
