"""The EVENT half of the RPC capability document, generated from ``AgentEvent``.

Reference: docs/REMOTE-CONTROL.md §6 recommendation item 3 ("Generate the
event half"); K1/K3 (block [8], the capability document); E1/E2/E3 (block
[4], the event stream).

§6 appraises and REJECTS deriving the RPC surface by decorating
``AgentSession`` and recommends "audit, don't generate" for the COMMAND half
(hand-written table, introspective audit test — that is a *different* unit,
not this one). The EVENT half is the explicit carve-out: ``AgentEvent``
(``tau_agent_core/events.py``) is already a pydantic model whose ``type``
field is a closed ``Literal`` union, the mapping to the wire is genuinely
1:1 "modulo the E1/E2 projections" (§6 item 3's own words), and pydantic
emits JSON Schema directly. This module is that generator.

Status — as of unit 2B (docs/REMOTE-CONTROL.md §4 block [4]), this schema is
the wire ``rpc/handler.py`` actually sends: ``rpc/wire_events.py`` builds a
:class:`WireEvent` instance for every outbound event (never a hand-rolled
dict shaped to match it by hand) and serializes THAT, so drift between "what
:class:`WireEvent` declares" and "what goes on the wire" would be a type
error at construction, not a silent divergence. Before 2B, the handler's old
``_serialize_event`` predated this design doc and emitted a different shape
entirely (whole ``message``, raw ``args``/``result``, unbounded
``tool_results``/``messages``, no ``turn_index``/``blocked``/``blocked_by``);
that code is gone.

D3 draws the one distinction that matters here: *"The wire event schema is a
projection of ``AgentEvent``, not ``AgentEvent`` itself. It may lag, and
adding an internal field must not change the wire without a version bump."*
So this module does not call ``AgentEvent.model_json_schema()`` and hand the
result out — that would make every future internal field on ``AgentEvent`` a
silent wire change. It declares :class:`WireEvent`, an explicit,
independently reviewable field list — see the comment above the class for
the field-by-field justification, including the fields (``delta``,
``block_type``, ``replace``, ``message_count``) that E1/E2 require as
*replacements* for excluded unbounded fields, not merely deletions of them.

E3 (additive and versioned): every field below is optional/defaulted and
:class:`WireEvent` does not set ``model_config["extra"] = "forbid"`` — a
client MUST ignore a field it does not recognize, so this module (and any
future edit to it) may only ever ADD fields, never repurpose an existing
name for a new meaning.

Contract: pure, no I/O, no side effects at import. Everything here is either
a ``typing`` introspection over an already-imported model or a call into
pydantic's own (side-effect-free) schema generation.
"""

from __future__ import annotations

import typing
from typing import Any, Literal

from pydantic import BaseModel, Field

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import SubmissionSource

