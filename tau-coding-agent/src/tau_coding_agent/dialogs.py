"""The one dialog shell every modal in the TUI is built from.

Eleven ``ModalScreen`` subclasses were written independently and converged on the
same shape without sharing it: a centred ``Container``, a bold title, a body, a row
of buttons, and ``escape`` meaning cancel. The cost was measured before this module
existed — nine identical ``Binding("escape", …)`` lines, nine ``action_cancel``
bodies, eight ``on_button_pressed`` id-dispatch bodies, twelve
``Container(id="…-dialog")`` openers — and, in the stylesheet, seven byte-identical
rules for the title alone.

Three classes, by how much of the shape a screen accepts:

* :class:`TauDialog` — the shell. A subclass supplies :meth:`~TauDialog.compose_body`
  and gets the frame, the title, the centering and the cancel binding.
* :class:`ChoiceDialog` — title, optional message, N buttons, each dismissing with a
  declared value. What a question with a fixed set of answers looks like.
* :class:`TextDialog` — title, optional help line, one ``TextArea``, submit and
  cancel. What "write something and hand it back" looks like.

Two secondary text roles, deliberately not merged into one. ``tau-dialog-help`` is a
paragraph stating a consequence before it happens, set left because a centred
paragraph is unreadable; ``tau-dialog-hint`` is the one centred line of key hints
under a body. The screens these replaced had four different treatments between them
for what turned out to be these two roles.

Widget ids are preserved from the screens these replaced, because tests and
``extension_ui`` query them by name; the STYLING moved to the ``tau-dialog-*``
classes, so a new dialog is styled the day it is written rather than the day
someone remembers to add seven rules for it.

Reference: docs/TUI-STYLE-GUIDE.md §1.
"""

from typing import Any, ClassVar, Iterable, Optional, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

_T = TypeVar("_T")

#: The one ``TextArea`` id every text dialog uses; ``app.py`` reads it back by name.
TEXT_INPUT_ID = "prompt-editor-textarea"


def dialog_buttons(
    buttons: Iterable[tuple[str, str, str]], *, id: str, vertical: bool = False
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
            yield Button(label, variant=variant, id=button_id)  # type: ignore[arg-type]


class TauDialog(ModalScreen[_T]):
    """A centred modal with a title, a body, and ``escape`` meaning cancel.

    Subclasses supply :meth:`compose_body` and, usually, :attr:`TITLE_TEXT`. What
    they do NOT supply is the frame, the centering, the title styling or the cancel
    binding — those are the things every dialog agreed on by accident before, and
    the things a new dialog now gets without asking.

    Attributes:
        DIALOG_ID: The frame's widget id. Carries the two properties that genuinely
            differ between dialogs (``width``, ``height``); everything else comes
            from the ``tau-dialog`` class.
        TITLE_TEXT: The title, for a dialog whose title never varies. A dialog whose
            title is supplied per instance passes it to ``__init__`` instead.
        CANCEL_VALUE: What ``escape`` dismisses with. ``None`` for a dialog returning
            an optional value, ``False`` for a confirmation — a cancelled question is
            never a hidden yes.
    """

    DIALOG_ID: ClassVar[str] = "tau-dialog"
    TITLE_TEXT: ClassVar[str] = ""
    CANCEL_VALUE: ClassVar[Any] = None

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: Optional[str] = None) -> None:
        super().__init__()
        self._title = self.TITLE_TEXT if title is None else title

    def dialog_title(self) -> str:
        """The text of the title row."""
        return self._title

    def compose_body(self) -> ComposeResult:
        """Everything between the title and the bottom of the frame."""
        raise NotImplementedError(f"{type(self).__name__} must supply compose_body()")

    def compose(self) -> ComposeResult:
        with Container(id=self.DIALOG_ID, classes="tau-dialog"):
            yield Static(self.dialog_title(), classes="tau-dialog-title")
            yield from self.compose_body()

    def action_cancel(self) -> None:
        self.dismiss(self.CANCEL_VALUE)


class ChoiceDialog(TauDialog[_T]):
    """A question with a fixed set of answers, one button each.

    :attr:`CHOICES` pairs each button with the value it dismisses with, so a subclass
    states the mapping once instead of writing an ``on_button_pressed`` that rebuilds
    it. A button whose declared value is :attr:`~TauDialog.CANCEL_VALUE` needs no
    special case — it is a choice like the others.

    Attributes:
        CHOICES: ``(label, widget_id, dismissed value)`` per button, in display order.
        MESSAGE: An optional line between the title and the buttons, for a dialog
            whose consequence needs stating before it runs.
        VERTICAL: Stack the buttons. True for a menu of sentences, False for a pair.
        BUTTONS_ID: The button container's id, kept for the per-dialog sizing rule.
    """

    CHOICES: ClassVar[tuple[tuple[str, str, Any], ...]] = ()
    MESSAGE: ClassVar[str] = ""
    VERTICAL: ClassVar[bool] = True
    BUTTONS_ID: ClassVar[str] = "tau-dialog-buttons"

    def __init__(self, title: Optional[str] = None, message: Optional[str] = None) -> None:
        super().__init__(title)
        self._message = self.MESSAGE if message is None else message

    def compose_body(self) -> ComposeResult:
        if self._message:
            yield Static(self._message, classes="tau-dialog-help")
        # The first choice is the default action, so it gets the primary variant.
        yield from dialog_buttons(
            [
                (label, button_id, "primary" if index == 0 else "default")
                for index, (label, button_id, _value) in enumerate(self.CHOICES)
            ],
            id=self.BUTTONS_ID,
            vertical=self.VERTICAL,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        for _label, button_id, value in self.CHOICES:
            if event.button.id == button_id:
                self.dismiss(value)
                return
        self.dismiss(self.CANCEL_VALUE)


class TextDialog(TauDialog[Optional[str]]):
    """Write something and hand it back, or cancel and hand back nothing.

    Attributes:
        SUBMIT_LABEL: The confirming button's text. It names the consequence
            ("Roll back & run", "Summarize"), never "OK".
        SUBMIT_ID: That button's widget id.
        CANCEL_ID: The cancelling button's widget id.
        HELP: An optional line under the title, for an operation that discards
            visible work.
        DIALOG_ID: All three text dialogs share one frame id, so they share one
            sizing rule.
    """

    DIALOG_ID: ClassVar[str] = "prompt-editor-dialog"
    SUBMIT_LABEL: ClassVar[str] = "Save"
    SUBMIT_ID: ClassVar[str] = "prompt-save"
    CANCEL_ID: ClassVar[str] = "prompt-cancel"
    HELP: ClassVar[str] = ""

    def __init__(self, prefill: str = "", title: Optional[str] = None) -> None:
        super().__init__(title)
        self._prefill = prefill

    def compose_body(self) -> ComposeResult:
        if self.HELP:
            yield Static(self.HELP, classes="tau-dialog-help")
        yield TextArea(self._prefill, id=TEXT_INPUT_ID, classes="tau-dialog-input")
        yield from dialog_buttons(
            [
                (self.SUBMIT_LABEL, self.SUBMIT_ID, "primary"),
                ("Cancel", self.CANCEL_ID, "default"),
            ],
            id="prompt-editor-buttons",
        )

    def entered_text(self) -> str:
        """What is in the editor right now."""
        return self.query_one(f"#{TEXT_INPUT_ID}", TextArea).text

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == self.SUBMIT_ID:
            self.dismiss(self.entered_text())
        elif event.button.id == self.CANCEL_ID:
            self.dismiss(None)
