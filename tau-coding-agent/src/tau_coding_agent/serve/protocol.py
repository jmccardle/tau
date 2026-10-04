"""The ``tau serve`` wire protocol: every message, defined once (docs/TAU-SERVE.md §5).

JSON-RPC 2.0, one message per WebSocket text frame. A client sends requests, each
with an ``id`` it chose; the daemon answers each with a :class:`Success` or a
:class:`Failure`, and sends an ``event`` notification carrying an :class:`Event`
for every session the client is attached to.

The definitions here are the protocol. Dataclasses are what the daemon builds and
sends. The shapes it shares with RPC (an entry, a message, a form, a verb's
records) are ``tau_agent_core.rpc.records``. :func:`json_schema` derives JSON
Schema from both with ``tau_agent_core.json_schema``,
``scripts/generate_serve_protocol.py`` writes it with ``docs/SERVE-PROTOCOL.md``,
and tau-code generates its TS types from that schema.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
)

from tau_agent_core.flows import FlowStep, Performed, Ready, View
from tau_agent_core.json_schema import Given, SchemaBuilder, Shape, Tagged, Named, summary
from tau_agent_core.rpc import capabilities
from tau_agent_core.rpc import commands as rpc
from tau_agent_core.rpc import dialect, records
from tau_agent_core.rpc.records import (
    AttachmentReport,
    CustomRoleMessage,
    Correlation,
    Entry,
    ExtensionInfo,
    ExtensionRequest,
    FormSpec,
    ModelRecord,
    PanelSpec,
    SessionScope,
    SessionTuple,
)
from tau_agent_core.rpc_event_schema import WireEvent
from tau_agent_core.submission import MultitaskStrategy, SubmissionSource

PROTOCOL_VERSION = "0.8"
"""``MAJOR.MINOR``. Below 1.0 any bump may break a client, and the hello refuses a mismatch."""

DEFAULT_PORT = 8256
"""The port ``tau serve`` listens on and ``tau --connect HOST`` dials ("ffwf" in base64, as decimal)."""

JSONRPC = "2.0"
"""The ``jsonrpc`` member every message carries."""

EVENT_METHOD = "event"
"""The notification method an :class:`Event` is sent under; RPC's events use the same name."""

RequestId = int | str
"""A request ``id``: JSON-RPC allows a number or a string, and the answer echoes it."""


# ─── Requests ────────────────────────────────────────────────────────────


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
    """Every session in the daemon's store, across every cwd, newest first (RPC ``list_sessions``)."""

    type: Literal["list_sessions"] = "list_sessions"


@dataclass
class NewSession:
    """Create a session in ``cwd``, a path on the daemon's machine, and load it (RPC ``new_session``).

    A connection has no current session to replace, so nothing moves: attach to
    the answer's ``session.session_id`` to drive it. RPC's ``persist`` is absent,
    because every daemon session is stored.

    Attributes:
        cwd: The directory its tools run in; the create fails if it does not exist.
        model: A model name from the daemon's config, or ``None`` for its default.
        name: A session name, or ``None``.
    """

    cwd: str
    model: str | None = None
    name: str | None = None
    type: Literal["new_session"] = "new_session"


@dataclass
class Fork:
    """Copy the path to a cursor's leaf into a new session in the same cwd, and load it (RPC ``fork``).

    The source is unchanged and the client stays attached to it. Refused with
    ``busy`` when the copy would carry an entry a turn is still writing.

    Attributes:
        at: The entry to fork at instead of the cursor's leaf.
    """

    session_id: str
    cursor_id: str
    at: str | None = None
    type: Literal["fork"] = "fork"


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
class PerformReady:
    """Perform a flow's ``Ready`` at a cursor, as a submit that resolved to it would.

    ``fork`` and ``switch_session`` move a client to another session, so they come
    back unperformed for the client to do.

    Attributes:
        ready: What :class:`NextStep` answered once every argument was bound. Its
            ``mutation`` must be its flow's.
    """

    session_id: str
    cursor_id: str
    ready: Ready
    type: Literal["perform_ready"] = "perform_ready"


@dataclass
class Describe:
    """Re-read a session's extension surface, after an extension was enabled or reloaded."""

    session_id: str
    type: Literal["describe"] = "describe"


