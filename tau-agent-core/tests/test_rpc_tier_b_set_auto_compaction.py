"""B4 — RPC Tier B verb ``set_auto_compaction`` (docs/RPC-TIER-B.md D-4).

Two layers:

* **plain setter** — params-schema validation, idempotence, the D-1
  ``turn_safety_guard`` refusal (``TURN_STILL_RUNNING``) when a turn is
  already in flight.
* **the tier's most interesting test** (docs/RPC-TIER-B.md, B4's own task
  description) — auto-compaction is hard-disabled on the RPC path
  (``backends.py:885``), so this file proves ``set_auto_compaction`` is the
  *only* route back to it: flip it on over the wire, drive a real turn
  through a real ``AgentSession``, and show an actual ``compaction`` entry
  lands in the log. Along the way it pins down the exact shape
  docs/RPC-TIER-B.md D-4 documents and asks B4 to test: ``_maybe_auto_compact``
  (``agent_session.py:3286-3291``) emits its own ``agent_start``/``agent_end``
  pair through ``self._events.emit`` directly, not ``_emit_stamped`` — so that
  pair carries no ``submission_id`` (an ORPHAN pair, from a host's point of
  view), while its ``agent_end`` still carries a ``cursor`` (every outbound
  ``agent_end`` is stamped at dequeue by ``RPCHandler.prepare_outbound``,
  regardless of provenance).

Not a cross-test-file import from ``test_rpc.py`` for the ``_Stream``/
``_assistant`` fakes: ``test_rpc_tier_b_scaffolding.py``'s own docstring
notes that idiom has no precedent in this suite, and this file needs its own
variant anyway (a large, deliberate ``Usage.total_tokens`` to trip
``should_compact`` without needing a wall of seeded messages first).

Reference: docs/RPC-TIER-B.md §2 D-1, D-4, §1.1, §1.2, §6.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.rpc import RPCHandler, commands
from tau_agent_core.rpc.dialect import TURN_STILL_RUNNING
from tau_agent_core.session_log import InMemorySessionLog


def _model(*, context_window: int = 1000) -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=context_window,
        max_tokens=256,
    )


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _assistant(text: str, *, total_tokens: int = 2) -> AssistantMessage:
    """Mirrors ``test_rpc.py``'s ``_assistant`` helper, but with a
    caller-chosen ``Usage.total_tokens`` — the lever ``should_compact``
    actually reads. ``estimate_context_tokens`` (compaction.py) anchors on
    the LAST assistant message's reported ``Usage`` when one is present
    (pi-ported behaviour: "the provider is the source of truth for
    everything up to the last assistant turn") — so seeding a long PRIOR
    conversation does nothing to trip the threshold once a fresh turn's own
    small ``Usage`` becomes that anchor. A big, honest ``total_tokens`` here
    is what a real provider reporting a nearly-full context would send.
    """
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=total_tokens),
    )


class _Stream:
    """The minimal ``stream_simple`` return shape.

    Unlike ``test_rpc.py``'s own ``_Stream`` (which never yields a
    ``DoneEvent`` and relies on ``AgentLoop._stream_response``'s "stream
    completed without DoneEvent" fallback — ``Usage()``, all zeros,
    ``agent_loop.py:843-856``), THIS one yields a real ``DoneEvent`` whose
    ``final.usage`` is the caller-chosen ``total_tokens``. That fallback is
    exactly why a fake carrying a big ``Usage`` but no ``DoneEvent`` would
    silently persist a ZERO-usage assistant message and never trip
    ``should_compact`` — found by running this test against exactly that
    shape first.
    """

    def __init__(self, text: str, *, total_tokens: int = 2) -> None:
        self._text = text
        self._total_tokens = total_tokens

    def __aiter__(self):
        async def _gen():
            partial = _assistant(self._text, total_tokens=self._total_tokens)
            yield TextDeltaEvent(delta=self._text, partial=partial)
            yield DoneEvent(final=partial, usage=partial.usage)

        return _gen()

    def abort(self) -> None:
        pass


async def _summary_response(model: Any, context: Any, options: Any = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="AUTO-COMPACTION-SUMMARY")],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",  # type: ignore[arg-type]
        timestamp=0,
    )


@pytest.fixture
def real_session() -> AgentSession:
    """RPC-shaped construction (§1.1): ``compaction_settings=CompactionSettings
    (enabled=False)``, the exact call ``backends.py:885`` makes — this fixture
    starts every test from the state a real RPC session is in today.
    ``keep_recent_tokens=1`` (mirrors ``test_trigger_compact.py``): with only
    one turn's worth of "recent" budget, whatever was on the log before this
    turn is eligible to be cut, so a real ``compaction`` entry — not merely
    the brackets around a no-op — lands once triggered.
    """
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=[],
        compaction_settings=CompactionSettings(
            enabled=False, reserve_tokens=100, keep_recent_tokens=1
        ),
    )


@pytest.fixture
def real_handler(real_session: AgentSession) -> RPCHandler:
    return RPCHandler(real_session)


async def _drain_until_two_agent_ends(
    handler: RPCHandler, *, limit: int = 500, timeout: float = 5.0
):
    """Pop everything off the output queue up to and including the SECOND
    ``agent_end`` event — the turn's own stamped one, then (if compaction
    fired) ``_maybe_auto_compact``'s orphan one. Returns every item seen, in
    wire order.
    """
    items: list[dict[str, Any]] = []
    agent_ends = 0
    for _ in range(limit):
        item = await asyncio.wait_for(handler._output_queue.get(), timeout=timeout)
        items.append(item)
        if item.get("method") == "event" and item["params"].get("type") == "agent_end":
            agent_ends += 1
            if agent_ends >= 2:
                return items
    raise AssertionError(f"never saw two agent_end events; got {agent_ends} in {len(items)} items")


def _events_of_type(items: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if item.get("method") == "event" and item["params"].get("type") == event_type
    ]


# ── plain setter: params, idempotence, D-1 guard ────────────────────────────


async def test_set_auto_compaction_enables_and_returns_the_effective_state(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    assert real_session._compaction_settings.enabled is False
    # `cursor` is E5's tier-wide answer, not this verb's own idea (see
    # commands.py "E5 in Tier B"). Already non-None on a session that has run
    # nothing: AgentSession records an `agent_spec` customEntry at
    # construction (W2), and this verb appends nothing on top of it.
    tip = real_session.session_log.cursor
    assert tip is not None
    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_auto_compaction", "params": {"enabled": True}}
    )
    (response,) = [item for item in await _drain(real_handler) if item.get("id") == 1]
    assert response["result"] == {
        "enabled": True,
        "cursor": tip,
        "method": "set_auto_compaction",
    }
    assert real_session._compaction_settings.enabled is True


async def test_set_auto_compaction_disables_and_is_idempotent(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    real_session._compaction_settings.enabled = True
    tip = real_session.session_log.cursor
    for msg_id in (1, 2):
        await real_handler._handle_request(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "set_auto_compaction",
                "params": {"enabled": False},
            }
        )
    responses = {item["id"]: item for item in await _drain(real_handler)}
    # Idempotent in the cursor too (E5): neither call appends anything, so
    # both report the same unchanged tip.
    expected = {"enabled": False, "cursor": tip, "method": "set_auto_compaction"}
    assert responses[1]["result"] == expected
    assert responses[2]["result"] == expected
    assert real_session._compaction_settings.enabled is False


async def test_set_auto_compaction_returns_the_live_tip_although_it_moves_nothing(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    """E5, rule 1 of commands.py's "E5 in Tier B": a mutator's completion
    carries the resulting `cursor` even when the call advanced nothing.

    Finding 5 of the Tier B review — this verb returned no `cursor` key at
    all while `compact`, equally mutating, returned "the unchanged current
    tip". The tier now answers E5 one way, and this test is the behavioural
    half of that: the value must be the log's REAL tip (not a hardcoded
    `None`, which the empty-log tests above cannot tell apart), and it must
    be unchanged by the call, because flipping an in-memory
    `CompactionSettings` field appends nothing.
    """
    tip = real_session.session_log.append_message(
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    )
    assert real_session.session_log.cursor == tip
    entries_before = len(real_session.session_log.entries())

    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_auto_compaction", "params": {"enabled": True}}
    )

    (response,) = [item for item in await _drain(real_handler) if item.get("id") == 1]
    assert response["result"]["cursor"] == tip
    # Unchanged because nothing was written: the log holds exactly the
    # entries it held before the call.
    assert real_session.session_log.cursor == tip
    assert len(real_session.session_log.entries()) == entries_before


class _CursorReadWatcher(InMemorySessionLog):
    """An ``InMemorySessionLog`` that records, for every read of ``cursor``,
    whether the session's ``turn_lock`` was held at the time."""

    def __init__(self, lock: asyncio.Lock) -> None:
        super().__init__()
        self._lock = lock
        self.reads_while_locked: list[bool] = []

    @property
    def cursor(self) -> str | None:
        self.reads_while_locked.append(self._lock.locked())
        return super().cursor


