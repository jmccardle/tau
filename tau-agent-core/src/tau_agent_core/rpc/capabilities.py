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

PROTOCOL_VERSION = "1.6"

DIALECT = "jsonrpc-2.0"

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
