"""The RPC command table: block [3], per docs/REMOTE-CONTROL.md §6 "Recommendation:
audit, do not generate".

One explicit entry per verb (`CommandEntry`), registered with the `@command(...)`
decorator (§6 point 4 — a registry INSIDE the RPC layer is fine; what is forbidden
is decorating `AgentSession` itself, §6 A1-A6).

`params_schema` is derived where the verb performs a declared capability, and
hand-written where it does not: `rpc.schema.params_schema_for` reads the argument
names, domains and requiredness out of `capabilities.CAPABILITIES`, and the verb
supplies the wire prose and any keyword a domain has no field for. Nothing is ever
derived from a Python signature via `inspect` (§6 A3) — the objection A3 raises is
to the wire tracking a kwarg rename nobody meant as a wire change, and an
`Argument.name` is the wire name, declared for that purpose. Structure is what
drifted between the two tables; prose never did, so prose stays reviewed. The four
verbs with no capability behind them — `prompt`, `get_capabilities`, `next_step`,
`enumerate_domain` — keep their literals outright, which is §6 A2 holding.

**Tier A** (required of any host): `prompt`, `abort`, `get_state`, `get_messages`,
`get_commands`, `get_tools`, `get_capabilities` (unit 2C), plus phase 3's
runtime-host trio `new_session`, `fork`, `switch_session` (H1,
docs/REMOTE-CONTROL.md §4[6] — backed by `AgentSessionRuntime`,
`agent_session_runtime.py`).
**Tier B** (parity — docs/RPC-TIER-B.md): `compact`, `complete_path`,
`get_last_assistant_text`,
`get_models`, `get_session_name`, `get_session_stats`, `list_sessions`,
`set_auto_compaction`, `set_model`, `set_session_name`. All of them are wired;
nothing in this table describes a Tier B verb as pending. (`get_models` and
`list_sessions` are the verbs with no row in RPC-TIER-B.md §3 — they answer
findings 7 and 8 of that tier's review, which found `set_model`'s `name` param
and `switch_session`'s `session_id` param unusable from the wire alone.)
**Tier C** (τ-justified, no pi equivalent): `submit`, the provenance
differentiator `prompt` is defined in terms of (§10 decision 10), plus the flow
loop's two reads, `next_step` and `enumerate_domain`. Those two are what let a
host drive a gesture it has no table for: τ's extensions are unknown to the
executing system, so it cannot enumerate valid actions from anything it ships
with. Both are READS and therefore carry no `cursor` (the E5 rule below).

Tier C also carries the eleven verbs that put the SESSION TREE and the
EXTENSION SYSTEM on the wire (`since="0.9.8"`). Five tree mutations —
`navigate`, `summarize_and_navigate`, `elide_span`, `commit_branch`,
`paste_subtree` — project `tau_agent_core.tree_ops`; three extension mutations —
`enable_extension`, `disable_extension`, `reload_extension` — project the
`AgentSession` methods of the same names; and three reads — `complete_message_id`,
`list_managed_extensions`, `get_extension_state` — are what make the mutations
callable by a host that was not handed an id or a path by something else. They
close the gap docs/VSCODE-HEAD.md §6 measured: 20 live verbs, none of which read
or wrote tree structure, so τ's differentiating feature was reachable only from
inside the Textual head. E5 and D-7 are answered for them the same way Tier B
answers them, and by the same two mechanisms rather than by a second rule — see
`tree_mutation_guard` for the five, and each extension verb's own notes for the
three that append nothing.
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
said what a verb RETURNS. Derived, since 2026-09-04, from
`capabilities.Capability.returns` by `rpc.schema.result_schema_for` — the same
move the params half made a day earlier, and it reverses the line this block
used to carry ("hand-written, always: nothing in the core declares what a
capability gives BACK"). Something does now, for all thirty. The four verbs
with no capability behind them keep their literals on this half too.

Reference: docs/REMOTE-CONTROL.md §3 "The command table", §4[3], §4[8], §6, §10.

E5 in Tier B — ONE answer, applied to every `since="tier-b"` verb.

No verb COUNT is stated anywhere in this block on purpose: `get_models`
landed after this rule was written (finding 7 of the same review) and
falsified every hand-written tally in the tier at a stroke. The
enumerations below are pinned instead — see the bottom of this block.

E5 (docs/REMOTE-CONTROL.md §4[4], line 267) is stated unconditionally:
"Every response to a mutating command returns the resulting cursor." The
tier first shipped TWO readings of it — `compact` returned the tip even
when it changed nothing ("the unchanged current tip"), while
`set_auto_compaction`, equally mutating and equally guarded by D-1,
returned no `cursor` key at all and neither its schema nor its notes said
why (finding 5 of the Tier B review). This is the settled rule, written
here rather than re-derived per verb:

  1. A MUTATING verb's COMPLETION always carries `cursor`, `required` in
     the schema that describes it and present on every success — INCLUDING
     when the call advanced nothing: a set that changed no value, a
     compaction that found nothing to compact, a verb that appends no log
     entry at all. "Completion" is the response itself for the
     synchronous mutators (`set_model`, `set_auto_compaction`,
     `set_session_name`) and the `compaction_end` notification for
     `compact`, whose response is only an acknowledgement (C3/D-5).
  2. A READ never carries one. `get_last_assistant_text`, `get_models`,
     `get_session_name`, `get_session_stats` and `list_sessions` have no
     `cursor` field; a host that wants the tip without mutating calls
     `get_state`.
  3. Absence is never a signal. Omitting `cursor` to mean "nothing moved"
     would make a host infer the tip from a missing key, which is exactly
     the inference F3 (§7.2, "no host may cache 'the tip'") exists to
     forbid — and it costs that host a round trip to learn what the
     response in its hand could have told it.

`abort` (and `submit`/`prompt`) are NOT counterexamples, and the exception
they carve is about TIME, not about no-ops: those verbs return before the
mutation they ask for has happened, so any cursor taken at signal time
would be the PRE-mutation tip — see `abort`'s own notes, which record the
phase-2 trace that measured the difference. Rule 1 applies wherever the
mutation is already complete when the completion is built, which is every
Tier B mutator.

Pinned by `test_rpc_tier_b_scaffolding.py`'s
`test_e5_is_answered_one_way_across_tier_b`, which also fails when a NEW
`since="tier-b"` verb is added without classifying it as a read or a
mutator — and by `test_the_prose_enumerations_of_tier_b_name_every_verb`,
which fails when a new verb is classified there but left out of rule 1's
or rule 2's list above (or out of `turn_safety_guard`'s docstring).

DURABILITY in Tier B (D-7) — ONE answer, applied to every `since="tier-b"`
verb. Written here so a host reads it once instead of deriving it from
whichever verb it happened to try first.

Finding 6 of the Tier B review measured three different answers on ONE
`new_session {"persist": false}` session: `set_model` and
`set_session_name` refused (-32603 "this session is unpersisted"),
`set_auto_compaction` returned a cursor, and `compact` ran to completion
and reported a cursor for a `compaction` entry that dies with the process.
No verb's notes said which of those was the rule.

The rule, and it is mechanical — a host can apply it without knowing any
verb's intent:

  1. A verb that APPENDS a session-log entry calls
     `require_durable_session` FIRST and refuses an unpersisted session
     outright: `set_model` (D-2's model_change), `set_session_name`
     (session_info), `compact` (compaction). Nothing is mutated before the
     refusal, so it is total.
  2. A verb that appends NOTHING never asks the question:
     `get_last_assistant_text`, `get_models`, `get_session_name`,
     `get_session_stats`, `list_sessions`, and `set_auto_compaction` — the
     last of which is why this rule is worth writing down, being a MUTATOR
     (D-1-guarded, E5-cursor-carrying) whose whole product is an in-memory
     field on `CompactionSettings`. Its `cursor` is the live tip reported
     as a READ (E5 rule 1 still requires it on a mutator's completion), not
     a claim that this call wrote anything.
     No verb COUNT appears above, for the reason the "E5 in Tier B" block
     states about its own lists: `get_models` landed after that rule was
     written and falsified every hand-written tally in the tier at a
     stroke. The enumerations are pinned instead.
  3. It is the SESSION's durability that is in question, never the
     directory it lives in. Unit S moved `--mode rpc`'s default session
     base to `<tmp>/.tau-<uid>/sessions` (D-6); that changes how LONG a persisted
     session lasts — stated on the wire in `set_model`'s and
     `set_session_name`'s notes — and changes nothing here. This rule keys
     on `path is None`, which is what `require_durable_session` asks.

Rule 1 is not new for `compact`; it is Blocker 2's answer, applied to the
one verb that had not been given it. `set_model` mutates live state AND
appends, and Blocker 2 settled that it refuses — so "it also does
something in memory" is already known not to buy an exemption, and giving
`compact` the opposite answer would be the fourth derivation, not a
principle.

TWO COSTS, both stated rather than hidden:

  - `compact` is now REFUSED on an unpersisted session, where it used to
    work. A host that wants both is asking for two contradictory things
    (nothing survives this process / rewrite the log I am keeping), and the
    honest fix is the one `require_durable_session`'s message already
    names: move onto a persisted session. Auto-compaction remains available
    there — see the next bullet, which is the same fact seen as a gap.
  - `set_auto_compaction(enabled=true)` on an unpersisted session ARMS a
    mechanism that then appends `compaction` entries to that same
    non-durable log, from inside `AgentSession._maybe_auto_compact` — a
    code path with no RPC verb on it and therefore nothing for rule 1 to
    guard. Same class as the gap `compact`'s notes already record about
    `AgentSession.compact()` not taking `turn_lock` itself: this tier
    guards the wire, not `AgentSession`'s internals, and widening
    `AgentSession` is out of its scope.

Out of scope, deliberately: `new_session`/`fork`/`switch_session` (Tier A).
They do not append to the bound log, they REPLACE it, and `persist` is
`new_session`'s own published parameter — a host states durability there
rather than discovering it.

Pinned by `test_rpc_tier_b_scaffolding.py`'s
`test_d7_is_answered_one_way_across_tier_b`, which reads the shipped
handler sources: a guarded verb that loses its `require_durable_session`
call, an unguarded verb that grows one, or a NEW `since="tier-b"` verb
classified in neither list, all fail there.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Literal
from uuid import uuid4

from tau_agent_core.agent_session_runtime import DEFAULT_SWAP_TIMEOUT_S
from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_agent_core.flows import Dispatched, FlowStep, Performed, Ready, View
from tau_agent_core.rpc import capabilities
from tau_agent_core.rpc.schema import params_schema_for, result_schema_for
from tau_agent_core.session_log import (
    DURABLE_LOCATION_ATTRS,
    declared_durable_locations,
    session_log_is_addressable,
)
from tau_agent_core.rpc.dialect import (
    COMMAND_NOT_SUPPORTED,
    INVALID_PARAMS,
    SESSION_NOT_PERSISTED,
    SUBMISSION_REJECTED,
    TURN_STILL_RUNNING,
)
from tau_agent_core.submission import Submission, SubmissionResult

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession, ExtensionActionResult
    from tau_agent_core.compaction import CompactionResult
    from tau_agent_core.rpc.handler import RPCHandler
    from tau_agent_core.session_catalog import SessionCatalog, SessionInfo

Tier = Literal["A", "B", "C", "D"]

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
    document"). Same discipline as `params_schema` — derived from the
    capability's own `returns` where there is one, hand-written where there is
    not, never derived via `inspect` (§6 A3) — and describes exactly what the
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
        _assert_supported_schema(self.params_schema, self.name)
        if self.result_schema is not None:
            _assert_supported_schema(self.result_schema, f"{self.name} (result)")


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


def validate_params(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    """Validate `params` against `schema`. Returns `None` if valid, else a
    human-readable description of the FIRST violation found (C2: `-32602`
    carries this message).
    """
    if schema.get("type") != "object":
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
    items = schema.get("items")
    if items is not None and isinstance(value, list):
        # An element schema is checked as an object or as a scalar, by its own `type`.
        check = _validate_object if items.get("type") == "object" else _validate_value
        for index, element in enumerate(value):
            violation = check(items, element, f"{path}[{index}]")
            if violation is not None:
                return violation
    return None


def _validate_object(schema: dict[str, Any], value: Any, path: str) -> str | None:
    """Validate one nested object — an array element under `items`.

    Separate from `validate_params` because that function's violation strings are
    the wire's `-32602` message and say "param"; a key inside a returned array is
    not a param and saying so would misdirect whoever reads the error.
    """
    if not isinstance(value, dict):
        return f"{path!r} must be an object, got {type(value).__name__}"
    properties: dict[str, Any] = schema.get("properties", {})
    for required_name in schema.get("required", []):
        if required_name not in value:
            return f"{path!r} is missing required key {required_name!r}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            return f"{path!r} has unexpected key(s): {', '.join(unknown)}"
    for key, element in value.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue
        violation = _validate_value(prop_schema, element, f"{path}.{key}")
        if violation is not None:
            return violation
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


_SUPPORTED_TYPES = frozenset({"string", "boolean", "integer", "number", "array", "object", "null"})
_SUPPORTED_OBJECT_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "description"}
)
_SUPPORTED_VALUE_KEYWORDS = frozenset({"type", "enum", "minimum", "description", "items"})


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
        items = prop_schema.get("items")
        if items is not None:
            if prop_schema.get("type") != "array":
                fail(f"property {prop_name!r} declares `items` without type 'array'")
            if not isinstance(items, dict):
                fail(f"property {prop_name!r} declares `items` that is not a schema")
            if items.get("type") == "object":
                _assert_supported_schema(items, f"{where}.{prop_name}[]")
            else:
                unknown = sorted(set(items) - _SUPPORTED_VALUE_KEYWORDS)
                if unknown:
                    fail(f"property {prop_name!r} items use unsupported keyword(s) {unknown}")
                declared_items = items.get("type")
                if declared_items is None:
                    fail(f"property {prop_name!r} items declare no `type`")
                names = declared_items if isinstance(declared_items, list) else [declared_items]
                bad = sorted(str(n) for n in names if n not in _SUPPORTED_TYPES)
                if bad:
                    fail(f"property {prop_name!r} items declare unsupported type(s) {bad}")
        declared = prop_schema.get("type")
        if declared is None:
            continue
        names = declared if isinstance(declared, list) else [declared]
        bad = sorted(str(n) for n in names if n not in _SUPPORTED_TYPES)
        if bad:
            fail(f"property {prop_name!r} declares unsupported type(s) {bad}")


NO_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

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
    "expand_attachments": {
        "type": "boolean",
        "description": (
            "Whether `@file` references in `text` are resolved into "
            "<attachment>/<reference> blocks before the Submission is built, "
            "the way the TUI's editor does (docs/FILE-ATTACHMENTS.md §2). "
            "Defaults to False, which sends `@notes.txt` to the model as those "
            "eleven literal characters. NOT a Submission field: expansion "
            "happens HERE, so what is persisted and what the model saw are the "
            "same string. Attached images are appended to `images`. Files are "
            "read at this moment, not when the host composed the text. See the "
            "`attachments` key on the result for what expansion did."
        ),
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


SUBMIT_RESULT_SCHEMA: dict[str, Any] = result_schema_for("submit")

ABORT_RESULT_SCHEMA: dict[str, Any] = result_schema_for("abort")

GET_STATE_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_state")

GET_MESSAGES_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_messages")

GET_COMMANDS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_commands")

GET_TOOLS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_tools")

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

SESSION_LIFECYCLE_RESULT_SCHEMA: dict[str, Any] = result_schema_for("new_session")


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
        return
    reason = _UNSUPPORTED_MULTITASK_STRATEGIES.get(strategy)
    if reason is not None:
        raise RPCError(INVALID_PARAMS, reason, data={"multitask_strategy": strategy})


def _expanded_text_and_images(
    params: dict[str, Any],
) -> tuple[str, list[dict[str, Any]] | None, dict[str, Any]]:
    """Resolve `@file` references in `params["text"]`, the way the TUI's editor does.

    The RPC counterpart of `TauApp._expand_attachments` (app.py), and
    deliberately the same two calls in the same order: `scan_attachments` to
    decide what each `@word` IS, then `render_attachments` to read the files
    at THIS moment. docs/FILE-ATTACHMENTS.md §2 puts expansion in the frontend
    because the core decides and the frontend performs — over this wire τ IS
    the frontend, which is the whole reason a head cannot do this for itself
    without re-implementing the block vocabulary in another language.

    Returns:
        `(text, images, report)`. `images` is `None` for "none", which is what
        `Submission` expects. `report` is the result's `attachments` key.
    """
    from tau_agent_core.attachments import (
        SENDABLE_KINDS,
        render_attachments,
        scan_attachments,
    )

    text: str = params["text"]
    attachments = scan_attachments(text, cwd=Path.cwd())
    unresolved = [a.token for a in attachments if a.kind == "unresolved"]
    sendable = [a for a in attachments if a.kind in SENDABLE_KINDS]

    if not sendable:
        return text, None, {"expanded": 0, "images": 0, "unresolved": unresolved, "failures": []}

    rendered = render_attachments(attachments)
    images = list(rendered.images) or None
    incoming = params.get("images")
    if incoming:
        images = list(incoming) + list(rendered.images)
    return (
        rendered.prefix + text,
        images,
        {
            "expanded": len(sendable),
            "images": len(rendered.images),
            "unresolved": unresolved,
            "failures": list(rendered.failures),
        },
    )


def _submission_from_params(params: dict[str, Any]) -> tuple[Submission, dict[str, Any] | None]:
    """Build a `Submission` from wire params. Provenance is ALWAYS present on
    the constructed record — defaulted when the caller omits it (`prompt`'s
    contract), never simply absent — regardless of which verb called this;
    it is each verb's `params_schema.required` list, not this function, that
    makes `submit` demand provenance on the wire (§10 decision 10).

    Returns:
        `(submission, attachment_report)`. The report is `None` when the
        request did not set `expand_attachments`, which is why the result's
        `attachments` key is ABSENT in that case rather than an empty summary
        claiming an expansion ran and found nothing.
    """
    _reject_unsupported_multitask_strategy(params)
    text: str = params["text"]
    images: list[dict[str, Any]] | None = params.get("images")
    report: dict[str, Any] | None = None
    if params.get("expand_attachments"):
        text, images, report = _expanded_text_and_images(params)

    kwargs: dict[str, Any] = {
        "text": text,
        "source": params.get("source", "rpc"),
        "submitter": params.get("submitter", "rpc-client"),
        "submission_id": params.get("submission_id") or str(uuid4()),
    }
    if report is not None:
        kwargs["images"] = images
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
        if optional_field in params and optional_field not in kwargs:
            kwargs[optional_field] = params[optional_field]
    return Submission(**kwargs), report


def _dispatched_result(
    sub: "Submission", dispatched: Dispatched, attachments: dict[str, Any] | None
) -> dict[str, Any]:
    """One arm of `Dispatched` as this verb's acceptance, or a refusal naming the arm.

    A `Performed` is the whole completion — the core ran an extension-registered
    command, no turn started, and there is no `agent_end` to carry it instead. The
    other three arms are things only a head does, and each refusal says which:

    - a `FlowStep` needs an argument bound, and the wire has `next_step` +
      `enumerate_domain` for exactly that loop.
    - a `Ready` names a mutation this table already publishes as its own verb, so
      the message says to call it.
    A `View` is neither: it comes back as a SUCCESS response carrying the view's
    name and, today, the reason no state rides with it. A host with its own browser
    opens it; a host without one prints the reason. It used to raise, and the reason
    it no longer does is that the tree payload has to land somewhere — putting it in
    an error response and moving it later is two breaks instead of one.

    The two refusals are `COMMAND_NOT_SUPPORTED` rather than a silent success, which
    is the same answer the old `performer="frontend"` check gave, said with the
    reason attached.
    """
    if isinstance(dispatched, Performed):
        return _accept_result(
            sub.submission_id,
            command=dispatched,
            command_name=dispatched.mutation,
            attachments=attachments,
        )
    if isinstance(dispatched, View):
        return _accept_result(sub.submission_id, view=dispatched, attachments=attachments)
    if isinstance(dispatched, FlowStep):
        detail = (
            f"/{dispatched.flow} still needs {dispatched.argument.name!r}. Bind it with "
            f"next_step + enumerate_domain (domain {dispatched.domain.name!r}), then call "
            f"the verb the flow ends in."
        )
        name = dispatched.flow
    elif isinstance(dispatched, Ready):
        detail = (
            f"/{dispatched.flow} is ready to perform {dispatched.mutation!r}, which this "
            f"table publishes as its own verb — call {dispatched.mutation!r} directly "
            "rather than submitting the slash line."
        )
        name = dispatched.flow
    else:
        raise AssertionError(f"unreachable Dispatched arm: {type(dispatched).__name__}")
    raise RPCError(
        COMMAND_NOT_SUPPORTED,
        detail,
        data={"submission_id": sub.submission_id, "command": name},
    )


def _accept_result(
    submission_id: str,
    *,
    command: Performed | None = None,
    command_name: str | None = None,
    view: View | None = None,
    attachments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The C3 acceptance payload. Never carries the turn's messages (C3:
    "the response must NOT carry the turn's messages") — those are pulled via
    `get_messages` (E2).

    `command` is set exactly when this acceptance is ALSO the only completion
    the host will ever get for this submission — a `Performed` from an
    extension-registered command (phase-2 review B2): no turn ran, so there is
    no `agent_end` to follow this response, and `SubmissionResult.command`
    would otherwise reach `_submit_and_acknowledge` and go no further. `None`
    (the default) covers every ordinary turn, where the real completion is
    the `agent_end` event on the subscription this handler already forwards.
    The other three arms of `Dispatched` never reach here — see
    `_dispatched_result`, which raises `RPCError` for each with the reason
    that arm carries.

    `attachments` is `_submission_from_params`'s report, set exactly when the
    request asked for expansion. It rides the ACCEPTANCE and not a later event
    because expansion happened before admission: by the time an `agent_end`
    could carry it, the model has already read the blocks, and a host told
    then that a file failed would be learning it too late to say so beside the
    message it typed.
    """
    result: dict[str, Any] = {
        "accepted": True,
        "submission_id": submission_id,
        "rejection_reason": None,
    }
    if view is not None:
        result["view"] = {
            "name": view.name,
            "state": view.state,
            "unavailable_because": view.unavailable_because,
        }
    if attachments is not None:
        result["attachments"] = attachments
    if command is not None:
        result["command"] = {
            "name": command_name,
            "output": command.data.get("output"),
        }
    return result


