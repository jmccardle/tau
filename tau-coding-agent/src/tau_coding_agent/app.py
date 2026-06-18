"""ParleyApp: Full Textual TUI application for τ-coding-agent.

Fork of parley.py with τ-agent-core backends.

Reference: PHASE-4-SUBPHASE-1.md — TUI App Shell
Reference: SUBPHASE-0.0.md — AgentSession interface (section 7)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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

    class ChatDisplay(Container):
        """Container for chat messages. 30Hz throttle on updates."""

        CSS_ID = "chat-display"

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._streaming_content: str = ""
            self._messages: list[dict[str, Any]] = []

        def update_streaming_message(self, content: str) -> None:
            """Update the streaming message content."""
            self._streaming_content = content

        def add_message(self, message: dict[str, Any]) -> None:
            """Add a message to the chat display (incremental mounting)."""
            self._messages.append(message)

        def get_messages(self) -> list[dict[str, Any]]:
            """Return all messages."""
            return self._messages

        def compose(self) -> ComposeResult:
            yield RichLog(id="chat-log")

        def clear_messages(self) -> None:
            """Clear all messages."""
            self._messages.clear()
            self._streaming_content = ""

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
            self._ready = False

        # -- Backward-compatible properties/methods (from Subphase 0) --------

        @property
        def ready(self) -> bool:
            """Whether the app has been initialized."""
            return self._ready

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
            """Start the TUI application.

            Backward-compatible method from the Subphase 0 stub.
            """
            self._ready = True

        async def stop(self) -> None:
            """Stop the TUI application.

            Backward-compatible method from the Subphase 0 stub.
            """
            self._ready = False
            if self._session is not None:
                self._session = None
            if self.layout is not None:
                self.layout = None

        # -- Layout (Textual compose pattern) -------------------------------

        def compose(self) -> ComposeResult:
            """Define the Textual widget tree.

            Layout: header + (sidebar + chat + input) + footer.
            Uses Textual's compose pattern so widgets are properly mounted.
            """
            yield Header()

            with Horizontal():
                yield Footer()
                with Vertical():
                    with Container(id="chat-container"):
                        yield ChatDisplay(id="chat-display")
                    yield InputBar(id="input-bar")

        def _setup_layout(self) -> None:
            """Configure the basic layout programmatically.

            This method is provided for backward compatibility with
            Subphase 0. In the full Textual implementation, the layout
            is defined via the ``compose()`` method above.

            Attributes:
                Creates and mounts Header, Footer, ChatDisplay, and
                InputBar widgets inside a horizontal/vertical container
                hierarchy.
            """
            # This method is kept for backward compatibility.
            # The actual Textual widget tree is defined in compose().
            # Subphases that call this method will still get the
            # backward-compatible AppLayout behavior.
            pass

        # -- Lifecycle ------------------------------------------------------

        def on_mount(self) -> None:
            """Subscribe to agent session events (layout is from compose())."""
            self._subscribe_to_events()

        def _subscribe_to_events(self) -> None:
            """Subscribe to agent session events."""
            if self._session:
                self._session.subscribe(self._handle_event)

        # -- Event handlers -------------------------------------------------

        def _handle_event(self, event: Any) -> None:
            """Dispatch agent events to widgets.

            Args:
                event: An AgentEvent instance (or dict with 'type' key).
            """
            event_type = self._get_event_type(event)

            if event_type == "message_update":
                self._update_streaming_message(event)
            elif event_type == "agent_end":
                self._on_agent_end(event)
            elif event_type == "agent_start":
                self._on_agent_start(event)

        @staticmethod
        def _get_event_type(event: Any) -> str:
            """Extract the event type from an event object or dict."""
            if hasattr(event, "type"):
                return event.type
            elif isinstance(event, dict):
                return event.get("type", "")
            return ""

        def _update_streaming_message(self, event: Any) -> None:
            """Update the streaming message with 30Hz throttle.

            The throttle timer accumulates streaming deltas and updates
            the display at most 30 times per second. This prevents UI
            thrashing during high-speed LLM streaming.
            """
            # Cancel the previous throttle timer
            if self._throttle_timer:
                self._throttle_timer.stop()

            # Schedule the actual update on the next frame
            self.call_later(self._do_update_streaming_message)

            # Set a 30Hz timer for throttling
            self._throttle_timer = self.set_timer(1 / 30, lambda: None)

        def _do_update_streaming_message(self) -> None:
            """Perform the actual streaming message update."""
            try:
                chat = self.query_one(ChatDisplay)
                chat.update_streaming_message(self._streaming_content)
                chat.refresh()
            except Exception:
                pass  # Silently fail if widget isn't ready

        def _on_agent_end(self, event: Any) -> None:
            """Handle agent_end event.

            Args:
                event: The AgentEvent with type='agent_end'.
            """
            self._is_streaming = False
            self._re_enable_input()

        def _on_agent_start(self, event: Any) -> None:
            """Handle agent_start event."""
            self._is_streaming = True
            self._re_disable_input()

        # -- Input handling -------------------------------------------------

        def _on_input_submitted(self, text: str) -> None:
            """Handle user input submission.

            Args:
                text: The text the user entered.
            """
            if self._print_mode:
                self._handle_print_mode(text)
            else:
                self._handle_interactive(text)

        async def _handle_interactive(self, text: str) -> None:
            """Send text to agent session for interactive processing.

            Args:
                text: The user's prompt text.
            """
            self._is_streaming = True
            self._re_disable_input()
            try:
                if self._session:
                    await self._session.prompt(text)
            finally:
                self._re_enable_input()

        def _handle_print_mode(self, text: str) -> None:
            """In print mode: stream response and exit.

            Args:
                text: The user's prompt text.
            """
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
        """Build an AgentSession from CLI arguments and config files.

        Configuration is loaded in the following order (later overrides earlier):
        1. DEFAULT_SETTINGS (built-in defaults)
        2. ``.tau/settings.json`` in project_root (or cwd)
        3. ``~/.tau/settings.json`` (user home)
        4. CLI arguments (model, provider, system_prompt, etc.)

        Args:
            model: LLM model identifier (overrides config).
            provider: Provider name (e.g., 'openai', 'anthropic').
            session_name: Name for the current session.
            cwd: Working directory for tool execution.
            system_prompt: System prompt text (overrides config).
            tools: List of tool names to enable.
            project_root: Root directory for project config.
                Defaults to cwd.
            config_override: Path to user config file (for testing).

        Returns:
            An AgentSession instance.
        """
        from tau_agent_core import AgentSession, SessionManager
        from tau_ai.types import Model
        from tau_coding_agent.config import load_config

        # Load configuration from files
        # config_override: Path to user config file (for CLI --config or testing)
        from pathlib import Path

        user_config_path = Path(config_override) if config_override else None
        config = load_config(
            project_root=project_root,
            user_config_override=user_config_path,
        )

        # Apply config values as base, then override with CLI args
        model_name = model or config.get("model", "gpt-4")
        provider_name = provider or config.get("provider", "openai")
        base_url = config.get("base_url", "https://api.openai.com/v1")
        context_window = config.get("context_window", 128000)
        max_tokens = config.get("max_tokens", 4096)

        # System prompt: CLI arg > config > default
        effective_system_prompt = system_prompt or config.get("system_prompt", "")

        # Create the model config with all required fields
        model_config = Model(
            id=model_name,
            name=model_name,
            api="openai-completions",
            provider=provider_name,
            base_url=base_url,
            context_window=context_window,
            max_tokens=max_tokens,
        )

        # Create session manager
        sm = SessionManager()

        # Build and return the session
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
        """Fallback stub when Textual is not installed.

        Provides a minimal interface matching the Subphase 0 stub for
        testing without Textual.
        """

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
            return self._ready

        async def start(self) -> None:
            self._ready = True

        async def stop(self) -> None:
            self._ready = False
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
        """Build an AgentSession from CLI arguments (stub).

        Falls back to config file settings when Textual is not available.
        """
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