@dataclass
class Shutdown:
    """Stop the daemon: it answers, then stops as it does on SIGTERM, closing every connection.

    ``tau serve --stop`` sends it. Every client shares the daemon, so a client
    asks its user before sending it.
    """

    type: Literal["shutdown"] = "shutdown"


@dataclass
class Compare:
    """Open one cursor per model at ``leaf`` and send each the same text (docs/TAU-SERVE.md §8).

    Answered at once; the turns run on. Each turn's ``submission_start`` carries
    the comparison in ``submission.correlation.compare``, so every attached
    client can draw the columns. An unknown model fails before anything is opened.

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
    nothing.
    """

    session_id: str
    comparison_id: str
    keep: str | None
    type: Literal["end_compare"] = "end_compare"


REQUESTS: tuple[type, ...] = (
    Hello,
    ListSessions,
    NewSession,
    Fork,
    Attach,
    Detach,
    OpenCursor,
    CloseCursor,
    MoveCursor,
    Answer,
    PerformReady,
    Describe,
    Shutdown,
    Compare,
    EndCompare,
)
"""Every request defined here, by its ``type``; :data:`RPC_VERBS` are the rest."""


@dataclass
class RpcCall:
    """A request RPC answers too: its verb, at one cursor of one session (docs/TAU-SERVE.md §5, 0.6).

    Sent as method ``verb`` with params ``{"session_id", "cursor_id", **params}``;
    ``params`` are RPC's own and are checked against its ``params_schema``.
    """

    verb: str
    session_id: str
    cursor_id: str
    params: dict[str, Any]


RPC_RUN: tuple[str, ...] = (
    "abort",
    "compact",
    "get_state",
    "get_messages",
    "get_commands",
    "get_tools",
    "get_models",
    "get_session_name",
    "get_session_stats",
    "get_last_assistant_text",
    "set_model",
    "set_auto_compaction",
    "set_session_name",
    "complete_path",
    "next_step",
    "enumerate_domain",
    "complete_message_id",
    "get_tree",
    "get_entry",
    "get_pending_request",
    "answer_request",
    "list_managed_extensions",
    "get_extension_state",
    "get_extension_config",
    "set_extension_config",
    "enable_extension",
    "disable_extension",
    "reload_extension",
    "navigate",
    "summarize_and_navigate",
    "elide_span",
    "commit_branch",
    "paste_subtree",
)
"""RPC verbs the daemon runs through RPC's own handler, bound to the named cursor.

``compact``'s outcome is the ``compaction_end`` event, as it is RPC's notification;
``abort`` reaches the cursor's turn, the cursors it owns, and its compaction.
"""

RPC_OWN: dict[str, str] = {
    "submit": (
        "Answered at admission, as over stdio, and its turn ends with a "
        '`submission_end` channel event. `multitask_strategy: "fork"` is accepted. '
        "A command answers success with `dispatched`, the arm it resolved to, where "
        "stdio refuses a step or a ready flow: the daemon performs a ready flow "
        "itself, except `fork` and `switch_session`, which come back for the client."
    ),
    "prompt": "`submit` with RPC's provenance defaults.",
}
"""RPC verbs the daemon answers itself, with RPC's params and result shape, and how each differs.

Their answer adds ``admitted`` and ``dispatched`` to RPC's.
"""

RPC_VERBS: tuple[str, ...] = (*RPC_OWN, *RPC_RUN)
"""Every RPC verb a client may send here. ``get_capabilities`` is ``hello`` and the
schema; ``switch_session`` is ``attach``; the rest of RPC's table is declined there too."""


# ─── Records the daemon sends ────────────────────────────────────────────


@dataclass
class SessionRow(records.SessionRow):
    """One line of :class:`ListSessions`' answer: RPC's row, plus where and whether it is loaded.

    Attributes:
        ref: The store's own handle for the session.
        title: A bounded display label; message text appears nowhere else here.
        created: ISO-8601.
        modified: ISO-8601.
        parent: The session this one was forked from, or ``None``.
        error: Why its entries could not be read, or ``None``; such a row stays listed.
        cwd: The directory its tools run in.
        loaded: Whether the daemon holds it now.
    """

    cwd: str
    loaded: bool


