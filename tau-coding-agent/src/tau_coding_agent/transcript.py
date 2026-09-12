"""transcript — split out of app.py."""

import time
from bisect import bisect_right
from typing import Callable, Optional, Any
from textual import events
from textual.app import ComposeResult
from textual.geometry import Size
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static
from tau_coding_agent.backends import (
    DEFAULT_LANE,
    prompt_tokens,
    resolve_tool_names,
    span_seconds,
)
from tau_coding_agent.chat_widgets import (
    ExchangeBox,
    ToolBox,
    format_duration,
    format_tokens,
    MessageBox,
    _split_assistant_blocks,
    _join_text_blocks,
    ContentSource,
    format_tool_result_body,
    format_tool_call_body,
)
from tau_agent_core.conversation_tree import TreeNode
from tau_agent_core.extension_locks import ExtensionRequest
from tau_agent_core.messages import is_displayed
from tau_coding_agent import extension_ui
from dataclasses import dataclass
from textual.containers import VerticalScroll
from tau_agent_core.attachments import elide_attachment_bodies, human_size
from rich.console import RenderableType
from rich.text import Text
from pathlib import Path


LANE_FOREIGN_CLASS = "lane-foreign"


class _LaneRender:
    """One render lane's live exchange state (B3-a).

    Was five instance attributes on :class:`ChatDisplay`, which is precisely why
    the display could render one turn at a time: ``begin_exchange`` reset them and
    ``finalize_exchange`` closed whatever they currently pointed at, so two
    overlapping turns interleaved into one exchange and finalized each other's.
    As a per-lane record the same state exists once per concurrently-streaming
    turn — a forked sub-agent, or a bus submission arriving mid-answer.
    """

    __slots__ = (
        "exchange",
        "label",
        "active_box",
        "active_text",
        "active_reasoning",
        "tool_routes",
        "started",
        "measured_output",
        "chunks",
    )

    def __init__(self, exchange: Optional[ExchangeBox] = None, label: str | None = None) -> None:
        self.exchange = exchange
        self.label = label
        #: The current turn's assistant step box (reasoning + text + tools).
        self.active_box: Optional[MessageBox] = None
        self.active_text: str = ""
        self.active_reasoning: str = ""
        #: Route each tool result to the step that issued the call, by id.
        self.tool_routes: dict[str, MessageBox] = {}
        self.started: float | None = None
        self.measured_output: int = 0
        self.chunks: int = 0


