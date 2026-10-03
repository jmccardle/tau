"""The ``tau serve`` wire protocol: every message, defined once (docs/TAU-SERVE.md §5).

One JSON object per WebSocket text frame. A client sends requests, each with an
``id`` it chose; the daemon answers each with one :class:`Response`, and pushes
:class:`Event` frames for every session the client is attached to.

The dataclasses here are the definition. :func:`json_schema` derives JSON Schema
from them, ``scripts/generate_serve_protocol.py`` writes it with
``docs/SERVE-PROTOCOL.md``, and tau-code generates its TS types from that schema.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin, get_type_hints

PROTOCOL_VERSION = "0.2"
"""``MAJOR.MINOR``. Below 1.0 any bump may break a client, and the hello refuses a mismatch."""

DEFAULT_PORT = 8256
"""The port ``tau serve`` listens on and ``tau --connect HOST`` dials ("ffwf" in base64, as decimal)."""


@dataclass
class Hello:
    """The first request on every connection; nothing else is served before it.

    Attributes:
        protocol: The client's :data:`PROTOCOL_VERSION`; a different one is refused.
        client: What the client is, for the daemon's log (``tui``, ``tail``, ``web``).
        token: The shared secret, required only when the daemon's config sets
            ``serve.token``. A client reads it from ``TAUD_TOKEN``.
    """

    protocol: str
    client: str
    token: str | None = None
    type: Literal["hello"] = "hello"


@dataclass
class ListSessions:
    """Every session in the daemon's store, across every cwd, newest first."""

    type: Literal["list_sessions"] = "list_sessions"


@dataclass
class CreateSession:
    """Create a session in ``cwd``, a path on the daemon's machine.

    Attributes:
        cwd: The directory its tools run in; the create fails if it does not exist.
        model: A model name from the daemon's config, or ``None`` for its default.
        name: A session name, or ``None``.
    """

    cwd: str
    model: str | None = None
    name: str | None = None
    type: Literal["create_session"] = "create_session"


@dataclass
class Attach:
    """Start receiving a session's events, loading it in the daemon if needed.

    Attributes:
        session_id: The session, or an unambiguous prefix of its id.
        epoch: The ``epoch`` of an earlier attach, to resume rather than re-read.
        since: The last ``seq`` the client applied under that epoch.
    """

    session_id: str
    epoch: str | None = None
    since: int | None = None
    type: Literal["attach"] = "attach"


@dataclass
class Detach:
    """Stop receiving a session's events. The session keeps running."""

    session_id: str
    type: Literal["detach"] = "detach"


@dataclass
class Submit:
    """Send text to a cursor: a prompt, or a ``/command`` when ``expand_commands``.

    Answered when the submission ends, with its :class:`SubmitResult`.

    Attributes:
        multitask_strategy: What to do when the cursor is busy (docs/SUBMISSION-LIFECYCLE.md).
        submission_id: The id the events of this submission carry; the daemon
            mints one when ``None``. A client that renders its own streams sends it.
        images: Image content blocks to send with the text.
    """

    session_id: str
    cursor_id: str
    text: str
    multitask_strategy: Literal["enqueue", "reject", "steer", "follow_up"] = "enqueue"
    expand_commands: bool = True
    submission_id: str | None = None
    images: list[dict[str, Any]] | None = None
    type: Literal["submit"] = "submit"


@dataclass
class Abort:
    """Abort the turn running on a cursor, and the cursors it owns."""

    session_id: str
    cursor_id: str
    type: Literal["abort"] = "abort"


@dataclass
class OpenCursor:
    """Open a cursor at ``leaf`` (an entry id, or ``None`` before the root)."""

    session_id: str
    leaf: str | None
    label: str = ""
    owner_id: str | None = None
    type: Literal["open_cursor"] = "open_cursor"


@dataclass
class CloseCursor:
    """Close a cursor this session opened; its branch stays in the tree."""

    session_id: str
    cursor_id: str
    type: Literal["close_cursor"] = "close_cursor"


@dataclass
class MoveCursor:
    """Move a cursor to ``leaf``. Writes nothing."""

    session_id: str
    cursor_id: str
    leaf: str | None
    type: Literal["move_cursor"] = "move_cursor"


@dataclass
class SetModel:
    """Switch the model a cursor's next turn runs, by a name from the daemon's config."""

    session_id: str
    cursor_id: str
    model: str
    type: Literal["set_model"] = "set_model"


