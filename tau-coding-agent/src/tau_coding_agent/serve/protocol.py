"""The ``tau serve`` wire protocol: every message, defined once (docs/TAU-SERVE.md §5).

One JSON object per WebSocket text frame. A client sends requests, each with an
``id`` it chose; the daemon answers each with one :class:`Response`, and pushes
:class:`Event` frames for every session the client is attached to.

The definitions here are the protocol. Dataclasses are what the daemon builds and
sends; TypedDicts are the shapes of dicts it forwards from the log and the core
(an entry, a message, a form). :func:`json_schema` derives JSON Schema from both,
``scripts/generate_serve_protocol.py`` writes it with ``docs/SERVE-PROTOCOL.md``,
and tau-code generates its TS types from that schema.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import types
import typing
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    Literal,
    Never,
    NotRequired,
    Required,
    TypedDict,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from tau_agent_core.extension_locks import ExtensionRequest as ExtensionRequestRecord
from tau_agent_core.flows import FlowStep, Performed, Ready, View
from tau_agent_core.rpc import commands as rpc
from tau_agent_core.rpc_event_schema import WireEvent
from tau_agent_core.submission import MultitaskStrategy, SubmissionSource
from tau_llm.types import (
    AssistantMessage,
    Usage,
    ImageContent,
    TextContent,
    ToolResultMessage,
    UserMessage,
)

PROTOCOL_VERSION = "0.6"
"""``MAJOR.MINOR``. Below 1.0 any bump may break a client, and the hello refuses a mismatch."""

DEFAULT_PORT = 8256
"""The port ``tau serve`` listens on and ``tau --connect HOST`` dials ("ffwf" in base64, as decimal)."""


@dataclass(frozen=True)
class Named:
    """Marks a union as one ``$defs`` entry whose members a ``const`` field tells apart.

    Attributes:
        name: The ``$defs`` key.
        doc: Its description.
    """

    name: str
    doc: str


@dataclass(frozen=True)
class Shape:
    """Marks a ``dict[str, Any]`` the daemon forwards as having the JSON shape of ``hint``."""

    hint: Any


@dataclass(frozen=True)
class Tagged:
    """Marks a record sent flattened with one more ``const`` field, ``{tag: value, **fields}``.

    Attributes:
        name: The ``$defs`` key of the tagged form.
        tag: The added field's name.
        value: Its constant value.
    """

    name: str
    tag: str
    value: str


@dataclass(frozen=True)
class Pattern:
    """Marks a ``str`` as matching ``regex``."""

    regex: str


@dataclass(frozen=True, eq=False)
class Given:
    """Marks a value whose JSON Schema is written out whole, as RPC's command table holds it.

    Attributes:
        name: The ``$defs`` key.
        schema: The schema, used as is but for ``types``.
        types: Nodes of ``schema`` it leaves open, and the type each has, as
            :data:`RPC_TYPES` maps them.
    """

    name: str
    schema: dict[str, Any]
    types: dict[str, Any] = field(default_factory=dict)


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
    Compare,
    EndCompare,
)
"""Every request defined here, by its ``type``; :data:`RPC_VERBS` are the rest."""


@dataclass
class RpcCall:
    """A request RPC answers too: its verb, at one cursor of one session (docs/TAU-SERVE.md §5, 0.6).

    Sent as ``{"type": verb, "session_id", "cursor_id", **params}``; ``params``
    are RPC's own and are checked against its ``params_schema``.
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


# ─── Shapes the daemon forwards: specs, messages, entries ────────────────


class FormField(TypedDict):
    """One field of an extension form (``extension_types.validate_form_spec``)."""

    name: str
    kind: Literal["text", "select", "multiselect", "confirm", "number"]
    label: NotRequired[str]
    default: NotRequired[Any]
    options: NotRequired[list[str]]


class FormSpec(TypedDict):
    """An extension's ``ui.form`` spec, as the extension passed it (docs/EXTENSION-LOCKS.md §8.2)."""

    title: NotRequired[str]
    fields: list[FormField]


class PanelText(TypedDict):
    """A panel body of text."""

    kind: Literal["text"]
    text: str


