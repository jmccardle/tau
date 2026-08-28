"""
Parley - A minimalist, performant chat interface for LLMs.

Clean, simple, fast. Built with Textual.
"""

from rich import box
from rich.console import RenderableType
from rich.style import Style
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

# Aliased: ``TreeNode`` is already the name of ``ConversationTree``'s DATA node in
# this module (imported below), and the browser deliberately keeps the two apart —
# one is a log entry's place in the conversation, the other is a row's place in the
# widget, and §2 made those two shapes differ.
from textual.widgets.tree import TreeNode as WidgetTreeNode
from textual.binding import Binding
from textual.reactive import reactive
from textual import events, work
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.worker import get_current_worker
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional
from uuid import uuid4

from tau_coding_agent.backends import (
    DEFAULT_LANE,
    Backend,
    RenderRouter,
    create_backend,
    make_model_resolver,
    prompt_tokens,
    resolve_tool_names,
)
from tau_coding_agent.tagline import pick_tagline
from tau_coding_agent.headless import resolve_extensions_config

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
from tau_coding_agent.themes import (
    DEFAULT_THEME_NAME,
    THEME_CONFIG_KEY,
    ThemeError,
    build_theme_registry,
    install_themes,
    resolve_theme,
)

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
from tau_agent_core.sdk import BASE_SYSTEM_PROMPT, LoadExtensionsResult, summarize_extensions

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


#: The narrowest column :func:`_elide` can still say something in: one character of
#: the text, plus the ``…`` that says the rest was cut.
_ELIDE_MIN_WIDTH = 2

#: What a row becomes when its column is narrower than :data:`_ELIDE_MIN_WIDTH`.
#: One cell — the marker alone, with nothing left to mark.
_ELIDE_TOO_NARROW = "…"


