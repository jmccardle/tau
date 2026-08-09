"""The RPC command table: block [3], per docs/REMOTE-CONTROL.md §6 "Recommendation:
audit, do not generate".

One explicit, hand-written entry per verb (`CommandEntry`), registered with the
`@command(...)` decorator (§6 point 4 — a registry INSIDE the RPC layer is fine;
what is forbidden is decorating `AgentSession` itself, §6 A1-A6). `params_schema`
is a JSON Schema dict written by hand, never derived from a Python signature via
`inspect` (§6 A3) — the wire schema is a deliberate, reviewed artifact, and a
renamed kwarg must not silently break every host.

**Tier A** (required of any host): `prompt`, `abort`, `get_state`, `get_messages`,
`get_commands`, `get_tools`, `get_capabilities` (unit 2C), plus phase 3's
runtime-host trio `new_session`, `fork`, `switch_session` (H1,
docs/REMOTE-CONTROL.md §4[6] — backed by `AgentSessionRuntime`,
`agent_session_runtime.py`).
**Tier B** (parity — docs/RPC-TIER-B.md): `compact`, `get_last_assistant_text`,
`get_models`, `get_session_name`, `get_session_stats`, `list_sessions`,
`set_auto_compaction`, `set_model`, `set_session_name`. All of them are wired;
nothing in this table describes a Tier B verb as pending. (`get_models` and
`list_sessions` are the verbs with no row in RPC-TIER-B.md §3 — they answer
findings 7 and 8 of that tier's review, which found `set_model`'s `name` param
and `switch_session`'s `session_id` param unusable from the wire alone.)
**Tier C** (τ-justified, no pi equivalent): `submit`, the provenance
differentiator `prompt` is defined in terms of (§10 decision 10).
**Declined**: `send_tool_result` (2A) and, as of 2C, the six Tier D verbs §4[3]
names — `cycle_model`, `cycle_thinking_level`, `set_steering_mode`,
`set_follow_up_mode`, `export_html`, `bash` — see each entry for its reason
(C1: declining is a documented act, not a silent no-op).

`get_capabilities` (K1, `tau_agent_core.rpc.capabilities.build_capabilities`)
is unit 2C's own verb, built by WALKING this table (and the declined map)
rather than by editing it — a new `@command(...)`/`decline(...)` call is an
addition, not an edit, to any existing row.

Every non-declined row also carries a `result_schema` (phase 3, a phase-2
review finding): `params_schema` alone meant `docs/RPC-PROTOCOL.md` never
said what a verb RETURNS. Same discipline — hand-written, reviewed, never
derived via `inspect` (§6 A3).

Reference: docs/REMOTE-CONTROL.md §3 "The command table", §4[3], §4[8], §6, §10.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Literal
from uuid import uuid4

from tau_agent_core.agent_session_runtime import DEFAULT_SWAP_TIMEOUT_S
from tau_agent_core.commands import FRONTEND_COMMANDS, CommandOutcome, unsupported_command_message
from tau_agent_core.rpc import capabilities
from tau_agent_core.rpc.dialect import (
    COMMAND_NOT_SUPPORTED,
    INVALID_PARAMS,
    SESSION_NOT_PERSISTED,
    SUBMISSION_REJECTED,
    TURN_STILL_RUNNING,
)
from tau_agent_core.submission import Submission, SubmissionResult

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession
    from tau_agent_core.compaction import CompactionResult
    from tau_agent_core.rpc.handler import RPCHandler
    from tau_agent_core.session_catalog import SessionCatalog, SessionInfo

Tier = Literal["A", "B", "C", "D"]

#: A table handler: awaited with the owning `RPCHandler` (so it can drive a
#: background task, reach `.session`, etc. — §6 A2, "the interesting verbs are
#: the non-1:1 ones"), the original request's `id` (`msg_id`), and the
#: already-validated `params` dict. `msg_id` is threaded to every handler
#: uniformly — not just `submit`/`prompt`, which are the only ones that need
#: it today — so the table stays one homogeneous callable type rather than
#: two shapes.
#:
#: Returns the dict that becomes `result` on the wire, with `RPCHandler
#: ._handle_request` adding `method` (D2) after the call — OR `None`, which
#: means the handler already sent its own response(s) via `handler
#: ._output_queue`/`_send_response` and `_handle_request` must not send a
#: second one. `submit`/`prompt` are the only handlers that return `None`
#: today (C3's dual completion: the acceptance response must be enqueued
#: synchronously, from inside `Submission`'s `on_admitted` callback, to beat
#: the turn's own first event onto the wire — see the `_submit_and_acknowledge`
#: docstring for why `_handle_request`'s normal "await, then send" shape
#: cannot do that).
CommandHandlerFn = Callable[
    ["RPCHandler", "int | None", dict[str, Any]], Awaitable["dict[str, Any] | None"]
]


class RPCError(Exception):
    """A structured JSON-RPC error a handler raises ON PURPOSE.

    Distinct from an ordinary exception (which `RPCHandler._handle_request`
    turns into a generic `INTERNAL_ERROR` — "the handler raised something
    nobody planned for"): `RPCError` is how a handler reports an EXPECTED,
    structured refusal with its own code — today, exactly one case, a rejected
    `Submission` (C3).
    """

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class CommandEntry:
    """One row of the command table (§6 point 1): `name`, `params_schema`,
    `result_schema`, `handler`, `tier`, `since`, `notes` — or, for a declined
    verb, `declined_because` in place of `handler`/`result_schema`. Never
    both, never neither.

    `result_schema` (phase 3, a phase-2 review finding): the params-only half
    of this table meant `docs/RPC-PROTOCOL.md` never said what any verb
    RETURNS (§2 G1: "a second implementation should be possible from this
    document"). Same discipline as `params_schema` — hand-written, reviewed,
    never derived via `inspect` (§6 A3) — and describes exactly what the
    handler's own return dict contains, NOT the `method` field
    `RPCHandler._handle_request` (D2) or `commands._submit_and_acknowledge`
    (C3) adds on top of every result uniformly; see `docs/RPC-PROTOCOL.md`'s
    generated "Response envelope" section for that part instead of repeating
    it eleven times here.
    """

    name: str
    tier: Tier
    since: str
    notes: str
    params_schema: dict[str, Any]
    handler: CommandHandlerFn | None = None
    declined_because: str | None = None
    result_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if (self.handler is None) == (self.declined_because is None):
            raise ValueError(
                f"CommandEntry {self.name!r}: exactly one of `handler` / "
                "`declined_because` must be set. A table row is either a working "
                "verb or a declined one (§6 point 1) — never both, and a handler-"
                "less, reason-less row would mean an unknown method that is "
                "silently NOT an error, which C2 forbids."
            )
        if (self.handler is None) != (self.result_schema is None):
            raise ValueError(
                f"CommandEntry {self.name!r}: `result_schema` must be set if and "
                "only if `handler` is — a working verb with no documented result "
                "is the exact gap this field exists to close, and a declined verb "
                "has no result to document (it never runs)."
            )
        # Import-time, not call-time: a schema using vocabulary `validate_params`
        # does not implement checks NOTHING, and a verb that silently accepts any
        # params is worse than one that refuses to load. See
        # `_assert_supported_schema`.
        _assert_supported_schema(self.params_schema, self.name)
        if self.result_schema is not None:
            _assert_supported_schema(self.result_schema, f"{self.name} (result)")


#: The table itself. Populated by `@command(...)`/`decline(...)` below, at
#: import time, in this one module — never by scanning `AgentSession` (§6 A6:
#: an import-time registry populated by decoration on a dynamically-loaded
#: module is a load-order Heisenbug; this table has exactly one definer).
COMMAND_TABLE: dict[str, CommandEntry] = {}


def command(
    name: str,
    *,
    tier: Tier,
    since: str,
    notes: str,
    params_schema: dict[str, Any],
    result_schema: dict[str, Any],
) -> Callable[[CommandHandlerFn], CommandHandlerFn]:
    """Register `name` into `COMMAND_TABLE` with the decorated function as its
    handler. A registry inside the RPC layer (§6 point 4) — not on
    `AgentSession`, which stays untouched by this module entirely.
    """

    def decorator(fn: CommandHandlerFn) -> CommandHandlerFn:
        if name in COMMAND_TABLE:
            raise ValueError(f"duplicate RPC command registration: {name!r}")
        COMMAND_TABLE[name] = CommandEntry(
            name=name,
            tier=tier,
            since=since,
            notes=notes,
            params_schema=params_schema,
            handler=fn,
            result_schema=result_schema,
        )
        return fn

    return decorator


def decline(
    name: str,
    *,
    tier: Tier,
    since: str,
    notes: str,
    declined_because: str,
    params_schema: dict[str, Any] | None = None,
) -> None:
    """Register `name` as a declined verb — present in the table (so both the
    2C capability document and a future §6 audit test can see it) but carrying
    no handler. Calling it over the wire still gets `METHOD_NOT_FOUND`
    (C2) — the sanctioned way to *learn* it is declined is `get_capabilities`
    (C1), never a bare unknown-method error indistinguishable from a typo.
    """
    if name in COMMAND_TABLE:
        raise ValueError(f"duplicate RPC command registration: {name!r}")
    COMMAND_TABLE[name] = CommandEntry(
        name=name,
        tier=tier,
        since=since,
        notes=notes,
        params_schema=params_schema or NO_PARAMS_SCHEMA,
        declined_because=declined_because,
    )


# ─────────────────────────────────────────────────────────────────────────
# A minimal, hand-rolled JSON Schema validator.
#
# `jsonschema` is not a dependency of any package in this repo (checked: not in
# any pyproject.toml, not installed in the project venv), and adding one for a
# handful of `object`/`string`/`boolean`/`integer`/`array`/`enum` checks this
# module fully controls both ends of is not worth the new dependency. This
# supports exactly the vocabulary the schemas below use — no `$ref`, no
# `oneOf`, no nested `items` schema — and raises loudly (ValueError, at import
# time via CommandEntry / at call time via the caller) rather than silently
# accepting what it cannot check.
# ─────────────────────────────────────────────────────────────────────────


def validate_params(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    """Validate `params` against `schema`. Returns `None` if valid, else a
    human-readable description of the FIRST violation found (C2: `-32602`
    carries this message).
    """
    if schema.get("type") != "object":
        # Unreachable via the table (`_assert_supported_schema` rejects it at
        # import), and a raise rather than a `return None` so it stays that way:
        # falling through to "valid" would mean an unwalked schema silently
        # accepting every params dict a host sends.
        raise ValueError(
            f"validate_params only walks object schemas, got type={schema.get('type')!r}"
        )
    if not isinstance(params, dict):
        return f"params must be an object, got {type(params).__name__}"
    properties: dict[str, Any] = schema.get("properties", {})
    for required_name in schema.get("required", []):
        if required_name not in params:
            return f"missing required param {required_name!r}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(params) - set(properties))
        if unknown:
            return f"unexpected param(s): {', '.join(unknown)}"
    for param_name, value in params.items():
        prop_schema = properties.get(param_name)
        if prop_schema is None:
            continue
        violation = _validate_value(prop_schema, value, param_name)
        if violation is not None:
            return violation
    return None


def _validate_value(schema: dict[str, Any], value: Any, path: str) -> str | None:
    schema_type = schema.get("type")
    if schema_type is not None:
        allowed = schema_type if isinstance(schema_type, list) else [schema_type]
        if not any(_matches_type(value, t) for t in allowed):
            return f"{path!r} must be of type {allowed}, got {type(value).__name__}"
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        return f"{path!r} must be one of {enum!r}, got {value!r}"
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        return f"{path!r} must be >= {minimum}, got {value!r}"
    return None


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "null":
        return value is None
    raise ValueError(
        f"unsupported JSON Schema type {type_name!r}. This validator implements "
        f"only {sorted(_SUPPORTED_TYPES)}; returning True here would silently "
        "accept every value of a type nobody checked. Unreachable in practice — "
        "`_assert_supported_schema` rejects the schema at import time — and kept "
        "as a raise rather than a fallback so it stays unreachable."
    )


#: The exact vocabulary `validate_params` implements. Anything outside these
#: sets is REJECTED at import time rather than ignored at call time: a schema
#: keyword this module does not understand (`items`, `pattern`, `maxLength`,
#: `oneOf`, `$ref`, …) would otherwise validate nothing at all, silently, and
#: the author would have no way to find out. That is the failure mode the
#: repo's Fail-Early rule exists to prevent, and it is the one a hand-rolled
#: validator is most likely to grow — the next person to write a schema is not
#: reading this module first. Widening the validator means widening these sets
#: in the same commit.
_SUPPORTED_TYPES = frozenset({"string", "boolean", "integer", "number", "array", "object", "null"})
_SUPPORTED_OBJECT_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "description"}
)
_SUPPORTED_VALUE_KEYWORDS = frozenset({"type", "enum", "minimum", "description"})


def _assert_supported_schema(schema: dict[str, Any], where: str) -> None:
    """Raise unless `schema` uses only the vocabulary `validate_params` checks.

    Called from `CommandEntry.__post_init__`, so every table row is verified at
    IMPORT time — the moment `@command(...)` runs — and a schema this validator
    cannot enforce is a hard startup failure rather than a verb that quietly
    accepts anything a host sends it.
    """

    def fail(detail: str) -> None:
        raise ValueError(f"RPC params_schema for {where!r}: {detail}")

    if schema.get("type") != "object":
        fail(
            f"top-level `type` must be 'object', got {schema.get('type')!r}. "
            "`validate_params` only walks object schemas; any other top-level "
            "shape would pass every params dict unchecked."
        )
    unknown = sorted(set(schema) - _SUPPORTED_OBJECT_KEYWORDS)
    if unknown:
        fail(f"unsupported schema keyword(s) {unknown} — this validator ignores them")

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        fail(f"`properties` must be an object, got {type(properties).__name__}")

    required = schema.get("required", [])
    if not isinstance(required, list):
        fail(f"`required` must be an array, got {type(required).__name__}")
    missing = sorted(name for name in required if name not in properties)
    if missing:
        fail(f"`required` names {missing}, which are absent from `properties`")

    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            fail(f"property {prop_name!r} must map to an object schema")
        unknown = sorted(set(prop_schema) - _SUPPORTED_VALUE_KEYWORDS)
        if unknown:
            fail(f"property {prop_name!r} uses unsupported keyword(s) {unknown}")
        declared = prop_schema.get("type")
        if declared is None:
            continue
        names = declared if isinstance(declared, list) else [declared]
        bad = sorted(str(n) for n in names if n not in _SUPPORTED_TYPES)
        if bad:
            fail(f"property {prop_name!r} declares unsupported type(s) {bad}")


# ─────────────────────────────────────────────────────────────────────────
# Params schemas — hand-written, reviewed, diffable (§6 A3).
# ─────────────────────────────────────────────────────────────────────────

NO_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: Mirrors `tau_agent_core.submission.SubmissionSource` / `MultitaskStrategy`
#: BY HAND, not via `typing.get_args()` — the wire enum is a reviewed
#: commitment to a vocabulary, not a derived reflection of whatever the
#: dataclass happens to allow today (§6 A3's objection applies just as much to
#: an automatic enum as to an automatic params list).
_SUBMISSION_SOURCE_ENUM = [
    "interactive",
    "rpc",
    "extension",
    "bus",
    "timer",
    "webhook",
    "voice",
    "agent",
]
_MULTITASK_STRATEGY_ENUM = ["reject", "enqueue", "steer", "rollback", "fork"]

#: Phase-2 review S3: "fork" stays IN the enum above — it is a real
#: `Submission.multitask_strategy` value, not a typo, and a client should be
#: able to name it and get an informative refusal — but it is rejected at
#: submission time (see `_reject_unsupported_multitask_strategy`) rather than
#: silently accepted. `_spawn_fork` (agent_session.py) runs the forked agent
#: on the `branch_event` channel, which `RPCHandler` does not subscribe to
#: (only the primary `"all"` AgentEvent stream is forwarded — see
#: `RPCHandler.__init__`'s comment on `_delta_projector`) — so a host that
#: asked for "fork" today would get `{"accepted": true}` and permanent
#: silence while it burns tokens and runs bash in the background, which is
#: exactly the G7/C1 violation ("declared, not silently no-op'd") this
#: module exists to prevent for every OTHER unsupported verb. Tier C's
#: `open_lane`/`list_lanes` (REMOTE-CONTROL.md §3) is the future route: it
#: forwards `branch_event` deliberately, as its own verb, instead of asking
#: a host to guess that "fork" needs a channel `submit`'s response never
#: mentions. "steer" is NOT rejected — it lands in the in-flight turn's own
#: observable stream, so a host watching that submission's events sees it.
_UNSUPPORTED_MULTITASK_STRATEGIES: dict[str, str] = {
    "fork": (
        "multitask_strategy='fork' is not supported over RPC yet: a fork's "
        "AgentEvents are forwarded on AgentSession's 'branch_event' channel, "
        "which this RPC handler does not subscribe to, so the submission "
        "would be accepted and then silently produce nothing observable. "
        "Tier C's open_lane/list_lanes (docs/REMOTE-CONTROL.md §3) is the "
        "future route for this; 'steer' is unaffected — it delivers into the "
        "in-flight turn's own observable stream."
    ),
}

#: The full set of `Submission` fields the wire may set, shared between
#: `submit` and `prompt` — they differ only in which of these are `required`
#: (§10 decision 10: "one implementation, two names").
_SUBMISSION_PROPERTIES: dict[str, Any] = {
    "text": {"type": "string", "description": "The prompt text."},
    "images": {
        "type": ["array", "null"],
        "description": "Optional list of image content blocks.",
    },
    "source": {
        "type": "string",
        "enum": _SUBMISSION_SOURCE_ENUM,
        "description": "Who originated this submission (Submission.source).",
    },
    "submitter": {
        "type": "string",
        "description": "WHO submitted — an extension name, 'human', a channel id.",
    },
    "submission_id": {
        "type": "string",
        "description": "Caller-assigned correlation id for this submission (uuid4 recommended).",
    },
    "multitask_strategy": {
        "type": "string",
        "enum": _MULTITASK_STRATEGY_ENUM,
        "description": (
            "Concurrency policy against an in-flight turn. Defaults to 'reject'. "
            "'fork' is a recognized value but currently REJECTED at submission "
            "time (-32602, phase-2 review S3) — its events reach no channel this "
            "handler forwards; see docs/REMOTE-CONTROL.md §3 Tier C open_lane."
        ),
    },
    "expand_commands": {
        "type": "boolean",
        "description": "Whether a leading '/' is command-dispatched. Defaults to False.",
    },
    "allow_user_input": {
        "type": "boolean",
        "description": "Whether this submission's turn may open a blocking dialog.",
    },
    "store_history": {
        "type": "boolean",
        "description": "Whether this turn is persisted to the session log. Defaults to True.",
    },
    "silent": {
        "type": "boolean",
        "description": (
            "Folds into store_history=False; NOT otherwise implemented — "
            "AgentSession.submit() raises NotImplementedError if True."
        ),
    },
    "correlation": {
        "type": "object",
        "description": "Free-form origin detail (bus subject, cron id, HTTP request id).",
    },
    "depth": {
        "type": "integer",
        "minimum": 0,
        "description": "Self-submission depth floor; submit() may raise it further.",
    },
}

SUBMIT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _SUBMISSION_PROPERTIES,
    "required": ["text", "source", "submitter", "submission_id"],
    "additionalProperties": False,
}

PROMPT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _SUBMISSION_PROPERTIES,
    "required": ["text"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────
# Result schemas — hand-written, reviewed, diffable (§6 A3), same discipline
# as the params schemas above. Document exactly what a handler's own return
# dict contains — never the `method` field every response also carries (D2)
# or `submit`/`prompt`'s dual-completion shape (C3); see the generated doc's
# "Response envelope" section for those, stated once instead of per verb.
# ─────────────────────────────────────────────────────────────────────────

#: submit/prompt's ONE possible result shape (C3's "acceptance" response —
#: whichever of the two paths in `_submit_and_acknowledge` sends it).
#: `rejection_reason` is always `null` here: an actual rejection raises
#: `RPCError(SUBMISSION_REJECTED, ...)` instead of returning this shape at
#: all (see the field's own description) — it is present so a host's static
#: type for "the submit result" does not have to special-case its absence.
SUBMIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accepted": {
            "type": "boolean",
            "description": "Always true here — a rejected submission is an RPCError, not this shape.",
        },
        "submission_id": {
            "type": "string",
            "description": "Echoes the request's submission_id (caller-supplied, or a minted uuid4 for prompt).",
        },
        "rejection_reason": {
            "type": "null",
            "description": "Always null on this success shape; a real rejection is SUBMISSION_REJECTED instead.",
        },
        "command": {
            "type": "object",
            "description": (
                "Present ONLY when this acceptance is also the submission's only "
                "completion: a core (extension-registered) slash command resolved "
                "synchronously with no turn started, so there is no later agent_end "
                "to carry it. {name, args, performer, output}. Absent for an "
                "ordinary turn — poll get_messages / watch for agent_end instead."
            ),
        },
    },
    "required": ["accepted", "submission_id", "rejection_reason"],
}

ABORT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["aborted"], "description": "Always 'aborted'."},
        "compaction_id": {
            "type": ["string", "null"],
            "description": (
                "The compaction this abort's signal was delivered to, or null "
                "when none was in flight (finding 5, Tier B review). Present "
                "so a host knows to expect a compaction_end carrying "
                "cancelled: true for that id. Whether the compaction actually "
                "stopped is reported THERE and not here — same signal-vs-"
                "outcome split that keeps `cursor` off this response."
            ),
        },
    },
    "required": ["status", "compaction_id"],
}

GET_STATE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "AgentSession.state.session_id."},
        "status": {
            "type": "string",
            "enum": ["idle", "running"],
            "description": "AgentSession.state.status.",
        },
        "is_streaming": {"type": "boolean", "description": "AgentSession.is_streaming."},
        "model": {
            "type": "object",
            "description": "AgentSession.get_model(): {id, provider, context_window}.",
        },
        "usage": {
            "type": ["object", "null"],
            "description": "AgentSession.get_usage() — null before the first completion.",
        },
        "message_count": {"type": "integer", "description": "len(AgentSession.messages)."},
        "cursor": {
            "type": ["string", "null"],
            "description": "session_log.cursor (F3: no host may cache 'the tip').",
        },
        "addressable": {
            "type": "boolean",
            "description": (
                "Whether the CURRENT session is persisted: true if list_sessions "
                "returns it and switch_session can reach it later. The same "
                "predicate new_session/fork/switch_session publish on their "
                "session tuple, asked about the session this connection is on "
                "right now. False means the appending verbs (set_model, "
                "set_session_name, compact — D-7) will refuse with -32004 "
                "SESSION_NOT_PERSISTED, and nothing this connection does is "
                "written to the store. Reachable without a respawn: "
                'new_session {"persist": true} moves onto a persisted session.'
            ),
        },
    },
    "required": [
        "session_id",
        "status",
        "is_streaming",
        "model",
        "usage",
        "message_count",
        "cursor",
        "addressable",
    ],
}

GET_MESSAGES_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "description": "AgentSession.messages — the terminal, flat message array (E2's pull side).",
        },
    },
    "required": ["messages"],
}

GET_COMMANDS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "description": (
                "Array of {name, description, performer}. name has no leading "
                "'/' — submit it as ordinary text with expand_commands=true, "
                "not as an RPC method."
            ),
        },
    },
    "required": ["commands"],
}

GET_TOOLS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "description": "Array of {name, description, parameters} — this session's bound AgentTool set.",
        },
    },
    "required": ["tools"],
}

GET_CAPABILITIES_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "protocol_version": {"type": "string", "description": "MAJOR.MINOR (K2)."},
        "dialect": {"type": "string", "description": "Always 'jsonrpc-2.0' in v1 (D1)."},
        "commands": {
            "type": "array",
            "description": "Every non-declined COMMAND_TABLE row: {name, tier, since, notes, params_schema, result_schema}.",
        },
        "events": {"type": "array", "description": "The AgentEvent-derived wire event type names."},
        "event_schema": {
            "type": "object",
            "description": "JSON Schema for a WireEvent's params (generated from AgentEvent, §6 point 3).",
        },
        "ui_methods": {
            "type": "array",
            "description": "Always [] in v1 (RC3) — the reverse channel does not exist yet.",
        },
        "declined": {
            "type": "array",
            "description": "Every declined verb: {name, reason} (C1).",
        },
        "limits": {
            "type": "object",
            "description": (
                "Bounds this process enforces on what a host may SEND, as "
                "numbers rather than as something to discover by tripping over "
                "it — today {max_request_line_bytes} (T7). Read live off the "
                "code that enforces each bound (capabilities.build_limits), so "
                "an advertised limit cannot drift from the applied one. A MAP "
                "rather than one field per bound: the next bound to be "
                "published is an addition INSIDE this object, which a host "
                "honoring E3 gets for free."
            ),
        },
    },
    "required": [
        "protocol_version",
        "dialect",
        "commands",
        "events",
        "event_schema",
        "ui_methods",
        "declined",
        "limits",
    ],
}

#: `new_session` / `fork` / `switch_session` share this shape (phase 3, H1-H4):
#: F2's session tuple, plus a top-level `cursor` duplicate for the same
#: reason `get_state`'s carries one (F3: no host may cache "the tip").
#:
#: `session.addressable` and the wording around it are finding 7 of the Tier
#: B review: this description read "F2's addressable tuple" unconditionally,
#: while `new_session {"persist": false}` was measured returning
#: `{"store": "file", "session_id": "5543562f…", …}` for a session
#: `switch_session` answered `-32602 no session matches '5543562f…'` for and
#: no file ever held. The shape predates that verb's `persist` param; what
#: made the unqualified claim wrong NOW is that the previous round turned
#: `persist` into a documented, selectable mode. H2:
#: `cancelled: true` means an extension vetoed via `session_before_switch` —
#: `session`/`cursor` are ABSENT in that case (nothing was touched, there is
#: no new tuple to report), never present-but-null. A THIRD outcome — the
#: in-flight turn did not stop in time (Finding 1) — never reaches this
#: shape at all: it is `RPCError(TURN_STILL_RUNNING, ...)` instead, the same
#: "an expected refusal is its own error code, not a result field" choice
#: `SUBMIT_RESULT_SCHEMA`'s `rejection_reason` documents for SUBMISSION_REJECTED.
SESSION_LIFECYCLE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cancelled": {
            "type": "boolean",
            "description": (
                "True if a session_before_switch extension hook vetoed (H2). When "
                "true, `session`/`cursor` are absent — nothing was touched. An "
                "in-flight turn that did not stop in time is a DIFFERENT outcome "
                "and never reaches this shape — see TURN_STILL_RUNNING."
            ),
        },
        "session": {
            "type": "object",
            "description": (
                "F2's session tuple: {store, session_id, lane, cursor, "
                "addressable}. `lane` is always 'primary' in v1 (lanes are "
                "Tier C, not this phase). Present only when cancelled is "
                "false. `addressable` (finding 7 of the Tier B review) is "
                "the field that says whether `session_id` is a value "
                "ANOTHER call can use: true means list_sessions returns this "
                "id and switch_session resolves it; false means this session "
                'exists in memory only — it is `new_session {"persist": '
                "false}`'s product, switch_session answers -32602 for it, "
                "list_sessions never shows it, and the verbs D-7 rule 1 "
                "governs (set_model/set_session_name/compact) refuse on it. "
                "`store` names the store THIS CONNECTION's catalog is on, "
                "which is not a claim that this session is in it: when "
                "addressable is false, nothing was written to that store."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "The resulting session_log.cursor, duplicated at top level "
                "(E5/F3 — every mutating response returns the resulting cursor). "
                "Present only when cancelled is false."
            ),
        },
    },
    "required": ["cancelled"],
}


# ─────────────────────────────────────────────────────────────────────────
# submit / prompt — one implementation (§10 decision 10), C3's dual completion.
# ─────────────────────────────────────────────────────────────────────────


def _reject_unsupported_multitask_strategy(params: dict[str, Any]) -> None:
    """S3: refuse a `multitask_strategy` this handler cannot honestly serve,
    BEFORE a `Submission` is built or admitted — never `{"accepted": true}`
    followed by silence. `-32602` (the same code a schema-level violation
    gets) rather than `SUBMISSION_REJECTED`: this is not a concurrency
    decision `AgentSession.submit()` made about the CONTENT of the request
    (a turn already in flight, say) — it is the wire refusing a value its
    schema syntactically allows but cannot wire up end to end, which is what
    C2/G7 call for a param the handler cannot honor at all.
    """
    strategy = params.get("multitask_strategy")
    if not isinstance(strategy, str):
        # Not a string at all (or absent) — validate_params's schema check
        # (INVALID_PARAMS, run before this function is ever reached) already
        # owns rejecting that; nothing in _UNSUPPORTED_MULTITASK_STRATEGIES
        # keys on a non-string, so there is nothing more to reject here.
        return
    reason = _UNSUPPORTED_MULTITASK_STRATEGIES.get(strategy)
    if reason is not None:
        raise RPCError(INVALID_PARAMS, reason, data={"multitask_strategy": strategy})


def _submission_from_params(params: dict[str, Any]) -> Submission:
    """Build a `Submission` from wire params. Provenance is ALWAYS present on
    the constructed record — defaulted when the caller omits it (`prompt`'s
    contract), never simply absent — regardless of which verb called this;
    it is each verb's `params_schema.required` list, not this function, that
    makes `submit` demand provenance on the wire (§10 decision 10).
    """
    _reject_unsupported_multitask_strategy(params)
    kwargs: dict[str, Any] = {
        "text": params["text"],
        "source": params.get("source", "rpc"),
        "submitter": params.get("submitter", "rpc-client"),
        "submission_id": params.get("submission_id") or str(uuid4()),
    }
    for optional_field in (
        "images",
        "multitask_strategy",
        "expand_commands",
        "allow_user_input",
        "store_history",
        "silent",
        "correlation",
        "depth",
    ):
        if optional_field in params:
            kwargs[optional_field] = params[optional_field]
    return Submission(**kwargs)


def _accept_result(submission_id: str, *, command: CommandOutcome | None = None) -> dict[str, Any]:
    """The C3 acceptance payload. Never carries the turn's messages (C3:
    "the response must NOT carry the turn's messages") — those are pulled via
    `get_messages` (E2).

    `command` is set exactly when this acceptance is ALSO the only completion
    the host will ever get for this submission — a resolved
    `performer="core"` command (phase-2 review B2): no turn ran, so there is
    no `agent_end` to follow this response, and `SubmissionResult.command`
    would otherwise reach `_submit_and_acknowledge` and go no further. `None`
    (the default) covers every ordinary turn, where the real completion is
    the `agent_end` event on the subscription this handler already forwards.
    A `performer="frontend"` outcome never reaches here — see
    `_submit_and_acknowledge`'s tail, which raises `RPCError` for that case
    instead of calling this function.
    """
    result: dict[str, Any] = {
        "accepted": True,
        "submission_id": submission_id,
        "rejection_reason": None,
    }
    if command is not None:
        result["command"] = {
            "name": command.name,
            "args": command.args,
            "performer": command.performer,
            "output": command.output,
        }
    return result


async def _submit_and_acknowledge(
    handler: "RPCHandler", msg_id: int | None, method: str, sub: Submission
) -> dict[str, Any] | None:
    """Drive `AgentSession.submit()` as a background task and get the C3
    acceptance response onto the wire the instant admission is decided —
    never after the turn itself finishes, which is what makes `submit`/
    `prompt` non-blocking (docs/REMOTE-CONTROL.md §4[3] C3, "two completions").

    Always returns `None`: this function sends its own response (once, either
    path below) rather than returning a dict for `RPCHandler._handle_request`
    to wrap and send — see why in the ordering paragraph.

    **Why the ack is enqueued from inside `on_admitted` itself, synchronously,
    rather than via the more obvious "await a Future the callback resolves,
    then build+return the response dict" shape:** `Future.set_result` does
    not suspend the caller or hand control to whatever is awaiting the
    Future — it merely schedules that task's resumption for a later turn of
    the event loop. Meanwhile the `_drive` task below keeps running,
    synchronously, past the callback — through the rest of `submit()`'s setup
    and often into the turn's first `AgentEvent` (`agent_start`) — because
    none of that has an actual `await` suspension point before it. Measured
    with a real `AgentSession` and a gated fake provider: the `agent_start`
    event reached `_output_queue` BEFORE a response built the "await a
    Future" way did, even though admission strictly preceded it. T6's FIFO
    guarantee is about preserving whatever order things are enqueued in, not
    about which of two concurrently-progressing tasks wins a race to enqueue
    first — so "first" has to be decided inside the synchronous callback
    itself, exactly where pi's own `preflightResult: (didSucceed) =>
    output(success(id, "prompt"))` (rpc-mode.ts) decides it, for the same
    reason.

    `sub.on_admitted` (agent_session.py `submit()`) fires from exactly one
    point: the moment every strategy that is actually going to run a turn on
    this call has committed to doing so, AFTER `_apply_input_pipeline` has
    also had its chance (phase-2 review B2 — `on_admitted` moved past that
    check precisely so this is true). Every OTHER shape a submission can
    resolve to already returns its own complete `SubmissionResult` fast,
    without ever calling the callback — so the `_drive` task below finishes
    almost immediately in those cases, and the `await admitted` picks that up
    as `outcome` being the real result rather than the sentinel the callback
    leaves:

    - "reject" failing, "steer" delivering into an in-flight turn, "rollback"
      refusing a stale target, "fork" succeeding or failing its admission
      check — the pre-admission strategy branches.
    - an `input` hook CONSUMING the submission, or `expand_commands` resolving
      it to a slash command (`_apply_input_pipeline`'s `early` return) — a
      submission that is fully handled without ever reaching the model.
      `outcome.command` carries a `CommandOutcome` on exactly this path, and
      is this call's ONLY chance to report it: no turn ran, so there is no
      `agent_end` to carry it instead (see this function's tail).

    Ordering is not at risk on any of those paths: a fast rejection/accept/
    command-resolution happens before a turn (if any) has emitted anything,
    so a response built and sent AFTER `_drive` returns is still first onto
    the wire.
    """
    loop = asyncio.get_running_loop()
    #: `True` once `_on_admitted` has already sent the response itself, so the
    #: post-await branch below knows not to send a second one.
    admitted: asyncio.Future[SubmissionResult | None] = loop.create_future()

    def _on_admitted() -> None:
        if admitted.done():
            return
        # T3 (docs/REMOTE-CONTROL.md §4[1]): this `put_nowait` stays safe
        # under the bounded outbound queue WITHOUT changing anything here.
        # `handler._output_queue` itself carries no capacity limit — T3's
        # bound applies only to AgentEvent-derived items, gated one layer up
        # by `RPCHandler._event_credits` (`_forward_event`/
        # `_acquire_event_credit`) — precisely so a synchronous, non-
        # suspending enqueue like this one (required by the ordering
        # argument above: `on_admitted` cannot `await` without reintroducing
        # the reordering the docstring above measures) can never raise
        # `QueueFull` and never has to choose between reordering C3 and
        # dropping this response.
        handler._output_queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {**_accept_result(sub.submission_id), "method": method},
            }
        )
        admitted.set_result(None)

    async def _drive() -> None:
        try:
            result = await handler.session.submit(sub, on_admitted=_on_admitted)
        except Exception as exc:  # noqa: BLE001 - see the two branches below
            if not admitted.done():
                # Failed before admission (e.g. silent=True's NotImplementedError,
                # or a reentrancy/depth-cap RuntimeError) — this call's own
                # response is the only place this can surface, and it has not
                # been sent yet.
                admitted.set_exception(exc)
            else:
                # Admission already happened and the acceptance response is
                # already on the wire. The ordinary AgentEvent stream already
                # carries this failure (AgentLoop.run emits agent_end with
                # is_error=True/error=... BEFORE re-raising — agent_loop.py's
                # `except BaseException` bracket), so this is not a silent
                # drop; it is surfaced here too, deliberately, rather than left
                # for asyncio's own "Task exception was never retrieved"
                # warning — T4 promises stderr is a real log channel, not
                # incidental noise.
                print(
                    f"[τ-rpc] submission {sub.submission_id!r} failed after admission: {exc!r}",
                    file=sys.stderr,
                )
            return
        if not admitted.done():
            # The strategy returned its own SubmissionResult without ever
            # calling on_admitted (see this function's docstring) — that
            # result IS the outcome, decided synchronously.
            admitted.set_result(result)

    task = asyncio.create_task(_drive())
    handler.track_background_task(task)

    outcome = await admitted
    if outcome is None:
        # _on_admitted already sent the response. Nothing left to do.
        return None

    if not outcome.accepted:
        raise RPCError(
            SUBMISSION_REJECTED,
            outcome.rejection_reason or "submission rejected",
            data={"submission_id": sub.submission_id},
        )
    # Accepted, and `on_admitted` never fired for this call (fork's success,
    # steer delivered into an in-flight turn, an `input` hook consuming the
    # submission, or a resolved command) — nothing else was enqueued ahead of
    # it, so the ordinary return-a-dict path is fine here.
    if outcome.command is not None:
        # B2: a resolved command is the one shape where THIS response is the
        # ONLY completion the host will ever get — no turn ran, so there is no
        # `agent_end` to follow it. `performer="core"` already ran (an
        # extension-registered command) and produced text any host can
        # render, so it rides the acceptance response. `performer="frontend"`
        # is a built-in (`/tree`, `/fork`, `/extensions`, `/compact`) the core
        # deliberately did not run because it needs a screen — the RPC wire
        # has none, so it is exactly the frontend
        # `tau_agent_core.commands`'s module docstring describes as unable to
        # perform one, and that docstring is explicit about what such a
        # frontend must do: raise `UnsupportedCommandError`, "rather than
        # return silently" — never the "works in the TUI, no-ops for the web
        # frontend" failure class the whole lifecycle exists to remove.
        if outcome.command.performer == "frontend":
            raise RPCError(
                COMMAND_NOT_SUPPORTED,
                unsupported_command_message(outcome.command, "the τ RPC wire"),
                data={"submission_id": sub.submission_id, "command": outcome.command.name},
            )
        return _accept_result(sub.submission_id, command=outcome.command)
    return _accept_result(sub.submission_id)


@command(
    "submit",
    tier="C",
    since="2A",
    notes=(
        "The provenance differentiator (REMOTE-CONTROL.md §3 Tier C): the full "
        "quad (source/submitter/submission_id/correlation) is required on the "
        "wire, and every AgentEvent the resulting turn emits is stamped with "
        "it. Dual completion (C3): this response only acknowledges admission."
    ),
    params_schema=SUBMIT_PARAMS_SCHEMA,
    result_schema=SUBMIT_RESULT_SCHEMA,
)
async def _handle_submit(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any] | None:
    return await _submit_and_acknowledge(handler, msg_id, "submit", _submission_from_params(params))


@command(
    "prompt",
    tier="A",
    since="2A",
    notes=(
        "§10 decision 10: one implementation, two names. Builds the same "
        "Submission `submit` does, with source/submitter/submission_id/"
        "correlation DEFAULTED rather than required — provenance is always "
        "present on the wire, merely defaulted when the host does not supply "
        "it. Same dual completion as `submit` (C3)."
    ),
    params_schema=PROMPT_PARAMS_SCHEMA,
    result_schema=SUBMIT_RESULT_SCHEMA,
)
async def _handle_prompt(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any] | None:
    return await _submit_and_acknowledge(handler, msg_id, "prompt", _submission_from_params(params))


# ─────────────────────────────────────────────────────────────────────────
# The rest of Tier A.
# ─────────────────────────────────────────────────────────────────────────


@command(
    "abort",
    tier="A",
    since="2A",
    notes=(
        "AgentSession.abort() (agent_session.py:3244) — synchronous, idempotent, "
        "and a SIGNAL only: it requests the in-flight turn stop, and returns "
        "immediately, before that turn has unwound or persisted anything. "
        "Phase-2 review B1: this response therefore does NOT carry a cursor — "
        "one taken here would be the PRE-abort tip, exactly the stale-tip "
        "failure E5/F3 exist to prevent, and the reviewer's own trace caught it "
        "(a 2s-gated turn aborted at 0.5s: this call's cursor and the cursor "
        "AFTER the turn actually finished differed). E5 for `abort` (and for "
        "`submit`/`prompt`, which share this trait) is satisfied instead by "
        "`WireEvent.cursor` on the `agent_end` that follows — the one point "
        "the mutation has genuinely happened — never by a value guessed at "
        "signal time. "
        "WHAT IT REACHES (finding 5, Tier B review): the in-flight turn, and "
        "— since that finding — an in-flight `compact`. It did not before, "
        "and said otherwise: a measured trace answered {status: aborted} at "
        "+0.00s and delivered compaction_end {performed: true} at +20.01s, "
        "because AgentSession.compact consults no abort flag anywhere. The "
        "compaction's background task is now cancelled here, and this "
        "response names it in `compaction_id` (null when none was running) "
        "so a host knows which compaction_end to expect. That notification "
        "carries `cancelled: true` and no `performed` — a compaction stopped "
        "part-way neither performed nor found nothing to do, and nothing was "
        "written (Fail Early: the summary is generated before the entry is "
        "appended). D-5's 'a cancelled compaction emits no compaction_end' "
        "is unchanged for the case it was written about — a SHUTDOWN reap, "
        "where there is no host left to correlate anything and the report "
        "goes to stderr (T4); a host that asked for the cancellation is the "
        "opposite case and is told on the wire. "
        "What abort still does NOT reach: an auto-compaction "
        "(`_maybe_auto_compact`), which runs inside AgentSession with no RPC "
        "task to cancel — see set_auto_compaction's notes, which state the "
        "same boundary from the other side."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=ABORT_RESULT_SCHEMA,
)
async def _handle_abort(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    # Both are signals, and both return before the thing they signalled has
    # unwound (see this verb's notes). `abort_compaction` reports WHICH
    # compaction it reached rather than whether that compaction stopped —
    # the latter is `compaction_end`'s to say.
    handler.session.abort()
    return {"status": "aborted", "compaction_id": handler.abort_compaction()}


@command(
    "get_state",
    tier="A",
    since="2A",
    notes=(
        "An aggregate over AgentSession.state (session_id/status), is_streaming, "
        "get_model(), get_usage(), messages, and session_log.cursor (F3: a host "
        "may not cache 'the tip', so cursor rides on every state read). τ has no "
        "equivalent of pi's thinkingLevel/steeringMode/followUpMode/"
        "sessionFile/pendingMessageCount — none of those exist as AgentSession "
        "state today, so they are omitted rather than fabricated. Two of pi's "
        "state fields DID gain a τ equivalent in Tier B, and are absent from "
        "THIS verb as duplication rather than as absence: pi's sessionName is "
        "get_session_name (B5 — read off the session log via "
        "extension_types.read_session_name; still not an AgentSession "
        "property, which is why it is not folded in here), and pi's "
        "autoCompactionEnabled is get_session_stats' "
        "compaction_settings.enabled (D-3), the field set_auto_compaction "
        "(D-4) writes. This verb answers 'what is running'; get_session_stats' "
        "own notes state that division of labour. `addressable` is the one "
        "field here that is not about the turn: it answers whether the CURRENT "
        "session is persisted, which stopped being a constant when --mode rpc "
        "began honoring --no-session. Before that the startup session was "
        "always persisted, new_session/fork/switch_session reported "
        "addressable on the sessions THEY produced, and a host that never "
        "called one of those three had no verb to ask — so the only way to "
        "learn an unpersisted session was to trip -32004 on set_model. It "
        "belongs on the state read rather than a verb of its own because a "
        "host already calls this one, and because it can change under the "
        "connection's feet (a switch_session onto an ephemeral session) "
        "exactly as `model` and `cursor` can."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_STATE_RESULT_SCHEMA,
)
async def _handle_get_state(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    state = session.state
    return {
        "session_id": state.session_id,
        "status": state.status,
        "is_streaming": session.is_streaming,
        "model": session.get_model(),
        "usage": session.get_usage(),
        "message_count": len(session.messages),
        "cursor": session.session_log.cursor,
        # Defined further down, next to require_durable_session, so the
        # predicate and the refusal that shares it stay adjacent — see its
        # docstring for why "addressable" is D-7's question and not a second
        # one.
        "addressable": session_log_is_addressable(session.session_log),
    }


@command(
    "get_messages",
    tier="A",
    since="2A",
    notes="E2's PULL side — the terminal message array, fetched, never pushed.",
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_MESSAGES_RESULT_SCHEMA,
)
async def _handle_get_messages(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return {"messages": handler.session.messages}


@command(
    "get_commands",
    tier="A",
    since="2C",
    notes=(
        "Enumerated at CALL time, not tabled. §6 'One thing to keep dynamic': slash "
        "commands contributed by extensions are genuinely runtime-variable, and pi "
        "builds this list by enumeration too. That dynamism is specific to THIS verb "
        "and is not an argument for a dynamic protocol-verb table (§6 A6). Returns "
        "τ's built-ins (commands.FRONTEND_COMMANDS, performer='frontend') plus "
        "whatever extensions registered via api.register_command (performer='core'), "
        "in `resolve_command`'s own precedence order so the listing cannot advertise "
        "a name that dispatch would resolve differently."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_COMMANDS_RESULT_SCHEMA,
)
async def _handle_get_commands(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    # `name` is the DISPATCH key, without the leading "/" — the form
    # `commands.parse_command` produces and `resolve_command` matches. A host
    # submits it as ordinary text (`"/compact"`) through `submit`/`prompt` with
    # `expand_commands: true`; it is not an RPC method.
    listed: list[dict[str, str]] = [
        {"name": name, "description": description, "performer": "frontend"}
        for name, description in FRONTEND_COMMANDS.items()
    ]
    # Built-ins win over an extension that registered the same name — exactly
    # what `resolve_command` does ("an extension cannot shadow /compact"). A
    # listing that showed the shadowed extension command would be advertising a
    # verb this session will never dispatch to it.
    for name, description in handler.session.get_extension_commands():
        if name in FRONTEND_COMMANDS:
            continue
        listed.append({"name": name, "description": description, "performer": "core"})
    return {"commands": listed}


@command(
    "get_tools",
    tier="A",
    since="2A",
    notes="Already implemented pre-2A; ported onto the table verbatim, no behaviour change.",
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_TOOLS_RESULT_SCHEMA,
)
async def _handle_get_tools(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    tools = handler.session._tools
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.definition.description,
                "parameters": t.definition.parameters,
            }
            for t in tools
        ]
    }


# ─────────────────────────────────────────────────────────────────────────
# get_capabilities — K1/K2/K3 (unit 2C). The handler itself is a one-line
# call into `capabilities.build_capabilities()`; all of K1's substance
# (walking COMMAND_TABLE, the events[]/event_schema projection, ui_methods
# always [], the declined[] reasons) lives in `rpc/capabilities.py` — see
# that module's docstring for why the table lookup is deferred to call time.
# ─────────────────────────────────────────────────────────────────────────


@command(
    "get_capabilities",
    tier="A",
    since="2C",
    notes=(
        "K1 (REMOTE-CONTROL.md §4[8]): {protocol_version, dialect, commands[], "
        "events[], event_schema, ui_methods[], declined[{name, reason}]}. Built "
        "by WALKING COMMAND_TABLE and rpc_event_schema (§6 recommendation), "
        "never hand-copied — see rpc/capabilities.py. K2: call this FIRST on a "
        "new connection and check protocol_version before sending anything "
        "mutating. ui_methods is always [] in v1 (RC3) — the honest statement "
        "that the reverse channel (§7.1) does not exist yet."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_CAPABILITIES_RESULT_SCHEMA,
)
async def _handle_get_capabilities(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return capabilities.build_capabilities()


# ─────────────────────────────────────────────────────────────────────────
# new_session / fork / switch_session — the runtime-host verbs (phase 3, H1).
# `AgentSessionRuntime` (agent_session_runtime.py) owns the H2 veto / H3
# reset / H4 atomicity; this module's job is only the wire shape — turn the
# runtime's `{cancelled, session, session_id, cursor, store}` into F2's
# session tuple (E5: every mutating response returns the resulting cursor).
# Whether that tuple is ADDRESSABLE is a field on it, not an assumption in
# the name (finding 7) — see `session_log_is_addressable`, and
# `list_sessions` (finding 8) for the enumeration that gives the word its
# meaning.
# ─────────────────────────────────────────────────────────────────────────

#: v1 has exactly one lane per session (F2 — lanes are Tier C, not this
#: phase). A named constant rather than a literal repeated three times below,
#: so the day lanes ship, the one place claiming "primary" is obvious.
_PRIMARY_LANE = "primary"


def _require_runtime(handler: "RPCHandler") -> Any:
    """`handler.runtime`, or a clear failure — never a bare `AttributeError`
    three calls deep. Production wiring (`tau_coding_agent.rpc_mode.run_rpc`)
    always constructs a runtime; a handler built without one (most of this
    package's own unit tests, which only need the verbs that do not ask for
    one) hitting one of the verbs that DOES is a construction gap, not a
    wire-level error, so this raises a plain exception rather than a
    structured `RPCError` — the generic `INTERNAL_ERROR` path in
    `RPCHandler._handle_request` is the honest classification: "the handler
    raised something nobody planned for" is exactly what "this process was
    never given a runtime" is.

    Deliberately count-free. This docstring read "the OTHER eight verbs …
    one of these three" from phase 3 until the Tier B review's integration
    pass, where it was simply wrong: `list_sessions` had become a fourth
    caller and the table had grown to twenty verbs. The verbs are enumerated
    in the raise below, which cannot go stale without failing a call; a tally
    in prose can, and did. Same reason the E5 and D-7 blocks state no counts.
    """
    runtime = handler.runtime
    if runtime is None:
        raise RuntimeError(
            "RPCHandler has no AgentSessionRuntime (constructed with "
            "runtime=None) — new_session/fork/switch_session/list_sessions "
            "need one."
        )
    return runtime


def _lifecycle_result(outcome: dict[str, Any]) -> dict[str, Any]:
    """`AgentSessionRuntime`'s `{cancelled, session, session_id, cursor,
    store}` -> the wire shape `SESSION_LIFECYCLE_RESULT_SCHEMA` describes.

    Finding 1 (phase-3 review): a `blocked` outcome (the in-flight turn did
    not stop within the runtime's bounded wait — `AgentSessionRuntime
    .DEFAULT_SWAP_TIMEOUT_S`) is never returned as a wire RESULT — it raises
    `RPCError(TURN_STILL_RUNNING, ...)` instead, the same "a refusal is a
    result at the runtime layer, an RPCError at the wire layer" conversion
    `_submit_and_acknowledge` already does for a rejected `Submission`
    (SUBMISSION_REJECTED). Checked before `cancelled`: the two are mutually
    exclusive outcomes of the SAME call (H2's veto never even reaches the
    turn-lock wait this timeout bounds), but `blocked` is the one that means
    "nothing happened, and it might still happen if you wait" — a distinct
    enough shape from "vetoed" that conflating them under `cancelled` would
    mislead a host into reporting the wrong reason.
    """
    if outcome.get("blocked"):
        raise RPCError(TURN_STILL_RUNNING, outcome["reason"])
    if outcome["cancelled"]:
        return {"cancelled": True}
    return {
        "cancelled": False,
        "session": {
            "store": outcome["store"],
            "session_id": outcome["session_id"],
            "lane": _PRIMARY_LANE,
            "cursor": outcome["cursor"],
            # Finding 7: the one field that keeps this tuple from lying about
            # itself. `session_log_is_addressable` is defined further down,
            # beside the `_DURABLE_LOCATION_ATTRS` list it reads (that is the
            # thing it is about) and resolved at call time; see its docstring
            # for why "addressable" is the same question D-7 asks and not a
            # second one.
            "addressable": session_log_is_addressable(outcome["session"]),
        },
        "cursor": outcome["cursor"],
    }


SWITCH_SESSION_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": (
                "An exact session id, or a unique id prefix, scoped to this "
                "process's cwd — the same resolution --session REF uses "
                "headlessly (SessionCatalog.resolve_ref). Every acceptable "
                "value is a `session_id` list_sessions returned (finding 8): "
                "resolve_ref is built on the same list(cwd) that verb "
                "publishes, so the two cannot disagree."
            ),
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


#: `new_session` params (Blocker 2 of the Tier B review). `persist` was a
#: hardcoded `False` in the handler until then — the same defect the startup
#: session had, one hop over the wire: a host got back an addressable-looking
#: `{store, session_id, cursor}` tuple for a session no `switch_session`
#: could ever resolve and no durability-promising verb could honestly serve.
#:
#: The DEFAULT lives here rather than on `AgentSessionRuntime.new_session`,
#: whose `persist` is deliberately required-not-defaulted ("Fail-Early: the
#: caller states what it wants rather than inheriting a guess" — that layer
#: also serves the TUI, where the answer differs). At the wire, a default is
#: not a guess: it is a published part of the contract, stated here and in
#: the verb's notes, and this handler still passes an explicit value down.
#:
#: `validate_params` implements no `default` keyword (`_SUPPORTED_VALUE_
#: KEYWORDS`, and it never rewrites the params dict anyway), so the default
#: is documented in this description and applied by the handler — not
#: declared in a keyword the validator would reject at import time.
NEW_SESSION_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "persist": {
            "type": "boolean",
            "description": (
                "Whether the new session is written to the configured store. "
                "Omitted defaults to TRUE: addressable by switch_session, "
                "listed by list_sessions, and able to keep what set_model/"
                "set_session_name/"
                "compact append to it. false gives an in-memory conversation "
                "that outlives nothing — the result then reports "
                "`session.addressable: false` (finding 7), no list_sessions "
                "row exists for its id, switch_session refuses it, and those "
                "three verbs "
                "REFUSE (SESSION_NOT_PERSISTED) rather than return "
                "a cursor for a write that never landed. Which verbs those "
                "are is not a list to memorise: D-7 (commands.py 'DURABILITY "
                "in Tier B') is 'the verb that appends refuses', and the "
                "rest — including set_auto_compaction — answer normally."
            ),
        },
    },
    "additionalProperties": False,
}