class PanelList(TypedDict):
    """A panel body listing strings."""

    kind: Literal["list"]
    items: list[str]


class PanelTable(TypedDict):
    """A panel body of string cells; every row has one cell per column."""

    kind: Literal["table"]
    columns: list[str]
    rows: list[list[str]]


PanelBody = Annotated[
    PanelText | PanelList | PanelTable,
    Named(
        "PanelBody", "A panel's body, told apart by `kind` (`extension_types.validate_panel_spec`)."
    ),
]


class PanelAction(TypedDict):
    """A panel button: it runs the extension command ``command`` with ``args``."""

    label: str
    command: str
    args: str


class PanelSpec(TypedDict):
    """A ``ui.panel`` spec, normalized (``extension_types.validate_panel_spec``)."""

    title: str
    body: PanelBody
    actions: list[PanelAction]


class AskAction(TypedDict):
    """An ask's button: it runs ``command`` with the request's id."""

    label: str
    command: str


class Ask(TypedDict):
    """What an extension request asks, normalized (``extension_types.validate_ask_spec``)."""

    title: str
    body: PanelBody | None
    fields: list[FormField]
    actions: list[AskAction]


class SystemMessage(TypedDict):
    """The system prompt, which a session stores as its first message entry."""

    role: Literal["system"]
    content: str


class CustomRoleMessage(TypedDict):
    """An extension's message (``messages.create_custom_message``); the model sees it as ``user``.

    ``visibleToModel`` is absent on nodes written before it existed, which read as true.
    """

    role: Literal["custom"]
    customType: str
    content: list[TextContent | ImageContent]
    display: bool
    visibleToModel: NotRequired[bool]
    details: NotRequired[Any]
    timestamp: NotRequired[int]


Message = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | SystemMessage | CustomRoleMessage,
    Named("Message", "A message as the log stores it, told apart by `role`."),
]


class SummaryMessage(TypedDict):
    """A compaction or branch summary as the context renders it: a user message with no timestamp.

    ``conversation_tree.summary_message_of`` recognises one by its text.
    """

    role: Literal["user"]
    content: list[TextContent]
    timestamp: NotRequired[Never]


ContextMessage = Annotated[
    UserMessage
    | SummaryMessage
    | AssistantMessage
    | ToolResultMessage
    | SystemMessage
    | CustomRoleMessage,
    Named(
        "ContextMessage",
        "A message of model input: a stored message, or a summary rendered as a user "
        "message, which alone has no `timestamp`.",
    ),
]


class _EntryBase(TypedDict):
    """Every finished entry's common fields; ``status`` is absent once an entry is finished.

    ``copiedFrom`` is set on an entry ``paste_subtree`` minted: the id it copies.
    """

    id: str
    parentId: str | None
    timestamp: str
    status: NotRequired[Never]
    copiedFrom: NotRequired[str]


class MessageEntry(_EntryBase):
    """A message on the conversation path."""

    type: Literal["message"]
    message: Message


class CustomMessageEntry(_EntryBase):
    """An extension's message, which reaches the model unless ``visibleToModel`` is false."""

    type: Literal["customMessage"]
    customType: str
    message: CustomRoleMessage


class CustomEntryEntry(_EntryBase):
    """Durable data the model never sees.

    ``data`` is open: ``customType`` ``config`` holds ``session_log.CONFIG_KEYS``
    (docs/CURSORS.md §5); ``extension_request`` and ``extension_response`` are
    docs/EXTENSION-LOCKS.md §4 and §3; ``agent_spec`` is the legacy config; any
    other ``customType`` is the appending extension's own (``api.append_entry``).
    """

    type: Literal["customEntry"]
    customType: str
    data: dict[str, Any]


class CompactionEntry(_EntryBase):
    """A summary that replaces the path before ``firstKeptId`` in the context.

    The provenance fields are absent on entries written before
    docs/TREE-BROWSER-AS-EDITOR.md §8.
    """

    type: Literal["compaction"]
    summary: str
    firstKeptId: str
    tokensBefore: int
    summarizerModelId: NotRequired[str]
    summaryUsage: NotRequired[dict[str, int]]
    coveredEntries: NotRequired[int]
    coveredTokens: NotRequired[int]
    configId: NotRequired[str | None]


