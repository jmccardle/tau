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
independently reviewable field list, including the fields (``delta``,
``block_type``, ``replace``, ``message_count``) that E1/E2 require as
*replacements* for excluded unbounded fields, not merely deletions of them.

Excluded from :class:`WireEvent`, each because it is unbounded on a stream a
host cannot backpressure — ``message``, ``args``, ``result``,
``tool_results``, ``messages``, ``details``, ``text``. All of them are
reachable by PULL instead: ``get_messages`` returns whole message dicts, and a
``toolResult`` message carries the same ``details`` value the
``tool_execution_end`` event does. ``details`` is on this list rather than on
the wire because ``edit`` puts a whole diff in it, and ``text`` because a
``side_completion_end`` carries a whole summary — which lands in the log as a
``compaction`` or ``branch_summary`` entry that ``get_entry`` serves.

``usage`` is excluded for a different reason and is the one field here that is
not unbounded: a side completion's spend is already ON the entry it produced
(``summary_usage``), so putting it on the event too would give a host two
sources for one number and no rule for which wins.

Three BOUNDED facts are lifted out of the excluded ``message`` and given fields
of their own: ``stop_reason``, ``dropped_tool_calls`` and ``reasoning_level``. Excluding a whole
message is about size, and a closed enum and a small integer are neither
unbounded nor pullable in time to matter — a host that learns from
``get_messages`` that the answer it already rendered was a truncated prefix
learns it too late.

``dropped`` is the one unbounded field ON the wire: the followUp texts a user
abort discarded were never written anywhere, so there is nothing to pull
(docs/TURN-SETTINGS.md §5).

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

