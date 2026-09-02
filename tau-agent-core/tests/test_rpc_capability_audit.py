"""R-T2 — the §6 capability audit: every public session method is triaged.

Reference: docs/REMOTE-CONTROL.md §6 point 2 ("An introspective audit test,
not an introspective mechanism..."), §9 R-T2, §6 "Cost, stated honestly".

    It walks AgentSession's and AgentSessionRuntime's public methods and
    asserts each is either in the table or in an explicit
    NOT_EXPOSED = {name: reason} map. It fails when a new session method is
    added and not triaged, *and* when a table entry's handler disappears.

This is the anti-drift mechanism §6 recommends INSTEAD of decorating
`AgentSession` (§6 A1-A6): rather than deriving the wire from the session,
this audits that every session method has been consciously ROUTED — onto a
live `COMMAND_TABLE` verb (`EXPOSED`) or into a written reason it is not on
the wire (`NOT_EXPOSED`). Nothing here changes `AgentSession` itself.

Not placed in `tau_agent_core.testing` (session_log_contract.py /
session_catalog_contract.py's home) despite following that idiom closely —
an EXECUTABLE contract rather than a document. That package exists for a
different shape of problem, stated in its own docstring: a conformance
suite "a second store has to satisfy," imported and subclassed by every
implementer. There is no second implementer of "AgentSession's RPC
exposure" — this audits ONE class (soon two) against ONE command table, so
it stays a plain test module.

**AgentSessionRuntime** (docs/REMOTE-CONTROL.md §4[6], H1) was extracted from
`tau_coding_agent.app` into `tau_agent_core.agent_session_runtime` in phase
3, exactly as this docstring once said it would be: one import, one append to
`AUDITED_CLASSES`, and `EXPOSED`/`NOT_EXPOSED` extended for its public
surface (`new_session` / `fork` / `switch_session` / `dispose` /
`set_rebind_session` — the last one snake_case in τ, camelCase in pi).
"""

from __future__ import annotations

import re

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.rpc import commands

# ─────────────────────────────────────────────────────────────────────────
# EXPOSED[method name] = the COMMAND_TABLE verb that reaches it.
#
# Not always a literal 1:1 call — §6 A2 is exactly the point that most of
# the interesting verbs are NOT 1:1: `get_state` is an aggregate over six of
# these (state/is_streaming/get_model/get_usage/messages/session_log), so
# each of those six maps to "get_state" here, not to a same-named verb.
# ─────────────────────────────────────────────────────────────────────────

