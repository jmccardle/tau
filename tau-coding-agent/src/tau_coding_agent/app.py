"""
Parley - A minimalist, performant chat interface for LLMs.

Clean, simple, fast. Built with Textual.
"""

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, Input, Header, Footer, Label, Markdown, Button, TextArea
from textual.binding import Binding
from textual.reactive import reactive
from textual import events
from textual.message import Message
from textual.screen import ModalScreen
from pathlib import Path
from datetime import datetime, timedelta
import json
import time
import asyncio
import traceback
from typing import Optional

from tau_coding_agent.backends import create_backend, Backend
# Chat persistence lives in a Textual-free module so `tau -p` can save sessions
# without importing the TUI. TAU_DIR/Chat are re-exported here for the TUI's use
# (and so existing `from tau_coding_agent.app import Chat` keeps working).
from tau_coding_agent.session_store import TAU_DIR, Chat
# Collapsible chat components. MessageBox (below) is the universal per-message
# host; these are the children it composes — one reasoning region and N tool
# boxes — plus the exchange grouping used by the streaming state machine.
from tau_coding_agent.chat_widgets import ExchangeBox, ReasoningRegion, ToolBox


class SystemPromptEditor(ModalScreen):
    """Modal screen for editing the system prompt."""

    def __init__(self, current_prompt: str):
        super().__init__()
        self.current_prompt = current_prompt
        self.new_prompt = current_prompt

    def compose(self) -> ComposeResult:
        """Compose the modal."""
        with Container(id="prompt-editor-dialog"):
            yield Static("Edit System Prompt", id="prompt-editor-title")
            yield TextArea(self.current_prompt, id="prompt-editor-textarea")
            with Horizontal(id="prompt-editor-buttons"):
                yield Button("Save", variant="primary", id="prompt-save")
                yield Button("Cancel", variant="default", id="prompt-cancel")

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        if event.button.id == "prompt-save":
            textarea = self.query_one("#prompt-editor-textarea", TextArea)
            self.new_prompt = textarea.text
            self.dismiss(self.new_prompt)
        elif event.button.id == "prompt-cancel":
            self.dismiss(None)


# Role → (display label, CSS modifier class). ONE widget renders every kind of
# message; the role only selects a label + color (via the `box-<role>` class in
# parley.tcss). Adding a kind = adding an entry here + a CSS rule, nothing else.
ROLE_LABELS: dict[str, str] = {
    "pending": "…",
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "toolCall": "Tool call",
    "toolResult": "Tool result",
}


def format_tool_call_body(name: str, arguments: object) -> str:
    """Render a tool call's Markdown body. Shared by the live streaming path and
    the saved-chat reload path so the two can never drift apart."""
    args_text = json.dumps(arguments, indent=2, default=str)
    return f"`{name}`\n\n```json\n{args_text}\n```"


def format_tool_result_body(name: str, result_text: str, is_error: bool) -> str:
    """Render a tool result's Markdown body (live + reload). Truncated for
    display, matching the live ``tool_execution_end`` rendering."""
    status = "Error" if is_error else "Success"
    return f"`{name}` — {status}\n\n```\n{result_text[:500]}\n```"