# ---------------------------------------------------------------------------
# WireEvent field-by-field justification, checked against AgentEvent
# (tau_agent_core/events.py). This lives here, as a source comment, rather
# than in WireEvent's docstring, because a pydantic model's docstring is
# copied verbatim into its JSON Schema's "description" — and that schema is
# wire payload (K1/K3), not contributor documentation. Keep this comment in
# sync with the field list below; it is not machine-checked.
#
# INCLUDED, 1:1 with AgentEvent —
#
# - type — the event discriminator; the entire reason a capability document
#   enumerates events[] (K1). Bounded: one of ten literal strings. This
#   Literal is a deliberate, hand-maintained *copy* of AgentEvent.type, not a
#   re-export of the same annotation object — see the anti-drift test suite
#   (tests/test_rpc_event_schema.py) for why that duplication is the point.
# - timestamp — ordering/observability a remote host cannot reconstruct
#   locally. Bounded: an int.
# - turn_index — which turn an event belongs to. Bounded: an int or None.
# - tool_call_id — correlates tool_execution_* events to the call that
#   produced them. Bounded: a string or None.
# - tool_name — names the tool a tool_execution_* event concerns. Bounded: a
#   string or None.
# - is_error — bounded boolean a host renders on.
# - error — WHY an agent_end closed when the loop raised rather than
#   finishing (AgentEvent's own docstring: "Without it 'the agent finished'
#   and 'the agent died mid-turn' are the same event"). Bounded: a string or
#   None, always paired with is_error=True when set. Phase-2 review B3: this
#   field existed on AgentEvent since before this module was written but was
#   never added here — neither projected nor declared EXCLUDED — so it fell
#   through both classifications this comment otherwise accounts for every
#   field by. See tests/test_rpc_event_schema.py's TestNoFieldSilentlyDropped
#   for the anti-drift check that closes the hole that let this happen once.
# - blocked — the S50 extension-veto flag, a *distinct* presentation from a
#   generic error per the events.py docstring. Bounded boolean.
# - blocked_by — names the vetoing extension, paired with blocked. Bounded:
#   a string or None.
# - submission_id, source, submitter, correlation — E4's provenance quad
#   (docs/REMOTE-CONTROL.md §4[4] E4, §2 G6): "every event carries the
#   submission provenance quad when a submission drove it, and null when
#   none did." Copied straight through from AgentEvent, never defaulted or
#   synthesized on this side — events.py:62-77 already states the rule this
#   projection must not weaken ("an honest 'no submission', never a
#   fabricated id"), and AgentEvent itself guarantees the four stay None
#   together. `source` reuses `SubmissionSource` (tau_agent_core.submission)
#   verbatim rather than a hand-copied Literal — unlike `type` above, this
#   is a value vocabulary the submission layer owns, not a wire-specific
#   commitment worth a second, driftable copy.
#
# EXCLUDED, with reason —
#
# - args — tool-call arguments are shaped by the *tool's* own schema, not
#   AgentEvent's contract, and unbounded in the general case (G3). Excluded
#   rather than guessed at. No replacement declared; there is no bounded
#   summary of "what arguments were" the way a delta or a count summarizes
#   text or a list.
# - result — Any-typed tool result: unbounded and untyped by construction,
#   no honest JSON Schema can describe Any. Excluded (G3).
# - tool_results — "list of tool result messages (turn_end)": an unbounded
#   list, the same shape E2 rules out for agent_end's messages. E2's text
#   names agent_end specifically ("agent_end announces completion and
#   carries counts"); it does not say turn_end's tool_results gets a count
#   too. Excluded with NO replacement declared here, on purpose — inventing
#   a `tool_result_count` would be guessing at a requirement REMOTE-CONTROL.md
#   does not state, which this module's own contract (declare only what is
#   asked for, or say you can't) rules out. Flagged for a future unit to
#   raise with the doc's owner if turn_end truly needs one.
#
# EXCLUDED, but REPLACED by a bounded field (E1/E2) —
#
# - message — a whole-message content block, unbounded in the general case
#   (a long assistant turn). E1: "message_update carries a delta, never the
#   cumulative message ... The prefix-diff tau's TUI already runs
#   (backends.py:192-199) MOVES INTO THE PROJECTION." So the *whole message*
#   is excluded, and `delta`/`block_type`/`replace` (below) are declared in
#   its place. `rpc/wire_events.py` (unit 2B) is the stateful, per-connection
#   transform that computes their VALUES for a live event — this module
#   stays pure and only declares the shape.
#   - delta — the diffable block's incremental suffix (or, when `replace` is
#     True, its entire new value — see `replace` below). Only set on
#     `message_update`, and only for a DIFFABLE block (`text`/`thinking`,
#     tau_agent_core.event_projection._DIFFABLE_FIELD); a non-diffable block
#     change (e.g. `toolCall`) produces no wire event at all (see
#     `rpc/wire_events.py`'s module docstring for why — G3, the same
#     unbounded-passthrough problem `args`/`result` above are excluded for).
#     None for every other event type.
#   - block_type — which diffable content-block kind `delta` belongs to,
#     mirroring `event_projection.BlockDelta.type` restricted to the kinds
#     that ever populate `delta`. A hand-copied Literal, not a shared alias
#     (`event_projection._DIFFABLE_FIELD`'s key set is a plain dict, not a
#     `typing.Literal`, so there is nothing to import) — kept in sync by
#     `TestBlockTypeMatchesDiffableFields` in tests/test_rpc_event_schema.py,
#     the same anti-drift idiom `type` uses against `AgentEvent.type`.
#     Without this a client cannot tell an answer-text delta from a
#     reasoning delta, which matters for R-T6: concatenating deltas of BOTH
#     kinds without discriminating would corrupt "the final assistant text."
#   - replace — mirrors `event_projection.BlockDelta.replace` exactly: False
#     (default) means `delta` is a suffix to append; True means the provider
#     replaced rather than extended the block and `delta` is the block's
#     WHOLE new value, so the receiver must reset its accumulator instead of
#     appending. Declaring `delta` without this flag would silently corrupt
#     reconstruction on the one path `event_projection`'s own docstring calls
#     out as "the defensive case" — see BlockDelta.replace's docstring.
# - messages — "list of messages produced (agent_end)": E2 by name — "the
#   message array is *pulled* via get_messages", not pushed. Excluded, and
#   `message_count` (below) is declared in its place, per E2's "agent_end
#   announces completion and carries counts." `rpc/wire_events.py` sets it
#   to `len(event.messages)` on `agent_end` (event.messages is never None on
#   that path — AgentLoop._emit_agent_end always passes a list, possibly
#   empty); None for every other event type.
#
# ADDED, not a projection of any AgentEvent field —
#
# - cursor — E5/F3 (docs/REMOTE-CONTROL.md §4[4] E5, §7.2 F3: "no host may
#   cache 'the tip'"), added by phase-2 review B1. AgentEvent carries no
#   cursor at all (the session log is AgentSession's concern, not the loop's);
#   this field exists so a MUTATING verb's result — `abort`, `submit`,
#   `prompt` — has a place to learn the resulting cursor from once the
#   mutation has genuinely happened, rather than a response built at signal
#   time guessing at a tip that has not moved yet. Only ever set on
#   `agent_end` (the one point every turn a submission may run — including
#   one `abort()` cut short — has finished unwinding and persisting), and
#   ONLY there: `rpc/transport.py`'s writer fills it in immediately before
#   serializing the `agent_end` line, not `rpc/wire_events.py` at event-
#   projection time (see that module's docstring for why "at projection
#   time" is exactly the same stale-tip bug `abort`'s old cursor had —
#   persistence happens strictly AFTER `agent_end` fires, not before). None
#   for every other event type.
#
# Per E3, fields may be *added* to this projection later without breaking
# existing clients (clients MUST ignore unknown fields); this model does not
# set model_config["extra"] = "forbid" for that reason — an absent/renamed
# field is a wire break to catch by test, an *added* one is supposed to be
# safe to ignore.
# ---------------------------------------------------------------------------


