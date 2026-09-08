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
from tau_agent_core.truncation import dropped_tool_calls


def project_event(
    projector: MessageDeltaProjector,
    event: AgentEvent,
    *,
    cache_notice: str | None = None,
) -> list[dict[str, Any]]:
    """Turn one ``AgentEvent`` into the wire payload(s) it produces.

    ``cache_notice`` rides ``agent_end`` beside ``message_count``, and is the one
    field here that is not a projection of ``event``: the caller's
    :class:`~tau_agent_core.prompt_cache.PromptCacheObserver` computes it from
    the whole turn, which no single event carries. Ignored on every other type.

    ``message_end`` gains ``stop_reason`` and ``dropped_tool_calls``, which ARE
    projections — of the excluded ``message``, see :func:`_truncation_fields`.

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
            extra["message_count"] = len(event.messages) if event.messages is not None else None
            extra["cache_notice"] = cache_notice
        if event.type == "message_end":
            extra.update(_truncation_fields(event.message or {}))
        return [_wire_event(event, **extra).model_dump(mode="json")]

    if event.message is None:
        return []

    payloads: list[dict[str, Any]] = []
    for block_delta in projector.project(event.message):
        if block_delta.delta is None:
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


def _truncation_fields(message: dict[str, Any]) -> dict[str, Any]:
    """``stop_reason``/``dropped_tool_calls`` lifted out of an excluded ``message``.

    Reference: docs/TRUNCATED-TOOL-CALLS.md §3. Both are None on the content-only
    duplicate ``message_end``, which carries neither — the same absence the TUI's
    ``TurnStream`` reads to skip it.

    ``dropped_tool_calls`` is None rather than 0 when nothing was dropped, because
    the wire field distinguishes "none lost" from "not reported" and every
    ``message_end`` would otherwise claim the first.
    """
    dropped = dropped_tool_calls(message.get("usage") or {})
    return {
        "stop_reason": message.get("stop_reason"),
        "dropped_tool_calls": dropped or None,
    }


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
        error=event.error,
        end_reason=event.end_reason,
        blocked=event.blocked,
        blocked_by=event.blocked_by,
        submission_id=event.submission_id,
        source=event.source,
        submitter=event.submitter,
        correlation=event.correlation,
        **extra,
    )
