"""The form renderer (D23 section 3): one widget set from a JSON Schema, for a question's answer
and for a command's arguments. It branches on the schema's shape and never on a command's or an
extension's name.

| the schema | the widget | its value |
|---|---|---|
| `enum` | a list of choices (`RadioSet`) | the chosen value |
| `boolean` | a switch | `True` or `False` |
| `array` of `enum` | a multi-select | the chosen values |
| anything else (text, a number, a named type, a list) | a text field | `commandline.value` of it |

A `T | None` is the `T` field, left empty for `None`. A named type's field (its schema has a
`"format"`) completes with Tab through `commandline.values`.
"""

from collections.abc import Awaitable, Callable
from typing import ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, RadioButton, RadioSet, SelectionList, Static, Switch

from tau._values import Json
from tau.commandline import value as read_value

Completer = Callable[[Json, str], Awaitable[list[str]]]
"""What finishes the text of a field: the field's schema and the text so far, to the candidates."""


class Refused(Message):
    """A completion the session could not make (the connection is gone): `text` says why. The app
    shows it as a toast."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class Unset:
    """The value of a field nobody filled in: the argument is left out, so its default applies."""


UNSET = Unset()


def shape_of(schema: Json) -> tuple[Json, bool]:
    """The schema of the value, and whether it may be left empty: a `T | None` is the `T`."""
    alternatives = cast("list[Json] | None", schema.get("anyOf"))
    if alternatives is None:
        return schema, False
    present = [a for a in alternatives if a.get("type") != "null"]
    return (present[0] if len(present) == 1 else schema), len(present) < len(alternatives)


def choices_of(schema: Json) -> list[object] | None:
    """The values to choose among, if the schema lists them: an `enum`."""
    return cast("list[object] | None", schema.get("enum"))


class CompletingInput(Input):
    """A text field that finishes its text with Tab, through the `Completer` it is given. Further
    Tabs cycle through the candidates."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("tab", "complete", "Complete", priority=True)]

    def __init__(self, schema: Json, completer: Completer, *, placeholder: str = "") -> None:
        super().__init__(placeholder=placeholder, classes="form-input")
        self._schema = schema
        self._completer = completer
        self._cycle: list[str] = []
        self._shown: str | None = None

    def action_complete(self) -> None:
        self.run_worker(self._complete(), exclusive=True)

    async def _complete(self) -> None:
        if self._shown is None or self.value != self._shown:
            try:
                self._cycle = await self._completer(self._schema, self.value)
            except ConnectionError as e:
                self.post_message(Refused(str(e)))
                return
            self._cycle.reverse()  # popped from the end: the first candidate comes first
        if not self._cycle:
            return
        pick = self._cycle.pop()
        self._cycle.insert(0, pick)
        self._shown = pick
        self.value = pick
        self.cursor_position = len(pick)


class SchemaField(Vertical):
    """One value of a schema: its label, its widget and a line for what is wrong with it. `value`
    reads the widget (`ValueError` says what is wrong, and the line shows it)."""

    def __init__(
        self,
        name: str,
        schema: Json,
        completer: Completer,
        *,
        required: bool,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id, classes="form-field")
        self.key = name
        self.schema, optional = shape_of(schema)
        self.required = required and not optional
        self._outer = schema
        self._completer = completer
        self._error = Static("", classes="form-error", markup=False)
        self._widget: RadioSet | Switch | SelectionList[int] | CompletingInput

    def compose(self) -> ComposeResult:
        described = self._outer.get("description") or self.schema.get("description")
        yield Static(
            f"{self.key}{'' if self.required else ' (optional)'}"
            + (f": {described}" if described else ""),
            classes="form-label",
            markup=False,
        )
        yield self._make()
        yield self._error

    def _make(self) -> RadioSet | Switch | SelectionList[int] | CompletingInput:
        schema = self.schema
        items = cast("Json", schema.get("items", {}))
        default = self._outer.get("default", schema.get("default"))
        if (found := choices_of(schema)) is not None:
            buttons = [RadioButton(str(v), value=(v == default)) for v in found]
            self._widget = RadioSet(*buttons, classes="form-choice")
        elif schema.get("type") == "boolean":
            self._widget = Switch(value=bool(default), classes="form-switch")
        elif schema.get("type") == "array" and choices_of(items) is not None:
            picks = choices_of(items) or []
            chosen: list[object] = default if isinstance(default, list) else []  # pyright: ignore[reportUnknownVariableType]
            options = [(str(v), i, v in chosen) for i, v in enumerate(picks)]  # pyright: ignore[reportUnknownVariableType]
            self._widget = SelectionList[int](*options, classes="form-multi")
        else:
            placeholder = "" if default is None else str(default)
            self._widget = CompletingInput(schema, self._completer, placeholder=placeholder)
        return self._widget

    @property
    def typed(self) -> bool:
        """Whether the person wrote a value in a text field (a choice or switch always has one)."""
        widget = self._widget
        return isinstance(widget, Input) and bool(widget.value.strip())

    def value(self) -> object:
        """What the field holds: `UNSET` if it was left empty and may be (its default applies).
        `ValueError` if it is required and empty, or the text does not read as the schema's type."""
        self._error.update("")
        try:
            return self._read()
        except ValueError as e:
            self._error.update(str(e))
            raise

    def _read(self) -> object:
        widget = self._widget
        if isinstance(widget, Switch):
            return widget.value
        if isinstance(widget, RadioSet):
            index = widget.pressed_index
            found = choices_of(self.schema) or []
            if index < 0:
                return self._empty("choose one")
            return found[index]
        if isinstance(widget, SelectionList):
            picks = choices_of(cast("Json", self.schema["items"])) or []
            return [picks[i] for i in widget.selected]
        text = widget.value.strip()
        return read_value(text, self.schema) if text else self._empty("a value is needed")

    def _empty(self, why: str) -> Unset:
        if self.required:
            raise ValueError(why)
        return UNSET

    def set_value(self, given: object) -> None:
        """Fill the field with a value already known (a command line's argument)."""
        widget = self._widget
        if isinstance(widget, Switch):
            widget.value = bool(given)
        elif isinstance(widget, RadioSet):
            found = choices_of(self.schema) or []
            if given in found:
                widget.query(RadioButton)[found.index(given)].value = True
        elif isinstance(widget, SelectionList):
            picks = choices_of(cast("Json", self.schema["items"])) or []
            for i, v in enumerate(picks):
                if isinstance(given, list) and v in given:  # pyright: ignore[reportUnknownVariableType]
                    widget.select(i)
        else:
            widget.value = ",".join(map(str, given)) if isinstance(given, list) else str(given)  # pyright: ignore[reportUnknownArgumentType]

    def focus_field(self) -> None:
        """Put the keyboard on the field's widget."""
        self._widget.focus()