@dataclass
class CursorState:
    """A live cursor as clients see it; sent whenever one opens, moves, closes or changes busy.

    Attributes:
        model: The config name of the model its next turn calls, as ``set_model``
            takes it and ``get_models`` lists it; not the model id, which two
            names may share.
        request: The extension request at its leaf, or ``None``.
    """

    cursor_id: str
    leaf: str | None
    label: str
    owner_id: str | None
    busy: bool
    model: str
    request: ExtensionRequest | None


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
class RequestEventData:
    """An extension form open now: the ``request`` event's data, and one of :class:`Attached`'s ``requests``.

    Attributes:
        request_id: What :class:`Answer` names.
    """

    request_id: str
    spec: Annotated[dict[str, Any], Shape(FormSpec)]


DispatchedCommand = Annotated[
    Annotated[Performed, Tagged("PerformedArm", "arm", "Performed")]
    | Annotated[FlowStep, Tagged("FlowStepArm", "arm", "FlowStep")]
    | Annotated[Ready, Tagged("ReadyArm", "arm", "Ready")]
    | Annotated[View, Tagged("ViewArm", "arm", "View")],
    Named(
        "DispatchedCommand",
        "What a command resolved to, told apart by `arm`: `Performed` (it ran), "
        "`FlowStep` (an argument is missing), `Ready` (a `fork` or `switch_session` "
        "for the client to perform) or `View` (a surface only a head opens).",
    ),
]


# ─── Results ─────────────────────────────────────────────────────────────


@dataclass
class HelloResult:
    """The answer to :class:`Hello`.

    Attributes:
        protocol: The daemon's :data:`PROTOCOL_VERSION`.
        client_id: This connection's name in the daemon's log.
        pid: The daemon's process id.
        version: τ's package version, as ``tau --version`` prints it.
        cwd: The daemon's working directory at start, absolute; a default for
            :class:`NewSession`.
    """

    protocol: str
    client_id: str
    pid: int
    version: str
    cwd: str


@dataclass
class SessionList:
    """The answer to :class:`ListSessions`, RPC's shape."""

    sessions: list[SessionRow]
    scope: SessionScope


