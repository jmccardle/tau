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
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timedelta
import json
import time
import asyncio
import traceback
from typing import Optional

from tau_coding_agent.backends import create_backend, Backend

# Get the tau data directory for config and chat storage
TAU_DIR = Path.home() / ".tau"


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


@dataclass
class Chat:
    """Represents a chat conversation."""
    model: str
    backend: str
    messages: list[dict]
    created_at: float
    title: Optional[str] = None

    def save(self) -> Path:
        """Save chat to JSON file."""
        chats_dir = TAU_DIR / "chats"
        chats_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(self.created_at)}.json"
        path = chats_dir / filename
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> 'Chat':
        """Load chat from JSON file."""
        data = json.loads(path.read_text())
        return cls(**data)

    @classmethod
    def list_recent(cls, limit: int = 50) -> list[Path]:
        """List recent chat files, newest first."""
        chats_dir = TAU_DIR / "chats"
        if not chats_dir.exists():
            return []

        files = sorted(
            chats_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        return files[:limit]

    def get_display_title(self) -> str:
        """Get display title for this chat."""
        if self.title:
            return self.title

        # Use first user message as title (strip newlines for display)
        for msg in self.messages:
            if msg["role"] == "user":
                # Replace newlines with spaces for title display
                content = msg["content"].replace('\n', ' ')[:50]
                if len(msg["content"]) > 50:
                    content += "..."
                return content

        return f"Chat ({self.model})"


class ChatMessage(Static):
    """A single chat message with styled borders and alignment."""

    def __init__(self, role: str, content: str, timestamp: str = "", tokens: str = ""):
        super().__init__(classes=f"chat-message message-{role}")
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.tokens = tokens

    def compose(self) -> ComposeResult:
        """Compose the message widget."""
        # Preserve newlines in markdown by converting single newlines to double
        # This makes Markdown treat them as paragraph breaks
        content = self.content.replace('\n', '\n\n')

        # Create markdown widget with border
        md = Markdown(content, classes=f"message-content")
        self._md_widget = md

        # Set border titles based on role
        role_label = self.role.capitalize()
        md.border_title = f"{role_label} {self.timestamp}"

        if self.tokens:
            md.border_subtitle = self.tokens

        yield md

    def update_content(self, content: str):
        """Update message content (for streaming)."""
        self.content = content
        # Preserve newlines
        display_content = content.replace('\n', '\n\n')
        if hasattr(self, "_md_widget"):
            self._md_widget.update(display_content)


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

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        if event.button.id == "new-chat-button":
            self.app.action_new_chat()


class ChatDisplay(VerticalScroll):
    """Main chat display area with incremental message rendering."""

    def __init__(self):
        super().__init__(id="chat-display")
        self._last_render_time = 0
        self._stream_buffer = ""
        self._streaming_message: Optional[ChatMessage] = None

    def clear_messages(self):
        """Clear all messages from display."""
        self.query("ChatMessage").remove()

    def add_message(self, role: str, content: str, timestamp: str = "", tokens: str = ""):
        """Add a message to the display."""
        msg = ChatMessage(role, content, timestamp, tokens)
        self.mount(msg)
        self.scroll_end(animate=False)

    def start_streaming_message(self, role: str):
        """Start a new streaming message."""
        self._stream_buffer = ""
        self._streaming_message = ChatMessage(role, "", datetime.now().strftime("%H:%M"))
        self.mount(self._streaming_message)
        self.scroll_end(animate=False)

    def update_streaming_message(self, chunk: str):
        """Update streaming message with new chunk (30Hz throttled)."""
        self._stream_buffer += chunk

        # Throttle to 30Hz
        now = time.time()
        if now - self._last_render_time > 0.033:  # ~30 FPS
            if self._streaming_message:
                self._streaming_message.update_content(self._stream_buffer)
                self.scroll_end(animate=False)
            self._last_render_time = now

    def finalize_streaming_message(self, tokens: str = ""):
        """Finalize the streaming message."""
        if self._streaming_message:
            # Final update with token count
            self._streaming_message.update_content(self._stream_buffer)
            self._streaming_message.tokens = tokens

            # Update border subtitle
            if hasattr(self._streaming_message, "_md_widget"):
                self._streaming_message._md_widget.border_subtitle = tokens

            self._streaming_message = None
            self._stream_buffer = ""

    def add_tool_call(self, tool_name: str, arguments: dict) -> Static:
        """Add a collapsible tool call block to the display."""
        # Format arguments for display
        args_text = json.dumps(arguments, indent=2, default=str)
        content = f"**Tool call:** `{tool_name}`\n\n```json\n{args_text}\n```"
        widget = Static(content, classes="tool-call")
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget

    def add_tool_result(self, tool_name: str, result_text: str, is_error: bool = False) -> Static:
        """Add a collapsible tool result block to the display."""
        status = "Error" if is_error else "Success"
        status_class = "error" if is_error else "success"
        content = f"**Tool result [{status}]** `{tool_name}`\n\n```\n{result_text[:500]}\n```"
        widget = Static(content, classes=f"tool-result {status_class}")
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget


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

    def __init__(self):
        super().__init__()
        self.load_config()

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
        """Get and display assistant response with streaming."""
        display = self.query_one(ChatDisplay)

        # Start streaming message
        display.start_streaming_message("assistant")

        # Stream response — now returns (content, usage, new_messages, tool_calls)
        content, usage, new_messages, tool_calls_info = await self.current_backend.stream_chat(
            self.current_chat.messages,
            display.update_streaming_message
        )

        # Finalize streaming message
        tokens_str = f"{usage['completion_tokens']} tokens"
        display.finalize_streaming_message(tokens_str)

        # Display tool calls from streaming events
        for tc in tool_calls_info:
            tool_name = tc["name"]
            args = tc.get("arguments", {})
            display.add_tool_call(tool_name, args)

            # Display tool result if available
            result = tc.get("result", "")
            is_error = tc.get("error", False)
            if result:
                display.add_tool_result(tool_name, result, is_error)

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
            content = msg["content"]
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

            # Display all messages
            for msg in chat.messages:
                if msg["role"] != "system":  # Skip system message
                    display.add_message(msg["role"], msg["content"])

            # Update UI
            self.sub_title = f"{chat.model}"
            self.notify(f"Loaded chat: {chat.get_display_title()}")

        except Exception as e:
            self.notify(f"Error loading chat: {str(e)}", severity="error")
            self.log.error(f"Failed to load chat: {e}", exc_info=True)


if __name__ == "__main__":
    app = Parley()
    app.run()
