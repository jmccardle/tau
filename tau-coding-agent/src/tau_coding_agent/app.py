"""
Parley - A minimalist, performant chat interface for LLMs.

Clean, simple, fast. Built with Textual.
"""

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static, Input, Header, Footer, Markdown, Button, TextArea, Tree
from textual.binding import Binding
from textual.reactive import reactive
from textual import events, work
from textual.message import Message
from textual.screen import ModalScreen
from pathlib import Path
from datetime import datetime, timedelta
import json
import os
import time
import traceback
from typing import Any, Optional

from tau_coding_agent.backends import create_backend, Backend

# Session persistence lives in a Textual-free module so `tau -p` can save
# sessions without importing the TUI. Sessions are append-only JSONL transcripts
# partitioned by cwd (docs/SESSION-UX-REDESIGN.md); the TUI keeps a live working
# message list and funnels each produced message through Session.append_message.
from tau_coding_agent.session_store import TAU_DIR, Session, SessionInfo, list_sessions

# The pure session-tree algebra lives in tau-agent-core (the loop's package, not
# the TUI); the tree-browser (§3) is a view over ConversationTree.tree().
from tau_agent_core.conversation_tree import ConversationTree, TreeNode

# Collapsible chat components. MessageBox (below) is the universal per-message
# host; these are the children it composes — one reasoning region and N tool
# boxes — plus the exchange grouping used by the streaming state machine.
from tau_coding_agent.chat_widgets import (
    ExchangeBox,
    ReasoningRegion,
    ToolBox,
    format_duration,
    format_tokens,
)


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