EXPOSED: dict[str, str] = {
    "submit": "submit",
    "abort": "abort",
    "messages": "get_messages",
    "state": "get_state",
    "is_streaming": "get_state",
    "get_model": "get_state",
    "get_usage": "get_state",
    "get_extension_commands": "get_commands",
    # AgentSessionRuntime (phase 3, H1) — one verb per method, 1:1.
    "new_session": "new_session",
    "fork": "fork",
    "switch_session": "switch_session",
    # ── Tier B (docs/RPC-TIER-B.md). Eight marker regions below, one per verb,
    # alphabetically ordered and CONTIGUOUS — see rpc/commands.py's own Tier B
    # marker-region comment for why an empty pair is a reservation, not
    # clutter to delete. A verb writes an entry here ONLY if wiring it makes
    # an AgentSession/AgentSessionRuntime method newly reachable — some do
    # not (a verb the RPC layer computes without calling a new session
    # method, e.g. one already covered by an existing EXPOSED entry, leaves
    # this region empty on purpose).
    ### begin tier-b:compact
    "compact": "compact",
    ### end tier-b:compact
    ### begin tier-b:complete_path
    # Deliberately empty (protocol 1.4). The verb touches no AgentSession or
    # AgentSessionRuntime member at all — public or private: it calls the pure
    # `attachments.complete_attachment` against the process working directory.
    # There is nothing for wiring it to make newly reachable, which is also
    # why it needs no D-1 guard and answers mid-turn.
    ### end tier-b:complete_path
    ### begin tier-b:get_last_assistant_text
    # B6: left EMPTY on purpose — get_last_assistant_text reads
    # AgentSession.messages, which "messages": "get_messages" above already
    # covers. No NEW AgentSession/AgentSessionRuntime method is reached.
    ### end tier-b:get_last_assistant_text
    ### begin tier-b:get_models
    # Deliberately empty (finding 7 of the Tier B review). The verb reads
    # `session._model_resolver` — a PRIVATE attribute, which `_public_members`
    # never sees (leading underscore), the same idiom `get_tools` uses for
    # `_tools` and `get_session_stats` for `_compaction_settings`. The one
    # PUBLIC member in the neighbourhood, `set_model_resolver`, keeps its
    # pre-existing NOT_EXPOSED entry below unchanged and outside any marker
    # region: this verb does not make it wire-reachable — a host still cannot
    # BIND a resolver, only read the names the one bound at startup accepts.
    ### end tier-b:get_models
    ### begin tier-b:get_session_stats
    # Deliberately empty (B3, RPC-TIER-B.md D-3: "Compute in the RPC layer
    # from public surface; do not add an AgentSession method"). Every member
    # the verb touches is already triaged: `messages` and `get_model`/
    # `get_usage` above (EXPOSED, each mapped to the verb that FIRST reached
    # it — this dict names one verb per member, not every verb that reads
    # it), `session_log` in NOT_EXPOSED below (this verb reads `.entries()`
    # off it; that entry says why the property itself is still not
    # wire-shaped), and `_compaction_settings`, which `_public_members` never
    # sees at all (leading underscore). No NEW AgentSession/
    # AgentSessionRuntime method became reachable, so per the marker-region
    # comment above there is nothing to add.
    ### end tier-b:get_session_stats
    ### begin tier-b:list_sessions
    # Deliberately empty (finding 8 of the Tier B review), for the same reason
    # `get_models`' region above is: the verb reads `runtime._catalog` and
    # `runtime._cwd` — PRIVATE attributes, which `_public_members` never sees
    # (leading underscore). `SessionCatalog.list` is not a member of either
    # AUDITED_CLASS at all; it belongs to the catalog those two attributes
    # hold, which this audit does not walk. The public members in the
    # neighbourhood — `new_session`/`fork`/`switch_session` — keep their
    # existing EXPOSED entries above, unchanged: this verb makes no new one
    # reachable, it makes the ids the LAST of them takes discoverable.
    ### end tier-b:list_sessions
    ### begin tier-b:set_auto_compaction
    # Deliberately empty (B4, RPC-TIER-B.md D-4 / §1): the verb mutates
    # `session._compaction_settings.enabled` directly, and `_compaction_settings`
    # is a private attribute — never a member of `_public_members(AgentSession)`
    # (leading underscore), so it was never in EXPOSED or NOT_EXPOSED before this
    # unit either. No public AgentSession/AgentSessionRuntime method became
    # newly reachable, so per the marker-region comment above there is nothing
    # to add here.
    ### end tier-b:set_auto_compaction
    ### begin tier-b:set_model
    "set_model": "set_model",
    ### end tier-b:set_model
    ### begin tier-b:set_session_name
    # Deliberately empty: `set_session_name`/`get_session_name` are not
    # public methods on AgentSession or AgentSessionRuntime (AUDITED_CLASSES
    # below) — they live on ExtensionAPI (extension_types.py), which this
    # audit does not walk, and the two verbs call
    # extension_types.apply_session_name/read_session_name directly
    # (docs/RPC-TIER-B.md B5), never a new AgentSession method. There is
    # nothing here for either verb to make newly reachable.
    ### end tier-b:set_session_name
}

# ─────────────────────────────────────────────────────────────────────────
# NOT_EXPOSED[method name] = why a host cannot reach it over RPC today.
#
# §6 "Cost, stated honestly": every reason says WHY, not "internal". Two
# different KINDS of reason appear below, and each entry says which:
#
#   (a) Tier B/C candidates (docs/REMOTE-CONTROL.md §3) that already exist
#       on AgentSession and are cheap to wire, but this unit does not wire
#       them — deliberately deferred future work, not a permanent refusal.
#   (b) methods that are not wire-shaped at all: Python-callable observer
#       APIs, TUI-only affordances, construction-time frontend wiring, or
#       internal helpers a verb already reaches indirectly. These are not
#       expected to ever become EXPOSED entries as-is.
# ─────────────────────────────────────────────────────────────────────────

