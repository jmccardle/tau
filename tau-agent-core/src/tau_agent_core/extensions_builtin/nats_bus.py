"""tau-007 — the tectum bus extension: τ speaks NATS, in both directions.

Reference: SIM_SPEC_v2.md §12.4; §16.6 (H7); §16.10 (H8); §13.7; §11.4; §12.3.
Wire contract: ``docs/WIRE-CONTRACT.md``.

**Rewritten 2026-07-29 against tectum's real implementation.** The first version
of this file was written when τ was the only end that existed, so its wire
format was a guess. Now that `~/Development/tectum` is readable, the guess is
known to have been wrong in five ways, all fixed here:

1. The wire format is a **TectumEvent envelope**, not a bare payload dict
   (``tectum/event.py:47``). ``TectumEvent.from_dict`` indexes ``event_id``,
   ``event_type``, ``source``, ``timestamp``, ``sequence_number``, ``ttl_ms``,
   ``payload`` and ``origin_node`` directly, so the old bare
   ``{"text": ..., "binding_id": ...}`` raised ``KeyError`` in every consumer.
2. The inbound subject has **no verb token** — ``subjects.agent_in()`` is
   ``events.workspace.<agent>.in`` (four tokens), so the old
   ``events.workspace.<ws>.in.>`` subscription (which needs five or more) could
   never match. Worse, the subject an agent is actually driven by is
   schema-dependent: in ``praxis/harness_text.yaml`` the responder is fed
   ``events.sensation.audio.resolved.clean``. It is therefore **config**, not a
   value this file may derive.
3. Ack subjects are **per-effector, in three different shapes**, and the "kind"
   token is not the verb: ``speak`` acks on
   ``events.action.speech.completed.<bid>`` (``effectors/speech.py:107``),
   ``journal_append`` on ``events.journal.append.<bid>`` — ``append``, not the
   tool name (``effectors/journal_append.py:48``) — and ``jmfts_write`` on
   ``events.journal.jmfts_write.<bid>``. See :data:`VERBS`.
4. There is **no ``status`` field** on an ack. Real acks are TectumEvents whose
   payload carries effector-specific data (``doc_id``; or ``backend``/``dsp``/
   ``ok`` for speech). Demanding ``status`` rejected every genuine ack.
5. ``binding_id`` is **preserved across hops**, not minted per publish
   (``event.py:23``). It correlates one logical flow, so an outbound effector
   event carries the *inbound* event's binding_id — which is also what makes
   the ack land on a subject this side can predict. tectum's shim does the same
   thing via ``TECTUM_BINDING_FILE`` ("per-turn binding re-stamp",
   ``tools.py:227``); :func:`register`'s ``state["binding_id"]`` is that stamp.

Why the envelope is re-implemented here rather than imported
--------------------------------------------------------------

τ does not import ``tectum.event``. τ is a standalone pip package; tectum is a
separate process, often on a separate machine. **The contract is the wire
format, not shared code** — the same reason tectum's own bash shim builds the
envelope inline with ``json.dumps`` rather than importing anything
(``tools.py:235-257``). :func:`_envelope` is that JSON object, field for field.

What this extension owns
-------------------------

**Inbound.** Subscribes the configured ``inbound_subject``, parses the
envelope, and drives one agent turn per event via
``api.submit(text, multitask_strategy="reject", correlation=…)`` — this is τ
*on the bus directly*, standing in as a tectum agent node rather than sitting
behind ``agent_pool``'s per-dispatch ``pi`` subprocess. It also still emits
``ext:nats_bus:inbound`` so a consumer that wants the event without the turn (a
test, a monitor) keeps that seam. Concurrency is the CORE's policy, not this
file's: ``"reject"`` is one implementation in ``AgentSession.submit``, and a
refused event is reported as ``ext:nats_bus:inbound_dropped`` carrying the
submission's own ``rejection_reason`` (docs/SUBMISSION-LIFECYCLE.md phase 5 —
this file used to hand-roll a ``turn_in_flight`` flag).

**Outbound.** Registers **one tool per verb** in ``verbs`` config, each with
**its own** schema (:attr:`VerbSpec.parameters`). The previous single generic
``workspace_effector`` tool made the model invent both the verb string and the
payload shape, and its own description named verbs (``'move'``, ``'say'``) that
do not exist.

Two producers, one wire (2026-08-01)
--------------------------------------

This file no longer talks only to tectum. :data:`VERBS` now spans two
independent consumers, and both were confirmed against running code rather than
a spec:

- **tectum's effector nodes** — ``speak``, ``journal_append``, ``jmfts_write``,
  ``delegate``.
- **McRogueFace's body node** (``robot_sim_stack/world/body_node.py``, a real
  NATS client running in a thread inside the engine process) — ``move_to``,
  ``wait``, ``note``.

Until this change every registered tool shared one ``{"text": string}`` schema,
hard-coded at the ``register_tool`` call and re-validated by hand in
``_execute``. A verb taking ``{x, y}`` was not merely unimplemented, it was
inexpressible — which is why the world half of the demo had never been
attempted. :attr:`VerbSpec.parameters` is that schema per verb, and the payload
projection is uniform: **the payload is the verb's arguments plus ``agent``**.

They also disagree about how an ack reports failure — tectum uses
``ok``/``error``, McRogueFace uses ``status: ok|refused|error`` — and reading
only the first dialect made this side blind to all three of the second's
values. See :func:`_ack_failure`; that one was a silent failure in the
optimistic direction, which is the class ``SIM_SPEC_v2.md`` §13.2 exists to
warn about.

Declared subjects (H7) and the capability preflight (H8)
---------------------------------------------------------

:data:`SUBJECTS` is the namespace this extension *type* operates in; a concrete
instance binds a configured subset. ``TOUCHES_BUS = True`` is checked by the
loader before ``register`` runs, so loading this into a session built with
``bus_available=False`` is refused rather than failing later at first use.

Config (``~/.tau/config.json`` → ``"extensions"``.``"nats_bus"``)
------------------------------------------------------------------

- ``workspace`` (**required**, no default): the agent identity this session
  publishes as — ``events.workspace.<workspace>.out.<verb>``.
- ``inbound_subject`` (**required**, no default): see point 2 above. Which
  subject drives this agent is a property of the active tectum schema, and a
  default here would bind silently to a subject nothing publishes on.
- ``draft_subject`` (optional, no default): a second inbound rail (e.g.
  ``events.sensation.audio.partial``) painted on the TUI status strip instead
  of driving a turn — see "Draft status" above. Omitting it leaves this
  extension exactly as it was: one inbound subject, one turn per event.
- ``nats_url`` (default ``nats://127.0.0.1:4222``)
- ``verbs`` (default ``("speak",)``): which effector tools to register.
- ``origin_node`` (default ``"tau"``), ``ack_timeout_s`` (default 30.0).

No blocking JMFTS call
-----------------------

This file makes zero JMFTS calls, so the H2/J7 concern ("no blocking call
inside a coroutine") has no instance here and there is no executor mechanism to
name or later delete. It applies the moment a JMFTS-backed tool joins this
session — tau-008's projection — and whoever adds that tool owns keeping it off
this loop, because the NATS client's heartbeats run here.

tau-002 (H9/T8) — what is built, and what is not
-------------------------------------------------

Built: the client half of publish-before-perform. Each effector call subscribes
the ack subject **before** publishing (so a fast effector cannot ack into a
subscription that does not exist yet), publishes, then awaits the ack —
polling the tool's :class:`~tau_llm.abort.AbortSignal` between short waits so an
aborted turn does not park this coroutine, bounded by ``ack_timeout_s`` so an
unacked mutation raises rather than hanging. An ack whose payload says
``ok: False`` or carries a non-null ``error`` raises too: "zero orphans" means
a mutation either completes or is loudly reported.

A verb whose :class:`VerbSpec` has ``ack_subject=None`` is fire-and-forget by
the *effector's* design, and the tool returns as soon as the publish lands.
That is not a fallback — it is the contract for verbs no effector acks.

Not built: constructing an ``Attribution``. Four of its ten fields are not
this extension's to fill (``model``/``surface_rendering`` are the reflex tier's
identity, ``world_tick`` is the engine's frame counter, ``read_watermark``
waits on jmfts-002's ``VisibilityBound``), and there is still no call site to
record one into. The whole ack payload rides the ``tool_execution_end`` event,
so a caller that *does* have a recorder loses nothing by this file declining to
build the dataclass itself. (It is no longer put in the MODEL's tool result for
a terminal verb — see :meth:`VerbSpec.result` and the infinite-loop note in
``_make_effector._success``.)

Draft status: a second inbound rail that never becomes a turn (2026-08-02)
-----------------------------------------------------------------------------

``inbound_subject`` drives a turn per event — right for
``events.sensation.audio.resolved.clean`` (the accumulator/resolver's
committed output), wrong for ``events.sensation.audio.partial`` (tectum's
``audio.stt``: "the revisable in-flight hypothesis", one event per growing
guess, same ``binding_id`` for the whole utterance). An optional
``draft_subject`` subscribes that second rail and paints it on the TUI's
status strip via ``api.ui.set_status("hearing", text)`` instead of calling
``api.submit`` — a draft is shown, not committed, and the committed turn (via
``inbound_subject``) clears the slot when it lands.

This is gated on :attr:`~tau_agent_core.extension_types.ExtensionUI.interactive`,
not on ``self._mode`` read directly: a headless run — ``--mode json``
included, whose record schema has no field for an arbitrary per-partial status
string — has nothing to paint the draft on, so ``_on_draft`` returns before
even parsing the envelope. Several partials arrive per second while someone is
mid-utterance; paying JSON-parse-and-format cost for a value nobody can see,
every time, on a robot running headless in production, is the kind of waste
this extension's own ack-polling loop (:func:`_await_ack`) is careful not to
introduce elsewhere.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

# nats-py is the ``[bus]`` extra, not a base dependency of tau-agent-core. This
# file is never imported by the package — it is read, compiled and exec'd by
# ``_load_one_extension``, so this line runs only when someone actually loads the
# extension. The loader reports the exception verbatim as "failed to load
# extension <path>: <error>", and "No module named 'nats'" is not something a user
# can act on: they asked for a bus, not for a library they have never heard of.
try:
    import nats
    import nats.errors
except ModuleNotFoundError as _exc:  # pragma: no cover - depends on install extras
    raise ImportError(
        "the nats_bus extension needs the 'bus' extra, which this install does "
        "not have: pip install 'ffwf-tau-agent-core[bus]'. Nothing else in τ "
        "requires nats-py."
    ) from _exc

#: The general subject namespace this extension type touches (H7). A concrete
#: instance binds the configured subset. ``events.sensation.>`` and
#: ``events.workspace.*.in`` are the two inbound shapes a tectum schema may
#: drive an agent with; ``events.journal.>`` and ``events.action.>`` are the
#: two ack namespaces (see :data:`VERBS`).
TOUCHES_BUS = True
SUBJECTS = (
    "events.sensation.>",
    "events.workspace.*.in",
    "events.workspace.*.out.>",
    "events.journal.>",
    "events.action.>",
)


@dataclass(frozen=True)
class VerbSpec:
    """One effector verb's contract — τ's counterpart to tectum's ``ToolSpec``.

    Deliberately shaped like ``tectum/tools.py``'s ``ToolSpec``, because that is
    where these facts are established and tectum learned them the hard way.

    Attributes:
        ack_subject: Template for the subject this verb's effector acknowledges
            on, or ``None`` when it acknowledges nothing (then the tool returns
            as soon as the publish lands). The token after ``events.journal`` is
            the effector's *kind*, which is not always the verb —
            ``journal_append`` acks as ``append``.
        description: What the model is told the tool does.
        parameters: This verb's JSON Schema, handed verbatim to
            ``register_tool``. It is also the ONLY argument validation τ does:
            :func:`~tau_llm.tools.validate_tool_arguments` checks it in
            ``AgentLoop._prepare_tool_call`` before ``execute`` is reached, so
            this file does not re-check types. See :func:`_make_effector` for
            where that boundary sits and what deliberately falls the other side
            of it.
        non_empty: Required string parameters that must additionally be
            non-blank. Separate from ``parameters`` because τ's schema validator
            implements ``required`` and ``type`` and NOTHING else — no
            ``minLength``, no ``minimum``, no ``enum``. Writing ``minLength``
            into the schema would look enforced and be ignored, which is worse
            than not writing it; this names the constraint where it is actually
            checked.
        result: What the tool returns to the MODEL on success. tectum's comment
            on its own equivalent field is the whole reason this exists: "This
            steers the turn: a bare 'ok' invites another call (observed live: a
            speak loop), so action tools say explicitly that the turn is over."
        terminal: Whether a successful call ENDS the agent loop
            (``AgentToolResult.terminate``). Per-verb, not global: speaking ends
            a turn, but journalling and then speaking is legitimate.
    """

    ack_subject: str | None
    description: str
    parameters: dict[str, Any]
    non_empty: tuple[str, ...] = ()
    result: str = ""
    terminal: bool = False


def _text_schema(detail: str) -> dict[str, Any]:
    """The one-string-parameter schema the four tectum effector verbs share."""
    return {
        "type": "object",
        "properties": {"text": {"type": "string", "description": detail}},
        "required": ["text"],
    }


#: Every verb τ can publish. The tectum verbs are transcribed from its effector
#: nodes and ``tectum/tools.py``; the world verbs from McRogueFace's body node
#: (``robot_sim_stack/world/body_node.py`` + ``world/entities/verbs.py``) and
#: confirmed against a running engine — see the WORLD VERBS comment below.
VERBS: dict[str, VerbSpec] = {
    # effectors/speech.py:107 — and parley-nats stands in for it in harness_text.
    # Terminal, with tectum's own SPEAK.result wording (tools.py:50). Both halves
    # matter: `terminal` stops the loop mechanically, and `result` tells the model
    # why, so it does not simply try again on the next turn.
    "speak": VerbSpec(
        ack_subject="events.action.speech.completed.{binding_id}",
        description=(
            "Speak text aloud. Call this only when you choose to respond out loud — "
            "staying silent is a valid choice. Your turn is over once it returns."
        ),
        parameters=_text_schema("what to say aloud"),
        # An empty utterance still reaches the TTS effector, which acks it happily:
        # silence that looks like speech, with no error anywhere. The schema cannot
        # say this (see VerbSpec.non_empty), so it is said here.
        non_empty=("text",),
        result="spoken. Your turn is over: make no further tool calls and write nothing.",
        terminal=True,
    ),
    # effectors/journal_append.py:48 — subjects.journal_ack("append", bid).
    # NOT terminal: tectum's JOURNAL_APPEND leaves ToolSpec.result at its "" default,
    # because noting something and then speaking about it is one coherent turn.
    "journal_append": VerbSpec(
        ack_subject="events.journal.append.{binding_id}",
        description=(
            "Append a brief note to your private journal (durable memory). Record "
            "what you noticed and what you decided."
        ),
        parameters=_text_schema("the note to append"),
        non_empty=("text",),
    ),
    # effectors/jmfts_write.py:61 — subjects.journal_ack("jmfts_write", bid)
    "jmfts_write": VerbSpec(
        ack_subject="events.journal.jmfts_write.{binding_id}",
        description="Write a durable record into JMFTS.",
        parameters=_text_schema("the record to write"),
        non_empty=("text",),
    ),
    # tools.py DELEGATE — consumed by agent.workspace_curator, which acks nothing;
    # the curator's answer arrives later as a separate `posted` event.
    "delegate": VerbSpec(
        ack_subject=None,
        description=(
            "Hand a question or task to your deliberation layer. It works in the "
            "background and its answer reaches you later."
        ),
        parameters=_text_schema("the question or task to hand off"),
        non_empty=("text",),
    ),
    # ---------------------------------------------------------------------
    # WORLD VERBS — McRogueFace's body node, not a tectum effector.
    #
    # Transcribed from ``robot_sim_stack/world/entities/verbs.py`` (the payload
    # contract) and ``world/body_node.py`` (the ack), then CONFIRMED on the wire
    # against a running headless engine: a ``move_to`` acked
    # ``{"status":"ok","trigger":"DONE","verb":"move_to","world_tick":383}`` on
    # ``events.journal.move_to.<bid>`` and the courier moved to the target cell.
    #
    # Two things this table does NOT try to enforce, deliberately:
    #
    #   * ``wait`` requires ``turns >= 1`` and ``move_to`` requires a REACHABLE
    #     cell. Neither is expressible in τ's schema validator, and neither is
    #     τ's fact to know — the grid, its walls, and its pathfinder live in the
    #     engine. τ validates SHAPE, the world validates SEMANTICS; a violation
    #     comes back as a ``status: "error"`` / ``"refused"`` ack, which is a
    #     real answer rather than a guess this side would have had to invent.
    #   * coordinates are ``integer``, and τ's validator rejects a ``bool`` for
    #     an integer param (``tau_llm/tools.py``). That matters here rather than
    #     abstractly: ``verbs._require_int`` rejects bools too, so without the
    #     τ-side check a model emitting ``{"x": true}`` would have burned a
    #     round trip to be told what τ already knew.
    # ---------------------------------------------------------------------
    "move_to": VerbSpec(
        ack_subject="events.journal.move_to.{binding_id}",
        description=(
            "Walk to a cell in the world. Give the target's x and y grid "
            "coordinates. Returns once you arrive, or reports that the way was "
            "blocked."
        ),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "target column, 0-indexed"},
                "y": {"type": "integer", "description": "target row, 0-indexed"},
            },
            "required": ["x", "y"],
        },
    ),
    "wait": VerbSpec(
        ack_subject="events.journal.wait.{binding_id}",
        description="Stand still for a number of world turns.",
        parameters={
            "type": "object",
            "properties": {
                "turns": {"type": "integer", "description": "how many turns to wait (>= 1)"}
            },
            "required": ["turns"],
        },
    ),
    # propose-class (verbs.py VERB_CLASSES): the body node records it in
    # ``pending_proposals`` and returns WITHOUT acking — no set_behavior, no
    # journal event. ``ack_subject=None`` is that fact, not a shortcut: waiting
    # on an ack here would time out on every call.
    "note": VerbSpec(
        ack_subject=None,
        description=(
            "Record an observation about the world without acting on it. Nothing "
            "moves; the note is filed for whoever reads it later."
        ),
        parameters=_text_schema("the observation to record"),
        non_empty=("text",),
    ),
}

DEFAULT_ACK_TIMEOUT_S = 30.0

#: How long each poll of the ack subscription waits before re-checking the
#: tool's AbortSignal — short enough to notice an abort promptly, long enough
#: not to busy-loop.
_ACK_POLL_INTERVAL_S = 0.2

DEFAULT_TTL_MS = 60_000


def _envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    source: str,
    origin_node: str,
    binding_id: str | None,
    sequence_number: int,
    hops: list[str],
    ttl_ms: int = DEFAULT_TTL_MS,
) -> dict[str, Any]:
    """One ``TectumEvent`` as its canonical JSON dict (``tectum/event.py:76``).

    Every key ``TectumEvent.from_dict`` indexes without a default is present;
    the optional ones are explicit ``None`` rather than omitted, matching what
    ``to_dict`` emits so a consumer sees no difference between τ's events and a
    tectum node's.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "source": source,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sequence_number": sequence_number,
        "ttl_ms": ttl_ms,
        "payload": payload,
        "produced_by_schema": None,
        "routed_by_schema": None,
        "binding_id": binding_id,
        "expectation": None,
        "residual": None,
        "origin_node": origin_node,
        "hops": hops,
        "seen_by": [],
        "audit": {"via": "tau"},
    }