@command(
    "new_session",
    tier="A",
    since="phase-3",
    notes=(
        "H1 (REMOTE-CONTROL.md §4[6]): starts a fresh conversation on the "
        "SAME AgentSession (model/tools/extensions/provider stay warm — H3). "
        "`persist` defaults to true (Blocker 2, Tier B review): the fresh "
        "session is written to the configured store, so it is addressable by "
        "switch_session and durable for the verbs that append to it "
        "(set_model/set_session_name/compact — D-7). Pass "
        "false for an in-memory conversation — those three verbs then refuse "
        "on it rather than promising a durable write, and the session tuple "
        "says so on the wire: `addressable: false` (finding 7 of the same "
        "review, which measured this verb returning an 'addressable tuple' "
        "for a session switch_session answered -32602 for). Addressable "
        "means exactly 'list_sessions returns this id'. {cancelled} is H2's "
        "veto contract: a session_before_switch extension hook may refuse, "
        "and a host must treat that as a hard failure rather than a silent "
        "no-op. E5: the result carries the resulting cursor — always the "
        "fresh log's cursor here, never a value from before this call ran. "
        "TURN_STILL_RUNNING (Finding 1): an in-flight turn that did not stop "
        "within the bounded wait after abort() — nothing was touched; retry, "
        "or wait for agent_end first."
    ),
    params_schema=NEW_SESSION_PARAMS_SCHEMA,
    result_schema=SESSION_LIFECYCLE_RESULT_SCHEMA,
)
async def _handle_new_session(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    runtime = _require_runtime(handler)
    # The wire's documented default, applied HERE and passed down explicitly —
    # the runtime below has no default of its own to inherit, on purpose.
    outcome = await runtime.new_session(persist=params.get("persist", True))
    return _lifecycle_result(outcome)


@command(
    "fork",
    tier="A",
    since="phase-3",
    notes=(
        "H1: branches the CURRENT session's active-path history into a new, "
        "addressable session, and moves this connection onto it — the source "
        "is left untouched on disk. Same {cancelled} veto contract as "
        "new_session (H2). E5: the result carries the FORK's resulting "
        "cursor (its tip after copying the source's history), not the source "
        "session's cursor. Same TURN_STILL_RUNNING failure mode as "
        "new_session (Finding 1). `session.addressable` is always true here "
        "(SessionCatalog.fork always writes — there is no unpersisted fork), "
        "so the fork's id is one list_sessions returns and switch_session "
        "accepts; the field is reported rather than assumed, because a host "
        "reads ONE contract across all three of these verbs (finding 7)."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=SESSION_LIFECYCLE_RESULT_SCHEMA,
)
async def _handle_fork(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    runtime = _require_runtime(handler)
    outcome = await runtime.fork()
    return _lifecycle_result(outcome)


@command(
    "switch_session",
    tier="A",
    since="phase-3",
    notes=(
        "H1: loads a different, already-addressable session (resolved by id "
        "or unique id prefix — SWITCH_SESSION_PARAMS_SCHEMA; list_sessions "
        "is where those ids come from, finding 8, and its rows are exactly "
        "what this verb resolves against) and moves this "
        "connection onto it. Same {cancelled} veto contract as new_session "
        "(H2); an unresolvable session_id raises INVALID_PARAMS instead — a "
        "bad id is a caller mistake the schema cannot catch syntactically, "
        "not a veto. E5: the result carries the LOADED session's cursor. "
        "Same TURN_STILL_RUNNING failure mode as new_session (Finding 1) — "
        "checked after resolution, so a bad id still fails INVALID_PARAMS "
        "even with an in-flight turn."
    ),
    params_schema=SWITCH_SESSION_PARAMS_SCHEMA,
    result_schema=SESSION_LIFECYCLE_RESULT_SCHEMA,
)
async def _handle_switch_session(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    runtime = _require_runtime(handler)
    try:
        outcome = await runtime.switch_session(params["session_id"])
    except LookupError as exc:
        raise RPCError(INVALID_PARAMS, str(exc), data={"session_id": params["session_id"]}) from exc
    return _lifecycle_result(outcome)


# ─────────────────────────────────────────────────────────────────────────
# Tier B shared helpers (B0, docs/RPC-TIER-B.md §3 "B0 — scaffolding").
#
# Two helpers every Tier B unit builds on, so none of B1/B2/B4/B5 hand-rolls
# its own copy of either pattern:
#   - turn_safety_guard   — D-1's bounded turn_lock acquire + TURN_STILL_RUNNING.
#   - require_log_appender — §1.1's "the bound log must have this appender,
#     else raise".
# Both live here, in the table-definition area, rather than inside any one
# verb's marker region below — B0 lands first and commits this section
# before any of the six worktrees branch, so there is nothing here for a
# parallel unit to conflict on.
# ─────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def turn_safety_guard(
    session: "AgentSession", *, timeout: float = DEFAULT_SWAP_TIMEOUT_S
) -> AsyncIterator[None]:
    """D-1: the bounded ``turn_lock`` acquire every MUTATING Tier B verb
    takes before touching session state, so a verb never races a turn that
    is concurrently reading/writing the same state.

    ``async with turn_safety_guard(handler.session): <mutate state>`` —
    acquires ``session.turn_lock``, bounded by ``timeout``, yields with the
    lock HELD, and releases it on the way out (success or exception) exactly
    as ``AgentSessionRuntime._apply_swap`` does around its own reset-and-swap
    (``agent_session_runtime.py:484-492``). On a timeout, raises
    ``RPCError(TURN_STILL_RUNNING, ...)`` directly rather than proceeding —
    and never silently skips the guard.

    This EXTRACTS the bounded-wait pattern phase 3 already established —
    ``agent_session_runtime.py:473``'s ``await asyncio.wait_for(session
    .turn_lock.acquire(), timeout=...)``, whose timeout outcome
    ``commands._lifecycle_result`` (``commands.py:1199-1210``) converts to
    this exact ``RPCError`` — into ONE reusable function, so B1/B2/B4/B5 do
    not each reimplement the ``wait_for``/``except TimeoutError`` pair. It is
    NOT the same function as ``_apply_swap``, and phase 3's own two call
    sites are untouched by this helper existing: ``_apply_swap`` also calls
    ``session.abort()`` first (H4 — it is about to DISCARD the log a running
    turn would otherwise keep writing to) and returns a plain
    ``{"blocked": True, ...}`` dict rather than raising, because its own
    module deliberately has no JSON-RPC vocabulary to raise with
    (``agent_session_runtime.py``'s module docstring: "this module itself
    has no JSON-RPC in it" — ``AgentSessionRuntime`` also serves non-RPC
    callers). A Tier B mutating verb has no log to swap and no reason to
    force a running turn to stop merely because, say, a host asked to rename
    the session — it REFUSES honestly instead, and lets the caller retry
    once the turn ends.

    Reuses ``AgentSessionRuntime.DEFAULT_SWAP_TIMEOUT_S`` rather than
    inventing a second "how long is a host willing to wait" constant: the
    reasoning behind that value — stay off the RPC reader's single serial
    chokepoint, since ``transport._read_stdin`` awaits every dispatched line
    to completion before parsing the next one — applies identically here,
    and is documented once, on that constant, rather than repeated.

    Callers (docs/RPC-TIER-B.md D-1 — the MUTATING Tier B verbs):
    ``set_model`` (B1), ``compact`` (B2), ``set_auto_compaction`` (B4),
    ``set_session_name`` (B5). The tier's reads take no guard:
    ``get_session_stats`` (B3), ``get_last_assistant_text`` (B6),
    ``get_session_name`` (B5's second verb), ``get_models`` (no B row —
    finding 7 of the Tier B review) and ``list_sessions`` (no B row either —
    finding 8 of the same review). Both lists are pinned complete by
    ``test_rpc_tier_b_scaffolding.py``'s
    ``test_the_prose_enumerations_of_tier_b_name_every_verb``.
    """
    try:
        await asyncio.wait_for(session.turn_lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RPCError(
            TURN_STILL_RUNNING,
            f"a turn is in flight and did not stop within {timeout:g}s of "
            "this call — retry, or wait for agent_end before retrying",
        ) from None
    try:
        yield
    finally:
        session.turn_lock.release()


def require_log_appender(session: "AgentSession", appender_name: str, *, verb: str) -> None:
    """§1.1: "the bound log must have this appender, else raise" — never a
    silent no-op.

    **Not a durability check, and never sufficient on its own** (Blocker 2 of
    the Tier B review; §1.1 has been corrected to say so). This aims at the
    METHOD axis: does the concrete bound object have somewhere to *call*?
    Every real ``ConversationSession`` — including an unpersisted one — has
    every appender, so on the RPC path this check passes on exactly the
    session whose appends go nowhere. A verb that promises durability calls
    :func:`require_durable_session` as well; this one only rules out a log
    that cannot take the entry at all (the SDK's ``InMemorySessionLog``).

    Raises ``RuntimeError`` unless ``session.session_log`` has an attribute
    named ``appender_name``. An ``InMemorySessionLog`` (the SDK / RPC-with-
    no-file-backing case) has nowhere durable to put a ``model_change`` or
    ``session_info`` entry, and the ``SessionLog`` Protocol deliberately
    OMITS these appenders (``session_log.py:38-48``: "``AgentSession`` never
    calls them ... so keeping them off the Protocol avoids an unused-method
    contract") precisely so a caller cannot assume they exist structurally —
    each call site must check, on the CONCRETE bound object, before appending.

    Reference implementation: ``ExtensionContext.set_session_name``
    (``extension_types.py:2203``), copied shape-for-shape — same ``hasattr``
    check on ``session_log``, same raise in place of the silent no-op it
    replaced. That method's own docstring names the bug this guards against:
    "The prior implementation looked for a ``_session_name`` attribute that
    ``AgentSession`` never defines — a silent no-op on every real session
    (only a ``MagicMock``'s auto-vivified attributes made the old tests
    pass)." This helper generalizes that shape so B1 and B5 share one
    implementation instead of each repeating the ``hasattr``/raise pair.

    Does not itself call the appender — a pure precondition check, so a
    caller does ``require_log_appender(session, "append_model_change",
    verb="set_model")`` then ``session.session_log.append_model_change(...)``
    as two explicit steps, never a hidden third thing this function does on
    a caller's behalf.

    Future callers (docs/RPC-TIER-B.md §1.1): B1's ``set_model`` (checks for
    ``append_model_change``), B5's ``set_session_name`` (checks for
    ``append_session_info``).
    """
    log = session.session_log
    if not hasattr(log, appender_name):
        raise RuntimeError(
            f"{verb}: the bound session log has no {appender_name!r} — "
            "nowhere durable to land this entry (e.g. an in-memory RPC session)"
        )


#: How a ``ConversationSession`` declares WHERE it durably lives — one
#: attribute name per store τ ships (Blocker 2 of the Tier B review).
#:
#: ``path`` — ``tau_coding_agent.session_store.Session``: a ``Path``, or
#: ``None`` on the ``create_in_memory``/``create_ephemeral`` product, whose
#: ``_persist_header``/``_persist_entry`` are then ``return``-on-``None``
#: no-ops (``session_store.py:585,593``).
#: ``root_doc_id`` — ``tau_jmfts.store.JmftsSessionLog``: the JMFTS document
#: id of the conversation root, which its own docstring names as "the
#: storage-agnostic ``ref`` a future catalog resolves ``load()`` against".
#: That store's ephemeral product (``tau_jmfts.catalog
#: ._EphemeralConversationSession``) has neither name — deliberately, since
#: its docstring rejects "silently returning a file-backed Session" as a
#: dishonest out.
#:
#: A NAME LIST rather than a Protocol member because the durability question
#: is asked HERE, at the wire, and answering it in the ``ConversationSession``
#: Protocol would force every store — and every test double — to grow a
#: member nothing else calls, which is the exact cost ``SessionLog``'s own
#: docstring exists to refuse. The coupling is real and is priced: a store
#: that renames its location attribute makes the RPC layer REFUSE every
#: appending verb (D-7 rule 1) — loud, and caught by the class-level pins in
#: ``test_rpc_tier_b_scaffolding.py``), never silently promise them. That
#: asymmetry is the whole point — unknown means no, not yes.
_DURABLE_LOCATION_ATTRS: tuple[str, ...] = ("path", "root_doc_id")


def _declared_durable_locations(log: object) -> dict[str, Any]:
    """Which of :data:`_DURABLE_LOCATION_ATTRS` this log declares, and to
    what. One implementation, two callers with different jobs:
    :func:`require_durable_session` (D-7 rule 1 — which of "declares nothing"
    and "declares None" happened decides which refusal message the host
    gets) and :func:`session_log_is_addressable` (finding 8 — which only
    needs the yes/no).
    """
    return {name: getattr(log, name) for name in _DURABLE_LOCATION_ATTRS if hasattr(log, name)}


def session_log_is_addressable(log: object) -> bool:
    """Whether this session is one a later ``switch_session`` could reach —
    the predicate ``new_session``/``fork``/``switch_session`` publish as
    ``session.addressable`` (finding 7 of the Tier B review).

    Finding 7 measured ``new_session {"persist": false}`` handing back
    ``{"store": "file", "session_id": "5543562f…", "lane": "primary",
    "cursor": "614c4017"}`` under a schema whose description read "F2's
    addressable tuple" — while ``switch_session`` on that very id answered
    ``-32602 no session matches '5543562f…'``, and ``store: "file"`` named a
    file that was never created. Unconditional prose about a value that has
    become conditional (the previous round turned ``persist`` into a
    documented, selectable mode) is the defect; this is the field that makes
    the result say which it is.

    **Deliberately the SAME question D-7 asks**, not a second one: a session
    is addressable exactly when it declares a durable location and that
    location is set, because that is also what puts it in the store's
    listing — ``list_sessions`` returns ``SessionCatalog.list(cwd)``, and
    ``switch_session`` resolves through ``resolve_ref``, which is built on
    that same listing. So "addressable" is not an opinion this function
    forms: it is "``list_sessions`` will return this id", stated at the
    moment the id is minted. An ephemeral session (``create_ephemeral`` —
    the file store's ``path``-less ``Session``, the JMFTS store's
    ``_EphemeralConversationSession``, which declares neither name) is
    therefore ``false``, and the verbs D-7 rule 1 governs refuse on it for
    the same underlying reason.

    A predicate rather than a raise, because the caller is not promising
    anything here: ``new_session`` was ASKED for an unpersisted session and
    correctly made one. What it must not do is describe it as addressable.
    """
    declared = _declared_durable_locations(log)
    return bool(declared) and any(value is not None for value in declared.values())


def require_durable_session(session: "AgentSession", *, verb: str) -> None:
    """The precondition a verb takes before promising a durable write — the
    corrected §1.1 guard (Blocker 2 of the Tier B review).

    Raises ``RPCError(SESSION_NOT_PERSISTED)`` unless the bound ``session_log``
    declares a durable location (:data:`_DURABLE_LOCATION_ATTRS`) and that
    location is actually set.

    Callers, and the rule that decides who calls it — D-7, stated once in
    the "DURABILITY in Tier B" block below: **every verb that APPENDS a
    session-log entry**. ``set_model`` (D-2's ``model_change``),
    ``set_session_name`` (``session_info``), and ``compact`` (the
    ``compaction`` entry — added by finding 6 of the Tier B review, which
    measured that verb running to completion on an unpersisted session and
    reporting a cursor for an entry that dies with the process, while the
    other two refused). A verb that appends nothing does not call this, and
    ``set_auto_compaction`` is the case that makes the line worth drawing:
    it mutates, and it is guarded by D-1, but its whole product is an
    in-memory field, so refusing it would deny a working capability over a
    promise it never made.

    **Why this exists, and why the old check did not cover it.**
    ``require_log_appender`` checks that the bound log HAS the appender.
    Every real ``ConversationSession`` has every appender, persisted or not,
    so on the RPC path that check passed on the one session every host starts
    on — a ``create_ephemeral`` session whose ``_persist_*`` are no-ops — and
    both verbs returned a cursor for an entry that was never written
    anywhere. D-2 exists because "a later replay of the session shows no
    record that it happened"; a promise that leaves no file is the same
    silent no-op one layer up. Method presence was the wrong axis.

    **Raise, not report.** The alternative — succeed and say
    ``{"durable": false}`` in the result — is rejected: a host asked for a
    thing this session cannot do, the result schemas' ``cursor`` is
    documented as the tip AFTER the write (E5), and Fail-Early's whole
    argument is that a caller finding out later is worse than a caller
    finding out now. Nothing is mutated before this check runs, so the
    refusal is also total: the model is not switched, no name is applied.

    ``RPCError(SESSION_NOT_PERSISTED)``, a code of its own — the OPPOSITE of
    what this guard first shipped with, corrected at round 3 of the Tier B
    review (finding 4). The original argument was ``set_session_name``'s about
    the appender raise: "the ``dialect`` error set is a published, closed
    vocabulary, and one unusual refusal does not earn an addition to it". That
    is a real principle and it loses here, because ``dialect``'s own doctrine —
    written on this same branch, for ``REQUEST_TOO_LARGE`` — says the
    -32000..-32099 range is exactly where a "structured, EXPECTED outcome the
    protocol has a considered answer for" belongs, as against "the handler
    raised something nobody planned for". This refusal is the most deliberate
    thing in the tier, and ``INTERNAL_ERROR`` is defined in the generated
    reference as meaning it was not deliberate at all: the two units answered
    one question opposite ways, and a host was left telling a considered
    refusal from a τ crash by matching English prose. See
    :data:`~tau_agent_core.rpc.dialect.SESSION_NOT_PERSISTED`.

    A host that wants durability and got this back has one honest fix — put
    the connection on a persisted session (``fork``, ``switch_session``, or
    ``new_session`` with ``persist`` left at its default). No ``data`` of its
    own: D2 already puts the ``method`` on every error where one was
    identified, and the ``verb`` this guard was given IS that method — a
    second copy under a different key is a field a host has to learn in
    exchange for nothing. (The session id is likewise absent, deliberately:
    this guard runs on log stubs with no ``AgentSession.state`` to snapshot,
    and a host on this connection already knows which session it is bound
    to.)
    """
    log = session.session_log
    declared = _declared_durable_locations(log)
    if not declared:
        raise RPCError(
            SESSION_NOT_PERSISTED,
            f"{verb}: the bound session log ({type(log).__name__}) declares no durable "
            f"location (none of {', '.join(_DURABLE_LOCATION_ATTRS)}) — this verb will "
            "not return a cursor for a write it cannot promise survives the process",
        )
    if all(value is None for value in declared.values()):
        empty = ", ".join(sorted(declared))
        raise RPCError(
            SESSION_NOT_PERSISTED,
            f"{verb}: this session is unpersisted ({empty} is None — e.g. the product of "
            'new_session {"persist": false}), so the entry would land in memory only; '
            "move onto a persisted session (fork/switch_session/new_session) and retry",
        )


# ─────────────────────────────────────────────────────────────────────────
# E5 in Tier B — ONE answer, applied to every `since="tier-b"` verb.
#
# No verb COUNT is stated anywhere in this block on purpose: `get_models`
# landed after this rule was written (finding 7 of the same review) and
# falsified every hand-written tally in the tier at a stroke. The
# enumerations below are pinned instead — see the bottom of this block.
#
# E5 (docs/REMOTE-CONTROL.md §4[4], line 267) is stated unconditionally:
# "Every response to a mutating command returns the resulting cursor." The
# tier first shipped TWO readings of it — `compact` returned the tip even
# when it changed nothing ("the unchanged current tip"), while
# `set_auto_compaction`, equally mutating and equally guarded by D-1,
# returned no `cursor` key at all and neither its schema nor its notes said
# why (finding 5 of the Tier B review). This is the settled rule, written
# here rather than re-derived per verb:
#
#   1. A MUTATING verb's COMPLETION always carries `cursor`, `required` in
#      the schema that describes it and present on every success — INCLUDING
#      when the call advanced nothing: a set that changed no value, a
#      compaction that found nothing to compact, a verb that appends no log
#      entry at all. "Completion" is the response itself for the
#      synchronous mutators (`set_model`, `set_auto_compaction`,
#      `set_session_name`) and the `compaction_end` notification for
#      `compact`, whose response is only an acknowledgement (C3/D-5).
#   2. A READ never carries one. `get_last_assistant_text`, `get_models`,
#      `get_session_name`, `get_session_stats` and `list_sessions` have no
#      `cursor` field; a host that wants the tip without mutating calls
#      `get_state`.
#   3. Absence is never a signal. Omitting `cursor` to mean "nothing moved"
#      would make a host infer the tip from a missing key, which is exactly
#      the inference F3 (§7.2, "no host may cache 'the tip'") exists to
#      forbid — and it costs that host a round trip to learn what the
#      response in its hand could have told it.
#
# `abort` (and `submit`/`prompt`) are NOT counterexamples, and the exception
# they carve is about TIME, not about no-ops: those verbs return before the
# mutation they ask for has happened, so any cursor taken at signal time
# would be the PRE-mutation tip — see `abort`'s own notes, which record the
# phase-2 trace that measured the difference. Rule 1 applies wherever the
# mutation is already complete when the completion is built, which is every
# Tier B mutator.
#
# Pinned by `test_rpc_tier_b_scaffolding.py`'s
# `test_e5_is_answered_one_way_across_tier_b`, which also fails when a NEW
# `since="tier-b"` verb is added without classifying it as a read or a
# mutator — and by `test_the_prose_enumerations_of_tier_b_name_every_verb`,
# which fails when a new verb is classified there but left out of rule 1's
# or rule 2's list above (or out of `turn_safety_guard`'s docstring).
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# DURABILITY in Tier B (D-7) — ONE answer, applied to every `since="tier-b"`
# verb. Written here so a host reads it once instead of deriving it from
# whichever verb it happened to try first.
#
# Finding 6 of the Tier B review measured three different answers on ONE
# `new_session {"persist": false}` session: `set_model` and
# `set_session_name` refused (-32603 "this session is unpersisted"),
# `set_auto_compaction` returned a cursor, and `compact` ran to completion
# and reported a cursor for a `compaction` entry that dies with the process.
# No verb's notes said which of those was the rule.
#
# The rule, and it is mechanical — a host can apply it without knowing any
# verb's intent:
#
#   1. A verb that APPENDS a session-log entry calls
#      `require_durable_session` FIRST and refuses an unpersisted session
#      outright: `set_model` (D-2's model_change), `set_session_name`
#      (session_info), `compact` (compaction). Nothing is mutated before the
#      refusal, so it is total.
#   2. A verb that appends NOTHING never asks the question:
#      `get_last_assistant_text`, `get_models`, `get_session_name`,
#      `get_session_stats`, `list_sessions`, and `set_auto_compaction` — the
#      last of which is why this rule is worth writing down, being a MUTATOR
#      (D-1-guarded, E5-cursor-carrying) whose whole product is an in-memory
#      field on `CompactionSettings`. Its `cursor` is the live tip reported
#      as a READ (E5 rule 1 still requires it on a mutator's completion), not
#      a claim that this call wrote anything.
#      No verb COUNT appears above, for the reason the "E5 in Tier B" block
#      states about its own lists: `get_models` landed after that rule was
#      written and falsified every hand-written tally in the tier at a
#      stroke. The enumerations are pinned instead.
#   3. It is the SESSION's durability that is in question, never the
#      directory it lives in. Unit S moved `--mode rpc`'s default session
#      base to `<tmp>/.tau-<uid>/sessions` (D-6); that changes how LONG a persisted
#      session lasts — stated on the wire in `set_model`'s and
#      `set_session_name`'s notes — and changes nothing here. This rule keys
#      on `path is None`, which is what `require_durable_session` asks.
#
# Rule 1 is not new for `compact`; it is Blocker 2's answer, applied to the
# one verb that had not been given it. `set_model` mutates live state AND
# appends, and Blocker 2 settled that it refuses — so "it also does
# something in memory" is already known not to buy an exemption, and giving
# `compact` the opposite answer would be the fourth derivation, not a
# principle.
#
# TWO COSTS, both stated rather than hidden:
#
#   - `compact` is now REFUSED on an unpersisted session, where it used to
#     work. A host that wants both is asking for two contradictory things
#     (nothing survives this process / rewrite the log I am keeping), and the
#     honest fix is the one `require_durable_session`'s message already
#     names: move onto a persisted session. Auto-compaction remains available
#     there — see the next bullet, which is the same fact seen as a gap.
#   - `set_auto_compaction(enabled=true)` on an unpersisted session ARMS a
#     mechanism that then appends `compaction` entries to that same
#     non-durable log, from inside `AgentSession._maybe_auto_compact` — a
#     code path with no RPC verb on it and therefore nothing for rule 1 to
#     guard. Same class as the gap `compact`'s notes already record about
#     `AgentSession.compact()` not taking `turn_lock` itself: this tier
#     guards the wire, not `AgentSession`'s internals, and widening
#     `AgentSession` is out of its scope.
#
# Out of scope, deliberately: `new_session`/`fork`/`switch_session` (Tier A).
# They do not append to the bound log, they REPLACE it, and `persist` is
# `new_session`'s own published parameter — a host states durability there
# rather than discovering it.
#
# Pinned by `test_rpc_tier_b_scaffolding.py`'s
# `test_d7_is_answered_one_way_across_tier_b`, which reads the shipped
# handler sources: a guarded verb that loses its `require_durable_session`
# call, an unguarded verb that grows one, or a NEW `since="tier-b"` verb
# classified in neither list, all fail there.
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# Tier B verb regions (docs/RPC-TIER-B.md). Six came from B0's scaffolding;
# `get_models` and `list_sessions` were added by the finding-7 and finding-8
# units of the Tier B review in the same alphabetical scheme. One per WRITING
# UNIT rather than per verb — B5 owns both `set_session_name` and
# `get_session_name` and holds them in the one region, which is the property
# that matters here (a region is a claim on the file, not an index of the
# table). Alphabetically ordered and CONTIGUOUS. Parallel agents each write
# ONLY inside their own region, in separate worktrees (docs/RPC-TIER-B.md §3
# "B0 — scaffolding" point 1) — that is the whole reason these exist EMPTY
# ahead of any verb's implementation, and why an empty pair must not be
# deleted as clutter: it is a reservation, not dead code. A verb's schema
# constants and its `@command(...)`-decorated handler both go inside its own
# region.
# ─────────────────────────────────────────────────────────────────────────

### begin tier-b:compact

COMPACT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "custom_instructions": {
            "type": "string",
            "description": (
                "Optional extra focus for the generated summary, threaded "
                "unchanged to AgentSession.compact(custom_instructions=...)."
            ),
        },
    },
    "additionalProperties": False,
}

#: Blocker 1 (Tier B review): `compact`'s RESPONSE is now an
#: acknowledgement, not the outcome — the summarization LLM call it starts is
#: bounded by nothing but the provider (measured: 20s on a gated fake, during
#: which `get_state` and `abort` were not merely slow but UNPARSED, because
#: `transport._read_stdin` awaits each dispatched line to completion before
#: reading the next). So this verb takes C3's dual completion, exactly as
#: `submit`/`prompt` do (`_submit_and_acknowledge`): this response says only
#: that the compaction was admitted and is running; the outcome arrives
#: later, on the `compaction_end` notification below.
#:
#: `accepted` is always `true` when this shape is returned at all — a refusal
#: is an error response (TURN_STILL_RUNNING), never `accepted: false` — and
#: is present for symmetry with `_accept_result`'s C3 shape rather than as a
#: field a host must branch on.
COMPACT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accepted": {
            "type": "boolean",
            "description": (
                "Always true — the compaction was admitted and is now running "
                "in the background. A refusal is an error response instead "
                "(TURN_STILL_RUNNING), never accepted: false."
            ),
        },
        "compaction_id": {
            "type": "string",
            "description": (
                "Correlates this acknowledgement to the compaction_end "
                "notification that reports the outcome. Server-generated; a "
                "host does not supply it."
            ),
        },
    },
    "required": ["accepted", "compaction_id"],
}