def _join_text_blocks(blocks: object) -> str:
    """Concatenate the ``text`` blocks of a τ message content list (or pass a
    plain string through). Used to flatten persisted assistant/toolResult bodies."""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, list):
        return "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class MessageBox(Static):
    """The ONE universal widget per message — the messages-list 1:1 mapping.

    Every ``{"role": ...}`` dict in the transcript renders as exactly one
    MessageBox, so the widget tree mirrors the data model (which is what makes
    reload trivial and freeze-proof). A box renders, top to bottom:

      - an optional :class:`ReasoningRegion` (assistant reasoning — streamed and
        collapsible), mounted lazily the instant reasoning arrives,
      - the message text (a Markdown body),
      - zero or more :class:`ToolBox` children (one per tool call; the matching
        tool *result* folds into its box by ``tool_call_id``).

    user/system messages use only the text body; an assistant turn may add
    reasoning and tool boxes — reasoning + answer + the turn's tools are one
    completion, so they live in one bordered box (per the design discussion).
    The role selects the border label + color (``box-<role>``); the border is
    on the box itself so the whole completion reads as a single box.

    A box may start as ``role="pending"`` and be *resolved* in place via
    :meth:`set_role` without re-mounting, preserving true arrival order.
    """

    def __init__(self, role: str, content: str = "", subtitle: str = ""):
        super().__init__(classes=f"chat-message box-{role}")
        self.role = role
        self._content = content
        self._subtitle = subtitle
        self._reasoning: ReasoningRegion | None = None
        self._tool_boxes: dict[str, ToolBox] = {}

    def _format(self, content: str) -> str:
        # Preserve single newlines as paragraph breaks for Markdown.
        return content.replace("\n", "\n\n")

    def compose(self) -> ComposeResult:
        # Three stacked slots: reasoning (lazy), the text body, tool boxes (lazy).
        # Empty slots collapse to zero height, so a plain user message looks
        # exactly like a single text box.
        self._reasoning_slot = Vertical(classes="message-reasoning")
        yield self._reasoning_slot
        md = Markdown(self._format(self._content), classes="message-content")
        self._md_widget = md
        yield md
        self._tools_slot = Vertical(classes="message-tools")
        yield self._tools_slot

    def on_mount(self) -> None:
        # The role label + color live on the box border (not the inner Markdown),
        # so reasoning + text + tools sit inside one titled border.
        self.border_title = ROLE_LABELS.get(self.role, self.role.capitalize())
        if self._subtitle:
            self.border_subtitle = self._subtitle

    # -- text body -----------------------------------------------------------

    def set_role(self, role: str) -> None:
        """Resolve/retype this box in place (e.g. pending → assistant)."""
        self.remove_class(f"box-{self.role}")
        self.role = role
        self.add_class(f"box-{role}")
        self.border_title = ROLE_LABELS.get(role, role.capitalize())

    def update_content(self, content: str) -> None:
        """Replace the text body in place (used for streaming text)."""
        self._content = content
        if hasattr(self, "_md_widget"):
            self._md_widget.update(self._format(content))

    @property
    def content_text(self) -> str:
        return self._content

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle = subtitle
        self.border_subtitle = subtitle

    # -- reasoning + tools: the unified host API (used by the task-4 wiring) --

    def ensure_reasoning(self) -> ReasoningRegion:
        """Lazily mount (once) and return this message's reasoning region."""
        if self._reasoning is None:
            self._reasoning = ReasoningRegion()
            self._reasoning_slot.mount(self._reasoning)
        return self._reasoning

    def add_tool_call(self, name: str, arguments: object, tool_call_id: str = "") -> ToolBox:
        """Append a tool call as a child ToolBox, tracked by id for its result."""
        box = ToolBox(name, arguments, tool_call_id)
        if tool_call_id:
            self._tool_boxes[tool_call_id] = box
        self._tools_slot.mount(box)
        return box

    def set_tool_result(self, tool_call_id: str, result_text: str, is_error: bool = False) -> bool:
        """Fold a tool result into its matching ToolBox. Returns ``False`` if no
        box matches the id — the caller decides what to do, nothing is fabricated."""
        box = self._tool_boxes.get(tool_call_id)
        if box is None:
            return False
        box.set_result(result_text, is_error)
        return True

    @property
    def reasoning(self) -> ReasoningRegion | None:
        return self._reasoning

    @property
    def tool_boxes(self) -> dict[str, ToolBox]:
        return self._tool_boxes


# Backwards-compatible alias: older code/tests referenced `ChatMessage`.
ChatMessage = MessageBox


