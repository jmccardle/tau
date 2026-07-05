"""E10 §6 (S66) — the declarative ``api.ui.form`` renders as ONE generic screen.

Two layers, both driven through the REAL Textual runtime (``App.run_test()`` /
Pilot), matching ``test_extension_dialogs`` (S47):

1. The single :class:`ExtensionFormScreen` in isolation: every field kind
   (text/select/multiselect/confirm/number) renders, pre-fills its declared
   default, and Submit dismisses with the ``{name: value}`` answers dict; Esc
   dismisses with ``None`` (a cancelled form is not a fabricated answer set).
2. The ``_ExtensionUIDelegate.form`` end-to-end: what a loaded extension's
   ``await ctx.ui.form(spec)`` actually awaits, run inside a worker (the context
   ``push_screen_wait`` requires), exactly like the generation worker.

The headless json/policy half lives in
``tau-agent-core/tests/test_extension_types.py`` (TestExtensionUIForm).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S66.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import Checkbox, Input, RadioSet, SelectionList

from tau_coding_agent.app import ExtensionFormScreen, _ExtensionUIDelegate

_SPEC = {
    "title": "New task",
    "fields": [
        {"name": "desc", "kind": "text", "label": "Description", "default": "draft"},
        {"name": "prio", "kind": "select", "options": ["low", "high"], "default": "high"},
        {"name": "tags", "kind": "multiselect", "options": ["a", "b", "c"], "default": ["b"]},
        {"name": "urgent", "kind": "confirm", "label": "Urgent?", "default": True},
        {"name": "points", "kind": "number", "default": 3},
    ],
}


# ---------------------------------------------------------------------------
# 1. the generic form screen in isolation
# ---------------------------------------------------------------------------


class _ModalHarness(App):
    """Minimal host that pushes one modal and records its dismissal value."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


async def test_form_defaults_prefill_every_field() -> None:
    harness = _ModalHarness(ExtensionFormScreen(_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        screen = harness.screen
        assert screen.query_one("#ext-form-field-0", Input).value == "draft"
        # select: the default option is the pressed radio.
        radio = screen.query_one("#ext-form-field-1", RadioSet)
        assert radio._nodes[radio.pressed_index].label.plain == "high"
        # multiselect: only the default is selected.
        assert list(screen.query_one("#ext-form-field-2", SelectionList).selected) == ["b"]
        assert screen.query_one("#ext-form-field-3", Checkbox).value is True
        assert screen.query_one("#ext-form-field-4", Input).value == "3"


async def test_form_submit_returns_answers_dict() -> None:
    harness = _ModalHarness(ExtensionFormScreen(_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        # Edit the free-text field; leave everything else on its default.
        field = harness.screen.query_one("#ext-form-field-0", Input)
        field.value = "ship it"
        await pilot.pause()
        await pilot.click("#ext-form-submit")
        await pilot.pause()
    assert harness.result == {
        "desc": "ship it",
        "prio": "high",
        "tags": ["b"],
        "urgent": True,
        "points": 3,
    }


async def test_form_confirm_toggle_and_number_edit() -> None:
    harness = _ModalHarness(ExtensionFormScreen(_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        harness.screen.query_one("#ext-form-field-3", Checkbox).value = False
        harness.screen.query_one("#ext-form-field-4", Input).value = "8"
        await pilot.pause()
        await pilot.click("#ext-form-submit")
        await pilot.pause()
    assert harness.result["urgent"] is False
    assert harness.result["points"] == 8


async def test_form_number_float_parses() -> None:
    spec = {"fields": [{"name": "ratio", "kind": "number", "default": 0}]}
    harness = _ModalHarness(ExtensionFormScreen(spec))
    async with harness.run_test() as pilot:
        await pilot.pause()
        harness.screen.query_one("#ext-form-field-0", Input).value = "1.5"
        await pilot.pause()
        await pilot.click("#ext-form-submit")
        await pilot.pause()
    assert harness.result == {"ratio": 1.5}


async def test_form_escape_returns_none() -> None:
    # Fail-Early: a cancelled form is not a fabricated answer set.
    harness = _ModalHarness(ExtensionFormScreen(_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


async def test_form_cancel_button_returns_none() -> None:
    harness = _ModalHarness(ExtensionFormScreen(_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#ext-form-cancel")
        await pilot.pause()
    assert harness.result is None


# ---------------------------------------------------------------------------
# 2. the delegate end-to-end (what an extension hook awaits)
# ---------------------------------------------------------------------------


class _DelegateHarness(App):
    """A bare app that hosts the ``_ExtensionUIDelegate`` form modal."""


async def test_delegate_form_flows_answers_back() -> None:
    app = _DelegateHarness()
    delegate = _ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.form(_SPEC)

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()  # let the modal mount
        app.screen.query_one("#ext-form-field-0", Input).value = "from delegate"
        await pilot.pause()
        await pilot.click("#ext-form-submit")
        await worker.wait()

    assert box["value"] == {
        "desc": "from delegate",
        "prio": "high",
        "tags": ["b"],
        "urgent": True,
        "points": 3,
    }


async def test_delegate_form_cancel_returns_none() -> None:
    app = _DelegateHarness()
    delegate = _ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {"value": "UNSET"}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.form(_SPEC)

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()
        await pilot.press("escape")
        await worker.wait()

    assert box["value"] is None
