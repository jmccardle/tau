"""The TUI as a live suspension responder: the modal, and the binding that keeps it current.

docs/STEP-DEBUGGER.md §3.5 (the TUI row) and §3.8. The pending list is read from
:meth:`~tau_agent_core.agent_session.AgentSession.pending_suspensions` on every
``suspension_open``/``suspension_closed``; the events only say when to look.
"""

import json
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static

from tau_agent_core.events import AgentEvent
from tau_coding_agent import modals
from tau_coding_agent.extension_ui import render_panel_body

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession
    from textual.app import App

RESPONDER_NAME = "tui"
"""What the TUI registers as with ``AgentSession.add_suspension_responder``."""

ANSWERED_BY: dict[str, Any] = {"client": "tui"}
"""Recorded on a resolution the TUI's modal sent."""

ABORT_LABEL = "abort"
"""The action label core treats as an abort of the turn (``suspensions.ABORT_ACTION``)."""

Answer = Callable[[str, str, dict[str, Any]], Optional[str]]
"""Sends ``(suspension_id, action, values)``; returns the refusal's text, or ``None`` when sent."""


def pending_states(session: "AgentSession") -> list[dict[str, Any]]:
    """Every pending suspension in ``session``, oldest first: the TUI's one read of the state.

    Args:
        session: The session the TUI answers for, every cursor included.

    Returns:
        Each ``SuspensionState``.
    """
    return list(session.pending_suspensions(scope="session"))


def site_line(state: dict[str, Any]) -> str:
    """One line naming who suspended the turn, where, and on which cursor.

    Args:
        state: A ``SuspensionState``.

    Returns:
        E.g. ``"80_step_mode at tool_call · cursor c1"``, with the depth of a child cursor.
    """
    parts = [f"{state['extension']} at {state['site']}", f"cursor {state['cursor_id']}"]
    if state.get("depth"):
        parts.append(f"child depth {state['depth']}")
    if state.get("entry_id"):
        parts.append(f"after entry {state['entry_id']}")
    return " · ".join(parts)


def call_lines(call: dict[str, Any] | None) -> str | None:
    """The tool call a ``tool_call`` suspension stopped before, as its id and arguments.

    Args:
        call: ``{id, effective_args}``, or ``None`` at any other site.

    Returns:
        The text, or ``None`` with no call.
    """
    if call is None:
        return None
    args = json.dumps(call.get("effective_args"), indent=2, sort_keys=True, default=str)
    return f"call {call.get('id')}\n{args}"


class _SuspensionActionButton(Button):
    """A button carrying the spec action label it answers with."""

    def __init__(self, label: str, index: int) -> None:
        super().__init__(label, variant="primary", id=f"suspension-action-{index}")
        self.action_label = label