@dataclass
class Answer:
    """Answer an extension's form (a ``request`` event); the first answer wins.

    Attributes:
        value: The ``{field: value}`` answers, or ``None`` to cancel the form.
    """

    session_id: str
    request_id: str
    value: dict[str, Any] | None
    type: Literal["answer"] = "answer"


@dataclass
class AnswerRequest:
    """Answer an extension request written in the tree (docs/EXTENSION-LOCKS.md §3).

    Attributes:
        request_id: The request entry's id.
        action: The label of the pressed action.
        values: The filled fields, keyed by name.
    """

    session_id: str
    cursor_id: str
    request_id: str
    action: str
    values: dict[str, Any] = field(default_factory=dict)
    type: Literal["answer_request"] = "answer_request"


@dataclass
class Perform:
    """Call one of the session backend's operations, acting at ``cursor_id``.

    The TUI's commands reach the backend by method name; under ``--connect`` that
    backend is the daemon's. Answered with a :class:`Performed`-shaped record or a
    plain value, tagged by ``kind``.

    Attributes:
        cursor_id: The cursor the operation acts on: the tree edits move and
            append at it, and ``rollback_turn`` runs its turn there.
        method: One of :data:`PERFORMABLE`.
        arguments: Its keyword arguments.
    """

    session_id: str
    cursor_id: str
    method: str
    arguments: dict[str, Any] = field(default_factory=dict)
    type: Literal["perform"] = "perform"


PERFORMABLE = (
    "compact",
    "set_auto_compaction",
    "set_session_name",
    "enable_extension",
    "disable_extension",
    "reload_extension",
    "run_extension_command",
    "answer_request",
    "navigate_tree",
    "elide_span",
    "commit_branch",
    "paste_subtree",
    "rollback_turn",
    "list_managed_extensions",
)
"""The backend operations :class:`Perform` may name; anything else is refused."""


@dataclass
class Describe:
    """Re-read a session's extension surface, after an extension was enabled or reloaded."""

    session_id: str
    type: Literal["describe"] = "describe"


@dataclass
class Compare:
    """Open one cursor per model at ``leaf`` and send each the same text (docs/TAU-SERVE.md §8).

    Answered at once with ``{comparison_id, cursors: [{cursor_id, model}], message}``;
    the turns run on. Each turn's ``submission_start`` carries the comparison in
    ``submission.correlation["compare"]`` as ``{id, models, index, cursor_id}``, so
    every attached client can draw the columns. An unknown model fails before
    anything is opened.

    Attributes:
        models: Model names from the daemon's config, one cursor each; one may repeat.
        text: The prompt every cursor receives, never expanded as a command.
        leaf: The entry the cursors start from; ``None`` is the head cursor's leaf.
    """

    session_id: str
    models: list[str]
    text: str
    leaf: str | None = None
    type: Literal["compare"] = "compare"


@dataclass
class EndCompare:
    """End a comparison: the head cursor moves onto ``keep``'s leaf, and every compare cursor closes.

    Turns still running on the others are aborted first; every branch stays in
    the tree. ``keep`` ``None`` keeps none and leaves the head where it is. Fails
    with ``busy`` while the kept turn or the head's turn is running, changing
    nothing. Answered with ``{"leaf": <the head's leaf>}``.
    """

    session_id: str
    comparison_id: str
    keep: str | None
    type: Literal["end_compare"] = "end_compare"


@dataclass
class NextStep:
    """The next argument a flow needs, or the mutation it is ready for (RPC ``next_step``).

    Answered with ``{status: "step"|"ready", step, ready}``: ``step`` is a
    ``FlowStep`` as JSON ``{flow, argument, domain, cursor, bound}``, ``ready`` a
    ``Ready`` ``{flow, mutation, arguments}``.

    Attributes:
        flow: A command ``name`` whose ``flow`` is true in the :class:`Surface`.
        bound: The arguments bound so far; ``None`` or ``{}`` is the first step.
        leaf: The entry a scoped ``message_id`` argument is relative to, echoed back
            as the step's ``cursor`` for :class:`EnumerateDomain`.
    """

    session_id: str
    flow: str
    bound: dict[str, Any] | None = None
    leaf: str | None = None
    type: Literal["next_step"] = "next_step"


