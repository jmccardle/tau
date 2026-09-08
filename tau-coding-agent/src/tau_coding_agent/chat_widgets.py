"""Collapsible chat widgets: reasoning regions, paired tool call+result boxes,
and exchange grouping.

Built on Textual's ``Collapsible`` (validated for deep nesting with no interior
scrollbars in ``tests/test_nested_collapsible_spike.py``). Collapse/expand is
the framework's reactive→CSS-display mechanism — widgets mount once as content
streams in; toggling never re-mounts the DOM.

Design (from the reasoning/TUI discussion):
- ``ReasoningRegion`` — a completion's reasoning, streamed live, collapsible and
  rendered distinctly from the answer (pi renders reasoning dim+italic).
- ``ToolBox`` — a tool call paired with its result in ONE collapsible: the
  collapsed title is the one-line call signature; the body holds args + result.
- ``ExchangeBox`` — groups one user→answer exchange's steps under a summary line
  ("N tools · X tok · M:SS"); the final answer streams inside it.

These have NO dependency on the TauApp app module, so they import cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Callable

from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Input, TextArea, Static, Button
from textual.widgets.markdown import MarkdownStream
from textual import events, work
from textual.binding import Binding
from tau_agent_core.attachments import AttachmentCompletions
from tau_agent_core.commands import ArgumentCompletions, CommandCompletions
import os
from datetime import datetime, timedelta
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll, Vertical
from textual.worker import get_current_worker
from tau_agent_core.session_catalog import SessionCatalog, SessionInfo
from textual.message import Message


ROLE_LABELS: dict[str, str] = {
    "pending": "…",
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "toolCall": "Tool call",
    "toolResult": "Tool result",
    "custom": "Extension",
    "interactive": "User",  # a human, but not the one at THIS frontend
    "rpc": "RPC",
    "extension": "Extension",
    "bus": "Bus",
    "timer": "Timer",
    "webhook": "Webhook",
    "voice": "Voice",
    "agent": "Sub-agent",
    "compaction": "Compaction",
    "branch_summary": "Branch summary",
    "navigate": "Navigate",
    "elide": "Elide",
    "customEntry": "Entry",
}


ENTER_KEY_CONFIG_KEY = "enter_key"


ENTER_KEY_MODES = ("newline", "submit")


DEFAULT_ENTER_KEY_MODE = "newline"


NEWLINE_KEYS_IN_SUBMIT_MODE = ("shift+enter", "ctrl+j")


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

    ``source`` says what the body text IS — an assistant's markdown, or verbatim
    line-oriented output (a tool result, a traceback, what a user typed). It is
    required, because only the caller knows, and the two are rendered differently
    (see :class:`MarkdownLineFormatter`). It is deliberately NOT derived from
    ``role``: ``set_role`` retypes a box in place, and a box's text does not
    change kind when its label does.
    """

    def __init__(
        self,
        role: str,
        content: str = "",
        subtitle: str = "",
        *,
        source: ContentSource,
    ):
        super().__init__(classes=f"chat-message box-{role}")
        self.role = role
        self._content = content
        self._subtitle = subtitle
        self._source = source
        self._reasoning: ReasoningRegion | None = None
        self._tool_boxes: dict[str, ToolBox] = {}
        self._deferred_children: list[tuple[str, Widget]] = []
        self._stream: MarkdownStream | None = None
        self._formatter = MarkdownLineFormatter(self._source)

    def _format(self, content: str) -> str:
        """Format a WHOLE body, and re-seat the streaming formatter to match.

        Every caller of this method replaces the entire document (``on_mount``
        catching up a pre-mount buffer, ``update_content`` swapping the body), so
        the incremental state has to restart from the same text — otherwise a
        delta appended afterwards would continue from a fence state belonging to
        text that is no longer there.
        """
        self._formatter = MarkdownLineFormatter(self._source)
        return self._formatter.feed(content)

    def compose(self) -> ComposeResult:
        self._reasoning_slot = Vertical(classes="message-reasoning")
        yield self._reasoning_slot
        md = Markdown("", classes="message-content")
        self._md_widget = md
        yield md
        self._tools_slot = Vertical(classes="message-tools")
        yield self._tools_slot

    def on_mount(self) -> None:
        self.border_title = ROLE_LABELS.get(self.role, self.role.capitalize())
        if self._content:
            self._md_widget.append(self._format(self._content))
        if self._subtitle:
            self.border_subtitle = self._subtitle
        for slot, widget in self._deferred_children:
            getattr(self, slot).mount(widget)
        self._deferred_children.clear()

    def _mount_lazy(self, slot: str, widget: Widget) -> None:
        """Mount a lazily-created child into one of ``compose()``'s slots.

        Buffers the child when ``compose()`` has not run yet; :meth:`on_mount`
        flushes the buffer. Before this existed, both callers raised
        ``AttributeError: 'MessageBox' object has no attribute '_reasoning_slot'``
        on the first delta of a turn — and because ``ensure_reasoning`` had
        already assigned ``self._reasoning``, every later call took the
        already-created branch and handed back a region that was never mounted,
        so the whole turn's reasoning accumulated into a widget nobody could see.
        """
        container = getattr(self, slot, None)
        if container is None:
            self._deferred_children.append((slot, widget))
            return
        container.mount(widget)

    # -- text body -----------------------------------------------------------

    def set_role(self, role: str) -> None:
        """Resolve/retype this box in place (e.g. pending → assistant)."""
        self.remove_class(f"box-{self.role}")
        self.role = role
        self.add_class(f"box-{role}")
        self.border_title = ROLE_LABELS.get(role, role.capitalize())

    def update_content(self, content: str) -> None:
        """Replace the text body in place (used for streaming text)."""
        if content == self._content:
            return
        self._content = content
        if hasattr(self, "_md_widget"):
            self._md_widget.update(self._format(content))

    async def append_content_delta(self, delta: str) -> None:
        """Stream one delta into the text body without a full document rebuild.

        Uses ``Markdown.get_stream``/``MarkdownStream.write`` (Textual 8.2.7),
        appending instead of the reparse+remount-everything ``update_content``/
        ``Markdown.update()`` does. Each delta is formatted by the box's
        :class:`MarkdownLineFormatter`, which carries fenced-code-block state
        forward across calls; feeding it one delta at a time therefore produces
        the identical document a whole-text ``update_content(self._content)``
        would, even when a delta splits mid newline or mid fence marker.

        ``self._content`` is kept in sync on every call (not just a throttled
        tick) so ``content_text``/``update_content``'s equality guard stay
        correct whether or not this delta was actually streamed yet. Mirrors
        ``update_content``'s existing ``hasattr`` gate: a delta that arrives
        before ``compose()`` has run is accumulated into ``self._content``
        only -- ``on_mount`` catches the full buffered text up via ``append()``
        once the widget mounts (not ``update()`` -- see its comment), and
        streaming resumes from there.
        """
        if not delta:
            return
        self._content += delta
        if not hasattr(self, "_md_widget"):
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md_widget)
        await self._stream.write(self._formatter.feed(delta))

    async def finish_stream(self) -> None:
        """Stop this box's open content stream, if any.

        Called at every point the active step stops being the streaming target
        (``_flush``, ``finalize_exchange``) so no ``MarkdownStream`` background
        task is left running once the box may be collapsed, promoted from, or
        removed. Safe to call when nothing was ever streamed (idempotent no-op).
        """
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

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
        ``set_text``/``append`` on the returned region immediately -- including
        before this box has composed, in which case :meth:`_mount_lazy` holds the
        region until ``on_mount``.
        """
        if self._reasoning is None:
            self._reasoning = ReasoningRegion()
            self._mount_lazy("_reasoning_slot", self._reasoning)
        return self._reasoning

    def add_tool_call(self, name: str, arguments: object, tool_call_id: str = "") -> ToolBox:
        """Append a tool call as a child ToolBox, tracked by id for its result.

        ``ToolBox`` holds a result written before it is opened, so a call and its
        result arriving in the same synchronous burst are both rendered even when
        this box has not composed yet.
        """
        box = ToolBox(name, arguments, tool_call_id)
        if tool_call_id:
            self._tool_boxes[tool_call_id] = box
        self._mount_lazy("_tools_slot", box)
        return box

    async def add_tool_call_async(
        self, name: str, arguments: object, tool_call_id: str = ""
    ) -> ToolBox:
        """Like :meth:`add_tool_call` but awaits the ToolBox mount.

        The reload path folds a tool *result* into this box immediately after the
        next persisted message; awaiting the mount here keeps that write on the
        direct path rather than through ``ToolBox``'s pre-mount buffer. Every
        reload caller has already awaited the step's own mount, so the slot
        exists and this really does await. The live path is network-paced and
        uses the fire-and-forget variant."""
        box = ToolBox(name, arguments, tool_call_id)
        if tool_call_id:
            self._tool_boxes[tool_call_id] = box
        slot = getattr(self, "_tools_slot", None)
        if slot is None:
            self._deferred_children.append(("_tools_slot", box))
            return box
        await slot.mount(box)
        return box

    def set_tool_result(
        self,
        tool_call_id: str,
        result_text: str,
        is_error: bool = False,
        *,
        blocked: bool = False,
        blocked_by: str | None = None,
    ) -> bool:
        """Fold a tool result into its matching ToolBox. Returns ``False`` if no
        box matches the id — the caller decides what to do, nothing is fabricated.

        ``blocked``/``blocked_by`` mark an extension VETO (S50) so the ToolBox
        renders "⛔ blocked by <ext>" instead of a generic error."""
        box = self._tool_boxes.get(tool_call_id)
        if box is None:
            return False
        box.set_result(result_text, is_error, blocked=blocked, blocked_by=blocked_by)
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
        self.chat_ref = info.ref
        self.info = info

    def on_click(self):
        """Handle click to load this session."""
        self.post_message(ChatSelected(self.chat_ref))


class ChatSelected(Message):
    """Message sent when a session is selected from the sidebar."""

    def __init__(self, chat_ref: str):
        super().__init__()
        self.chat_ref = chat_ref


class ReclaimPending(Message):
    """alt+up: put the pending steering buffer back in the editor.

    Reference: docs/TUI-STEERING.md §4. A message rather than a direct call
    because this variant of the gesture works with a draft in the box, and
    combining the two texts is the app's job — the editor knows what it holds,
    the app knows what is pending, and only one of them can be told to do it.
    """


class ChatSidebar(Container):
    """Sidebar showing this directory's recent sessions, grouped by date."""

    _GROUP_LIMIT = 10

    def __init__(self, catalog: SessionCatalog):
        super().__init__(id="sidebar")
        self.catalog = catalog
        self.sessions: list[SessionInfo] = []
        self._render_pending = False

    def compose(self) -> ComposeResult:
        """Compose sidebar contents."""
        yield Static("τ", classes="sidebar-title")
        yield Button("+ New Chat", id="new-chat-button", variant="primary")

        with VerticalScroll(id="chat-list"):
            # Will be populated dynamically
            pass

    def refresh_chats(self) -> None:
        """Refresh the session list (cwd-scoped — §8 of the redesign).

        ``catalog.list()`` is a synchronous call that can be a genuine blocking
        network round trip: the JMFTS-backed catalog pages over EVERY
        ``tau:conversation`` root in the whole instance and filters by cwd
        client-side (measured live: 1,762 roots, 18 sequential HTTP requests,
        ~154ms — and it only grows). Calling it directly here, on the event
        loop, used to freeze the entire TUI for that long. It is dispatched to
        a thread worker instead (Textual's own rule: "if the await might take
        more than ~50ms, use a worker").

        This method itself stays synchronous and returns immediately — it only
        *starts* the worker. Callers that must observe the refreshed list
        before proceeding (chiefly tests) should await it settling — see
        ``tests/conftest.py``'s ``wait_for_workers_settled``, not the bare
        ``app.workers.wait_for_complete()``: because this worker is
        ``exclusive``, a still-running previous refresh gets cancelled rather
        than awaited to completion, and ``Worker.wait()`` raises
        ``WorkerCancelled`` for that — a benign, expected outcome of this
        method's own staleness guard, not a failure a caller should have to
        handle case-by-case.
        """
        self._refresh_chats_worker()

    @work(thread=True, exclusive=True, group="sidebar-refresh")
    def _refresh_chats_worker(self) -> None:
        """The blocking fetch, off the event loop.

        ``exclusive=True`` cancels any still-running refresh from this same
        widget when a newer one starts (turns can end back-to-back faster than
        one listing round trip). That cancellation only flips
        ``worker.is_cancelled`` — a thread already blocked inside
        ``catalog.list()`` keeps running to completion regardless — so the
        result is checked for staleness before it is applied. Without that
        check, a slow superseded fetch could land after a faster newer one and
        overwrite the sidebar with stale data: a freeze traded for a lie.
        """
        worker = get_current_worker()
        sessions = self.catalog.list(os.getcwd())
        if worker.is_cancelled:
            return
        self.app.call_from_thread(self._apply_sessions, sessions)

    def _apply_sessions(self, sessions: list[SessionInfo]) -> None:
        """Runs on the UI thread via ``call_from_thread`` — the only place
        ``self.sessions`` is written and the chat list re-rendered, so widget
        mutation never happens off the main thread.

        While the sidebar is collapsed (``display: none``, toggled by
        ``action_toggle_sidebar``), ``_render_chat_list`` is skipped rather
        than run into a DOM nobody can see: a catalog fetch started before
        collapsing (mount-time, or a stale one still in flight) can land at
        an arbitrary later moment, and its render cost does not go away just
        because the widget is hidden — Textual still pays it in full,
        synchronously, on the main thread (confirmed live via py-spy: an
        unbatched mount loop over a few hundred sessions pinned the event
        loop — and with it every keystroke — for 8+ seconds). The data is
        still recorded so ``ensure_rendered`` can catch up on expand.
        """
        self.sessions = sessions
        if self.styles.display == "none":
            self._render_pending = True
            return
        self._render_chat_list()

    def ensure_rendered(self) -> None:
        """Catch up a render that ``_apply_sessions`` deferred while collapsed.

        Called by ``action_toggle_sidebar`` when the sidebar becomes visible
        again — the counterpart to the skip in ``_apply_sessions``.
        """
        if self._render_pending:
            self._render_pending = False
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

        widgets: list[Widget] = []
        if today:
            widgets.append(Static("[bold]Today[/bold]", classes="chat-group-header"))
            widgets.extend(ChatListItem(info) for info in today[: self._GROUP_LIMIT])

        if yesterday:
            widgets.append(Static("[bold]Yesterday[/bold]", classes="chat-group-header"))
            widgets.extend(ChatListItem(info) for info in yesterday[: self._GROUP_LIMIT])

        if older:
            widgets.append(Static("[bold]Older[/bold]", classes="chat-group-header"))
            widgets.extend(ChatListItem(info) for info in older[: self._GROUP_LIMIT])

        chat_list.mount(*widgets)

    def on_mount(self):
        """Refresh sessions when mounted."""
        self.refresh_chats()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "new-chat-button":
            await self.app.run_action("new_chat")