async def _submit_and_acknowledge(
    handler: "RPCHandler",
    msg_id: int | None,
    method: str,
    sub: Submission,
    attachments: dict[str, Any] | None = None,
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
    admitted: asyncio.Future[SubmissionResult | None] = loop.create_future()

    def _on_admitted() -> None:
        if admitted.done():
            return
        handler._output_queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    **_accept_result(sub.submission_id, attachments=attachments),
                    "method": method,
                },
            }
        )
        admitted.set_result(None)

    async def _drive() -> None:
        try:
            result = await handler.session.submit(sub, on_admitted=_on_admitted)
        except Exception as exc:  # noqa: BLE001 - see the two branches below
            if not admitted.done():
                admitted.set_exception(exc)
            else:
                print(
                    f"[τ-rpc] submission {sub.submission_id!r} failed after admission: {exc!r}",
                    file=sys.stderr,
                )
            return
        if not admitted.done():
            admitted.set_result(result)

    task = asyncio.create_task(_drive())
    handler.track_background_task(task)

    outcome = await admitted
    if outcome is None:
        # _on_admitted already sent the response. Nothing left to do.
        return None

    if not outcome.accepted:
        data: dict[str, Any] = {"submission_id": sub.submission_id}
        if outcome.lock is not None:
            # The one refusal a host can act on rather than only report (EXTENSION-LOCKS §10).
            data["lock"] = {
                "entry_id": outcome.lock.entry_id,
                "extension": outcome.lock.extension,
                "sentence": outcome.lock.sentence,
                "label": outcome.lock.label,
                "release": outcome.lock.release,
                "ask": outcome.lock.ask,
            }
        raise RPCError(
            SUBMISSION_REJECTED,
            outcome.rejection_reason or "submission rejected",
            data=data,
        )
    if outcome.command is not None:
        return _dispatched_result(sub, outcome.command, attachments)
    return _accept_result(sub.submission_id, attachments=attachments)


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
    sub, attachments = _submission_from_params(params)
    return await _submit_and_acknowledge(handler, msg_id, "submit", sub, attachments)


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
    sub, attachments = _submission_from_params(params)
    return await _submit_and_acknowledge(handler, msg_id, "prompt", sub, attachments)


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
    params_schema=params_schema_for("abort"),
    result_schema=ABORT_RESULT_SCHEMA,
)
async def _handle_abort(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
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
    params_schema=params_schema_for("get_state"),
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
        "addressable": session.is_addressable,
    }