@dataclass
class EnumerateDomain:
    """The values legal for a domain right now (RPC ``enumerate_domain``).

    Answered with ``{domain, values: [{value, label}], total}``; ``total`` counts
    past ``limit``. ``path`` and ``session_id`` are read in the session's cwd.

    Attributes:
        domain: A domain name, as a step's ``domain.name`` gives it.
        scope: For ``message_id``: which entries are candidates; ``None`` is
            ``in_session``.
        leaf: The entry a scoped ``message_id`` is relative to; ``None`` is the
            head cursor's leaf.
        query: A prefix of the value, or a substring of the label; empty matches all.
        limit: The most values answered.
    """

    session_id: str
    domain: str
    scope: Literal["in_session", "ancestors_of_cursor", "descendants_of_cursor"] | None = None
    leaf: str | None = None
    query: str = ""
    limit: int = 50
    type: Literal["enumerate_domain"] = "enumerate_domain"


@dataclass
class CompletePath:
    """Complete the ``@path`` at ``offset`` in ``text`` against the session's cwd.

    Answered as RPC ``complete_path``: ``{completion: null}`` outside an ``@``
    token, else ``{completion: {start, end, token, matches: [{name, detail,
    is_dir}], total}}``. The paths are on the daemon's machine.

    Attributes:
        offset: The caret's character offset in ``text``.
    """

    session_id: str
    text: str
    offset: int
    type: Literal["complete_path"] = "complete_path"


@dataclass
class GetTree:
    """Every entry of a session's tree as a browser row, seen from one cursor.

    Answered with ``{nodes: [TreeRow, ...], leaf, count}``: ``leaf`` is the
    cursor's, and the one row whose ``is_leaf`` is true.
    """

    session_id: str
    cursor_id: str
    type: Literal["get_tree"] = "get_tree"


@dataclass
class ForkSession:
    """Copy a session into a new one in the same cwd; answered with ``{session_id}``.

    The source is unchanged and the client stays attached to it; it attaches to
    the new session to continue there.

    Attributes:
        at: The entry to fork at, copying only the path to it; ``None`` copies the
            whole tree.
    """

    session_id: str
    at: str | None
    type: Literal["fork_session"] = "fork_session"


REQUESTS: tuple[type, ...] = (
    Hello,
    ListSessions,
    CreateSession,
    Attach,
    Detach,
    Submit,
    Abort,
    OpenCursor,
    CloseCursor,
    MoveCursor,
    SetModel,
    Answer,
    AnswerRequest,
    Perform,
    Describe,
    Compare,
    EndCompare,
    NextStep,
    EnumerateDomain,
    CompletePath,
    GetTree,
    ForkSession,
)
"""Every request a client may send, by its ``type``."""


@dataclass
class SessionRow:
    """One line of :class:`ListSessions`' answer."""

    id: str
    cwd: str
    name: str | None
    modified: str
    message_count: int
    first_message: str
    loaded: bool


@dataclass
class CursorState:
    """A live cursor as clients see it; sent whenever one opens, moves, closes or changes busy."""

    cursor_id: str
    leaf: str | None
    label: str
    owner_id: str | None
    busy: bool
    model: str


@dataclass
class CommandInfo:
    """One slash command a session answers, as RPC ``get_commands`` lists it.

    Attributes:
        origin: ``builtin`` for τ's own, ``extension`` for one an extension registered.
        flow: Whether :class:`NextStep` steps it.
        hidden: A qualified extension name (``ext:pirate.speak``): it resolves,
            and a completion list leaves it out.
    """

    name: str
    description: str
    origin: Literal["builtin", "extension"]
    flow: bool
    hidden: bool


@dataclass
class ExtensionInfo:
    """One loaded extension and what it registered, as RPC ``get_extension_state`` lists it."""

    name: str
    path: str
    tools: list[str]
    commands: list[str]
    shortcuts: list[str]
    hooks: list[str]
    content_hash: str
    subjects: list[str]


@dataclass
class Surface:
    """What a session answers beyond its tree, which a head reads without a round trip.

    Attributes:
        commands: Every command, built-in and extension, in resolution order.
        command_args: Each extension command's argument hint, or ``None``.
        shortcuts: ``[key, command, args, description]`` per extension shortcut.
        extensions: ``[path, enabled]`` per managed extension.
        loaded: Every loaded extension and what it registered.
        load_errors: ``[path, error]`` per extension file that failed to load.
    """

    commands: list[CommandInfo]
    command_args: dict[str, str | None]
    shortcuts: list[list[str]]
    extensions: list[list[Any]]
    loaded: list[ExtensionInfo]
    load_errors: list[list[str]]


