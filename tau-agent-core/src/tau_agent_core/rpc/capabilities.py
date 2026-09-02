"""The RPC capability document — block [8], K1/K2/K3.

Reference: docs/REMOTE-CONTROL.md §4 block [8] (K1-K3), §6 "Recommendation:
audit, do not generate" (items 1 and 3), §8 decision 8, §9 R-T2, §10.

This module does exactly one thing: WALK the two artifacts unit 2A/2B
already built — `commands.COMMAND_TABLE` (hand-written, §6 point 1) and
`rpc_event_schema` (generated from `AgentEvent`, §6 point 3) — into the one
document K1 describes. It does not decorate or introspect `AgentSession`
(§6 A1-A6); every field below is a projection of data those two modules
already declare. `docs/RPC-PROTOCOL.md` (K3) is `rpc.protocol_doc.render()`
applied to this module's `build_capabilities()` output, checked by
`tests/test_rpc_protocol_doc.py` against drift.

`COMMAND_TABLE` is imported inside `build_capabilities()`, not at module
scope: `commands.py` imports THIS module (to register the `get_capabilities`
verb against it — see the bottom of commands.py), so a top-level `from
tau_agent_core.rpc.commands import COMMAND_TABLE` here would be a genuine
import cycle, not a merely awkward one. Deferring the lookup to call time
(the same lazy-import idiom `agent_session.py`'s `load_extensions` uses to
avoid its own cycle with `sdk.py`) costs nothing — both modules are fully
imported by the time any handler actually runs.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core import rpc_event_schema

#: K2 — version negotiation. `MAJOR.MINOR`, bumped BY HAND (never derived —
#: nothing here can tell "additive" from "breaking" for you):
#:
#: - MAJOR bumps on a BREAKING change to something already on the wire: a
#:   command, event, or field removed or renamed; a field's meaning or type
#:   changed; a previously optional `params_schema` entry made required; an
#:   error code's meaning repurposed. A host pinned to a MAJOR version may
#:   treat a mismatch as "do not even try" (K2: "a host may refuse to run
#:   against an incompatible protocol rather than discovering it on first
#:   failure").
#: - MINOR bumps on an ADDITIVE change: a new command, a new optional
#:   `params_schema` property, a new field on an existing result or event.
#:   E3 is what makes a MINOR mismatch safe to ignore: "a client MUST
#:   ignore any field it does not recognize." A newly declined verb
#:   (Tier D gaining a member) is also additive — declining is documented
#:   information, not a removal of anything that worked.
#:
#: `1.0` named the FIRST version of the redesigned wire — the JSON-RPC 2.0 +
#: declarative-command-table + capability-document surface built across
#: units 2A-2C. It is not a continuation of the pre-2A `rpc.py` (`send_prompt`
#: / `get_session_info` / …), which was never versioned and is not wire-
#: compatible with this one.
#:
#: `1.1` (phase 3, H1): three new commands (`new_session`/`fork`/
#: `switch_session`) and a new `result_schema` field on every non-declined
#: `commands[]` row — both purely additive per the MINOR rule above (a new
#: command a host does not recognize is simply unreachable to it; a new
#: result field is ignored by a client honoring E3).
#:
#: `1.2` (Tier B): the tier's new verbs, plus the new top-level `limits` key
#: below (T7, review finding 9). Additive on both counts — but the `limits`
#: half is the one that actually needs the bump to be USEFUL rather than
#: merely permitted: a host that must know whether it may rely on
#: `limits.max_request_line_bytes` being there, instead of guessing a safe
#: request size, is exactly the "refuse to run rather than discover it on
#: first failure" case K2 exists for.
#:
#: Also 1.2, and NOT its own bump: the two error codes the tier added
#: (`REQUEST_TOO_LARGE`, and `SESSION_NOT_PERSISTED` at the review's round 3).
#: A code is additive to a host that does not recognize it — an unknown
#: `-32xxx` is still an error response with a message — and the D-7 refusal
#: that moved off `INTERNAL_ERROR` moved WITHIN the tier that introduced it,
#: so no host was ever entitled to the old code. A new code against a
#: SHIPPED version would be a different question and would earn a MINOR bump
#: on the same "needs the bump to be useful" ground `limits` did.
#:
#: `1.3`: `get_state` gained `addressable`. Additive — a host honoring E3
#: ignores an unrecognized result field — and it is one of the cases where
#: the bump is what makes the field USEFUL rather than merely permitted, the
#: same argument `limits` made at 1.2. `--mode rpc` now honors
#: `--no-session`, so the startup session's persistence became a variable
#: where it had been a constant; a host that must decide whether to rely on
#: `set_model`'s cursor needs to know whether it may READ `addressable` or
#: must instead treat -32004 as the only signal. That is exactly K2's
#: "refuse to run rather than discover it on first failure".
#: `1.4`: the two halves of a usable chat editor, for a head that is not the
#: TUI. `complete_path` (a new Tier B verb) and `expand_attachments` (a new
#: optional property on `submit`/`prompt`, with a matching optional
#: `attachments` key on their result). Additive on all three counts — a host
#: that does not recognize the verb never calls it, and one that does not set
#: the flag gets byte-identical behaviour to 1.3.
#:
#: It earns the bump on the same "needed to be USEFUL rather than merely
#: permitted" ground `limits` did at 1.2. Before this, `@notes.txt` sent over
#: this wire reached the model as those eleven literal characters: expansion
#: was a FRONTEND job (docs/FILE-ATTACHMENTS.md §2, `Parley._expand_attachments`)
#: and the RPC wire had no frontend to do it. A head therefore had to choose
#: between re-implementing the `<attachment>`/`<reference>` vocabulary in its
#: own language and shipping an editor whose `@` did nothing. `protocol_version`
#: is how it now decides which, without discovering the answer by watching a
#: model fail to see a file it was told about.
PROTOCOL_VERSION = "1.4"

#: D1 — the one native dialect (JSON-RPC 2.0). `--rpc-dialect pi` (G8's
#: stated-expiry compat shim) does not exist yet; when it does, a host that
#: negotiates it reads a different value here.
DIALECT = "jsonrpc-2.0"

#: K2, concretely. A host calls `get_capabilities` — the one command with NO
#: preconditions (no session state, no prior admission) — as the FIRST
#: request on a fresh connection, before sending anything that mutates
#: state (`submit`/`prompt`/`abort`). `get_capabilities` costs nothing to
#: call this early: it is read-only, side-effect-free, and answers
#: synchronously (unlike `submit`/`prompt`'s C3 dual completion). Reading
#: `protocol_version` off the response and comparing it against whatever
#: MAJOR version the host was built against IS the negotiation — there is
#: no separate handshake verb, and none is needed: K1 already promises this
#: field, so a second wire message to fetch just the version would be a
#: distinct exchange over the same information get_capabilities already
#: carries. A host that finds an incompatible MAJOR version simply never
#: sends a second request and exits — the "refuse to run" K2 asks for is a
#: HOST-side decision, not a rejection this process issues, because by the
#: time get_capabilities has answered, the process is already running and
#: has not been asked to do anything a version mismatch could corrupt.
NEGOTIATION_NOTE = (
    "Call get_capabilities (no params) first on every new connection, before "
    "any mutating command. Compare protocol_version's MAJOR component against "
    "what this host was built against; refuse to send anything else on a "
    "mismatch rather than discovering it on the first failing request."
)


def build_limits() -> dict[str, Any]:
    """The `limits` block of the capability document (K1, T7).

    Every bound this process enforces on what a host may SEND, published as
    a number rather than left to be discovered by tripping over it.
    docs/REMOTE-CONTROL.md §10 open question 4 names this exact shape for
    this exact purpose: "an advertised max ... the host is obliged to
    respect", living "in the capability document rather than in the
    transport".

    Read live off the transport's own constant, never re-declared here: the
    number a host is told and the number the reader enforces are the same
    object, so they cannot drift into a document promising 8 MiB while the
    reader refuses at 64 KiB — which is the failure this key exists to
    retire (`transport.MAX_REQUEST_LINE_BYTES`, review finding 9). Imported
    inside the function rather than at module scope so the lookup happens at
    CALL time: a module-scope `from ... import MAX_REQUEST_LINE_BYTES` would
    bind today's integer once and then keep answering with it, which is the
    stale-copy problem this whole function exists to avoid, one level down
    (and is why `rpc/__init__.py` proxies the transport globals through
    `__getattr__` rather than re-importing them).

    One entry today. A MAP rather than a bare top-level
    `max_request_line_bytes` field, because the next bound to be published
    — §10's own still-open in-flight-request cap is the obvious candidate —
    is then an addition INSIDE this object rather than another top-level
    key, and a host that already ignores unknown fields (E3) gets that
    addition for free.
    """
    from tau_agent_core.rpc.transport import MAX_REQUEST_LINE_BYTES

    return {"max_request_line_bytes": MAX_REQUEST_LINE_BYTES}


def build_capabilities() -> dict[str, Any]:
    """The `get_capabilities` payload (K1): `{protocol_version, dialect,
    commands[], events[], event_schema, ui_methods[], declined[{name,
    reason}], limits{}}`.

    `commands[]` and `declined[]` are a WALK of `commands.COMMAND_TABLE` —
    never hand-copied (§6's whole point: the table and the document must be
    unable to drift apart because one is generated from the other).
    `events[]`/`event_schema` come from `rpc_event_schema.event_capability_doc()`
    unchanged (§6 point 3 — already generated, from `AgentEvent`).
    `ui_methods` is always `[]` in v1 — RC3: "the honest statement that the
    reverse channel does not exist" — present as an empty list, never
    omitted, because an absent key would claim ignorance rather than
    absence.
    """
    from tau_agent_core.rpc.commands import COMMAND_TABLE  # see module docstring

    commands_out: list[dict[str, Any]] = []
    declined_out: list[dict[str, Any]] = []
    for entry in COMMAND_TABLE.values():
        if entry.declined_because is not None:
            declined_out.append({"name": entry.name, "reason": entry.declined_because})
        else:
            commands_out.append(
                {
                    "name": entry.name,
                    "tier": entry.tier,
                    "since": entry.since,
                    "notes": entry.notes,
                    "params_schema": entry.params_schema,
                    "result_schema": entry.result_schema,
                }
            )
    # Deterministic order (tier, then name) — independent of COMMAND_TABLE's
    # insertion/import order, which is a function of decorator placement in
    # commands.py and should not leak into the wire document's ordering.
    commands_out.sort(key=lambda c: (c["tier"], c["name"]))
    declined_out.sort(key=lambda d: d["name"])

    event_doc = rpc_event_schema.event_capability_doc()

    return {
        "protocol_version": PROTOCOL_VERSION,
        "dialect": DIALECT,
        "commands": commands_out,
        "events": event_doc["events"],
        "event_schema": event_doc["event_schema"],
        "ui_methods": [],
        "declined": declined_out,
        "limits": build_limits(),
    }