def _elide(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, ending in ``…`` when anything was cut.

    Below :data:`_ELIDE_MIN_WIDTH` the label is *replaced* by
    :data:`_ELIDE_TOO_NARROW` rather than returned whole. The previous behaviour
    returned the text unchanged on the theory that a label elided to nothing tells
    the reader less than one that overflows. It tells them less either way, and the
    overflow is not free: ``textual.widgets.Tree`` renders one unwrapped line per
    node and sizes ``virtual_size`` to the widest of them, so one un-elided row grows
    a horizontal scrollbar across the whole browser — the exact defect this function
    exists to prevent, manufactured by the function itself
    (TREE-BROWSER-AS-EDITOR.md §1.1, "a Fail-Early inversion in its own right").

    A row that cannot be shortened is a bug worth showing. ``…`` in a column with no
    room for anything else shows it, and costs one cell instead of the row's whole
    length (TREE-BROWSER-AS-EDITOR.md §2).
    """
    if width < _ELIDE_MIN_WIDTH:
        return _ELIDE_TOO_NARROW
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


#: What a :class:`SessionTreeModal` can answer with (TREE-BROWSER-AS-EDITOR.md
#: §5.3 / §11.1). A ``Literal`` rather than the document's loose ``str``: the
#: browser is about to grow gestures that each mean something different to the
#: caller, and an action name that is only ever compared against string literals
#: turns a typo into a branch that silently never runs. Listed here are the
#: actions the modal ACTUALLY emits — two. §6's ``commit`` and §7's copy/paste
#: actions join them when the code that emits them lands; naming them early would
#: be a vocabulary no producer backs.
#:
#: ``navigate`` continues from BELOW the named node. ``revise`` says the reader
#: named a USER message, which means the opposite side of it: fork from that
#: message's parent and hand its text back for editing (PLAN-0.9.4 §4, item 2).
#: Both carry exactly one id — the node that was pointed at, in both cases.
#: ``elide`` is the only one that carries TWO ids, and it is the reason
#: :attr:`TreeIntent.sole_id` raises rather than returning ``ids[0]``: an elide
#: names ``(anchor, first_kept)`` and a caller that read one of them would fold a
#: span it did not choose. It replaces the ``"elide"`` entry of
#: :class:`TreeModeModal`, which asked for the second id by re-opening this same
#: browser — a second full-screen modal for a question the first one could answer
#: with a key (PLAN-0.9.4 §4, the elide feedback).
TreeAction = Literal["navigate", "revise", "elide"]


@dataclass(frozen=True)
class TreeIntent:
    """What the browser was asked to do, and to which nodes (§5.3, §11.1).

    Replaces the bare ``Optional[str]`` the modal used to dismiss with. Every
    operation §1.3 lists needs more than a node id — a subtree summary is one node
    plus an action, a traversal summary is a *set* of nodes — so the return type is
    widened once, here, rather than rewritten per operation.

    ``TreeIntent("navigate", (id,))`` is the degenerate case §5.3 names: exactly
    what ``dismiss(id)`` used to mean, said in the wider vocabulary.

    Frozen, and ``ids`` is a tuple rather than a list, because the intent crosses a
    screen boundary: the modal is gone by the time the caller reads it, and a
    mutable answer would let the caller edit a record of what it was told.
    """

    action: TreeAction
    ids: tuple[str, ...]

    @property
    def sole_id(self) -> str:
        """The one id this intent applies to.

        Fail-Early for the callers that can only act on a single node (both of
        today's): an intent carrying zero or several ids means the modal answered a
        question the caller did not ask, and reading ``ids[0]`` would act on an
        arbitrary one of them instead of saying so.
        """
        if len(self.ids) != 1:
            raise ValueError(f"{self.action!r} intent names {len(self.ids)} ids, expected 1")
        return self.ids[0]


@dataclass(frozen=True)
class ElidePlan:
    """A legal ``elide_span`` call, worked out from the marked node and the cursor.

    **The two ends bracket what is KEPT, not what is removed.** This is the thing
    about an elide that a reader guesses backwards, and it is worth stating in
    the type rather than only in the manual: over ``[1,2,3,4,5,6]``, pairing 2
    with 4 leaves ``[2,3,4]``, not ``[1,5,6]``. An elide is the summary-less form
    of the compaction anchor, and a compaction keeps a tail and drops the head —
    ``ConversationTree._active_path_entries`` emits the anchor and then its
    ancestors from ``firstKeptId`` onward, so the kept region is always ONE
    contiguous run ending at the anchor. Cutting a span out of the middle is not
    a shape this operation can express at all.

    ``anchor`` is where the fold jumps FROM — the elide entry is appended under it
    and the conversation continues there. ``first_kept`` is where it jumps TO: the
    oldest entry the fold keeps.

    **Which of the two nodes is which is decided by the tree, not by the gesture
    order.** The two ends of an elide are an ancestor and a descendant of each
    other; the deeper one is always the anchor, because the shallower one is by
    construction on its path and the reverse is impossible. So the reader marks
    one node and puts the cursor on the other and does not have to remember which
    they picked first — which is what was asked for.

    Two counts, because they answer two different questions and the first one
    alone under-reports:

    * ``folded`` — entries the fold itself drops, measured at the ANCHOR. This is
      the number ``TauBackend.elide_span`` computes, and an elide that folds 0 is
      what it refuses, so this is what gates the offer.
    * ``dropped`` — entries that leave the context the model can see RIGHT NOW.
      Never smaller than ``folded``, and larger whenever the anchor is not the
      current tip: moving the cursor back to it abandons everything newer. Over
      ``[1..6]`` with the cursor at 6, pairing 2 with 4 folds ``[1]`` and drops
      ``[1,5,6]``. This is what the reader loses, so it is what the offer says.

    ``moves_cursor`` is that same difference stated as a fact rather than a
    number: the conversation will continue somewhere other than where it is now.
    """

    anchor: str
    first_kept: str
    folded: int
    dropped: int
    moves_cursor: bool


@dataclass(frozen=True)
class TreeRow:
    """One row the tree browser will draw, and where it sits (PLAN-0.9.4 §4).

    The output of :func:`plan_tree_rows`. ``parent`` is an INDEX into the row list
    rather than a node id, because two rows can name the same id only if the
    planner is broken, and an index makes the widget build a single pass with no
    lookup table. ``None`` means the widget root.

    ``depth`` is the WIDGET depth — what ``_relabel`` spends ``guide_depth`` cells
    on per level — and is not the ``parentId`` depth. See :meth:`_index` for the
    data depth, which is a different number and stays a property of the log.
    """

    node: TreeNode
    parent: Optional[int]
    depth: int
    expanded: bool
    #: Whether any other row hangs off this one — i.e. whether this row is a turn
    #: group or a fork. ``Tree.render_label`` draws its expand toggle off
    #: ``allow_expand`` ALONE and never asks whether there are children (textual
    #: 8.2.7, _tree.py), so without this every assistant and tool row wears an
    #: arrow that clicks and toggles and reveals nothing. It is a property of the
    #: plan, not of the entry: a node whose only child is a hidden ``navigate``
    #: has children in the log and none on screen.
    has_children: bool


def _row_is_hidden(node: TreeNode) -> bool:
    """Whether this entry gets no row at all (PLAN-0.9.4 §4, item 4).

    A ``navigate`` entry records that the cursor moved. It carries no message, it
    is not a branch target worth naming, and it sits between an assistant message
    and the user message that forked off it — which is the one place an extra row
    does the most damage to the shape the reader is trying to read. Its children
    attach to its nearest drawn ancestor, which reads as what actually happened:
    the new turn hangs off the node it was forked from.

    Two exceptions, and neither is tidiness:

    * **The cursor is never hidden.** A browser that will not say where you are
      has failed at the one thing it must do.
    * **A ``navigate`` with more than one child is a real fork point.** Hiding it
      would draw two branches as one run — a shape the log does not have.

    Only ``navigate``. ``model_change`` and ``agent_spec`` carry no message
    either, but each records a real change to what the model is and what it was
    told, which is worth seeing while browsing history.
    """
    return node.kind == "navigate" and not node.is_leaf and len(node.children) <= 1


def _drawn_children(node: TreeNode) -> list[TreeNode]:
    """``node``'s children with hidden ones spliced out, in order.

    Recurses only through runs of hidden nodes (a ``navigate`` under a
    ``navigate``), which are at most a handful long.
    """
    drawn: list[TreeNode] = []
    for child in node.children:
        if _row_is_hidden(child):
            drawn.extend(_drawn_children(child))
        else:
            drawn.append(child)
    return drawn


def plan_tree_rows(roots: list[TreeNode]) -> list[TreeRow]:
    """Decide what the browser draws, under what, at what depth (PLAN-0.9.4 §4).

    Pure, and separate from the widget build, because these are the rules the
    owner's feedback was about and they are worth testing without a terminal.

    **Two nesting rules, and they compose.**

    1. **A fork opens a level** — TREE-BROWSER-AS-EDITOR.md §2, unchanged. A run
       of single-child entries is a run of SIBLINGS, so indent depth counts
       branches rather than messages and does not grow as a conversation does.
    2. **A user message opens a level, and the next user message closes it.** A
       user turn is the boundary §2 did not use: everything from a user message
       down to the next one is that turn, so it gets a widget parent and can be
       folded. The next user message is that group's SIBLING, not its child,
       which is what keeps rule 1's bound intact — a hundred linear turns is a
       hundred rows at depth 0, each holding its own tool traffic.

    The walk carries two containers to make rule 2 work. ``current`` is where an
    ordinary row attaches (inside the open turn group). ``outer`` is where the
    NEXT user message attaches, which is the group's own parent — that is the
    whole of "the group closes at the next user message". A fork sets both, since
    a fork's branches are the next turns.

    **Turn groups mount collapsed; everything else mounts open.** The exception is
    the groups the cursor row is actually inside — its WIDGET ancestors, not its
    ``parentId`` ancestors, and the difference is the whole rule. In a linear
    conversation every earlier user message is a ``parentId`` ancestor of the
    cursor but none of them is a widget ancestor, because rule 2 makes them
    siblings; keying off the data chain would leave every turn in the session
    open, which is the state the owner asked to get out of. Off the widget chain,
    exactly one turn opens — the one you are in. Fork rows never collapse: a
    browser that opens without showing where you are has failed at the one thing
    it must do, and folding the branch you are on is a gesture, not a default.

    Iterative rather than recursive: a linear conversation is one frame per entry
    and Python's default limit is 1000, so the recursive build this replaces would
    have raised on a long session. ``ConversationTree.tree`` went iterative for the
    same reason.
    """
    # (node, widget parent index or None, widget depth, opens a turn group)
    built: list[tuple[TreeNode, Optional[int], int, bool]] = []
    # A container is (row index or None for the widget root, depth for its rows).
    top: tuple[Optional[int], int] = (None, 0)
    # A hidden root has no ancestor to splice into, so its children become roots
    # themselves rather than vanishing with it.
    drawn_roots: list[TreeNode] = []
    for root in roots:
        drawn_roots.extend([root] if not _row_is_hidden(root) else _drawn_children(root))
    stack: list[tuple[TreeNode, tuple[Optional[int], int], tuple[Optional[int], int]]] = [
        (node, top, top) for node in reversed(drawn_roots)
    ]
    while stack:
        node, outer, current = stack.pop()
        is_turn = node.kind == "message" and node.role == "user"
        container = outer if is_turn else current
        index = len(built)
        built.append((node, container[0], container[1], is_turn))
        children = _drawn_children(node)
        mine: tuple[Optional[int], int] = (index, container[1] + 1)
        if len(children) > 1:
            child_outer = child_current = mine
        elif is_turn:
            child_outer, child_current = outer, mine
        else:
            child_outer, child_current = outer, current
        for child in reversed(children):
            stack.append((child, child_outer, child_current))

    # The cursor's widget ancestry, walked back up the parent indices recorded
    # above. ``is_leaf`` is ``ConversationTree``'s own cursor mark and
    # :func:`_row_is_hidden` never drops it, so this finds a row whenever the tree
    # has a cursor at all.
    open_groups: set[int] = set()
    walk: Optional[int] = next((i for i, (n, _p, _d, _t) in enumerate(built) if n.is_leaf), None)
    while walk is not None:
        open_groups.add(walk)
        walk = built[walk][1]
    parents = {parent for _n, parent, _d, _t in built if parent is not None}
    return [
        TreeRow(
            node=node,
            parent=parent,
            depth=depth,
            expanded=not is_turn or i in open_groups,
            has_children=i in parents,
        )
        for i, (node, parent, depth, is_turn) in enumerate(built)
    ]


@dataclass(frozen=True)
class TreeZones:
    """The four selection sets the browser renders against (§5.3).

    §5.3's list, with set 3 ("derived — what the cursor node covers or connects")
    split into the three roles §3's class table actually distinguishes:

    1. ``cursor`` — one node; drives :class:`TreeDetailPane`.
    2. ``marked`` — the multi-select set; drives the counts and the lowest common
       ancestor.
    3. derived: ``path`` (the cursor's ancestor chain), ``folded`` (entries on that
       chain that the active splice anchor drops) and ``covered`` (``folded``, when
       the cursor IS that anchor).
    4. ``hidden`` — collapsed or archived. **View state** (§11.2): nothing is
       appended for either, and this is computed from the modal, never read from
       the log.

    ``copied`` is §7's set. It is carried here and declared in
    :attr:`ZoneTree.COMPONENT_CLASSES` so the renderer is complete, and it has no
    producer until copy entries exist — see §10 step 7. No default value: a
    constructor that quietly fills in an empty set is how a real producer gets
    forgotten.

    ``summary``/``abandoned`` are §4.3's pair, and ``hover_common``/
    ``hover_divergent`` are §3's hover divergence (§10 step 5). Both arrived after
    §5.3's list was written and neither is a *selection* set — the first is a fixed
    property of the log's shape, the second is a function of the mouse. They live
    here anyway so there is one zone container, one "nothing painted" value
    (:data:`_NO_ZONES`) and one place a renderer has to look. What differs is how
    they are REFRESHED: see :meth:`ZoneTree.set_hover_zones`.

    Frozen sets, because the renderer holds this across many ``render_label`` calls
    and a set mutated underneath it would paint two rows from two different states.
    """

    cursor: Optional[str]
    marked: frozenset[str]
    path: frozenset[str]
    folded: frozenset[str]
    covered: frozenset[str]
    hidden: frozenset[str]
    copied: frozenset[str]
    #: A ``branch_summary`` row that has an abandoned branch to be the summary OF,
    #: and the head of that branch (TREE-BROWSER-AS-EDITOR.md §4.3). Two sets rather
    #: than one so the two halves of the relation read differently — a single
    #: "this row is part of a summary pair" class cannot say which half it is.
    summary: frozenset[str]
    abandoned: frozenset[str]
    #: Rows that cannot be the other end of the elide the reader has started —
    #: everything not on one root→leaf line with the marked node (PLAN-0.9.4 §4,
    #: the elide feedback). Empty unless exactly ONE node is marked: a mark is
    #: what says "I am choosing a span", and greying half the tree while somebody
    #: is only browsing would answer a question they did not ask.
    ineligible: frozenset[str]
    #: The hovered node's ancestry, split where it leaves the cursor's (§3, step 5).
    #: ``hover_common`` is the shared prefix, ``hover_divergent`` the tail below it.
    #: Both empty when there is no divergence to report — see
    #: :meth:`SessionTreeModal._hover_divergence`.
    hover_common: frozenset[str]
    hover_divergent: frozenset[str]


#: What :class:`ZoneTree` renders with before the modal has computed anything.
#: Every set empty, so the first ``get_label_width`` pass (which Textual runs
#: during ``_build``, before ``on_mount`` can set real zones) paints plain rows.
_NO_ZONES = TreeZones(
    cursor=None,
    marked=frozenset(),
    path=frozenset(),
    folded=frozenset(),
    covered=frozenset(),
    hidden=frozenset(),
    copied=frozenset(),
    summary=frozenset(),
    abandoned=frozenset(),
    ineligible=frozenset(),
    hover_common=frozenset(),
    hover_divergent=frozenset(),
)


#: Which ``tree--kind-*`` component class a row's ``role``/``kind`` tag is painted
#: with. The KEY is exactly what :meth:`SessionTreeModal._label` puts before the
#: colon (``node.role or node.kind``), so the table is read off the rendered label
#: rather than off a second derivation of it.
#:
#: Five classes for eleven tags, because the reader is separating *sides of a
#: conversation*, not enumerating entry kinds: who spoke (user / assistant), what
#: the tools said, what the system said, and what is bookkeeping. A class per kind
#: would put eight colours on one screen and say nothing more.
_TREE_KIND_CLASS: dict[str, str] = {
    # message roles
    "user": "tree--kind-user",
    "assistant": "tree--kind-assistant",
    "toolResult": "tree--kind-tool",
    "system": "tree--kind-system",
    # non-message entry kinds. `navigate` is in the table although the planner
    # drops its row (PLAN-0.9.4 §4): a `navigate` that forks, or that is the
    # cursor, KEEPS its row, and an unpainted row there would be the only tag on
    # screen with no colour.
    "compaction": "tree--kind-structural",
    "branch_summary": "tree--kind-structural",
    "elide": "tree--kind-structural",
    "navigate": "tree--kind-structural",
    "model_change": "tree--kind-structural",
    "agent_spec": "tree--kind-structural",
    "customEntry": "tree--kind-structural",
}


def tree_kind_span(node: TreeNode) -> tuple[str, int] | None:
    """``(component class, tag length)`` for ``node``'s label, or ``None``.

    The tag length counts the tag AND its colon — ``"user:"`` is 5 — which is the
    range :meth:`ZoneTree.render_label` paints. ``None`` for a tag the table does
    not know: an unmapped kind renders in the row's ordinary colour rather than
    borrowing a hue that means something else (Fail-Early — an unknown kind should
    look unknown, not look like a tool result).
    """
    tag = node.role or node.kind
    component = _TREE_KIND_CLASS.get(tag)
    if component is None:
        return None
    return component, len(tag) + 1


class ZoneTree(Tree[str]):
    """A ``Tree`` that paints per-row *zone* styling (TREE-BROWSER-AS-EDITOR.md §3).

    Textual ``Tree`` rows are not DOM nodes and cannot carry per-row CSS classes,
    so §3 uses the two hooks that exist instead: a ``COMPONENT_CLASSES`` frozenset
    resolved through ``get_component_styles`` (textual 8.2.7, ``dom.py:601`` /
    ``widget.py:1175``), and an override of ``render_label`` (``_tree.py:877``),
    which Textual calls once per row.

    The classes name zone **roles**, not branches. A class per branch would mint an
    unbounded vocabulary that no stylesheet can enumerate; branch-distinguishing
    colour, when it is wanted, cycles a small fixed palette modulo N instead (§3) —
    not implemented here, and deliberately not faked with a role class.

    This subclass exists for the styling alone. It adds no state the tree does not
    already have except :attr:`zones`, and every colour lives in ``parley.tcss``.
    """

    #: Declared as a ``set`` and not the ``frozenset`` §3's prose names, because
    #: the base declares ``ClassVar[set[str]]`` (textual 8.2.7, ``dom.py:144``) and
    #: narrowing it in a subclass is a type error. Textual is what produces the
    #: frozenset: ``DOMNode._get_component_classes`` (``dom.py:757``) unions this
    #: with every base's, so the seven inherited ``tree--*`` names stay available
    #: and only the new ones are listed.
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        # On the cursor's ancestor chain. §10 puts this in step 5; it is populated
        # HERE because step 1 removed the guide-hover ancestry highlight (§2, "§3 is
        # the replacement, not an embellishment") and leaving it unpopulated would
        # ship that removal as a regression. What step 5 still owns is the *hover
        # divergence* highlight — where a hovered node's path leaves the cursor's.
        "tree--zone-path",
        # On the path, dropped by a splice anchor: in the chain, not in the context.
        "tree--zone-folded",
        # The same span, when the cursor is the anchor doing the dropping.
        "tree--zone-covered",
        # In the multi-select set.
        "tree--zone-marked",
        # Collapsed or archived (§11.2: view state, never read from the log).
        "tree--zone-hidden",
        # §7's copies. DECLARED WITH NO PRODUCER: copy entries do not exist yet, so
        # `TreeZones.copied` is always empty. The class is here so §7 adds a
        # producer rather than a producer plus a stylesheet plus a renderer branch.
        "tree--zone-copied",
        # §4.3's pair: a `branch_summary` row, and the head of the abandoned branch
        # it looks back on. Two classes for the two ends of ONE relation — the
        # stylesheet gives them a shared hue (that is what says "these two go
        # together") and different weights (that is what says which is which).
        "tree--zone-summary",
        "tree--zone-abandoned",
        # Cannot be the other end of the elide in progress. Greyed rather than
        # made unselectable: the cursor still moves through these rows, because
        # the reader is also using them to work out WHERE the eligible ones are,
        # and a tree the arrow keys skip around in is harder to read than a tree
        # that says which rows are live. `ctrl+E` is what refuses.
        "tree--zone-ineligible",
        # §3's hover divergence (§10 step 5): where the hovered node's ancestry
        # parts company with the cursor's. These are laid over whatever the row
        # already carries rather than instead of it — see `render_label`.
        "tree--zone-hover-common",
        "tree--zone-hover-divergent",
        # The row's TYPE tag — the `user:` / `assistant:` / `toolResult:` prefix
        # `_label` writes. Not a zone: a zone is a set the reader's gestures move,
        # and a row's kind never moves. They live in the same frozenset because
        # `get_component_rich_style` is the only way a `Tree` subclass can resolve
        # a stylesheet rule at all, and `render_label` is the only place either
        # gets applied. See `_TREE_KIND_CLASS` for why there are five and not one
        # per entry kind.
        "tree--kind-user",
        "tree--kind-assistant",
        "tree--kind-tool",
        "tree--kind-system",
        "tree--kind-structural",
    }

    #: Label-portion precedence, first match wins. Ordered by how much the reader
    #: asked for the row: a mark is a deliberate act, so it outranks everything;
    #: ``hidden`` states that a row is excluded, which outranks what it is; the
    #: structural zones follow, most specific first (``covered`` is the anchor's own
    #: span, ``folded`` is any anchor's); ``path`` is last because it is true of a
    #: whole chain and would otherwise swallow the rest.
    #:
    #: §4.3's pair sits between the fold zones and ``path``. Behind the fold zones
    #: because "this row is not in your context" outranks what the row IS; ahead of
    #: ``path`` for the reason ``path`` is last at all — the summary pair names two
    #: specific rows, and a whole-chain zone would swallow it.
    _LABEL_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("marked", "tree--zone-marked"),
        # Ahead of everything but the mark: "you cannot pick this" is the only
        # thing that matters about a row while a span is being chosen, and it has
        # to outrank what the row IS. Behind the mark because the marked node is
        # never ineligible and the reader's own act still wins.
        ("ineligible", "tree--zone-ineligible"),
        ("hidden", "tree--zone-hidden"),
        ("copied", "tree--zone-copied"),
        ("covered", "tree--zone-covered"),
        ("folded", "tree--zone-folded"),
        ("summary", "tree--zone-summary"),
        ("abandoned", "tree--zone-abandoned"),
        ("path", "tree--zone-path"),
    )

    #: The hover divergence (§3, step 5). NOT part of :attr:`_LABEL_ZONES`, because
    #: it does not compete with those classes — it COMPOSES with them. A hovered
    #: chain crosses rows that are already on the path, already marked, already
    #: folded, and the reader wants to keep knowing that while they trace it. So
    #: ``render_label`` stylizes this over the whole row as a second span, and Rich
    #: merges the two attribute-wise: the stylesheet's ``underline`` survives on top
    #: of a marked row's green, and the divergent half's colour is what separates
    #: the two halves of the trace.
    #:
    #: Two entries and first-match-wins, but the two sets are disjoint by
    #: construction (a node is on one side of the divergence or the other), so the
    #: order is a formality rather than a precedence decision.
    _HOVER_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("hover_divergent", "tree--zone-hover-divergent"),
        ("hover_common", "tree--zone-hover-common"),
    )

    #: Gutter-portion precedence — the range BEFORE the label, which for
    #: ``render_label`` is the expand toggle (the indentation rails further left are
    #: drawn by ``Tree._render_line`` from ``tree--guides*`` and are not ours to
    #: style per row). Only the span zones are listed: a fold marking its own
    #: toggle is §3's "a row can carry one style on its gutter portion and another
    #: on its text", and it is the case §4's fold header needs that capability for.
    _GUTTER_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("covered", "tree--zone-covered"),
        ("folded", "tree--zone-folded"),
    )

    class HoverChanged(Message):
        """The ROW under the mouse changed — not the mouse (§3, step 5).

        ``Tree.hover_line`` is a ``var`` (textual 8.2.7, ``_tree.py:655``), so
        ``watch_hover_line`` runs only when the line number actually changes.
        Sliding the mouse along one row therefore costs the assignment in
        ``_on_mouse_move`` and nothing else, and this message is posted at most once
        per row the pointer crosses. That is what makes it affordable to recompute
        anything at all on hover.

        Carries the ENTRY id rather than the widget node, because the divergence is
        a fact about ``parentId`` ancestry and the widget nesting counts forks (§2)
        — the two are deliberately different shapes. ``None`` when the pointer left
        the tree, or landed on a row with no entry behind it.
        """

        def __init__(self, zone_tree: "ZoneTree", entry_id: Optional[str]) -> None:
            super().__init__()
            self.zone_tree = zone_tree
            self.entry_id = entry_id

        @property
        def control(self) -> "ZoneTree":
            return self.zone_tree

    def __init__(self, label: str, *, id: Optional[str] = None) -> None:
        super().__init__(label, id=id)
        self._zones = _NO_ZONES
        self._kinds: dict[str, tuple[str, int]] = {}

    @property
    def zones(self) -> TreeZones:
        """The sets the next repaint will render against."""
        return self._zones

    def set_kinds(self, kinds: dict[str, tuple[str, int]]) -> None:
        """Tell the renderer each row's type tag: ``id -> (class, tag length)``.

        Handed in whole, once, at build time. Unlike every zone this is not state
        a gesture can move — a row's kind is a property of its entry — so there is
        no cache to clear and no repaint to ask for: it is set before the first
        row is drawn. Rows missing from the map render their tag plainly.
        """
        self._kinds = kinds

    def watch_hover_line(self, previous_hover_line: int, hover_line: int) -> None:
        """Tell the screen which row the pointer moved onto (§3, step 5).

        ``super()`` first: the base flips ``_hover`` on the two nodes and refreshes
        their regions, which is what drives ``tree--highlight-line`` and the guide
        hover. This adds the announcement the *divergence* highlight needs, and
        computes nothing itself — the ancestry it would need is the ``parentId``
        chain, which lives in :class:`SessionTreeModal` and not in a widget that
        nests by fork.

        ``_get_node`` is private and is used deliberately: it is the base's own way
        of turning a line number into a node (``_tree.py:1102``, called by the
        watcher this overrides), the mapping is not exposed publicly, and
        re-deriving it from ``_tree_lines`` would touch the same privates one level
        deeper.
        """
        super().watch_hover_line(previous_hover_line, hover_line)
        node = self._get_node(hover_line)
        data = None if node is None else node.data
        self.post_message(self.HoverChanged(self, None if data is None else str(data)))

    def set_zones(self, zones: TreeZones) -> None:
        """Replace the zone sets and repaint the visible rows.

        The line cache has to be dropped by hand. ``Tree._render_line``'s cache key
        (textual 8.2.7, ``_tree.py:1325-1332``) is ``(y, is_hover, width,
        self._updates, pseudo_class_state, per-node _updates)`` — zone state appears
        in none of it, and only the two nodes whose ``_selected`` flips get a new
        per-node ``_updates`` when the cursor moves (``_tree.py:178-181``). Every
        OTHER row on the old and new ancestor chains would keep serving the strip it
        was painted with, which is precisely the set of rows ``tree--zone-path``
        exists to change.

        ``self._line_cache.clear()`` rather than ``Tree._invalidate()``: the latter
        also drops ``_tree_lines_cached`` and asks for a layout pass, which rebuilds
        every row's width. Zone styling changes no row's WIDTH — ``render_label``
        adds spans, never characters — so the geometry is still correct and the cost
        should be the visible rows, not the whole tree, on every arrow key.
        """
        self._zones = zones
        self._line_cache.clear()
        self.refresh()

    def set_hover_zones(self, common: frozenset[str], divergent: frozenset[str]) -> None:
        """Replace ONLY the hover divergence, leaving the selection sets alone (step 5).

        The separate write path is the whole performance story of this step.
        :meth:`SessionTreeModal._refresh_zones` walks the conversation twice
        (``ConversationTree.path`` and ``context_entries``) and every widget row
        (``_hidden``); the hover divergence is two ``parentId`` walks and a common
        prefix, bounded by the tree's DEPTH. Rebuilding the whole
        :class:`TreeZones` on hover would put the first cost on every row the
        pointer crosses, so ``replace`` swaps the two hover fields and nothing else.

        **The no-op guard is not an optimisation detail.** The commonest hover is
        along the cursor's own path, where there IS no divergence and both sets stay
        empty; without this the reader would pay a full repaint per row for a frame
        that is identical to the last one. Returning early also keeps ``refresh``
        out of the ``_on_leave`` → already-empty case.
        """
        if common == self._zones.hover_common and divergent == self._zones.hover_divergent:
            return
        self._zones = replace(self._zones, hover_common=common, hover_divergent=divergent)
        # Same reasoning as `set_zones`: the strip cache key holds no zone state, and
        # the base's `watch_hover_line` refreshes only the two rows whose `_hover`
        # flipped — while a divergence highlight changes a whole chain of them.
        self._line_cache.clear()
        self.refresh()

    def render_label(self, node: WidgetTreeNode[str], base_style: Style, style: Style) -> Text:
        """Paint one row's zone styling over Textual's own label (§3).

        Composes with the base rather than replacing it: ``super()`` assembles the
        expand toggle and the label with Textual's styles, and this adds spans over
        character RANGES of the result — the gutter portion gets the span zones, the
        text portion gets the selection zones. Rich combines span styles
        attribute-wise, so a zone that sets only ``color`` leaves the row's
        background and weight alone.

        A fourth range, ahead of those three: the row's TYPE TAG — the ``user:`` /
        ``toolResult:`` prefix — is painted from :meth:`set_kinds`. It is what the
        reader scans a long tree with, and it is not a zone (see the comment on
        the ``tree--kind-*`` entries in :attr:`COMPONENT_CLASSES`).

        **The cursor row is left alone.** Its style is resolved with ``partial=False``
        (``_tree.py:1424-1427``) and, when the tree has focus, sets a foreground
        against the cursor's own background; a zone colour layered on top wins the
        foreground and loses the contrast that made the row readable. The cursor is
        already the strongest state on the screen and needs no second marking. The
        cost is that marking the row under the cursor shows no change on that row —
        which is why :meth:`SessionTreeModal._marks_summary` reports the count. The
        hover divergence is skipped there for the same reason, and can only ever
        want the cursor row for its COMMON half anyway: the divergent tail is by
        construction the part of the hovered chain the cursor's does not contain.
        """
        text = super().render_label(node, base_style, style)
        entry_id = node.data
        if entry_id is None or entry_id == self._zones.cursor:
            return text
        # ``super()`` returns ``prefix + label``; the prefix is the 2-cell expand
        # toggle or nothing (``_tree.py:893-901``), so the split is the difference
        # in length. Character offsets, which is what ``Text.stylize`` takes.
        label = node.label
        label_len = len(label.plain if isinstance(label, Text) else label)
        split = len(text.plain) - label_len
        # The type tag first, so a zone the reader put there paints OVER it. The
        # tag says what the row is, which is true of every row; a zone says what
        # the reader has done to it, which is true of few — and when both apply,
        # the answer to "did my mark land?" is the one that has to win.
        #
        # The end offset is clamped: `_relabel` elides from the tail, so the tag
        # survives at any width a row can still say something at, but a tree
        # narrow enough to elide INTO the tag would otherwise paint past the end
        # of the line.
        kind = self._kinds.get(entry_id)
        if kind is not None:
            component, tag_len = kind
            tag_end = min(split + tag_len, len(text.plain))
            if tag_end > split:
                text.stylize(self.get_component_rich_style(component, partial=True), split, tag_end)
        gutter_class = self._first_zone(self._GUTTER_ZONES, entry_id)
        if gutter_class is not None and split > 0:
            text.stylize(self.get_component_rich_style(gutter_class, partial=True), 0, split)
        label_class = self._first_zone(self._LABEL_ZONES, entry_id)
        if label_class is not None:
            text.stylize(
                self.get_component_rich_style(label_class, partial=True),
                split,
                len(text.plain),
            )
        # The hover divergence goes on LAST and over the WHOLE row, including the
        # toggle: it is a trace the reader is drawing with the pointer, so it should
        # read as one continuous thing down the rows it covers rather than stopping
        # at each row's gutter. Layered rather than substituted (see
        # :attr:`_HOVER_ZONES`) — the row keeps saying what it is while it says it
        # is on the traced chain.
        hover_class = self._first_zone(self._HOVER_ZONES, entry_id)
        if hover_class is not None:
            text.stylize(
                self.get_component_rich_style(hover_class, partial=True),
                0,
                len(text.plain),
            )
        return text

    def _first_zone(self, order: tuple[tuple[str, str], ...], entry_id: str) -> Optional[str]:
        """The first component class in ``order`` whose set holds ``entry_id``."""
        for field_name, component_class in order:
            members: frozenset[str] = getattr(self._zones, field_name)
            if entry_id in members:
                return component_class
        return None


class SessionTreeModal(ModalScreen[Optional[TreeIntent]]):
    """Browse the conversation tree and pick a node to branch from (§3.2).

    Port of pi's ``showTreeSelector`` (interactive-mode.ts:4446): a
    ``textual.widgets.Tree`` populated from ``ConversationTree.tree()``, the current
    leaf highlighted. ``Enter`` dismisses with a :class:`TreeIntent`; ``Esc`` cancels
    (``None``). Copies the ``SystemPromptEditor`` modal template.

    Selecting and committing are two gestures, not one (TREE-BROWSER-AS-EDITOR.md
    §5.1). A click moves the cursor and leaves the browser open; only ``Enter``
    dismisses. ``left`` collapses a fork, or moves to the enclosing one (§5.2).

    The widget nesting handed to ``Tree`` counts **forks, not messages** (§2): see
    :meth:`on_mount`.

    **It takes the whole ``ConversationTree``, not ``roots`` plus a resolver**
    (§5.3). Three of the four selection sets are derived rather than handed in —
    ``path`` is ``ConversationTree.path``, ``folded``/``covered`` are the difference
    between that and ``context_entries``, and the lowest common ancestor of the
    marked set comes off the ``_parent_of`` map built at :meth:`_index`. A resolver
    alone cannot answer any of them. Collapsing ``roots`` and ``resolve_entry`` into
    the one object also removes the way they could disagree: the rows and the bodies
    are now provably the same log, where before a caller could pass a ``roots`` graph
    built from one tree and a resolver closed over another.

    This does not weaken the standing contract that a body must be showable — it
    strengthens it. ``resolve_entry`` was required because "a browser that cannot
    show a body is the elided-preview draft this replaced". A ``ConversationTree``
    cannot be passed without one: :meth:`~ConversationTree.entry` answers for every
    id :meth:`~ConversationTree.tree` produced, by construction.

    **It holds no ``SessionLog`` and performs no durable operation** (§11.1). Every
    gesture accumulates in-memory state and the commit returns one intent for the
    caller to apply. The rejected alternative — injecting a live editor the modal
    calls — is on the record in §11.1; the visible consequence of not taking it is
    that this class is constructible from a ``ConversationTree`` alone, which is what
    every test across four files does.

    ``title``/``help_text`` exist for the SECOND pick of the elide flow (W3), which
    asks a different question of the same browser — "where does the fold resume?"
    rather than "where do we branch from?". One reused browser with a different
    caption, not a second widget: the tree, the leaf highlight and the key handling
    are identical, and only the sentence above them is not.

    Rows sit beside a :class:`TreeDetailPane` showing the highlighted node in full
    — the rows say *which* node, the pane says *what it is*.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        # ``priority`` is what splits the click from the commit
        # (TREE-BROWSER-AS-EDITOR.md §5.1). ``App._check_bindings(key,
        # priority=True)`` runs the whole binding chain from the App DOWN before the
        # key is forwarded to the focused widget (textual 8.2.7, app.py:3966/4136),
        # so this fires and ``Tree``'s own ``enter`` → ``action_select_cursor``
        # (_tree.py:544) never does. ``Tree.NodeSelected`` therefore reaches
        # :meth:`on_tree_node_selected` only from a click, which is what lets that
        # handler select rather than dismiss. The rejected alternative — overriding
        # ``Tree._on_click`` to suppress ``select_cursor`` — couples the modal to a
        # private method across Textual versions.
        Binding("enter", "commit", "Choose", priority=True, show=False),
        Binding("left", "collapse", "Collapse", show=False),
        # `left` collapses, so something has to re-open. `right` is unbound in
        # `Tree.BINDINGS` exactly as `left` was (textual 8.2.7, _tree.py:524-551),
        # and it is the other half of the file-tree idiom §5.2 borrowed.
        Binding("right", "expand", "Expand", show=False),
        # `space` for the multi-select mark is MY choice, not the document's — §5.3
        # names the marked set and binds no key to it. Priority, and therefore
        # taking the key from `Tree`'s own space=toggle-expand (_tree.py:556): the
        # expand gesture moved to `left`/`right` above, which is where a reader of a
        # fork-nested tree reaches for it, and marking is the gesture with no other
        # home. Same trade `enter` already made for the commit.
        Binding("space", "toggle_mark", "Mark", priority=True, show=False),
        # Fold the detail pane away to see more tree (§4a).
        #
        # `ctrl+d`, and NOT the `ctrl+m` that was asked for: a terminal sends one
        # byte (0x0D) for both Enter and Ctrl+M, and textual says so —
        # ``KEY_ALIASES`` maps ``enter`` to ``["ctrl+m"]`` (textual 8.2.7,
        # keys.py), so a ``ctrl+m`` binding on this screen would fire on every
        # Enter, which is the commit key three lines above. There is no way to
        # tell the two apart, so the gesture takes the next free key rather than
        # a key that works some of the time.
        Binding("ctrl+d", "toggle_detail", "Detail pane", priority=True, show=False),
        # Fold a span out of the context, from the browser rather than through the
        # mode chooser and a second copy of this screen (PLAN-0.9.4 §4). Priority
        # for the same reason the others are, and because the App binds `ctrl+e`
        # to the extension chord — that binding is not priority, so a screen-level
        # one would win anyway, but relying on which of two non-priority bindings
        # textual reaches first is how a key silently changes meaning.
        Binding("ctrl+e", "elide", "Elide", priority=True, show=False),
    ]

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
        tree: ConversationTree,
        *,
        title: str = "Browse Conversation Tree",
        # Two spaces between items rather than three, and ``Tab/^D: pane`` for what
        # was ``Tab: detail pane``: this line is one row and must stay one row. The
        # dialog's interior is the terminal less its border and padding — 76
        # columns at 80 — and a help line that wraps takes a row from the tree to
        # tell it about a key.
        help_text: str = (
            "Enter: choose  Space: mark  ←/→: fold  ^E: elide  Tab/^D: pane  Esc: cancel"
        ),
    ) -> None:
        super().__init__()
        self._tree = tree
        # Computed here rather than taken as a parameter: see the class docstring on
        # §5.3. `tree()` is one O(entries) walk at construction, which the callers
        # were already paying to build the argument this replaced.
        self._roots = tree.tree()
        self._resolve_entry: Callable[[str], dict[str, Any]] = tree.entry
        self._title = title
        self._help_text = help_text
        self._rows: list[tuple[Any, str, int, bool]] = []
        # Set 2 of §5.3's four: the multi-select set, toggled by `space`. In-memory
        # and per-open — nothing is appended for it (§11.1).
        self._marked: set[str] = set()
        # id → node, and id → parent id, for the detail pane's neighbours. Built
        # from the graph handed in, so the pane never re-walks the session log.
        self._by_id: dict[str, TreeNode] = {}
        self._parent_of: dict[str, str] = {}
        self._depth_of: dict[str, int] = {}
        for root in self._roots:
            self._index(root, 0)
        # §4.3's pair, computed ONCE. It is a property of the log's shape and no
        # gesture can move it, so recomputing it in `_refresh_zones` beside the
        # cursor-dependent sets would pay an O(entries) walk per arrow key for an
        # answer that never changes.
        self._summary_zone, self._abandoned_zone = self._branch_summary_pairs()
        # The entry under the mouse, kept so `_refresh_zones` can re-derive the
        # divergence after a CURSOR move — the divergence is a relation between two
        # nodes and either end moving makes the painted one stale.
        self._hovered: Optional[str] = None
        # Whether the reader has folded the detail pane away to see more tree
        # (PLAN-0.9.4 §4a). A CHOICE, kept separate from the height rule in
        # `_apply_detail_pane` that also hides the pane: a terminal that grows
        # back past `DETAIL_MIN_HEIGHT` must not un-fold a pane the reader folded.
        self._detail_folded = False
        # Every node on one root→leaf line with the elide's other end — its
        # ancestors and its descendants. Cached because it moves only when the
        # MARK moves, while the rows are repainted on every cursor key.
        self._elide_line = self._line_through(self._elide_other_end())

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
                tree = ZoneTree("session", id="tree-browser-tree")
                tree.show_root = False
                # 2 is ``validate_guide_depth``'s floor (textual 8.2.7,
                # _tree.py:1063) and the whole indent budget a row can spare once
                # nesting counts forks (TREE-BROWSER-AS-EDITOR.md §2). It also makes
                # ``_relabel``'s width arithmetic exact rather than approximate —
                # see the comment there.
                tree.guide_depth = 2
                yield tree
                yield TreeDetailPane(self._resolve_entry)
                # What stands where the pane was when it is folded away: one row,
                # so the tree gains the pane's whole half of the body minus this.
                # It exists so the fold is REVERSIBLE by pointing at it — a pane
                # that vanishes with only a key to bring it back is a pane the
                # reader has to remember they hid.
                yield Static(
                    "▸ detail pane hidden — ctrl+D, or click here, to show it",
                    id="tree-detail-folded",
                )
            # The selection readout: what is marked, where those marks converge,
            # and what they are estimated to cost. Outside the body, so it keeps
            # its row when the detail pane gives its own away on a short terminal
            # — the count is the ONLY feedback a mark on the cursor row produces
            # (see :meth:`ZoneTree.render_label`).
            yield Static(self._marks_summary(), id="tree-browser-marks")
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
        """Move the detail pane, and the zone sets, to what the cursor now sits on."""
        await self._show_node(event.node.data)
        self._refresh_zones()

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        """Folding a branch changes set 4 (``hidden``), so the zones are stale.

        The labels are NOT refitted here. A fold changes how many rows the tree
        holds and therefore whether it has a vertical scrollbar — but
        :meth:`_relabel` reserves that width whether the bar is there or not, so
        the answer it gives does not depend on a fold. See the comment there for
        why chasing the current state instead is a loop.
        """
        self._refresh_zones()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """The other direction. Not posted during :meth:`on_mount`'s build — the
        rows are added with ``add(expand=…)``, which sets the flag without a
        message (textual 8.2.7, ``_tree.py:426-431``), so this does not fire once
        per row at startup."""
        self._refresh_zones()

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
        """Show or hide the pane for the current height and fold state.

        The one place the pane's ``display`` is written, mirroring
        ``Parley._apply_side_columns``: an inline style set from two places is
        permanent and invisible to the other.

        Two reasons the pane can be absent and they are not the same reason. The
        HEIGHT rule (:attr:`DETAIL_MIN_HEIGHT`) is the layout's — below it the
        pane cannot say what it exists to say, so it gives its rows to the tree
        and the one-row marker would be a worse use of the last of them. The FOLD
        is the reader's, and it gets the marker, because a choice needs a way back.
        """
        pane = self.query_one(TreeDetailPane)
        marker = self.query_one("#tree-detail-folded", Static)
        tall_enough = self.app.size.height >= self.DETAIL_MIN_HEIGHT
        pane.display = tall_enough and not self._detail_folded
        marker.display = tall_enough and self._detail_folded

    async def action_toggle_detail(self) -> None:
        """``ctrl+D``: fold the detail pane away, or bring it back (§4a).

        The pane takes half the body, and a reader who is following the SHAPE of
        a conversation rather than reading a message wants those rows. Nothing
        but the terminal's height used to be able to hide it.

        Redrawing on the way back rather than on the way out: :meth:`_show_node`
        does nothing while the pane is hidden (there is no audience), so the
        cursor may have moved several rows since the last frame the pane drew.
        :meth:`TreeDetailPane.show` dedupes, so a cursor that did not move costs
        nothing here.
        """
        self._detail_folded = not self._detail_folded
        self._apply_detail_pane()
        if not self._detail_folded:
            await self._show_cursor_node()

    async def on_click(self, event: events.Click) -> None:
        """Double-click the pane to fold it; single-click the marker to unfold.

        ``event.widget is pane`` and not "the pane or anything in it": a click
        inside the pane lands on a :class:`MessageBox`, a tool box or a markdown
        block, and those are things the reader is *reading*. The pane itself is
        reachable only at its border and padding, which is the gesture asked for.

        The marker is one row of text with nothing to read, so one click is
        enough — a fold you have to double-click your way out of is a trap.
        """
        pane = self.query_one(TreeDetailPane)
        if event.widget is pane and event.chain >= 2:
            await self.action_toggle_detail()
            return
        if event.widget is self.query_one("#tree-detail-folded", Static):
            await self.action_toggle_detail()

    def on_mount(self) -> None:
        """Build the widget tree from :func:`plan_tree_rows`.

        The nesting rules live in that function, which is pure and has its own
        tests; this is the widget build alone. Two rules: a fork opens a level
        (TREE-BROWSER-AS-EDITOR.md §2 — indent counts branches, not messages, so
        it does not grow with the conversation) and a user message opens a level
        that the next user message closes (PLAN-0.9.4 §4, the turn group). Line
        ORDER is a depth-first walk either way, so the rows appear in the sequence
        the log has.

        ``data=node.id`` is deliberately unchanged — the widget nesting is a
        rendering decision and ``parentId`` stays the property of the data, so
        :meth:`_view_of`, :class:`TreeDetailPane` and every caller reading
        ``node.data`` are unaffected. :attr:`_depth_of` (built in :meth:`_index`)
        remains the *data* depth the pane counts with; the depth recorded in
        :attr:`_rows` is the *widget* depth, because that is what :meth:`_relabel`
        needs to know how much indentation a row is paying for. A row the planner
        drops is dropped from the DRAWING only: it keeps its entry, its place in
        :attr:`_by_id` and its ancestry.

        The cost is on the record: Textual highlights the hovered row's ancestry
        through its guide rails, and a flattened run has no rails between siblings.
        §3's ``tree--zone-path`` is the replacement.
        """
        self._apply_detail_pane()
        tree = self.query_one("#tree-browser-tree", Tree)
        if isinstance(tree, ZoneTree):
            # Every node, not just the drawn ones — building the map from
            # `_by_id` costs one pass over the log and means a row the planner
            # later starts drawing (a `navigate` that forks) needs no second
            # producer. `isinstance` because `query_one` is typed to `Tree` here
            # and a test double could supply a plain one.
            tree.set_kinds(
                {
                    node_id: span
                    for node_id, node in self._by_id.items()
                    if (span := tree_kind_span(node)) is not None
                }
            )
        leaf_widget: list[Any] = []
        # (widget node, full label, WIDGET depth) for every row, kept so _relabel
        # can re-elide from the untruncated text on every resize. Eliding an already
        # elided label would eat a character per resize.
        self._rows = []

        widgets: list[Any] = []
        for row in plan_tree_rows(self._roots):
            label = self._label(row.node)
            parent = tree.root if row.parent is None else widgets[row.parent]
            # ``expand`` on the ``add`` rather than a following ``.expand()``: both
            # set the flag, but ``expand()`` also POSTS ``NodeExpanded`` (textual
            # 8.2.7, _tree.py:249-258), and :meth:`on_tree_node_expanded` recomputes
            # every zone set. One message per row at mount would make building the
            # tree quadratic in the conversation's length.
            # ``allow_expand`` off for a row nothing hangs from: Textual draws the
            # toggle off that flag alone and never checks for children, so an
            # assistant or tool row otherwise wears an arrow that clicks, toggles,
            # and reveals nothing.
            widget_node = parent.add(
                label,
                data=row.node.id,
                expand=row.expanded,
                allow_expand=row.has_children,
            )
            widgets.append(widget_node)
            self._rows.append((widget_node, label, row.depth, row.has_children))
            if row.node.is_leaf:
                leaf_widget.append(widget_node)

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
        # After the deferred cursor move, for the same reason the pane's first draw
        # is deferred: the zones are a function of where the cursor ended up, and a
        # session whose leaf is also the first row posts no ``NodeHighlighted``.
        tree.call_after_refresh(self._refresh_zones)
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
        # The vertical scrollbar's width, subtracted ALWAYS. ``content_size`` is
        # ``region.shrink(styles.gutter)`` in textual 8.2.7 — border and padding
        # only, never the scrollbar — so a tree tall enough to scroll had every
        # label sized two cells too wide, the rows overflowed, and it grew a
        # horizontal scrollbar showing two cells of nothing (which then cost a
        # row of height as well). The arithmetic below was always exact; it was
        # the width handed to it that was wrong.
        #
        # Reserved unconditionally rather than read off the CURRENT scrollbar
        # state (``scrollable_content_region``), because that state is a moving
        # target and this method is one of the things that moves it: shortening
        # the labels can retire the horizontal scrollbar, which gives back a row
        # of height, which can retire the VERTICAL one, which would widen the
        # labels again and bring the first one back. Measured: three ``_relabel``
        # passes at mount, all three seeing a vertical scrollbar that was gone by
        # the time the tree settled. Folding a turn open moves it too, and
        # reproduced the reported symptom exactly — a tree that opened at four
        # rows with no scrollbar reached ``max_scroll_x == 2`` on the first
        # expand.
        #
        # The cost is two cells of preview on a tree short enough not to scroll.
        # A stable answer that is occasionally two cells conservative beats a
        # tight one that oscillates.
        #
        # ``styles.scrollbar_size_vertical`` and not ``Widget`` 's property of the
        # same name: the property answers 0 when the bar is not currently shown,
        # which is the very state this refuses to depend on.
        width = tree.content_size.width - tree.styles.scrollbar_size_vertical
        if width <= 0:
            return
        for widget_node, label, depth, has_children in self._rows:
            # Textual indents each level by ``guide_depth`` cells (_tree.py:65-81,
            # ``show_root`` False) and ``render_label`` prefixes the row with a
            # 2-cell toggle (_tree.py:876-901); both eat into the label's share of
            # the line. ``depth`` is the WIDGET depth, which counts forks and turn
            # groups — so the term no longer grows with the conversation. At
            # ``guide_depth == 2`` the toggle is exactly one more level, which makes
            # this arithmetic exact instead of merely conservative.
            #
            # A row nothing hangs from carries no toggle (``allow_expand`` is off
            # for it), so it gets those two cells back for its preview.
            toggle = tree.guide_depth if has_children else 0
            available = width - depth * tree.guide_depth - toggle
            widget_node.set_label(_elide(label, available))

    @staticmethod
    def _label(node: TreeNode) -> str:
        tag = node.role or node.kind
        text = node.preview or f"({node.kind})"
        marker = "  ◀ current" if node.is_leaf else ""
        return f"{tag}: {text}{marker}"

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """A click SELECTS; it does not commit (TREE-BROWSER-AS-EDITOR.md §5.1).

        ``Tree._on_click`` sets ``cursor_line`` and then runs ``select_cursor``
        (textual 8.2.7, _tree.py:1453-1466), which posts this message — the same
        message ``Enter`` used to arrive by. Dismissing here is why a click jumped
        straight out of the browser: the reader could not point at a node to read it
        in the detail pane without leaving. The screen's priority ``enter`` binding
        now owns the commit, so this message can only have come from a click.

        ``move_cursor`` rather than nothing: the click has already moved the cursor,
        but a ``NodeSelected`` raised any other way should still leave the cursor —
        and therefore the detail pane — on the node the reader named.
        """
        event.stop()
        self.query_one("#tree-browser-tree", Tree).move_cursor(event.node)

    def action_commit(self) -> None:
        """``Enter``: dismiss with an intent naming the cursor node (§5.1).

        ``TreeIntent("navigate", (id,))`` is §5.3's degenerate case, and is exactly
        what ``dismiss(id)`` meant before the return type widened (§11.1). One id,
        because the cursor is one node; the marked set is not committed here — no
        gesture consumes it yet, and inventing one would be a producer for an
        operation §6 has not built.

        **Two actions, because pointing at a user message means something else**
        (PLAN-0.9.4 §4, item 2). ``navigate`` continues from BELOW the named node,
        which is right for an assistant or tool row. A user message's below is the
        one place a conversation cannot go — two user turns in a row — and what
        the reader means by pointing at one is "ask this differently", which is a
        fork from that message's PARENT with its text in hand to edit. That is
        ``revise``, and the id it carries is still the node the reader named.

        The id, not the parent: this modal reports what was pointed at and the
        CALLER knows the question (§5.3 / §11.1). The elide flow asks a different
        question of this same browser and reads ``sole_id``, which both actions
        carry — see :meth:`Parley._elide_span_flow`, which says so rather than
        relying on it.

        No cursor means nothing was named, so there is nothing to answer with and
        the browser stays open. Dismissing with ``None`` here would be indexed as a
        cancel, which is a different thing than "Enter on an empty tree".
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        entry_id = str(node.data)
        picked = self._by_id.get(entry_id)
        is_user = picked is not None and picked.kind == "message" and picked.role == "user"
        self.dismiss(TreeIntent("revise" if is_user else "navigate", (entry_id,)))

    def action_collapse(self) -> None:
        """``left``: fold this fork, or step out to the enclosing one (§5.2).

        ``left`` is unbound in ``Tree.BINDINGS`` (textual 8.2.7, _tree.py:524-551 —
        only ``shift+left`` is ``cursor_parent``), so the standard file-tree idiom
        was simply missing. A node with widget children is a fork or a user turn
        (PLAN-0.9.4 §4 added the second), which makes "collapse what the cursor is
        on, else go to what contains it" the gesture for folding away a branch or a
        turn rather than for hiding one message.

        The widget root is skipped: ``show_root`` is ``False``, so it occupies no
        line and ``move_cursor`` onto it would clear the cursor rather than move it.
        """
        tree = self.query_one("#tree-browser-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.children and node.is_expanded:
            node.collapse()
            return
        parent = node.parent
        if parent is not None and parent is not tree.root:
            tree.move_cursor(parent)

    def action_expand(self) -> None:
        """``right``: unfold the fork or turn the cursor is on.

        The counterpart :meth:`action_collapse` needs, and the gesture that opens a
        turn group, which is how most rows now arrive (PLAN-0.9.4 §4: a group
        mounts collapsed unless the cursor is inside it). ``space`` used to be
        ``Tree``'s expand/collapse toggle and now marks (see :attr:`BINDINGS`), so
        without this a collapsed row could not be reopened from the keyboard at
        all. A row with no widget children has nothing to unfold; moving the cursor
        into the subtree on ``right`` is deliberately NOT done — ``down`` already
        goes there, and the pair here is about folding.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is not None and node.children and not node.is_expanded:
            node.expand()

    def action_toggle_mark(self) -> None:
        """``space``: add or remove the cursor node from the marked set (§5.3 set 2).

        The key is this implementation's choice; §5.3 names the set and binds
        nothing to it. Marks are in-memory and per-open: §11.1 keeps the modal free
        of a ``SessionLog``, so nothing here is durable and closing the browser
        forgets them.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        entry_id = str(node.data)
        if entry_id in self._marked:
            self._marked.remove(entry_id)
        else:
            self._marked.add(entry_id)
        self._elide_line = self._line_through(self._elide_other_end())
        self._refresh_zones()

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- the elide, from inside the browser (PLAN-0.9.4 §4) -------------------

    def _elide_other_end(self) -> Optional[str]:
        """The node the cursor is being paired WITH, or ``None`` if there isn't one.

        Exactly one mark is the pairing the reader asked for. **No** mark still
        elides — against the current leaf, which is the ordinary case ("fold the
        history behind where I am and keep going") and the one that would
        otherwise cost a mark to say. More than one mark is refused: an elide has
        two ends and a set of three does not name them.
        """
        if len(self._marked) == 1:
            return next(iter(self._marked))
        if self._marked:
            return None
        return self._leaf_id()

    def _leaf_id(self) -> Optional[str]:
        """The ``◀ current`` node — the log's cursor as the browser drew it."""
        for node in self._by_id.values():
            if node.is_leaf:
                return node.id
        return None

    def _line_through(self, node_id: Optional[str]) -> frozenset[str]:
        """Every node on a root→leaf line through ``node_id``: its ancestry and its
        descendants.

        This is exactly the set an elide's other end can come from, because
        ``elide_span`` requires the resume point to be on the anchor's path — the
        fold's forward scan only ever walks ancestors, and a boundary it cannot
        reach would empty the context in silence (``TauBackend.elide_span``).

        Both directions, because either node can turn out to be the anchor: the
        deeper of the two always is (see :class:`ElidePlan`).
        """
        if node_id is None:
            return frozenset()
        line = {node_id}
        walk = self._parent_of.get(node_id)
        while walk is not None:
            line.add(walk)
            walk = self._parent_of.get(walk)
        stack = list(self._by_id[node_id].children)
        while stack:
            node = stack.pop()
            line.add(node.id)
            stack.extend(node.children)
        return frozenset(line)

    def _elide_ineligible(self) -> frozenset[str]:
        """Rows to grey out: everything off the line, while a span is being chosen.

        Only when exactly ONE node is marked. With none, the reader is browsing
        and the elide is merely available; with several, nothing has been named
        and greying to a set of three would be a guess.

        This is the ANCESTRY rule alone. The other way an elide can be illegal —
        a legal pair whose span happens to be empty — is one row, and it is
        refused by name when ``ctrl+E`` is pressed rather than greyed here,
        because computing it for every row means one context walk per row.
        """
        if len(self._marked) != 1:
            return frozenset()
        return frozenset(node_id for node_id in self._by_id if node_id not in self._elide_line)

    def _elide_plan(self, cursor: Optional[str]) -> "ElidePlan | None":
        """The elide ``cursor`` and the other end would make, or ``None``.

        Every rejection ``TauBackend.elide_span`` performs is performed here
        first, on the same rules and against the same tree, so the help line can
        only offer an elide the backend will accept. That is the point of doing
        it here: the reported problem was learning the pick was illegal by
        landing back in the conversation with an error.

        **The refusal is measured at the anchor and the COST at the cursor**, and
        those are two different sets — see :class:`ElidePlan`. Measuring both at
        the anchor is the defect this fixed: over ``[1..6]`` with the cursor at 6,
        pairing 2 with 4 was offered as "elide 1 message" when three leave, because
        ``context_entries(anchor)`` cannot see the two the cursor move abandons.
        Measuring both at the cursor would be wrong the other way — it would offer
        an elide whose fold hides nothing, which the backend then refuses.
        """
        other = self._elide_other_end()
        if cursor is None or other is None or cursor == other:
            return None
        # Deeper end is the anchor. `_depth_of` is the DATA depth (built in
        # `_index`), not the widget depth the planner assigns — the fold walks
        # `parentId`, so this has to be the same graph `elide_span` will walk.
        if self._depth_of[cursor] > self._depth_of[other]:
            anchor, first_kept = cursor, other
        else:
            anchor, first_kept = other, cursor
        path_ids = [entry["id"] for entry in self._tree.path(anchor)]
        if first_kept not in path_ids:
            return None
        kept = set(path_ids[path_ids.index(first_kept) :])
        folded = [e for e in self._tree.context_entries(anchor) if e["id"] not in kept]
        if not folded:
            return None
        dropped = [e for e in self._tree.context_entries() if e["id"] not in kept]
        return ElidePlan(
            anchor=anchor,
            first_kept=first_kept,
            folded=len(folded),
            dropped=len(dropped),
            moves_cursor=anchor != self._tree.cursor,
        )

    def action_elide(self) -> None:
        """``ctrl+E``: fold the span between the cursor and the other end.

        Dismisses with the pair; the caller performs it. This screen still holds
        no ``SessionLog`` and still writes nothing (§11.1).

        An illegal pick is refused HERE, with the reason, and the browser stays
        open — which is the whole change. It used to be discovered one modal
        later, after the browser had closed, as an error notification over a
        conversation the reader could no longer see the shape of.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        plan = self._elide_plan(cursor)
        if plan is not None:
            self.dismiss(TreeIntent("elide", (plan.anchor, plan.first_kept)))
            return
        self.app.notify(self._elide_refusal(cursor), severity="warning")

    def _elide_refusal(self, cursor: Optional[str]) -> str:
        """Why the elide the reader just asked for is not one. One sentence each.

        Ordered from "nothing was named" to "this pair is legal but empty", which
        is the order the reader hits them in.
        """
        if cursor is None:
            return "Put the cursor on a node first."
        if len(self._marked) > 1:
            return (
                f"{len(self._marked)} nodes are marked. An elide has two ends — "
                "mark one node, and put the cursor on the other."
            )
        other = self._elide_other_end()
        if other is None:
            return "There is no current node to fold back to."
        if cursor == other:
            return "That is both ends of the elide. Move the cursor, or mark another node."
        if cursor not in self._elide_line:
            return (
                "Those two nodes are on different branches. An elide folds a span "
                "of ONE line of the conversation, so the two ends have to be on it."
            )
        return "That would hide nothing — the span between those two nodes is already empty."

    # -- the four selection sets (§5.3) --------------------------------------

    def _refresh_zones(self) -> None:
        """Recompute all four sets and hand them to the renderer.

        One entry point, called from every gesture that can move a set: the cursor
        (``NodeHighlighted``), a mark (``space``), a fold (``NodeCollapsed`` /
        ``NodeExpanded``) and the deferred initial layout. Recomputing all four
        rather than patching the one that moved — ``path``/``folded``/``covered``
        are all functions of the cursor, and a partial update is how two of them
        end up describing different cursors.

        This is the EXPENSIVE path — two walks of the conversation plus one of every
        widget row — and it is deliberately not what a hover runs. See
        :meth:`ZoneTree.set_hover_zones`.
        """
        if not self._rows:
            return
        tree = self.query_one("#tree-browser-tree", ZoneTree)
        node = tree.cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        path, folded, covered = self._derived(cursor)
        hover_common, hover_divergent = self._hover_divergence(cursor, self._hovered)
        tree.set_zones(
            TreeZones(
                cursor=cursor,
                marked=frozenset(self._marked),
                path=path,
                folded=folded,
                covered=covered,
                hidden=self._hidden(),
                # §7 mints these; there is no copy entry to be in the set yet, and
                # an empty frozenset is the truth rather than a placeholder.
                copied=frozenset(),
                summary=self._summary_zone,
                abandoned=self._abandoned_zone,
                ineligible=self._elide_ineligible(),
                # Re-derived, not carried over: the divergence is measured FROM the
                # cursor, so the cursor move that brought us here invalidated
                # whatever is painted. Cheap — see :meth:`_hover_divergence`.
                hover_common=hover_common,
                hover_divergent=hover_divergent,
            )
        )
        self.query_one("#tree-browser-marks", Static).update(self._marks_summary(cursor))

    def _derived(
        self, cursor: Optional[str]
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """§5.3's set 3, as the three roles §3's class table distinguishes.

        ``path`` is the cursor's raw ancestor chain (``ConversationTree.path``,
        every kind, no splice). ``folded`` is what the fold at the cursor drops from
        that chain — the difference between the raw walk and ``context_entries``,
        which is exactly "on the path, dropped by a splice anchor".

        ``covered`` is the same span, reported separately when the cursor IS the
        anchor doing the dropping. Which anchor that is comes out of
        ``context_entries`` rather than out of a kind test:
        ``_active_path_entries`` emits ``[anchor] + kept + after``
        (``conversation_tree.py:383-391``), so element 0 of a folded result is the
        anchor by construction. Reading it there instead of re-testing
        ``entry["type"] in ("compaction", "elide")`` keeps the anchor vocabulary in
        the one module that owns it — ``_SPLICE_ANCHOR_KINDS`` is private to
        ``conversation_tree`` and a second copy in the TUI would be a fifth kind
        away from being wrong.

        ``folded`` deliberately still contains ``covered``: the two are the same
        rows seen from two positions, and :attr:`ZoneTree._LABEL_ZONES` resolves
        which class wins rather than the sets pre-subtracting each other.
        """
        empty: frozenset[str] = frozenset()
        if cursor is None:
            return empty, empty, empty
        path_ids = frozenset(entry["id"] for entry in self._tree.path(cursor))
        kept = self._tree.context_entries(cursor)
        kept_ids = {entry["id"] for entry in kept}
        folded = frozenset(entry_id for entry_id in path_ids if entry_id not in kept_ids)
        anchor = kept[0]["id"] if kept else None
        covered = folded if (folded and anchor == cursor) else empty
        return path_ids, folded, covered

    def _branch_summary_pairs(self) -> tuple[frozenset[str], frozenset[str]]:
        """§4.3's two-row relation: each ``branch_summary`` and what it summarizes.

        §1.2 established that no structural change is needed —
        ``SessionStore.append_branch_summary`` (``session_store.py:665``) moves the
        leaf to ``from_id`` *before* appending, mirroring pi's ``branchWithSummary``
        (``session-manager.ts:1272``), so ``parentId == fromId`` and the summary is
        already a sibling of the abandoned branch's first message. What was missing
        is that a reader cannot see it. §4.3 was attempted inside ``_preview_of``
        and correctly bounced there: that renders one line for one node, and this is
        a relation BETWEEN two rows. So it is zone work (§3).

        **Which sibling.** The immediately PRECEDING one, in the order the browser
        already draws them (``ConversationTree.tree`` sorts children by timestamp;
        roots keep load order, which for an append-only log is the same order). Not
        "every sibling that is not the summary": a branch point can be abandoned
        more than once, and ``b1, S1, b2, S2`` then pairs correctly — ``S1`` looks
        back at ``b1``, ``S2`` at ``b2`` — where a set-difference rule would blame
        ``S2`` for ``b1`` as well. It is also the phrase §4.3 uses: *the* abandoned
        branch's first message, singular.

        A ``branch_summary`` with no earlier sibling is left out of both sets rather
        than paired with something. That shape means a branch was summarized before
        it existed; painting half a pair would state a relation that is not there.

        Computed on the ``TreeNode`` graph, so it needs no payload lookup — ``kind``
        is on the node (``conversation_tree.py:173``) and the ``fromId`` the payload
        carries would only re-state the ``parentId`` the graph is already built
        from.
        """
        summary: set[str] = set()
        abandoned: set[str] = set()
        stack: list[list[TreeNode]] = [self._roots]
        while stack:
            siblings = stack.pop()
            for position, node in enumerate(siblings):
                if node.children:
                    stack.append(node.children)
                if node.kind != "branch_summary" or position == 0:
                    continue
                summary.add(node.id)
                abandoned.add(siblings[position - 1].id)
        return frozenset(summary), frozenset(abandoned)

    def on_zone_tree_hover_changed(self, event: "ZoneTree.HoverChanged") -> None:
        """Repaint the divergence for the row the pointer moved onto (§3, step 5).

        §2 removed the guide-rail ancestry highlight — after flattening, a
        30-message run is 30 siblings at one level with no rails between them — and
        §3 is "the replacement, not an embellishment". ``tree--zone-path`` replaced
        the CURSOR's ancestry in step 3; this replaces the HOVER's, and says
        something the rails never did: not just "these are the hovered row's
        ancestors" but *where that ancestry stops agreeing with where you are*.

        Nothing here recomputes a selection set. The two ancestry walks in
        :meth:`_hover_divergence` are the whole per-hover cost.
        """
        event.stop()
        self._hovered = event.entry_id
        tree = event.zone_tree
        node = tree.cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        tree.set_hover_zones(*self._hover_divergence(cursor, self._hovered))

    def _hover_divergence(
        self, cursor: Optional[str], hovered: Optional[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Split ``hovered``'s ancestry where it leaves ``cursor``'s (§3, step 5).

        The common prefix and the divergent tail are different facts and the
        stylesheet reads them differently: the prefix is context the two nodes
        share, the tail is what you would be picking up if you went there.

        **A node ON the cursor's path reports no divergence.** Its chain is then a
        PREFIX of the cursor's, the tail is empty by construction, and this returns
        two empty sets rather than a prefix with nothing to contrast against —
        painting the shared half alone would show a highlight that means "you are
        already here", which reads as a divergence that is not there. Hovering the
        cursor itself, or leaving the tree entirely, lands in the same case.

        A DESCENDANT of the cursor is not on the cursor's path and does diverge:
        the rows below the cursor are exactly what the cursor's context does not
        contain, which is the question this answers.

        ``k == 0`` — two different roots, which ``ConversationTree.tree`` really can
        produce from an orphaned entry — gives an empty prefix and the hovered
        chain entire. That is the honest answer, and it is the same answer
        :meth:`_lowest_common_ancestor` gives for the same shape.

        Cost is O(depth), twice: :meth:`_ancestry` is a ``_parent_of`` walk, and the
        prefix scan is a ``zip``. No conversation walk, no row walk — which is the
        constraint, because this runs on every row the pointer crosses.
        """
        empty: frozenset[str] = frozenset()
        if cursor is None or hovered is None:
            return empty, empty
        hover_chain = self._ancestry(hovered)
        cursor_chain = self._ancestry(cursor)
        shared = 0
        for mine, theirs in zip(hover_chain, cursor_chain):
            if mine != theirs:
                break
            shared += 1
        if shared == len(hover_chain):
            # `hovered` is the cursor, or one of its ancestors. Nothing diverges.
            return empty, empty
        return frozenset(hover_chain[:shared]), frozenset(hover_chain[shared:])

    def _hidden(self) -> frozenset[str]:
        """§5.3's set 4: rows the reader has folded out of sight.

        View state, per §11.2 — computed from the widget tree, never read from the
        log, and nothing is appended for it. The *archived* half of §11.2's decision
        has no gesture yet, so this is the collapsed half alone; a row in it is not
        currently drawn, which is why ``tree--zone-hidden`` has no visible effect
        today and why :meth:`_marks_summary` is where the set is observable at all.

        The walk stops one short of the widget root. ``Tree.__init__`` builds its
        root with ``expand=False`` (textual 8.2.7, ``_tree.py:783`` →
        ``_add_node``'s default) and ``_build`` adds the root's children anyway
        when ``show_root`` is ``False`` (``_tree.py:1272-1275``) — so the root's
        collapsed flag means nothing here, and counting it would report every
        top-level row as folded away.
        """
        hidden: set[str] = set()
        for widget_node, _label, _depth, _has_children in self._rows:
            walk = widget_node.parent
            while walk is not None and walk.parent is not None:
                if not walk.is_expanded:
                    hidden.add(str(widget_node.data))
                    break
                walk = walk.parent
        return frozenset(hidden)

    def _lowest_common_ancestor(self, ids: set[str]) -> Optional[str]:
        """The deepest node every id in ``ids`` descends from, itself included.

        §5.3: the ``_parent_of`` map :meth:`_index` already builds "gives the lowest
        common ancestor for free". Root→node chains, then the longest common prefix.

        ``None`` when the marked nodes are in different roots — an orphaned entry
        (broken parent chain) is its own root in ``ConversationTree.tree``, so two
        marks really can have no ancestor in common, and saying so beats naming an
        arbitrary one.
        """
        if not ids:
            return None
        chains = [self._ancestry(entry_id) for entry_id in sorted(ids)]
        common: Optional[str] = None
        for step in zip(*chains):
            if len(set(step)) != 1:
                break
            common = step[0]
        return common

    def _ancestry(self, entry_id: str) -> list[str]:
        """``entry_id``'s root→self chain through :attr:`_parent_of`."""
        chain = [entry_id]
        walk = self._parent_of.get(entry_id)
        seen = {entry_id}
        while walk is not None and walk not in seen:
            chain.append(walk)
            seen.add(walk)
            walk = self._parent_of.get(walk)
        chain.reverse()
        return chain

    def _marks_summary(self, cursor: Optional[str] = None) -> str:
        """The marked set's count, its lowest common ancestor and its size (§5.3).

        **The size is a labelled estimate, and says so.** ``compaction.estimate_tokens``
        is a ~4-chars-per-token heuristic over the payload; the only measured number
        in a session is ``usage.input_tokens`` on an assistant message
        (``agent_loop.py:819``), which is a measurement of one request rather than of
        an arbitrary set of entries. §5.3: "a row may state a measured
        ``input_tokens``; a selection total may only state an estimate, and must say
        so". So this prints ``~N tokens (estimate)`` and never a bare number.

        **The elide offer goes FIRST when there is one.** This line is one row
        (``#tree-browser-marks`` is ``height: 1``), so it clips rather than
        wrapping — and the readout plus the offer runs to about 96 columns, which
        on an 80-column terminal means whatever is last is what disappears. The
        offer is the only part of the line that is an action.
        """
        elide = self._elide_offer(cursor)
        if not self._marked:
            base = "nothing marked · space marks the row under the cursor"
            return f"{elide} · {base}" if elide else base
        count = len(self._marked)
        noun = "node" if count == 1 else "nodes"
        folded_away = len(self._marked & self._hidden())
        out_of_sight = f" ({folded_away} folded away)" if folded_away else ""
        ancestor = self._lowest_common_ancestor(self._marked)
        where = f"common ancestor {ancestor}" if ancestor is not None else "no common ancestor"
        line = (
            f"{count} {noun} marked{out_of_sight} · {where} · "
            f"~{self._estimated_tokens(self._marked)} tokens (estimate)"
        )
        return f"{elide} · {line}" if elide else line

    def _elide_offer(self, cursor: Optional[str]) -> str:
        """``ctrl+E: keep this span, drop the other 14`` — or ``""``.

        The empty string is the feature, not a fallback: the offer appears exactly
        when pressing the key would do something, so a reader who has never used
        the gesture meets it on the row where it applies rather than in a list of
        keys they have to test one at a time. This is what was asked for in place
        of the mode-chooser's ``Elide a span ending here…`` button, which named the
        operation on every node whether or not it could be performed on that one.

        **"keep this span" is load-bearing wording.** It read ``elide N messages``,
        which every reader parses as "remove the N between these two rows" — the
        opposite of what an elide does (:class:`ElidePlan`). The frame has to be
        stated where the gesture is, not only in the manual, because the manual is
        not open at the moment somebody presses the key.

        ``and move back to it`` is appended when the anchor is not the current tip,
        because that is a second thing happening: the conversation resumes
        somewhere else, and the entries newer than the anchor are part of the
        ``dropped`` count precisely for that reason.

        ``cursor`` is passed in rather than read off the tree: :meth:`compose`
        writes the first version of this line, and the tree it would query does
        not exist yet at that point. ``None`` there is honest — no row is under
        the cursor until one is drawn.
        """
        plan = self._elide_plan(cursor)
        if plan is None:
            return ""
        noun = "entry" if plan.dropped == 1 else "entries"
        move = ", and move back to it" if plan.moves_cursor else ""
        return f"ctrl+E: keep this span, drop the other {plan.dropped} {noun}{move}"

    def _estimated_tokens(self, ids: set[str]) -> int:
        """A character-based token estimate over the entries ``ids`` names.

        ``compaction.estimate_tokens`` for the entries that carry a message, and the
        same arithmetic over the summary text for the kinds that carry one instead
        (``compaction``, ``branch_summary`` — what ``context_for`` injects for them
        is a message built from that string, ``conversation_tree.py:78-96``). Kinds
        with neither, such as ``navigate`` and ``elide``, contribute nothing to a
        context and so contribute nothing here.

        Never presented without the word "estimate" beside it — see
        :meth:`_marks_summary`.
        """
        from tau_agent_core.compaction import estimate_tokens

        total = 0
        for entry_id in ids:
            entry = self._resolve_entry(entry_id)
            message = entry.get("message")
            if isinstance(message, dict):
                total += estimate_tokens(message)
                continue
            summary = entry.get("summary")
            if isinstance(summary, str):
                total += estimate_tokens({"role": "user", "content": summary})
        return total