class WireEvent(BaseModel):
    """The wire projection of ``AgentEvent`` (D3) — REMOTE-CONTROL.md's
    designed shape, and (as of unit 2B) what ``rpc/handler.py`` actually
    sends: ``rpc/wire_events.py`` constructs instances of this class rather
    than a hand-shaped dict. See the module docstring's Status note and the
    field-by-field comment above this class.
    """

    type: Literal[
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
    ] = Field(description="Event type discriminator.")
    timestamp: int = Field(ge=0, description="Milliseconds since epoch.")
    turn_index: int | None = Field(default=None, description="Turn number (turn_*).")
    tool_call_id: str | None = Field(default=None, description="Tool call id (tool_*).")
    tool_name: str | None = Field(default=None, description="Tool name (tool_*).")
    is_error: bool = Field(default=False, description="Whether this event represents an error.")
    error: str | None = Field(
        default=None,
        description="Why an agent_end closed when the loop raised rather than "
        "finishing (e.g. 'RuntimeError: Connection refused'). None on a normal "
        "close; always paired with is_error=True when set. Without it 'the "
        "agent finished' and 'the agent died mid-turn' are the same event on "
        "the wire.",
    )
    blocked: bool = Field(
        default=False,
        description="Whether a tool_execution_end is an extension veto (S50), "
        "distinct from a generic errored result.",
    )
    blocked_by: str | None = Field(
        default=None,
        description="The extension that vetoed the call; paired with blocked.",
    )
    submission_id: str | None = Field(
        default=None,
        description="The Submission that drove this turn, if any (E4/G6). None "
        "for an event from a call that never went through submit()/prompt() — "
        "never a fabricated id.",
    )
    source: SubmissionSource | None = Field(
        default=None,
        description="The submission's origin (E4). None alongside submission_id.",
    )
    submitter: str | None = Field(
        default=None,
        description="WHO submitted (E4). None alongside submission_id.",
    )
    correlation: dict[str, Any] | None = Field(
        default=None,
        description="The submission's free-form origin detail (E4). None alongside "
        "submission_id — an empty dict would claim a submission with no "
        "correlation data, which is a different statement.",
    )
    delta: str | None = Field(
        default=None,
        description="A diffable content-block's delta on message_update (E1) — "
        "the prefix-diff against the previous message_update in the same turn, "
        "never the cumulative message. Only set for a diffable block kind (see "
        "block_type); a non-diffable block change (e.g. a growing toolCall) "
        "produces no wire event. None for all other event types. See `replace` "
        "for how to apply this value.",
    )
    block_type: Literal["text", "thinking"] | None = Field(
        default=None,
        description="Which diffable content-block kind `delta` belongs to. Set "
        "exactly when `delta` is set.",
    )
    replace: bool = Field(
        default=False,
        description="Only meaningful when delta is set. False (the common case): "
        "delta is an incremental suffix — append it to whatever was already "
        "accumulated for this block_type this turn. True: the provider replaced "
        "rather than extended the block's content — delta is the block's ENTIRE "
        "new value, and the receiver must RESET its accumulator to delta rather "
        "than appending. Mirrors event_projection.BlockDelta.replace exactly.",
    )
    message_count: int | None = Field(
        default=None,
        description="Count of messages produced this turn, on agent_end (E2). "
        "The messages themselves are pulled via get_messages, never pushed. "
        "None for all other event types.",
    )
    cursor: str | None = Field(
        default=None,
        description="The session log's resulting cursor, on agent_end (E5/F3). "
        "Filled in by rpc/transport.py's writer immediately before this line "
        "is serialized — not by rpc/wire_events.py at event-projection time — "
        "because persistence happens strictly AFTER agent_end fires; reading "
        "it any earlier reproduces the exact stale-tip bug this field exists "
        "to close. None for all other event types.",
    )