class ElideEntry(_EntryBase):
    """A splice anchor with no summary: the path before ``firstKeptId`` leaves the context."""

    type: Literal["elide"]
    firstKeptId: str
    coveredEntries: NotRequired[int]
    coveredTokens: NotRequired[int]
    configId: NotRequired[str | None]


class BranchSummaryEntry(_EntryBase):
    """A summary of the branch left at ``fromId``, in the path where it was appended."""

    type: Literal["branch_summary"]
    summary: str
    fromId: str | None


class SessionInfoEntry(_EntryBase):
    """The session's display name from here on; the model never sees it."""

    type: Literal["session_info"]
    name: str


class NavigateEntry(_EntryBase):
    """Legacy: a recorded move to ``targetId``, written before cursors (docs/CURSORS.md §1.1)."""

    type: Literal["navigate"]
    targetId: str | None


class ModelChangeEntry(_EntryBase):
    """Legacy: the config model from here on, before config entries."""

    type: Literal["model_change"]
    model: str | None
    backend: NotRequired[str | None]


class ThinkingChangeEntry(_EntryBase):
    """Legacy: the reasoning level from here on, before config entries."""

    type: Literal["thinking_change"]
    level: str | None


class ForeignEntry(_EntryBase):
    """A document another system's store put in the tree, typed ``system:kind`` (``jmfts:document``)."""

    type: Annotated[str, Pattern(r"^[^:]+:.+$")]


class IncompleteEntry(TypedDict):
    """An entry opened and not yet finalized (docs/TAU-SERVE.md §4).

    Its payload is partial: a message holds its ``role`` and what has streamed.
    An ``entry_final`` with the same ``id`` replaces it.
    """

    type: str
    id: str
    parentId: str | None
    timestamp: str
    status: Literal["incomplete"]


Entry = Annotated[
    MessageEntry
    | CustomMessageEntry
    | CustomEntryEntry
    | CompactionEntry
    | ElideEntry
    | BranchSummaryEntry
    | SessionInfoEntry
    | NavigateEntry
    | ModelChangeEntry
    | ThinkingChangeEntry
    | ForeignEntry
    | IncompleteEntry,
    Named(
        "Entry",
        "One session-log entry, told apart by `type`; an unfinished one carries "
        '`status: "incomplete"`. Apply by `id`, last write wins (docs/TAU-SERVE.md §4.1).',
    ),
]


class CompareCorrelation(TypedDict):
    """A comparison turn's place in its comparison (``tau_agent_core.compare.COMPARE_KEY``)."""

    id: str
    models: list[str]
    index: int
    cursor_id: str


class Correlation(TypedDict):
    """``Submission.correlation``: open, JSON-safe keys a submitter attached; ``compare`` is τ's."""

    compare: NotRequired[CompareCorrelation]


# ─── Records the daemon sends ────────────────────────────────────────────


@dataclass
class SessionRow:
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

    session_id: str
    ref: str
    name: str | None
    title: str
    message_count: int
    created: str
    modified: str
    parent: str | None
    error: str | None
    cwd: str
    loaded: bool


@dataclass
class SessionScope:
    """What universe a listing is: the daemon's store, and ``cwd`` ``None`` for every directory."""

    store: str
    cwd: str | None


@dataclass
class SessionTuple:
    """A loaded session, as RPC's session tuple names it.

    Attributes:
        cursor_id: Its head cursor.
        leaf: The head cursor's leaf.
        addressable: Whether another request can name it; always true here.
    """

    store: str
    session_id: str
    cursor_id: str
    leaf: str | None
    addressable: bool


@dataclass
class ExtensionRequest:
    """An extension request at a cursor, as RPC ``get_pending_request`` answers it (docs/EXTENSION-LOCKS.md).

    Attributes:
        entry_id: The request entry's id; :class:`AnswerRequest` names it.
        extension_name: The display stem of ``extension``.
        label: τ's framing line for the request (§9).
        lock: Whether a submission at this cursor is refused.
        ask: What it asks, or ``None`` for a bare lock.
        release: A command that clears the lock, or ``None``.
    """

    entry_id: str
    extension: str
    extension_name: str
    sentence: str
    label: str
    lock: bool
    ask: Annotated[dict[str, Any], Shape(Ask)] | None
    release: str | None


