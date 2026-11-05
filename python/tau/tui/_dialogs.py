"""The one dialog shell every modal in the TUI is built from.

Carried from τ 0.15: tau-coding-agent/src/tau_coding_agent/dialogs.py (tag v0.15.0).

Eleven ``ModalScreen`` subclasses were written independently and converged on the
same shape without sharing it: a centred ``Container``, a bold title, a body, a row
of buttons, and ``escape`` meaning cancel. The cost was measured before this module
existed — nine identical ``Binding("escape", …)`` lines, nine ``action_cancel``
bodies, eight ``on_button_pressed`` id-dispatch bodies, twelve
``Container(id="…-dialog")`` openers — and, in the stylesheet, seven byte-identical
rules for the title alone.

:class:`TauDialog` is the shell. A subclass supplies :meth:`~TauDialog.compose_body` and gets the
shell, the title, the centering and the cancel binding. (0.15's `ChoiceDialog` and `TextDialog`
are written new, small, below: `ConfirmDialog` and `TextDialog`.)

Two secondary text roles, deliberately not merged into one. ``tau-dialog-help`` is a
paragraph stating a consequence before it happens, set left because a centred
paragraph is unreadable; ``tau-dialog-hint`` is the one centred line of key hints
under a body. The screens these replaced had four different treatments between them
for what turned out to be these two roles.

Widget ids are stable, because tests and the app query them by name; the STYLING
lives in the ``tau-dialog-*`` classes, so a new dialog is styled the day it is
written rather than the day someone remembers to add seven rules for it.
"""

from collections.abc import Iterable
from typing import Any, ClassVar, Literal, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

_T = TypeVar("_T")

ButtonVariant = Literal["default", "primary", "success", "warning", "error"]


def dialog_buttons(
    buttons: Iterable[tuple[str, str, ButtonVariant]], *, id: str, vertical: bool = False
) -> ComposeResult:
    """A row (or column) of dialog buttons, styled by class rather than by id.

    Args:
        buttons: ``(label, widget_id, variant)`` per button, in display order.
        id: The container's id, kept from the screen this replaced so the
            per-dialog sizing rules still match.
        vertical: Stack the buttons instead of placing them side by side. A menu of
            full-sentence choices stacks; a Yes/No pair does not.
    """
    container = Vertical if vertical else Horizontal
    with container(id=id, classes="tau-dialog-buttons"):
        for label, button_id, variant in buttons:
            yield Button(label, variant=variant, id=button_id)


class TauDialog(ModalScreen[_T]):
    """A centred modal with a title, a body, and ``escape`` meaning cancel.

    Subclasses supply :meth:`compose_body` and, usually, :attr:`TITLE_TEXT`. What
    they do NOT supply is the shell, the centering, the title styling or the cancel
    binding — those are the things every dialog agreed on by accident before, and
    the things a new dialog now gets without asking.

    Attributes:
        DIALOG_ID: The shell's widget id. Carries the two properties that genuinely
            differ between dialogs (``width``, ``height``); everything else comes
            from the ``tau-dialog`` class.
        TITLE_TEXT: The title, for a dialog whose title never varies. A dialog whose
            title is supplied per instance passes it to ``__init__`` instead.
        CANCEL_VALUE: What ``escape`` dismisses with. ``None`` for a dialog returning
            an optional value, ``False`` for a confirmation — a cancelled question is
            never a hidden yes.
        BODY_ID: Set it to give the dialog a scrolling body with that id between a
            header (the title plus :meth:`compose_header`) and a footer
            (:meth:`compose_footer`), both pinned, so the buttons stay on screen
            however long the body is . ``None`` keeps the
            plain stack.
    """

    DIALOG_ID: ClassVar[str] = "tau-dialog"
    TITLE_TEXT: ClassVar[str] = ""
    CANCEL_VALUE: ClassVar[Any] = None
    BODY_ID: ClassVar[str | None] = None

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str | None = None) -> None:
        super().__init__()
        self._title = self.TITLE_TEXT if title is None else title

    def dialog_title(self) -> str:
        """The text of the title row."""
        return self._title

    def compose_body(self) -> ComposeResult:
        """Everything between the title and the bottom of the shell."""
        raise NotImplementedError(f"{type(self).__name__} must supply compose_body()")

    def compose_header(self) -> ComposeResult:
        """What stays pinned under the title when :attr:`BODY_ID` is set."""
        yield from ()

    def compose_footer(self) -> ComposeResult:
        """What stays pinned at the bottom when :attr:`BODY_ID` is set: hints, buttons."""
        yield from ()

    def compose(self) -> ComposeResult:
        """The shell: a title and the body, pinned header and footer if `BODY_ID` is set."""
        if self.BODY_ID is None:
            with Container(id=self.DIALOG_ID, classes="tau-dialog"):
                yield Static(self.dialog_title(), classes="tau-dialog-title", markup=False)
                yield from self.compose_body()
            return
        with Container(id=self.DIALOG_ID, classes="tau-dialog tau-dialog-scrolling"):
            with Vertical(classes="tau-dialog-header"):
                yield Static(self.dialog_title(), classes="tau-dialog-title", markup=False)
                yield from self.compose_header()
            with Vertical(classes="tau-dialog-footer"):
                yield from self.compose_footer()
            with VerticalScroll(id=self.BODY_ID, classes="tau-dialog-body"):
                yield from self.compose_body()

    def action_cancel(self) -> None:
        """Esc: dismiss with `CANCEL_VALUE`."""
        self.dismiss(self.CANCEL_VALUE)


class ConfirmDialog(TauDialog[bool]):
    """Ask a yes-or-no question; dismissed with the answer, `False` on Esc (a cancelled question
    is never a hidden yes)."""

    DIALOG_ID = "confirm-dialog"
    CANCEL_VALUE = False

    def __init__(self, title: str, question: str) -> None:
        super().__init__(title)
        self._question = question

    def compose_body(self) -> ComposeResult:
        yield Static(self._question, classes="tau-dialog-help", markup=False)
        yield from dialog_buttons(
            [("Yes", "confirm-yes", "primary"), ("No", "confirm-no", "default")],
            id="confirm-buttons",
        )

    def on_mount(self) -> None:
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class TextDialog(TauDialog[str | None]):
    """Ask for one line of text; dismissed with it (Enter), or `None` (Esc). An empty line is not
    an answer."""

    DIALOG_ID = "text-dialog"

    def __init__(self, title: str, prompt: str) -> None:
        super().__init__(title)
        self._prompt = prompt

    def compose_body(self) -> ComposeResult:
        yield Static(self._prompt, classes="tau-dialog-help", markup=False)
        yield Input(id="text-dialog-input", classes="tau-dialog-input")
        yield Static("Enter accepts; Esc cancels", classes="tau-dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value.strip():
            self.dismiss(event.value.strip())
