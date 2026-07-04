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

import inspect
from typing import Any, Callable

from tau_ai.abort import AbortSignal
from tau_ai.types import Model, UserMessage

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI
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

        # Register extensions against a SINGLE ExtensionAPI bound to this
        # session's real event bus + registry, so handlers subscribe to the
        # live loop bus and registered tools land in the session-owned registry.
        self._extension_api = self._make_extension_api()
        # The return-collecting hook dispatcher (E2). One per session, bound to
        # the live ExtensionContext so the mutating-hook handlers receive the
        # real ctx. Injected into every AgentLoop this session builds
        # (`hook_dispatcher=`) so the four hook call-sites (S11-S14) can reach
        # it; empty until extensions register mutating hooks, so has_handlers()
        # gives every call-site the zero-extension fast path.
        self._extension_runner = ExtensionRunner(context=self._extension_api.context)
        for ext in self._extensions:
            ext(self._extension_api)

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
            # Gated on has_handlers for the zero-extension fast path.
            turn_system_prompt = self._system_prompt
            custom_messages: list[UserMessage] = []
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
                        custom_messages.append(self._custom_message_to_user(msg))

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

            # Run the loop — handles LLM call, tool execution, re-tries. The
            # accumulated custom messages follow the user turn (pi order:
            # [user, ...custom]); the loop concatenates context + prompts.
            final_messages = await loop.run(
                prompts=[user_msg, *custom_messages],
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

            # The user message is new to the conversation only when the caller
            # didn't already include it in the provided context (the TUI does;
            # a bare prompt("hi") does not).
            if not context_ends_with_user:
                user_dict = user_msg.model_dump()
                self._session_log.append_message(user_dict)
                turn_messages.append(user_dict)

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

            # Auto-compaction: now that this turn is persisted, compact in place
            # if the conversation is approaching the model's context window, so
            # the NEXT turn starts within budget (pi checks shouldCompact after
            # each turn). A failure here propagates — Fail-Early, no silent skip —
            # but this turn's messages are already saved and returned-to-be.
            await self._maybe_auto_compact()

            return turn_messages

        finally:
            self._is_streaming = False

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

    def _custom_message_to_user(self, message: dict[str, Any]) -> UserMessage:
        """Convert a ``before_agent_start`` custom message into a ``UserMessage``.

        Handlers return ``{customType, content, display, details}`` (pi
        ``BeforeAgentStartEventResult.message``); on the wire pi maps a custom
        message to a ``user`` message carrying its content (messages.ts custom→user:
        a string ``content`` becomes a single text block, a block list passes
        through). τ mirrors that mapping so the injected message reaches the model.

        Raises:
            ValueError: if the message has no ``content`` — a handler that returns
                a ``message`` must say what to inject (Fail-Early, no empty block).
        """
        if "content" not in message:
            raise ValueError("before_agent_start message is missing 'content' — nothing to inject")
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        return UserMessage.model_validate(
            {
                "role": "user",
                "content": content,
                "timestamp": self._timestamp(),
            }
        )

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

    def _make_extension_api(self) -> ExtensionAPI:
        """Create an ExtensionAPI bound to this session's real refs.

        Binds the live loop event bus (``self._events``) and the session-owned
        registry so ``api.on(event, handler)`` subscribes to the same bus the
        agent loop emits on, and registered tools/commands land where the
        session can read them (E1.1 / step S3).

        Returns:
            An ExtensionAPI bound to this session, its event bus, and registry.
        """
        return ExtensionAPI(
            session=self,
            event_bus=self._events,
            registry=self._registry,
        )

    @staticmethod
    def _timestamp() -> int:
        """Get current timestamp in milliseconds."""
        import time

        return int(time.time() * 1000)
