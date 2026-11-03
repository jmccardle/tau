"""`PickDialog` (D23 sections 3, 10): choose one value from a list too long for buttons.

The picker of `/model` (the values are `complete(cursor, "model", "")`), of a missing argument whose
type is a closed set of names, of `/config` with no extension, and of the theme. Written new, in
the manner of 0.15's `ChoiceDialog`: a filter field over a list; typing narrows the list, Up and
Down move in it from the field, Enter picks the highlighted row, Esc cancels. The value picked is
the row's text, so the caller decides what it means.
"""

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Input, OptionList, Static

from tau.tui._dialogs import TauDialog

_CURRENT = "  (current)"


class PickDialog(TauDialog[str | None]):
    """Pick one of `values`; the dialog is dismissed with it, or with `None` when cancelled. The
    row of `current`, if one is named, is marked and starts highlighted."""

    DIALOG_ID = "pick-dialog"
    BODY_ID = "pick-body"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
    ]

    def __init__(self, title: str, values: Sequence[str], current: str | None = None) -> None:
        super().__init__(title)
        self._values = list(values)
        self._current = current
        self._shown: list[str] = []

    def compose_header(self) -> ComposeResult:
        yield Input(placeholder="type to filter", id="pick-filter", classes="form-input")

    def compose_footer(self) -> ComposeResult:
        yield Static("Enter picks; Up and Down move; Esc cancels", classes="tau-dialog-hint")

    def compose_body(self) -> ComposeResult:
        yield OptionList(id="pick-list")

    def on_mount(self) -> None:
        self._narrow("")
        self.query_one(Input).focus()

    def _narrow(self, typed: str) -> None:
        """Show the values that hold `typed` (any case), those that start with it first,
        the current one highlighted if shown."""
        words = typed.lower().split()
        found = [v for v in self._values if all(w in v.lower() for w in words)]
        self._shown = sorted(found, key=lambda v: not v.lower().startswith(typed.lower()))
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([v + (_CURRENT if v == self._current else "") for v in self._shown])
        if self._shown:
            options.highlighted = (
                self._shown.index(self._current)
                if (self._current in self._shown and not words)
                else 0
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._narrow(event.value)

    def on_input_submitted(self) -> None:
        index = self.query_one(OptionList).highlighted
        if index is not None:
            self.dismiss(self._shown[index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._shown[event.option_index])

    def action_move(self, by: int) -> None:
        """Up or Down from the filter field: move the highlight."""
        options = self.query_one(OptionList)
        if by > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()
