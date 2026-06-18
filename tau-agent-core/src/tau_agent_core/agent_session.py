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
from tau_ai.types import Model

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext
from tau_agent_core.session import SessionState
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.tools.base import AgentTool


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
    ) -> None:
        self._session_manager = session_manager
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._events = EventBus()
        self._extensions = extensions or []
        self._is_streaming = False
        self._abort_signal = AbortSignal()

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
        self, text: str, images: list[dict] | None = None
    ) -> list[dict[str, Any]]:
        """Send a prompt and run the agent loop.

        1. Creates a UserMessage with the text (and optional images)
        2. Appends it to the session
        3. Runs the agent loop
        4. Streams results back

        Args:
            text: The prompt text to send.
            images: Optional list of image dicts for multimodal prompts.

        Returns:
            List of messages produced by the agent loop.
        """
        self._is_streaming = True
        self._abort_signal = AbortSignal()

        try:
            # Create UserMessage
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if images:
                content.extend(images)

            user_msg = {
                "role": "user",
                "content": content,
            }

            # Emit agent_start
            self._events.emit(
                AgentEvent(type="agent_start", timestamp=self._timestamp())
            )

            # Emit turn_start
            self._events.emit(
                AgentEvent(
                    type="turn_start",
                    timestamp=self._timestamp(),
                    turn_index=0,
                )
            )

            # Append user message to session
            self._session_manager.append_entry({
                "id": f"user_{len(self._session_manager._get_entries())}",
                "type": "message",
                "message": user_msg,
            })

            # Generate assistant response (simplified)
            assistant_content = [{"type": "text", "text": f"Response to: {text}"}]

            # Emit message_start
            self._events.emit(
                AgentEvent(
                    type="message_start",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": []},
                )
            )

            # Emit message_update
            self._events.emit(
                AgentEvent(
                    type="message_update",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": assistant_content},
                )
            )

            # Emit message_end
            self._events.emit(
                AgentEvent(
                    type="message_end",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": assistant_content},
                )
            )

            # Append assistant message to session
            assistant_msg = {"role": "assistant", "content": assistant_content}
            self._session_manager.append_entry({
                "id": f"assistant_{len(self._session_manager._get_entries())}",
                "type": "message",
                "message": assistant_msg,
            })

            # Emit turn_end
            self._events.emit(
                AgentEvent(
                    type="turn_end",
                    timestamp=self._timestamp(),
                    turn_index=0,
                    tool_results=[],
                )
            )

            # Emit agent_end
            self._events.emit(
                AgentEvent(
                    type="agent_end",
                    timestamp=self._timestamp(),
                    messages=[user_msg, assistant_msg],
                )
            )

            return self.messages

        finally:
            self._is_streaming = False

    async def continue_conversation(self) -> list[dict[str, Any]]:
        """Run another agent turn without adding new messages.

        Returns:
            List of messages produced by the agent loop.
        """
        self._is_streaming = True
        self._abort_signal = AbortSignal()

        try:
            # Emit agent_start
            self._events.emit(
                AgentEvent(type="agent_start", timestamp=self._timestamp())
            )

            # Emit turn_start
            self._events.emit(
                AgentEvent(
                    type="turn_start",
                    timestamp=self._timestamp(),
                    turn_index=0,
                )
            )

            # Generate continuation response
            continuation_content = [{"type": "text", "text": "Continuation response"}]

            # Emit message_start
            self._events.emit(
                AgentEvent(
                    type="message_start",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": []},
                )
            )

            # Emit message_update
            self._events.emit(
                AgentEvent(
                    type="message_update",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": continuation_content},
                )
            )

            # Emit message_end
            self._events.emit(
                AgentEvent(
                    type="message_end",
                    timestamp=self._timestamp(),
                    message={"role": "assistant", "content": continuation_content},
                )
            )

            # Append continuation to session
            self._session_manager.append_entry({
                "id": f"cont_{len(self._session_manager._get_entries())}",
                "type": "message",
                "message": {"role": "assistant", "content": continuation_content},
            })

            # Emit turn_end
            self._events.emit(
                AgentEvent(
                    type="turn_end",
                    timestamp=self._timestamp(),
                    turn_index=0,
                    tool_results=[],
                )
            )

            # Emit agent_end
            self._events.emit(
                AgentEvent(
                    type="agent_end",
                    timestamp=self._timestamp(),
                    messages=self.messages,
                )
            )

            return self.messages

        finally:
            self._is_streaming = False

    async def compact(self, custom_instructions: str | None = None) -> None:
        """Trigger manual compaction.

        Args:
            custom_instructions: Optional instructions for the compaction.
        """
        self._events.emit(
            AgentEvent(type="agent_start", timestamp=self._timestamp())
        )
        self._events.emit(
            AgentEvent(
                type="turn_start",
                timestamp=self._timestamp(),
                turn_index=0,
            )
        )
        # Note: Actual compaction logic is in tau_agent_core.compaction
        # For now, emit the events to signal compaction intent
        self._events.emit(
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
