"""
Parley - A minimalist, performant chat interface for LLMs.

Clean, simple, fast. Built with Textual.
"""

from rich import box
from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
    Static,
    Input,
    Header,
    Footer,
    Markdown,
    Button,
    TextArea,
    Tree,
    OptionList,
    Checkbox,
    RadioSet,
    RadioButton,
    SelectionList,
)
from textual.widgets.markdown import MarkdownStream
from textual.binding import Binding
from textual.reactive import reactive
from textual import events, work
from textual.message import Message
from textual.screen import ModalScreen
from textual.worker import get_current_worker
from dataclasses import dataclass
from datetime import datetime, timedelta
import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from uuid import uuid4

from tau_coding_agent.backends import (
    DEFAULT_LANE,
    Backend,
    RenderRouter,
    create_backend,
    make_model_resolver,
    resolve_tool_names,
)
from tau_coding_agent.tagline import pick_tagline
from tau_coding_agent.headless import _append_system_prompt, resolve_extensions_config

# Session persistence lives in a Textual-free module so `tau -p` can save
# sessions without importing the TUI. Sessions are append-only JSONL transcripts
# partitioned by cwd (docs/SESSION-UX-REDESIGN.md); the TUI keeps a live working
# message list and funnels each produced message through Session.append_message.
# Construction/lookup goes through the storage-agnostic SessionCatalog seam (W10)
# rather than the concrete Session directly, so the TUI never hardcodes the file
# store either.
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_coding_agent.config import TAU_DIR, bootstrap_config, update_config
from tau_coding_agent.session_picker import SessionPickerModal
from tau_coding_agent.session_store import (
    subscribe_session_events,
)
from tau_coding_agent.store_factory import build_session_catalog, resolve_backend_name

# The pure session-tree algebra lives in tau-agent-core (the loop's package, not
# the TUI); the tree-browser (§3) is a view over ConversationTree.tree().
from tau_agent_core.conversation_tree import ConversationTree, TreeNode

# The declarative ``ui.form`` spec validator — the single source of truth the
# generic ``ExtensionFormScreen`` shares with ``ExtensionUI.form`` (E10 §6 / S66).
# ``ui.panel`` (S68) normalizes in ``ExtensionUI.panel`` before reaching the delegate,
# so ``ExtensionPanel`` renders the already-normalized ``{title, body, actions}`` dict.
from tau_agent_core.extension_types import validate_form_spec

# The result of running an extension command (handled flag + the handler's
# returned output) — the command output channel (E7 §3 / S46).
from tau_agent_core.agent_session import ExtensionCommandResult

# Extension load result + the read-only per-extension summary the /extensions
# palette listing renders (E5 §5 / S34).
from tau_agent_core.sdk import LoadExtensionsResult, summarize_extensions

# The submission record every input source funnels through
# (docs/SUBMISSION-LIFECYCLE.md "The one door"). The TUI constructs one per typed
# prompt in ``on_input_submitted`` — it is a SOURCE like any other, not a
# privileged path into the loop.
from tau_agent_core.submission import Submission

# Command dispatch (docs/SUBMISSION-LIFECYCLE.md submit() step 3 / B2-b). The DECISION
# — "this input is command X with arguments Y" — is the core's and is shared with
# ``AgentSession.submit``; performing a frontend-shaped outcome (a modal, a panel, a
# transcript re-render) is this app's, and failing to be able to is an exception.
from tau_agent_core.commands import (
    CommandOutcome,
    UnsupportedCommandError,
    resolve_command,
    unsupported_command_message,
)

# Collapsible chat components. MessageBox (below) is the universal per-message
# host; these are the children it composes — one reasoning region and N tool
# boxes — plus the exchange grouping used by the streaming state machine.
from tau_coding_agent.chat_widgets import (
    ContentSource,
    ExchangeBox,
    MarkdownLineFormatter,
    ReasoningRegion,
    ToolBox,
    format_duration,
    format_telemetry,
    format_tokens,
)


class _ExtensionUIDelegate:
    """Paints a loaded extension's ``api.notify(...)`` onto the live TUI (E5 §4 / S33).

    Bound onto every extension's shared ``ExtensionContext`` via
    ``TauBackend.set_ui_delegate`` → ``AgentSession.set_ui_delegate`` after each
    ``create_backend``, so ``api.ui.notify(msg, level)`` reaches the Textual screen
    instead of the headless stderr sink. Extension hooks run on the app's event
    loop (the generation worker is async, not threaded), so ``App.notify`` is
    called directly.

    S33 wired ``notify`` only. E7 §3 / S47 now wires the interactive dialogs
    (``confirm`` / ``select`` / ``input``) onto real ``ModalScreen`` overlays
    (:class:`ExtensionConfirmModal` / :class:`ExtensionSelectModal` /
    :class:`ExtensionInputModal`), pushed via ``push_screen_wait`` so the extension
    hook awaits the user's answer. Extension hooks run inside the generation worker
    (async, not threaded), which is the worker context ``push_screen_wait`` needs.
    A cancelled ``confirm`` resolves to ``False`` — flipping into TUI mode must not
    turn a ``confirm`` prompt into a hidden "yes" (Fail-Early).
    """

    #: extension notify level → Textual ``App.notify`` severity.
    _SEVERITY: dict[str, Literal["information", "warning", "error"]] = {
        "info": "information",
        "warning": "warning",
        "error": "error",
    }

    def __init__(self, app: "Parley") -> None:
        self._app = app

    def notify(self, message: str, level: str = "info") -> None:
        self._app.notify(message, severity=self._SEVERITY.get(level, "information"))

    async def confirm(self, title: str, message: str) -> bool:
        return await self._app.push_screen_wait(ExtensionConfirmModal(title, message))

    async def select(self, title: str, items: list[str]) -> str | None:
        return await self._app.push_screen_wait(ExtensionSelectModal(title, items))

    async def input(self, title: str, default: str = "") -> str:
        # The modal dismisses with None on cancel (Esc / Cancel); the ExtensionUI
        # contract is ``-> str``, so a cancelled prompt resolves to the default
        # (the same value the headless path returns), never a fabricated "".
        result = await self._app.push_screen_wait(ExtensionInputModal(title, default))
        return result if result is not None else default

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        # One generic screen renders every field kind (E10 §6 / S66). Submit
        # dismisses with the ``{name: value}`` dict; Esc/Cancel dismisses with
        # ``None`` (a cancelled form is not a fabricated answer set — Fail-Early).
        return await self._app.push_screen_wait(ExtensionFormScreen(spec))

    def set_status(self, key: str, text: str | None) -> None:
        # Ambient keyed slot in the footer status strip (E10 §6 / S67). Non-blocking
        # (no dialog to await): forwards to the app, which updates the single
        # ``ExtensionStatusBar`` slot in place; ``text=None`` clears it. Extension
        # hooks run on the app's event loop, so the widget mutation is direct.
        self._app.set_extension_status(key, text)

    def panel(self, key: str, spec: dict[str, Any] | None) -> None:
        # Persistent keyed panel (E10 §6 / S68). Non-blocking (not a dialog): forwards
        # to the app, which mounts / updates / removes the keyed ``ExtensionPanel`` in
        # the panel host; ``spec=None`` clears it. ``spec`` is already the normalized
        # ``{title, body, actions}`` dict from ``ExtensionUI.panel``.
        self._app.set_extension_panel(key, spec)


class ExtensionStatusBar(Static):
    """One-line footer strip of keyed extension status slots (E10 §6 / S67).

    The TUI surface behind ``ctx.ui.set_status(key, text)`` (pi's ``setStatus``,
    types.ts:141): ambient, live state — e.g. budget proximity ticking each turn.
    Each ``key`` names a SLOT; :meth:`set_slot` UPDATES that slot in place on a
    re-call (never appends a duplicate) and REMOVES it when ``text is None`` (pi's
    "pass undefined to clear"). Slots render in first-seen order (an insertion-
    ordered dict) joined by a thin separator, so the strip reads left-to-right in a
    stable order across updates.

    When no slots remain the strip hides itself (``display = False``) so it costs
    zero rows — it only occupies its one line while at least one extension has
    something live to show. It sits just above the built-in ``Footer`` in the app's
    vertical flow (not docked), so the two stack cleanly.
    """

    _SEPARATOR = "  │  "

    def __init__(self) -> None:
        super().__init__("", id="ext-status-bar")
        # Insertion-ordered slots: {key: text}. A dict preserves first-seen order,
        # so an in-place update of an existing key keeps its position.
        self._slots: dict[str, str] = {}
        self.display = False

    def set_slot(self, key: str, text: str | None) -> None:
        """Set, update, or clear one keyed slot, then re-render the strip."""
        if text is None:
            self._slots.pop(key, None)
        else:
            self._slots[key] = text
        if self._slots:
            self.display = True
            self.update(self._SEPARATOR.join(self._slots.values()))
        else:
            # Nothing live to show — collapse the strip to zero rows rather than
            # leave an empty bar.
            self.display = False
            self.update("")


class LaneStrip(Static):
    """One-line footer strip naming every FOREIGN lane currently streaming (B3-b).

    Reference: docs/SUBMISSION-LIFECYCLE.md phase 3. The transcript shows a
    foreign lane's *content* — badged bubbles, a labelled exchange — but content
    scrolls, and a forked sub-agent that runs for two minutes inside a collapsed
    exchange three screens up is running invisibly. This is the ambient half: while
    anything the user did not type is in flight, one line says so, and it says
    which.

    Deliberately a separate widget from :class:`ExtensionStatusBar` rather than a
    slot in it. That bar's slots are an EXTENSION's to name (``ctx.ui.set_status``
    keys come from extension code), so lane activity living there would be one
    ``set_status("lanes", …)`` away from being silently overwritten by the very
    extension whose fork it is reporting.

    Same idiom as that bar, though — an insertion-ordered dict of live entries,
    joined by a thin separator, hidden (``display = False``) at zero entries so it
    costs no rows on an ordinary session. Only foreign lanes are listed: the
    frontend's own typed turn already has the input disabled and the header
    subtitle to say it is working, and a strip that lit up for every prompt would
    be the noise the badge rules exist to avoid.
    """

    _SEPARATOR = "  │  "

    def __init__(self) -> None:
        super().__init__("", id="lane-strip")
        # lane id -> origin badge, in the order the lanes opened.
        self._lanes: dict[str, str] = {}
        self.display = False

    def open_lane(self, lane: str, label: str | None) -> None:
        """Track ``lane`` as live under its origin badge.

        ``label is None`` is this frontend's own typed turn, which the strip does
        not report — not a filtered-out source, a lane the reader is already
        looking at.
        """
        if label is None:
            return
        self._lanes[lane] = label
        self._render_strip()

    def close_lane(self, lane: str) -> None:
        """Drop ``lane`` from the strip. A lane it never tracked is a no-op —
        that is the ordinary interactive lane ending."""
        if self._lanes.pop(lane, None) is not None:
            self._render_strip()

    def clear_lanes(self) -> None:
        """Forget every tracked lane (a backend/session swap abandons them)."""
        if self._lanes:
            self._lanes = {}
            self._render_strip()

    @property
    def lanes(self) -> dict[str, str]:
        """The live lanes, ``{lane: badge}``, in open order."""
        return dict(self._lanes)

    @property
    def summary(self) -> str:
        """The line this strip currently shows — ``""`` when it is hidden.

        Derived from :attr:`lanes` rather than cached, so what the strip says and
        what it is tracking cannot drift; the widget's own text is set from here.
        """
        if not self._lanes:
            return ""
        count = len(self._lanes)
        noun = "lane" if count == 1 else "lanes"
        return f"⑂ {count} other {noun}: " + self._SEPARATOR.join(self._lanes.values())

    def _render_strip(self) -> None:
        summary = self.summary
        self.display = bool(summary)
        self.update(summary)


def render_panel_body(body: dict[str, Any]) -> RenderableType:
    """Render a normalized ``ui.panel`` body dict to the panel's body renderable (S68).

    Pure (no widget access) so it is unit-testable: given a ``{"kind": …}`` body from
    :func:`~tau_agent_core.extension_types.validate_panel_spec` it returns what the
    panel's body :class:`Static` shows. ``text`` → the string as-is; ``list`` → one
    ``• item`` line per entry; ``table`` → a Rich :class:`~rich.table.Table` styled as
    the same monospace grid (a header row, a rule row, then the data rows).

    The table is a *renderable* rather than a pre-padded string because the width it
    has to fit is not knowable here: the body ``Static`` is built in
    :meth:`ExtensionPanel.compose`, before the compositor has given the panel a
    region, and that region changes again on every terminal resize. A grid padded to
    its widest cell overflows any panel narrower than the sum of those cells, and the
    ``Static`` then soft-wraps it mid-row into unreadable fragments. A ``Table`` is
    measured against the console width it is actually handed, so it divides that width
    between the columns, wraps a cell that has somewhere to wrap, and marks one it had
    to cut with an ellipsis.
    """
    kind = body["kind"]
    if kind == "text":
        return str(body["text"])
    if kind == "list":
        return "\n".join(f"• {item}" for item in body["items"])
    # table
    columns: list[str] = body["columns"]
    rows: list[list[str]] = body["rows"]
    # SIMPLE_HEAD is the header rule and nothing else; its column divider is a space,
    # which with the right padding reproduces the two-space gap of the old grid.
    table = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=None,
    )
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    return table


class _PanelActionButton(Button):
    """A panel action button carrying the command it dispatches (E10 §6 / S68).

    Subclasses :class:`Button` to attach the ``command``/``args`` an action declares,
    so :meth:`ExtensionPanel.on_button_pressed` can map a press straight to a command
    dispatch without brittle id-parsing — and so an ordinary ``Button`` elsewhere in a
    panel (were one added) would not be mistaken for an action.
    """

    def __init__(self, label: str, command: str, args: str) -> None:
        super().__init__(label, classes="ext-panel-action")
        self.command = command
        self.args = args


class ExtensionPanel(Vertical):
    """One persistent keyed panel from a declarative spec (E10 §6 / S68).

    The TUI surface behind ``ctx.ui.panel(key, spec)`` (D-E6-4: a plain-data SPEC, not
    a widget factory) — the fleet-dashboard primitive. Renders the normalized
    ``{title, body, actions}`` from
    :func:`~tau_agent_core.extension_types.validate_panel_spec`: a title
    :class:`Static`, a body :class:`Static` (via :func:`render_panel_body` — text /
    bullet list / table grid), and, when the spec declares ``actions``, a row of
    :class:`_PanelActionButton`. Pressing an action posts an :class:`Action` message
    that bubbles to the app, which DISPATCHES the action's ``command`` back into the
    extension as a ``register_command`` call — the panel→extension loop.

    Live-updatable: :meth:`update_spec` rebuilds the panel's children in place (the
    panel widget keeps its DOM position, so a re-call for the same key updates content
    without reordering sibling panels — the fleet table ticking each turn).
    """

    class Action(Message):
        """A panel action was pressed → dispatch ``command`` with ``args`` (S68)."""

        def __init__(self, command: str, args: str) -> None:
            self.command = command
            self.args = args
            super().__init__()

    def __init__(self, key: str, spec: dict[str, Any]) -> None:
        super().__init__(classes="ext-panel")
        self._key = key
        self._spec = spec

    def compose(self) -> ComposeResult:
        yield from self._build_widgets()

    def _build_widgets(self) -> ComposeResult:
        yield Static(self._spec["title"], classes="ext-panel-title")
        yield Static(render_panel_body(self._spec["body"]), classes="ext-panel-body")
        actions = self._spec["actions"]
        if actions:
            yield Horizontal(
                *(_PanelActionButton(a["label"], a["command"], a["args"]) for a in actions),
                classes="ext-panel-actions",
            )

    async def update_spec(self, spec: dict[str, Any]) -> None:
        """Re-render this panel in place from a new normalized spec (live update)."""
        self._spec = spec
        await self.remove_children()
        await self.mount(*self._build_widgets())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if isinstance(button, _PanelActionButton):
            # Consume the press here and re-emit it as a semantic Action, so the app
            # sees "run command X" rather than a raw button event it must decode.
            event.stop()
            self.post_message(self.Action(button.command, button.args))


class ExtensionPanelHost(VerticalScroll):
    """The container of live keyed :class:`ExtensionPanel` widgets (E10 §6 / S68).

    The app-side landing for ``ctx.ui.panel(key, spec)``. :meth:`set_panel` MOUNTS a
    new panel for an unseen key, UPDATES an existing key's panel in place (identity
    preserved, so sibling order is stable across a live re-call), and REMOVES a panel
    when ``spec is None`` (pi's "pass undefined to clear"). When no panels remain the
    host hides itself (``display = False``) so it costs zero space — it only occupies
    the side column while at least one extension has a panel live. Docked to the right
    of the main area, so the chat flow is untouched when empty.
    """

    # There was a ``VisibilityChanged`` message here. It existed for exactly one
    # listener — ``Parley._apply_side_columns``, which used to re-decide whether
    # the SIDEBAR still fit beside a newly-opened panel. §8 mounts the sidebar
    # closed and honors ctrl+b at any width, so that decision no longer exists and
    # neither does the message: an announcement nobody listens to is a promise
    # this class would have to keep for no one.

    def __init__(self) -> None:
        super().__init__(id="ext-panel-host")
        # Insertion-ordered {key: panel}; a dict preserves first-seen order so an
        # in-place update keeps a panel's column position.
        self._panels: dict[str, ExtensionPanel] = {}
        self.display = False

    def _set_visible(self, visible: bool) -> None:
        """Show/hide the host. Idempotent — an unchanged state is left alone."""
        if self.display == visible:
            return
        self.display = visible

    def set_panel(self, key: str, spec: dict[str, Any] | None) -> None:
        """Mount, update in place, or remove one keyed panel (E10 §6 / S68)."""
        if spec is None:
            panel = self._panels.pop(key, None)
            if panel is not None:
                panel.remove()
            if not self._panels:
                self._set_visible(False)
            return
        self._set_visible(True)
        existing = self._panels.get(key)
        if existing is not None:
            # Update the SAME widget (order-stable). update_spec is async (mount /
            # remove children); schedule it on the loop — the delegate call is sync.
            self.call_later(existing.update_spec, spec)
        else:
            panel = ExtensionPanel(key, spec)
            self._panels[key] = panel
            self.mount(panel)


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


