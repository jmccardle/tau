"""The form renderer's two places (D23 section 3): the dialog a command's arguments are asked in,
and the question box an extension's question is answered in. Both are built from
`tau.tui._fields.SchemaField`.

Written new, in place of 0.15's modals.py (`ChoiceList`, `ChoiceField`, `_FieldForm`), over JSON
Schema. The dialog is a modal over the screen: a command line that is missing arguments
(`Parsed.missing`) opens it, with the command's whole schema, the arguments already typed filled
in, and the missing ones first. The question box is inline, docked above the editor, so the
transcript stays readable while a question waits (a question can wait for hours).
"""

from collections.abc import Mapping
from typing import ClassVar, Protocol, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static, Switch

from tau._client import Client
from tau._events import CursorView, OpenQuestion
from tau._values import Json
from tau.tui._dialogs import TauDialog, dialog_buttons
from tau.tui._fields import UNSET, Completer, SchemaField, choices_of, shape_of


class FormDialog(TauDialog[dict[str, object] | None]):
    """Ask for a command's arguments. It is dismissed with the arguments (those typed before,
    and the ones filled in here; a field left empty is left out, so its default applies), or with
    `None` when cancelled. A value that does not read stays in the dialog with its problem shown."""

    DIALOG_ID = "form-dialog"
    BODY_ID = "form-body"
    RUN_LABEL = "Run"

    def __init__(
        self,
        title: str,
        schema: Json,
        completer: Completer,
        given: dict[str, object] | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        super().__init__(title)
        self._schema = schema
        self._completer = completer
        self._given = given or {}
        self._missing = missing
        self._fields: list[SchemaField] = []

    def compose_body(self) -> ComposeResult:
        properties = cast("dict[str, Json]", self._schema.get("properties", {}))
        required = cast("list[str]", self._schema.get("required", []))
        names = [*self._missing, *(n for n in properties if n not in self._missing)]
        for name in names:
            field = SchemaField(
                name,
                properties[name],
                self._completer,
                required=name in required,
                id=f"form-{name}",
            )
            self._fields.append(field)
            yield field

    def compose_footer(self) -> ComposeResult:
        yield Static("Enter in a field moves on; Esc cancels", classes="tau-dialog-hint")
        yield from dialog_buttons(
            [(self.RUN_LABEL, "form-run", "primary"), ("Cancel", "form-cancel", "default")],
            id="form-buttons",
        )

    def on_mount(self) -> None:
        for field in self._fields:
            if field.key in self._given:
                field.set_value(self._given[field.key])
        if self._fields:
            self._fields[0].focus_field()

    def on_input_submitted(self) -> None:
        self.screen.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "form-cancel":
            self.dismiss(None)
        elif event.button.id == "form-run":
            self._submit()

    def _submit(self) -> None:
        arguments = dict(self._given)
        problems = 0
        for field in self._fields:
            try:
                value = field.value()
            except ValueError:
                problems += 1
                continue
            if value is not UNSET:
                arguments[field.key] = value
        if not problems:
            self.dismiss(self._result(arguments))

    def _result(self, arguments: dict[str, object]) -> dict[str, object]:
        """What the dialog is dismissed with, from the values the fields hold."""
        return arguments


class OptionsDialog(FormDialog):
    """Edit an extension's options (`/config EXTENSION`, D23 section 3): the same renderer over
    the options' schema, each field showing the value this branch has. It is dismissed with the
    options to set, and `save` says whether the "save for every session" switch was on.

    Without the switch those are the options changed from the branch's value. With it they are
    also the ones the person wrote a value for, and those that differ from what the user's file
    holds (`saved`): saving the value the branch already has is a change to the file. `None` when
    cancelled."""

    DIALOG_ID = "options-dialog"
    RUN_LABEL = "Apply"

    def __init__(
        self,
        title: str,
        schema: Json,
        completer: Completer,
        saved: Mapping[str, object],
        save: bool = False,
    ) -> None:
        super().__init__(title, schema, completer)
        self.save = save
        self._saved = saved

    def compose_footer(self) -> ComposeResult:
        with Horizontal(classes="form-save"):
            yield Switch(value=self.save, id="form-save")
            yield Static("save for every session (your config file)", markup=False)
        yield from super().compose_footer()

    def _result(self, arguments: dict[str, object]) -> dict[str, object]:
        self.save = self.query_one("#form-save", Switch).value
        properties = cast("dict[str, Json]", self._schema.get("properties", {}))
        typed = {f.key for f in self._fields if f.typed}

        def wanted(key: str, value: object) -> bool:
            if value != properties[key].get("default"):
                return True
            return self.save and (
                key in typed or (key in self._saved and value != self._saved[key])
            )

        return {k: v for k, v in arguments.items() if wanted(k, v)}


class QuestionBox(Vertical):
    """One open question, inline: who asks, the text, the field and the buttons. A question that
    only asks to choose among values has a button per value; any other has its field and an
    Answer button. Decline (Esc) answers `None`. A question `to` another client is shown with
    its name and no buttons. It posts `Answered` or `Declined`; the app makes the call and, if the
    answer is refused, `show_error`."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "decline", "Decline", show=False)]

    class Answered(Message):
        """The person answered question `question` with `value`."""

        def __init__(self, question: int, value: object) -> None:
            super().__init__()
            self.question = question
            self.value = value

    class Declined(Message):
        """The person declined question `question`."""

        def __init__(self, question: int) -> None:
            super().__init__()
            self.question = question

    def __init__(self, question: OpenQuestion, me: str, completer: Completer) -> None:
        super().__init__(id=f"question-{question.id}", classes="question-box")
        self.question = question
        self._mine = question.to in (None, me)
        self._completer = completer
        self._field: SchemaField | None = None
        self._message = Static("", classes="form-error", markup=False)
        self.border_title = f"{question.extension} asks"

    @property
    def text(self) -> str:
        """What the box says: the question, its choices if it asks to choose, and any problem."""
        shown = self.question.text
        found = choices_of(shape_of(self.question.schema)[0])
        if found is not None and self._mine:
            shown += "  [" + " / ".join(map(str, found)) + "]"
        if not self._mine:
            shown += f"  (waiting for {self.question.to})"
        return shown

    def compose(self) -> ComposeResult:
        yield Static(self.text, classes="question-text", markup=False)
        if not self._mine:
            return
        found = choices_of(shape_of(self.question.schema)[0])
        qid = self.question.id
        if found is None:
            self._field = SchemaField(
                "answer", self.question.schema, self._completer, required=True, id=f"answer-{qid}"
            )
            yield self._field
        yield self._message
        with Horizontal(classes="question-buttons"):
            for i, choice in enumerate(found or ()):
                yield Button(str(choice), id=f"choice-{qid}-{i}", name=str(i), variant="primary")
            if found is None:
                yield Button("Answer", id=f"answer-{qid}-send", variant="primary")
            yield Button("Decline", id=f"decline-{qid}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        qid = self.question.id
        button = event.button.id or ""
        if button == f"decline-{qid}":
            self.action_decline()
        elif button == f"answer-{qid}-send":
            self._answer_from_field()
        else:
            found = choices_of(shape_of(self.question.schema)[0]) or []
            self.post_message(self.Answered(qid, found[int(event.button.name or "0")]))

    def _answer_from_field(self) -> None:
        assert self._field is not None
        try:
            value = self._field.value()
        except ValueError:
            return
        self.post_message(self.Answered(self.question.id, None if value is UNSET else value))

    def on_input_submitted(self, event: Message) -> None:
        """Enter in the field answers."""
        if self._field is not None:
            self._answer_from_field()

    def action_decline(self) -> None:
        """Answer `None`."""
        if self._mine:
            self.post_message(self.Declined(self.question.id))

    def show_error(self, message: str) -> None:
        """Say why the answer was refused; the box stays."""
        self._message.update(message)


class Owner(Protocol):
    """What the question stack needs of the app that holds it."""

    @property
    def client(self) -> Client: ...

    async def complete_value(self, schema: Json, text: str) -> list[str]: ...

    def focus_editor(self) -> None: ...

    @property
    def draft(self) -> str: ...


class QuestionStack(Vertical):
    """The open questions of the viewed cursor, oldest first, each a `QuestionBox`. It follows
    the cursor's view (`follow`) and makes the calls the boxes ask for: an answer the session
    refuses stays in its box with the reason, an answer someone else gave first closes the box
    quietly."""

    def __init__(self, owner: Owner) -> None:
        super().__init__(id="questions")
        self.owner = owner

    async def follow(self, view: CursorView) -> None:
        """One box per open question: those closed are removed, those new added. A new question
        for this client takes the keyboard unless the person is typing."""
        open_ids = {q.id for q in view.questions}
        for box in self.query(QuestionBox):
            if box.question.id not in open_ids:
                await box.remove()
        shown = {box.question.id for box in self.query(QuestionBox)}
        me = self.owner.client.id
        for question in view.questions:
            if question.id in shown:
                continue
            box = QuestionBox(question, me, self.owner.complete_value)
            await self.mount(box)
            if question.to in (None, me) and not self.owner.draft:
                box.query("Button").first().focus()

    async def on_question_box_answered(self, message: QuestionBox.Answered) -> None:
        await self._answer(message.question, message.value)

    async def on_question_box_declined(self, message: QuestionBox.Declined) -> None:
        await self._answer(message.question, None)

    async def _answer(self, question: int, value: object) -> None:
        box = self.query_one(f"#question-{question}", QuestionBox)
        try:
            await self.owner.client.answer(question, value)
        except (ValueError, PermissionError, ConnectionError) as e:
            box.show_error(str(e))
        except LookupError:
            await box.remove()
        else:
            self.owner.focus_editor()
