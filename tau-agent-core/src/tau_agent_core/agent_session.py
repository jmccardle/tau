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
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from tau_ai.abort import AbortSignal
from tau_ai.types import Model, UserMessage

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.messages import create_custom_message
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import ExtensionRunner
from tau_agent_core.session import SessionState
from tau_agent_core.session_log import SessionLog
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionPreparation,
    CompactionResult,
    CompactionSettings,
    compact as run_compaction,
    estimate_context_tokens,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.compaction_utils import create_file_ops, extract_file_ops_from_message
from tau_agent_core.tools.base import AgentTool, ToolDefinition

if TYPE_CHECKING:
    from tau_agent_core.sdk import LoadExtensionsResult


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
    """

    def __init__(
        self,
        session_log: SessionLog,
        model: Model,
        system_prompt: str = "",
        tools: list | None = None,
        extensions: list[Callable] | None = None,
        api_key: str | None = None,
        reasoning: str | None = None,
        compaction_settings: CompactionSettings | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._session_log = session_log
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._events = EventBus()
        # Session-owned registry for extension-registered tools/commands/flags.
        # Bound into the one ExtensionAPI below; read by the loop in a later step.
        self._registry = ExtensionRegistry()
        self._extensions = extensions or []
        # Per-extension config map (E6 §2 / S40): ``{"<file-stem>": {…}}``, sourced
        # from ``~/.tau/config.json`` ``"extensions"`` + per-run ``--ext-config``
        # overrides. ``_bind_extension_api`` slices the right entry by file stem and
        # hands it to each extension's ``api.config``. Set BEFORE the inline-factory
        # bind loop below so constructor-passed extensions see their slice too.
        # NOT persisted onto the session tree — it is run-scoped runtime config,
        # re-sourced each run (deliberately excluded from the tree-as-truth path).
        self._extensions_config: dict[str, dict[str, Any]] = extensions_config or {}
        self._is_streaming = False
        self._abort_signal = AbortSignal()
        # Forwarded to the agent loop -> provider. Kept off the Model so it is
        # never written to the on-disk session JSON. None means "rely on the
        # env/provider default".
        self._api_key = api_key
        # Requested thinking level ("off".."xhigh") forwarded to the loop ->
        # provider as the `reasoning` option. None = don't request reasoning.
        self._reasoning = reasoning
        # Compaction thresholds; drives both manual compact() and the automatic
        # post-turn check in prompt(). Defaults to the harness defaults.
        self._compaction_settings = compaction_settings or DEFAULT_COMPACTION_SETTINGS

        # Injection queues + deferred-op ledger (S20 / decision 3 + 5). A tool
        # running mid-turn cannot mutate the conversation under the live loop, so
        # requests are RECORDED here and DRAINED at the tail of prompt() — the
        # same site as _maybe_auto_compact(), never per-inner-turn:
        #   _deferred_ops           — deferred compact/fork intents (applied once)
        #   _pending_follow_up_messages — followUp: re-enter the loop THIS prompt()
        #   _pending_next_turn_messages — nextTurn: injected on the NEXT prompt()
        self._deferred_ops: list[dict[str, Any]] = []
        self._pending_follow_up_messages: list[str] = []
        self._pending_next_turn_messages: list[str] = []

        # Seam-3 bridge (S21 / §E3c.4): strong refs to the fire-and-forget tasks
        # that route session-lifecycle events onto the extension bus. Held so the
        # loop keeps them alive until they complete (an un-referenced create_task
        # may be GC'd mid-flight); each task discards itself on done.
        self._session_event_tasks: set[asyncio.Task[None]] = set()

        # The session-shared ExtensionAPI: bound to this session's real event bus
        # + registry + live ExtensionContext. Kept for internal consumers (the ctx
        # the deferred-op drain and tool wrapper reach through). It has NO hook
        # bucket — the per-extension apis below are the surface factories receive.
        self._extension_api = self._make_extension_api()
        # The return-collecting hook dispatcher (E2). One per session, bound to
        # the live ExtensionContext so the mutating-hook handlers receive the
        # real ctx. Injected into every AgentLoop this session builds
        # (`hook_dispatcher=`) so the four hook call-sites (S11-S14) can reach
        # it; empty until extensions register mutating hooks, so has_handlers()
        # gives every call-site the zero-extension fast path.
        self._extension_runner = ExtensionRunner(context=self._extension_api.context)
        # Register each extension against its OWN api, bound to its OWN runner
        # bucket (load order preserved) but SHARING the session registry, event
        # bus, and live context. This is the S24 bridge: api.on("tool_call"/…)
        # now lands in a per-extension ExtensionHandlers bucket the runner
        # dispatches, instead of silently no-op'ing on the notify bus.
        for ext in self._extensions:
            ext(self._bind_extension_api(_extension_factory_label(ext)))

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """
        if not self._extension_runner.has_handlers("session_start"):
            return
        await self._extension_runner.emit_session_start({"type": "session_start", "reason": reason})

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        """Fire the notify-grade ``session_shutdown`` lifecycle hook (S41).

        The teardown counterpart to :meth:`emit_session_start`, dispatched through
        the runner (error-surfaced, not swallowed). The frontends fire it on the
        genuine end-of-runtime moments — TUI quit, headless completion, and
        SIGINT/SIGTERM — so an extension can commit exit state / stop watchers.
        Returns nothing; gated on ``has_handlers`` for the zero-extension fast path.

        ``reason`` mirrors pi's ``SessionShutdownEvent.reason``
        (``"quit" | "reload" | "new" | "resume" | "fork"``).
        """
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
        returned :class:`LoadExtensionsResult` ``errors`` and skipped. The caller
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

        # Lazy import: sdk imports agent_session at module load, so a top-level
        # import here would be circular.
        from tau_agent_core.sdk import _load_extensions

        return await _load_extensions(
            explicit_paths,
            discover=discover,
            user_dir=user_dir,
            api_factory=self._bind_extension_api,
        )

    async def prompt(
        self,
        text: str,
        images: list[dict] | None = None,
        context: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Send a prompt and run the agent loop.

        Delegates to AgentLoop which:
        1. Creates a UserMessage with the text (and optional images)
        2. Builds the full context from session messages (or provided context)
        3. Streams the LLM response via stream_simple() -> provider
        4. Emits streaming events through the event bus
        5. Executes any tool calls from the response
        6. Saves the results back to the session

        Args:
            text: The prompt text to send.
            images: Optional list of image dicts for multimodal prompts.
            context: Optional list of message dicts to use as conversation
                     context instead of session messages. This allows
                     passing a pre-built message history (e.g. from a
                     loaded chat) to the agent loop.

        Returns:
            List of messages produced by the agent loop.
        """
        self._is_streaming = True
        self._abort_signal = AbortSignal()
        # Bind the fresh per-prompt abort signal onto the live ExtensionContext so
        # a hook's ``ctx.abort()`` (e.g. the budget guard, example 24 / step S17)
        # aborts the signal THIS loop actually polls. pi's ctx reads the live agent
        # signal (agent-session.ts:2254-2261); the signal is recreated each
        # prompt(), so rebind here — a signal captured once at construction is
        # stale by the next turn.
        self._extension_api.context._signal = self._abort_signal

        try:
            # Drain any pending "nextTurn" messages into THIS prompt's first turn
            # (S20): a message queued last prompt with ``deliver_as="nextTurn"`` is
            # injected alongside the user turn, exactly as pi pushes
            # ``_pendingNextTurnMessages`` after the user message (agent-session.ts:
            # 1096-1099). Snapshot-and-clear so the injection happens exactly once
            # and does NOT recur on the followUp re-entry below.
            next_turn = self._pending_next_turn_messages
            self._pending_next_turn_messages = []
            queued = [self._queued_content_to_user(c) for c in next_turn]

            turn_messages = await self._run_one_turn(text, images, context, queued=queued)

            # End-of-prompt drain (S20 / decision 3): auto-compaction, then the
            # deferred compact/fork intents (applied exactly ONCE here, never
            # mid-turn — no loop reentrancy), then followUp messages re-enter the
            # loop WITHIN this same prompt() call. All three share this single
            # tail site (the _maybe_auto_compact() site).
            await self._end_of_prompt_drain(turn_messages)

            return turn_messages

        finally:
            self._is_streaming = False

    async def _run_one_turn(
        self,
        text: str,
        images: list[dict] | None,
        context: list[dict] | None,
        queued: list[UserMessage] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one agent-loop turn: build the user message, run the loop, persist.

        Extracted from ``prompt()`` so the end-of-prompt followUp drain (S20) can
        re-enter it within the same ``prompt()`` call. ``queued`` are pending
        ``nextTurn`` messages threaded (and persisted) after the user turn on the
        first turn of a prompt; empty on a followUp re-entry.

        Returns THIS turn's new messages only — the user message, any ``queued``
        messages, then the assistant/tool messages the loop produced.
        """
        queued = queued or []

        # Create UserMessage for tau-ai
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if images:
            content.extend(images)
        # content holds raw block dicts; model_validate lets pydantic coerce
        # them into the TextContent | ImageContent union UserMessage declares.
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
            # Did the caller already include this user turn as the final
            # context message? The TUI passes the full history (which ends
            # with the latest user turn); a bare prompt("hi") does not. This
            # flag also drives the persist/return logic below.
            context_ends_with_user = _ends_with_user_text(context_messages, text)
        else:
            context_messages = self.messages
            context_ends_with_user = False

        # Thread the user message to the loop exactly once — via
        # prompts=[user_msg] passed to loop.run() below. The context must
        # therefore NOT also carry a trailing copy, so drop the duplicate the
        # caller supplied. (pi parity: runAgentLoop concatenates context +
        # prompts with no dedup, agent-loop.ts:103-106; the old loop-level
        # strip-compare dedup is removed.)
        if context_ends_with_user:
            context_messages = context_messages[:-1]

        # Fire the before_agent_start hook just before the loop runs (E2,
        # step S13; pi agent-session.ts:1101-1125). Two return channels:
        #   - system_prompt CHAINS (last handler wins; each handler sees the
        #     running value, threaded inside the dispatcher) and replaces the
        #     base prompt for THIS turn only — the config is rebuilt every
        #     prompt(), so next turn resets to the base (pi resets to
        #     _baseSystemPrompt when no handler modifies it);
        #   - message(s) ACCUMULATE across handlers and are injected as custom
        #     messages after the user turn (pi pushes role:"custom" messages;
        #     on the wire they read as user messages — messages.ts custom→user).
        #     They are DURABLE (E5 §3.1 / S29): threaded to the loop this turn AND
        #     persisted as ``customMessage`` tree nodes below, so a reload replays
        #     the exact path the model saw (no second history / reload fork).
        # Gated on has_handlers for the zero-extension fast path.
        turn_system_prompt = self._system_prompt
        custom_messages: list[dict[str, Any]] = []
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
                    custom_messages.append(self._custom_message_node(msg))

        # Build the agent loop config
        config = AgentLoopConfig(
            system_prompt=turn_system_prompt,
            temperature=getattr(self._model, "temperature", 0.7),
            api_key=self._api_key,
            reasoning=self._reasoning,
        )

        # Create and run the agent loop
        loop = AgentLoop(
            config=config,
            emit=self._events.emit,
            tools=self._build_turn_tools(),
            model=self._model,
            abort_signal=self._abort_signal,
            hook_dispatcher=self._extension_runner,
        )

        # Run the loop — handles LLM call, tool execution, re-tries. The pending
        # nextTurn messages and then the accumulated before_agent_start custom
        # messages follow the user turn (pi order: [user, ...nextTurn, ...custom]);
        # the loop concatenates context + prompts.
        final_messages = await loop.run(
            prompts=[user_msg, *queued, *custom_messages],
            context=context_messages,
        )

        # Persist this turn's messages AND collect them to return. The
        # return value is THIS turn's new messages only — the user message
        # (when it wasn't already supplied in the context) plus the
        # assistant/tool messages the loop produced — NOT the full
        # accumulated session history.
        #
        # Returning the whole history here was a compounding bug: the TUI
        # appends prompt()'s return to its own message store (which already
        # holds every prior turn), so each turn re-appended all earlier
        # assistant/tool messages. The model then saw earlier exchanges
        # duplicated and got confused about what it had already done.
        turn_messages: list[dict[str, Any]] = []

        # Persist this turn's user message. AgentSession is the AUTHORITATIVE
        # persister (E3-ctx / D3): on the live path it appends through the TUI's
        # own file ``Session`` (bound via ``session_log``), so the user turn is
        # recorded HERE and nowhere else — the TUI dropped its own
        # ``append_message`` to resolve the double-write. ``context_ends_with_user``
        # still governs the loop-threading STRIP above (so the user turn is fed to
        # the loop exactly once), but NOT persistence: the caller echoing the turn
        # into the context it passed does not mean the log already holds it. The
        # message is new to the log exactly once this turn, so append it
        # unconditionally (a bare ``prompt("hi")`` with no context lands here too).
        user_dict = user_msg.model_dump()
        self._session_log.append_message(user_dict)
        turn_messages.append(user_dict)

        # Persist the injected nextTurn messages too — they are genuine queued
        # user content that joins the conversation.
        for qmsg in queued:
            qdict = qmsg.model_dump()
            self._session_log.append_message(qdict)
            turn_messages.append(qdict)

        # Persist the before_agent_start injected messages as durable
        # ``customMessage`` tree nodes (E5 §3.1 / S29). They reached the model as
        # prompts THIS turn (threaded above, in the same [user, ...nextTurn,
        # ...custom] order pi uses); recording them as extension-origin nodes here
        # — in that same order, AFTER the user/queued turns and BEFORE the
        # assistant response — closes the reload fork (agent_session.py:419-421):
        # the next load rebuilds the exact path the model saw, with each node's
        # ``role: "custom"`` rendered as extension-injected and serialized
        # custom→user on the wire. Each carries the node into the returned
        # transcript too (so it is visible, not a hidden channel).
        for cmsg in custom_messages:
            self._session_log.append_custom_message(cmsg, custom_type=str(cmsg["customType"]))
            turn_messages.append(cmsg)

        # Assistant responses and tool results produced this turn.
        for msg in final_messages:
            if hasattr(msg, "model_dump"):
                msg_dict = msg.model_dump()
            elif isinstance(msg, dict):
                msg_dict = msg
            else:
                continue

            self._session_log.append_message(msg_dict)
            turn_messages.append(msg_dict)

        return turn_messages

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

    async def continue_conversation(self) -> list[dict[str, Any]]:
        """Run another agent turn without adding new messages.

        Delegates to AgentLoop.run_continue() which streams the LLM response
        via stream_simple() and handles tool calls.

        Returns:
            List of messages produced by the agent loop.
        """
        self._is_streaming = True
        self._abort_signal = AbortSignal()
        # Rebind the fresh abort signal onto the live ExtensionContext (see
        # prompt(); pi agent-session.ts:2254-2261) so a hook's ctx.abort() reaches
        # the signal this continuation polls.
        self._extension_api.context._signal = self._abort_signal

        try:
            # Get existing messages from session for context
            context_messages = self.messages

            # Build the agent loop config
            config = AgentLoopConfig(
                system_prompt=self._system_prompt,
                temperature=getattr(self._model, "temperature", 0.7),
                api_key=self._api_key,
                reasoning=self._reasoning,
            )

            # Create and run the agent loop (continuation mode)
            loop = AgentLoop(
                config=config,
                emit=self._events.emit,
                tools=self._build_turn_tools(),
                model=self._model,
                abort_signal=self._abort_signal,
                hook_dispatcher=self._extension_runner,
            )

            # Run the loop — handles LLM call, tool execution, re-tries
            final_messages = await loop.run_continue(
                context=context_messages,
            )

            # Save all new messages (assistant responses, tool results) and
            # collect them to return. Like prompt(), the return value is only
            # the messages produced THIS continuation — not the accumulated
            # session history — so a caller appending the result to its own
            # store doesn't re-append prior turns.
            turn_messages: list[dict[str, Any]] = []
            for msg in final_messages:
                if hasattr(msg, "model_dump"):
                    msg_dict = msg.model_dump()
                elif isinstance(msg, dict):
                    msg_dict = msg
                else:
                    continue

                self._session_log.append_message(msg_dict)
                turn_messages.append(msg_dict)

            return turn_messages

        finally:
            self._is_streaming = False

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

        # Keep the most recent user turn (the last user message and everything
        # after it); summarize everything before it.
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
        result = await run_compaction(
            preparation,
            self._model,
            self._api_key,
            custom_instructions=custom_instructions,
            thinking_level=self._reasoning,
        )

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
        preparation = prepare_compaction(path_entries, self._compaction_settings)
        if preparation is None:
            return None

        result = await run_compaction(
            preparation,
            self._model,
            self._api_key,
            custom_instructions=custom_instructions,
            thinking_level=self._reasoning,
        )
        # Append-only boundary through the same log the caller persists through.
        # The System-B compaction entry records ``tokensBefore`` (not the retired
        # manager's tokens_saved/compacted_entry_ids); the read-time splice needs
        # only the summary + firstKeptId (§2.3).
        self._session_log.append_compaction(
            summary=result.summary,
            first_kept_id=result.first_kept_entry_id,
            tokens_before=result.tokens_before,
        )
        return result

    async def _maybe_auto_compact(self) -> None:
        """Compact automatically when context approaches the model's window.

        Mirrors pi's harness, which checks ``shouldCompact`` after each turn.
        Skipped unless compaction is enabled and the window is larger than the
        reserve (a window smaller than the reserve makes the threshold
        meaningless — e.g. tiny test models — so we never auto-compact there).
        """
        settings = self._compaction_settings
        if not settings.enabled:
            return
        context_window = getattr(self._model, "context_window", 0) or 0
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

    # ------------------------------------------------------------------
    # Injection queue + deferred ops (S20 / decision 3 + 5)
    # ------------------------------------------------------------------

    def _queue_message(self, content: str, deliver_as: str = "followUp") -> None:
        """Queue a user message for injection (the seam ``send_user_message`` calls).

        ``deliver_as`` selects WHEN the queued content re-enters the conversation
        (the API validates the same set before reaching here; this method is the
        session-side seam and validates too — Fail-Early, no silent misroute):

        - ``"followUp"``: drains at the end of the CURRENT ``prompt()`` and
          re-enters the agent loop within that same call.
        - ``"nextTurn"``: queued for the NEXT ``prompt()``, injected alongside its
          user turn.

        The delivery mode stays a plain string so a future ``steer`` mode can be
        added additively (decision 5).
        """
        if deliver_as == "followUp":
            self._pending_follow_up_messages.append(content)
        elif deliver_as == "nextTurn":
            self._pending_next_turn_messages.append(content)
        else:
            raise ValueError(
                f"_queue_message: deliver_as must be 'followUp' or 'nextTurn', got {deliver_as!r}"
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

    def _queued_content_to_user(self, content: str) -> UserMessage:
        """Wrap queued ``send_user_message`` content as a ``UserMessage`` text turn."""
        return UserMessage.model_validate(
            {
                "role": "user",
                "content": [{"type": "text", "text": content}],
                "timestamp": self._timestamp(),
            }
        )

    def abort(self) -> None:
        """Abort the current agent turn."""
        self._is_streaming = False
        self._abort_signal.abort()

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

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
            ext_execute = defn["execute"]

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
                        name=name,
                        label=defn.get("label", name),
                        description=defn["description"],
                        parameters=defn["parameters"],
                        execute=_make_adapter(),
                        prompt_snippet=defn.get("prompt_snippet"),
                        prompt_guidelines=defn.get("prompt_guidelines"),
                        execution_mode=defn.get("execution_mode", "parallel"),
                    )
                )
            )
        return resolved

    def _custom_message_node(self, message: dict[str, Any]) -> dict[str, Any]:
        """Build the durable ``custom`` message dict for a ``before_agent_start`` hook.

        Handlers return ``{customType, content, display?, details?}`` (pi
        ``BeforeAgentStartEventResult.message``). This is turned into an
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
            raise ValueError("before_agent_start message is missing 'content' — nothing to inject")
        if "customType" not in message:
            raise ValueError(
                "before_agent_start message is missing 'customType' — the extension-origin "
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
        return self._session_log.append_custom_message(node, custom_type=str(message["customType"]))

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

    def _build_turn_tools(self) -> list:
        """Merge the built-in tools with the active extension tools for a turn.

        Extension tools override a built-in of the same name (pi parity:
        ``_refreshToolRegistry`` sets extension tools last, agent-session.ts:2320).
        Returns ``self._tools`` unchanged when no extension tools are registered.
        """
        ext_tools = self._resolve_extension_tools()
        if not ext_tools:
            return self._tools
        by_name: dict[str, Any] = {t.name: t for t in self._tools}
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

    def get_extension_commands(self) -> list[tuple[str, str]]:
        """List extension-registered slash commands (E5 §5 / S35).

        Returns ``(name, description)`` for every command an extension registered
        via ``api.register_command`` — the palette (:meth:`Parley.get_system_commands`)
        reads this to LIST them. Description falls back to the empty string when a
        command omitted one (listing is best-effort chrome, not a durable node).
        """
        return [
            (name, str(command.get("description", "")))
            for name, command in self._registry.get_commands().items()
        ]

    async def run_extension_command(self, name: str, args: str = "") -> bool:
        """Run an extension-registered slash command (E5 §5 / S35).

        Port of pi's ``_tryExecuteExtensionCommand`` (agent-session.ts:1143). Looks
        up ``name`` in the session registry and, if found, invokes its ``handler``
        with ``(args, ctx)`` where ``ctx`` is the session's ONE live
        :class:`ExtensionContext` (the same object hook handlers and ``api.ui``
        reach through, so a command's ``ctx.ui.notify`` paints in the same TUI).
        Returns ``True`` iff the command existed and ran; ``False`` for an unknown
        command so the caller can fall through (e.g. treat the text as a prompt).

        Fail-Early: a command registered without a callable ``handler`` cannot run,
        so an attempt to invoke one RAISES rather than silently no-op'ing — a
        registered-but-inert command is a construction bug, not a runnable command.
        """
        command = self._registry.get_command(name)
        if command is None:
            return False
        handler = command.get("handler")
        if not callable(handler):
            raise RuntimeError(
                f"extension command {name!r} has no callable 'handler'; it was "
                "registered but cannot run (register_command requires a handler)."
            )
        result = handler(args, self._extension_api.context)
        if inspect.isawaitable(result):
            await result
        return True

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