@dataclass
class CursorState:
    """A live cursor as clients see it; sent whenever one opens, moves, closes or changes busy.

    Attributes:
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


@dataclass
class AttachmentReport:
    """What ``Submit.expand_attachments`` did.

    Attributes:
        expanded: How many ``@`` references were sent as attachments.
        images: How many of them were images.
        unresolved: The ``@`` tokens that named no file, sent as written.
        failures: One line per file that resolved and could not be read.
    """

    expanded: int
    images: int
    unresolved: list[str]
    failures: list[str]


@dataclass
class DomainChoice:
    """One legal value of a domain: ``value`` is what is bound, ``label`` what is shown."""

    value: str
    label: str


@dataclass
class MessageMatch:
    """One entry ``complete_message_id`` offers: its id, and its first line."""

    entry_id: str
    preview: str


@dataclass
class PathMatch:
    """One path an ``@`` token can complete to."""

    name: str
    detail: str
    is_dir: bool


@dataclass
class AttachmentCompletion:
    """The ``@`` token at the caret and what it completes to.

    Attributes:
        start: The token's first character offset in the text.
        end: The offset after its last.
        total: How many paths match, counting past the bound on ``matches``.
    """

    start: int
    end: int
    token: str
    matches: list[PathMatch]
    total: int


@dataclass
class CommandOutput:
    """An extension command's completion, as RPC's ``submit`` answer names it.

    Attributes:
        name: The command that ran, which an input hook may have rewritten.
        output: What it returned, as display text, or ``None``.
    """

    name: str | None
    output: str | None


@dataclass
class ManagedExtension:
    """One managed extension file and whether it is enabled."""

    path: str
    enabled: bool


@dataclass
class LoadError:
    """An extension file that failed to load, and why."""

    path: str
    error: str


@dataclass
class ContextEstimate:
    """The context's size, as ``get_session_stats`` measures it (``compaction.ContextUsageEstimate``)."""

    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None


@dataclass
class CompactionSettingsRecord:
    """When compaction runs (``compaction.CompactionSettings``)."""

    enabled: bool
    reserve_tokens: int
    keep_recent_tokens: int


@dataclass
class LastCompaction:
    """The newest compaction entry on the path (``agent_session.CompactionRecord``).

    Attributes:
        timestamp: ISO-8601.
    """

    id: str
    timestamp: str
    summary: str
    first_kept_id: str | None
    tokens_before: int | None


RPC_TYPES: dict[str, dict[str, Any]] = {
    "submit": {"command": CommandOutput, "view": View, "attachments": AttachmentReport},
    "prompt": {"command": CommandOutput, "view": View, "attachments": AttachmentReport},
    "get_state": {"model": ModelSpec, "usage": Usage | None},
    "get_messages": {"messages": list[ContextMessage]},
    "get_tools": {"tools[].parameters": dict[str, Any]},
    "get_models": {"models": list[ModelRecord]},
    "get_session_stats": {
        "context": ContextEstimate,
        "compaction_settings": CompactionSettingsRecord,
        "last_compaction": LastCompaction | None,
        "usage": Usage | None,
    },
    "set_model": {"model": ModelSpec},
    "complete_path": {"completion": AttachmentCompletion | None},
    "next_step": {
        "step": Annotated[dict[str, Any], Shape(FlowStep)] | None,
        "ready": Annotated[dict[str, Any], Shape(Ready)] | None,
    },
    "enumerate_domain": {"values": list[DomainChoice]},
    "complete_message_id": {"matches": list[MessageMatch]},
    "get_entry": {"entry": Annotated[dict[str, Any], Shape(Entry)]},
    "get_pending_request": {"request": ExtensionRequest | None},
    "list_managed_extensions": {"extensions": list[ManagedExtension]},
    "get_extension_state": {"extensions": list[ExtensionInfo], "errors": list[LoadError]},
    "get_extension_config": {
        "schema": Annotated[dict[str, Any], Shape(FormSpec)] | None,
        "values": dict[str, Any],
    },
    "navigate": {"messages": list[ContextMessage]},
    "summarize_and_navigate": {"messages": list[ContextMessage]},
    "elide_span": {"messages": list[ContextMessage]},
    "commit_branch": {"messages": list[ContextMessage]},
    "paste_subtree": {"minted_ids": list[str]},
}
"""Where an RPC verb's result schema leaves a shape open, the type it has.

RPC's result schemas describe some objects in prose; serve's schema replaces
each such node with its record, so a client generates every field. A path
steps into ``properties`` by name and into ``items`` by ``[]``.
"""

