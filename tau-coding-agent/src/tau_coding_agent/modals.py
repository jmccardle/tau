"""modals — split out of app.py, built on the shared shell in :mod:`dialogs`."""

from typing import Any, ClassVar, Optional, TypeVar
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    Static,
    Input,
    Button,
    Checkbox,
    OptionList,
    SelectionList,
)
from textual.widgets.option_list import Option
from tau_agent_core.extension_types import (
    CHOICE_FILTER_THRESHOLD,
    filter_choices,
    validate_form_spec,
)
from textual import events

from tau_coding_agent.dialogs import ChoiceDialog, TauDialog, TextDialog, dialog_buttons

_T = TypeVar("_T")


class SystemPromptEditor(TextDialog):
    """Modal screen for editing the system prompt."""

    TITLE_TEXT = "Edit System Prompt"

    def __init__(self, current_prompt: str):
        super().__init__(current_prompt)
        self.current_prompt = current_prompt
        self.new_prompt = current_prompt


class RollbackPromptModal(TextDialog):
    """Collect the prompt that replaces the turn being rolled back.

    The affordance for ``multitask_strategy="rollback"``
    (docs/SUBMISSION-LIFECYCLE.md decision 2). A rollback is not "stop" — it is
    "stop, un-path what that turn did, and run THIS instead", and the core has no
    way to express the first two halves without the third: ``submit()`` needs the
    replacement text, and it must be submitted while the doomed turn still holds
    the turn slot. So the affordance has to ask for text. It asks in a modal
    rather than in the chat editor: since docs/TUI-STEERING.md that editor is
    usable during a turn and may hold a half-typed steering message, and taking
    it over for the rollback prompt would destroy what is in it.

    Prefilled with the aborted turn's own prompt, because the two things a person
    wants here are "run that again from before it went wrong" (accept the prefill)
    and "run this corrected version instead" (edit it), and prefilling makes the
    first one a single keypress. Reuses the ``SystemPromptEditor`` shell like
    :class:`TreeCustomInstructionsModal` does, plus one line saying what is about
    to happen to the running turn — the operation discards visible work, and the
    other destructive tree operations (branch, summarize, elide) all name their
    consequence before they run.
    """

    TITLE_TEXT = "Roll back the in-flight turn"
    SUBMIT_LABEL = "Roll back & run"
    SUBMIT_ID = "rollback-run"
    CANCEL_ID = "rollback-cancel"
    HELP = (
        "The running turn is aborted and its messages fall off the active path — "
        "nothing is deleted, the tree browser still shows them. This prompt runs in "
        "their place, from the context as it stood before that turn started."
    )


class ExtensionInputModal(TauDialog[Optional[str]]):
    """One line of text, for the palette's argument prompt.

    Was ``api.ui.input``'s screen; that method is gone
    (docs/EXTENSION-LOCKS.md §8.2) and this is now head-local, opened by
    ``TauApp._prompt_command_args`` for a command declaring ``"args"``. An
    ``Input`` pre-filled with ``default``; ``Enter`` or ``OK`` dismisses with the
    (possibly edited) text, ``Esc`` or ``Cancel`` with ``None`` — and a cancel
    does NOT dispatch, so a command never runs on an argument nobody typed.
    """

    DIALOG_ID = "ext-input-dialog"

    def __init__(self, title: str, default: str = "") -> None:
        super().__init__(title)
        self._default = default

    def compose_body(self) -> ComposeResult:
        yield Input(value=self._default, id="ext-input-field", classes="tau-dialog-input")
        yield from dialog_buttons(
            [("OK", "ext-input-ok", "primary"), ("Cancel", "ext-input-cancel", "default")],
            id="ext-input-buttons",
        )

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


class ExtensionChordScreen(TauDialog[Optional[tuple[str, str]]]):
    """Which-key popup for the ``ctrl+e`` extension shortcut chord (E10 §6 / S69).

    The second half of an extension key binding: after the ``ctrl+e`` leader (the
    guarded namespace), this modal lists every registered shortcut as
    ``ctrl+e <key> → /command`` and captures the NEXT key. A matching key dismisses
    with ``(command, args)`` — dispatched by :meth:`TauApp.action_extension_chord`
    through the SAME ``run_extension_command`` path a typed ``/name args`` uses;
    ``escape`` (or any unbound key) dismisses ``None``.

    Rendered as a menu (not a silent capture) so the guarded namespace is
    DISCOVERABLE — the user sees what ``ctrl+e`` offers, the same shortcuts the
    command palette also lists. Only ``Static`` children (none focusable), so the
    screen itself receives the key event — no inner widget swallows the tail key.
    """

    DIALOG_ID = "ext-chord-dialog"
    TITLE_TEXT = "Extension shortcuts — ctrl+e then…"

    def __init__(self, shortcuts: list[tuple[str, str, str, str]]) -> None:
        super().__init__()
        self._shortcuts = shortcuts
        # tail key -> (command, args) for O(1) capture; the list drives display order.
        self._by_key: dict[str, tuple[str, str]] = {
            key: (command, args) for key, command, args, _desc in shortcuts
        }

    def compose_body(self) -> ComposeResult:
        for key, command, args, desc in self._shortcuts:
            label = f"  [b]{key}[/b]  →  /{command}"
            if args:
                label += f" {args}"
            if desc:
                label += f"   — {desc}"
            yield Static(label, classes="ext-chord-entry")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
            return
        self.dismiss(self._by_key.get(event.key))