class TreeModeModal(ModalScreen[Optional[str]]):
    """The mode chooser after a node is picked (§3.1).

    pi's ``showExtensionSelector`` (interactive-mode.ts:4479-4483): "No summary" /
    "Summarize" / "Summarize with custom instructions". Dismisses with
    ``"navigate"`` / ``"summarize"`` / ``"custom"`` (or ``None`` on cancel).

    **``elide`` used to be a fourth button here and is not one any more**
    (PLAN-0.9.4 §4). It never fitted: the other three treat the picked node as a
    BRANCH POINT and move the cursor back to it, while an elide treats it as the
    fold's ANCHOR and needs a second node before it can do anything — so choosing
    it re-opened the whole tree browser to ask for that second node, and an
    illegal pick was reported after both screens had closed. It is a key in the
    browser now (``ctrl+E``, :meth:`SessionTreeModal.action_elide`), which is the
    one place both nodes are visible at once and the only place a refusal can be
    stated while the reader can still see what they picked.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Container(id="tree-mode-dialog"):
            yield Static("Act on selected node", id="tree-mode-title")
            with Vertical(id="tree-mode-buttons"):
                yield Button("Branch: no summary", variant="primary", id="mode-navigate")
                yield Button("Branch: summarize abandoned branch", id="mode-summarize")
                yield Button("Branch: summarize with custom instructions…", id="mode-custom")
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
        # Children created before compose() ran, mounted in creation order by
        # on_mount. A live step box is mounted fire-and-forget
        # (ExchangeBox.add_step) and the agent loop drains a non-empty
        # asyncio.Queue without ever yielding to the event loop, so the first
        # reasoning delta or tool call of a turn routinely arrives while
        # compose() still has not run and the slots below do not exist yet.
        # Mirrors the text body's own pre-compose buffering (see
        # append_content_delta's hasattr gate and on_mount's catch-up).
        #
        # NOT named _pending_children: Textual's Widget already owns that name
        # for its own compose stack, and shadowing it with these (slot, widget)
        # tuples makes mount_composed_widgets try to mount a tuple.
        self._deferred_children: list[tuple[str, Widget]] = []
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
        # Mount whatever ensure_reasoning/add_tool_call created while the slots
        # did not exist yet, in creation order. compose() has run by now, so
        # every slot named here is present and attached.
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

        ``ToolBox`` buffers a result written before it mounts, so a call and its
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
        "started",
        "measured_output",
        "chunks",
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
        #: When this lane's exchange opened, on the MONOTONIC clock — the right
        #: one for an elapsed readout that must not jump when the wall clock is
        #: adjusted. ``None`` for a lane nobody opened an exchange for (the
        #: defensive path), which shows no duration rather than a fabricated one.
        #: The FINISHED exchange's duration is still ``Parley``'s own
        #: ``time.time()`` measurement, passed to :meth:`finalize_exchange`, so
        #: this clock cannot change what a completed turn reports.
        self.started: float | None = None
        #: Real ``output_tokens`` summed over this lane's COMPLETED completions,
        #: as of the last ``completion_end``. Never an estimate.
        self.measured_output: int = 0
        #: Stream events received since the last completion boundary — the
        #: completion in flight, which has no measured token count at all.
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
        #: The list the last :meth:`reload_messages` was handed, kept by
        #: reference so :meth:`show_all_messages` can re-render the WHOLE
        #: transcript. It is the caller's own list object, not a copy — there is
        #: no second source of truth to fall out of step.
        self._reload_source: list[dict] = []
        #: How many messages the last reload declined to MOUNT. Zero when the
        #: whole transcript is on screen.
        self._elided = 0
        #: Repaints every open exchange's live counter. Created paused in
        #: :meth:`on_mount` and only running while a lane is open, so an idle
        #: chat costs nothing. ``None`` on a display that was never mounted (a
        #: bare renderer harness), which simply has no counter.
        self._live_timer: Timer | None = None

    #: Bound on how much of a reloaded transcript is mounted as widgets.
    #: Whichever limit is reached first walking BACKWARDS from the end wins.
    #:
    #: This is a RENDERING bound and nothing else. The whole conversation is
    #: still loaded, still in the session log, and still what the model is sent;
    #: only the widget tree is capped. If this ever changes model input it has
    #: become a silent context bug (docs/TREE-BROWSER-AS-EDITOR.md's
    #: tree-as-truth invariant), which is why the cap lives here on the display
    #: and touches no message list.
    #:
    #: The numbers matter because the render cost is quadratic in the mounted
    #: widget count: an 800-message reload takes over four minutes, and the same
    #: mounted tree throttles streaming to a couple of tokens per second.
    RENDER_CAP_TURNS = 4
    RENDER_CAP_MESSAGES = 50

    #: How often the live exchange counter repaints, in seconds. Deliberately
    #: decoupled from the delta rate: the counter must keep moving through a
    #: thirty-second tool call, when nothing is arriving at all, and it must not
    #: cost a title repaint per delta on a fast stream.
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
        # The ``⋯ N earlier`` row is neither, and a stale one would keep claiming
        # a count for a transcript that is no longer on screen.
        await self.query(".chat-fold").remove()
        self._elided = 0
        self._lanes = {}
        # Every exchange the counter had to draw has just been removed.
        self._sync_live_timer()
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
        state = _LaneRender(exchange, label)
        state.started = time.monotonic()
        self._lanes[lane] = state
        await self.mount(exchange)
        self._sync_live_timer()
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
            state.chunks += 1
            await self._on_reasoning_delta(state, event.get("delta", ""))
        elif kind == "text_delta":
            state.chunks += 1
            await self._on_text_delta(state, event.get("delta", ""))
        elif kind == "tool_call":
            await self._on_tool_call(state, event)
        elif kind == "tool_result":
            self._on_tool_result(state, event)
        elif kind == "completion_end":
            # A completion boundary: what was estimated is now measured, and
            # nothing is in flight until the next delta. Reasoning and answer
            # text both count as chunks above — both are stream events, and a
            # reasoning model that thinks for a minute before answering is the
            # case this counter exists for.
            state.measured_output = int(event.get("output", 0) or 0)
            state.chunks = 0

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
        # Popped above, so the counter timer stops with the last lane and can no
        # longer overwrite the summary this call is about to stamp.
        self._sync_live_timer()
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
            context=context,
            output=output,
            seconds=seconds,
            telemetry=telemetry,
            label=state.label,
        )
        self.scroll_end(animate=False)

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

    # ------------------------------------------------------------------
    # Reload: reconstruct exchanges from the persisted flat message list
    # ------------------------------------------------------------------

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
        # 0 leads the candidates so a transcript that already fits reports a
        # start of 0 rather than the first user message -- otherwise a leading
        # system message would read as one elided message that never renders.
        starts = [0, *users]
        within = [s for s in starts if len(messages) - s <= self.RENDER_CAP_MESSAGES]
        # No candidate leaves a short enough tail: one span is over the bound on
        # its own, so mount that span rather than nothing.
        by_count = within[0] if within else users[-1]
        return max(by_turns, by_count)

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

        The mounting runs inside ``App.batch_update``, which holds off the screen
        layout until it is done. Without it every awaited mount hands control
        back to the event loop, Textual's screen timer fires, and the ENTIRE
        widget tree is re-arranged — 78 to 104 full layout passes on a
        200-message reload, each over a tree that is still growing. Batched it is
        5, and the reader sees the finished transcript rather than it being
        assembled a message at a time.
        """
        self._reload_source = messages
        start = self.render_cap_start(messages) if cap else 0
        # Cleared FIRST: clear_messages resets the elided count, so setting it
        # before this would hand the row a zero.
        await self.clear_messages()
        # System messages never render, so counting them in "N earlier" would
        # claim more is hidden than a reader could ever get back.
        self._elided = sum(1 for m in messages[:start] if m.get("role", "") != "system")
        with self.app.batch_update():
            if self._elided:
                await self.mount(self._earlier_row())
            n = len(messages)
            i = start
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

    @property
    def elided_count(self) -> int:
        """How many messages the last reload declined to mount. 0 when all are on."""
        return self._elided

    def _earlier_row(self) -> Static:
        """The ``⋯ N earlier`` row that stands where the elided messages would be.

        A count rather than a blank gap, matching :class:`TreeDetailPane`'s row,
        because a gap does not say anything and this does. It names its own
        gesture: the row is clickable, and the same action is in the command
        palette, so neither the mouse nor the keyboard is a dead end.
        """
        row = Static(f"⋯ {self._elided} earlier · click to show them", classes="chat-fold")
        row.tooltip = "Mount the whole conversation. On a long one this takes a while."
        return row

    async def on_click(self, event: events.Click) -> None:
        """Show the whole transcript when the reader clicks the ``⋯`` row."""
        widget = getattr(event, "widget", None)
        if widget is not None and widget.has_class("chat-fold"):
            await self.show_all_messages()

    async def show_all_messages(self) -> None:
        """Re-render the last reloaded transcript with no cap.

        A no-op when nothing was elided, so the palette entry is safe to invoke
        at any time. It is deliberately not cheap: mounting the rest costs the
        same quadratic layout the cap avoided, which is why it is a gesture the
        reader asks for rather than something scrolling triggers.
        """
        if not self._elided:
            return
        await self.reload_messages(self._reload_source, cap=False)

    async def _reload_exchange(self, span: list[dict]) -> None:
        """Rebuild one user→answer span (assistant + toolResult messages) as a
        collapsed exchange, then close it out exactly like the live path."""
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
                    # The call always precedes its result on disk; a missing box
                    # means a dangling id — surface it, don't fabricate one.
                    self.app.log(f"reload: toolResult for unknown tool_call_id {tc_id!r}")
            else:
                # Unexpected role inside an answer span — render flat rather than
                # drop it (add_persisted_message raises on a bad content shape).
                self.add_persisted_message(msg)
        await self._close_exchange(exchange, context=context, output=output, seconds=None)


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
        # NOT bound straight to `quit` any more: one mistimed press ended a session
        # with a draft in the input and no warning. See `action_interrupt` for the
        # four steps. `priority` so it reaches this action while the ChatInput has
        # focus, which is where it is pressed.
        # `show=False` keeps the Footer exactly as it was. Textual's own system
        # `ctrl+c` binding is `show=False` and used to shadow this one; making this
        # `priority` put `^c Quit` in the footer for the first time, costing ten
        # columns to advertise the most widely known key in a terminal. The first
        # press now says what the second one will do, which is the affordance.
        Binding("ctrl+c", "interrupt", "Quit", priority=True, show=False),
        # priority=True: caught during generation regardless of which widget holds
        # focus. Idle, it offers the tree browser on a second press.
        Binding("escape", "escape", "Cancel", show=False, priority=True),
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
        # The "press it again" offers currently standing, action name -> the timer
        # that will withdraw one. A dict rather than two attributes because both
        # keys that use it (`ctrl+C` to exit, `Esc` to open the tree) write the
        # SAME status bar, and only one offer can be readable there at a time —
        # see :meth:`_offer_again`.
        self._pending_confirm: dict[str, Timer] = {}
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
        # ``--max-turns`` (the turn ceiling): run-level like the flags above. Its
        # ``None`` is meaningful and is NOT coerced — it means "the flag was
        # absent", which lets ``_apply_run_config`` fall through to config.json's
        # top-level ``max_turns`` and then to no ceiling at all.
        self._max_turns: Optional[int] = run_config.get("max_turns")
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

        # === Colour theme (docs/PLAN-0.9.4.md §6) ===
        # Registered and applied HERE, in ``__init__``, and not in ``on_mount``:
        # ``App.__init__`` has already built ``self.stylesheet`` from
        # ``get_css_variables()``, and the first parse of ``parley.tcss`` happens
        # before ``on_mount`` runs. Every colour in that sheet is a ``$tau-*``
        # variable a theme supplies, so a theme applied at mount time would be a
        # sheet parsed against variables that do not exist yet. ``_apply_theme``
        # re-seeds the stylesheet's variable table for exactly that reason.
        #
        # A theme that cannot be loaded does not stop τ from starting. Each
        # failure lands in ``_theme_errors`` and ``on_mount`` raises it as an
        # error toast, and the app runs in the default theme — so the problem is
        # reported on the one screen the user is looking at, without a broken file
        # for a theme they are not even selecting taking the whole TUI down.
        # ``themes.py``'s module docstring has the Fail-Early reasoning.
        self._theme_errors: list[str] = []
        self._theme_registry = build_theme_registry(errors=self._theme_errors)
        self._apply_theme(self._configured_theme_name())

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
        # ``--theme`` (docs/PLAN-0.9.4.md §6). It rides the same in-memory config
        # the other overrides do, which is exactly what makes it a ONE-RUN choice:
        # ``action_set_theme``'s ``update_config`` re-reads the file rather than
        # writing ``self.config`` back, so switching themes in a ``--theme latte``
        # session saves the theme the user picked and not the one the flag set.
        if THEME_CONFIG_KEY in overrides:
            self.config[THEME_CONFIG_KEY] = overrides[THEME_CONFIG_KEY]

    def load_config(self):
        """Load ``~/.tau/config.json``, creating it from the packaged template if absent.

        Delegates to the single reader in ``config.py``. The TUI used to carry its
        own hardcoded default here, which disagreed with the packaged
        ``tau_default_config.json`` — so the file a first-run user actually got was
        not the one we maintain.
        """
        self.config = bootstrap_config()
        self.log(f"Loaded config with {len(self.config.get('models', {}))} models")

    # ------------------------------------------------------------------
    # Colour themes (docs/PLAN-0.9.4.md §6)
    # ------------------------------------------------------------------

    def _configured_theme_name(self) -> str:
        """The theme this run asks for — ``--theme``, else config.json, else the default.

        Absent means "no preference", which resolves to
        :data:`~tau_coding_agent.themes.DEFAULT_THEME_NAME`.

        *Present and wrong* — a name nothing answers to, or a non-string where a
        name belongs — records the reason in :attr:`_theme_errors` and also
        resolves to the default. The two outcomes look the same on screen for
        about a second, and then ``on_mount``'s toast says which one happened.
        That toast is the whole reason this can return a name instead of raising:
        without it, "mocah" would silently render as mocha and the user would have
        no way to tell a typo from a theme that looks like the default.

        ``--theme`` reaches here through ``self.config`` because
        ``_apply_cli_overrides`` wrote it there. That is a change to the config
        *in memory* only; ``update_config`` re-reads the file, so a one-run
        override cannot ride into the saved config on the back of a later swap.
        """
        configured = self.config.get(THEME_CONFIG_KEY)
        if configured is not None and not isinstance(configured, str):
            self._theme_errors.append(
                f"config key {THEME_CONFIG_KEY!r} must be a theme name (a string), "
                f"got {type(configured).__name__}"
            )
            return DEFAULT_THEME_NAME
        try:
            return resolve_theme(configured, self._theme_registry).name
        except ThemeError as exc:
            self._theme_errors.append(str(exc))
            return DEFAULT_THEME_NAME

    def _apply_theme(self, name: str) -> None:
        """Make *name* the live theme, at construction time or mid-session.

        Delegates to :func:`~tau_coding_agent.themes.install_themes`, which is
        also what the bare-``App`` harnesses that load ``parley.tcss`` outside
        this class call — one implementation of "make this app wear this theme",
        so a harness cannot drift into a half-registered palette.

        Passing ``self._theme_registry`` rather than letting the helper rebuild
        one matters: rebuilding re-reads ``~/.tau/themes`` from disk, so a
        mid-session swap would silently pick up a file added since startup and
        the palette listing (built from the stored registry) would disagree with
        what a swap can reach.
        """
        install_themes(self, name, registry=self._theme_registry)

    def action_set_theme(self, name: str) -> None:
        """Switch themes in-session and remember the choice.

        The second of the two surfaces §6 asked for ("selectable / swappable"):
        the config key is the standing setting, this is the live swap. It
        **persists** — a colour scheme picked once and gone at the next launch is
        a worse answer than one that sticks, and the gesture that undoes it is the
        same gesture that did it. The saving itself lives in :meth:`watch_theme`,
        which is on the far side of ``app.theme``, so Textual's own theme palette
        gets it too.

        An unknown name here cannot come from the palette, which is built from the
        registry — it comes from ``run_action`` with a name typed by hand. It
        reports and changes nothing: the app already wears a theme that works, and
        the startup rule ("fall back to the default") would be the wrong answer
        for a swap, because it would take the colours away from a user who asked
        for a different set and mistyped.
        """
        try:
            theme = resolve_theme(name, self._theme_registry)
        except ThemeError as exc:
            self.notify(str(exc), title="Theme", severity="error", timeout=10)
            return
        self._apply_theme(theme.name)  # watch_theme saves it
        self.notify(f"Theme: {theme.name}")

    def watch_theme(self, theme_name: str) -> None:
        """Remember whichever theme became live, however it became live.

        ``action_set_theme`` is not the only way in. Textual's own "Theme" system
        command opens a second palette over ``App.available_themes`` and assigns
        ``app.theme`` directly, and every theme there is now selectable
        (``themes.textual_themes`` gives Textual's 21 the ``$tau-*`` palette
        ``parley.tcss`` needs). Persisting from the action alone would mean two
        theme lists in one palette where one sticks and one is forgotten at the
        next launch, which is worse than either behaviour on its own.

        Two conditions keep this from writing when nothing was chosen. Before the
        app is running the only assignment is ``__init__``'s, which is applying
        what config.json already says. And a name that matches the in-memory
        config is a no-op, so a ``--theme`` override is not written to disk unless
        the user picks something else — ``update_config``'s read-modify-write is
        what keeps the rest of a one-run override out of the file, and this is the
        same rule for this key.
        """
        if not self.is_running:
            return
        if self.config.get(THEME_CONFIG_KEY) == theme_name:
            return
        # Keep the in-memory config in step with the file, so a later read of
        # ``self.config`` sees what disk says.
        self.config[THEME_CONFIG_KEY] = theme_name
        update_config(THEME_CONFIG_KEY, theme_name)

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

        # Theme load failures collected in __init__ (docs/PLAN-0.9.4.md §6).
        # Reported here rather than there because ``notify`` needs a screen: the
        # toasts are mounted on it, and __init__ runs before there is one. One
        # toast per failure, each naming its own file, because two broken themes
        # are two things to fix. The timeout is long: this is the only notice the
        # user gets that the colours they are looking at are not the ones they
        # asked for.
        for message in self._theme_errors:
            self.notify(message, title="Theme", severity="error", timeout=10)

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

    # -- "press it again": two keys that ask before doing something big -------

    #: How long a standing "press it again" offer lasts. Long enough to read the
    #: status bar and act, short enough that an unrelated later press of the same
    #: key is a fresh first press rather than a confirmation of something the
    #: reader has forgotten about.
    CONFIRM_SECONDS = 3.0

    def _offer_again(self, name: str, message: str) -> bool:
        """``True`` when ``name``'s offer was already standing — so act now.

        The other half of the answer is the side effect: on a first press this
        writes ``message`` into the status bar and starts the clock. So a caller
        reads it as "has the reader already been told what this does, and said
        yes by pressing again?".

        A press of one key withdraws the OTHER key's offer. There is one status
        bar, and an offer nobody can see any longer is one that must not still be
        answerable — a reader who presses Esc and then Ctrl+C should get Ctrl+C's
        warning, not an exit.
        """
        standing = self._pending_confirm.pop(name, None)
        if standing is not None:
            standing.stop()
            self._withdraw_offers()
            return True
        self._withdraw_offers()
        self.sub_title = message
        self._pending_confirm[name] = self.set_timer(
            self.CONFIRM_SECONDS, lambda: self._withdraw_offer(name)
        )
        return False

    def _withdraw_offer(self, name: str) -> None:
        """Time is up for ``name``'s offer: forget it and put the subtitle back."""
        timer = self._pending_confirm.pop(name, None)
        if timer is not None:
            timer.stop()
        self._restore_subtitle()

    def _withdraw_offers(self) -> None:
        for name in list(self._pending_confirm):
            self._withdraw_offer(name)

    def _restore_subtitle(self) -> None:
        """Undo whatever an offer wrote. ``_refresh_subtitle`` returns early with no
        session, which would leave the offer's text standing — so say nothing
        instead, which is what the header shows before a session exists."""
        if self.current_session is None:
            self.sub_title = ""
            return
        self._refresh_subtitle()

    def action_interrupt(self) -> None:
        """``ctrl+C``, in four steps from "stop that" to "quit".

        1. Generating — abort the turn. Same as Esc; the key a terminal user
           reaches for to stop a runaway process should stop the runaway process.
        2. Something typed — clear the input. The draft is thrown away, which is
           what ``ctrl+C`` means at a shell prompt.
        3. Nothing typed, nothing offered — offer the exit and say so.
        4. Nothing typed, the offer standing — quit.

        Steps 3 and 4 are the point: ``ctrl+C`` used to be bound straight to
        ``quit``, so one mistimed keypress ended a session with unsaved input and
        no warning.
        """
        if self.is_generating:
            self.action_cancel_generation()
            return
        editor = self.query_one("#chat-input", ChatInput)
        if editor.text:
            editor.text = ""
            self._withdraw_offers()
            return
        if self._offer_again("exit", "press ctrl+C again to exit"):
            self.exit()

    def action_escape(self) -> None:
        """``Esc``: stop the turn if one is running, else offer the tree browser.

        Esc has meant "cancel this response" since the beginning and still does —
        that is checked first, and nothing about it changes. What it used to mean
        when nothing was generating is *nothing at all*: the binding is
        ``priority=True`` so the key was consumed, the action no-op'd, and the
        reader got no feedback of any kind.

        It now offers the tree, on the second press. Two presses rather than one
        because Esc is also the key people hit to mean "never mind", and opening a
        full-screen modal on that is worse than doing nothing was.
        """
        if self.is_generating:
            self.action_cancel_generation()
            return
        if self._offer_again("tree", "press Esc again to view the tree"):
            self.action_browse_tree()

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
        if action in ("rollback_turn", "escape", "interrupt") and len(self.screen_stack) > 1:
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
                context=int(event.get("context", 0) or 0),
                output=int(event.get("output", 0) or 0),
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
        # The system prompt follows reasoning_replay's rule — a model entry that
        # names its own wins, else the top-level default — and lands on the ENTRY
        # so ``TauBackend`` receives it as ``custom_prompt`` and composes it with
        # the project context files and the tool list. It used to be written
        # straight into the session's first message instead, which took
        # precedence over the built prompt and threw that composition away.
        base_prompt = model_config.get("system_prompt") or self.config.get("system_prompt")
        inject_prompt = bool(base_prompt) and "system_prompt" not in model_config
        # The turn ceiling resolves like ``reasoning_replay``, with ``--max-turns``
        # ahead of both: the flag, else the entry's own key, else the top-level
        # config default, else nothing — and nothing means no ceiling, which is
        # ``AgentLoopConfig``'s default and pi's behaviour.
        global_max_turns = self.config.get("max_turns")
        resolved_max_turns: Optional[int] = None
        if self._max_turns is not None:
            resolved_max_turns = self._max_turns
        elif global_max_turns is not None and "max_turns" not in model_config:
            resolved_max_turns = int(global_max_turns)
        if not (
            self._exclude_tools
            or self._no_tools
            or self._tool_allowlist is not None
            or inject_replay
            or inject_prompt
            or self._append_system_prompt
            or self._bus_available
            or self._no_context_files
            or resolved_max_turns is not None
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
        if inject_prompt:
            mc["system_prompt"] = base_prompt
        if self._append_system_prompt:
            # Carried as its own key rather than folded in here: ``TauBackend``
            # applies it to whichever base text it resolves, so the sections
            # augment the base rather than the composed whole, and one code path
            # decides that for every frontend.
            mc["append_system_prompt"] = list(self._append_system_prompt)
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
        if resolved_max_turns is not None:
            mc["max_turns"] = resolved_max_turns
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
        #
        # The stored message is the prompt the backend BUILT — base text, project
        # context files, tool list — because that message is what goes on the
        # wire: it takes precedence over ``AgentSession``'s own prompt, so
        # anything composed and not stored here is silently discarded. This line
        # used to compose its own string from ``config["system_prompt"]``, with
        # ``"You are a helpful assistant."`` when the key was absent. That
        # fallback meant the message was NEVER absent, so on the TUI path the
        # built prompt was never used: no AGENTS.md context and no
        # ``Available tools:`` list ever reached a model, and a default install
        # was told it was a helpful assistant rather than a coding agent.
        # ``--append-system-prompt`` and a configured prompt now ride the model
        # entry into that build (see ``_apply_run_config``); a resumed session
        # keeps its own stored prompt, unchanged.
        system_prompt = getattr(self.current_backend, "system_prompt", "") or ""
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

    async def action_show_all_messages(self) -> None:
        """Mount the messages a capped reload left off screen.

        Says so when there is nothing to show, rather than looking broken. The
        conversation was never truncated — :meth:`ChatDisplay.reload_messages`
        bounds what is MOUNTED, and this lifts that bound for the current view.
        """
        display = self.query_one(ChatDisplay)
        elided = display.elided_count
        if not elided:
            self.notify("The whole conversation is already on screen.")
            return
        self.notify(f"Mounting {elided} earlier messages…")
        await display.show_all_messages()

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
        """Conversation-level rollup: tool calls, cumulative usage, current context.

        Follows pi's footer (``modes/interactive/components/footer.ts``): the
        cumulative counts are broken out per direction — ``↑`` uncached input,
        ``↓`` output, ``R`` cache reads, ``W`` cache writes — and the context size
        is a SEPARATE, non-cumulative number.

        This replaced a single ``Σ total_tokens`` over every assistant message.
        That sum was quadratic in conversation length: each completion's
        ``total_tokens`` includes its whole prompt, and each prompt contains every
        earlier turn, so an N-turn conversation counted turn 1 N times. On a real
        17-message session it read 192.9k for a 22.6k-token conversation that had
        generated 10.5k tokens.

        Derived purely from the transcript, so it reads identically for a live
        session and a reloaded one. Wall-clock time is intentionally absent — it
        is not persisted per completion, and we don't fabricate it (Fail-Early).
        Returns an empty string when there's nothing to roll up yet."""
        assistants = [m for m in messages if m.get("role") == "assistant"]
        tools = sum(
            1
            for m in assistants
            for b in (m.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "toolCall"
        )
        usages = [u for m in assistants if isinstance(u := m.get("usage"), dict)]
        totals = {
            key: sum(int(u.get(key, 0) or 0) for u in usages)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        }
        # The LAST completion's prompt is the conversation's current size. Not a
        # sum — the prompts nest, so summing them counts the same text repeatedly.
        context = prompt_tokens(usages[-1]) if usages else 0
        parts = [f"{tools} tool" + ("" if tools == 1 else "s")] if tools else []
        # Each arrow is dropped when its count is 0 (pi does the same): a provider
        # that reports no caching should not show an empty R/W, and a fresh chat
        # should not show a row of zeroes. The four share one ``·`` part — they are
        # one reading of the same meter, not four separate stats.
        arrows = " ".join(
            f"{prefix}{format_tokens(totals[key])}"
            for prefix, key in (
                ("↑", "input_tokens"),
                ("↓", "output_tokens"),
                ("R", "cache_read_tokens"),
                ("W", "cache_write_tokens"),
            )
            if totals[key]
        )
        if arrows:
            parts.append(arrows)
        if context:
            parts.append(f"{format_tokens(context)} ctx")
        return " · ".join(parts)

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

        # The keyboard half of the "⋯ N earlier" row's gesture (the row itself is
        # clickable). Listed unconditionally, like the rollback entry above and
        # for the same reason: an entry that appears only sometimes is harder to
        # find than one that explains it had nothing to do.
        yield SystemCommand(
            "Show earlier messages",
            "Mount the whole conversation, not just the last few turns. Slow on a long one.",
            self.action_show_all_messages,
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

        # Themes (docs/PLAN-0.9.4.md §6). One entry per theme rather than one
        # "Theme…" entry opening a second chooser: the palette IS a chooser and
        # it already filters by substring, so 24 entries sharing a "Theme: "
        # prefix cost one word of typing rather than a second screen.
        # The active one is marked rather than hidden — an entry that disappears
        # when you pick it makes the list a different length every time you open
        # it, and "which one am I on" is the question a theme list is asked most.
        #
        # The list is the whole registry, which since ``themes.textual_themes``
        # includes Textual's own 21 themes adapted to τ's palette. Textual's
        # separate "Theme" command opens a second palette over the same set;
        # these entries save a keystroke and report the switch by toast, and
        # ``watch_theme`` makes both routes persist the choice.
        #
        # ``run_action`` rather than a bound lambda, so the palette entry, a
        # keybinding and ``app.run_action("set_theme('latte')")`` from a test all
        # land on the same one action.
        for theme_name in sorted(self._theme_registry):
            active = " (active)" if theme_name == self.theme else ""
            yield SystemCommand(
                f"Theme: {theme_name}{active}",
                f"Switch to the {theme_name} colours and save the choice to config.json",
                lambda t=theme_name: self.run_action(f'set_theme("{t}")'),
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

        # The modal returns an INTENT, not a bare id (§5.3 / §11.1). ``sole_id`` is
        # the node the reader pointed at under either action, and it raises rather
        # than silently taking ids[0] if a later gesture starts answering with a set.
        intent = await self.push_screen_wait(SessionTreeModal(tree))
        if intent is None:
            return
        if intent.action == "elide":
            # Both ends already, and already checked against the same rules
            # ``elide_span`` will apply (``SessionTreeModal._elide_plan``). No mode
            # chooser: "elide" was never one of the three branch modes, and the
            # second browse it used to need is the key that produced this intent.
            anchor_id, first_kept_id = intent.ids
            await self._elide_span_flow(session, anchor_id, first_kept_id)
            return
        picked_id = intent.sole_id
        # ``revise``: the reader named a USER message, which means fork from its
        # PARENT and hand the old text back to edit (PLAN-0.9.4 §4, item 2).
        # Navigating to the message itself would make the next turn its child —
        # two user turns in a row, the one shape a conversation cannot have.
        prefill: Optional[str] = None
        target_id = picked_id
        if intent.action == "revise":
            revised = tree.entry(picked_id)
            parent_id = revised.get("parentId")
            if parent_id is None:
                # The first message in the session has nothing to fork FROM. Say
                # so rather than silently falling back to navigating onto it,
                # which is the gesture this action exists to stop doing.
                self.notify(
                    "That is the first message — there is no earlier point to fork from.",
                    severity="warning",
                )
                return
            target_id = str(parent_id)
            prefill = tree.message_text(picked_id)

        mode = await self.push_screen_wait(TreeModeModal())
        if mode is None:
            return

        if target_id == session.cursor:
            # Checked AFTER the mode pick, not before: it is a statement about
            # *branching*, which is what all three remaining modes do. The elide
            # returned above without reaching it — an anchor that is already the
            # cursor is the NORMAL elide ("fold the history behind where I am and
            # keep going"), not a no-op.
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
        if prefill is None:
            self.notify(
                "Summarized and moved to selected node" if summarize else "Moved to selected node"
            )
            return
        # ``revise``: the old text goes back in the input, selected end-first so
        # typing replaces it and the cursor is where an edit starts. Written AFTER
        # the navigation, not before, so a failed one leaves the input alone rather
        # than staging a message against a conversation that did not move.
        editor = self.query_one("#chat-input", ChatInput)
        editor.text = prefill
        editor.move_cursor(editor.document.end)
        editor.focus()
        self.notify("Forked from before that message — edit it and send.")

    async def _elide_span_flow(
        self,
        session: ConversationSession,
        anchor_id: str,
        first_kept_id: str,
    ) -> None:
        """Perform the elide the browser worked out, and re-render (W3).

        ``anchor_id`` is where the fold jumps FROM (the elide entry is appended
        under it, and the conversation continues there); ``first_kept_id`` is
        where it jumps TO — the oldest entry the fold keeps. Everything on the
        anchor's path before it leaves the context.

        **Both ids arrive together now.** This used to take the anchor plus the
        whole ``ConversationTree`` and open a SECOND full-screen browser to ask
        for the resume point, with a different caption. That is gone: the pair is
        two nodes of one line and the browser can name both of them without
        closing (``ctrl+E``), which also means an illegal pair is refused while
        the reader can still see the tree they picked it from. The rejected
        design and why it looked reasonable are in PLAN-0.9.4 §4.

        Still split out rather than inlined, and still validating on the backend
        side as well: this is called from :meth:`action_browse_tree`'s worker with
        a pair the modal checked against a tree it built at open time, and the
        session is live. ``elide_span`` checks before it writes, so a pair that
        went stale is refused rather than half-applied, and the failure is
        surfaced through ``notify(severity="error")`` like every other mode's.
        """
        elide_span = getattr(self.current_backend, "elide_span", None)
        if elide_span is None:
            self.notify("This backend does not support eliding", severity="warning")
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
        """Edit the system prompt.

        The editor opens on τ's own base text when the config names no prompt,
        not on a stand-in: this key REPLACES that text, so seeding the editor
        with anything else would offer the user a starting point their agent has
        never actually been given. What the editor shows is the base text alone —
        the project context files and the tool list compose around it and are not
        the user's to edit here.
        """
        current_prompt = self.config.get("system_prompt") or BASE_SYSTEM_PROMPT

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