from tau_agent_core.events import (
    AgentEndReason,
    AgentEvent,
    ErrorDetail,
    SideCompletionPurpose,
    SideCompletionReason,
    SuspensionCloseReason,
)
from tau_agent_core.submission import SubmissionSource


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
        "side_completion_start",
        "side_completion_update",
        "side_completion_end",
        "provider_retry",
        "follow_ups_dropped",
        "suspension_open",
        "suspension_closed",
        "lock_changed",
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
    error_detail: ErrorDetail | None = Field(
        default=None,
        description="`error` as fields, set exactly when `error` is (agent_end, "
        "provider_retry): `type` (the exception class name, always set), "
        "`status` (the HTTP status of a refusal), `retry_after` (seconds the "
        "server asked for), `headers` (the refusal's response headers, "
        "lowercased, set-cookie removed) and `retryable` (true for a transient "
        "failure — 408/429/502/503/504/529 or a connection failure — false for "
        "any other refusal, null when τ makes no judgement). Each is null when "
        "not known. See docs/RESPONSE-HEADERS.md §5.",
    )
    response_headers: dict[str, str] | None = Field(
        default=None,
        description="The HTTP response headers of the completion this "
        "message_end closes, names lowercased, set-cookie removed. Set on the "
        "message_end that carries stop_reason; null there when the transport did "
        "not capture them, {} when the server sent none. τ parses none of them: "
        "rate-limit counters differ per vendor. None for all other event types. "
        "See docs/RESPONSE-HEADERS.md §5.",
    )
    end_reason: AgentEndReason | None = Field(
        default=None,
        description="How an agent_end closed: 'done' (the model had nothing more "
        "to say), 'terminate' (a tool asked to stop), 'aborted', 'max_turns' "
        "(the ceiling truncated the run), 'repeat_tool_calls' (the loop stopped "
        "itself because the model kept repeating an identical, wholly-failing "
        "batch), 'error' or 'suspended' (the turn unwound at a suspension and "
        "resumes from the tree). None on every other event type. `error` says whether "
        "the loop raised; this says how it stopped when it did not, which is what "
        "tells a host that an answer is TRUNCATED rather than finished.",
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
    parent_tool_call_id: str | None = Field(
        default=None,
        description="On a tool_execution_* event for a call a tool made, the "
        "calling tool's call id; None for a call the model made. Experimental.",
    )
    cursor_id: str | None = Field(
        default=None,
        description="The cursor whose turn emitted this event (docs/CURSORS.md "
        "§6). Several cursors run turns on one session at once; a host routes "
        "by this. None outside a turn.",
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
        description="One text fragment that arrived. On message_update (E1) it "
        "is a diffable content-block's prefix-diff against the previous "
        "message_update in the same turn, never the cumulative message; only a "
        "diffable block kind sets it (see block_type), and a non-diffable block "
        "change (e.g. a growing toolCall) produces no wire event. On "
        "side_completion_update it is the next fragment of the summary. Both "
        "are applied the same way, which is why they share a field rather than "
        "asking a client to keep two accumulators — see `replace`. None for all "
        "other event types.",
    )
    block_type: Literal["text", "thinking"] | None = Field(
        default=None,
        description="Which diffable content-block kind `delta` belongs to. Set "
        "exactly when `delta` is set — 'text' on a side_completion_update, which "
        "has no other kind.",
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
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = Field(
        default=None,
        description="Why the model stopped this completion, on the message_end "
        "that carries usage. 'length' means the output cap ended it, so the "
        "content is a PREFIX and not an answer — the one value an operator has "
        "to act on. None on the content-only duplicate message_end (which "
        "carries no usage either) and on every other event type. This rides a "
        "field of its own because the message it belongs to is excluded from "
        "the wire; it is a closed enum, not unbounded content. See "
        "docs/TRUNCATED-TOOL-CALLS.md.",
    )
    dropped_tool_calls: int | None = Field(
        default=None,
        description="How many tool calls in this completion were not run, on "
        "message_end. A completion cut off at the output cap runs none of its "
        "calls: each stays in the message with its partial arguments and gets "
        "an error result, or is dropped when nothing of it decodes; an aborted "
        "stream drops a call whose arguments were mid-flight. Null rather than "
        "0 when every call ran, so 'none lost' and 'not reported' stay "
        "distinguishable. None for all other event types. See "
        "docs/TRUNCATED-TOOL-CALLS.md §3.2.",
    )
    reasoning_level: Literal["off", "minimal", "low", "medium", "high", "xhigh"] | None = Field(
        default=None,
        description="The thinking level this completion was requested with, after "
        "the provider clamped it to what the model supports, on the message_end "
        "that carries usage. Null when no reasoning control was sent — no level "
        "was set, or the model does not declare reasoning — so a requested level "
        "that was clamped or not sent is visible rather than silent. None for all "
        "other event types. See docs/REASONING-WIRE.md §3.",
    )
    cache_notice: str | None = Field(
        default=None,
        description="One sentence saying this turn's prompt cache should have "
        "been read and was not, on agent_end. Null is the normal case and says "
        "nothing was observed: the cache was read, the server accounts for no "
        "cache, the prompt is under the minimum cacheable prefix, or a read "
        "earlier in this session already proved caching is on. A host renders "
        "it as a warning; see docs/PROMPT-CACHING.md §7 for the three gates. "
        "None for all other event types.",
    )
    purpose: SideCompletionPurpose | None = Field(
        default=None,
        description="Which side completion a side_completion_* event reports: "
        "'compaction' or 'branch_summary'. Side work spends tokens and produces "
        "text outside any turn, so it stamps no submission_id and a host keys on "
        "this instead. The summary TEXT and what it cost are deliberately not on "
        "the wire — both land in the session log as a compaction or "
        "branch_summary entry, which get_entry serves with tokens_before, "
        "covered_tokens and summary_usage attached. Pushing unbounded content "
        "through an event is what WireEvent exists to avoid. None for all other "
        "event types. See docs/STREAMING-SIDE-WORK.md.",
    )
    reason: SideCompletionReason | SuspensionCloseReason | None = Field(
        default=None,
        description="What asked for a side completion: 'manual' (a person or "
        "this host), 'threshold' (the auto-trigger, which nobody asked for) or "
        "'navigate' (the tree browser's summarising move). On all three "
        "side_completion_* events, so a host that attached mid-summary still "
        "learns whether the work was requested or imposed. On suspension_closed, "
        "why the suspension closed: 'answered', 'aborted' (its turn's abort, an "
        "`abort` answer to an earlier one included), 'cursor_closed' (the session "
        "shut down) or 'abandoned' (its task was cancelled). None for all other "
        "event types.",
    )
    attempt: int | None = Field(
        default=None,
        description="Which retry a provider_retry announces, counting from 1. A "
        "provider_retry says a request failed before producing any output and "
        "will be sent again, byte-identical, after delay_seconds; `error` is the "
        "failure. A host renders it as a passing warning: if the retries run "
        "out, the turn's agent_end carries the final error as usual. None for "
        "all other event types. See docs/PROVIDER-RETRY.md.",
    )
    max_retries: int | None = Field(
        default=None,
        description="The most retries that request will make, on provider_retry. "
        "None for all other event types.",
    )
    delay_seconds: float | None = Field(
        default=None,
        description="Seconds τ waits before re-sending, on provider_retry: the "
        "server's Retry-After when it sent one, else an exponential backoff. "
        "None for all other event types.",
    )
    dropped: list[str] | None = Field(
        default=None,
        description="The texts a user abort discarded, on follow_ups_dropped: the "
        "queued followUps in queue order, then the enqueue submissions that were "
        "waiting for the turn, in submission order. They were never written to the session, "
        "so this is the one field here that carries content no verb can pull "
        "afterwards. None for all other event types. See docs/TURN-SETTINGS.md "
        "§5 and §5.1.",
    )
    suspension: dict[str, Any] | None = Field(
        default=None,
        description="On suspension_open and suspension_closed, the suspension "
        "(SuspensionState): `id` (what answer_suspension names), `session_id`, "
        "`cursor_id`, `parent_cursor_id`, `depth` (owner-chain length), "
        "`spawned_from` (a child cursor's origin entry), `spec` ({title, body, "
        "fields, actions, evidence, targetId}), `site`, `extension`, "
        "`target_id`, `call` ({id, effective_args} at a tool_call), `entry_id` "
        "(at an entry_written), `queued` and `opened_at_ms`. suspension_open is "
        "sent again with `queued` false when a queued one reaches the front of "
        "its cursor's queue. See docs/STEP-DEBUGGER.md §3.4. None for all other "
        "event types.",
    )
    resolution: dict[str, Any] | None = Field(
        default=None,
        description="On suspension_closed, how it was resolved: {id, action, "
        "values, answered_by, at_ms}; `answered_by` is null when the turn's "
        "abort resolved it. None for all other event types.",
    )
    read_only: bool | None = Field(
        default=None,
        description="On lock_changed, whether this process may no longer write "
        "the session: another process holds its write lock. None for all other "
        "event types.",
    )
    lock_holder: dict[str, Any] | None = Field(
        default=None,
        description="On lock_changed, the process holding the session's write "
        "lock ({pid, host, head, started, detail}), or null. None for all other "
        "event types.",
    )
    leaf: str | None = Field(
        default=None,
        description="The emitting cursor's resulting leaf, on agent_end (E5/F3). "
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