class ChatInput(TextArea):
    """Custom input with multiline support and history navigation.

    Enter and Ctrl+J trade places according to :data:`ENTER_KEY_CONFIG_KEY`; see
    :meth:`on_key` for why the swap lives in a key handler rather than in
    ``BINDINGS``, and ``docs/ENTER-KEY.md`` for what the terminal can and cannot
    tell us about a modifier on Enter.
    """

    BINDINGS = [
        Binding("ctrl+j", "submit", "Send", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_history: list[str] = []
        self.command_history_index = -1
        self.current_draft = ""
        self.reclaim_pending: Callable[[], str | None] | None = None
        self.enter_key_mode: Callable[[], str] | None = None
        self.command_completions: Callable[[str], CommandCompletions | None] | None = None
        self.argument_completions: Callable[[str], ArgumentCompletions | None] | None = None
        self.attachment_completions: Callable[[str, int], AttachmentCompletions | None] | None = (
            None
        )
        self._completion_prefix = ""
        self._completion_index: int | None = None
        self._completion_inserted: str | None = None
        self._completion_cursor: int = 0

    @property
    def completion_index(self) -> int | None:
        """Which candidate the running Tab cycle has inserted, or ``None``.

        Read by the app to mark the row in :class:`CommandPopup`. It is set BEFORE
        the text is replaced, so the ``TextArea.Changed`` the replacement posts
        already sees the new value.

        Guarded by the same test :meth:`_complete` uses to decide whether a cycle
        is still running, rather than by the raw field: the marker must vanish the
        moment the user types a character, and typing does not run any code of
        ours that could clear the field.
        """
        if self._completion_inserted is not None and self.text == self._completion_inserted:
            return self._completion_index
        return None

    @property
    def cursor_offset(self) -> int:
        """The cursor's character offset into :attr:`text`.

        ``TextArea`` counts in ``(row, column)``; the attachment vocabulary is
        defined on the flat string, because a file reference is a word and words
        do not know about rows. This is the translation between the two.

        Computed from ``document.lines`` and ``document.newline`` rather than from
        ``Document.get_index_from_location``, which is not on the ``DocumentBase``
        the ``document`` property is typed as.
        """
        row, column = self.cursor_location
        separator = len(self.document.newline)
        lines = self.document.lines
        return sum(len(line) + separator for line in lines[:row]) + column

    def _location_of_offset(self, offset: int) -> tuple[int, int]:
        """The ``(row, column)`` for a character offset into :attr:`text`.

        The inverse of :attr:`cursor_offset`. An offset past the end of the text
        clamps to the end, which is where a caller that computed it from a string
        it just built would want the cursor anyway.
        """
        separator = len(self.document.newline)
        remaining = offset
        lines = self.document.lines
        for row, line in enumerate(lines):
            if remaining <= len(line):
                return row, remaining
            remaining -= len(line) + separator
        last = max(len(lines) - 1, 0)
        return last, len(lines[last]) if lines else 0

    def _complete(self) -> bool:
        """Insert the next candidate — a path, an argument value, or a command. True
        if it did.

        A press with a cycle already running advances to the next candidate and
        wraps at the end; a press without one starts a cycle at the first. The
        cycle is identified by the editor still holding exactly what the previous
        press wrote — a user who typed a character since then gets a fresh cycle
        from the new prefix, which is what they meant by typing it.

        Three vocabularies, asked in the order the text decides, and at most one can
        apply. A ``@…`` the cursor is inside goes first (docs/FILE-ATTACHMENTS.md
        §3), which is what makes ``/fork @notes.txt`` complete the path rather than
        re-completing the command. Then the argument value, which exists only once a
        space follows a word that names a command. Then the command word itself,
        which is what is left.
        """
        cycling = self._completion_inserted is not None and self.text == self._completion_inserted
        source = self._completion_prefix if cycling else self.text
        cursor = self._completion_cursor if cycling else self.cursor_offset

        if self._complete_attachment(source, cursor, cycling):
            return True
        if self._complete_argument(source, cycling):
            return True
        return self._complete_command(source, cycling)

    def _complete_argument(self, source: str, cycling: bool) -> bool:
        """Replace a command's argument with the next legal value. True if it did.

        Replaces the SPAN the core named
        (:class:`~tau_agent_core.commands.ArgumentSlot`) rather than the whole line,
        so the command word survives — which is the difference from
        :meth:`_complete_command`, and the reason a Tab on ``/model loc`` no longer
        throws ``loc`` away.

        Args:
            source: The text the cycle started from.
            cycling: Whether a Tab cycle is already running.

        Returns:
            True when a value was inserted.
        """
        if self.argument_completions is None:
            return False
        completions = self.argument_completions(source)
        if completions is None or not completions.matches:
            return False

        if cycling and self._completion_index is not None:
            index = (self._completion_index + 1) % len(completions.matches)
        else:
            index = 0

        slot = completions.slot
        text = source[: slot.start] + completions.matches[index].value + " " + source[slot.end :]
        self._completion_prefix = source
        self._completion_index = index
        self._completion_inserted = text
        self.text = text
        self.move_cursor(self.document.end)

        if len(completions.matches) == 1:
            self._reset_completion()
        return True

    def _complete_attachment(self, source: str, cursor: int, cycling: bool) -> bool:
        """Replace the ``@…`` the cursor is inside with the next candidate path.

        Unlike a command, a reference can sit anywhere in the line, so this
        replaces a SPAN rather than the whole text and leaves the cursor just
        after what it inserted — the human is usually mid-sentence.

        A directory is inserted without a trailing space, because the next thing
        they want is to keep completing into it; a file gets one, like a command.

        Args:
            source: The text the cycle started from (the editor's current text
                when no cycle is running).
            cursor: The cursor offset within ``source``.
            cycling: Whether a Tab cycle is already running.

        Returns:
            True when a candidate was inserted.
        """
        if self.attachment_completions is None:
            return False
        completions = self.attachment_completions(source, cursor)
        if completions is None or not completions.matches:
            return False

        if cycling and self._completion_index is not None:
            index = (self._completion_index + 1) % len(completions.matches)
        else:
            index = 0

        match = completions.matches[index]
        insert = f"@{match.name}" if match.is_dir else f"@{match.name} "
        text = source[: completions.start] + insert + source[completions.end :]

        self._completion_prefix = source
        self._completion_cursor = cursor
        self._completion_index = index
        self._completion_inserted = text
        self.text = text
        self.move_cursor(self._location_of_offset(completions.start + len(insert)))

        if len(completions.matches) == 1:
            self._reset_completion()
        return True

    def _complete_command(self, source: str, cycling: bool) -> bool:
        """Insert the next candidate command. True if it did.

        The inserted text keeps its trailing space (pi's ``applyCompletion``, at
        ``tui/src/autocomplete.ts:393``): a command taking arguments is then ready
        for them, and one taking none resolves identically, because
        :func:`~tau_agent_core.commands.parse_command` strips.

        Only the command WORD is replaced. Anything already typed after it is kept
        verbatim, because a half-typed command with arguments after it
        (``/nam my session``) is a line whose word needs fixing and whose argument
        does not — and rewriting the whole editor would silently discard the second.

        Args:
            source: The text the cycle started from.
            cycling: Whether a Tab cycle is already running.

        Returns:
            True when a candidate was inserted.
        """
        if self.command_completions is None:
            return False

        completions = self.command_completions(source)
        if completions is None or not completions.matches:
            self._reset_completion()
            return False

        if cycling and self._completion_index is not None:
            index = (self._completion_index + 1) % len(completions.matches)
        else:
            index = 0

        lead = len(source) - len(source.lstrip())
        rest = source.lstrip()[1 + len(completions.token) :].lstrip(" ")
        inserted = source[:lead] + f"/{completions.matches[index].name} " + rest
        self._completion_prefix = source
        self._completion_index = index
        self._completion_inserted = inserted
        self.text = inserted
        self.move_cursor(self._location_of_offset(len(inserted) - len(rest)))
        return True

    def _reset_completion(self) -> None:
        """Forget the running Tab cycle, so the next Tab starts a new one."""
        self._completion_prefix = ""
        self._completion_index = None
        self._completion_inserted = None
        self._completion_cursor = 0

    def _enter_sends(self) -> bool:
        """True when Enter submits and Ctrl+J inserts a line break.

        Unconfigured — a ``ChatInput`` built without the app, which is how its
        own unit tests build it — is τ's documented default rather than an error:
        this reports a *setting*, and :data:`DEFAULT_ENTER_KEY_MODE` is what the
        setting is when nobody set it. The Fail-Early check on a misspelled value
        is one layer up, in :meth:`TauApp._configured_enter_key_mode`, where there
        is a config to be wrong about.
        """
        if self.enter_key_mode is None:
            return DEFAULT_ENTER_KEY_MODE == "submit"
        return self.enter_key_mode() == "submit"

    def action_submit(self):
        """Submit the current message."""
        text = self.text.strip()
        if text:
            self.post_message(Input.Submitted(self, text))

    def _insert_newline(self) -> None:
        """Insert a line break at the cursor, as Enter does in ``"newline"`` mode.

        ``TextArea._on_key`` is what normally does this, and in ``"submit"`` mode
        it is exactly what we have suppressed, so the two newline keys have to do
        it themselves. ``_replace_via_keyboard`` rather than ``insert`` because it
        is the method TextArea's own Enter uses: it respects a selection and the
        read-only flag, and it records a single undo step.
        """
        start, end = self.selection
        self._replace_via_keyboard("\n", start, end)

    def _try_reclaim(self) -> bool:
        """Pull pending steering text back into the editor. True if it did.

        The gesture is Up on an EMPTY editor: pending input is the newest thing
        the user wrote, so it sits one step in front of the history that Up
        otherwise walks. Requiring the editor to be empty is what keeps the two
        apart without a mode — with a draft in the box, Up still means history,
        and reclaiming would have overwritten the draft.
        """
        if self.text or self.reclaim_pending is None:
            return False
        text = self.reclaim_pending()
        if not text:
            return False
        self.text = text
        self.move_cursor(self.document.end)
        return True

    def on_key(self, event: events.Key) -> None:
        """Handle history navigation, and the Enter/Ctrl+J swap.

        The swap is here and not in ``BINDINGS`` because a ``Binding("enter", …)``
        on a ``TextArea`` never fires. ``TextArea._on_key`` claims Enter, inserts
        ``"\\n"``, and calls ``event.stop()``; Textual checks non-priority bindings
        only once the key has bubbled up to the App, which a stopped key does not
        do. ``priority=True`` would fire, but it is checked before the event is
        forwarded to *any* widget, so it would take Enter away from every other
        editor on the screen too.

        A handler is the right seam instead of a workaround. Textual walks the MRO
        for handlers and takes ``_on_key`` over ``on_key`` per class, so the order
        is ``ChatInput.on_key`` then ``TextArea._on_key``, and ``prevent_default``
        ends the walk. This method therefore gets Enter first and decides.
        """

        if event.key == "tab":
            if self._complete():
                event.prevent_default()
                event.stop()
            return

        if event.key == "enter" and self._enter_sends():
            # Before ``TextArea._on_key`` can insert the line break.
            event.prevent_default()
            event.stop()
            self.action_submit()
            return

        if event.key in NEWLINE_KEYS_IN_SUBMIT_MODE and self._enter_sends():
            event.prevent_default()
            event.stop()
            self._insert_newline()
            return

        if event.key == "alt+up":
            self.post_message(ReclaimPending())
            event.prevent_default()
            return

        # Up/Down for history (only when on first/last line)
        if event.key == "up":
            if self._try_reclaim():
                event.prevent_default()
                return
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


def _extension_display_name(blocked_by: str | None) -> str:
    """A short, readable name for the vetoing extension (S50).

    The runner attributes a veto by its bucket LABEL — a file path for a
    discovered/`-e` extension (``…/30_permission_gate.py``) or a ``module:qualname``
    for an inline factory. Show the file stem when it looks like a path, else the
    label verbatim; ``None`` (an unattributed block) reads as the honest
    ``"extension"`` rather than a fabricated name."""
    if not blocked_by:
        return "extension"
    if blocked_by.endswith(".py") or "/" in blocked_by:
        return Path(blocked_by).stem
    return blocked_by


ContentSource = Literal["markdown", "verbatim"]


class MarkdownLineFormatter:
    """Prepare a message body for a Textual ``Markdown`` widget, according to
    what the body's SOURCE is.

    A ``"verbatim"`` body — a tool result, an error listing, shell output — is not
    markdown, and markdown collapses each of its lone newlines into a space. Every
    newline is doubled so each line survives as its own paragraph. (Not inside a
    fenced code block, where a newline is already literal: doing it there is what
    used to put a blank line between every line of every code block.)

    A ``"markdown"`` body — an assistant's answer — is passed through untouched.
    Doubling there is actively wrong: a model that hard-wraps its prose at 80
    columns got a blank line between every wrapped line, so one sentence rendered
    as several paragraphs. The split is by SOURCE and never by inspecting the text,
    because the only text-level test available is "does this look hard-wrapped",
    and a heuristic that guesses wrong rejoins two lines of a tool result — the one
    thing the doubling exists to protect. The caller always knows which it has.

    Stateful by necessity: whether a newline is inside a fence depends on every
    line before it. The state is carried across :meth:`feed` calls, so a streaming
    caller can hand over one delta at a time and get exactly the document a single
    whole-text call would produce, no matter where the deltas split — including
    mid-newline. That is the property ``MessageBox.append_content_delta`` relies on.
    A ``"markdown"`` formatter tracks fences too, so :attr:`in_fence` reads the same
    either way even though nothing is rewritten.

    Fence detection is a line whose first non-space characters are ``` — an opener
    and its closer are not required to match, which is looser than CommonMark but
    is what a streamed body actually contains.
    """

    __slots__ = ("_in_fence", "_line", "_source")

    def __init__(self, source: ContentSource) -> None:
        self._source = source
        self._in_fence = False
        self._line = ""

    @property
    def source(self) -> ContentSource:
        """What this formatter was told the body is."""
        return self._source

    @property
    def in_fence(self) -> bool:
        """Whether the next character belongs to an open fenced code block."""
        return self._in_fence

    def feed(self, text: str) -> str:
        """Format the next fragment, continuing from the state so far."""
        out: list[str] = []
        double = self._source == "verbatim"
        for char in text:
            if char != "\n":
                out.append(char)
                self._line += char
                continue
            is_delimiter = self._line.lstrip().startswith("```")
            out.append("\n" if (not double or self._in_fence or is_delimiter) else "\n\n")
            if is_delimiter:
                self._in_fence = not self._in_fence
            self._line = ""
        return "".join(out)


def format_tool_summary(name: str, arguments: object) -> str:
    """A one-line call signature, ``name(key=val, …)``, truncated for the
    collapsed title row. Values are shortened individually and the whole
    argument list is capped so the line stays scannable."""
    inner = ""
    if isinstance(arguments, dict) and arguments:
        parts = []
        for key, value in arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = text.replace("\n", " ")
            if len(text) > 40:
                text = text[:39] + "…"
            parts.append(f"{key}={text}")
        inner = ", ".join(parts)
        if len(inner) > 60:
            inner = inner[:59] + "…"
    return f"{name}({inner})"


def format_tokens(n: int) -> str:
    """102700 → '102.7k'; small counts stay exact."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_duration(seconds: float) -> str:
    """186.0 → '3:06'."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def format_telemetry(extra: dict[str, Any]) -> str | None:
    """Render one telemetry line from a completion's ``usage["extra"]`` dict (G4).

    The single formatter shared by the TUI exchange summary and
    ``examples/70_telemetry.py`` (which imports this) — one implementation, so the
    live readout and the example demo can never drift. ``extra`` is the
    server-reported, non-portable per-completion telemetry τ folds onto
    ``Usage.extra`` (llama.cpp's ``timings`` block + τ's own JSON-repair count);
    pass ``usage.get("extra") or {}``.

    Renders (each only when its source figure is actually present):

    * effective decode speed — ``timings.predicted_per_second`` as ``N.N t/s``;
    * the tool-arg JSON-repair count, when the completion reported one
      (``repairs``);
    * the number of tool calls dropped because the stream ended mid-``arguments``
      (``dropped_partial_tool_calls``), when the completion dropped any;
    * the forced-token share ``n_ff_total / predicted_n`` as ``forced=NN%`` — but
      ONLY when ``n_ff_total`` is present. Stock llama.cpp builds never send it (a
      jump-forward-fork-only field); omitting it is the honest move, never a
      fabricated ``0%``.

    Returns ``None`` when the completion carried no telemetry at all (no
    ``timings``, no ``repairs``) — the caller shows the summary exactly as it would
    without telemetry, never a stale or fabricated reading (Fail-Early).
    """
    timings = extra.get("timings") or {}
    parts: list[str] = []

    predicted_per_second = timings.get("predicted_per_second")
    if isinstance(predicted_per_second, (int, float)):
        parts.append(f"{predicted_per_second:.1f} t/s")

    repairs = extra.get("repairs")
    if isinstance(repairs, int):
        parts.append(f"repairs={repairs}")

    dropped = extra.get("dropped_partial_tool_calls")
    if isinstance(dropped, int):
        parts.append(f"dropped={dropped}")

    n_ff_total = timings.get("n_ff_total")
    predicted_n = timings.get("predicted_n")
    if isinstance(n_ff_total, (int, float)) and isinstance(predicted_n, (int, float)):
        if predicted_n > 0:
            parts.append(f"forced={n_ff_total / predicted_n:.0%}")

    return " · ".join(parts) if parts else None


class QuietCollapsible(Collapsible):
    """A ``Collapsible`` that does not scroll its container when it folds.

    Textual's own (8.2.7) ends ``_watch_collapsed`` with::

        if self.is_mounted:
            self.call_after_refresh(self.scroll_visible)

    which is right for a page of collapsibles a reader is clicking through, and
    wrong for a transcript: **these boxes fold without anyone asking.** A finished
    exchange folds behind its summary when a turn ends, a reasoning region folds
    when the answer starts, and a reload folds one of each per rebuilt span. Every
    one of those scrolled the transcript to put that box on screen — dragging a
    reader who was deliberately reading history, on the model's schedule, and
    landing a freshly reloaded conversation three rows short of its newest
    message.

    So the scroll is dropped and the two state-keeping lines are kept verbatim.
    The messages still post, so anything that wants to react to a fold still can;
    what changes is that the transcript decides where the transcript scrolls
    (``ChatDisplay._size_updated``, ``_finish_build``), which is the same
    ownership rule the window already follows.

    Keyboard focus is unaffected: ``Screen.set_focus`` brings a widget into view
    with ``scroll_to_center``, not with ``scroll_visible`` (verified, textual
    8.2.7), so tabbing to an off-screen box still scrolls to it.
    """

    def _watch_collapsed(self, collapsed: bool) -> None:
        self._update_collapsed(collapsed)
        if self.collapsed:
            self.post_message(self.Collapsed(self))
        else:
            self.post_message(self.Expanded(self))


class ReasoningRegion(QuietCollapsible):
    """A collapsible reasoning/thinking block that streams live.

    Kept distinct from the answer so it can be reviewed and collapsed
    independently. Expanded by default (matches pi, which shows reasoning by
    default); the streaming state machine may collapse it once answer/tool
    content begins.
    """

    def __init__(self, *, collapsed: bool = False) -> None:
        self._md = Markdown("")
        super().__init__(self._md, title="Thinking…", collapsed=collapsed)
        self.add_class("reasoning-region")
        self._text = ""
        self._stream: MarkdownStream | None = None
        self._rendered = False

    def on_mount(self) -> None:
        if self.collapsed:
            return
        self._render_now()

    def _on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        """Render the buffered text the first time this region is opened.

        ``Collapsible._watch_collapsed`` posts this message on EVERY
        collapsed->False transition, including the one that happens inside
        ``Collapsible.__init__`` itself when a region is constructed with
        ``collapsed=False`` (our own default) — that message is queued before
        mount and can still be pending when a caller flips ``collapsed`` back
        to ``True`` synchronously afterwards (exactly what ``_promote_answer``
        and ``_reload_exchange`` do: create, ``set_text``, then collapse, all
        before the next await). By the time that stale message is finally
        delivered, ``self.collapsed`` already reads ``True`` again, so this
        checks the CURRENT state rather than trusting the message -- a truly
        stale event is a no-op, a real user/toggle expand renders.
        """
        if self.collapsed:
            return
        self._render_now()

    def _render_now(self) -> None:
        """Parse the buffered text into ``self._md``, once."""
        if self._rendered:
            return
        self._rendered = True
        if self._text:
            self._md.append(self._text)

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        if self._rendered:
            self._md.update(text)

    def append(self, delta: str) -> None:
        """Whole-text convenience append (non-streaming callers, tests)."""
        self.set_text(self._text + delta)

    async def append_delta(self, delta: str) -> None:
        """Stream one delta into the reasoning body without a full rebuild.

        Uses ``Markdown.get_stream``/``MarkdownStream.write`` (Textual 8.2.7),
        which appends the fragment to the tail of the document instead of
        reparsing+remounting everything ``set_text``/``Markdown.update()``
        would. ``self._text`` is kept in sync on every call (not just on a
        throttled tick) so ``text``/``set_text``'s equality guard stay correct
        whether or not this delta was actually streamed yet.

        Mirrors ``on_mount``'s pre-mount buffering: a delta that arrives before
        the inner Markdown is mounted (or before this region is ever expanded,
        D1) is accumulated into ``self._text`` only — the eventual mount/expand
        catches the full buffered text up via ``append()`` (not ``update()`` —
        see its comment), and streaming resumes from there.
        """
        if not delta:
            return
        self._text += delta
        if not self._rendered:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md)
        await self._stream.write(delta)

    async def finish_stream(self) -> None:
        """Stop this region's open stream, if any.

        Called at every point the active step stops being the streaming target
        (``_flush``, ``_collapse_active_reasoning``, ``finalize_exchange``) so
        no ``MarkdownStream`` background task is left running once the box may
        be collapsed, promoted from, or removed. Safe to call when nothing was
        ever streamed (idempotent no-op).
        """
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    @property
    def text(self) -> str:
        return self._text

    def mark_done(self, seconds: float | None = None) -> None:
        """Freeze the title once reasoning is complete."""
        self.title = "Thought" if seconds is None else f"Thought for {format_duration(seconds)}"


class ToolBox(QuietCollapsible):
    """A tool call paired with its result in ONE collapsible.

    Collapsed (the default — matching pi's default-collapsed tool output) shows
    just the call signature; expanded shows the arguments and, once it arrives,
    the result. The collapsed title gains a ✓/✗ status mark when the result
    lands, so the one-liner reads as call + outcome.

    **The two bodies are mounted on first expand**, the way
    :class:`ReasoningRegion` renders its buffered text on first expand. A box
    that builds them in ``__init__`` costs 9 widgets collapsed where 3 would do,
    and Textual arranges hidden widgets too (``_arrange_root(...,
    visible_only=False)``, 8.2.7) — so one turn of 60 tool calls was 552 mounted
    widgets that no reader had asked to see. docs/TRANSCRIPT-WINDOW.md §10 has
    the measurements and why the transcript window could not reach this case.

    :attr:`result_markdown` is the body whether or not it has ever been mounted,
    which is what a caller that wants the text rather than the widget should read.
    """

    def __init__(self, name: str, arguments: object, tool_call_id: str = "") -> None:
        self.tool_name = name
        self.tool_call_id = tool_call_id
        self._summary = format_tool_summary(name, arguments)
        self._arguments = arguments
        self._result_markdown = ""
        self._built = False
        self._args_md: Markdown | None = None
        self._result_md: Markdown | None = None
        super().__init__(title=self._summary, collapsed=True)
        self.add_class("tool-box")
        self.has_result = False

    def on_mount(self) -> None:
        if not self.collapsed:
            self._build_body()

    def _on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        """Build the body the first time this box is opened.

        Checks the CURRENT state rather than trusting the message, for the reason
        :meth:`ReasoningRegion._on_collapsible_expanded` records: an ``Expanded``
        posted before a synchronous re-collapse arrives after it.
        """
        if self.collapsed:
            return
        self._build_body()

    def _build_body(self) -> None:
        """Mount the argument and result bodies into the contents slot, once.

        A no-op until this box is mounted, because the slot does not exist before
        then; :meth:`on_mount` is the second entry point that covers that case.
        """
        if self._built or not self.is_mounted:
            return
        self._built = True
        self._args_md = Markdown(self._args_block(self._arguments))
        self._result_md = Markdown(self._result_markdown)
        self._result_md.display = self.has_result
        self.query_one(Collapsible.Contents).mount(self._args_md, self._result_md)

    @property
    def result_markdown(self) -> str:
        """The result body as markdown, ``""`` until a result lands."""
        return self._result_markdown

    def _write_result_body(self, markdown: str) -> None:
        """Hold ``markdown`` as the result body, and show it if the body is built."""
        self._result_markdown = markdown
        if self._result_md is not None:
            self._result_md.display = True
            self._result_md.update(markdown)

    @staticmethod
    def _args_block(arguments: object) -> str:
        return "```json\n" + json.dumps(arguments, indent=2, default=str) + "\n```"

    def set_result(
        self,
        result_text: str,
        is_error: bool = False,
        *,
        blocked: bool = False,
        blocked_by: str | None = None,
    ) -> None:
        if blocked:
            who = _extension_display_name(blocked_by)
            self.title = f"⛔ {self._summary}"
            body = f"blocked by {who}: {result_text}"
            body = body if len(body) <= 2000 else body[:2000] + "\n…(truncated)"
            self._write_result_body(f"```\n{body}\n```")
            self.has_result = True
            self.add_class("box-blocked")
            return
        mark = "✗" if is_error else "✓"
        self.title = f"{mark} {self._summary}"
        body = result_text if len(result_text) <= 2000 else result_text[:2000] + "\n…(truncated)"
        self._write_result_body(f"```\n{body}\n```")
        self.has_result = True
        if is_error:
            self.add_class("box-error")


class ExchangeBox(QuietCollapsible):
    """Groups one user→answer exchange's steps (reasoning, tool calls, the final
    answer) under a single summary line.

    Expanded by default so streaming is visible; the title shows a live
    ``Working… · 1.2k out · ~83 chunks · 0:12`` readout while the exchange runs
    (:meth:`set_live`) and a ``N tools · X tok · M:SS`` summary once it finishes
    (:meth:`set_summary`). Steps are mounted into the collapsible body as they
    arrive.

    ``label`` names the lane this exchange belongs to when it is NOT the ordinary
    "a human typed this" one — ``"bus · nats_bus"``, ``"agent · fork:explore"``
    (B3-a). It rides on both the running title and the finished summary, because
    the whole point of rendering another source's turn is that the reader can tell
    it apart at a glance; ``None`` leaves both reading exactly as they always have.
    """

    def __init__(self, *, collapsed: bool = False, label: str | None = None) -> None:
        super().__init__(
            title="Working…" if label is None else f"{label} · Working…", collapsed=collapsed
        )
        self.add_class("exchange-box")
        self._tool_count = 0
        self._label = label
        if label is not None:
            self.add_class("exchange-foreign")

    def add_step(self, widget: Widget) -> None:
        """Mount a step widget into the exchange body, in arrival order.

        The exchange is mounted (and its ``Collapsible.Contents`` composed)
        before any step is added — the caller awaits the exchange mount — so the
        body container is always present here.
        """
        self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    async def add_step_async(self, widget: Widget) -> None:
        """Like :meth:`add_step` but awaits the mount.

        The reload path builds an exchange synchronously — it writes reasoning,
        text and tool *results* into a step right after mounting it — so it must
        wait for the step (and its slots) to compose before touching them. The
        live path is network-paced and doesn't need to wait, so it uses the
        fire-and-forget :meth:`add_step`.
        """
        await self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    @property
    def tool_count(self) -> int:
        return self._tool_count

    def set_live(self, *, seconds: float | None, output: int, chunks: int) -> None:
        """Update the running title while the exchange is still streaming.

        The liveness readout. Before this the title said ``Working…`` and then
        nothing on screen changed until the answer text started arriving — so a
        turn spent thinking, or waiting on a slow tool, was indistinguishable
        from a turn that had died.

        Three parts, and the distinction between them is the whole point:

        * ``N out`` is MEASURED. It is the sum of the per-completion
          ``usage.output_tokens`` this lane has been told, so it steps at each
          completion boundary (each tool call) and is omitted entirely until the
          first completion reports one. It is never an approximation of the
          completion currently in flight — no such measurement exists.
        * ``~N chunks`` is the completion in flight, and is labelled twice over:
          ``~`` and the word *chunks*. It counts stream events, not tokens. Most
          OpenAI-compatible servers send one token per chunk, but nothing
          guarantees that, so this never claims to be a token count.
        * the duration is wall-clock since the exchange opened.

        A part with nothing to say is left out rather than shown as zero: no
        completion has reported, or nothing is in flight (a tool is running).
        ``seconds=None`` omits the duration for the same Fail-Early reason
        :meth:`set_summary` does — an unknown time is not a 0:00 one.
        """
        parts = ["Working…"]
        if output:
            parts.append(f"{format_tokens(output)} out")
        if chunks:
            parts.append(f"~{chunks} chunk" + ("" if chunks == 1 else "s"))
        if seconds is not None:
            parts.append(format_duration(seconds))
        if self._label is not None:
            parts.insert(0, self._label)
        self.title = " · ".join(parts)

    def set_summary(
        self,
        *,
        tools: int,
        context: int,
        output: int,
        seconds: float | None = None,
        telemetry: str | None = None,
    ) -> None:
        """Finalize the title with the exchange's stats. A count of 0 is shown as
        0 — we never hide a real value behind a branch.

        ``context`` is how large the prompt had grown by the end of this exchange
        (the last completion's prompt size); ``output`` is what the exchange
        generated. They are reported separately and never summed: a prompt already
        contains every earlier turn, so ``context + output`` restates the whole
        conversation as if it were this exchange's cost.

        ``seconds`` is omitted on the reload path: wall-clock duration is not
        persisted, so a reconstructed exchange shows ``N tools · X ctx · Y out``
        without a fabricated time (Fail-Early).

        ``telemetry`` is the last completion's G4 readout (t/s · repairs ·
        forced-share, from :func:`format_telemetry`); it is per-completion, not an
        exchange aggregate like ``output``, so it is appended verbatim as one
        more ``·`` part when present. ``None`` (a provider that reported nothing)
        appends nothing — the summary reads exactly as it did before G4."""
        tool_label = f"{tools} tool" + ("" if tools == 1 else "s")
        parts = [tool_label, f"{format_tokens(context)} ctx", f"{format_tokens(output)} out"]
        if seconds is not None:
            parts.append(format_duration(seconds))
        if telemetry is not None:
            parts.append(telemetry)
        if self._label is not None:
            parts.insert(0, self._label)
        self.title = "✓ " + " · ".join(parts)
