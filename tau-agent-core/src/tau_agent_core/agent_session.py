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
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

from tau_llm.abort import AbortSignal
from tau_llm.types import Model, UserMessage

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI, validate_form_values
from tau_agent_core.extension_locks import (
    RESPONSE_ENTRY_TYPE,
    ExtensionRequest,
    build_response_data,
    find_request,
    refusal_reason,
    request_at_cursor,
)
from tau_agent_core.messages import CUSTOM_ROLE, create_custom_message, last_assistant_text
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import (
    MESSAGE_POSITION_BEFORE_USER,
    ExtensionError,
    ExtensionRunner,
)
from tau_agent_core.session import SessionState
from tau_agent_core.session_log import (
    SessionLog,
    agent_spec_in_force,
    session_log_is_addressable,
)
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.agent_loop import AgentLoop, completed_messages
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.capabilities import BUILTIN, CAPABILITIES, Vocabulary
from tau_agent_core.commands import (
    CommandInvocation,
    UnsupportedCommandError,
    dispatch_builtin,
    resolve_command,
)
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
    CompactionPreparation,
    CompactionResult,
    CompactionSettings,
    ContextUsageEstimate,
    compact as run_compaction,
    estimate_context_tokens,
    estimate_span_tokens,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.compaction_policy import CompactionPolicy