@dataclass
class SessionOpened:
    """The answer to :class:`NewSession` and :class:`Fork`, RPC's lifecycle shape.

    Attributes:
        cancelled: Always false: no session is switched away from, so no hook can veto.
        leaf: The new session's head leaf, duplicated out of ``session``.
    """

    cancelled: bool
    session: SessionTuple
    leaf: str | None


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
        requests: The extension forms open now, which an ``Answer`` closes.
    """

    session_id: str
    epoch: str
    seq: int
    entries: list[Annotated[dict[str, Any], Shape(Entry)]] | None
    cursors: list[CursorState]
    head_cursor_id: str
    cwd: str
    models: list[ModelRecord]
    surface: Surface
    requests: list[RequestEventData]


@dataclass
class CursorOpened:
    """The answer to :class:`OpenCursor`."""

    cursor_id: str


@dataclass
class CompareCursor:
    """One column of a comparison."""

    cursor_id: str
    model: str


@dataclass
class CompareStarted:
    """The answer to :class:`Compare`, sent before the turns end.

    Attributes:
        message: One line naming the models, for a head to show.
    """

    comparison_id: str
    cursors: list[CompareCursor]
    message: str


@dataclass
class CompareEnded:
    """The answer to :class:`EndCompare`.

    Attributes:
        leaf: The head cursor's leaf afterwards.
    """

    leaf: str | None


RESULTS: dict[type, Any] = {
    Hello: HelloResult,
    ListSessions: SessionList,
    NewSession: SessionOpened,
    Fork: SessionOpened,
    Attach: Attached,
    Detach: None,
    OpenCursor: CursorOpened,
    CloseCursor: None,
    MoveCursor: None,
    Answer: None,
    PerformReady: DispatchedCommand,
    Describe: Surface,
    Shutdown: None,
    Compare: CompareStarted,
    EndCompare: CompareEnded,
}
"""What each request is answered with; ``None`` is a ``null`` result."""


ERROR_CODES: dict[int, str] = {
    dialect.PARSE_ERROR: "The frame is not JSON.",
    dialect.INVALID_REQUEST: "Not a JSON-RPC 2.0 request with an `id`, or not `hello` "
    "first, or a batch.",
    dialect.METHOD_NOT_FOUND: "No such method, or one RPC declines.",
    dialect.INVALID_PARAMS: "`params` do not fit the method, or name something the daemon refuses.",
    dialect.INTERNAL_ERROR: "The daemon failed while answering.",
    dialect.SUBMISSION_REJECTED: "RPC's `SUBMISSION_REJECTED`: the submission was refused.",
    dialect.COMMAND_NOT_SUPPORTED: "RPC's `COMMAND_NOT_SUPPORTED`.",
    dialect.TURN_STILL_RUNNING: "Busy: a turn is running where the request acts.",
    dialect.SESSION_NOT_PERSISTED: "RPC's `SESSION_NOT_PERSISTED`.",
    dialect.UNAUTHORIZED: "The hello's token is missing or wrong.",
    dialect.PROTOCOL_MISMATCH: "The hello names another protocol version.",
    dialect.NOT_FOUND: "No such session, cursor, entry, form or flow.",
}
"""Every ``error.code`` the daemon sends; the JSON-RPC and RPC codes keep their meaning."""


@dataclass
class Error:
    """Why a request failed. ``code`` is stable; ``message`` is for a human.

    Attributes:
        code: One of :data:`ERROR_CODES`, the table RPC uses too
            (``tau_agent_core.rpc.dialect``).
        data: RPC's ``error.data`` for an RPC verb's refusal (a submission's
            ``lock``, the offending ``name``), else ``None``.
    """

    code: int
    message: str
    data: dict[str, Any] | None = None


@dataclass
class Success:
    """The answer to a request that succeeded, matched by ``id``.

    Attributes:
        result: The request's result: ``Results[method]`` in the schema.
    """

    id: RequestId
    result: Any
    jsonrpc: Literal["2.0"] = "2.0"


@dataclass
class Failure:
    """The answer to a request that failed, matched by ``id``.

    Attributes:
        id: The request's ``id``, or ``null`` when the frame had none the daemon
            could read.
    """

    id: RequestId | None
    error: Error
    jsonrpc: Literal["2.0"] = "2.0"


Response = Annotated[
    Success | Failure,
    Named("Response", "The one answer to a request: a `result` or an `error`, never both."),
]


# ─── Events ──────────────────────────────────────────────────────────────


@dataclass
class EntryEventData:
    """An ``entry_open``, ``entry_final`` or ``entry_append``: one log write.

    Apply by ``entry.id``, last write wins, keeping the first position (§4.1).

    Attributes:
        entry: The entry as the log now holds it.
        cursor_id: The cursor whose turn or request wrote it; an ``entry_final``
            names the cursor that opened the entry. Null for a write no cursor
            made. A client joins ``agent_event`` to the entries a turn writes by it.
    """

    entry: Annotated[dict[str, Any], Shape(Entry)]
    cursor_id: str | None = None


@dataclass
class CursorsEventData:
    """The whole cursor set after a change."""

    cursors: list[CursorState]


@dataclass
class RequestClosedEventData:
    """A form some client answered, or its asker gave up on."""

    request_id: str


@dataclass
class SubmissionInfo:
    """A ``Submission`` as its channel events carry it (``tau_agent_core.submission``)."""

    text: str
    source: SubmissionSource
    submitter: str
    submission_id: str
    images: list[dict[str, Any]] | None
    multitask_strategy: MultitaskStrategy
    expand_commands: bool
    allow_user_input: bool
    store_history: bool
    silent: bool
    correlation: Annotated[dict[str, Any], Shape(Correlation)]
    depth: int


@dataclass
class SubmissionStartPayload:
    """A submission admitted to run a turn.

    Attributes:
        cursor_id: The cursor its turn extends.
        owner_id: That cursor's owner, for a sub-agent's turn.
        attachments: What ``expand_attachments`` did, or ``None`` when it was not asked.
    """

    submission: SubmissionInfo
    text: str
    images: list[dict[str, Any]] | None
    cursor_id: str | None
    owner_id: str | None
    attachments: AttachmentReport | None


@dataclass
class SubmissionEndPayload:
    """A submission's turn ended, however it ended.

    Attributes:
        side_usage: Tokens its side completions (summaries) spent, by usage field.
    """

    submission: SubmissionInfo
    side_usage: dict[str, int] | None


@dataclass
class CustomMessagePayload:
    """An extension message appended outside a turn."""

    entry_id: str
    message: Annotated[dict[str, Any], Shape(CustomRoleMessage)]


@dataclass
class SubmissionStartChannel:
    """The ``submission_start`` channel."""

    payload: SubmissionStartPayload
    name: Literal["submission_start"] = "submission_start"


@dataclass
class SubmissionEndChannel:
    """The ``submission_end`` channel."""

    payload: SubmissionEndPayload
    name: Literal["submission_end"] = "submission_end"


@dataclass
class CustomMessageChannel:
    """The ``custom_message`` channel."""

    payload: CustomMessagePayload
    name: Literal["custom_message"] = "custom_message"


ChannelEventData = Annotated[
    SubmissionStartChannel | SubmissionEndChannel | CustomMessageChannel,
    Named("ChannelEventData", "A session bus channel, told apart by `name`. Changes no state."),
]


@dataclass
class UiNotify:
    """An extension's notification."""

    message: str
    level: str
    op: Literal["notify"] = "notify"


