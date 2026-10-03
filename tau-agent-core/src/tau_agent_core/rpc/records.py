"""The records RPC's results carry, typed to their leaves (docs/TAU-SERVE.md §5, 0.6).

RPC's command table describes some result objects in prose: its schemas are
the vocabulary ``commands.validate_params`` enforces, which has no ``$ref``.
:data:`RESULT_TYPES` and :data:`PARAM_TYPES` name the type of each such node,
and ``capabilities.typed_command_schemas`` substitutes them, so the capability
document, ``docs/RPC-PROTOCOL.md`` and serve's schema (which runs the same verbs)
define every field. The TypedDicts are shapes the core already holds as dicts:
a log entry, a message, a form spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never, NotRequired, TypedDict

from tau_agent_core.flows import FlowStep, Ready, View
from tau_agent_core.json_schema import Named, Pattern, Shape
from tau_llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

# ─── Shapes the core holds as dicts: specs, messages, entries ────────────


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


OPEN_SHAPES: frozenset[Any] = frozenset({IncompleteEntry, ForeignEntry, Correlation})
"""Shapes whose undeclared keys are payload by design, not merely tolerated additions."""


# ─── Records a result carries ────────────────────────────────────────────


@dataclass
class SessionRow:
    """One session ``list_sessions`` lists.

    Attributes:
        session_id: What ``switch_session`` takes.
        ref: The store's own handle for the session (a file store's path, a
            JMFTS document id), which names the universe the listing is.
        name: What ``set_session_name`` set, or ``None``.
        title: A bounded display label; message text appears nowhere else here.
        created: ISO-8601.
        modified: ISO-8601.
        parent: The session this one was forked from, or ``None``.
        error: Why its entries could not be read, or ``None``; such a row stays listed.
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


@dataclass
class SessionScope:
    """What universe a listing is: a store, and the ``cwd`` it is scoped to, ``None`` for every one."""

    store: str
    cwd: str | None


@dataclass
class SessionTuple:
    """A session a connection drives (F2).

    Attributes:
        store: The backend label of the connection's catalog.
        cursor_id: The cursor the connection drives in it.
        leaf: That cursor's leaf.
        addressable: Whether another call can name ``session_id``; false for an
            in-memory session, which ``list_sessions`` never shows.
    """

    store: str
    session_id: str
    cursor_id: str
    leaf: str | None
    addressable: bool


@dataclass
class ExtensionRequest:
    """An extension request at a cursor, as ``get_pending_request`` answers it (docs/EXTENSION-LOCKS.md).

    Attributes:
        entry_id: The request entry's id; ``answer_request`` names it.
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
class ExtensionInfo:
    """One loaded extension and what it registered, as ``get_extension_state`` lists it."""

    name: str
    path: str
    tools: list[str]
    commands: list[str]
    shortcuts: list[str]
    hooks: list[str]
    content_hash: str
    subjects: list[str]


@dataclass
class ModelSpec:
    """What a config model name resolves to."""

    id: str
    provider: str
    context_window: int


@dataclass
class ModelRecord:
    """One model the config defines, as ``get_models`` lists it."""

    name: str
    model: ModelSpec


@dataclass
class AttachmentReport:
    """What a submission's ``expand_attachments`` did.

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
    """An extension command's completion, as ``submit``'s answer names it.

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


@dataclass
class CommandRow:
    """One verb ``get_capabilities`` lists.

    Attributes:
        since: The unit that added the verb, not a protocol version.
        params_schema: JSON Schema for its params; ``$ref`` resolves against the
            capability document's ``$defs``.
        result_schema: JSON Schema for its result, the same way.
    """

    name: str
    tier: Literal["A", "B", "C", "D"]
    since: str
    notes: str
    params_schema: dict[str, Any]
    result_schema: dict[str, Any]


@dataclass
class DeclinedVerb:
    """A verb τ does not implement, and why; calling it answers ``METHOD_NOT_FOUND``."""

    name: str
    reason: str


@dataclass
class Limits:
    """Bounds on what a host may send (T7).

    Attributes:
        max_request_line_bytes: The longest request line read, excluding its LF.
    """

    max_request_line_bytes: int


# ─── Where RPC's schemas leave a shape open ──────────────────────────────

_SESSION = {"session": SessionTuple}

RESULT_TYPES: dict[str, dict[str, Any]] = {
    "get_capabilities": {
        "commands": list[CommandRow],
        "events": list[str],
        "event_schema": dict[str, Any],
        "ui_methods": list[str],
        "declined": list[DeclinedVerb],
        "limits": Limits,
        "$defs": dict[str, dict[str, Any]],
    },
    "new_session": _SESSION,
    "fork": _SESSION,
    "switch_session": _SESSION,
    "list_sessions": {"sessions": list[SessionRow], "scope": SessionScope},
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
"""Per verb, each node of its result schema left open, and the type it has.

A path steps into ``properties`` by name and into ``items`` by ``[]``. A verb
absent here declares every field already.
"""

_SUBMISSION_PARAMS = {
    "images": list[ImageContent] | None,
    "correlation": Annotated[dict[str, Any], Shape(Correlation)],
}

PARAM_TYPES: dict[str, dict[str, Any]] = {
    "submit": _SUBMISSION_PARAMS,
    "prompt": _SUBMISSION_PARAMS,
    "commit_branch": {"ids": list[str]},
    "next_step": {"bound": dict[str, Any]},
    "answer_request": {"values": dict[str, Any]},
    "set_extension_config": {"values": dict[str, Any]},
}
"""The same, for params. A map keyed by argument or field name holds whatever that one takes."""

COMPACTION_END_TYPES: dict[str, Any] = {
    "compacted_entry_ids": list[str],
    "read_files": list[str],
    "modified_files": list[str],
    "usage": Usage,
}
"""The same, for the ``compaction_end`` notification's params."""