class ChatListItem(Static):
    """A clickable chat list item."""

    def __init__(self, chat_path: Path, chat: Chat):
        super().__init__(f"• {chat.get_display_title()}", classes="chat-list-item")
        self.chat_path = chat_path
        self.chat = chat

    def on_click(self):
        """Handle click to load this chat."""
        self.post_message(ChatSelected(self.chat_path))


class ChatSelected(Message):
    """Message sent when a chat is selected from the sidebar."""

    def __init__(self, chat_path: Path):
        super().__init__()
        self.chat_path = chat_path


class ChatSidebar(Container):
    """Sidebar showing recent chats grouped by date."""

    def __init__(self):
        super().__init__(id="sidebar")
        self.chats: list[Path] = []

    def compose(self) -> ComposeResult:
        """Compose sidebar contents."""
        yield Static("Parley", classes="sidebar-title")
        yield Button("+ New Chat", id="new-chat-button", variant="primary")

        with VerticalScroll(id="chat-list"):
            # Will be populated dynamically
            pass

    def refresh_chats(self):
        """Refresh the list of chats."""
        self.chats = Chat.list_recent()
        self._render_chat_list()

    def _render_chat_list(self):
        """Render the chat list grouped by date."""
        chat_list = self.query_one("#chat-list", VerticalScroll)

        # Clear existing items
        chat_list.query("ChatListItem, Static").remove()

        if not self.chats:
            chat_list.mount(Static("No chats yet", classes="chat-list-empty"))
            return

        # Group by date
        now = datetime.now()
        today = []
        yesterday = []
        older = []

        for path in self.chats:
            try:
                chat = Chat.load(path)
                created = datetime.fromtimestamp(chat.created_at)

                if created.date() == now.date():
                    today.append((path, chat))
                elif created.date() == (now - timedelta(days=1)).date():
                    yesterday.append((path, chat))
                else:
                    older.append((path, chat))
            except Exception as e:
                self.app.log(f"Failed to load chat {path}: {e}")

        # Mount grouped items
        if today:
            chat_list.mount(Static("[bold]Today[/bold]", classes="chat-group-header"))
            for path, chat in today:
                chat_list.mount(ChatListItem(path, chat))

        if yesterday:
            chat_list.mount(Static("[bold]Yesterday[/bold]", classes="chat-group-header"))
            for path, chat in yesterday:
                chat_list.mount(ChatListItem(path, chat))

        if older:
            chat_list.mount(Static("[bold]Older[/bold]", classes="chat-group-header"))
            for path, chat in older[:10]:  # Limit older chats
                chat_list.mount(ChatListItem(path, chat))

    def on_mount(self):
        """Refresh chats when mounted."""
        self.refresh_chats()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "new-chat-button":
            # action_new_chat is async; dispatch through run_action so it is
            # actually awaited. A bare self.app.action_new_chat() just builds an
            # un-awaited coroutine and silently does nothing — the "+ New Chat"
            # button bug.
            await self.app.run_action("new_chat")