def _elide(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, ending in ``…`` when anything was cut.

    A width too small to hold even one character plus the marker returns the text
    unchanged: a label elided to nothing tells the reader less than one that
    overflows, and the caller can see the overflow.
    """
    if width < 2 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


class SessionTreeModal(ModalScreen[Optional[str]]):
    """Browse the conversation tree and pick a node to branch from (§3.2).

    Port of pi's ``showTreeSelector`` (interactive-mode.ts:4446): a
    ``textual.widgets.Tree`` populated from ``ConversationTree.tree()``, the current
    leaf highlighted. ``Enter`` dismisses with the chosen entry id; ``Esc`` cancels
    (``None``). Copies the ``SystemPromptEditor`` modal template.

    ``title``/``help_text`` exist for the SECOND pick of the elide flow (W3), which
    asks a different question of the same browser — "where does the fold resume?"
    rather than "where do we branch from?". One reused browser with a different
    caption, not a second widget: the tree, the leaf highlight and the key handling
    are identical, and only the sentence above them is not.

    Rows sit beside a :class:`TreeDetailPane` showing the highlighted node in full
    — the rows say *which* node, the pane says *what it is*. ``resolve_entry`` is
    what makes that possible and is required: a browser that cannot show a body is
    the elided-preview draft this replaced, not a degraded mode worth keeping.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    #: Terminal HEIGHT at or above which the detail pane is drawn. The pane is
    #: stacked under the tree and takes half the body, so rows are what it can run
    #: out of — width it always has all of. Its floor is the arrangement it exists
    #: to produce: :attr:`TreeDetailPane.LEAD_ROWS` of the previous message, then
    #: the selected box's top border and a line of its text. Below that the pane
    #: can no longer say what it was added to say, and the rows are better spent
    #: on tree nodes. Measured, not derived — 20 is where the pane first holds the
    #: lead plus the selected box's top border and two lines of its text; at 18 it
    #: is down to one line and at 16 to the border alone, which identifies a node
    #: without showing it. ``test_detail_pane_min_height_is_where_the_floor_is``
    #: re-measures this, so a later change to the split, the chrome or the title
    #: block fails rather than silently drifting.
    DETAIL_MIN_HEIGHT = 20

    def __init__(
        self,
        roots: list[TreeNode],
        *,
        resolve_entry: Callable[[str], dict[str, Any]],
        title: str = "Browse Conversation Tree",
        help_text: str = "Enter: branch    Tab: detail pane    Esc: cancel",
    ) -> None:
        super().__init__()
        self._roots = roots
        self._resolve_entry = resolve_entry
        self._title = title
        self._help_text = help_text
        self._rows: list[tuple[Any, str, int]] = []
        # id → node, and id → parent id, for the detail pane's neighbours. Built
        # from the graph handed in, so the pane never re-walks the session log.
        self._by_id: dict[str, TreeNode] = {}
        self._parent_of: dict[str, str] = {}
        self._depth_of: dict[str, int] = {}
        for root in roots:
            self._index(root, 0)

    def _index(self, node: TreeNode, depth: int) -> None:
        self._by_id[node.id] = node
        self._depth_of[node.id] = depth
        for child in node.children:
            self._parent_of[child.id] = node.id
            self._index(child, depth + 1)

    def compose(self) -> ComposeResult:
        with Container(id="tree-browser-dialog"):
            yield Static(self._title, id="tree-browser-title")
            # Stacked, not side by side: both halves are wrapped text, and a
            # column split starves both of the width they need (see the
            # `#tree-browser-body` rule in parley.tcss).
            with Vertical(id="tree-browser-body"):
                tree: Tree[str] = Tree("session", id="tree-browser-tree")
                tree.show_root = False
                yield tree
                yield TreeDetailPane(self._resolve_entry)
            yield Static(self._help_text, id="tree-browser-help")

    # -- the detail pane's window on the tree --------------------------------

    # ``DetailView``/``TreeDetailPane`` are defined further down the module (they
    # build on ``MessageBox``, which builds on nothing here); quoted because this
    # file does not use postponed annotation evaluation.
    def _view_of(self, node_id: str) -> "DetailView | None":
        """The three-node window around ``node_id``, or ``None`` if unknown.

        ``None`` rather than a raise: ``Tree.NodeHighlighted`` also fires for the
        widget's own hidden root, whose ``data`` is ``None`` and which names no
        conversation node at all.
        """
        selected = self._by_id.get(node_id)
        if selected is None:
            return None
        parent_id = self._parent_of.get(node_id)
        previous = self._by_id.get(parent_id) if parent_id is not None else None
        # The oldest child (``ConversationTree.tree`` sorts children by
        # timestamp), which is the message that actually followed this one in
        # time. A later sibling is a *branch*, counted separately rather than
        # silently chosen as "the" next message.
        following = selected.children[0] if selected.children else None
        return DetailView(
            selected=selected,
            previous=previous,
            following=following,
            earlier=self._depth_of[previous.id] if previous is not None else 0,
            later=self._subtree_size(following) - 1 if following is not None else 0,
            branches=len(selected.children),
        )

    @staticmethod
    def _subtree_size(node: TreeNode) -> int:
        total = 0
        stack = [node]
        while stack:
            current = stack.pop()
            total += 1
            stack.extend(current.children)
        return total

    async def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Move the detail pane to whatever the cursor now sits on."""
        await self._show_node(event.node.data)

    async def _show_cursor_node(self) -> None:
        """Draw the pane for wherever the cursor already is.

        ``Tree`` posts ``NodeHighlighted`` only when ``cursor_line`` *changes*, so
        a session whose current leaf is also the first row — a conversation with
        no branches yet, which is most of them — never emits one, and the pane
        would open blank next to a highlighted row. Called once after the initial
        layout; :meth:`TreeDetailPane.show` dedupes it against the event that a
        session with a deeper leaf does emit.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        await self._show_node(None if node is None else node.data)

    async def _show_node(self, node_id: object) -> None:
        pane = self.query_one(TreeDetailPane)
        if not pane.display:
            return
        view = None if node_id is None else self._view_of(str(node_id))
        if view is not None:
            await pane.show(view)

    def _apply_detail_pane(self) -> None:
        """Show or hide the pane for the current height (see :attr:`DETAIL_MIN_HEIGHT`).

        The one place the pane's ``display`` is written, mirroring
        ``Parley._apply_side_columns``: an inline style set from two places is
        permanent and invisible to the other.
        """
        pane = self.query_one(TreeDetailPane)
        pane.display = self.app.size.height >= self.DETAIL_MIN_HEIGHT

    def on_mount(self) -> None:
        self._apply_detail_pane()
        tree = self.query_one("#tree-browser-tree", Tree)
        leaf_widget: list[Any] = []
        # (widget node, full label, depth) for every row, kept so _relabel can
        # re-elide from the untruncated text on every resize. Eliding an already
        # elided label would eat a character per resize.
        self._rows = []

        def _add(parent, node: TreeNode, depth: int) -> None:
            label = self._label(node)
            widget_node = parent.add(label, data=node.id)
            widget_node.expand()
            self._rows.append((widget_node, label, depth))
            if node.is_leaf:
                leaf_widget.append(widget_node)
            for child in node.children:
                _add(widget_node, child, depth + 1)

        for root in self._roots:
            _add(tree.root, root, 0)

        # Highlight the current leaf (pi passes realLeafId to the selector). Defer
        # until after the first refresh — a node's ``line`` (which ``move_cursor``
        # reads) is only assigned once the tree has laid out.
        if leaf_widget:
            leaf_node = leaf_widget[0]
            tree.call_after_refresh(tree.move_cursor, leaf_node)
        # Same reason: the tree has no width yet, so the labels cannot be sized
        # to it until it has laid out at least once.
        tree.call_after_refresh(self._relabel)
        tree.call_after_refresh(self._show_cursor_node)
        tree.focus()

    def on_resize(self, event: object) -> None:
        self._apply_detail_pane()
        self._relabel()

    def _relabel(self) -> None:
        """Fit every row's label to the tree's current width.

        ``textual.widgets.Tree`` renders one physical line per node and does not
        wrap, so a preview longer than the row is not shortened — it runs off the
        edge and the tree grows a horizontal scrollbar, which is a poor way to
        read a sentence. Eliding puts the truncation where the reader can see it.

        An elided preview is a preview the reader cannot finish, which is what
        :class:`TreeDetailPane` beside these rows is for: the row identifies the
        node, the pane shows it whole and wrapped.
        """
        if not self._rows:
            return
        tree = self.query_one("#tree-browser-tree", Tree)
        width = tree.content_size.width
        if width <= 0:
            return
        for widget_node, label, depth in self._rows:
            # Textual indents each level by ``guide_depth`` cells and prefixes the
            # row with a toggle; both eat into the label's share of the line.
            available = width - (depth + 1) * tree.guide_depth
            widget_node.set_label(_elide(label, available))

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
    """The mode chooser after a node is picked (§3.1).

    pi's ``showExtensionSelector`` (interactive-mode.ts:4479-4483): "No summary" /
    "Summarize" / "Summarize with custom instructions". Dismisses with
    ``"navigate"`` / ``"summarize"`` / ``"custom"`` (or ``None`` on cancel).

    Plus a fourth, τ-only mode: ``"elide"`` (W3, NODE-ADDRESSABLE-AGENTS.md). It is
    the odd one out and the title says so — the other three treat the picked node as
    a BRANCH POINT and move the cursor back to it, while ``elide`` treats it as the
    fold's ANCHOR and needs a second node (the resume point) before it can do
    anything. :meth:`Parley.action_browse_tree` collects that second pick; this
    modal only names the mode.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Container(id="tree-mode-dialog"):
            yield Static("Act on selected node", id="tree-mode-title")
            with Vertical(id="tree-mode-buttons"):
                yield Button("Branch: no summary", variant="primary", id="mode-navigate")
                yield Button("Branch: summarize abandoned branch", id="mode-summarize")
                yield Button("Branch: summarize with custom instructions…", id="mode-custom")
                yield Button("Elide a span ending here…", id="mode-elide")
                yield Button("Cancel", id="mode-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "mode-navigate": "navigate",
            "mode-summarize": "summarize",
            "mode-custom": "custom",
            "mode-elide": "elide",
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


class RollbackPromptModal(ModalScreen[Optional[str]]):
    """Collect the prompt that replaces the turn being rolled back.

    The affordance for ``multitask_strategy="rollback"``
    (docs/SUBMISSION-LIFECYCLE.md decision 2). A rollback is not "stop" — it is
    "stop, un-path what that turn did, and run THIS instead", and the core has no
    way to express the first two halves without the third: ``submit()`` needs the
    replacement text, and it must be submitted while the doomed turn still holds
    the turn slot. So the affordance has to ask for text, and the input widget is
    disabled for the duration of a turn, which leaves a modal.

    Prefilled with the aborted turn's own prompt, because the two things a person
    wants here are "run that again from before it went wrong" (accept the prefill)
    and "run this corrected version instead" (edit it), and prefilling makes the
    first one a single keypress. Reuses the ``SystemPromptEditor`` shell like
    :class:`TreeCustomInstructionsModal` does, plus one line saying what is about
    to happen to the running turn — the operation discards visible work, and the
    other destructive tree operations (branch, summarize, elide) all name their
    consequence before they run.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, prefill: str = "") -> None:
        super().__init__()
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        with Container(id="prompt-editor-dialog"):
            yield Static("Roll back the in-flight turn", id="prompt-editor-title")
            yield Static(
                "The running turn is aborted and its messages fall off the active "
                "path — nothing is deleted, the tree browser still shows them. This "
                "prompt runs in their place, from the context as it stood before "
                "that turn started.",
                id="rollback-help",
            )
            yield TextArea(self._prefill, id="prompt-editor-textarea")
            with Horizontal(id="prompt-editor-buttons"):
                yield Button("Roll back & run", variant="primary", id="rollback-run")
                yield Button("Cancel", variant="default", id="rollback-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rollback-run":
            self.dismiss(self.query_one("#prompt-editor-textarea", TextArea).text)
        elif event.button.id == "rollback-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExtensionConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation for an extension's ``api.ui.confirm`` (E7 §3 / S47).

    Ports pi's ``ctx.ui.confirm(title, message)`` (types.ts:129). Copies the
    ``TreeModeModal`` button template. ``Yes`` → ``True``; ``No`` or ``Esc`` →
    ``False`` (a cancelled confirmation is a "no", never a hidden yes — Fail-Early).
    pi's ``timed-confirm`` timeout option is deferred.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="ext-confirm-dialog"):
            yield Static(self._title, id="ext-confirm-title")
            yield Static(self._message, id="ext-confirm-message")
            with Horizontal(id="ext-confirm-buttons"):
                yield Button("Yes", variant="primary", id="ext-confirm-yes")
                yield Button("No", variant="default", id="ext-confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ext-confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ExtensionSelectModal(ModalScreen[Optional[str]]):
    """Single-choice selector for an extension's ``api.ui.select`` (E7 §3 / S47).

    Ports pi's ``ctx.ui.select(title, options)`` (types.ts:126). An ``OptionList``
    of the items; ``Enter``/click dismisses with the chosen string, ``Esc`` with
    ``None`` (no selection). The index into the original ``items`` list is the
    source of truth, so the returned value is exactly the caller's string.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, items: list[str]) -> None:
        super().__init__()
        self._title = title
        self._items = items

    def compose(self) -> ComposeResult:
        with Container(id="ext-select-dialog"):
            yield Static(self._title, id="ext-select-title")
            yield OptionList(*self._items, id="ext-select-list")
            yield Static("Enter: choose    Esc: cancel", id="ext-select-help")

    def on_mount(self) -> None:
        self.query_one("#ext-select-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self._items[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExtensionInputModal(ModalScreen[Optional[str]]):
    """Text prompt for an extension's ``api.ui.input`` (E7 §3 / S47).

    Ports pi's ``ctx.ui.input(title, placeholder?)`` (types.ts:132). An ``Input``
    pre-filled with the extension-supplied default; ``Enter`` or ``OK`` dismisses
    with the (possibly edited) text, ``Esc`` or ``Cancel`` with ``None``. The
    delegate maps a ``None`` cancel back to the default (see
    ``_ExtensionUIDelegate.input``).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, default: str = "") -> None:
        super().__init__()
        self._title = title
        self._default = default

    def compose(self) -> ComposeResult:
        with Container(id="ext-input-dialog"):
            yield Static(self._title, id="ext-input-title")
            yield Input(value=self._default, id="ext-input-field")
            with Horizontal(id="ext-input-buttons"):
                yield Button("OK", variant="primary", id="ext-input-ok")
                yield Button("Cancel", variant="default", id="ext-input-cancel")

    def on_mount(self) -> None:
        self.query_one("#ext-input-field", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ext-input-ok":
            self.dismiss(self.query_one("#ext-input-field", Input).value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExtensionChordScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Which-key popup for the ``ctrl+e`` extension shortcut chord (E10 §6 / S69).

    The second half of an extension key binding: after the ``ctrl+e`` leader (the
    guarded namespace), this modal lists every registered shortcut as
    ``ctrl+e <key> → /command`` and captures the NEXT key. A matching key dismisses
    with ``(command, args)`` — dispatched by :meth:`Parley.action_extension_chord`
    through the SAME ``run_extension_command`` path a typed ``/name args`` uses;
    ``escape`` (or any unbound key) dismisses ``None``.

    Rendered as a menu (not a silent capture) so the guarded namespace is
    DISCOVERABLE — the user sees what ``ctrl+e`` offers, the same shortcuts the
    command palette also lists. Only ``Static`` children (none focusable), so the
    screen itself receives the key event — no inner widget swallows the tail key.
    """

    def __init__(self, shortcuts: list[tuple[str, str, str, str]]) -> None:
        super().__init__()
        self._shortcuts = shortcuts
        # tail key -> (command, args) for O(1) capture; the list drives display order.
        self._by_key: dict[str, tuple[str, str]] = {
            key: (command, args) for key, command, args, _desc in shortcuts
        }

    def compose(self) -> ComposeResult:
        with Container(id="ext-chord-dialog"):
            yield Static("Extension shortcuts — ctrl+e then…", id="ext-chord-title")
            for key, command, args, desc in self._shortcuts:
                label = f"  [b]{key}[/b]  →  /{command}"
                if args:
                    label += f" {args}"
                if desc:
                    label += f"   — {desc}"
                yield Static(label, classes="ext-chord-entry")

    def on_key(self, event: events.Key) -> None:
        # The chord tail: a registered key dispatches its command, anything else
        # (including escape) cancels. Stop + prevent-default either way so the
        # captured key never leaks to the app underneath.
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
            return
        self.dismiss(self._by_key.get(event.key))


class ExtensionFormScreen(ModalScreen[Optional[dict]]):
    """One generic declarative form for an extension's ``api.ui.form`` (E10 §6 / S66).

    The τ answer to pi's ``question``/``questionnaire`` widget factory: instead of an
    extension shipping bespoke TUI code, it hands ``ui.form`` a plain-data SPEC
    (D-E6-4) and THIS single screen renders every field. One widget maps to each
    :data:`~tau_agent_core.extension_types.FORM_FIELD_KINDS` kind:

    - ``text`` / ``number`` → :class:`Input` (``number`` restricts to numerics);
    - ``confirm`` → :class:`Checkbox` (its own label carries the field label);
    - ``select`` → :class:`RadioSet` of :class:`RadioButton` (single choice);
    - ``multiselect`` → :class:`SelectionList` (N-of-M).

    ``Submit`` dismisses with the ``{name: value}`` answers dict; ``Cancel``/``Esc``
    dismisses with ``None`` (a cancelled form is not a fabricated answer set —
    Fail-Early). The spec is validated by the SAME
    :func:`~tau_agent_core.extension_types.validate_form_spec` the headless path
    uses, so the two frontends can never disagree about a field's meaning.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, spec: dict[str, Any]) -> None:
        super().__init__()
        # Re-validate the raw spec here (idempotent with ExtensionUI.form's own
        # check) so the screen is self-contained and never renders a malformed field.
        self._form_title, self._fields = validate_form_spec(spec)

    @staticmethod
    def _field_widget_id(index: int) -> str:
        return f"ext-form-field-{index}"

    def compose(self) -> ComposeResult:
        with Container(id="ext-form-dialog"):
            yield Static(self._form_title, id="ext-form-title")
            with VerticalScroll(id="ext-form-fields"):
                for index, field in enumerate(self._fields):
                    yield from self._compose_field(index, field)
            yield Static("Submit: confirm    Esc: cancel", id="ext-form-help")
            with Horizontal(id="ext-form-buttons"):
                yield Button("Submit", variant="primary", id="ext-form-submit")
                yield Button("Cancel", variant="default", id="ext-form-cancel")

    def _compose_field(self, index: int, field: dict[str, Any]) -> ComposeResult:
        wid = self._field_widget_id(index)
        kind = field["kind"]
        label = field["label"]
        if kind == "confirm":
            # The Checkbox carries its own label; no separate Static row.
            yield Checkbox(label, value=bool(field.get("default", False)), id=wid)
            return
        yield Static(label, classes="ext-form-label")
        if kind in ("text", "number"):
            default = field.get("default", "")
            yield Input(
                value="" if default == "" else str(default),
                id=wid,
                type="number" if kind == "number" else "text",
                classes="ext-form-input",
            )
        elif kind == "select":
            options = field["options"]
            chosen = field.get("default", options[0])
            yield RadioSet(
                *(RadioButton(opt, value=(opt == chosen)) for opt in options),
                id=wid,
            )
        elif kind == "multiselect":
            options = field["options"]
            chosen_set = set(field.get("default", []) or [])
            yield SelectionList[str](
                *((opt, opt, opt in chosen_set) for opt in options),
                id=wid,
            )

    def _collect(self) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for index, field in enumerate(self._fields):
            wid = f"#{self._field_widget_id(index)}"
            kind = field["kind"]
            name = field["name"]
            if kind == "text":
                answers[name] = self.query_one(wid, Input).value
            elif kind == "number":
                answers[name] = self._parse_number(self.query_one(wid, Input).value, field)
            elif kind == "confirm":
                answers[name] = self.query_one(wid, Checkbox).value
            elif kind == "select":
                radio_set = self.query_one(wid, RadioSet)
                idx = radio_set.pressed_index
                options = field["options"]
                # A RadioSet always keeps one pressed once composed with a default;
                # -1 (nothing pressed) falls back to the declared/first option.
                answers[name] = (
                    options[idx] if 0 <= idx < len(options) else field.get("default", options[0])
                )
            elif kind == "multiselect":
                answers[name] = list(self.query_one(wid, SelectionList).selected)
        return answers

    @staticmethod
    def _parse_number(text: str, field: dict[str, Any]) -> Any:
        # The Input is numeric-restricted, so ``text`` is a number or empty. An empty
        # field resolves to the declared default (or 0) — a UI default the user left
        # untouched, not a fabricated headless answer.
        stripped = text.strip()
        if not stripped:
            return field.get("default", 0)
        try:
            return int(stripped)
        except ValueError:
            return float(stripped)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ext-form-submit":
            self.dismiss(self._collect())
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
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
    # Extension-injected durable node (before_agent_start, E5 §3.1): rendered
    # distinctly so it never reads as a literal user turn (role "custom" on the
    # node, serialized custom→user only on the wire).
    "custom": "Extension",
    # Submission sources (B3-b). A turn nobody typed HERE still opens with a
    # bubble carrying its own text — Jupyter's ``execute_input`` re-broadcast —
    # and that bubble must not claim a human wrote it. So the SOURCE is the role
    # (docs/SUBMISSION-LIFECYCLE.md ``SubmissionSource``) and it selects the label
    # + colour exactly the way every other kind here does.
    #
    # These are entries, not an allow-list: an unlisted source falls through to
    # ``role.capitalize()`` in :meth:`MessageBox.on_mount` and to the shared
    # ``lane-foreign`` CSS rule, so a novel source renders with a generic
    # attribution instead of being dropped. Filtering is the failure mode this
    # whole design exists to prevent.
    "interactive": "User",  # a human, but not the one at THIS frontend
    "rpc": "RPC",
    "extension": "Extension",
    "bus": "Bus",
    "timer": "Timer",
    "webhook": "Webhook",
    "voice": "Voice",
    "agent": "Sub-agent",
    # Session-log entry kinds that are NOT messages. They never appear in the
    # transcript — nothing produces one as a turn — but the tree browser's detail
    # pane (:class:`TreeDetailPane`) draws them in the same boxes, and the same
    # ``role.capitalize()`` fallthrough would title them "Branch_summary".
    "compaction": "Compaction",
    "branch_summary": "Branch summary",
    "navigate": "Navigate",
    "elide": "Elide",
    "customEntry": "Entry",
}

#: CSS class worn by every widget belonging to a lane this frontend did not
#: originate — a bus/timer submission's bubble, a forked sub-agent's steps, the
#: answer promoted out of either (B3-b). ONE class rather than one per source, so
#: a source nobody has heard of is still visually foreign; the box's border TITLE
#: names which source it actually was.
LANE_FOREIGN_CLASS = "lane-foreign"


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
        # Lazily created by append_content_delta on the first streamed delta
        # once the inner Markdown has composed; see append_content_delta/finish_stream.
        self._stream: MarkdownStream | None = None
        # Carries fenced-code-block state across streamed deltas. Reset by
        # _format, which re-seats it from a whole document.
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
        # Three stacked slots: reasoning (lazy), the text body, tool boxes (lazy).
        # Empty slots collapse to zero height, so a plain user message looks
        # exactly like a single text box.
        #
        # The inner Markdown is always constructed EMPTY, even when
        # self._content is already non-empty here (a streaming step whose
        # first delta(s) landed before compose() ran, buffered by
        # append_content_delta's hasattr gate) -- on_mount catches it up via
        # append(), deliberately not by seeding the constructor. See
        # on_mount's comment for why: Markdown(text)'s implicit update(text)
        # call computes its _last_parsed_line bookkeeping differently than
        # append() does, and the two can disagree.
        self._reasoning_slot = Vertical(classes="message-reasoning")
        yield self._reasoning_slot
        md = Markdown("", classes="message-content")
        self._md_widget = md
        yield md
        self._tools_slot = Vertical(classes="message-tools")
        yield self._tools_slot

    def on_mount(self) -> None:
        # The role label + color live on the box border (not the inner Markdown),
        # so reasoning + text + tools sit inside one titled border.
        self.border_title = ROLE_LABELS.get(self.role, self.role.capitalize())
        # Catch up whatever content was already set (a normal fully-formed
        # message, or a streaming step whose deltas outran compose()) onto the
        # now-mounted, still-empty Markdown widget.
        #
        # Routed through append(), deliberately NOT update(): Markdown.update()
        # computes its internal _last_parsed_line bookkeeping as a naive
        # "physical line count", whereas Markdown.append() derives it from the
        # actual parse tree (the start line of the last still-open top-level
        # block). These disagree whenever the seeded text ends with a
        # construct spanning more than one physical line as ONE block -- a
        # still-open fenced code block is the everyday case, and _format now
        # leaves its interior newlines alone, so it stays one multi-line block --
        # a
        # later append_content_delta would then reparse from update()'s wrong
        # line offset and *replace* the block already rendered, silently
        # dropping the earlier text (same failure mode verified directly on
        # ReasoningRegion's identical fix). Since existing_blocks is empty on
        # a freshly-mounted widget either way, append() here mounts the exact
        # same block tree update() would have for this first-ever content —
        # the only difference is _last_parsed_line staying self-consistent
        # with every append_content_delta call that follows.
        if self._content:
            self._md_widget.append(self._format(self._content))
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
        # Same string in twice in a row -- a _flush/finalize call after
        # append_content_delta already streamed every delta in -- is the
        # common case now; Markdown.update() unconditionally re-parses and
        # remounts every block, so skip it when nothing actually changed.
        # content_text/_promote_answer read self._content, not the widget, so
        # it's fine to bail before touching the Markdown -- the value they'd
        # see is unchanged either way.
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
        # A storage-agnostic handle (SessionCatalog.load(ref)), not a filesystem
        # path — a path for the file store, a doc id for a future JMFTS-backed one
        # (W10). Named for what it IS, not for the one store that happens to back
        # it today.
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


class ChatSidebar(Container):
    """Sidebar showing this directory's recent sessions, grouped by date."""

    # Applies to today/yesterday/older alike (older was the only one capped
    # before this fix) — an unbounded group is how a single mount storm grows
    # without limit as the catalog does.
    _GROUP_LIMIT = 10

    def __init__(self, catalog: SessionCatalog):
        super().__init__(id="sidebar")
        self.catalog = catalog
        self.sessions: list[SessionInfo] = []
        # Set by _apply_sessions when a refresh lands while collapsed — the
        # mount/compositor cost of _render_chat_list is real (seconds, for a
        # large catalog) and paid for zero visible effect while nothing can
        # see #chat-list. ensure_rendered() catches it up on expand.
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

        # One mount() call for every widget in the group, not one call per
        # item: Textual's mount() does real synchronous attach/CSS/layout
        # work per call, and looping it item-by-item over a few hundred
        # sessions is exactly what turned into the multi-second freeze this
        # fixes. Every group is capped at _GROUP_LIMIT for the same reason
        # "older" already was — today/yesterday were the unbounded ones.
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
            # action_new_chat is async; dispatch through run_action so it is
            # actually awaited. A bare self.app.action_new_chat() just builds an
            # un-awaited coroutine and silently does nothing — the "+ New Chat"
            # button bug.
            await self.app.run_action("new_chat")


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
    )

    def __init__(self, exchange: Optional[ExchangeBox] = None, label: str | None = None) -> None:
        self.exchange = exchange
        #: This lane's origin badge (``"bus · nats_bus"``, ``"agent · fork:explore"``),
        #: or ``None`` for the ordinary "a human typed it here" lane. Held on the
        #: LANE rather than only on the exchange because the exchange does not
        #: survive the span: a no-tool exchange is unwrapped at finalize and its
        #: answer promoted to top level, so a fork's answer would otherwise end up
        #: an unlabelled ``Assistant`` box in the middle of the primary transcript
        #: — exactly the mistake a reader must never be able to make (B3-b).
        self.label = label
        #: The current turn's assistant step box (reasoning + text + tools).
        self.active_box: Optional[MessageBox] = None
        # Accumulators for the active step (the 30 Hz throttle can skip the final
        # delta, so the tails are flushed when the target changes).
        self.active_text: str = ""
        self.active_reasoning: str = ""
        #: Route each tool result to the step that issued the call, by id.
        self.tool_routes: dict[str, MessageBox] = {}


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


#: The parenthetical for an empty tool row, keyed by the resolved ``no_tools``
#: policy. The two flags are NOT interchangeable to a reader of this row: under
#: ``--no-tools`` the next turn has nothing at all, while under
#: ``--no-builtin-tools`` an extension may still be offering tools this row does
#: not list (it states the built-ins, which is all ``resolve_tool_names`` knows).
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

    #: One line under the τ. Constant unless ``--fun`` is on (see
    #: :mod:`tau_coding_agent.tagline`).
    tagline: str
    #: The model id that goes on the wire, e.g. ``qwen36-35B-IQ4_XS``.
    model: str
    #: Where it goes: a ``base_url`` when the entry has one, else the backend name
    #: (``anthropic``/``gemini`` entries address their own endpoints).
    endpoint: str
    #: The working directory every ``read``/``write``/``bash`` call is relative
    #: to, with ``$HOME`` collapsed to ``~``.
    cwd: str
    #: What the next turn's tool set is, already rendered: the resolved built-in
    #: names space-separated, or — when there are none — ``none`` naming the flag
    #: that emptied it (``--no-tools`` withholds extension tools too;
    #: ``--no-builtin-tools`` does not). An empty tool set is a fact worth
    #: reading, not a blank row, and WHICH flag produced it is the fact.
    #: Empty string only when there is no resolvable model entry to ask.
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

    #: Width of the label gutter. ``store`` is the longest label at 5, so 9 leaves
    #: four spaces before the value column.
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
            # ``_session_facts`` has already named the flag; the bare "none" is
            # only reachable from the unresolvable-model branch, which has no
            # config to attribute it to.
            ("tools", f.tools or "none"),
            ("store", f.store),
        ]
        body = Text(justify="left")
        body.append("τ\n", style="bold")
        body.append(f.tagline + "\n\n", style="dim")
        for label, value in rows:
            # `dim` rather than a hex literal: it composes with whatever color the
            # stylesheet gives this widget, so the label/value hierarchy survives a
            # future theme swap without a second palette living in Python.
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

    def add_message(self, role: str, content: str, subtitle: str = "", *, source: ContentSource):
        """Add a finished (non-streaming) message box to the display.

        ``source`` is passed straight through to :class:`MessageBox` and is
        required for the same reason it is required there: only this caller knows
        whether it is handing over an assistant's markdown or verbatim output.
        """
        box = MessageBox(role, content, subtitle, source=source)
        self.mount(box)
        self.scroll_end(animate=False)
        return box

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
                source="verbatim",
            )
            if msg.get("is_error"):
                box.add_class("box-error")
            return [box]

        # A bare-string body is the on-disk shape for a user or system turn —
        # literal text nobody wrote as markdown — and, for an assistant turn, the
        # answer markdown it authored.
        text_source: ContentSource = "markdown" if role == "assistant" else "verbatim"

        content = msg.get("content", "")
        if isinstance(content, str):
            return [self.add_message(role, content, source=text_source)]
        if isinstance(content, list):
            # Assistant turns interleave text and tool calls. Accumulate text
            # into one box, flushing it before each tool call so order is kept
            # (text-then-call renders as two boxes, the call after the text).
            boxes: list[MessageBox] = []
            text_buf: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_buf.append(block.get("text", ""))
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

    #: Rows of the previous message kept on screen above the selection. Enough to
    #: show its closing border and a line of its tail, so the selected box reads
    #: as following something rather than starting the pane.
    LEAD_ROWS = 3

    def __init__(self, resolve_entry: Callable[[str], dict[str, Any]]) -> None:
        super().__init__(id="tree-detail")
        #: id → raw session entry. The pane needs bodies, which ``TreeNode`` does
        #: not carry (see ``ConversationTree.entry``); injected rather than
        #: imported so the modal's caller decides what log is being browsed.
        self._resolve_entry = resolve_entry
        self._shown_id: str | None = None
        self._selected_boxes: list[MessageBox] = []
        #: A rebuild is waiting to be scrolled into place. Cleared by
        #: :meth:`_scroll_to_selection` once the layout it needs actually exists.
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

    Reloaded (persisted) chats still render as flat boxes via
    :meth:`add_persisted_message` — rebuilding exchanges from the saved message
    list is a separate concern.
    """

    def __init__(self, facts: Callable[[], SessionFacts] | None = None):
        super().__init__(id="chat-display")
        # Per-LANE streaming state (B3-a). One entry per concurrently-streaming
        # turn; created on demand so an event for a lane nobody opened still
        # renders (the pre-B3-a defensive path, where a step with no exchange
        # mounts at top level) instead of vanishing.
        self._lanes: dict[str, _LaneRender] = {}
        # A CALLABLE, not a snapshot: the model can change (/model, a resumed
        # session) while the chat is empty, and a pane stating last hour's model
        # is worse than no pane. Called on each show, which is rare.
        #
        # Optional because a bare renderer harness legitimately has no empty
        # state — two test apps mount a ChatDisplay purely to assert what
        # add_message produced. `None` composes no placeholder at all rather than
        # inventing facts nobody supplied.
        self._facts_source = facts
        self._placeholder: ChatPlaceholder | None = None

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
        """
        placeholder = self._placeholder
        if placeholder is None:
            return
        has_content = bool(self.query(MessageBox)) or bool(self.query(ExchangeBox))
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
        self._lanes = {}
        # The "content left" half of the pair with :meth:`mount`. Awaiting the two
        # removals above is what makes this correct rather than racy: _sync reads
        # the DOM, so it has to run after the nodes are actually gone.
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

    # ------------------------------------------------------------------
    # Streaming state machine (driven by backends.RenderRouter's lane events)
    # ------------------------------------------------------------------

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
        """
        exchange = ExchangeBox(label=label)
        self._lanes[lane] = _LaneRender(exchange, label)
        await self.mount(exchange)
        self.scroll_end(animate=False)

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
        state = self._lane(event.get("lane") or DEFAULT_LANE)
        kind = event.get("kind")
        if kind == "turn_start":
            await self._on_turn_start(state)
        elif kind == "reasoning_delta":
            await self._on_reasoning_delta(state, event.get("delta", ""))
        elif kind == "text_delta":
            await self._on_text_delta(state, event.get("delta", ""))
        elif kind == "tool_call":
            await self._on_tool_call(state, event)
        elif kind == "tool_result":
            self._on_tool_result(state, event)

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
        self.scroll_end(animate=False)

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
        # Flush the previous step's tail and freeze its reasoning (a new turn
        # means the previous completion is done, even if it was reasoning-only),
        # then open a fresh step and reset the accumulators.
        await self._flush(state)
        await self._collapse_active_reasoning(state)
        state.active_text = ""
        state.active_reasoning = ""
        state.active_box = self._start_step(state)
        self.scroll_end(animate=False)

    async def _on_reasoning_delta(self, state: _LaneRender, delta: str) -> None:
        if not delta or state.active_box is None:
            return
        state.active_reasoning += delta
        region = state.active_box.ensure_reasoning()
        # No hand-rolled throttle: MarkdownStream coalesces bursts on its own
        # (its background task batches whatever accumulated in its pending
        # queue while a previous append was still being applied). Measured at
        # 40 tok/s over 8s (tmp/paced.py): the old 30 Hz gate still issued 152
        # Markdown.update() full-document rebuilds (89,696 chars re-parsed,
        # 76x the document size); routing every delta here through
        # append_delta instead issues 1 update() (the framework's own
        # mount-time seed) and 299 Markdown.append() calls totalling 1,196
        # chars -- i.e. almost exactly the document size, not a multiple of it.
        await region.append_delta(delta)
        self.scroll_end(animate=False)

    async def _on_text_delta(self, state: _LaneRender, delta: str) -> None:
        if not delta or state.active_box is None:
            return
        # Answer content has begun — this step's reasoning is complete.
        await self._collapse_active_reasoning(state)
        state.active_text += delta
        await state.active_box.append_content_delta(delta)
        self.scroll_end(animate=False)

    async def _on_tool_call(self, state: _LaneRender, event: dict) -> None:
        # Preamble reasoning/text for this step is complete; show it, fold the
        # reasoning, then add the tool box below the text (reasoning→text→tools).
        if state.active_box is None:
            state.active_box = self._start_step(state)
        await self._flush(state)
        await self._collapse_active_reasoning(state)
        tc_id = event.get("id", "") or ""
        state.active_box.add_tool_call(event.get("name", ""), event.get("arguments", {}), tc_id)
        if tc_id:
            state.tool_routes[tc_id] = state.active_box
        self.scroll_end(animate=False)

    def _on_tool_result(self, state: _LaneRender, event: dict) -> None:
        tc_id = event.get("id", "") or ""
        result_text = str(event.get("result", ""))
        is_error = bool(event.get("is_error", False))
        blocked = bool(event.get("blocked", False))
        blocked_by = event.get("blocked_by")
        # Routed within the lane: two turns streaming at once can each have a live
        # tool call, and a shared route table would fold one lane's result into the
        # other lane's box.
        box = state.tool_routes.get(tc_id)
        if box is not None and box.set_tool_result(
            tc_id, result_text, is_error, blocked=blocked, blocked_by=blocked_by
        ):
            self.scroll_end(animate=False)
            return
        # No matching tool box: the call always precedes its result in the live
        # loop, so this means an id we never saw a call for. Don't fabricate a
        # standalone box — surface it loudly instead (Fail-Early).
        self.app.log(f"tool_result for unknown tool_call_id {tc_id!r}; no ToolBox to fold into")

    async def finalize_exchange(
        self,
        *,
        tokens: int,
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
        if state is None:
            # Nothing was ever opened for this lane — a lane_end whose lane_start
            # never rendered (a chat cleared mid-turn). Say so rather than
            # finalizing some other lane's exchange, which is the interleaving
            # this refactor exists to make impossible.
            self.app.log(f"finalize_exchange for lane {lane!r} with no open exchange")
            return
        await self._flush(state)
        await self._collapse_active_reasoning(state)  # freeze the last step's reasoning
        exchange = state.exchange
        if exchange is None:
            return

        await self._close_exchange(
            exchange,
            tokens=tokens,
            seconds=seconds,
            telemetry=telemetry,
            label=state.label,
        )
        self.scroll_end(animate=False)

    @staticmethod
    def _exchange_subtitle(
        tokens: int,
        seconds: float | None,
        telemetry: str | None = None,
        label: str | None = None,
    ) -> str:
        """The stats line stamped on an unwrapped (no-tool) answer. Duration is
        omitted when unknown (reload) rather than fabricated (Fail-Early).

        ``telemetry`` is the last completion's G4 readout, appended as one more
        ``·`` part when present; ``None`` appends nothing.

        ``label`` is the lane's origin badge and leads the line when present
        (B3-b), because this subtitle is the ONLY chrome an unwrapped answer has
        left: the exchange that carried the badge is removed on this path."""
        parts = [f"{format_tokens(tokens)} tok"]
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
        tokens: int,
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
            promoted = await self._promote_answer(final, after=exchange, label=label)

        if tool_count == 0:
            # Nothing worth grouping — drop the wrapper entirely (this also
            # removes the original `final` box it still contains). A trivial
            # span has no summary line, so the (real) token + duration would be
            # lost — stamp them on the answer's subtitle instead of hiding them.
            if promoted is not None:
                promoted.set_subtitle(self._exchange_subtitle(tokens, seconds, telemetry, label))
            exchange.remove()
        else:
            if final is not None:
                final.remove()
            exchange.collapsed = True
            exchange.set_summary(
                tools=tool_count, tokens=tokens, seconds=seconds, telemetry=telemetry
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
        await self.clear_messages()
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

    # Declared, not assigned in on_mount: `App.title` falls back to the CLASS
    # NAME until something overwrites it, so anything that reads it before mount
    # — the terminal window title Textual sets on startup, `take_svg_screenshot`
    # captioning a screenshot — got "Parley", the fork's name rather than this
    # program's. Same values, set early enough to be the only ones there ever
    # were. `sub_title` still changes constantly at runtime; this is its resting
    # value.
    TITLE = "Tau"
    SUB_TITLE = "Ready"

    #: Narrowest terminal on which the sidebar and an open extension panel can BOTH
    #: sit beside a readable chat column. It is no longer a breakpoint the app acts
    #: on — since the sidebar defaults to CLOSED (SESSION-UX-REDESIGN §8, decision
    #: 4) the only way it is on screen is that someone pressed ctrl+b, and an
    #: explicit request is honored at any width — but it is still the measured
    #: geometry cliff that request runs into, which is why it stays written down.
    #:
    #: It follows from the two CSS widths (``#sidebar`` 25%, ``#ext-panel-host`` 30%
    #: in parley.tcss): the chat is left 45%, less 2 columns of scrollbar gutter and
    #: 4 of ChatDisplay padding, so its content column is about ``0.45W - 6``. 101 is
    #: the measured width at which that first reaches the 40-column floor — 100 is
    #: one column short, which is why the number is not round.
    #: ``test_side_columns_min_width_is_where_the_floor_is`` re-measures it, so a
    #: later change to either percentage fails rather than silently drifts.
    SIDE_COLUMNS_MIN_WIDTH = 101

    BINDINGS = [
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+n", "new_chat", "New Chat"),
        Binding("ctrl+e", "extension_chord", "Extensions"),
        Binding("ctrl+g", "browse_tree", "Tree"),
        Binding("ctrl+r", "toggle_reasoning", "Reasoning", priority=True),
        Binding("ctrl+t", "toggle_tools", "Tools", priority=True),
        Binding("ctrl+j", "focus_and_send", "^Enter=Send", show=True),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+c", "quit", "Quit"),
        # priority=True: caught during generation regardless of which widget holds
        # focus. The action no-ops when nothing is generating.
        Binding("escape", "cancel_generation", "Cancel", show=False, priority=True),
        # Esc's "and un-path what it did" variant (docs/SUBMISSION-LIFECYCLE.md
        # decision 2). ctrl+z is TextArea's undo, and this steals it ONLY while a
        # turn is generating — which is exactly when the input is disabled and that
        # undo is unreachable anyway: ``check_action`` returns False otherwise, and a
        # priority binding whose check_action is False does not consume the key
        # (textual app.py ``_check_bindings`` → ``run_action``), so it falls through
        # to the editor untouched. The same False hides it from the Footer, so the
        # label appears exactly when pressing it would do something.
        Binding("ctrl+z", "rollback_turn", "Rollback", show=True, priority=True),
    ]

    # The active persisted session (append-only sink) and the live working
    # message list sent to the model. They are kept in step: every produced
    # message is appended to both; clear/compact mutate the working list (the
    # session file keeps the full transcript — append-only, no rewrite).
    current_session: reactive[Optional[ConversationSession]] = reactive(None)
    current_backend: Optional[Backend] = None
    config: dict = {}
    # Global show/hide state for the two collapsible content kinds. Each toggle
    # flips every reasoning region / tool box in the transcript at once; the
    # reactive records the last-applied intent (for the toggle's feedback).
    reasoning_collapsed: reactive[bool] = reactive(False)
    tools_collapsed: reactive[bool] = reactive(False)
    # True while ANY submitted turn is outstanding (streaming, or waiting behind
    # one that is). Gates Esc-to-cancel and the input-disabled state; flipped on in
    # on_input_submitted, off in the worker's finally once the LAST outstanding
    # submission has finished — see ``_submissions_in_flight``.
    is_generating: reactive[bool] = reactive(False)

    def __init__(
        self,
        cli_overrides: Optional[dict] = None,
        cli_run_config: Optional[dict] = None,
        session_catalog: Optional[SessionCatalog] = None,
        fun: bool = False,
        resume: bool = False,
    ):
        super().__init__()
        # ``tau --resume``: open the session picker over the first frame (§6/§7).
        # Its own argument rather than a ``cli_run_config`` key, for the reason
        # ``fun`` is one — run_config is threaded into every backend this app
        # builds, and a one-shot startup action has no business being visible
        # from there. It is the whole reach of the flag: `on_mount` reads it once
        # and nothing downstream can branch on "was this a --resume run".
        self._resume_on_start: bool = resume
        # --fun (see tau_coding_agent.tagline). Defaults FALSE here rather than to
        # tagline.FUN_DEFAULT, so constructing a Parley programmatically — every
        # test, every scene, devshot — is deterministic whether or not this tree
        # was packaged. Only cli.py passes the packaged default through.
        #
        # Resolved to a string ONCE, right here: this is the whole reach of the
        # flag. `self._tagline` is a str from this line onward, so nothing
        # downstream can branch on "is fun on".
        self._tagline: str = pick_tagline(fun)
        # The working directory the empty chat pane reports. An attribute rather
        # than a Path.cwd() call at the point of use, because it is part of what
        # `testing.sandbox.build_parley` has to pin: a rendered scene that printed
        # the developer's real cwd would differ on every machine, which is the
        # same class of leak as a test app reading the real ~/.tau/config.json.
        # Nothing but the pane reads it — tools resolve paths themselves.
        self._cwd: Path = Path.cwd()
        # The live conversation context (sent to the model). Mirrors the active
        # session's messages but is mutable for clear/compact.
        self.messages: list[dict] = []
        # Seam-3 → extension bus bridge (S21): the module-global session-lifecycle
        # subscription for the CURRENT backend. Rebound on every _bind_backend_session
        # (new-chat / clear / resume / model-swap) — unsub the old backend first so a
        # replaced backend's dead bus stops receiving events (no listener leak).
        self._session_event_unsub: Optional[Callable[[], None]] = None
        # How many submissions this app has handed to the backend and not yet seen
        # finish. Almost always 0 or 1; >1 only when a second prompt is submitted
        # while a turn is outstanding, which the core queues (the submissions
        # declare ``multitask_strategy="enqueue"``). ``is_generating`` and the
        # input-disabled state are functions of "is this zero", so a turn ending
        # while another is still queued must NOT re-enable the input.
        self._submissions_in_flight: int = 0
        # B3-a: the persistent render subscription. ONE attach for the life of a
        # backend (``_bind_backend_session``), not one per awaited turn — which is
        # what makes a turn this app never initiated renderable at all. Rebound
        # alongside ``_session_event_unsub`` when the backend/session changes.
        self._render_router: Optional[RenderRouter] = None
        # Whether the sidebar is open. FALSE at startup (SESSION-UX-REDESIGN §8,
        # decision 4): the picker and the command palette are the canonical session
        # surface now, so the list does not spend a quarter of the first screen
        # before anyone asks for it. ctrl+b is the only thing that writes it, and
        # what it writes STICKS — nothing in the app overrides a choice the user
        # made by hand, at any terminal width (see SIDE_COLUMNS_MIN_WIDTH).
        self._sidebar_open: bool = False
        # When each open lane started, for the exchange summary's wall clock. Keyed
        # by lane, because two lanes have two different clocks — the previous code
        # could keep one ``start`` local precisely because it could only ever be
        # rendering one turn.
        self._lane_started: dict[str, float] = {}
        # A mutex over THE WORKING MESSAGE LIST, not over the display.
        #
        # Until B3-a this was ``_display_lock`` and its job was to stop two turns
        # interleaving into one exchange; the per-lane renderer makes that
        # impossible by construction, so that reason is gone. What remains is a
        # real, narrower one: ``self.messages`` is the context handed to
        # ``submit_turn`` and is REBOUND from ``session.context`` when a turn
        # finishes, so a second submission that read it before the first turn
        # reconciled would send the model a conversation missing the answer it is
        # replying to. Held from reading the list to writing it back.
        #
        # It serializes only THIS app's own typed submissions, which declare
        # ``multitask_strategy="enqueue"`` and are serialized by the core anyway. It
        # is not on the render path at all: a forked or bus-originated lane streams
        # while this is held.
        self._working_list_lock = asyncio.Lock()
        # Run-level extension loading config (CLI ``-e`` / ``-ne``), applied to
        # EVERY backend this app creates via ``_load_backend_extensions`` so a model
        # switch doesn't drop extensions (E5 §2.2). Defaults match a bare ``tau``:
        # no explicit paths, discovery ON (scan ``~/.tau/extensions``).
        run_config = cli_run_config or {}
        self._extension_paths: list[str] = list(run_config.get("extensions", []))
        self._discover_extensions: bool = not run_config.get("no_extensions", False)
        # The most recent extension load result (E5 §5 / S34) — read by the
        # ``/extensions`` palette listing. Starts empty (nothing loaded yet); every
        # ``_load_backend_extensions`` replaces it with the live result.
        self._extension_load_result: LoadExtensionsResult = LoadExtensionsResult()
        # Run-level tool/prompt flags (S28), applied at each create_backend /
        # new-chat so a model switch keeps them. Defaults are inert (a bare tau).
        self._exclude_tools: list[str] = list(run_config.get("exclude_tools", []))
        # The resolved tool-suppression policy for this RUN: ``"all"`` (-nt),
        # ``"builtin"`` (-nbt) or ``None``. Run-level for the same reason
        # ``_exclude_tools`` is — ``/model`` builds a new backend from a different
        # model entry, and a policy that lived on the entry would be handed back
        # by the switch. ``cli._launch_tui`` collapses the two flags into this one
        # value before the app ever sees them.
        self._no_tools: str | None = run_config.get("no_tools")
        #: ``--tools``: the built-in allowlist, run-level for the same reason
        #: ``no_tools`` is (see :meth:`_apply_run_config`). ``None`` means the flag
        #: was absent and each model entry's own ``tools`` key still applies.
        self._tool_allowlist: list[str] | None = run_config.get("tools")
        self._append_system_prompt: list[str] = list(run_config.get("append_system_prompt", []))
        # ``--bus`` (H8): run-level, for the same reason the extension flags are —
        # the capability gates which extensions may load, so a model switch that
        # silently revoked it would unload the bus mid-session.
        self._bus_available: bool = bool(run_config.get("bus", False))
        # ``--no-context-files``/``-nc``: run-level like the flags above, so a
        # ``/model`` switch cannot silently re-enable the AGENTS.md/CLAUDE.md
        # discovery this invocation turned off.
        self._no_context_files: bool = bool(run_config.get("no_context_files", False))
        # Per-extension config overrides (S40): the parsed ``--ext-config`` map
        # ({name: {key: value}}). Merged over config.json's ``"extensions"`` block at
        # each backend load (``_load_backend_extensions``) so each extension's
        # ``api.config`` gets its slice. Resolved lazily against ``self.config``
        # (loaded just below), not here, so a config reload is reflected.
        self._ext_config_overrides: dict[str, dict[str, Any]] = dict(
            run_config.get("ext_config", {})
        )
        self.load_config()
        if cli_overrides:
            self._apply_cli_overrides(cli_overrides)

        # The storage-agnostic construction/lookup seam (W10): every current_session
        # assignment goes through this one instance rather than the concrete file
        # Session, so ``--store``/config ``session_store`` (W12,
        # docs/JMFTS-INTEGRATION-PLAN.md §3.1) can inject a different SessionCatalog
        # without touching the TUI again. Built AFTER ``self.config`` is loaded
        # (``load_config()`` above) since resolving the "jmfts" backend needs it —
        # and it performs a real network health check, so a misconfigured/
        # unreachable store must fail HERE, before the TUI's event loop starts
        # (Fail-Early), not on the first session action. ``session_catalog`` (an
        # explicit constructor arg, e.g. from tests) always wins over resolving one.
        self.session_catalog: SessionCatalog = (
            session_catalog
            if session_catalog is not None
            else build_session_catalog(
                self.config,
                run_config.get("store"),
                # --session-dir (unit S): the TUI's default is unchanged
                # (~/.tau/sessions); passing DIR is also how a human opens the
                # sessions --mode rpc wrote to its private <tmp>/.tau-<uid>/sessions.
                run_config.get("session_dir"),
            )
        )
        # Purely descriptive metadata for AgentSessionRuntime's F2 wire tuple
        # (docs/REMOTE-CONTROL.md §7.2) — the TUI itself never reads it back.
        # Resolved the same way session_catalog itself is; if a caller passed
        # an explicit `session_catalog=` not built from `run_config["store"]`
        # (a test double), this label may not describe it — harmless, since
        # nothing here branches on it.
        self._store_name: str = resolve_backend_name(self.config, run_config.get("store"))
        # AgentSessionRuntime (phase 3, H1) — the session-lifecycle layer
        # behind action_new_chat/action_clear_chat/on_chat_selected. `None`
        # until the first real (agent_session-bearing) backend is bound;
        # stays `None` for a backend double with no `.agent_session` (the
        # same tolerance every other backend-capability read in this class
        # already has — see `_rebind_after_session_swap`).
        self._session_runtime: Optional[AgentSessionRuntime] = None

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
        """Load ``~/.tau/config.json``, creating it from the packaged template if absent.

        Delegates to the single reader in ``config.py``. The TUI used to carry its
        own hardcoded default here, which disagreed with the packaged
        ``tau_default_config.json`` — so the file a first-run user actually got was
        not the one we maintain.
        """
        self.config = bootstrap_config()
        self.log(f"Loaded config with {len(self.config.get('models', {}))} models")

    def _session_facts(self) -> SessionFacts:
        """The configuration the empty chat pane states (handoff §4.4).

        Read fresh on every show — :class:`ChatDisplay` holds this method, not its
        result — because ``/model`` and a resumed session both change the answer
        while the chat is still empty.

        The tool list comes from :func:`_tools_row`, which asks
        :func:`~tau_coding_agent.backends.resolve_tool_names` over
        ``_apply_run_config``'s output — the exact call :class:`TauBackend` makes
        when it constructs them, so the pane cannot advertise a tool the next turn
        will not have. An unknown ``default_model``
        is reported as such rather than papered over: it is the same condition
        :meth:`action_new_chat` refuses to start on, and the pane is where a user
        can see it before typing.
        """
        name = self.config.get("default_model", "local-llm")
        entry = self.config.get("models", {}).get(name)
        if entry is None:
            return SessionFacts(
                tagline=self._tagline,
                model=f"{name} — not in config.json",
                endpoint="unusable until this is fixed",
                cwd=_display_path(self._cwd),
                tools="",
                store=self._store_name,
            )
        resolved = self._apply_run_config(entry)
        return SessionFacts(
            tagline=self._tagline,
            model=str(resolved.get("model", name)),
            # A model entry addressing a first-party API (anthropic, gemini) has no
            # base_url of its own; naming the backend is the true answer there, and
            # a blank row would read as "nowhere".
            endpoint=str(resolved.get("base_url") or resolved.get("backend", "")),
            cwd=_display_path(self._cwd),
            tools=_tools_row(resolved),
            store=self._store_name,
        )

    def compose(self) -> ComposeResult:
        """Compose the application layout."""
        yield Header()

        with Horizontal():
            yield ChatSidebar(self.session_catalog)

            with Vertical(id="main-area"):
                yield ChatDisplay(self._session_facts)
                yield ChatInput(id="chat-input")

            # The extension panel host (E10 §6 / S68) sits to the right of the main
            # area; it hides itself until an extension opens a panel.
            yield ExtensionPanelHost()

        # The foreign-lane strip (B3-b) and the extension status strip (E10 §6 /
        # S67) sit in the vertical flow just above the docked Footer; each hides
        # itself until it has something live to say.
        yield LaneStrip()
        yield ExtensionStatusBar()
        yield Footer()

    def on_mount(self):
        """Set up the application on mount."""
        # title/sub_title are now class-level TITLE/SUB_TITLE — see the comment
        # there for why they cannot wait until mount.

        # Focus input
        self.query_one("#chat-input", ChatInput).focus()

        # The first frame is a layout decision like any later one: an app started at
        # 80x24 with a panel already open must not render the starved chat once
        # before a resize corrects it.
        self._apply_side_columns()

        # ``tau --resume`` (§7): open the picker over the first frame. After the
        # refresh, not during mount — ``action_resume_session`` pushes a screen,
        # and a screen pushed before the base screen has laid out is placed
        # against a geometry that does not exist yet.
        if self._resume_on_start:
            self.call_after_refresh(self.action_resume_session)

    # ------------------------------------------------------------------
    # Side columns
    # ------------------------------------------------------------------

    def _apply_side_columns(self) -> None:
        """Show or hide the sidebar for the current ``_sidebar_open``.

        The ONE place ``#sidebar``'s display is written, so mount and ctrl+b
        cannot fight over it — an inline style set by one of them is otherwise
        permanent and invisible to the other.

        There is no longer a *responsive* half to this decision. It used to hide
        the sidebar automatically when an extension panel opened on a terminal too
        narrow for both (``_sidebar_fits``), but that rule only ever decided the
        case where the user had expressed no preference — an explicit ctrl+b won
        over it by design. §8 makes "no preference" mean CLOSED, so there is
        nothing left for the rule to decide: a visible sidebar is now, always,
        one the user asked for, and it is honored at any width.
        """
        sidebar = self.query_one(ChatSidebar)
        visible = self._sidebar_open
        if sidebar.display == visible:
            return
        sidebar.display = visible
        if visible:
            # A refresh may have landed and been deferred (_apply_sessions) while
            # this was collapsed — catch it up now that it's visible.
            sidebar.ensure_rendered()

    def set_extension_status(self, key: str, text: str | None) -> None:
        """Update one keyed slot in the extension status strip (E10 §6 / S67).

        The app-side landing for ``ctx.ui.set_status(key, text)`` — reached through
        ``_ExtensionUIDelegate.set_status``. Forwards to the single
        :class:`ExtensionStatusBar` in the layout, which updates the slot in place
        (or clears it when ``text is None``) and re-renders. The bar is composed
        unconditionally, so it is present for the whole app lifetime; the delegate is
        only bound after mount, so no pre-mount call can reach here.
        """
        self.query_one(ExtensionStatusBar).set_slot(key, text)

    def set_extension_panel(self, key: str, spec: dict[str, Any] | None) -> None:
        """Mount, update, or clear one keyed extension panel (E10 §6 / S68).

        The app-side landing for ``ctx.ui.panel(key, spec)`` — reached through
        ``_ExtensionUIDelegate.panel``. Forwards to the single
        :class:`ExtensionPanelHost` in the layout, which mounts a new panel, updates
        an existing key's panel in place, or removes it when ``spec is None``. The
        host is composed unconditionally, so it is present for the whole app lifetime;
        the delegate is only bound after mount, so no pre-mount call can reach here.
        """
        self.query_one(ExtensionPanelHost).set_panel(key, spec)

    async def on_extension_panel_action(self, message: ExtensionPanel.Action) -> None:
        """Dispatch a panel action's command back into the extension (E10 §6 / S68).

        A :class:`ExtensionPanel.Action` bubbles here when a user presses a panel
        action button. It runs the action's ``command`` (a name an extension
        registered via ``api.register_command``) through the SAME
        :meth:`run_extension_command` path the palette (S35) and typed slash commands
        (S46) use, with the action's ``args`` — so a panel closes the loop from a live
        surface to extension logic. The handler's returned value renders as a
        display-only ``system`` box (:meth:`_render_command_output`), never appended to
        the active path (the tree-as-truth invariant is untouched).

        Fail-Early: an action that names an UNKNOWN command surfaces an error notice
        (``handled is False``) rather than silently doing nothing — a mis-wired action
        is a construction bug, not a no-op. A handler exception is likewise surfaced,
        never swallowed.
        """
        runner = getattr(self.current_backend, "run_extension_command", None)
        if runner is None:
            return
        try:
            result = await runner(message.command, message.args)
        except Exception as e:
            self.notify(f"Panel action /{message.command} failed: {e}", severity="error")
            self.log.error(f"Panel action /{message.command} failed: {e}", exc_info=True)
            return
        if not result.handled:
            self.notify(
                f"Panel action → unknown command /{message.command}",
                severity="error",
            )
            return
        self._render_command_output(result)

    async def on_unmount(self) -> None:
        """Fire the notify-grade ``session_shutdown`` lifecycle hook on TUI quit (S41).

        This is the teardown counterpart to the ``session_start`` fired from
        :meth:`_load_backend_extensions`; it runs while the event loop is still
        alive (Textual awaits the app's unmount handler during shutdown), covering
        both an explicit quit and a Ctrl-C — Textual routes SIGINT through its own
        shutdown, which unmounts the app. getattr-guarded so a non-``TauBackend``
        test double (or a run that never built a backend) is a no-op. An extension's
        teardown exception is surfaced by the runner (never swallowed), not
        re-raised here — a failing shutdown hook must not wedge app teardown.

        Also closes the pooled τ-llm providers' HTTP clients for this loop
        (docs/PROVIDER-LIFETIME.md §6.3) — AFTER the shutdown hook above, since
        an extension's ``session_shutdown`` handler may itself make a final LLM
        call and needs a live client to do it with. This is the last point the
        loop is still guaranteed alive; there is no later hook to defer to.
        """
        backend = getattr(self, "current_backend", None)
        emit_shutdown = getattr(backend, "emit_session_shutdown", None)
        if emit_shutdown is not None:
            await emit_shutdown("quit")

        from tau_llm.client import aclose_providers

        await aclose_providers()

    async def on_input_submitted(self, event: Input.Submitted):
        """Handle message submission — the TUI's ONE input source.

        docs/SUBMISSION-LIFECYCLE.md phase 3. What a human typing means is now a
        :class:`~tau_agent_core.submission.Submission` handed to
        :meth:`AgentSession.submit` (via ``TauBackend.submit_turn``) rather
        than a private route into ``prompt()``: ``source="interactive"``,
        ``submitter="human"`` so every event this turn emits is attributable to a
        person at a terminal (phase 2's provenance stamp), and
        ``multitask_strategy="enqueue"`` per decision 1 — pi's TUI eventually binds
        Enter→steer and Alt+Enter→followUp, and because the strategy is a field on
        the record, the second keybinding is a one-line change here when ``steer``
        lands in phase 4.

        ``expand_commands=True`` (B2-b): the slash-command block that used to sit in
        this method is gone. ``AgentSession.submit`` resolves ``/compact`` / ``/tree``
        / ``/fork`` / ``/extensions`` and every extension-registered ``/name args``
        through :mod:`tau_agent_core.commands`, and reports the decision on
        ``SubmissionResult.command``; :meth:`_perform_command_outcome` does the half
        only a TUI can do. A NATS or timer submission still passes ``False`` and its
        "/compact" is literal prompt text — the flag is the security boundary, and
        this call site is the one that positively declares itself a human frontend.

        The PEEK before the submission (:func:`resolve_command`, the same pure
        function ``submit()`` uses) is a rendering concern, not a second dispatch:
        the transcript must not grow a user bubble, and ``self.messages`` must not
        grow a user turn, for input that will never become one. Asking afterwards
        would mean rendering the turn and then unrendering it. ``submit()`` remains
        the authority and resolves again on the post-``input``-hook text; the two
        can only disagree if a hook rewrites one into the other, which
        :meth:`_dispatch_command_submission` and :meth:`_get_assistant_response`
        each report rather than absorb.

        ``allow_user_input=True`` is the assertion only this call site (and
        ``rollback_turn``) can honestly make: a human typed this, so an extension
        hook running under the turn may ask that same human a question.

        Input history and clearing the widget stay here and are NOT part of the
        submission: they are properties of the ChatInput widget (up-arrow recall),
        and a bus or timer submission has no widget to recall into. Session
        materialisation, the working-list append and the rendered user turn stay
        here too, now gated on the peek.
        """
        # ChatInput is the app's only Input.Submitted source (it posts
        # Input.Submitted(self, ...)), so the submitting widget is always the
        # #chat-input ChatInput.
        input_widget = self.query_one("#chat-input", ChatInput)

        message = event.value.strip()

        if not message:
            return

        input_widget.add_to_history(message)
        input_widget.clear_input()

        # The submission record. See the docstring for every field's reason.
        submission = Submission(
            text=message,
            source="interactive",
            submitter="human",
            submission_id=uuid4().hex,
            multitask_strategy="enqueue",
            expand_commands=True,
            allow_user_input=True,
        )

        # Peek: will this dispatch as a command instead of starting a turn? Pure —
        # it runs nothing. ``submit()`` remains the authority and resolves again on
        # the post-``input``-hook text; this only decides whether to render a user
        # turn. An unknown "/…" resolves to None and falls through to the model
        # exactly as it always has.
        is_command = resolve_command(message, self._extension_command_names()) is not None

        # Session materialisation — the spec's submit() step 4, which that method's
        # own docstring assigns to the FRONTEND ("e.g. the TUI's action_new_chat").
        # It happens for a command as well as a prompt, and before either: ``submit()``
        # is a method ON an AgentSession, so with no session there is no door to admit
        # anything through. The visible consequence is that typing "/extensions" as the
        # very first thing starts a chat — which is what the app was one keystroke away
        # from doing anyway, and is preferable to a second, session-less command path
        # that would quietly diverge from this one.
        if self.current_session is None:
            await self.action_new_chat()
        assert self.current_session is not None  # action_new_chat sets current_session

        if is_command:
            await self._dispatch_command_submission(submission)
            return

        # Add the user turn to the working list so it is part of the context sent
        # to the model this turn. Do NOT persist it here: the AgentSession (bound to
        # this live Session, E3-ctx / D3) is the sole persister — it records the user
        # turn when the loop runs. The working list is reconciled back to the
        # authoritative log at turn-end (``self.messages = session.context``).
        self.messages.append({"role": "user", "content": message})

        # The user BUBBLE is NOT rendered here any more (B3-a). It is rendered from
        # the ``lane_start`` this submission produces, like every other source's,
        # which is Jupyter re-broadcasting ``execute_input``: *the submission
        # itself* goes out on the wire so every client shows it. Two things follow.
        # The text shown is the POST-``input``-hook text — what actually reached the
        # model, rather than what was typed at something that rewrote it. And a
        # queued second prompt's bubble appears when its turn starts rather than
        # stranded above a still-running exchange. Rendering it here as well would
        # be the half-migrated renderer: one source drawn by the frontend that
        # submitted it, every other drawn by the bus.

        # Run the turn in a worker so the event loop stays free while the model
        # streams — that is what lets Esc-to-cancel be processed mid-response
        # (a direct `await` here parked the App message pump for the whole turn).
        # Input is disabled for the duration; the LAST worker to finish re-enables
        # it (``_submissions_in_flight``).
        input_widget.disabled = True
        self._submissions_in_flight += 1
        self.is_generating = True
        self.sub_title = "Thinking… (Esc to cancel)"
        self._generate_response(submission)

    def _extension_command_names(self) -> list[str]:
        """The names extensions have registered as slash commands, for the peek.

        ``getattr``-guarded like every other backend-capability read in this class
        (:meth:`_disabled_extension_paths`, :meth:`get_system_commands`): a test
        double or a backend built before extensions loaded simply has none, which
        makes the peek fall through to the model — the same thing an unregistered
        ``/…`` has always done. The built-in commands need no backend at all; they
        are τ's own vocabulary, hardcoded in :mod:`tau_agent_core.commands`.
        """
        lister = getattr(self.current_backend, "get_extension_commands", None)
        if lister is None:
            return []
        return [name for name, _description in lister()]

    async def _dispatch_command_submission(self, submission: Submission) -> None:
        """Admit a command submission through the one door and perform its outcome.

        docs/SUBMISSION-LIFECYCLE.md phase 3. The submission goes through
        ``AgentSession.submit`` exactly like a prompt does — same admission, same
        ``input`` hook chain, same provenance stamp — and comes back with a typed
        :class:`~tau_agent_core.commands.CommandOutcome` instead of messages.

        No worker and no ``is_generating``: a dispatched command runs no model call,
        so there is nothing to stream, nothing to cancel with Esc, and no reason to
        gate the input. That is also why it does not go through
        :meth:`_get_assistant_response` — opening an exchange and taking the display
        lock for a turn that will not happen would leave an empty collapsible box in
        the transcript.

        Three outcomes, all of which say something rather than nothing:

        - a backend with no :meth:`submit_command` (a test double, a future backend)
          RAISES — the user typed a command and there is no door to send it through.
        - ``result.accepted is False`` surfaces the ``rejection_reason`` verbatim.
        - ``result.command is None`` means ``submit()`` ran a TURN instead: an
          ``input`` hook rewrote the text between this app's peek and the core's own
          resolution. That turn really ran, unrendered, so it is reported as an
          error rather than passed over — the transcript is now behind the session,
          and pretending otherwise is the divergence this method must not hide.
        """
        submit_command = getattr(self.current_backend, "submit_command", None)
        if submit_command is None:
            raise UnsupportedCommandError(
                f"{type(self.current_backend).__name__} has no submit_command(), so "
                f"the command {submission.text!r} cannot be admitted. Command "
                "dispatch lives in AgentSession.submit (docs/SUBMISSION-LIFECYCLE.md "
                "phase 3); a backend that cannot reach it cannot run commands, and "
                "sending the text to the model instead would be the silent fallback "
                "this lifecycle removes."
            )
        result = await submit_command(submission)
        if not result.accepted:
            self.notify(
                result.rejection_reason or f"{submission.text} was refused",
                severity="warning",
            )
            return
        if result.command is None:
            raise UnsupportedCommandError(
                f"{submission.text!r} was dispatched as a command by this app but "
                "AgentSession.submit ran a TURN for it — an `input` hook transformed "
                "the text after the app resolved it. The turn ran without being "
                "rendered; reload the transcript. Fix the hook, or stop it from "
                "rewriting text that resolves to a command."
            )
        await self._perform_command_outcome(result.command)

    async def _perform_command_outcome(self, outcome: CommandOutcome) -> None:
        """Do the half of a dispatched command only a frontend can do (B2-b).

        The other side of :mod:`tau_agent_core.commands`' split. ``performer="core"``
        means the session already ran it (an extension-registered command) and the
        only thing left is to show what it returned, as the same display-only
        ``system`` box :meth:`_render_command_output` mounts — never into
        ``self.messages``, so a command's report cannot leak into model input (E5 §1
        tree-as-truth).

        ``performer="frontend"`` is a built-in the core deliberately did not run
        because it needs a screen: ``/compact`` re-renders the transcript, ``/tree``
        and ``/fork`` open the browser, ``/resume`` opens the session picker, and
        ``/extensions`` paints a panel or runs a runtime management action. Each
        lands on the identical action the keybinding and the palette already call,
        so there is one implementation of each command and this method only routes.

        Fail-Early: an outcome naming a built-in this app has no branch for RAISES.
        That is the whole point of the seam — the core is allowed to resolve
        commands a given frontend cannot perform, and the contract is that such a
        frontend says so out loud instead of returning as though it had. A silent
        ``else: pass`` here would make :data:`FRONTEND_COMMANDS` a list of things
        that may or may not work depending on where you typed them.
        """
        if outcome.performer == "core":
            self._render_command_output(ExtensionCommandResult(handled=True, output=outcome.output))
            return

        if outcome.name == "compact":
            await self.action_compact()
            return
        if outcome.name in ("tree", "fork"):
            # pi aliases the two (keybindings.ts:252-253) — both open the browser.
            self.action_browse_tree()
            return
        if outcome.name == "resume":
            # §7's third surface. Bare ``/resume`` opens the picker — the SAME
            # ``action_resume_session`` the palette entry and ``--resume`` call.
            # ``/resume <ref>`` skips the picker and names the session directly.
            # Both end at the one ``ChatSelected`` loader, which resolves the ref
            # through ``SessionCatalog.resolve_ref`` — the same path/id/id-prefix
            # grammar ``--session REF`` uses headlessly (§7: one grammar). A ref
            # that resolves to nothing fails there with its own message rather
            # than silently degrading into "the picker opened instead".
            if outcome.args:
                self.post_message(ChatSelected(outcome.args))
                return
            self.action_resume_session()
            return
        if outcome.name == "extensions":
            if not outcome.args:
                self.action_show_extensions()
                return
            parts = outcome.args.split(None, 1)
            verb = parts[0]
            target = parts[1].strip() if len(parts) > 1 else ""
            await self.action_manage_extensions(verb, target)
            return

        raise UnsupportedCommandError(unsupported_command_message(outcome, "the Parley TUI"))

    @work(group="generation")
    async def _generate_response(self, submission: Submission) -> None:
        """Background worker: admit one submission and render the turn it starts.

        Replaces the old inline ``await``. The ``finally`` restores the input
        regardless of how the turn ended (normal, error, or cooperative abort —
        which returns the partial answer rather than raising).

        **Not ``exclusive`` any more, and that is the fix, not a relaxation.** An
        exclusive group cancels the group's other workers when a new one starts, so
        a second submission arriving mid-turn hard-cancelled the first — killing an
        admitted turn inside ``submit()`` and losing both the partial answer and the
        second prompt. That is precisely the silent drop docs/SUBMISSION-LIFECYCLE.md
        exists to remove ("nats_bus.py hand-rolls state['turn_in_flight'] and
        drops"). The submissions declare ``enqueue``; the second one now waits and
        then runs. Nothing else depended on the exclusivity:
        :meth:`action_cancel_generation` has never cancelled the worker — it trips
        the backend's abort signal and lets the turn unwind through its own
        ``finally``, which is what keeps the partial answer and the persistence
        consistent.
        """
        input_widget = self.query_one("#chat-input", ChatInput)
        try:
            await self._get_assistant_response(submission)
        except Exception as e:
            self.notify(f"Error: {str(e)}", severity="error")
            self.log.error(f"Error getting response: {e}")
            self.log.error(traceback.format_exc())
            # A traceback is verbatim by definition — its line breaks ARE the
            # stack frames, and this box is the only place the user sees them.
            self.query_one(ChatDisplay).add_message(
                "system",
                f"**Error occurred:**\n```\n{str(e)}\n{traceback.format_exc()}\n```",
                source="verbatim",
            )
        finally:
            # A turn ending while another submission is still outstanding must not
            # re-enable the input or clear ``is_generating``: the app is still busy,
            # Esc must still reach the turn that is running, and typing into a live
            # queue is exactly how the second prompt used to get lost.
            self._submissions_in_flight -= 1
            if self._submissions_in_flight <= 0:
                self._submissions_in_flight = 0
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
        with the partial answer and the last worker's ``finally`` re-enables input.
        No hard task-cancel, so there is no half-applied widget/persistence state.

        It aborts THE turn that is running, not everything outstanding: a submission
        queued behind it gets its own fresh ``AbortSignal`` when ``submit()`` admits
        it, so Esc cancels this answer and the next queued prompt still runs. That
        matches what Esc has always meant here ("stop this response"); "discard the
        queue" is not a thing the TUI can express today and inventing it silently —
        cancelling a submission the core has already accepted — is the drop this
        lifecycle removes rather than adds.
        """
        if not self.is_generating or self.current_backend is None:
            return
        self.current_backend.abort()
        self.sub_title = "Cancelling…"

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Decide which of the two ``priority=True`` app bindings are live right now.

        ``False`` does two things at once, and both are load-bearing here: the Footer
        stops advertising the binding, and the key is no longer consumed — a priority
        binding whose ``check_action`` is falsy makes ``run_action`` return ``False``,
        so textual's ``_check_bindings`` keeps walking the chain down to the focused
        widget.

        - **``rollback_turn`` needs a turn to roll back.** Idle, ``ctrl+z`` falls
          through to the ``ChatInput`` TextArea as its ordinary undo; generating, the
          input is disabled and that undo is unreachable anyway, so the two uses never
          contend.
        - **Neither survives a modal.** A ``priority=True`` App binding beats a modal's
          own bindings (the priority pass walks ``reversed(_binding_chain)``, and the
          App is at the far end of it), so while a dialog is up ``escape`` reached
          ``action_cancel_generation`` — which dispatches, and therefore CONSUMES the
          key — instead of closing the dialog. That was invisible while no modal could
          be open during a turn: the action no-op'd and Esc merely did nothing. It is
          not invisible now. :class:`RollbackPromptModal` is open precisely while a
          turn generates, so Esc would have aborted the very turn the modal exists to
          roll back, leaving the user with a dialog that will not close and a
          submission that can no longer be admitted. Ceding both keys to whatever
          dialog is on top restores "Esc closes the dialog" everywhere, and ``ctrl+z``
          becomes undo inside the rollback prompt editor rather than a second
          rollback modal stacked on the first.
        """
        if action in ("rollback_turn", "cancel_generation") and len(self.screen_stack) > 1:
            return False
        if action == "rollback_turn":
            return self.is_generating
        return super().check_action(action, parameters)

    def watch_is_generating(self, generating: bool) -> None:
        """Re-evaluate the Footer when a turn starts or stops.

        :meth:`check_action` reads ``is_generating``, and Textual only re-queries
        bindings when it is told to; without this the "Rollback" label would appear
        and disappear a beat late (on the next focus change), which for a binding
        whose whole point is "press this DURING a turn" is the wrong beat.
        """
        self.refresh_bindings()

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        """The most recent user message's text, flattened — the rollback prefill.

        Mirrors ``TauBackend._extract_last_user_message``: content is a plain string
        on the TUI's own working list and a block list once it has been round-tripped
        through the session log, and both shapes reach here.
        """
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        return ""

    @work(group="rollback")
    async def action_rollback_turn(self) -> None:
        """ctrl+z: abort the in-flight turn, un-path it, and run a prompt in its place.

        The TUI affordance for ``multitask_strategy="rollback"``
        (docs/SUBMISSION-LIFECYCLE.md decision 2), which has been in the core since
        phase 2 with no way for a human to reach it. Esc's
        :meth:`action_cancel_generation` stops a turn and leaves its partial work on
        the active path; this is the variant that also moves the cursor back to the
        leaf the aborted turn started from, so the abandoned messages fall off the
        ``parentId`` walk. Nothing is deleted — the tree browser still shows them
        (decision 2: "append-only means nothing was un-said, only un-pathed").

        Runs as a worker: it must ``push_screen_wait`` the prompt modal, and — more
        importantly — it must not block the message pump, because the turn it is
        rolling back is still streaming into the display while this runs. The group
        is deliberately NOT ``"generation"``: that group is ``exclusive``, so landing
        in it would hard-cancel the very worker whose turn ``submit()`` is about to
        abort cooperatively, and a cancelled task mid-``submit_turn`` is the
        half-applied state ``action_cancel_generation`` exists to avoid.

        The refusals, all of which say so rather than silently doing something else:

        - **Nothing generating.** ``submit()`` reads "is a turn in flight" at
          admission and, finding none, degrades to an ordinary turn at the current
          cursor — nothing is un-pathed. That is right for the core (there is nothing
          to discard) and wrong for a human who just asked to discard something, so
          the check is here, before the submission, and again after the modal closes,
          since the turn can finish while the prompt is being typed.
        - **A slash command.** ``rollback_turn`` submits with ``expand_commands``
          ``False`` and keeps it that way now that B2-b has given the flag a
          consumer: this submission's whole job is to run a MODEL turn in place of
          the one it aborted, and dispatching a command instead would leave the
          conversation un-pathed with nothing running in the discarded turn's place.
          A leading "/" is therefore refused with the reason rather than sent.
        - **``accepted=False``.** The stale-target guard (``_current_turn_token``)
          refuses when a different submission was admitted and completed while this
          one waited for the turn slot, because rolling back then would discard THAT
          submission's work. Its ``rejection_reason`` is shown verbatim: a typed
          refusal the UI swallowed is the silent drop this whole lifecycle exists to
          prevent.

        The replacement turn now DOES stream into the transcript, and this method
        did not have to ask for it: since B3-a the renderer is a persistent bus
        subscription, and a rollback submission is admitted through the same
        ``submit()`` as any other, so it opens its own lane like any other. What
        this method still does afterwards is swap ``self.messages`` and
        ``reload_messages`` — the same seam ``/compact`` and the tree browser use —
        because the un-pathing is a TREE change and only a rebuild from the
        post-rollback session shows the abandoned turn dropping out of the context.
        """
        if not self.is_generating:
            self.notify(
                "Nothing is generating — rollback discards an in-flight turn",
                severity="warning",
            )
            return
        # Bound up front: it survives the intervening ``await``s (unlike a
        # hasattr-narrowed local) and is ``None`` for a backend that lacks it.
        rollback_turn = getattr(self.current_backend, "rollback_turn", None)
        if rollback_turn is None:
            self.notify("This backend does not support rollback", severity="warning")
            return

        text = await self.push_screen_wait(RollbackPromptModal(self._last_user_text(self.messages)))
        if text is None:
            return
        text = text.strip()
        if not text:
            self.notify(
                "Rollback needs a prompt to run in place of the aborted turn",
                severity="warning",
            )
            return
        if text.startswith("/"):
            self.notify(
                "A rollback prompt is sent to the model as-is — slash commands are "
                "not expanded here",
                severity="warning",
            )
            return
        if not self.is_generating:
            self.notify(
                "The turn finished while you were typing — there is nothing left to roll back",
                severity="warning",
            )
            return

        self.sub_title = "Rolling back…"
        try:
            result = await rollback_turn(text)
        except Exception as e:
            self.notify(f"Rollback failed: {e}", severity="error")
            self.log.error(f"Rollback failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        if not result.accepted:
            self.notify(result.rejection_reason or "Rollback was refused", severity="warning")
            self._refresh_subtitle()
            return

        # Same re-render seam as action_compact / action_browse_tree / the elide flow:
        # the session is the authority (the AgentSession persisted this turn through
        # the bound live log), so read the post-rollback context back rather than
        # patching the working list.
        assert self.current_session is not None  # is_generating implies a session
        self.messages = list(self.current_session.context)
        await self.query_one(ChatDisplay).reload_messages(self.messages)
        self._refresh_subtitle()
        self.notify("Rolled back and re-ran from before the aborted turn")

    async def _get_assistant_response(self, submission: Submission) -> None:
        """Admit ``submission`` and await the turn it starts. Renders nothing.

        B3-a. This method used to be the renderer: it opened an exchange, awaited
        ``stream_submission``, fed its ``on_event`` stream into the display, and
        closed the exchange with the returned usage. That shape is single-stream by
        construction — one awaited call, one buffer, one exchange — so a forked
        second agent and a turn originated by a bus, timer or extension had no
        representation in it. Rendering now happens in :meth:`_on_render_event`,
        off a subscription that is attached for the life of the backend and sees
        every lane, including the ones this app never submitted.

        What is left here is the half that genuinely belongs to the SUBMITTER
        rather than to the renderer: awaiting completion (so the input re-enables
        and ``is_generating`` clears when THIS turn is done), surfacing a typed
        refusal, performing a command outcome, and reconciling the working message
        list against the session that just recorded the turn.

        :attr:`_working_list_lock` is held across read-context → await → write-back
        for the reason its own comment gives: ``self.messages`` is both the context
        handed over and the thing rebuilt afterwards. It is NOT a render lock any
        more — another lane streams into the display while this is held.
        """
        assert self.current_session is not None  # set before a turn runs
        # Same idiom, newly load-bearing: annotating ``submission`` makes this a
        # TYPED function, so mypy now checks the body it previously skipped.
        assert self.current_backend is not None  # a turn cannot start without one
        async with self._working_list_lock:
            # THE one door, awaited without a second subscription: the persistent
            # renderer has already drawn every delta this turn produced, so asking
            # for them again (``stream_submission``) would mean rendering twice.
            result = await self.current_backend.submit_turn(submission, self.messages)

            # A typed in-band refusal (LSP ``ApplyWorkspaceEditResult``) is SHOWN,
            # never swallowed — a refusal the UI hides is the silent drop the whole
            # lifecycle exists to prevent. ``enqueue`` waits rather than refusing, so
            # this is not reachable today; it is here because the strategy is a field
            # on the record and the value at the call site is one line from changing
            # (decision 1's Alt+Enter / phase 4's steer).
            if not result.accepted:
                self.notify(result.rejection_reason or "The turn was refused", severity="warning")

            # An `input` hook rewrote this prompt into a command AFTER the app
            # resolved it as ordinary text (:meth:`on_input_submitted`'s peek), so
            # ``submit()`` dispatched instead of running a turn. Perform the outcome
            # rather than showing an empty answer — the user's input WAS acted on.
            # No exchange or user bubble was drawn for it either, because a
            # dispatched submission emits no ``submission_start``.
            if result.command is not None:
                await self._perform_command_outcome(result.command)

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

            # Refresh sidebar. Starts a thread worker and returns immediately
            # (see ChatSidebar.refresh_chats) — nothing after this point in
            # this turn depends on the listing having landed, so ending the
            # turn does not wait on it.
            self.query_one(ChatSidebar).refresh_chats()

    # ------------------------------------------------------------------
    # The renderer: one persistent bus subscription, many lanes (B3-a)
    # ------------------------------------------------------------------

    @staticmethod
    def _lane_label(source: object, submitter: object) -> str | None:
        """How this lane should be marked, or ``None`` for "a human typed it here".

        The Jupyter rule the spec states and warns is easy to get backwards: a
        frontend filters on "is this mine?" to decide HOW to render, and still
        renders the rest. So this returns a LABEL, never a "drop it" — the only
        thing the answer changes is whether the exchange is badged with where it
        came from.
        """
        if source == "interactive" and submitter == "human":
            return None
        return f"{source} · {submitter}"

    @staticmethod
    def _lane_role(source: object) -> str:
        """The :class:`MessageBox` role for a foreign lane's submission bubble (B3-b).

        The SOURCE is the role, so ``ROLE_LABELS`` gives the bubble its border
        title — "Timer", "Bus", "Sub-agent" — instead of the "User" a submission
        nobody typed used to wear. An unlisted source is passed through verbatim
        and capitalized by :meth:`MessageBox.on_mount`, which is the whole reason
        this is a lookup with a fallback rather than a match: a source this build
        has never heard of must still render, attributed as best we can, because a
        renderer that hides what it does not recognise is the failure mode.

        A source that is missing or blank is the one case with nothing to say, and
        it says exactly that — ``"unknown"`` — rather than borrowing ``"user"``
        and claiming a human was involved.
        """
        text = str(source or "").strip()
        return text or "unknown"

    async def _on_render_event(self, event: dict) -> None:
        """Render one lane-tagged event from the persistent bus subscription.

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3 / B3-a. Called for EVERY
        turn the session runs — this app's own typed prompts, an extension's or a
        bus driver's submission, and a ``fork``'s second agent — because the
        subscription is attached to the session rather than to a call.
        """
        display = self.query_one(ChatDisplay)
        kind = event.get("kind")
        lane = event.get("lane") or DEFAULT_LANE
        if kind == "lane_start":
            label = self._lane_label(event.get("source"), event.get("submitter"))
            # The submission itself, rendered — Jupyter's ``execute_input``
            # re-broadcast. A foreign lane's bubble is badged with its origin so
            # "the agent just said something I did not ask for" is legible rather
            # than mysterious, AND typed as its source (B3-b) so the border reads
            # "Timer" rather than "User" over text no user typed.
            role = "user" if label is None else self._lane_role(event.get("source"))
            # The submission text verbatim: whoever (or whatever) submitted it
            # typed lines, not markdown, and this bubble is the record of what
            # was actually sent.
            bubble = display.add_message(
                role, event.get("text", ""), subtitle=label or "", source="verbatim"
            )
            if label is not None:
                bubble.add_class(LANE_FOREIGN_CLASS)
            self._lane_started[lane] = time.time()
            self.query_one(LaneStrip).open_lane(lane, label)
            await display.begin_exchange(lane, label=label)
            return
        if kind == "lane_end":
            self.query_one(LaneStrip).close_lane(lane)
            started = self._lane_started.pop(lane, None)
            # Fail-Early: an unknown start time is reported as unknown (the summary
            # omits the duration) rather than as a fabricated 0.0 second turn.
            elapsed = None if started is None else time.time() - started
            # The G4 telemetry rides the LAST completion's usage.extra (t/s /
            # forced-share are per-completion, not aggregates); format_telemetry
            # returns None on a provider that reported nothing, so the summary then
            # reads exactly as it did pre-G4.
            telemetry = format_telemetry(event.get("extra") or {})
            await display.finalize_exchange(
                tokens=int(event.get("tokens", 0) or 0),
                seconds=elapsed,
                telemetry=telemetry,
                lane=lane,
            )
            # Reconcile the working list against the session that just recorded
            # this turn. :meth:`_get_assistant_response` does this too, and for its
            # own turn this is the earlier (pre-drain) half of the same read — but
            # a turn nobody here submitted has NO awaiting caller to do it, and a
            # rendered turn the model-input list does not know about is exactly the
            # divergence that makes the next typed prompt contradict the screen.
            if self.current_session is not None:
                self.messages = list(self.current_session.context)
            self._refresh_subtitle()
            return
        await display.handle_stream_event(event)

    def _bind_render_subscription(self) -> None:
        """(Re)attach the persistent renderer to the current backend's bus.

        One subscription per backend, dropped and remade when the backend or its
        session changes — the same lifetime ``_session_event_unsub`` has, and for
        the same reason: a replaced backend's dead bus must stop reaching this
        app's widgets.

        Lanes still open on the OLD router are abandoned rather than closed,
        deliberately: every caller of this method (new-chat, clear, resume,
        model-swap) also clears or reloads the transcript, so the exchange those
        lanes were drawing no longer exists to be finalized.

        ``getattr``-guarded like every other backend-capability read in this class:
        a test double or a non-``TauBackend`` simply renders nothing.
        """
        if self._render_router is not None:
            self._render_router.detach()
            self._render_router = None
        self._lane_started = {}
        # The strip reports what is live; lanes abandoned with the old router are
        # not, so it is cleared with the clocks rather than left advertising a fork
        # whose events can no longer arrive.
        self.query_one(LaneStrip).clear_lanes()
        subscribe_render = getattr(self.current_backend, "subscribe_render", None)
        if subscribe_render is None:
            return
        self._render_router = subscribe_render(self._on_render_event, on_orphan=self._log_orphan)

    def _log_orphan(self, reason: str) -> None:
        """Report an event that named no open lane (never drop it in silence).

        These are real — ``continue_conversation()`` on resume, and a bare
        ``compact()``, emit ``agent_start``/``agent_end`` with no submission to
        stamp them — and they are not errors, so this is the Textual log rather
        than a toast. What it is not is nothing: a renderer that swallowed them
        would be indistinguishable from one that had quietly stopped working.
        """
        self.log(f"render router: {reason}")

    def _bind_backend_session(self) -> None:
        """Rebind the backend's AgentSession onto the current live ``Session``,
        without going through ``AgentSessionRuntime``.

        The fallback path for a backend with no real ``AgentSession`` to build
        a runtime around (a test double, or a non-``TauBackend`` — the same
        tolerance every backend-capability read in this class already has).
        Every call site that DOES have a real ``AgentSession`` goes through
        ``AgentSessionRuntime`` instead (H1, phase 3): the runtime performs
        this same ``session_log`` bind internally (plus the H2 veto and H3
        reset this method knows nothing about) and then invokes
        :meth:`_rebind_after_session_swap` itself, via
        ``AgentSessionRuntime.set_rebind_session``. This method exists so the
        NO-runtime case still gets the ``session_log`` bind
        :meth:`_rebind_after_session_swap` does not perform.
        """
        binder = getattr(self.current_backend, "bind_session_log", None)
        if binder is not None and self.current_session is not None:
            binder(self.current_session)
        self._rebind_after_session_swap()

    def _rebind_after_session_swap(self) -> None:
        """Reattach TUI-specific plumbing to ``self.current_backend`` (H1's
        rebind callback).

        Everything ``AgentSessionRuntime``'s own swap does NOT know how to do
        — the seam-3 extension-bus bridge, the model-name resolver, the
        renderer — because it is TUI-specific, not session-lifecycle logic
        (``agent_session_runtime.py``'s module docstring is explicit that
        this is exactly why ``set_rebind_session`` exists rather than the
        runtime hardcoding it). Two callers:

        - :meth:`_bind_backend_session` (the no-runtime fallback above), which
          calls this directly, after its own ``session_log`` bind.
        - ``AgentSessionRuntime.set_rebind_session``'s callback, registered by
          ``action_new_chat``/``action_clear_chat``/``on_chat_selected``
          BEFORE calling ``new_session``/``fork``/``switch_session`` — invoked
          by the runtime itself, AFTER the swap's ``session_log`` is already
          live and its turn lock has been released (so this method reading
          ``self.current_backend.agent_session`` fresh, right here, can never
          race the swap that produced it).

        Reads ``self.current_backend``/``self.current_session`` rather than
        taking them as parameters — both callers above have ALREADY updated
        them before this runs.
        """
        # Seam-3 → extension bus (S21 / §E3c.4): route this backend's session
        # lifecycle events onto its AgentSession's EventBus so extension handlers
        # (api.on("session_before_compact", …)) fire. Rebind on every current-session
        # change, dropping the previous backend's subscription first so a swapped
        # backend's dead bus stops receiving events (single live listener, no leak).
        if self._session_event_unsub is not None:
            self._session_event_unsub()
            self._session_event_unsub = None
        agent_session = getattr(self.current_backend, "agent_session", None)
        if agent_session is not None:
            self._session_event_unsub = subscribe_session_events(agent_session.route_session_event)
            # Bind the model-name resolver (S45) so an extension's ctx.set_model(name)
            # resolves NAME through the same config "models" map --model uses. Guarded
            # by getattr so a non-TauBackend / test double is a no-op, not an error.
            binder = getattr(agent_session, "set_model_resolver", None)
            if binder is not None:
                binder(make_model_resolver(self.config.get("models", {})))

        # B3-a: (re)attach the persistent renderer, alongside — and for the same
        # lifetime as — the extension-bus bridge above. Rendering is no longer
        # something a turn brings with it, so it has to be bound where the backend
        # is, not where a prompt is.
        self._bind_render_subscription()

    def _build_session_runtime(
        self, backend: Any, model: str, backend_name: str
    ) -> Optional[AgentSessionRuntime]:
        """``AgentSessionRuntime`` over ``backend``'s ``AgentSession`` — or
        ``None`` when ``backend`` has none (test double / non-``TauBackend``,
        the same tolerance :meth:`_rebind_after_session_swap` already has).

        Always installs :meth:`_rebind_after_session_swap` as the rebind
        callback — the one thing every one of the three call sites needs and
        would otherwise have to register identically three times.
        """
        agent_session = getattr(backend, "agent_session", None)
        if agent_session is None:
            return None
        runtime = AgentSessionRuntime(
            agent_session, self.session_catalog, os.getcwd(), model, backend_name, self._store_name
        )
        runtime.set_rebind_session(lambda _session: self._rebind_after_session_swap())
        return runtime

    def _apply_run_config(self, model_config: dict) -> dict:
        """Inject run-level tool flags into a model_config before create_backend (S28).

        Both tool-suppression flags empty the built-in set (``tools=[]``); which
        one was given rides as ``no_tools`` — ``"all"`` (``--no-tools``) or
        ``"builtin"`` (``--no-builtin-tools``) — the single resolved policy
        ``TauBackend`` forwards to ``AgentSession``. Only ``"all"`` also withholds
        extension-registered tools; under ``"builtin"`` they merge in as usual
        (``AgentSession._build_turn_tools``). ``--exclude-tools`` rides as an
        ``exclude_tools`` denylist that ``TauBackend`` applies to the resolved
        built-ins. Returns the config unchanged when no flag is set, so a bare
        ``tau`` is untouched; otherwise a shallow copy (never mutate the shared
        ``config["models"]`` entry).

        This runs at EVERY ``create_backend``, which is what makes these flags
        survive a mid-session ``/model`` switch: the policy is re-applied to
        whichever model entry the switch selected, instead of having been baked
        into the one entry the process started on.
        """
        # Fold the top-level ``reasoning_replay`` default into this entry when the
        # entry doesn't set its own (per-model wins; else the global default; else
        # build_model_from_config's "turn"). Done here so both create_backend sites
        # (new-chat, resume) inherit it.
        global_replay = self.config.get("reasoning_replay")
        inject_replay = global_replay is not None and "reasoning_replay" not in model_config
        if not (
            self._exclude_tools
            or self._no_tools
            or self._tool_allowlist is not None
            or inject_replay
            or self._bus_available
            or self._no_context_files
        ):
            return model_config
        mc = dict(model_config)
        # Allowlist first, suppression second: --no-tools is the stronger claim and
        # must win when both are given, matching resolve_model_config's own
        # if/elif on the headless path. Writing them the other way round would let
        # -t hand back tools that -nt had just withheld.
        if self._tool_allowlist is not None:
            mc["tools"] = list(self._tool_allowlist)
        if self._no_tools:
            mc["no_tools"] = self._no_tools
            mc["tools"] = []
        if self._exclude_tools:
            mc["exclude_tools"] = self._exclude_tools
        if inject_replay:
            mc["reasoning_replay"] = global_replay
        if self._bus_available:
            # Only ever set TRUE here: ``--bus`` grants the capability, and a
            # model entry may grant it on its own (``"bus_available": true``).
            # Writing False would let the absence of a flag REVOKE what the
            # config file deliberately allowed.
            mc["bus_available"] = True
        if self._no_context_files:
            # Only ever set TRUE, like ``bus_available``: -nc's absence must not
            # revoke a ``"no_context_files": true`` the config file set.
            mc["no_context_files"] = True
        return mc

    async def _load_backend_extensions(self) -> None:
        """Load file-path extensions into the current backend's live session (E5 §2.2).

        Called after every ``create_backend`` (new-chat, model-swap, resume) so a
        file extension's mutating hooks fire in the ``AgentSession`` that backend
        drives — the TUI half of the seam the E0–E4 loader left disconnected (§0).
        Loads the run-level explicit ``-e`` paths + ``~/.tau/extensions`` discovery
        (unless ``-ne``).

        Errors are surfaced as TUI notices, never to stderr — a stderr write during
        a live Textual render corrupts the screen (this is why the loader stopped
        printing, S25). Both *discovered* AND *explicit* ``-e`` failures are collected
        into ``result.errors`` (``collect_explicit_errors=True``) and shown as
        per-extension warnings, so the extensions that DID load stay bound and the
        ``/extensions`` listing shows them plus a "Load errors" section — a launched
        TUI can't cleanly abort mid-load, and raising past the partial result left
        the listing empty while the good extensions' tools kept working (split-brain
        fix, docs/EXTENSIONS-DEMO-ROADMAP.md). The outer ``except`` remains a
        backstop for a non-per-extension failure (e.g. config resolution). Guarded by
        ``getattr`` so a backend without the seam (a test double, a non-``TauBackend``)
        is a no-op.
        """
        # Route extension api.notify(...) to this TUI (E5 §4 / S33). Set on every
        # backend that supports it, right after it is created, so a loaded
        # extension's notify paints on-screen instead of the headless stderr sink.
        # Guarded by getattr so a non-TauBackend test double is a no-op.
        set_delegate = getattr(self.current_backend, "set_ui_delegate", None)
        if set_delegate is not None:
            set_delegate(_ExtensionUIDelegate(self))

        # Reset first so a backend swap never leaves the /extensions listing (S34)
        # showing the previous backend's extensions if this load fails or no-ops.
        self._extension_load_result = LoadExtensionsResult()

        loader = getattr(self.current_backend, "load_extensions", None)
        if loader is None:
            return
        # Resolve per-extension config (S40): config.json ``"extensions"`` slices +
        # the parsed ``--ext-config`` overrides (CLI > config.json), sliced per
        # extension by file stem inside the session and handed to ``api.config``.
        extensions_config = resolve_extensions_config(self.config, self._ext_config_overrides)
        try:
            result = await loader(
                self._extension_paths or None,
                discover=self._discover_extensions,
                extensions_config=extensions_config,
                # collect_explicit_errors: a launched TUI can't cleanly abort
                # mid-load, so an explicit ``-e`` failure is demoted to a collected
                # error (same as a discovered one) instead of raising. This keeps the
                # extensions that DID load bound AND returned — without it the loader
                # raised past the partial result and left the /extensions listing
                # empty while the good extensions' tools/commands kept working
                # (split-brain fix, docs/EXTENSIONS-DEMO-ROADMAP.md).
                collect_explicit_errors=True,
            )
        except Exception as e:
            self.notify(f"Extension failed to load: {e}", severity="error")
            self.log.error(f"Extension load failed: {e}", exc_info=True)
            return
        # Keep the result for the /extensions palette listing (E5 §5 / S34).
        self._extension_load_result = result
        for err in result.errors:
            self.notify(f"Extension error ({err.path}): {err.error}", severity="warning")
        if result.extensions:
            self.log(f"Loaded {len(result.extensions)} extension(s)")

        # Fire the notify-grade ``session_start`` lifecycle hook (E6 §2 / S41) now
        # that the just-loaded extensions' handlers are registered — so a
        # ``session_start`` handler can reconstruct state from ``ctx.entries()`` /
        # install watchers. getattr-guarded so a non-``TauBackend`` test double is a
        # no-op (same pattern as ``set_ui_delegate``/``load_extensions``). The
        # teardown counterpart fires from :meth:`on_unmount` on TUI quit.
        emit_start = getattr(self.current_backend, "emit_session_start", None)
        if emit_start is not None:
            await emit_start("startup")

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
            self.current_backend = create_backend(self._apply_run_config(model_config))
            self.log(f"Created backend: {model_config.get('backend')} for model {model}")
        except Exception as e:
            self.notify(f"Failed to create backend: {str(e)}", severity="error")
            self.log.error(f"Backend creation failed: {e}", exc_info=True)
            return

        # Create new session (writes the header + system message; append-only).
        # --append-system-prompt sections augment the base prompt on a NEW session
        # (S28); a resumed session keeps its own stored prompt, so no append there.
        system_prompt = _append_system_prompt(
            self.config.get("system_prompt", "You are a helpful assistant."),
            self._append_system_prompt,
        )
        # AgentSessionRuntime (H1, phase 3): the runtime performs the
        # session_log bind, the H2 veto check, and the H3 reset (a no-op
        # here — this AgentSession was just constructed, so there is
        # nothing dirty to reset) — see _build_session_runtime.
        self._session_runtime = self._build_session_runtime(
            self.current_backend, model, model_config["backend"]
        )
        if self._session_runtime is not None:
            result = await self._session_runtime.new_session(
                persist=True, system_prompt=system_prompt or None
            )
            if result.get("blocked"):
                # Finding 1 (phase-3 review): the in-flight turn did not stop
                # within the runtime's bounded wait — nothing was touched.
                self.notify(result["reason"], severity="warning")
                return
            if result["cancelled"]:
                self.notify("New chat cancelled by an extension", severity="warning")
                return
            self.current_session = result["session"]
        else:
            # No real AgentSession on this backend (test double / non-TauBackend)
            # — same tolerance _bind_backend_session's own getattr guards have.
            self.current_session = self.session_catalog.create(
                os.getcwd(),
                model,
                model_config["backend"],
                system_prompt=system_prompt or None,
            )
            self._bind_backend_session()
        await self._load_backend_extensions()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(ChatDisplay)
        await display.clear_messages()

        # Update UI
        self.sub_title = f"{model}"
        self.notify(f"Started new chat with {model}")

        # Refresh sidebar. Starts a thread worker and returns immediately (see
        # ChatSidebar.refresh_chats); this is the last thing the action does,
        # so nothing here waits on the listing. The new session is already
        # current — it just won't show up in the sidebar list until the
        # worker lands, same as any other session created elsewhere while
        # this one is open. Callers that need it to have landed (tests) can
        # ``await app.workers.wait_for_complete()``.
        self.query_one(ChatSidebar).refresh_chats()

    def action_toggle_sidebar(self):
        """Toggle sidebar visibility.

        Records the flip as ``_sidebar_open`` and lets ``_apply_side_columns`` do
        the write, so the key always inverts what is ON SCREEN — including opening
        the sidebar next to an extension panel on a narrow terminal, which costs
        the chat columns and is nonetheless what was asked for. The choice sticks
        (see the attribute): a keypress is not a hint the layout may overrule.

        This is the ONLY way the sidebar opens now that §8 mounts it closed, which
        is why the binding is listed in the Footer rather than hidden.
        """
        self._sidebar_open = not self.query_one(ChatSidebar).display
        self._apply_side_columns()

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

    def action_show_extensions(self) -> None:
        """List loaded extensions + load errors in the transcript (E5 §5 / S34).

        Reads the last ``_load_backend_extensions`` result — the now-populated
        registry/runner via :func:`summarize_extensions` — annotated with each
        extension's live enabled/disabled state (E10 §6 / S70), and renders it as a
        display-only ``system`` box. This is UI chrome, NOT a conversation node: it is
        neither appended to the working message list nor persisted, so the durable-hook
        invariant (the model's input = system prompt + the linear active path) is
        untouched — runtime management lifts the D-E5-6 read-only stance without
        touching that invariant (the actions run on the runner, not the tree).
        """
        listing = self._format_extensions_listing(
            self._extension_load_result, self._disabled_extension_paths()
        )
        # τ authors this string as markdown (see _format_extensions_listing) —
        # headings and bullet lists it wrote on purpose, not captured output.
        self.query_one(ChatDisplay).add_message("system", listing, source="markdown")

    def _disabled_extension_paths(self) -> set[str]:
        """The set of currently runtime-disabled extension paths (E10 §6 / S70).

        Read from the live backend's managed-extension state; ``getattr``-guarded so a
        non-``TauBackend`` test double (no seam) reports none disabled.
        """
        lister = getattr(self.current_backend, "list_managed_extensions", None)
        if lister is None:
            return set()
        return {path for path, enabled in lister() if not enabled}

    async def action_manage_extensions(self, verb: str, target: str) -> None:
        """Run a runtime ``/extensions`` action (enable/disable/reload) — E10 §6 / S70.

        Lifts the D-E5-6 read-only stance: dispatches to the live backend's
        ``disable_extension`` / ``enable_extension`` / ``reload_extension`` (each fires
        the S41 ``session_shutdown`` / ``session_start`` lifecycle hooks for clean
        teardown/bring-up), then re-renders the listing so the outcome is visible. All
        output is display-only chrome — never a conversation node, so the tree-as-truth
        invariant holds. A bad verb, an empty target, or a broken reload is surfaced as
        an error notice (Fail-Early: reported, never swallowed or faked).
        """
        actions = {"enable", "disable", "reload"}
        if verb not in actions:
            self.notify(
                f"Unknown /extensions action {verb!r} (use: enable | disable | reload)",
                severity="error",
            )
            return
        if not target:
            self.notify(f"/extensions {verb} needs an extension name", severity="error")
            return
        action = getattr(self.current_backend, f"{verb}_extension", None)
        if action is None:
            self.notify("Runtime extension management is unavailable here", severity="warning")
            return
        try:
            result = await action(target)
        except Exception as e:
            self.notify(f"/extensions {verb} {target} failed: {e}", severity="error")
            self.log.error(f"/extensions {verb} {target} failed: {e}", exc_info=True)
            return
        self.notify(result.message, severity="information" if result.ok else "warning")
        # Re-render the listing so the enabled/disabled column reflects the action.
        self.action_show_extensions()

    async def _dispatch_extension_command(self, name: str, args: str = "") -> None:
        """Run an extension-registered command from the palette (E5 §5 / S35).

        The command-palette entry (:meth:`get_system_commands`) invokes this;
        it forwards to the live backend's :meth:`run_extension_command`. A palette
        entry for a command that declares ``"args"`` first collects the arg string
        via :meth:`_prompt_command_args` (S51) and passes it here; a command without
        an ``args`` placeholder dispatches with the empty string (the palette has no
        argument line). A handler exception is surfaced as an error notice (pi's
        ``_tryExecuteExtensionCommand`` likewise reports rather than crashing the
        screen), never swallowed silently.
        """
        runner = getattr(self.current_backend, "run_extension_command", None)
        if runner is None:
            return
        try:
            result = await runner(name, args)
        except Exception as e:
            self.notify(f"Command /{name} failed: {e}", severity="error")
            self.log.error(f"Extension command /{name} failed: {e}", exc_info=True)
            return
        self._render_command_output(result)

    @work
    async def _prompt_command_args(self, name: str, placeholder: str) -> None:
        """Collect an arg string for an ``args``-declaring palette command (E7 §3 / S51).

        A command that declares ``"args": "<placeholder>"`` expects a free-form
        argument string, exactly as if the user had typed ``/name args``. The palette
        entry has no argument line, so this opens the S47 :class:`ExtensionInputModal`
        to collect it, then dispatches through :meth:`_dispatch_extension_command`
        with the entered text. Runs as a worker because ``push_screen_wait`` requires
        one (the same context the S47 delegate uses).

        Fail-Early: a cancelled modal (Cancel → ``None``) does NOT dispatch — an
        arg-declaring command is not run on a fabricated empty argument the user
        never confirmed. An entered (possibly empty) value dispatches as typed.
        """
        collected = await self.push_screen_wait(
            ExtensionInputModal(f"/{name} {placeholder}".rstrip())
        )
        if collected is None:
            return
        await self._dispatch_extension_command(name, collected)

    def _render_command_output(self, result: ExtensionCommandResult) -> None:
        """Render a command's returned value as a display-only ``system`` box (S46).

        Same chrome as ``/extensions`` (:meth:`action_show_extensions`) — a
        ``system`` ``MessageBox`` mounted into the transcript view. It is
        deliberately NOT added to ``self.messages`` (the working list that becomes
        the model's context), so a command's report cannot leak into model input,
        preserving the E5 §1 tree-as-truth invariant. A command that returned
        nothing (``output_text() is None``) shows no box.
        """
        text = result.output_text()
        if text is None:
            return
        # Command output, and τ did not write it: an extension handler may return
        # a markdown report, but it may equally return a value that
        # ``output_text`` stringified, whose line breaks are all the structure it
        # has. Verbatim keeps those; the cost on a markdown report is blank lines,
        # which is the cheaper of the two mistakes.
        self.query_one(ChatDisplay).add_message("system", text, source="verbatim")

    @staticmethod
    def _format_extensions_listing(
        result: LoadExtensionsResult, disabled: set[str] | None = None
    ) -> str:
        """Render a ``LoadExtensionsResult`` as the ``/extensions`` listing text (S34).

        Pure (no widget access) so it is unit-testable: given the load result it
        returns the exact markdown the listing box shows — a section per loaded
        extension (name, path, tools/commands/hooks) plus a load-errors section.
        ``disabled`` is the set of runtime-disabled extension paths (E10 §6 / S70); a
        disabled extension is tagged in its heading so the listing reflects live state.
        """
        disabled = disabled or set()
        infos = summarize_extensions(result)
        if not infos and not result.errors:
            return "No extensions loaded."

        lines: list[str] = ["# Extensions"]
        for info in infos:
            lines.append("")
            status = " _(disabled)_" if info.path in disabled else ""
            lines.append(f"**{info.name}**{status} — `{info.path}`")
            lines.append(f"- hooks: {', '.join(info.hooks) if info.hooks else '(none)'}")
            lines.append(f"- tools: {', '.join(info.tools) if info.tools else '(none)'}")
            lines.append(f"- commands: {', '.join(info.commands) if info.commands else '(none)'}")
            shortcuts_disp = ", ".join(f"ctrl+e {k}" for k in info.shortcuts) or "(none)"
            lines.append(f"- shortcuts: {shortcuts_disp}")

        if result.errors:
            lines.append("")
            lines.append("## Load errors")
            for err in result.errors:
                lines.append(f"- `{err.path}`: {err.error}")

        return "\n".join(lines)

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
        yield SystemCommand(
            "Resume session…",
            "Pick a saved session from this directory (Tab: every directory)",
            self.action_resume_session,
        )

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

        # The rollback affordance's second discovery path (the first is the Footer
        # label, which appears while a turn runs). Listed unconditionally: the
        # palette is built once per invocation and an entry that vanishes is harder
        # to find than one that explains why it did nothing — the action says
        # "nothing is generating" when there is no turn to roll back.
        yield SystemCommand(
            "Roll back the in-flight turn…",
            "Abort the running turn, drop it off the active path, and run another "
            "prompt from where it started",
            self.action_rollback_turn,
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

        yield SystemCommand(
            "Extensions",
            "List loaded extensions (name/path/tools/commands/hooks) and load errors",
            self.action_show_extensions,
        )

        # Extension-registered slash commands (E5 §5 / S35): list each so it is BOTH
        # visible here and runnable (dispatch mirrors this in on_input_submitted).
        # Read from the live backend's session registry; getattr-guarded so a
        # non-TauBackend test double is a no-op, matching set_ui_delegate/load_extensions.
        get_commands = getattr(self.current_backend, "get_extension_commands", None)
        get_args = getattr(self.current_backend, "get_extension_command_args", None)
        if get_commands is not None:
            for cmd_name, cmd_desc in get_commands():
                # S51: a command declaring ``"args"`` collects that string via the
                # S47 input modal (worker-context) before dispatch; one without args
                # dispatches directly (the palette has no argument line).
                help_text = cmd_desc or f"Run extension command /{cmd_name}"
                placeholder = get_args(cmd_name) if get_args is not None else None
                if placeholder:
                    yield SystemCommand(
                        f"/{cmd_name}",
                        help_text,
                        lambda n=cmd_name, p=placeholder: self._prompt_command_args(n, p),
                    )
                else:
                    yield SystemCommand(
                        f"/{cmd_name}",
                        help_text,
                        lambda n=cmd_name: self._dispatch_extension_command(n),
                    )

        # Extension-registered key shortcuts (E10 §6 / S69): list each chord so it is
        # palette-DISCOVERABLE (the guard's second discovery path alongside the ctrl+e
        # menu) and runnable — the palette entry dispatches the shortcut's command
        # through the SAME path as the chord and a typed /command.
        get_shortcuts = getattr(self.current_backend, "get_extension_shortcuts", None)
        if get_shortcuts is not None:
            for key, command, args, desc in get_shortcuts():
                help_text = desc or f"Run extension command /{command}"
                yield SystemCommand(
                    f"ctrl+e {key}  →  /{command}",
                    help_text,
                    lambda c=command, a=args: self._dispatch_extension_command(c, a),
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
        # AgentSessionRuntime (H1, phase 3): the SAME backend/AgentSession as
        # before — reuse the runtime already bound to it (constructed by
        # whichever of action_new_chat/on_chat_selected last set
        # current_backend) rather than building a new one. This is the one
        # call site where H3's reset set is NOT a no-op: unlike a freshly
        # constructed AgentSession, this one may carry usage/queued-message/
        # deferred-op state from the conversation being cleared, and
        # new_session() is what actually clears it — action_clear_chat had no
        # such cleanup before this phase.
        if self._session_runtime is not None:
            result = await self._session_runtime.new_session(
                persist=True, system_prompt=system_prompt
            )
            if result.get("blocked"):
                # Finding 1 (phase-3 review): the in-flight turn did not stop
                # within the runtime's bounded wait — nothing was touched.
                self.notify(result["reason"], severity="warning")
                return
            if result["cancelled"]:
                self.notify("Clear chat cancelled by an extension", severity="warning")
                return
            self.current_session = result["session"]
        else:
            self.current_session = self.session_catalog.create(
                os.getcwd(),
                self.current_session.model,
                self.current_session.backend,
                system_prompt=system_prompt,
            )
            self._bind_backend_session()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(ChatDisplay)
        await display.clear_messages()
        # Starts a thread worker and returns immediately (see
        # ChatSidebar.refresh_chats). The notify below doesn't depend on it,
        # and the new session is already current regardless of when the
        # sidebar list itself catches up.
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

    @work(group="session-picker")
    async def action_resume_session(self) -> None:
        """Open the session picker and load whatever it returns (§6).

        A worker so ``push_screen_wait`` is legal here, exactly as
        :meth:`action_browse_tree` is one — that is the ``push_screen_wait``
        branch of the either/or §6 states, chosen once rather than tried
        alongside a ``push_screen(callback)`` fallback.

        The chosen ``ref`` is posted as a :class:`ChatSelected`, which is the
        message the sidebar already posts, so the picker adds an entry point and
        not a second loader: model lookup, backend construction, the runtime's
        switch/veto and the transcript reload all stay in
        :meth:`on_chat_selected`. ``os.getcwd()`` rather than ``self._cwd`` for
        the scope, matching ``ChatSidebar._refresh_chats_worker`` — ``_cwd`` is
        the string the empty pane prints, not the directory sessions are keyed on.
        """
        ref = await self.push_screen_wait(SessionPickerModal(self.session_catalog, os.getcwd()))
        if ref is None:
            return
        self.post_message(ChatSelected(ref))

    @work
    async def action_browse_tree(self) -> None:
        """Open the tree-browser and act on the chosen node (§3).

        Runs as a worker so it can ``push_screen_wait`` the modal steps (browse →
        mode → optional custom instructions, or → a second browse for ``elide``).
        Operates on the LIVE ``current_session`` — the TUI owns persistence (§2.6) —
        building a ``ConversationTree`` over its entries and handing the picked node
        to ``backend.navigate_tree``, which appends the ``navigate``/``branch_summary``
        entry and returns the post-navigate context. Re-renders through the same
        path ``action_compact`` uses (§3.4): swap ``self.messages`` + reload.

        The ``elide`` mode (W3) branches off to :meth:`_elide_span_flow` after the
        mode pick, because it needs a second node id rather than a summarizer.
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

        tree = ConversationTree(session.entries(), session.cursor)
        roots = tree.tree()
        if not roots:
            self.notify("Conversation tree is empty", severity="warning")
            return

        target_id = await self.push_screen_wait(SessionTreeModal(roots, resolve_entry=tree.entry))
        if target_id is None:
            return

        mode = await self.push_screen_wait(TreeModeModal())
        if mode is None:
            return

        if mode == "elide":
            # The picked node is the elide's ANCHOR, not a branch point — and an
            # anchor that is already the cursor is the NORMAL case (fold the history
            # behind the current tip and keep going), so the "already at that node"
            # guard below deliberately does not apply to it.
            await self._elide_span_flow(session, target_id, roots, tree.entry)
            return

        if target_id == session.cursor:
            # Checked AFTER the mode pick, not before: it is a statement about
            # *branching* (the three modes that move the cursor back), and hoisting
            # it above the chooser would make it reject the elide flow too.
            self.notify("Already at that node")
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

    async def _elide_span_flow(
        self,
        session: ConversationSession,
        anchor_id: str,
        roots: list[TreeNode],
        resolve_entry: Callable[[str], dict[str, Any]],
    ) -> None:
        """Second half of the ``elide`` mode: pick the resume point, then fold (W3).

        Called from :meth:`action_browse_tree`'s worker, so ``push_screen_wait`` is
        legal here. Split out rather than inlined because it asks a *second* node
        question, which no other mode does: ``anchor_id`` is where the fold jumps
        FROM (the elide is appended under it, and the kept region ends there) and the
        node picked here is where it jumps TO — ``firstKeptId``, the first entry the
        fold keeps.

        The browser is the SAME :class:`SessionTreeModal` with a different caption:
        the second question is asked of the same tree, and a purpose-built second
        widget would be the same list rendered twice. It shows the whole tree rather
        than pre-filtering to the anchor's ancestors, because the tree is how a user
        recognizes the node they mean — and an unreachable pick is caught by
        ``elide_span``'s validation and reported, which is strictly more informative
        than a node that mysteriously cannot be selected.

        Every failure is surfaced through ``notify(severity="error")`` — the path the
        other modes' failures already take — and nothing is appended when validation
        fails: ``elide_span`` checks before it writes, so a rejected elide leaves the
        session byte-identical rather than half-applied.
        """
        elide_span = getattr(self.current_backend, "elide_span", None)
        if elide_span is None:
            self.notify("This backend does not support eliding", severity="warning")
            return

        first_kept_id = await self.push_screen_wait(
            SessionTreeModal(
                roots,
                resolve_entry=resolve_entry,
                title="Elide: pick the resume point",
                help_text="Enter: resume here    Tab: detail pane    Esc: cancel",
            )
        )
        if first_kept_id is None:
            return

        before = len(self.messages)
        self.sub_title = "Eliding span…"
        try:
            new_messages = elide_span(session, anchor_id, first_kept_id)
        except Exception as e:
            self.notify(f"Elide failed: {e}", severity="error")
            self.log.error(f"Elide failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        # Same re-render seam as action_compact / action_browse_tree (§3.4): swap the
        # working list, reload the display, refresh the rollup.
        self.messages = new_messages
        await self.query_one(ChatDisplay).reload_messages(self.messages)
        self._refresh_subtitle()
        self.notify(f"Elided {before} → {len(new_messages)} messages")

    def _extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        """The live backend's registered key shortcuts (E10 §6 / S69).

        Returns ``(key, command, args, description)`` per shortcut, or ``[]`` when the
        backend is a non-``TauBackend`` test double / not yet built (getattr-guarded,
        matching the other extension-surface reads like :meth:`get_system_commands`).
        """
        getter = getattr(self.current_backend, "get_extension_shortcuts", None)
        if getter is None:
            return []
        result: list[tuple[str, str, str, str]] = getter()
        return result

    async def action_extension_chord(self) -> None:
        """The ``ctrl+e`` extension chord leader (E10 §6 / S69).

        When one or more extensions registered a shortcut, ``ctrl+e`` opens the
        :class:`ExtensionChordScreen` which-key menu; the picked key's command is
        dispatched through :meth:`_dispatch_extension_command` (the SAME path the
        palette and typed ``/name`` use), so a keyboard shortcut is a pure accelerator
        over an already-runnable command — nothing model-visible, no new headless
        surface (the command it fires stays reachable by name and via ``ctrl+p``).

        When NO extension registered a shortcut, ``ctrl+e`` keeps its legacy meaning
        and edits the system prompt — zero regression for the common case where the
        chord namespace is empty.

        Kept non-priority (matching the binding it replaced), so while the message
        input is focused ``ctrl+e`` still moves to line-end (the ``TextArea`` default);
        the chord fires when focus is off the input, and the palette (S69 listing) is
        the always-reachable dispatch path. The menu is pushed with a result CALLBACK
        (not ``push_screen_wait``, which requires a worker the binding action is not),
        and the async command dispatch is scheduled from that callback via
        ``run_worker``.
        """
        shortcuts = self._extension_shortcuts()
        if not shortcuts:
            await self.action_edit_system_prompt()
            return

        def _dispatch_chosen(chosen: Optional[tuple[str, str]]) -> None:
            if chosen is None:
                return
            command, args = chosen
            self.run_worker(self._dispatch_extension_command(command, args))

        await self.push_screen(ExtensionChordScreen(shortcuts), _dispatch_chosen)

    async def action_edit_system_prompt(self):
        """Edit the system prompt."""
        current_prompt = self.config.get("system_prompt", "You are a helpful assistant.")

        def handle_result(new_prompt: str | None):
            if new_prompt is not None:
                # Read-modify-write the ON-DISK config, not ``self.config``: the
                # latter has CLI overrides merged into it (_apply_cli_overrides), so
                # writing it back would persist a one-run --model/--system-prompt
                # flag as the permanent default.
                update_config("system_prompt", new_prompt)
                self.config["system_prompt"] = new_prompt
                self.notify("System prompt updated")

        await self.push_screen(SystemPromptEditor(current_prompt), handle_result)

    async def on_chat_selected(self, message: ChatSelected):
        """Load a session the sidebar, the picker, or ``/resume <ref>`` named.

        The one loader, three posters (§6/§7). ``chat_ref`` is resolved with
        ``resolve_ref`` rather than ``load``: the sidebar and the picker post a
        catalog ref, but ``/resume`` posts whatever the human typed, and
        ``resolve_ref`` is the grammar ``--session REF`` already uses — a
        directly-addressable ref, an exact session id, or a unique id prefix. It
        is also what ``AgentSessionRuntime.switch_session`` uses three lines
        below, so the two resolutions in this method can no longer disagree
        about what a ref is.
        """
        try:
            # Resolve the selected session — needed to learn its model, which
            # decides what backend to build BEFORE a runtime can switch onto it.
            session = self.session_catalog.resolve_ref(message.chat_ref, cwd=os.getcwd())

            # Get model config and create backend
            model_config = self.config["models"].get(session.model)
            if not model_config:
                self.notify(f"Model {session.model} not found in config", severity="error")
                return

            self.current_backend = create_backend(self._apply_run_config(model_config))
            # AgentSessionRuntime (H1, phase 3): switch_session() re-resolves
            # message.chat_ref through the catalog — one extra load beyond the
            # one above, needed regardless since the target's model has to be
            # known before a backend for it can even be built — and performs
            # the H2 veto / H3 reset (a no-op on this brand-new AgentSession)
            # before binding.
            self._session_runtime = self._build_session_runtime(
                self.current_backend, session.model, model_config["backend"]
            )
            if self._session_runtime is not None:
                result = await self._session_runtime.switch_session(message.chat_ref)
                if result.get("blocked"):
                    # Finding 1 (phase-3 review): the in-flight turn did not
                    # stop within the runtime's bounded wait — nothing was
                    # touched.
                    self.notify(result["reason"], severity="warning")
                    return
                if result["cancelled"]:
                    self.notify("Switch cancelled by an extension", severity="warning")
                    return
                session = result["session"]
                self.current_session = session
            else:
                self.current_session = session
                self._bind_backend_session()
            await self._load_backend_extensions()
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
