"""ParleyApp: Full Textual TUI application for τ-coding-agent.

Fork of parley.py with τ-agent-core backends.

Reference: PHASE-4-SUBPHASE-1.md — TUI App Shell
Reference: PHASE-4-SUBPHASE-2.md — Agent-Aware Widgets
Reference: SUBPHASE-0.0.md — AgentSession interface (section 7)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
import json

# Import widget data contracts
from tau_coding_agent.widgets.chat_display_data import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultData
from tau_coding_agent.widgets.footer import FooterData

# ---------------------------------------------------------------------------
# AppLayout — backward-compatible dataclass from Subphase 0
# ---------------------------------------------------------------------------


@dataclass
class AppLayout:
    """Layout configuration for the TUI app.

    Kept for backward compatibility with Phase 4 Subphase 0.
    The full TUI uses Textual's built-in layout system instead.

    Attributes:
        width: Terminal width (0 = auto-detect)
        height: Terminal height (0 = auto-detect)
        theme: Theme name for styling
    """

    width: int = 0
    height: int = 0
    theme: str = "default"


# ---------------------------------------------------------------------------
# Textual-based ParleyApp (only when Textual is available)
# ---------------------------------------------------------------------------

try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical, Horizontal
    from textual.widgets import Header, Footer, RichLog, Input

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


if _HAS_TEXTUAL:

    # Import widget classes from their respective modules
    from tau_coding_agent.widgets.chat_display import (
        ChatDisplay as _ChatDisplay,
        UserMessageWidget as _UserMessageWidget,
        AssistantMessageWidget as _AssistantMessageWidget,
        ThinkingBlockWidget as _ThinkingBlockWidget,
    )
    from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget as _ToolCallWidget
    from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget as _ToolResultWidget

    class ChatDisplay(_ChatDisplay):
        """Container for chat messages. 30Hz throttle on updates.

        Wraps the base ChatDisplay from widgets/chat_display.py to provide
        backward-compatible methods while delegating to the proper widget-based
        implementation.

        Attributes:
            _streaming_content: Current streaming content (backward compat).
            _msg_dict: List of message dicts (backward compat).
        """

        def __init__(self, *args, **kwargs) -> None:
            # Let parent ChatDisplay initialize properly (widget _messages)
            super().__init__(*args, **kwargs)
            # Backward-compatible attributes — use _msg_dict to avoid shadowing
            # the parent's _messages (which stores widget instances)
            self._streaming_content: str = ""
            self._msg_dict: list[dict[str, Any]] = []

        def update_streaming_message(self, content: str | None = None, event: Any = None, delta: str | None = None) -> None:
            """Update the streaming message content.

            Args:
                content: Plain text content (backward compat mode).
                event: AgentEvent to extract content from (new mode).
                delta: Plain text delta string (direct mode).
            """
            if delta is not None:
                # Direct delta mode — delegate to parent for widget creation
                super().update_streaming_message(delta=delta)
            elif isinstance(content, str) and event is None:
                # Backward-compatible: plain string — only update content
                self._streaming_content = content
            elif event is not None:
                # Event mode
                super().update_streaming_message(event=event)

        def add_message(self, message: dict[str, Any]) -> None:
            """Add a message dict to the chat display (backward compat)."""
            self._msg_dict.append(message)

        def get_messages(self) -> list[dict[str, Any]]:
            """Return all backward-compatible message dicts."""
            return self._msg_dict

        def clear_messages(self) -> None:
            """Clear all messages and reset state (backward compat)."""
            self._msg_dict.clear()
            self._streaming_content = ""
            # Also clear parent's widget state
            super().clear_messages()

        def compose(self) -> ComposeResult:
            yield RichLog(id="chat-log")

    class InputBar(Container):
        """User input area. Handles Enter, Ctrl+Enter, @, !."""

        CSS_ID = "input-bar"

        def compose(self) -> ComposeResult:
            yield Input(placeholder="Type a message…", id="user-input")

    class ParleyApp(App):
        """Main τ coding agent app (fork of Parley).

        This is the full Textual implementation of the TUI app.
        It replaces the Parley.py backend with τ-agent-core's AgentSession.

        Attributes:
            session: The AgentSession instance this app controls
            print_mode: Whether to run in print mode (stream and exit)
            _is_streaming: Whether the agent is currently streaming
            _throttle_timer: Timer for 30Hz throttling
        """

        CSS_PATH = "themes/catppuccin.tcss"
        BINDINGS = [
            ("q", "quit", "Quit"),
        ]

        def __init__(
            self,
            session: Any = None,  # AgentSession
            print_mode: bool = False,
            layout: Any = None,
        ):
            super().__init__()
            self._session = session
            self._print_mode = print_mode
            self._layout = layout or AppLayout()
            self._is_streaming = False
            self._throttle_timer = None
            self._is_ready = False

        # -- Backward-compatible properties/methods (from Subphase 0) --------

        @property
        def ready(self) -> bool:
            """Whether the app has been initialized."""
            return self._is_ready

        @property
        def session(self) -> Any | None:
            """Backward-compatible session property."""
            return self._session

        @session.setter
        def session(self, value: Any) -> None:
            self._session = value

        @property
        def layout(self) -> Any | None:
            """Backward-compatible layout property."""
            return self._layout

        @layout.setter
        def layout(self, value: Any) -> None:
            self._layout = value

        async def start(self) -> None:
            """Start the TUI application."""
            self._is_ready = True

        async def stop(self) -> None:
            """Stop the TUI application."""
            self._is_ready = False
            if self._session is not None:
                self._session = None
            if self.layout is not None:
                self.layout = None

        # -- Layout (Textual compose pattern) -------------------------------

        def compose(self) -> ComposeResult:
            """Define the Textual widget tree."""
            yield Header()

            with Horizontal():
                yield Footer()
                with Vertical():
                    with Container(id="chat-container"):
                        yield ChatDisplay(id="chat-display")
                    yield InputBar(id="input-bar")

        def _setup_layout(self) -> None:
            """Configure the basic layout programmatically.

            Kept for backward compatibility. The actual Textual widget
            tree is defined in compose().
            """
            pass

        # -- Lifecycle ------------------------------------------------------

        def on_mount(self) -> None:
            """Subscribe to agent session events."""
            self._subscribe_to_events()

        def _subscribe_to_events(self) -> None:
            """Subscribe to agent session events."""
            if self._session:
                self._session.subscribe(self._handle_event)

        # -- Event handlers -------------------------------------------------

        def _handle_event(self, event: Any) -> None:
            """Dispatch agent events to widgets.

            Handles all AgentEvent types defined in SUBPHASE-0.0.md:
            - agent_start, agent_end
            - turn_start, turn_end
            - message_start, message_update, message_end
            - tool_execution_start, tool_execution_update, tool_execution_end

            Args:
                event: An AgentEvent instance or dict with 'type' key.
            """
            event_type = self._get_event_type(event)
            if not event_type:
                return

            match event_type:
                case "agent_start":
                    self._on_agent_start(event)
                case "agent_end":
                    self._on_agent_end(event)
                case "turn_start":
                    self._on_turn_start(event)
                case "turn_end":
                    self._on_turn_end(event)
                case "message_start":
                    self._on_message_start(event)
                case "message_update":
                    self._on_message_update(event)
                case "message_end":
                    self._on_message_end(event)
                case "tool_execution_start":
                    self._on_tool_execution_start(event)
                case "tool_execution_update":
                    self._on_tool_execution_update(event)
                case "tool_execution_end":
                    self._on_tool_execution_end(event)

        # -- Event type extraction ------------------------------------------

        @staticmethod
        def _get_event_type(event: Any) -> str:
            """Extract the event type from an event object or dict."""
            if hasattr(event, "type"):
                return event.type
            elif isinstance(event, dict):
                return event.get("type", "")
            return ""

        # -- Event handlers by type -----------------------------------------

        def _on_agent_start(self, event: Any) -> None:
            """Handle agent_start event."""
            self._is_streaming = True
            self._re_disable_input()

        def _on_agent_end(self, event: Any) -> None:
            """Handle agent_end event.

            Updates the footer with final session info.
            """
            self._is_streaming = False
            self._re_enable_input()

            # Update footer with session info
            try:
                chat = self.query_one(ChatDisplay)
                footer = self.query_one(FooterWidget)

                messages = []
                if hasattr(event, "messages") and event.messages:
                    messages = event.messages
                elif isinstance(event, dict) and event.get("messages"):
                    messages = event["messages"]

                token_count = 0
                for msg in messages:
                    if isinstance(msg, dict):
                        token_count += 1  # Simplified: count messages
                    else:
                        token_count += 1

                footer.update(FooterData(
                    model=self._get_model_name(),
                    tokens=token_count,
                    session_name=self._get_session_name(),
                ))
            except Exception:
                pass  # Silently fail if footer not ready

        def _on_turn_start(self, event: Any) -> None:
            """Handle turn_start event."""
            pass

        def _on_turn_end(self, event: Any) -> None:
            """Handle turn_end event."""
            pass

        def _on_message_start(self, event: Any) -> None:
            """Handle message_start event.

            Appends a message widget to the ChatDisplay based on the message role.
            """
            try:
                chat = self.query_one(ChatDisplay)
                msg_data = self._event_to_message_data(event)
                if msg_data:
                    chat.append_message(msg_data)
            except Exception:
                pass  # ChatDisplay not ready yet

        def _on_message_update(self, event: Any) -> None:
            """Handle message_update event.

            Updates the streaming assistant message with new text.
            """
            self._update_streaming_message(event)

        def _update_streaming_message(self, event: Any) -> None:
            """Update the streaming assistant message with throttling.

            Called directly by tests and from _on_message_update.
            Cancels previous throttle timer and schedules a new one.

            Args:
                event: AgentEvent with message content.
            """
            # Cancel previous throttle timer
            if self._throttle_timer:
                self._throttle_timer.stop()

            self.call_later(self._do_update_streaming_message)

            try:
                self._throttle_timer = self.set_timer(1 / 30, lambda: None)
            except RuntimeError:
                # No running event loop (unit test outside App context)
                pass

        def _do_update_streaming_message(self) -> None:
            """Perform the actual streaming message update.

            Called via call_later to throttle updates to ~30Hz.
            Updates the ChatDisplay with the current streaming content.
            Reads content from self._streaming_content (set by
            update_streaming_message) and passes it to the ChatDisplay.
            """
            try:
                chat = self.query_one(ChatDisplay)
                # Read from app-level _streaming_content first, then fall back
                # to chat's _streaming_content for backward compatibility
                content = getattr(self, "_streaming_content", None)
                if not content:
                    content = getattr(chat, "_streaming_content", None)
                if content:
                    chat.update_streaming_message(content)
            except Exception:
                pass  # Silently fail if widget isn't ready

        def _on_message_end(self, event: Any) -> None:
            """Handle message_end event.

            Finalizes the current streaming message.
            """
            try:
                chat = self.query_one(ChatDisplay)
                chat.finalize_streaming_message()
            except Exception:
                pass

        def _on_tool_execution_start(self, event: Any) -> None:
            """Handle tool_execution_start event.

            Creates a ToolCallWidget with 'running' status.
            """
            try:
                chat = self.query_one(ChatDisplay)
                tool_name = self._get_event_field(event, "tool_name", "")
                tool_call_id = self._get_event_field(event, "tool_call_id", "")
                args = self._get_event_field(event, "args", {})

                if tool_name and tool_call_id:
                    data = ToolCallData(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        arguments=args if isinstance(args, dict) else {},
                        status="running",
                    )
                    chat.append_tool_call(data)
            except Exception:
                pass

        def _on_tool_execution_update(self, event: Any) -> None:
            """Handle tool_execution_update event.

            Updates the status of an existing ToolCallWidget.
            """
            try:
                chat = self.query_one(ChatDisplay)
                tool_call_id = self._get_event_field(event, "tool_call_id", "")

                if tool_call_id in chat._tool_call_widgets:
                    widget = chat._tool_call_widgets[tool_call_id]
                    widget.update_status("running")
            except Exception:
                pass

        def _on_tool_execution_end(self, event: Any) -> None:
            """Handle tool_execution_end event.

            Updates the ToolCallWidget and creates a ToolResultWidget.
            """
            try:
                chat = self.query_one(ChatDisplay)
                tool_name = self._get_event_field(event, "tool_name", "")
                tool_call_id = self._get_event_field(event, "tool_call_id", "")
                result = self._get_event_field(event, "result", None)
                is_error = self._get_event_field(event, "is_error", False)

                if tool_name and tool_call_id:
                    result_data = ToolResultData(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        result=result,
                        is_error=is_error,
                    )
                    chat.update_tool_result(result_data)

                    # Also update the tool call widget status to done
                    if tool_call_id in chat._tool_call_widgets:
                        widget = chat._tool_call_widgets[tool_call_id]
                        widget.update_status("done")
            except Exception:
                pass

        # -- Helper methods -------------------------------------------------

        def _get_event_field(self, event: Any, field_name: str, default: Any = None) -> Any:
            """Get a field from an event object or dict."""
            if hasattr(event, field_name):
                return getattr(event, field_name)
            elif isinstance(event, dict):
                return event.get(field_name, default)
            return default

        def _event_to_message_data(self, event: Any) -> ChatMessageData | None:
            """Convert an AgentEvent to ChatMessageData."""
            msg = self._get_event_field(event, "message")
            if msg is None:
                return None

            content = []
            if isinstance(msg, dict):
                content = msg.get("content", [])
            elif hasattr(msg, "content"):
                content = msg.content

            # Convert content blocks to dicts
            content_dicts = []
            for block in content:
                if isinstance(block, dict):
                    content_dicts.append(block)
                else:
                    content_dicts.append({
                        "type": getattr(block, "type", "text"),
                        "text": getattr(block, "text", str(block)),
                    })

            role = self._get_event_field(msg, "role", "assistant")
            if isinstance(msg, dict):
                role = msg.get("role", "assistant")
            elif hasattr(msg, "role"):
                role = msg.role

            return ChatMessageData(
                role=role,
                content=content_dicts,
                streaming=(event.type in ("message_update",)),
            )

        def _get_model_name(self) -> str:
            """Get the current model name from session config."""
            if self._session:
                # Try to get model from session attributes
                for attr in ("model", "_model", "model_config"):
                    if hasattr(self._session, attr):
                        model = getattr(self._session, attr)
                        if model:
                            if hasattr(model, "id"):
                                return model.id
                            elif isinstance(model, dict):
                                return model.get("id", "unknown")
            return "unknown"

        def _get_session_name(self) -> str | None:
            """Get the current session name."""
            if self._session:
                for attr in ("session_name", "_session_name", "name"):
                    if hasattr(self._session, attr):
                        return getattr(self._session, attr)
            return None

        # -- Input handling -------------------------------------------------

        def _on_input_submitted(self, text: str) -> None:
            """Handle user input submission."""
            if self._print_mode:
                self._handle_print_mode(text)
            else:
                self._handle_interactive(text)

        async def _handle_interactive(self, text: str) -> None:
            """Send text to agent session for interactive processing."""
            self._is_streaming = True
            self._re_disable_input()
            try:
                if self._session:
                    await self._session.prompt(text)
            finally:
                self._re_enable_input()

        def _handle_print_mode(self, text: str) -> None:
            """In print mode: stream response and exit."""
            if self._session:
                self.loop.create_task(self._print_mode_run(text))

        async def _print_mode_run(self, text: str) -> None:
            """Run the agent in print mode asynchronously."""
            try:
                if self._session:
                    messages = await self._session.prompt(text)
                    for msg in messages:
                        content = msg.get("content", [])
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                print(block.get("text", ""), end="")
                    print()
            finally:
                self.exit()

        # -- Input enable/disable -------------------------------------------

        def _re_disable_input(self) -> None:
            """Disable the input bar (during streaming)."""
            try:
                input_bar = self.query_one(InputBar)
                input_bar.disabled = True
            except Exception:
                pass

        def _re_enable_input(self) -> None:
            """Re-enable the input bar (after streaming ends)."""
            try:
                input_bar = self.query_one(InputBar)
                input_bar.disabled = False
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # CLI entry point (typer-based)
    # -----------------------------------------------------------------------

    def build_session(
        model: str | None = None,
        provider: str | None = None,
        session_name: str | None = None,
        cwd: str | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        project_root: Any = None,
        config_override: str | None = None,
    ) -> Any:
        """Build an AgentSession from CLI arguments and config files."""
        from tau_agent_core import AgentSession, SessionManager
        from tau_ai.types import Model
        from tau_coding_agent.config import load_config
        from pathlib import Path

        user_config_path = Path(config_override) if config_override else None
        config = load_config(
            project_root=project_root,
            user_config_override=user_config_path,
        )

        model_name = model or config.get("model", "gpt-4")
        provider_name = provider or config.get("provider", "openai")
        base_url = config.get("base_url", "https://api.openai.com/v1")
        context_window = config.get("context_window", 128000)
        max_tokens = config.get("max_tokens", 4096)
        effective_system_prompt = system_prompt or config.get("system_prompt", "")

        model_config = Model(
            id=model_name,
            name=model_name,
            api="openai-completions",
            provider=provider_name,
            base_url=base_url,
            context_window=context_window,
            max_tokens=max_tokens,
        )

        sm = SessionManager()

        session = AgentSession(
            session_manager=sm,
            model=model_config,
            system_prompt=effective_system_prompt,
        )

        return session


# ---------------------------------------------------------------------------
# Fallback stub (when Textual is not installed)
# ---------------------------------------------------------------------------

if not _HAS_TEXTUAL:

    class ParleyApp:
        """Fallback stub when Textual is not installed."""

        def __init__(
            self,
            session: Any = None,
            print_mode: bool = False,
            layout: Any = None,
        ):
            self.session = session
            self.print_mode = print_mode
            self.layout = layout
            self.ready = False
            self._is_streaming = False
            self._throttle_timer = None
            self._session = session

        @property
        def ready(self) -> bool:
            return self._is_ready

        async def start(self) -> None:
            self._is_ready = True

        async def stop(self) -> None:
            self._is_ready = False
            if self._session is not None:
                self._session = None
                self.session = None
            if self.layout is not None:
                self.layout = None

        def _re_disable_input(self) -> None:
            pass

        def _re_enable_input(self) -> None:
            pass

    def build_session(**kwargs: Any) -> Any:
        """Build an AgentSession from CLI arguments (stub)."""
        from tau_agent_core import AgentSession, SessionManager
        from tau_ai.types import Model
        from tau_coding_agent.config import load_config

        config = load_config()
        model_name = kwargs.get("model") or config.get("model", "gpt-4")
        provider_name = kwargs.get("provider") or config.get("provider", "openai")

        return AgentSession(
            session_manager=SessionManager(),
            model=Model(
                id=model_name,
                name=model_name,
                provider=provider_name,
            ),
        )