class ChatDisplay(VerticalScroll):
    """Main chat display area with incremental, arrival-ordered rendering.

    Every entry is a :class:`MessageBox`. During a streaming turn the display
    runs a small state machine driven by normalized backend events
    (see ``TauBackend.stream_chat``'s ``on_event``):

    - ``turn_start`` opens a *pending* box (unknown type) at the end.
    - the first ``text_delta`` *resolves* that pending box into an assistant
      box and streams text into it in place (so text is never duplicated).
    - a ``tool_call`` resolves an *empty* pending box into a tool-call box,
      otherwise mounts a NEW tool-call box below (text-then-call keeps both,
      in order).
    - a ``tool_result`` mounts a tool-result box.

    Because pending boxes resolve in place and new boxes mount at the end, the
    assistant's final text (a fresh turn after the tool calls) lands LAST.
    """

    def __init__(self):
        super().__init__(id="chat-display")
        self._last_render_time = 0.0
        # The current turn's text accumulates into exactly ONE box.
        self._active_text: str = ""
        # The pending/active box for the current turn (resolved in place).
        self._pending_box: Optional[MessageBox] = None
        # The box currently receiving streamed assistant text.
        self._text_box: Optional[MessageBox] = None

    def clear_messages(self):
        """Clear all messages from display and reset streaming state."""
        self.query(MessageBox).remove()
        self._pending_box = None
        self._text_box = None
        self._active_text = ""

    def add_message(self, role: str, content: str, subtitle: str = ""):
        """Add a finished (non-streaming) message box to the display."""
        box = MessageBox(role, content, subtitle)
        self.mount(box)
        self.scroll_end(animate=False)
        return box

    def add_persisted_message(self, msg: dict) -> None:
        """Render one *persisted* message (from a saved chat) in arrival order.

        Unlike the live path — driven by streaming lifecycle events — a reloaded
        message carries its content as the τ on-disk shape: a plain string
        (user/system), or a list of block dicts (assistant: ``text`` +
        ``toolCall`` blocks; ``toolResult``: a separate role with ``text`` blocks
        plus top-level ``tool_name``/``is_error``). Each block becomes the SAME
        ``MessageBox`` kind the live path would have produced — a ``str``-only
        renderer here is exactly the bug that froze the TUI on chat reload, so we
        normalize instead of handing a list to ``MessageBox``.

        Raises ``TypeError`` on an unrenderable content shape rather than
        silently dropping it (Fail-Early): an unexpected shape is a real bug.
        """
        role = msg.get("role", "")

        # toolResult is its own message role; the tool name + error flag live at
        # the message level, the result text in `text` blocks.
        if role == "toolResult":
            result_text = _join_text_blocks(msg.get("content", []))
            box = self.add_message(
                "toolResult",
                format_tool_result_body(
                    msg.get("tool_name", ""),
                    result_text,
                    bool(msg.get("is_error", False)),
                ),
            )
            if msg.get("is_error"):
                box.add_class("box-error")
            return

        content = msg.get("content", "")
        if isinstance(content, str):
            self.add_message(role, content)
            return
        if isinstance(content, list):
            # Assistant turns interleave text and tool calls. Accumulate text
            # into one box, flushing it before each tool call so order is kept
            # (text-then-call renders as two boxes, the call after the text).
            text_buf: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_buf.append(block.get("text", ""))
                elif btype == "toolCall":
                    if text_buf:
                        self.add_message(role, "".join(text_buf))
                        text_buf = []
                    self.add_message(
                        "toolCall",
                        format_tool_call_body(
                            block.get("name", ""), block.get("arguments", {})
                        ),
                    )
            if text_buf:
                self.add_message(role, "".join(text_buf))
            return

        raise TypeError(
            f"cannot render persisted message content of type {type(content).__name__}"
        )

    # ------------------------------------------------------------------
    # Streaming state machine (driven by TauBackend.stream_chat on_event)
    # ------------------------------------------------------------------

    def handle_stream_event(self, event: dict) -> None:
        """Render one normalized backend lifecycle event in arrival order."""
        kind = event.get("kind")
        if kind == "turn_start":
            self._on_turn_start()
        elif kind == "text_delta":
            self._on_text_delta(event.get("delta", ""))
        elif kind == "tool_call":
            self._on_tool_call(event)
        elif kind == "tool_result":
            self._on_tool_result(event)

    def _flush_text(self) -> None:
        """Force the active text box to show all accumulated text.

        The 30 Hz throttle in :meth:`_on_text_delta` can skip the final delta;
        call this whenever the current text box stops being the active target
        (a new turn, a tool call, or end of stream) so no tail text is lost.
        """
        if self._text_box is not None:
            self._text_box.update_content(self._active_text)
            self.scroll_end(animate=False)

    def _on_turn_start(self) -> None:
        # Flush any throttled tail from the previous turn's text box, then open
        # a fresh unknown/pending slot for this turn and reset text state.
        self._flush_text()
        self._active_text = ""
        self._text_box = None
        self._pending_box = MessageBox("pending", "")
        self.mount(self._pending_box)
        self.scroll_end(animate=False)

    def _on_text_delta(self, delta: str) -> None:
        if not delta:
            return
        # Resolve the pending slot into the assistant text box (in place), or
        # open one if the turn produced text without a turn_start (defensive).
        if self._text_box is None:
            if self._pending_box is not None:
                self._pending_box.set_role("assistant")
                self._text_box = self._pending_box
                self._pending_box = None
            else:
                self._text_box = self.add_message("assistant", "")
        self._active_text += delta
        # Throttle re-render to ~30 Hz.
        now = time.time()
        if now - self._last_render_time > 0.033:
            self._text_box.update_content(self._active_text)
            self.scroll_end(animate=False)
            self._last_render_time = now

    def _on_tool_call(self, event: dict) -> None:
        # Preamble text (if any) belongs to its own box and is now complete.
        self._flush_text()
        tool_name = event.get("name", "")
        arguments = event.get("arguments", {})
        content = format_tool_call_body(tool_name, arguments)
        # If the pending slot is still empty (model went straight to a tool
        # call with no preamble text), resolve it in place; else mount a new
        # box after whatever came before (preserves order).
        if self._pending_box is not None and not self._active_text:
            box = self._pending_box
            box.set_role("toolCall")
            box.update_content(content)
            self._pending_box = None
        else:
            box = self.add_message("toolCall", content)
        # Once a tool call lands, the current turn's assistant-text box (if any)
        # is finished; subsequent text belongs to a later turn / new box.
        self._text_box = None
        self._active_text = ""
        box._tool_call_id = event.get("id", "")  # type: ignore[attr-defined]
        self.scroll_end(animate=False)

    def _on_tool_result(self, event: dict) -> None:
        tool_name = event.get("name", "")
        result_text = str(event.get("result", ""))
        is_error = bool(event.get("is_error", False))
        content = format_tool_result_body(tool_name, result_text, is_error)
        box = self.add_message("toolResult", content)
        if is_error:
            box.add_class("box-error")

    def finalize_turn(self, subtitle: str = "") -> None:
        """Flush the active text box and clear per-turn streaming state.

        Called once after the whole agent loop finishes. Any box that never
        received text (a leftover pending slot) is removed so an empty
        placeholder is never left behind.
        """
        if self._text_box is not None:
            self._text_box.update_content(self._active_text)
            if subtitle:
                self._text_box.set_subtitle(subtitle)
        # A pending slot that was opened but never resolved (e.g. the final
        # turn was tool-only and produced no trailing text) is empty — drop it.
        if self._pending_box is not None:
            self._pending_box.remove()
        self._pending_box = None
        self._text_box = None
        self._active_text = ""
        self.scroll_end(animate=False)