def _ack_failure(payload: dict[str, Any]) -> str | None:
    """Why this ack reports failure, or ``None`` if it does not.

    **Two producers, two failure dialects, and they do not overlap.**

    *tectum's effectors* have no ``status`` field at all (see the module
    docstring): failure is ``ok: False`` or a non-null ``error``, and the
    absence of both is success — the right reading, because
    ``journal_append``'s ack carries only ``doc_id`` and is a success.

    *McRogueFace's body node* signals with ``status: "ok" | "refused" |
    "error"`` (``world/body_node.py``). Reading only the tectum markers made
    this side blind to all three — verified against the payloads it actually
    emits, every one of which returned ``None`` from the old version of this
    function. A blocked robot and a malformed payload both came back to the
    model as a completed move. That is the silent-in-the-optimistic-direction
    failure ``SIM_SPEC_v2.md`` §13.2 warns about, arriving through τ.

    An UNKNOWN ``status`` value counts as failure, not success. No producer
    emits one today, so this only fires if a third dialect appears — and then
    the safe reading is "τ does not understand this ack", loudly, rather than
    "it must have worked".
    """
    if payload.get("ok") is False:
        return f"effector reported ok=False: {payload!r}"
    error = payload.get("error")
    if error is not None:
        return f"effector reported error={error!r}"
    status = payload.get("status")
    if status is not None and status != "ok":
        # `trigger` names WHICH refusal (BLOCKED vs an immediate busy refusal,
        # which carries trigger: null) — the model needs that to decide whether
        # retrying could ever work, so it goes in the message rather than being
        # left on the event for an operator to correlate.
        trigger = payload.get("trigger")
        detail = f" (trigger={trigger})" if trigger is not None else ""
        return f"effector reported status={status!r}{detail}: {payload!r}"
    return None