from tau_agent_core.compaction_utils import create_file_ops, extract_file_ops_from_message
from tau_agent_core.usage import add_usage, zero_usage
from tau_agent_core.tools.base import AgentTool, ToolDefinition
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


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class ExtensionActionResult:
    """Outcome of a runtime ``/extensions`` action (E10 §6 / S70).

    Runtime enable/disable/reload (lifting the D-E5-6 read-only stance) return this
    so the frontend can report what happened as **display-only** chrome — it is never
    appended to the active path, so the tree-as-truth invariant is untouched. ``ok``
    distinguishes a completed action from a legitimate no-op / bad target (e.g. a name
    that is not loaded); ``message`` is the human-readable line the listing box shows.
    A hard failure (a broken file on reload) still raises out of the action —
    ``ok=False`` is reserved for reportable, non-exceptional outcomes (Fail-Early).

    There is no ``cursor`` field, though one action moves it: disabling an
    extension whose lock is the cursor releases it (docs/EXTENSION-LOCKS.md §6).
    Both projections of this record already carry the LIVE cursor — the RPC verb
    reads ``session.session_log.cursor``, and ``AgentSession.performed`` writes it
    and refuses a caller that hands it one — so a second copy here would be the
    two-writers drift that method exists to remove. The move is visible in
    ``message``.
    """

    action: str
    path: str
    ok: bool
    message: str


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class ExtensionCommandResult:
    """Outcome of :meth:`AgentSession.run_extension_command` (E7 §3 / S46).

    ``run_extension_command`` used to return a bare ``bool`` (handled / unknown)
    and DISCARD the handler's return value, so an extension command could only
    toast (G7). This carries both:

    - ``handled`` — ``True`` iff a command by that name existed and ran (``False``
      lets the caller fall through, e.g. treat the text as a prompt). This is the
      old bool, now a named field.
    - ``output`` — the value the handler RETURNED (a string, a renderable, or
      ``None``). The frontends render it as a **display-only** system box (TUI) /
      printed-or-emitted text (headless); it is chrome, never model input — it is
      NOT appended to the active path, so the E5 §1 tree-as-truth invariant holds
      (a command that wants a durable node uses ``ctx`` explicitly).

    Unknown command → ``ExtensionCommandResult(handled=False)`` (no output).
    """

    handled: bool
    output: object | None = None

    def output_text(self) -> str | None:
        """Coerce ``output`` to display text, or ``None`` when there is nothing to show.

        A handler that returned ``None`` (or an empty string) has no output box.
        Any other value is rendered as its string form — report commands return
        markdown strings; a non-``str`` value is stringified so the text/JSON
        channels stay honest rather than fabricating a shape. Display-only.
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


def _system_prompt_digest(system_prompt: str) -> str:
    """SHA-256 hex digest of the system prompt text — NEVER the prompt itself.

    ``agent_spec`` (W2, NODE-ADDRESSABLE-AGENTS.md) records a system prompt's
    IDENTITY (so a reader can tell "did turns 1-5 and turns 6-10 run under the
    same prompt") without recording its CONTENT — a system prompt routinely
    carries a repo's ``AGENTS.md``/project instructions, which do not belong in a
    durable, JMFTS-indexed record on top of already being available at the
    invoker's fingertips. Same convention as ``LoadedExtension.content_hash``
    (sdk.py) — a sha256 of the exact bytes, so two different prompts are never
    mistaken for the same one.
    """
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _extension_factory_label(ext: Callable) -> str:
    """A stable, identifiable path label for an inline extension factory.

    Extensions loaded from a file get their real path; an inline factory callable
    has none, so derive one from ``__module__`` + ``__qualname__`` (falling back to
    ``repr``) purely so the runner's per-extension buckets are distinguishable in
    load order and in error reporting — the label is not load-bearing.
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


@agent_facing(topic="sessions")
class AgentSession:
    """High-level session API. Combines agent loop, a session log, and events.

    This is the primary entry point for both SDK and TUI usage.

    Persistence goes through a :class:`~tau_agent_core.session_log.SessionLog`
    (the coding-agent's file ``Session`` on the live path, an
    :class:`~tau_agent_core.session_log.InMemorySessionLog` on the SDK default
    path); context is rebuilt from the log's entries + cursor via
    :class:`~tau_agent_core.conversation_tree.ConversationTree` — the retired
    System-A ``SessionManager`` no longer participates (§2.6).

    Attributes:
        _session_log: SessionLog the turn's messages/compactions append to.
        _model: Model configuration for LLM calls.
        _system_prompt: System prompt for the agent.
        _tools: List of AgentTool instances.
        _events: EventBus for event dispatch.
        _extensions: List of extension factory callables.
        _is_streaming: Whether the agent loop is currently running.
        _abort_signal: Signal for aborting the current turn.
        _turn_lock: The admission gate every :meth:`submit`-driven turn (and
            :meth:`continue_conversation`) holds while running — see
            docs/SUBMISSION-LIFECYCLE.md "The one door" step 1 and the "two
            unguarded doors" callout.
        _pre_turn_leaf: The log cursor immediately BEFORE the most recently
            admitted turn's user node (or, for :meth:`continue_conversation`,
            immediately before it ran at all) — recorded at admission for
            provenance and, per decision 2, ``rollback``'s navigate-back target.
        _current_turn_token: Monotonically incremented once per admitted turn
            (:meth:`submit` OR :meth:`continue_conversation` — review fix,
            must_fix #1: ``rollback`` used to trust a *session-level*
            ``_pre_turn_leaf`` as if it always belonged to "whatever turn is
            CURRENTLY running", which is false the instant that turn is a
            ``continue_conversation()`` call (never writes it) or a rollback
            queued FIFO-behind an "enqueue" submission (a second turn can run
            to completion, and overwrite it, before the rollback resumes).
            Set alongside ``_pre_turn_leaf`` and never reset to ``None`` on
            turn end, so ``rollback`` can compare "the token I captured before
            signalling abort" against "the token now current" and detect that
            a DIFFERENT turn — not the one it aborted — is the one it is
            about to navigate away from.
        _current_submission: The ``Submission`` currently holding ``_turn_lock``
            (``None`` when idle) — read by :meth:`_stamp_event` so every
            ``AgentEvent`` a submission-driven turn emits carries its
            ``submission_id``/``source``/``submitter``/``correlation``
            (docs/SUBMISSION-LIFECYCLE.md "Provenance on events"). Its ``depth``
            is the ADMITTED depth (:func:`~tau_agent_core.submission.next_submission_depth`),
            not necessarily the one the submitter constructed the record with.
            The same value is published for the duration of the turn on
            :data:`~tau_agent_core.submission.DRIVING_SUBMISSION_DEPTH` — the
            context var, NOT this attribute, is what a nested submission
            inherits from, because a task spawned inside this turn must count as
            self-submission even if it does not reach :meth:`submit` until after
            the turn has ended (decision 3; see that constant for why an
            attribute gets this backwards in both directions).
        _turn_task: The ``asyncio.Task`` currently holding ``_turn_lock``
            (``None`` when idle) — review fix, must_fix #2: a hook (``input``,
            ``tool_call``, ``turn_end``, ``user_turn_end``) dispatched from
            INSIDE that same task calling back into :meth:`submit` (directly,
            or via :meth:`prompt`/``ctx.prompt``) can never be admitted —
            every ``multitask_strategy`` here waits on or inspects a lock this
            very task already holds — so :meth:`submit` compares
            ``asyncio.current_task()`` against this and raises rather than
            deadlocking silently. A DIFFERENT task submitting concurrently is
            unaffected: that is the "enqueue" waits genuinely for.
        _pending_steer_messages: The steering queue for
            ``multitask_strategy="steer"`` (docs/SUBMISSION-LIFECYCLE.md phase 4).
            Appended to by a submission that found a turn already in flight, and
            by ``ctx.send_user_message(deliver_as="steer")``; DRAINED by the
            running :class:`~tau_agent_core.agent_loop.AgentLoop` immediately
            before each provider call, because "before the next LLM call" is a
            point only the loop can name. The list object itself is passed to
            every loop this session builds — see
            :attr:`~tau_agent_core.agent_loop.AgentLoop._steer_queue` for why a
            shared list and not a drain callback. Cleared by :meth:`abort` and by
            ``rollback``'s abort signal (pi's ``abort()`` calls
            ``clearSteeringQueue()``): the turn the content was aimed at is gone,
            and replaying it into the turn that REPLACES it would put words into
            a conversation the submitter rolled back past.
        _forked_tasks: The supervised registry for ``multitask_strategy="fork"``
            submissions (docs/SUBMISSION-LIFECYCLE.md "fork"), keyed by
            ``submission_id`` (chosen up front — unlike the branch's ``lane``,
            which ``spawn_branch`` only mints once the task is already running).
            Cancelled by :meth:`abort` and drained by :meth:`emit_session_shutdown`
            so a forked branch cannot outlive the session that spawned it.
        _loop: The event loop this session is BOUND to — the only one allowed to
            call :meth:`submit` (docs/SUBMISSION-LIFECYCLE.md "Task marshalling").
            ``None`` until something actually runs the session on a loop; see
            :meth:`_bind_loop` for why binding is lazy rather than a constructor
            argument, and for the one case in which the binding legitimately
            moves (the previous loop is dead).
        _threadsafe_tasks: The supervised registry for submissions marshalled in
            from a foreign loop/thread by :meth:`submit_threadsafe`, keyed by
            ``submission_id``. Same reason ``_forked_tasks`` exists: nobody on
            this loop is awaiting them, so without a strong reference asyncio may
            garbage-collect a running task, and without a registry a submission
            admitted a millisecond before shutdown would outlive the session.
    """

    def __init__(
        self,
        session_log: SessionLog,
        model: Model,
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
    ) -> None:
        self._bus_available = bus_available
        self._session_log = session_log
        self._model = model
        self._system_prompt = system_prompt
        self._tools: list[AgentTool] = tools or []
        if no_tools not in (None, "all", "builtin"):
            raise ValueError(
                f"no_tools must be 'all', 'builtin' or None, got {no_tools!r} — "
                "this is a resolved policy, not free text; an unrecognised value "
                "would silently mean 'no suppression'."
            )
        self._no_tools = no_tools
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
        self._is_streaming = False
        self._abort_signal = AbortSignal()
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._threadsafe_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pre_turn_leaf: str | None = None
        self._turn_token_counter: int = 0
        self._current_turn_token: int | None = None
        self._current_submission: Submission | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._forked_tasks: dict[str, asyncio.Task[Any]] = {}
        self._api_key = api_key
        self._reasoning = reasoning
        if compaction_policy is not None and compaction_settings is not None:
            raise ValueError(
                "pass compaction_policy OR compaction_settings, not both: the policy "
                "supplies its own settings, and two sources for the reserve would make "
                "the threshold the policy proves itself against unknowable"
            )
        self._compaction_policy = compaction_policy
        if compaction_policy is not None:
            compaction_policy.bind_to(model)
            self._compaction_settings = compaction_policy.compaction_settings
        else:
            self._compaction_settings = compaction_settings or DEFAULT_COMPACTION_SETTINGS
        self._policy_turns_used = 0
        self._model_resolver = model_resolver
        self._last_usage: dict[str, Any] | None = None

        self._side_usage: dict[str, int] = zero_usage()

        self._deferred_ops: list[dict[str, Any]] = []
        self._pending_follow_up_messages: list[str] = []
        self._pending_next_turn_messages: list[str] = []
        self._pending_steer_messages: list[UserMessage] = []

        self._session_event_tasks: set[asyncio.Task[None]] = set()

        self._extension_api = self._make_extension_api()
        self._extension_runner = ExtensionRunner(context=self._extension_api.context)
        self._extension_runner.on_error(self._surface_extension_error)
        self._events.on_error(self._surface_notify_error)
        self._events.on("message_end", self._record_completion_usage)
        for ext in self._extensions:
            ext(self._bind_extension_api(_extension_factory_label(ext)))

        self._record_agent_spec()

    def _record_agent_spec(self) -> None:
        """Append a NON-AUTHORITATIVE ``agent_spec`` provenance node (W2).

        Written at construction and again on every runtime spec swap
        (:meth:`set_model` — the one frame field this class lets a caller change
        after construction). Carries exactly what NODE-ADDRESSABLE-AGENTS.md §5 /
        Decision 3 asks for and nothing more:

        - ``model`` — the :meth:`get_model` projection (id/provider/context_window).
        - ``system_prompt_digest`` — a sha256 of the prompt TEXT, never the prompt
          itself (:func:`_system_prompt_digest`); the prompt routinely carries a
          repo's project instructions and has no business duplicated into a
          durable, JMFTS-indexed record.
        - ``tools`` — tool names, so "read-only reviewer" vs. "full-tool builder"
          is legible from the transcript without re-deriving it from tool-call
          blocks.
        - ``extensions`` — every extension bound to THIS session AS OF THE
          MOMENT THIS METHOD RUNS: inline-factory labels
          (:func:`_extension_factory_label`) plus the path of every file
          extension :meth:`load_extensions` has bound and
          :meth:`disable_extension` has not since removed. Decision 4 ("forks
          start extension-free") is about what a NEW forked session inherits
          from its parent — it says nothing about whether the PRIMARY
          session's own record reflects extensions it loads into itself,
          which is what this field is for.

          Note what this does NOT do: :meth:`load_extensions` is not itself a
          re-record trigger (only :meth:`set_model` is, alongside
          construction) — a session that loads file extensions and never
          swaps its model keeps the construction-time snapshot, which still
          shows ``extensions: []``. Making ``load_extensions`` re-record was
          considered and rejected here: it is called mid-turn by several
          extension demos (e.g. ``examples/60_retrieval_review.py``'s
          ``/review`` dispatch) whose own tests assert that dispatch writes
          NOTHING to the tree (``test_60_retrieval_review.py``'s
          ``test_the_fan_out_writes_nothing_to_the_tree``) — coupling a
          provenance write to every load call would falsify that invariant
          for an unrelated reason. What IS fixed here: this field no longer
          silently drops file extensions from whichever record DOES get
          written after they load (e.g. a ``set_model`` following
          ``load_extensions``), which was the literal defect — the field
          read only ``self._extensions`` (inline factories) and never
          consulted ``self._loaded_extensions`` at all.
        - ``cwd`` — ``os.getcwd()``. ``AgentSession`` has no cwd field of its own
          (§5 "The filesystem is frame, not path": cwd is process-shared state no
          tree operation forks), so this is the actual working directory the
          process was in at the moment of the record, not something threaded
          through the constructor.

        Uses ``append_custom_entry`` — a plain ``customEntry`` — for the reason
        the work item names: ``ConversationTree`` already excludes that KIND from
        ``context_for`` (conversation_tree.py `# customEntry ... deliberately NOT
        rendered here`), so this can never reach the model and needs no new
        exclusion logic. It is durable (readable via ``ctx.entries()``/the tree
        browser) but it is a RECORD, not a contract — nothing reads it back to
        reconstruct a session (§5), and it must never be asked to.

        ABSOLUTE: ``self._api_key`` never appears here, hashed or otherwise. It is
        not part of the frame this node records — a credential has no legibility
        value and the one prohibition this work item cannot relax.
        """
        loaded_paths = [
            path for path in self._loaded_extensions if path not in self._disabled_paths
        ]
        self._session_log.append_custom_entry(
            "agent_spec",
            {
                "model": self.get_model(),
                "system_prompt_digest": _system_prompt_digest(self._system_prompt),
                "tools": [t.name for t in self._tools],
                "extensions": [_extension_factory_label(ext) for ext in self._extensions]
                + loaded_paths,
                "cwd": os.getcwd(),
            },
        )

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Current conversation messages (active path).

        Built at read time from the log's raw entries + persisted cursor by
        ``ConversationTree.context_for`` — the leaf→root walk plus the
        compaction/branch_summary splice (§2.1, §2.6).
        """
        return ConversationTree(self._session_log.entries(), self._session_log.cursor).context_for()

    @property
    def session_log(self) -> SessionLog:
        """The persistence facade this session reads from and appends to.

        Exposed as a *settable* seam so a caller that OWNS the authoritative log —
        the TUI, whose live ``session_store.Session`` object is swapped on new-chat
        / clear / resume — can rebind this ``AgentSession`` onto the live session
        (``TauBackend.bind_session_log``). That makes ``AgentSession`` the SOLE
        persister on the live path (E3-ctx / D3): the turn's messages, compactions,
        and cursor moves all append through the one on-disk log the TUI reads back.
        pi keeps a single session per process; τ's TUI replaces the file ``Session``
        object, so the seam is a rebindable property rather than a
        construction-only argument.
        """
        return self._session_log

    @session_log.setter
    def session_log(self, log: SessionLog) -> None:
        self._session_log = log

    @property
    def state(self) -> SessionState:
        """Read-only access to session state. Identity is the session UUID (§4.2)."""
        return SessionState(
            session_id=self._session_log.id,
            status="running" if self._is_streaming else "idle",
        )

    @property
    def is_streaming(self) -> bool:
        """Whether the agent loop is currently streaming."""
        return self._is_streaming

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
        return self._abort_signal.is_aborted()

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
        return self._turn_lock

    def get_model(self) -> dict[str, Any]:
        """The active model as ``{id, provider, context_window}`` (S45).

        A small, stable projection of the loop's ``Model`` (pi returns the whole
        ``Model``; τ exposes only the three fields an extension needs to route,
        price, or gauge a context window — keeping the extension API decoupled from
        the full model schema). Read at call time, so it reflects a prior
        :meth:`set_model`.
        """
        model = self._model
        return {
            "id": model.id,
            "provider": model.provider,
            "context_window": model.context_window,
        }

    def set_model_resolver(self, resolver: Callable[[str], Model]) -> None:
        """Bind the model-name resolver used by :meth:`set_model` (S45).

        A frontend calls this after building the session so ``ctx.set_model(name)``
        can turn a config model NAME into a concrete ``Model`` (see
        ``backends.make_model_resolver``, a closure over ``config["models"]``). The
        harness core deliberately does not read ``~/.tau/config.json`` itself
        (layering) — the resolver is the seam.
        """
        self._model_resolver = resolver

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
        """The tools bound to this session, in the order the loop sees them.

        A fresh list, so a caller listing them cannot append to the session's own.
        The :class:`~tau_llm.tools.AgentTool` objects themselves are shared — they
        are what the loop executes, and copying them would hand a reader a tool that
        is not the one that runs.
        """
        return list(self._tools)

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
        self._compaction_settings.enabled = bool(enabled)
        return self._compaction_settings.enabled

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
                cursor — this adds that.
            flow: The flow that named the mutation, when one did.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` carrying ``data`` plus the
            resulting cursor.

        Raises:
            KeyError: No capability has that name.
            ValueError: The named capability is a read, or the caller already put a
                ``cursor`` in ``data``. Both are Fail-Early: a read reporting a
                cursor is E5 rule 2 broken, and a hand-supplied cursor is a second
                answer to the question this method exists to answer.
        """
        declared = CAPABILITIES[mutation]
        if declared.kind != "mutation":
            raise ValueError(
                f"performed({mutation!r}) names a read. E5 rule 2: a read never carries "
                "a cursor, so there is no completion here to stamp."
            )
        if "cursor" in data:
            raise ValueError(
                f"performed({mutation!r}) was handed a cursor in `data`. This method is "
                "what puts it there, and two writers of one field is the drift it removes."
            )
        cursor = self._session_log.cursor
        returns = declared.returns or {}
        carries = "cursor" in returns.get("properties", {})
        return Performed(
            flow=flow,
            mutation=mutation,
            data={**data, "cursor": cursor} if carries else dict(data),
            cursor=cursor if carries else None,
        )

    @agent_facing(topic="sessions")
    def get_session_name(self) -> str | None:
        """This session's durable display name, or ``None`` if it was never named.

        Derived from the log's latest ``session_info`` entry at call time, so it is
        correct across a reload and after another writer renamed the session.

        Returns:
            The name, or ``None``.

        Raises:
            RuntimeError: The bound log has no name to read — an in-memory log has
                nowhere for a ``session_info`` entry to live. Distinct from "never
                named", which is ``None``.
        """
        from tau_agent_core.extension_types import read_session_name

        return read_session_name(self)

    def set_session_name(self, name: str) -> None:
        """Give this session a durable display name.

        Appends a ``session_info`` entry, which is ambient metadata:
        :class:`~tau_agent_core.conversation_tree.ConversationTree` never folds one
        into context, so a rename is persisted and is never model input.

        Args:
            name: The name to give it. Empty is refused rather than stored.

        Raises:
            ValueError: ``name`` is empty.
            RuntimeError: The bound log has no ``append_session_info`` — session
                naming needs a log with somewhere durable to put it.
        """
        from tau_agent_core.extension_types import apply_session_name

        apply_session_name(self, name)

    def set_model(self, name: str) -> dict[str, Any]:
        """Switch the active model by NAME, effective on the NEXT turn (S45).

        Mirrors pi's ``setModel`` (agent-session.ts:1444), adapted to τ: pi takes a
        resolved ``Model`` object; τ takes a config model NAME and resolves it
        through the bound resolver (:meth:`set_model_resolver`). The new ``Model`` is
        stored on ``self._model``; because every turn rebuilds its ``AgentLoop`` with
        ``model=self._model`` (see :meth:`_run_one_turn`), the switch takes effect on
        the next completion — never mid-stream.

        Scope boundary (documented, not a silent fallback): this switches the
        ``Model`` (id / provider / base_url / context_window) only. The session's API
        key (``self._api_key``) is unchanged, so a switch between models that share a
        provider/key — the preset and router cases this unblocks — is correct; a
        cross-provider switch to a model needing a *different* key will surface a
        loud provider auth error, not silently wrong output. It is a RUNTIME switch:
        it is not written back to the session header, so a reload resumes on the
        session's originally stored model.

        Returns:
            The new :meth:`get_model` projection.

        Whatever the resolver raises for an unknown ``name`` (e.g. ``KeyError`` or
        ``ValueError``) propagates unchanged — never swallowed.

        Raises:
            RuntimeError: no resolver is bound (Fail-Early — nothing to resolve
                ``name`` against).
        """
        if self._model_resolver is None:
            raise RuntimeError(
                f"set_model({name!r}): no model resolver is bound to this AgentSession. "
                "The frontend must call set_model_resolver(...) (a closure over the "
                "config 'models' map) before an extension can switch models by name."
            )
        model = self._model_resolver(name)
        if not isinstance(model, Model):
            raise TypeError(
                f"set_model({name!r}): resolver returned {type(model).__name__}, "
                "expected a tau_llm.types.Model"
            )
        if self._compaction_policy is not None:
            self._compaction_policy.bind_to(model)
        self._model = model
        self._record_agent_spec()
        return self.get_model()

    def _summarizer(self) -> tuple[Model, str | None]:
        """The model and key a compaction summarises through (H5 / §16.8).

        Read live rather than cached at construction, so it tracks
        :meth:`set_model` exactly as the shipped behaviour does. With no declared
        policy — the default — this is ``(self._model, self._api_key)``, i.e. what
        ``_perform_compaction`` already did; only a ``local_summarizer`` policy
        answers differently, and it answers with a model and key it declared.
        """
        if self._compaction_policy is None:
            return self._model, self._api_key
        model, key = self._compaction_policy.summarizer_for(self._model)
        return model, (key if key is not None else self._api_key)

    def get_usage(self) -> dict[str, Any] | None:
        """The most recent completion's token usage, or ``None`` (S45).

        The public per-completion usage accessor (anchor G14): a copy of the
        ``usage`` dict the provider filled on the last completion's ``message_end``
        (keys ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
        ``cache_write_tokens`` / ``total_tokens`` / ``cost`` / ``extra``). Extensions
        read this instead of pulling ``event.message["usage"]`` out of a notify event
        or reaching the private ``_model``. ``None`` means no completion has landed
        yet — an honest absence, never a fabricated zero (Fail-Early).

        A DEEP copy: ``cost`` and ``extra`` (the server's ``timings`` block, the
        tool-arg repair count — W8/G4) are nested dicts, so a shallow copy would
        hand every caller a live alias into the session's own record, and one
        extension mutating what it read would silently rewrite the completion's
        measured telemetry for every later reader. Same bug class as the
        ``SessionLog.entries()`` shallow copy.
        """
        return copy.deepcopy(self._last_usage) if self._last_usage is not None else None

    def record_side_usage(self, usage: dict[str, int]) -> None:
        """Add an out-of-loop completion's tokens to the session's side ledger.

        Called by every path that spends tokens without going through the agent loop.
        Cumulative and monotonic for the session's lifetime; consumers take a
        before/after DELTA to attribute the spend to a particular exchange.
        """
        self._side_usage = add_usage(self._side_usage, usage)

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
        return session_log_is_addressable(self._session_log)

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

        model, api_key = self._summarizer()
        messages, usage = await summarize_and_navigate(
            self._session_log,
            target_id,
            model,
            api_key=api_key,
            custom_instructions=custom_instructions,
        )
        self.record_side_usage(usage)
        return messages

    @agent_facing(topic="sessions")
    def get_last_compaction(self) -> CompactionRecord | None:
        """The newest ``compaction`` entry in the bound log, or ``None``.

        Scans ``session_log.entries()`` in append order rather than the
        :class:`~tau_agent_core.conversation_tree.ConversationTree` active path.
        Stated as a scope note rather than hidden: on a session with a second open
        lane this would report a compaction that happened on the other lane, and a
        lane-aware caller wants ``ConversationTree.context_entries`` instead.

        Returns:
            A :class:`CompactionRecord`, or ``None`` if this session has never
            compacted — an honest absence, never a fabricated entry.
        """
        for entry in reversed(self._session_log.entries()):
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
        estimate = estimate_context_tokens(self.messages)
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
            self._last_usage = copy.deepcopy(usage)

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
        """Subscribe to one of the bus's NON-``AgentEvent`` channels. Returns unsubscribe.

        :meth:`subscribe` covers the ``AgentEvent`` stream, whose ``type`` Literal is
        deliberately closed (S49). Everything that is lifecycle rather than a loop
        event rides a separate string channel instead, and until now the only way to
        reach one from outside was to reach into ``_events`` — so ``branch_event``
        had no public consumer at all, which is why a fork was unobservable
        (docs/SUBMISSION-LIFECYCLE.md, end of "Phasing").

        The channels a renderer cares about:

        - ``"submission_start"`` — ``(submission, text, images)``, once per admitted
          submission that will actually run a turn, before the loop starts. The
          submission-level bracket ``agent_start`` is not: a followUp re-entry runs a
          second ``loop.run()`` inside one ``submit()``.
        - ``"submission_end"`` — ``(submission, side_usage)``, in a ``finally``, so it
          arrives however the turn ended.
        - ``"branch_event"`` — ``(lane, label, event)``, one per ``AgentEvent`` a
          ``spawn_branch``/``fork`` sub-agent emits, tagged with the branch's lane.
        - ``"branch_end"`` — ``(lane, label, error)``, once per branch, in a
          ``finally``, so it arrives however the branch ended (returned, contained
          failure, or cancelled by ``abort()``). The branch's OWN ``agent_end`` is
          not that bracket: ``AgentLoop.run`` emits it after the while loop rather
          than from a ``finally``, so a branch that raised or was cancelled never
          emits one — and a consumer bracketing on it holds the span open forever.

        Handlers may be sync or async and are dispatched exactly like
        :meth:`subscribe`'s (fire-and-forget; an exception is surfaced through the
        bus's ``on_error`` sink, never swallowed).
        """
        return self._events.on(channel, handler)

    def route_session_event(self, event: dict[str, Any]) -> None:
        """Route a coding-agent session-lifecycle event onto the extension bus.

        The seam-3 emitter (``session_store.subscribe_session_events``, coding-agent)
        publishes raw dicts ``{"type": <name>, "session": <Session>, **extra}`` for
        ``session_start`` / ``session_before_fork`` / ``session_before_compact`` /
        ``session_shutdown``. This is the bridge that gives them their first
        consumer: each dict is re-emitted onto this session's ``EventBus`` on a
        **separate string channel** named by ``event["type"]`` (``emit_channel``),
        so ``api.on("session_before_compact", handler)`` — a handler subscribed to
        the same bus the loop emits on — fires. The seam is a distinct channel, NOT
        a member of the ``AgentEvent`` Literal (which carries no session events;
        §E3c.4, §7 decision E3-c).

        Wired from the coding-agent layer (which owns both the emitter and this
        session) — tau-agent-core never imports ``session_store``. Register it via
        ``subscribe_session_events(agent_session.route_session_event)``.

        The seam emitter is synchronous but fires from within the agent loop's
        running event loop (e.g. ``append_compaction`` inside ``compact()``); the
        bus dispatch is async, so the emit is scheduled as a fire-and-forget task
        on the running loop (the ``EventBus`` contract is fire-and-forget). No
        running loop is a misuse of the seam, surfaced loudly by ``get_running_loop``
        (Fail-Early — no swallow, no synchronous fallback).
        """
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._events.emit_channel(str(event["type"]), event))
        self._session_event_tasks.add(task)
        task.add_done_callback(self._session_event_tasks.discard)

    async def emit_session_start(self, reason: str = "startup") -> None:
        """Fire the notify-grade ``session_start`` lifecycle hook (S41).

        Dispatched through the session's :class:`ExtensionRunner` (not the notify
        ``EventBus``) so a handler's exception is SURFACED (S44), not swallowed.
        Called by the frontends *after* extensions are loaded — so a
        ``session_start`` handler can reconstruct state from ``ctx.entries()`` /
        install watchers with its registration already in place. Returns nothing:
        the hook has no path effect. Gated on ``has_handlers`` for the
        zero-extension fast path (no event dict built when nobody listens).

        ``reason`` mirrors pi's ``SessionStartEvent.reason``
        (``"startup" | "reload" | "new" | "resume" | "fork"``); the frontend that
        knows why the session began passes the right one.

        Also claims the loop binding (:meth:`_bind_loop`), UNCONDITIONALLY — before
        the zero-extension fast path below, because the binding is about this
        session, not about who is listening. This is the earliest moment a frontend
        reliably runs the session on its loop, and a ``session_start`` handler is
        exactly where a driver opens the subscription/timer/socket whose callbacks
        will later need :meth:`submit_threadsafe` to have somewhere to marshal to.
        """
        self._bind_loop()
        if not self._extension_runner.has_handlers("session_start"):
            return
        await self._extension_runner.emit_session_start({"type": "session_start", "reason": reason})

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        """Fire the notify-grade ``session_shutdown`` lifecycle hook (S41), and
        drain every still-running forked branch and marshalled submission.

        The teardown counterpart to :meth:`emit_session_start`, dispatched through
        the runner (error-surfaced, not swallowed). The frontends fire it on the
        genuine end-of-runtime moments — TUI quit, headless completion, and
        SIGINT/SIGTERM — so an extension can commit exit state / stop watchers.

        This is also where the supervised fork-task registry closes out
        (docs/SUBMISSION-LIFECYCLE.md "fork" — "must not become an orphan on
        session close", :meth:`_cancel_forked_tasks`): run UNCONDITIONALLY,
        before the ``has_handlers`` fast path below, because a forked branch must
        not survive session teardown regardless of whether any extension
        happens to be listening for it.

        The same closes out :attr:`_threadsafe_tasks` (phase 4): a submission
        marshalled in from a bus thread a millisecond before quit has no awaiting
        caller either, and the reason it must not outlive the session is identical.

        ``reason`` mirrors pi's ``SessionShutdownEvent.reason``
        (``"quit" | "reload" | "new" | "resume" | "fork"``).
        """
        await self._cancel_forked_tasks()
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
    ) -> LoadExtensionsResult:
        """Load file-path extensions into THIS live session (E5 §2, S26/S27).

        Discovers + imports each extension and invokes its ``register(api)``
        against an :class:`ExtensionAPI` bound to this session's live
        :class:`ExtensionRunner` bucket — so the four mutating hooks a file
        extension registers actually FIRE in this session's loop. This is the
        seam the E0–E4 loader left disconnected from any live process (E5 §0):
        ``_load_extensions`` was called only by tests, never against a running
        session's runner.

        Binding reuses :meth:`_bind_extension_api` as the loader's per-extension
        ``api_factory``: each extension gets its OWN bucket appended in load
        order and labelled by its **file path**, sharing this session's registry,
        event bus, and live :class:`ExtensionContext`. An async ``register`` is
        awaited by the loader. This runs once per run, AFTER construction (the
        runner already exists), which is exactly what resolves the load-vs-bind
        ordering (E5 D-E5-7): build the session, then bind file extensions to its
        runner here — rather than needing the runner before the session exists.

        Error policy is the loader's (Fail-Early): an explicit ``-e`` failure
        RAISES (the user named it); a *discovered* failure is collected into the
        returned :class:`LoadExtensionsResult` ``errors`` and skipped.
        ``collect_explicit_errors=True`` demotes an explicit failure to a collected
        error too (the TUI passes this — it can't abort mid-load; headless leaves it
        False to keep the raise). The caller
        surfaces ``errors`` (headless → stderr, TUI → a notice); the loader no
        longer prints them itself, so this is safe to call under a live Textual
        screen.

        ``extensions_config`` (S40) is the per-extension config map
        (``{"<file-stem>": {…}}``) this run resolved from ``~/.tau/config.json`` +
        ``--ext-config`` overrides. It is stored on the session BEFORE binding so
        :meth:`_bind_extension_api` can slice each extension's ``api.config`` by
        file stem. ``None`` leaves the constructor-supplied map (default ``{}``).
        """
        if extensions_config is not None:
            self._extensions_config = extensions_config

        from tau_agent_core.sdk import _load_extensions

        result = await _load_extensions(
            explicit_paths,
            discover=discover,
            user_dir=user_dir,
            api_factory=self._bind_extension_api,
            collect_explicit_errors=collect_explicit_errors,
            bus_available=self._bus_available,
        )
        for loaded in result.extensions:
            self._loaded_extensions[loaded.path] = loaded
            self._disabled_paths.discard(loaded.path)
        self._extension_load_errors = list(result.errors)
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

    def list_managed_extensions(self) -> list[tuple[str, bool]]:
        """Every file extension under management as ``(path, enabled)`` in load order.

        The authoritative runtime state the ``/extensions`` listing reads so a
        disabled extension shows as such: ``enabled`` is ``False`` exactly when the
        path is in ``_disabled_paths`` (its bucket removed from the runner). Pure
        read — no path effect, display-only.
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
            "values": dict(self._extensions_config.get(Path(target).stem, {})),
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
                f"{Path(target).stem} declares no CONFIG_SCHEMA, so there is no "
                "contract to check these values against. Refusing to write a slice "
                "the extension never said it reads."
            )
        from tau_agent_core.extension_types import validate_form_values

        try:
            validate_form_values(schema["fields"], values)
        except ValueError as err:
            raise ValueError(f"{Path(target).stem} config: {err}") from err
        self._extensions_config[Path(target).stem] = dict(values)
        result = await self.reload_extension(target)
        return ExtensionActionResult("configure", result.path, result.ok, result.message)

    def resolve_extension_target(self, token: str) -> str | None:
        """Resolve a user token (full path or file stem) to a managed path.

        The ``/extensions <verb> <token>`` frontend passes what the user typed; the
        listing shows stems (``Path(path).stem``), so ``disable 21_reminders`` must
        map to the loaded path. Matches an exact path first, then a unique stem.
        Returns ``None`` when nothing matches or a stem is ambiguous (the caller
        reports it — no guessing, Fail-Early).
        """
        if token in self._loaded_extensions:
            return token
        matches = [p for p in self._loaded_extensions if Path(p).stem == token]
        if len(matches) == 1:
            return matches[0]
        return None

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

    async def disable_extension(self, path: str) -> ExtensionActionResult:
        """Tear down and detach a loaded extension so its hooks stop firing (S70).

        Fires the extension's own ``session_shutdown`` (reason ``"disable"``) FIRST —
        the S41 teardown seam, so a watcher/exit-commit runs cleanly — then removes its
        runner bucket (hooks stop) and unwinds its registry tools/commands/shortcuts.
        The ``LoadedExtension`` record is KEPT so :meth:`enable_extension` can bring it
        back. A no-op (unknown / already disabled) returns ``ok=False``, not an error.
        """
        target = self.resolve_extension_target(path)
        if target is None:
            return ExtensionActionResult("disable", path, False, f"no loaded extension {path!r}")
        bucket = self._extension_runner.get_extension(target)
        if bucket is None:
            return ExtensionActionResult(
                "disable", target, False, f"{Path(target).stem} is already disabled"
            )
        await self._extension_runner.emit_session_shutdown_for(
            bucket, {"type": "session_shutdown", "reason": "disable"}
        )
        self._extension_runner.remove_extension(target)
        self._unregister_bucket(bucket)
        self._disabled_paths.add(target)
        message = f"disabled {Path(target).stem}"
        if self._release_lock_of(target):
            message += ", releasing its lock on the session"
        return ExtensionActionResult("disable", target, True, message)

    def _release_lock_of(self, path: str) -> bool:
        """Move the cursor off ``path``'s locking request, if that is where it sits.

        docs/EXTENSION-LOCKS.md §6, escape 3. Navigation, not a special case in
        the lock check: the cursor moving IS the release, so this is the same
        release the user gets by branching, reached by a different gesture.
        Returns whether it moved anything — false in the ordinary case, where the
        extension held no lock.
        """
        entries = self._session_log.entries()
        request = request_at_cursor(entries, self._session_log.cursor)
        if request is None or not request.lock or request.extension != path:
            return False
        # The REQUEST's parent, not the cursor's: the cursor may be a provenance node above it.
        parent = next(
            (e.get("parentId") for e in reversed(entries) if str(e.get("id")) == request.entry_id),
            None,
        )
        self._session_log.append_navigate(str(parent) if parent is not None else None)
        return True

    async def enable_extension(self, path: str) -> ExtensionActionResult:
        """Re-bind a disabled extension by re-invoking its ``register`` (S70).

        Binds a FRESH runner bucket (its tools/commands/shortcuts re-enter the
        registry) and re-invokes the stored ``register(api)`` — the same entry point
        the loader called — then fires ``session_start`` (reason ``"enable"``) so a
        watcher re-installs. A no-op (unknown / already enabled) returns ``ok=False``.
        """
        target = self.resolve_extension_target(path)
        if target is None:
            return ExtensionActionResult("enable", path, False, f"no loaded extension {path!r}")
        if self._extension_runner.get_extension(target) is not None:
            return ExtensionActionResult(
                "enable", target, False, f"{Path(target).stem} is already enabled"
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
        )
        self._disabled_paths.discard(target)
        bucket = self._extension_runner.get_extension(target)
        if bucket is not None:
            await self._extension_runner.emit_session_start_for(
                bucket, {"type": "session_start", "reason": "enable"}
            )
        return ExtensionActionResult("enable", target, True, f"enabled {Path(target).stem}")

    async def reload_extension(self, path: str) -> ExtensionActionResult:
        """Tear down, re-import from disk, and re-register an extension (S70).

        Fires ``session_shutdown`` (reason ``"reload"``) for the current instance (if
        enabled), removes its bucket + registry entries, then RE-IMPORTS the file fresh
        (a new module object — code edits on disk take effect) and re-invokes
        ``register`` against a fresh bucket, finally firing ``session_start`` (reason
        ``"reload"``). A broken file RAISES out of here (Fail-Early — the extension is
        left torn down; the frontend surfaces the error), which is why reload does not
        return an ``ok=False`` for an import failure.
        """
        target = self.resolve_extension_target(path)
        if target is None:
            return ExtensionActionResult("reload", path, False, f"no loaded extension {path!r}")
        bucket = self._extension_runner.get_extension(target)
        if bucket is not None:
            await self._extension_runner.emit_session_shutdown_for(
                bucket, {"type": "session_shutdown", "reason": "reload"}
            )
            self._extension_runner.remove_extension(target)
            self._unregister_bucket(bucket)
        from tau_agent_core.sdk import _load_one_extension

        new_loaded = await _load_one_extension(
            Path(target), self._bind_extension_api, bus_available=self._bus_available
        )
        self._loaded_extensions[target] = new_loaded
        self._disabled_paths.discard(target)
        new_bucket = self._extension_runner.get_extension(target)
        if new_bucket is not None:
            await self._extension_runner.emit_session_start_for(
                new_bucket, {"type": "session_start", "reason": "reload"}
            )
        return ExtensionActionResult("reload", target, True, f"reloaded {Path(target).stem}")

    def _turn_cap(self) -> dict[str, Any]:
        """The ``max_turns`` kwarg for this session's loop, or nothing.

        Returns an EMPTY dict when unset rather than a number, so ``AgentLoopConfig``'s
        own default stays the single definition of the default ceiling — copying it here
        would be a second source of truth that silently drifts. That default is now
        ``None``: no ceiling, until a caller states one.
        """
        return {} if self._max_turns is None else {"max_turns": self._max_turns}

    async def _reserve_turn_or_reject(self) -> bool:
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
        if self._turn_lock.locked():
            return False
        await self._turn_lock.acquire()
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

        The observation-grade half of "Task marshalling": called from the points
        where the session demonstrably starts running on a loop
        (:meth:`emit_session_start`, and :meth:`_bind_or_check_loop` below) so that
        :meth:`submit_threadsafe` has somewhere to marshal TO before the first
        submission has been made. That matters for the real driver: ``nats_bus``
        opens its subscription in ``session_start``, and a driver that then calls
        ``submit_threadsafe`` from its own thread must not be told "this session is
        not bound to a loop yet" when the session is plainly running on one.

        Silent when there is no running loop and when a live owner already holds the
        binding — this is not the guard, it is the binding. :meth:`submit` is where a
        conflict is a hard error.
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
        """Attach :attr:`_current_submission`'s provenance to ``event``.

        docs/SUBMISSION-LIFECYCLE.md "Provenance on events" — Jupyter's
        ``parent_header``: a *copy* of the causing submission's identity, carried
        onto every event so a renderer can decide HOW to show a turn without the
        core knowing any renderer exists. ``event.model_copy`` because
        :class:`AgentEvent` is a plain (non-frozen) pydantic model shared with
        whatever emitted it — mutating it in place would be visible to that
        caller too, which is not this method's business.

        Returns ``event`` UNCHANGED when no submission is current (e.g.
        ``continue_conversation()``, which does not set
        :attr:`_current_submission`) — an honest "nothing to attribute this to",
        not four fabricated ``None``s indistinguishable from the real default.
        """
        sub = self._current_submission
        if sub is None:
            return event
        return event.model_copy(
            update={
                "submission_id": sub.submission_id,
                "source": sub.source,
                "submitter": sub.submitter,
                "correlation": dict(sub.correlation),
            }
        )

    async def _emit_stamped(self, event: AgentEvent) -> None:
        """The ``emit=`` callable a submission-driven turn's :class:`AgentLoop` gets.

        Wraps :attr:`_events` (the real bus) with :meth:`_stamp_event` so every
        event this turn produces carries the current submission's provenance —
        the SAME bus, SAME fire-and-forget contract (``EventBus.emit``), nothing
        added except the stamp.
        """
        await self._events.emit(self._stamp_event(event))

    def resolve_command(self, text: str) -> CommandInvocation | None:
        """Would ``text`` dispatch as a command, and which one? (``submit()`` step 3.)

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3; :mod:`tau_agent_core.commands`.

        PURE and side-effect-free — it decides, it does not run. That matters because
        this has two callers and they need the same answer for different reasons:

        - :meth:`submit`, which is the AUTHORITY. It calls this only when
          ``sub.expand_commands`` is ``True``, which is where the security property
          lives — this method itself does not know or care who is asking, so it must
          never be the thing that decides whether dispatch is ALLOWED.
        - a frontend PEEKING before it renders. The TUI must know whether a turn is
          coming before it appends a user bubble to the transcript and its working
          message list; asking after the fact would mean rendering a user turn for a
          ``/tree`` that never becomes one. The old code answered this by owning the
          whole dispatch block; now it asks the same function ``submit()`` asks, so
          the two cannot drift.

        Resolution reads the live extension-command registry, so a command an
        extension registered (or a reload removed) is reflected immediately. See
        :func:`~tau_agent_core.commands.resolve_command` for the ordering rule
        (built-ins win) and for why an unknown ``/…`` falls through to the model.
        """
        return resolve_command(text, self._registry.get_commands().keys())

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
                cursor=self._session_log.cursor,
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
        if self._extension_runner.has_handlers("input"):
            input_result = await self._extension_runner.emit_input(
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

        return text, images, None

    async def submit(
        self,
        sub: Submission,
        *,
        context: list[dict[str, Any]] | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> SubmissionResult:
        """The single admission point every input source funnels through.

        Reference: docs/SUBMISSION-LIFECYCLE.md, "The one door" (phase 1, part 2).
        TUI, headless, RPC, the SDK, and every extension are meant to converge on
        this method (:meth:`prompt` below is now a thin compatibility wrapper over
        it); the ``input`` hook chain moves HERE from the old ``prompt()`` body so
        every submitter gets the same parsing/transform pipeline, not just a human
        typing.

        ``context`` is deliberately NOT a field on :class:`~tau_agent_core.submission.Submission`
        — it is the pre-existing ``prompt(text, images, context)`` capability (the
        TUI passes its own live working message list, ``backends.py`` ``stream_chat``)
        threaded through as a direct keyword so :meth:`prompt` can keep its exact
        signature and return type. No other submission source has ever had a
        "context override" concept; ``Submission.text``/``images`` remain the only
        per-submission content fields the cross-source contract carries.

        ``on_admitted`` (docs/REMOTE-CONTROL.md §4[3], C3) is the RPC surface's
        admission signal, added for `tau_agent_core.rpc.commands`'s ``submit``/
        ``prompt`` handlers and additive for every other caller (default ``None``,
        never invoked, no observable change to any existing call site). C3 needs a
        response at the moment a submission is ADMITTED — long before the turn this
        call may go on to run has finished, which is the only point at which this
        method historically returned anything at all. Fired exactly once, with no
        arguments, from the one place below where every strategy that is actually
        going to run a turn ON THIS CALL has already committed to that (see the
        call site's own comment for why that is a clean, single point rather than
        one per branch): the branches that return their own ``SubmissionResult``
        before reaching it — "reject"'s failure, "steer"'s turn-in-flight delivery,
        "rollback"'s stale-target refusal, "fork"'s admission failure or spawn — are
        each already a complete, fast result the caller can react to synchronously,
        so none of them need a separate signal. Fired AFTER
        :meth:`_apply_input_pipeline` (phase-2 review B2), not before: an ``input``
        hook that consumes the submission, or ``expand_commands`` resolving a slash
        command, is a THIRD way to reach a complete result without a turn ever
        running, and calling ``on_admitted`` ahead of that check made its own
        promise below false for exactly those two cases. The turn's own progress
        after admission is reported the ordinary way, through the ``AgentEvent``
        stream this call stamps with the submission's provenance — ``on_admitted``
        says only "this call is now committed to running a turn", nothing about how
        that turn goes.

        **No-raise contract.** ``on_admitted`` MUST NOT raise. It is called from
        inside this method's own ``try`` (S2), so a raising callback still unwinds
        through the ``finally`` that releases :attr:`_turn_lock` and resets the
        per-turn state — the session is not wedged — but the exception itself then
        propagates straight out of :meth:`submit`, past everything the turn would
        otherwise have done (no ``agent_start``, no persisted user node). A caller
        that cannot guarantee its callback won't raise should catch inside the
        callback itself and log, not rely on :meth:`submit` to recover for it.

        Owns, in order (the spec's numbered list):

        1. **Admission / concurrency** — ``sub.multitask_strategy`` against the
           in-flight-turn lock.

           - ``"reject"`` returns ``accepted=False`` with a reason, never
             blocking and never raising.
           - ``"enqueue"`` waits for the in-flight turn (if any) to finish, then
             runs — never "parks" the way ``send_user_message(deliver_as=
             "nextTurn")`` does; it is guaranteed to run within THIS call.
           - ``"rollback"`` (decision 2): if a turn is genuinely in flight, signals
             its :attr:`_abort_signal` (a REQUEST — the turn still unwinds through
             its own ``finally``, which is what actually persists whatever it
             produced before the signal was checked), then waits for the slot
             exactly like ``"enqueue"``. Once acquired, it navigates the log back
             to the leaf THAT turn recorded at ITS OWN admission
             (:attr:`_pre_turn_leaf`, read BEFORE this call's own admission
             overwrites it) via ``append_navigate`` — the same "move the cursor,
             the abandoned suffix falls off the ``parentId`` walk" mechanism
             ``append_branch_summary`` uses (minus the summary; see
             :meth:`~tau_agent_core.session_log.SessionLog.append_navigate` and
             ``ctx.fork(mode="in_place")``, which is the identical shape). If NO
             turn is in flight there is nothing to discard, so this degrades to a
             plain admission at the current cursor — no navigate, no signal.
             **Known limitation:** ``asyncio.Lock`` is FIFO; a rollback queued
             behind an already-waiting ``"enqueue"`` submission does not jump the
             queue (it is granted the slot only after that submission ALSO
             completes a full turn). Fixing that needs a priority-aware admission
             primitive this work item does not build. Review fix, must_fix #1:
             this used to then navigate to the ORIGINAL aborted turn's pre-turn
             leaf regardless, silently discarding the queued submission's
             completed work from the active path. It now detects that case
             (:attr:`_current_turn_token` bumped by every admitted turn) and
             refuses (``accepted=False``) rather than corrupting the tree — the
             queue-jump gap stands, but it now fails safely instead of silently.
           - ``"fork"`` (decision 2): does **not** touch :attr:`_turn_lock` at
             all — the in-flight turn, if any, is genuinely untouched. The fork
             point is the log's current committed tip
             (:attr:`~tau_agent_core.session_log.SessionLog.cursor`); admission
             checks it is TURN-COMPLETE
             (:meth:`~tau_agent_core.conversation_tree.ConversationTree.fork_admission_reason`)
             and returns ``accepted=False`` with a clear reason rather than
             producing a bad prefix. On success, a second agent is spawned in a
             SUPERVISED background task (:meth:`_spawn_fork`,
             :attr:`_forked_tasks`) — reusing ``ctx.spawn_branch``'s entire
             mechanism (``BranchView``, tool scoping, failure containment,
             ``branch_event`` forwarding) — and ``submit()`` returns
             ``accepted=True`` immediately, before the branch's turn finishes;
             there is no caller left to await it the way ``spawn_branch``'s
             caller does.
           - ``"steer"`` (phase 4): deliver the content into the turn that is
             ALREADY running — after its current tool calls have appended their
             results, before its next LLM call. That point has one owner, the
             agent loop, so this method's whole job is to put the content where
             the loop will find it: :attr:`_pending_steer_messages`, which every
             :class:`~tau_agent_core.agent_loop.AgentLoop` this session builds
             receives as ``steer_queue=`` and drains immediately before each
             provider call. Returns ``accepted=True`` with NO messages — the
             transcript belongs to the submission that owns the turn.

             With NO turn in flight there is nothing to steer, and the "next LLM
             call" is the one this submission is about to make, so it takes the
             slot and runs an ordinary turn (pi's split exactly:
             ``streamingBehavior`` is consulted only ``if (this.isStreaming)``).

             A steer left undelivered because the turn ended first (a
             ``terminate``-ing tool, ``max_turns``) STAYS queued and is delivered
             at the start of the next turn — it is never dropped and never
             re-ordered. It IS dropped by :meth:`abort` and by ``rollback``'s
             abort, which is pi's behaviour (``clearSteeringQueue()``) and the
             only coherent one: the turn it was aimed at no longer exists.

             This method is not the only door onto that queue: a hook running
             INSIDE the turn cannot call ``submit()`` at all (the reentrancy
             guard below), so mid-turn extension steering goes through
             ``ctx.send_user_message(deliver_as="steer")`` →
             :meth:`_queue_message`, which appends to the same list.

           Also records :attr:`_pre_turn_leaf` — the log's cursor immediately
           BEFORE this submission's user node — needed for provenance regardless
           and, per decision 2, what makes ``rollback`` cheap: a LATER
           submission's abort target is exactly this cursor.
        2. **The `input` hook chain** — moved here from ``prompt()``. The event
           now carries ``source``/``submitter`` so a handler can branch on
           provenance (pi's ``InputSource`` equivalent). ``handled`` still
           consumes without a turn.
        3. **Command dispatch**, gated on ``sub.expand_commands`` (B2-b). The
           built-ins ``/compact`` / ``/tree`` / ``/fork`` / ``/extensions`` and
           every extension-registered ``/name args`` used to be intercepted inside
           a Textual event handler (``tau_coding_agent/app.py``
           ``on_input_submitted``), so no other input source had a command
           vocabulary at all. The DECISION now lives in
           :mod:`tau_agent_core.commands` and is taken here, via
           :meth:`resolve_command`, against the POST-``input``-hook text (the
           spec's own step order).

           A resolved command is not a turn: nothing is persisted, no model call
           is made, ``messages`` is empty, and the decision is reported on
           :attr:`~tau_agent_core.submission.SubmissionResult.command`. An
           extension command is RUN here (:meth:`run_extension_command`) because
           any frontend can render the string it returns, and reported as a
           :class:`~tau_agent_core.flows.Performed`; a built-in is STEPPED and
           reported as the arm it is at — a :class:`~tau_agent_core.flows.FlowStep`,
           a :class:`~tau_agent_core.flows.Ready` or a
           :class:`~tau_agent_core.flows.View` — because the core cannot push a
           Textual screen, and a frontend that cannot perform the arm it got must
           raise :class:`~tau_agent_core.commands.UnsupportedCommandError` rather
           than return having done nothing. An unrecognised ``/…`` resolves to
           ``None`` and is sent to the model as ordinary text, unchanged.

           ``expand_commands`` defaults to ``False`` and that is a SECURITY
           property: a bus/timer/webhook payload beginning with ``"/compact"`` is
           literal prompt text, and cannot smuggle a command through. Only a
           submitter that positively declares itself an interactive frontend gets
           dispatch. An extension that wants to compact calls the typed API.
        4. **Session materialisation** — a no-op for THIS class: ``AgentSession``
           requires a ``session_log`` at construction, so a session always
           exists by the time ``submit()`` runs. "Create one if absent" is a
           FRONTEND concern (e.g. the TUI's ``action_new_chat``), not this
           method's.
        5. **Persistence honouring ``store_history``** — ``True`` (the default,
           and what :meth:`prompt` always passes) is byte-for-byte the
           pre-existing persisted pipeline. ``False`` runs the same turn through
           the model but persists nothing (:meth:`_run_one_turn`'s ``persist=``
           parameter) and skips the end-of-prompt drain / ``user_turn_end`` —
           both are about consolidating DURABLE session state (auto-compaction
           acts on the persisted log; a followUp re-entry persists its own turn),
           which a non-persisted submission has no business triggering.

           ``silent`` is NOT honoured here and raises (see below). It folds into
           ``store_history=False`` at construction (``Submission.__post_init__``),
           but that is only half of what it promises: the other half — suppressing
           renderer-visible output — has no coherent implementation at THIS seam.
           The core has one multiplexed event bus and its subscribers are not all
           renderers (``latency.py`` measures off it, ``rpc.py`` captures off it,
           ``spawn_branch`` forwards off it), so declining to emit would blind
           measurement and transport rather than a screen; and marking events
           instead would only work once renderers honour the mark, which is the
           multi-stream renderer contract of **Block 3** (``backends.py``
           ``stream_chat`` is single-stream by construction and filters on nothing
           today). The spec's own Jupyter rule says the same from the other
           direction: a frontend filters to decide HOW to render and "dropping
           other sources' events is how a multi-client session becomes
           incoherent" — deciding that in the core is not this method's business.
           A caller who wants the half that exists asks for it by name:
           ``store_history=False``.
        6. **Run, then return** — the admission step above already resolved
           "run or enqueue" (enqueue is a wait, not a separate code path); this
           step is the bare return of the produced messages. Every ``AgentEvent``
           the turn's :class:`AgentLoop` emits is stamped with this submission's
           ``submission_id``/``source``/``submitter``/``correlation``
           (:meth:`_stamp_event`, "Provenance on events") — Jupyter's
           ``parent_header``: a renderer decides HOW to show a turn from this,
           never whether to drop it.

           The submission's own SPAN is published too, on the ``submission_start``
           / ``submission_end`` bus channels (B3-a; see :meth:`subscribe_channel`).
           An ``AgentEvent`` brackets a TURN, and a followUp re-entry runs a second
           ``loop.run()`` inside one ``submit()``, so ``agent_start``/``agent_end``
           cannot tell a renderer where one user→answer exchange begins and ends.
           These two can, and they are what lets a frontend render a turn it never
           initiated — a bus, timer or extension submission — instead of only the
           one it happens to be awaiting.

        ``allow_user_input`` (Jupyter's ``allow_stdin``) is ENFORCED here, for the
        whole lifetime of the admitted turn: this method publishes it on
        :data:`~tau_agent_core.submission.SUBMISSION_ALLOWS_USER_INPUT`, and
        :class:`~tau_agent_core.extension_types.ExtensionUI`'s blocking dialogs
        (``confirm``/``select``/``input``/``form``) consult it before reaching a TUI
        delegate. ``False`` (the ``Submission`` default, and what every non-human
        source gets) means those dialogs take the headless-answer route even in a
        TUI process — the configured ``--ui-defaults`` answer, or
        :class:`~tau_agent_core.extension_types.HeadlessDialogError`. That is the
        enforcement the spec names ("Enforcement stays HeadlessDialogError"), and
        it is why the capability is per-submission rather than per-process: an
        embedded τ serving a human at a terminal and a cron-triggered submission
        must be able to bar the second from opening a modal on the first. Publication
        is causal, not temporal — see the ContextVar's own docstring.

        Depth (decision 3) is DERIVED here, not merely read off the record: the
        submission's own ``depth`` is a floor, and
        :func:`~tau_agent_core.submission.next_submission_depth` raises it to one
        below the turn this call was originated from, if any
        (:data:`~tau_agent_core.submission.DRIVING_SUBMISSION_DEPTH`, which this
        method publishes for the lifetime of the turn it admits). "Originated
        from" is causal, not temporal — a task spawned by a hook inside a turn
        inherits that turn's depth even if it reaches this method after the turn
        ended, and a task that predates the turn inherits nothing however much
        traffic it delivers mid-turn. That is what makes the cap below reachable:
        a self-continuing extension (``turn_end`` hook → spawn a task →
        ``api.submit`` → its own ``turn_end`` hook → …) climbs one per link and
        raises on the eleventh, instead of looping forever.

        **This method is safe only from the session's own event loop** (phase 4,
        "Task marshalling"). A NATS callback, a timer or a webhook handler that
        fires in a context this session does not own must call
        :meth:`submit_threadsafe` instead; calling this one from there RAISES,
        because the alternative — working by accident, or silently rerouting — is
        the "bus disconnected randomly" bug the spec names. See
        :meth:`_bind_or_check_loop` for exactly what counts as foreign.

        Raises:
            RuntimeError: this call is on a different (live) event loop than the
                one this session is bound to, or on no event loop at all. The
                message names :meth:`submit_threadsafe` as the fix.
            RuntimeError: the derived depth exceeds ``MAX_SUBMISSION_DEPTH``
                (decision 3 — a hard cap that raises, never a silent drop or an
                advisory flag).
            RuntimeError: this call is reentrant — running on the SAME
                ``asyncio.Task`` as the turn currently holding ``_turn_lock``
                (review fix, must_fix #2). A hook (``input``, ``tool_call``,
                ``turn_end``, ``user_turn_end``) that calls back into
                :meth:`submit`/:meth:`prompt`/``ctx.prompt`` before its own
                turn returns can never be admitted — every strategy below
                either waits on or inspects a lock this task already holds,
                so the call would hang forever with no exception and no log
                line. A DIFFERENT task submitting concurrently is not this
                case and is unaffected (see :attr:`_turn_task`).
            NotImplementedError: ``sub.multitask_strategy`` is not one of the
                five names in
                :data:`~tau_agent_core.submission.MultitaskStrategy` — refused
                rather than silently falling through to whichever branch is last.
            UnsupportedCommandError: ``sub.expand_commands`` resolved a command
                that was registered when :meth:`resolve_command` looked but gone
                by the time :meth:`run_extension_command` ran (an extension
                unloaded across the ``await``). Refusing loudly beats falling
                through to the model with text the user meant as a command.
            NotImplementedError: ``sub.silent`` is ``True`` — renderer
                suppression lands in Block 3 (see step 5). ``store_history=False``
                is the part that exists today and is not affected.
        """
        self._bind_or_check_loop("submit()")

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

        if self._turn_task is not None and asyncio.current_task() is self._turn_task:
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
            if not await self._reserve_turn_or_reject():
                return SubmissionResult(
                    accepted=False,
                    submission_id=sub.submission_id,
                    rejection_reason="a turn is already in flight",
                )
        elif sub.multitask_strategy == "enqueue":
            await self._turn_lock.acquire()
        elif sub.multitask_strategy == "steer":
            if not await self._reserve_turn_or_reject():
                steer_depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
                steer_input_token = SUBMISSION_ALLOWS_USER_INPUT.set(sub.allow_user_input)
                try:
                    text, images, early = await self._apply_input_pipeline(sub)
                finally:
                    DRIVING_SUBMISSION_DEPTH.reset(steer_depth_token)
                    SUBMISSION_ALLOWS_USER_INPUT.reset(steer_input_token)
                if early is not None:
                    return early
                self._pending_steer_messages.append(self._queued_content_to_user(text, images))
                return SubmissionResult(accepted=True, submission_id=sub.submission_id, messages=[])
        elif sub.multitask_strategy == "rollback":
            was_in_flight = self._turn_lock.locked()
            rollback_target = self._pre_turn_leaf
            aborted_token = self._current_turn_token
            if was_in_flight:
                self._abort_signal.abort()
                self._pending_steer_messages.clear()
            await self._turn_lock.acquire()
            if was_in_flight:
                if rollback_target is None or self._current_turn_token != aborted_token:
                    self._turn_lock.release()
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
                self._session_log.append_navigate(rollback_target)
        elif sub.multitask_strategy == "fork":
            fork_point = self._session_log.cursor
            reason = ConversationTree(
                self._session_log.entries(), fork_point
            ).fork_admission_reason(fork_point)
            if reason is not None:
                return SubmissionResult(
                    accepted=False, submission_id=sub.submission_id, rejection_reason=reason
                )
            fork_depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
            try:
                self._spawn_fork(sub, fork_point)
            finally:
                DRIVING_SUBMISSION_DEPTH.reset(fork_depth_token)
            return SubmissionResult(accepted=True, submission_id=sub.submission_id, messages=[])
        else:
            raise NotImplementedError(
                f"multitask_strategy={sub.multitask_strategy!r} is not a known "
                "strategy. The five docs/SUBMISSION-LIFECYCLE.md names are "
                "'reject', 'enqueue', 'steer', 'rollback' and 'fork'."
            )

        depth_token = DRIVING_SUBMISSION_DEPTH.set(sub.depth)
        user_input_token = SUBMISSION_ALLOWS_USER_INPUT.set(sub.allow_user_input)
        try:
            self._current_submission = sub
            self._turn_task = asyncio.current_task()

            self._turn_token_counter += 1
            self._current_turn_token = self._turn_token_counter
            self._pre_turn_leaf = self._session_log.cursor

            self._is_streaming = True
            self._abort_signal = AbortSignal()
            self._extension_api.context._signal = self._abort_signal

            original_text = sub.text
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

            if self._compaction_policy is not None:
                self._policy_turns_used += 1
                self._compaction_policy.admit_turn(self._policy_turns_used)

            next_turn = self._pending_next_turn_messages
            self._pending_next_turn_messages = []
            queued = [self._queued_content_to_user(c) for c in next_turn]

            side_usage_before = self.side_usage
            await self._events.emit_channel(
                "submission_start", submission=sub, text=text, images=images
            )
            try:
                turn_messages = await self._run_one_turn(
                    text,
                    images,
                    context,
                    queued=queued,
                    strip_ref_text=original_text,
                    persist=sub.store_history,
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
            self._is_streaming = False
            self._current_submission = None
            self._turn_task = None
            DRIVING_SUBMISSION_DEPTH.reset(depth_token)
            SUBMISSION_ALLOWS_USER_INPUT.reset(user_input_token)
            self._turn_lock.release()

    def _spawn_fork(self, sub: Submission, fork_point: str | None) -> None:
        """Schedule a ``multitask_strategy="fork"`` submission as a supervised task.

        docs/SUBMISSION-LIFECYCLE.md "fork" / NODE-ADDRESSABLE-AGENTS.md §5: the
        cost of ``fork`` is not tree work (``ctx.spawn_branch`` already does all
        of it — ``BranchView``, tool scoping, failure containment, forwarding the
        branch's own events onto the ``branch_event`` channel) — it is lifecycle.
        ``spawn_branch`` is designed to be awaited by its caller; a fork
        submission has none, so this wraps the SAME coroutine in an
        ``asyncio.Task`` and tracks it in :attr:`_forked_tasks`, keyed by
        ``sub.submission_id`` (chosen up front — the branch's own ``lane`` does
        not exist until ``open_branch`` runs INSIDE the coroutine, which is too
        late to key a registry meant to reference the task before it finishes).

        The forked session runs with THIS session's own tools (a second full
        agent, not a scoped-down evaluator — spawn_branch's allowlist exists to
        protect against inheriting tools BY ACCIDENT; a fork explicitly asks to
        continue the same job) and turn cap. It does NOT inherit this session's
        extensions (`spawn_branch` passes none — NODE-ADDRESSABLE-AGENTS.md
        decision 4), abort signal (it gets its OWN, fresh, inside the new
        ``AgentSession``), or usage ledger (``_last_usage``/``_side_usage`` stay
        per-session) — see the work item's report for whether that is a gap or a
        documented choice.

        "Own tools" means everything a turn of THIS session is built from,
        extension registrations included: a continuation missing half the
        vocabulary cannot perform the job it was forked to continue. That is not
        in tension with the line above — the extensions themselves do not carry,
        so no hook, command or subscription runs in the fork; an extension's
        TOOL does, still bound to the parent's ``ctx``, which is what an
        extension tool closes over wherever it is called from.

        Errors are surfaced, never silently swallowed (Fail-Early): ``spawn_branch``
        itself contains a failing branch as a ``BranchResult(ok=False, ...)``, so
        the task should not normally raise — if it somehow does anyway (a bug,
        not a modelled failure), :meth:`_on_fork_task_done` routes it through the
        same :meth:`_surface_extension_error` sink a raising hook uses, rather
        than letting ``asyncio``'s "Task exception was never retrieved" warning
        be the only trace of it.
        """
        submission_id = sub.submission_id
        task = asyncio.get_running_loop().create_task(
            self._extension_api.context.spawn_branch(
                fork_point,
                sub.text,
                tools=[t.name for t in self._build_turn_tools()],
                max_turns=self._max_turns,
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
            UnsupportedCommandError: ``text`` resolves to a command. See above —
                this method has nowhere to put the outcome, so it refuses before
                anything runs instead of swallowing it.
        """
        invocation = self.resolve_command(text)
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
        text: str,
        images: list[dict] | None,
        context: list[dict] | None,
        queued: list[UserMessage] | None = None,
        strip_ref_text: str | None = None,
        persist: bool = True,
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
        writes NONE of it to ``self._session_log`` — the model sees and answers
        the turn; the durable tree never does.

        Returns THIS turn's new messages only — the user message, any ``queued``
        messages, then the assistant/tool messages the loop produced.
        """
        queued = queued or []

        # Create UserMessage for tau-llm
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
            context_ends_with_user = _ends_with_user_text(context_messages, strip_ref)
        else:
            context_messages = self.messages
            context_ends_with_user = False

        if context_ends_with_user:
            context_messages = context_messages[:-1]

        turn_system_prompt = self._system_prompt
        pre_user_messages: list[dict[str, Any]] = []
        post_user_messages: list[dict[str, Any]] = []
        if self._extension_runner.has_handlers("before_agent_start"):
            before = await self._extension_runner.emit_before_agent_start(
                prompt=text,
                images=images,
                system_prompt=self._system_prompt,
            )
            if before is not None:
                if before.get("system_prompt") is not None:
                    turn_system_prompt = before["system_prompt"]
                for msg in before.get("messages") or []:
                    node = self._custom_message_node(msg)
                    if msg.get("position") == MESSAGE_POSITION_BEFORE_USER:
                        pre_user_messages.append(node)
                    else:
                        post_user_messages.append(node)

        # Build the agent loop config
        config = AgentLoopConfig(
            system_prompt=turn_system_prompt,
            temperature=self._model.temperature,
            api_key=self._api_key,
            reasoning=self._reasoning,
            tool_execution_mode=self._tool_execution_mode,
            **self._turn_cap(),
        )

        loop = AgentLoop(
            config=config,
            emit=self._emit_stamped,
            tools=self._build_turn_tools(),
            model=self._model,
            abort_signal=self._abort_signal,
            hook_dispatcher=self._extension_runner,
            steer_queue=self._pending_steer_messages,
        )

        turn_messages: list[dict[str, Any]] = []

        try:
            final_messages = await loop.run(
                prompts=[*pre_user_messages, user_msg, *queued, *post_user_messages],
                context=context_messages,
            )
        except BaseException as exc:
            self._persist_turn_inputs(
                pre_user_messages, user_msg, queued, post_user_messages, turn_messages, persist
            )
            self._persist_loop_messages(completed_messages(exc), turn_messages, persist=persist)
            raise

        self._persist_turn_inputs(
            pre_user_messages, user_msg, queued, post_user_messages, turn_messages, persist
        )

        self._persist_loop_messages(final_messages, turn_messages, persist=persist)

        return turn_messages

    def _persist_turn_inputs(
        self,
        pre_user_messages: list[dict[str, Any]],
        user_msg: UserMessage,
        queued: list[UserMessage],
        post_user_messages: list[dict[str, Any]],
        turn_messages: list[dict[str, Any]],
        persist: bool,
    ) -> None:
        """Append this turn's INPUT messages to the log and to *turn_messages*.

        Everything the model was given before it answered: the ``before_user``
        injections, the user's own message, the queued ``nextTurn`` messages, and
        the ``after_user`` injections — in the order the model saw them.

        Split out of :meth:`_run_one_turn` so the success path and the failure
        path write the same things. It was inline, and therefore only on the
        success path, which is why an aborted turn lost the user's prompt as well
        as the assistant's reply.
        """
        for pre_msg in pre_user_messages:
            if persist:
                self._session_log.append_custom_message(
                    pre_msg, custom_type=str(pre_msg["customType"])
                )
            turn_messages.append(pre_msg)

        user_dict = user_msg.model_dump()
        if persist:
            self._session_log.append_message(user_dict)
        turn_messages.append(user_dict)

        for qmsg in queued:
            qdict = qmsg.model_dump()
            if persist:
                self._session_log.append_message(qdict)
            turn_messages.append(qdict)

        for cmsg in post_user_messages:
            if persist:
                self._session_log.append_custom_message(cmsg, custom_type=str(cmsg["customType"]))
            turn_messages.append(cmsg)

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
           are appended to ``turn_messages`` and itself drains at its tail.
        """
        await self._maybe_auto_compact()
        await self._drain_deferred_ops()

        while self._pending_follow_up_messages:
            follow_up = self._pending_follow_up_messages.pop(0)
            follow_up_messages = await self._run_one_turn(follow_up, None, None)
            turn_messages.extend(follow_up_messages)
            await self._maybe_auto_compact()
            await self._drain_deferred_ops()

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
        if not self._extension_runner.has_handlers("user_turn_end"):
            return
        loop_turns = sum(1 for m in turn_messages if m.get("role") == "assistant")
        injected = await self._extension_runner.emit_user_turn_end(
            loop_turns=loop_turns,
            messages=list(turn_messages),
        )
        for raw in injected:
            node = self._custom_message_node(raw, hook="user_turn_end")
            self._session_log.append_custom_message(node, custom_type=str(node["customType"]))
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
        if not await self._reserve_turn_or_reject():
            raise RuntimeError(
                "continue_conversation(): a turn is already in flight on this "
                "session. This is one of the two admission-guarded doors "
                "(docs/SUBMISSION-LIFECYCLE.md) — the other is submit()'s "
                "multitask_strategy='reject', which returns "
                "SubmissionResult(accepted=False, ...) instead of raising, "
                "because it has a result channel to carry the refusal through "
                "and this method does not."
            )

        self._is_streaming = True
        self._abort_signal = AbortSignal()
        self._extension_api.context._signal = self._abort_signal
        self._turn_task = asyncio.current_task()
        self._turn_token_counter += 1
        self._current_turn_token = self._turn_token_counter
        self._pre_turn_leaf = self._session_log.cursor

        try:
            # Get existing messages from session for context
            context_messages = self.messages

            # Build the agent loop config
            config = AgentLoopConfig(
                system_prompt=self._system_prompt,
                temperature=self._model.temperature,
                api_key=self._api_key,
                reasoning=self._reasoning,
                tool_execution_mode=self._tool_execution_mode,
                **self._turn_cap(),
            )

            # Create and run the agent loop (continuation mode)
            loop = AgentLoop(
                config=config,
                emit=self._events.emit,
                tools=self._build_turn_tools(),
                model=self._model,
                abort_signal=self._abort_signal,
                hook_dispatcher=self._extension_runner,
                steer_queue=self._pending_steer_messages,
            )

            # Run the loop — handles LLM call, tool execution, re-tries
            final_messages = await loop.run_continue(
                context=context_messages,
            )

            turn_messages: list[dict[str, Any]] = []
            self._persist_loop_messages(final_messages, turn_messages)

            return turn_messages

        finally:
            self._is_streaming = False
            self._turn_task = None
            self._turn_lock.release()

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
            return await self._perform_compaction(custom_instructions=custom_instructions)
        finally:
            await self._events.emit(AgentEvent(type="agent_end", timestamp=self._timestamp()))

    async def compact_messages(
        self, messages: list[dict[str, Any]], custom_instructions: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Compact a caller-supplied message list and return the shortened list.

        This is the **manual** compaction path (the TUI's ``/compact``): it
        summarizes everything before the most recent user turn and keeps that
        turn intact, returning a new list shaped ``[<system messages>, <summary
        as a user message>, <most recent user turn onward>]``. It is for callers
        whose own store — not the session manager — is the authoritative context
        they send to the model (the TUI's ``current_chat.messages`` is exactly
        this).

        The cut is **count-based** (keep the last user turn), deliberately unlike
        auto-compaction's token-budget cut: a manual compaction should visibly do
        something on a normal-sized chat, not no-op until the conversation
        exceeds ``keep_recent_tokens``.

        Returns None when there is nothing older to compact (zero or one user
        turn), so the caller can no-op rather than grow the list with an empty
        summary.

        Raises:
            CompactionError: if summary generation fails. Fail-Early.
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
            tokens_before=estimate_context_tokens(convo).tokens,
            file_ops=file_ops,
            settings=self._compaction_settings,
            previous_summary=None,
            compacted_entry_ids=[str(i) for i in range(last_user_idx)],
        )
        summarizer_model, summarizer_api_key = self._summarizer()
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
        self, custom_instructions: str | None = None
    ) -> CompactionResult | None:
        """Compaction core shared by manual ``compact`` and the auto-trigger.

        Emits no lifecycle events of its own; the callers bracket it.
        """
        path_entries = ConversationTree(
            self._session_log.entries(), self._session_log.cursor
        ).context_entries()
        if not any(e.get("type") in ("message", "customMessage") for e in path_entries):
            return None
        preparation = prepare_compaction(path_entries, self._compaction_settings)
        if preparation is None:
            return None

        summarizer_model, summarizer_api_key = self._summarizer()
        result = await run_compaction(
            preparation,
            summarizer_model,
            summarizer_api_key,
            custom_instructions=custom_instructions,
            thinking_level=self._reasoning,
        )
        self.record_side_usage(result.usage)
        covered = _covered_span(path_entries, result.first_kept_entry_id)
        self._session_log.append_compaction(
            summary=result.summary,
            first_kept_id=result.first_kept_entry_id,
            tokens_before=result.tokens_before,
            summarizer_model_id=summarizer_model.id,
            summary_usage=result.usage,
            covered_entries=len(covered),
            covered_tokens=estimate_span_tokens(covered),
            agent_spec_id=agent_spec_in_force(
                self._session_log.entries(), self._session_log.cursor
            ),
        )
        return result

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
        context_window = getattr(self._model, "context_window", 0) or 0

        if self._compaction_policy is not None:
            self._compaction_policy.observe_context(
                turns_used=self._policy_turns_used,
                context_tokens=estimate_context_tokens(self.messages).tokens,
                context_window=context_window,
            )

        if not settings.enabled:
            return
        if context_window <= settings.reserve_tokens:
            return

        messages = self.messages
        estimate = estimate_context_tokens(messages)
        if not should_compact(estimate.tokens, context_window, settings):
            return

        await self._events.emit(AgentEvent(type="agent_start", timestamp=self._timestamp()))
        try:
            await self._perform_compaction()
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
            self._pending_follow_up_messages.append(content)
        elif deliver_as == "nextTurn":
            self._pending_next_turn_messages.append(content)
        elif deliver_as == "steer":
            self._pending_steer_messages.append(self._queued_content_to_user(content))
        else:
            raise ValueError(
                "_queue_message: deliver_as must be 'followUp', 'nextTurn' or "
                f"'steer', got {deliver_as!r}"
            )

    def _defer_compact(self, custom_instructions: str | None = None) -> None:
        """Record a deferred compaction intent (drained at prompt()'s tail, S20)."""
        self._deferred_ops.append({"kind": "compact", "custom_instructions": custom_instructions})

    def _defer_fork(self, entry_id: str | None = None, mode: str = "in_place") -> None:
        """Record a deferred fork intent (drained at prompt()'s tail, S20)."""
        self._deferred_ops.append({"kind": "fork", "entry_id": entry_id, "mode": mode})

    async def _drain_deferred_ops(self) -> None:
        """Apply the recorded deferred compact/fork intents exactly once.

        Snapshots and clears the ledger first, then dispatches each intent, so an
        op recorded WHILE draining waits for the next drain rather than looping
        here — "applies exactly once at end-of-prompt" (decision 3). Delegates to
        the immediate paths (``compact`` / ``ctx.fork``); Fail-Early on an unknown
        kind rather than silently dropping it.
        """
        if not self._deferred_ops:
            return
        ops = self._deferred_ops
        self._deferred_ops = []
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

    def abort(self) -> None:
        """Abort the current agent turn, and every still-running forked branch.

        A ``multitask_strategy="fork"`` submission's second agent is a REAL
        ``AgentSession``, but not one an ``abort()`` caller has a handle to — it
        was constructed and awaited from inside :meth:`_spawn_fork`'s background
        task. Cancelling that ``asyncio.Task`` is this session's own "abort path"
        reaching it (docs/SUBMISSION-LIFECYCLE.md "fork" — "must be cancellable
        via the session's abort path"): the forked branch's own ``AgentSession``
        does not need — and does not get — a direct ``abort()`` call, because
        cancelling its driving task raises ``CancelledError`` into whichever
        ``await`` it is suspended on, unwinding it the same way any cancelled
        coroutine unwinds.

        :attr:`_threadsafe_tasks` is deliberately NOT cancelled here, and the
        asymmetry is the point. A fork is a second agent that ``abort()`` has no
        other handle on; a marshalled submission is an ORDINARY turn on this
        session — if it is the one in flight, the signal above already aborts it,
        by the same mechanism as any other turn. Cancelling the rest would discard
        submissions that have not been admitted yet, i.e. input this abort was
        never about, arriving from a source the aborting user cannot see. They are
        drained at session shutdown instead (:meth:`emit_session_shutdown`).
        """
        self._is_streaming = False
        self._abort_signal.abort()
        self._pending_steer_messages.clear()
        for task in self._forked_tasks.values():
            task.cancel()

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

    def _persist_loop_messages(
        self,
        final_messages: list[Any],
        turn_messages: list[dict[str, Any]],
        persist: bool = True,
    ) -> None:
        """Persist a loop's produced messages, routing durable ``custom`` nodes.

        Assistant / tool-result messages persist as plain ``message`` tree nodes.
        An extension-injected ``role: "custom"`` node produced mid-loop by the
        mutating ``turn_end`` hook (S43) persists as a ``customMessage`` tree node
        instead — so it lands on the active path exactly like a ``before_agent_start``
        injection (persisted == rendered == sent) and a reload replays the same path.
        Each persisted dict is collected into ``turn_messages`` (this turn's new
        messages — the ``prompt()`` return value), preserving loop order.

        ``persist=False`` (:meth:`submit`'s ``store_history=False`` path) still
        builds and collects ``turn_messages`` exactly as before but skips both
        ``append_*`` calls — the transcript is returned to the caller, never
        written to the log.

        Fail-Early: a ``custom`` node missing ``customType`` raises rather than
        fabricating an extension-origin identity — checked regardless of
        ``persist``, since a malformed node is a construction bug either way.
        """
        for msg in final_messages:
            if hasattr(msg, "model_dump"):
                msg_dict = msg.model_dump()
            elif isinstance(msg, dict):
                msg_dict = msg
            else:
                continue

            if msg_dict.get("role") == CUSTOM_ROLE:
                custom_type = msg_dict.get("customType")
                if not custom_type:
                    raise ValueError(
                        "turn_end custom node is missing 'customType' — the "
                        "extension-origin type is required (Fail-Early)"
                    )
                if persist:
                    self._session_log.append_custom_message(msg_dict, custom_type=str(custom_type))
            else:
                if persist:
                    self._session_log.append_message(msg_dict)
            turn_messages.append(msg_dict)

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

    def _append_custom_message(
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
        entry_id = self._session_log.append_custom_message(
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

    def _append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
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
        return self._session_log.append_custom_entry(custom_type, data)

    @property
    def pending_request(self) -> ExtensionRequest | None:
        """The extension request the cursor points at, or ``None`` (EXTENSION-LOCKS §2).

        The cursor only — never a walk. Every head reads this to decide what to
        draw, and :meth:`submit` reads it to decide whether to refuse, so the
        thing a user is looking at and the thing that refused them are one entry.
        """
        return request_at_cursor(self._session_log.entries(), self._session_log.cursor)

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
        request = find_request(self._session_log.entries(), request_id)
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
        self._append_custom_entry(
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
        if not ext_tools:
            return self._tools
        by_name: dict[str, AgentTool] = {t.name: t for t in self._tools}
        for t in ext_tools:
            by_name[t.name] = t
        return list(by_name.values())

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
        the abort signal / live session onto; ``None`` lets ``ExtensionAPI`` make a
        fresh context (used only for the session-shared api built at construction).
        ``config`` is this extension's per-extension config slice (S40); ``None``
        for the session-shared api (which no extension file receives).

        Returns:
            An ExtensionAPI bound to this session, its event bus, and registry.
        """
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
        (stderr, unchanged). Passing ``None`` clears it.
        """
        self._extension_api.context.set_record_sink(sink)

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Set the headless dialog-answer policy for this session (E7 §3 / S48).

        Threads the resolved ``--ui-defaults`` / config ``"ui_defaults"`` map onto
        the session's ONE shared :class:`ExtensionUI` (via the context), so a
        headless dialog opened by any loaded extension auto-answers only for the
        methods the user explicitly opted into — every other headless dialog
        raises :class:`HeadlessDialogError` (Fail-Early, D-E6-2). This is run-scoped
        runtime config: it is NOT persisted onto the session tree (the policy is
        re-sourced each run, like ``--ext-config``). The TUI path never calls this —
        it sets a live delegate instead, so a human answers.

        Raises:
            ValueError: an unknown method or answer token (propagated from
                :meth:`ExtensionUI.set_headless_defaults`); the frontend renders it
                as a clean CLI error.
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
        """List extension-registered slash commands (E5 §5 / S35).

        Returns ``(name, description)`` for every command an extension registered
        via ``api.register_command`` — the palette (:meth:`TauApp.get_system_commands`)
        reads this to LIST them. Description falls back to the empty string when a
        command omitted one (listing is best-effort chrome, not a durable node).
        """
        return [
            (name, str(command.get("description", "")))
            for name, command in self._registry.get_commands().items()
        ]

    def get_extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        """List extension-registered key shortcuts (E10 §6 / S69).

        Returns ``(key, command, args, description)`` for every shortcut an extension
        registered via ``api.register_shortcut`` — the TUI's ``ctrl+e`` chord menu and
        the command palette read this to LIST + dispatch them. ``key`` is the chord
        tail (bound under the ``ctrl+e`` leader); ``command``/``args`` are dispatched
        through the SAME :meth:`run_extension_command` path as a typed ``/name args``.
        ``description`` falls back to the target command's registered description, then
        to the empty string (listing is best-effort chrome, not a durable node).
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

        A command may declare ``"args": "<placeholder>"`` in its ``register_command``
        definition to signal that it expects a free-form argument string (parity with
        typing ``/name args``). The palette (:meth:`TauApp.get_system_commands`) reads
        this to decide whether a palette entry, which has no argument line, must first
        open the S47 input modal to collect the arg string before dispatch.

        Returns the placeholder string when declared, ``None`` when the command is
        unknown or declares no ``args``. Fail-Early: a non-string ``args`` is a
        construction bug (the field IS the placeholder text), so it RAISES rather than
        being coerced or silently ignored.
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

        Port of pi's ``_tryExecuteExtensionCommand`` (agent-session.ts:1143). Looks
        up ``name`` in the session registry and, if found, invokes its ``handler``
        with ``(args, ctx)`` where ``ctx`` is the session's ONE live
        :class:`ExtensionContext` (the same object hook handlers and ``api.ui``
        reach through, so a command's ``ctx.ui.notify`` paints in the same TUI).

        Returns an :class:`ExtensionCommandResult`: ``handled`` is ``True`` iff the
        command existed and ran (``False`` for an unknown command so the caller can
        fall through and treat the text as a prompt), and ``output`` carries the
        value the handler RETURNED (E7 §3 / S46 — previously discarded, G7). The
        frontends render ``output`` as display-only chrome; it is never appended to
        the active path (the tree-as-truth invariant is untouched).

        Fail-Early: a command registered without a callable ``handler`` cannot run,
        so an attempt to invoke one RAISES rather than silently no-op'ing — a
        registered-but-inert command is a construction bug, not a runnable command.
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

        The per-extension config slice (S40) is selected here by the extension's
        **file stem** — ``Path(path_label).stem`` — from ``self._extensions_config``
        (``~/.tau/config.json`` ``"extensions"`` + ``--ext-config`` overrides), so
        ``~/.tau/extensions/24_budget.py`` reads the ``"24_budget"`` entry. An
        unconfigured extension gets ``{}`` (never fabricated). Inline factory
        extensions carry a ``module:qualname`` label rather than a file path, so
        their stem simply won't match a config key unless one is named for it.
        """
        bucket = self._extension_runner.register_extension(path_label)
        config = self._extensions_config.get(Path(path_label).stem, {})
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