#: The JSON-RPC notification method `compact`'s SECOND completion arrives on.
#: A distinct method rather than the ordinary `event` channel: every `event`
#: notification carries a `WireEvent` (`rpc_event_schema.py`), whose `type`
#: is a closed Literal copy of `AgentEvent.type` — a compaction outcome is
#: not an `AgentEvent` and has nowhere to live in that model without
#: widening two modules this unit does not own. See the verb's `notes` for
#: the discoverability consequence, stated rather than hidden.
COMPACTION_END_METHOD = "compaction_end"

#: The payload of that notification — the shape this verb's RESULT used to
#: have, plus the two correlation fields (`compaction_id`, `request_id`) a
#: host needs now that it no longer rides the response, plus `is_error`/
#: `error` (the `agent_end` spelling) for a compaction that raised.
#:
#: `AgentSession.compact()` returns `CompactionResult | None` (§1 ground
#: truth) — `None` is a REAL outcome (an empty conversation; one already
#: ending in a compaction summary; or a cut that would remove no message
#: from the context, which under the shipped `keep_recent_tokens` is every
#: conversation smaller than 20000 tokens and therefore this verb's ORDINARY
#: default-settings answer), not an error, so `performed` carries that
#: outcome on the wire rather than a raised/absent distinction a client
#: would have to infer.
#: `performed=true` mirrors `CompactionResult`'s own fields one-for-one
#: (`compaction.py`): `summary`, `first_kept_entry_id`, `tokens_before`,
#: `tokens_saved`, `compacted_entry_ids`, `usage`, plus `read_files`/
#: `modified_files` flattened out of `CompactionDetails` (never `null` when
#: `details` itself is `None` — that case reports empty lists, the same
#: "nothing observed" `CompactionDetails`'s own default factory reports,
#: not a fabricated value). `performed` is ABSENT (not `false`) when
#: `is_error` is true: a compaction that raised did not "not perform because
#: there was nothing to compact", and reporting the two the same way would
#: fabricate an outcome nobody observed (Fail Early). `cursor` is always
#: present (E5) — the post-compaction tip when `performed`, otherwise the
#: unchanged tip the call left in place — because by the time this
#: notification is built the mutation (or non-mutation, or failure) has
#: already fully happened, unlike `abort`'s signal-only shape (see
#: `ABORT_RESULT_SCHEMA`'s notes on why THAT verb omits cursor).
COMPACTION_END_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "compaction_id": {
            "type": "string",
            "description": "The id the compact acknowledgement returned (correlation).",
        },
        "request_id": {
            "type": ["integer", "null"],
            "description": (
                "The JSON-RPC id of the compact request that started this "
                "compaction — null when that request was a notification "
                "(no id), which is the one case compaction_id is the only "
                "correlation handle."
            ),
        },
        "is_error": {
            "type": "boolean",
            "description": (
                "True when AgentSession.compact() raised (e.g. CompactionError "
                "— summary generation failed and, Fail-Early, nothing was "
                "written). `error` carries the detail and `performed` is absent."
            ),
        },
        "error": {
            "type": ["string", "null"],
            "description": "The exception's repr when is_error, else null.",
        },
        "cancelled": {
            "type": "boolean",
            "description": (
                "True when a host's `abort` stopped this compaction part-way "
                "(finding 5, Tier B review). Nothing was written — the "
                "summary is generated before the entry is appended — so "
                "`performed` is ABSENT, exactly as it is when is_error is "
                "true, and `cursor` is the unchanged tip. False on every "
                "other outcome rather than omitted: absence is not this "
                "tier's way of saying anything (E5 rule 3). A compaction "
                "cancelled by SHUTDOWN never reaches this notification at "
                "all — that one reports on stderr (D-5, T4)."
            ),
        },
        "performed": {
            "type": "boolean",
            "description": (
                "False when AgentSession.compact() returned None — a real "
                "outcome (nothing to compact), not an error, and the expected "
                "answer under the shipped keep_recent_tokens for any "
                "conversation smaller than it, because the cut then removes "
                "nothing. Absent entirely when is_error or cancelled is true. "
                "Every CompactionResult field below is absent unless this is "
                "true."
            ),
        },
        "summary": {
            "type": "string",
            "description": "CompactionResult.summary — the generated text.",
        },
        "first_kept_entry_id": {
            "type": "string",
            "description": "Session-log entry id of the first entry kept verbatim after the cut.",
        },
        "tokens_before": {
            "type": "integer",
            "description": "Estimated context tokens before this compaction.",
        },
        "tokens_saved": {
            "type": "integer",
            "description": (
                "Estimated context tokens this compaction removed: the "
                "summarized prefix, less the summary that replaces it. NOT "
                "tokens_before less the summary — tokens_before includes the "
                "recent context the cut keeps. May be negative when the "
                "summary is larger than the prefix it replaced; that is "
                "reported rather than clamped to 0."
            ),
        },
        "compacted_entry_ids": {
            "type": "array",
            "description": "Session-log entry ids folded into the summary.",
        },
        "read_files": {
            "type": "array",
            "description": "CompactionDetails.read_files ([] when details is None).",
        },
        "modified_files": {
            "type": "array",
            "description": "CompactionDetails.modified_files ([] when details is None).",
        },
        "usage": {
            "type": "object",
            "description": (
                "What GENERATING this summary cost (CompactionResult.usage) — "
                "routinely the priciest single call in a session; distinct "
                "from tokens_saved, which is what compaction bought."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor once the compaction finished (E5/F3): the "
                "post-compaction tip when performed is true, else the "
                "unchanged tip."
            ),
        },
    },
    "required": ["compaction_id", "request_id", "is_error", "cancelled", "cursor"],
}