def _display_path(path: Path) -> str:
    """``path`` with ``$HOME`` collapsed to ``~``, for display only.

    Purely cosmetic, and never fed back to anything that opens a file — the whole
    point is that ``~/Development/agent-harness-py`` fits the chat column where
    the absolute path may not. A path outside ``$HOME`` is returned unchanged.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


_NO_TOOLS_REASON: dict[str, str] = {
    "all": "none (--no-tools)",
    "builtin": "none (--no-builtin-tools)",
}


def _tools_row(model_config: dict[str, Any]) -> str:
    """Render the pane's ``tools`` row for one resolved model config.

    Names the flag that emptied the list rather than guessing one. Before this,
    the row said ``--no-builtin-tools`` for every empty set — including under
    ``--no-tools``, and including a ``"tools": []`` the config file itself
    declared, neither of which that flag caused. A config-declared empty set now
    reads as a plain ``none``: true, and not attributed to a flag nobody passed.
    """
    names = resolve_tool_names(model_config)
    if names:
        return " ".join(names)
    return _NO_TOOLS_REASON.get(str(model_config.get("no_tools")), "none")


@dataclass(frozen=True)
class SessionFacts:
    """What the empty chat pane states, already resolved to display strings.

    Everything here is *configuration*, never a probe: ``endpoint`` is the URL τ
    will post to, not a URL it has reached. Showing a reachability tick nothing
    verified is the failure this type is shaped to avoid — the pane can be honest
    about what it was told without pretending to know what it was not.
    """

    tagline: str
    #: The model id that goes on the wire, e.g. ``qwen36-35B-IQ4_XS``.
    model: str
    endpoint: str
    cwd: str
    tools: str
    #: The session store this chat will be written to.
    store: str


class ChatPlaceholder(Static):
    """What the chat column says before the first message (handoff §4.4).

    Not a greeting. The header already says ``Tau``, the footer already lists the
    keybindings, and the sidebar already says whether there are saved sessions —
    so the one thing this frame can add is the **configuration the next turn will
    run against**, which nothing else on screen states. A wrong ``base_url`` or a
    missing ``bash`` is otherwise discovered after a prompt is typed and a
    timeout elapses.

    It also carries the one line of identity: the τ and its tagline. That is here
    rather than in a splash because this frame IS the screenshot — the README and
    the docs header render exactly this, for a reader who has never run τ.

    Deliberately NOT here: rotating startup tips. A tip that changes on the next
    ``ctrl+n`` cannot be found again, restates a footer that is three rows below,
    and makes the pane non-deterministic — which would break the SVG snapshot
    suite and ``devshot``. The single fixed hint below explains a footer key
    (``ctrl+g``) instead of repeating it.
    """

    LABEL_WIDTH = 9

    def __init__(self, facts: SessionFacts) -> None:
        super().__init__(id="chat-placeholder")
        self._facts = facts

    def update_facts(self, facts: SessionFacts) -> None:
        """Re-state the pane against ``facts``.

        Called by :meth:`ChatDisplay._sync_placeholder` every time the pane
        becomes visible, because the model can change (``/model``, a resumed
        session) while the chat is empty. Re-reading on show — rather than
        snapshotting once at construction — is what keeps a visible fact true.
        """
        self._facts = facts
        self.refresh()

    def render(self) -> RenderableType:
        """Rebuild the pane from :attr:`_facts`.

        ``render`` rather than a stored renderable, so :meth:`update_facts` only
        has to swap the dataclass and call ``refresh()`` — there is no second copy
        of the text to keep in step.
        """
        f = self._facts
        rows = [
            ("model", f.model),
            ("", f.endpoint),
            ("cwd", f.cwd),
            ("tools", f.tools or "none"),
            ("store", f.store),
        ]
        body = Text(justify="left")
        body.append("τ\n", style="bold")
        body.append(f.tagline + "\n\n", style="dim")
        for label, value in rows:
            body.append(f"{label:<{self.LABEL_WIDTH}}", style="dim")
            body.append(value + "\n")
        body.append("\nCtrl+Enter sends · Ctrl+P commands\n", style="dim")
        body.append(
            "Ctrl+G opens the session tree, where you can branch from any earlier message.",
            style="dim",
        )
        return body


class MessageList(VerticalScroll):
    """A scrollable column of :class:`MessageBox` widgets, and how to fill it.

    The two renderers every transcript view shares: :meth:`add_message` (one
    finished box) and :meth:`add_persisted_message` (one τ on-disk message, which
    may be several boxes). :class:`ChatDisplay` adds the live streaming state
    machine on top; :class:`TreeDetailPane` adds nothing but a scroll position.

    It is a base class rather than a helper function because the boxes must be
    CHILDREN of the scrolling widget, and because ``.chat-message`` styling is
    written against that containment — the detail pane looks like the chat view
    for the reason that it *is* the chat view's renderer, not because a second
    implementation was kept in step by hand.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._follow_tail: bool = True

    # -- following the tail, and letting go of it ---------------------------

    def scroll_to_tail(self) -> None:
        """Show the newest content, but ONLY if the reader is already there.

        Every renderer in this class and in :class:`ChatDisplay` calls this
        instead of ``scroll_end``. The difference is the whole of "you can read
        while it writes": a turn that streams for ninety seconds used to yank
        the view back to the bottom on every delta, so scrolling up to re-read a
        tool result was impossible until the turn ended.

        :attr:`_follow_tail` is maintained by :meth:`watch_scroll_y` rather than
        being decided here, because "is the reader at the bottom" has to be
        sampled when the READER moves, not when content arrives: content arriving
        while detached grows ``max_scroll_y`` without moving ``scroll_y``, which
        would make a check at this moment answer "no" forever after the first
        delta, and a check after the mount answer "yes" every time.
        """
        if self._follow_tail:
            self.scroll_end(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Re-decide whether to follow the tail, on every vertical scroll.

        Textual's own watcher, extended. It fires for a scroll from any cause —
        wheel, keys, scrollbar drag, and this class's own
        :meth:`scroll_to_tail` — and the rule is the same for all of them:
        following means the view is at the bottom right now.

        So scrolling up releases the tail on the next delta, and scrolling back
        down re-attaches it, with no separate gesture to learn. Textual's own
        ``anchor()`` implements almost this, but its release is wired into
        ``scroll_to``'s ``release_anchor`` argument rather than into the
        position, so this class's own ``scroll_end`` calls would release it.
        """
        super().watch_scroll_y(old_value, new_value)
        self._follow_tail = self.is_vertical_scroll_end

    def add_message(self, role: str, content: str, subtitle: str = "", *, source: ContentSource):
        """Add a finished (non-streaming) message box to the display.

        ``source`` is passed straight through to :class:`MessageBox` and is
        required for the same reason it is required there: only this caller knows
        whether it is handing over an assistant's markdown or verbatim output.
        """
        box = MessageBox(role, content, subtitle, source=source)
        self.mount(box)
        self.scroll_to_tail()
        return box

    def set_extension_request(self, request: "ExtensionRequest | None") -> None:
        """Show (or clear) the extension-request row at the end of the transcript.

        docs/EXTENSION-LOCKS.md §9. Idempotent and re-callable: any existing row
        is removed first, so a second request replaces the first rather than
        stacking. The row is mounted LAST because the request is at the cursor
        and the cursor is the leaf; a request that is not at the cursor is not
        drawn here at all.

        Args:
            request: The request to draw, or ``None`` to clear the row.
        """
        for existing in self.query(extension_ui.ExtensionRequestBox):
            existing.remove()
        if request is None:
            return
        self.mount(extension_ui.ExtensionRequestBox(request))
        self.scroll_to_tail()

    def add_persisted_message(self, msg: dict) -> list[MessageBox]:
        """Render one *persisted* message (from a saved chat) in arrival order.

        Unlike the live path — driven by streaming lifecycle events — a reloaded
        message carries its content as the τ on-disk shape: a plain string
        (user/system), or a list of block dicts (assistant: ``text`` +
        ``toolCall`` blocks; ``toolResult``: a separate role with ``text`` blocks
        plus top-level ``tool_name``/``is_error``). Each block becomes the SAME
        ``MessageBox`` kind the live path would have produced — a ``str``-only
        renderer here is exactly the bug that froze the TUI on chat reload, so we
        normalize instead of handing a list to ``MessageBox``.

        Returns every box it mounted, in order, because one message is not one
        box: an assistant turn that interleaves text and tool calls becomes
        several, and a caller that has to style or scroll to "that message"
        (:class:`TreeDetailPane`) needs all of them, not the first.

        Raises ``TypeError`` on an unrenderable content shape rather than
        silently dropping it (Fail-Early): an unexpected shape is a real bug.
        """
        role = msg.get("role", "")

        if role == "toolResult":
            result_text = _join_text_blocks(msg.get("content", []))
            box = self.add_message(
                "toolResult",
                format_tool_result_body(
                    msg.get("tool_name", ""),
                    result_text,
                    bool(msg.get("is_error", False)),
                ),
                source="verbatim",
            )
            if msg.get("is_error"):
                box.add_class("box-error")
            return [box]

        text_source: ContentSource = "markdown" if role == "assistant" else "verbatim"

        content = msg.get("content", "")
        if isinstance(content, str):
            if role == "user":
                content = elide_attachment_bodies(content)
            return [self.add_message(role, content, source=text_source)]
        if isinstance(content, list):
            boxes: list[MessageBox] = []
            text_buf: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    text_buf.append(elide_attachment_bodies(text) if role == "user" else text)
                elif btype == "image":
                    encoded = block.get("data", "")
                    size = human_size(len(encoded) * 3 // 4)
                    text_buf.append(f"[{block.get('mime_type', 'image')}, {size}]")
                elif btype == "toolCall":
                    if text_buf:
                        boxes.append(self.add_message(role, "".join(text_buf), source=text_source))
                        text_buf = []
                    boxes.append(
                        self.add_message(
                            "toolCall",
                            format_tool_call_body(
                                block.get("name", ""), block.get("arguments", {})
                            ),
                            source="verbatim",
                        )
                    )
            if text_buf:
                boxes.append(self.add_message(role, "".join(text_buf), source=text_source))
            return boxes

        raise TypeError(f"cannot render persisted message content of type {type(content).__name__}")


@dataclass(frozen=True)
class DetailView:
    """The window of conversation the tree browser's detail pane shows.

    Three nodes at most — the selected one and its two conversational neighbours
    — plus counts of what lies beyond them, which is all the ``⋯`` rows need to
    say something true. Computed by :meth:`SessionTreeModal._view_of` from the
    ``TreeNode`` graph alone; the pane resolves the bodies.
    """

    selected: TreeNode
    previous: TreeNode | None
    following: TreeNode | None
    #: Ancestors strictly above ``previous`` (0 → the fold marker is not drawn).
    earlier: int
    #: Descendants strictly below ``following`` along the whole subtree.
    later: int
    #: Children of ``selected``, when more than one — a fork below the cursor.
    branches: int


class TreeDetailPane(MessageList):
    """The tree browser's right-hand pane: the selected node, in full, in context.

    The browser row is one elided line, which answers "which node is this?" and
    not "is this the one I meant?". This pane answers the second question with
    the SAME renderer the transcript uses (:class:`MessageList`), so a node reads
    here exactly as it read in the chat — collapsibles and all, since the boxes
    are real :class:`MessageBox` widgets and a tool box or reasoning region in
    one can be opened.

    Only three nodes are mounted at a time. That is not a performance hedge that
    trades away completeness — a tree browser's neighbours are the PARENT and a
    CHILD, and moving between forks replaces both, so a full-conversation list
    would be rewritten on most moves anyway. ``⋯ N earlier`` / ``⋯ N later`` rows
    state what is not drawn instead of implying the conversation is three
    messages long.

    The neighbours wear ``detail-context``, which desaturates their border and
    body while keeping the per-role hue: the reader still sees "user above,
    assistant below" without those boxes competing with the selection.
    """

    LEAD_ROWS = 3

    def __init__(self, resolve_entry: Callable[[str], dict[str, Any]]) -> None:
        super().__init__(id="tree-detail")
        self._resolve_entry = resolve_entry
        self._shown_id: str | None = None
        self._selected_boxes: list[MessageBox] = []
        self._position_pending = False

    async def show(self, view: DetailView) -> None:
        """Rebuild the pane for ``view`` and scroll the selection into place.

        A repeat of the node already shown is a no-op — Textual re-emits
        ``NodeHighlighted`` on events that do not move the cursor (a click on the
        current row, a re-focus), and rebuilding on those would drop the reader's
        scroll position and any collapsible they had opened.
        """
        if view.selected.id == self._shown_id:
            return
        self._shown_id = view.selected.id
        await self.remove_children()
        self._selected_boxes = []

        if view.earlier:
            await self.mount(self._fold_row(f"⋯ {view.earlier} earlier"))
        if view.previous is not None:
            self._dim(self._render_node(view.previous))
        self._selected_boxes = self._render_node(view.selected)
        if view.following is not None:
            self._dim(self._render_node(view.following))
        trailer = self._trailer(view)
        if trailer:
            await self.mount(self._fold_row(trailer))

        # Positioning needs measured heights, which exist only after a layout.
        self._position_pending = True
        self.call_after_refresh(self._scroll_to_selection)

    def _size_updated(self, size, virtual_size, container_size, layout: bool = True) -> bool:
        """Take the second chance at positioning, once the layout is real.

        The ``call_after_refresh`` in :meth:`show` is not reliably late enough:
        the boxes it mounted may still be unmeasured on that tick, and a scroll
        against a zero ``max_scroll_y`` clamps to the top and silently does
        nothing — leaving the selection under a long previous message, which is
        the one arrangement the pane exists to prevent. This hook is where
        ``ScrollView`` learns its virtual size, so it is the earliest point at
        which the answer can be right. Both paths run; whichever finds a measured
        layout first clears the flag and the other becomes a no-op.
        """
        changed = super()._size_updated(size, virtual_size, container_size, layout)
        if self._position_pending:
            self._scroll_to_selection()
        return changed

    @staticmethod
    def _trailer(view: DetailView) -> str:
        """The bottom ``⋯`` row's text, or ``""`` when nothing is below.

        A fork is reported separately from the count because it is a different
        fact: ``later`` says how much conversation is hidden, ``branches`` says
        that the node the reader is looking at is where the history splits, which
        is usually why they opened this browser at all.
        """
        parts = []
        if view.branches > 1:
            parts.append(f"{view.branches} branches from here")
        if view.later:
            parts.append(f"{view.later} later")
        return f"⋯ {', '.join(parts)}" if parts else ""

    @staticmethod
    def _fold_row(text: str) -> Static:
        return Static(text, classes="detail-fold")

    @staticmethod
    def _dim(boxes: list[MessageBox]) -> None:
        for message_box in boxes:
            message_box.add_class("detail-context")

    def _render_node(self, node: TreeNode) -> list[MessageBox]:
        """One tree node as the boxes the transcript would have drawn for it.

        Three kinds of payload, handled explicitly rather than by a single
        ``preview`` shortcut, because the pane exists to show more than the row
        already did:

        - a ``message``/``customMessage`` carries a real message — rendered by
          the shared :meth:`~MessageList.add_persisted_message`, so an assistant
          turn arrives with its tool boxes attached;
        - a ``compaction``/``branch_summary`` carries a ``summary``, of which the
          row showed only the first line — the whole text is drawn here;
        - anything else (``navigate``, ``elide``, ``customEntry``) has no body
          beyond what ``ConversationTree`` already composed into ``preview``, so
          that IS the full text, not a truncation of one.
        """
        entry = self._resolve_entry(node.id)
        kind = str(entry.get("type", "")) or node.kind
        if kind in ("message", "customMessage"):
            message = entry.get("message")
            if isinstance(message, dict):
                return self.add_persisted_message(message)
        if kind in ("compaction", "branch_summary"):
            return [self.add_message(kind, str(entry.get("summary", "")), source="verbatim")]
        return [self.add_message(kind, node.preview, source="verbatim")]

    def _scroll_to_selection(self) -> None:
        """Put the selected node's top edge :attr:`LEAD_ROWS` below the pane top.

        Not ``scroll_to_widget(top=True)``: that would park the selection flush
        against the pane's top edge and scroll the previous message entirely out,
        which is the one thing the layout is supposed to prevent. Scrolling to an
        absolute virtual row instead means a short previous message (nothing to
        scroll past) and a very long one land in the same place.

        Returns without clearing :attr:`_position_pending` when the boxes have no
        measured height yet — the position is not yet knowable, and guessing at it
        would put the selection somewhere arbitrary. :meth:`_size_updated` calls
        back when the layout lands.
        """
        if not self._selected_boxes:
            self._position_pending = False
            return
        selected = self._selected_boxes[0]
        if not selected.virtual_region.height:
            return
        self._position_pending = False
        self.scroll_to(y=max(0, selected.virtual_region.y - self.LEAD_ROWS), animate=False)

    @property
    def selected_boxes(self) -> list[MessageBox]:
        """The boxes drawn for the selected node (the undimmed ones)."""
        return list(self._selected_boxes)

    @property
    def shown_id(self) -> str | None:
        """The node currently drawn, or ``None`` before the first :meth:`show`.

        Public so a caller can wait for the pane to catch up with the tree rather
        than guess at how many event-loop turns that takes — the pane's draw is
        two deferred callbacks behind a cursor move.
        """
        return self._shown_id


class ChatDisplay(MessageList):
    """Main chat display area with incremental, arrival-ordered rendering.

    One user→answer span is an **exchange**, and each concurrently-streaming turn
    is a **lane** (B3-a) — keyed by ``submission_id``, or ``branch:<lane>`` for a
    forked sub-agent. Lanes render side by side without interleaving; a lane
    nobody named is :data:`DEFAULT_LANE`, which is what every pre-B3-a caller (the
    reload path, a test replaying widget events) implicitly used.

    While the agent loop streams, each lane runs a state machine driven by
    normalized backend events (see ``RenderRouter`` in ``backends.py``) that groups
    the span under one collapsible :class:`ExchangeBox`:

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
    - while any lane is open, a timer repaints each exchange's ``Working…``
      title with a measured token count, the in-flight chunk count and the
      elapsed time (:meth:`_tick_live_counters`). Per lane rather than one
      global readout: two concurrent turns have two different answers, and the
      one line a header subtitle has could only report one of them.

    Reloaded (persisted) chats still render as flat boxes via
    :meth:`add_persisted_message` — rebuilding exchanges from the saved message
    list is a separate concern.
    """

    def __init__(self, facts: Callable[[], SessionFacts] | None = None):
        super().__init__(id="chat-display")
        self._lanes: dict[str, _LaneRender] = {}
        self._facts_source = facts
        self._placeholder: ChatPlaceholder | None = None
        self._reload_source: list[dict] = []
        self._transcript_source: Callable[[], list[dict]] | None = None
        self._elided = 0
        self._elided_after = 0
        self._window_start = 0
        self._window_end = 0
        self._window_moving = False
        self._window_generation = 0
        self._turn_anchors: dict[int, Widget] = {}
        self._trim_deferred = False
        self._building = False
        self._live_timer: Timer | None = None

    RENDER_CAP_TURNS = 4
    RENDER_CAP_MESSAGES = 100

    LIVE_TICK_SECONDS = 0.25

    def on_mount(self) -> None:
        """Create the (paused) live-counter timer.

        Paused, because it is started by :meth:`begin_exchange` and stopped again
        when the last lane closes — a chat with nothing streaming does no work.
        """
        self._live_timer = self.set_interval(
            self.LIVE_TICK_SECONDS, self._tick_live_counters, pause=True
        )

    def _tick_live_counters(self) -> None:
        """Repaint every open exchange's ``Working…`` line.

        Reads the lane state and writes the title; it measures nothing itself, so
        a lane whose provider reports no usage shows the duration and the chunk
        count and makes no token claim.
        """
        now = time.monotonic()
        for state in self._lanes.values():
            if state.exchange is None:
                continue
            state.exchange.set_live(
                seconds=None if state.started is None else now - state.started,
                output=state.measured_output,
                chunks=state.chunks,
            )

    def _sync_live_timer(self) -> None:
        """Run the counter timer exactly while at least one lane is open."""
        if self._live_timer is None:
            return
        if self._lanes:
            self._live_timer.resume()
        else:
            self._live_timer.pause()

    def compose(self) -> ComposeResult:
        """Compose the placeholder, when this display has facts to state.

        Yielded rather than mounted, so it does not pass through :meth:`mount` —
        which would re-enter :meth:`_sync_placeholder` before the attribute it
        reads is assigned.
        """
        if self._facts_source is not None:
            self._placeholder = ChatPlaceholder(self._facts_source())
            yield self._placeholder

    def _sync_placeholder(self) -> None:
        """Show the placeholder exactly while this display holds no messages.

        **Derived from the DOM, never told.** Every caller that adds or removes
        content would otherwise have to remember to update a flag, and the one
        that forgot would leave the pane visible under a live transcript. Asking
        the tree what is in it cannot go stale — the only failure mode left is a
        missed call, which is why ``test_chat_placeholder.py`` asserts the state
        after every public entry point.

        Derived from the DIRECT children, not from two deep ``query()`` calls.
        Each ``query()`` walks the whole subtree and builds a list; this runs on
        every mount, so on a reload it was Σ O(widgets) — 12 % of a 150-message
        reload, and 3.2 s of an 8.6 s 200-message one. The direct children answer
        the same question: a nested ``MessageBox`` is a step inside an
        ``ExchangeBox``, and that exchange IS a direct child. ``any()`` also stops
        at the first box instead of collecting every one of them.
        """
        placeholder = self._placeholder
        if placeholder is None:
            return
        has_content = any(isinstance(child, (MessageBox, ExchangeBox)) for child in self.children)
        placeholder.display = not has_content
        if not has_content:
            placeholder.update_facts(self._facts_source())  # type: ignore[misc]

    def mount(self, *widgets: Widget, before=None, after=None):
        """Mount children, then re-decide whether the placeholder still applies.

        The structural hook for "content arrived": every box that enters this
        display directly — :meth:`MessageList.add_message`, :meth:`begin_exchange`,
        the defensive top-level step path — goes through here. Overriding one
        method beats sprinkling a sync call through five call sites and finding
        out later which one was missed.
        """
        result = super().mount(*widgets, before=before, after=after)
        self._sync_placeholder()
        return result

    async def clear_messages(self):
        """Clear all messages from display and reset streaming state.

        Async: a chat cleared *mid-stream* (new-chat/clear-chat while a turn is
        still streaming) can have an open ``MarkdownStream`` on the active
        lane's step (content and/or reasoning) -- ``.remove()``ing that box out
        without stopping its stream first would leave the stream's background
        task referencing a detached widget forever (a leaked task, and the
        exact "left open on a box that gets removed" case the streaming
        redesign has to not raise on). Stopping first, via the same
        ``finish_stream`` every other lane-transition point uses, makes the
        ensuing ``.remove()`` calls safe.
        """
        for state in self._lanes.values():
            box = state.active_box
            if box is None:
                continue
            if box.reasoning is not None:
                await box.reasoning.finish_stream()
            await box.finish_stream()
        await self.query(ExchangeBox).remove()
        await self.query(MessageBox).remove()
        await self.query(".chat-fold").remove()
        self._elided = 0
        self._elided_after = 0
        self._window_start = 0
        self._window_end = 0
        self._turn_anchors = {}
        self._trim_deferred = False
        self._lanes = {}
        # Every exchange the counter had to draw has just been removed.
        self._sync_live_timer()
        self._sync_placeholder()

    def _lane(self, lane: str) -> _LaneRender:
        """This lane's render state, created on demand.

        On demand rather than "raise if absent": the display has always tolerated
        an event with no exchange open (``_start_step`` mounts at top level), and
        that tolerance is what keeps a chat cleared mid-turn from turning every
        subsequent delta into an error.
        """
        state = self._lanes.get(lane)
        if state is None:
            state = _LaneRender()
            self._lanes[lane] = state
        return state

    def active_step(self, lane: str = DEFAULT_LANE) -> Optional[MessageBox]:
        """The step box a lane is currently streaming into, if any.

        The one piece of lane state anything outside this class reads (tests
        asserting where reasoning/tool output landed). Public and lane-addressed
        rather than a poked-at private attribute, because "which box is live" is
        now a question that has a different answer per lane.
        """
        state = self._lanes.get(lane)
        return None if state is None else state.active_box

    async def begin_exchange(self, lane: str = DEFAULT_LANE, *, label: str | None = None) -> None:
        """Open a new exchange for ``lane`` before its agent loop runs.

        Awaits the mount so the exchange's collapsible body has composed before
        the first ``turn_start`` adds a step into it (begin→turn_start has no
        natural render tick between them, unlike the network-paced events that
        follow). Steps mount into the expanded ``ExchangeBox`` as the loop
        streams; :meth:`finalize_exchange` later collapses it to a summary line.

        ``label`` marks a lane that is NOT this frontend's own typed turn — a bus
        or timer submission, a forked sub-agent — so the reader can tell it apart
        (Jupyter's rule: render every source, differently). ``None`` renders
        exactly as it always has. It is kept on the lane as well as on the
        exchange, because every box the lane mounts wears it (B3-b): the exchange
        outlives neither the promoted answer nor, for a no-tool span, itself.

        A turn start is the SECOND window point, and it is the one that bounds a
        reader who does not come back: see :meth:`_claim_tail_for_trim`. Both run
        before the lane is registered, because :meth:`_maybe_trim` declines to
        evict while any lane is open and this one is about to be.
        """
        await self.snap_window_to_tail()
        self._claim_tail_for_trim()
        await self._maybe_trim()
        exchange = ExchangeBox(label=label)
        state = _LaneRender(exchange, label)
        state.started = time.monotonic()
        self._lanes[lane] = state
        await self.mount(exchange)
        self._sync_live_timer()
        self.scroll_to_tail()

    async def snap_window_to_tail(self) -> bool:
        """Put the window back on the end of the transcript. Returns whether it
        had to move.

        A no-op unless the reader has slid the window back into history, which is
        the only state this exists for. It is called from :meth:`begin_exchange`,
        and it is the answer to the one question a slid window cannot otherwise
        answer: a turn is starting, and the live state machine mounts into the
        END of this display — so a window showing turn 5 of 40 would grow a live
        exchange directly under turn 8, in a place that means nothing.

        It DOES pull a reader out of history, and that is the cost. It is the
        cheaper of the two costs: the alternative is a live turn rendering where
        the reader cannot see it, and the state machine having to tolerate
        streaming into a box that is not on screen. A turn beginning is the
        present changing, and the present is what the tail shows.
        """
        if not self._elided_after:
            return False
        self._refresh_transcript()
        messages = self._reload_source
        start = self.render_cap_start(messages)
        await self._render_window(messages, start, self.window_end(messages, start))
        self._follow_tail = True
        self.scroll_to_tail()
        return True

    async def handle_stream_event(self, event: dict) -> None:
        """Render one normalized backend lifecycle event in arrival order.

        The event names its lane; an event that names none belongs to
        :data:`DEFAULT_LANE`, the one implicit lane every pre-B3-a caller used.

        Async because reasoning/text deltas now stream through a
        ``MarkdownStream`` (``MessageBox.append_content_delta`` /
        ``ReasoningRegion.append_delta``), whose ``write()`` is itself async;
        every caller in the live path already awaits its way down from the
        event bus, so this just extends that chain one level further.
        """
        if event.get("kind") == "custom_message":
            await self._on_custom_message(event.get("message") or {})
            return
        state = self._lane(event.get("lane") or DEFAULT_LANE)
        kind = event.get("kind")
        if kind == "turn_start":
            await self._on_turn_start(state)
        elif kind == "reasoning_delta":
            state.chunks += 1
            await self._on_reasoning_delta(state, event.get("delta", ""))
        elif kind == "text_delta":
            state.chunks += 1
            await self._on_text_delta(state, event.get("delta", ""))
        elif kind == "tool_call":
            await self._on_tool_call(state, event)
        elif kind == "tool_result":
            self._on_tool_result(state, event)
        elif kind == "steer_message":
            await self._on_steer_message(state, event.get("text", ""))
        elif kind == "completion_end":
            state.measured_output = int(event.get("output", 0) or 0)
            state.chunks = 0

    async def _on_custom_message(self, message: dict) -> None:
        """Mount an extension's durable message as it is appended (§9.1).

        The one message with no completion behind it, which is why it needs its
        own channel: ``api.send_message`` writes to the tree and the turn's
        streaming events say nothing about it, so before this the box appeared
        only after the next reload — and, from a command handler, not even then.

        Rendered through :meth:`add_persisted_message`, the same call the reload
        path makes, so the live box and the reloaded one are one widget. Mounted
        at top level rather than into an open lane's exchange: it belongs to no
        completion, and the exchange closes above it, which leaves it after the
        turn it was appended during.

        A node the extension marked ``display: False`` is skipped here and not in
        :meth:`add_persisted_message`, because the tree browser's detail pane calls
        that method too and a hidden node is hidden from the TRANSCRIPT only
        (docs/EXTENSION-MESSAGES.md §2).
        """
        if not message or not is_displayed(message):
            return
        self.add_persisted_message(message)

    def _start_step(self, state: _LaneRender) -> MessageBox:
        """Mount a fresh assistant step box for this lane's current turn.

        Steps live inside the lane's exchange so the whole span groups under one
        summary. If no exchange is open (defensive — the live path always calls
        :meth:`begin_exchange` first), the step mounts at top level.

        A foreign lane's step is badged and class-marked (B3-b). The step is an
        ``assistant`` message either way — a forked sub-agent's answer really is
        an assistant message — but WHOSE assistant it is has to be on the box
        itself, not only on the enclosing exchange, or a reader scrolling past a
        collapsed summary reads a sub-agent's text as the main line's.
        """
        box = MessageBox("assistant", "", state.label or "", source="markdown")
        if state.label is not None:
            box.add_class(LANE_FOREIGN_CLASS)
        if state.exchange is not None:
            state.exchange.add_step(box)
        else:
            self.mount(box)
        return box

    async def _flush(self, state: _LaneRender) -> None:
        """Stop the lane's active step's streams and show all accumulated text.

        Every stream write is applied as it arrives now (no throttle to skip a
        final delta), so by the time this runs ``self._text``/``self._content``
        already equal ``state.active_reasoning``/``state.active_text`` and the
        ``set_text``/``update_content`` calls below are no-ops in the streaming
        case — they remain as a safety net for any caller that set content some
        other way. Stopping the stream FIRST (rather than after) is what makes
        this call safe to follow with ``.remove()``: no ``MarkdownStream``
        background task is left referencing a box that leaves the DOM.
        """
        box = state.active_box
        if box is None:
            return
        if box.reasoning is not None:
            await box.reasoning.finish_stream()
            if state.active_reasoning:
                box.reasoning.set_text(state.active_reasoning)
        await box.finish_stream()
        if state.active_text:
            box.update_content(state.active_text)
        self.scroll_to_tail()

    async def _collapse_active_reasoning(self, state: _LaneRender) -> None:
        """Freeze + collapse the lane's active reasoning once the answer begins.

        Reasoning precedes a completion's answer/tool calls, so the first text
        or tool event marks it complete. Runs once per step (a collapsed region
        short-circuits), stopping the reasoning stream and flushing the full
        text before it folds away.
        """
        box = state.active_box
        if box is not None and box.reasoning is not None and not box.reasoning.collapsed:
            await box.reasoning.finish_stream()
            if state.active_reasoning:
                box.reasoning.set_text(state.active_reasoning)
            box.reasoning.mark_done()
            box.reasoning.collapsed = True

    async def _on_turn_start(self, state: _LaneRender) -> None:
        await self._flush(state)
        await self._collapse_active_reasoning(state)
        state.active_text = ""
        state.active_reasoning = ""
        state.active_box = self._start_step(state)
        self.scroll_to_tail()

    async def _on_reasoning_delta(self, state: _LaneRender, delta: str) -> None:
        if not delta or state.active_box is None:
            return
        state.active_reasoning += delta
        region = state.active_box.ensure_reasoning()
        await region.append_delta(delta)
        self.scroll_to_tail()

    async def _on_text_delta(self, state: _LaneRender, delta: str) -> None:
        if not delta or state.active_box is None:
            return
        # Answer content has begun — this step's reasoning is complete.
        await self._collapse_active_reasoning(state)
        state.active_text += delta
        await state.active_box.append_content_delta(delta)
        self.scroll_to_tail()

    async def _on_steer_message(self, state: _LaneRender, text: str) -> None:
        """Show a steering message the running turn has just been given.

        Reference: docs/TUI-STEERING.md §5. The core weaves it into the context
        and the log between one tool result and the next call to the model
        (``AgentLoop._deliver_steer``), so the transcript has to show it in the
        same place — a user line that appeared only after a reload would make the
        model's next answer read as a non-sequitur.

        It mounts as a step INSIDE the open exchange, in arrival order, rather
        than as a top-level bubble: this is a user turn that happened inside
        somebody else's exchange, and hoisting it out would put it above content
        that preceded it.
        """
        await self._flush(state)
        await self._collapse_active_reasoning(state)
        box = MessageBox("user", text, source="verbatim")
        if state.exchange is not None:
            state.exchange.add_step(box)
        else:
            self.mount(box)
        # The next completion opens its own step; this one is not it.
        state.active_box = None
        self.scroll_to_tail()

    async def _on_tool_call(self, state: _LaneRender, event: dict) -> None:
        if state.active_box is None:
            state.active_box = self._start_step(state)
        await self._flush(state)
        await self._collapse_active_reasoning(state)
        tc_id = event.get("id", "") or ""
        state.active_box.add_tool_call(event.get("name", ""), event.get("arguments", {}), tc_id)
        if tc_id:
            state.tool_routes[tc_id] = state.active_box
        self.scroll_to_tail()

    def _on_tool_result(self, state: _LaneRender, event: dict) -> None:
        tc_id = event.get("id", "") or ""
        result_text = str(event.get("result", ""))
        is_error = bool(event.get("is_error", False))
        blocked = bool(event.get("blocked", False))
        blocked_by = event.get("blocked_by")
        box = state.tool_routes.get(tc_id)
        if box is not None and box.set_tool_result(
            tc_id, result_text, is_error, blocked=blocked, blocked_by=blocked_by
        ):
            self.scroll_to_tail()
            return
        self.app.log(f"tool_result for unknown tool_call_id {tc_id!r}; no ToolBox to fold into")

    async def finalize_exchange(
        self,
        *,
        context: int,
        output: int,
        seconds: float | None,
        telemetry: str | None = None,
        lane: str = DEFAULT_LANE,
    ) -> None:
        """Close ``lane``'s exchange after its agent loop finishes.

        Flushes tails, then snaps the final text-only answer OUT below the
        collapsed summary so it stays visible. A trivial exchange (no tools) is
        unwrapped to just the plain answer — no grouping where there's nothing
        to group. One reparent, here, by reconstruction (Textual cannot move a
        live widget across parents).

        ``telemetry`` is the last completion's G4 readout string (from
        :func:`format_telemetry`), appended to the summary/subtitle when present;
        ``None`` (a provider that reported no timings) leaves the summary unchanged.
        """
        state = self._lanes.pop(lane, None)
        self._sync_live_timer()
        if state is None:
            self.app.log(f"finalize_exchange for lane {lane!r} with no open exchange")
            return
        await self._flush(state)
        await self._collapse_active_reasoning(state)  # freeze the last step's reasoning
        exchange = state.exchange
        if exchange is None:
            return

        await self._close_exchange(
            exchange,
            context=context,
            output=output,
            seconds=seconds,
            telemetry=telemetry,
            label=state.label,
        )
        await self._maybe_trim()
        self.scroll_to_tail()

    async def _maybe_trim(self) -> None:
        """Run the live window, if this is a moment at which it is safe to.

        Called at both turn edges — :meth:`finalize_exchange` and
        :meth:`begin_exchange` — because the end alone leaves a deferred trim
        with nowhere to run (:meth:`_claim_tail_for_trim`).

        Two conditions, and both are about not moving something a reader or a
        renderer is holding:

        * every lane must be closed. Evicting while another lane streams could
          take out that lane's own exchange, and :meth:`trim_to_cap` cuts by
          transcript position, which says nothing about which lane a widget
          belongs to.
        * the reader must be at the tail. Removing the head shifts every row
          under someone who scrolled up to read, which is precisely what
          docs/TUI-STEERING.md's scroll release exists to prevent. The trim is
          held instead, and :meth:`watch_scroll_y` runs it when they come back.

        The transcript is re-read FIRST, before either guard. A turn edge is the
        moment the app's list is known to be current, and a held trim must be
        held against what the conversation is now — not against what it was when
        the reader last stood at the bottom.
        """
        self._refresh_transcript()
        if self._lanes:
            return
        if not self._follow_tail:
            self._trim_deferred = True
            return
        self._trim_deferred = False
        await self.trim_to_cap()

    def _claim_tail_for_trim(self) -> None:
        """Take the tail back, but only when a trim is being held off by not having it.

        :meth:`_maybe_trim` defers an eviction while the reader is up in history,
        and :meth:`watch_scroll_y` is the only thing that clears the deferral —
        which it cannot do during a turn, because it returns early while a lane is
        open. So a reader who scrolls up once and then keeps prompting had an
        UNBOUNDED transcript again: measured over five 40-tool turns, 372 → 1860
        mounted widgets with the deferral still set at every turn edge
        (docs/TRANSCRIPT-WINDOW.md §10).

        The cost is that submitting pulls such a reader down to the tail. That is
        the trade :meth:`snap_window_to_tail` already makes one line earlier, for
        the same reason: a turn beginning is the present changing. It is taken
        ONLY when there is a held trim, so the ordinary turn — nothing over the
        cap, or a reader already at the bottom — moves nobody.
        """
        if self._trim_deferred:
            self._follow_tail = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Re-decide tail-following, then run a trim this scroll made safe.

        Extends :meth:`MessageList.watch_scroll_y`, which owns the tail decision
        itself. The trim runs through ``call_later`` because this watcher is
        synchronous and the eviction is not — and because the eviction moves
        ``scroll_y``, which would re-enter this method. :attr:`_trim_deferred` is
        cleared before the call is scheduled, so the re-entry finds nothing to do.

        Sliding the window is NOT decided here; see :meth:`_slide_at_edge` for
        why a scroll position is the wrong signal for it.
        """
        super().watch_scroll_y(old_value, new_value)
        if self._window_moving or self._lanes:
            return
        if self._trim_deferred and self._follow_tail:
            self._trim_deferred = False
            self.call_later(self.trim_to_cap)

    def _size_updated(
        self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True
    ) -> bool:
        """Re-assert the tail once the mounted content actually has a height.

        :meth:`MessageList.scroll_to_tail` scrolls to ``max_scroll_y``, and
        ``max_scroll_y`` is derived from ``virtual_size`` — which is still the
        PREVIOUS layout's answer at the moment the renderers call it, because
        :meth:`_render_window` mounts inside ``App.batch_update`` and the batch is
        what holds the layout off. A scroll against a stale (usually zero) maximum
        clamps and silently does nothing, so ``reload_messages`` opened the reader
        at the TOP of the conversation they resumed to continue, with
        :attr:`_follow_tail` still saying they were at the bottom of it.

        This hook is where ``ScrollView`` learns its new virtual size, so it is the
        earliest point at which the answer can be right —
        :meth:`TreeDetailPane._size_updated` takes the same second chance, for the
        same reason, and its comment records the same clamp.

        **Only while following.** A reader who has scrolled away released the tail
        in :meth:`watch_scroll_y`, and this must not drag them back: growing content
        pulls the view down only for someone already at the bottom, which is the
        whole of "you can read while it writes" (docs/TUI-STEERING.md §1).

        Why it was invisible in the tests: a transcript of plain messages lands on
        the tail anyway, because every ``add_message`` on the way down calls
        ``scroll_to_tail`` and the last of them runs late enough to find a measured
        layout. A transcript with tool calls rebuilds its answers through
        ``_reload_exchange`` instead, whose final height arrives with the
        ``Collapsible`` — after the last scroll anybody performs.
        """
        changed = super()._size_updated(size, virtual_size, container_size, layout)
        if self._follow_tail and self.scroll_y < self.max_scroll_y:
            self.scroll_end(animate=False, immediate=True)
        return changed

    # -- sliding the window: a scroll that pushes PAST an edge ----------------

    def _slide_at_edge(self, turns: int) -> bool:
        """Schedule a slide if this scroll is a push against an edge, else False.

        The trigger is the gesture, not the position. A reader who scrolls to the
        top ARRIVES at the top; sliding there would mean the ``⋯ N earlier`` row
        can never be looked at, let alone clicked — reaching it would load more
        and scroll it away, every time. So the first scroll takes them to the
        edge and the next one, which has nowhere left to go, moves the window.

        This is also why the trigger is not in :meth:`watch_scroll_y`. A push
        against an edge does not change ``scroll_y``, so the watcher cannot see
        it; and the watcher sees a great deal that is not a reader — mounting
        content, a resize, this class's own ``scroll_end`` — each of which lands
        on an edge and none of which is someone asking for more transcript.

        The claim is taken here, synchronously, for the reason
        :attr:`_window_moving` exists: one flick of a wheel is several events.
        """
        if self._window_moving or self._lanes:
            return False
        if turns < 0:
            if self.scroll_offset.y > 0 or self._window_start <= 0:
                return False
        elif not self.is_vertical_scroll_end or not self._elided_after:
            return False
        self._window_moving = True
        self.call_later(self.move_window, turns, self._window_generation)
        return True

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._slide_at_edge(-1):
            event.stop()
            return
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._slide_at_edge(1):
            event.stop()
            return
        super()._on_mouse_scroll_down(event)

    def action_scroll_up(self) -> None:
        """Up-arrow at the top edge loads older turns, exactly as the wheel does.

        The keyboard half. Without it a reader who never touches the mouse can
        reach the ``⋯`` row and find no way past it.
        """
        if self._slide_at_edge(-1):
            return
        super().action_scroll_up()

    def action_scroll_down(self) -> None:
        """Down-arrow at the bottom edge loads newer turns. The keyboard half of
        :meth:`_on_mouse_scroll_down`."""
        if self._slide_at_edge(1):
            return
        super().action_scroll_down()

    @staticmethod
    def _exchange_subtitle(
        context: int,
        output: int,
        seconds: float | None,
        telemetry: str | None = None,
        label: str | None = None,
    ) -> str:
        """The stats line stamped on an unwrapped (no-tool) answer. Duration is
        omitted when unknown (reload) rather than fabricated (Fail-Early).

        Two token numbers, never their sum. ``context`` is how large the prompt had
        grown by the end of this turn; ``output`` is what the turn generated. The
        single ``N tok`` this replaced was ``total_tokens``, i.e. context + output —
        which on turn 12 read as ~the whole conversation and looked like a running
        total, because it was one.

        ``telemetry`` is the last completion's G4 readout, appended as one more
        ``·`` part when present; ``None`` appends nothing.

        ``label`` is the lane's origin badge and leads the line when present
        (B3-b), because this subtitle is the ONLY chrome an unwrapped answer has
        left: the exchange that carried the badge is removed on this path."""
        parts = [f"{format_tokens(context)} ctx", f"{format_tokens(output)} out"]
        if seconds is not None:
            parts.append(format_duration(seconds))
        if telemetry is not None:
            parts.append(telemetry)
        if label is not None:
            parts.insert(0, label)
        return " · ".join(parts)

    async def _close_exchange(
        self,
        exchange: ExchangeBox,
        *,
        context: int,
        output: int,
        seconds: float | None,
        telemetry: str | None = None,
        label: str | None = None,
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
        answers = [b for b in steps if b.role == "assistant"]
        final = answers[-1] if answers and not answers[-1].tool_boxes else None
        if final is not None and not final.content_text.strip() and final.reasoning is None:
            final = None

        promoted = None
        if final is not None:
            promoted = await self._promote_answer(final, after=exchange, label=label)

        if tool_count == 0 and len(answers) == len(steps):
            if promoted is not None:
                promoted.set_subtitle(
                    self._exchange_subtitle(context, output, seconds, telemetry, label)
                )
            exchange.remove()
        else:
            if final is not None:
                final.remove()
            exchange.collapsed = True
            exchange.set_summary(
                tools=tool_count,
                context=context,
                output=output,
                seconds=seconds,
                telemetry=telemetry,
            )

    async def _promote_answer(
        self, src: MessageBox, *, after: Widget, label: str | None = None
    ) -> MessageBox:
        """Mount a fresh top-level answer box copied from ``src``, after ``after``.

        Reconstructs rather than reparents (Textual has no cross-parent move).
        The terminal answer is text + optional reasoning (no tools), so copying
        its text and reasoning string is faithful and cheap.

        The copied reasoning is mounted collapsed (D1): ``ReasoningRegion``
        defers the actual Markdown parse until the region is expanded, so
        copying a long reasoning string here no longer means parsing it for a
        Contents container nobody can see (measured 104ms at 2.2k reasoning
        tokens before the fix). ``region.text`` still returns the real string
        immediately either way -- only the widget-side parse is deferred.

        ``label`` is copied too (B3-b). Promotion moves the answer OUT of the
        exchange to top level, where the primary transcript lives; a fork's
        answer arriving there unbadged is the one place a sub-agent's text could
        be read as the main agent's.
        """
        new = MessageBox("assistant", src.content_text, label or "", source="markdown")
        if label is not None:
            new.add_class(LANE_FOREIGN_CLASS)
        await self.mount(new, after=after)
        if src.reasoning is not None:
            region = new.ensure_reasoning()
            region.set_text(src.reasoning.text)
            region.mark_done()
            region.collapsed = True
        return new

    def render_cap_start(self, messages: list[dict]) -> int:
        """Index of the first message :meth:`reload_messages` will MOUNT.

        Walks backwards from the end and stops at whichever bound is reached
        first: :attr:`RENDER_CAP_TURNS` user turns, or
        :attr:`RENDER_CAP_MESSAGES` messages. The larger index wins, because
        walking backwards the bound that cuts more is the one reached first.

        The answer is always a ``user`` message, so a user→answer span is never
        cut in half. That is also why the message bound cannot be applied
        literally: it lands wherever it lands, so the true start is the LAST user
        message that still leaves the span within the bound.

        A single span longer than the message bound mounts whole rather than
        being cut, and a transcript with no user message at all mounts whole.
        Rendering nothing is not a smaller version of rendering something.
        """
        users = [i for i, m in enumerate(messages) if m.get("role", "") == "user"]
        if not users:
            return 0
        by_turns = users[-self.RENDER_CAP_TURNS] if len(users) > self.RENDER_CAP_TURNS else 0
        starts = [0, *users]
        within = [s for s in starts if len(messages) - s <= self.RENDER_CAP_MESSAGES]
        by_count = within[0] if within else users[-1]
        return max(by_turns, by_count)

    def window_end(self, messages: list[dict], start: int) -> int:
        """One past the last message a window starting at *start* mounts.

        The forward twin of :meth:`render_cap_start`, bound by the same two caps,
        and required to AGREE with it at the tail::

            window_end(m, render_cap_start(m)) == len(m)

        That identity is what lets one pair of numbers describe both a window the
        cap placed and a window the reader slid — without it, attaching to the
        tail would leave a phantom ``⋯ 0 later`` row, or a reload and a
        scrolled-back-then-forward window would mount different things.

        Turns are counted from the window's FIRST turn, not from *start*. The two
        differ when a leading system message sits above it: ``start`` is then 0
        and the first turn opens at 1, and counting from ``start`` would spend one
        of the four turns on a message that renders nothing.
        """
        n = len(messages)
        users = [i for i, m in enumerate(messages) if m.get("role", "") == "user" and i >= start]
        if not users:
            return n
        later = [u for u in users if u > users[0]]
        by_turns = later[self.RENDER_CAP_TURNS - 1] if len(later) >= self.RENDER_CAP_TURNS else n
        limit = start + self.RENDER_CAP_MESSAGES
        if limit >= n:
            by_count = n
        else:
            within = [u for u in later if u <= limit]
            by_count = within[-1] if within else (later[0] if later else n)
        return min(by_turns, by_count)

    def turn_starts(self, messages: list[dict]) -> list[int]:
        """Every message index a window may legally start at, ascending.

        The user messages, plus 0 — the same candidate set
        :meth:`render_cap_start` picks from, so a window the reader slides can
        only ever land where the cap could have put it. Sliding is therefore
        movement along this list, one entry per step, which is why a move never
        cuts a turn in half and never needs to ask how tall anything is.
        """
        users = [i for i, m in enumerate(messages) if m.get("role", "") == "user"]
        return sorted({0, *users})

    async def _render_window(self, messages: list[dict], start: int, end: int) -> None:
        """Mount ``messages[start:end]`` as the whole transcript view.

        The one renderer. :meth:`reload_messages` calls it with the tail, and
        :meth:`move_window` with a span the reader slid to; neither has a second
        way to build a box, which is what keeps a scrolled-back view identical to
        the view a reload of the same span would produce.

        The mounting runs inside ``App.batch_update``, which holds off the screen
        layout until it is done. Without it every awaited mount hands control
        back to the event loop, Textual's screen timer fires, and the ENTIRE
        widget tree is re-arranged — 78 to 104 full layout passes on a
        200-message reload, each over a tree that is still growing. Batched it is
        5, and the reader sees the finished transcript rather than it being
        assembled a message at a time.

        Leaves the scroll position alone. The two callers want different ones —
        a reload re-attaches to the tail, a move holds the reader's place — and
        neither can be derived from the span.
        """
        self._building = True
        await self.clear_messages()
        self._window_start = start
        self._window_end = end
        self._window_generation += 1
        self._elided = sum(1 for m in messages[:start] if m.get("role", "") != "system")
        self._elided_after = sum(1 for m in messages[end:] if m.get("role", "") != "system")
        anchors: dict[int, Widget] = {}
        self._follow_tail = False
        with self.app.batch_update():
            if self._elided:
                await self.mount(self._earlier_row())
            i = start
            while i < end:
                role = messages[i].get("role", "")
                if role == "system":
                    i += 1
                    continue
                turn_at = i
                if role == "user":
                    # The user box sits above the exchange, as in the live path.
                    boxes = self.add_persisted_message(messages[i])
                    if boxes:
                        anchors[turn_at] = boxes[0]
                    i += 1
                span: list[dict] = []
                while i < end and messages[i].get("role") not in ("user", "system"):
                    # Filtered HERE so a span of only hidden nodes mounts no empty exchange.
                    if is_displayed(messages[i]):
                        span.append(messages[i])
                    i += 1
                if span:
                    await self._reload_exchange(span)
            if self._elided_after:
                await self.mount(self._later_row())
        if start not in anchors:
            content = [c for c in self.children if isinstance(c, (MessageBox, ExchangeBox))]
            if content:
                anchors[start] = content[0]
        self._turn_anchors = anchors
        self.call_after_refresh(self._finish_build)

    def _finish_build(self) -> None:
        """End the build, and land where the build's own scrolling was aiming.

        Clears :attr:`_building`, and re-asserts the tail for a caller that wanted
        it. The re-assertion is not belt-and-braces: the collapses performed during
        the build each schedule a ``scroll_visible`` of their own
        (``Collapsible._watch_collapsed``), and those were queued BEFORE this
        callback, so the last word about the scroll position would otherwise
        belong to whichever box happened to fold last — three rows short of the
        newest message, in the case that started this.

        Only when following. :meth:`move_window` rebuilds through the same method
        with :attr:`_follow_tail` false and puts the reader back itself
        (:meth:`_settle_move`), which runs after this.
        """
        self._building = False
        if self._follow_tail:
            self.scroll_to_tail()

    async def move_window(self, turns: int, generation: int | None = None) -> bool:
        """Slide the mounted window *turns* turns along the transcript.

        Negative moves back into history, positive forward toward the tail.
        Returns whether it actually moved: at either end of the transcript, with
        a lane still streaming, during another move, or — when *generation* is
        given — if the window has been rebuilt since the move was decided on.

        *generation* is how :meth:`watch_scroll_y` says "act on the window I saw".
        The watcher decides synchronously and the move runs a tick later, and a
        reload or a :meth:`snap_window_to_tail` can land in between; without the
        check, a queued step back undoes whichever of them overtook it. A caller
        that means "now", including every test, passes nothing.

        **The window is bounded, not anchored.** Sliding back mounts older turns
        and drops the same number of newer ones, so the mounted count — which is
        the only thing the render cost depends on (docs/TRANSCRIPT-WINDOW.md §1)
        — does not change. That is what makes reading history free rather than a
        slow return of the defect the window was built to fix.

        Movement is along :meth:`turn_starts`, one entry per step, so a step is
        always one whole turn and never asks how tall anything is.

        The reader's place is held across the re-render by scrolling a turn they
        were already looking at back under the viewport's top edge: moving back
        that is the turn that WAS at the top, moving forward it is the last turn
        of the old window. Either way something they had just read stays on
        screen, which is what makes repeated steps read as scrolling rather than
        as paging.
        """
        self._window_moving = True
        handed_off = False
        try:
            if generation is not None and generation != self._window_generation:
                return False
            if self._lanes:
                return False
            messages = self._reload_source
            if not messages:
                return False
            candidates = self.turn_starts(messages)
            position = max(0, bisect_right(candidates, self._window_start) - 1)
            last = max(0, bisect_right(candidates, self.render_cap_start(messages)) - 1)
            moved_to = min(max(position + turns, 0), last)
            new_start = candidates[moved_to]
            if new_start == self._window_start:
                return False
            old_start = self._window_start
            old_last = max((c for c in candidates if c < self._window_end), default=old_start)

            await self._render_window(messages, new_start, self.window_end(messages, new_start))

            self.call_after_refresh(
                self._settle_move,
                old_start if turns < 0 else old_last,
                self._window_generation,
            )
            handed_off = True
            return True
        finally:
            if not handed_off:
                self._window_moving = False

    def _settle_move(self, anchor_at: int, generation: int) -> None:
        """Put the reader back on *anchor_at* once the moved window has laid out.

        The second half of :meth:`move_window`, split off because the first half
        cannot see where anything is. Releases :attr:`_window_moving`, which is
        what re-arms :meth:`watch_scroll_y` — deliberately last, so the scroll
        this method performs is not read as the reader asking for another move.

        Carries the same generation check :meth:`move_window` does, and for a
        sharper reason: a reload that lands between the move and this refresh has
        already scrolled the view where it wants it, and an anchor scroll from
        the window it replaced would drag the reader off the tail it just
        attached to.
        """
        if generation != self._window_generation:
            self._window_moving = False
            return
        try:
            anchor = self._turn_anchors.get(anchor_at)
            if anchor is not None:
                self.scroll_to_widget(anchor, top=True, animate=False, immediate=True)
            self._follow_tail = self.is_vertical_scroll_end
        finally:
            self._window_moving = False

    async def reload_messages(self, messages: list[dict], *, cap: bool = True) -> None:
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

        ``cap=True`` mounts only the tail :meth:`render_cap_start` names and
        writes a ``⋯ N earlier`` row above it; ``cap=False`` mounts everything.
        The whole list is loaded either way — see :attr:`RENDER_CAP_MESSAGES`.

        A reload always lands on the TAIL, whatever the reader had slid the
        window to. It replaces the transcript — resume, compact, rollback, a
        branch swap — so a window position taken in the old document names
        nothing in the new one.
        """
        self._reload_source = messages
        start = self.render_cap_start(messages) if cap else 0
        end = self.window_end(messages, start) if cap else len(messages)
        await self._render_window(messages, start, end)
        self._follow_tail = True
        self.scroll_to_tail()

    @property
    def is_building(self) -> bool:
        """Whether a window build is still in flight.

        :meth:`reload_messages` and :meth:`move_window` return when the build has
        been SCHEDULED, not when it has landed: the last thing ``_render_window``
        does is ``call_after_refresh(self._finish_build)``, and that callback
        re-asserts the scroll position. Anything that measures geometry before
        then is racing it.

        False is necessary but not sufficient for "the transcript has landed":
        :meth:`_finish_build` clears this flag and then calls
        :meth:`scroll_to_tail`, whose ``scroll_end`` is itself deferred, so the
        position moves once more after this reads False. A caller that needs the
        final position waits for this AND for ``scroll_y`` to stop changing.
        """
        return self._building

    @property
    def elided_count(self) -> int:
        """How many messages are hidden ABOVE the window. 0 at the top of the chat.

        Set by :meth:`reload_messages` (what the reload declined to mount),
        :meth:`trim_to_cap` (what the live window has since evicted) and
        :meth:`move_window` (what the reader has slid past). One number for all
        three, because they hide the head of the same transcript for the same
        reason.

        This is the count the ``⋯ N earlier`` row states, and it deliberately
        does NOT include what a slid window hides BELOW it — see
        :attr:`later_count`. Two rows, two questions, two numbers.
        """
        return self._elided

    @property
    def later_count(self) -> int:
        """How many messages are hidden BELOW the window.

        Non-zero only while the reader has slid the window back into history:
        every other state has the window on the tail, where there is nothing
        below it. :meth:`snap_window_to_tail` reads this to decide whether a
        starting turn has anything to snap back from.
        """
        return self._elided_after

    @property
    def hidden_count(self) -> int:
        """Everything :meth:`show_all_messages` would mount, both directions."""
        return self._elided + self._elided_after

    def set_transcript_source(self, source: Callable[[], list[dict]]) -> None:
        """Tell this display where to read the app's CURRENT transcript.

        A callable, not a list: the app rebinds its working list after every turn
        (``self.messages = list(session.context)``), so a display holding the list
        object answers for the conversation as it stood at the last reload. That
        staleness is only a bug once something reads the transcript BETWEEN
        reloads, which is exactly what the live window does —
        :meth:`trim_to_cap` decides where to cut from it, and
        :meth:`show_all_messages` mounts it back.

        Optional. A bare renderer harness that never calls this keeps the old
        behaviour: :attr:`_reload_source`, whatever the last reload was handed.

        Not read here and now. :meth:`_refresh_transcript` calls it at each turn
        edge, which is the moment the app's list is known to be current — reading
        it at any other moment is how a display ends up holding a transcript that
        belongs to neither the screen nor the session.
        """
        self._transcript_source = source

    def _refresh_transcript(self) -> None:
        """Re-read the app's transcript into :attr:`_reload_source`.

        Called at every turn edge (:meth:`_maybe_trim`), which is what keeps that
        attribute meaning "the transcript this display is a view of" rather than
        "the list the last reload happened to be handed". The two writers are this
        and :meth:`reload_messages`, and they write the same kind of thing.

        A display with no source — a bare renderer harness — keeps whatever
        ``reload_messages`` gave it. That is not a fallback for a missing value:
        such a display has no app behind it, so the list it was handed is the only
        transcript in existence.
        """
        if self._transcript_source is not None:
            self._reload_source = self._transcript_source()

    async def trim_to_cap(self) -> int:
        """Evict the head of a LIVE transcript down to the same cap a reload uses.

        Returns the number of top-level widgets removed; 0 when this display has
        no transcript, when the one it has already fits the cap, or when the cut
        the cap names is already the top of the mounted tree.

        It runs again after :meth:`show_all_messages`, at the end of the next
        turn. That is deliberate rather than overlooked: "show them" mounts the
        whole conversation to be READ, and the reader is not prompting while they
        read, so the re-trim lands when they have moved on. Suppressing the
        window after an explicit show-all would be the unbounded transcript back,
        by request.

        A live session grew without bound before this existed. Only
        :meth:`reload_messages` ever applied :attr:`RENDER_CAP_TURNS`, so a
        transcript that was capped when it was opened climbed straight back past
        the cap as the reader worked in it — and the render cost climbs with it
        (see the cap's own comment for the measurements).

        **The cut point is a user message, in both representations at once.**
        :meth:`render_cap_start` names it as an index into the message list; this
        walks the top-level children backwards for the same user box. That is
        what makes the eviction and the ``⋯ N earlier`` count describe the same
        place: one top-level user ``MessageBox`` is mounted per user message, so
        counting user boxes from the end and counting user messages from the end
        arrive together. Cutting anywhere else — at a widget budget, say, which
        is what the cost law is actually written in — would strand an
        :class:`ExchangeBox` above the user turn that opened it, and leave the
        count with nothing true to say.

        Streams are stopped before the removal, exactly as
        :meth:`clear_messages` does it. Nothing should be streaming here (the
        caller only trims with every lane closed), so this is belt and braces
        rather than a live case — but a ``MarkdownStream`` left running on a
        removed box leaks its task forever, and that is not a failure worth
        risking on a should.
        """
        messages = self._reload_source
        if not messages:
            return 0
        if self._elided_after:
            return 0
        start = self.render_cap_start(messages)
        if start == 0:
            return 0
        keep_users = sum(1 for m in messages[start:] if m.get("role", "") == "user")
        if keep_users == 0:
            return 0

        content = [c for c in self.children if isinstance(c, (MessageBox, ExchangeBox))]
        seen = 0
        cut = 0
        for index in range(len(content) - 1, -1, -1):
            child = content[index]
            if isinstance(child, MessageBox) and child.role == "user":
                seen += 1
                if seen == keep_users:
                    cut = index
                    break
        if cut == 0:
            return 0

        evicted = content[:cut]
        for child in evicted:
            boxes = list(child.query(MessageBox))
            if isinstance(child, MessageBox):
                boxes.append(child)
            # Not ``box``: that name is a module-level import here (ruff F402).
            for message_box in boxes:
                if message_box.reasoning is not None:
                    await message_box.reasoning.finish_stream()
                await message_box.finish_stream()
            await child.remove()

        self._elided = sum(1 for m in messages[:start] if m.get("role", "") != "system")
        self._window_start = start
        self._window_end = len(messages)
        await self._sync_earlier_row()
        self.scroll_to_tail()
        return len(evicted)

    async def _sync_earlier_row(self) -> None:
        """Put the ``⋯ N earlier`` row above the transcript, or update the one there.

        Updated in place when it already exists, rather than removed and
        remounted: the row is a widget like any other, and churning it every turn
        is the cost this whole window exists to stop paying.
        """
        existing = self.query(".chat-fold")
        if existing:
            existing.first(Static).update(self._earlier_text())
            return
        row = self._earlier_row()
        survivors = [c for c in self.children if isinstance(c, (MessageBox, ExchangeBox))]
        if survivors:
            await self.mount(row, before=survivors[0])
        else:
            await self.mount(row)

    def _earlier_text(self) -> str:
        """What the top ``⋯`` row says. Shared so the mounted row and an updated
        one cannot word the same count differently."""
        return f"⋯ {self._elided} earlier · scroll up to load, click for all"

    def _later_text(self) -> str:
        """What the bottom ``⋯`` row says. Present only while the reader has slid
        the window back, which is the only way messages end up BELOW it."""
        return f"⋯ {self._elided_after} later · scroll down to load, click for all"

    def _earlier_row(self) -> Static:
        """The ``⋯ N earlier`` row that stands where the elided messages would be.

        A count rather than a blank gap, matching :class:`TreeDetailPane`'s row,
        because a gap does not say anything and this does. It names BOTH its
        gestures: scrolling into it slides the window one turn back
        (:meth:`move_window`), and clicking it mounts the whole conversation. The
        second is also in the command palette, so neither the mouse nor the
        keyboard is a dead end.
        """
        row = Static(self._earlier_text(), classes="chat-fold")
        row.tooltip = "Scroll into this row to load older turns, or click to mount them all."
        return row

    def _later_row(self) -> Static:
        """The ``⋯ N later`` row, the bottom half of a slid window.

        Carries ``chat-fold`` as well as its own class so it gets the same
        styling, the same click, and the same removal in :meth:`clear_messages` —
        the two rows are one idea pointing in two directions, and giving the new
        one its own vocabulary would be two things to keep in step.
        """
        row = Static(self._later_text(), classes="chat-fold chat-fold-later")
        row.tooltip = "Scroll into this row to load newer turns, or click to mount them all."
        return row

    async def on_click(self, event: events.Click) -> None:
        """Show the whole transcript when the reader clicks the ``⋯`` row."""
        widget = getattr(event, "widget", None)
        if widget is not None and widget.has_class("chat-fold"):
            await self.show_all_messages()

    async def show_all_messages(self) -> None:
        """Re-render the WHOLE current transcript, with no cap.

        A no-op when nothing was elided, so the palette entry is safe to invoke
        at any time. It is deliberately not cheap: mounting the rest costs the
        same superlinear layout the cap avoided, which is why it is a gesture the
        reader asks for rather than something scrolling triggers.

        :attr:`_reload_source` is re-read from the app at every turn edge
        (:meth:`_refresh_transcript`), so what this mounts includes the live turns
        the window put behind the ``⋯`` row and no reload ever saw. Before that
        refresh existed this method could only restore the conversation as it
        stood when it was opened: the reader would ask to see more and be shown
        less.

        Guarded on :attr:`hidden_count`, not on :attr:`elided_count`: a reader who
        has slid the window all the way back to the top has nothing hidden above
        them and most of the conversation hidden below, and the row they clicked
        to get here is the ``⋯ N later`` one.
        """
        if not self.hidden_count:
            return
        await self.reload_messages(self._reload_source, cap=False)

    async def _reload_exchange(self, span: list[dict]) -> None:
        """Rebuild one user→answer span (assistant + toolResult messages) as a
        collapsed exchange, then close it out exactly like the live path.

        The duration comes from the span's own message timestamps, which are the
        agent loop's clock — the same one the live path reads off its events — so
        a reloaded exchange reports the span a live one reported
        (docs/MESSAGE-TIMESTAMPS.md §3)."""
        exchange = ExchangeBox()
        await self.mount(exchange)
        routes: dict[str, ToolBox] = {}
        # Mirrors the live path (TurnStream): output sums, context replaces.
        output = 0
        context = 0
        for msg in span:
            role = msg.get("role", "")
            if role == "assistant":
                step = MessageBox("assistant", "", source="markdown")
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
                    output += int(usage.get("output_tokens", 0) or 0)
                    context = prompt_tokens(usage)
            elif role == "toolResult":
                tc_id = msg.get("tool_call_id", "") or ""
                target = routes.get(tc_id)
                result_text = _join_text_blocks(msg.get("content", []))
                if target is not None:
                    target.set_result(result_text, bool(msg.get("is_error", False)))
                else:
                    self.app.log(f"reload: toolResult for unknown tool_call_id {tc_id!r}")
            else:
                self.add_persisted_message(msg)
        await self._close_exchange(
            exchange, context=context, output=output, seconds=span_seconds(span)
        )
