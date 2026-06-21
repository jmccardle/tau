"""τ-agent-core agent_session: AgentSession public API and SDK entry point.

This module implements:
- AgentSession: High-level session API combining agent loop, session manager, and events.
- create_agent_session(): SDK factory function for creating fully configured sessions.
- ExtensionAPI: Public API exposed to extension modules.

Reference: PHASE-2-SUBPHASE-4.md — Agent Session and SDK Entry Point.
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.
Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from tau_ai.abort import AbortSignal
from tau_ai.types import Model, UserMessage

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext
from tau_agent_core.session import SessionState
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.tools.base import AgentTool
from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig


def _message_text(content: Any) -> str:
    """Join the text blocks of a message ``content`` (a str, or a list of blocks).

    Non-text blocks (images, etc.) are ignored — this is a text-only view used
    for comparing whether two user turns are "the same" prompt.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
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
    """High-level session API. Combines agent loop, session manager, and events.

    This is the primary entry point for both SDK and TUI usage.

    Attributes:
        _session_manager: SessionManager for persistence.
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
        session_manager: SessionManager,
        model: Model,
        system_prompt: str = "",
        tools: list | None = None,
        extensions: list[Callable] | None = None,
        api_key: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._events = EventBus()
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

        # Register extensions
        for ext in self._extensions:
            ext(self._make_extension_api())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Current conversation messages (active path)."""
        return self._session_manager.get_active_messages()

    @property
    def state(self) -> SessionState:
        """Read-only access to session state."""
        return SessionState(
            session_id=self._session_manager._active_session_path or "",
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
        self, text: str, images: list[dict] | None = None,
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

        try:
            # Create UserMessage for tau-ai
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if images:
                content.extend(images)
            user_msg = UserMessage(
                role="user",
                content=content,
                timestamp=self._timestamp(),
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
                context_messages = self._session_manager.get_active_messages()
                context_ends_with_user = False

            # Thread the user message to the loop exactly once — via
            # prompts=[user_msg] passed to loop.run() below. The context must
            # therefore NOT also carry a trailing copy, so drop the duplicate the
            # caller supplied. (pi parity: runAgentLoop concatenates context +
            # prompts with no dedup, agent-loop.ts:103-106; the old loop-level
            # strip-compare dedup is removed.)
            if context_ends_with_user:
                context_messages = context_messages[:-1]

            # Build the agent loop config
            config = AgentLoopConfig(
                system_prompt=self._system_prompt,
                temperature=getattr(self._model, "temperature", 0.7),
                api_key=self._api_key,
                reasoning=self._reasoning,
            )

            # Create and run the agent loop
            loop = AgentLoop(
                config=config,
                emit=self._events.emit,
                tools=self._tools,
                model=self._model,
                abort_signal=self._abort_signal,
            )

            # Run the loop — handles LLM call, tool execution, re-tries
            final_messages = await loop.run(
                prompts=[user_msg],
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
                self._session_manager.append_entry({
                    "id": f"msg_{len(self._session_manager._get_entries())}",
                    "type": "message",
                    "message": user_dict,
                })
                turn_messages.append(user_dict)

            # Assistant responses and tool results produced this turn.
            for msg in final_messages:
                if hasattr(msg, "model_dump"):
                    msg_dict = msg.model_dump()
                elif isinstance(msg, dict):
                    msg_dict = msg
                else:
                    continue

                msg_type = msg_dict.get("type", "message")
                self._session_manager.append_entry({
                    "id": f"msg_{len(self._session_manager._get_entries())}",
                    "type": msg_type,
                    "message": msg_dict,
                })
                turn_messages.append(msg_dict)

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

        try:
            # Get existing messages from session for context
            context_messages = self._session_manager.get_active_messages()

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
                tools=self._tools,
                model=self._model,
                abort_signal=self._abort_signal,
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

                msg_type = msg_dict.get("type", "message")
                self._session_manager.append_entry({
                    "id": f"cont_{len(self._session_manager._get_entries())}",
                    "type": msg_type,
                    "message": msg_dict,
                })
                turn_messages.append(msg_dict)

            return turn_messages

        finally:
            self._is_streaming = False

    async def compact(self, custom_instructions: str | None = None) -> None:
        """Trigger manual compaction.

        Args:
            custom_instructions: Optional instructions for the compaction.
        """
        await self._events.emit(
            AgentEvent(type="agent_start", timestamp=self._timestamp())
        )
        await self._events.emit(
            AgentEvent(
                type="turn_start",
                timestamp=self._timestamp(),
                turn_index=0,
            )
        )
        # Note: Actual compaction logic is in tau_agent_core.compaction
        # For now, emit the events to signal compaction intent
        await self._events.emit(
            AgentEvent(type="agent_end", timestamp=self._timestamp())
        )

    def abort(self) -> None:
        """Abort the current agent turn."""
        self._is_streaming = False
        self._abort_signal.abort()

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _make_extension_api(self) -> ExtensionAPI:
        """Create an ExtensionAPI bound to this session.

        Returns:
            ExtensionAPI instance with session-bound methods.
        """
        api = ExtensionAPI()
        return api

    @staticmethod
    def _timestamp() -> int:
        """Get current timestamp in milliseconds."""
        import time
        return int(time.time() * 1000)