# Not a `CommandEntry` (a notification has no row in the command table), so
# `CommandEntry.__post_init__`'s import-time vocabulary check never sees it —
# run it here explicitly instead. Without this, a keyword `validate_params`
# does not implement could sit in the schema above validating nothing, which
# is precisely the failure `_assert_supported_schema` exists to make loud.
_assert_supported_schema(COMPACTION_END_PARAMS_SCHEMA, f"{COMPACTION_END_METHOD} (notification)")


def _compaction_outcome(result: "CompactionResult | None", cursor: str | None) -> dict[str, Any]:
    """The `CompactionResult | None` half of a `compaction_end` payload."""
    if result is None:
        return {"cancelled": False, "performed": False, "cursor": cursor}
    details = result.details
    return {
        "cancelled": False,
        "performed": True,
        "summary": result.summary,
        "first_kept_entry_id": result.first_kept_entry_id,
        "tokens_before": result.tokens_before,
        "tokens_saved": result.tokens_saved,
        "compacted_entry_ids": result.compacted_entry_ids,
        "read_files": details.read_files if details is not None else [],
        "modified_files": details.modified_files if details is not None else [],
        "usage": result.usage,
        "cursor": cursor,
    }


@command(
    "compact",
    tier="B",
    since="tier-b",
    notes=(
        "Dual completion (C3), the same shape submit/prompt use: THIS "
        "response only acknowledges that a compaction was admitted and is "
        "running ({accepted, compaction_id}); the outcome arrives later as a "
        "`compaction_end` NOTIFICATION whose REQUIRED keys are compaction_id "
        "+ request_id (correlation), is_error, cancelled and cursor (E5) — "
        "`cancelled` is on EVERY one of them, false on the ordinary paths, "
        "and it is what distinguishes 'a host aborted this' from 'this "
        "failed' and from 'this found nothing'. Optional beside those: error, "
        "performed, and the full CompactionResult "
        "(summary/first_kept_entry_id/tokens_before/tokens_saved/"
        "compacted_entry_ids/usage, plus CompactionDetails' read_files/"
        "modified_files). COMPACTION_END_PARAMS_SCHEMA is the authority and a "
        "test pins this sentence against it, because the first version of "
        "this list was written before `cancelled` existed and silently stayed "
        "wrong for a round (round-3 finding 3 of the Tier B review) — a "
        "second implementor building a parser from it would have omitted a "
        "required field. Blocker 1 (Tier B "
        "review) is why: summarization is an unbounded provider call, and "
        "running it inline on the dispatch path stopped transport._read_stdin "
        "from PARSING the next line — measured at 20s, with `abort` itself "
        "unanswerable for the duration, which is exactly the availability "
        "property REMOTE-CONTROL.md §4[1] refuses to trade ('a host whose "
        "whole problem is that τ is producing faster than it can read is "
        "precisely the host that needs to abort'). "
        "performed=false reports AgentSession.compact() returning None (§1 "
        "ground truth) as a real outcome, not an error — an empty "
        "conversation, one already ending in a compaction summary, or a cut "
        "that would remove no message from the context at all, which under "
        "the shipped keep_recent_tokens (20000) is what any smaller "
        "conversation gets, so it is this verb's ordinary default-settings "
        "answer rather than an edge case; when "
        "is_error is true, performed is ABSENT rather than false (a "
        "compaction that raised did not 'find nothing to compact'). "
        "E5, answered the one way the whole tier answers it (see commands.py "
        "'E5 in Tier B'): `cursor` rides the COMPLETION — the compaction_end "
        "notification, where the mutation has genuinely happened — never the "
        "acknowledgement, which is built before it has; and it is present on "
        "ALL THREE outcomes, the post-compaction tip when performed and the "
        "unchanged tip when performed=false or is_error, because absence is "
        "not this tier's way of saying 'nothing moved'. "
        "custom_instructions, when given, is threaded unchanged to "
        "AgentSession.compact. "
        "Refusals, all three on THIS response. Two are TURN_STILL_RUNNING "
        "(D-1's vocabulary for 'no, a thing is running'): a turn that did not "
        "release turn_lock within DEFAULT_SWAP_TIMEOUT_S (the guard is taken "
        "in the background task and HELD across the whole compact() call, so "
        "a submit() using 'enqueue'/'rollback' cannot interleave with it), "
        "and a second compact while one is already in flight — refused "
        "immediately, without waiting out the guard, since a compaction "
        "holds that lock for as long as the provider takes. The third is "
        "D-7 (see commands.py 'DURABILITY in Tier B'): this verb appends a "
        "`compaction` entry, so an UNPERSISTED session — the product of "
        'new_session {"persist": false} — is refused outright '
        "(require_durable_session -> SESSION_NOT_PERSISTED), checked "
        "before the single-flight slot is taken and before the provider is "
        "paid, and the same answer set_model and set_session_name give. "
        "Stated cost, not hidden: compaction is unavailable on an "
        "unpersisted session; the fix is to move onto a persisted one. "
        "WHERE the entry lands, and for how long (unit S, added at this "
        "review's integration — the other two appending verbs said it and "
        "this one did not): a --mode rpc process defaults to storing its "
        "sessions under a private <tmp>/.tau-<uid>/sessions, NOT the user's "
        "~/.tau/sessions. Most systems clear the temp dir on reboot, so the "
        "durability D-7 refuses to promise without is itself bounded by "
        "MACHINE UPTIME rather than forever — including the compaction entry "
        "and the rewritten tree behind it. A host that needs more must be "
        "started with --session-dir DIR (accepted under --mode rpc precisely "
        "so a host can choose, including --session-dir ~/.tau/sessions). "
        "ABORT (finding 5, Tier B review): a host's `abort` now cancels an "
        "in-flight compaction, where it used to answer 'aborted' while the "
        "tree was rewritten anyway. The compaction_end that follows carries "
        "cancelled=true, no `performed`, and the unchanged cursor; nothing "
        "was written, because the summary is generated before the entry is "
        "appended. abort's own response names the compaction_id, so the two "
        "correlate. "
        "Shutdown (D-5, as corrected by finding 3 of the Tier B review, and "
        "distinct from the abort above — nobody asked for this one): a "
        "compaction still running when the host disconnects is reaped by "
        "run()'s teardown, and it reports its outcome EXACTLY ONE of three "
        "ways, never none of them. If it finishes while the writer is still "
        "alive — which now includes the whole grace period run() gives "
        "background tasks, because that reap was moved ahead of the stdout "
        "drain — the ordinary compaction_end is delivered. If it finishes "
        "after the writer is genuinely gone (broken pipe, or SIGTERM "
        "cancelling the writer outright), the full outcome goes to stderr "
        "(T4), because a notification nobody can read is not a completion. "
        "If it is cancelled before finishing, nothing was written and that "
        "too is said on stderr. The hole this closes was real and measured: "
        "a compaction completing inside the reap's grace window used to be "
        "enqueued onto a queue whose writer had already exited — rc 0, empty "
        "stderr, no compaction_end, and a compaction entry durably in the "
        "session log for the next process to find unannounced. "
        "Discoverability gap, stated not hidden: get_capabilities publishes a "
        "params_schema and a result_schema per verb and the event half of the "
        "capability document is generated from AgentEvent, so there is no "
        "slot in it for a server->client notification's payload. "
        "compaction_end's field list is therefore documented here and in "
        "commands.COMPACTION_END_PARAMS_SCHEMA (import-time-checked by "
        "_assert_supported_schema, like every table schema) rather than "
        "published over the wire; widening the capability document to carry "
        "notification schemas is a real gap this unit deliberately did not "
        "take on. "
        "Known gaps, stated not hidden (not fixed by this unit): "
        "AgentSession.compact() itself does not acquire turn_lock — the guard "
        "closes the race for THIS call only, and any OTHER direct caller "
        "(e.g. a future TUI path) remains unprotected; that is AgentSession's "
        "debt. compact()'s own agent_start/agent_end bracket "
        "(agent_session.py:3119,3123) is emitted via self._events.emit "
        "directly, not _emit_stamped, so — same orphan-provenance shape D-4 "
        "documents for _maybe_auto_compact — a host correlating events to "
        "submission_id sees that pair unstamped; correlate on compaction_end "
        "instead. And finding 3 (Tier B review) is only narrowed, not closed: "
        "this verb's ACKNOWLEDGEMENT still waits out the D-1 guard on the "
        "dispatch path, so a compact sent while a TURN is running still costs "
        "the reader up to DEFAULT_SWAP_TIMEOUT_S — bounded, unlike the "
        "unbounded case above, and the same bound set_model/"
        "set_auto_compaction/set_session_name each pay."
    ),
    params_schema=COMPACT_PARAMS_SCHEMA,
    result_schema=COMPACT_RESULT_SCHEMA,
)
async def _handle_compact(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Start a compaction in the background and acknowledge it (C3).

    Always returns `None`: like `_submit_and_acknowledge`, this handler
    enqueues its own response — from inside the background task, at the
    instant the D-1 guard is acquired — rather than returning a dict for
    `RPCHandler._handle_request` to send. The reason is the same one that
    function's docstring measures: `Future.set_result` does not hand control
    to the awaiting task, so a response built on this side of the future
    would race the compaction's own `agent_start`, which
    `AgentSession.compact` emits with no suspension point in between.

    Two concurrent compactions are impossible by construction: the
    single-flight check below runs synchronously, before the first `await`,
    and `handler.compaction_in_flight` stays set until the background task's
    `finally`. A second `compact` cannot even be parsed before the first has
    set it (serial reader), so the check cannot be raced.
    """
    session = handler.session
    custom_instructions = params.get("custom_instructions")

    # D-7 rule 1, BEFORE the single-flight slot is taken and before the
    # provider is paid: this verb appends a `compaction` entry, so it refuses
    # a session that cannot keep one. See the "DURABILITY in Tier B" block
    # above for why the in-memory half of the work buys no exemption.
    require_durable_session(session, verb="compact")

    in_flight = handler.compaction_in_flight
    if in_flight is not None:
        raise RPCError(
            TURN_STILL_RUNNING,
            f"compaction {in_flight} is still running — wait for its "
            "compaction_end notification before starting another",
            data={"compaction_id": in_flight},
        )
    compaction_id = str(uuid4())
    handler.compaction_in_flight = compaction_id

    loop = asyncio.get_running_loop()
    #: Resolved when the guard is held and the acknowledgement is already
    #: enqueued; carries the guard's `RPCError` instead when it refuses, so
    #: THIS call's own response is the refusal (unchanged D-1 contract).
    acknowledged: asyncio.Future[None] = loop.create_future()
    #: Finding 5: which of the two cancellation sources reached this task.
    #: `abort` (host asked, host is still there to be told) reports
    #: `cancelled: true` on `compaction_end`; a shutdown reap reports on
    #: stderr and emits nothing (D-5). `asyncio.CancelledError` alone cannot
    #: tell them apart, so the aborter records it.
    cancelled_by_abort = False

    def _acknowledge() -> None:
        # Enqueued synchronously, exactly like `_submit_and_acknowledge`'s
        # `_on_admitted` and for exactly that function's stated reason (T3:
        # `_output_queue` carries no capacity limit, so a control-plane
        # `put_nowait` cannot raise QueueFull and never has to choose
        # between reordering C3 and dropping the response).
        handler._output_queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "accepted": True,
                    "compaction_id": compaction_id,
                    "method": "compact",
                },
            }
        )
        # Finding 5: `abort` can reach this compaction from exactly here on —
        # the instant the host learns `compaction_id` exists, and the instant
        # after which cancelling the task can no longer wedge the `await
        # acknowledged` below. See `RPCHandler.bind_compaction_aborter`.
        handler.bind_compaction_aborter(_cancel_for_abort)
        acknowledged.set_result(None)

    def _cancel_for_abort() -> None:
        nonlocal cancelled_by_abort
        cancelled_by_abort = True
        # `task` is bound before `_drive` ever runs (`create_task` schedules,
        # it does not call), and this closure only ever runs from
        # `_handle_abort` — i.e. from a later dispatch, on the same loop.
        task.cancel()

    def _complete(payload: dict[str, Any]) -> None:
        params = {
            "compaction_id": compaction_id,
            "request_id": msg_id,
            **payload,
        }
        if not handler.output_is_deliverable:
            # Finding 3 (Tier B review), T4. `run()`'s writer is already
            # gone, so this `put_nowait` would enqueue onto a queue nobody
            # will ever dequeue — the measured hole in D-5's completion
            # contract, where a compaction ran, was durably written to the
            # session log, and reported nothing at all (rc 0, empty stderr).
            # `run()` now reaps background tasks BEFORE draining the writer,
            # which is what makes the ordinary shutdown DELIVER this
            # notification; this branch covers the paths where delivery is
            # genuinely impossible rather than merely late — a broken pipe
            # (T6), or SIGTERM cancelling the writer outright (P1) — and
            # reports the outcome on stderr for exactly the reason the
            # cancellation arm below already does. The whole payload, not a
            # summary: a truncated report of a mutation that landed is the
            # same defect wearing a smaller hat.
            print(
                f"[τ-rpc] compaction {compaction_id!r} finished after the RPC "
                "writer had already exited — no compaction_end could be "
                f"delivered. Outcome: {params!r}",
                file=sys.stderr,
            )
            return
        handler._output_queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "method": COMPACTION_END_METHOD,
                "params": params,
            }
        )

    async def _drive() -> None:
        try:
            try:
                async with turn_safety_guard(session):
                    _acknowledge()
                    result = await session.compact(custom_instructions=custom_instructions)
            except asyncio.CancelledError:
                # Two sources, and they get opposite answers (finding 5).
                #
                # Either way nothing was written: `AgentSession.compact()`
                # generates the summary BEFORE appending the `compaction`
                # entry, so a cancellation inside the provider call leaves the
                # log exactly as it was (Fail-Early — no partial summary is
                # ever appended).
                if cancelled_by_abort:
                    # A HOST asked for this, by name, and is still connected
                    # holding the `compaction_id` this call acknowledged. It
                    # gets told on the wire, with `cancelled: true` and no
                    # `performed`: a compaction stopped part-way neither
                    # performed nor found nothing to do. `abort`'s own
                    # response already named this id, so the pair correlates.
                    _complete(
                        {
                            "is_error": False,
                            "error": None,
                            "cancelled": True,
                            "cursor": session.session_log.cursor,
                        }
                    )
                    raise
                # `run()`'s teardown reaping this task (`_cancel_background_
                # tasks`), i.e. phase 2 — the compaction outlived the grace
                # period and was cancelled inside `AgentSession.compact()`.
                # D-5 settles what that reports: stderr (T4), and NO
                # `compaction_end`. The reason it is not the branch above:
                # nobody asked for this cancellation, so an unsolicited
                # outcome would be the process announcing its own death to a
                # host that is, in the case this arm exists for, already gone.
                #
                # This arm's ORIGINAL reason ("the writer is already gone by
                # then") lapsed with finding 3's fix: the reap now runs while
                # the writer is still alive, so a notification built here
                # WOULD reach the host. The decision above is what keeps it
                # from being built, not the wire's availability. Behaviour
                # unchanged; only the stated reason is, because a comment
                # that argues from a premise that stopped being true is how
                # the next edit gets it wrong.
                print(
                    f"[τ-rpc] compaction {compaction_id!r} was cancelled before it "
                    "finished (RPC shutdown) — no compaction_end will follow",
                    file=sys.stderr,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - see the two branches
                if not acknowledged.done():
                    # Failed before the acknowledgement — the guard's
                    # TURN_STILL_RUNNING refusal (D-1), or anything else that
                    # went wrong before compact() started. This call's own
                    # response is the only place it can surface.
                    acknowledged.set_exception(exc)
                    return
                # Already acknowledged: the outcome channel is the
                # notification, and a compaction that raised is reported as
                # is_error rather than dropped (CompactionError is the
                # expected shape here — Fail-Early, no summary was written).
                _complete(
                    {
                        "is_error": True,
                        "error": repr(exc),
                        "cancelled": False,
                        "cursor": session.session_log.cursor,
                    }
                )
                return
            _complete(
                {
                    "is_error": False,
                    "error": None,
                    **_compaction_outcome(result, session.session_log.cursor),
                }
            )
        finally:
            # Frees the single-flight slot AND drops `abort`'s handle on this
            # task in one step, so a later `abort` cannot report having
            # signalled a compaction that has already finished (finding 5).
            handler.release_compaction()
            if not acknowledged.done():
                # Unreachable on every path above (each either acknowledges,
                # sets the exception, or re-raises after `_acknowledge` ran) —
                # kept so a future edit that adds an exit path cannot wedge
                # the awaiting dispatch coroutine forever instead of failing.
                acknowledged.cancel()

    task = asyncio.create_task(_drive())
    handler.track_background_task(task)
    await acknowledged  # raises the guard's RPCError, or returns once acked
    return None


### end tier-b:compact

### begin tier-b:get_last_assistant_text
#: B6 (docs/RPC-TIER-B.md §3 table): read-only, no D-1 guard — `session
#: .messages` (already `EXPOSED["messages"] = "get_messages"`;
#: `test_rpc_capability_audit.py`'s Tier B region for this verb is left
#: EMPTY on purpose, per that file's own comment: no NEW AgentSession/
#: AgentSessionRuntime method is reached here) is the only surface this
#: handler touches — there is no `AgentSession.get_last_assistant_text`
#: to call (§1 ground-truth table: "No method").
GET_LAST_ASSISTANT_TEXT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": ["string", "null"],
            "description": (
                "The last assistant message's concatenated 'text' content "
                "blocks, trimmed. null if no qualifying assistant message "
                "exists YET, or if one exists but it has no text (e.g. a "
                "pure tool-call turn) — the two cases are DELIBERATELY "
                "indistinguishable on the wire, matching pi's own "
                "`getLastAssistantText(): string | undefined` (pi "
                "agent-session.ts:3092) and its RPC verb "
                '(rpc-mode.ts:609-612, docs/rpc.md: \'Returns {"text": '
                "null} if no assistant messages exist' — silent on the "
                "second null-producing case, because on the wire there is "
                "only one representable 'nothing' and pi does not either)."
            ),
        },
    },
    "required": ["text"],
}


def _last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    """B6's whole contract, ported field-for-field from pi's
    `AgentSession.getLastAssistantText()` (agent-session.ts:3092-3113) onto
    τ's dict-shaped `session.messages` (`role`/`content`/`stop_reason` keys —
    snake_case, per every other Tier A dict already on this table; pi's
    `stopReason` is the only spelling that differs).

    Two decisions this function is the answer to (unit brief): what a
    caller with no assistant message yet sees, and what "text" means when
    the last assistant message carries tool calls or thinking blocks
    alongside — or instead of — text.

    1. **Search order and the one message that is skipped.** Walk
       `messages` from the END, and return on the first `role == "assistant"`
       entry — EXCEPT one still-aborted-with-nothing-said turn: `stop_reason
       == "aborted"` AND an empty `content` list. That combination is a
       turn that was cut off before the model produced a single block (a
       `stop_reason == "aborted"` message WITH content — e.g. abort mid
       -stream after some text landed — is not skipped; its text still
       counts, exactly as pi's own `msg.content.length === 0` check reads).
       Skipping it means abort-and-retry does not erase visibility into
       the last REAL answer.
    2. **What counts as "text".** Only `type == "text"` content blocks,
       concatenated in order with no separator (pi: `text += content.text`
       inside the same `for` loop that silently passes over `"thinking"`
       and `"toolCall"` blocks by never matching their type) — a
       thinking block is not this session's ANSWER, and a bare tool-call
       block has no `text` field to contribute. `.strip()` at the end (pi:
       `.trim()`), and an empty result after stripping returns `None`, not
       `""` — pi's `text.trim() || undefined`, so a pure-tool-call
       assistant turn (real message, zero text) reads identically to "no
       assistant message at all." Documented here, not hidden: a host
       that needs to tell those two apart cannot do it from this verb
       alone and must additionally consult `get_messages`.

    Never raises: an empty or assistant-less `messages` list is the
    ordinary "fresh session" case, not a Fail-Early violation — there is
    nothing malformed about a session with no assistant turn yet.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        if message.get("stop_reason") == "aborted" and not message.get("content"):
            continue
        text = "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        text = text.strip()
        return text or None
    return None


@command(
    "get_last_assistant_text",
    tier="B",
    since="tier-b",
    notes=(
        "Derived from AgentSession.messages (already the get_messages verb's "
        "surface) — there is no AgentSession.get_last_assistant_text to call "
        "(docs/RPC-TIER-B.md §1: 'No method. Trivially derived'). Read-only, "
        "no D-1 turn_safety_guard (nothing here mutates session state). "
        "Ports pi's AgentSession.getLastAssistantText() (agent-session.ts:3092) "
        "verb-for-verb: 'text' is the last qualifying assistant message's "
        "'text'-type content blocks concatenated and trimmed — thinking and "
        "toolCall blocks are skipped, never concatenated in; an assistant "
        "message that is itself stop_reason='aborted' with empty content is "
        "skipped as though it never happened, so an aborted-before-anything "
        'turn does not hide the last real answer. Returns {"text": null} '
        "both when no assistant message exists yet AND when the last one has "
        "no text (a pure tool-call turn) — pi does not distinguish these "
        "either (docs/rpc.md only documents the first case); a host that "
        "needs to tell them apart must additionally call get_messages. "
        "No `cursor`: E5 binds mutators, and this is a read (commands.py "
        "'E5 in Tier B', rule 2 — a host that wants the tip calls get_state). "
        "D-7 (commands.py 'DURABILITY in Tier B', rule 2): appends nothing, "
        "so no require_durable_session — this answers the same on a "
        "persisted and an unpersisted session."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_LAST_ASSISTANT_TEXT_RESULT_SCHEMA,
)
async def _handle_get_last_assistant_text(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return {"text": _last_assistant_text(handler.session.messages)}


### end tier-b:get_last_assistant_text

### begin tier-b:get_models
#: Finding 7 of the Tier B review: `set_model` takes a config model NAME and
#: NOTHING on this table enumerated them — `get_state` and
#: `get_session_stats` publish only the ACTIVE model's `{id, provider,
#: context_window}` — so a host's only route to a name `set_model` would
#: accept was reading the child's `~/.tau/config.json` out of band. That
#: defeats G1 (docs/REMOTE-CONTROL.md: "a second implementation should be
#: possible from this document plus the generated reference"), and it is
#: doubly awkward because `cycle_model`'s own `decline()` reason justifies
#: refusing that verb on the grounds that NAMING a model is the supported
#: path. This verb is that path's missing half.
#:
#: One level of `properties`, prose for the rest — `_assert_supported_schema`
#: walks exactly one level and has no `items` vocabulary, so the element
#: shape is DESCRIBED rather than declared, the same choice
#: `GET_TOOLS_RESULT_SCHEMA`/`GET_COMMANDS_RESULT_SCHEMA` already make for
#: their own arrays (pretending to check a second level would check nothing,
#: silently).
GET_MODELS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "description": (
                "Every config model NAME this child can switch to, sorted, as "
                "[{name, model}]: `name` is the exact string set_model's "
                "`name` param takes, and `model` is the SAME projection "
                "get_state publishes for the active model — {id, provider, "
                "context_window} — obtained by resolving `name` through the "
                "session's bound model resolver, i.e. by asking the one "
                "component set_model itself would ask. Empty only when the "
                "child's config declares no models; a resolver that cannot "
                "be enumerated is an INTERNAL_ERROR, never an empty list."
            ),
        },
    },
    "required": ["models"],
}