RPC_PARAM_TYPES: dict[str, dict[str, Any]] = {
    "submit": {"images": list[ImageContent] | None},
    "prompt": {"images": list[ImageContent] | None},
    "commit_branch": {"ids": list[str]},
}
"""The same, for RPC params: arrays whose items its schema leaves open."""

COMPACTION_END_TYPES: dict[str, Any] = {
    "compacted_entry_ids": list[str],
    "read_files": list[str],
    "modified_files": list[str],
    "usage": Usage,
}
"""The same, for RPC's ``compaction_end`` payload."""


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
    Compare: CompareStarted,
    EndCompare: CompareEnded,
}
"""What each request is answered with; ``None`` is a ``null`` result."""


@dataclass
class Error:
    """Why a request failed. ``code`` is stable; ``message`` is for a human.

    An RPC verb's refusal keeps its meaning: ``submission_rejected``,
    ``command_not_supported`` and ``session_not_persisted`` are RPC's codes of
    those names, ``busy`` is ``TURN_STILL_RUNNING``, ``bad_request`` is
    ``INVALID_PARAMS``.

    Attributes:
        data: RPC's ``error.data`` for an RPC verb's refusal (a submission's
            ``lock``, the offending ``name``), else ``None``.
    """

    code: Literal[
        "bad_request",
        "unauthorized",
        "protocol_mismatch",
        "not_found",
        "busy",
        "failed",
        "submission_rejected",
        "command_not_supported",
        "session_not_persisted",
    ]
    message: str
    data: dict[str, Any] | None = None


@dataclass
class Response:
    """The one answer to a request, matched by ``id``.

    Attributes:
        result: When ``ok``, the request's result: ``Results[request.type]`` in
            the schema. ``null`` when not ``ok``.
    """

    id: int
    ok: bool
    result: Any = None
    error: Error | None = None
    type: Literal["response"] = "response"


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
        Given("CompactionEnd", rpc.COMPACTION_END_PARAMS_SCHEMA, COMPACTION_END_TYPES),
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
        "compaction_end",
    ]
    data: dict[str, Any] = field(default_factory=dict)
    type: Literal["event"] = "event"


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


# ─── Building and checking frames ────────────────────────────────────────


def to_wire(message: Any) -> dict[str, Any]:
    """A protocol dataclass as the JSON object it is sent as; an :class:`RpcCall` flat."""
    if isinstance(message, RpcCall):
        return {
            "type": message.verb,
            "session_id": message.session_id,
            "cursor_id": message.cursor_id,
            **message.params,
        }
    return dataclasses.asdict(message)


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
    if kind in RPC_VERBS:
        return request_id, _rpc_call(str(kind), {k: v for k, v in raw.items() if k != "id"})
    for cls in REQUESTS:
        if _type_tag(cls) == kind:
            fields = {k: v for k, v in raw.items() if k != "id"}
            try:
                return request_id, _build(cls, fields, str(kind))
            except TypeError as exc:
                raise ValueError(f"{kind}: {exc}") from None
    raise ValueError(f"unknown request type {kind!r}")


def _rpc_call(verb: str, fields: dict[str, Any]) -> RpcCall:
    """An RPC verb's frame as an :class:`RpcCall`, its params checked by RPC's own schema."""
    params = {k: v for k, v in fields.items() if k not in ("type", "session_id", "cursor_id")}
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

_OPEN_SHAPES: tuple[Any, ...] = (IncompleteEntry, ForeignEntry, Correlation)
"""Shapes whose undeclared keys are payload by design, not merely tolerated additions."""