@dataclass
class UiStatus:
    """An extension's status-line text under ``key``; ``None`` clears it."""

    key: str
    text: str | None
    op: Literal["status"] = "status"


@dataclass
class UiPanel:
    """An extension's panel under ``key``; ``None`` closes it."""

    key: str
    spec: Annotated[dict[str, Any], Shape(PanelSpec)] | None
    op: Literal["panel"] = "panel"


UiEventData = Annotated[
    UiNotify | UiStatus | UiPanel,
    Named("UiEventData", "An extension display call, told apart by `op`."),
]


EVENT_DATA: dict[str, Any] = {
    "entry_open": EntryEventData,
    "entry_final": EntryEventData,
    "entry_append": EntryEventData,
    "agent_event": WireEvent,
    "channel": ChannelEventData,
    "cursors": CursorsEventData,
    "request": RequestEventData,
    "request_closed": RequestClosedEventData,
    "ui": UiEventData,
    "compaction_end": Annotated[
        dict[str, Any],
        Given("CompactionEnd", rpc.COMPACTION_END_PARAMS_SCHEMA, records.COMPACTION_END_TYPES),
    ],
}
"""What each event kind carries in ``data``.

``agent_event`` is RPC's ``WireEvent``, built by the same
``tau_agent_core.rpc.wire_events.WireEventProjector``: ``message_update``
carries a delta, never the whole message. Unbounded fields (a tool's arguments
and result, a message's content and usage) are left out; the entry events carry
them. It changes no state.

``compaction_end`` is RPC's notification of that name, for a ``compact`` this
session was asked for, and changes no state either: the compaction entry's own
``entry_append`` does.
"""

EVENT_KINDS = tuple(EVENT_DATA)
"""Every event ``kind``, in :data:`EVENT_DATA`'s order."""


@dataclass
class Event:
    """A push for an attached session, numbered per session within an ``epoch``; an ``event`` notification's ``params``."""

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
        "compaction_end",
    ]
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServeStarted:
    """What ``tau serve -d --json`` prints: the one daemon at ``address``.

    Not a frame. ``tau serve -d`` starts no second daemon where one answers a hello.

    Attributes:
        address: ``HOST:PORT`` or ``unix:/PATH``.
        pid: The daemon's process id.
        started: Whether this call started it.
        log: The background log a daemon started by ``-d`` writes.
    """

    address: str
    pid: int
    started: bool
    log: str


@dataclass
class ServeStopped:
    """What ``tau serve --stop --json`` prints. Not a frame.

    Attributes:
        address: ``HOST:PORT`` or ``unix:/PATH``.
        pid: The process id of the daemon that stopped, or ``None`` when none answered.
        stopped: Whether this call stopped one; false when no daemon answered there.
    """

    address: str
    pid: int | None
    stopped: bool


# ─── Building and checking frames ────────────────────────────────────────


def to_wire(message: Any) -> dict[str, Any]:
    """A protocol dataclass as the JSON object it is sent as."""
    return dataclasses.asdict(message)


def method_of(request: Any) -> str:
    """The JSON-RPC ``method`` a request is sent under."""
    return request.verb if isinstance(request, RpcCall) else _type_tag(type(request))