#: What the bound model resolver must expose for this verb to answer, and the
#: whole of the coupling between it and `tau_coding_agent.backends`
#: (`ConfigModelResolver.model_names`). Probed by name — `AgentSession
#: ._model_resolver` is typed `Callable[[str], Model] | None`, so the method
#: is invisible statically — exactly as `set_model` probes
#: `append_model_change`, a method deliberately off the `SessionLog`
#: Protocol (§1.1). Same asymmetry `_DURABLE_LOCATION_ATTRS` documents:
#: unknown means REFUSE, never "assume there are no models".
_MODEL_CATALOG_ATTR = "model_names"


@command(
    "get_models",
    tier="B",
    since="tier-b",
    notes=(
        "Finding 7 of the Tier B review: set_model takes a config model NAME "
        "(a key in ~/.tau/config.json's 'models' map) and nothing on this "
        "table enumerated them, so a host could only learn a valid name by "
        "reading the child's config file out of band — which defeats G1 ('a "
        "second implementation should be possible from this document plus "
        "the generated reference'). Returns `models`: every name the "
        "session's bound resolver knows, sorted, each with the SAME {id, "
        "provider, context_window} projection get_state publishes for the "
        "active model. Each entry's `model` is produced by RESOLVING that "
        "name through the bound resolver — the component set_model itself "
        "calls (AgentSession.set_model -> set_model_resolver) — so what this "
        "verb advertises is what a set_model on that name would actually "
        "install, not a second reading of the config. "
        "Read-only: no D-1 turn_safety_guard (nothing here mutates session "
        "state; a turn may be in flight and this still answers) and no "
        "`cursor` (E5 binds mutators — commands.py 'E5 in Tier B', rule 2; a "
        "host that wants the tip calls get_state), and no "
        "require_durable_session (D-7, commands.py 'DURABILITY in Tier B', "
        "rule 2: it appends nothing, so it answers the same on an "
        "unpersisted session — even though the set_model it exists to serve "
        "would then refuse there). "
        "Refuses, rather than reporting an empty catalogue, when the session "
        "has NO resolver bound at all, or a resolver that cannot enumerate "
        "(RuntimeError -> INTERNAL_ERROR): 'this child has no configured "
        "models' and 'nobody can answer that here' are different facts, and "
        "an empty list for the second is the silent-fallback shape Fail-"
        "Early forbids — a host would read it as 'set_model has no valid "
        "argument' and stop. A config entry that does not BUILD (e.g. an "
        "invalid `reasoning_replay`, backends.build_model_from_config's own "
        "ValueError) fails this verb naming that entry, rather than dropping "
        "it: a silently shorter list is a list a host would trust. "
        "KNOWN GAPS, stated not hidden. (1) The ACTIVE model need not appear "
        "here, and this verb does not flag which entry is active: a startup "
        "`--model provider/id` is an ad-hoc model with no config key "
        "(headless.resolve_model_config), so set_model cannot switch back to "
        "it either, and two config names may alias one model id — guessing "
        "'active' by matching get_state's id would be a fabrication where "
        "the aliases differ. A host reads get_state for what is running and "
        "this for what it may ask for. (2) `context_window` is whatever the "
        "resolver returns; today backends.build_model_from_config assigns "
        "every config entry the same 128000, including the one get_state "
        "reports, so a host must not read a difference into these numbers "
        "that the child does not currently make. (3) Nothing here is a "
        "capability probe: a name resolving does not mean the endpoint is "
        "reachable or the api key is right — set_model's own notes record "
        "that a cross-provider switch surfaces a provider auth error on the "
        "next turn."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_MODELS_RESULT_SCHEMA,
)
async def _handle_get_models(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    # `_model_resolver` (private) rather than a new public AgentSession
    # accessor, the same idiom `get_tools` uses for `session._tools` and
    # `get_session_stats` for `session._compaction_settings` — and the reason
    # this verb's EXPOSED/NOT_EXPOSED regions in test_rpc_capability_audit.py
    # are empty: no public AgentSession member becomes newly reachable.
    resolver = session._model_resolver
    if resolver is None:
        raise RuntimeError(
            "get_models: no model resolver is bound to this AgentSession, so there is "
            "no set of names to enumerate — the frontend binds one at startup "
            "(set_model_resolver, a closure over config 'models'; rpc_mode.py does "
            "this before RPCHandler.run()). set_model would raise here too."
        )
    model_names = getattr(resolver, _MODEL_CATALOG_ATTR, None)
    if model_names is None:
        raise RuntimeError(
            f"get_models: the bound model resolver ({type(resolver).__name__}) does not "
            f"declare {_MODEL_CATALOG_ATTR}(), so the names it accepts cannot be listed "
            "— refusing rather than answering with an empty catalogue a host would read "
            "as 'set_model has no valid argument' (backends.ConfigModelResolver is the "
            "resolver every shipped frontend binds)"
        )
    listed: list[dict[str, Any]] = []
    for name in model_names():
        try:
            model = resolver(name)
        except (KeyError, ValueError) as exc:
            # The resolver's own prose, verbatim (see _resolver_error_message —
            # set_model's region — for why KeyError needs unwrapping). Not
            # skipped: a config entry this child cannot build is a fact the
            # host asking "what may I switch to?" most needs, and a list that
            # quietly omitted it would be trusted as complete.
            raise RuntimeError(
                f"get_models: config model {name!r} does not build: {_resolver_error_message(exc)}"
            ) from exc
        listed.append(
            {
                "name": name,
                # AgentSession.get_model()'s projection, field for field
                # (agent_session.py) — the shape get_state already publishes,
                # so a host compares the two directly.
                "model": {
                    "id": model.id,
                    "provider": model.provider,
                    "context_window": model.context_window,
                },
            }
        )
    return {"models": listed}


### end tier-b:get_models

### begin tier-b:get_session_stats
#: D-3: hand-written, reviewed (§6 A3) — same discipline as every other
#: result schema in this module. Nested objects (`context`,
#: `compaction_settings`, `last_compaction`) are typed `"object"` with a
#: prose description rather than their own `properties`, matching
#: `GET_STATE_RESULT_SCHEMA`'s `model`/`usage` fields above — this
#: module's hand-rolled validator (`_assert_supported_schema`) only walks
#: one level of `properties`, so a second level would validate nothing
#: silently; the house answer is to describe it in prose instead of
#: pretending to check it.
GET_SESSION_STATS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "context": {
            "type": "object",
            "description": (
                "estimate_context_tokens(session.messages) (compaction.py) "
                "projected as {tokens, usage_tokens, trailing_tokens, "
                "last_usage_index}: tokens is the total estimate the "
                "compaction threshold is checked against; usage_tokens is "
                "the anchored provider-reported count up to the last "
                "assistant Usage, trailing_tokens the heuristic estimate "
                "for messages after it, last_usage_index that message's "
                "index (null if no assistant Usage exists yet, in which "
                "case tokens==trailing_tokens and the whole list was "
                "heuristically estimated)."
            ),
        },
        "context_window": {
            "type": "integer",
            "description": "The active model's context_window (get_model()).",
        },
        "context_headroom": {
            "type": "integer",
            "description": (
                "context_window - context.tokens. Can be negative: an "
                "honest over-budget number, never clamped to zero."
            ),
        },
        "compaction_settings": {
            "type": "object",
            "description": (
                "The session's EFFECTIVE CompactionSettings — {enabled, "
                "reserve_tokens, keep_recent_tokens}. No AgentSession "
                "accessor exists for this (§1.1's ground truth), so this "
                "reads session._compaction_settings directly, the same "
                "precedent get_tools already sets for _tools. An RPC "
                "session is CONSTRUCTED with enabled=False (backends.py:885) "
                "— that is how a host discovers auto-compaction is off "
                "(§1.1) — and set_auto_compaction (D-4, shipped in this same "
                "tier) is the one thing that changes it, so this reports the "
                "session's LIVE effective setting at call time, never a "
                "constant."
            ),
        },
        "last_compaction": {
            "type": ["object", "null"],
            "description": (
                "{id, timestamp, summary, first_kept_id, tokens_before} for "
                "the most recent type=='compaction' entry in "
                "session_log.entries(), or null if this session has never "
                "compacted — an honest absence, never a fabricated entry."
            ),
        },
        "usage": {
            "type": ["object", "null"],
            "description": "AgentSession.get_usage() — null before the first completion.",
        },
    },
    "required": [
        "context",
        "context_window",
        "context_headroom",
        "compaction_settings",
        "last_compaction",
        "usage",
    ],
}