async def test_set_auto_compaction_reads_the_cursor_under_the_same_guard_as_the_mutation(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    """This verb reads the cursor while D-1's ``turn_safety_guard`` is still
    HELD, so the tip it reports is the tip as of the moment the setting took
    effect — not one re-read after the lock was handed to a turn that may
    have appended in between.

    Scope, stated rather than implied: E5 (rule 1 of commands.py's "E5 in
    Tier B") settles that a mutator's completion CARRIES a cursor, not where
    it is read. Placement is still per-verb across the tier — ``set_model``
    also reads under its guard, while ``set_session_name`` and ``compact``
    build their completions after releasing it (``compact`` necessarily: its
    payload is assembled in the background task once ``compact()`` has
    returned). This test pins THIS verb's choice, which nothing forced and
    which a later edit could silently undo: the response bytes are identical
    either way, so only an observer of WHEN the read happens can see it.
    """
    watcher = _CursorReadWatcher(real_session.turn_lock)
    real_session.session_log = watcher  # type: ignore[assignment]

    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_auto_compaction", "params": {"enabled": True}}
    )

    (response,) = [item for item in await _drain(real_handler) if item.get("id") == 1]
    assert response["result"]["enabled"] is True
    assert watcher.reads_while_locked == [True], (
        "the cursor this verb reports was read outside the D-1 guard "
        f"(reads: {watcher.reads_while_locked})"
    )