NOT_EXPOSED: dict[str, str] = {
    # -- (a) Tier B/C: exists, cheap, deliberately not wired by this unit --
    # set_model's and compact's entries that lived here moved to EXPOSED
    # (docs/RPC-TIER-B.md B1 and B2) — see the EXPOSED dict's own comment on
    # why deleting, not moving into the tier-b marker region below, is the
    # ordinary case.
    "compact_messages": (
        "A caller-supplied-list variant of compact(), used internally by "
        "the auto-compaction path — not a standalone Tier B candidate "
        "distinct from compact() itself; wiring compact would not need this."
    ),
    "disable_extension": (
        "Tier C candidate (§3: 'already implemented, trivially exposed') — "
        "not yet wired to an RPC verb; deferred, not declined."
    ),
    "enable_extension": (
        "Tier C candidate (§3: 'already implemented, trivially exposed') — "
        "not yet wired to an RPC verb; deferred, not declined."
    ),
    "reload_extension": (
        "Tier C candidate (§3: 'already implemented, trivially exposed') — "
        "not yet wired to an RPC verb; deferred, not declined."
    ),
    "list_managed_extensions": (
        "Backs the not-yet-wired Tier C extension-management group above "
        "(enable/disable/reload) as the listing a management verb would "
        "return; no verb reads it yet."
    ),
    "resolve_extension_target": (
        "A token-resolution helper ('/extensions <verb> <token>' parsing) "
        "for the same not-yet-wired Tier C group; not independently useful "
        "as its own verb."
    ),
    # -- (b) not wire-shaped: Python-callable observer / construction APIs --
    "session_log": (
        "The settable persistence-facade seam (H1's rebind point for "
        "new_session/fork, phase 3, AgentSessionRuntime) — no verb hands a "
        "host the SessionLog itself, nor lets one set it. Verbs read DERIVED "
        "values through it: get_state and the Tier B mutators take `.cursor` "
        "(E5), and get_session_stats (D-3) scans `.entries()` for the last "
        "compaction. Neither makes this property wire-reachable in the sense "
        "EXPOSED means — a host can never name it."
    ),
    "set_model_resolver": (
        "A construction-time binding a frontend performs once (closure over "
        "~/.tau/config.json's 'models' map) so set_model can resolve a NAME "
        "— not a per-call verb any host would invoke over the wire; the RPC "
        "process wires this during setup exactly as the TUI does."
    ),
    "record_side_usage": (
        "An internal ledger write invoked by out-of-loop completion paths "
        "(compaction, branch summaries) inside AgentSession itself — never "
        "called by a frontend or host, so there is no wire action for a "
        "verb to perform here."
    ),
    "side_usage": (
        "Cumulative token spend OUTSIDE the agent loop (compaction, branch "
        "summaries) — a real number a cost-tracking host would want, but "
        "get_state does not fold it in today (its own notes list exactly "
        "what it aggregates, and this is not among them); a genuine gap "
        "left honest rather than silently merged into get_usage's answer, "
        "which is deliberately just the per-completion number."
    ),
    "subscribe": (
        "The in-process Python-callable observer API for the AgentEvent "
        "stream. A wire host never calls this directly — RPCHandler itself "
        "is the ONE permanent subscribe() call for the whole connection "
        "(RPCHandler.__init__); every wire event a host sees rides that "
        "single subscription, not a per-call verb."
    ),
    "subscribe_channel": (
        "Same category as subscribe() — a Python-callable observer API, "
        "here for the 'branch_event'/'submission_start' string channels. "
        "The eventual wire-shaped exposure of the branch data it backs is "
        "Tier C's open_lane/list_lanes/close_lane (§3), not this raw method, "
        "which cannot itself be called from outside the process."
    ),
    "route_session_event": (
        "The seam-3 bridge from tau-coding-agent's session_store event "
        "stream onto this session's extension bus — wired once by the TUI "
        "at construction, not invoked per-call by any host. RPC mode has no "
        "session_store to bridge from in the first place."
    ),
    "emit_session_start": (
        "The session-construction lifecycle hook, fired once by whichever "
        "frontend builds the session, immediately after extension load. A "
        "verb letting a host re-fire this mid-connection would let it "
        "replay a moment no extension is written to see twice."
    ),
    "emit_session_shutdown": (
        "The session-teardown lifecycle hook, fired once at process end "
        "(SIGTERM/stdin EOF) by the frontend — not a verb a host calls "
        "mid-session; RPC mode's own shutdown path is what will call this, "
        "not a wire request."
    ),
    "load_extensions": (
        "The startup-time extension loader (discovery, import, register) — "
        "a frontend concern invoked once during session construction. "
        "Loading NEW extension code over the wire mid-session is a "
        "materially bigger feature (a remote host requesting arbitrary code "
        "execution) than managing an ALREADY-loaded extension, which Tier "
        "C's enable/disable/reload proposes instead (§3)."
    ),
    "submit_threadsafe": (
        "The cross-thread / foreign-event-loop marshalling door "
        "(docs/SUBMISSION-LIFECYCLE.md 'Task marshalling') for a driver on "
        "a DIFFERENT thread of the SAME process (a bus client, a Textual "
        "worker). RPCHandler runs entirely on the session's own asyncio "
        "loop, so every RPC call already reaches submit() directly — there "
        "is no foreign thread here for this method to matter to."
    ),
    "is_aborted": (
        "Phase 4, T3/G5: read by RPCHandler._acquire_event_credit as an "
        "abort checkpoint for the backpressure wait itself, not by any verb "
        "handler. A host never polls this — it sends `abort` and observes "
        "the result the ordinary way (`agent_end`'s `is_error`/`error`); "
        "this property exists so a STALLED emit notices `abort()` was "
        "called, not to be read over the wire."
    ),
    "shutdown_requested": (
        "Phase 4, P3: read by transport._read_stdin after each dispatched "
        "line, not by any verb handler. A host cannot read or set this over "
        "the wire — it is how RPCHandler observes that an EXTENSION called "
        "`ctx.shutdown()`, entirely internal to the process contract (§4[7])."
    ),
    "resolve_command": (
        "A pure peek used by a frontend that renders BEFORE submitting (the "
        "TUI painting a user bubble only if a turn is actually coming). "
        "submit()'s own dispatch already calls this internally when "
        "expand_commands=True; a wire host has no pre-render step to "
        "protect and reaches command dispatch through submit regardless."
    ),
    "prompt": (
        "NOT the implementation behind the wire `prompt` verb despite the "
        "shared name — §10 decision 10 built the wire prompt on submit() "
        "directly (a Submission with defaulted provenance) so both wire "
        "verbs share ONE dual-completion implementation (C3). This method "
        "predates that: synchronous, single-completion, and it RAISES on a "
        "resolved command rather than returning one — a shape a wire host "
        "cannot reach and should not want, since it lacks C3's admission "
        "signal."
    ),
    "continue_conversation": (
        "Runs another turn with no new message — a TUI-only affordance "
        "(continuing after e.g. a manual context edit) with its own ad hoc "
        "concurrency guard that predates submit()'s admission contract (its "
        "own docstring says so). Not exposed until it is re-founded on "
        "Submission the way prompt() was."
    ),
    "set_ui_delegate": (
        "Wires extension api.ui calls to a live TUI screen (E5 §4) — "
        "meaningless over RPC, which has no reverse channel yet to be a "
        "delegate for. This is precisely the gap RC1-RC3 reserve for later, "
        "not an oversight of this unit."
    ),
    "set_extension_record_sink": (
        "Routes extension activity into the --mode json headless record "
        "stream — a construction-time frontend wiring choice (which sink "
        "object receives records), not a per-call verb. An RPC-mode sink "
        "would need the same construction-time wiring, which is a reverse-"
        "channel design question (§7.1), not a K1 command."
    ),
    "set_headless_ui_defaults": (
        "Sets the auto-answer policy for headless extension dialogs "
        "(--ui-defaults) — resolved once at process start from CLI flags "
        "or config, the same layering set_model_resolver uses. Not a "
        "per-call verb."
    ),
    "get_extension_shortcuts": (
        "Lists extension-registered keyboard shortcuts (the TUI's ctrl+e "
        "chord menu) — no wire host has a keyboard to bind chords to. Same "
        "judgment as Tier D's cycle_* declines (a keybinding affordance, "
        "not a protocol concern), though this one was never a verb at all "
        "(pi has no analogue either), so it is an omission, not a decline."
    ),
    "get_extension_command_args": (
        "Tells a palette whether a command needs a free-text argument "
        "prompt before dispatch — gates an interactive modal. A wire host "
        "either already knows the args it wants to send or does not; there "
        "is no modal on the other end of stdio for this to gate."
    ),
    "run_extension_command": (
        "The real backing for '/name args' dispatch — already reachable "
        "indirectly through submit/prompt with expand_commands=True (via "
        "resolve_command + the command-dispatch path), which is the "
        "sanctioned route (§10). A standalone verb would be a second, "
        "ungated way to invoke an extension command outside the Submission/"
        "provenance pipeline — the same 'second privileged path' argument "
        "the send_tool_result decline already makes for tool execution."
    ),
    # -- AgentSession, phase 3 (H1-H4) additions --
    "extension_runner": (
        "Read-only access to the mutating-hook dispatcher, added so "
        "AgentSessionRuntime can fire its own session_before_switch veto "
        "hook (H2) through the same mechanism tool_call/input/etc. already "
        "use. Not itself wire-shaped — a host never calls a Python property; "
        "the hook it exposes is reached indirectly, through new_session/"
        "fork/switch_session's {cancelled} outcome."
    ),
    "turn_lock": (
        "Read-only access to the turn-admission asyncio.Lock, added so "
        "AgentSessionRuntime can guarantee H4 atomicity (no event from a "
        "swapped-out session arrives after the new_session/fork/"
        "switch_session response — see agent_session_runtime.py's module "
        "docstring). A synchronization primitive, not a verb; a host never "
        "acquires a lock over the wire."
    ),
    # -- AgentSessionRuntime, phase 3 (H1) --
    "dispose": (
        "Process-lifetime teardown (pi's dispose()) — fires "
        "session_shutdown once, at process end. Not a per-call verb a host "
        "invokes mid-connection; RPC mode's own shutdown path "
        "(rpc_mode.py's finally) calls this, not a wire request."
    ),
    "set_rebind_session": (
        "Construction-time wiring: the caller (app.py, rpc_mode.py) installs "
        "its own post-swap callback (re-subscribing a renderer, a model "
        "resolver) once, the same layering set_model_resolver/"
        "set_ui_delegate already use. Not a per-call verb — a host has no "
        "callback to hand across a wire protocol."
    ),
    # ── Tier B (docs/RPC-TIER-B.md). Eight marker regions below, one per verb,
    # alphabetically ordered and CONTIGUOUS — see the EXPOSED dict above (and
    # rpc/commands.py's own Tier B marker-region comment) for why an empty
    # pair is a reservation, not clutter to delete. A verb moves its existing
    # NOT_EXPOSED "deferred" entry here only if it turns out NOT to expose
    # the AgentSession method that entry names (unusual — the ordinary case
    # is deleting that pre-existing entry and adding to EXPOSED instead);
    # most Tier B units are expected to leave this region empty.
    ### begin tier-b:compact
    ### end tier-b:compact
    ### begin tier-b:complete_path
    # Deliberately empty, symmetric with the EXPOSED region above: the verb
    # defers no AgentSession method because it reaches none.
    ### end tier-b:complete_path
    ### begin tier-b:get_last_assistant_text
    # B6: left EMPTY, symmetric with the EXPOSED region above — this verb
    # does not name a deferred AgentSession method either; "messages" is
    # already triaged (EXPOSED) and stays there.
    ### end tier-b:get_last_assistant_text
    ### begin tier-b:get_models
    # Deliberately empty, symmetric with the EXPOSED region above: there was no
    # pre-existing NOT_EXPOSED entry to move (no public AgentSession method
    # enumerates models — the names live on the resolver a frontend binds, not
    # on the session), and `set_model_resolver`'s own entry above stays exactly
    # where and as it was.
    ### end tier-b:get_models
    ### begin tier-b:get_session_stats
    # Deliberately empty, symmetric with the EXPOSED region above: B3 names no
    # deferred AgentSession method either. The one member it reads that lives
    # in THIS dict — `session_log` — was already triaged here before Tier B,
    # outside any marker region, and this unit does not move or re-file it.
    ### end tier-b:get_session_stats
    ### begin tier-b:list_sessions
    # Deliberately empty, symmetric with the EXPOSED region above: there was no
    # pre-existing NOT_EXPOSED entry to move (no public AgentSession or
    # AgentSessionRuntime method enumerates sessions — listing lives on the
    # `SessionCatalog` the runtime was constructed with, and this audit walks
    # neither that class nor a private attribute holding one), and this unit
    # adds none.
    ### end tier-b:list_sessions
    ### begin tier-b:set_auto_compaction
    # Deliberately empty, same reasoning as the EXPOSED region above: there was
    # no pre-existing NOT_EXPOSED["set_auto_compaction"] entry to move (no
    # public AgentSession method of that name ever existed — §1 ground truth:
    # "No accessor"), and this unit does not add one.
    ### end tier-b:set_auto_compaction
    ### begin tier-b:set_model
    ### end tier-b:set_model
    ### begin tier-b:set_session_name
    # Same reasoning as the EXPOSED region above: nothing to defer here
    # either, since the verbs never touch a new AgentSession/
    # AgentSessionRuntime method in the first place (session_log — the one
    # AgentSession property the underlying apply_session_name/
    # read_session_name DO read — already has its own pre-existing
    # NOT_EXPOSED entry above, outside any tier-b marker, unchanged by this
    # unit).
    ### end tier-b:set_session_name
}