def _last_compaction_state(session: "AgentSession") -> dict[str, Any] | None:
    """The newest `type=="compaction"` entry in `session.session_log.entries()`,
    projected to the wire shape, or `None` if the session has never compacted.

    Scans the log's own append order (`entries()` is "in load order" —
    `session_log.py`), NOT the `ConversationTree` active path — cheaper, and
    today's RPC surface never opens a second lane (Tier C's `open_lane` is
    unbuilt), so there is no branch whose own compaction this could
    misattribute to the primary session. Stated as a scope note, not hidden:
    a future lane-aware caller would need `ConversationTree.context_entries()`
    instead of this reversed linear scan.
    """
    for entry in reversed(session.session_log.entries()):
        if entry.get("type") == "compaction":
            return {
                "id": entry.get("id"),
                "timestamp": entry.get("timestamp"),
                "summary": entry.get("summary"),
                "first_kept_id": entry.get("firstKeptId"),
                "tokens_before": entry.get("tokensBefore"),
            }
    return None


@command(
    "get_session_stats",
    tier="B",
    since="tier-b",
    notes=(
        "D-3: get_state already returns usage/message_count/cursor, so this "
        "is not a re-shaping of that — it is the verb a host reads to decide "
        "WHETHER and WHEN to compact. Returns: estimate_context_tokens("
        "session.messages) (compaction.py) as `context`; the model's "
        "context_window and the resulting context_headroom; the EFFECTIVE "
        "CompactionSettings as `compaction_settings` (enabled/reserve_tokens/"
        "keep_recent_tokens — read from session._compaction_settings, since "
        "no AgentSession accessor exists, §1.1's ground truth); the newest "
        "compaction log entry as `last_compaction` (null if none — an "
        "honest absence); and get_usage() as `usage`, for cost. An RPC "
        "session is CONSTRUCTED with compaction_settings=CompactionSettings"
        "(enabled=False) (backends.py:885), so a host that has not changed it "
        "reads compaction_settings.enabled=false here — that is how it "
        "discovers auto-compaction is off (§1.1). That is a starting value, "
        "not a constant this verb may promise: set_auto_compaction (D-4) "
        "shipped in this same tier and flips exactly this field, so what "
        "comes back is the session's LIVE effective setting read at call "
        "time. The verb itself changes nothing. Read-only: no "
        "turn_safety_guard (D-1 — only the MUTATING Tier B verbs take "
        "it) and no `cursor` (E5 binds mutators — commands.py 'E5 in Tier B', "
        "rule 2; a host that wants the tip calls get_state). "
        "Refuses nothing: no params, and no precondition beyond a "
        "constructed session — including no require_durable_session (D-7, "
        "commands.py 'DURABILITY in Tier B', rule 2: it appends nothing). "
        "That matters here specifically: this is the verb a host reads to "
        "decide whether to compact, and on an unpersisted session it still "
        "answers while `compact` itself refuses (D-7 rule 1). Known gap: "
        "`last_compaction` is a scan of the "
        "log's own append order, not the ConversationTree active path — see "
        "_last_compaction_state's docstring."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_SESSION_STATS_RESULT_SCHEMA,
)
async def _handle_get_session_stats(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    # Local import, not module-level (§4 contention map): this file's top
    # import block is shared by all six Tier B worktrees, and a new
    # module-level import line there is exactly the kind of one-line
    # addition that conflicts across parallel edits. This region is the
    # only place B3 writes.
    from tau_agent_core.compaction import estimate_context_tokens

    session = handler.session
    estimate = estimate_context_tokens(session.messages)
    context_window = session.get_model()["context_window"]
    settings = session._compaction_settings
    return {
        "context": {
            "tokens": estimate.tokens,
            "usage_tokens": estimate.usage_tokens,
            "trailing_tokens": estimate.trailing_tokens,
            "last_usage_index": estimate.last_usage_index,
        },
        "context_window": context_window,
        "context_headroom": context_window - estimate.tokens,
        "compaction_settings": {
            "enabled": settings.enabled,
            "reserve_tokens": settings.reserve_tokens,
            "keep_recent_tokens": settings.keep_recent_tokens,
        },
        "last_compaction": _last_compaction_state(session),
        "usage": session.get_usage(),
    }


### end tier-b:get_session_stats

### begin tier-b:list_sessions
#: Finding 8 of the Tier B review: `switch_session` takes "an exact session
#: id, or a unique id prefix" and NOTHING on this table produced one. A host
#: could only ever switch to a session it had created in this process
#: (`new_session`/`fork`) or whose id it had recorded from a previous run's
#: `get_state`; a session made by the TUI, by `tau -p`, or by an earlier RPC
#: child was unreachable. Same defect `get_models` closed one round earlier
#: for `set_model`'s config NAME, in the same shape and for the same reason —
#: G1 (docs/REMOTE-CONTROL.md: "a second implementation should be possible
#: from this document plus the generated reference"), which an out-of-band
#: read of `~/.tau/sessions/<dashed-cwd>/*.jsonl` defeats. It was also absent
#: from the `declined` table, which made it a C1 violation ("every verb τ
#: deliberately does not implement is declined here, with a reason — never
#: silently absent") rather than a deferral.
#:
#: One level of `properties`, prose for the rest — `_assert_supported_schema`
#: walks exactly one level and has no `items` vocabulary, so the element
#: shape is DESCRIBED rather than declared, the same choice
#: `GET_MODELS_RESULT_SCHEMA` and `GET_TOOLS_RESULT_SCHEMA` already make for
#: their own arrays (pretending to check a second level would check nothing,
#: silently).
LIST_SESSIONS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sessions": {
            "type": "array",
            "description": (
                "Every session `switch_session` can resolve from this "
                "connection, newest-modified first, as [{session_id, ref, "
                "name, title, message_count, created, modified, parent, "
                "error}]. `session_id` is the exact string switch_session's "
                "`session_id` param takes. `ref` is the STORE's own handle "
                "for that session (SessionCatalog's listing ref — the file "
                "store's absolute .jsonl path, a JMFTS catalog's document "
                "id): it is what names WHICH universe this listing is, since "
                "--mode rpc's default session base is <tmp>/.tau-<uid>/sessions "
                "and the TUI's is ~/.tau/sessions (D-6/H1b). `name` is what "
                "set_session_name set, null if never named; `title` is the "
                "picker's bounded display label (SessionInfo.display_title) "
                "and is the only place message TEXT appears here — "
                "first_message/last_message are deliberately not published, "
                "being unbounded (a 40kB prompt would ride every listing). "
                "`created`/`modified` are ISO-8601; `parent` is the id this "
                "session was forked from, else null; `error` is why this "
                "row's entries could not be read, else null — an unreadable "
                "session stays LISTED and says so (SessionInfo.error) rather "
                "than vanishing from a host's view."
            ),
        },
        "scope": {
            "type": "object",
            "description": (
                "What universe the list above is, as {store, cwd}: `store` "
                "is the same backend label the session tuple of new_session/"
                "fork/switch_session carries, and `cwd` is the working "
                "directory the listing is scoped to — this process's own, "
                "the identical scope switch_session resolves against "
                "(SessionCatalog.resolve_ref is built on list(cwd)), so the "
                "ids here are exactly the ids that verb accepts. Sessions in "
                "OTHER directories are not listed because switch_session "
                "could not reach them either. The BASE DIRECTORY is not a "
                "field: no SessionCatalog declares one (the file store's is "
                "private and `None` means the default), and each entry's "
                "`ref` names it exactly — stated as a limit, not hidden: an "
                "EMPTY list therefore names no location at all."
            ),
        },
    },
    "required": ["sessions", "scope"],
}