@dataclass
class ModelSpec:
    """What a config model name resolves to."""

    id: str
    provider: str
    context_window: int


@dataclass
class ModelRecord:
    """One model the daemon's config defines, as RPC ``get_models`` lists it."""

    name: str
    model: ModelSpec


@dataclass
class TreeRow:
    """One :class:`GetTree` row: RPC ``get_tree``'s node, with ``is_cursor`` named ``is_leaf``.

    Attributes:
        kind: The entry's ``type``.
        role: The message role; ``None`` on a bookkeeping entry.
        preview: The entry's first line.
        is_leaf: Whether this entry is the cursor's leaf.
        timestamp: Epoch milliseconds, or ``None``.
        first_kept_id: On a ``compaction`` or ``elide``, the oldest entry kept.
        from_id: On a ``branch_summary``, the branch head it summarizes.
        is_system: Whether this is the system prompt.
        tool_call_ids: The tool call ids an assistant message declares.
        tool_call_id: The call a tool result answers.
        copyable: Whether ``paste_subtree`` can take this entry as its source.
        estimated_tokens: An estimate of the entry's tokens, 0 when it holds no message.
    """

    entry_id: str
    parent_id: str | None
    kind: str
    role: str | None
    preview: str
    is_leaf: bool
    timestamp: int | None
    first_kept_id: str | None
    from_id: str | None
    is_system: bool
    tool_call_ids: list[str]
    tool_call_id: str | None
    copyable: bool
    estimated_tokens: int


@dataclass
class Attached:
    """The answer to :class:`Attach`.

    With ``entries`` set it is a snapshot: replace the replica with them. With
    ``entries`` ``None`` the client's replica is current to ``since``, and the
    events that follow (``seq > since``) are a replay.

    Attributes:
        epoch: Changes when the daemon restarts or reloads the session; a client
            holding another epoch must take a snapshot.
        seq: The newest event number folded into this answer.
        head_cursor_id: The cursor a client drives unless it opens its own.
        models: Every model the daemon's config defines, for a picker.
    """

    session_id: str
    epoch: str
    seq: int
    entries: list[dict[str, Any]] | None
    cursors: list[CursorState]
    head_cursor_id: str
    cwd: str
    models: list[ModelRecord]
    surface: Surface


@dataclass
class SubmitResult:
    """How a submission ended; a refusal is an answer, not an error.

    Attributes:
        command: When the text was a command, the dispatched arm as
            ``{"arm": "Performed"|"FlowStep"|"Ready"|"View", ...its fields}``. The
            daemon performs a ``Ready`` itself, except ``fork`` and
            ``switch_session``, which move a client and are the client's to perform.
    """

    accepted: bool
    submission_id: str
    reason: str | None = None
    command: dict[str, Any] | None = None


@dataclass
class Error:
    """Why a request failed. ``code`` is stable; ``message`` is for a human."""

    code: Literal[
        "bad_request",
        "unauthorized",
        "protocol_mismatch",
        "not_found",
        "busy",
        "failed",
    ]
    message: str


@dataclass
class Response:
    """The one answer to a request, matched by ``id``."""

    id: int
    ok: bool
    result: Any = None
    error: Error | None = None
    type: Literal["response"] = "response"


EVENT_KINDS = (
    "entry_open",
    "entry_final",
    "entry_append",
    "agent_event",
    "channel",
    "cursors",
    "request",
    "request_closed",
    "ui",
)
"""What an :class:`Event` carries in ``data``:

- ``entry_open`` / ``entry_final`` / ``entry_append``: ``{"entry": {...}}``, a log
  write. Apply by id, last write wins, keeping the first position (§4.1).
- ``agent_event``: an ``AgentEvent`` as JSON. Changes no state.
- ``channel``: ``{"name": ..., "payload": {...}}`` for ``submission_start``,
  ``submission_end`` and ``custom_message``. Changes no state.
- ``cursors``: ``{"cursors": [CursorState, ...]}``, the whole set after a change.
- ``request`` / ``request_closed``: ``{"request_id", "spec"}`` / ``{"request_id"}``,
  an extension form opened, then answered by some client.
- ``ui``: ``{"op": "notify"|"status"|"panel", ...}``, extension display calls.
"""


