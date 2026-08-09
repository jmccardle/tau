"""Renders `docs/RPC-PROTOCOL.md` FROM the capability document — K3.

Reference: docs/REMOTE-CONTROL.md §4[8] K3 ("The document is machine-
readable and is the same artifact the generated reference is built from"),
§6 point 1 ("[the hand-written command table] IS the capability document;
K3 falls out"), §1 ("`RPC-PROTOCOL.md` ... becomes the generated reference
once §6 lands").

`render()` is pure: it calls `capabilities.build_capabilities()` and
`dialect`'s error-code constants and formats them as Markdown — no file I/O
here. `scripts/generate_rpc_protocol_doc.py` (checked in) is the CLI that
writes `render()`'s output to `docs/RPC-PROTOCOL.md`;
`tests/test_rpc_protocol_doc.py` asserts the checked-in file already equals
`render()`'s current output, so a hand-edit of the doc — or a table/schema
change nobody re-ran the generator for — fails the suite instead of the doc
quietly lying (Fail Early). Never hand-edit `docs/RPC-PROTOCOL.md`.
"""

from __future__ import annotations

import json
from typing import Any

from tau_agent_core.rpc import capabilities, dialect

#: One row per JSON-RPC error code, in PRESENTATION order (the conventional
#: JSON-RPC 2.0 ordering, then τ's implementation-defined ones) — not a
#: sort by numeric value, which would put SUBMISSION_REJECTED's -32000 in a
#: confusing spot relative to the -327xx standard range it deliberately sits
#: outside of. The numeric values are read live off `dialect`'s own
#: constants (so a changed code value cannot silently desync the doc from
#: the wire); the prose descriptions are maintained by hand once, here,
#: mirroring dialect.py's own inline comments — the same "small, stable
#: vocabulary, hand-described once" choice `rpc_event_schema.py` already
#: makes for `WireEvent`'s field docstrings.
#:
#: The SET is pinned, not just the rows: `test_rpc_protocol_doc.py` asserts
#: this list covers exactly the error-code constants `dialect` exports, both
#: ways. Round-3 finding 2 of the Tier B review is why — `COMMAND_NOT_SUPPORTED`
#: had been reachable from an ordinary `submit` since Tier C and was never in
#: this table, and the round that added `REQUEST_TOO_LARGE` pinned that ROW
#: rather than the set, so nothing could fail when a code existed and was
#: undocumented. A code a host can meet and cannot look up is the same defect
#: as a stale sentence; both are prose no test could falsify.
_ERROR_ORDER: list[tuple[int, str, str]] = [
    (
        dialect.PARSE_ERROR,
        "PARSE_ERROR",
        "The client sent bytes that are not valid JSON. Two ways to get here: "
        "the line decoded but did not parse, or the line was not valid UTF-8 "
        "at all — JSON text is UTF-8, so those bytes are not JSON either. The "
        "second case carries `error.data` `{encoding, reason, byte_offset, "
        "offending_bytes}` naming the byte that broke it, and `id` is `null` "
        "(the request's id was inside the bytes that could not be read). "
        "Either way the line is discarded and the connection stays usable.",
    ),
    (
        dialect.INVALID_REQUEST,
        "INVALID_REQUEST",
        "Valid JSON, but not a valid Request object (no `method`).",
    ),
    (
        dialect.METHOD_NOT_FOUND,
        "METHOD_NOT_FOUND",
        "`method` names no table entry, or names a DECLINED one (see `declined[]`).",
    ),
    (
        dialect.INVALID_PARAMS,
        "INVALID_PARAMS",
        "`params` fails the method's `params_schema`.",
    ),
    (
        dialect.INTERNAL_ERROR,
        "INTERNAL_ERROR",
        "The handler raised something it did not raise on purpose.",
    ),
    (
        dialect.SUBMISSION_REJECTED,
        "SUBMISSION_REJECTED",
        "A `submit`/`prompt` call's `Submission` was refused by admission "
        '(e.g. `multitask_strategy="reject"` against an in-flight turn) — '
        "an expected, structured outcome, not a crash.",
    ),
    (
        dialect.COMMAND_NOT_SUPPORTED,
        "COMMAND_NOT_SUPPORTED",
        "A `submit`/`prompt` with `expand_commands: true` resolved to a command "
        "whose `performer` is `frontend` — `/tree`, `/fork`, `/extensions`, "
        "`/compact`. The core identified WHAT it is; the RPC wire has no screen "
        "to push a panel onto and will not silently no-op it. An expected, "
        "structured refusal, reachable from an ordinary Tier C call: submit the "
        "text without `expand_commands`, or use the verb that does the same job "
        "(`compact` for `/compact`, `fork` for `/fork`).",
    ),
    (
        dialect.TURN_STILL_RUNNING,
        "TURN_STILL_RUNNING",
        "A `new_session`/`fork`/`switch_session` call requested the in-flight "
        "turn stop and waited, but it did not free the admission lock within "
        "the bounded wait — an expected, structured refusal (nothing was "
        "touched; retry, or wait for `agent_end` first), not a crash and not "
        "an unbounded hang.",
    ),
    (
        dialect.REQUEST_TOO_LARGE,
        "REQUEST_TOO_LARGE",
        "One request line exceeded `limits.max_request_line_bytes` and was "
        "discarded unread, through its next LF (T7). `id` is `null` — the "
        "request's own id was inside the bytes that were never parsed — and "
        "`error.data` carries `max_request_line_bytes`, the length observed, "
        "and whether that length is exact (`line_complete: true`) or a lower "
        "bound (the line was refused while still arriving). The connection is "
        "otherwise unaffected: the next well-formed line is served normally.",
    ),
    (
        dialect.SESSION_NOT_PERSISTED,
        "SESSION_NOT_PERSISTED",
        "A verb that APPENDS a session-log entry was called on a session with "
        'no durable location — the product of `new_session {"persist": '
        "false}`, or of a process started with `--no-session` (D-7). "
        "`set_model`, `set_session_name` and `compact` refuse here; "
        "`set_auto_compaction` and every read do not, because they append "
        "nothing. Nothing was mutated before the refusal, and "
        "`error.data.method` names the verb that refused. The one honest fix "
        "is to put the "
        "connection on a persisted session (`fork`, `switch_session`, or "
        "`new_session` with `persist` left at its default) and retry. A host "
        "does not have to meet this by tripping it: `get_state` reports "
        "`addressable`, the same predicate, for whichever session the "
        "connection is on — and under `--no-session` that is false from the "
        "first request, so the answer is available before any write is "
        "attempted.",
    ),
]