class SuspensionScreen(modals._FieldForm[None]):
    """One suspension as a modal: spec, site, call, evidence, fields, and its actions.

    It never closes on ``Esc`` (§3.5): only ``suspension_closed`` dismisses it,
    through :meth:`close`, so an answer refused by core leaves the form up with
    what was typed. With no ``abort`` action in the spec, an ``Abort turn``
    button aborts the turn the way ``Esc`` does on the main screen. A queued
    suspension shows with its buttons disabled until it reaches the front.

    Args:
        state: The ``SuspensionState`` to render.
        answer: Sends an answer; see :data:`Answer`.
        abort_turn: Aborts the suspended turn.
    """

    DIALOG_ID: ClassVar[str] = "suspension-dialog"
    BODY_ID: ClassVar[Optional[str]] = "suspension-body"

    def __init__(
        self, state: dict[str, Any], answer: Answer, abort_turn: Callable[[], None]
    ) -> None:
        self.state = state
        self._spec = state["spec"]
        self._fields = list(self._spec["fields"])
        self._answer_with = answer
        self._abort_turn = abort_turn
        self._queue: list[dict[str, Any]] = []
        self._suspension_closed = False
        self._sent = False
        super().__init__(self._spec["title"])

    @property
    def suspension_id(self) -> str:
        """The suspension this screen renders."""
        return str(self.state["id"])

    @property
    def answerable(self) -> bool:
        """Whether the action buttons may send now: at the front, unanswered, still open."""
        return not (self.state["queued"] or self._sent or self._suspension_closed)

    def compose_header(self) -> ComposeResult:
        yield Static(
            site_line(self.state), id="suspension-site", classes="suspension-site", markup=False
        )

    def compose_body(self) -> ComposeResult:
        if self._spec["body"] is not None:
            yield Static(
                render_panel_body(self._spec["body"]), classes="suspension-text", markup=False
            )
        call = call_lines(self.state.get("call"))
        if call is not None:
            yield Static(call, id="suspension-call", classes="suspension-call", markup=False)
        for item in self._spec["evidence"]:
            yield Static(
                f"[b]{escape(item['label'])}[/b]\n{escape(item['text'])}",
                classes="suspension-evidence",
            )
        for index, field in enumerate(self._fields):
            yield from self._compose_field(index, field)

    def compose_footer(self) -> ComposeResult:
        yield Static("", id="suspension-queued", classes="suspension-queued", markup=False)
        yield Static("", id="suspension-queue", classes="suspension-queue", markup=False)
        yield Static(
            "Esc does not dismiss: choose an action", classes="tau-dialog-hint", markup=False
        )
        with Horizontal(id="suspension-buttons", classes="tau-dialog-buttons suspension-buttons"):
            for index, action in enumerate(self._spec["actions"]):
                yield _SuspensionActionButton(action["label"], index)
            if not any(a["label"] == ABORT_LABEL for a in self._spec["actions"]):
                yield Button("Abort turn", variant="error", id="suspension-abort-turn")

    def on_mount(self) -> None:
        self._show_state()

    def set_state(self, state: dict[str, Any], queue: list[dict[str, Any]]) -> None:
        """Take a newer reading of this suspension and the ones queued behind it.

        Args:
            state: This suspension's ``SuspensionState`` now.
            queue: Every other pending suspension, oldest first.
        """
        self.state = state
        self._queue = queue
        if self.is_mounted:
            self._show_state()

    def _show_state(self) -> None:
        """Enable or disable the buttons and redraw the queued notice and the queue list."""
        queued = self.query_one("#suspension-queued", Static)
        queued.display = bool(self.state["queued"])
        queued.update(
            f"Queued behind another suspension on cursor {self.state['cursor_id']}: "
            "not answerable until it reaches the front"
        )
        others = self._queue
        listing = self.query_one("#suspension-queue", Static)
        listing.display = bool(others)
        listing.update(
            "Also pending:\n"
            + "\n".join(
                f"  {'queued' if s['queued'] else 'open'}: {s['spec']['title']} "
                f"(cursor {s['cursor_id']})"
                for s in others
            )
        )
        for button in self.query(_SuspensionActionButton):
            button.disabled = not self.answerable
        for abort in self.query("#suspension-abort-turn").results(Button):
            abort.disabled = self._suspension_closed

    def close(self) -> None:
        """The suspension closed: dismiss now if on top, else as soon as this screen is."""
        self._suspension_closed = True
        if self.is_current:
            # Not awaited, and not via call_later: invoke awaits the pop on this screen's pump.
            self.dismiss(None)
        elif self.is_mounted:
            self._show_state()

    def on_screen_resume(self) -> None:
        if self._suspension_closed:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.notify("Choose an action, or Abort turn", severity="warning")

    def action_submit(self) -> None:
        """Press the one declared action; with several, focus the first instead."""
        buttons = list(self.query(_SuspensionActionButton))
        if len(buttons) != 1:
            buttons[0].focus()
            return
        self._send(buttons[0].action_label)

    def _send(self, action_label: str) -> None:
        """Answer with ``action_label`` and the fields; a refusal is shown and the form kept."""
        if not self.answerable:
            self.notify("This suspension is not answerable yet", severity="warning")
            return
        values = self._collect()
        if values is None:
            return
        refusal = self._answer_with(self.suspension_id, action_label, values)
        if refusal is not None:
            self.notify(refusal, title="Not answered", severity="error")
            return
        self._sent = True
        self._show_state()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if isinstance(button, _SuspensionActionButton):
            event.stop()
            self._send(button.action_label)
        elif button.id == "suspension-abort-turn":
            event.stop()
            self._abort_turn()


class SuspensionResponder:
    """Registers the TUI as ``session``'s live responder and keeps one modal up per front suspension.

    The modal shows the session's oldest answerable suspension, or the oldest
    queued one when none is; every other pending one is listed in it.

    Args:
        app: The app the modal is pushed on.
        session: The session whose suspensions the TUI answers.
        abort_turn: What the modal's ``Abort turn`` does.
    """

    def __init__(
        self, app: "App[Any]", session: "AgentSession", abort_turn: Callable[[], None]
    ) -> None:
        self._app = app
        self._session = session
        self._abort_turn = abort_turn
        self._screen: SuspensionScreen | None = None
        self._remove = session.add_suspension_responder(RESPONDER_NAME)
        self._unsubscribe = session.subscribe(self._on_event)
        self._detached = False
        app.call_later(self.refresh)

    @property
    def screen(self) -> SuspensionScreen | None:
        """The modal on show, or ``None``."""
        return self._screen

    def detach(self) -> None:
        """Withdraw the registration and the subscription, and close the modal."""
        self._detached = True
        self._remove()
        self._unsubscribe()
        if self._screen is not None:
            self._screen.close()
            self._screen = None

    def _on_event(self, event: AgentEvent) -> None:
        """A suspension opened or closed somewhere in the session: look again on the app's loop."""
        if event.type in ("suspension_open", "suspension_closed"):
            self._app.call_later(self.refresh)

    def refresh(self) -> None:
        """Show the front suspension, update the one on show, or close it when none is pending."""
        if self._detached:
            return
        pending = pending_states(self._session)
        front = next((s for s in pending if not s["queued"]), pending[0] if pending else None)
        screen = self._screen
        if screen is not None and (front is None or front["id"] != screen.suspension_id):
            screen.close()
            self._screen = screen = None
        if front is None:
            return
        queue = [s for s in pending if s["id"] != front["id"]]
        if screen is None:
            screen = SuspensionScreen(front, self.answer, self._abort_turn)
            screen.set_state(front, queue)
            self._screen = screen
            self._app.push_screen(screen)
            return
        screen.set_state(front, queue)

    def answer(self, suspension_id: str, action: str, values: dict[str, Any]) -> str | None:
        """Send an answer through ``AgentSession.answer_suspension``.

        Returns:
            Core's refusal as text, or ``None`` when it was accepted or another
            responder's answer already won (``suspension_closed`` follows either way).
        """
        try:
            self._session.answer_suspension(suspension_id, action, values, dict(ANSWERED_BY))
        except ValueError as exc:
            return str(exc)
        return None