AUDITED_CLASSES: tuple[type, ...] = (AgentSession, AgentSessionRuntime)


def _public_members(cls: type) -> set[str]:
    """Names `cls` itself defines (`vars(cls)`, not `dir(cls)`) that do not
    start with `_`.

    `vars()` rather than `dir()`: this walks exactly what THIS class's
    author writes in its body, not everything `object` hands every class
    (which would all be dunders and get filtered anyway, but `vars()` is
    the more honest "what did a change to this file just add" scope — the
    property this audit exists to catch).
    """
    return {name for name in vars(cls) if not name.startswith("_")}


def _all_audited_members() -> set[str]:
    members: set[str] = set()
    for cls in AUDITED_CLASSES:
        members |= _public_members(cls)
    return members


# ─────────────────────────────────────────────────────────────────────────
# The audit itself.
# ─────────────────────────────────────────────────────────────────────────


def test_every_public_method_is_triaged():
    """§6 point 2 / R-T2, forward direction: a NEW public method on an
    audited class that is neither EXPOSED nor NOT_EXPOSED fails the suite.

    Fail-Early: no skip, no default bucket, no 'unclassified' catch-all —
    an untriaged method is a hard failure, on purpose (2C's own mandate:
    "do not let the audit test skip, soft-pass, or warn").
    """
    for cls in AUDITED_CLASSES:
        members = _public_members(cls)
        triaged = set(EXPOSED) | set(NOT_EXPOSED)
        untriaged = members - triaged
        assert not untriaged, (
            f"{cls.__name__} has new public method(s) {sorted(untriaged)} that "
            "are neither EXPOSED nor NOT_EXPOSED (docs/REMOTE-CONTROL.md §6 "
            "point 2 / §9 R-T2). Triage each one: wire it to a COMMAND_TABLE "
            "verb and add it to EXPOSED, or add a real one-line reason to "
            "NOT_EXPOSED. 'internal' is not a reason — say why a host does "
            "not need it, or what it would mean on the wire."
        )