#: The verbs whose response is only an ACKNOWLEDGEMENT, with the real outcome
#: arriving later on a second message (C3). Named, never counted: this list
#: went from two members to three when `compact` shipped, and the sentence
#: beside it ("These two are the whole list") went stale in the same commit.
#: `test_rpc.py` derives the same set from `COMMAND_TABLE`'s acknowledgement
#: result schemas and fails if this tuple disagrees, so a fourth such verb
#: cannot be added without the reference learning about it.
_DUAL_COMPLETION_VERBS: tuple[str, ...] = ("submit", "prompt", "compact")

#: The verb whose RESPONSE is the largest a host is guaranteed to meet — and
#: the one K2 says to send first, which is what makes the outbound-size note
#: in `render()` a live hazard rather than a footnote.
_LARGEST_RESPONSE_VERB = "get_capabilities"


def _capability_response_bytes() -> int:
    """How many bytes `get_capabilities`' result occupies on the wire.

    Measured, never asserted by hand: the note in `render()` claims this
    document is over 64 KiB, and a claim about a number is only worth making
    if the number is the real one. Serialized exactly as
    `transport._write_stdout` serializes it (compact separators), so the
    figure is the response's own contribution to its line rather than a
    prettier or uglier hypothetical. The JSON-RPC envelope and `result.method`
    (D2) add a few dozen bytes on top, which is why the doc says "before the
    envelope" rather than quoting this as the line length.
    """
    return len(json.dumps(capabilities.build_capabilities(), separators=(",", ":")))


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"