class SessionTreeModal(ModalScreen[Optional[str]]):
    """Browse the conversation tree and pick a node to branch from (§3.2).

    Port of pi's ``showTreeSelector`` (interactive-mode.ts:4446): a
    ``textual.widgets.Tree`` populated from ``ConversationTree.tree()``, the current
    leaf highlighted. ``Enter`` dismisses with the chosen entry id; ``Esc`` cancels
    (``None``). Copies the ``SystemPromptEditor`` modal template.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, roots: list[TreeNode]) -> None:
        super().__init__()
        self._roots = roots

    def compose(self) -> ComposeResult:
        with Container(id="tree-browser-dialog"):
            yield Static("Browse Conversation Tree", id="tree-browser-title")
            tree: Tree[str] = Tree("session", id="tree-browser-tree")
            tree.show_root = False
            yield tree
            yield Static("Enter: branch from node    Esc: cancel", id="tree-browser-help")

    def on_mount(self) -> None:
        tree = self.query_one("#tree-browser-tree", Tree)
        leaf_widget: list[Any] = []

        def _add(parent, node: TreeNode) -> None:
            widget_node = parent.add(self._label(node), data=node.id)
            widget_node.expand()
            if node.is_leaf:
                leaf_widget.append(widget_node)
            for child in node.children:
                _add(widget_node, child)

        for root in self._roots:
            _add(tree.root, root)

        # Highlight the current leaf (pi passes realLeafId to the selector). Defer
        # until after the first refresh — a node's ``line`` (which ``move_cursor``
        # reads) is only assigned once the tree has laid out.
        if leaf_widget:
            leaf_node = leaf_widget[0]
            tree.call_after_refresh(tree.move_cursor, leaf_node)
        tree.focus()

    @staticmethod
    def _label(node: TreeNode) -> str:
        tag = node.role or node.kind
        text = node.preview or f"({node.kind})"
        marker = "  ◀ current" if node.is_leaf else ""
        return f"{tag}: {text}{marker}"

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter (or click) on a node confirms the branch point.
        event.stop()
        self.dismiss(event.node.data)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TreeModeModal(ModalScreen[Optional[str]]):
    """The three-mode chooser after a branch point is picked (§3.1).

    pi's ``showExtensionSelector`` (interactive-mode.ts:4479-4483): "No summary" /
    "Summarize" / "Summarize with custom instructions". Dismisses with
    ``"navigate"`` / ``"summarize"`` / ``"custom"`` (or ``None`` on cancel).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Container(id="tree-mode-dialog"):
            yield Static("Branch from selected node", id="tree-mode-title")
            with Vertical(id="tree-mode-buttons"):
                yield Button("No summary", variant="primary", id="mode-navigate")
                yield Button("Summarize abandoned branch", id="mode-summarize")
                yield Button("Summarize with custom instructions…", id="mode-custom")
                yield Button("Cancel", id="mode-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "mode-navigate": "navigate",
            "mode-summarize": "summarize",
            "mode-custom": "custom",
        }
        self.dismiss(mapping.get(event.button.id or ""))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TreeCustomInstructionsModal(ModalScreen[Optional[str]]):
    """Collect the custom summarizer instructions for mode 3 (§3.1).

    pi's ``showExtensionEditor`` (interactive-mode.ts:4494). Reuses the
    ``SystemPromptEditor`` ``TextArea`` shell; Save dismisses with the text, Cancel
    with ``None``.
    """

    def compose(self) -> ComposeResult:
        with Container(id="prompt-editor-dialog"):
            yield Static("Custom Summary Instructions", id="prompt-editor-title")
            yield TextArea("", id="prompt-editor-textarea")
            with Horizontal(id="prompt-editor-buttons"):
                yield Button("Summarize", variant="primary", id="custom-save")
                yield Button("Cancel", variant="default", id="custom-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "custom-save":
            self.dismiss(self.query_one("#prompt-editor-textarea", TextArea).text)
        elif event.button.id == "custom-cancel":
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
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _split_assistant_blocks(content: object) -> tuple[str, str, list[dict]]:
    """Split a persisted assistant message's content into ``(thinking, text,
    tool_calls)`` for exchange reconstruction.

    Mirrors how a completion is composed live: one reasoning region, one answer
    body, and N tool calls. Fragments are joined — both the fixed single-block
    shape and the legacy bloated shape (hundreds of one-fragment blocks, written
    before the provider consolidated them) collapse to one reasoning + one answer
    string here. A plain-string body is treated as answer text."""
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    calls: list[dict] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "thinking":
                thinking_parts.append(b.get("thinking", ""))
            elif btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "toolCall":
                calls.append(b)
    return "".join(thinking_parts), "".join(text_parts), calls


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
        """Lazily mount (once) and return this message's reasoning region.

        The region buffers its own streamed text until it mounts, so callers may
        ``set_text``/``append`` on the returned region immediately.
        """
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

    async def add_tool_call_async(
        self, name: str, arguments: object, tool_call_id: str = ""
    ) -> ToolBox:
        """Like :meth:`add_tool_call` but awaits the ToolBox mount.

        The reload path folds a tool *result* into this box immediately after the
        next persisted message, so the box must have composed first (a Markdown
        update before mount is silently lost — see ``ReasoningRegion``). The live
        path is network-paced and uses the fire-and-forget variant."""
        box = ToolBox(name, arguments, tool_call_id)
        if tool_call_id:
            self._tool_boxes[tool_call_id] = box
        await self._tools_slot.mount(box)
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
    """A clickable session list item."""

    def __init__(self, info: SessionInfo):
        super().__init__(f"• {info.display_title()}", classes="chat-list-item")
        self.chat_path = info.path
        self.info = info

    def on_click(self):
        """Handle click to load this session."""
        self.post_message(ChatSelected(self.chat_path))


class ChatSelected(Message):
    """Message sent when a session is selected from the sidebar."""

    def __init__(self, chat_path: Path):
        super().__init__()
        self.chat_path = chat_path


class ChatSidebar(Container):
    """Sidebar showing this directory's recent sessions, grouped by date."""

    def __init__(self):
        super().__init__(id="sidebar")
        self.sessions: list[SessionInfo] = []

    def compose(self) -> ComposeResult:
        """Compose sidebar contents."""
        yield Static("Parley", classes="sidebar-title")
        yield Button("+ New Chat", id="new-chat-button", variant="primary")

        with VerticalScroll(id="chat-list"):
            # Will be populated dynamically
            pass

    def refresh_chats(self):
        """Refresh the session list (cwd-scoped — §8 of the redesign)."""
        self.sessions = list_sessions(os.getcwd())
        self._render_chat_list()

    def _render_chat_list(self):
        """Render the session list grouped by recency."""
        chat_list = self.query_one("#chat-list", VerticalScroll)

        # Clear existing items
        chat_list.query("ChatListItem, Static").remove()

        if not self.sessions:
            chat_list.mount(Static("No sessions yet", classes="chat-list-empty"))
            return

        # Group by date (SessionInfo.modified is UTC; compare in local time).
        now = datetime.now()
        today: list[SessionInfo] = []
        yesterday: list[SessionInfo] = []
        older: list[SessionInfo] = []

        for info in self.sessions:
            when = info.modified.astimezone()
            if when.date() == now.date():
                today.append(info)
            elif when.date() == (now - timedelta(days=1)).date():
                yesterday.append(info)
            else:
                older.append(info)

        # Mount grouped items
        if today:
            chat_list.mount(Static("[bold]Today[/bold]", classes="chat-group-header"))
            for info in today:
                chat_list.mount(ChatListItem(info))

        if yesterday:
            chat_list.mount(Static("[bold]Yesterday[/bold]", classes="chat-group-header"))
            for info in yesterday:
                chat_list.mount(ChatListItem(info))

        if older:
            chat_list.mount(Static("[bold]Older[/bold]", classes="chat-group-header"))
            for info in older[:10]:  # Limit older sessions
                chat_list.mount(ChatListItem(info))

    def on_mount(self):
        """Refresh sessions when mounted."""
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

    One user→answer span is an **exchange**. While the agent loop streams, the
    display runs a state machine driven by normalized backend events (see
    ``TauBackend.stream_chat``'s ``on_event``) that groups the span under one
    collapsible :class:`ExchangeBox`:

    - :meth:`begin_exchange` (before the loop) opens an expanded ``ExchangeBox``.
    - each ``turn_start`` mounts ONE assistant :class:`MessageBox` *step* into
      the exchange — a completion's reasoning + text + tool boxes share it.
    - ``reasoning_delta`` streams into the step's lazily-mounted reasoning
      region; the region collapses the instant answer text / a tool call begins.
    - ``text_delta`` streams into the step's text body (in place, never dup'd).
    - ``tool_call`` adds a :class:`ToolBox` child to the step; ``tool_result``
      folds into it, matched by ``tool_call_id`` (routed across the exchange).
    - :meth:`finalize_exchange` (after the loop) flushes tails, snaps the final
      text-only answer OUT below the now-collapsed summary line, and stamps the
      summary (``N tools · X tok · M:SS``). A trivial no-tool exchange is
      unwrapped entirely — just the plain answer, no grouping. ONE reparent, at
      the end (Textual has no live reparent, so the answer is reconstructed).

    Reloaded (persisted) chats still render as flat boxes via
    :meth:`add_persisted_message` — rebuilding exchanges from the saved message
    list is a separate concern.
    """

    def __init__(self):
        super().__init__(id="chat-display")
        self._last_render_time = 0.0
        # Per-exchange streaming state (one user→answer span).
        self._exchange: Optional[ExchangeBox] = None
        # The current turn's assistant step box (reasoning + text + tools).
        self._active_box: Optional[MessageBox] = None
        # Accumulators for the active step (the 30 Hz throttle can skip the
        # final delta, so the tails are flushed when the target changes).
        self._active_text: str = ""
        self._active_reasoning: str = ""
        # Route each tool result to the step that issued the call, by id.
        self._tool_routes: dict[str, MessageBox] = {}

    def clear_messages(self):
        """Clear all messages from display and reset streaming state."""
        self.query(ExchangeBox).remove()
        self.query(MessageBox).remove()
        self._reset_exchange_state()

    def _reset_exchange_state(self) -> None:
        """Drop all per-exchange streaming references."""
        self._exchange = None
        self._active_box = None
        self._active_text = ""
        self._active_reasoning = ""
        self._tool_routes = {}

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
                        format_tool_call_body(block.get("name", ""), block.get("arguments", {})),
                    )
            if text_buf:
                self.add_message(role, "".join(text_buf))
            return

        raise TypeError(f"cannot render persisted message content of type {type(content).__name__}")

    # ------------------------------------------------------------------
    # Streaming state machine (driven by TauBackend.stream_chat on_event)
    # ------------------------------------------------------------------

    async def begin_exchange(self) -> None:
        """Open a new exchange (one user→answer span) before the agent loop runs.

        Awaits the mount so the exchange's collapsible body has composed before
        the first ``turn_start`` adds a step into it (begin→turn_start has no
        natural render tick between them, unlike the network-paced events that
        follow). Steps mount into the expanded ``ExchangeBox`` as the loop
        streams; :meth:`finalize_exchange` later collapses it to a summary line.
        """
        self._reset_exchange_state()
        self._exchange = ExchangeBox()
        await self.mount(self._exchange)
        self.scroll_end(animate=False)

    def handle_stream_event(self, event: dict) -> None:
        """Render one normalized backend lifecycle event in arrival order."""
        kind = event.get("kind")
        if kind == "turn_start":
            self._on_turn_start()
        elif kind == "reasoning_delta":
            self._on_reasoning_delta(event.get("delta", ""))
        elif kind == "text_delta":
            self._on_text_delta(event.get("delta", ""))
        elif kind == "tool_call":
            self._on_tool_call(event)
        elif kind == "tool_result":
            self._on_tool_result(event)

    def _start_step(self) -> MessageBox:
        """Mount a fresh assistant step box for the current turn.

        Steps live inside the exchange so the whole span groups under one
        summary. If no exchange is open (defensive — the live path always calls
        :meth:`begin_exchange` first), the step mounts at top level.
        """
        box = MessageBox("assistant", "")
        if self._exchange is not None:
            self._exchange.add_step(box)
        else:
            self.mount(box)
        return box

    def _flush(self) -> None:
        """Force the active step to show all accumulated reasoning + text.

        The 30 Hz throttle can skip the final delta; call this whenever the
        active step stops being the target (new turn, tool call, end of stream).
        """
        box = self._active_box
        if box is None:
            return
        if self._active_reasoning and box.reasoning is not None:
            box.reasoning.set_text(self._active_reasoning)
        if self._active_text:
            box.update_content(self._active_text)
        self.scroll_end(animate=False)

    def _collapse_active_reasoning(self) -> None:
        """Freeze + collapse the active step's reasoning once the answer begins.

        Reasoning precedes a completion's answer/tool calls, so the first text
        or tool event marks it complete. Runs once per step (a collapsed region
        short-circuits), flushing the full reasoning text before it folds away.
        """
        box = self._active_box
        if box is not None and box.reasoning is not None and not box.reasoning.collapsed:
            if self._active_reasoning:
                box.reasoning.set_text(self._active_reasoning)
            box.reasoning.mark_done()
            box.reasoning.collapsed = True

    def _on_turn_start(self) -> None:
        # Flush the previous step's throttled tail and freeze its reasoning (a
        # new turn means the previous completion is done, even if it was
        # reasoning-only), then open a fresh step and reset the accumulators.
        self._flush()
        self._collapse_active_reasoning()
        self._active_text = ""
        self._active_reasoning = ""
        self._active_box = self._start_step()
        self.scroll_end(animate=False)

    def _on_reasoning_delta(self, delta: str) -> None:
        if not delta or self._active_box is None:
            return
        self._active_reasoning += delta
        region = self._active_box.ensure_reasoning()
        # Throttle re-render to ~30 Hz (shared with text; within a turn reasoning
        # fully precedes text, so they don't contend).
        now = time.time()
        if now - self._last_render_time > 0.033:
            region.set_text(self._active_reasoning)
            self.scroll_end(animate=False)
            self._last_render_time = now

    def _on_text_delta(self, delta: str) -> None:
        if not delta or self._active_box is None:
            return
        # Answer content has begun — this step's reasoning is complete.
        self._collapse_active_reasoning()
        self._active_text += delta
        now = time.time()
        if now - self._last_render_time > 0.033:
            self._active_box.update_content(self._active_text)
            self.scroll_end(animate=False)
            self._last_render_time = now

    def _on_tool_call(self, event: dict) -> None:
        # Preamble reasoning/text for this step is complete; show it, fold the
        # reasoning, then add the tool box below the text (reasoning→text→tools).
        if self._active_box is None:
            self._active_box = self._start_step()
        self._flush()
        self._collapse_active_reasoning()
        tc_id = event.get("id", "") or ""
        self._active_box.add_tool_call(event.get("name", ""), event.get("arguments", {}), tc_id)
        if tc_id:
            self._tool_routes[tc_id] = self._active_box
        self.scroll_end(animate=False)

    def _on_tool_result(self, event: dict) -> None:
        tc_id = event.get("id", "") or ""
        result_text = str(event.get("result", ""))
        is_error = bool(event.get("is_error", False))
        box = self._tool_routes.get(tc_id)
        if box is not None and box.set_tool_result(tc_id, result_text, is_error):
            self.scroll_end(animate=False)
            return
        # No matching tool box: the call always precedes its result in the live
        # loop, so this means an id we never saw a call for. Don't fabricate a
        # standalone box — surface it loudly instead (Fail-Early).
        self.app.log(f"tool_result for unknown tool_call_id {tc_id!r}; no ToolBox to fold into")

    async def finalize_exchange(self, *, tokens: int, seconds: float) -> None:
        """Close the current exchange after the agent loop finishes.

        Flushes tails, then snaps the final text-only answer OUT below the
        collapsed summary so it stays visible. A trivial exchange (no tools) is
        unwrapped to just the plain answer — no grouping where there's nothing
        to group. One reparent, here, by reconstruction (Textual cannot move a
        live widget across parents).
        """
        self._flush()
        self._collapse_active_reasoning()  # freeze the last step's reasoning
        exchange = self._exchange
        if exchange is None:
            self._reset_exchange_state()
            return

        await self._close_exchange(exchange, tokens=tokens, seconds=seconds)
        self._reset_exchange_state()
        self.scroll_end(animate=False)

    @staticmethod
    def _exchange_subtitle(tokens: int, seconds: float | None) -> str:
        """The stats line stamped on an unwrapped (no-tool) answer. Duration is
        omitted when unknown (reload) rather than fabricated (Fail-Early)."""
        if seconds is None:
            return f"{format_tokens(tokens)} tok"
        return f"{format_tokens(tokens)} tok · {format_duration(seconds)}"

    async def _close_exchange(
        self, exchange: ExchangeBox, *, tokens: int, seconds: float | None
    ) -> None:
        """Collapse a fully-built exchange to its summary and surface the answer.

        Shared close-out for both the live state machine (:meth:`finalize_exchange`,
        which builds the exchange as events stream) and the reload reconstruction
        (:meth:`_reload_exchange`, which builds it all at once). Given an exchange
        already populated with step boxes, it: promotes the terminal text answer
        OUT below the exchange so it stays visible, unwraps a no-tool span to a
        plain answer, and otherwise collapses the exchange behind its summary
        line. ``seconds=None`` means duration is unknown (reload) and is omitted.
        """
        steps = list(exchange.query(MessageBox))
        tool_count = sum(len(b.tool_boxes) for b in steps)
        # The terminal turn is the no-tool-call answer; pull it out so it stays
        # visible. If the last step still has tools (e.g. max_turns hit mid-
        # tool), there is no clean final answer — leave everything collapsed.
        final = steps[-1] if steps and not steps[-1].tool_boxes else None
        # An entirely empty terminal step (no text, no reasoning) is not a real
        # answer — don't promote a blank box (Fail-Early: render nothing, not a
        # placeholder).
        if final is not None and not final.content_text.strip() and final.reasoning is None:
            final = None

        promoted = None
        if final is not None:
            promoted = await self._promote_answer(final, after=exchange)

        if tool_count == 0:
            # Nothing worth grouping — drop the wrapper entirely (this also
            # removes the original `final` box it still contains). A trivial
            # span has no summary line, so the (real) token + duration would be
            # lost — stamp them on the answer's subtitle instead of hiding them.
            if promoted is not None:
                promoted.set_subtitle(self._exchange_subtitle(tokens, seconds))
            exchange.remove()
        else:
            if final is not None:
                final.remove()
            exchange.collapsed = True
            exchange.set_summary(tools=tool_count, tokens=tokens, seconds=seconds)

    async def _promote_answer(self, src: MessageBox, *, after: Widget) -> MessageBox:
        """Mount a fresh top-level answer box copied from ``src``, after ``after``.

        Reconstructs rather than reparents (Textual has no cross-parent move).
        The terminal answer is text + optional reasoning (no tools), so copying
        its text and reasoning string is faithful and cheap.
        """
        new = MessageBox("assistant", src.content_text)
        await self.mount(new, after=after)
        if src.reasoning is not None:
            region = new.ensure_reasoning()
            region.set_text(src.reasoning.text)
            region.mark_done()
            region.collapsed = True
        return new

    # ------------------------------------------------------------------
    # Reload: reconstruct exchanges from the persisted flat message list
    # ------------------------------------------------------------------

    async def reload_messages(self, messages: list[dict]) -> None:
        """Render a saved chat as exchanges, matching the finalized live look.

        The persisted transcript is a flat list — ``system``, ``user``, then per
        completion an ``assistant`` message (reasoning + text + ``toolCall``
        blocks) and a ``toolResult`` message per call. This walks it back into
        the same widget tree the live state machine leaves behind: each
        user→answer span groups under one collapsed :class:`ExchangeBox` (summary
        ``N tools · X tok``), the terminal answer promoted out below it; a no-tool
        span is unwrapped to a plain answer.

        The ONE difference from live is the summary omits wall-clock duration —
        it is not persisted and we do not fabricate it (Fail-Early). Tokens come
        from each completion's persisted ``usage`` (a true 0 for pre-fix chats).
        """
        self.clear_messages()
        n = len(messages)
        i = 0
        while i < n:
            role = messages[i].get("role", "")
            if role == "system":
                i += 1
                continue
            if role == "user":
                # The user box sits above the exchange, as in the live path.
                self.add_persisted_message(messages[i])
                i += 1
            # Collect the answer span (assistant + toolResult) up to the next
            # user/system message, and rebuild it as one exchange.
            span: list[dict] = []
            while i < n and messages[i].get("role") not in ("user", "system"):
                span.append(messages[i])
                i += 1
            if span:
                await self._reload_exchange(span)
        self.scroll_end(animate=False)

    async def _reload_exchange(self, span: list[dict]) -> None:
        """Rebuild one user→answer span (assistant + toolResult messages) as a
        collapsed exchange, then close it out exactly like the live path."""
        exchange = ExchangeBox()
        await self.mount(exchange)
        routes: dict[str, ToolBox] = {}
        tokens = 0
        for msg in span:
            role = msg.get("role", "")
            if role == "assistant":
                step = MessageBox("assistant", "")
                await exchange.add_step_async(step)
                thinking, text, calls = _split_assistant_blocks(msg.get("content"))
                if thinking:
                    region = step.ensure_reasoning()
                    region.set_text(thinking)
                    region.mark_done()
                    region.collapsed = True
                if text:
                    step.update_content(text)
                for call in calls:
                    tc_id = call.get("id", "") or ""
                    box = await step.add_tool_call_async(
                        call.get("name", ""), call.get("arguments", {}), tc_id
                    )
                    if tc_id:
                        routes[tc_id] = box
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    tokens += int(usage.get("total_tokens", 0) or 0)
            elif role == "toolResult":
                tc_id = msg.get("tool_call_id", "") or ""
                target = routes.get(tc_id)
                result_text = _join_text_blocks(msg.get("content", []))
                if target is not None:
                    target.set_result(result_text, bool(msg.get("is_error", False)))
                else:
                    # The call always precedes its result on disk; a missing box
                    # means a dangling id — surface it, don't fabricate one.
                    self.app.log(f"reload: toolResult for unknown tool_call_id {tc_id!r}")
            else:
                # Unexpected role inside an answer span — render flat rather than
                # drop it (add_persisted_message raises on a bad content shape).
                self.add_persisted_message(msg)
        await self._close_exchange(exchange, tokens=tokens, seconds=None)


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
            if (
                cursor_row == 0
                and self.command_history
                and self.command_history_index < len(self.command_history) - 1
            ):
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
        Binding("ctrl+g", "browse_tree", "Tree"),
        Binding("ctrl+r", "toggle_reasoning", "Reasoning", priority=True),
        Binding("ctrl+t", "toggle_tools", "Tools", priority=True),
        Binding("ctrl+j", "focus_and_send", "^Enter=Send", show=True),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+c", "quit", "Quit"),
        # priority=True: caught during generation regardless of which widget holds
        # focus. The action no-ops when nothing is generating.
        Binding("escape", "cancel_generation", "Cancel", show=False, priority=True),
    ]

    # The active persisted session (append-only sink) and the live working
    # message list sent to the model. They are kept in step: every produced
    # message is appended to both; clear/compact mutate the working list (the
    # session file keeps the full transcript — append-only, no rewrite).
    current_session: reactive[Optional[Session]] = reactive(None)
    current_backend: Optional[Backend] = None
    config: dict = {}
    # Global show/hide state for the two collapsible content kinds. Each toggle
    # flips every reasoning region / tool box in the transcript at once; the
    # reactive records the last-applied intent (for the toggle's feedback).
    reasoning_collapsed: reactive[bool] = reactive(False)
    tools_collapsed: reactive[bool] = reactive(False)
    # True while a response worker is streaming. Gates Esc-to-cancel and the
    # input-disabled state; flipped on in on_input_submitted, off in the worker's
    # finally.
    is_generating: reactive[bool] = reactive(False)

    def __init__(self, cli_overrides: Optional[dict] = None):
        super().__init__()
        # The live conversation context (sent to the model). Mirrors the active
        # session's messages but is mutable for clear/compact.
        self.messages: list[dict] = []
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
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"],
                    },
                    "claude-3.5-sonnet": {
                        "backend": "anthropic",
                        "model": "claude-3-5-sonnet-20241022",
                        "api_key": "your-api-key-here",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"],
                    },
                    "gemini-2.0": {
                        "backend": "gemini",
                        "model": "gemini-2.0-flash-exp",
                        "api_key": "your-api-key-here",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"],
                    },
                    "local-llm": {
                        "backend": "openai",
                        "model": "qwen3-32b-kv4b",
                        "base_url": "http://192.168.1.100:8000/v1",
                        "api_key": "not-needed",
                        "tools": ["read", "write", "edit", "bash", "ls", "grep", "find"],
                    },
                },
                "default_model": "local-llm",
                "system_prompt": "You are a helpful assistant. Be concise and clear.",
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
        # ChatInput is the app's only Input.Submitted source (it posts
        # Input.Submitted(self, ...)), so the submitting widget is always the
        # #chat-input ChatInput.
        input_widget = self.query_one("#chat-input", ChatInput)

        message = event.value.strip()

        if not message:
            return

        input_widget.add_to_history(message)
        input_widget.clear_input()

        # Slash commands are intercepted here, BEFORE the text reaches the model.
        # Without this, "/compact" was just sent as a prompt and the model
        # "played along" instead of the harness compacting the conversation.
        if message == "/compact":
            await self.action_compact()
            return

        # /tree and /fork both open the tree-browser (pi aliases,
        # keybindings.ts:252-253). Intercepted here so the text never reaches the model.
        if message in ("/tree", "/fork"):
            self.action_browse_tree()
            return

        # Create new session if needed
        if self.current_session is None:
            await self.action_new_chat()
        assert self.current_session is not None  # action_new_chat sets current_session

        # Add the user turn to the working list so it is part of the context sent
        # to the model this turn. Do NOT persist it here: the AgentSession (bound to
        # this live Session, E3-ctx / D3) is the sole persister — it records the user
        # turn when the loop runs. The working list is reconciled back to the
        # authoritative log at turn-end (``self.messages = session.context``).
        self.messages.append({"role": "user", "content": message})

        # Display user message
        display = self.query_one(ChatDisplay)
        display.add_message("user", message)

        # Run the turn in a worker so the event loop stays free while the model
        # streams — that is what lets Esc-to-cancel be processed mid-response
        # (a direct `await` here parked the App message pump for the whole turn).
        # Input is disabled for the duration; the worker re-enables it on finish.
        input_widget.disabled = True
        self.is_generating = True
        self.sub_title = "Thinking… (Esc to cancel)"
        self._generate_response()

    @work(exclusive=True, group="generation")
    async def _generate_response(self) -> None:
        """Background worker: run one assistant turn and render it.

        Replaces the old inline ``await``. ``exclusive``/``group`` let
        :meth:`action_cancel_generation` target it. The ``finally`` restores the
        input regardless of how the turn ended (normal, error, or cooperative
        abort — which returns the partial answer rather than raising).
        """
        input_widget = self.query_one("#chat-input", ChatInput)
        try:
            await self._get_assistant_response()
        except Exception as e:
            self.notify(f"Error: {str(e)}", severity="error")
            self.log.error(f"Error getting response: {e}")
            self.log.error(traceback.format_exc())
            self.query_one(ChatDisplay).add_message(
                "system", f"**Error occurred:**\n```\n{str(e)}\n{traceback.format_exc()}\n```"
            )
        finally:
            self.is_generating = False
            input_widget.disabled = False
            input_widget.focus()
            # Show the running conversation rollup (tools · tokens) next to the
            # model, refreshed now that this exchange has been appended + saved.
            self._refresh_subtitle()

    def action_cancel_generation(self) -> None:
        """Esc: cooperatively abort the in-flight response (no-op if idle).

        Trips the backend's abort signal — the provider stops at the next streamed
        delta and the agent loop unwinds, so ``_get_assistant_response`` returns
        with the partial answer and the worker's ``finally`` re-enables input. No
        hard task-cancel, so there is no half-applied widget/persistence state.
        """
        if not self.is_generating or self.current_backend is None:
            return
        self.current_backend.abort()
        self.sub_title = "Cancelling…"

    async def _get_assistant_response(self):
        """Get and display assistant response with streaming.

        One user→answer span renders as a single collapsible exchange. The span
        is opened (:meth:`ChatDisplay.begin_exchange`) before the loop runs and
        closed (:meth:`ChatDisplay.finalize_exchange`) after it returns; in
        between, the backend's normalized ``on_event`` stream drives the steps
        (reasoning, text, tool calls/results) in true arrival order. The summary
        line is stamped from REAL usage + measured wall-clock — never an
        approximation (Fail-Early: a true 0 is shown as 0).
        """
        display = self.query_one(ChatDisplay)

        # Bridge backend lifecycle events onto the display state machine.
        # The separate text `callback` is unused here (text is delivered via
        # the `text_delta` structured event), but the contract still requires
        # it, so pass a no-op.
        def on_event(event: dict) -> None:
            display.handle_stream_event(event)

        assert self.current_session is not None  # set before a turn runs
        start = time.time()
        await display.begin_exchange()
        content, usage, _new_messages, tool_calls_info = await self.current_backend.stream_chat(
            self.messages,
            lambda _delta: None,
            on_event=on_event,
        )
        elapsed = time.time() - start

        # Collapse the exchange to its summary and surface the final answer.
        await display.finalize_exchange(tokens=usage.get("total_tokens", 0), seconds=elapsed)

        # Rebuild the working list as a VIEW over the authoritative session
        # (E3-ctx / D3, pi ``rebuildChatFromMessages``). The AgentSession — bound to
        # this live Session — already persisted this turn's user + assistant/tool
        # messages as the loop ran; there is one write path, so the app no longer
        # appends them itself (that was the double-write). Reading ``session.context``
        # back reconciles the working list (which carried a transient copy of the
        # user turn) with what was actually recorded, applying any compaction/branch
        # splice. This is a data rebuild only — the incremental streaming render
        # already mounted this turn's widgets, so the display is left untouched.
        self.messages = list(self.current_session.context)

        # Refresh sidebar
        self.query_one(ChatSidebar).refresh_chats()

    def _bind_backend_session(self) -> None:
        """Rebind the backend's AgentSession onto the current live ``Session``.

        Called after every point that makes a ``Session`` current (new-chat, clear,
        resume) so the AgentSession persists the turn — and any agent-driven
        compact/navigate — through the one on-disk log the TUI reads back (E3-ctx /
        D3, AgentSession is the sole persister). Guarded by ``getattr`` so a backend
        without the seam (a test double, or a non-``TauBackend``) is a no-op rather
        than an error.
        """
        binder = getattr(self.current_backend, "bind_session_log", None)
        if binder is not None and self.current_session is not None:
            binder(self.current_session)

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

        # Create new session (writes the header + system message; append-only).
        system_prompt = self.config.get("system_prompt", "You are a helpful assistant.")
        self.current_session = Session.create(
            os.getcwd(),
            model,
            model_config["backend"],
            system_prompt=system_prompt or None,
        )
        self._bind_backend_session()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(ChatDisplay)
        display.clear_messages()

        # Update UI
        self.sub_title = f"{model}"
        self.notify(f"Started new chat with {model}")

        # Refresh sidebar
        self.query_one(ChatSidebar).refresh_chats()

    def action_toggle_sidebar(self):
        """Toggle sidebar visibility."""
        sidebar = self.query_one(ChatSidebar)
        sidebar.styles.display = "none" if sidebar.styles.display == "block" else "block"

    def action_toggle_reasoning(self) -> None:
        """Fold/unfold every reasoning region in the transcript at once.

        A global override of the per-completion behavior (reasoning streams
        expanded then auto-folds when the answer begins): one keypress hides all
        the thinking, or expands it for review. Smart-toggle — if any region is
        open it collapses all, otherwise it expands all — so the key always does
        something visible regardless of the mixed starting states."""
        self.reasoning_collapsed = self._fold_all(self.query(ReasoningRegion), "Reasoning")

    def action_toggle_tools(self) -> None:
        """Fold/unfold every tool box (call + result) in the transcript at once."""
        self.tools_collapsed = self._fold_all(self.query(ToolBox), "Tool output")

    def _fold_all(self, widgets, label: str) -> bool:
        """Collapse all ``widgets`` if any is currently expanded, else expand all.

        Returns the applied collapsed state (also recorded on the reactive for
        the binding's intent). A no-op when there are no such widgets yet."""
        items = list(widgets)
        if not items:
            return False
        target_collapsed = any(not w.collapsed for w in items)
        for w in items:
            w.collapsed = target_collapsed
        self.notify(f"{label} {'collapsed' if target_collapsed else 'expanded'}")
        return target_collapsed

    @staticmethod
    def _aggregate_label(messages: list[dict]) -> str:
        """Conversation-level rollup: total tool calls + summed completion tokens.

        Derived purely from the transcript, so it reads identically for a live
        session and a reloaded one. Wall-clock time is intentionally absent — it
        is not persisted per completion, and we don't fabricate it (Fail-Early).
        Returns an empty string when there's nothing to roll up yet."""
        tools = sum(
            1
            for m in messages
            if m.get("role") == "assistant"
            for b in (m.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "toolCall"
        )
        tokens = sum(
            int((m.get("usage") or {}).get("total_tokens", 0) or 0)
            for m in messages
            if m.get("role") == "assistant"
        )
        if not tools and not tokens:
            return ""
        label = f"{tools} tool" + ("" if tools == 1 else "s")
        return f"{label} · {format_tokens(tokens)} tok"

    def _refresh_subtitle(self) -> None:
        """Set the header subtitle to the model plus the conversation rollup."""
        if not self.current_session:
            return
        agg = self._aggregate_label(self.messages)
        model = str(self.current_session.model)
        self.sub_title = f"{model} · {agg}" if agg else model

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
                lambda m=model_name: self.run_action(f'new_chat("{m}")'),
            )

        # General commands
        yield SystemCommand("Clear Chat", "Clear current conversation", self.action_clear_chat)

        yield SystemCommand("Export Chat", "Export chat to markdown", self.action_export_chat)

        yield SystemCommand(
            "Compact Conversation",
            "Summarize older messages into a checkpoint to free up context",
            self.action_compact,
        )

        yield SystemCommand(
            "Browse conversation tree…",
            "Navigate to an earlier node; optionally summarize the abandoned branch",
            self.action_browse_tree,
        )

        yield SystemCommand(
            "Edit System Prompt",
            "Edit the system prompt for new chats",
            self.action_edit_system_prompt,
        )

        yield SystemCommand(
            "Toggle Reasoning",
            "Collapse/expand all reasoning regions",
            self.action_toggle_reasoning,
        )

        yield SystemCommand(
            "Toggle Tool Output",
            "Collapse/expand all tool call/result boxes",
            self.action_toggle_tools,
        )

    async def action_clear_chat(self):
        """Clear the current conversation, starting a fresh session.

        The store is append-only, so "clear" can't truncate the file in place —
        it begins a new session carrying just the system prompt (the prior
        session stays on disk as its own transcript).
        """
        if not self.current_session:
            return

        system_msg = next((m for m in self.messages if m.get("role") == "system"), None)
        system_prompt = (
            system_msg["content"]
            if system_msg and isinstance(system_msg.get("content"), str)
            else None
        )
        self.current_session = Session.create(
            os.getcwd(),
            self.current_session.model,
            self.current_session.backend,
            system_prompt=system_prompt,
        )
        self._bind_backend_session()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(ChatDisplay)
        display.clear_messages()
        self.query_one(ChatSidebar).refresh_chats()

        self.notify("Chat cleared")

    async def action_export_chat(self):
        """Export current session to markdown."""
        if not self.current_session:
            self.notify("No chat to export", severity="warning")
            return

        # Build markdown
        created = datetime.fromisoformat(self.current_session.header["timestamp"])
        lines = [f"# {self.current_session.display_title()}\n"]
        lines.append(f"Model: {self.current_session.model}\n")
        lines.append(f"Date: {created.astimezone().strftime('%Y-%m-%d %H:%M')}\n")
        lines.append("---\n")

        for msg in self.messages:
            role = msg["role"].capitalize()
            # Persisted assistant/tool messages store content as a block list;
            # flatten to readable text rather than dumping a Python list repr.
            content = _join_text_blocks(msg.get("content", ""))
            lines.append(f"## {role}\n\n{content}\n")

        # Save to file
        export_path = TAU_DIR / "exports"
        export_path.mkdir(parents=True, exist_ok=True)

        filename = f"chat_{self.current_session.id}.md"
        file_path = export_path / filename
        file_path.write_text("\n".join(lines))

        self.notify(f"Exported to {file_path}")

    async def action_compact(self):
        """Compact the current conversation into a summary checkpoint.

        Summarizes the older messages via the model and replaces them with a
        single checkpoint, freeing context for the conversation to continue.
        Operates on ``self.messages`` — the live list sent to the model — then
        re-renders. The session file keeps the full transcript (append-only, no
        rewrite); compaction is a runtime context optimization on the working
        list, so a resumed session still has its complete history.
        """
        if not self.current_session:
            self.notify("No chat to compact", severity="warning")
            return

        backend = self.current_backend
        if not hasattr(backend, "compact_messages"):
            self.notify("This backend does not support compaction", severity="warning")
            return

        self.notify("Compacting conversation…")
        self.sub_title = "Compacting…"
        before = len(self.messages)
        try:
            new_messages = await backend.compact_messages(self.messages)
        except Exception as e:
            self.notify(f"Compaction failed: {e}", severity="error")
            self.log.error(f"Compaction failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        if new_messages is None:
            self.notify("Nothing to compact yet")
            self._refresh_subtitle()
            return

        self.messages = new_messages
        # reload_messages lives on the ChatDisplay widget, not the app.
        await self.query_one(ChatDisplay).reload_messages(self.messages)
        self._refresh_subtitle()
        self.notify(f"Compacted {before} → {len(new_messages)} messages")

    @work
    async def action_browse_tree(self) -> None:
        """Open the tree-browser and act on the chosen branch point (§3).

        Runs as a worker so it can ``push_screen_wait`` the three modal steps
        (browse → mode → optional custom instructions). Operates on the LIVE
        ``current_session`` — the TUI owns persistence (§2.6) — building a
        ``ConversationTree`` over its entries and handing the picked node to
        ``backend.navigate_tree``, which appends the ``navigate``/``branch_summary``
        entry and returns the post-navigate context. Re-renders through the same
        path ``action_compact`` uses (§3.4): swap ``self.messages`` + reload.
        """
        session = self.current_session
        if session is None:
            self.notify("No conversation to browse", severity="warning")
            return
        # Bind the method up front: it survives the intervening ``await``s (unlike a
        # hasattr-narrowed local) and is ``None`` for a backend that lacks it.
        navigate_tree = getattr(self.current_backend, "navigate_tree", None)
        if navigate_tree is None:
            self.notify("This backend does not support tree navigation", severity="warning")
            return

        roots = ConversationTree(session.entries(), session.cursor).tree()
        if not roots:
            self.notify("Conversation tree is empty", severity="warning")
            return

        target_id = await self.push_screen_wait(SessionTreeModal(roots))
        if target_id is None:
            return
        if target_id == session.cursor:
            self.notify("Already at that node")
            return

        mode = await self.push_screen_wait(TreeModeModal())
        if mode is None:
            return

        custom_instructions: Optional[str] = None
        if mode == "custom":
            custom_instructions = await self.push_screen_wait(TreeCustomInstructionsModal())
            if custom_instructions is None:
                return

        summarize = mode in ("summarize", "custom")
        self.sub_title = "Summarizing branch…" if summarize else "Navigating tree…"
        try:
            new_messages = await navigate_tree(
                session,
                target_id,
                summarize=summarize,
                custom_instructions=custom_instructions,
            )
        except Exception as e:
            self.notify(f"Tree navigation failed: {e}", severity="error")
            self.log.error(f"Tree navigation failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        self.messages = new_messages
        await self.query_one(ChatDisplay).reload_messages(self.messages)
        self._refresh_subtitle()
        self.notify(
            "Summarized and moved to selected node" if summarize else "Moved to selected node"
        )

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
        """Handle session selection from sidebar."""
        try:
            # Load the selected session
            session = Session.load(message.chat_path)

            # Get model config and create backend
            model_config = self.config["models"].get(session.model)
            if not model_config:
                self.notify(f"Model {session.model} not found in config", severity="error")
                return

            self.current_backend = create_backend(model_config)
            self.current_session = session
            self._bind_backend_session()
            # Seed from the active-path context (cursor + compaction/branch splices),
            # NOT the raw linear fold — else a resumed compacted/branched session
            # would render its dropped history and hide the summary (§2.6).
            self.messages = list(session.context)

            # Reload the display, reconstructing exchanges from the persisted
            # flat message list so a reloaded session looks like a freshly-streamed
            # one (collapsed exchanges, folded tool boxes, promoted final answer).
            display = self.query_one(ChatDisplay)
            await display.reload_messages(self.messages)

            # Update UI — model + the reloaded conversation's rollup.
            self._refresh_subtitle()
            self.notify(f"Loaded session: {session.display_title()}")

        except Exception as e:
            self.notify(f"Error loading session: {str(e)}", severity="error")
            self.log.error(f"Failed to load session: {e}", exc_info=True)


if __name__ == "__main__":
    app = Parley()
    app.run()
