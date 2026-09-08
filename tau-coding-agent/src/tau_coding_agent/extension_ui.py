"""extension_ui — split out of app.py."""

from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional
from textual import events
from textual.containers import VerticalScroll, Horizontal, Vertical
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static, Button
from rich import box
from rich.console import RenderableType
from rich.markup import escape
from rich.table import Table
from tau_agent_core.extension_locks import ExtensionRequest
from tau_coding_agent import modals

if TYPE_CHECKING:
    from tau_coding_agent.app import TauApp


class _ExtensionUIDelegate:
    """Paints a loaded extension's ``api.notify(...)`` onto the live TUI (E5 §4 / S33).

    Bound onto every extension's shared ``ExtensionContext`` via
    ``TauBackend.set_ui_delegate`` → ``AgentSession.set_ui_delegate`` after each
    ``create_backend``, so ``api.ui.notify(msg, level)`` reaches the Textual screen
    instead of the headless stderr sink. Extension hooks run on the app's event
    loop (the generation worker is async, not threaded), so ``App.notify`` is
    called directly.

    Four methods, which is the whole delegate protocol since
    docs/EXTENSION-LOCKS.md §8.2 removed ``confirm``/``select``/``input``: three
    that paint (``notify``, ``set_status``, ``panel``) and one that blocks
    (``form``, pushed via ``push_screen_wait`` so a flow can collect a missing
    argument). An extension that wants to ASK a human declares a request instead
    — ``api.request_user_action`` — which every head renders and which does not
    hold the turn lock while nobody answers.
    """

    #: extension notify level → Textual ``App.notify`` severity.
    _SEVERITY: dict[str, Literal["information", "warning", "error"]] = {
        "info": "information",
        "warning": "warning",
        "error": "error",
    }

    def __init__(self, app: "TauApp") -> None:
        self._app = app

    def notify(self, message: str, level: str = "info") -> None:
        self._app.notify(message, severity=self._SEVERITY.get(level, "information"))

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        return await self._app.push_screen_wait(modals.ExtensionFormScreen(spec))

    def set_status(self, key: str, text: str | None) -> None:
        self._app.set_extension_status(key, text)

    def panel(self, key: str, spec: dict[str, Any] | None) -> None:
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
            self.display = False
            self.update("")


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
        yield Static(self._spec["title"], classes="ext-panel-title", markup=False)
        yield Static(render_panel_body(self._spec["body"]), classes="ext-panel-body", markup=False)
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
            event.stop()
            self.post_message(self.Action(button.command, button.args))


class _AskActionButton(Button):
    """An ask action button carrying the action label it answers with.

    The same reason :class:`_PanelActionButton` subclasses ``Button``: the press
    maps to a declared action without id-parsing, and an ordinary ``Button`` in
    the same dialog is not mistaken for one.
    """

    def __init__(self, label: str, index: int) -> None:
        super().__init__(label, variant="primary", id=f"ext-ask-action-{index}")
        self.action_label = label


class ExtensionAskScreen(modals._FieldForm[Optional[tuple[str, dict]]]):
    """An extension's ask, as a modal (docs/EXTENSION-LOCKS.md §8, §9).

    The panel shape with fields: τ's framing label as the title, the extension's
    own sentence, an optional body rendered by :func:`render_panel_body`, the
    fields, and one button per declared action. Pressing an action dismisses with
    ``(action_label, values)``, which ``TauApp`` hands to
    :meth:`~tau_agent_core.agent_session.AgentSession.answer_request`.

    Dismissable, always — ``Esc`` returns ``None`` and changes nothing, because a
    lock is held by the tree rather than by this screen, and the transcript row
    brings it back. A dialog that could not be closed would be a second, worse
    copy of the lock (§9).
    """

    DIALOG_ID: ClassVar[str] = "ext-ask-dialog"

    def __init__(self, request: ExtensionRequest) -> None:
        if request.ask is None:
            raise ValueError("ExtensionAskScreen: this request carries no ask to render")
        self._request = request
        self._ask = request.ask
        self._fields = list(self._ask["fields"])
        super().__init__(request.label)

    def compose_body(self) -> ComposeResult:
        yield Static(self._request.sentence, classes="tau-dialog-help", markup=False)
        with VerticalScroll(id="ext-ask-body"):
            if self._ask["body"] is not None:
                yield Static(
                    render_panel_body(self._ask["body"]), classes="ext-ask-panel", markup=False
                )
            for index, field in enumerate(self._fields):
                yield from self._compose_field(index, field)
        hint = "Esc: dismiss, the row brings it back"
        if self._request.release:
            hint = f"/{self._request.release} also clears it    {hint}"
        yield Static(hint, classes="tau-dialog-hint", markup=False)
        with Horizontal(id="ext-ask-buttons", classes="tau-dialog-buttons"):
            for index, action in enumerate(self._ask["actions"]):
                yield _AskActionButton(action["label"], index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if isinstance(button, _AskActionButton):
            event.stop()
            self.dismiss((button.action_label, self._collect()))


class ExtensionRequestBox(Static):
    """The transcript row for the extension request under the cursor (§9).

    A ``customEntry`` contributes no message, and the transcript is built over
    messages, so this is mounted beside them rather than rendered from one — see
    :meth:`~tau_coding_agent.transcript.MessageList.set_extension_request`. Only
    the request AT THE CURSOR is drawn, which is every request a user can act on:
    a lock is read at the cursor and only there (§2), so one buried mid-path is
    inert and belongs in the tree browser rather than at the end of the chat.

    Clicking it posts :class:`Reopen`, which is how a dismissed ask comes back.
    """

    class Reopen(Message):
        """The request row was clicked → re-open its ask (§9)."""

        def __init__(self, request: ExtensionRequest) -> None:
            self.request = request
            super().__init__()

    def __init__(self, request: ExtensionRequest) -> None:
        self._request = request
        hint = ""
        if request.ask is not None:
            hint = "click to answer"
        elif request.release:
            hint = f"/{request.release} clears it"
        #: The row's plain text, kept because Textual's ``Static`` does not read it back.
        self.body_text = "\n".join(p for p in (request.label, request.sentence, hint) if p)
        # The sentence is the extension's; escaped, or Rich eats every [bracketed] word.
        markup = f"[b]{escape(request.label)}[/b]\n{escape(request.sentence)}"
        if hint:
            markup += f"\n[dim]{escape(hint)}[/dim]"
        # No id: Textual defers remove(), so a replacement row would collide with one.
        super().__init__(markup, classes="ext-request-row")
        self.add_class("ext-request-locked" if request.lock else "ext-request-open")

    def on_click(self, event: events.Click) -> None:
        if self._request.ask is not None:
            event.stop()
            self.post_message(self.Reopen(self._request))


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

    def __init__(self) -> None:
        super().__init__(id="ext-panel-host")
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
            self.call_later(existing.update_spec, spec)
        else:
            panel = ExtensionPanel(key, spec)
            self._panels[key] = panel
            self.mount(panel)