def event_types() -> tuple[str, ...]:
    """The ``AgentEvent.type`` Literal values, in declaration order.

    Reflective, not a hand-maintained copy: extracted via ``typing.get_args``
    directly from ``AgentEvent``'s own ``type`` field annotation, so this
    function cannot itself drift from ``AgentEvent`` — there is nothing here
    to forget to update. It is :class:`WireEvent`'s *separately declared*
    ``type`` Literal that can drift, and that is what the anti-drift test
    checks this function's output against.

    Raises:
        TypeError: if ``AgentEvent.type`` is ever widened off a ``Literal``
            (``typing.get_args`` then returns ``()``). Fail Early: an empty
            ``events[]`` is a well-formed but false capability document, and
            this module must not publish one silently.
    """
    annotation = AgentEvent.model_fields["type"].annotation
    args = typing.get_args(annotation)
    if not args:
        raise TypeError(
            "AgentEvent.type is no longer a typing.Literal (annotation="
            f"{annotation!r}); event_types() cannot enumerate event types "
            "from it. Refusing to publish an empty events[] capability list."
        )
    return args


def wire_event_schema() -> dict[str, Any]:
    """The JSON Schema for :class:`WireEvent`, the wire projection of ``AgentEvent``.

    Deterministic: pydantic's ``model_json_schema()`` walks ``model_fields``
    in declaration order and serializes into a plain ``dict`` of ``dict``/
    ``list`` structures — no ``set`` is consulted, so two calls in the same
    process produce byte-identical JSON.
    """
    return WireEvent.model_json_schema()


def event_capability_doc() -> dict[str, Any]:
    """The ``events``/``event_schema`` portion of the ``get_capabilities`` document.

    ``{"events": [...type names, in AgentEvent's declared order...],
    "event_schema": {...JSON Schema for WireEvent...}}``

    NOT IN SCOPE here (see docs/REMOTE-CONTROL.md §6 recommendation, and this
    unit's NOT IN SCOPE list): ``commands[]`` (hand-written, blocked on H1),
    ``declined[]``, ``ui_methods[]``, ``protocol_version`` — those belong to
    the command-table half and to the ``get_capabilities`` RPC method itself,
    neither of which this module wires up.
    """
    return {
        "events": list(event_types()),
        "event_schema": wire_event_schema(),
    }
