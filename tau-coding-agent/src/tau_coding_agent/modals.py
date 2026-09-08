"""modals — split out of app.py, built on the shared shell in :mod:`dialogs`."""

from typing import Any, ClassVar, Optional, TypeVar
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import (
    Static,
    Input,
    Button,
    Checkbox,
    RadioSet,
    RadioButton,
    SelectionList,
)
from tau_agent_core.extension_types import validate_form_spec
from textual import events

from tau_coding_agent.dialogs import TauDialog, TextDialog, dialog_buttons

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


class _FieldForm(TauDialog[_T]):
    """The widget-per-field machinery two dialogs share (E10 §6 / S66).

    One widget maps to each
    :data:`~tau_agent_core.extension_types.FORM_FIELD_KINDS` kind:

    - ``text`` / ``number`` → :class:`Input` (``number`` restricts to numerics);
    - ``confirm`` → :class:`Checkbox` (its own label carries the field label);
    - ``select`` → :class:`RadioSet` of :class:`RadioButton` (single choice);
    - ``multiselect`` → :class:`SelectionList` (N-of-M).

    Subclasses set :attr:`_fields` to a validated field list and decide what
    surrounds them and what a submit dismisses with. Extracted when
    :class:`ExtensionAskScreen` needed the same five widgets under a different
    frame (docs/EXTENSION-LOCKS.md §8) — the field vocabulary is shared, so the
    rendering of it is too.
    """

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
                answers[name] = (
                    options[idx] if 0 <= idx < len(options) else field.get("default", options[0])
                )
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

    The τ answer to pi's ``question``/``questionnaire`` widget factory: instead of an
    extension shipping bespoke TUI code, it hands ``ui.form`` a plain-data SPEC
    (D-E6-4) and :class:`_FieldForm` renders every field.

    ``Submit`` dismisses with the ``{name: value}`` answers dict; ``Cancel``/``Esc``
    dismisses with ``None`` (a cancelled form is not a fabricated answer set —
    Fail-Early). The spec is validated by the SAME
    :func:`~tau_agent_core.extension_types.validate_form_spec` the headless path
    uses, so the two frontends can never disagree about a field's meaning.
    """

    DIALOG_ID: ClassVar[str] = "ext-form-dialog"

    def __init__(self, spec: dict[str, Any]) -> None:
        form_title, self._fields = validate_form_spec(spec)
        super().__init__(form_title)

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="ext-form-fields"):
            for index, field in enumerate(self._fields):
                yield from self._compose_field(index, field)
        yield Static("Submit: confirm    Esc: cancel", classes="tau-dialog-hint")
        yield from dialog_buttons(
            [("Submit", "ext-form-submit", "primary"), ("Cancel", "ext-form-cancel", "default")],
            id="ext-form-buttons",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ext-form-submit":
            self.dismiss(self._collect())
        else:
            self.dismiss(None)