def request_frame(request_id: RequestId, request: Any) -> dict[str, Any]:
    """A request as the JSON-RPC message a client sends."""
    if isinstance(request, RpcCall):
        params = {"session_id": request.session_id, "cursor_id": request.cursor_id}
        params.update(request.params)
    else:
        params = {k: v for k, v in dataclasses.asdict(request).items() if k != "type"}
    return {"jsonrpc": JSONRPC, "id": request_id, "method": method_of(request), "params": params}


def event_frame(event: Event) -> dict[str, Any]:
    """An event as the ``event`` notification the daemon sends."""
    return {"jsonrpc": JSONRPC, "method": EVENT_METHOD, "params": to_wire(event)}


class FrameError(ValueError):
    """A client frame the daemon answers with an error; ``id`` is ``None`` when unreadable."""

    def __init__(self, code: int, message: str, request_id: RequestId | None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def json_safe(value: Any) -> Any:
    """``value`` with everything JSON cannot hold turned into its ``str``."""
    return json.loads(json.dumps(value, default=str))


def _members(hint: Any) -> tuple[Any, ...]:
    """The member hints of a :class:`Named` union."""
    return get_args(get_args(hint)[0])


def _base(hint: Any) -> Any:
    """``hint`` without its ``Annotated`` wrapper."""
    return get_args(hint)[0] if get_origin(hint) is Annotated else hint


def command_to_wire(command: Any) -> dict[str, Any] | None:
    """A dispatched command as its :data:`DispatchedCommand` arm, or ``None``.

    Raises:
        TypeError: a record that is no arm of the union.
    """
    if command is None:
        return None
    for member in _members(DispatchedCommand):
        if type(command) is _base(member):
            tag = member.__metadata__[0]
            return {tag.tag: tag.value, **json_safe(dataclasses.asdict(command))}
    raise TypeError(f"{type(command).__name__} is not an arm of DispatchedCommand")


def result_to_wire(request: Any, result: Any) -> Any:
    """``result`` as the JSON its request is answered with, checked against :data:`RESULTS`.

    Raises:
        TypeError: ``result`` is not what :data:`RESULTS` declares for ``request``.
    """
    if isinstance(request, RpcCall):
        return json_safe(result)
    declared = RESULTS[type(request)]
    if declared is None:
        if result is not None:
            raise TypeError(f"{type(request).__name__} answers null, not {result!r}")
        return None
    if declared is DispatchedCommand:
        return command_to_wire(result)
    allowed = (
        tuple(_base(m) for m in _members(declared))
        if get_origin(declared) is Annotated
        else (declared,)
    )
    if not isinstance(result, allowed):
        raise TypeError(
            f"{type(request).__name__} answers {[c.__name__ for c in allowed]}, "
            f"not {type(result).__name__}"
        )
    return to_wire(result)


def parse_request(raw: Any) -> tuple[RequestId, Any]:
    """Read a client frame into its request dataclass.

    Returns:
        The request ``id`` and the dataclass instance.

    Raises:
        FrameError: ``INVALID_REQUEST`` for a frame that is not a JSON-RPC 2.0
            request with an ``id`` (a batch, a notification), ``METHOD_NOT_FOUND``
            for an unknown method, ``INVALID_PARAMS`` for params that do not fit.
    """
    if isinstance(raw, list):
        raise FrameError(dialect.INVALID_REQUEST, "batches are not supported", None)
    if not isinstance(raw, dict):
        raise FrameError(dialect.INVALID_REQUEST, "a request is a JSON object", None)
    request_id = raw.get("id")
    if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
        raise FrameError(
            dialect.INVALID_REQUEST,
            "a request needs an integer or string 'id'; notifications are not served",
            None,
        )
    if raw.get("jsonrpc") != JSONRPC:
        raise FrameError(dialect.INVALID_REQUEST, "'jsonrpc' must be \"2.0\"", request_id)
    unknown = sorted(set(raw) - {"jsonrpc", "id", "method", "params"})
    if unknown:
        raise FrameError(dialect.INVALID_REQUEST, f"unknown member(s) {unknown}", request_id)
    method = raw.get("method")
    params = raw.get("params", {})
    if not isinstance(method, str):
        raise FrameError(dialect.INVALID_REQUEST, "'method' must be a string", request_id)
    if not isinstance(params, dict):
        raise FrameError(dialect.INVALID_PARAMS, "'params' must be an object", request_id)
    try:
        if method in RPC_VERBS:
            return request_id, _rpc_call(method, params)
        for cls in REQUESTS:
            if _type_tag(cls) == method:
                if "type" in params:
                    raise ValueError(f"{method}: unknown field(s) ['type']")
                return request_id, _build(cls, params, method)
    except (TypeError, ValueError) as exc:
        raise FrameError(dialect.INVALID_PARAMS, str(exc), request_id) from None
    raise FrameError(dialect.METHOD_NOT_FOUND, f"unknown method {method!r}", request_id)


def _rpc_call(verb: str, fields: dict[str, Any]) -> RpcCall:
    """An RPC verb's params as an :class:`RpcCall`, checked by RPC's own schema."""
    params = {k: v for k, v in fields.items() if k not in ("session_id", "cursor_id")}
    for name in ("session_id", "cursor_id"):
        if not isinstance(fields.get(name), str):
            raise ValueError(f"{verb}: {name!r} must be a string")
    violation = rpc.validate_params(rpc.COMMAND_TABLE[verb].params_schema, params)
    if violation is not None:
        raise ValueError(f"{verb}: {violation}")
    return RpcCall(verb, fields["session_id"], fields["cursor_id"], params)


def _build(cls: type, fields: dict[str, Any], where: str) -> Any:
    """``cls(**fields)``, refusing unknown keys and building nested record fields from dicts."""
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {unknown}")
    hints = get_type_hints(cls)
    built = dict(fields)
    for name, value in fields.items():
        nested = hints[name]
        if isinstance(nested, type) and dataclasses.is_dataclass(nested):
            if not isinstance(value, dict):
                raise ValueError(f"{where}: {name!r} must be an object")
            built[name] = _build(nested, value, f"{where}.{name}")
        elif not _fits(value, nested):
            raise ValueError(f"{where}: {name!r} = {value!r} does not fit {nested}")
    return cls(**built)


def _fits(value: Any, hint: Any) -> bool:
    """Whether a JSON value fits an annotation, as the generated schema would judge it.

    Checks the shapes requests use: scalars, ``Literal``, unions, ``list[...]`` and
    ``dict[str, ...]``; anything else (``Any``, a record) is left to its own reader.
    """
    origin = get_origin(hint)
    if hint is Any:
        return True
    if hint is type(None):
        return value is None
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if hint is str:
        return isinstance(value, str)
    if origin is Literal:
        return value in get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return any(_fits(value, arg) for arg in get_args(hint))
    if origin is list:
        (item,) = get_args(hint)
        return isinstance(value, list) and all(_fits(v, item) for v in value)
    if origin is dict:
        _, item = get_args(hint)
        return isinstance(value, dict) and all(_fits(v, item) for v in value.values())
    return True


def _type_tag(cls: type) -> str:
    """The ``type`` literal a protocol dataclass declares."""
    return str(get_args(get_type_hints(cls)["type"])[0])


# ─── JSON Schema ─────────────────────────────────────────────────────────


def _camel(kind: str) -> str:
    """``request_closed`` → ``RequestClosed``."""
    return "".join(part.capitalize() for part in kind.split("_"))


RECORD_RULE = (
    "Request records are closed (`additionalProperties: false`), because the daemon "
    "refuses an unknown field. Everything the daemon sends is open: a client must "
    "ignore a field it does not know, so a minor version may add one."
)
"""Which records are closed, stated once for the schema and the Markdown."""

SCHEMA_DESCRIPTION = (
    f"The tau serve protocol, JSON-RPC 2.0 over WebSocket (docs/SERVE-PROTOCOL.md). "
    f"{RECORD_RULE} Each request $def is its method's `params` and names its answer "
    "in `x-result`, and `Results` maps every method to it; `Event`'s `x-data` maps "
    "every `kind` to its data."
)
"""The schema's top-level ``description``."""


def _rpc_request(s: SchemaBuilder, verb: str) -> dict[str, Any]:
    """An RPC verb's request: RPC's typed params, plus where it acts."""
    params = capabilities.typed_params(s, verb)
    how = RPC_OWN.get(verb, "The daemon runs RPC's own handler at `cursor_id`.")
    return {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "cursor_id": {"type": "string", "description": "The cursor the verb acts at."},
            **params.get("properties", {}),
        },
        "required": ["session_id", "cursor_id", *params.get("required", [])],
        "additionalProperties": False,
        "description": f"RPC `{verb}` (docs/RPC-PROTOCOL.md) at one cursor. {how}",
    }