def _listed_session(info: "SessionInfo") -> dict[str, Any]:
    """One `SessionInfo` -> one wire row (`LIST_SESSIONS_RESULT_SCHEMA`).

    A deliberate PROJECTION, not a `dataclasses.asdict`: `first_message` and
    `last_message` are whole message texts, unbounded (the conformance suite
    routinely sends 40kB turns), and a listing that carried two of them per
    row would put an arbitrary multiple of the transcript on the wire every
    time a host asked what it may switch to. `display_title()` is the same
    data bounded to ~50 characters — the projection the TUI's own picker
    renders — and it is the store's answer for an unreadable row too.
    """
    return {
        "session_id": info.id,
        "ref": info.ref,
        "name": info.name,
        "title": info.display_title(),
        "message_count": info.message_count,
        "created": info.created.isoformat(),
        "modified": info.modified.isoformat(),
        "parent": info.parent,
        "error": info.error,
    }


@command(
    "list_sessions",
    tier="B",
    since="tier-b",
    notes=(
        "Finding 8 of the Tier B review: switch_session takes a session id "
        "(exact, or a unique prefix) and nothing on this table produced one, "
        "so a host could only reach a session it had created in this process "
        "— the same G1 hole get_models closed for set_model's config NAME, "
        "and, being absent from `declined` too, a C1 violation rather than a "
        "deferral. Returns `sessions`: every session this connection can "
        "switch to, newest-modified first, and `scope`: {store, cwd}, which "
        "says WHICH universe that is. "
        "The listing and switch_session's resolution are the SAME set by "
        "construction, not by agreement — both are SessionCatalog.list(cwd) "
        "for this process's cwd (resolve_ref is built on it), so an id this "
        "verb returns is an id that verb accepts, and a session it omits is "
        "one switch_session refuses with -32602. That is also this tier's "
        "definition of `addressable` on new_session/fork/switch_session's "
        "session tuple: addressable means listed here. "
        "Scope, stated because unit S (D-6/H1b) made it a real question: "
        "--mode rpc's DEFAULT session base is <tmp>/.tau-<uid>/sessions while the "
        "TUI's and --print's is ~/.tau/sessions, so a host and the human at "
        "the terminal are normally looking at DIFFERENT lists (--session-dir "
        "DIR, accepted under --mode rpc, is how a host joins the user's). "
        "Each row's `ref` — the store's own handle, the file store's "
        "absolute path — is what tells them apart; an RPC child's own "
        "startup session is always one of these rows, so `get_state`'s "
        "session_id finds it and its ref names the base. "
        "Read-only: no D-1 turn_safety_guard (nothing here mutates session "
        "state; a turn may be in flight and this still answers), no `cursor` "
        "(E5 binds mutators — commands.py 'E5 in Tier B', rule 2; a host "
        "that wants the tip calls get_state), and no "
        "require_durable_session (D-7, commands.py 'DURABILITY in Tier B', "
        "rule 2: it appends nothing, so it answers the same on an "
        "unpersisted session — where the answer is precisely that the "
        "current session is NOT among the rows). "
        "KNOWN GAPS, stated not hidden. (1) It does not flag which row is "
        "the session this connection is on, for the reason get_models does "
        "not flag the active model: the host already has that from "
        "get_state's `session_id`, and here the comparison is exact rather "
        "than a guess. (2) No `cwd` parameter, and no way to widen the scope "
        "to every directory: switch_session resolves against THIS cwd only, "
        "so a wider list would advertise ids it would then refuse. (3) A row "
        "is metadata, never a promise that loading succeeds — `error` "
        "non-null says the entries could not be read, and switch_session on "
        "that id will raise the store's real reason rather than silently "
        "loading an empty conversation."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=LIST_SESSIONS_RESULT_SCHEMA,
)
async def _handle_list_sessions(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    # The runtime's catalog and cwd, read the same private-attribute way
    # `get_models` reads `session._model_resolver` and `get_tools` reads
    # `session._tools` — and the reason this verb's EXPOSED/NOT_EXPOSED
    # regions in test_rpc_capability_audit.py are empty: no PUBLIC
    # AgentSession/AgentSessionRuntime member becomes newly reachable.
    #
    # No presence probe here, unlike `get_models`' `_MODEL_CATALOG_ATTR`:
    # `list` is one of the five abstract methods of the `SessionCatalog` ABC,
    # so a catalog without it cannot be instantiated at all. There is nothing
    # for a probe to discover that construction has not already refused.
    runtime = _require_runtime(handler)
    catalog: SessionCatalog = runtime._catalog
    cwd: str = runtime._cwd
    return {
        "sessions": [_listed_session(info) for info in catalog.list(cwd)],
        "scope": {"store": runtime._store, "cwd": cwd},
    }


### end tier-b:list_sessions

### begin tier-b:set_auto_compaction
SET_AUTO_COMPACTION_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": (
                "Desired auto-compaction state. RPC sessions are constructed "
                "with CompactionSettings(enabled=False) (backends.py:885, "
                "RPC-TIER-B.md §1.1) — this verb is the only route to turning "
                "it on for a session reached over the wire."
            ),
        },
    },
    "required": ["enabled"],
    "additionalProperties": False,
}

SET_AUTO_COMPACTION_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": (
                "The effective state after this call (D-4: 'a plain, "
                "idempotent setter ... returns the effective state') — read "
                "back off session._compaction_settings.enabled, never an "
                "echo of the request."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor after this call (E5, rule 1 of 'E5 in "
                "Tier B' above). ALWAYS the unchanged tip: this verb mutates "
                "an in-memory CompactionSettings and appends no log entry, so "
                "there is nothing here that could move it. Returned rather "
                "than omitted because absence is not a signal (rule 3) — a "
                "host reads the same field from every mutator and never has "
                "to infer the tip from a missing key (F3)."
            ),
        },
    },
    "required": ["enabled", "cursor"],
}


