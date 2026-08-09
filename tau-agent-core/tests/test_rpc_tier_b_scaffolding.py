"""B0 — Tier B scaffolding: the two shared helpers docs/RPC-TIER-B.md §3
"B0 — scaffolding" points 2 and 3 ask for, tested directly since no verb
exists yet to exercise them through the wire.

- ``commands.turn_safety_guard`` — D-1's bounded ``turn_lock`` acquire,
  raising ``RPCError(TURN_STILL_RUNNING, ...)`` on timeout rather than
  proceeding.
- ``commands.require_log_appender`` — §1.1's "the bound log must have this
  appender, else raise".
- ``commands.require_durable_session`` — the corrected §1.1 guard (Blocker 2
  of the Tier B review): "the bound log must be able to KEEP the entry".
  Added later than the other two, and it is the one that actually holds the
  line: every real ``ConversationSession`` has every appender, so the
  method-presence check above passes on precisely the unpersisted session
  whose appends go nowhere.

It also carries the TWO tier-wide pins no single verb's file could own:

- the rule "E5 in Tier B" states in ``rpc/commands.py`` — every mutator's
  completion carries ``cursor``, every read omits it — which the tier first
  shipped two answers to (finding 5 of the Tier B review);
- the rule "DURABILITY in Tier B" (D-7) states beside it — the verb that
  APPENDS refuses an unpersisted session, the verb that appends nothing does
  not ask — which the tier first shipped THREE answers to (finding 6 of the
  same review: ``set_model``/``set_session_name`` refused, ``compact`` ran
  and reported a cursor for an entry lost on exit, ``set_auto_compaction``
  reported the live tip).

Neither of the first two helpers had a caller when this file was written
(B1-B6 landed later, in parallel worktrees) — that is precisely why this is
its own file rather than folded into ``test_rpc.py`` or
``test_rpc_capability_audit.py``: no unit contends on it.

Reference: docs/RPC-TIER-B.md §3 "B0 — scaffolding", §1.1, §6 "Every test
you write must be able to fail."
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import commands as commands_module
from tau_agent_core.rpc.commands import (
    COMMAND_TABLE,
    COMPACT_RESULT_SCHEMA,
    COMPACTION_END_PARAMS_SCHEMA,
    _DURABLE_LOCATION_ATTRS,
    RPCError,
    require_durable_session,
    require_log_appender,
    turn_safety_guard,
)
from tau_agent_core.rpc.dialect import SESSION_NOT_PERSISTED, TURN_STILL_RUNNING
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


@pytest.fixture
def real_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


class _LogWithAppenders:
    """A minimal stand-in log carrying the two §1.1 appenders — mirrors
    ``test_agent_session_runtime.py``'s ``_FakeConversationSession`` (a
    small test-local fake rather than a cross-test-file import; that file's
    own docstring states cross-test-file imports have no precedent in this
    suite)."""

    def __init__(self) -> None:
        self.model_changes: list[tuple[str, str]] = []
        self.session_infos: list[str] = []

    def append_model_change(self, model: str, backend: str) -> str:
        self.model_changes.append((model, backend))
        return "model-change-id"

    def append_session_info(self, name: str) -> str:
        self.session_infos.append(name)
        return "session-info-id"


# ── turn_safety_guard (D-1) ─────────────────────────────────────────────


async def test_turn_safety_guard_acquires_and_releases_the_lock(
    real_session: AgentSession,
) -> None:
    """Uncontended: the guard acquires the lock for the body's duration and
    releases it on the way out."""
    assert not real_session.turn_lock.locked()
    async with turn_safety_guard(real_session, timeout=1.0):
        assert real_session.turn_lock.locked()
    assert not real_session.turn_lock.locked()


async def test_turn_safety_guard_releases_the_lock_even_if_the_body_raises(
    real_session: AgentSession,
) -> None:
    with pytest.raises(ValueError):
        async with turn_safety_guard(real_session, timeout=1.0):
            raise ValueError("boom")
    assert not real_session.turn_lock.locked()


async def test_turn_safety_guard_raises_turn_still_running_on_timeout(
    real_session: AgentSession,
) -> None:
    """Something else already holds ``turn_lock`` (a turn in flight) — the
    guard refuses with ``RPCError(TURN_STILL_RUNNING, ...)`` rather than
    blocking, and never enters the body."""
    await real_session.turn_lock.acquire()
    entered_body = False
    try:
        with pytest.raises(RPCError) as excinfo:
            async with turn_safety_guard(real_session, timeout=0.05):
                entered_body = True
        assert excinfo.value.code == TURN_STILL_RUNNING
        assert not entered_body
    finally:
        real_session.turn_lock.release()


async def test_turn_safety_guard_does_not_touch_a_lock_it_never_acquired(
    real_session: AgentSession,
) -> None:
    """The timeout path must not release a lock some OTHER holder still
    owns — that would let a second caller acquire it out from under the
    turn that is genuinely still running."""
    await real_session.turn_lock.acquire()
    with pytest.raises(RPCError):
        async with turn_safety_guard(real_session, timeout=0.05):
            pass  # pragma: no cover - never reached
    assert real_session.turn_lock.locked()
    real_session.turn_lock.release()


# ── require_log_appender (§1.1) ─────────────────────────────────────────


def test_require_log_appender_passes_silently_when_the_log_has_it(
    real_session: AgentSession,
) -> None:
    real_session.session_log = _LogWithAppenders()  # type: ignore[assignment]
    require_log_appender(real_session, "append_model_change", verb="set_model")
    require_log_appender(real_session, "append_session_info", verb="set_session_name")


def test_require_log_appender_raises_when_the_log_lacks_it(
    real_session: AgentSession,
) -> None:
    """``InMemorySessionLog`` deliberately has neither appender
    (session_log.py:38-48) — the real "nowhere durable to land this" case,
    not a contrived fake."""
    with pytest.raises(RuntimeError, match="append_model_change"):
        require_log_appender(real_session, "append_model_change", verb="set_model")
    with pytest.raises(RuntimeError, match="append_session_info"):
        require_log_appender(real_session, "append_session_info", verb="set_session_name")


def test_require_log_appender_passes_on_an_unpersisted_log(
    real_session: AgentSession,
) -> None:
    """The premise Blocker 2 rests on, pinned rather than asserted in prose:
    this guard is about METHOD PRESENCE and says nothing about durability.
    An unpersisted file-store session (``path is None``) has every appender,
    so this passes — which is exactly why the RPC verbs cannot rely on it
    alone, and why ``require_durable_session`` exists below.

    Delete ``require_durable_session``'s call sites and no test in this
    section notices; that is the point of keeping the two apart.
    """
    log = _LogWithAppenders()
    log.path = None  # type: ignore[attr-defined]
    real_session.session_log = log  # type: ignore[assignment]

    require_log_appender(real_session, "append_model_change", verb="set_model")
    require_log_appender(real_session, "append_session_info", verb="set_session_name")

    # ... and the durability guard, asked the same question, says no.
    with pytest.raises(RPCError, match="unpersisted") as refusal:
        require_durable_session(real_session, verb="set_model")
    assert refusal.value.code == SESSION_NOT_PERSISTED


# ── require_durable_session (Blocker 2, the corrected §1.1) ─────────────


class _DurableFileishLog(_LogWithAppenders):
    """Declares a durable location the way the file store does — a
    non-``None`` ``path`` (``tau_coding_agent.session_store.Session``)."""

    path = Path("/tmp/does-not-need-to-exist.jsonl")


class _DurableDocLog(_LogWithAppenders):
    """Declares one the way the JMFTS store does — ``root_doc_id``
    (``tau_jmfts.store.JmftsSessionLog``), and NO ``path`` at all: proof
    the guard is not secretly file-store-only."""

    root_doc_id = 4171


def test_require_durable_session_passes_on_a_file_backed_location(
    real_session: AgentSession,
) -> None:
    real_session.session_log = _DurableFileishLog()  # type: ignore[assignment]
    require_durable_session(real_session, verb="set_model")


def test_require_durable_session_passes_on_a_document_backed_location(
    real_session: AgentSession,
) -> None:
    real_session.session_log = _DurableDocLog()  # type: ignore[assignment]
    require_durable_session(real_session, verb="set_session_name")


def test_require_durable_session_raises_on_an_unpersisted_location(
    real_session: AgentSession,
) -> None:
    """``path is None`` — ``Session.create_in_memory``'s product, whose
    ``_persist_header``/``_persist_entry`` are ``return``-on-``None`` no-ops
    (session_store.py:585,593). The message names the condition, so a host
    reading stderr/the error can tell this from "unknown store"."""
    log = _DurableFileishLog()
    log.path = None  # type: ignore[assignment]
    real_session.session_log = log  # type: ignore[assignment]

    with pytest.raises(RPCError, match="unpersisted") as refusal:
        require_durable_session(real_session, verb="set_model")
    assert refusal.value.code == SESSION_NOT_PERSISTED


def test_require_durable_session_raises_when_nothing_is_declared(
    real_session: AgentSession,
) -> None:
    """``InMemorySessionLog`` declares no location at all. Unknown
    durability REFUSES rather than assumes — the asymmetry
    ``_DURABLE_LOCATION_ATTRS``' note argues for ("unknown means no, not
    yes"), and the reason a future store cannot silently inherit a promise
    it does not keep."""
    with pytest.raises(RPCError, match="declares no durable location") as refusal:
        require_durable_session(real_session, verb="set_session_name")
    assert refusal.value.code == SESSION_NOT_PERSISTED


def test_durable_location_attrs_still_name_the_shipped_stores() -> None:
    """The coupling ``_DURABLE_LOCATION_ATTRS`` prices, pinned so the bill
    arrives at the right desk: if a store renames the attribute it declares
    its location with, the RPC layer starts refusing ``set_model``/
    ``set_session_name`` on that store — loudly, but far from the rename.
    This test fails AT the rename instead.

    Imported inside the test, matching ``test_60_retrieval_review.py``'s own
    in-function ``from tau_jmfts.client import ...`` (tau-jmfts depends on
    tau-agent-core, never the reverse, so this direction exists only in the
    test tree). Class-level ``hasattr``: no client, no server, no session —
    the question is only what the type declares.
    """
    from tau_jmfts.catalog import _EphemeralConversationSession
    from tau_jmfts.store import JmftsSessionLog

    assert "root_doc_id" in _DURABLE_LOCATION_ATTRS
    assert hasattr(JmftsSessionLog, "root_doc_id")
    # The ephemeral product declares nothing — the case that must be refused.
    assert not any(hasattr(_EphemeralConversationSession, a) for a in _DURABLE_LOCATION_ATTRS)


# ── E5, answered ONE way across the tier (finding 5) ────────────────────


#: The four MUTATING Tier B verbs, exactly as docs/RPC-TIER-B.md D-1 names
#: them ("Every mutating Tier B verb uses it: set_model, compact,
#: set_auto_compaction, set_session_name").
_TIER_B_MUTATORS = frozenset({"compact", "set_auto_compaction", "set_model", "set_session_name"})

#: The rest of the tier: D-1's own sentence "get_session_stats and
#: get_last_assistant_text are reads and take no guard", plus B5's second
#: verb ("Also expose the read (get_session_name) in the same unit"),
#: `get_models`, the read finding 7 of the Tier B review added so a host can
#: discover the config NAMES `set_model` accepts (G1), and `list_sessions`,
#: the read finding 8 added for the identical reason one param over — the
#: session IDS `switch_session` accepts.
_TIER_B_READS = frozenset(
    {
        "get_last_assistant_text",
        "get_models",
        "get_session_name",
        "get_session_stats",
        "list_sessions",
    }
)


def test_e5_is_answered_one_way_across_tier_b() -> None:
    """The rule ``rpc/commands.py``'s "E5 in Tier B" block states, pinned so
    the tier cannot ship two answers to it again.

    Finding 5 of the Tier B review: ``compact`` returned ``cursor`` even
    when it changed nothing ("the unchanged current tip") while
    ``set_auto_compaction`` — equally mutating, equally D-1-guarded —
    returned no ``cursor`` key at all, and neither its schema nor its notes
    said why. E5 (docs/REMOTE-CONTROL.md:267) is stated unconditionally, so
    the settled answer is "always, on every mutator's completion".

    Three things this asserts, in the order the rule states them:

    1. Every mutator's COMPLETION declares ``cursor`` and REQUIRES it —
       which for ``compact`` is ``COMPACTION_END_PARAMS_SCHEMA``, not its
       result schema: C3/D-5 moved that verb's outcome to a notification,
       and its acknowledgement is built before the compaction has moved
       anything, so a cursor there would be the pre-mutation tip (the same
       reasoning ``abort``'s notes record from the phase-2 trace). Asserted
       both ways round: the ack must NOT carry one.
    2. No read declares one.
    3. The classification is TOTAL over the tier. A new ``since="tier-b"``
       verb that is in neither set fails here, so the next verb's author
       has to answer E5 deliberately instead of inheriting whichever
       neighbour they copied.

    Reads the shipped schemas rather than a hand-copied list of field names:
    a schema that stops requiring ``cursor`` is exactly the regression, and
    a test carrying its own copy of the answer could not see it.
    """
    shipped = {name for name, entry in COMMAND_TABLE.items() if entry.since == "tier-b"}
    assert shipped == _TIER_B_MUTATORS | _TIER_B_READS, (
        "a Tier B verb is neither classified as a mutator nor as a read — "
        "E5 (rule 1 vs rule 2) has no answer for it"
    )

    for name in sorted(_TIER_B_MUTATORS):
        completion = (
            COMPACTION_END_PARAMS_SCHEMA if name == "compact" else COMMAND_TABLE[name].result_schema
        )
        assert completion is not None
        assert "cursor" in completion["properties"], f"{name}'s completion declares no cursor (E5)"
        assert "cursor" in completion["required"], (
            f"{name}'s completion makes cursor optional — absence is not this "
            "tier's way of saying 'nothing moved' (E5 rule 3)"
        )

    # compact's ACKNOWLEDGEMENT is the one Tier B mutator response that must
    # not carry one: the mutation has not happened when it is built.
    assert "cursor" not in COMPACT_RESULT_SCHEMA["properties"]

    for name in sorted(_TIER_B_READS):
        schema = COMMAND_TABLE[name].result_schema
        assert schema is not None
        assert "cursor" not in schema["properties"], (
            f"{name} is a read and must not carry a cursor (E5 rule 2) — "
            "a host that wants the tip calls get_state"
        )


# ── D-7, answered ONE way across the tier (finding 6) ───────────────────


#: The Tier B verbs that APPEND a session-log entry, i.e. the ones D-7 rule 1
#: makes take ``require_durable_session``: ``set_model``'s `model_change`,
#: ``set_session_name``'s `session_info`, ``compact``'s `compaction`.
_TIER_B_APPENDERS = frozenset({"compact", "set_model", "set_session_name"})

#: The rest. ``set_auto_compaction`` is the interesting member: a MUTATOR
#: (D-1-guarded, E5-cursor-carrying) that still appends nothing, which is
#: what keeps D-7 from collapsing into "mutators refuse".
_TIER_B_NON_APPENDERS = frozenset(
    {
        "get_last_assistant_text",
        "get_models",
        "get_session_name",
        "get_session_stats",
        "list_sessions",
        "set_auto_compaction",
    }
)


def _handler_body(verb: str) -> str:
    """The handler function's CODE, with the ``@command(...)`` decorator
    stripped off.

    ``inspect.getsource`` on a decorated function returns the decorator call
    too, and every verb's ``notes`` now cite ``require_durable_session`` by
    name (that is the point of finding 6) — so a naive whole-source search
    reports every verb as guarded. Slicing at the ``async def`` line is what
    makes the "does NOT call it" half of the assertion below able to fail.
    """
    handler = COMMAND_TABLE[verb].handler
    assert handler is not None
    source = inspect.getsource(handler)
    return source[source.index(f"async def {handler.__name__}") :]


def test_d7_is_answered_one_way_across_tier_b() -> None:
    """The rule ``rpc/commands.py``'s "DURABILITY in Tier B" block states,
    pinned so the tier cannot ship three answers to it again.

    Finding 6 of the Tier B review measured, on ONE ``new_session
    {"persist": false}`` session: ``set_model`` and ``set_session_name``
    refused ("this session is unpersisted" — as ``-32603`` then, as
    ``SESSION_NOT_PERSISTED`` since round-3 finding 4), ``set_auto_compaction``
    returned a cursor, and ``compact`` ran to completion and reported a
    cursor for a ``compaction`` entry that dies with the process. No verb's
    notes said which of those was the rule, so a host could not derive it
    from any one of them.

    Three things this asserts:

    1. Every APPENDING verb's handler calls ``require_durable_session``.
       Read out of the shipped SOURCE, not from a list of names this file
       carries: a handler that loses the call is exactly the regression, and
       a test holding its own copy of the answer could not see it.
    2. No non-appending verb calls it — the half that stops "restoring
       consistency" by guarding ``set_auto_compaction`` too, which would
       deny a working capability over a promise that verb never makes.
    3. The classification is TOTAL over the tier, so a new ``since="tier-b"``
       verb has to answer D-7 deliberately rather than inherit whichever
       neighbour its author copied.

    MUTATION TARGET: delete the ``require_durable_session(session,
    verb="compact")`` line from ``_handle_compact`` (claim 1), or add one to
    ``_handle_set_auto_compaction`` (claim 2).
    """
    shipped = {name for name, entry in COMMAND_TABLE.items() if entry.since == "tier-b"}
    assert shipped == _TIER_B_APPENDERS | _TIER_B_NON_APPENDERS, (
        "a Tier B verb is classified neither as appending nor as "
        "non-appending — D-7 has no answer for it"
    )

    for name in sorted(_TIER_B_APPENDERS):
        assert "require_durable_session" in _handler_body(name), (
            f"{name} appends a session-log entry but does not call "
            "require_durable_session — D-7 rule 1"
        )

    for name in sorted(_TIER_B_NON_APPENDERS):
        assert "require_durable_session" not in _handler_body(name), (
            f"{name} appends nothing, so refusing an unpersisted session "
            "denies a capability over a promise it never made — D-7 rule 2"
        )


def test_every_tier_b_verb_tells_a_host_where_the_durability_rule_lives() -> None:
    """D-7 is written down ONCE; every verb's ``notes`` — which are
    published verbatim into ``docs/RPC-PROTOCOL.md`` — point at it.

    That pointer is the whole deliverable of finding 6. The behaviours were
    already two of the three possible ones; what a host could not do was
    LOOK UP which, from the verb it happened to be calling. A verb whose
    notes never mention D-7 leaves the next host to re-derive it.

    MUTATION TARGET: delete the "D-7" clause from any one Tier B verb's
    ``notes``.
    """
    for name in sorted(_TIER_B_APPENDERS | _TIER_B_NON_APPENDERS):
        notes = COMMAND_TABLE[name].notes
        assert notes is not None
        assert "D-7" in notes, (
            f"{name}'s notes never cite D-7, so a host reading only this "
            "verb cannot find out whether it refuses an unpersisted session"
        )


def test_every_appending_verb_says_where_the_entry_lands_and_for_how_long() -> None:
    """D-6/unit S, stated by ALL the verbs it applies to rather than by two
    of the three.

    ``--mode rpc`` stopped writing to the user's ``~/.tau/sessions`` and now
    defaults to a private ``<tmp>/.tau-<uid>/sessions``, which most systems clear
    on reboot. That turns "this entry is durable" — the promise D-7 refuses
    to make on an unpersisted session — into "durable until the machine
    restarts", and it is a promise only the APPENDING verbs make, so it is
    exactly their notes that have to carry the qualification.

    Found at this round's integration: unit S wrote it into ``set_model`` and
    ``set_session_name``; finding 6 then made ``compact`` the third member of
    the same rule, from a tree where that sentence did not exist yet, and the
    tier shipped two verbs answering "where does my write live?" and one
    silent about it while its own notes described a compaction entry sitting
    "durably in the session log for the next process to find".

    MUTATION TARGET: delete either clause from any one appending verb's
    ``notes``.
    """
    for name in sorted(_TIER_B_APPENDERS):
        notes = COMMAND_TABLE[name].notes
        assert notes is not None
        assert "<tmp>/.tau-<uid>/sessions" in notes, (
            f"{name} appends a durable entry but never says WHERE it lands — "
            "a host reading this verb would assume the user's ~/.tau/sessions "
            "(D-6, unit S)"
        )
        assert "MACHINE UPTIME" in notes, (
            f"{name} appends a durable entry but never says how long that "
            "durability lasts — under the RPC default it ends at the next "
            "reboot (D-6, unit S)"
        )


def _tier_b_prose_enumerations() -> list[tuple[str, str, frozenset[str]]]:
    """The places in ``rpc/commands.py`` that ENUMERATE the tier in prose
    rather than deriving it from ``COMMAND_TABLE``, as (label, text, the verbs
    that list claims to name).

    Each slice is the LIST ITSELF, not the paragraph around it. That is not
    fussiness: the first draft of this helper handed the whole "E5 in Tier B"
    block to the assertion below, and the mutation that restores the pre-fix
    rule-2 sentence STAYED GREEN — the block's own header happens to mention
    ``get_models`` while explaining why the counts were removed, which is
    enough to satisfy a whole-block search. A slice that a neighbouring
    sentence can satisfy pins nothing.
    """
    source = inspect.getsource(commands_module)
    e5 = source.index("# E5 in Tier B")
    rule1 = source.index("#   1. A MUTATING verb", e5)
    rule2 = source.index("#   2. A READ never carries one.", e5)
    rule3 = source.index("#   3. Absence is never a signal.", e5)

    # The D-7 block beside it, sliced the same way and for the same reason:
    # its two rules are the only place the appends/does-not-append split is
    # written out by name.
    d7 = source.index("# DURABILITY in Tier B")
    d7_rule1 = source.index("#   1. A verb that APPENDS", d7)
    d7_rule2 = source.index("#   2. A verb that appends NOTHING", d7)
    d7_rule3 = source.index("#   3. It is the SESSION's durability", d7)

    module_doc = commands_module.__doc__
    assert module_doc is not None
    # Just the **Tier B** SENTENCE that lists the verbs — not the whole
    # docstring (Tier A's own list must not satisfy this) and not even the
    # whole paragraph: the same measurement as above caught that too. The
    # paragraph's trailing parenthetical explains where `get_models` came
    # from, so deleting `get_models` from the LIST left the paragraph still
    # naming it and the assertion still green. The list ends where "All of
    # them are wired" begins; if that anchor is ever reworded this raises
    # ValueError and the test errors loudly rather than degrading to a
    # weaker check.
    tier_b_para = module_doc[module_doc.index("**Tier B**") : module_doc.index("**Tier C**")]
    tier_b_list = tier_b_para[: tier_b_para.index("All of them are wired")]

    guard_doc = turn_safety_guard.__doc__
    assert guard_doc is not None

    return [
        ("the 'E5 in Tier B' block, rule 1", source[rule1:rule2], _TIER_B_MUTATORS),
        ("the 'E5 in Tier B' block, rule 2", source[rule2:rule3], _TIER_B_READS),
        (
            "the 'DURABILITY in Tier B' block, rule 1",
            source[d7_rule1:d7_rule2],
            _TIER_B_APPENDERS,
        ),
        (
            "the 'DURABILITY in Tier B' block, rule 2",
            source[d7_rule2:d7_rule3],
            _TIER_B_NON_APPENDERS,
        ),
        (
            "commands.py's module docstring",
            tier_b_list,
            _TIER_B_MUTATORS | _TIER_B_READS,
        ),
        (
            "turn_safety_guard's docstring",
            guard_doc,
            _TIER_B_MUTATORS | _TIER_B_READS,
        ),
    ]


def test_the_prose_enumerations_of_tier_b_name_every_verb() -> None:
    """Every hand-written LIST of Tier B verbs in ``rpc/commands.py`` names
    all of them — the drift finding 10's first bullet is an instance of, one
    round later and in a different shape.

    Five agents edited this file in parallel without seeing each other, and
    ``get_models`` landed last. It left three prose enumerations describing a
    tier that no longer existed: the "E5 in Tier B" block said "all seven
    verbs" and rule 2 named three reads out of four; ``set_model``'s ``notes``
    — which are PUBLISHED, verbatim, into ``docs/RPC-PROTOCOL.md`` — told a
    host that "only the three reads omit" ``cursor``; and
    ``turn_safety_guard``'s docstring named two of the four guard-free reads.
    None of it was catchable: the classification test above pins
    ``_TIER_B_READS`` against ``COMMAND_TABLE`` but never looks at a sentence.

    So this asserts the property those sentences claim, against the same
    ``COMMAND_TABLE``-derived sets: a verb classified in this file must be
    NAMED by every list in it. The lists stay, because "which verbs take D-1's
    guard" is worth reading in place.

    STATED LIMIT, not hidden: the verb COUNTS ("all seven verbs", "only the
    three reads") were deleted from the prose rather than pinned. A first draft
    of this file asserted no count appears at all; it flagged
    ``get_models is the tier's one verb with no row in RPC-TIER-B.md``, a true
    and useful sentence, so it was policing English rather than a property and
    was dropped. A count that comes BACK is therefore not caught directly —
    what catches it is that this test forces an editor adding a verb to visit
    every one of these locations, which is where a stale tally lives.

    Matching is on the BACKTICKED name (```compact```), not on the bare word,
    for one measured reason: rule 1's own sentence contains the phrase "a
    compaction that found nothing to compact", so a bare-word search for
    ``compact`` passes that slice whether or not the enumeration still names
    the verb. Every one of these lists writes its verbs in backticks (single
    in the comment blocks, double in the docstrings — the double pair contains
    the single one, so one pattern reads both), and prose about compacting
    does not.

    Mutation that reddens it: drop ``get_models`` from rule 2's enumeration in
    the "E5 in Tier B" block (i.e. restore the pre-fix sentence), or from
    ``turn_safety_guard``'s docstring. Both are single-edit and both are
    exactly what shipped.

    Reference: docs/RPC-TIER-B.md §6; finding 10 of the Tier B review.
    """
    for label, text, expected in _tier_b_prose_enumerations():
        for verb in sorted(expected):
            assert re.search(rf"`{verb}`", text), (
                f"{label} does not name the Tier B verb {verb!r} — a prose "
                "enumeration of this tier that is missing a verb is the "
                "stale-string class finding 10 named"
            )