_DEF_NAMES: dict[type, str] = {ExtensionRequestRecord: "ExtensionRequestRecord"}
"""``$defs`` keys for core classes whose own name a protocol class already takes."""

_PYDANTIC = "pydantic"
"""The owner recorded for a ``$defs`` entry pydantic generated."""


def _attribute_docs(cls: type) -> dict[str, str]:
    """A Google docstring's ``Attributes:`` entries, by name, each joined onto one line."""
    lines = (cls.__doc__ or "").splitlines()
    docs: dict[str, str] = {}
    current: str | None = None
    current_indent = 0
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Attributes:":
            inside = True
            continue
        if not inside:
            continue
        if not stripped:
            current = None
            continue
        indent = len(line) - len(line.lstrip())
        name, sep, text = stripped.partition(": ")
        if sep and name.isidentifier() and (current is None or indent <= current_indent):
            current, current_indent = name, indent
            docs[name] = text
        elif current is not None:
            docs[current] += " " + stripped
    return docs


def _markdown(text: str) -> str:
    """Docstring prose as a schema description: one line, Sphinx roles as Markdown code."""
    text = re.sub(r":\w+:`~?([^`]+)`", r"`\1`", " ".join(text.split()))
    return text.replace("``", "`")


def _summary(cls: type) -> str:
    """A docstring's first paragraph, on one line."""
    return _markdown((cls.__doc__ or "").strip().split("\n\n", 1)[0])