@dataclass
class Event:
    """A push for an attached session, numbered per session within an ``epoch``."""

    session_id: str
    epoch: str
    seq: int
    kind: Literal[
        "entry_open",
        "entry_final",
        "entry_append",
        "agent_event",
        "channel",
        "cursors",
        "request",
        "request_closed",
        "ui",
    ]
    data: dict[str, Any] = field(default_factory=dict)
    type: Literal["event"] = "event"


def to_wire(message: Any) -> dict[str, Any]:
    """A protocol dataclass as the JSON object it is sent as."""
    return dataclasses.asdict(message)


def parse_request(raw: dict[str, Any]) -> tuple[int, Any]:
    """Read a client frame into its request dataclass.

    Returns:
        The request ``id`` and the dataclass instance.

    Raises:
        ValueError: no integer ``id``, an unknown ``type``, or fields that do not fit.
    """
    request_id = raw.get("id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise ValueError("a request needs an integer 'id'")
    kind = raw.get("type")
    for cls in REQUESTS:
        if _type_tag(cls) == kind:
            fields = {k: v for k, v in raw.items() if k != "id"}
            known = {f.name for f in dataclasses.fields(cls)}
            unknown = sorted(set(fields) - known)
            if unknown:
                raise ValueError(f"{kind}: unknown field(s) {unknown}")
            try:
                return request_id, cls(**fields)
            except TypeError as exc:
                raise ValueError(f"{kind}: {exc}") from None
    raise ValueError(f"unknown request type {kind!r}")


def _type_tag(cls: type) -> str:
    """The ``type`` literal a protocol dataclass declares."""
    return str(get_args(get_type_hints(cls)["type"])[0])


def _schema_of(hint: Any, defs: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema for one annotation; a nested dataclass goes in ``defs``."""
    origin = get_origin(hint)
    if hint is Any:
        return {}
    if hint is type(None):
        return {"type": "null"}
    if hint is str:
        return {"type": "string"}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if origin is Literal:
        return {"enum": list(get_args(hint))}
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_schema_of(arg, defs) for arg in get_args(hint)]}
    if origin is list:
        (item,) = get_args(hint)
        return {"type": "array", "items": _schema_of(item, defs)}
    if origin is dict:
        _, value = get_args(hint)
        return {"type": "object", "additionalProperties": _schema_of(value, defs)}
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        name = hint.__name__
        if name not in defs:
            defs[name] = {}
            defs[name] = _object_schema(hint, defs)
        return {"$ref": f"#/$defs/{name}"}
    raise TypeError(f"no JSON Schema for annotation {hint!r}")


def _object_schema(cls: type, defs: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema for one dataclass: its fields, and which have no default."""
    hints = get_type_hints(cls)
    properties = {}
    required = []
    for f in dataclasses.fields(cls):
        properties[f.name] = _schema_of(hints[f.name], defs)
        no_default = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        if no_default or f.name == "type":
            required.append(f.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
    if cls.__doc__:
        schema["description"] = cls.__doc__.strip().split("\n", 1)[0]
    return schema


def json_schema() -> dict[str, Any]:
    """The whole protocol as one JSON Schema document.

    ``ClientFrame`` is any request plus its ``id``; ``ServerFrame`` is a response
    or an event. Every dataclass here is under ``$defs`` by its class name.
    """
    defs: dict[str, Any] = {}
    for cls in (
        *REQUESTS,
        Response,
        Event,
        SessionRow,
        CursorState,
        Attached,
        Surface,
        SubmitResult,
        TreeRow,
    ):
        _schema_of(cls, defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"tau serve protocol {PROTOCOL_VERSION}",
        "$defs": defs,
        "ClientFrame": {
            "oneOf": [
                {
                    "allOf": [
                        {"$ref": f"#/$defs/{cls.__name__}"},
                        {"type": "object", "properties": {"id": {"type": "integer"}}},
                    ],
                    "required": ["id"],
                }
                for cls in REQUESTS
            ]
        },
        "ServerFrame": {"oneOf": [{"$ref": "#/$defs/Response"}, {"$ref": "#/$defs/Event"}]},
    }