class ChoiceList(OptionList):
    """A single-choice list: the highlighted option is the choice, Enter commits it.

    Enter is rebound from ``OptionList``'s select to :class:`Committed`, so a
    click only moves the choice and Enter is what acts on it.
    """

    BINDINGS = [Binding("enter", "commit", "Choose", show=False)]

    class Committed(Message):
        """Enter was pressed on a highlighted option."""

    def action_commit(self) -> None:
        """Post :class:`Committed` when an option is highlighted."""
        if self.highlighted is not None:
            self.post_message(self.Committed())


class ChoiceField(Vertical):
    """One ``select`` field: a :class:`ChoiceList`, and a filter box over a long one.

    The filter appears past
    :data:`~tau_agent_core.extension_types.CHOICE_FILTER_THRESHOLD` options and
    narrows by :func:`~tau_agent_core.extension_types.filter_choices`. While it
    has focus the arrow keys move the list and Enter commits the highlighted
    option (docs/MODEL-PICKER.md §4).
    """

    class Committed(Message):
        """The person committed this field's choice with Enter."""

        def __init__(self, field: "ChoiceField") -> None:
            self.field = field
            super().__init__()

    _MOVES: ClassVar[dict[str, str]] = {
        "up": "cursor_up",
        "down": "cursor_down",
        "pageup": "page_up",
        "pagedown": "page_down",
    }

    def __init__(self, options: list[str], default: Any, *, id: str) -> None:
        super().__init__(id=id, classes="tau-choice")
        self._options = list(options)
        self._shown = list(options)
        self._default = default if default in options else options[0]

    def compose(self) -> ComposeResult:
        if len(self._options) > CHOICE_FILTER_THRESHOLD:
            yield Input(
                placeholder=f"type to filter {len(self._options)} options",
                classes="tau-dialog-input tau-choice-filter",
            )
        yield ChoiceList(*self._prompts(self._options), classes="tau-dialog-input tau-choice-list")

    def on_mount(self) -> None:
        self.query_one(ChoiceList).highlighted = self._options.index(self._default)

    @staticmethod
    def _prompts(options: list[str]) -> list[Option]:
        return [Option(Content(option)) for option in options]

    @property
    def shown(self) -> list[str]:
        """The options the list shows now, after any filter."""
        return list(self._shown)

    @property
    def value(self) -> Optional[str]:
        """The highlighted option, or ``None`` when the filter leaves none."""
        index = self.query_one(ChoiceList).highlighted
        return None if index is None else self._shown[index]

    def focus_target(self) -> Widget:
        """Where focus goes when this field is entered: the filter if any, else the list."""
        filters = self.query(".tau-choice-filter")
        return filters.first() if filters else self.query_one(ChoiceList)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        keep = self.value
        self._shown = filter_choices(self._options, event.value)
        choices = self.query_one(ChoiceList)
        choices.set_options(self._prompts(self._shown))
        if self._shown:
            choices.highlighted = self._shown.index(keep) if keep in self._shown else 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.value is not None:
            self.post_message(self.Committed(self))

    def on_choice_list_committed(self, event: ChoiceList.Committed) -> None:
        event.stop()
        self.post_message(self.Committed(self))

    def on_key(self, event: events.Key) -> None:
        move = self._MOVES.get(event.key)
        if move is not None and isinstance(self.screen.focused, Input):
            event.stop()
            getattr(self.query_one(ChoiceList), f"action_{move}")()