@command(
    "get_messages",
    tier="A",
    since="2A",
    notes="E2's PULL side — the terminal message array, fetched, never pushed.",
    params_schema=params_schema_for("get_messages"),
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
        "τ's built-ins (commands.FRONTEND_COMMANDS, origin='builtin') plus "
        "whatever extensions registered via api.register_command (origin='extension'), "
        "in `resolve_command`'s own precedence order so the listing cannot advertise "
        "a name that dispatch would resolve differently. "
        "`flow` says whether the command declares what it TAKES: true means `next_step` "
        "steps it and `enumerate_domain` lists its argument's values, so a host builds a "
        "form or a completion list rather than asking for one opaque line. An extension "
        "command is a flow only if it used api.register_flow "
        "(docs/EXTENSION-FLOWS.md)."
    ),
    params_schema=params_schema_for("get_commands"),
    result_schema=GET_COMMANDS_RESULT_SCHEMA,
)
async def _handle_get_commands(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    vocabulary = handler.session.vocabulary
    listed: list[dict[str, Any]] = [
        {
            "name": name,
            "description": description,
            "origin": "builtin",
            "flow": name not in vocabulary.views,
        }
        for name, description in FRONTEND_COMMANDS.items()
    ]
    for name, description in handler.session.get_extension_commands():
        if name in FRONTEND_COMMANDS:
            continue
        listed.append(
            {
                "name": name,
                "description": description,
                "origin": "extension",
                "flow": name in vocabulary.extension_flows,
            }
        )
    return {"commands": listed}


@command(
    "get_tools",
    tier="A",
    since="2A",
    notes="Already implemented pre-2A; ported onto the table verbatim, no behaviour change.",
    params_schema=params_schema_for("get_tools"),
    result_schema=GET_TOOLS_RESULT_SCHEMA,
)
async def _handle_get_tools(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    tools = handler.session.tools
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
            "addressable": session_log_is_addressable(outcome["session"]),
        },
        "cursor": outcome["cursor"],
    }


SWITCH_SESSION_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "switch_session",
    overrides={
        "session_id": {
            "description": (
                "An exact session id, or a unique id prefix, scoped to this "
                "process's cwd — the same resolution --session REF uses headlessly "
                "(SessionCatalog.resolve_ref). Every acceptable value is a "
                "`session_id` list_sessions returned (finding 8): resolve_ref is "
                "built on the same list(cwd) that verb publishes, so the two cannot "
                "disagree."
            ),
        },
    },
)