async def test_set_auto_compaction_rejects_a_missing_enabled(real_handler: RPCHandler) -> None:
    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_auto_compaction", "params": {}}
    )
    (response,) = await _drain(real_handler)
    assert response["error"]["code"] == -32602
    assert "enabled" in response["error"]["message"]


async def test_set_auto_compaction_rejects_a_non_boolean_enabled(real_handler: RPCHandler) -> None:
    await real_handler._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "set_auto_compaction",
            "params": {"enabled": "yes"},
        }
    )
    (response,) = await _drain(real_handler)
    assert response["error"]["code"] == -32602


async def test_set_auto_compaction_refuses_while_a_turn_is_in_flight(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    """D-1: the same ``TURN_STILL_RUNNING`` refusal every mutating Tier B verb
    takes — proven by holding ``turn_lock`` exactly as
    ``test_rpc_tier_b_scaffolding.py`` does for the bare helper, now through
    the actual verb dispatch.
    """
    await real_session.turn_lock.acquire()
    try:
        await real_handler._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "set_auto_compaction",
                "params": {"enabled": True},
            }
        )
        (response,) = await _drain(real_handler)
        assert response["error"]["code"] == TURN_STILL_RUNNING
    finally:
        real_session.turn_lock.release()
    # Refused, not applied — the setting is untouched by the timed-out call.
    assert real_session._compaction_settings.enabled is False


async def _drain(handler: RPCHandler) -> list[dict[str, Any]]:
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out


# ── the tier's most interesting test: enabling it actually compacts ────────