def register(api: Any) -> None:
    """Bind this session's inbound subscription and its per-verb effector tools.

    Raises:
        ValueError: ``workspace`` or ``inbound_subject`` missing from config, or
            ``verbs`` naming a verb with no known ack contract. All three are
            Fail-Early: a guessed workspace publishes to the wrong subject, a
            guessed inbound subject silently never fires, and a guessed ack
            subject waits out the timeout on every call.
    """
    workspace = api.config.get("workspace")
    if not workspace or not isinstance(workspace, str):
        raise ValueError(
            "nats_bus extension requires config 'workspace' — the agent identity "
            "it publishes as (events.workspace.<workspace>.out.<verb>). There is "
            "no default: an unnamed workspace makes every subject wrong, quietly."
        )
    inbound_subject = api.config.get("inbound_subject")
    if not inbound_subject or not isinstance(inbound_subject, str):
        raise ValueError(
            "nats_bus extension requires config 'inbound_subject' — which subject "
            "drives this agent is a property of the active tectum schema (e.g. "
            "'events.sensation.audio.resolved.clean' in praxis/harness_text.yaml, "
            f"or 'events.workspace.{workspace}.in' for a dispatch binding). There "
            "is no default: a wrong subject never fires, and never fires silently."
        )
    draft_subject = api.config.get("draft_subject")
    if draft_subject is not None and not isinstance(draft_subject, str):
        raise ValueError("nats_bus: config 'draft_subject' must be a string subject")
    nats_url = api.config.get("nats_url", "nats://127.0.0.1:4222")
    origin_node = api.config.get("origin_node", "tau")
    ack_timeout_s = float(api.config.get("ack_timeout_s", DEFAULT_ACK_TIMEOUT_S))
    verbs = tuple(api.config.get("verbs", ("speak",)))
    unknown = [v for v in verbs if v not in VERBS]
    if unknown:
        raise ValueError(
            f"nats_bus: no ack contract known for verb(s) {unknown!r}. Add them to "
            "VERBS with the subject their effector actually acks on "
            "(read the effector node, don't guess — the kind token is not always "
            f"the verb). Known: {sorted(VERBS)}"
        )

    outbound_prefix = f"events.workspace.{workspace}.out"
    source = f"agent.{workspace}"

    # Closure state, not module globals: a second instance of this file (a
    # second workspace in one process) must not share a connection or a
    # binding stamp with the first.
    #
    # ``binding_id`` is the per-turn stamp (point 5 in the module docstring):
    # the inbound event's id, carried onto whatever this turn publishes so the
    # flow stays correlated and the ack lands where this side is listening.
    state: dict[str, Any] = {
        "nc": None,
        "sub": None,
        "draft_sub": None,
        "binding_id": None,
        "seq": 0,
    }

    def _next_seq() -> int:
        seq = int(state["seq"])
        state["seq"] = seq + 1
        return seq

    def _draft(text: str | None) -> None:
        """Update (or clear) the "hearing" status slot — TUI only, by design.

        Gated on :attr:`ExtensionUI.interactive`, not on catching an exception
        or checking a mode string here: the check has to happen BEFORE
        ``set_status`` is even called, or a headless run pays the same cost
        (format the string, cross the extension boundary) it was gated to
        avoid. See the module docstring, "Draft status".
        """
        if api.ui.interactive:
            api.ui.set_status("hearing", text)

    async def _on_draft(msg: Any) -> None:
        """A revisable in-flight ASR hypothesis — paint it, never submit it.

        Mirrors :func:`_on_inbound`'s envelope parsing (same TectumEvent shape,
        same ``payload.text`` contract) but stops at :func:`_draft`: a partial
        is not a turn, and calling ``api.submit`` here would drive one turn per
        growing guess instead of one per committed utterance. Never raises out
        of the NATS callback, for the same reason ``_on_inbound`` does not.
        """
        if not api.ui.interactive:
            return  # nothing to paint on; skip the parse too (see _draft)
        try:
            event = json.loads(msg.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await api.emit("draft_error", {"subject": msg.subject, "error": str(exc)})
            return
        if not isinstance(event, dict):
            return
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return
        _draft(payload["text"] or None)

    async def _on_inbound(msg: Any) -> None:
        """Parse the envelope, stamp the binding, drive one turn.

        Never raises out of the NATS callback: a bad payload is an event to
        report, not a reason to tear down the subscription — and a raising
        callback is exactly what must not stall this connection's read loop.

        **This callback runs on the session's own event loop, so ``api.submit``
        is the correct door and ``api.submit_threadsafe`` would be wrong here.**
        Checked rather than assumed (docs/SUBMISSION-LIFECYCLE.md "Task
        marshalling" — the whole point of that section is that this question has
        an answer per driver, not a default): ``nats-py`` spawns no thread of its
        own. ``Subscription._start`` creates its ``_wait_for_msgs`` task with
        ``asyncio.get_running_loop().create_task`` (``aio/subscription.py:216``)
        and awaits ``self._cb(msg)`` from inside it (``:310``), so the callback
        runs on whichever loop called ``subscribe()`` — which is
        :func:`_on_session_start` below, dispatched by the session's own
        extension runner on the session's loop. Marshalling it anyway would only
        add a scheduling hop and hand back a ``concurrent.futures.Future`` this
        coroutine cannot await without deadlocking its own loop.
        """
        try:
            event = json.loads(msg.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await api.emit("inbound_error", {"subject": msg.subject, "error": str(exc)})
            return
        if not isinstance(event, dict):
            await api.emit(
                "inbound_error",
                {"subject": msg.subject, "error": "event is not a JSON object"},
            )
            return
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            await api.emit(
                "inbound_error",
                {
                    "subject": msg.subject,
                    "error": (
                        "TectumEvent payload must be an object with a string 'text' "
                        "(tectum/event.py: the body is at .payload, not top level)"
                    ),
                    "event": event,
                },
            )
            return

        # Preserve the flow's binding_id for anything this turn publishes.
        state["binding_id"] = event.get("binding_id")
        await api.emit("inbound", {"subject": msg.subject, "event": event})
        # The committed utterance supersedes whatever guess was on the strip.
        _draft(None)

        # Refuse rather than queue while a turn is in flight — but the REFUSAL is
        # the core's now (docs/SUBMISSION-LIFECYCLE.md phase 5), not a flag this
        # file keeps. ``multitask_strategy="reject"`` is the same policy the
        # hand-rolled ``state["turn_in_flight"]`` implemented: tectum's own
        # dispatcher runs one dispatch at a time, interleaving two turns on one
        # session would corrupt the message history, and silently queueing would
        # make the agent answer a stale utterance minutes later. What changes is
        # WHERE it lives: one implementation in ``AgentSession.submit`` instead of
        # one per extension, and a typed ``SubmissionResult(accepted=False,
        # rejection_reason=…)`` instead of a bare local flag — a refusal this
        # side did not have to invent a reason string for. The flag also could
        # not see a turn started by anything else (a human at the TUI, another
        # extension); the lock it now consults can.
        #
        # ``correlation`` carries the flow's identity onto every AgentEvent the
        # turn emits (the spec names exactly this use), so a renderer or monitor
        # can tie a rendered turn back to the bus message that caused it without
        # this extension publishing a second correlating event.
        try:
            result = await api.submit(
                payload["text"],
                multitask_strategy="reject",
                correlation={
                    "subject": msg.subject,
                    "binding_id": event.get("binding_id"),
                    "event_id": event.get("event_id"),
                },
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn must not kill the sub
            await api.emit("turn_error", {"subject": msg.subject, "error": repr(exc)})
            return
        if not result.accepted:
            await api.emit(
                "inbound_dropped",
                {
                    "subject": msg.subject,
                    "reason": result.rejection_reason,
                    "submission_id": result.submission_id,
                },
            )

    async def _on_session_start(event: dict[str, Any], ctx: Any) -> None:
        # ``ctx`` is deliberately not stashed: a turn is originated through
        # ``api.submit`` (bucket-bound, so the submission is attributed to THIS
        # extension), which needs no context. The effector tools receive their
        # own ctx per call.
        nc = await nats.connect(nats_url)
        state["nc"] = nc
        state["sub"] = await nc.subscribe(inbound_subject, cb=_on_inbound)
        if draft_subject is not None:
            state["draft_sub"] = await nc.subscribe(draft_subject, cb=_on_draft)

    async def _on_session_shutdown(event: dict[str, Any], ctx: Any) -> None:
        sub = state.get("sub")
        if sub is not None:
            await sub.unsubscribe()
            state["sub"] = None
        draft_sub = state.get("draft_sub")
        if draft_sub is not None:
            await draft_sub.unsubscribe()
            state["draft_sub"] = None
        nc = state.get("nc")
        if nc is not None:
            await nc.close()
            state["nc"] = None

    async def _await_ack(ack_sub: Any, *, signal: Any) -> Any:
        """First message on ``ack_sub``, honouring ``signal`` and the timeout."""
        deadline = time.monotonic() + ack_timeout_s
        while True:
            if signal is not None and signal.is_aborted():
                raise asyncio.CancelledError("effector: aborted while awaiting the effector's ack")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"effector: no ack on {ack_sub.subject!r} within {ack_timeout_s}s "
                    "— treating this mutation as unconfirmed rather than silently "
                    "reporting success (tau-002: zero orphans)"
                )
            try:
                return await ack_sub.next_msg(timeout=min(_ACK_POLL_INTERVAL_S, remaining))
            except nats.errors.TimeoutError:
                continue

    def _make_effector(verb: str) -> Any:
        """One tool whose execution publishes ``…out.<verb>`` and awaits its ack."""
        subject = f"{outbound_prefix}.{verb}"
        spec = VERBS[verb]
        ack_template = spec.ack_subject

        def _success(detail: str) -> dict[str, Any]:
            """The tool result, with the loop-stopping half and the model-facing half.

            Both halves are needed, and a live infinite loop proved it: with
            neither, ``speak`` returned a transport blob
            (``…acked on events.action.speech.completed.<bid>: {json}``) which
            told the model nothing about the turn being over AND quoted the
            model's own sentence back to it. The model called ``speak`` again
            with identical text, and the loop ran to turn 28 before being killed
            by hand. Nothing else would have killed it: ``max_turns`` was 50 at
            the time and is now ``None`` by default, so a terminal verb that does
            not terminate has no backstop but the operator.

            ``terminal`` → ``terminate`` is the mechanical stop, read off this
            dict by ``agent_loop._build_batch_result``. ``spec.result`` is the
            semantic one, in tectum's own words, so the model does not simply
            retry on the next turn.

            The transport detail is dropped for a terminal verb rather than
            appended: an operator still sees the whole ack on the
            ``tool_execution_end`` event (that is what the demo runner prints),
            so nothing is lost by keeping it out of the model's context, where
            its only demonstrated effect was to invite another call.
            """
            text = spec.result or detail
            result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            if spec.terminal:
                result["terminate"] = True
            return result

        async def _execute(
            tool_call_id: str,
            params: dict[str, Any],
            signal: Any,
            on_update: Any,
            ctx: Any,
        ) -> dict[str, Any]:
            # Types and required-ness were already checked against
            # ``spec.parameters`` by ``AgentLoop._prepare_tool_call`` before this
            # coroutine was reached, so re-validating them here would be a second
            # implementation of one contract — the thing this file exists to
            # avoid. What is left is only what that validator cannot express.
            for field in spec.non_empty:
                value = params.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{verb}: {field!r} must be a non-empty string "
                        f"(got {value!r}) — an empty one publishes cleanly and "
                        "is acked cleanly, which is worse than failing here"
                    )
            nc = state.get("nc")
            if nc is None:
                raise RuntimeError(
                    f"{verb}: not connected to NATS (session_start has not run, or "
                    "session_shutdown already tore the connection down)"
                )
            # The flow's binding_id, preserved across hops. Absent an inbound
            # event (a turn started some other way) this mints one so the event
            # is still correlatable — the same thing tectum's shim does when
            # TECTUM_BINDING_ID is unset.
            binding_id = state.get("binding_id") or uuid.uuid4().hex
            # The payload IS the verb's arguments, plus the agent identity —
            # one projection for every verb, not a builder per verb. Confirmed
            # against both consumers: tectum's shim publishes
            # ``{"text": …, "agent": …}`` (tools.py:247) and McRogueFace's body
            # node reads the verb args straight off ``payload``, so
            # ``{"x": …, "y": …, "agent": …}`` is the same shape with a
            # different verb's arguments in it.
            wire = _envelope(
                subject,
                {**params, "agent": workspace},
                source=source,
                origin_node=origin_node,
                binding_id=binding_id,
                sequence_number=_next_seq(),
                hops=[f"tool.{verb}"],
            )

            if ack_template is None:
                await nc.publish(subject, json.dumps(wire).encode("utf-8"))
                return _success(
                    f"published {subject} (binding_id={binding_id}); "
                    "this verb's effector acks nothing"
                )

            ack_subject = ack_template.format(binding_id=binding_id or "none")
            # Subscribe-first: be listening before the effector could reply, or
            # a fast ack races the subscribe and is lost.
            ack_sub = await nc.subscribe(ack_subject)
            try:
                await nc.publish(subject, json.dumps(wire).encode("utf-8"))
                msg = await _await_ack(ack_sub, signal=signal)
            finally:
                await ack_sub.unsubscribe()

            try:
                ack = json.loads(msg.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{verb}: ack on {ack_subject!r} is not valid JSON: {exc}"
                ) from exc
            ack_payload = ack.get("payload") if isinstance(ack, dict) else None
            if not isinstance(ack_payload, dict):
                raise ValueError(
                    f"{verb}: ack on {ack_subject!r} is not a TectumEvent with a "
                    f"payload object: {ack!r}"
                )
            failure = _ack_failure(ack_payload)
            if failure is not None:
                raise RuntimeError(
                    f"{verb}: effector did not complete the mutation "
                    f"(binding_id={binding_id}): {failure}"
                )
            return _success(
                f"{subject} acked on {ack_subject} (binding_id={binding_id}): "
                f"{json.dumps(ack_payload, sort_keys=True)}"
            )

        return _execute

    api.on("session_start", _on_session_start)
    api.on("session_shutdown", _on_session_shutdown)
    for verb in verbs:
        api.register_tool(
            {
                "name": verb,
                "description": VERBS[verb].description,
                # The verb's own schema, not a shared one-string shape. The
                # shared shape was why every tool took `text`: `move_to` could
                # not be expressed at any price, so the world verbs did not
                # exist. This is also the schema the loop validates against
                # before `execute` runs — see VerbSpec.parameters.
                "parameters": VERBS[verb].parameters,
                "execute": _make_effector(verb),
            }
        )