def _schema_field_rows(schema: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(field, type, description)` rows from a JSON Schema `properties` map,
    in declaration order. Handles the `anyOf: [{type: X}, {type: "null"}]`
    shape pydantic emits for `X | None` and the `enum` shape for a `Literal`
    — the two forms every `WireEvent` field actually uses.
    """
    rows: list[tuple[str, str, str]] = []
    for name, prop in schema.get("properties", {}).items():
        rows.append((name, _type_of(prop), str(prop.get("description", ""))))
    return rows


def _type_of(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return " \\| ".join(f"`{v}`" for v in prop["enum"])
    if "anyOf" in prop:
        return " \\| ".join(_type_of(sub) for sub in prop["anyOf"])
    type_name = prop.get("type", "any")
    return "`null`" if type_name == "null" else str(type_name)


def render() -> str:
    """The full Markdown text of `docs/RPC-PROTOCOL.md`, generated from
    `capabilities.build_capabilities()` plus `dialect`'s error codes.
    """
    # Deferred for the same reason `capabilities.build_capabilities()` defers
    # it (see that module's docstring): `commands` imports `capabilities`, so a
    # module-scope import here would tangle the same cycle. Only the two
    # `compaction_end` names are needed, and only to keep this hand-written
    # prose from drifting off the symbols it describes.
    from tau_agent_core.rpc import commands

    doc = capabilities.build_capabilities()
    lines: list[str] = []
    w = lines.append

    w("# τ RPC Protocol")
    w("")
    w("> **Generated reference — do not hand-edit.** Run")
    w("> `python scripts/generate_rpc_protocol_doc.py` after any change to")
    w("> `tau_agent_core.rpc.commands.COMMAND_TABLE` or")
    w("> `tau_agent_core.rpc_event_schema.WireEvent`, and commit the result.")
    w("> `tests/test_rpc_protocol_doc.py` fails the suite if this file and the")
    w("> generator disagree (K3, docs/REMOTE-CONTROL.md §4[8]).")
    w(">")
    w("> Design of record: `docs/REMOTE-CONTROL.md`. This file is the generated")
    w("> capability reference §6 promised it would become.")
    w("")
    w("## Overview")
    w("")
    w(
        "τ speaks **JSON-RPC 2.0** over LF-delimited stdin/stdout. Full transport "
        "contract (framing, stdout takeover, backpressure, signals) is "
        "docs/REMOTE-CONTROL.md §4 blocks [1] and [7] — not restated here, to keep "
        "this file's only source of truth the capability document itself."
    )
    w("")
    w("## Version negotiation")
    w("")
    w(f"- **Protocol version:** `{doc['protocol_version']}`")
    w(f"- **Dialect:** `{doc['dialect']}`")
    w("")
    w(capabilities.NEGOTIATION_NOTE)
    w("")
    w(
        "`protocol_version` is `MAJOR.MINOR`. MAJOR bumps on a change that breaks "
        "something already on the wire (a command/event/field removed or renamed, "
        "a field's meaning or type changed, a previously optional param made "
        "required). MINOR bumps on a purely additive change (a new command, a new "
        "optional param, a new field on an existing result or event) — additive "
        "changes are always safe to ignore: **a client MUST ignore any field it "
        "does not recognize.**"
    )
    w("")
    w("## Limits")
    w("")
    w("### What a host may SEND")
    w("")
    w(
        "Bounds this process enforces, as numbers rather than as something to discover "
        "by tripping over it (K1/T7). Every value below is read live off the "
        "code that enforces it, so this section cannot promise one bound while "
        "the process applies another. `get_capabilities` returns the same map "
        "as `limits`."
    )
    w("")
    w("| Limit | Value | What happens past it |")
    w("|---|---|---|")
    w(
        f"| `max_request_line_bytes` | `{doc['limits']['max_request_line_bytes']}` | "
        "The line is refused with `REQUEST_TOO_LARGE` (see [Error "
        "codes](#error-codes)) and discarded through its next LF. **The process "
        "does not exit and the connection stays usable** — the next well-formed "
        "line is served normally. Counted in bytes of the line itself, "
        "excluding the terminating LF; a line of exactly this length is "
        "accepted. A host with a payload larger than this must split the WORK "
        "(several requests), not the line: there is no continuation frame, and "
        "a request split across two lines is two malformed requests. |"
    )
    w("")
    w("### What a host must be prepared to RECEIVE")
    w("")
    w(
        "**There is no matching bound on τ's side of the wire, and a host must "
        "not impose one** (T8). Response lines are as large as the answer is: "
        f"`{_LARGEST_RESPONSE_VERB}` alone answers with **more than 64 KiB** "
        f"(its result serializes to {_capability_response_bytes():,} bytes, "
        "before the JSON-RPC envelope) — and that is the one verb [version "
        "negotiation](#version-negotiation) tells every host to send FIRST, "
        "before anything else. `get_messages` has no ceiling at all."
    )
    w("")
    w(
        "This is worth stating because 64 KiB is the *default* line length in "
        "widely-used stream readers — `asyncio.StreamReader` among them, whose "
        "`readline()` raises `ValueError: Separator is found, but chunk is "
        "longer than limit` rather than returning a short read. It is the same "
        "number, and the same failure, that `max_request_line_bytes` above "
        "exists to have fixed on the inbound side. A host that frames its own "
        "lines over chunked reads has neither problem; a host that delegates "
        "framing to a capped `readline` has chosen a fatal input class without "
        "meaning to."
    )
    w("")
    w(
        "Events are the exception, and deliberately: no unbounded field is ever "
        "*pushed* (G3 — see [Event stream](#event-stream)). The rule above is "
        "about RESPONSES, which a host asked for."
    )
    w("")
    w("## Commands")
    w("")
    w(
        "One entry per non-declined `COMMAND_TABLE` row, grouped by tier. `since` "
        "names the unit that added the verb, not a protocol version."
    )
    w("")
    current_tier: str | None = None
    for row in doc["commands"]:
        if row["tier"] != current_tier:
            current_tier = row["tier"]
            w(f"### Tier {current_tier}")
            w("")
        w(f"#### `{row['name']}`")
        w("")
        w(f"*Since {row['since']}.* {row['notes']}")
        w("")
        w("**Params schema:**")
        w("")
        w(_json_block(row["params_schema"]))
        w("")
        w("**Result schema** (see [Response envelope](#response-envelope) for")
        w("what rides on top of this on every response):")
        w("")
        w(_json_block(row["result_schema"]))
        w("")
    w("## Response envelope")
    w("")
    w(
        "What follows is true of every response and is NOT repeated in each "
        "verb's result schema above. (This paragraph counted the bullets "
        'below — "Two things" — until the Tier B review\'s integration, by '
        "which point there were four; a tally in prose beside a list that "
        "grows is a claim nothing keeps.)"
    )
    w("")
    w(
        "- **Every success response echoes the method it answered** as "
        "`result.method` (D2) — JSON-RPC gives only `id`; a human (or a log "
        "processor) reading a transcript should not have to maintain the "
        "id→method map. This field is added by the transport on top of "
        "whatever the handler returns, so it is never listed in a verb's own "
        "result schema above."
    )
    w(
        "- **`submit`/`prompt` have TWO completions** (C3): an immediate "
        "response acknowledging *acceptance* — the `SUBMIT_RESULT_SCHEMA` "
        "shape shown above under those two verbs — followed later by an "
        "`agent_end` event on the ordinary event stream once the turn "
        "actually finishes. A rejected submission errors on the response "
        "instead (`SUBMISSION_REJECTED`) and there is no later event for it."
    )
    w(
        "- **`compact` also has TWO completions** (C3 extended to Tier B, "
        "because a summarization call is bounded only by the provider and "
        "awaiting it inline held the single serial reader — see `compact`'s "
        "own notes): an immediate response acknowledging that the compaction "
        "STARTED (`{accepted, compaction_id}`), followed later by a "
        f"`{commands.COMPACTION_END_METHOD}` NOTIFICATION carrying the outcome. "
        "That notification is **not** an `event` — it is its own method, with "
        "its own params shape (`commands.COMPACTION_END_PARAMS_SCHEMA`), "
        "correlated to the request by both `compaction_id` and `request_id`. "
        "A host that treats every non-`event` notification as a protocol "
        "violation will drop it and see `compact` never finish."
    )
    w(
        "- **No other verb in this table has more than one completion.** "
        f"`{'`, `'.join(_DUAL_COMPLETION_VERBS)}` are the whole list; a verb "
        "not named here answers exactly once."
    )
    w("")
    w("## Declined")
    w("")
    w(
        "C1: every verb τ deliberately does not implement is declined here, with "
        "a reason — never silently absent. Calling a declined verb still returns "
        f"`{dialect.METHOD_NOT_FOUND}` (`METHOD_NOT_FOUND`); the reason below, not "
        "the bare error, is how a host learns WHY."
    )
    w("")
    w("| Verb | Reason |")
    w("|---|---|")
    for row in doc["declined"]:
        w(f"| `{row['name']}` | {row['reason']} |")
    w("")
    w("## Reverse channel")
    w("")
    w(
        "`ui_methods` is always `[]` in v1 — RC3's honest statement that the "
        "reverse (server → host) channel does not exist yet. See "
        "docs/REMOTE-CONTROL.md §7.1 for the three reservations that keep it "
        "additive when it arrives."
    )
    w("")
    w("## Event stream")
    w("")
    w(
        'Every `type: "event"` notification carries a `WireEvent` payload '
        "(generated from `AgentEvent`, §6 point 3) as `params`. No unbounded field "
        "is ever pushed (G3): `message_update` carries a bounded per-chunk "
        "`delta`, never the cumulative message; `agent_end` carries a "
        "`message_count`, never the message array (pull it with `get_messages`)."
    )
    w("")
    w(f"**Event types:** {', '.join(f'`{t}`' for t in doc['events'])}")
    w("")
    w("**Fields** (every event carries the full set; unpopulated fields are")
    w("`null`/`false`, never omitted — a fixed record shape, not a variant one):")
    w("")
    w("| Field | Type | Description |")
    w("|---|---|---|")
    for name, type_str, description in _schema_field_rows(doc["event_schema"]):
        w(f"| `{name}` | {type_str} | {description} |")
    w("")
    w("## Error codes")
    w("")
    w("| Code | Name | Description |")
    w("|---|---|---|")
    for code, name, description in _ERROR_ORDER:
        w(f"| `{code}` | `{name}` | {description} |")
    w("")
    w("## License")
    w("")
    w("MIT")
    w("")

    return "\n".join(lines)