NEW_SESSION_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "new_session",
    overrides={
        "persist": {
            "description": (
                "Whether the new session is written to the configured store. "
                "Omitted defaults to TRUE: addressable by switch_session, listed "
                "by list_sessions, and able to keep what set_model/"
                "set_session_name/compact append to it. false gives an in-memory "
                "conversation that outlives nothing — the result then reports "
                "`session.addressable: false` (finding 7), no list_sessions row "
                "exists for its id, switch_session refuses it, and those three "
                "verbs REFUSE (SESSION_NOT_PERSISTED) rather than return a cursor "
                "for a write that never landed. Which verbs those are is not a "
                "list to memorise: D-7 (commands.py 'DURABILITY in Tier B') is "
                "'the verb that appends refuses', and the rest — including "
                "set_auto_compaction — answer normally."
            ),
        },
    },
)


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
    result_schema=result_schema_for("new_session"),
)
async def _handle_new_session(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    runtime = _require_runtime(handler)
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
    params_schema=params_schema_for("fork"),
    result_schema=result_schema_for("fork"),
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
    result_schema=result_schema_for("switch_session"),
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
    ``set_session_name`` (B5). Tier C's mutating verbs take it too, through
    :func:`tree_mutation_guard` for ``navigate``, ``summarize_and_navigate``,
    ``elide_span``, ``commit_branch`` and ``paste_subtree``, and directly for
    ``enable_extension``, ``disable_extension`` and ``reload_extension``. The
    tier's reads take no guard:
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


def require_durable_session(session: "AgentSession", *, verb: str) -> None:
    """The precondition a verb takes before promising a durable write — the
    corrected §1.1 guard (Blocker 2 of the Tier B review).

    Raises ``RPCError(SESSION_NOT_PERSISTED)`` unless the bound ``session_log``
    declares a durable location (:data:`DURABLE_LOCATION_ATTRS`) and that
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
    declared = declared_durable_locations(log)
    if not declared:
        raise RPCError(
            SESSION_NOT_PERSISTED,
            f"{verb}: the bound session log ({type(log).__name__}) declares no durable "
            f"location (none of {', '.join(DURABLE_LOCATION_ATTRS)}) — this verb will "
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


### begin tier-b:compact

COMPACT_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "compact",
    overrides={
        "custom_instructions": {
            "description": (
                "Optional extra focus for the generated summary, threaded "
                "unchanged to AgentSession.compact(custom_instructions=...)."
            ),
        },
    },
)

COMPACT_RESULT_SCHEMA: dict[str, Any] = result_schema_for("compact")

COMPACTION_END_METHOD = "compaction_end"

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
    acknowledged: asyncio.Future[None] = loop.create_future()
    cancelled_by_abort = False

    def _acknowledge() -> None:
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
        handler.bind_compaction_aborter(_cancel_for_abort)
        acknowledged.set_result(None)

    def _cancel_for_abort() -> None:
        nonlocal cancelled_by_abort
        cancelled_by_abort = True
        task.cancel()

    def _complete(payload: dict[str, Any]) -> None:
        params = {
            "compaction_id": compaction_id,
            "request_id": msg_id,
            **payload,
        }
        if not handler.output_is_deliverable:
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
                if cancelled_by_abort:
                    _complete(
                        {
                            "is_error": False,
                            "error": None,
                            "cancelled": True,
                            "cursor": session.session_log.cursor,
                        }
                    )
                    raise
                print(
                    f"[τ-rpc] compaction {compaction_id!r} was cancelled before it "
                    "finished (RPC shutdown) — no compaction_end will follow",
                    file=sys.stderr,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - see the two branches
                if not acknowledged.done():
                    acknowledged.set_exception(exc)
                    return
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
            handler.release_compaction()
            if not acknowledged.done():
                acknowledged.cancel()

    task = asyncio.create_task(_drive())
    handler.track_background_task(task)
    await acknowledged  # raises the guard's RPCError, or returns once acked
    return None


### end tier-b:compact

### begin tier-b:complete_path
COMPLETE_PATH_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "complete_path",
    overrides={
        "text": {"description": "The editor's contents as typed, NOT just the @word."},
        "cursor": {
            "minimum": 0,
            "description": (
                "The cursor's character offset into `text`. Which @reference is "
                "being completed is decided from this, so a host that sends the "
                "@word alone with cursor 0 gets `completion: null` rather than a "
                "listing — the cursor is not optional and is not defaulted."
            ),
        },
    },
)

COMPLETE_PATH_RESULT_SCHEMA: dict[str, Any] = result_schema_for("complete_path")


@command(
    "complete_path",
    tier="B",
    since="1.4",
    notes=(
        "The @file half of what a chat editor needs to be usable, and the "
        "counterpart of `get_commands` for the slash half. A host cannot "
        "compute this for itself: a browser has no filesystem at all, and even "
        "a host that does have one (a VS Code extension) would be listing ITS "
        "filesystem, which under Remote SSH or a devcontainer is the wrong "
        "machine. This answers from the process working directory — the same "
        "directory the agent's own tools resolve against and the same one "
        "`submit {expand_attachments: true}` reads, so the listing, the "
        "expansion and the tools cannot disagree about which file @notes.txt "
        "names. "
        "A thin wrapper over `attachments.complete_attachment`, which the TUI's "
        "own editor calls: one implementation of the matching rules (case-"
        "sensitive prefix on the last path segment, hidden entries only once "
        "the prefix itself starts with a dot), not two that drift. "
        "Read-only and pure apart from reading directory entries: no D-1 "
        "turn_safety_guard (it mutates nothing, so it answers mid-turn), no "
        "`cursor` (E5 binds mutators), no require_durable_session (D-7: it "
        "appends nothing, and it answers the same under --no-session). "
        "G3, 'nothing unbounded is pushed': `matches` is bounded by "
        "attachments._COMPLETION_LIMIT and `total` reports the true count, so a "
        "host is told when it is seeing a prefix of the answer instead of "
        "silently shown one. "
        "SCOPE, stated not hidden: an absolute or ../ token lists outside the "
        "working directory, exactly as it does in the TUI. That is not a new "
        "hole — this same connection can run `bash` through the agent — but a "
        "host serving this to a browser on a shared network is publishing a "
        "directory lister to whoever holds the token, and should know it."
    ),
    params_schema=COMPLETE_PATH_PARAMS_SCHEMA,
    result_schema=COMPLETE_PATH_RESULT_SCHEMA,
)
async def _handle_complete_path(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.attachments import complete_attachment

    completion = complete_attachment(params["text"], params["cursor"], cwd=Path.cwd())
    if completion is None:
        return {"completion": None}
    return {
        "completion": {
            "start": completion.start,
            "end": completion.end,
            "token": completion.token,
            "matches": [
                {"name": m.name, "detail": m.detail, "is_dir": m.is_dir} for m in completion.matches
            ],
            "total": completion.total,
        }
    }


### end tier-b:complete_path

GET_LAST_ASSISTANT_TEXT_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_last_assistant_text")


@command(
    "get_last_assistant_text",
    tier="B",
    since="tier-b",
    notes=(
        "AgentSession.get_last_assistant_text(), projected. The derivation used "
        "to live HERE, in the wire layer (docs/RPC-TIER-B.md §1: 'No method. "
        "Trivially derived') — which meant a head that was not this one had to "
        "re-derive it and could reach a different answer; it is now one core call "
        "and this verb is its projection. Read-only, "
        "no D-1 turn_safety_guard (nothing here mutates session state). "
        "'text' is the last qualifying assistant message's "
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
    params_schema=params_schema_for("get_last_assistant_text"),
    result_schema=GET_LAST_ASSISTANT_TEXT_RESULT_SCHEMA,
)
async def _handle_get_last_assistant_text(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return {"text": handler.session.get_last_assistant_text()}


### end tier-b:get_last_assistant_text

GET_MODELS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_models")

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
    params_schema=params_schema_for("get_models"),
    result_schema=GET_MODELS_RESULT_SCHEMA,
)
async def _handle_get_models(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    resolver = session.model_resolver
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
            raise RuntimeError(
                f"get_models: config model {name!r} does not build: {_resolver_error_message(exc)}"
            ) from exc
        listed.append(
            {
                "name": name,
                "model": {
                    "id": model.id,
                    "provider": model.provider,
                    "context_window": model.context_window,
                },
            }
        )
    return {"models": listed}


### end tier-b:get_models

GET_SESSION_STATS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_session_stats")


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
        "keep_recent_tokens — read from AgentSession.compaction_settings, "
        "which returns a copy); the newest "
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
        "answers while `compact` itself refuses (D-7 rule 1). The composition "
        "is AgentSession.get_session_stats(), one core call, and this verb is "
        "its projection: it used to be assembled here, in the wire layer, which "
        "left every other head to assemble its own. Known gap: "
        "`last_compaction` is a scan of the "
        "log's own append order, not the ConversationTree active path — see "
        "AgentSession.get_last_compaction's docstring."
    ),
    params_schema=params_schema_for("get_session_stats"),
    result_schema=GET_SESSION_STATS_RESULT_SCHEMA,
)
async def _handle_get_session_stats(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    stats = handler.session.get_session_stats()
    last = stats.last_compaction
    return {
        "context": {
            "tokens": stats.context.tokens,
            "usage_tokens": stats.context.usage_tokens,
            "trailing_tokens": stats.context.trailing_tokens,
            "last_usage_index": stats.context.last_usage_index,
        },
        "context_window": stats.context_window,
        "context_headroom": stats.context_headroom,
        "compaction_settings": {
            "enabled": stats.compaction_settings.enabled,
            "reserve_tokens": stats.compaction_settings.reserve_tokens,
            "keep_recent_tokens": stats.compaction_settings.keep_recent_tokens,
        },
        "last_compaction": None
        if last is None
        else {
            "id": last.id,
            "timestamp": last.timestamp,
            "summary": last.summary,
            "first_kept_id": last.first_kept_id,
            "tokens_before": last.tokens_before,
        },
        "usage": stats.usage,
    }


### end tier-b:get_session_stats

LIST_SESSIONS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("list_sessions")


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
    params_schema=params_schema_for("list_sessions"),
    result_schema=LIST_SESSIONS_RESULT_SCHEMA,
)
async def _handle_list_sessions(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    runtime = _require_runtime(handler)
    catalog: SessionCatalog = runtime.catalog
    cwd: str = runtime.cwd
    return {
        "sessions": [_listed_session(info) for info in catalog.list(cwd)],
        "scope": {"store": runtime.store, "cwd": cwd},
    }


### end tier-b:list_sessions

### begin tier-b:set_auto_compaction
SET_AUTO_COMPACTION_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "set_auto_compaction",
    overrides={
        "enabled": {
            "description": (
                "Desired auto-compaction state. RPC sessions are constructed "
                "with CompactionSettings(enabled=False) (backends.py:885, "
                "RPC-TIER-B.md §1.1), so a host that wants it on says so here; "
                "AgentSession.set_auto_compaction is the same setter in process."
            ),
        },
    },
)

SET_AUTO_COMPACTION_RESULT_SCHEMA: dict[str, Any] = result_schema_for("set_auto_compaction")


@command(
    "set_auto_compaction",
    tier="B",
    since="tier-b",
    notes=(
        "D-4: a plain, idempotent setter — `AgentSession.set_auto_compaction`, "
        "which this verb calls rather than writing the field itself. It used to "
        "write `session._compaction_settings.enabled` directly, on the ground "
        "that no accessor existed (§1's ground truth) and that `get_tools` set "
        "the precedent with `session._tools`. Both reaches are gone: the method "
        "exists, the TUI and the CLI can call it too, and this verb is no longer "
        "the only door onto it. "
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
        "Why a host has to ask at all: rpc_mode.py -> backends.create_backend "
        "-> TauBackend constructs its session with CompactionSettings("
        "enabled=False) (backends.py:885, §1.1) and nothing else in RPC mode "
        "flips it. "
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
        effective = session.set_auto_compaction(bool(params["enabled"]))
        cursor = session.session_log.cursor
    return {"enabled": effective, "cursor": cursor}


### end tier-b:set_auto_compaction

### begin tier-b:set_model

SET_MODEL_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "set_model",
    overrides={
        "name": {
            "description": (
                "A config model NAME (a key in ~/.tau/config.json's 'models' "
                "map), resolved through AgentSession's bound model resolver "
                "(set_model_resolver) — the same name --model NAME accepts "
                "headlessly. NOT a model id; a config key may alias one."
            ),
        },
    },
)

SET_MODEL_RESULT_SCHEMA: dict[str, Any] = result_schema_for("set_model")


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
        require_durable_session(session, verb="set_model")
        require_log_appender(session, "append_model_change", verb="set_model")
        try:
            model = session.set_model(name)
        except (KeyError, ValueError) as exc:
            raise RPCError(
                INVALID_PARAMS, _resolver_error_message(exc), data={"name": name}
            ) from exc
        getattr(session.session_log, "append_model_change")(name, model["provider"])
        return {"model": model, "cursor": session.session_log.cursor}


### end tier-b:set_model

SET_SESSION_NAME_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "set_session_name",
    overrides={
        "name": {"description": "The session's new durable display name. Must be non-empty."},
    },
)

SET_SESSION_NAME_RESULT_SCHEMA: dict[str, Any] = result_schema_for("set_session_name")

GET_SESSION_NAME_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_session_name")


@command(
    "set_session_name",
    tier="B",
    since="tier-b",
    notes=(
        "D-1 (mutating): takes turn_safety_guard before writing. Calls "
        "AgentSession.set_session_name, which is extension_types."
        "apply_session_name — the SAME body ExtensionAPI.set_session_name "
        "calls (docs/RPC-TIER-B.md B5: 'do "
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
        "Nothing is mutated before either check. This verb was RPC's only "
        "door onto append_session_info until AgentSession.set_session_name "
        "existed; a head now reaches the same body without a wire. WHERE the "
        "rename lands, and for how long "
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
    session = handler.session
    name = params["name"]
    try:
        async with turn_safety_guard(session):
            require_durable_session(session, verb="set_session_name")
            session.set_session_name(name)
    except ValueError as exc:
        raise RPCError(INVALID_PARAMS, str(exc), data={"name": name}) from exc
    return {"name": name, "cursor": session.session_log.cursor}


@command(
    "get_session_name",
    tier="B",
    since="tier-b",
    notes=(
        "Read-only (docs/RPC-TIER-B.md B5: 'the read does not' take D-1's "
        "guard or carry a cursor). Calls AgentSession.get_session_name, "
        "which is extension_types.read_session_name — the SAME body "
        "ExtensionAPI.get_session_name calls. A session "
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
    params_schema=params_schema_for("get_session_name"),
    result_schema=GET_SESSION_NAME_RESULT_SCHEMA,
)
async def _handle_get_session_name(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return {"name": handler.session.get_session_name()}


### end tier-b:set_session_name

### begin tier-c:next_step
NEXT_STEP_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "flow": {
            "type": "string",
            "description": (
                "The flow's name — one of the `name`s `get_commands` lists. An "
                "unknown name is an error, not an empty answer: a host that "
                "believes a flow exists must be told it does not."
            ),
        },
        "bound": {
            "type": "object",
            "description": (
                "The arguments bound so far, keyed by argument name. Omit it, or "
                "send {}, for the flow's first step. Only REQUIRED arguments "
                "block, so a flow whose arguments are all optional is `ready` on "
                "the first call."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "The entry a scoped `message_id` argument is relative to. A "
                "parameter rather than the live tip, so a host stepping a "
                "sub-agent's flow scopes to THAT agent's cursor. It is echoed "
                "back on the step so the host hands it straight to "
                "`enumerate_domain`."
            ),
        },
    },
    "required": ["flow"],
    "additionalProperties": False,
}

NEXT_STEP_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["step", "ready"],
            "description": (
                "`step` — one required argument is still unbound and `step` "
                "describes it. `ready` — every required argument is bound and "
                "`ready` names the mutation to perform and what to perform it "
                "with. The two are mutually exclusive and exactly one is present."
            ),
        },
        "step": {
            "type": ["object", "null"],
            "description": (
                "{flow, argument, domain, cursor, bound}. `argument` is "
                "{name, domain, description, cardinality, required, scope}; "
                "`domain` is the resolved domain record "
                "{name, description, free, values, enumerator}, included so a "
                "host can render the field without a second call — `values` is "
                "non-null for a small fixed set, and `enumerator` non-null means "
                "call `enumerate_domain` for the live set."
            ),
        },
        "ready": {
            "type": ["object", "null"],
            "description": (
                "{flow, mutation, arguments}. `mutation` is the capability to "
                "perform — the named flow's, always, so a host that already knows "
                "which flow it stepped can dispatch before this returns. "
                "`arguments` is what to perform it with, keyed by the mutation's "
                "own parameter names. This is a commitment: τ does not ask a "
                "second time, and a host that wants a confirmation renders one "
                "from this."
            ),
        },
    },
    "required": ["status"],
}


@command(
    "next_step",
    tier="C",
    since="0.9.8",
    notes=(
        "Half of the flow loop, and the reason a host can offer a gesture it has "
        "never heard of. A flow is an ordered argument list ending in one "
        "mutation; this returns either the next argument or the mutation, and a "
        "host renders whatever it gets. The same call drives a modal wizard, a "
        "tab-completion popup and a shell — the difference between them is the "
        "presenter, not the protocol. "
        "It has to be on the wire rather than computed host-side because τ's "
        "extensions are unknown to the host: a host cannot enumerate valid "
        "actions it has no table for. "
        "PURE and a READ: it performs nothing, reads no session, and therefore "
        "carries no `cursor` (E5 rule 2). No D-1 turn_safety_guard — it mutates "
        "nothing, so it answers mid-turn — and no require_durable_session, since "
        "it appends nothing and answers the same under --no-session. "
        "Partial arguments ARE the dry run: a flow invoked with nothing bound "
        "reports its first step and changes nothing, which is why there is no "
        "`-y` and no confirmation verb."
    ),
    params_schema=NEXT_STEP_PARAMS_SCHEMA,
    result_schema=NEXT_STEP_RESULT_SCHEMA,
)
async def _handle_next_step(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from dataclasses import asdict

    from tau_agent_core.flows import Ready, UnknownFlowError, next_step

    try:
        outcome = next_step(
            params["flow"],
            params.get("bound"),
            params.get("cursor"),
            vocabulary=handler.session.vocabulary,
        )
    except UnknownFlowError as exc:
        raise RuntimeError(str(exc)) from exc
    if isinstance(outcome, Ready):
        return {"status": "ready", "ready": asdict(outcome), "step": None}
    return {"status": "step", "step": asdict(outcome), "ready": None}


### end tier-c:next_step

### begin tier-c:enumerate_domain
ENUMERATE_DOMAIN_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": (
                "The domain's name, as a `next_step` step reported it. A domain "
                "that is `free` returns no values and total 0 — that is the "
                "answer, not a failure."
            ),
        },
        "scope": {
            "type": ["string", "null"],
            "enum": ["in_session", "ancestors_of_cursor", "descendants_of_cursor", None],
            "description": (
                "For `message_id` only: which entries are candidates. Defaults to `in_session`."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "For a scoped `message_id`: the entry the scope is relative to. "
                "Null uses the live tip. An id that names no entry is an error, "
                "not an empty listing."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Filter text. A value matches on a case-sensitive PREFIX of the "
                "value itself, or a case-insensitive SUBSTRING of its label — "
                "completion and search, because an id and its text are looked for "
                "differently. Empty matches everything in scope."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "How many values to return at most. Defaults to 50.",
        },
    },
    "required": ["domain"],
    "additionalProperties": False,
}

ENUMERATE_DOMAIN_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "The domain that was enumerated."},
        "values": {
            "type": "array",
            "description": (
                "A list of {value, label}. `value` is what a host binds into "
                "`next_step`'s `bound`; `label` is what it shows. They are equal "
                "for a domain whose values already read as text."
            ),
        },
        "total": {
            "type": "integer",
            "description": (
                "How many values matched before `limit` was applied, so a host "
                "says '12 of 340' instead of implying it showed everything (G3). "
                "An empty `values` with a non-zero `total` cannot happen; an "
                "empty one with total 0 means the domain genuinely has none."
            ),
        },
    },
    "required": ["domain", "values", "total"],
}


@command(
    "enumerate_domain",
    tier="C",
    since="0.9.8",
    notes=(
        "The other half of the flow loop. A flow argument carries a DOMAIN — a "
        "named type in τ's object model — rather than a list of strings, and this "
        "is what turns one into the values that are legal right now, each with a "
        "label a person can read. "
        "The rule it states once was previously rediscovered twice by hand: "
        "`get_models` was added because `set_model`'s config NAME was "
        "unconstructible from the wire (finding 7), and `list_sessions` because "
        "`switch_session`'s id was (finding 8). A mutation with a bounded "
        "parameter is uncallable without an enumerating read. "
        "It dispatches to those same readers rather than reimplementing them, so "
        "a listing here and the corresponding verb cannot disagree about what "
        "exists. `model_name` -> the bound model resolver's catalogue (the same "
        "one `get_models` reads); `session_id` -> the runtime's SessionCatalog "
        "(the same `list_sessions` publishes); `path` -> "
        "attachments.complete_attachment (the same `complete_path` wraps); "
        "`message_id` -> ConversationTree.complete_message_id; `extension_name` "
        "-> AgentSession.list_managed_extensions. "
        "A READ: no `cursor` (E5 rule 2), no D-1 turn_safety_guard, no "
        "require_durable_session. "
        "Fail-Early on a missing dependency: a domain whose reader needs a "
        "runtime this process does not have RAISES rather than answering with an "
        "empty list, which a host would read as 'there are none'."
    ),
    params_schema=ENUMERATE_DOMAIN_PARAMS_SCHEMA,
    result_schema=ENUMERATE_DOMAIN_RESULT_SCHEMA,
)
async def _handle_enumerate_domain(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.flows import enumerate_domain

    found = enumerate_domain(
        params["domain"],
        session=handler.session,
        runtime=handler._runtime,
        scope=params.get("scope"),
        cursor=params.get("cursor"),
        query=params.get("query", ""),
        limit=params.get("limit", 50),
        vocabulary=handler.session.vocabulary,
    )
    return {
        "domain": found.domain,
        "values": [{"value": v.value, "label": v.label} for v in found.values],
        "total": found.total,
    }


### end tier-c:enumerate_domain


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


COMPLETE_MESSAGE_ID_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "complete_message_id",
    overrides={
        "scope": {
            "description": (
                "Which entries are candidates. 'in_session' is every entry in the "
                "log; 'ancestors_of_cursor' is the parent chain from the root to "
                "`cursor` inclusive; 'descendants_of_cursor' is the subtree below "
                "it, excluding `cursor` itself. Omitted means 'in_session'."
            ),
        },
        "cursor": {
            "description": (
                "The entry the two scoped variants are relative to. Omitted uses "
                "the session's own cursor (get_state's `cursor`). An id that names "
                "no entry is INVALID_PARAMS, never an empty match list."
            ),
        },
        "query": {
            "description": (
                "Filter text. Matches a case-sensitive PREFIX of an entry id, or a "
                "case-insensitive SUBSTRING of its preview — completion and search "
                "in one field. Omitted or empty matches everything in scope."
            ),
        },
        "limit": {
            "description": "How many matches to return at most. Omitted means 50.",
            "minimum": 1,
        },
    },
)

COMPLETE_MESSAGE_ID_RESULT_SCHEMA: dict[str, Any] = result_schema_for("complete_message_id")


@command(
    "complete_message_id",
    tier="C",
    since="0.9.8",
    notes=(
        "The message_id domain's enumerator, addressed by its own capability name. "
        "ConversationTree.complete_message_id over the session's live entries and "
        "cursor. Until this verb, every capability taking an entry id was callable "
        "over the wire only by a host that had been handed an id by something "
        "else — the hole get_models closed for set_model and list_sessions for "
        'switch_session. Overlaps enumerate_domain {"domain": "message_id"} '
        "deliberately and returns the same data under its own field names "
        "(entry_id/preview rather than value/label): a host walking a FLOW's "
        "argument list reaches it through enumerate_domain without knowing which "
        "capability enumerates that domain, and a host calling the capability by "
        "name calls this. Read-only: no D-1 turn_safety_guard, no `cursor` in the "
        "result (E5 rule 2 — a host that wants the tip calls get_state), and no "
        "require_durable_session (D-7 rule 2: it appends nothing). Refuses: a "
        "`cursor` naming no entry, under a scope that needs one, is a CALLER error "
        "and comes back as INVALID_PARAMS — the same classification set_model "
        "gives an unknown model name. Fail-Early, because the alternative is an "
        "empty match list that reads as 'the scope held nothing'."
    ),
    params_schema=COMPLETE_MESSAGE_ID_PARAMS_SCHEMA,
    result_schema=COMPLETE_MESSAGE_ID_RESULT_SCHEMA,
)
async def _handle_complete_message_id(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.conversation_tree import ConversationTree

    log = handler.session.session_log
    tree = ConversationTree(log.entries(), log.cursor)
    try:
        found = tree.complete_message_id(
            scope=params.get("scope", "in_session"),
            cursor=params.get("cursor"),
            query=params.get("query", ""),
            limit=params.get("limit", 50),
        )
    except KeyError as exc:
        raise RPCError(
            INVALID_PARAMS, str(exc.args[0]) if exc.args else str(exc), data=dict(params)
        ) from exc
    return {
        "matches": [{"entry_id": m.entry_id, "preview": m.preview} for m in found.matches],
        "total": found.total,
    }


### end tier-c:complete_message_id

### begin tier-c:get_tree

GET_TREE_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_tree")


@command(
    "get_tree",
    tier="C",
    since="0.10.1",
    notes=(
        "ConversationTree.browse() over the session's live entries and cursor — the "
        "shape half of the tree, and the read the five tree MUTATIONS were "
        "uncallable without. docs/VSCODE-HEAD.md §6 measured that gap and named "
        "this verb as what closes it: 0.9.8 put navigate, elide_span, "
        "commit_branch, paste_subtree and summarize_and_navigate on the wire, and "
        "complete_message_id returns a flat list of (entry_id, preview) pairs with "
        "no parent links — a picker, not a browser. A host could edit a tree it had "
        "no way to draw. Every node carries the facts a browser COLOURS a row with "
        "as well as the ones it draws it from, because each of them is read out of "
        "the raw entry and no other verb hands a raw entry over: `first_kept_id` is "
        "the fold's boundary, `tool_call_ids`/`tool_call_id` the pairing a mark "
        "expands over, `copyable` the paste source rule, `from_id` the "
        "branch-summary pair. Without them an out-of-process head would recompute "
        "each from a second reading of the log's shape, which is the drift the "
        "capability registry exists to make impossible. FLAT with `parent_id`, not "
        "nested: a five-hundred-message linear conversation nests five hundred "
        "deep, and json.dumps has a recursion limit where a tree does not. "
        "UNBOUNDED, deliberately, and this is the one place G3 is argued rather "
        "than applied: G3 forbids pushing something unbounded, and this is a PULL — "
        "the shape IS the answer, and a bounded shape is a different tree. `count` "
        "is there so a host can say it read a whole one. Read-only: no D-1 "
        "turn_safety_guard (it mutates nothing, so it answers mid-turn — a browser "
        "opened while a turn streams shows the tree as it stands), no `cursor` in "
        "the E5 sense (the `cursor` key here is the tip this READ observed, not a "
        "mutation's product), and no require_durable_session (D-7 rule 2: it "
        "appends nothing, and an unpersisted session has a tree like any other). "
        "The `/tree` VIEW command still carries `unavailable_because` rather than "
        "state: resolve_command is pure and holds no session, so a head opens the "
        "view from THIS read — which is what that sentence has said since 0.9.8 and "
        "what it can now mean."
    ),
    params_schema=params_schema_for("get_tree"),
    result_schema=GET_TREE_RESULT_SCHEMA,
)
async def _handle_get_tree(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.conversation_tree import ConversationTree

    log = handler.session.session_log
    tree = ConversationTree(log.entries(), log.cursor)
    nodes = [
        {
            "entry_id": node.entry_id,
            "parent_id": node.parent_id,
            "kind": node.kind,
            "role": node.role,
            "preview": node.preview,
            "is_cursor": node.is_cursor,
            "timestamp": node.timestamp,
            "first_kept_id": node.first_kept_id,
            "from_id": node.from_id,
            "is_system": node.is_system,
            "tool_call_ids": list(node.tool_call_ids),
            "tool_call_id": node.tool_call_id,
            "copyable": node.copyable,
            "estimated_tokens": node.estimated_tokens,
        }
        for node in tree.browse()
    ]
    return {"nodes": nodes, "cursor": log.cursor, "count": len(nodes)}


### end tier-c:get_tree

### begin tier-c:get_entry

GET_ENTRY_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "get_entry",
    overrides={
        "entry_id": {
            "description": (
                "The entry to read — an `entry_id` from get_tree or "
                "complete_message_id. An id naming no entry is INVALID_PARAMS, "
                "never a null entry."
            ),
        },
    },
)

GET_ENTRY_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_entry")


@command(
    "get_entry",
    tier="C",
    since="0.10.1",
    notes=(
        "ConversationTree.entry(entry_id) — one node's full body, which is what a "
        "detail pane beside a tree draws and the reason get_tree carries a one-line "
        "`preview` per row instead of a message. The pair is the same one the TUI's "
        "own browser makes: `tree()` for the rows, `entry` for the node it is "
        "showing (tree_browser.py's `_resolve_entry`). get_messages does not serve "
        "this — it answers for the ACTIVE PATH, and the node a reader has moved the "
        "browser's cursor onto is very often not on it. The entry is handed over "
        "RAW, in its stored camelCase shape, rather than projected: the caller is "
        "rendering one node, and a projection would be a second message shape to "
        "keep in step with get_messages'. Bounded by the caller: one id, one entry, "
        "and a host that wants ten asks ten times — the alternative, an ids array, "
        "buys nothing over stdio and invites a host to pull a whole tree's bodies "
        "in one line. Read-only: no D-1 turn_safety_guard, no E5 cursor (rule 2), "
        "no require_durable_session (D-7 rule 2)."
    ),
    params_schema=GET_ENTRY_PARAMS_SCHEMA,
    result_schema=GET_ENTRY_RESULT_SCHEMA,
)
async def _handle_get_entry(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.conversation_tree import ConversationTree

    log = handler.session.session_log
    tree = ConversationTree(log.entries(), log.cursor)
    entry_id = params["entry_id"]
    try:
        entry = tree.entry(entry_id)
    except KeyError as exc:
        raise RPCError(INVALID_PARAMS, f"no entry with id {entry_id!r}", data=dict(params)) from exc
    return {"entry": entry}


### end tier-c:get_entry

### begin tier-c:get_pending_request

GET_PENDING_REQUEST_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_pending_request")


def _request_payload(request: Any) -> dict[str, Any]:
    """One :class:`ExtensionRequest`, projected onto the wire.

    `label` and `extension_name` are properties rather than fields, and both are
    sent: a host recomputing τ's four-state framing line from `lock` and `ask`
    would be a second copy of the one table `docs/EXTENSION-LOCKS.md` §9 owns.
    """
    return {
        "entry_id": request.entry_id,
        "extension": request.extension,
        "extension_name": request.extension_name,
        "sentence": request.sentence,
        "label": request.label,
        "lock": request.lock,
        "ask": request.ask,
        "release": request.release,
    }


@command(
    "get_pending_request",
    tier="C",
    since="0.10.1",
    notes=(
        "AgentSession.pending_request, projected. 0.10.0 replaced ui.confirm / "
        "ui.select / ui.input with one persisted `extension_request` entry and "
        "said every head renders all four of its states — and the state was "
        "reachable over THIS wire only as `SUBMISSION_REJECTED` data, which means "
        "an RPC host learned about a lock by being refused by it and could not "
        "see one that had not refused it yet. A head polls this at every cursor "
        "move: after a turn ends, after a command, on a resume, on a session "
        "switch. Null is the ordinary answer and is not a failure. Read-only: no "
        "D-1 turn_safety_guard (a request raised by a tool_call hook is exactly "
        "the case a host wants to see mid-turn), no E5 cursor (rule 2), no "
        "require_durable_session (D-7 rule 2)."
    ),
    params_schema=params_schema_for("get_pending_request"),
    result_schema=GET_PENDING_REQUEST_RESULT_SCHEMA,
)
async def _handle_get_pending_request(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    request = handler.session.pending_request
    return {"request": None if request is None else _request_payload(request)}


### end tier-c:get_pending_request

### begin tier-c:answer_request

ANSWER_REQUEST_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {
            "type": "string",
            "description": (
                "The request's `entry_id`, as get_pending_request reported it or as "
                "a SUBMISSION_REJECTED refusal carried it."
            ),
        },
        "action": {
            "type": "string",
            "description": (
                "The pressed action's `label` — one of the labels in the ask's "
                "`actions`. The label, not the command it names: the label is what "
                "a person chose and what the ask's own table is keyed by."
            ),
        },
        "values": {
            "type": "object",
            "description": (
                "The filled fields, keyed by field name. Omitted is the empty dict, "
                "which is what an ask declaring no fields takes. Checked against "
                "the ask's declared fields before anything is appended: nothing is "
                "coerced and no partial answer is persisted."
            ),
        },
    },
    "additionalProperties": False,
    "required": ["request_id", "action"],
}

ANSWER_REQUEST_RESULT_SCHEMA: dict[str, Any] = result_schema_for("answer_request")


@command(
    "answer_request",
    tier="C",
    since="0.10.1",
    notes=(
        "AgentSession.answer_request, projected — the write half of the pair "
        "get_pending_request reads. Without it an RPC host could SEE a lock and "
        "not release one, which makes a locked session a session that host can "
        "never continue; the TUI and the REPL both had the release and this wire "
        "did not. The append happens BEFORE the dispatch, which is what releases "
        "the lock first: the handler then runs on a session that is already "
        "unlocked and may submit a turn of its own. `handled: false` is a WARNING "
        "and not a failure — the extension was not loaded, the response was "
        "appended anyway and the lock is gone. TWO GUARDS THIS DOES NOT TAKE, both "
        "stated rather than omitted. It takes no D-1 turn_safety_guard, for two "
        "reasons that compound: a request is very often RAISED by a tool_call hook "
        "inside a turn (docs/EXTENSION-LOCKS.md), so answering mid-turn is the "
        "designed case and not a race — and the dispatched action may itself "
        "submit, which under a held turn_lock would deadlock against the lock this "
        "verb was holding. The TUI and the REPL call the same method with no lock. "
        "It takes no D-7 require_durable_session either, which is the one "
        "deliberate exception to 'the verb that appends refuses': the append's "
        "product here is a RELEASED LOCK in this process, and refusing would leave "
        "an unpersisted session locked with no way out at all — strictly worse "
        "than the promise D-7 exists to stop being made, and the same reasoning "
        "`handled: false` already applies to an absent extension. Refuses with "
        "INVALID_PARAMS, before any append: an unknown request id, a request "
        "carrying no ask (a bare lock is cleared by navigating or by its own "
        "command, not answered), an action label the ask does not declare, or "
        "values its fields reject."
    ),
    params_schema=ANSWER_REQUEST_PARAMS_SCHEMA,
    result_schema=ANSWER_REQUEST_RESULT_SCHEMA,
)
async def _handle_answer_request(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        result = await handler.session.answer_request(
            params["request_id"], params["action"], params.get("values")
        )
    except ValueError as exc:
        raise RPCError(INVALID_PARAMS, str(exc), data=dict(params)) from exc
    return {
        "handled": result.handled,
        "output": result.output,
        "cursor": handler.session.session_log.cursor,
    }


### end tier-c:answer_request

LIST_MANAGED_EXTENSIONS_RESULT_SCHEMA: dict[str, Any] = result_schema_for("list_managed_extensions")


@command(
    "list_managed_extensions",
    tier="C",
    since="0.9.8",
    notes=(
        "The extension_name domain's enumerator, addressed by its own capability "
        "name: AgentSession.list_managed_extensions(), projected. Overlaps "
        'enumerate_domain {"domain": "extension_name"} the way '
        "complete_message_id overlaps its own domain — with one difference worth a "
        "host's attention: enumerate_domain has only value/label to work with, so "
        "it renders enabled-ness INTO the label ('path (disabled)'), and this verb "
        "hands back the boolean. A host deciding whether to offer enable or "
        "disable wants this one. Read-only: no turn_safety_guard, no `cursor` (E5 "
        "rule 2), no require_durable_session (D-7 rule 2). Answers only about "
        "MANAGED file extensions — a file that failed to import is not here, "
        "because it can never be a legal extension_name; get_extension_state is "
        "the read that shows it."
    ),
    params_schema=params_schema_for("list_managed_extensions"),
    result_schema=LIST_MANAGED_EXTENSIONS_RESULT_SCHEMA,
)
async def _handle_list_managed_extensions(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    return {
        "extensions": [
            {"path": path, "enabled": enabled}
            for path, enabled in handler.session.list_managed_extensions()
        ]
    }


### end tier-c:list_managed_extensions

GET_EXTENSION_STATE_RESULT_SCHEMA: dict[str, Any] = result_schema_for("get_extension_state")


@command(
    "get_extension_state",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.get_extension_state(), projected through "
        "sdk.summarize_extensions — the read the /extensions listing is built "
        "from, and the read a head needs before it can offer enable/disable/reload "
        "as anything but a blind form. Whether each extension is currently ENABLED "
        "is the separate read list_managed_extensions; a host that wants both "
        "composes them, which is the division AgentSession.get_extension_state's "
        "own docstring states. Read-only: no turn_safety_guard, no `cursor` (E5 "
        "rule 2), no require_durable_session (D-7 rule 2 — extension state is "
        "runtime state and is never appended to the session log, so this answers "
        "the same on a persisted and an unpersisted session)."
    ),
    params_schema=params_schema_for("get_extension_state"),
    result_schema=GET_EXTENSION_STATE_RESULT_SCHEMA,
)
async def _handle_get_extension_state(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core.sdk import summarize_extensions

    state = handler.session.get_extension_state()
    return {
        "extensions": [
            {
                "name": info.name,
                "path": info.path,
                "tools": list(info.tools),
                "commands": list(info.commands),
                "shortcuts": list(info.shortcuts),
                "hooks": list(info.hooks),
                "content_hash": info.content_hash,
                "subjects": list(info.subjects),
            }
            for info in summarize_extensions(state)
        ],
        "errors": [{"path": err.path, "error": err.error} for err in state.errors],
    }


### end tier-c:get_extension_state


@asynccontextmanager
async def tree_mutation_guard(
    handler: "RPCHandler", *, verb: str, appenders: tuple[str, ...]
) -> AsyncIterator["AgentSession"]:
    """The four things every tree-mutating verb does around its one core call.

    D-1 (`turn_safety_guard`): a tree mutation rewrites what the next turn will be
    sent, so it must not land while a turn is reading that same path.

    D-7 rule 1 (`require_durable_session`): all five of these APPEND, so all five
    refuse an unpersisted session before touching anything. A tree edit that is
    lost with the process is worse than the same promise `set_model` refuses to
    make — the host has re-shaped a conversation it can never load again.

    `require_log_appender` for each name the operation will call, checked BEFORE
    the first append rather than discovered halfway through: `commit_branch`
    writes through three appenders, and finding out about the third after the
    first two have landed would leave a half-built branch.

    A `ValueError` out of `tau_agent_core.tree_ops` becomes `INVALID_PARAMS`. Every
    one of them is a caller error the schema cannot check syntactically — an
    unknown id, a resume point that is not an ancestor, a selection that composes
    no turn-complete path — and `tree_ops` checks all of them before the first
    append, so the refusal is total and the log is byte-identical.

    Args:
        handler: The RPC handler, for its bound session.
        verb: The verb's name, for the refusal messages.
        appenders: The `SessionLog` methods this operation will call.

    Yields:
        The bound session, with `turn_lock` held.
    """
    session = handler.session
    async with turn_safety_guard(session):
        require_durable_session(session, verb=verb)
        for appender in appenders:
            require_log_appender(session, appender, verb=verb)
        try:
            yield session
        except ValueError as exc:
            raise RPCError(INVALID_PARAMS, str(exc)) from exc


_TREE_MUTATION_NOTES = (
    "D-1: guarded by turn_safety_guard, so this refuses with TURN_STILL_RUNNING "
    "rather than re-shaping the path an in-flight turn is being run against. "
    "D-7 rule 1: it APPENDS, so require_durable_session refuses an unpersisted "
    "session (SESSION_NOT_PERSISTED) before anything is touched — a tree edit "
    "that dies with the process leaves a host holding a conversation it can "
    "never load again. E5 rule 1: the completion carries the resulting `cursor`. "
    "Refuses: every caller error tau_agent_core.tree_ops raises — an unknown id "
    "above all — comes back as INVALID_PARAMS, checked before the first append, "
    "so a refusal leaves the log byte-identical. WHERE the entries land and for "
    "how long is set_model's own note: a --mode rpc child defaults to a private "
    "<tmp>/.tau-<uid>/sessions, so durability is bounded by machine uptime "
    "unless the host passed --session-dir DIR."
)

NAVIGATE_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "navigate",
    overrides={
        "target_id": {
            "description": (
                "The entry to move the cursor onto — an `entry_id` from "
                "complete_message_id. The abandoned branch drops out of context "
                "via the parentId walk but stays on disk and stays browsable; "
                "nothing is erased."
            ),
        },
    },
)

TREE_CONTEXT_RESULT_SCHEMA: dict[str, Any] = result_schema_for("navigate")


@command(
    "navigate",
    tier="C",
    since="0.9.8",
    notes=(
        "tau_agent_core.tree_ops.navigate, projected. Moves the session cursor to "
        "an entry and hands back the context that produces. Zero model calls: it "
        "appends one `navigate` entry. A target_id that is already the cursor is a "
        "no-op that still returns the context, so a host need not check first. "
        "Until this verb τ's differentiating feature — a session tree a caller can "
        "move around in — was reachable only from inside the Textual head "
        "(docs/VSCODE-HEAD.md §6). " + _TREE_MUTATION_NOTES
    ),
    params_schema=NAVIGATE_PARAMS_SCHEMA,
    result_schema=result_schema_for("navigate"),
)
async def _handle_navigate(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core import tree_ops

    async with tree_mutation_guard(handler, verb="navigate", appenders=("append_navigate",)) as s:
        messages = tree_ops.navigate(s.session_log, params["target_id"])
        return {"messages": messages, "cursor": s.session_log.cursor}


### end tier-c:navigate

SUMMARIZE_AND_NAVIGATE_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "summarize_and_navigate",
    overrides={
        "target_id": {
            "description": (
                "The branch point. The subtree BELOW it is what gets summarized, "
                "and the branch_summary entry is parented at it."
            ),
        },
        "custom_instructions": {
            "description": (
                "Extra guidance for the summarizer's SYSTEM prompt. Omitted runs "
                "the default summarizer prompt."
            ),
        },
    },
)


@command(
    "summarize_and_navigate",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.summarize_and_navigate(), projected. The summarizing arm of "
        "navigate, and a SEPARATE verb rather than a flag on it for the reason the "
        "core splits them: this one makes a completion call, so it costs tokens "
        "and takes wall time that `navigate` does not. A host offering both should "
        "say so in what it offers. The summarizer's tokens are banked to the "
        "session's side ledger (AgentSession.record_side_usage) and are NOT "
        "itemised in this response — stated, not hidden: there is no verb on this "
        "wire that reports side_usage, so a host tracking spend sees them only in "
        "aggregate. A summarizer that returns nothing usable RAISES "
        "(session_manager.summarize_branch) and reaches the host as INTERNAL_ERROR "
        "— it is a runtime failure, not a bad argument, and it is never fabricated "
        "into an empty summary. " + _TREE_MUTATION_NOTES
    ),
    params_schema=SUMMARIZE_AND_NAVIGATE_PARAMS_SCHEMA,
    result_schema=result_schema_for("summarize_and_navigate"),
)
async def _handle_summarize_and_navigate(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    async with tree_mutation_guard(
        handler,
        verb="summarize_and_navigate",
        appenders=("append_navigate", "append_branch_summary"),
    ) as s:
        messages = await s.summarize_and_navigate(
            params["target_id"],
            custom_instructions=params.get("custom_instructions"),
        )
        return {"messages": messages, "cursor": s.session_log.cursor}


### end tier-c:summarize_and_navigate

ELIDE_SPAN_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "elide_span",
    overrides={
        "anchor_id": {
            "description": (
                "The entry the fold jumps FROM. The elide entry is appended as its "
                "child, so the anchor becomes the end of the kept region and the "
                "new tip."
            ),
        },
        "first_kept_id": {
            "description": (
                "The entry the fold resumes at — the anchor itself or one of its "
                "ANCESTORS, never a descendant. Everything on the path before it "
                "is the elided span. A resume point the fold's scan cannot reach "
                "would empty the context silently, which is why the wrong "
                "direction is refused rather than tolerated."
            ),
        },
    },
)


@command(
    "elide_span",
    tier="C",
    since="0.9.8",
    notes=(
        "tau_agent_core.tree_ops.elide_span, projected. Folds a span out of the "
        "active context — the summary-less generalization of the compaction "
        "anchor. Synchronous and free: no summary, therefore no model call. "
        "Nothing is erased; every entry the fold now skips is still in the log and "
        "still browsable. Two refusals beyond the ordinary unknown-id one, both "
        "INVALID_PARAMS and both checked before the first append: a first_kept_id "
        "that is not on the anchor's path (the fold's forward scan would never "
        "find it and would emit the anchor and nothing else), and a span that "
        "would hide nothing (a persisted node that changes nothing about the "
        "context it was created to change is indistinguishable to a user from a "
        "successful fold). " + _TREE_MUTATION_NOTES
    ),
    params_schema=ELIDE_SPAN_PARAMS_SCHEMA,
    result_schema=result_schema_for("elide_span"),
)
async def _handle_elide_span(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core import tree_ops

    async with tree_mutation_guard(
        handler, verb="elide_span", appenders=("append_navigate", "append_elide")
    ) as s:
        messages = tree_ops.elide_span(s.session_log, params["anchor_id"], params["first_kept_id"])
        return {"messages": messages, "cursor": s.session_log.cursor}


### end tier-c:elide_span

COMMIT_BRANCH_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "commit_branch",
    overrides={
        "ids": {
            "description": (
                "The marked entry ids, in any order — tree_surgery puts them into "
                "tree order. The longest run that is already an ancestor chain is "
                "kept in place; the rest are minted as copies parented under it, "
                "so nothing is re-parented and nothing is erased."
            ),
        },
        "drop_context": {
            "description": (
                "Whether the branch keeps ONLY the selection. true appends an "
                "elide resuming at the root-most mark, so the context becomes the "
                "system prompt plus the branch; false leaves everything above the "
                "attach point in context."
            ),
        },
    },
)


@command(
    "commit_branch",
    tier="C",
    since="0.9.8",
    notes=(
        "tau_agent_core.tree_ops.commit_branch, projected. Builds a branch out of "
        "a set of marked entries and continues on it "
        "(docs/TREE-BROWSER-AS-EDITOR.md §6). The copies are minted with append_at, "
        "which does NOT move the leaf, and the leaf moves onto the last minted "
        "entry afterwards — so the commit is atomic from the cursor's point of "
        "view and a mint that fails partway leaves orphans hanging off the attach "
        "point rather than a half-moved conversation. Refuses, all INVALID_PARAMS "
        "and all before the first append: an empty selection, an unknown id, an "
        "entry no branch can carry, or a selection composing a path that is not "
        "turn-complete. " + _TREE_MUTATION_NOTES
    ),
    params_schema=COMMIT_BRANCH_PARAMS_SCHEMA,
    result_schema=result_schema_for("commit_branch"),
)
async def _handle_commit_branch(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core import tree_ops

    async with tree_mutation_guard(
        handler,
        verb="commit_branch",
        appenders=("append_navigate", "append_at", "append_elide"),
    ) as s:
        messages = tree_ops.commit_branch(
            s.session_log, params["ids"], drop_context=params["drop_context"]
        )
        return {"messages": messages, "cursor": s.session_log.cursor}


### end tier-c:commit_branch

PASTE_SUBTREE_PARAMS_SCHEMA: dict[str, Any] = params_schema_for(
    "paste_subtree",
    overrides={
        "source_id": {"description": "The copied node — the root of the subtree."},
        "target_id": {
            "description": (
                "The entry the copy hangs from. May not be inside the source's own subtree."
            ),
        },
    },
)

PASTE_SUBTREE_RESULT_SCHEMA: dict[str, Any] = result_schema_for("paste_subtree")


@command(
    "paste_subtree",
    tier="C",
    since="0.9.8",
    notes=(
        "tau_agent_core.tree_ops.paste_subtree, projected "
        "(docs/TREE-BROWSER-AS-EDITOR.md §7). Every copied entry is a new entry "
        "carrying `copiedFrom`, minted with append_at, parents before children, "
        "with a source-to-new id map re-hanging each child under its copied parent "
        "— so the copy keeps the original's shape including its forks. The one "
        "tree mutation whose result is NOT a message list: the leaf never moves, "
        "so what the model sees changes only when someone navigates onto the copy. "
        "Refuses: an unknown id, a source whose kind cannot be copied, a target "
        "inside the source's own subtree, or a copied tool result whose call is on "
        "neither the target's path nor the copied run. " + _TREE_MUTATION_NOTES
    ),
    params_schema=PASTE_SUBTREE_PARAMS_SCHEMA,
    result_schema=PASTE_SUBTREE_RESULT_SCHEMA,
)
async def _handle_paste_subtree(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    from tau_agent_core import tree_ops

    async with tree_mutation_guard(handler, verb="paste_subtree", appenders=("append_at",)) as s:
        minted = tree_ops.paste_subtree(s.session_log, params["source_id"], params["target_id"])
        return {"minted_ids": minted, "cursor": s.session_log.cursor}


### end tier-c:paste_subtree


EXTENSION_ACTION_RESULT_SCHEMA: dict[str, Any] = result_schema_for("enable_extension")

_EXTENSION_MUTATION_NOTES = (
    "D-1: guarded by turn_safety_guard. An extension's hooks fire inside the turn, "
    "and its tools are resolved from the registry this action rewrites, so running "
    "it mid-turn would change the tool table under a loop that had already read it "
    "— TURN_STILL_RUNNING rather than that race. D-7 rule 2: appends NOTHING, so no "
    "require_durable_session; extension state is runtime state and is never written "
    "to the session log, which is also why it does not survive a respawn and a host "
    "that wants an extension loaded at startup passes it on the command line. E5 "
    "rule 1: the completion carries `cursor` anyway. `path` accepts a full managed "
    "path or a unique file stem (AgentSession.resolve_extension_target); an "
    "ambiguous stem resolves to nothing and comes back as ok=false, never a guess."
)


def _extension_action_result(
    outcome: "ExtensionActionResult", session: "AgentSession"
) -> dict[str, Any]:
    """One :class:`ExtensionActionResult` plus the E5 cursor, as the three verbs report it.

    Written once because all three actions return the same record and E5 rule 1
    applies to all three identically; three copies of this projection is how the
    tier's own findings 5 and 6 started.
    """
    return {
        "action": outcome.action,
        "path": outcome.path,
        "ok": outcome.ok,
        "message": outcome.message,
        "cursor": session.session_log.cursor,
    }


@command(
    "enable_extension",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.enable_extension(), projected. Re-binds a DISABLED extension "
        "by re-invoking the stored register(api) — the same entry point the loader "
        "called — against a fresh runner bucket, then fires session_start with "
        "reason 'enable' so a watcher re-installs. It does not re-read the file; "
        "reload_extension is the verb that does. ok=false for an unknown target or "
        "one that is already enabled. " + _EXTENSION_MUTATION_NOTES
    ),
    params_schema=params_schema_for(
        "enable_extension",
        overrides={
            "path": {
                "description": (
                    "The managed extension to enable — a `path` from "
                    "list_managed_extensions, or a unique file stem."
                ),
            },
        },
    ),
    result_schema=result_schema_for("enable_extension"),
)
async def _handle_enable_extension(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    async with turn_safety_guard(session):
        return _extension_action_result(await session.enable_extension(params["path"]), session)


### end tier-c:enable_extension


@command(
    "disable_extension",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.disable_extension(), projected. Fires the extension's own "
        "session_shutdown with reason 'disable' FIRST — the teardown seam, so a "
        "watcher or exit-commit runs cleanly — then removes its runner bucket and "
        "unwinds the tools, commands and shortcuts it registered. The LoadedExtension "
        "record is KEPT, which is what lets enable_extension bring it back without "
        "re-reading the file. ok=false for an unknown target or one that is already "
        "disabled. " + _EXTENSION_MUTATION_NOTES
    ),
    params_schema=params_schema_for(
        "disable_extension",
        overrides={
            "path": {
                "description": (
                    "The managed extension to disable — a `path` from "
                    "list_managed_extensions, or a unique file stem."
                ),
            },
        },
    ),
    result_schema=result_schema_for("disable_extension"),
)
async def _handle_disable_extension(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    async with turn_safety_guard(session):
        return _extension_action_result(await session.disable_extension(params["path"]), session)


### end tier-c:disable_extension


@command(
    "reload_extension",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.reload_extension(), projected. Tears the current instance "
        "down, RE-IMPORTS the file from disk as a new module object — so edits on "
        "disk take effect — and re-registers it against a fresh bucket. The one "
        "verb of the three that can hit a hard failure: a file that no longer "
        "imports RAISES out of the action and reaches the host as INTERNAL_ERROR, "
        "with the extension left torn down. That is Fail-Early and deliberate — an "
        "ok=false there would report a no-op for a session whose extension is now "
        "gone. Same argument shape as enable/disable, different risk, which is why "
        "it is a third verb and not a mode of one of them. " + _EXTENSION_MUTATION_NOTES
    ),
    params_schema=params_schema_for(
        "reload_extension",
        overrides={
            "path": {
                "description": (
                    "The managed extension to re-import — a `path` from "
                    "list_managed_extensions, or a unique file stem."
                ),
            },
        },
    ),
    result_schema=result_schema_for("reload_extension"),
)
async def _handle_reload_extension(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    async with turn_safety_guard(session):
        return _extension_action_result(await session.reload_extension(params["path"]), session)


### end tier-c:reload_extension


@command(
    "get_extension_config",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.get_extension_config(), projected. The read a settings "
        "screen is built from: `schema` is the extension's own CONFIG_SCHEMA "
        "module attribute, validated at load into the {title, fields} shape "
        "ui.form takes, and `values` is the live slice api.config returns for it. "
        "A null `schema` is the honest answer for an extension that declares none "
        "— a host renders no settings screen rather than an empty one. Read: no "
        "cursor (E5 rule 2), no turn guard. Fail-Early: an unresolvable `path` "
        "RAISES here rather than returning a null row, because unlike the "
        "enable/disable/reload verbs there is no ok=false channel on a read."
    ),
    params_schema=params_schema_for(
        "get_extension_config",
        overrides={
            "path": {
                "description": (
                    "The managed extension to read — a `path` from "
                    "list_managed_extensions, or a unique file stem."
                ),
            },
        },
    ),
    result_schema=result_schema_for("get_extension_config"),
)
async def _handle_get_extension_config(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        return handler.session.get_extension_config(params["path"])
    except ValueError as err:
        raise RPCError(INVALID_PARAMS, str(err)) from err


### end tier-c:get_extension_config


@command(
    "set_extension_config",
    tier="C",
    since="0.9.8",
    notes=(
        "AgentSession.set_extension_config(), projected. Replaces the slice "
        "wholesale — not a merge — after checking every value against the "
        "declared schema, then reloads the extension, because api.config is "
        "captured when the extension's API is bound and a slice written without a "
        "reload would be read by nobody. An undeclared key, a missing declared "
        "field, or a value whose type does not match its field's kind reaches the "
        "host as INVALID_PARAMS; so does an extension that declares no schema, "
        "since there is then no contract to check against. `values` is why this "
        "row's params are hand-written: its keys are whatever THIS extension "
        "declared, which is not sayable in the Argument vocabulary — the same "
        "reason `submit` carries arguments=None. Applies for this session only: "
        "the core does not own ~/.tau/config.json, so persisting is head-local. "
        + _EXTENSION_MUTATION_NOTES
    ),
    params_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "The managed extension to configure — a `path` from "
                    "list_managed_extensions, or a unique file stem."
                ),
            },
            "values": {
                "type": "object",
                "description": (
                    "The complete new slice, keyed by the field names "
                    "get_extension_config's `schema` declares. Every declared "
                    "field must be present: missing is not empty, and nothing is "
                    "filled in for you."
                ),
            },
        },
        "required": ["path", "values"],
    },
    result_schema=result_schema_for("set_extension_config"),
)
async def _handle_set_extension_config(
    handler: "RPCHandler", msg_id: int | None, params: dict[str, Any]
) -> dict[str, Any]:
    session = handler.session
    async with turn_safety_guard(session):
        try:
            outcome = await session.set_extension_config(params["path"], params["values"])
        except ValueError as err:
            raise RPCError(INVALID_PARAMS, str(err)) from err
        return _extension_action_result(outcome, session)


### end tier-c:set_extension_config


_RESULT_FAMILIES: dict[str, tuple[str, ...]] = {
    "SESSION_LIFECYCLE_RESULT_SCHEMA": ("new_session", "fork", "switch_session"),
    "TREE_CONTEXT_RESULT_SCHEMA": (
        "navigate",
        "summarize_and_navigate",
        "elide_span",
        "commit_branch",
    ),
    "EXTENSION_ACTION_RESULT_SCHEMA": (
        "enable_extension",
        "disable_extension",
        "reload_extension",
        "set_extension_config",
    ),
}


def _check_result_families() -> None:
    """The three named aliases above must describe every verb that shares them.

    Each row derives its own `result_schema` from its own capability, so the aliases
    are only names for a shape three or four capabilities happen to declare
    identically — and `docs/RPC-PROTOCOL.md` and two tests quote them as if that
    identity held. Checked at import so a capability that drifts out of its family
    fails here rather than leaving a doc sentence quietly describing a shape one of
    its verbs no longer returns.
    """
    for alias, members in _RESULT_FAMILIES.items():
        shared = globals()[alias]
        for member in members:
            if result_schema_for(member) != shared:
                raise ValueError(
                    f"{alias} no longer describes {member!r}: its capability declares a "
                    "different `returns`. Either give it its own alias or restore the shared "
                    "declaration in capabilities.py."
                )


_check_result_families()