def _clean_pydantic(node: Any) -> Any:
    """Pydantic schema without titles, and with each ``const`` property required."""
    if isinstance(node, list):
        return [_clean_pydantic(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _clean_pydantic(sub) for name, sub in value.items()}
        else:
            out[key] = _clean_pydantic(value)
    consts = [
        n for n, s in out.get("properties", {}).items() if isinstance(s, dict) and "const" in s
    ]
    if consts:
        required = list(out.get("required", []))
        out["required"] = required + [n for n in consts if n not in required]
    return out


class _Schema:
    """Builds the ``$defs`` of :func:`json_schema`, one entry per named type.

    Records reachable from a request are closed (``additionalProperties: false``),
    because :func:`parse_request` refuses unknown fields. Everything the daemon
    sends is open, because a client must tolerate an added field.
    """

    def __init__(self) -> None:
        self.defs: dict[str, Any] = {}
        self._owners: dict[str, Any] = {}
        self._closed = False

    def _define(self, name: str, owner: Any, build: Any) -> dict[str, Any]:
        """Register ``$defs[name]`` once, refusing two different types under one name."""
        ref = {"$ref": f"#/$defs/{name}"}
        if name in self._owners:
            if self._owners[name] != owner:
                raise TypeError(f"two types want $defs/{name}: {self._owners[name]!r}, {owner!r}")
            if self._closed and self.defs[name].get("additionalProperties") is not False:
                raise TypeError(f"$defs/{name} is sent open and also used in a request")
            return ref
        self._owners[name] = owner
        self.defs[name] = {}
        self.defs[name] = build()
        return ref

    def request(self, cls: type) -> None:
        """Define a request's ``$defs`` entry, closed with everything it reaches."""
        self._closed = True
        try:
            self.of(cls)
        finally:
            self._closed = False

    def of(self, hint: Any) -> Any:
        """JSON Schema for one annotation."""
        origin = get_origin(hint)
        if hint is Any or hint is object:
            return {}
        if hint is Never:
            return False
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
        if origin is Annotated:
            return self._annotated(hint)
        if origin in (NotRequired, Required):
            return self.of(get_args(hint)[0])
        if origin is Literal:
            values = list(get_args(hint))
            return {"const": values[0]} if len(values) == 1 else {"enum": values}
        if origin in (typing.Union, types.UnionType):
            return {"anyOf": [self.of(arg) for arg in get_args(hint)]}
        if origin in (list, tuple):
            args = get_args(hint)
            return {"type": "array", "items": self.of(args[0])}
        if origin is dict:
            return {"type": "object", "additionalProperties": self.of(get_args(hint)[1])}
        if isinstance(hint, type) and hasattr(hint, "model_json_schema"):
            return self._pydantic(hint)
        if is_typeddict(hint):
            return self._define(hint.__name__, hint, lambda: self._typed_dict(hint))
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            name = _DEF_NAMES.get(hint, hint.__name__)
            return self._define(name, hint, lambda: self._record(hint))
        raise TypeError(f"no JSON Schema for annotation {hint!r}")

    def _annotated(self, hint: Any) -> Any:
        base, marker = get_args(hint)[0], hint.__metadata__[0]
        if isinstance(marker, Shape):
            return self.of(marker.hint)
        if isinstance(marker, Pattern):
            return {"type": "string", "pattern": marker.regex}
        if isinstance(marker, Given):
            return self._define(
                marker.name,
                marker.name,
                lambda: _typed(self, marker.schema, marker.types, marker.name),
            )
        if isinstance(marker, Named):
            return self._define(
                marker.name,
                hint,
                lambda: {"oneOf": [self.of(m) for m in get_args(base)], "description": marker.doc},
            )
        if isinstance(marker, Tagged):

            def tagged() -> dict[str, Any]:
                record = self._record(base)
                record["properties"] = {marker.tag: {"const": marker.value}, **record["properties"]}
                record["required"] = [marker.tag, *record["required"]]
                return record

            return self._define(marker.name, hint, tagged)
        raise TypeError(f"no JSON Schema for annotation {hint!r}")

    def _record(self, cls: type) -> dict[str, Any]:
        """A dataclass: its fields; required are those with no default, and every constant."""
        hints = get_type_hints(cls, include_extras=True)
        docs = _attribute_docs(cls)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for f in dataclasses.fields(cls):
            prop = self.of(hints[f.name])
            if f.name in docs and isinstance(prop, dict):
                prop = {**prop, "description": _markdown(docs[f.name])}
            properties[f.name] = prop
            no_default = (
                f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            )
            if no_default or _is_const(hints[f.name]):
                required.append(f.name)
        return self._object(cls, properties, required)

    def _typed_dict(self, cls: Any) -> dict[str, Any]:
        """A TypedDict: its keys, each required unless marked ``NotRequired``.

        Read off the hints, because under postponed annotations the class's own
        ``__required_keys__`` does not see ``NotRequired``.
        """
        hints = get_type_hints(cls, include_extras=True)
        properties = {name: self.of(hint) for name, hint in hints.items()}
        required = [name for name, hint in hints.items() if get_origin(hint) is not NotRequired]
        schema = self._object(cls, properties, required)
        if cls in _OPEN_SHAPES:
            schema["additionalProperties"] = True
        return schema

    def _object(self, cls: Any, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
        if self._closed:
            schema["additionalProperties"] = False
        if cls.__doc__:
            schema["description"] = _summary(cls)
        return schema

    def _pydantic(self, model: Any) -> dict[str, Any]:
        """A pydantic model's own schema, its nested ``$defs`` hoisted beside it."""
        generated = _clean_pydantic(model.model_json_schema(ref_template="#/$defs/{model}"))
        for name, sub in {**generated.pop("$defs", {}), model.__name__: generated}.items():
            owner = self._owners.get(name)
            if owner is None:
                self._owners[name] = _PYDANTIC
                self.defs[name] = sub
            elif owner != _PYDANTIC or self.defs[name] != sub:
                raise TypeError(f"two types want $defs/{name}")
        return {"$ref": f"#/$defs/{model.__name__}"}


def _is_const(hint: Any) -> bool:
    """Whether ``hint`` is a one-value ``Literal``: a discriminator, always sent."""
    return get_origin(hint) is Literal and len(get_args(hint)) == 1


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
    f"The tau serve WebSocket protocol (docs/SERVE-PROTOCOL.md). {RECORD_RULE} Each "
    "request $def names its answer in `x-result`, and `Results` maps every request "
    "`type` to it; `Event`'s `x-data` maps every `kind` to its data."
)
"""The schema's top-level ``description``."""


def _rpc_request(s: _Schema, verb: str) -> dict[str, Any]:
    """An RPC verb's request: RPC's params, typed by :data:`RPC_PARAM_TYPES`, plus where it acts."""
    params = _typed(s, rpc.COMMAND_TABLE[verb].params_schema, RPC_PARAM_TYPES.get(verb, {}), verb)
    how = RPC_OWN.get(verb, "The daemon runs RPC's own handler at `cursor_id`.")
    return {
        "type": "object",
        "properties": {
            "type": {"const": verb},
            "session_id": {"type": "string"},
            "cursor_id": {"type": "string", "description": "The cursor the verb acts at."},
            **params.get("properties", {}),
        },
        "required": ["type", "session_id", "cursor_id", *params.get("required", [])],
        "additionalProperties": False,
        "description": f"RPC `{verb}` (docs/RPC-PROTOCOL.md) at one cursor. {how}",
    }


def _rpc_result(s: _Schema, verb: str) -> dict[str, Any]:
    """An RPC verb's answer: RPC's result schema; ``submit`` and ``prompt`` add ``dispatched``."""
    declared = rpc.COMMAND_TABLE[verb].result_schema
    if declared is None:
        raise TypeError(f"RPC {verb!r} declares no result schema")
    result = _typed(s, declared, RPC_TYPES.get(verb, {}), verb)
    if verb in ("submit", "prompt"):
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


def _typed(s: _Schema, schema: dict[str, Any], types: dict[str, Any], where: str) -> dict[str, Any]:
    """``schema`` with each node ``types`` names replaced by its type's schema, its description kept.

    Raises:
        KeyError: a path that names no node, so the table cannot outlive the schema.
    """
    result = copy.deepcopy(schema)
    for path, hint in types.items():
        parent: dict[str, Any] = result
        steps = path.split(".")
        for step in steps[:-1]:
            name, _, item = step.partition("[]")
            parent = parent["properties"][name]
            if step.endswith("[]"):
                parent = parent["items"]
        last = steps[-1]
        if last not in parent.get("properties", {}):
            raise KeyError(f"{where}: no node {path!r} to type")
        described = parent["properties"][last].get("description")
        typed = dict(s.of(hint))
        parent["properties"][last] = (
            {**typed, "description": described} if described is not None else typed
        )
    return result


def json_schema() -> dict[str, Any]:
    """The whole protocol as one JSON Schema document.

    ``ClientFrame`` is any request plus its ``id``; ``ServerFrame`` is a response
    or an event. Every named type is under ``$defs``.
    """
    s = _Schema()
    frames = []
    results: dict[str, Any] = {}
    for cls in REQUESTS:
        s.request(cls)
    for cls in REQUESTS:
        definition = s.defs[cls.__name__]
        result = s.of(RESULTS[cls] if RESULTS[cls] is not None else type(None))
        definition["x-result"] = result
        tag = _type_tag(cls)
        results[tag] = result
        frames.append(
            {
                "type": "object",
                "properties": {"id": {"type": "integer"}, **definition["properties"]},
                "required": ["id", *definition["required"]],
                "additionalProperties": False,
                "description": f"A `{tag}` request with its id.",
            }
        )
    for verb in RPC_VERBS:
        definition = _rpc_request(s, verb)
        name = _camel(verb)
        result = s.of(Annotated[dict[str, Any], Given(f"{name}Result", _rpc_result(s, verb))])
        s.defs[name] = {**definition, "x-result": result}
        results[verb] = result
        frames.append(
            {
                **definition,
                "properties": {"id": {"type": "integer"}, **definition["properties"]},
                "required": ["id", *definition["required"]],
                "description": f"A `{verb}` request with its id.",
            }
        )
    s.of(Response)
    base = s._record(Event)
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
        "description": _summary(Event),
        "x-data": variants,
    }
    s.of(ServeStarted)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"tau serve protocol {PROTOCOL_VERSION}",
        "description": SCHEMA_DESCRIPTION,
        "x-protocol-version": PROTOCOL_VERSION,
        "x-default-port": DEFAULT_PORT,
        "$defs": s.defs,
        "ClientFrame": {"oneOf": frames},
        "ServerFrame": {"oneOf": [{"$ref": "#/$defs/Response"}, {"$ref": "#/$defs/Event"}]},
        "Results": results,
    }