def test_no_stale_triage_entries():
    """The reverse direction of the same forward check: an EXPOSED/
    NOT_EXPOSED entry naming a method no longer on any audited class is
    stale and must be deleted — left in place, it would silently paper over
    a rename or removal instead of prompting a fresh triage decision."""
    live = _all_audited_members()
    stale_exposed = set(EXPOSED) - live
    stale_not_exposed = set(NOT_EXPOSED) - live
    assert not stale_exposed, (
        f"EXPOSED names method(s) no longer on any audited class: {sorted(stale_exposed)}"
    )
    assert not stale_not_exposed, (
        f"NOT_EXPOSED names method(s) no longer on any audited class: {sorted(stale_not_exposed)}"
    )


def test_exposed_and_not_exposed_are_disjoint():
    overlap = set(EXPOSED) & set(NOT_EXPOSED)
    assert not overlap, f"method(s) triaged both ways: {sorted(overlap)}"


def test_every_not_exposed_reason_is_a_real_sentence():
    """§6 'Cost, stated honestly' / C1 by extension: a placeholder reason
    ('internal', 'n/a', '') defeats the entire point of the map."""
    banned = {"internal", "n/a", "na", "todo", "tbd", ""}
    for name, reason in NOT_EXPOSED.items():
        assert reason.strip().lower() not in banned, f"{name!r}: placeholder reason: {reason!r}"
        assert len(reason) >= 20, f"{name!r}: reason too short to explain anything: {reason!r}"


