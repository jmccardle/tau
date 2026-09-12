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

EXPOSED: dict[str, str] = {
    "submit": "submit",
    "abort": "abort",
    "messages": "get_messages",
    "state": "get_state",
    "is_streaming": "get_state",
    "get_model": "get_state",
    "get_usage": "get_state",
    "get_extension_commands": "get_commands",
    "get_qualified_commands": "get_commands",
    # AgentSessionRuntime (phase 3, H1) — one verb per method, 1:1.
    "new_session": "new_session",
    "fork": "fork",
    "switch_session": "switch_session",
    "compact": "compact",
    "set_model": "set_model",
    # The accessors that replaced the RPC layer's reaches past this class's
    # public surface. Each verb below used to read a private attribute; the
    # audit could not see any of them, which is why the count jumped without a
    # single new behaviour being added.
    "tools": "get_tools",
    "model_resolver": "get_models",
    "compaction_settings": "get_session_stats",
    "set_auto_compaction": "set_auto_compaction",
    "get_session_name": "get_session_name",
    "set_session_name": "set_session_name",
    "catalog": "list_sessions",
    "cwd": "list_sessions",
    "store": "list_sessions",
    # The three aggregate reads the wire layer used to assemble for itself.
    "get_last_assistant_text": "get_last_assistant_text",
    "get_session_stats": "get_session_stats",
    "get_last_compaction": "get_session_stats",
    "is_addressable": "get_state",
    # 0.9.8's eleven; the other four tree mutations are `tree_ops` functions
    # over a SessionLog, which this audit cannot see and never could.
    "summarize_and_navigate": "summarize_and_navigate",
    "enable_extension": "enable_extension",
    "disable_extension": "disable_extension",
    "reload_extension": "reload_extension",
    "list_managed_extensions": "list_managed_extensions",
    "get_extension_state": "get_extension_state",
    "get_extension_config": "get_extension_config",
    "set_extension_config": "set_extension_config",
}

NOT_EXPOSED: dict[str, str] = {
    "pending_request": (
        "The extension request at the cursor (docs/EXTENSION-LOCKS.md §2). A host "
        "learns of it the moment it matters — the submit refusal carries the whole "
        "entry in its error data — and a verb that answered 'is anything pending' "
        "between submissions would be a second reader of the cursor that could "
        "disagree with the first."
    ),
    "answer_request": (
        "Answering an ask appends a response entry and dispatches the action's "
        "command (docs/EXTENSION-LOCKS.md §8). Both halves are already verbs a host "
        "has: the command is run_extension_command, and the entry the answer writes "
        "is the extension's own business rather than the protocol's. It goes on the "
        "wire when a head that is not the TUI actually renders an ask, which is the "
        "point at which its argument shape can be fixed against a real caller."
    ),
    "vocabulary": (
        "The registry this session reads — τ's flows plus the ones its extensions "
        "declared (docs/EXTENSION-FLOWS.md). Every fact a host wants from it is "
        "already on the wire in the shape a host can use: get_commands says which "
        "names are flows, next_step says what one still needs, enumerate_domain "
        "lists a domain's values. Publishing the object itself would put a Python "
        "callable (a domain's enumerator) in a JSON result, where it cannot go — "
        "which is exactly why the enumerators are held apart from the domains."
    ),
    "performed": (
        "The stamp that turns a mutation's return value into a Performed "
        "carrying the cursor its capability declares. It reports a call the host "
        "already made rather than making one, so naming it over the wire would "
        "mean asking for a completion record for nothing — and every verb that "
        "does mutate already returns that shape as its result."
    ),
    "compact_messages": (
        "A caller-supplied-list variant of compact(), used internally by "
        "the auto-compaction path — not a standalone Tier B candidate "
        "distinct from compact() itself; wiring compact would not need this."
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