async def test_enabling_over_the_wire_makes_a_real_turn_actually_compact(
    real_handler: RPCHandler, real_session: AgentSession
) -> None:
    """RPC-TIER-B.md D-4's task for B4, in full: prove ``set_auto_compaction``
    is not a parity checkbox but the only route to a capability RPC mode
    cannot otherwise reach (§1.1) — enabling it makes the NEXT turn actually
    compact, not merely toggles a flag nothing reads.
    """
    log = real_session.session_log
    for i in range(3):
        log.append_message(_msg("user", f"seed user {i}"))
        log.append_message(_msg("assistant", f"seed assistant {i}"))
    assert not any(e.get("type") == "compaction" for e in log.entries())
    pre_turn_cursor = log.cursor

    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_auto_compaction", "params": {"enabled": True}}
    )
    (enable_response,) = [item for item in await _drain(real_handler) if item.get("id") == 1]
    assert enable_response["result"] == {
        "enabled": True,
        # E5: the tip at the moment the setting took effect — the seeded log's
        # own leaf, since enabling appends nothing (commands.py "E5 in Tier B").
        "cursor": pre_turn_cursor,
        "method": "set_auto_compaction",
    }

    # A big, honest Usage.total_tokens (5000) against context_window=1000,
    # reserve_tokens=100 — should_compact's threshold (context_window -
    # reserve_tokens = 900) is comfortably exceeded by the turn's OWN
    # reported usage, the only thing estimate_context_tokens anchors on once
    # this turn's assistant message lands (see `_assistant`'s docstring).
    async def _fast_stream_simple(model: Any, context: Any, options: Any = None) -> _Stream:
        return _Stream("turn reply", total_tokens=5000)

    with (
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple),
        patch("tau_agent_core.compaction.complete_simple", side_effect=_summary_response),
    ):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": "hello"}}
        )
        ack = await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)
        assert ack["result"]["accepted"] is True
        submission_id = ack["result"]["submission_id"]
        assert submission_id is not None

        items = await _drain_until_two_agent_ends(real_handler)

    # A genuine compaction happened — not just the event brackets around a
    # no-op (§1.2's "consulted, never obeyed-if-convenient" for a POLICY does
    # not apply here at all — D-4: no RPC session ever carries one — but the
    # SAME discipline of proving the real effect, not just the signal, does).
    compactions = [e for e in log.entries() if e.get("type") == "compaction"]
    assert len(compactions) == 1

    agent_starts = _events_of_type(items, "agent_start")
    agent_ends = _events_of_type(items, "agent_end")
    assert len(agent_starts) == 2
    assert len(agent_ends) == 2

    turn_start, orphan_start = agent_starts
    turn_end, orphan_end = agent_ends

    # The turn's OWN pair carries this call's submission_id (`_emit_stamped`).
    assert turn_start["params"]["submission_id"] == submission_id
    assert turn_end["params"]["submission_id"] == submission_id

    # _maybe_auto_compact's pair (agent_session.py:3286-3291) goes straight
    # through `self._events.emit`, not `_emit_stamped` — D-4's documented
    # gap: a host correlating events to submission_id sees an ORPHAN pair it
    # cannot attribute to anything it asked for.
    assert orphan_start["params"]["submission_id"] is None
    assert orphan_end["params"]["submission_id"] is None

    # Stamp both agent_ends the way the real transport does, at DEQUEUE
    # (RPCHandler.prepare_outbound / _stamp_agent_end_cursor) — raw queue
    # items carry `_cursor_log`, not yet a `cursor` field, until this runs.
    # Both items are stamped here, together, well after the whole background
    # turn (including the auto-compaction it triggered) has already finished
    # — unlike the real writer, which calls `prepare_outbound` on each item
    # as it is individually dequeued, interleaved with the still-running
    # turn. That timing difference is why this test does NOT assert
    # `orphan_end`'s cursor differs from `turn_end`'s (both would read the
    # SAME final value here, deterministically, which is a fact about when
    # THIS test chose to stamp them, not about the mechanism); the
    # dequeue-time / interleaved-with-a-live-turn half of `_stamp_agent_end
    # _cursor`'s contract is already `test_agent_end_wire_event_carries_the
    # _post_persistence_cursor` (test_rpc.py) — B1, phase-2 review — and is
    # not re-proven here.
    real_handler.prepare_outbound(turn_end)
    real_handler.prepare_outbound(orphan_end)

    # Despite carrying no submission_id, the orphan agent_end DOES carry a
    # cursor (every agent_end is stamped at dequeue regardless of
    # provenance) — F3: a host that never caches "the tip" and instead reads
    # cursor off every agent_end stays correct across a compaction it never
    # explicitly asked for, even though submission_id alone cannot explain
    # why its context just shrank. Compared against the cursor from BEFORE
    # this whole exchange started (deterministic, unlike comparing it to
    # `turn_end`'s — see above) to prove it really moved, past both the
    # turn's own persistence AND the compaction's.
    assert orphan_end["params"]["cursor"] is not None
    assert orphan_end["params"]["cursor"] == log.cursor
    assert orphan_end["params"]["cursor"] != pre_turn_cursor
