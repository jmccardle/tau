"""τ-agent-core agent_session: AgentSession public API and SDK entry point.

This module implements:
- AgentSession: High-level session API combining agent loop, session manager, and events.
- create_agent_session(): SDK factory function for creating fully configured sessions.
- ExtensionAPI: Public API exposed to extension modules.

Reference: PHASE-2-SUBPHASE-4.md — Agent Session and SDK Entry Point.
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.
Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.
Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (persist via SessionLog, read via
ConversationTree; System-A SessionManager retired), §4.2 (identity = UUID).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import inspect
import logging
import os
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Literal, cast
from uuid import uuid4

from tau_llm.abort import AbortSignal
from tau_llm.models import EXTENDED_THINKING_LEVELS
from tau_llm.types import Model, UserMessage

from tau_agent_core.events import (
    AgentEvent,
    EventBus,
    SideCompletionPurpose,
    SideCompletionReason,
)
from tau_agent_core.extension_types import (
    BranchResult,
    ExtensionAPI,
    ExtensionContext,
    validate_form_values,
)
from tau_agent_core.extension_locks import (
    RESPONSE_ENTRY_TYPE,
    ExtensionRequest,
    build_response_data,
    find_request,
    refusal_reason,
    request_at,
)
from tau_agent_core.messages import (
    CUSTOM_ROLE,
    create_custom_message,
    last_assistant_text,
    turn_error_message,
)
from tau_agent_core.extension_warnings import (
    TAU_SOURCE,
    ExtensionWarning,
    ExtensionWarnings,
    WarningSeverity,
    validate_warning,
)
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import (
    MESSAGE_POSITION_BEFORE_USER,
    ExtensionError,
    ExtensionRunner,
)
from tau_agent_core.session import SessionState
from tau_agent_core.session_log import (
    SessionLog,
    config_at,
    config_entry_at,
    session_log_is_addressable,
)
from tau_agent_core.cursor import TURN_CURSOR, Cursor, EnqueuedWait, TurnFrame
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.seed import check_seed, normalise_seed
from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig, TurnSettings
from tau_agent_core.capabilities import BUILTIN, CAPABILITIES, Vocabulary
from tau_agent_core.commands import (
    CommandCompletion,
    CommandInvocation,
    UnsupportedCommandError,
    dispatch_builtin,
    expand_prompt_command,
    prompt_command_completions,
    resolve_command,
)
from tau_agent_core.extension_sources import (
    ExtensionRecord,
    extension_identifier,
    resolve_shorthand,
)
from tau_agent_core.prompt_templates import EMPTY_TEMPLATES, PromptTemplateCatalog
from tau_agent_core.skills import EMPTY_SKILLS, SkillCatalog
from tau_agent_core.flows import Dispatched, Performed, Ready
from tau_agent_core.submission import (
    DRIVING_SUBMISSION_DEPTH,
    MAX_SUBMISSION_DEPTH,
    SUBMISSION_ALLOWS_USER_INPUT,
    Submission,
    SubmissionResult,
    next_submission_depth,
)
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionConfig,
    CompactionPreparation,
    CompactionResult,
    CompactionSettings,
    ContextUsageEstimate,
    compact as run_compaction,
    count_message,
    estimate_context_tokens,
    estimate_span_tokens,
    must_compact,
    prepare_compaction,
    should_compact,
    try_compaction_limits,
)
from tau_agent_core.compaction_policy import CompactionPolicy, CompactionPolicyError
from tau_agent_core.compaction_utils import create_file_ops, extract_file_ops_from_message
from tau_agent_core.prompt_cache import prompt_tokens
from tau_agent_core.usage import add_usage, zero_usage
from tau_llm.tokens import ZERO_TOKENS, ContextCalibrator, TextCounter, counter_for
from tau_agent_core.tools.base import AgentTool, ToolDefinition
from tau_agent_core.prompt_cache import ToolBlockChange, tool_block_change
from tau_agent_core.tool_catalog import (
    CALLABLE_EXPOSURES,
    EXPOSURE_NOTICE_TYPE,
    EXPOSURE_NOTICES,
    DECLARED_EXPOSURES,
    ExtensionInfo,
    ToolDescription,
    describe_tool,
)
from tau_agent_core.tool_pipeline import NestedToolResult, run_nested_call
from tau_llm.tools import TOOL_EXPOSURES
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_agent_core.sdk import ExtensionLoadError, LoadedExtension, LoadExtensionsResult


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class CompactionRecord:
    """One ``compaction`` entry in the session log, read back as a record.

    What :meth:`AgentSession.get_last_compaction` returns. Field-for-field the
    entry's own payload with the log's camelCase keys spelled the way the rest of
    this package spells them, so a reader never has to know that ``firstKeptId``
    is how it is written on disk.

    Attributes:
        id: The entry's id.
        timestamp: When it was appended, ISO-8601.
        summary: The generated summary text the compaction spliced in.
        first_kept_id: The entry the context resumes at — everything before it on
            the path is folded away.
        tokens_before: The context size the compaction was measured against.
    """

    id: str
    timestamp: str
    summary: str
    first_kept_id: str | None
    tokens_before: int | None


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class SessionStats:
    """Everything a caller needs to decide whether and when to compact.

    Composed by :meth:`AgentSession.get_session_stats` from five reads that were
    each already public. It exists because the composition was not: the RPC
    ``get_session_stats`` verb assembled it inside the wire layer, so a head that
    was not the wire had to assemble its own and could reach a different answer.

    ``context`` is an ESTIMATE and ``usage`` is what the provider reported for the
    last completion. They are not the same measurement and neither replaces the
    other: a caller that wants what the model was actually charged for reads
    ``usage``; a caller deciding whether the NEXT turn will fit reads ``context``,
    which covers messages appended since that completion.

    Attributes:
        context: ``compaction.estimate_context_tokens`` over the active path.
        context_window: The active model's window, from :meth:`AgentSession.get_model`.
        context_headroom: ``context_window - context.tokens``. Negative when the
            path is already over budget — an honest number, never clamped.
        compaction_settings: The settings in force, as a copy.
        last_compaction: The newest compaction entry, or ``None`` if this session
            has never compacted.
        usage: :meth:`AgentSession.get_usage` — ``None`` before the first
            completion.
    """

    context: ContextUsageEstimate
    context_window: int
    context_headroom: int
    compaction_settings: CompactionSettings
    last_compaction: CompactionRecord | None
    usage: dict[str, Any] | None


_EXT_LOG = logging.getLogger("tau.extensions")
"""Where load-time debug notices go, such as a subscription no loaded extension can answer (#44)."""


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class ExtensionActionResult:
    """Outcome of a runtime ``/extensions`` action (E10 §6 / S70).

    Display-only chrome: never appended to the active path. A hard failure (a
    broken file on reload) raises out of the action instead (Fail-Early).

    There is no ``cursor`` field, though disabling an extension whose lock is
    the cursor releases it (docs/EXTENSION-LOCKS.md §6): both projections of
    this record already read the live cursor, so a copy here would be a second
    writer. The move is visible in ``message``.

    Attributes:
        action: ``"enable"``, ``"disable"``, ``"reload"``, ``"configure"`` or ``"authorize"``.
        path: The resolved extension path, or the caller's token when it named
            no loaded extension.
        ok: False for a reportable no-op or bad target (not loaded, already in
            that state); True for a completed action.
        message: The human-readable line the listing shows.
    """

    action: str
    path: str
    ok: bool
    message: str


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class ExtensionCommandResult:
    """Outcome of :meth:`AgentSession.run_extension_command` (E7 §3 / S46).

    Attributes:
        handled: True iff a command by that name existed and ran; False lets
            the caller fall through (e.g. treat the text as a prompt).
        output: What the handler returned (a string, a renderable, or
            ``None``). Display-only chrome, never appended to the active path
            (E5 §1); a command that wants a durable node uses ``ctx``. ``None``
            for an unknown command.
    """

    handled: bool
    output: object | None = None

    def output_text(self) -> str | None:
        """Coerce ``output`` to display text.

        Returns:
            ``output`` as a string (a non-``str`` value is ``str()``-ed, not
            reshaped), or ``None`` when it is ``None`` or empty: no output box.
        """
        if self.output is None:
            return None
        text = self.output if isinstance(self.output, str) else str(self.output)
        return text or None


def _message_text(content: Any) -> str:
    """Join the text blocks of a message ``content`` (a str, or a list of blocks).

    Non-text blocks (images, etc.) are ignored — this is a text-only view used
    for comparing whether two user turns are "the same" prompt.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _ends_with_user_text(messages: list[Any], text: str) -> bool:
    """True if ``messages`` ends with a user message whose text equals ``text``.

    Detects a caller (e.g. the TUI, which passes the full history including the
    latest user turn) that already placed the current prompt at the tail of the
    context, so it can be threaded to the loop exactly once instead of twice.
    Context messages are always dicts (``context: list[dict]``).
    """
    last = messages[-1] if messages else None
    if not isinstance(last, dict) or last.get("role") != "user":
        return False
    return _message_text(last.get("content", "")).strip() == text.strip()


_UNSET: Any = object()

_REQUEST_KEYS: frozenset[str] = frozenset({"model", "model_spec", "thinking"})
"""The frame keys a request reads, so the only ones recorded between two requests."""


def _leading_system_prompt(messages: list[Any]) -> str | None:
    """The text of the context's opening system message, or ``None`` if it has none.

    That message is the prompt the model actually receives, because the loop
    only sends ``AgentLoopConfig.system_prompt`` into a context without one.
    """
    first = messages[0] if messages else None
    if first is None:
        return None
    role = first.get("role") if isinstance(first, dict) else getattr(first, "role", None)
    if role != "system":
        return None
    content = first.get("content") if isinstance(first, dict) else getattr(first, "content", "")
    return _message_text(content)


def _system_prompt_digest(system_prompt: str) -> str:
    """SHA-256 hex digest of the system prompt text — NEVER the prompt itself.

    A config entry (docs/CURSORS.md §5) records a system prompt's
    IDENTITY (so a reader can tell "did turns 1-5 and turns 6-10 run under the
    same prompt") without recording its CONTENT — a system prompt routinely
    carries a repo's ``AGENTS.md``/project instructions, which do not belong in a
    durable, JMFTS-indexed record on top of already being available at the
    invoker's fingertips. Same convention as ``LoadedExtension.content_hash``
    (sdk.py) — a sha256 of the exact bytes, so two different prompts are never
    mistaken for the same one.
    """
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _seed_label(seed: list[dict[str, Any]]) -> str:
    """A seeded branch's default label: the tool names its calls use, else ``"seeded"``."""
    names = [
        str(block.get("name"))
        for message in seed
        if message.get("role") == "assistant"
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]
    return "+".join(names) or "seeded"


def _extension_factory_label(ext: Callable) -> str:
    """The bucket label for an inline extension factory: ``module:qualname``.

    :func:`~tau_agent_core.extension_sources.extension_identifier` turns it into
    the identifier ``module.qualname``; ``repr`` is the last resort for a
    callable with neither attribute.
    """
    module = getattr(ext, "__module__", None)
    qualname = getattr(ext, "__qualname__", None)
    if module and qualname:
        return f"{module}:{qualname}"
    if qualname:
        return str(qualname)
    return repr(ext)


def _covered_span(path_entries: list[dict[str, Any]], first_kept_id: str) -> list[dict[str, Any]]:
    """The entries a compaction anchored at ``first_kept_id`` removes from the fold.

    ``path_entries`` is ``ConversationTree.context_entries()`` at the leaf the anchor
    will parent under — i.e. the fold as it stands *before* the compaction — so the
    covered span is exactly its prefix up to the boundary. That is the same
    difference ``ConversationTree._splice_span_phrase`` (conversation_tree.py:552)
    reads back out of the tree afterwards, computed here at write time because the
    token figure over it is not recoverable later (TREE-BROWSER-AS-EDITOR.md §8.2).

    Fail-Early on a boundary that is not on the path. ``prepare_compaction`` picks
    ``first_kept_entry_id`` *out of* ``path_entries``, so this cannot happen without
    a defect upstream — and the failure mode if it did would be a provenance record
    claiming a span of zero for a compaction that folded the whole conversation,
    which is worse than a traceback.
    """
    for index, entry in enumerate(path_entries):
        if entry.get("id") == first_kept_id:
            return path_entries[:index]
    raise ValueError(
        f"compaction boundary {first_kept_id!r} is not on the path it was cut from; "
        "the covered-span provenance would be a fabricated zero"
    )


def _newest_compaction_ms(entries: list[dict[str, Any]]) -> int:
    """Epoch-ms of the newest compaction in ``entries``, or 0 if there is none.

    A reloaded session has to know where its own last fold was: every usage
    reported before it was reported about a context that fold removed
    (docs/TOKEN-ACCOUNTING.md §1). Entry stamps are ISO; message stamps are
    epoch-ms; this is the one place they are compared, so this is where the
    conversion lives. An unparseable stamp yields 0 — no boundary rather than a
    wrong one, which degrades to the pre-existing behaviour.
    """
    newest = 0
    for entry in entries:
        if entry.get("type") != "compaction":
            continue
        stamp = entry.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        newest = max(newest, int(moment.timestamp() * 1000))
    return newest


class _CursorWriter:
    """The :class:`~tau_agent_core.agent_loop.TurnWriter` one turn writes through.

    Every message reaches the log when it exists, not when the turn ends
    (docs/TAU-SERVE.md §4.2), so a crash keeps everything finished before it.

    Attributes:
        messages: This turn's new messages in order: ``prompt()``'s return.
        persist: ``store_history``. ``False`` still collects, and writes nothing.
    """

    def __init__(self, cursor: Cursor, persist: bool) -> None:
        self._cursor = cursor
        self.persist = persist
        self.messages: list[dict[str, Any]] = []

    async def open(self, message: dict[str, Any]) -> str | None:
        if not self.persist:
            return None
        return await self._cursor.open("message", message=message)

    async def finalize(self, handle: str | None, message: Any) -> None:
        message_dict = message.model_dump() if hasattr(message, "model_dump") else message
        if self.persist:
            if handle is None:
                raise RuntimeError("finalize: no entry was opened for this message")
            await self._cursor.finalize(handle, message=message_dict)
        self.messages.append(message_dict)

    async def append(self, message: Any) -> None:
        """Write a finished message; a ``custom`` role node becomes a ``customMessage``.

        Raises:
            ValueError: a ``custom`` node has no ``customType``; the extension-origin
                identity is not made up. Checked whether or not it is written.
        """
        message_dict = message.model_dump() if hasattr(message, "model_dump") else message
        if message_dict.get("role") == CUSTOM_ROLE:
            custom_type = message_dict.get("customType")
            if not custom_type:
                raise ValueError(
                    "custom node is missing 'customType' — the extension-origin type "
                    "is required (Fail-Early)"
                )
            if self.persist:
                await self._cursor.append_custom_message(message_dict, str(custom_type))
        elif self.persist:
            await self._cursor.append_message(message_dict)
        self.messages.append(message_dict)


class _SideCompletionWatch:
    """The handle one bracketed side completion writes its progress through.

    Held by :meth:`AgentSession._side_completion`, which owns the start and end
    events; this owns the middle. Two things go through it: :meth:`delta`, which
    the summariser calls per fragment, and :meth:`finish`, which the summariser's
    caller calls with the whole text and what it cost.

    ``finish`` is separate from the fragments because the two are not the same
    text. A summariser appends to what it streamed — compaction stitches on the
    file lists, and a split turn stitches on a second summary that never
    streamed — so the accumulated fragments are a prefix of the result, not the
    result. The end event carries what was actually produced.
    """

    def __init__(
        self,
        events: EventBus,
        purpose: SideCompletionPurpose,
        reason: SideCompletionReason,
        timestamp: Callable[[], int],
    ) -> None:
        self._events = events
        self._purpose = purpose
        self._reason = reason
        self._timestamp = timestamp
        self._streamed: list[str] = []
        self.text: str | None = None
        self.usage: dict[str, int] | None = None

    async def delta(self, fragment: str) -> None:
        """Publish one fragment. The sink handed to the summariser."""
        self._streamed.append(fragment)
        await self._events.emit(
            AgentEvent(
                type="side_completion_update",
                timestamp=self._timestamp(),
                purpose=self._purpose,
                reason=self._reason,
                delta=fragment,
            )
        )

    @property
    def streamed(self) -> str:
        """Everything :meth:`delta` has published, joined. A prefix of ``text``."""
        return "".join(self._streamed)

    def finish(self, text: str, usage: dict[str, int]) -> None:
        """Record what the end event will carry. Not itself an emit."""
        self.text = text
        self.usage = dict(usage)


@agent_facing(topic="sessions")
class AgentSession:
    """One conversation: an agent loop, its events, and a cursor over a session log.

    The entry point for the SDK and every head. Every turn extends one
    :class:`~tau_agent_core.cursor.Cursor` over a
    :class:`~tau_agent_core.session_log.SessionLog`.

    Turn admission and the fork and thread-safe task registries follow
    docs/SUBMISSION-LIFECYCLE.md; per-cursor turn state follows docs/CURSORS.md.

    Args:
        model: The model the head cursor's turns call.
        session_log: The tree to extend, from a new cursor at its
            :func:`~tau_agent_core.session_log.default_leaf`. Exactly one of
            ``session_log`` and ``cursor`` is required.
        cursor: A position someone already holds, to extend instead.
        system_prompt: The system prompt for the head cursor's turns.
        tools: The built-in tools offered to the model. Two with one name raise
            when the tool list is next assembled.
        extensions: Inline extension factories, each called at construction
            with an api bound to its own runner bucket.
        api_key: The key ``model`` is called with; ``None`` passes no key.
        reasoning: Requested thinking level for every turn and compaction;
            ``None`` sends no reasoning control, which is not ``"off"``
            (docs/REASONING-WIRE.md §4).
        compaction_settings: Settings when no ``compaction_policy`` is given;
            ``None`` means the defaults.
        compaction_policy: A declared policy that supplies its own settings and
            summarizer. Mutually exclusive with ``compaction_settings``. One
            naming a summarizer needs ``model_resolver``, or construction raises.
        extensions_config: Per-extension config keyed by file stem; each
            extension's ``api.config`` is its slice.
        model_resolver: Turns a config model name into a ``Model`` for
            :meth:`set_model` and the policy's summarizer; see
            :meth:`set_model_resolver`.
        max_turns: The loop's turn ceiling, at least 1; ``None`` is no ceiling.
        tool_execution_mode: Whether one response's tool calls run in
            parallel or one at a time.
        bus_available: Whether a bus transport backs this session. An extension
            declaring ``TOUCHES_BUS`` is refused at load when False.
        no_tools: ``"all"`` offers the model no tools at all, extension tools
            included. ``"builtin"`` and ``None`` change nothing here: the caller
            omits built-ins from ``tools``.
        cwd: Where tools and ``@path`` references resolve; ``None`` means the
            process cwd. Stored absolute.
        prompt_templates: The templates ``submit`` expands for ``/name args``;
            none by default.
        skills: The skills ``submit`` expands for ``/skill:name args``; none by
            default.
        tool_allowlist: ``--tools``: when given, only these names are offered,
            built-in or extension-registered. Names are checked once
            extensions have loaded (:meth:`check_tool_filters`, #43).
        tool_denylist: ``--exclude-tools``: names never offered, checked the same way.

    Raises:
        ValueError: Both or neither of ``session_log`` and ``cursor``; both
            compaction arguments; ``max_turns`` below 1; an unknown ``no_tools``.
    """

    def __init__(
        self,
        *,
        model: Model,
        session_log: SessionLog | None = None,
        cursor: Cursor | None = None,
        system_prompt: str = "",
        tools: list[AgentTool] | None = None,
        extensions: list[Callable] | None = None,
        api_key: str | None = None,
        reasoning: str | None = None,
        compaction_settings: CompactionSettings | None = None,
        compaction_policy: CompactionPolicy | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
        model_resolver: Callable[[str], Model] | None = None,
        max_turns: int | None = None,
        tool_execution_mode: Literal["sequential", "parallel"] = "parallel",
        bus_available: bool = False,
        no_tools: Literal["all", "builtin"] | None = None,
        cwd: str | None = None,
        prompt_templates: PromptTemplateCatalog = EMPTY_TEMPLATES,
        skills: SkillCatalog = EMPTY_SKILLS,
        tool_allowlist: Sequence[str] | None = None,
        tool_denylist: Sequence[str] = (),
    ) -> None:
        if cursor is not None and session_log is not None:
            raise ValueError("pass session_log or cursor, not both")
        if cursor is None:
            if session_log is None:
                raise ValueError("pass session_log or cursor: a session needs a tree to extend")
            cursor = Cursor.newest(session_log)
        self._bus_available = bus_available
        self._cursor = cursor
        self._cursors: dict[str, Cursor] = {cursor.id: cursor}
        self._model = model
        self._model_name: str | None = None
        self._cwd = os.path.abspath(cwd) if cwd is not None else os.getcwd()
        self._system_prompt = system_prompt
        self._prompt_templates = prompt_templates
        self._skills = skills
        self._tools: list[AgentTool] = tools or []
        if no_tools not in (None, "all", "builtin"):
            raise ValueError(
                f"no_tools must be 'all', 'builtin' or None, got {no_tools!r} — "
                "this is a resolved policy, not free text; an unrecognised value "
                "would silently mean 'no suppression'."
            )
        self._no_tools = no_tools
        self._tool_allowlist = None if tool_allowlist is None else frozenset(tool_allowlist)
        self._tool_denylist = frozenset(tool_denylist)
        if max_turns is not None and max_turns < 1:
            raise ValueError(
                f"max_turns must be at least 1, got {max_turns} — "
                "no ceiling is spelled None, never 0."
            )
        self._max_turns = max_turns
        self._tool_execution_mode = tool_execution_mode

        self._events = EventBus()
        self._registry = ExtensionRegistry()
        self._vocabulary: Vocabulary | None = None
        self._vocabulary_revision = -1
        self._extensions = extensions or []
        self._extensions_config: dict[str, dict[str, Any]] = extensions_config or {}
        self._loaded_extensions: dict[str, LoadedExtension] = {}
        self._disabled_paths: set[str] = set()
        self._extension_load_errors: list["ExtensionLoadError"] = []
        self._warnings = ExtensionWarnings()
        self._extension_withheld: list[ExtensionRecord] = []
        self._extension_dirs: tuple[str | None, str | None] = (None, None)
        self._extension_start_errors: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._threadsafe_tasks: dict[str, asyncio.Task[Any]] = {}
        self._turn_token_counter: int = 0
        self._forked_tasks: dict[str, asyncio.Task[Any]] = {}
        self._delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._api_key = api_key
        self._reasoning = reasoning
        if compaction_policy is not None and compaction_settings is not None:
            raise ValueError(
                "pass compaction_policy OR compaction_settings, not both: the policy "
                "supplies its own settings, and two sources for the reserve would make "
                "the threshold the policy proves itself against unknowable"
            )
        self._compaction_policy = compaction_policy
        self._model_resolver = model_resolver
        if compaction_policy is not None:
            self._compaction_settings = compaction_policy.compaction_settings
        else:
            self._compaction_settings = compaction_settings or DEFAULT_COMPACTION_SETTINGS
        self._pending_summarizer: str | None = None
        self._configure_compaction(model)
        self._policy_turns_used = 0

        self._token_counter: TextCounter = counter_for(model, fallback=True)
        self._calibrator = ContextCalibrator()
        self._calibration_seen: set[tuple[int, int]] = set()
        self._usage_valid_after = _newest_compaction_ms(self._cursor.entries())

        self._side_usage: dict[str, int] = zero_usage()

        self._session_event_tasks: set[asyncio.Task[None]] = set()

        self._extension_api = self._make_extension_api()
        self._extension_runner = ExtensionRunner(context=self._extension_api.context)
        self._extension_runner.on_error(self._surface_extension_error)
        self._quiet_runner = ExtensionRunner(context=self._extension_api.context)
        self._events.on_error(self._surface_notify_error)
        self._events.on("message_end", self._record_completion_usage)
        for ext in self._extensions:
            ext(self._bind_extension_api(_extension_factory_label(ext)))

    def _frame_config(self) -> dict[str, Any]:
        """The config keys this session's frame sets (docs/CURSORS.md §5).

        ``model_spec`` is the :meth:`get_model` projection; ``model`` the config
        name :meth:`set_model` resolved, present only once one is known, which
        is what ``--continue`` resumes on (docs/TURN-SETTINGS.md §6);
        ``system_prompt_digest`` a sha256 of the prompt, never the text;
        ``tools`` the names the request's ``tools`` block declares, extension
        tools included (an older session with one gains a config entry on first
        resume, docs/CURSORS.md §5); ``extensions`` the inline-factory labels plus every loaded file extension
        not disabled. The API key is never part of it, hashed or otherwise.
        """
        loaded_paths = [
            path for path in self._loaded_extensions if path not in self._disabled_paths
        ]
        model = self._turn_model()
        name = self._turn_model_name()
        return {
            **({} if name is None else {"model": name}),
            "model_spec": {
                "id": model.id,
                "provider": model.provider,
                "context_window": model.context_window,
            },
            "thinking": self._turn_reasoning(),
            "tools": [t.name for t in self._turn_tools()],
            "extensions": [_extension_factory_label(ext) for ext in self._extensions]
            + loaded_paths,
            "cwd": self._cwd,
            "system_prompt_digest": _system_prompt_digest(self._turn_system_prompt()),
        }

    async def _record_config(self, keys: frozenset[str] | None = None) -> None:
        """Write the frame keys that differ from the walk at the turn's cursor.

        Called before a turn's first append and before each later request, so the
        entry sits ahead of what it governs: config takes effect where it is
        recorded (docs/CURSORS.md §5). Writes nothing when the path already says
        the same.

        Args:
            keys: Only these frame keys; ``None`` is all of them.
        """
        cursor = self.acting_cursor
        recorded = config_at(cursor.entries(), cursor.leaf)
        changed = {
            k: v
            for k, v in self._frame_config().items()
            if (keys is None or k in keys) and recorded.get(k, _UNSET) != v
        }
        if changed:
            await cursor.append_config(**changed)

    async def _request_settings(self, persist: bool) -> TurnSettings:
        """The acting cursor's settings for its next request, recorded first if they changed.

        The :class:`~tau_agent_core.agent_loop.AgentLoop` ``turn_settings``
        source. Only the keys a request reads are compared, so a mid-loop config
        entry never claims a change the running loop does not make
        (docs/TURN-SETTINGS.md §3).

        Args:
            persist: The turn's ``store_history``; ``False`` records nothing.
        """
        if persist:
            await self._record_config(_REQUEST_KEYS)
        model = self._turn_model()
        return TurnSettings(
            model=model,
            api_key=self._turn_api_key(),
            reasoning=self._turn_reasoning(),
            temperature=model.temperature,
        )

    @agent_facing(topic="sessions")
    async def start(self) -> None:
        """Record the session's config without running a turn.

        The awaited door onto :meth:`_record_config` for a caller that reads the
        tree before it prompts. ``tau --mode rpc`` calls it once before
        ``RPCHandler.run`` for exactly that reason. Idempotent, and unnecessary
        before a turn: :meth:`submit` and :meth:`continue_conversation` both record
        before reading the pre-turn leaf, so the entry lands AHEAD of the turn it
        governs — which is where a rollback to that leaf needs it to be.
        """
        await self._record_config()

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The model-input context at this session's cursor."""
        return self.acting_cursor.context()

    @property
    def cursor(self) -> Cursor:
        """The position this session's turns extend. Read position from here, never the log.

        Settable: a head that owns its cursor attaches this session to it. A head
        cursor replaced by one on another tree is retired, unless a turn still
        holds it; it would otherwise stay in :attr:`cursors` for good.
        """
        return self._cursor

    @cursor.setter
    def cursor(self, cursor: Cursor) -> None:
        old = self._cursor
        if old is not cursor and old.log is not cursor.log and not old.busy:
            self._cursors.pop(old.id, None)
        self._cursor = cursor
        self._cursors[cursor.id] = cursor

    @property
    def acting_cursor(self) -> Cursor:
        """The cursor the code running now acts on.

        :data:`~tau_agent_core.cursor.TURN_CURSOR` when it is set to one of this
        session's cursors: inside a turn, its cursor, which a hook or tool running in
        it inherits; under ``tau serve``, the cursor a request names. Anywhere else
        :attr:`cursor`.
        """
        cursor = TURN_CURSOR.get()
        if cursor is not None and self._cursors.get(cursor.id) is cursor:
            return cursor
        return self._cursor

    @property
    def cwd(self) -> str:
        """The absolute directory this session's tools and ``@path`` references resolve in."""
        return self._cwd

    @property
    def cursors(self) -> tuple[Cursor, ...]:
        """Every live cursor on this session's trees, the head's included, oldest first."""
        return tuple(self._cursors.values())

    async def open_cursor(
        self, at: str | None, *, owner: Cursor | None = None, label: str = ""
    ) -> Cursor:
        """Open a cursor at ``at`` on the head's tree, live until :meth:`close_cursor`.

        A ``cursor_open`` channel event announces it.

        Args:
            at: The entry to sit on; ``None`` is before the root.
            owner: The cursor the new one answers to: aborting the owner aborts
                it (:meth:`abort`).
            label: What the cursor was opened to do, for display.

        Returns:
            The new cursor, registered with this session.

        Raises:
            ValueError: ``at`` names no entry.
        """
        cursor = Cursor(self._cursor.log, at, owner=owner, label=label)
        self._cursors[cursor.id] = cursor
        await self._events.emit_channel("cursor_open", cursor=cursor)
        return cursor

    async def close_cursor(self, cursor: Cursor) -> None:
        """Retire ``cursor``; a ``cursor_close`` channel event announces it.

        Args:
            cursor: The cursor to retire. One this session does not hold is a
                no-op and announces nothing.

        Raises:
            ValueError: ``cursor`` is the head's, which a head swap replaces instead.
            RuntimeError: a turn still holds it.
        """
        if cursor is self._cursor:
            raise ValueError("close_cursor: the head's cursor is replaced, never closed")
        if cursor.busy:
            raise RuntimeError(f"close_cursor: cursor {cursor.id} is running a turn")
        if self._cursors.pop(cursor.id, None) is not None:
            await self._events.emit_channel("cursor_close", cursor=cursor)

    def _registered(self, cursor: Cursor) -> Cursor:
        """``cursor``, if this session opened it; raise rather than run a stranger's."""
        if self._cursors.get(cursor.id) is not cursor:
            raise ValueError(f"cursor {cursor.id} was not opened by this session")
        return cursor

    @property
    def session_log(self) -> SessionLog:
        """The storage under :attr:`cursor`.

        Settable: a head that swaps sessions (new chat, resume) rebinds this
        session onto another store, and a fresh cursor opens at its default leaf.
        """
        return self._cursor.log

    @session_log.setter
    def session_log(self, log: SessionLog) -> None:
        self.cursor = Cursor.newest(log)
        self._reannounce_warnings()

    def _reannounce_warnings(self) -> None:
        """Show each current warning once more, for the user of the session just swapped to.

        Warnings are extension state and survive the swap (#60), so the store and
        its listeners do not change; only the notices repeat.
        """
        ui = self._extension_api.context.ui
        for warning in self._warnings.current():
            ui.notify(warning.summary(), warning.severity, source=warning.source)

    @property
    def state(self) -> SessionState:
        """Read-only access to session state. Identity is the session UUID (§4.2)."""
        return SessionState(
            session_id=self._cursor.session_id,
            status="running" if self._cursor.is_streaming else "idle",
        )

    @property
    def is_streaming(self) -> bool:
        """Whether the agent loop is currently streaming."""
        return self._cursor.is_streaming

    @property
    def is_aborted(self) -> bool:
        """Whether the CURRENT turn's abort signal has fired.

        docs/REMOTE-CONTROL.md §4[1] T3/G4/G5: `RPCHandler._forward_event`'s
        backpressure wait (`handler.py`'s `_acquire_event_credit`) polls this
        so that a turn told to stop is not ALSO stuck waiting on a host that
        has stopped reading — see that method's docstring for the full
        argument. Reads the SAME `_abort_signal` `abort()`/`is_streaming`
        already read; a fresh one is bound per admitted turn (`submit()`),
        so this is always "is the turn in flight right now aborted", never a
        stale answer from a turn that already finished.
        """
        return self._cursor.abort_signal.is_aborted()

    @property
    def shutdown_requested(self) -> bool:
        """Whether an extension has called `ctx.shutdown()` on this session
        (P3, docs/REMOTE-CONTROL.md §4[7]).

        Delegates to the one `ExtensionContext` every bound extension shares
        (`_extension_api.context` — the same object `ctx.abort()`'s signal
        rebind touches). `RPCHandler`/`transport._read_stdin` checks this
        AFTER dispatching each line (not a poll loop) and, once true, shuts
        down exactly like stdin EOF.
        """
        return self._extension_api.context.shutdown_requested

    @property
    def extension_runner(self) -> ExtensionRunner:
        """The mutating-hook dispatcher (read-only access).

        Mirrors pi's own ``AgentSession.extensionRunner`` getter
        (``agent-session-runtime.ts:137`` reads ``this.session.extensionRunner``
        to fire ``session_before_switch``/``session_before_fork``) — this is the
        SAME seam, added for the SAME reason:
        :class:`~tau_agent_core.agent_session_runtime.AgentSessionRuntime` needs
        to dispatch its own veto hook (H2, docs/REMOTE-CONTROL.md §4[6]) through
        the extensions a session already has bound, without a second dispatch
        mechanism. Not a general extension-authoring seam — extensions register
        via ``api.on(...)``, never through this property.
        """
        return self._extension_runner

    @property
    def persistence_settled(self) -> asyncio.Event:
        """Set except while a turn is between emitting ``agent_end`` and persisting.

        docs/ASYNC-SESSION-LOG.md §3.3. ``RPCHandler._stamp_agent_end_cursor``
        reads the captured cursor's leaf when the writer task dequeues an
        ``agent_end``, and that read is only right if this turn's messages are
        already written. Until the appenders became coroutines that was free:
        :meth:`_run_one_turn` ran from the enqueue through persistence without
        suspending, so the writer could not be scheduled in between. A store
        whose appends really suspend — ``JmftsSessionLog`` hops to a thread —
        breaks that, and the wire carried a null cursor.

        So the ordering is stated rather than inherited. :meth:`_run_one_turn`
        clears this before ``loop.run`` (which is what emits ``agent_end``) and
        sets it in a ``finally`` after both persistence calls, on the error path
        as well. The writer awaits it before framing an ``agent_end``.

        It cannot deadlock against the T3 credit pool: persistence emits no
        events of its own, so nothing it does needs a credit the blocked writer
        would have to release.
        """
        return self._cursor.persistence_settled

    @property
    def turn_lock(self) -> asyncio.Lock:
        """The admission lock a turn holds for its duration (read-only access).

        The SAME object as :attr:`_turn_lock` (see the class docstring for its
        full reentrancy contract) — exposed, read-only, for
        :class:`~tau_agent_core.agent_session_runtime.AgentSessionRuntime`'s H4
        atomicity guarantee: acquiring it and performing a ``session_log`` swap
        synchronously (no ``await`` between acquire and release) is what proves
        (a) no NEW turn can start mid-swap, because every strategy in
        :meth:`submit` either checks ``.locked()`` or awaits this same lock, and
        (b) every ``AgentEvent`` an in-flight turn was going to emit has already
        been dispatched to every subscriber before the swap proceeds —
        :meth:`~tau_agent_core.events.EventBus.emit` awaits each handler to
        completion, and this lock is released only after
        :meth:`~tau_agent_core.agent_loop.AgentLoop.run`'s own await has
        returned and this session's turn-teardown ``finally`` block runs.

        NOT a general-purpose extension seam. The same reentrancy hazard
        :meth:`submit` guards against (see ``_turn_task`` in the class
        docstring) applies here — never acquire this from a hook running on the
        session's own current turn task, or it deadlocks exactly as a reentrant
        ``submit()`` call would.
        """
        return self._cursor.turn_lock

    @property
    @agent_facing(topic="sessions")
    def model_name(self) -> str:
        """The acting cursor's model's ``Model.name``.

        A head that builds models from config names them by their config key,
        the name :meth:`set_model` takes; :meth:`get_model`'s id is not one,
        because two names may resolve to one id.
        """
        return self._turn_model().name

    def get_model(self) -> dict[str, Any]:
        """The acting cursor's model as a small, stable projection (S45).

        Only the fields an extension needs to route, price or gauge a context
        window, keeping the extension API decoupled from the full model schema.

        Returns:
            ``{"id", "provider", "context_window"}`` of the acting cursor's frame
            model, else the session's, read at call time.
        """
        model = self._turn_model()
        return {
            "id": model.id,
            "provider": model.provider,
            "context_window": model.context_window,
        }

    def set_model_resolver(self, resolver: Callable[[str], Model]) -> None:
        """Bind the model-name resolver used by :meth:`set_model` (S45).

        The core does not read ``~/.tau/config.json`` itself; the resolver is the
        seam through which a head supplies its config's models. A resolver with a
        ``compaction(name)`` method is also where compaction comes from: the
        settings and summarizer are re-read from it now, for the current model, and
        on every :meth:`set_model` (docs/PRICING-AND-COMPACTION-CONFIG.md §6). A
        plain ``name -> Model`` resolver keeps whatever settings or policy the
        session was constructed with.

        Args:
            resolver: Maps a config model name to a ``Model``. An ``api_key(name)``
                method supplies that model's key (:meth:`resolve_model`); a
                ``compaction(name)`` method returning a
                :class:`~tau_agent_core.compaction.CompactionConfig` supplies its
                compaction.

        Raises:
            CompactionPolicyError: the summarizer, as ``resolver`` resolves it,
                fails :meth:`CompactionPolicy.bind_to`. Nothing changes, and the
                old resolver stays bound.
        """
        previous = self._model_resolver
        self._model_resolver = resolver
        try:
            self._configure_compaction(self._model)
        except Exception:
            self._model_resolver = previous
            raise
        if callable(getattr(resolver, "compaction", None)):
            self._pending_summarizer = None

    def require_compaction_resolver(self, summarizer: str) -> None:
        """Refuse turns and summaries until a resolver supplies the compaction config.

        A head whose config names ``summarizer`` calls this at construction, before
        it has bound the resolver that can resolve the name; binding one with a
        ``compaction(name)`` method clears it. Until then no turn runs and nothing
        is summarised, rather than compacting through the wrong model.

        Args:
            summarizer: The summarizer the config names, for the refusal message.
        """
        self._pending_summarizer = summarizer

    def _require_compaction_ready(self) -> None:
        """Raise while :meth:`require_compaction_resolver` is outstanding.

        Raises:
            CompactionPolicyError: no compaction-capable resolver is bound yet.
        """
        if self._pending_summarizer is not None:
            raise CompactionPolicyError(
                f"this session's config summarises through {self._pending_summarizer!r}, "
                "but no resolver supplying compaction config is bound; the head must "
                "call set_model_resolver before any turn or summary"
            )

    def _configure_compaction(self, model: Model) -> None:
        """Put in force the compaction the resolver gives ``model``, or re-check the current one.

        Raises:
            CompactionPolicyError: the policy refuses ``model`` or its summarizer.
            TypeError: ``compaction(name)`` returned something other than a
                :class:`~tau_agent_core.compaction.CompactionConfig`.
        """
        compaction_for = getattr(self._model_resolver, "compaction", None)
        if not callable(compaction_for):
            self._bind_policy(self._compaction_policy, model)
            return
        config = compaction_for(model.name)
        if not isinstance(config, CompactionConfig):
            raise TypeError(
                f"resolver.compaction({model.name!r}) returned {type(config).__name__}, "
                "expected a tau_agent_core.compaction.CompactionConfig"
            )
        policy = None
        if config.summarizer is not None:
            policy = CompactionPolicy(
                mode="local_summarizer",
                summarizer=config.summarizer,
                reserve_tokens=config.settings.reserve_tokens,
                keep_recent_tokens=config.settings.keep_recent_tokens,
            )
        self._bind_policy(policy, model)
        self._compaction_policy = policy
        self._compaction_settings = replace(config.settings)

    def _bind_policy(self, policy: CompactionPolicy | None, model: Model) -> None:
        """Check ``policy`` against ``model`` and its resolved summarizer.

        Raises:
            CompactionPolicyError: the policy refuses the pair, or it names a
                summarizer and no model resolver is bound to resolve it.
        """
        if policy is None:
            return
        if policy.summarizer is None:
            policy.bind_to(model)
            return
        if self._model_resolver is None:
            raise CompactionPolicyError(
                f"compaction policy summarises through {policy.summarizer!r}, but this "
                "session has no model resolver to resolve that name; pass "
                "model_resolver= at construction"
            )
        policy.bind_to(model, self.resolve_model(policy.summarizer)[0])

    @property
    @agent_facing(topic="sessions")
    def model_resolver(self) -> Callable[[str], Model] | None:
        """The bound model-name resolver, or ``None`` if none was bound.

        The read behind the ``model_name`` domain: enumerating it means asking the
        resolver which names it accepts, and the resolver is the only object that
        knows. ``None`` is the honest answer for a session nobody called
        :meth:`set_model_resolver` on — :meth:`set_model` would raise on that session
        too, so a caller learns it before offering a choice rather than after.
        """
        return self._model_resolver

    @property
    @agent_facing(topic="sessions")
    def tools(self) -> list[AgentTool]:
        """Every tool a turn draws on: constructor tools, then active extension tools.

        The merge each turn builds, so an extension tool replaces a same-name
        built-in. A cursor frame and declarations narrow it further; see
        :meth:`describe_tools`. A fresh list: constructor tools are the shared
        objects, extension tools are rewrapped on every read. ``[]`` under
        ``no_tools="all"``.

        Raises:
            ValueError: two constructor tools share a name.
        """
        return list(self._build_turn_tools())

    @property
    @agent_facing(topic="sessions")
    def compaction_settings(self) -> CompactionSettings:
        """The compaction settings in force, as a copy.

        A copy because the live object is read by a turn already in flight: handing
        it out is handing out a way to change a running turn's policy from another
        thread of control. :meth:`set_auto_compaction` is the way to change it, and
        it takes the same guard every other mutation takes.
        """
        return replace(self._compaction_settings)

    def set_auto_compaction(self, enabled: bool) -> bool:
        """Turn automatic compaction on or off, and report the effective state.

        Idempotent. Mutates an in-memory field and appends no log entry, so it works
        on an unpersisted session where the appending mutations refuse — and so the
        setting does not survive the process.

        Args:
            enabled: The state to put it in.

        Returns:
            The state after the call, read back off the settings rather than echoed
            from the argument.
        """
        self._compaction_settings.auto = bool(enabled)
        return self._compaction_settings.auto

    @agent_facing(topic="sessions")
    def performed(
        self, mutation: str, data: dict[str, Any], *, flow: str | None = None
    ) -> Performed:
        """Stamp a completed mutation with the cursor its capability declares.

        The one place E5 — "a mutation's completion carries a cursor, a read never
        does" — is applied in process, and it is applied MECHANICALLY: whether the
        cursor rides in ``data`` is read off
        :attr:`~tau_agent_core.capabilities.Capability.returns`, not decided per call
        site. Three copies of that decision is how the Tier B review's findings 5 and
        6 started, on the wire side, where the same rule is now one helper.

        Args:
            mutation: The capability that ran, a key of
                :data:`~tau_agent_core.capabilities.CAPABILITIES`.
            data: What it returned, keyed as its ``returns`` declares, without the
                leaf — this adds that.
            flow: The flow that named the mutation, when one did.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` carrying ``data`` plus the
            resulting leaf.

        Raises:
            KeyError: No capability has that name.
            ValueError: The named capability is a read, or the caller already put a
                ``leaf`` in ``data``. Both are Fail-Early: a read reporting a
                leaf is E5 rule 2 broken, and a hand-supplied leaf is a second
                answer to the question this method exists to answer.
        """
        declared = CAPABILITIES[mutation]
        if declared.kind != "mutation":
            raise ValueError(
                f"performed({mutation!r}) names a read. E5 rule 2: a read never carries "
                "a leaf, so there is no completion here to stamp."
            )
        if "leaf" in data:
            raise ValueError(
                f"performed({mutation!r}) was handed a leaf in `data`. This method is "
                "what puts it there, and two writers of one field is the drift it removes."
            )
        leaf = self.acting_cursor.leaf
        returns = declared.returns or {}
        carries = "leaf" in returns.get("properties", {})
        return Performed(
            flow=flow,
            mutation=mutation,
            data={**data, "leaf": leaf} if carries else dict(data),
            leaf=leaf if carries else None,
        )

    @agent_facing(topic="sessions")
    def get_session_name(self) -> str | None:
        """Read this session's name.

        Returns:
            The newest ``session_info`` name in the log, or ``None`` if never named.
        """
        from tau_agent_core.extension_types import read_session_name

        return read_session_name(self)

    async def set_session_name(self, name: str) -> None:
        """Name this session: a ``session_info`` entry at the cursor, never model input.

        Args:
            name: The new name.

        Raises:
            ValueError: ``name`` is empty.
        """
        from tau_agent_core.extension_types import apply_session_name

        await apply_session_name(self, name)

    def set_model(self, name: str) -> dict[str, Any]:
        """Switch the acting cursor's model by NAME, effective on its next request.

        At the head cursor this is the session's model; at any other cursor that
        cursor's :class:`~tau_agent_core.cursor.TurnFrame` (a cursor with no frame
        gets one that changes nothing else). The key switches with the model, by
        :meth:`resolve_model`, and so does a head switch's compaction when the
        resolver supplies it (:meth:`set_model_resolver`). Called between two
        requests of a running loop — a ``turn_end`` hook — the next request already
        uses it, never one in flight. Nothing is written here: the next request
        records the change ahead of itself (docs/TURN-SETTINGS.md §2-3).

        Args:
            name: A config model name, resolved through :meth:`set_model_resolver`'s
                resolver.

        Returns:
            The new :meth:`get_model` projection.

        Raises:
            RuntimeError: No resolver is bound.
            TypeError: The resolver returned something other than a ``Model``.
            Exception: Whatever the resolver raises for an unknown ``name``,
                unchanged.
            CompactionPolicyError: the new model's compaction refuses it; the
                model does not change.
        """
        if self._model_resolver is None:
            raise RuntimeError(
                f"set_model({name!r}): no model resolver is bound to this AgentSession. "
                "The frontend must call set_model_resolver(...) (a closure over the "
                "config 'models' map) before an extension can switch models by name."
            )
        model, api_key = self.resolve_model(name)
        cursor = self.acting_cursor
        if cursor is not self._cursor:
            cursor.frame = replace(
                self._frame_or_default(cursor), model=model, api_key=api_key, model_name=name
            )
            return self.get_model()
        self._configure_compaction(model)
        self._model = model
        self._model_name = name
        self._api_key = api_key
        return self.get_model()

    def set_reasoning(self, level: str) -> str:
        """Set the acting cursor's requested thinking level, effective on its next request.

        Placed and recorded as :meth:`set_model` is: the head's level is the
        session's, another cursor's lives on its frame, and the next request
        records ``thinking`` ahead of itself. The provider clamps the level to
        what the model supports and reports the one it sent as
        ``AssistantMessage.reasoning_level`` (docs/REASONING-WIRE.md §3).

        Args:
            level: One of ``"off"``, ``"minimal"``, ``"low"``, ``"medium"``,
                ``"high"``, ``"xhigh"``.

        Returns:
            ``level``.

        Raises:
            ValueError: ``level`` is not one of those.
        """
        if level not in EXTENDED_THINKING_LEVELS:
            raise ValueError(
                f"set_reasoning({level!r}): not a thinking level; "
                f"expected one of {', '.join(EXTENDED_THINKING_LEVELS)}"
            )
        cursor = self.acting_cursor
        if cursor is not self._cursor:
            cursor.frame = replace(self._frame_or_default(cursor), reasoning=level)
        else:
            self._reasoning = level
        return level

    def _frame_or_default(self, cursor: Cursor) -> TurnFrame:
        """``cursor``'s frame, or one that changes nothing (every tool, hooks on)."""
        return cursor.frame or TurnFrame(tools=tuple(t.name for t in self.tools), hooks=True)

    def resolve_model(self, name: str) -> tuple[Model, str | None]:
        """The model a config NAME resolves to, and the key it is called with.

        Args:
            name: A config model name.

        The one way a credential enters the session: no other path hands one
        provider's key to another provider's model (docs/CURSORS.md §6).

        Returns:
            ``(model, key)``. The key is the resolver's ``api_key(name)`` when it
            has one, as ``ConfigModelResolver`` does; a plain ``name -> Model``
            resolver knows no keys, so the key is ``None`` (the provider's
            environment variable) unless ``model`` equals the session's model.

        Raises:
            RuntimeError: no resolver is bound.
            TypeError: the resolver returned something other than a ``Model``.
        """
        if self._model_resolver is None:
            raise RuntimeError(f"resolve_model({name!r}): no model resolver is bound")
        model = self._model_resolver(name)
        if not isinstance(model, Model):
            raise TypeError(
                f"resolve_model({name!r}): resolver returned {type(model).__name__}, "
                "expected a tau_llm.types.Model"
            )
        key_for = getattr(self._model_resolver, "api_key", None)
        if callable(key_for):
            return model, key_for(name)
        return model, self._api_key if model == self._model else None

    def model_and_key(self, model: str | Model | None) -> tuple[Model, str | None]:
        """The model ``model`` names and the key it is called with.

        Args:
            model: ``None`` for the session's model and key; a config name,
                through :meth:`resolve_model`; or a ``Model``, which keeps the
                session's key only if it equals the session's model and otherwise
                gets ``None``, the provider's environment variable.

        Returns:
            ``(model, key)``.

        Raises:
            RuntimeError: a name was given and no resolver is bound.
            TypeError: the resolver returned something other than a ``Model``.
        """
        if model is None:
            return self._model, self._api_key
        if isinstance(model, str):
            return self.resolve_model(model)
        return model, self._api_key if model == self._model else None

    def summarizer(self) -> tuple[Model, str | None]:
        """The model and key a compaction or a branch summary runs on (H5 / §16.8).

        Read live, so it tracks :meth:`set_model` and :meth:`set_model_resolver`.

        Returns:
            ``(model, key)``: the session's own model and key unless the policy
            names a summarizer; then :meth:`resolve_model` of that name, the one
            way a credential enters the session (docs/PRICING-AND-COMPACTION-CONFIG.md §4).

        Raises:
            CompactionPolicyError: :meth:`require_compaction_resolver` is outstanding.
        """
        self._require_compaction_ready()
        policy = self._compaction_policy
        if policy is None or policy.summarizer is None:
            return self._model, self._api_key
        return self.resolve_model(policy.summarizer)

    def get_usage(self) -> dict[str, Any] | None:
        """The acting cursor's most recent completion's token usage (S45, anchor G14).

        Returns:
            A deep copy of the ``usage`` dict from the last completion's
            ``message_end`` (``input_tokens``, ``output_tokens``,
            ``cache_read_tokens``, ``cache_write_tokens``, ``total_tokens``,
            ``cost``, ``extra``), so a caller mutating the nested ``cost`` or
            ``extra`` cannot rewrite the record. ``None`` until a completion has
            landed, never a fabricated zero.
        """
        return (
            copy.deepcopy(self.acting_cursor.last_usage)
            if self.acting_cursor.last_usage is not None
            else None
        )

    def record_side_usage(self, usage: dict[str, int]) -> None:
        """Add an out-of-loop completion's tokens to the session's side ledger.

        Called by every path that spends tokens without going through the agent loop.
        Cumulative and monotonic for the session's lifetime; consumers take a
        before/after DELTA to attribute the spend to a particular exchange.

        Args:
            usage: The completion's token counts, added key by key.
        """
        self._side_usage = add_usage(self._side_usage, usage)

    @agent_facing(topic="sessions", since="0.11.0")
    @asynccontextmanager
    async def watch_side_completion(
        self,
        purpose: SideCompletionPurpose,
        reason: SideCompletionReason,
        summarizer_model_id: str,
    ) -> AsyncIterator[_SideCompletionWatch]:
        """Bracket one piece of side work (a compaction or branch summary) with events.

        Emits ``side_completion_start`` on entry, one ``side_completion_update``
        per fragment through the yielded watch, and exactly one
        ``side_completion_end`` on every exit. An exception still gets the end
        (``is_error=True``) and then propagates (docs/STREAMING-SIDE-WORK.md §3).

        Args:
            purpose: Which side completion this is.
            reason: What asked for it; carried on all three events.
            summarizer_model_id: The model id doing the summarising, which is
                often NOT the conversation's — the start event carries it so a
                reader can see what they are paying for.

        Returns:
            An async context manager yielding the watch: ``delta`` is the sink
            to hand the summariser, and ``finish`` records the text and usage
            the end event carries.
        """
        await self._events.emit(
            AgentEvent(
                type="side_completion_start",
                timestamp=self._timestamp(),
                purpose=purpose,
                reason=reason,
                message={"model": summarizer_model_id},
            )
        )
        watch = _SideCompletionWatch(self._events, purpose, reason, self._timestamp)
        try:
            yield watch
        except BaseException as exc:
            await self._events.emit(
                AgentEvent(
                    type="side_completion_end",
                    timestamp=self._timestamp(),
                    purpose=purpose,
                    reason=reason,
                    is_error=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        await self._events.emit(
            AgentEvent(
                type="side_completion_end",
                timestamp=self._timestamp(),
                purpose=purpose,
                reason=reason,
                text=watch.text,
                usage=watch.usage,
            )
        )

    @property
    def side_usage(self) -> dict[str, int]:
        """Cumulative tokens spent on completions OUTSIDE the agent loop.

        A copy — the ledger is the session's own record, and handing out a live alias
        would let one reader's arithmetic rewrite it (see :meth:`get_usage`).
        """
        return dict(self._side_usage)

    @property
    @agent_facing(topic="sessions")
    def is_addressable(self) -> bool:
        """Whether this session is one the store can hand back later.

        :func:`~tau_agent_core.session_log.session_log_is_addressable` asked of the
        bound log. Not a constant: a ``switch_session`` onto an ephemeral session
        changes it under a reader's feet, exactly as the active model does.
        """
        return session_log_is_addressable(self._cursor.log)

    @agent_facing(topic="sessions")
    def get_last_assistant_text(self) -> str | None:
        """The most recent assistant message's text on the active path, or ``None``.

        :func:`~tau_agent_core.messages.last_assistant_text` applied to
        :attr:`messages`, which is where the two skip rules are documented: a turn
        aborted before it said anything is passed over, and only ``text`` blocks
        contribute.

        Returns:
            The concatenated text, stripped, or ``None`` — which covers both "no
            assistant message yet" and "the last one carried no text".
        """
        return last_assistant_text(self.messages)

    @agent_facing(topic="sessions")
    async def summarize_and_navigate(
        self, target_id: str, *, custom_instructions: str | None = None
    ) -> list[dict[str, Any]]:
        """Summarize the subtree at ``target_id``, splice the summary on, and move there.

        The one tree mutation with a method here. The other four —
        :func:`~tau_agent_core.tree_ops.navigate`, ``elide_span``, ``commit_branch``,
        ``paste_subtree`` — take nothing a caller holding a
        :class:`~tau_agent_core.session_log.SessionLog` does not already have, so
        they stay module functions and every head calls them directly. This one
        needs the summarizer model and its key, which are the session's and are not
        on its public surface, and it spends tokens that something has to bank.

        Args:
            target_id: The branch point. The subtree BELOW it is summarized, and the
                ``branch_summary`` entry is parented at it.
            custom_instructions: Extra guidance for the summarizer's system prompt.

        Returns:
            ``ConversationTree.context_for(cursor)`` — the flat message list a head
            swaps into its transcript.

        Raises:
            ValueError: The summarizer returned nothing usable. Raised by
                ``session_manager.summarize_branch``, never fabricated into an
                empty summary here.
        """
        from tau_agent_core.tree_ops import summarize_and_navigate

        model, api_key = self.summarizer()
        messages, usage = await summarize_and_navigate(
            self.acting_cursor,
            target_id,
            model,
            api_key=api_key,
            custom_instructions=custom_instructions,
        )
        self.record_side_usage(usage)
        return messages

    @agent_facing(topic="sessions")
    def get_last_compaction(self) -> CompactionRecord | None:
        """The newest ``compaction`` on this session's cursor path, or ``None``.

        The path, not the log: a compaction on a sibling branch never shaped this
        cursor's context (docs/CURSORS.md §2).

        Returns:
            A :class:`CompactionRecord`, or ``None`` if this path never compacted.
        """
        for entry in reversed(self.acting_cursor.tree().path()):
            if entry.get("type") != "compaction":
                continue
            return CompactionRecord(
                id=str(entry["id"]),
                timestamp=str(entry.get("timestamp", "")),
                summary=str(entry.get("summary", "")),
                first_kept_id=entry.get("firstKeptId"),
                tokens_before=entry.get("tokensBefore"),
            )
        return None

    @agent_facing(topic="sessions")
    def get_session_stats(self) -> SessionStats:
        """Token accounting for this session, and the compaction settings in force.

        The one call behind the ``get_session_stats`` capability. Every field was
        already readable one at a time; what this adds is that two heads asking the
        question get the same answer, computed once.

        Returns:
            A :class:`SessionStats`. Nothing here mutates, and it answers the same
            on a persisted and an unpersisted session.
        """
        estimate = self.context_estimate()
        context_window = int(self.get_model()["context_window"])
        return SessionStats(
            context=estimate,
            context_window=context_window,
            context_headroom=context_window - estimate.tokens,
            compaction_settings=self.compaction_settings,
            last_compaction=self.get_last_compaction(),
            usage=self.get_usage(),
        )

    def _record_completion_usage(self, event: AgentEvent) -> None:
        """Capture this completion's usage from a ``message_end`` event (S45).

        Only the per-completion ``message_end`` carries a ``usage`` dict (the
        duplicate tool-turn ``message_end`` ``run()`` also emits has none, so it is
        skipped — no stale overwrite). Runs before extension handlers (registered
        first at construction), so ``get_usage()`` is current when they fire.
        """
        message = event.message
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.acting_cursor.last_usage = copy.deepcopy(usage)

    def subscribe(self, handler: Callable[[AgentEvent], Any]) -> Callable[[], None]:
        """Subscribe to agent events. Returns unsubscribe function.

        Args:
            handler: Callable that receives AgentEvent instances.

        Returns:
            Unsubscribe function that removes the handler.

        Example:
            >>> unsub = session.subscribe(lambda event: print(event.type))
            >>> unsub()  # Remove the subscription
        """
        return self._events.on("all", handler)

    def subscribe_channel(self, channel: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """Subscribe to one of the bus's string channels, outside the ``AgentEvent`` stream.

        :meth:`subscribe` covers ``AgentEvent``. The channels a renderer cares
        about (handlers take these as keywords):

        - ``"submission_start"`` — ``(submission, text, images, cursor)``, once per
          admitted submission that will actually run a turn, before the loop starts.
          ``cursor`` is the one it extends; a sub-agent's has an ``owner``. The
          submission-level bracket ``agent_start`` is not: a followUp re-entry runs a
          second ``loop.run()`` inside one ``submit()``.
        - ``"submission_end"`` — ``(submission, side_usage)``, in a ``finally``, so it
          arrives however the turn ended.
        - ``"cursor_open"`` / ``"cursor_close"`` — ``(cursor)``, when
          :meth:`open_cursor` / :meth:`close_cursor` run (docs/CURSORS.md §4).

        Args:
            channel: The channel name.
            handler: Sync or async; an exception it raises is surfaced through
                the bus's ``on_error`` sink, never swallowed.

        Returns:
            A callable that removes ``handler``.
        """
        return self._events.on(channel, handler)

    def route_session_event(self, event: dict[str, Any]) -> None:
        """Route a coding-agent session-lifecycle event onto the extension bus.

        Re-emits the event on the string channel named by its ``type``, so
        ``api.on("session_before_compact", handler)`` fires. These are channels,
        NOT members of the ``AgentEvent`` Literal (§E3c.4, §7 decision E3-c).
        The coding-agent layer registers this with its seam-3 emitter
        (``subscribe_session_events``); tau-agent-core never imports
        ``session_store``. The emit is scheduled as a fire-and-forget task.

        Args:
            event: ``{"type": <name>, "session": <Session>, **extra}`` for
                ``session_start`` / ``session_before_fork`` /
                ``session_before_compact`` / ``session_shutdown``; handlers
                receive the whole dict.

        Raises:
            RuntimeError: No event loop is running (Fail-Early, no synchronous
                fallback).
        """
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._events.emit_channel(str(event["type"]), event))
        self._session_event_tasks.add(task)
        task.add_done_callback(self._session_event_tasks.discard)

    async def emit_session_start(self, reason: str = "startup", *, strict: bool = False) -> None:
        """Bind this session to the running loop, then fire ``session_start`` (S41).

        Call it after extensions are loaded, so a handler finds its registration
        in place. The loop binding (which :meth:`submit_threadsafe` marshals to)
        happens whether or not anything listens. A handler's exception is
        reported through the runner's error sink (S44), the remaining handlers
        still run, and :meth:`extension_records` shows the extension as failed.

        Args:
            reason: Why the session began, as pi's ``SessionStartEvent.reason``:
                ``"startup"``, ``"reload"``, ``"new"``, ``"resume"`` or ``"fork"``.
            strict: Raise once dispatch is done if an extension named with
                ``-e`` raised (#47). ``-p`` and ``--mode rpc`` pass True.

        Raises:
            ExtensionStartupError: ``strict``, and an explicit extension's
                handler raised; the first such extension is named.
        """
        self._bind_loop()
        if not self._extension_runner.has_handlers("session_start"):
            return
        failures: list[Any] = []

        def _collect(error: Any) -> None:
            if error.event == "session_start":
                failures.append(error)

        stop = self._extension_runner.on_error(_collect)
        try:
            await self._extension_runner.emit_session_start(
                {"type": "session_start", "reason": reason}
            )
        finally:
            stop()
        for failure in failures:
            self._extension_start_errors[failure.extension_path] = failure.error
        if not strict:
            return
        for failure in failures:
            loaded = self._loaded_extensions.get(failure.extension_path)
            if loaded is not None and loaded.explicit:
                from tau_agent_core.sdk import ExtensionStartupError

                raise ExtensionStartupError(
                    extension_identifier(failure.extension_path),
                    failure.extension_path,
                    "session_start",
                    failure.error,
                )

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        """Fire the ``session_shutdown`` hook (S41) after cancelling background work.

        The teardown counterpart to :meth:`emit_session_start`, dispatched through
        the runner (error-surfaced, not swallowed). The frontends fire it on TUI
        quit, headless completion and SIGINT/SIGTERM.

        First, and whether or not any extension listens, it cancels and awaits
        every forked branch, :meth:`deliver_queued` delivery and marshalled
        submission: none has an awaiting caller, and none may outlive the
        session (docs/SUBMISSION-LIFECYCLE.md "fork").

        Args:
            reason: As pi's ``SessionShutdownEvent.reason``: ``"quit"``,
                ``"reload"``, ``"new"``, ``"resume"`` or ``"fork"``.
        """
        await self._cancel_forked_tasks()
        await self._cancel_task_registry(self._delivery_tasks)
        await self._cancel_task_registry(self._threadsafe_tasks)
        if not self._extension_runner.has_handlers("session_shutdown"):
            return
        await self._extension_runner.emit_session_shutdown(
            {"type": "session_shutdown", "reason": reason}
        )

    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
        collect_explicit_errors: bool = False,
        store_path: str | None = None,
    ) -> LoadExtensionsResult:
        """Load the authorized and the explicitly named extensions into THIS session.

        :func:`~tau_agent_core.sdk.plan_extensions` decides what loads
        (docs/EXTENSION-PACKAGES.md §5). Each extension's ``register(api)`` runs
        against an api bound to its own runner bucket (:meth:`_bind_extension_api`),
        in load order, so its hooks fire in this session's loop. It claims the
        running loop as the session's, as a turn does, so a thread an extension
        starts here reaches that loop. Nothing is printed; the caller surfaces
        ``errors``, ``withheld`` and ``grandfathered``.

        Args:
            explicit_paths: ``-e`` values, paths or module names. A failure
                here raises unless ``collect_explicit_errors``.
            discover: Whether to discover drop-ins and packages; ``-ne`` sets
                False. ``explicit_paths`` load either way.
            user_dir: The drop-in directory; ``None`` means
                ``<tau home>/extensions``.
            extensions_config: Per-extension config keyed by identifier (S40),
                stored BEFORE binding so each ``api.config`` is sliced from it.
                ``None`` keeps the current map.
            collect_explicit_errors: Collect an explicit failure into ``errors``
                like a discovered one, and keep loading (the TUI cannot abort
                mid-load).
            store_path: The authorization store; ``None`` is the file beside
                the drop-in directory. :meth:`authorize_extension` writes the same one.

        Returns:
            The extensions loaded, the failures collected and the sources
            withheld. The failures and withheld sources also replace what
            :meth:`extension_records` reports.

        Raises:
            ValueError: A ``--tools``/``--exclude-tools`` name matches no tool
                once loading is done (:meth:`check_tool_filters`).
            ExtensionStartupError: An explicit extension failed to import or
                register (#47), with the original as ``__cause__``; not raised
                when ``collect_explicit_errors`` is set.
        """
        self._bind_loop()
        if extensions_config is not None:
            self._extensions_config = extensions_config

        from tau_agent_core.sdk import _load_extensions

        self._extension_dirs = (user_dir, store_path)
        result = await _load_extensions(
            explicit_paths,
            discover=discover,
            user_dir=user_dir,
            api_factory=self._bind_extension_api,
            collect_explicit_errors=collect_explicit_errors,
            bus_available=self._bus_available,
            store_path=store_path,
        )
        for loaded in result.extensions:
            self._loaded_extensions[loaded.path] = loaded
            self._disabled_paths.discard(loaded.path)
        self._extension_load_errors = list(result.errors)
        self._sync_load_warnings()
        self._extension_withheld = list(result.withheld)
        self.check_tool_filters()
        for label, topics in self.orphan_subscriptions().items():
            _EXT_LOG.debug(
                "%s subscribes to %s, but no loaded extension emits under that name (#44)",
                extension_identifier(label),
                ", ".join(topics),
            )
        return result

    @agent_facing(topic="extensions")
    def get_extension_state(self) -> "LoadExtensionsResult":
        """Every managed extension and every file that failed to load, read LIVE.

        The read the ``/extensions`` listing is built from. It is not the value
        :meth:`load_extensions` returned: the extensions come from
        ``_loaded_extensions``, which :meth:`reload_extension` REPLACES, so a listing
        rendered from this reflects a reload and the load-time snapshot did not — the
        TUI cached that snapshot and showed the pre-reload tool list.

        Load errors are the one half that cannot be recomputed, so they are kept from
        the last :meth:`load_extensions` call. A file that failed to import is in here
        and can never be a legal ``extension_name`` value, which is why this is a read
        of its own rather than the ``extension_name`` domain: the domain answers "what
        may I bind", and this answers "what is the state of the extension system".

        Returns:
            A :class:`~tau_agent_core.sdk.LoadExtensionsResult` — the same type the
            loader returns, so the listing formatter and the loader cannot disagree
            about the shape. Whether each extension is currently enabled is the
            separate read :meth:`list_managed_extensions`; a caller that wants both
            composes them.
        """
        from tau_agent_core.sdk import LoadExtensionsResult

        return LoadExtensionsResult(
            extensions=list(self._loaded_extensions.values()),
            errors=list(self._extension_load_errors),
        )

    @agent_facing(topic="extensions")
    def extension_warnings(self) -> list[ExtensionWarning]:
        """Every current extension warning (#60).

        Extension state, not session state: kept across new, fork and switch
        until its writer clears or replaces it, or its extension is disabled
        or reloaded. τ mirrors each extension that failed to load as a
        ``"tau"`` error keyed ``load:<label>``.

        Returns:
            The warnings, oldest key first.
        """
        return self._warnings.current()

    def _sync_load_warnings(self) -> None:
        """Mirror the load failures into the store, quietly: every head already announced them."""
        wanted = {f"load:{e.path}": e for e in self._extension_load_errors}
        with self._warnings.batch():
            for w in self._warnings.current():
                if w.source == TAU_SOURCE and w.key.startswith("load:") and w.key not in wanted:
                    self._warnings.clear(w.source, w.key)
            for key, error in wanted.items():
                lines = error.error.strip().splitlines()
                detail = f": {lines[0]}" if lines else ""
                name = extension_identifier(error.path)
                self._warnings.set(
                    ExtensionWarning(TAU_SOURCE, key, f"{name} failed to load{detail}", "error")
                )

    def _on_own_loop(self, change: Callable[[], object]) -> None:
        """Make a warnings change where it is safe, without ever claiming the session's loop.

        While the session's bound loop runs, the change runs on it: inline when
        called there, otherwise marshalled with ``call_soon_threadsafe``, so
        listeners (serve's publish, the TUI's marker) see only that loop. With
        no live bound loop the change runs here under the store's lock, which is
        safe only while no listener depends on a loop that never bound (an SDK
        host under ``asyncio.run`` before any turn, ``load_extensions`` or
        ``session_start``; no τ mode does this). Only those three bind a loop; a
        warning write never does (#60).
        """
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        bound = self._loop
        if bound is not None and self._loop_owns_session(bound) and running is not bound:
            bound.call_soon_threadsafe(change)
            return
        change()

    @agent_facing(topic="extensions")
    def set_extension_warning(
        self, source: str, key: str, message: str, severity: str = "warning"
    ) -> None:
        """Set the warning ``key`` from ``source``, replacing one already there.

        An extension calls :meth:`ExtensionAPI.set_warning`, which names itself;
        τ writes with ``source="tau"``. A new or changed warning is also shown
        once through ``ui.notify`` at ``severity``. Safe from any thread: the
        warning is checked here, then set by :meth:`_on_own_loop`. Marshalled
        from another thread, it returns before the warning lands, and a
        listener's exception goes to the loop's exception handler, not here.

        Args:
            source: An extension identifier, or ``"tau"``.
            key: The warning's name within ``source``.
            message: One line.
            severity: ``"warning"`` or ``"error"``.

        Raises:
            ValueError: a non-string or blank source, key or message, a message
                of more than one line, or an unknown severity.
        """
        warning = ExtensionWarning(source, key, message, cast(WarningSeverity, severity))
        validate_warning(warning)

        def change() -> None:
            if self._warnings.set(warning):
                self._extension_api.context.ui.notify(warning.summary(), severity, source=source)

        self._on_own_loop(change)

    @agent_facing(topic="extensions")
    def clear_extension_warning(self, source: str, key: str) -> None:
        """Remove a warning; clearing an absent one does nothing. Safe from any thread.

        Args:
            source: The extension identifier, or ``"tau"``.
            key: The warning's name within ``source``.
        """
        self._on_own_loop(lambda: self._warnings.clear(source, key))

    def subscribe_extension_warnings(
        self, listener: Callable[[list[ExtensionWarning]], None]
    ) -> Callable[[], None]:
        """Call ``listener`` with every current warning after each change.

        Args:
            listener: Called once per change, on the session's loop once one is
                bound, under the store's lock: it must not block or wait on
                another thread.

        Returns:
            The unsubscribe.
        """
        return self._warnings.subscribe(listener)

    @agent_facing(topic="extensions")
    def extension_records(self) -> list[ExtensionRecord]:
        """Every extension this session knows of and its state: the ``tau ext list`` rows, live.

        Loaded and disabled extensions, those that failed, and those found and
        withheld (docs/EXTENSION-PACKAGES.md §7). A loaded row carries the
        topics it subscribed to that no loaded extension emits (#44).

        Returns:
            The records, from
            :func:`~tau_agent_core.sdk.session_extension_records`.
        """
        from tau_agent_core.sdk import session_extension_records

        return session_extension_records(
            list(self._loaded_extensions.values()),
            self._disabled_paths,
            self._extension_load_errors,
            self._extension_withheld,
            self.orphan_subscriptions(),
            self._extension_start_errors,
        )

    def orphan_subscriptions(self) -> dict[str, tuple[str, ...]]:
        """Per enabled extension, the ``ext:`` topics it subscribed to whose emitter is not loaded (#44).

        A topic's emitter is its middle segment, which every emit derives from
        the emitting extension's own identifier, so an emitter that is not
        loaded can never deliver (docs/EXTENSION-PACKAGES.md §10).

        Returns:
            ``{label: topics}``, for extensions with at least one such topic.
        """
        from tau_agent_core.extension_types import EXT_CHANNEL_PREFIX

        emitters = {bucket.name for bucket in self._extension_runner._extensions}
        found: dict[str, tuple[str, ...]] = {}
        for bucket in self._extension_runner._extensions:
            orphans = []
            for topic in bucket.subscriptions:
                emitter = topic[len(EXT_CHANNEL_PREFIX) :].rpartition(":")[0]
                if emitter not in emitters:
                    orphans.append(topic)
            if orphans:
                found[bucket.path] = tuple(orphans)
        return found

    async def authorize_extension(self, token: str) -> ExtensionActionResult:
        """Authorize a withheld, discovered extension and load it into this session.

        Writes the authorization record (docs/EXTENSION-PACKAGES.md §5), loads
        the source, and fires its ``session_start`` (reason ``"enable"``). A
        discovered extension that fails to load is reported, not raised, as at
        startup.

        Args:
            token: The withheld extension's identifier, or its last dotted segment.

        Returns:
            The outcome, with ``action`` ``"authorize"``.

        Raises:
            AuthorizationStoreError: The store cannot be read.
        """
        from tau_agent_core.extension_auth import AuthorizationStore, store_path_for
        from tau_agent_core.extension_sources import default_extensions_dir
        from tau_agent_core.sdk import ExtensionLoadError, _load_source, find_discovered_extension

        pending = {r.identifier: r for r in self._extension_withheld if r.state == "unauthorized"}
        try:
            ident = resolve_shorthand(token, list(pending))
        except ValueError as err:
            return ExtensionActionResult("authorize", token, False, str(err))
        if ident is None:
            return ExtensionActionResult(
                "authorize", token, False, f"no extension awaiting authorization is named {token!r}"
            )
        record = pending[ident]
        user_dir, store_path = self._extension_dirs
        directory = Path(user_dir) if user_dir is not None else default_extensions_dir()
        kind: Literal["package", "dropin"] = "dropin" if record.kind == "dropin" else "package"
        target = record.target if record.kind == "dropin" else ident
        try:
            source = find_discovered_extension(target, extensions_dir=directory, kind=kind)
        except ValueError as err:
            return ExtensionActionResult("authorize", ident, False, str(err))
        store = AuthorizationStore.load(store_path or store_path_for(directory))
        store.authorize(source)
        store.save()
        self._extension_withheld = [r for r in self._extension_withheld if r is not record]
        try:
            loaded = await _load_source(
                source, self._bind_extension_api, bus_available=self._bus_available
            )
        except Exception as exc:
            self._extension_load_errors.append(
                ExtensionLoadError(path=source.label, error=str(exc), source=source)
            )
            self._sync_load_warnings()
            return ExtensionActionResult(
                "authorize",
                source.label,
                False,
                f"authorized {ident}, but it failed to load: {exc}",
            )
        self._loaded_extensions[loaded.path] = loaded
        bucket = self._extension_runner.get_extension(loaded.path)
        if bucket is not None:
            await self._extension_runner.emit_session_start_for(
                bucket, {"type": "session_start", "reason": "enable"}
            )
        return ExtensionActionResult(
            "authorize", loaded.path, True, f"authorized and loaded {ident}"
        )

    def list_managed_extensions(self) -> list[tuple[str, bool]]:
        """Every file extension under management, enabled or not.

        Returns:
            ``(path, enabled)`` in load order; ``enabled`` is False exactly when
            :meth:`disable_extension` removed its bucket and nothing re-bound it.
        """
        return [(path, path not in self._disabled_paths) for path in self._loaded_extensions]

    def get_extension_config(self, path: str) -> dict[str, Any]:
        """One extension's declared config schema and its current values.

        The read a settings screen is built from. ``schema`` is the extension's
        ``CONFIG_SCHEMA``, normalized at load into ``{title, fields}`` — the same
        shape ``ui.form`` takes, so a head that can render a form can render this
        with no new widget. ``values`` is the live slice ``api.config`` returns
        for the extension, keyed by file stem.

        Args:
            path: A managed path or a unique file stem.

        Returns:
            ``{path, schema, values}``. ``schema`` is ``None`` for an extension
            that declares none — the honest answer, and the one that tells a head
            to offer no settings screen rather than an empty one.

        Raises:
            ValueError: ``path`` resolves to no managed extension.
        """
        target = self.resolve_extension_target(path)
        if target is None:
            raise ValueError(f"no loaded extension {path!r}")
        loaded = self._loaded_extensions[target]
        return {
            "path": target,
            "schema": loaded.config_schema,
            "values": dict(self._extensions_config.get(extension_identifier(target), {})),
        }

    async def set_extension_config(
        self, path: str, values: dict[str, Any]
    ) -> ExtensionActionResult:
        """Replace an extension's config slice and reload it so the values take.

        The write half of :meth:`get_extension_config`. ``values`` is checked
        against the declared schema first: an undeclared key, or a value whose
        Python type does not match its field's kind, RAISES rather than being
        dropped — a settings screen that silently discards a key is the failure
        this schema exists to prevent.

        The reload is what makes the change visible: ``api.config`` is captured
        when the extension's API is bound, so a slice written without one would
        be read by nobody until the next run.

        Persistence is head-local and deliberately not done here. The core does
        not own ``~/.tau/config.json`` — ``resolve_extensions_config`` in
        ``tau_coding_agent.headless`` reads it and hands the merged map in — so
        these values last for this session, and a head that wants them to survive
        writes the file itself.

        Args:
            path: A managed path or a unique file stem.
            values: The complete new slice. Not merged: what is passed is what
                the extension will read.

        Returns:
            The reload's :class:`ExtensionActionResult`, with ``action`` set to
            ``"configure"``.

        Raises:
            ValueError: ``path`` resolves to nothing, the extension declares no
                schema, or ``values`` does not satisfy the schema.
        """
        target = self.resolve_extension_target(path)
        if target is None:
            raise ValueError(f"no loaded extension {path!r}")
        schema = self._loaded_extensions[target].config_schema
        if schema is None:
            raise ValueError(
                f"{extension_identifier(target)} declares no CONFIG_SCHEMA, so there is no "
                "contract to check these values against. Refusing to write a slice "
                "the extension never said it reads."
            )
        from tau_agent_core.extension_types import validate_form_values

        try:
            validate_form_values(schema["fields"], values)
        except ValueError as err:
            raise ValueError(f"{extension_identifier(target)} config: {err}") from err
        self._extensions_config[extension_identifier(target)] = dict(values)
        result = await self.reload_extension(target)
        return ExtensionActionResult("configure", result.path, result.ok, result.message)

    def resolve_extension_target(self, token: str) -> str | None:
        """Resolve what a user typed to a managed extension's label.

        ``token`` may be the label itself, the identifier, or the identifier's
        last dotted segment (docs/EXTENSION-PACKAGES.md §4), resolved fresh.

        Args:
            token: A label, an identifier, or a shorthand segment.

        Returns:
            The managed label, or ``None`` when nothing matches.

        Raises:
            ValueError: The shorthand matches two extensions; the message names both.
        """
        if token in self._loaded_extensions:
            return token
        by_ident = {extension_identifier(label): label for label in self._loaded_extensions}
        resolved = resolve_shorthand(token, list(by_ident))
        return None if resolved is None else by_ident[resolved]

    def _resolve_or_refuse(self, action: str, token: str) -> str | ExtensionActionResult:
        """:meth:`resolve_extension_target`, or the ``ok=False`` result that says why not."""
        try:
            target = self.resolve_extension_target(token)
        except ValueError as err:
            return ExtensionActionResult(action, token, False, str(err))
        if target is None:
            return ExtensionActionResult(action, token, False, f"no loaded extension {token!r}")
        return target

    def _unregister_bucket(self, bucket: Any) -> None:
        """Unwind a bucket's registry contributions (tools/commands/shortcuts) — S70.

        The runner bucket is the only per-extension record of WHICH names an
        extension registered (the registry is a flat, unattributed map, D-E5-5). On
        disable/reload we remove exactly those names so a disabled extension's tools
        and slash-commands stop being offered. Idempotent at the registry (a name a
        later extension overwrote is simply absent).
        """
        for name in bucket.tools:
            self._registry.unregister_tool(name)
        for name in bucket.commands:
            self._registry.unregister_command(name)
        for key in bucket.shortcuts:
            self._registry.unregister_shortcut(key)
        self._warnings.clear_source(bucket.name)

    async def disable_extension(self, path: str) -> ExtensionActionResult:
        """Tear down and detach a loaded extension so its hooks stop firing (S70).

        Fires the extension's own ``session_shutdown`` (reason ``"disable"``) FIRST —
        the S41 teardown seam, so a watcher/exit-commit runs cleanly — then removes its
        runner bucket (hooks stop) and unwinds its registry tools/commands/shortcuts.
        The ``LoadedExtension`` record is KEPT so :meth:`enable_extension` can bring it
        back. If the extension's lock held the cursor, the cursor moves off it.

        Args:
            path: A managed path or a unique file stem.

        Returns:
            The outcome; ``ok=False`` for an unknown or already-disabled
            extension, not an error.
        """
        target = self._resolve_or_refuse("disable", path)
        if isinstance(target, ExtensionActionResult):
            return target
        bucket = self._extension_runner.get_extension(target)
        if bucket is None:
            return ExtensionActionResult(
                "disable", target, False, f"{extension_identifier(target)} is already disabled"
            )
        await self._extension_runner.emit_session_shutdown_for(
            bucket, {"type": "session_shutdown", "reason": "disable"}
        )
        self._extension_runner.remove_extension(target)
        self._unregister_bucket(bucket)
        self._disabled_paths.add(target)
        message = f"disabled {extension_identifier(target)}"
        if await self._release_lock_of(target):
            message += ", releasing its lock on the session"
        return ExtensionActionResult("disable", target, True, message)

    async def _release_lock_of(self, path: str) -> bool:
        """Move the cursor off ``path``'s locking request, if that is where it sits.

        docs/EXTENSION-LOCKS.md §6, escape 3. Navigation, not a special case in
        the lock check: the cursor moving IS the release, so this is the same
        release the user gets by branching, reached by a different gesture.
        Returns whether it moved anything — false in the ordinary case, where the
        extension held no lock.
        """
        entries = self.acting_cursor.entries()
        request = request_at(entries, self.acting_cursor.leaf)
        if request is None or not request.lock or request.extension != path:
            return False
        # The REQUEST's parent, not the cursor's: the cursor may be a provenance node above it.
        parent = next(
            (e.get("parentId") for e in reversed(entries) if str(e.get("id")) == request.entry_id),
            None,
        )
        await self._record_config()
        self.acting_cursor.move(str(parent) if parent is not None else None)
        return True

    async def enable_extension(self, path: str) -> ExtensionActionResult:
        """Re-bind a disabled extension by re-invoking its ``register`` (S70).

        Binds a FRESH runner bucket (its tools/commands/shortcuts re-enter the
        registry) and re-invokes the stored ``register(api)`` — the same entry point
        the loader called — then fires ``session_start`` (reason ``"enable"``) so a
        watcher re-installs.

        Args:
            path: A managed path or a unique file stem.

        Returns:
            The outcome; ``ok=False`` for an unknown or already-enabled extension.
        """
        target = self._resolve_or_refuse("enable", path)
        if isinstance(target, ExtensionActionResult):
            return target
        if self._extension_runner.get_extension(target) is not None:
            return ExtensionActionResult(
                "enable", target, False, f"{extension_identifier(target)} is already enabled"
            )
        loaded = self._loaded_extensions[target]
        api = self._bind_extension_api(target)
        outcome = loaded.register(api)
        if inspect.isawaitable(outcome):
            await outcome
        from tau_agent_core.sdk import LoadedExtension

        self._loaded_extensions[target] = LoadedExtension(
            path=target,
            register=loaded.register,
            api=api,
            content_hash=loaded.content_hash,
            subjects=loaded.subjects,
            touches_bus=loaded.touches_bus,
            config_schema=loaded.config_schema,
            source=loaded.source,
        )
        self._disabled_paths.discard(target)
        bucket = self._extension_runner.get_extension(target)
        if bucket is not None:
            await self._extension_runner.emit_session_start_for(
                bucket, {"type": "session_start", "reason": "enable"}
            )
        return ExtensionActionResult(
            "enable", target, True, f"enabled {extension_identifier(target)}"
        )

    async def reload_extension(self, path: str) -> ExtensionActionResult:
        """Tear down, re-import from disk, and re-register an extension (S70).

        Fires ``session_shutdown`` (reason ``"reload"``) for the current instance (if
        enabled), removes its bucket + registry entries, then RE-IMPORTS the file fresh
        (a new module object — code edits on disk take effect) and re-invokes
        ``register`` against a fresh bucket, finally firing ``session_start`` (reason
        ``"reload"``). Works on a disabled extension too, and leaves it enabled.

        Args:
            path: A managed path or a unique file stem.

        Returns:
            The outcome; ``ok=False`` only for an unknown extension.

        Raises:
            FileNotFoundError: The file is gone from disk.
            ExtensionCapabilityError: The new code declares a capability this
                session lacks, or an invalid ``CONFIG_SCHEMA``.
            Exception: Whatever the module's import or ``register`` raises,
                unchanged. In every case the extension is left torn down, never
                reported as ``ok=False``.
        """
        target = self._resolve_or_refuse("reload", path)
        if isinstance(target, ExtensionActionResult):
            return target
        bucket = self._extension_runner.get_extension(target)
        if bucket is not None:
            await self._extension_runner.emit_session_shutdown_for(
                bucket, {"type": "session_shutdown", "reason": "reload"}
            )
            self._extension_runner.remove_extension(target)
            self._unregister_bucket(bucket)
        from tau_agent_core.extension_sources import ExtensionSource
        from tau_agent_core.sdk import _load_source

        previous = self._loaded_extensions[target].source
        source = previous or ExtensionSource(
            identifier=extension_identifier(target), kind="path", target=target
        )
        new_loaded = await _load_source(
            source, self._bind_extension_api, bus_available=self._bus_available, reload=True
        )
        self._loaded_extensions[target] = new_loaded
        self._disabled_paths.discard(target)
        new_bucket = self._extension_runner.get_extension(target)
        if new_bucket is not None:
            await self._extension_runner.emit_session_start_for(
                new_bucket, {"type": "session_start", "reason": "reload"}
            )
        return ExtensionActionResult(
            "reload", target, True, f"reloaded {extension_identifier(target)}"
        )

    def _turn_cap(self) -> dict[str, Any]:
        """The ``max_turns`` kwarg for this session's loop, or nothing.

        Returns an EMPTY dict when unset rather than a number, so ``AgentLoopConfig``'s
        own default stays the single definition of the default ceiling — copying it here
        would be a second source of truth that silently drifts. That default is now
        ``None``: no ceiling, until a caller states one.
        """
        frame = self.acting_cursor.frame
        max_turns = self._max_turns if frame is None else frame.max_turns
        return {} if max_turns is None else {"max_turns": max_turns}

    def _turn_model(self) -> Model:
        """The model the running turn calls: its cursor frame's, else the session's."""
        frame = self.acting_cursor.frame
        return self._model if frame is None or frame.model is None else frame.model

    def _turn_api_key(self) -> str | None:
        """The key :meth:`_turn_model` is called with: its frame's, else the session's."""
        frame = self.acting_cursor.frame
        return self._api_key if frame is None or frame.model is None else frame.api_key

    def _turn_model_name(self) -> str | None:
        """The config name of :meth:`_turn_model`, or ``None`` when none is known."""
        frame = self.acting_cursor.frame
        return self._model_name if frame is None or frame.model is None else frame.model_name

    def _turn_reasoning(self) -> str | None:
        """The thinking level the running turn requests: its frame's, else the session's."""
        frame = self.acting_cursor.frame
        return self._reasoning if frame is None or frame.reasoning is None else frame.reasoning

    def _turn_system_prompt(self) -> str:
        """The prompt the running turn runs under: its cursor frame's, else the session's."""
        frame = self.acting_cursor.frame
        if frame is None or frame.system_prompt is None:
            return self._system_prompt
        return frame.system_prompt

    def _turn_tools(self) -> list[AgentTool]:
        """The tools the running turn declares to the model, narrowed by the frame.

        Only an explicit :meth:`redeclare_tools` changes this list; an exposure
        override never does, so nothing a model or extension does on its own
        changes the request's ``tools`` block (experiments/tool-exposure/RESULTS.md).
        """
        declaration = self._declaration_overrides()
        return self._narrowed(
            [
                t
                for t in self._build_turn_tools()
                if declaration.get(t.name, t.exposure in DECLARED_EXPOSURES)
            ]
        )

    def _callable_tools(self) -> list[AgentTool]:
        """The tools ``ctx.execute_tool`` may reach on the acting cursor (experimental)."""
        overrides = self._exposure_overrides()
        return self._narrowed(
            [
                t
                for t in self._build_turn_tools()
                if overrides.get(t.name, t.exposure) in CALLABLE_EXPOSURES
            ]
        )

    def _narrowed(self, tools: list[AgentTool]) -> list[AgentTool]:
        """``tools`` narrowed by the acting cursor's frame allowlist, if it has one."""
        frame = self.acting_cursor.frame
        if frame is None:
            return tools
        allowed = set(frame.tools)
        return [t for t in tools if t.name in allowed]

    def _config_map(self, key: str) -> dict[str, Any]:
        """The ``key`` map folded on the acting cursor's path; ``{}`` if none was recorded."""
        cursor = self.acting_cursor
        found: dict[str, Any] = config_at(cursor.entries(), cursor.leaf).get(key) or {}
        return found

    def _exposure_overrides(self) -> dict[str, str]:
        """The ``tool_exposure`` map on the acting cursor's path."""
        return self._config_map("tool_exposure")

    def _declaration_overrides(self) -> dict[str, bool]:
        """The ``tool_declaration`` map on the acting cursor's path."""
        return self._config_map("tool_declaration")

    def _model_call_refusal(self, name: str) -> str | None:
        """Why a model-issued call to declared tool ``name`` is refused, or ``None``.

        A masked tool stays declared, to keep the prompt cache, and is refused
        here instead (experiments/tool-exposure/RESULTS.md).
        """
        exposure = self._exposure_overrides().get(name)
        if exposure is None or exposure in DECLARED_EXPOSURES:
            return None
        return (
            f"Tool {name!r} is not available in this conversation: it was set to "
            f"{exposure!r}. Do not call it again."
        )

    @agent_facing(topic="extensions")
    def describe_tools(self, *, include_hidden: bool = False) -> list[ToolDescription]:
        """Every tool on the acting cursor, as discovery reports it (experimental).

        A cursor frame's allowlist applies.

        Args:
            include_hidden: Also report tools whose effective exposure is ``hidden``.

        Returns:
            One description per tool, in registry order, with the effective
            exposure and whether the ``tools`` block declares it.
        """
        overrides = self._exposure_overrides()
        declared = {t.name for t in self._turn_tools()}
        found = []
        for tool in self._narrowed(self._build_turn_tools()):
            exposure = overrides.get(tool.name, tool.exposure)
            if exposure == "hidden" and not include_hidden:
                continue
            found.append(describe_tool(tool, exposure, tool.name in declared))
        return found

    @agent_facing(topic="extensions")
    async def set_tool_exposure(self, name: str, exposure: str) -> None:
        """Override ``name``'s exposure from the acting cursor's leaf on (experimental).

        The declared ``tools`` block does not change, so the prompt cache holds.
        The override is a ``tool_exposure`` config entry, and the model is told by
        an appended ``tool_exposure`` message on the path: now when no turn is
        running, before the next completion when one is. A masked tool stays
        declared and a model call to it is refused with the reason. Repeating the
        exposure ``name`` already has, by override or by default, writes nothing.

        Args:
            name: The tool to override.
            exposure: The new exposure.

        Raises:
            ValueError: ``exposure`` is not one of :data:`~tau_llm.tools.ToolExposure`,
                or no tool is named ``name``.
        """
        if exposure not in TOOL_EXPOSURES:
            raise ValueError(f"set_tool_exposure: {exposure!r} is not one of {TOOL_EXPOSURES}")
        tool = next((t for t in self._build_turn_tools() if t.name == name), None)
        if tool is None:
            raise ValueError(f"set_tool_exposure: no tool named {name!r}")
        overrides = dict(self._exposure_overrides())
        if overrides.get(name, tool.exposure) == exposure:
            return
        overrides[name] = exposure
        await self._record_config()
        cursor = self.acting_cursor
        await cursor.append_config(tool_exposure=overrides)
        text = EXPOSURE_NOTICES[exposure].format(name=name)
        if cursor.is_streaming:
            cursor.steer_queue.append(
                create_custom_message(
                    custom_type=EXPOSURE_NOTICE_TYPE,
                    content=text,
                    visible_to_model=True,
                    timestamp=self._timestamp(),
                )
            )
        else:
            await self._append_custom_message(
                {"customType": EXPOSURE_NOTICE_TYPE, "content": text},
                {"visible_to_model": True},
            )

    @agent_facing(topic="extensions")
    def preview_redeclare(self, changes: dict[str, bool]) -> ToolBlockChange:
        """What :meth:`redeclare_tools` with ``changes`` would do to the ``tools`` block.

        A head calls this to warn the user before a change that re-bills the
        prompt. Writes nothing.

        Args:
            changes: Tool name to ``True`` (declare) or ``False`` (withdraw),
                laid over the declarations already on the path.

        Returns:
            :func:`~tau_agent_core.prompt_cache.tool_block_change` from the
            current block to the one ``changes`` would produce.

        Raises:
            ValueError: a name is not a tool here.
        """
        tools = self._build_turn_tools()
        unknown = sorted(set(changes) - {t.name for t in tools})
        if unknown:
            raise ValueError(f"redeclare_tools: no tool named {unknown}")
        declaration = {**self._declaration_overrides(), **changes}
        after = self._narrowed(
            [t for t in tools if declaration.get(t.name, t.exposure in DECLARED_EXPOSURES)]
        )
        return tool_block_change(self._turn_tools(), after)

    def _declared_names(self, declaration: dict[str, bool]) -> set[str]:
        """The tools ``declaration`` declares, before any cursor frame narrows them."""
        return {
            t.name
            for t in self._build_turn_tools()
            if declaration.get(t.name, t.exposure in DECLARED_EXPOSURES)
        }

    @agent_facing(topic="extensions")
    async def redeclare_tools(self, changes: dict[str, bool]) -> ToolBlockChange:
        """Declare (``True``) or withdraw (``False``) tools from the model's ``tools`` block.

        A user action, never something a tool or extension does on its own: any
        change here costs a full prompt-cache miss on the next request. Recorded as
        a ``tool_declaration`` config entry, so a resume declares the same set; a
        change that leaves every tool's declaration as it was writes nothing.

        Args:
            changes: Tool name to ``True`` (declare) or ``False`` (withdraw).

        Returns:
            The change, as :meth:`preview_redeclare` reported it.

        Raises:
            ValueError: a name is not a tool here.
        """
        change = self.preview_redeclare(changes)
        current = self._declaration_overrides()
        declaration = {**current, **changes}
        if self._declared_names(declaration) != self._declared_names(current):
            await self._record_config()
            await self.acting_cursor.append_config(tool_declaration=declaration)
        return change

    @agent_facing(topic="extensions")
    def list_extensions(self) -> list[ExtensionInfo]:
        """The extensions loaded and enabled now, in load order (experimental).

        Returns:
            One :class:`~tau_agent_core.tool_catalog.ExtensionInfo` per extension.
        """
        found = []
        for bucket in self._extension_runner._extensions:
            found.append(
                ExtensionInfo(
                    name=bucket.name,
                    path=bucket.path,
                    tools=tuple(bucket.tools),
                    commands=tuple(bucket.commands),
                )
            )
        return found

    @agent_facing(topic="extensions")
    async def execute_tool(
        self, name: str, args: dict[str, Any], *, parent_tool_call_id: str | None = None
    ) -> NestedToolResult:
        """Run tool ``name`` through the pipeline a model-issued call takes (experimental).

        See :func:`~tau_agent_core.tool_pipeline.run_nested_call`, which raises
        on every failure; the tools it may reach are the acting cursor's callable
        ones, and the hooks are the ones that cursor's turns run.

        Args:
            name: The tool to run.
            args: Its arguments.
            parent_tool_call_id: The parent call, only when not inside a running tool.

        Returns:
            The tool's structured value, content and details.
        """
        cursor = self.acting_cursor
        return await run_nested_call(
            tools={t.name: t for t in self._callable_tools()},
            dispatcher=self._hooks(),
            emit=self._emit_stamped,
            signal=cursor.abort_signal,
            name=name,
            args=args,
            parent_tool_call_id=parent_tool_call_id,
        )

    def _hooks(self) -> ExtensionRunner:
        """The runner the running turn dispatches hooks through.

        A cursor whose frame turns hooks off gets a runner with no registrations,
        which is every hook point answering "no handlers" (docs/CURSORS.md §6).
        """
        frame = self.acting_cursor.frame
        return (
            self._quiet_runner if frame is not None and not frame.hooks else self._extension_runner
        )

    async def _reserve_turn_or_reject(self, cursor: Cursor) -> bool:
        """Reserve the in-flight-turn slot if free; ``False`` without blocking if not.

        The non-blocking half of admission's concurrency guard — LangGraph's
        ``multitask_strategy`` "reject" (docs/SUBMISSION-LIFECYCLE.md "The one
        door" step 1). Checking ``locked()`` and then ``acquire()``-ing with no
        ``await`` between the two is race-free under asyncio's cooperative
        scheduling: nothing else can run between the two calls, and an
        uncontended ``Lock.acquire()`` never actually suspends (CPython's fast
        path sets ``_locked`` and returns synchronously without touching the
        running loop), so this coroutine never blocks despite being declared
        ``async``. Shared by :meth:`submit` (``multitask_strategy="reject"``)
        and :meth:`continue_conversation` — the "two unguarded doors" the spec
        calls out by name; the latter has no ``Submission``/``SubmissionResult``
        to carry a refusal through, so it turns a ``False`` here into a raise.
        """
        if cursor.turn_lock.locked():
            return False
        await cursor.turn_lock.acquire()
        return True

    # -- Task marshalling (docs/SUBMISSION-LIFECYCLE.md phase 4) --------------

    @staticmethod
    def _loop_owns_session(loop: asyncio.AbstractEventLoop) -> bool:
        """Is ``loop`` still a live owner, or a dead binding to be replaced?

        A loop that is closed, or open but no longer running, cannot be servicing
        this session: nothing is scheduled on it and nothing ever will be until
        someone runs it again. Treating such a binding as an owner would break the
        pattern half this suite is written in — ``asyncio.run(session.prompt(...))``
        called more than once against one long-lived session creates a NEW loop per
        call, so the second call is legitimately a different loop and must be
        allowed to take ownership.

        The residual honesty gap, stated rather than hidden: a loop that is alive
        but momentarily not running (a ``run_until_complete`` between calls) also
        reads as dead here, so a genuinely concurrent thread could take ownership in
        that window. There is no way to tell those two apart from outside the loop —
        ``is_running()`` is the only signal asyncio offers — and the alternative
        (treat every non-running loop as an owner) would refuse the sequential
        pattern above, which is real, in favour of a pattern nothing in this
        codebase uses.
        """
        return not loop.is_closed() and loop.is_running()

    def _bind_loop(self) -> None:
        """Claim the running loop as this session's owner if no live one holds it.

        The observation-grade half of "Task marshalling", called where the session
        demonstrably starts running on a loop (:meth:`load_extensions`,
        :meth:`emit_session_start`, :meth:`_bind_or_check_loop`), so
        :meth:`submit_threadsafe` has a loop to marshal to before the first
        submission: ``nats_bus`` subscribes in ``session_start`` and its thread may
        submit at once.

        Silent with no running loop or a live owner already bound; :meth:`submit`
        is where a conflict is a hard error.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        bound = self._loop
        if bound is None or bound is running or not self._loop_owns_session(bound):
            self._loop = running

    def _bind_or_check_loop(self, op: str) -> asyncio.AbstractEventLoop:
        """Verify this call is on the session's own loop, or RAISE naming the fix.

        docs/SUBMISSION-LIFECYCLE.md "Task marshalling", in the spec's own words:
        ``submit()`` is safe only from the session's own loop, and calling it from a
        foreign one must be a hard error "rather than working by accident. A silent
        fallback here produces exactly the 'bus disconnected randomly' class of bug
        that is investigated as a network problem for hours." This is Neovim's
        ``E5560`` (``:h api-fast``: most API functions are deferred, and calling an
        editor-state function from a fast context is an error fixed with
        ``vim.schedule_wrap``) and Textual's ``call_from_thread`` split, applied to
        the one door.

        Deliberately NOT auto-detection that reroutes: no ``call_soon_threadsafe``
        fallback, no quiet hand-off to :meth:`submit_threadsafe`. A caller whose
        concurrency assumption is wrong is told so, at the call site, with the fix
        named — because the failure it prevents (two loops mutating
        :attr:`_turn_lock`, the log cursor and the event bus) does not present as an
        exception, it presents as a corrupted transcript hours later.

        Three cases:

        - **No running loop at all** — a plain thread driving this coroutine.
          Raises: there is nothing here to await on.
        - **A different, LIVE loop** — the genuine foreign-loop case. Raises.
        - **The bound loop, or no live binding** — proceeds, claiming the binding.
          "No live binding" covers both the first call on a fresh session and the
          sequential ``asyncio.run`` pattern (see :meth:`_loop_owns_session`).

        Args:
            op: The calling method, named in the error message.

        Returns:
            The loop this session is bound to, which is the running one.

        Raises:
            RuntimeError: called with no running loop, or from a different loop
                than the live one this session is bound to.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                f"{op} was called with no running event loop — from a plain "
                "thread, or from a coroutine being driven by something other "
                "than asyncio. It touches session state that only the session's "
                "own loop may touch (the turn lock, the session log cursor, the "
                "event bus), and there is no loop here to await on. Marshal it "
                "instead: session.submit_threadsafe(sub) enqueues the submission "
                "onto the session's loop and hands back a "
                "concurrent.futures.Future[SubmissionResult]. "
                "(docs/SUBMISSION-LIFECYCLE.md 'Task marshalling'; Neovim's "
                "E5560/vim.schedule_wrap, Textual's call_from_thread.)"
            ) from None
        bound = self._loop
        if bound is not running and bound is not None and self._loop_owns_session(bound):
            raise RuntimeError(
                f"{op} was called from a different event loop ({running!r}) than "
                f"the one this session is bound to ({bound!r}), which is still "
                "running. A NATS callback, a timer or a webhook handler fires in "
                "a context this session does not own, and running a turn from "
                "there races the owning loop over the turn lock, the session log "
                "cursor and the event bus — a corruption that surfaces as a "
                "transcript that makes no sense, not as an exception here. "
                "Marshal it instead: session.submit_threadsafe(sub) enqueues the "
                "submission onto the session's own loop and hands back a "
                "concurrent.futures.Future[SubmissionResult]. "
                "(docs/SUBMISSION-LIFECYCLE.md 'Task marshalling'; Neovim's "
                "E5560/vim.schedule_wrap, Textual's call_from_thread.)"
            )
        self._loop = running
        return running

    def submit_threadsafe(
        self, sub: Submission, *, context: list[dict[str, Any]] | None = None
    ) -> concurrent.futures.Future[SubmissionResult]:
        """Submit from a foreign loop or thread — the marshalling door.

        docs/SUBMISSION-LIFECYCLE.md "Task marshalling". :meth:`submit` refuses a
        foreign caller; this is what that refusal names. **Synchronous by design**:
        the caller may have no event loop at all (a paho-mqtt client thread, a
        ``watchdog`` observer, a WSGI request thread), so there is nothing for it to
        ``await``.

        The submission is enqueued onto the session's own loop —
        ``loop.call_soon_threadsafe``, whose callback queue IS the queue the session
        drains, in FIFO order, on its own loop — and run there through the ordinary
        :meth:`submit`, so every admission semantic is the one the caller asked for.

        **Each marshalled submission gets its own task, deliberately.** A single
        drainer that awaited each ``submit()`` to completion before taking the next
        would quietly convert ``multitask_strategy="reject"`` into ``"enqueue"``:
        two bus events arriving while a turn is in flight would both eventually run
        instead of the second being refused. ``nats_bus`` picked ``"reject"`` for a
        stated reason — "silently queueing would make the agent answer a stale
        utterance minutes later" — and a marshalling layer that overrides the
        strategy on the way in is precisely the per-source divergence this lifecycle
        exists to delete. Ordering is still FIFO: the callbacks run in submission
        order, each creates its task before the next runs, and ``submit()``'s
        admission takes the lock without suspending, so the first to arrive is the
        first admitted.

        Args:
            sub: The submission, built by the caller exactly as for :meth:`submit`.
            context: Same meaning as :meth:`submit`'s — a caller-supplied working
                message list. A foreign thread rarely has one; it is threaded here
                so this method is a complete counterpart rather than a subset.

        Returns:
            A :class:`concurrent.futures.Future` resolving to the
            :class:`~tau_agent_core.submission.SubmissionResult` — the standard
            cross-thread handle (``asyncio.run_coroutine_threadsafe``'s return type).
            A caller that wants the answer blocks on it; a fire-and-forget caller
            drops it, and any exception is still surfaced through the session's
            extension-error sink rather than vanishing (see
            :meth:`_on_threadsafe_task_done`). Cancelling it before the loop picks
            it up prevents the turn from running at all.

        Raises:
            RuntimeError: this session is not bound to a live loop, so there is
                nothing to marshal onto — Fail-Early rather than queueing into a
                void that may never be drained. Bind it by running the session on
                its loop first (``emit_session_start``, or any ``submit``).
            RuntimeError: called from the session's OWN loop, where it is a
                mistake with a silent cost: the returned future can only be
                resolved by that same loop, so blocking on it deadlocks. Await
                :meth:`submit` (or ``asyncio.create_task`` it) instead. This is
                Textual's ``call_from_thread`` rule — "must run in a different
                thread from the app" — not a stylistic preference.
        """
        loop = self._loop
        if loop is None or not self._loop_owns_session(loop):
            raise RuntimeError(
                "submit_threadsafe(): this session is not bound to a running "
                f"event loop (bound={loop!r}), so there is nothing to marshal "
                "the submission onto. The binding is claimed the first time the "
                "session runs on a loop — emit_session_start(), or any submit() "
                "— so a driver thread started before that must wait for it. "
                "Queueing into a loop that may never run it would be exactly the "
                "silent drop docs/SUBMISSION-LIFECYCLE.md 'Task marshalling' "
                "refuses."
            )
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "submit_threadsafe() was called from the session's OWN event "
                "loop. Marshalling here buys nothing and costs correctness: the "
                "returned concurrent.futures.Future can only be completed by this "
                "same loop, so blocking on it deadlocks. Use `await "
                "session.submit(sub)`, or asyncio.create_task(session.submit(sub)) "
                "if you do not want to wait. (Textual's call_from_thread has the "
                "same rule for the same reason.)"
            )
        future: concurrent.futures.Future[SubmissionResult] = concurrent.futures.Future()
        loop.call_soon_threadsafe(self._accept_threadsafe, sub, context, future)
        return future

    def _accept_threadsafe(
        self,
        sub: Submission,
        context: list[dict[str, Any]] | None,
        future: concurrent.futures.Future[SubmissionResult],
    ) -> None:
        """Take one marshalled submission off the loop's queue — ON the session's loop.

        The other side of :meth:`submit_threadsafe`'s ``call_soon_threadsafe``.
        Registers the driving task in :attr:`_threadsafe_tasks` for the same two
        reasons ``_forked_tasks`` exists: nothing on this loop awaits it (so a bare
        ``create_task`` is collectable mid-turn), and a submission admitted just
        before shutdown must not outlive the session.

        A ``submission_id`` already in flight is REFUSED rather than overwritten:
        the registry is keyed by it, so a second entry would silently drop the
        first task's only strong reference — losing both supervision and the
        shutdown drain for a turn that is still running.
        """
        if sub.submission_id in self._threadsafe_tasks:
            future.set_exception(
                RuntimeError(
                    f"submit_threadsafe(): submission_id {sub.submission_id!r} is "
                    "already in flight on this session. Ids key the supervised "
                    "registry, so accepting this one would drop the running "
                    "submission's only reference. Mint a fresh id per submission "
                    "(uuid4().hex, as every built-in submitter does)."
                )
            )
            return
        if not future.set_running_or_notify_cancel():
            return
        task = asyncio.get_running_loop().create_task(
            self._run_threadsafe_submission(sub, context, future)
        )
        self._threadsafe_tasks[sub.submission_id] = task
        task.add_done_callback(lambda t: self._on_threadsafe_task_done(sub.submission_id, t))

    async def _run_threadsafe_submission(
        self,
        sub: Submission,
        context: list[dict[str, Any]] | None,
        future: concurrent.futures.Future[SubmissionResult],
    ) -> SubmissionResult:
        """Run one marshalled submission through the ordinary door, resolving ``future``.

        Nothing is special-cased: this is :meth:`submit`, on the session's own loop,
        with the caller's own :class:`~tau_agent_core.submission.Submission`. The
        only addition is transporting the outcome — result OR exception — back
        across the thread boundary, because a caller in another thread cannot see
        this task.
        """
        try:
            result = await self.submit(sub, context=context)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        future.set_result(result)
        return result

    def _on_threadsafe_task_done(self, submission_id: str, task: asyncio.Task[Any]) -> None:
        """Untrack a finished marshalled submission; surface its exception (Fail-Early).

        Mirrors :meth:`_on_fork_task_done`. The exception is already on the caller's
        future, but a fire-and-forget caller never looks at it and
        ``concurrent.futures.Future`` — unlike asyncio's — logs nothing when an
        exception is never retrieved. Surfacing it through the session's own error
        sink is what keeps a failing bus-driven turn from being invisible; a caller
        that DOES read the future simply sees it twice, which is the right way round.
        """
        self._threadsafe_tasks.pop(submission_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._surface_extension_error(
                ExtensionError(
                    extension_path=f"submit_threadsafe:{submission_id}",
                    event="submit_threadsafe",
                    error=str(error),
                )
            )

    def _stamp_event(self, event: AgentEvent) -> AgentEvent:
        """Attach the turn's cursor and its submission's provenance to ``event``.

        docs/SUBMISSION-LIFECYCLE.md "Provenance on events" — Jupyter's
        ``parent_header``: a *copy* of the causing submission's identity, so a
        renderer decides HOW to show a turn without the core knowing any renderer
        exists. ``cursor_id`` says WHICH turn, now that several run at once
        (docs/CURSORS.md §6). A copy, because :class:`AgentEvent` is shared with
        whatever emitted it. The four submission fields stay unset for a turn
        no submission drove (``continue_conversation()``), rather than fabricated.
        """
        cursor = self.acting_cursor
        update: dict[str, Any] = {"cursor_id": cursor.id}
        sub = cursor.submission
        if sub is not None:
            update.update(
                submission_id=sub.submission_id,
                source=sub.source,
                submitter=sub.submitter,
                correlation=dict(sub.correlation),
            )
        return event.model_copy(update=update)

    async def _emit_stamped(self, event: AgentEvent) -> None:
        """The ``emit=`` callable a submission-driven turn's :class:`AgentLoop` gets.

        Wraps :attr:`_events` (the real bus) with :meth:`_stamp_event` so every
        event this turn produces carries the current submission's provenance —
        the SAME bus, SAME fire-and-forget contract (``EventBus.emit``), nothing
        added except the stamp.
        """
        await self._events.emit(self._stamp_event(event))

    def _submission_runs_a_turn(self, sub: Submission) -> bool:
        """Whether ``sub`` will reach the model, rather than write nothing at all.

        Read before the pre-turn leaf is captured, to decide whether to record the
        config (:meth:`_record_config`). The record has to happen THERE and not
        later: a rollback refuses when the pre-turn leaf is ``None``, so a first
        turn on an empty log needs the entry written before the leaf is read.

        But two submissions promise to write NOTHING, and both are pinned:
        ``store_history=False`` (test_submit_admission.py) and a command dispatch,
        which returns before any turn (test_submit_commands.py). Recording for
        either would put a config entry in a log the caller was told stays
        untouched.

        The command test is the same one :meth:`_apply_input_pipeline` makes, run
        against the PRE-hook text. The two can disagree — an ``input`` hook can
        rewrite a slash command in or out — and this errs by running the drain for
        a submission that turns out to dispatch, which writes a record early
        rather than writing a wrong one.

        Args:
            sub: The submission about to be admitted.

        Returns:
            Whether to expect a turn that persists.
        """
        if not sub.store_history:
            return False
        if sub.expand_commands and self.resolve_command(sub.text) is not None:
            return False
        return True

    def resolve_command(self, text: str) -> CommandInvocation | None:
        """Would ``text`` dispatch as a command, and which one? (``submit()`` step 3.)

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3; :mod:`tau_agent_core.commands`.

        Pure: it decides, it does not run, and it does not decide whether dispatch
        is ALLOWED (:meth:`submit` gates that on ``sub.expand_commands``). A head
        asks it too, before rendering a user bubble for a ``/tree`` that will never
        be a turn, so both get the same answer. It reads the live extension-command
        registry; see :func:`~tau_agent_core.commands.resolve_command` for the
        ordering rule (built-ins win).

        Args:
            text: The submitted text.

        Returns:
            The command it would dispatch as, or ``None`` for ordinary text,
            including an unknown ``/…``, which goes to the model. An unresolvable
            ``/ext:…`` comes back with ``refusal`` set, which :meth:`submit` refuses.
        """
        return resolve_command(text, self._registry.command_names())

    async def _perform_command(self, invocation: CommandInvocation) -> Dispatched:
        """Do the half of a resolved command the CORE can do, and report the rest.

        Returns whichever of the four arms the command IS, never a flag saying who
        should run it:

        - an extension-registered command RUNS here, through the same
          :meth:`run_extension_command` the palette and the panel-action path use, so
          there is one implementation of "invoke a registered command" rather than one
          per frontend. Its returned value is coerced to display text
          (:meth:`ExtensionCommandResult.output_text`) because that is a string any
          frontend can render, and reported as a
          :class:`~tau_agent_core.flows.Performed` whose ``data`` is ``{"output": …}``.
        - a built-in FLOW is STEPPED — the typed text bound through
          :func:`~tau_agent_core.flows.bind_command_args`, then
          :func:`~tau_agent_core.flows.next_step` — so ``/resume`` with no reference is
          a :class:`~tau_agent_core.flows.FlowStep` a head renders as its picker, and
          ``/name the refactor`` is a :class:`~tau_agent_core.flows.Ready`.
        - a VIEW command is a :class:`~tau_agent_core.flows.View`. The core cannot push
          a screen, and pretending to have run one would be worse than saying it did
          not.

        ``/extensions <verb> <target>`` is rewritten here into the flow that verb names
        (:data:`~tau_agent_core.commands.EXTENSION_VIEW_VERBS`), because a ``View``
        carries no argument string — a head left holding that sugar would have had the
        target silently dropped.

        Fail-Early: a command that :meth:`resolve_command` found but
        :meth:`run_extension_command` no longer knows (an extension unloaded across the
        ``await`` that got us here) RAISES. Falling through to "send it to the model"
        would ship the user's ``/note buy milk`` to an LLM as a prompt.
        """
        vocabulary = self.vocabulary
        if invocation.origin == "builtin" or vocabulary.flow(invocation.name) is not None:
            dispatched = dispatch_builtin(
                invocation.name,
                invocation.args,
                leaf=self.acting_cursor.leaf,
                vocabulary=vocabulary,
            )
            if isinstance(dispatched, Ready) and invocation.origin == "extension":
                return await self._run_extension_flow(dispatched)
            return dispatched

        name, args = invocation.name, invocation.args
        result = await self.run_extension_command(name, args)
        if not result.handled:
            raise UnsupportedCommandError(
                f"/{name} resolved as a registered extension command but "
                "run_extension_command reports it unknown — it was unregistered between "
                "the dispatch decision and the dispatch itself (an extension reload or "
                "disable). Refusing rather than falling through to the model, which "
                "would send the command text to an LLM as a prompt."
            )
        return Performed(flow=None, mutation=name, data={"output": result.output_text()})

    async def _apply_input_pipeline(
        self, sub: Submission
    ) -> tuple[str, list[dict] | None, SubmissionResult | None]:
        """:meth:`submit` steps 2-3 — the ``input`` hook chain, then command dispatch.

        A line that is not a command but invokes a prompt template or a skill is
        expanded here, after the hooks, so the expansion is the text the turn
        persists and every head renders (docs/PROMPT-TEMPLATES-AND-SKILLS.md §6).

        Returns ``(text, images, early)``. ``early`` is non-``None`` when the
        submission was fully handled without a turn: an ``input`` handler CONSUMED
        it (``handled``), or ``expand_commands`` resolved it to a command. In both
        cases the submission was still ACCEPTED — admitted and acted on, just not
        by a model turn — which is exactly the distinction
        :attr:`~tau_agent_core.submission.SubmissionResult.command` carries.

        Extracted from :meth:`submit`'s body so the ``"steer"`` branch can run the
        SAME pipeline on the SAME terms. A steer that skipped it would be the one
        input source in the harness whose text never reaches the transform chain —
        the exact per-source divergence docs/SUBMISSION-LIFECYCLE.md exists to
        remove — and pi runs it for steering messages too (``prompt()`` emits
        ``input`` and handles extension commands BEFORE it looks at
        ``streamingBehavior``, ``agent-session.ts:995-1036``).
        """
        text = sub.text
        images = sub.images
        hooks = self._hooks()
        if hooks.has_handlers("input"):
            input_result = await hooks.emit_input(
                text, images, source=sub.source, submitter=sub.submitter
            )
            if input_result["handled"]:
                return (
                    text,
                    images,
                    SubmissionResult(accepted=True, submission_id=sub.submission_id, messages=[]),
                )
            text = input_result["prompt"]
            images = input_result["images"]

        if sub.expand_commands:
            invocation = self.resolve_command(text)
            if invocation is not None and invocation.refusal is not None:
                return (
                    text,
                    images,
                    SubmissionResult(
                        accepted=False,
                        submission_id=sub.submission_id,
                        rejection_reason=invocation.refusal,
                    ),
                )
            if invocation is not None:
                return (
                    text,
                    images,
                    SubmissionResult(
                        accepted=True,
                        submission_id=sub.submission_id,
                        command=await self._perform_command(invocation),
                    ),
                )
            expanded = expand_prompt_command(text, self._prompt_templates, self._skills)
            if expanded is not None:
                text = expanded

        return text, images, None

    async def submit(
        self,
        sub: Submission,
        *,
        context: list[dict[str, Any]] | None = None,
        on_admitted: Callable[[], None] | None = None,
        cursor: Cursor | None = None,
    ) -> SubmissionResult:
        """The single admission point every input source funnels through.

        Reference: docs/SUBMISSION-LIFECYCLE.md, "The one door". In order:

        1. **Admission** by ``sub.multitask_strategy``, against ``cursor``'s own
           lock, queues and abort signal, so turns on two cursors run concurrently
           (docs/CURSORS.md §2). ``"reject"`` refuses if a turn is in flight;
           ``"enqueue"`` waits for it, and a user :meth:`abort` refuses every
           waiting enqueue; ``"steer"`` puts the text on the running
           turn's steer queue, delivered before its next LLM call, or runs a turn
           if none is in flight; ``"rollback"`` aborts the in-flight turn, waits,
           and moves the cursor back to that turn's pre-turn leaf, refusing if a
           different turn ran in between (the lock is FIFO, so a rollback does not
           jump an already-queued enqueue); ``"fork"`` checks the leaf is
           turn-complete and runs a supervised :meth:`spawn` in the background.
        2. **The ``input`` hook chain**, which may transform or consume the text.
        3. **Command dispatch**, only when ``sub.expand_commands``: a resolved
           command (:meth:`resolve_command`) runs or steps instead of a turn and is
           reported on ``SubmissionResult.command``; an unresolvable ``/ext:…``
           name is refused (``accepted=False``, #57). Off by default, which is a
           security property: a bus payload starting ``/compact`` stays prompt text.
        4. **A pending extension lock** refuses the turn.
        5. **The turn**, persisted unless ``sub.store_history`` is False, bracketed
           by ``submission_start``/``submission_end`` channel events, with every
           ``AgentEvent`` stamped with the submission's provenance.

        For the turn's lifetime, ``sub.allow_user_input`` decides whether extension
        dialogs may reach a TUI delegate, and the derived submission depth is
        published so a self-continuing extension hits ``MAX_SUBMISSION_DEPTH``.

        Args:
            sub: The submission.
            context: The model-input context to use instead of the cursor's path;
                the TUI passes its working message list.
            on_admitted: Called once, with no arguments, when this call commits to
                running a turn (after the input pipeline, never on a path that
                returns without one). It must not raise: an exception propagates
                out of this method before the turn starts.
            cursor: The cursor the turn extends; a cursor this session opened.
                ``None`` means :attr:`cursor`.

        Returns:
            The outcome: ``accepted=False`` with a ``rejection_reason`` when
            refused; otherwise the turn's messages, or none for a steer, a fork, a
            consumed input or a command.

        Raises:
            ValueError: ``cursor`` was not opened by this session.
            RuntimeError: Called off the session's own event loop (use
                :meth:`submit_threadsafe`); the derived depth exceeds
                ``MAX_SUBMISSION_DEPTH``; or the call is reentrant, from a hook on
                the task already running this cursor's turn, which would deadlock.
            NotImplementedError: ``sub.multitask_strategy`` is unknown, or
                ``sub.silent`` is True (only its ``store_history=False`` half
                exists).
            UnsupportedCommandError: A resolved command was unregistered before
                it ran.
        """
        self._bind_or_check_loop("submit()")
        cur = self._cursor if cursor is None else self._registered(cursor)

        sub = self._with_checked_depth(sub)

        if cur.turn_task is not None and asyncio.current_task() is cur.turn_task:
            raise RuntimeError(
                "submit(): reentrant self-submission. This call is running on "
                "the same asyncio task as the turn currently in flight on this "
                "session — a hook (input/tool_call/turn_end/user_turn_end) "
                "calling session.submit()/session.prompt()/ctx.prompt() "
                "before its own turn has returned. Every multitask_strategy "
                "here waits for or acts on _turn_lock, which this task "
                "already holds, so it can never be admitted; unlike two "
                "DIFFERENT tasks racing to submit(), this would hang forever "
                "with no exception and no log line (docs/SUBMISSION-LIFECYCLE.md "
                "'Task marshalling': 'a silent fallback here produces exactly "
                "the bus disconnected randomly class of bug'). Decision 3's "
                "depth cap anticipates bounded self-submission, but nested "
                "execution that bypasses this lock is not implemented, so "
                "this raises unconditionally rather than deadlocking."
            )

        if sub.silent:
            raise NotImplementedError(
                "silent=True is not implemented yet. Its store_history half is "
                "real — Submission.__post_init__ folds silent into "
                "store_history=False, and submit() honours that — but the half it "
                "is NAMED for, suppressing renderer-visible output, has nothing "
                "behind it: the core has one multiplexed event bus whose "
                "subscribers are not all renderers (latency measurement, RPC "
                "capture, branch forwarding), so suppressing emission here would "
                "blind those too, and marking events instead only means anything "
                "once renderers honour the mark — the multi-stream renderer "
                "contract of Block 3 (docs/SUBMISSION-LIFECYCLE.md phase 3, "
                "'TUI becomes renderer + one source'). Ask for the half that "
                "exists by name: store_history=False."
            )

        if sub.multitask_strategy == "reject":
            if not await self._reserve_turn_or_reject(cur):
                return SubmissionResult(
                    accepted=False,
                    submission_id=sub.submission_id,
                    rejection_reason="a turn is already in flight",
                )
        elif sub.multitask_strategy == "enqueue":
            wait = EnqueuedWait(sub.text)
            cur.enqueued.append(wait)
            try:
                await cur.turn_lock.acquire()
            finally:
                if wait in cur.enqueued:
                    cur.enqueued.remove(wait)
            if wait.dropped:
                try:
                    if cur.dropped_enqueued:
                        await self._drop_follow_ups(cur)
                finally:
                    cur.turn_lock.release()
                return SubmissionResult(
                    accepted=False,
                    submission_id=sub.submission_id,
                    rejection_reason="dropped by a user abort while waiting for the turn",
                )
        elif sub.multitask_strategy == "steer":
            if not await self._reserve_turn_or_reject(cur):
                steer_depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
                steer_input_token = SUBMISSION_ALLOWS_USER_INPUT.set(sub.allow_user_input)
                try:
                    text, images, early = await self._apply_input_pipeline(sub)
                finally:
                    DRIVING_SUBMISSION_DEPTH.reset(steer_depth_token)
                    SUBMISSION_ALLOWS_USER_INPUT.reset(steer_input_token)
                if early is not None:
                    return early
                cur.steer_queue.append(self._queued_content_to_user(text, images))
                return SubmissionResult(accepted=True, submission_id=sub.submission_id, messages=[])
        elif sub.multitask_strategy == "rollback":
            was_in_flight = cur.turn_lock.locked()
            rollback_target = cur.pre_turn_leaf
            aborted_token = cur.turn_token
            if was_in_flight:
                cur.abort_signal.abort(reason="user")
                cur.steer_queue.clear()
            await cur.turn_lock.acquire()
            if was_in_flight:
                if rollback_target is None or cur.turn_token != aborted_token:
                    cur.turn_lock.release()
                    return SubmissionResult(
                        accepted=False,
                        submission_id=sub.submission_id,
                        rejection_reason=(
                            "rollback target is stale: the turn this submission "
                            "aborted is no longer the turn whose slot was just "
                            "granted (a different submission was admitted and "
                            "completed first), so rolling back now would "
                            "discard that submission's work instead of the "
                            "aborted turn's"
                        ),
                    )
                cur.move(rollback_target)
        elif sub.multitask_strategy == "fork":
            fork_point = cur.leaf
            reason = cur.tree().fork_admission_reason(fork_point)
            if reason is not None:
                return SubmissionResult(
                    accepted=False, submission_id=sub.submission_id, rejection_reason=reason
                )
            fork_depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
            try:
                self._spawn_fork(sub, fork_point, cur)
            finally:
                DRIVING_SUBMISSION_DEPTH.reset(fork_depth_token)
            return SubmissionResult(accepted=True, submission_id=sub.submission_id, messages=[])
        else:
            raise NotImplementedError(
                f"multitask_strategy={sub.multitask_strategy!r} is not a known "
                "strategy. The five docs/SUBMISSION-LIFECYCLE.md names are "
                "'reject', 'enqueue', 'steer', 'rollback' and 'fork'."
            )

        return await self._drive_turn(sub, cur, context=context, on_admitted=on_admitted)

    def _with_checked_depth(self, sub: Submission) -> Submission:
        """``sub`` at its derived submission depth, refused past the cap.

        Raises:
            RuntimeError: the derived depth exceeds ``MAX_SUBMISSION_DEPTH``.
        """
        depth = next_submission_depth(sub.depth)
        if depth != sub.depth:
            sub = replace(sub, depth=depth)

        if sub.depth > MAX_SUBMISSION_DEPTH:
            raise RuntimeError(
                f"submit(): submission depth {sub.depth} exceeds "
                f"MAX_SUBMISSION_DEPTH ({MAX_SUBMISSION_DEPTH}). Decision 3 "
                "(docs/SUBMISSION-LIFECYCLE.md): self-submission depth is a "
                "hard cap that raises — Neovim's shape (non-reentrant by "
                "default, opt-in nesting, hard cap), not AutoGen's advisory "
                "'stop_hook_active' flag that 'does not prevent loops "
                "automatically'."
            )
        return sub

    async def _drive_turn(
        self,
        sub: Submission,
        cur: Cursor,
        *,
        context: list[dict[str, Any]] | None = None,
        on_admitted: Callable[[], None] | None = None,
        seed: list[dict[str, Any]] | None = None,
        user_turn: bool = True,
    ) -> SubmissionResult:
        """:meth:`submit` steps 2-5 on ``cur``, whose turn lock the caller holds.

        Releases the lock before it returns or raises.

        Args:
            sub: The admitted submission.
            cur: Its cursor.
            context: As for :meth:`submit`.
            on_admitted: As for :meth:`submit`.
            seed: Validated messages written ahead of the user turn
                (docs/SEEDED-BRANCHES.md §4).
            user_turn: ``False`` runs no input pipeline and adds no user message:
                the loop answers ``seed``.
        """
        depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
        user_input_token = SUBMISSION_ALLOWS_USER_INPUT.set(sub.allow_user_input)
        cursor_token = TURN_CURSOR.set(cur)
        try:
            cur.submission = sub
            cur.turn_task = asyncio.current_task()

            self._turn_token_counter += 1
            cur.turn_token = self._turn_token_counter
            if self._submission_runs_a_turn(sub):
                await self._record_config()
            cur.pre_turn_leaf = cur.leaf

            cur.is_streaming = True
            cur.abort_signal = AbortSignal()

            original_text = sub.text
            if not user_turn:
                text, images = "", None
            else:
                text, images, early = await self._apply_input_pipeline(sub)
                if early is not None:
                    return early

            # Commands resolve inside the pipeline above, so the lock cannot refuse its own release.
            locked = self.pending_request
            if locked is not None and locked.lock:
                return SubmissionResult(
                    accepted=False,
                    submission_id=sub.submission_id,
                    rejection_reason=refusal_reason(locked),
                    lock=locked,
                )

            if on_admitted is not None:
                on_admitted()

            self._require_compaction_ready()
            if self._compaction_policy is not None:
                self._policy_turns_used += 1
                self._compaction_policy.admit_turn(self._policy_turns_used)

            next_turn = cur.next_turn_queue
            cur.next_turn_queue = []
            queued = [self._queued_content_to_user(c) for c in next_turn]

            side_usage_before = self.side_usage
            await self._events.emit_channel(
                "submission_start", submission=sub, text=text, images=images, cursor=cur
            )
            try:
                turn_messages = await self._run_one_turn(
                    text if user_turn else None,
                    images,
                    context,
                    queued=queued,
                    strip_ref_text=original_text,
                    persist=sub.store_history,
                    seed=seed,
                )

                if sub.store_history:
                    await self._end_of_prompt_drain(turn_messages)

                    await self._run_user_turn_end(turn_messages)

                return SubmissionResult(
                    accepted=True, submission_id=sub.submission_id, messages=turn_messages
                )
            finally:
                after = self.side_usage
                await self._events.emit_channel(
                    "submission_end",
                    submission=sub,
                    side_usage={
                        key: after.get(key, 0) - before for key, before in side_usage_before.items()
                    },
                )

        finally:
            cur.is_streaming = False
            cur.submission = None
            cur.turn_task = None
            TURN_CURSOR.reset(cursor_token)
            DRIVING_SUBMISSION_DEPTH.reset(depth_token)
            SUBMISSION_ALLOWS_USER_INPUT.reset(user_input_token)
            cur.turn_lock.release()

    async def spawn(
        self,
        at: str | None,
        prompt: str | None = None,
        *,
        seed: list[dict[str, Any]] | None = None,
        tools: list[str],
        model: str | Model | None = None,
        max_turns: int | None = None,
        label: str | None = None,
        system_prompt: str | None = None,
        hooks: bool = False,
        owner: Cursor | None = None,
    ) -> BranchResult:
        """Run a sub-agent turn on a new cursor at ``at`` (docs/CURSORS.md §6).

        The turn answers ``prompt``, or continues from ``seed``
        (docs/SEEDED-BRANCHES.md): the seed's messages are written on the branch
        first, then the prompt as a user message if one is given, and with no
        prompt the loop answers the seed directly. The cursor is owned by ``owner`` (default: the running turn's), so aborting
        the owner aborts it, and it runs under a :class:`~tau_agent_core.cursor.TurnFrame`:
        only ``tools``, ``model``/``system_prompt`` when given, ``max_turns``, and
        no extension hooks unless ``hooks``. Its turn is an ordinary submission
        (``source="agent"``, ``submitter="fork:<label>"``) whose events carry its
        cursor's id. Its first append records that frame as a config entry, so the
        sub-agent's spec is in the tree. The cursor is closed before this returns,
        unless a turn still holds it.

        Failure is contained, not propagated (§9.2/5): a sub-agent that raises
        leaves a ``branch_error`` entry and returns ``ok=False``, as does a
        refused submission (without the entry).

        Args:
            at: The entry to branch from; ``None`` is before the root. Inside a
                tool call, the parent of the calling assistant message, never that
                message itself.
            prompt: The sub-agent's user message; ``None`` needs a ``seed``.
            seed: Messages the branch starts from (:func:`~tau_agent_core.seed.normalise_seed`
                lists the shapes; :func:`~tau_agent_core.seed.seed_tool_call` builds
                an injected call and its result).
            tools: Names of the tools it may call, each one the spawning turn
                offers.
            model: Its model and key, by :meth:`model_and_key`: a config name
                (recorded as the branch's ``model``), a ``Model``, or ``None`` for
                the session's.
            max_turns: Its turn ceiling; ``None`` is no ceiling, not the session's.
            label: What it is for, for display; ``None`` means the prompt's first
                60 characters, or the seed's tool names.
            system_prompt: Its system prompt; ``None`` means the session's.
            hooks: Whether extension hooks fire in its turn.
            owner: The cursor it answers to; ``None`` means :attr:`acting_cursor`.

        Returns:
            What the sub-agent came back with; check ``ok`` first.

        Raises:
            ValueError: a name in ``tools`` is not one the spawning turn offers;
                ``at`` names no entry; neither ``prompt`` nor ``seed`` is given; or
                the seed is malformed or would leave the context invalid
                (:func:`~tau_agent_core.seed.check_seed`, naming the call id).
                Raised before anything is written or any model is called.
            RuntimeError: ``model`` is a name and no resolver is bound.
        """
        if prompt is None and seed is None:
            raise ValueError("spawn: give a prompt, a seed, or both")
        resolved, key = (None, None) if model is None else self.model_and_key(model)
        available = {t.name for t in self._turn_tools()}
        missing = [t for t in tools if t not in available]
        if missing:
            raise ValueError(
                f"spawn: tool(s) {missing!r} are not available on this session "
                f"(available: {sorted(available)}). A sub-agent silently missing a tool it "
                "was told to use would return a confident wrong answer."
            )
        stored_seed = None if seed is None else self._checked_seed(at, seed, prompt is not None)
        if label is None:
            label = prompt[:60] if prompt is not None else _seed_label(stored_seed or [])
        cursor = await self.open_cursor(
            at, owner=self.acting_cursor if owner is None else owner, label=label
        )
        cursor.frame = TurnFrame(
            tools=tuple(tools),
            model=resolved,
            api_key=key,
            model_name=model if isinstance(model, str) else None,
            system_prompt=system_prompt,
            max_turns=max_turns,
            hooks=hooks,
        )
        sub = Submission(
            text=prompt or "",
            source="agent",
            submitter=f"fork:{cursor.label}",
            submission_id=uuid.uuid4().hex,
            multitask_strategy="enqueue",
        )
        try:
            if stored_seed is None:
                result = await self.submit(sub, cursor=cursor)
            else:
                result = await self._submit_seeded(sub, cursor, stored_seed, prompt is not None)
        except Exception as exc:  # noqa: BLE001 — containment is the point (§9.2/5)
            await cursor.append_custom_entry(
                "branch_error", {"label": cursor.label, "error": str(exc)}
            )
            return BranchResult(
                ok=False,
                cursor_id=cursor.id,
                label=cursor.label,
                leaf=cursor.leaf,
                messages=[],
                error=str(exc),
            )
        finally:
            if not cursor.busy:
                await self.close_cursor(cursor)
        if not result.accepted:
            return BranchResult(
                ok=False,
                cursor_id=cursor.id,
                label=cursor.label,
                leaf=cursor.leaf,
                messages=[],
                error=result.rejection_reason or "the sub-agent's submission was refused",
            )
        return BranchResult(
            ok=True,
            cursor_id=cursor.id,
            label=cursor.label,
            leaf=cursor.leaf,
            messages=list(result.messages),
            error=None,
        )

    def _checked_seed(
        self, at: str | None, seed: list[dict[str, Any]], prompted: bool
    ) -> list[dict[str, Any]]:
        """``seed`` normalised and checked against the context at ``at``; writes nothing.

        Raises:
            ValueError: ``at`` names no entry, or :func:`~tau_agent_core.seed.normalise_seed`
                or :func:`~tau_agent_core.seed.check_seed` refuses the seed.
        """
        stored = normalise_seed(seed)
        tree = ConversationTree(self._cursor.log.entries(), at)
        if at is not None and not tree.contains(at):
            raise ValueError(f"spawn: branch point {at!r} not found")
        check_seed(tree.context_for(at) if at is not None else [], stored, prompted=prompted)
        return stored

    async def _submit_seeded(
        self, sub: Submission, cursor: Cursor, seed: list[dict[str, Any]], prompted: bool
    ) -> SubmissionResult:
        """Run ``sub`` on ``cursor``, a branch :meth:`spawn` just opened, from ``seed``.

        The fresh cursor has no turn to wait out or roll back, so admission is
        the depth cap and the lock; the turn is :meth:`submit`'s.
        """
        cur = self._registered(cursor)
        sub = self._with_checked_depth(sub)
        await cur.turn_lock.acquire()
        return await self._drive_turn(sub, cur, seed=seed, user_turn=prompted)

    def _spawn_fork(self, sub: Submission, fork_point: str | None, owner: Cursor) -> None:
        """Run a ``multitask_strategy="fork"`` submission as a supervised :meth:`spawn`.

        docs/SUBMISSION-LIFECYCLE.md "fork": a fork is a sub-agent nobody awaits, so
        this wraps :meth:`spawn` in a task tracked in :attr:`_forked_tasks`, keyed by
        ``sub.submission_id`` (known now; the cursor is not opened until the task runs).
        It offers every tool a turn of ``owner``'s would — a fork continues the same
        job — under ``owner``'s turn cap, with no extension hooks (NODE-ADDRESSABLE-AGENTS.md
        decision 4). An exception the task should never raise is surfaced through
        :meth:`_surface_extension_error` (:meth:`_on_fork_task_done`), not left to
        asyncio's "never retrieved" warning.
        """
        submission_id = sub.submission_id
        task = asyncio.get_running_loop().create_task(
            self.spawn(
                fork_point,
                sub.text,
                tools=[t.name for t in self._turn_tools()],
                max_turns=self._max_turns,
                owner=owner,
            )
        )
        self._forked_tasks[submission_id] = task
        task.add_done_callback(lambda t: self._on_fork_task_done(submission_id, t))

    def _on_fork_task_done(self, submission_id: str, task: asyncio.Task[Any]) -> None:
        """Untrack a finished forked task; surface an unexpected exception (Fail-Early)."""
        self._forked_tasks.pop(submission_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._surface_extension_error(
                ExtensionError(
                    extension_path=f"fork:{submission_id}", event="fork", error=str(error)
                )
            )

    def deliver_queued(self, cursor: Cursor) -> None:
        """Run what ``cursor`` still holds as turns on its own tree, then close it.

        For a cursor no head is attached to any more — the one a head swap left
        behind (docs/CURSORS.md §8). Its accepted messages are delivered where they
        were aimed, the way a sub-agent's turn runs with nobody watching, rather
        than discarded. Supervised: :meth:`emit_session_shutdown` cancels it, and
        :meth:`wait_for_deliveries` awaits it.

        Args:
            cursor: A non-head cursor this session opened. Returns at once; the
                delivery runs as a task on the running loop.

        Raises:
            ValueError: ``cursor`` is the head's, or this session did not open it.
        """
        if self._registered(cursor) is self._cursor:
            raise ValueError("deliver_queued: the head's cursor delivers through its own turns")
        task = asyncio.get_running_loop().create_task(self._deliver_queued(cursor))
        self._delivery_tasks[cursor.id] = task
        task.add_done_callback(lambda t: self._on_delivery_done(cursor.id, t))

    async def wait_for_deliveries(self) -> None:
        """Await every :meth:`deliver_queued` still running."""
        while self._delivery_tasks:
            await asyncio.gather(*list(self._delivery_tasks.values()), return_exceptions=True)

    async def _deliver_queued(self, cursor: Cursor) -> None:
        """Drain ``cursor``'s queues, one submission per turn, then close it.

        Waits out a turn already holding the cursor first; a submission that
        reached the cursor just before a head swap still runs. Each turn takes the oldest next-turn or follow-up text as its prompt; the
        submission it drives drains the rest the ordinary way (next-turn texts beside
        its user node, steers before its first call, follow-ups at its end). A cursor
        holding only steers turns the first into the prompt. The queues keep no
        submitter, so these turns are attributed to ``cursor:<id>``.
        """
        while True:
            async with cursor.turn_lock:
                pass
            if not cursor.has_queued:
                break
            if cursor.next_turn_queue:
                text, images = cursor.next_turn_queue.pop(0), None
            elif cursor.follow_up_queue:
                text, images = cursor.follow_up_queue.pop(0), None
            else:
                steer = cursor.steer_queue.pop(0)
                blocks = list(getattr(steer, "content", []) or [])
                text = "".join(
                    getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"
                )
                images = [
                    b.model_dump() for b in blocks if getattr(b, "type", "") == "image"
                ] or None
            await self.submit(
                Submission(
                    text=text,
                    images=images,
                    source="extension",
                    submitter=f"cursor:{cursor.id}",
                    submission_id=uuid.uuid4().hex,
                    multitask_strategy="enqueue",
                ),
                cursor=cursor,
            )
        await self.close_cursor(cursor)

    def _on_delivery_done(self, cursor_id: str, task: asyncio.Task[Any]) -> None:
        """Untrack a finished delivery; surface an unexpected exception (Fail-Early)."""
        self._delivery_tasks.pop(cursor_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._surface_extension_error(
                ExtensionError(
                    extension_path=f"deliver:{cursor_id}", event="deliver_queued", error=str(error)
                )
            )

    async def _cancel_forked_tasks(self) -> None:
        """Cancel every still-running forked branch and wait for it to unwind.

        The "must not become an orphan on session close" half of the supervised
        task registry (docs/SUBMISSION-LIFECYCLE.md "fork"): called from
        :meth:`emit_session_shutdown`, the one place every frontend (TUI quit,
        headless completion, SIGINT/SIGTERM) already calls at genuine
        end-of-runtime, so a forked branch cannot keep running after the session
        that spawned it is gone. Cancel-then-await, not fire-and-forget: a bare
        ``task.cancel()`` schedules the cancellation but does not wait for the
        coroutine to actually unwind, which is not "not an orphan," only "not an
        orphan eventually, maybe."
        """
        await self._cancel_task_registry(self._forked_tasks)

    async def _cancel_task_registry(self, registry: dict[str, asyncio.Task[Any]]) -> None:
        """Cancel every task in a supervised registry and wait for it to unwind.

        Shared by :meth:`_cancel_forked_tasks` and the ``submit_threadsafe``
        registry, which need the identical cancel-then-await discipline for the
        identical reason (see the caller above) — one implementation so a second
        registry cannot quietly get the fire-and-forget version.
        """
        tasks = list(registry.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def prompt(
        self,
        text: str,
        images: list[dict] | None = None,
        context: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Send a prompt and run the agent loop — the interactive compatibility wrapper.

        docs/SUBMISSION-LIFECYCLE.md "The one door": every ``prompt()`` call is now
        a :class:`~tau_agent_core.submission.Submission` with
        ``source="interactive"``, ``submitter="human"``,
        ``multitask_strategy="enqueue"`` (decision 1 — pi's TUI binds Enter→steer
        and Alt+Enter→followUp; ``"steer"`` now exists (phase 4), and because the
        strategy is a per-submission parameter the second keybinding remains a
        one-line change at the TUI call site), ``expand_commands=True``,
        ``allow_user_input=True``. See :meth:`submit` for what it does with that,
        in order, and for why ``context`` is threaded as a direct keyword rather
        than a ``Submission`` field.

        **``expand_commands`` is ``True`` again (B2-b), and this method therefore
        RAISES on a command rather than returning one.** Its return type is
        ``list[dict]`` — the turn's messages — which has no channel for a
        :class:`~tau_agent_core.flows.Dispatched`, and a resolved command
        produces no messages. Returning ``[]`` would be indistinguishable from a
        turn that said nothing, so ``/compact`` through this method would look like
        a model that ignored you. The check runs BEFORE :meth:`submit`, using the
        same :meth:`resolve_command` ``submit()`` uses, so nothing has run by the
        time it raises: an extension command is not executed and then reported as
        an error. A caller that wants commands calls :meth:`submit` and reads
        ``result.command``; a caller that wants a turn passes text that is not a
        command, exactly as today.

        A ``/…`` that is not a registered command still falls through to the model
        as ordinary text — unchanged, and the reason this is not a compatibility
        break for anything that pastes a path.

        ``allow_user_input=True`` is the one place in the codebase that asserts it:
        a human typed this, so an extension hook running under this turn may ask
        that same human a question (see :meth:`submit` for the enforcement).

        This is the LIVE path for the TUI backend, headless print mode, and the
        SDK (``backends.py``, ``headless.py``, ``rpc.py``, every ``examples/*``
        script) — its signature and return type are unchanged from before
        ``submit()`` existed.

        Args:
            text: The prompt text to send.
            images: Optional list of image dicts for multimodal prompts.
            context: Optional list of message dicts to use as conversation
                     context instead of session messages. This allows
                     passing a pre-built message history (e.g. from a
                     loaded chat) to the agent loop.

        Returns:
            List of messages produced by the agent loop (this turn's new
            messages only — see :meth:`_run_one_turn`).

        Raises:
            UnsupportedCommandError: ``text`` resolves to a command, or to a refused
                ``/ext:`` name. See above — this method has nowhere to put the
                outcome, so it refuses before anything runs instead of swallowing it.
        """
        invocation = self.resolve_command(text)
        if invocation is not None and invocation.refusal is not None:
            raise UnsupportedCommandError(invocation.refusal)
        if invocation is not None:
            raise UnsupportedCommandError(
                f"prompt() was handed the command /{invocation.name}, and its "
                "list[dict] return type has no channel for a command outcome — a "
                "dispatched command produces no messages, so returning [] here would "
                "be indistinguishable from a turn that said nothing. Checked before "
                "submit() so nothing has run. Use submit(Submission(..., "
                "expand_commands=True)) and read result.command "
                "(docs/SUBMISSION-LIFECYCLE.md phase 3)."
            )
        result = await self.submit(
            Submission(
                text=text,
                source="interactive",
                submitter="human",
                submission_id=uuid4().hex,
                images=images,
                multitask_strategy="enqueue",
                expand_commands=True,
                allow_user_input=True,
            ),
            context=context,
        )
        if result.command is not None:
            raise UnsupportedCommandError(
                "an `input` hook rewrote this prompt into a command, which prompt() "
                f"cannot return ({type(result.command).__name__}). Unlike the "
                "pre-submit check above this fires AFTER admission, so a command the "
                "core performs has already run — the hook, not the caller, is the "
                "culprit. Use submit() and read result.command."
            )
        return result.messages

    async def _run_one_turn(
        self,
        text: str | None,
        images: list[dict] | None,
        context: list[dict] | None,
        queued: list[UserMessage] | None = None,
        strip_ref_text: str | None = None,
        persist: bool = True,
        seed: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one agent-loop turn: build the user message, run the loop, persist.

        Extracted from ``prompt()`` so the end-of-prompt followUp drain (S20) can
        re-enter it within the same ``prompt()`` call. ``queued`` are pending
        ``nextTurn`` messages threaded (and persisted) after the user turn on the
        first turn of a prompt; empty on a followUp re-entry.

        ``strip_ref_text`` is the text used to detect the caller's echoed user
        turn (the TUI passes the full history, ending with this turn). It exists
        because the S42 ``input`` hook may have transformed ``text`` upstream in
        ``prompt()`` while the caller's echo still holds the PRE-transform text —
        so the strip must compare against the original, not the transformed value,
        or the loop would see both the echo AND the transformed user_msg. ``None``
        (the followUp re-entry, where ``context`` is ``None`` anyway) falls back to
        ``text``.

        ``persist`` is :meth:`submit`'s ``Submission.store_history`` (default
        ``True``, byte-for-byte the pre-existing behaviour). ``False`` (a
        submission with ``store_history=False``/``silent=True``) still builds the
        user message, runs the real loop, and returns the produced messages, but
        writes NONE of it to the log — the model sees and answers
        the turn; the durable tree never does.

        ``seed`` (validated by :func:`~tau_agent_core.seed.check_seed`) is written
        and threaded ahead of everything else; ``text=None`` then adds no user
        message and fires no ``before_agent_start``, so the loop answers the seed
        (docs/SEEDED-BRANCHES.md §4).

        Returns THIS turn's new messages only — the seed, the user message, any
        ``queued`` messages, then the assistant/tool messages the loop produced.
        """
        queued = queued or []
        seed = seed or []

        user_msg: UserMessage | None = None
        if text is not None:
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if images:
                content.extend(images)
            user_msg = UserMessage.model_validate(
                {
                    "role": "user",
                    "content": content,
                    "timestamp": self._timestamp(),
                }
            )

        # Get context: use provided context or fall back to session messages.
        if context is not None:
            context_messages = list(context)  # copy to avoid mutation
            strip_ref = strip_ref_text if strip_ref_text is not None else text
            context_ends_with_user = strip_ref is not None and _ends_with_user_text(
                context_messages, strip_ref
            )
        else:
            context_messages = self.messages
            context_ends_with_user = False

        if context_ends_with_user:
            context_messages = context_messages[:-1]

        system_prompt_override: str | None = None
        pre_user_messages: list[dict[str, Any]] = []
        post_user_messages: list[dict[str, Any]] = []
        hooks = self._hooks()
        if text is not None and hooks.has_handlers("before_agent_start"):
            stored_prompt = _leading_system_prompt(context_messages)
            before = await hooks.emit_before_agent_start(
                prompt=text,
                images=images,
                system_prompt=stored_prompt
                if stored_prompt is not None
                else self._turn_system_prompt(),
            )
            if before is not None:
                if before.get("system_prompt") is not None:
                    system_prompt_override = before["system_prompt"]
                for msg in before.get("messages") or []:
                    node = self._custom_message_node(msg)
                    if msg.get("position") == MESSAGE_POSITION_BEFORE_USER:
                        pre_user_messages.append(node)
                    else:
                        post_user_messages.append(node)

        # Build the agent loop config
        config = AgentLoopConfig(
            system_prompt=self._turn_system_prompt(),
            system_prompt_override=system_prompt_override,
            tool_execution_mode=self._tool_execution_mode,
            **self._turn_cap(),
        )

        writer = _CursorWriter(self.acting_cursor, persist)
        loop = AgentLoop(
            config=config,
            emit=self._emit_stamped,
            tools=self._turn_tools(),
            turn_settings=lambda: self._request_settings(persist),
            abort_signal=self.acting_cursor.abort_signal,
            hook_dispatcher=hooks,
            steer_queue=self.acting_cursor.steer_queue,
            mid_turn_compactor=self._compact_mid_turn,
            writer=writer,
            call_gate=self._model_call_refusal,
        )

        # The inputs are written before the loop runs, so a crash mid-turn keeps the prompt.
        if persist:
            await self._record_config()
        user = [] if user_msg is None else [user_msg]
        prompts = [*seed, *pre_user_messages, *user, *queued, *post_user_messages]
        for message in prompts:
            await writer.append(message)
        self.acting_cursor.turn_writer = writer

        # Cleared BEFORE loop.run, because loop.run is what emits agent_end.
        self.acting_cursor.persistence_settled.clear()
        try:
            await loop.run(prompts=prompts, context=context_messages)
        except Exception as exc:
            await writer.append(turn_error_message(exc))
            raise
        finally:
            self.acting_cursor.turn_writer = None
            self.acting_cursor.in_flight_context = None
            self.acting_cursor.persistence_settled.set()

        return writer.messages

    async def _end_of_prompt_drain(self, turn_messages: list[dict[str, Any]]) -> None:
        """Drain the auto-compaction + deferred + followUp work at prompt()'s tail.

        Called once at the end of ``prompt()`` (and again after each followUp
        re-entry). Runs, in order:

        1. **Auto-compaction** — compact in place if the conversation is
           approaching the model's context window so the NEXT turn starts within
           budget (pi checks ``shouldCompact`` after each turn). A failure here
           propagates — Fail-Early, no silent skip — but this turn's messages are
           already saved.
        2. **Deferred compact/fork** — the intents a mid-turn tool recorded
           (``ctx.compact(defer=True)`` / ``ctx.fork(defer=True)``) are applied
           EXACTLY ONCE here, never mid-turn (decision 3). No loop reentrancy.
        3. **followUp** — messages queued ``deliver_as="followUp"`` re-enter the
           agent loop WITHIN this same ``prompt()`` call; each new turn's messages
           are appended to ``turn_messages`` and itself drains at its tail. After
           an ``"extension"`` abort each runs on a fresh signal; after a
           ``"user"`` abort all are dropped and announced (docs/TURN-SETTINGS.md §5).
        """
        await self._maybe_auto_compact()
        await self._drain_deferred_ops()

        cursor = self.acting_cursor
        while cursor.follow_up_queue:
            if cursor.abort_signal.is_aborted():
                if cursor.abort_signal.reason == "user":
                    await self._drop_follow_ups(cursor)
                    break
                cursor.abort_signal = AbortSignal()
            follow_up = cursor.follow_up_queue.pop(0)
            follow_up_messages = await self._run_one_turn(follow_up, None, None)
            turn_messages.extend(follow_up_messages)
            await self._maybe_auto_compact()
            await self._drain_deferred_ops()
        if cursor.dropped_enqueued:
            await self._drop_follow_ups(cursor)

    async def _drop_follow_ups(self, cursor: Cursor) -> None:
        """Empty ``cursor``'s followUp queue and dropped enqueues, and announce them.

        Writes nothing. One ``follow_ups_dropped`` carries the followUps, then the
        enqueued texts in submission order (docs/TURN-SETTINGS.md §5, §5.1).
        """
        dropped = [*cursor.follow_up_queue, *cursor.dropped_enqueued]
        cursor.follow_up_queue.clear()
        cursor.dropped_enqueued.clear()
        event = AgentEvent(type="follow_ups_dropped", timestamp=self._timestamp(), dropped=dropped)
        if cursor is self.acting_cursor:
            await self._emit_stamped(event)
        else:
            await self._events.emit(event.model_copy(update={"cursor_id": cursor.id}))

    async def _run_user_turn_end(self, turn_messages: list[dict[str, Any]]) -> None:
        """Fire the ``user_turn_end`` hook once, at the user-turn boundary (§16.5).

        The call-site is the tail of :meth:`prompt`, after
        :meth:`_end_of_prompt_drain`. That is the one point in the harness where a
        *user* turn — as opposed to an agent-loop turn — is over: the loop has
        finished, every followUp re-entry has finished, and compaction has run.

        ``loop_turns`` is the number of agent-loop turns this user turn consumed,
        counted as the assistant messages in ``turn_messages``. It is the exact
        quantity §16.5 is about: it is also the number of times ``turn_end`` fired,
        so a handler can see the factor it would have over-counted by.

        A returned ``message`` becomes a durable ``customMessage`` node appended to
        the session log and to ``turn_messages`` (so it is in the returned
        transcript, not a hidden channel) — append-only, and visible to the model on
        the NEXT prompt, which is the same reload-durable path the mutating
        ``turn_end`` uses for its final turn. Gated on ``has_handlers`` for the
        zero-extension fast path.

        τ divergence from pi: pi has no once-per-prompt mutating hook (its
        ``turn_end`` is notify-only, ``agent-session.ts:617``; the harness's
        once-per-prompt ``settled`` is notify-only too, ``agent-harness.ts:533``).
        Deliberate — §12.4's consolidative tempo has no correct home without it.
        """
        hooks = self._hooks()
        if not hooks.has_handlers("user_turn_end"):
            return
        loop_turns = sum(1 for m in turn_messages if m.get("role") == "assistant")
        injected = await hooks.emit_user_turn_end(
            loop_turns=loop_turns,
            messages=list(turn_messages),
        )
        for raw in injected:
            node = self._custom_message_node(raw, hook="user_turn_end")
            await self.acting_cursor.append_custom_message(
                node, custom_type=str(node["customType"])
            )
            turn_messages.append(node)

    async def continue_conversation(self) -> list[dict[str, Any]]:
        """Run another agent turn without adding new messages.

        Delegates to AgentLoop.run_continue() which streams the LLM response
        via stream_simple() and handles tool calls.

        Concurrency guard (docs/SUBMISSION-LIFECYCLE.md "two unguarded doors" —
        this method sets ``_is_streaming = True`` with no check, exactly as
        ``prompt()`` did before ``submit()`` existed): this is the SAME
        in-flight-turn admission :meth:`submit` applies for
        ``multitask_strategy="reject"``. It has no ``Submission`` and no
        ``SubmissionResult`` to carry a refusal through — it predates that
        contract — so the refusal that would be ``accepted=False`` there is a
        raise here instead of a silently-corrupting concurrent run.

        Raises:
            RuntimeError: a turn is already in flight on this session.

        Returns:
            List of messages produced by the agent loop.
        """
        cur = self._cursor
        if not await self._reserve_turn_or_reject(cur):
            raise RuntimeError(
                "continue_conversation(): a turn is already in flight on this "
                "session. This is one of the two admission-guarded doors "
                "(docs/SUBMISSION-LIFECYCLE.md) — the other is submit()'s "
                "multitask_strategy='reject', which returns "
                "SubmissionResult(accepted=False, ...) instead of raising, "
                "because it has a result channel to carry the refusal through "
                "and this method does not."
            )

        cursor_token = TURN_CURSOR.set(cur)
        cur.is_streaming = True
        cur.abort_signal = AbortSignal()
        cur.turn_task = asyncio.current_task()
        self._turn_token_counter += 1
        cur.turn_token = self._turn_token_counter
        try:
            # Before the leaf is read: the record belongs ahead of the turn, not in it.
            await self._record_config()
            cur.pre_turn_leaf = cur.leaf

            # Get existing messages from session for context
            context_messages = self.messages

            config = AgentLoopConfig(
                system_prompt=self._turn_system_prompt(),
                tool_execution_mode=self._tool_execution_mode,
                **self._turn_cap(),
            )

            writer = _CursorWriter(cur, persist=True)
            loop = AgentLoop(
                config=config,
                emit=self._emit_stamped,
                tools=self._turn_tools(),
                turn_settings=lambda: self._request_settings(True),
                abort_signal=cur.abort_signal,
                hook_dispatcher=self._hooks(),
                steer_queue=cur.steer_queue,
                writer=writer,
                call_gate=self._model_call_refusal,
            )
            try:
                await loop.run_continue(context=context_messages)
            except Exception as exc:
                await writer.append(turn_error_message(exc))
                raise
            return writer.messages

        finally:
            cur.is_streaming = False
            cur.turn_task = None
            TURN_CURSOR.reset(cursor_token)
            cur.turn_lock.release()

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult | None:
        """Compact the active conversation into an LLM-generated summary.

        Runs the full pipeline — build the active-path entries
        (``ConversationTree.context_entries``), choose the cut point, generate the
        structured summary via the LLM (:func:`tau_agent_core.compaction.compact`),
        and record the boundary by APPENDING a compaction entry
        (``SessionLog.append_compaction``, append-only) so the compacted prefix
        drops out of future context at read time. ``agent_start`` / ``agent_end``
        bracket the work for subscribers (e.g. the TUI).

        Args:
            custom_instructions: Optional extra focus for the summary.

        Returns:
            The CompactionResult, or None when there is nothing to compact (an
            empty conversation, or one already ending in a compaction summary).

        Raises:
            CompactionError: if summary generation fails. Fail-Early — no
                fabricated summary is written.
        """
        await self._events.emit(AgentEvent(type="agent_start", timestamp=self._timestamp()))
        try:
            return await self._perform_compaction("manual", custom_instructions=custom_instructions)
        finally:
            await self._events.emit(AgentEvent(type="agent_end", timestamp=self._timestamp()))

    async def compact_messages(
        self, messages: list[dict[str, Any]], custom_instructions: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Summarize everything before the last user turn of a caller-held list.

        For a caller whose own message list, not the session log, is the context
        it sends. Unlike auto-compaction's token-budget cut, the cut is by count:
        the most recent user turn onward is kept whole, whatever its size. The
        summary's tokens go to the side ledger.

        Args:
            messages: The context to compact; not modified.
            custom_instructions: Extra guidance for the summarizer, or ``None``.

        Returns:
            A new list ``[<system messages>, <summary as a user message>, <last
            user turn onward>]``, or ``None`` when there is at most one user
            turn and so nothing older to compact.

        Raises:
            CompactionError: The summarizer's completion failed or was aborted.
        """
        # System messages are never summarized; set them aside and restore them.
        system_msgs = [m for m in messages if m.get("role") == "system"]
        convo = [m for m in messages if m.get("role") != "system"]

        last_user_idx = -1
        for i, m in enumerate(convo):
            if m.get("role") == "user":
                last_user_idx = i
        if last_user_idx <= 0:
            return None  # zero or one user turn — nothing older to compact

        to_summarize = convo[:last_user_idx]
        kept = convo[last_user_idx:]

        file_ops = create_file_ops()
        for m in to_summarize:
            extract_file_ops_from_message(m, file_ops)

        preparation = CompactionPreparation(
            first_kept_entry_id=str(last_user_idx),
            messages_to_summarize=to_summarize,
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=self.context_estimate(convo).tokens,
            file_ops=file_ops,
            settings=self._compaction_settings,
            previous_summary=None,
            compacted_entry_ids=[str(i) for i in range(last_user_idx)],
        )
        summarizer_model, summarizer_api_key = self.summarizer()
        result = await run_compaction(
            preparation,
            summarizer_model,
            summarizer_api_key,
            custom_instructions=custom_instructions,
            thinking_level=self._reasoning,
        )
        self.record_side_usage(result.usage)

        summary_msg: dict[str, Any] = {
            "role": "user",
            "content": [{"type": "text", "text": f"[[Compaction summary: {result.summary}]]"}],
        }
        return [*system_msgs, summary_msg, *kept]

    async def _perform_compaction(
        self,
        reason: SideCompletionReason,
        custom_instructions: str | None = None,
    ) -> CompactionResult | None:
        """Compaction core shared by manual ``compact`` and the auto-trigger.

        ``reason`` is what asked — the one thing the two callers differ by that a
        reader can see, and the reason it is a REQUIRED parameter rather than a
        defaulted one: a default would be one caller's answer silently standing in
        for the other's.

        Emits no ``agent_start``/``agent_end`` of its own; the callers bracket it.
        It DOES emit its own ``side_completion_*`` events, because those describe
        the summary rather than the call — the auto-trigger and ``/compact``
        produce the same three, and a bracket written twice would drift.

        The two no-op returns above the completion emit nothing at all: no tokens
        were spent and no text was produced, so a start with no end would leave
        every renderer holding an open box forever.
        """
        path_entries = self.acting_cursor.tree().context_entries()
        if not any(e.get("type") in ("message", "customMessage") for e in path_entries):
            return None
        preparation = prepare_compaction(path_entries, self._compaction_settings)
        if preparation is None:
            return None

        summarizer_model, summarizer_api_key = self.summarizer()
        async with self.watch_side_completion("compaction", reason, summarizer_model.id) as watch:
            result = await run_compaction(
                preparation,
                summarizer_model,
                summarizer_api_key,
                custom_instructions=custom_instructions,
                thinking_level=self._reasoning,
                on_text_delta=watch.delta,
            )
            watch.finish(result.summary, result.usage)
        self.record_side_usage(result.usage)
        self._usage_valid_after = self._timestamp()
        covered = _covered_span(path_entries, result.first_kept_entry_id)
        await self._record_config()
        await self.acting_cursor.append_compaction(
            summary=result.summary,
            first_kept_id=result.first_kept_entry_id,
            tokens_before=result.tokens_before,
            summarizer_model_id=summarizer_model.id,
            summary_usage=result.usage,
            covered_entries=len(covered),
            covered_tokens=estimate_span_tokens(covered),
            config_id=config_entry_at(self.acting_cursor.entries(), self.acting_cursor.leaf),
        )
        return result

    def _observe_calibration(self, messages: list[dict[str, Any]]) -> None:
        """Feed the newest billed turn to the calibrator, at most once each.

        One observation per assistant message that reported usage: the request
        that produced it held every message before it, so ``messages`` is that
        index and ``payload`` is what the counter says those messages cost. The
        provider's own prompt size is the target
        (:func:`~tau_agent_core.prompt_cache.prompt_tokens`), which is the whole
        point — the fit learns the chat-template framing by watching the gap
        between what we counted and what we were charged.

        Turns are keyed by ``(timestamp, prompt_tokens)`` rather than by index,
        because a compaction renumbers the path and an index-keyed set would
        re-observe every surviving turn after each one.
        """
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") != "assistant" or msg.get("stop_reason") in ("aborted", "error"):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            billed = prompt_tokens(usage)
            if billed <= 0 or i == 0:
                return
            key = (int(msg.get("timestamp") or 0), billed)
            if key in self._calibration_seen:
                return
            self._calibration_seen.add(key)
            payload = ZERO_TOKENS
            for prior in messages[:i]:
                payload = payload + count_message(prior, self._token_counter)
            self._calibrator.observe(
                messages=i, payload_tokens=payload.tokens, billed_tokens=billed
            )
            return

    def context_estimate(
        self, messages: list[dict[str, Any]] | None = None
    ) -> ContextUsageEstimate:
        """This session's context size, counted with its own counter and fit.

        The one number the header, ``get_session_stats``, both compaction checks
        and every extension read, so a display and a trigger never disagree.

        Args:
            messages: The messages to measure. ``None`` means the context the
                running turn holds (published at each turn boundary by
                :meth:`_compact_mid_turn`), else the active path. A turn's FIRST
                request is still measured on the persisted path, so it omits
                that turn's own user message.

        Returns:
            The estimate; its ``count`` carries the provenance — whether a
            tokenizer or the character classes produced it, and whether the
            chat-template framing is inside it.
        """
        in_flight = self.acting_cursor.in_flight_context
        if messages is not None:
            path = messages
        elif in_flight is not None:
            path = in_flight
        else:
            path = self.messages
        self._observe_calibration(path)
        return estimate_context_tokens(
            path,
            counter=self._token_counter,
            calibrator=self._calibrator,
            usage_valid_after=self._usage_valid_after,
        )

    async def _compact_mid_turn(self, messages: list[Any], produced: list[Any]) -> list[Any] | None:
        """Compact from INSIDE the agent loop when the hard limit is crossed.

        The end-of-turn check cannot help a turn that blows the window partway
        through — twenty tool calls into a search, the next request is refused and
        the turn is lost. This runs at every loop turn boundary and does nothing
        until :func:`~tau_agent_core.compaction.must_compact` says the next
        request would not fit.

        When it does fire, the turn so far is PERSISTED FIRST and the compaction
        goes through the ordinary tree path. That ordering is the whole design:
        a summary that only existed in the loop's local list would be a context
        the session tree never saw, and the tree is what the model's input is
        supposed to be derived from. The cost is that the loop's remaining
        messages must not be written twice, which
        :attr:`_mid_turn_persisted` tracks.

        Returns the post-compaction active path, or None to leave the loop alone.
        """
        if not self._compaction_settings.auto:
            return None
        context_window = int(getattr(self._turn_model(), "context_window", 0) or 0)
        if context_window <= 0:
            return None

        writer = self.acting_cursor.turn_writer
        if writer is None:
            return None

        path = self.messages if writer.persist else [*self.messages, *writer.messages]
        self.acting_cursor.in_flight_context = path
        estimate = self.context_estimate(path)
        if not must_compact(estimate.tokens, context_window, self._compaction_settings):
            return None
        if not writer.persist:
            raise RuntimeError(
                "a store_history=False turn crossed the hard context limit; it cannot "
                "compact, because compaction writes to the tree and this turn writes nothing"
            )

        await self._events.emit(AgentEvent(type="agent_start", timestamp=self._timestamp()))
        try:
            await self._perform_compaction("threshold")
        finally:
            await self._events.emit(AgentEvent(type="agent_end", timestamp=self._timestamp()))
        compacted = list(self.messages)
        self.acting_cursor.in_flight_context = compacted
        return compacted

    async def _maybe_auto_compact(self) -> None:
        """Compact automatically when context approaches the model's window.

        Mirrors pi's harness, which checks ``shouldCompact`` after each turn.
        Skipped unless compaction is enabled and the window is larger than the
        reserve (a window smaller than the reserve makes the threshold
        meaningless — e.g. tiny test models — so we never auto-compact there).

        This is also where a declared
        :class:`~tau_agent_core.compaction_policy.CompactionPolicy` is consulted
        (H5 / §16.8) — see the comment at the call. It is consulted, never
        obeyed-if-convenient: the only thing it can do here is raise.

        Raises:
            CompactionPolicyViolation: a declared policy's budget was exceeded.
        """
        settings = self._compaction_settings
        context_window = getattr(self._turn_model(), "context_window", 0) or 0

        if self._compaction_policy is not None:
            self._compaction_policy.observe_context(
                turns_used=self._policy_turns_used,
                context_tokens=self.context_estimate().tokens,
                context_window=context_window,
            )

        if not settings.auto:
            return
        if try_compaction_limits(context_window, settings) is None:
            return

        estimate = self.context_estimate()
        if not should_compact(estimate.tokens, context_window, settings):
            return

        await self._events.emit(AgentEvent(type="agent_start", timestamp=self._timestamp()))
        try:
            await self._perform_compaction("threshold")
        finally:
            await self._events.emit(AgentEvent(type="agent_end", timestamp=self._timestamp()))

    def _queue_message(self, content: str, deliver_as: str = "followUp") -> None:
        """Queue a user message for injection (the seam ``send_user_message`` calls).

        ``deliver_as`` selects WHEN the queued content re-enters the conversation
        (the API validates the same set before reaching here; this method is the
        session-side seam and validates too — Fail-Early, no silent misroute):

        - ``"followUp"``: drains at the end of the CURRENT ``prompt()`` and
          re-enters the agent loop within that same call.
        - ``"nextTurn"``: queued for the NEXT ``prompt()``, injected alongside its
          user turn.
        - ``"steer"`` (phase 4): delivered by the loop that is running RIGHT NOW,
          before its next LLM call — the mode this method's docstring reserved
          space for, now real. This is the seam an extension hook must use to
          steer the turn it is itself running inside: ``submit()`` refuses a call
          from the in-flight turn's own asyncio task (it could never be admitted),
          and this needs no admission because it starts no turn. With no turn
          running the content simply waits for the next loop's first LLM call —
          the same "before the next LLM call" promise, no special case.

        The delivery mode stays a plain string so a further mode can be added
        additively (decision 5).
        """
        if deliver_as == "followUp":
            self.acting_cursor.follow_up_queue.append(content)
        elif deliver_as == "nextTurn":
            self.acting_cursor.next_turn_queue.append(content)
        elif deliver_as == "steer":
            self.acting_cursor.steer_queue.append(self._queued_content_to_user(content))
        else:
            raise ValueError(
                "_queue_message: deliver_as must be 'followUp', 'nextTurn' or "
                f"'steer', got {deliver_as!r}"
            )

    def _defer_compact(self, custom_instructions: str | None = None) -> None:
        """Record a deferred compaction intent (drained at prompt()'s tail, S20)."""
        self.acting_cursor.deferred_ops.append(
            {"kind": "compact", "custom_instructions": custom_instructions}
        )

    def _defer_fork(self, entry_id: str | None = None, mode: str = "in_place") -> None:
        """Record a deferred fork intent (drained at prompt()'s tail, S20)."""
        self.acting_cursor.deferred_ops.append({"kind": "fork", "entry_id": entry_id, "mode": mode})

    async def _drain_deferred_ops(self) -> None:
        """Apply the recorded deferred compact/fork intents exactly once.

        Snapshots and clears the ledger first, then dispatches each intent, so an
        op recorded WHILE draining waits for the next drain rather than looping
        here — "applies exactly once at end-of-prompt" (decision 3). Delegates to
        the immediate paths (``compact`` / ``ctx.fork``); Fail-Early on an unknown
        kind rather than silently dropping it.
        """
        if not self.acting_cursor.deferred_ops:
            return
        ops = self.acting_cursor.deferred_ops
        self.acting_cursor.deferred_ops = []
        ctx = self._extension_api.context
        for op in ops:
            kind = op["kind"]
            if kind == "compact":
                await self.compact(custom_instructions=op["custom_instructions"])
            elif kind == "fork":
                await ctx.fork(entry_id=op["entry_id"], mode=op["mode"])
            else:
                raise ValueError(f"_drain_deferred_ops: unknown deferred op kind {kind!r}")

    def _queued_content_to_user(
        self, content: str, images: list[dict] | None = None
    ) -> UserMessage:
        """Wrap queued content as a ``UserMessage`` turn.

        ``images`` is the ``Submission.images`` a ``multitask_strategy="steer"``
        submission may carry; the ``nextTurn``/``followUp`` queues hold plain
        strings and pass nothing. Same block layout as :meth:`_run_one_turn`
        builds for an ordinary user turn, so a steered message and a typed one
        are the same shape on the wire.
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
        if images:
            blocks.extend(images)
        return UserMessage.model_validate(
            {
                "role": "user",
                "content": blocks,
                "timestamp": self._timestamp(),
            }
        )

    def abort(self, cursor: Cursor | None = None) -> None:
        """Abort ``cursor``'s turn, every cursor it owns, and every still-running forked branch.

        A ``"user"`` abort: every head's stop key lands here, and the followUps
        queued for the aborted turn, and every ``enqueue`` submission waiting for
        it, are dropped and announced rather than run (docs/TURN-SETTINGS.md §4-5). Owned cursors are aborted first, deepest
        first, so a sub-agent stops before the turn that spawned it
        (docs/CURSORS.md §6). Each loses its queued steers: the turn they were
        aimed at is gone. A fork's background task is cancelled, since no caller
        holds its cursor (docs/SUBMISSION-LIFECYCLE.md "fork").

        Marshalled :meth:`submit_threadsafe` submissions are deliberately NOT
        cancelled: one in flight is an ordinary turn the signal already aborts,
        and the rest are input this abort was never about. They are drained at
        :meth:`emit_session_shutdown`.

        Args:
            cursor: A cursor this session opened; ``None`` means
                :attr:`acting_cursor`.
        """
        target = self.acting_cursor if cursor is None else self._registered(cursor)
        for owned in reversed(self._owned_by(target)):
            owned.abort_signal.abort(reason="user")
            owned.steer_queue.clear()
            _drop_enqueued(owned)
        target.is_streaming = False
        target.abort_signal.abort(reason="user")
        target.steer_queue.clear()
        _drop_enqueued(target)
        for task in self._forked_tasks.values():
            task.cancel()

    def _owned_by(self, owner: Cursor) -> list[Cursor]:
        """Live cursors ``owner`` owns, directly or through another, shallowest first."""
        found: list[Cursor] = []
        frontier = [owner]
        while frontier:
            parent = frontier.pop(0)
            children = [c for c in self._cursors.values() if c.owner is parent]
            found.extend(children)
            frontier.extend(children)
        return found

    def _resolve_extension_tools(self) -> list[AgentTool]:
        """Resolve the registry's active extension tools into ``AgentTool``s.

        Reads the session-owned registry's *active* extension tool definitions
        (pi ``ToolDefinition`` dicts registered via ``api.register_tool``) and
        wraps each so the agent loop can call it. The loop invokes
        ``tool.execute(tool_call_id=…, args=…, signal=…)``; the wrapper adapts
        that to the extension's pi-shaped
        ``execute(tool_call_id, params, signal, on_update, ctx)`` — binding the
        live ``ExtensionContext`` as ``ctx`` (pi's ``wrapRegisteredTools`` /
        ``wrapToolDefinition``, coding-agent/src/core/tools/tool-definition-wrapper.ts).

        Because the loop is rebuilt every ``prompt()`` / ``continue_conversation``
        (this method is called at each), a ``register_tool`` mid-session is live
        on the next turn for free.
        """
        ctx = self._extension_api.context
        resolved: list[AgentTool] = []
        for name, defn in self._registry.get_active_tools().items():
            ext_execute = defn.execute

            def _make_adapter(ext_execute: Callable = ext_execute) -> Callable:
                async def _adapter(
                    tool_call_id: str,
                    args: dict,
                    signal: Any = None,
                    on_update: Callable | None = None,
                ) -> Any:
                    result = ext_execute(tool_call_id, args, signal, on_update, ctx)
                    if inspect.isawaitable(result):
                        result = await result
                    return result

                return _adapter

            resolved.append(
                AgentTool(
                    definition=ToolDefinition(
                        **{**defn.model_dump(exclude={"execute", "source"}), "name": name},
                        execute=_make_adapter(),
                    )
                )
            )
        return resolved

    def _custom_message_node(
        self, message: dict[str, Any], hook: str = "before_agent_start"
    ) -> dict[str, Any]:
        """Build the durable ``custom`` message dict for a mutating hook's return.

        Handlers return ``{customType, content, display?, details?}`` (pi
        ``BeforeAgentStartEventResult.message``), and ``before_agent_start`` returns
        may additionally carry ``position`` — a threading directive consumed by
        :meth:`_run_one_turn`, not durable node content, so it is not copied here.
        ``hook`` names the returning hook and appears in the Fail-Early messages
        below; the two callers are ``before_agent_start`` and ``user_turn_end``.
        This is turned into an
        agent-level custom message (``role: "custom"``,
        :func:`~tau_agent_core.messages.create_custom_message`) that is both
        threaded to the loop this turn AND persisted as a ``customMessage`` tree
        node (E5 §3.1 / S29). The ``role: "custom"`` marks it extension-origin for
        the TUI / tree browser; :func:`~tau_agent_core.messages.convert_to_llm`
        serializes it to a ``user`` message on the wire (pi messages.ts
        custom→user), so the injected message still reaches the model.

        Raises:
            ValueError: if the message has no ``content`` (a handler returning a
                ``message`` must say what to inject) or no ``customType`` (the
                extension-origin identity is not fabricated) — Fail-Early.
        """
        if "content" not in message:
            raise ValueError(f"{hook} message is missing 'content' — nothing to inject")
        if "customType" not in message:
            raise ValueError(
                f"{hook} message is missing 'customType' — the extension-origin "
                "type is required (Fail-Early, no fabricated default)"
            )
        return create_custom_message(
            custom_type=str(message["customType"]),
            content=message["content"],
            display=bool(message.get("display", True)),
            details=message.get("details"),
            timestamp=self._timestamp(),
        )

    async def _append_custom_message(
        self, message: dict[str, Any], options: dict[str, Any] | None = None
    ) -> str:
        """Append a durable extension ``customMessage`` node (``api.send_message``).

        The backend for ``ExtensionAPI.send_message`` (E6 §2 / S38). Builds a
        ``role: "custom"`` node from ``{customType, content, display?, details?}``
        and APPENDs it to the authoritative session log, so it lands on the active
        path exactly like a ``before_agent_start`` injection: persisted, rendered
        in the transcript / tree, and reload-invariant.

        Per D-E6-1 the node is **display-only by default** — ``options`` may set
        ``visible_to_model: True`` to also feed it to the model (remapped
        custom→user on the wire); left unset (or ``False``) the node is dropped by
        :func:`~tau_agent_core.messages.convert_to_llm` and never reaches the LLM.
        This deliberately does NOT create a third model-visible default channel
        (``before_agent_start`` / ``send_user_message`` already serve that).

        The append is announced on the ``custom_message`` channel
        (:meth:`_announce_append`), because this is the one durable message
        nothing else on the bus reports: it belongs to no completion and no tool
        call, so a head built from streaming events would show it only after the
        next reload (docs/EXTENSION-LOCKS.md §9.1).

        Returns the appended entry id.

        Raises:
            ValueError: if ``message`` has no ``content`` (nothing to append) or no
                ``customType`` (the extension-origin identity is not fabricated) —
                Fail-Early.
        """
        options = options or {}
        if "content" not in message:
            raise ValueError("send_message: message is missing 'content' — nothing to append")
        if "customType" not in message:
            raise ValueError(
                "send_message: message is missing 'customType' — the extension-origin "
                "type is required (Fail-Early, no fabricated default)"
            )
        node = create_custom_message(
            custom_type=str(message["customType"]),
            content=message["content"],
            display=bool(message.get("display", True)),
            details=message.get("details"),
            visible_to_model=bool(options.get("visible_to_model", False)),
            timestamp=self._timestamp(),
        )
        await self._record_config()
        entry_id = await self.acting_cursor.append_custom_message(
            node, custom_type=str(message["customType"])
        )
        self._announce_append("custom_message", entry_id=entry_id, message=node)
        return entry_id

    def _announce_append(self, channel: str, **payload: Any) -> None:
        """Publish an append that no other bus event reports, fire-and-forget.

        Same dispatch as :meth:`route_session_event`: the appender is
        synchronous, the bus is async, so the emit is a task on the running loop.

        With NO running loop there is nothing to schedule on, and what that means
        depends on whether anyone was listening. Nobody subscribed — a headless
        script building a session — is not a lost event, so it returns. Somebody
        subscribed is a head that will never hear about a message that is now on
        the tree, which is the divergence this method exists to prevent, so it
        raises with the channel named.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._events.has_listeners(channel):
                raise RuntimeError(
                    f"an append was announced on {channel!r} from outside the event loop, "
                    f"and {channel!r} has subscribers that can never be told. The entry is "
                    "on the tree and every attached head is now behind it. Append from the "
                    "session's own loop, or from a thread via the threadsafe door."
                ) from None
            return
        task = loop.create_task(self._events.emit_channel(channel, **payload))
        self._session_event_tasks.add(task)
        task.add_done_callback(self._session_event_tasks.discard)

    async def _append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        """Append a durable, NON-message ``customEntry`` node (``api.append_entry``).

        The backend for ``ExtensionAPI.append_entry`` (E6 §2 / S39). Persists the
        extension's ``{customType, data}`` into the authoritative session log as its
        own tree entry KIND, replacing the old RAM-only registry ``_entry_store``
        that was lost on restart (G4). Unlike ``_append_custom_message`` this is NOT
        a model-facing message: ``ConversationTree`` never folds a ``customEntry``
        into the loop context and ``convert_to_llm`` never sees it, so it is durable
        tree-as-backplane state — persisted, reload-invariant, readable through
        ``ctx.entries()`` — but explicitly excluded from model input.

        Returns the appended entry id.

        Raises:
            ValueError: if ``custom_type`` is empty (the extension-origin identity is
                not fabricated) or ``data`` is not a dict — Fail-Early, no silent
                default.
        """
        if not custom_type:
            raise ValueError(
                "append_entry: custom_type is required (Fail-Early, no fabricated default)"
            )
        if not isinstance(data, dict):
            raise ValueError(f"append_entry: data must be a dict, got {type(data).__name__}")
        await self._record_config()
        return await self.acting_cursor.append_custom_entry(custom_type, data)

    @property
    def pending_request(self) -> ExtensionRequest | None:
        """The extension request the cursor points at, or ``None`` (EXTENSION-LOCKS §2).

        The cursor only — never a walk. Every head reads this to decide what to
        draw, and :meth:`submit` reads it to decide whether to refuse, so the
        thing a user is looking at and the thing that refused them are one entry.
        """
        return request_at(self.acting_cursor.entries(), self.acting_cursor.leaf)

    async def answer_request(
        self, request_id: str, action: str, values: dict[str, Any] | None = None
    ) -> ExtensionCommandResult:
        """Answer an extension's ask: append the response, then dispatch its action.

        Reference: docs/EXTENSION-LOCKS.md §3, §8. The append is what releases a
        lock — appending moves the cursor and the cursor is where a lock is read
        — so the order matters: the handler runs on a session that is already
        unlocked and may therefore submit a turn of its own.

        Args:
            request_id: The entry :meth:`~tau_agent_core.extension_types.ExtensionAPI.request_user_action`
                returned.
            action: The pressed action's ``label``, which names the command.
            values: The filled fields, keyed by field name. ``None`` is the empty
                dict, which is what an ask declaring no fields takes.

        Returns:
            The dispatched command's :class:`ExtensionCommandResult`. ``handled``
            is ``False`` when the extension is not loaded — the response entry is
            still appended and the lock still released, because a lock the owner
            cannot answer must not become a session nobody can continue.

        Raises:
            ValueError: no such request, the request carries no ask, an unknown
                action label, or values that :func:`validate_form_values`
                rejects. Fail-Early: nothing is coerced and no partial answer is
                persisted.
        """
        request = find_request(self.acting_cursor.entries(), request_id)
        if request is None:
            raise ValueError(f"answer_request: no extension request with id {request_id!r}")
        if request.ask is None:
            raise ValueError(
                f"answer_request: request {request_id!r} carries no ask, so there is "
                "nothing to answer; it is cleared by navigating or by its own command"
            )
        actions = {a["label"]: a["command"] for a in request.ask["actions"]}
        if action not in actions:
            raise ValueError(
                f"answer_request: {action!r} is not one of this ask's actions {sorted(actions)}"
            )
        answered = dict(values or {})
        validate_form_values(request.ask["fields"], answered)
        await self._append_custom_entry(
            RESPONSE_ENTRY_TYPE,
            build_response_data(request_id, request.extension, answered, action),
        )
        return await self.run_extension_command(actions[action], request_id)

    def _build_turn_tools(self) -> list[AgentTool]:
        """Merge the built-in tools with the active extension tools for a turn.

        Two same-name collisions meet at this dict and they are **not** the same
        thing (H3):

        - **Extension over built-in is a documented precedence with a fixed
          winner** and it is kept exactly as it was — pi parity,
          ``_refreshToolRegistry`` sets extension tools last
          (``coding-agent/src/core/agent-session.ts:2356-2360``), and it is what
          makes ``ext_kit.steer.wrap_tool("read", …)`` able to shadow a built-in.
          The outcome does not depend on load order, so nothing here is undetermined.
        - **Two entries of the same name inside ``self._tools`` raises.** That one
          *is* last-write-wins with no signal: the survivor is whichever the caller's
          list happened to put last, so which implementation the model calls becomes
          an artifact of construction order. §7.1.1 makes that a determinism break,
          and it would surface as a flaky assertion somewhere that never touched the
          registry. Fail-Early: refuse it where it is still legible.

        The duplicate check runs before the early return, so a session with a broken
        tool list fails identically whether or not an extension happens to be loaded.
        Extension tools cannot collide with each other here — they arrive from
        :meth:`ExtensionRegistry.get_active_tools`, a name-keyed dict whose own
        duplicate guard is :meth:`ExtensionRegistry.register_tool`.

        ``no_tools="all"`` short-circuits between those two — see the comment at
        the check itself for why that position and not another.

        Raises:
            ValueError: if ``self._tools`` holds more than one tool per name.
        """
        seen: dict[str, int] = {}
        for index, t in enumerate(self._tools):
            if t.name in seen:
                raise ValueError(
                    f"Duplicate tool name {t.name!r} in the session tool list "
                    f"(indices {seen[t.name]} and {index}). Which one the model "
                    "would call depends on list order, so the collision is refused "
                    "rather than silently resolved."
                )
            seen[t.name] = index

        if self._no_tools == "all":
            return []

        ext_tools = self._resolve_extension_tools()
        if not ext_tools and self._tool_allowlist is None and not self._tool_denylist:
            return self._tools
        by_name: dict[str, AgentTool] = {t.name: t for t in self._tools}
        for t in ext_tools:
            by_name[t.name] = t
        return [
            tool
            for name, tool in by_name.items()
            if (self._tool_allowlist is None or name in self._tool_allowlist)
            and name not in self._tool_denylist
        ]

    def check_tool_filters(self) -> None:
        """Refuse a ``--tools`` or ``--exclude-tools`` name that no tool answers to (#43).

        Called once extensions have loaded, because an extension's tools exist
        only after its ``register`` ran. A built-in name counts even when it was
        not constructed.

        Raises:
            ValueError: ``Unknown tool: <name>`` for the first such name.
        """
        from tau_agent_core.sdk import _BUILTIN_TOOL_CLASSES

        known = (
            set(_BUILTIN_TOOL_CLASSES)
            | {t.name for t in self._tools}
            | {info.name for info in self._registry.get_all_tools()}
        )
        for name in sorted((self._tool_allowlist or frozenset()) | self._tool_denylist):
            if name not in known:
                raise ValueError(f"Unknown tool: {name}")

    def _make_extension_api(
        self,
        hook_handlers: Any = None,
        context: Any = None,
        config: dict[str, Any] | None = None,
    ) -> ExtensionAPI:
        """Create an ExtensionAPI bound to this session's real refs.

        Binds the live loop event bus (``self._events``) and the session-owned
        registry so ``api.on(event, handler)`` subscribes to the same bus the
        agent loop emits on, and registered tools/commands land where the
        session can read them (E1.1 / step S3).

        ``hook_handlers`` is this extension's own :class:`ExtensionHandlers` bucket
        in the runner: ``api.on`` routes the four mutating hooks there (S24).
        ``context`` shares the live :class:`ExtensionContext` — passed for the
        per-extension apis so every handler sees the one context the session binds
        the abort signal / live session onto; ``None`` (the session-shared api built
        at construction) makes a fresh one at the session's cwd, never ``"."``.
        ``config`` is this extension's per-extension config slice (S40); ``None``
        for the session-shared api (which no extension file receives).

        Returns:
            An ExtensionAPI bound to this session, its event bus, and registry.
        """
        if context is None:
            # The session's cwd, where built-in tools resolve paths; "." is the process cwd.
            context = ExtensionContext(cwd=self._cwd)
        return ExtensionAPI(
            session=self,
            event_bus=self._events,
            registry=self._registry,
            context=context,
            hook_handlers=hook_handlers,
            config=config,
        )

    def _surface_extension_error(self, error: ExtensionError) -> None:
        """Route a hook / notify handler failure to the live UI surface (S44).

        The single on_error sink for BOTH the mutating-hook / lifecycle dispatcher
        (:class:`ExtensionRunner`) and the notify ``EventBus`` (via
        :meth:`_surface_notify_error`). Paints through the session's ONE shared
        :class:`ExtensionUI` at ``warning`` level: a TUI warning notice once a
        delegate is set (:meth:`set_ui_delegate`), else the headless
        ``[τ] warning: …`` stderr line. Read at call time, so whichever delegate is
        current when the error fires is used.

        The reporter must not itself crash the dispatch it is reporting from (a
        raising ``notify`` would propagate out of the hook call-site and, worse,
        mask the original error), so a failure of the surface falls back to stderr —
        still visible, never swallowed (Fail-Early).
        """
        message = f"extension error in {error.extension_path} ({error.event}): {error.error}"
        try:
            self._extension_api.ui.notify(message, "warning", source=error.extension_path)
        except Exception as report_err:  # noqa: BLE001 — reporter must not crash the loop
            import sys

            print(f"[τ] {message} (surface failed: {report_err})", file=sys.stderr)

    def _surface_notify_error(self, error: BaseException, channel: str) -> None:
        """Adapt a notify-``EventBus`` handler failure onto the on_error surface (S44).

        The bus is anonymous — it cannot attribute a failing subscriber to a
        specific extension file (unlike the runner, whose buckets are path-labelled),
        so the origin is reported honestly as the notify channel rather than
        fabricating an extension name (Fail-Early: no invented attribution). Wraps
        the raw ``(exc, channel)`` into an :class:`ExtensionError` and routes it
        through the shared :meth:`_surface_extension_error` sink so a raising
        observer surfaces exactly like a raising mutating hook.
        """
        self._surface_extension_error(
            ExtensionError(
                extension_path="notify handler",
                event=channel,
                error=str(error),
            )
        )

    def set_ui_delegate(self, delegate: Any) -> None:
        """Route extension ``api.ui`` calls to a live front-end delegate (E5 §4 / S33).

        Sets the delegate on the session's ONE shared :class:`ExtensionContext` —
        the same context every bound extension api receives (``_bind_extension_api``
        passes ``self._extension_api.context``), so a single call flips the shared
        :class:`ExtensionUI` into TUI mode for EVERY loaded extension at once. From
        then on ``api.ui.notify(msg, level)`` reaches the delegate (the TUI screen)
        instead of the headless stderr sink. Nothing calls this on the headless
        path, so ``tau -p`` keeps the stderr behaviour.

        Args:
            delegate: The front-end object ``api.ui`` calls are forwarded to.
        """
        self._extension_api.context.set_ui_delegate(delegate)

    def set_extension_record_sink(self, sink: Any) -> None:
        """Route extension activity to a headless JSON record sink (E7 §3 / S49 — G10).

        Sets the sink on the session's ONE shared :class:`ExtensionUI` (via the
        context), so every loaded extension's ``api.ui.notify(...)`` emits a
        ``{"type": "extension", …}`` record through ``sink`` instead of the headless
        stderr line — the parallel record family the ``--mode json`` frontend writes
        alongside the closed ``AgentEvent`` set. Only the headless JSON path installs
        one; the TUI sets a live delegate instead and ``--mode text`` leaves it unset
        (stderr, unchanged).

        Args:
            sink: Called with each extension record; ``None`` clears it.
        """
        self._extension_api.context.set_record_sink(sink)

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Set the headless dialog-answer policy for every loaded extension (E7 §3 / S48).

        A headless dialog auto-answers only for the methods in ``policy``; any
        other raises :class:`HeadlessDialogError`. Run-scoped: nothing is written
        to the session tree.

        Args:
            policy: Dialog method name to the answer token it auto-answers with
                (the resolved ``--ui-defaults`` / config ``"ui_defaults"``).

        Raises:
            ValueError: An unknown method or answer token.
        """
        self._extension_api.context.set_headless_ui_defaults(policy)

    @property
    def vocabulary(self) -> Vocabulary:
        """τ's registry, plus the flows this session's extensions declared.

        What every caller reading the flow tables should pass — ``next_step``,
        ``flow_arguments``, ``enumerate_domain``, ``complete_command_argument`` — so a
        gesture an extension added is offered and stepped exactly like a built-in.
        See docs/EXTENSION-FLOWS.md.

        Per-session rather than a mutable global, which is the property that makes it
        safe: a fork, a ``switch_session`` and a sub-agent are separate sessions in one
        process, and a global would have let one see another's flows. :data:`BUILTIN`
        is returned unchanged when nothing declared a flow, so a session with no
        extensions costs nothing and is the same object every test already reads.

        Cached against the registry's ``flows_revision`` because building it runs the
        whole registry cross-check and a head asks on every keystroke.
        """
        revision = self._registry.flows_revision
        if self._vocabulary is None or self._vocabulary_revision != revision:
            declared = self._registry.get_flows().values()
            built = BUILTIN.extended_with(
                [entry.flow for entry in declared],
                domains={entry.domain.name: entry.domain for entry in declared if entry.domain},
                enumerators={
                    entry.domain.name: entry.enumerator
                    for entry in declared
                    if entry.domain is not None and entry.enumerator is not None
                },
                aliases=self._registry.get_bindings(),
            )
            self._vocabulary = built
            self._vocabulary_revision = revision
        return self._vocabulary

    async def _run_extension_flow(self, ready: Ready) -> Dispatched:
        """Perform a bound extension flow by calling the command's own handler.

        The bound value goes back to text, because the handler contract is
        ``(args, ctx)`` and declaring a flow does not change it — an extension that
        adds a declaration to a command it already shipped keeps working for every
        caller that never learned about the declaration. A flow with no argument
        passes ``""``, which is what a bare ``/name`` has always passed.

        Args:
            ready: The bound flow, whose ``flow`` names the registered command.

        Returns:
            The :class:`~tau_agent_core.flows.Performed` the handler's return value
            becomes, the same record an undeclared command reports.

        Raises:
            UnsupportedCommandError: The command was unregistered between the dispatch
                decision and this call.
        """
        values = [str(value) for value in ready.arguments.values()]
        result = await self.run_extension_command(ready.flow, " ".join(values))
        if not result.handled:
            raise UnsupportedCommandError(
                f"/{ready.flow} declared a flow but run_extension_command reports it "
                "unknown — it was unregistered between the dispatch decision and the "
                "dispatch itself. Refusing rather than falling through to the model."
            )
        return Performed(
            flow=ready.flow, mutation=ready.mutation, data={"output": result.output_text()}
        )

    def get_extension_commands(self) -> list[tuple[str, str]]:
        """List the TYPED extension commands — what a reader can write after a ``/``.

        What the palette and the completion popup list (docs/EXTENSION-NAMESPACE.md).
        An extension that lost a contested name is absent here and present in
        :meth:`get_qualified_commands`.

        Returns:
            ``(typed name, description)`` for every typed name bound to a
            command; the description is ``""`` when the command gave none.
        """
        out: list[tuple[str, str]] = []
        for typed, qualified in self._registry.get_bindings().items():
            command = self._registry.get_command(qualified)
            if command is not None:
                out.append((typed, str(command.get("description", ""))))
        return out

    @property
    def prompt_templates(self) -> PromptTemplateCatalog:
        """The prompt templates ``/name args`` expands (docs/PROMPT-TEMPLATES-AND-SKILLS.md)."""
        return self._prompt_templates

    @property
    def skills(self) -> SkillCatalog:
        """The skills ``/skill:name args`` expands and the system prompt lists."""
        return self._skills

    def get_prompt_commands(self) -> tuple[CommandCompletion, ...]:
        """The template and skill command words, as a completion list offers them.

        Leaves out a template whose name a built-in or an extension command holds,
        since dispatch reaches those first; lists a broken one with its error.

        Returns:
            One completion per offered template and skill command.
        """
        taken = {name for name, _ in self.get_extension_commands()}
        return prompt_command_completions(self._prompt_templates, self._skills, taken)

    def get_qualified_commands(self) -> list[tuple[str, str]]:
        """List the PRIVATE registry — every command at its ``ext:…`` name.

        Heads keep these out of ordinary completion and offer them under the
        ``ext:`` prefix, so a command that lost its typed name is still runnable.

        Returns:
            ``(qualified name, description)`` for every command an extension
            registered, contested or not; the description is ``""`` when absent.
        """
        return [
            (name, str(command.get("description", "")))
            for name, command in self._registry.get_commands().items()
        ]

    def get_extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        """List extension-registered key shortcuts (E10 §6 / S69).

        The TUI's ``ctrl+e`` chord menu and the command palette list and dispatch
        these; ``command``/``args`` go through :meth:`run_extension_command`, the
        same path as a typed ``/name args``.

        Returns:
            ``(key, command, args, description)`` per ``api.register_shortcut``.
            ``key`` is the chord tail under the ``ctrl+e`` leader. ``description``
            falls back to the target command's, then to ``""``.
        """
        out: list[tuple[str, str, str, str]] = []
        for key, shortcut in self._registry.get_shortcuts().items():
            command = str(shortcut.get("command", ""))
            args = str(shortcut.get("args", ""))
            description = shortcut.get("description")
            if not description:
                cmd = self._registry.get_command(command)
                description = str(cmd.get("description", "")) if cmd else ""
            out.append((key, command, args, str(description)))
        return out

    def get_extension_command_args(self, name: str) -> str | None:
        """The declared argument placeholder for command ``name`` (E7 §3 / S51).

        A command declares ``"args": "<placeholder>"`` in ``register_command`` when
        it expects a free-form argument string. The palette, which has no argument
        line, reads this to decide whether to open the S47 input modal first.

        Args:
            name: The command's registered name.

        Returns:
            The placeholder, or ``None`` when the command is unknown or declares
            no ``args``.

        Raises:
            TypeError: The declared ``args`` is not a string (Fail-Early).
        """
        command = self._registry.get_command(name)
        if command is None:
            return None
        placeholder = command.get("args")
        if placeholder is None:
            return None
        if not isinstance(placeholder, str):
            raise TypeError(
                f"extension command {name!r} declared non-string 'args' "
                f"({type(placeholder).__name__}); 'args' must be the placeholder string."
            )
        return placeholder

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        """Run an extension-registered slash command (E5 §5 / S35; output channel S46).

        Port of pi's ``_tryExecuteExtensionCommand`` (agent-session.ts:1143). The
        handler is called with ``(args, ctx)``, ``ctx`` being the session's one live
        :class:`ExtensionContext`, and awaited if it returns an awaitable.

        Args:
            name: The command's registered name.
            args: The argument string, as typed after ``/name``.

        Returns:
            ``handled=False`` for an unknown command, so the caller can fall
            through; otherwise ``handled=True`` and the handler's return value as
            display-only ``output``.

        Raises:
            RuntimeError: The command has no callable ``handler``.
            Exception: Whatever the handler raises, unchanged.
        """
        command = self._registry.get_command(name)
        if command is None:
            return ExtensionCommandResult(handled=False)
        handler = command.get("handler")
        if not callable(handler):
            raise RuntimeError(
                f"extension command {name!r} has no callable 'handler'; it was "
                "registered but cannot run (register_command requires a handler)."
            )
        result = handler(args, self._extension_api.context)
        if inspect.isawaitable(result):
            result = await result
        return ExtensionCommandResult(handled=True, output=result)

    def _bind_extension_api(self, path_label: str) -> ExtensionAPI:
        """The bucket-bound ExtensionAPI a loaded extension is handed (S24).

        Appends a fresh :class:`ExtensionHandlers` bucket for ``path_label`` to the
        session's :class:`ExtensionRunner` (load order preserved) and returns an
        ``ExtensionAPI`` bound to it, sharing the session's registry, event bus, and
        live :class:`ExtensionContext`. ``api.on("tool_call"/…)`` on the returned
        api lands in this bucket — the dispatch surface the loop's hook call-sites
        actually read.

        The per-extension config slice (S40) is selected by the bucket's
        identifier (docs/EXTENSION-PACKAGES.md §2) from ``self._extensions_config``
        (``~/.tau/config.json`` ``"extensions"`` + ``--ext-config`` overrides). An
        unconfigured extension gets ``{}``.
        """
        bucket = self._extension_runner.register_extension(path_label)
        config = self._extensions_config.get(bucket.name, {})
        return self._make_extension_api(
            hook_handlers=bucket,
            context=self._extension_api.context,
            config=config,
        )

    @staticmethod
    def _timestamp() -> int:
        """Get current timestamp in milliseconds."""
        import time

        return int(time.time() * 1000)


def _drop_enqueued(cursor: Cursor) -> None:
    """Mark every ``enqueue`` waiting on ``cursor`` dropped, keeping its text to announce."""
    for wait in cursor.enqueued:
        wait.dropped = True
        cursor.dropped_enqueued.append(wait.text)
    cursor.enqueued.clear()