class _FieldForm(TauDialog[_T]):
    """The widget-per-field machinery two dialogs share (E10 §6 / S66).

    One widget maps to each
    :data:`~tau_agent_core.extension_types.FORM_FIELD_KINDS` kind:

    - ``text`` / ``number`` → :class:`Input` (``number`` restricts to numerics);
    - ``confirm`` → :class:`Checkbox` (its own label carries the field label);
    - ``select`` → :class:`ChoiceField` (single choice, filterable when long);
    - ``multiselect`` → :class:`SelectionList` (N-of-M).

    The body scrolls between a pinned title and a pinned footer
    (:attr:`~tau_coding_agent.dialogs.TauDialog.BODY_ID`). Enter in a text box or
    on a choice commits that field: it submits a one-field form, and moves to the
    next control in a longer one. Enter that no control takes submits
    (docs/MODEL-PICKER.md §5). Subclasses set :attr:`_fields` and say what
    submitting is in :meth:`action_submit`.
    """

    BINDINGS = [Binding("enter", "submit", "Submit", show=False)]

    _fields: list[dict[str, Any]]

    @staticmethod
    def _field_widget_id(index: int) -> str:
        return f"ext-form-field-{index}"

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
                classes="tau-dialog-input",
            )
        elif kind == "select":
            yield ChoiceField(field["options"], field.get("default"), id=wid)
        elif kind == "multiselect":
            options = field["options"]
            chosen_set = set(field.get("default", []) or [])
            yield SelectionList[str](
                *((opt, opt, opt in chosen_set) for opt in options),
                id=wid,
            )

    def on_mount(self) -> None:
        if not self._fields:
            return
        first = self.query_one(f"#{self._field_widget_id(0)}")
        (first.focus_target() if isinstance(first, ChoiceField) else first).focus()

    def action_submit(self) -> None:
        """What Enter on the frame does; a subclass says what submitting means."""
        raise NotImplementedError(f"{type(self).__name__} must supply action_submit()")

    def _commit(self, field: Widget) -> None:
        """Enter in ``field``: submit a one-field form, else focus the control after it."""
        if len(self._fields) == 1:
            self.action_submit()
            return
        chain = self.focus_chain
        inside = [i for i, w in enumerate(chain) if w is field or field in w.ancestors]
        if inside and inside[-1] + 1 < len(chain):
            chain[inside[-1] + 1].focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit(event.input)

    def on_choice_field_committed(self, event: ChoiceField.Committed) -> None:
        event.stop()
        self._commit(event.field)

    def _collect(self) -> Optional[dict[str, Any]]:
        """Every field's answer, or ``None`` after saying which choice has none."""
        for index, field in enumerate(self._fields):
            wid = f"#{self._field_widget_id(index)}"
            if field["kind"] == "select" and self.query_one(wid, ChoiceField).value is None:
                self.notify(f"{field['label']}: the filter matches no option", severity="warning")
                return None
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
                answers[name] = self.query_one(wid, ChoiceField).value
            elif kind == "multiselect":
                answers[name] = list(self.query_one(wid, SelectionList).selected)
        return answers

    @staticmethod
    def _parse_number(text: str, field: dict[str, Any]) -> Any:
        stripped = text.strip()
        if not stripped:
            return field.get("default", 0)
        try:
            return int(stripped)
        except ValueError:
            return float(stripped)


class ExtensionFormScreen(_FieldForm[Optional[dict]]):
    """One generic declarative form for an extension's ``api.ui.form`` (E10 §6 / S66).

    An extension hands ``ui.form`` a plain-data spec and :class:`_FieldForm`
    renders every field. ``OK`` (or Enter, by :class:`_FieldForm`'s rule)
    dismisses with the ``{name: value}`` answers; ``Cancel``/``Esc`` with
    ``None``, never a fabricated answer set. The spec goes through the same
    :func:`~tau_agent_core.extension_types.validate_form_spec` the headless path
    uses.
    """

    DIALOG_ID: ClassVar[str] = "ext-form-dialog"
    BODY_ID: ClassVar[Optional[str]] = "ext-form-fields"

    def __init__(self, spec: dict[str, Any]) -> None:
        form_title, self._fields = validate_form_spec(spec)
        super().__init__(form_title)

    def compose_body(self) -> ComposeResult:
        for index, field in enumerate(self._fields):
            yield from self._compose_field(index, field)

    def compose_footer(self) -> ComposeResult:
        yield Static("Enter: OK    Esc: cancel", classes="tau-dialog-hint")
        yield from dialog_buttons(
            [("OK", "ext-form-submit", "primary"), ("Cancel", "ext-form-cancel", "default")],
            id="ext-form-buttons",
        )

    def action_submit(self) -> None:
        """Dismiss with the answers, unless a choice has none."""
        answers = self._collect()
        if answers is not None:
            self.dismiss(answers)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ext-form-submit":
            self.action_submit()
        else:
            self.dismiss(None)


class ExtensionAuthorizationDialog(ChoiceDialog[bool]):
    """Ask whether to authorize and load a discovered extension (docs/EXTENSION-PACKAGES.md §9).

    The title and body come from :func:`tau_agent_core.sdk.authorization_prompt`;
    ``escape`` is "not now", never a hidden yes.
    """

    DIALOG_ID = "extension-authorization-dialog"
    CHOICES = (
        ("Authorize and load", "extension-authorize-yes", True),
        ("Not now", "extension-authorize-no", False),
    )
    CANCEL_VALUE = False
    VERTICAL = False