def test_every_exposed_method_names_a_live_undeclined_handler():
    """§6 point 2, the OTHER failure mode it names: 'it fails ... when a
    table entry's handler disappears.' If a COMMAND_TABLE verb an EXPOSED
    entry points at is removed, renamed, or later declined, this fails."""
    for method_name, verb in EXPOSED.items():
        entry = commands.COMMAND_TABLE.get(verb)
        assert entry is not None, (
            f"EXPOSED[{method_name!r}] = {verb!r}, but {verb!r} is no longer "
            "in commands.COMMAND_TABLE at all."
        )
        assert entry.handler is not None, (
            f"EXPOSED[{method_name!r}] = {verb!r}, but {verb!r} has no handler "
            f"(declined: {entry.declined_because!r}) — a verb backing an "
            "EXPOSED method cannot itself be declined."
        )


#: "not wired", "not yet implemented" and the rest of the family, as they
#: appear in prose a HOST reads — `notes` and `declined_because` are both
#: rendered into `get_capabilities` and into docs/RPC-PROTOCOL.md.
_UNWIRED_CLAIM = re.compile(r"not\s+(?:yet\s+)?(?:wired|implemented|available|shipped|built)")

#: How much text either side of such a claim counts as "what it is about".
_CLAIM_WINDOW = 140