class ChatInput(TextArea):
    """Custom input with multiline support and history navigation."""

    BINDINGS = [
        Binding("ctrl+j", "submit", "Send", show=False),  # Ctrl+Enter
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_history: list[str] = []
        self.command_history_index = -1
        self.current_draft = ""

    def action_submit(self):
        """Submit the current message."""
        text = self.text.strip()
        if text:
            self.post_message(Input.Submitted(self, text))

    def on_key(self, event: events.Key) -> None:
        """Handle key events for history navigation."""

        # Up/Down for history (only when on first/last line)
        if event.key == "up":
            cursor_row, _ = self.cursor_location
            if cursor_row == 0 and self.command_history and self.command_history_index < len(self.command_history) - 1:
                if self.command_history_index == -1:
                    self.current_draft = self.text
                self.command_history_index += 1
                self.text = self.command_history[-(self.command_history_index + 1)]
                event.prevent_default()
        elif event.key == "down":
            cursor_row, _ = self.cursor_location
            if cursor_row == self.document.line_count - 1 and self.command_history_index > -1:
                self.command_history_index -= 1
                if self.command_history_index == -1:
                    self.text = self.current_draft
                else:
                    self.text = self.command_history[-(self.command_history_index + 1)]
                event.prevent_default()

    def add_to_history(self, text: str):
        """Add text to command history."""
        if text.strip():
            self.command_history.append(text)
            self.command_history_index = -1
            self.current_draft = ""

    def clear_input(self):
        """Clear the input area."""
        self.text = ""


class Parley(App):
    """Main Parley application."""

    CSS_PATH = "parley.tcss"

    BINDINGS = [
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+n", "new_chat", "New Chat"),
        Binding("ctrl+e", "edit_system_prompt", "Edit Prompt"),
        Binding("ctrl+j", "focus_and_send", "^Enter=Send", show=True),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    current_chat: reactive[Optional[Chat]] = reactive(None)
    current_backend: Optional[Backend] = None
    config: dict = {}

    def __init__(self, cli_overrides: Optional[dict] = None):
        super().__init__()
        self.load_config()
        if cli_overrides:
            self._apply_cli_overrides(cli_overrides)

    def _apply_cli_overrides(self, overrides: dict) -> None:
        """Merge CLI flag overrides over the loaded config (CLI > config.json).

        Used by ``tau --model …``/``--system-prompt …`` so the TUI opens with
        the requested model/prompt instead of the config default.
        """
        models = overrides.get("models")
        if models:
            self.config.setdefault("models", {}).update(models)
        if "default_model" in overrides:
            self.config["default_model"] = overrides["default_model"]
        if "system_prompt" in overrides:
            self.config["system_prompt"] = overrides["system_prompt"]

    def load_config(self):
        """Load configuration from config.json."""
        config_path = TAU_DIR / "config.json"
        if config_path.exists():
            self.config = json.loads(config_path.read_text())
            self.log(f"Loaded config with {len(self.config.get('models', {}))} models")
        else:
            # Create default config
            self.log("No config found, creating default")
            self.config = {
                "models": {
                    "gpt-4o": {
                        "backend": "openai",
                        "model": "gpt-4o",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "your-api-key-here",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"]
                    },
                    "claude-3.5-sonnet": {
                        "backend": "anthropic",
                        "model": "claude-3-5-sonnet-20241022",
                        "api_key": "your-api-key-here",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"]
                    },
                    "gemini-2.0": {
                        "backend": "gemini",
                        "model": "gemini-2.0-flash-exp",
                        "api_key": "your-api-key-here",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"]
                    },
                    "local-llm": {
                        "backend": "openai",
                        "model": "qwen3-32b-kv4b",
                        "base_url": "http://192.168.1.100:8000/v1",
                        "api_key": "not-needed",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"]
                    }
                },
                "default_model": "local-llm",
                "system_prompt": "You are a helpful assistant. Be concise and clear."
            }
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(self.config, indent=2))
            self.log(f"Created default config at {config_path}")

    def compose(self) -> ComposeResult:
        """Compose the application layout."""
        yield Header()

        with Horizontal():
            yield ChatSidebar()

            with Vertical(id="main-area"):
                yield ChatDisplay()
                yield ChatInput(id="chat-input")

        yield Footer()

    def on_mount(self):
        """Set up the application on mount."""
        self.title = "Parley"
        self.sub_title = "Ready"

        # Focus input
        self.query_one("#chat-input", ChatInput).focus()

    async def on_input_submitted(self, event: Input.Submitted):
        """Handle message submission."""
        # Handle both Input and TextArea submissions
        if hasattr(event, 'input'):
            input_widget = event.input
        else:
            input_widget = self.query_one(ChatInput)

        message = event.value.strip()

        if not message:
            return

        # Add to history
        if hasattr(input_widget, 'add_to_history'):
            input_widget.add_to_history(message)

        # Clear input
        if hasattr(input_widget, 'clear_input'):
            input_widget.clear_input()
        else:
            input_widget.value = ""

        # Create new chat if needed
        if self.current_chat is None:
            await self.action_new_chat()

        # Add user message to chat
        self.current_chat.messages.append({"role": "user", "content": message})

        # Display user message
        display = self.query_one(ChatDisplay)
        display.add_message("user", message)

        # Disable input during response
        input_widget.disabled = True
        self.sub_title = "Thinking..."

        try:
            # Get response
            await self._get_assistant_response()
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            self.notify(error_msg, severity="error")
            self.log.error(f"Error getting response: {e}")
            self.log.error(traceback.format_exc())

            # Display error in chat
            display = self.query_one(ChatDisplay)
            display.add_message("system", f"**Error occurred:**\n```\n{str(e)}\n{traceback.format_exc()}\n```")
        finally:
            # Re-enable input
            input_widget.disabled = False
            input_widget.focus()
            self.sub_title = f"{self.current_chat.model}"

    async def _get_assistant_response(self):
        """Get and display assistant response with streaming.

        Widgets are mounted live, in true arrival order, off the backend's
        normalized event stream (``on_event``): a pending placeholder opens at
        each turn and resolves into an assistant-text or tool-call box, with
        tool results mounted as they complete. The assistant's final text
        therefore lands LAST, after the tool calls it follows.
        """
        display = self.query_one(ChatDisplay)

        # Bridge backend lifecycle events onto the display state machine.
        # The separate text `callback` is unused here (text is delivered via
        # the `text_delta` structured event), but the contract still requires
        # it, so pass a no-op.
        def on_event(event: dict) -> None:
            display.handle_stream_event(event)

        content, usage, new_messages, tool_calls_info = await self.current_backend.stream_chat(
            self.current_chat.messages,
            lambda _delta: None,
            on_event=on_event,
        )

        # Flush the active text box and clear per-turn streaming state.
        tokens_str = f"{usage['completion_tokens']} tokens"
        display.finalize_turn(tokens_str)

        # Update chat history with new messages from agent loop
        # (assistant responses + tool results, skip user message which
        # is already in self.current_chat.messages)
        for msg in new_messages:
            if msg.get("role") != "user":
                self.current_chat.messages.append(msg)

        # Save chat
        self.current_chat.save()

        # Refresh sidebar
        self.query_one(ChatSidebar).refresh_chats()

    async def action_new_chat(self, model: Optional[str] = None):
        """Start a new chat."""
        if model is None:
            model = self.config.get("default_model", "local-llm")

        self.log(f"Starting new chat with model: {model}")

        # Get model config
        model_config = self.config["models"].get(model)
        if not model_config:
            self.notify(f"Unknown model: {model}", severity="error")
            self.log(f"Available models: {list(self.config['models'].keys())}")
            return

        # Create backend
        try:
            self.current_backend = create_backend(model_config)
            self.log(f"Created backend: {model_config.get('backend')} for model {model}")
        except Exception as e:
            self.notify(f"Failed to create backend: {str(e)}", severity="error")
            self.log.error(f"Backend creation failed: {e}", exc_info=True)
            return

        # Create new chat
        system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
        self.current_chat = Chat(
            model=model,
            backend=model_config["backend"],
            messages=[{"role": "system", "content": system_prompt}],
            created_at=time.time()
        )

        # Clear display
        display = self.query_one(ChatDisplay)
        display.clear_messages()

        # Update UI
        self.sub_title = f"{model}"
        self.notify(f"Started new chat with {model}")

        # Save initial chat
        self.current_chat.save()

        # Refresh sidebar
        self.query_one(ChatSidebar).refresh_chats()

    def action_toggle_sidebar(self):
        """Toggle sidebar visibility."""
        sidebar = self.query_one(ChatSidebar)
        sidebar.styles.display = "none" if sidebar.styles.display == "block" else "block"

    def action_focus_and_send(self):
        """Focus input and send if focused (for global hotkey)."""
        input_widget = self.query_one(ChatInput)
        if input_widget.has_focus:
            input_widget.action_submit()
        else:
            input_widget.focus()

    def get_system_commands(self, screen):
        """Provide commands for the command palette."""
        yield from super().get_system_commands(screen)

        # Model switching commands
        models = self.config.get("models", {})
        self.log(f"Generating commands for {len(models)} models: {list(models.keys())}")

        for model_name in models.keys():
            yield SystemCommand(
                f"New Chat: {model_name}",
                f"Start a new chat with {model_name}",
                lambda m=model_name: self.run_action(f'new_chat("{m}")')
            )

        # General commands
        yield SystemCommand(
            "Clear Chat",
            "Clear current conversation",
            self.action_clear_chat
        )

        yield SystemCommand(
            "Export Chat",
            "Export chat to markdown",
            self.action_export_chat
        )

        yield SystemCommand(
            "Edit System Prompt",
            "Edit the system prompt for new chats",
            self.action_edit_system_prompt
        )

    async def action_clear_chat(self):
        """Clear the current chat."""
        if self.current_chat:
            # Keep only system message
            system_msg = next((m for m in self.current_chat.messages if m["role"] == "system"), None)
            if system_msg:
                self.current_chat.messages = [system_msg]
            else:
                self.current_chat.messages = []

            # Clear display
            display = self.query_one(ChatDisplay)
            display.clear_messages()

            self.notify("Chat cleared")

    async def action_export_chat(self):
        """Export current chat to markdown."""
        if not self.current_chat:
            self.notify("No chat to export", severity="warning")
            return

        # Build markdown
        lines = [f"# {self.current_chat.get_display_title()}\n"]
        lines.append(f"Model: {self.current_chat.model}\n")
        lines.append(f"Date: {datetime.fromtimestamp(self.current_chat.created_at).strftime('%Y-%m-%d %H:%M')}\n")
        lines.append("---\n")

        for msg in self.current_chat.messages:
            role = msg["role"].capitalize()
            # Persisted assistant/tool messages store content as a block list;
            # flatten to readable text rather than dumping a Python list repr.
            content = _join_text_blocks(msg.get("content", ""))
            lines.append(f"## {role}\n\n{content}\n")

        # Save to file
        export_path = TAU_DIR / "exports"
        export_path.mkdir(parents=True, exist_ok=True)

        filename = f"chat_{int(self.current_chat.created_at)}.md"
        file_path = export_path / filename
        file_path.write_text("\n".join(lines))

        self.notify(f"Exported to {file_path}")

    async def action_edit_system_prompt(self):
        """Edit the system prompt."""
        current_prompt = self.config.get("system_prompt", "You are a helpful assistant.")

        def handle_result(new_prompt: str | None):
            if new_prompt is not None:
                self.config["system_prompt"] = new_prompt
                # Save config
                config_path = TAU_DIR / "config.json"
                config_path.write_text(json.dumps(self.config, indent=2))
                self.notify("System prompt updated")

        await self.push_screen(SystemPromptEditor(current_prompt), handle_result)

    async def on_chat_selected(self, message: ChatSelected):
        """Handle chat selection from sidebar."""
        try:
            # Load the selected chat
            chat = Chat.load(message.chat_path)

            # Get model config and create backend
            model_config = self.config["models"].get(chat.model)
            if not model_config:
                self.notify(f"Model {chat.model} not found in config", severity="error")
                return

            self.current_backend = create_backend(model_config)
            self.current_chat = chat

            # Clear and reload display
            display = self.query_one(ChatDisplay)
            display.clear_messages()

            # Display all messages, normalizing the persisted block shape. A raw
            # list handed to add_message is exactly what froze reload before.
            for msg in chat.messages:
                if msg.get("role") != "system":  # Skip system message
                    display.add_persisted_message(msg)

            # Update UI
            self.sub_title = f"{chat.model}"
            self.notify(f"Loaded chat: {chat.get_display_title()}")

        except Exception as e:
            self.notify(f"Error loading chat: {str(e)}", severity="error")
            self.log.error(f"Failed to load chat: {e}", exc_info=True)


if __name__ == "__main__":
    app = Parley()
    app.run()
