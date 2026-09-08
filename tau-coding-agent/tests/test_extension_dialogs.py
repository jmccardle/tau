"""The TUI's remaining blocking dialogs, after docs/EXTENSION-LOCKS.md §8.2.

``confirm``/``select``/``input`` are gone from ``ExtensionUI`` and from the
delegate protocol, and with them ``ExtensionConfirmModal`` and
``ExtensionSelectModal``. Two screens survive, for two different reasons:

* ``ExtensionInputModal`` is now HEAD-LOCAL — the palette opens it to collect the
  argument string for a command that declares ``"args"``, and no extension
  reaches it. It is tested here because that is where its tests were.
* ``ExtensionFormScreen`` is the one dialog an extension can still open, through
  ``ctx.ui.form``, and it is how a flow collects a missing argument.

Both are driven through the REAL Textual runtime (``App.run_test()`` / Pilot).
The ask that replaced the three removed dialogs is
``test_extension_locks_tui.py``.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S47; docs/EXTENSION-LOCKS.md §8.2.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import Input

from tau_coding_agent import modals, extension_ui


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


async def test_input_modal_type_and_submit_returns_text() -> None:
    harness = _ModalHarness(modals.ExtensionInputModal("Name?", default="draft"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        field = harness.screen.query_one("#ext-input-field", Input)
        # The default pre-fills the field; clear and type a fresh value.
        field.value = ""
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == "hi"


async def test_input_modal_default_prefills_and_ok_returns_it() -> None:
    harness = _ModalHarness(modals.ExtensionInputModal("Name?", default="draft"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        assert harness.screen.query_one("#ext-input-field", Input).value == "draft"
        await pilot.click("#ext-input-ok")
        await pilot.pause()
    assert harness.result == "draft"


async def test_input_modal_escape_returns_none() -> None:
    harness = _ModalHarness(modals.ExtensionInputModal("Name?", default="draft"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


_FORM_SPEC = {
    "title": "Details",
    "fields": [{"name": "who", "kind": "text", "label": "Who", "default": "nobody"}],
}


async def test_form_modal_submit_returns_the_answers() -> None:
    harness = _ModalHarness(modals.ExtensionFormScreen(_FORM_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#ext-form-submit")
        await pilot.pause()
    assert harness.result == {"who": "nobody"}


async def test_form_modal_escape_fabricates_nothing() -> None:
    harness = _ModalHarness(modals.ExtensionFormScreen(_FORM_SPEC))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


class _DelegateHarness(App):
    """A bare app that hosts the ``_ExtensionUIDelegate`` modals.

    The delegate is app-agnostic at runtime (it only calls ``push_screen_wait`` /
    ``notify``), so this stands in for ``TauApp`` without booting a backend.
    """


async def test_delegate_form_flows_answers_back() -> None:
    """The one dialog an extension still awaits, end to end through the delegate."""
    app = _DelegateHarness()
    delegate = extension_ui._ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.form(_FORM_SPEC)

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()  # let the modal mount
        await pilot.click("#ext-form-submit")
        await worker.wait()

    assert box["value"] == {"who": "nobody"}


async def test_the_delegate_offers_four_methods() -> None:
    """§8.2: three that paint, one that blocks. A fifth would be a new surface."""
    public = {
        name
        for name in dir(extension_ui._ExtensionUIDelegate)
        if not name.startswith("_") and callable(getattr(extension_ui._ExtensionUIDelegate, name))
    }
    assert public == {"notify", "set_status", "panel", "form"}