def test_no_capability_text_calls_a_SHIPPED_verb_unwired():
    """Finding 6 of the Tier B review: `cycle_model`'s decline reason told
    every host that `set_model` was "Tier B, not yet wired" — five commits
    after `set_model` shipped on this same table.

    That is a capability-document defect, not a comment typo: `notes` and
    `declined_because` are what `get_capabilities` returns and what
    `scripts/generate_rpc_protocol_doc.py` publishes, so regeneration
    propagates a stale claim faithfully and a host acts on it. A verb
    describing a NOT-yet-shipped thing as unwired stays legal — Tier C's
    lane verbs are honestly described that way today; what this forbids is
    naming a verb that has a handler right now.

    Scans every entry's prose, not a list of known-stale strings: the next
    instance will be written by whoever ships the next tier, and this has to
    catch it without being told the sentence in advance.
    """
    shipped = {name for name, e in commands.COMMAND_TABLE.items() if e.handler is not None}
    for name, entry in commands.COMMAND_TABLE.items():
        texts = {"notes": entry.notes, "declined_because": entry.declined_because or ""}
        for field, text in texts.items():
            for match in _UNWIRED_CLAIM.finditer(text):
                around = text[max(0, match.start() - _CLAIM_WINDOW) : match.end() + _CLAIM_WINDOW]
                named = sorted(
                    verb for verb in shipped if re.search(rf"\b{re.escape(verb)}\b", around)
                )
                assert not named, (
                    f"{name}.{field} says {match.group(0)!r} within "
                    f"{_CLAIM_WINDOW} characters of {named} — every one of "
                    "those verbs has a live handler on this table. A host "
                    "reads this string out of get_capabilities and "
                    "docs/RPC-PROTOCOL.md; say what is true now, or name "
                    "something that genuinely has not shipped."
                )


