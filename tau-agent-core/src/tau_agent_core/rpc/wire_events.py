"""Projects one ``AgentEvent`` into zero or more wire ``WireEvent`` payloads.

Reference: docs/REMOTE-CONTROL.md §4 block [4] (E1/E2/E3/E4), §9 R-T6.

This is unit 2B's "wiring" — the module that connects two pieces that already
existed and were already tested in isolation, per the unit's own framing:

- ``tau_agent_core.event_projection.MessageDeltaProjector`` turns a stream of
  CUMULATIVE ``message_update`` snapshots into bounded per-chunk deltas (E1).
  Its only prior consumer was the TUI (``tau_coding_agent.backends
  .TurnStream``); this module is the second, and does not modify it.
- ``tau_agent_core.rpc_event_schema.WireEvent`` is the declared wire shape.
  This module builds actual :class:`~tau_agent_core.rpc_event_schema.WireEvent`
  INSTANCES (never a hand-shaped dict merely resembling one) and serializes
  those — so "what goes on the wire" and "what WireEvent declares" cannot
  drift apart without pydantic raising at construction.

Neither of those two modules is changed here (their own module docstrings ask
that a believed bug be reported rather than fixed in place, since each has a
second consumer / a merge history worth respecting).

**Non-diffable content blocks are dropped from the wire entirely.** A
``toolCall`` block passed through by ``MessageDeltaProjector`` (its
``BlockDelta.block``, whole, on every change — see that class's docstring
for why: it is O(n^2) in the block's final size, a stated, deliberate
limitation of the projector) has no home in :class:`WireEvent` — ``delta`` is
a ``str`` and there is no companion field for an arbitrary block payload,
matching G3 ("nothing unbounded is ever pushed") and the schema's own
EXCLUDED-with-reason treatment of ``args``/``result``. This mirrors (but does
not depend on) the TUI's ``TurnStream._feed_message_update``, which drops the
same passthrough deltas for the same underlying reason: tool identity/name
already rides ``tool_execution_start``/``_end``, and full arguments are
available by pulling ``get_messages`` (E2). Unlike the TUI, this module is a
new consumer with no legacy ``block_delta.replace``-ignoring bug to preserve
(see ``TurnStream``'s own comment) — the diffable path below applies
``replace`` correctly.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.event_projection import MessageDeltaProjector
from tau_agent_core.events import AgentEvent
from tau_agent_core.rpc_event_schema import WireEvent


def project_event(projector: MessageDeltaProjector, event: AgentEvent) -> list[dict[str, Any]]:
    """Turn one ``AgentEvent`` into the wire payload(s) it produces.

    Returns a LIST because ``message_update`` is not 1:1: a single incoming
    event may project into zero deltas (an unchanged block re-sent, or a
    ``toolCall``-only change — dropped, see module docstring), one delta (the
    overwhelmingly common case — a single text or thinking chunk), or more
    than one (``MessageDeltaProjector.project`` documents this: a non-diffable
    kind "CAN appear more than once at once", and nothing rules out a single
    incoming snapshot changing both a diffable and a non-diffable block in
    the same call). Every other event type always projects to exactly one
    payload.

    ``turn_start`` resets ``projector`` before building its own payload — see
    ``RPCHandler.__init__``'s comment on the projector field for why
    resetting exactly here, on this one event type, is correct for this
    handler's subscription (a single, sequential turn stream) rather than a
    TUI-specific convenience being copied without re-justifying it.
    """
    if event.type == "turn_start":
        projector.reset()

    if event.type != "message_update":
        extra: dict[str, Any] = {}
        if event.type == "agent_end":
            # E2: agent_end carries a COUNT, never the message array itself
            # (that is get_messages's job). event.messages is a list on every
            # real agent_end (AgentLoop._emit_agent_end always passes one,
            # possibly empty) — None only for a MagicMock/test double that
            # never set it, which len()-ing would crash on, so this stays
            # conditional rather than assuming the production invariant here.
            extra["message_count"] = len(event.messages) if event.messages is not None else None
        return [_wire_event(event, **extra).model_dump(mode="json")]

    if event.message is None:
        return []

    payloads: list[dict[str, Any]] = []
    for block_delta in projector.project(event.message):
        if block_delta.delta is None:
            # Non-diffable passthrough (today: toolCall) — deliberately
            # dropped. See module docstring.
            continue
        payloads.append(
            _wire_event(
                event,
                delta=block_delta.delta,
                block_type=block_delta.type,
                replace=block_delta.replace,
            ).model_dump(mode="json")
        )
    return payloads


def _wire_event(event: AgentEvent, **extra: Any) -> WireEvent:
    """Build a :class:`WireEvent` for ``event``'s bounded/provenance fields.

    Every field NOT reproduced here (``message``, ``args``, ``result``,
    ``tool_results``, ``messages``) is exactly the set ``WireEvent`` excludes
    (E1/E2/G3) — see ``rpc_event_schema.py``'s field-by-field comment.

    ``cursor`` is ALSO not set here, deliberately, and for a different reason
    than the exclusions above: it is not a projection of any ``AgentEvent``
    field at all (E5/F3, phase-2 review B1). This function runs synchronously
    inside ``AgentLoop._emit_agent_end``, strictly BEFORE
    ``AgentSession._run_one_turn`` persists the turn — reading the session
    log's cursor here would capture the PRE-persistence tip, the exact
    stale-tip bug B1 fixes. ``rpc/transport.py``'s writer
    (``_write_stdout`` → ``RPCHandler.prepare_outbound``, composed onto
    ``RPCHandler``) fills ``cursor`` in immediately before serializing an
    ``agent_end`` line instead — see that function for why that is late
    enough.
    """
    return WireEvent(
        type=event.type,
        timestamp=event.timestamp,
        turn_index=event.turn_index,
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        is_error=event.is_error,
        # B3: was silently dropped — neither projected nor declared EXCLUDED —
        # so an agent_end with is_error=True gave a host no way to learn WHY
        # (AgentEvent.error's own docstring: "the agent finished" and "the
        # agent died mid-turn" were the same event on the wire).
        error=event.error,
        # Same triage as `error` above, one field later: `error` says whether the
        # loop RAISED, `end_reason` says how it stopped when it did not. Without
        # it a host cannot tell a truncated run (max_turns, repeat_tool_calls)
        # from a finished one, which is the silence PLAN-0.9.4 §8 recorded.
        end_reason=event.end_reason,
        blocked=event.blocked,
        blocked_by=event.blocked_by,
        # E4/G6: copied straight through, never defaulted or fabricated —
        # events.py:62-77 states the "no submission -> None, never a
        # fabricated id" rule this wire must not weaken.
        submission_id=event.submission_id,
        source=event.source,
        submitter=event.submitter,
        correlation=event.correlation,
        **extra,
    )