def _rpc_result(s: SchemaBuilder, verb: str) -> dict[str, Any]:
    """An RPC verb's answer: RPC's typed result; ``submit`` and ``prompt`` add ``admitted`` and ``dispatched``."""
    result = capabilities.typed_result(s, verb)
    if verb in RPC_OWN:
        result["properties"] = {
            **result["properties"],
            "admitted": {
                "type": "boolean",
                "description": "Whether a turn was admitted for this submission, so a "
                "`submission_end` channel event with its id will follow. False for a "
                "steer delivered into another turn, and for a command.",
            },
            "dispatched": {
                "anyOf": [s.of(DispatchedCommand), {"type": "null"}],
                "description": "What a command resolved to, after the daemon performed it; "
                "`null` for a prompt.",
            },
        }
    return result


def _request_message(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The JSON-RPC request a client sends for ``method``, with ``params`` as its params."""
    return {
        "type": "object",
        "properties": {
            "jsonrpc": {"const": JSONRPC},
            "id": {"type": ["integer", "string"]},
            "method": {"const": method},
            "params": params,
        },
        "required": ["jsonrpc", "id", "method"],
        "additionalProperties": False,
        "description": f"A `{method}` request.",
    }


def json_schema() -> dict[str, Any]:
    """The whole protocol as one JSON Schema document.

    ``ClientFrame`` is any request; ``ServerFrame`` is an answer or an ``event``
    notification. Every named type is under ``$defs``.
    """
    s = capabilities.schema_builder()
    frames = []
    results: dict[str, Any] = {}
    for cls in REQUESTS:
        s.request(cls)
    for cls in REQUESTS:
        definition = s.defs[cls.__name__]
        definition["properties"].pop("type")
        definition["required"] = [r for r in definition["required"] if r != "type"]
        result = s.of(RESULTS[cls] if RESULTS[cls] is not None else type(None))
        definition["x-result"] = result
        tag = _type_tag(cls)
        results[tag] = result
        frames.append(_request_message(tag, {"$ref": f"#/$defs/{cls.__name__}"}))
    for verb in RPC_VERBS:
        definition = _rpc_request(s, verb)
        name = _camel(verb)
        result = s.of(Annotated[dict[str, Any], Given(f"{name}Result", _rpc_result(s, verb))])
        s.defs[name] = {**definition, "x-result": result}
        results[verb] = result
        frames.append(_request_message(verb, {"$ref": f"#/$defs/{name}"}))
    s.of(Response)
    s.defs["Error"]["properties"]["code"]["enum"] = sorted(ERROR_CODES)
    base = s.record(Event)
    variants = {}
    for kind, data in EVENT_DATA.items():
        name = f"{_camel(kind)}Event"
        properties = {**base["properties"], "kind": {"const": kind}, "data": s.of(data)}
        s.defs[name] = {
            **base,
            "properties": properties,
            "required": [*base["required"], "data"],
            "description": f"A `{kind}` event.",
        }
        variants[kind] = properties["data"]
    s.defs["Event"] = {
        "oneOf": [{"$ref": f"#/$defs/{_camel(kind)}Event"} for kind in EVENT_DATA],
        "description": summary(Event),
        "x-data": variants,
    }
    s.defs["EventNotification"] = {
        "type": "object",
        "properties": {
            "jsonrpc": {"const": JSONRPC},
            "method": {"const": EVENT_METHOD},
            "params": {"$ref": "#/$defs/Event"},
        },
        "required": ["jsonrpc", "method", "params"],
        "description": "An `event` notification: no `id`, and no answer.",
    }
    s.of(ServeStarted)
    s.of(ServeStopped)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"tau serve protocol {PROTOCOL_VERSION}",
        "description": SCHEMA_DESCRIPTION,
        "x-protocol-version": PROTOCOL_VERSION,
        "x-default-port": DEFAULT_PORT,
        "$defs": s.defs,
        "ClientFrame": {"oneOf": frames},
        "ServerFrame": {
            "oneOf": [{"$ref": "#/$defs/Response"}, {"$ref": "#/$defs/EventNotification"}]
        },
        "Results": results,
    }