#: pi state fields `get_state`'s notes once listed as having no τ equivalent,
#: and the Tier B verb that now publishes each — as (pi field, τ verb, the
#: result-schema property that carries it).
#:
#: `get_state` itself is deliberately NOT widened to carry them; the claim
#: being pinned is only that its notes stop telling a host τ cannot answer a
#: question τ now answers. Both halves are asserted, so the table cannot rot
#: into a promise about a verb that stopped publishing the field.
_PI_STATE_FIELDS_TIER_B_ANSWERED = (
    ("sessionName", "get_session_name", "name"),
    ("autoCompactionEnabled", "get_session_stats", "compaction_settings"),
)


def test_get_state_does_not_claim_tau_lacks_what_tier_b_now_publishes():
    """The same defect as `test_no_capability_text_calls_a_SHIPPED_verb_unwired`
    above, in the shape the "unwired" scan cannot see.

    `get_state`'s notes carried a list of pi state fields "τ has no equivalent
    yet of", written in 2A. Tier B then shipped `get_session_name` (B5) and
    `get_session_stats`/`set_auto_compaction` (D-3/D-4), which answer two of
    them — and the sentence went out unchanged into `get_capabilities` and
    into docs/RPC-PROTOCOL.md, where a host building against this wire reads
    it as "do not bother asking". That is finding 10's first bullet exactly
    (`get_session_stats` promising a constant `set_auto_compaction` had
    already made variable), one verb further along.

    The regex scan cannot catch this one: "τ has no equivalent yet of pi's
    ... sessionName" names a PI field, not a τ verb, so there is no shipped
    verb inside the claim's window to trip on.

    Two assertions per row, and the first is what keeps the second honest:
    the named verb really does publish the field TODAY (so this test cannot
    quietly become a list of things nobody serves), and `get_state`'s notes
    do not name that pi field in the "no equivalent" sentence.

    Mutations that redden it, both measured: put `sessionName/` (or
    `autoCompactionEnabled/`) back into `get_state`'s "no equivalent" list;
    or rename `compaction_settings` THROUGHOUT
    `GET_SESSION_STATS_RESULT_SCHEMA`, property and `required` alike. Renaming
    only the property reddens it too, but from
    `_assert_supported_schema` at IMPORT — the schema's own
    `required`-names-a-property check gets there first — so it is the
    two-place rename that actually exercises the premise assertion here.
    """
    notes = commands.COMMAND_TABLE["get_state"].notes
    claim_start = notes.index("τ has no equivalent")
    # Only the "no equivalent" sentence — the notes go on to name both fields
    # deliberately, as things OTHER verbs answer, and that mention must not
    # satisfy (or trip) this.
    claim = notes[claim_start : notes.index(".", notes.index("fabricated", claim_start))]

    for pi_field, verb, property_name in _PI_STATE_FIELDS_TIER_B_ANSWERED:
        entry = commands.COMMAND_TABLE.get(verb)
        assert entry is not None and entry.handler is not None, (
            f"{verb!r} is supposed to be τ's answer to pi's {pi_field!r}, but "
            "it is not a live verb on this table"
        )
        assert entry.result_schema is not None
        assert property_name in entry.result_schema["properties"], (
            f"{verb}'s result schema no longer carries {property_name!r} — "
            f"τ's answer to pi's {pi_field!r} has gone, so get_state's notes "
            "may be right again and this row is the thing that is wrong"
        )
        assert pi_field not in claim, (
            f"get_state's notes still list pi's {pi_field!r} as something τ "
            f"has no equivalent of, but {verb!r} publishes it "
            f"({property_name!r}) on this same table. A host reads this "
            "string out of get_capabilities and docs/RPC-PROTOCOL.md."
        )