@command(
    "set_auto_compaction",
    tier="B",
    since="tier-b",
    notes=(
        "D-4: a plain, idempotent setter over "
        "`AgentSession._compaction_settings.enabled` — a direct field "
        "mutation, not a method call, because none exists (§1 ground truth: "
        "'No accessor. AgentSession._compaction_settings is a mutable "
        "CompactionSettings(enabled, reserve_tokens, keep_recent_tokens)'). "
        "Same private-attribute-from-commands.py idiom `get_tools` already "
        "uses for `session._tools` — reached directly rather than adding a "
        "new AgentSession method for one caller (no EXPOSED/NOT_EXPOSED move: "
        "`_compaction_settings` is not a public member, so R-T2's audit never "
        "sees it either way). "
        "D-1: takes turn_safety_guard before mutating, so this never races a "
        "turn's own read of the same settings object; TURN_STILL_RUNNING on "
        "a bounded timeout, same as set_model/compact/set_session_name. "
        "E5, answered the one way the whole tier answers it (see commands.py "
        "'E5 in Tier B'): this response carries `cursor`, and for this verb "
        "it is ALWAYS the unchanged tip — the mutation is an in-memory "
        "CompactionSettings field, not a log entry. It is returned rather "
        "than omitted because a missing key is not a way to say 'nothing "
        "moved': that would be the tip-inference F3 forbids, and it would "
        "make one tier answer E5 two ways. "
        "D-7, the same one-way answer for durability (commands.py "
        "'DURABILITY in Tier B', rule 2): this verb appends NOTHING, so it "
        "takes no require_durable_session and answers on an unpersisted "
        "session — where compact/set_model/set_session_name all refuse "
        "(rule 1), because those three do append. Read the `cursor` above "
        "accordingly: on any session it is the live tip, never a claim that "
        "this call wrote something. "
        "No policy guard (§1.2): CompactionPolicy is constructed in exactly "
        "one place, sdk.py:865, which rpc_mode.py never goes through — no "
        "RPC session ever carries one for this verb to protect. "
        "This is the ONLY route to a capability RPC mode otherwise cannot "
        "reach at all: rpc_mode.py -> backends.create_backend -> TauBackend "
        "constructs its session with CompactionSettings(enabled=False) "
        "(backends.py:885, §1.1) and nothing else in RPC mode flips it. "
        "KNOWN GAP, stated not hidden (D-4): enabling this can cause "
        "`_maybe_auto_compact` (agent_session.py:3286-3291) to fire on the "
        "NEXT turn, and that method emits its own `agent_start`/`agent_end` "
        "pair through `self._events.emit` directly, not `_emit_stamped` — so "
        "that pair carries NO `submission_id`. A host correlating events to "
        "the `submission_id` a prior `submit`/`prompt` returned will see an "
        "ORPHAN agent_start/agent_end it cannot attribute to any request it "
        "made. The `agent_end` DOES carry a `cursor` (the handler stamps "
        "every outbound `agent_end` at DEQUEUE, in `prepare_outbound` / "
        "`_stamp_agent_end_cursor`, regardless of provenance), so a host "
        "obeying F3 (never cache 'the tip') stays correct across a "
        "compaction it did not explicitly ask for, even though it cannot "
        "explain WHY its context just shrank from submission_id alone. "
        "SECOND KNOWN GAP, the other face of D-7 rule 2: enabling this on an "
        "UNPERSISTED session arms a mechanism that then appends `compaction` "
        "entries to a log that dies with the process — from inside "
        "`_maybe_auto_compact`, a code path with no RPC verb on it and so "
        "nothing for rule 1 to guard. Same class as the AgentSession-"
        "internal gap compact's notes record about turn_lock: this tier "
        "guards the wire, not AgentSession. An auto-compaction is also NOT "
        "reachable by `abort` for the same reason — there is no background "
        "task the RPC layer owns to cancel (finding 5)."
    ),
    params_schema=SET_AUTO_COMPACTION_PARAMS_SCHEMA,
    result_schema=SET_AUTO_COMPACTION_RESULT_SCHEMA,
)
async def _handle_set_auto_compaction(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    async with turn_safety_guard(session):
        session._compaction_settings.enabled = bool(params["enabled"])
        effective = session._compaction_settings.enabled
        # Read while the guard is STILL HELD, so the tip reported is the tip
        # as of the moment the setting took effect rather than one re-read
        # after the lock was handed to a turn that may have appended since.
        # E5 (rule 1) governs PRESENCE, not this; where a mutator reads is
        # still per-verb across the tier — `set_model` reads under its guard
        # too, while `set_session_name` and `compact` build their completions
        # after releasing it (`compact` necessarily: its payload is assembled
        # in the background task once compact() has returned). Nothing here
        # forces this verb to take the looser reading, so it does not.
        cursor = session.session_log.cursor
    return {"enabled": effective, "cursor": cursor}


### end tier-b:set_auto_compaction

### begin tier-b:set_model

#: docs/RPC-TIER-B.md §3 "B1 | set_model": switch the active model by NAME
#: (`AgentSession.set_model`) and, unlike that bare session method, PERSIST
#: the change (D-2). `name` is a config model NAME — a key in
#: `~/.tau/config.json`'s `models` map, resolved through the session's bound
#: `set_model_resolver` — the same string `--model NAME` accepts headlessly.
#: Not a model id: a config key may alias one (`backends.resolve_model_config`).
SET_MODEL_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "A config model NAME (a key in ~/.tau/config.json's 'models' "
                "map), resolved through AgentSession's bound model resolver "
                "(set_model_resolver) — the same name --model NAME accepts "
                "headlessly. NOT a model id; a config key may alias one."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

SET_MODEL_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "object",
            "description": "AgentSession.get_model() after the switch: {id, provider, context_window}.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor immediately after the model_change entry "
                "this call appended (E5) — that entry's own id, since the "
                "append is the last write this handler makes."
            ),
        },
    },
    "required": ["model", "cursor"],
}


def _resolver_error_message(exc: KeyError | ValueError) -> str:
    """The model resolver's own prose, rendered for the wire without
    `KeyError.__str__`'s quotes (finding 10 of the Tier B review).

    `KeyError.__str__` is `repr(args[0])`, not the message — so a resolver
    that raises `KeyError("unknown model 'nope'; configured models: fake,
    fake-alt")` (`backends.make_model_resolver`, backends.py:631) reached the
    wire as `"unknown model 'nope'; configured models: fake, fake-alt"`,
    quotes included, inside a JSON string that quotes it again. Unwrapping
    the single argument is the ONLY difference: nothing is reworded,
    truncated or replaced, because the resolver is the component that knows
    which names exist and this layer must not paraphrase it.

    `ValueError` — `set_model`'s other documented "no such name" shape — has
    a plain `__str__` and passes through untouched. A `KeyError` carrying
    anything other than exactly one argument has no single message to
    unwrap: `KeyError.__str__` is then the args tuple's own repr, which IS
    the whole of what the raiser said, so `str(exc)` is the honest rendering
    rather than a fallback covering a case this function declined to handle.
    """
    if isinstance(exc, KeyError) and len(exc.args) == 1:
        return str(exc.args[0])
    return str(exc)


@command(
    "set_model",
    tier="B",
    since="tier-b",
    notes=(
        "D-2: switches the active model by NAME (AgentSession.set_model, "
        "agent_session.py:785 — effective on the NEXT turn, never mid-"
        "stream) and, unlike the bare session method, PERSISTS the switch: "
        "appends a model_change entry and returns the resulting cursor (E5, "
        "answered the one way the whole tier answers it — see commands.py "
        "'E5 in Tier B': every Tier B mutator's completion carries `cursor`, "
        "present even when the call moved nothing; only the tier's reads omit "
        "it. Here the append always moves it, so it is that entry's own id). "
        "D-1: guarded by turn_safety_guard, so this refuses with "
        "TURN_STILL_RUNNING rather than racing an in-flight turn's own "
        "AgentLoop, which reads self._model when it rebuilds each turn. "
        "Refuses: an unknown `name` is a CALLER error, not a runtime "
        "failure — the bound resolver's KeyError/ValueError (both are "
        "AgentSession.set_model's own documented shapes for 'no such name') "
        "is converted to INVALID_PARAMS, the same classification "
        "switch_session already gives an unresolvable session_id: a value "
        "the schema cannot check syntactically, refused before anything is "
        "touched. The resolver's own message (it is the component that knows "
        "which names exist) reaches the host verbatim, unwrapped from "
        "KeyError.__str__'s repr quotes rather than paraphrased — see "
        "_resolver_error_message. An UNPERSISTED session (new_session "
        "{persist:false}) is "
        "refused too, before anything is touched — require_durable_session, "
        "Blocker 2 of the Tier B review — because a cursor returned for an "
        "append that lands only in memory is a durability promise this verb "
        "cannot keep; SESSION_NOT_PERSISTED, which is also what a log declaring "
        "no durable location at all gets (the SDK's InMemorySessionLog). A "
        "log MISSING append_model_change entirely is the different, blunter "
        "failure it always was — require_log_appender, §1.1, RuntimeError -> "
        "INTERNAL_ERROR — because that is a store wired wrong, not a session "
        "the host can move off. That "
        "refusal is D-7 rule 1, stated once for the whole tier in "
        "commands.py's 'DURABILITY in Tier B' block: a verb that APPENDS "
        "refuses an unpersisted session — this one, set_session_name, and "
        "(since finding 6) compact, which used to run there and report a "
        "cursor for an entry that died with the process. Both "
        "checks run BEFORE session.set_model(name), so a refusal leaves the "
        "in-process model unswitched: this verb never reports 'maybe "
        "switched, definitely not persisted'. Known gap (D-2, stated not "
        "hidden): the append happens HERE, in the RPC verb, not inside "
        "AgentSession.set_model itself — widening that method is out of "
        "this phase's scope, since it is also the TUI's own call path — so "
        "a TUI model switch still does NOT persist a model_change entry; "
        "only a switch made through this RPC verb does. WHERE the entry "
        "lands, and for how long (unit S): a --mode rpc process defaults to "
        "storing its sessions under a private <tmp>/.tau-<uid>/sessions, NOT the "
        "user's ~/.tau/sessions — one 0-message session per spawn would "
        "otherwise take over `tau -c` for whoever is working in the same "
        "directory. Most systems clear the temp dir on reboot, so this "
        "cursor's durability is bounded by MACHINE UPTIME, not forever: a "
        "replay can find the entry for the life of the session, and a host "
        "that needs more must be started with --session-dir DIR (accepted "
        "under --mode rpc precisely so a host can choose, including "
        "--session-dir ~/.tau/sessions)."
    ),
    params_schema=SET_MODEL_PARAMS_SCHEMA,
    result_schema=SET_MODEL_RESULT_SCHEMA,
)
async def _handle_set_model(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    name = params["name"]
    async with turn_safety_guard(session):
        # Both persistence preconditions run FIRST, while nothing has been
        # mutated (Blocker 2, Tier B review). D-2 fixes the order of the
        # APPEND (after the switch succeeds), not of the checks — and
        # checking first is what makes this verb's refusals total: a host
        # that gets an error knows the model did not change, instead of
        # having to re-read get_state to find out.
        require_durable_session(session, verb="set_model")
        require_log_appender(session, "append_model_change", verb="set_model")
        try:
            model = session.set_model(name)
        except (KeyError, ValueError) as exc:
            # AgentSession.set_model's own docstring names both as the
            # resolver's documented shapes for "no such name" — never
            # swallowed, converted to the wire's caller-error code instead
            # (same move switch_session makes for LookupError on a bad
            # session_id).
            raise RPCError(
                INVALID_PARAMS, _resolver_error_message(exc), data={"name": name}
            ) from exc
        # `append_model_change` is deliberately off the `SessionLog` Protocol
        # (§1.1) — `session.session_log` is typed as `SessionLog`, which has
        # no such attribute statically. `getattr` rather than a bespoke
        # `# type: ignore[attr-defined]`, matching
        # `ExtensionContext.set_session_name`'s own `log.append_session_info`
        # call site (extension_types.py:2237) for exactly the same reason.
        getattr(session.session_log, "append_model_change")(name, model["provider"])
        return {"model": model, "cursor": session.session_log.cursor}


### end tier-b:set_model

### begin tier-b:set_session_name
#: `set_session_name` params (docs/RPC-TIER-B.md B5). `name` has no
#: `minLength` check here — this module's hand-rolled `validate_params`
#: implements only `_SUPPORTED_VALUE_KEYWORDS` (no `minLength`), so an empty
#: string reaches the handler and is caught there as INVALID_PARAMS instead
#: (mirrors `switch_session`'s "a bad id is a caller mistake the schema
#: cannot catch syntactically" — same shape, different field).
SET_SESSION_NAME_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The session's new durable display name. Must be non-empty.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

#: E5: every mutating response returns the resulting cursor, alongside the
#: `name` that was just persisted (an echo, not a re-read — the write and the
#: read the wire result reports are the SAME call, so there is no staleness
#: window between them to close).
SET_SESSION_NAME_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The name just persisted (echoes params.name).",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "The resulting session_log.cursor (E5/F3 — every mutating "
                "response returns the resulting cursor)."
            ),
        },
    },
    "required": ["name", "cursor"],
}

#: `get_session_name` is a READ (docs/RPC-TIER-B.md B5: "the read does not"
#: carry a cursor) — no `cursor` field, unlike the write's result above.
GET_SESSION_NAME_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": ["string", "null"],
            "description": (
                "The session's durable display name, or null if never set "
                "(extension_types.read_session_name)."
            ),
        },
    },
    "required": ["name"],
}


@command(
    "set_session_name",
    tier="B",
    since="tier-b",
    notes=(
        "D-1 (mutating): takes turn_safety_guard before writing. Reuses "
        "extension_types.apply_session_name — the SAME body "
        "ExtensionAPI.set_session_name calls (docs/RPC-TIER-B.md B5: 'do "
        "not reinvent it and do not copy-paste it'), which itself performs "
        "§1.1's raise ('the bound log must have append_session_info, else "
        "raise') — so this handler does NOT also call "
        "require_log_appender: that would check the identical fact twice. "
        "require_log_appender (B0) is for a verb with no pre-existing "
        "extension-API body to reuse, e.g. set_model. It DOES take "
        "require_durable_session first (Blocker 2, Tier B review), which "
        "asks a different question — not 'does the log have the appender' "
        "(every real session does) but 'will the entry outlive this "
        "process': an unpersisted session (new_session {persist:false}) is "
        "refused rather than handed a cursor for a rename nobody will ever "
        "read back. That is D-7 rule 1, which commands.py's 'DURABILITY in "
        "Tier B' block now states once for the whole tier — this verb "
        "appends, so it refuses; `compact` appends too and, since finding 6, "
        "gives the same answer instead of a third one. E5, answered the one "
        "way the whole tier answers it (see "
        "commands.py 'E5 in Tier B'): this response carries the resulting "
        "`cursor`, as every Tier B mutator's completion does, present even "
        "when the call moved nothing — here the append always moves it. "
        "An empty name is INVALID_PARAMS (validate_params has no "
        "minLength — see the params schema's own note); an unpersisted "
        "session, or a log declaring no durable location (e.g. the SDK's "
        "InMemorySessionLog), is SESSION_NOT_PERSISTED — round-3 finding 4 "
        "of the Tier B review moved it off INTERNAL_ERROR, which the "
        "generated reference defines as 'the handler raised something it did "
        "not raise on purpose' and which this refusal is the opposite of. A "
        "log MISSING append_session_info altogether still surfaces as "
        "INTERNAL_ERROR (require_log_appender): a store wired wrong is not a "
        "session the host can move off. "
        "Nothing is mutated before either check. Known gap: unlike "
        "set_model (D-2), there is no TUI "
        "path this duplicates or diverges from — pi's setSessionName has no "
        "τ TUI verb yet either, so this is RPC's only door onto "
        "append_session_info today. WHERE the rename lands, and for how long "
        "(unit S): a --mode rpc process defaults to storing its sessions "
        "under a private <tmp>/.tau-<uid>/sessions, NOT the user's "
        "~/.tau/sessions — so a name set here does not show up in that "
        "user's TUI picker unless the host was started with --session-dir "
        "(accepted under --mode rpc precisely so a host can choose, "
        "including --session-dir ~/.tau/sessions). Most systems clear the "
        "temp dir on reboot, so this cursor's durability is bounded by "
        "MACHINE UPTIME, not forever."
    ),
    params_schema=SET_SESSION_NAME_PARAMS_SCHEMA,
    result_schema=SET_SESSION_NAME_RESULT_SCHEMA,
)
async def _handle_set_session_name(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.extension_types import apply_session_name

    session = handler.session
    name = params["name"]
    try:
        async with turn_safety_guard(session):
            # Blocker 2 (Tier B review): the durability question
            # apply_session_name's own §1.1 raise does NOT answer — it checks
            # the appender exists, which an unpersisted session's does.
            require_durable_session(session, verb="set_session_name")
            apply_session_name(session, name)
    except ValueError as exc:
        raise RPCError(INVALID_PARAMS, str(exc), data={"name": name}) from exc
    return {"name": name, "cursor": session.session_log.cursor}


@command(
    "get_session_name",
    tier="B",
    since="tier-b",
    notes=(
        "Read-only (docs/RPC-TIER-B.md B5: 'the read does not' take D-1's "
        "guard or carry a cursor). Reuses extension_types.read_session_name "
        "— the SAME body ExtensionAPI.get_session_name calls. A session "
        "log with no durable name to read (e.g. the SDK's "
        "InMemorySessionLog) raises RuntimeError, uncaught here, surfacing "
        "as INTERNAL_ERROR: this is a READ, so it never takes D-7's guard "
        "and never earns SESSION_NOT_PERSISTED — a log that cannot even be "
        "asked is a store wired wrong. Never set is NOT that case: it "
        "returns {name: null}, same as read_session_name's own None. "
        "No `cursor`: E5 binds mutators, and this is a read (commands.py "
        "'E5 in Tier B', rule 2 — a host that wants the tip calls get_state). "
        "No require_durable_session either (D-7, commands.py 'DURABILITY in "
        "Tier B', rule 2): it appends nothing, so it reads a name back on an "
        "unpersisted session even though set_session_name refuses to write "
        "one there."
    ),
    params_schema=NO_PARAMS_SCHEMA,
    result_schema=GET_SESSION_NAME_RESULT_SCHEMA,
)
async def _handle_get_session_name(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.extension_types import read_session_name

    return {"name": read_session_name(handler.session)}


### end tier-b:set_session_name


# ─────────────────────────────────────────────────────────────────────────
# Declined.
# ─────────────────────────────────────────────────────────────────────────

decline(
    "send_tool_result",
    tier="D",
    since="2A",
    notes=(
        "Replaces the old `_handle_send_tool_result` stub, which returned "
        "{'status': 'accepted'} and did nothing — a Fail-Early violation "
        "sitting on the wire. Deleted, not ported."
    ),
    declined_because=(
        "τ's AgentLoop executes tool calls itself. Accepting a tool result over "
        "RPC would open a second, unauthenticated path into the same executor "
        "that a host never drove the call for — Tier D's reasoning "
        "(REMOTE-CONTROL.md §3): 'a second privileged path into the same "
        "executor is a second thing to secure.'"
    ),
)

decline(
    "cycle_model",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D; pi has no analogue, this is a TUI-only affordance.",
    declined_because=(
        "A TUI keybinding affordance (step to the next configured model) leaking "
        "into a machine protocol — Tier D (REMOTE-CONTROL.md §3): 'a remote host "
        "enumerates and sets; it does not cycle.' A host that wants a specific "
        "model names it via set_model(name) — a shipped Tier B verb on this same "
        "table — rather than stepping through an ordered list it cannot see."
    ),
)

decline(
    "cycle_thinking_level",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D.",
    declined_because=(
        "Same Tier D judgment as cycle_model: a keybinding-shaped 'step to the "
        "next level' affordance, not a protocol verb. τ has no thinkingLevel "
        "concept on AgentSession today either (get_state's own notes list what "
        "τ has no equivalent of yet) — there is nothing for a set_* verb to set, "
        "let alone cycle."
    ),
)

decline(
    "set_steering_mode",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D.",
    declined_because=(
        "A TUI keybinding-adjacent mode toggle (pi's Enter-steers / Alt+Enter-"
        "follows binding) with no session-wide state on AgentSession to toggle: "
        "multitask_strategy is already a PER-SUBMISSION parameter on submit/"
        "prompt's own params_schema, not a mode a host would set once and "
        "forget. The per-call knob this would duplicate already exists."
    ),
)

decline(
    "set_follow_up_mode",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D.",
    declined_because=(
        "Same judgment as set_steering_mode: a TUI mode toggle with a per-"
        "submission equivalent (multitask_strategy='enqueue') already on the "
        "wire via submit/prompt, not a session-wide switch worth its own verb."
    ),
)

decline(
    "export_html",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D.",
    declined_because=(
        "Tier D (REMOTE-CONTROL.md §3): 'a host can render.' τ's job over this "
        "wire is to hand back messages (get_messages) and events; rendering "
        "them as HTML is presentation logic that belongs in the host, not a "
        "service τ provides over stdio."
    ),
)

decline(
    "bash",
    tier="D",
    since="2C",
    notes="docs/REMOTE-CONTROL.md §4[3] Tier D — out-of-band bash, distinct from the bash TOOL.",
    declined_because=(
        "τ's bash is a tool the agent loop executes under a Submission's "
        "provenance and admission rules, same as any other tool. An out-of-band "
        "'run this in the shell' RPC verb would be a second, unauthenticated "
        "path into the same executor a host never drove a turn for — identical "
        "reasoning to the send_tool_result decline above: 'a second privileged "
        "path into the same executor is a second thing to secure.'"
    ),
)
