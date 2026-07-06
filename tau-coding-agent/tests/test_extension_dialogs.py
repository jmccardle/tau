"""E7 §3 (S47) — the TUI ``confirm`` / ``select`` / ``input`` dialogs are wired.

Two layers, both driven through the REAL Textual runtime (``App.run_test()`` /
Pilot), matching the ``test_session_tree_browser`` modal style:

1. The three ``ModalScreen`` overlays in isolation: each dialogs' Yes/No, choose,
   type-and-submit, and Esc/Cancel paths dismiss with the right value.
2. The ``_ExtensionUIDelegate`` end-to-end: an extension calling ``api.ui.confirm``
   / ``select`` / ``input`` (via the delegate) pushes the modal, the user answers,
   and the awaited value flows back to the caller. This is what a loaded
   extension's hook actually awaits, so it runs inside a worker (the context
   ``push_screen_wait`` requires), exactly like the generation worker.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S47.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import Input, OptionList

from tau_coding_agent.app import (
    ExtensionConfirmModal,
    ExtensionInputModal,
    ExtensionSelectModal,
    _ExtensionUIDelegate,
)


# ---------------------------------------------------------------------------
# 1. the modal overlays in isolation
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


async def test_confirm_modal_yes_returns_true() -> None:
    harness = _ModalHarness(ExtensionConfirmModal("Delete?", "Are you sure?"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#ext-confirm-yes")
        await pilot.pause()
    assert harness.result is True


async def test_confirm_modal_no_returns_false() -> None:
    harness = _ModalHarness(ExtensionConfirmModal("Delete?", "Are you sure?"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#ext-confirm-no")
        await pilot.pause()
    assert harness.result is False


async def test_confirm_modal_escape_returns_false() -> None:
    # Fail-Early: a cancelled confirm is a "no", never a hidden yes.
    harness = _ModalHarness(ExtensionConfirmModal("Delete?", "Are you sure?"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is False


async def test_select_modal_enter_returns_highlighted_item() -> None:
    harness = _ModalHarness(ExtensionSelectModal("Pick one", ["alpha", "beta", "gamma"]))
    async with harness.run_test() as pilot:
        await pilot.pause()
        option_list = harness.screen.query_one("#ext-select-list", OptionList)
        # Move the highlight to the second option, then Enter selects it.
        option_list.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == "beta"


async def test_select_modal_escape_returns_none() -> None:
    harness = _ModalHarness(ExtensionSelectModal("Pick one", ["alpha", "beta"]))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


async def test_input_modal_type_and_submit_returns_text() -> None:
    harness = _ModalHarness(ExtensionInputModal("Name?", default="draft"))
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
    harness = _ModalHarness(ExtensionInputModal("Name?", default="draft"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        assert harness.screen.query_one("#ext-input-field", Input).value == "draft"
        await pilot.click("#ext-input-ok")
        await pilot.pause()
    assert harness.result == "draft"


async def test_input_modal_escape_returns_none() -> None:
    harness = _ModalHarness(ExtensionInputModal("Name?", default="draft"))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


# ---------------------------------------------------------------------------
# 2. the delegate end-to-end (what an extension hook awaits)
# ---------------------------------------------------------------------------


class _DelegateHarness(App):
    """A bare app that hosts the ``_ExtensionUIDelegate`` modals.

    The delegate is app-agnostic at runtime (it only calls ``push_screen_wait`` /
    ``notify``), so this stands in for ``Parley`` without booting a backend.
    """


async def test_delegate_confirm_flows_answer_back() -> None:
    app = _DelegateHarness()
    delegate = _ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.confirm("Proceed?", "Run the command?")

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()  # let the modal mount
        await pilot.click("#ext-confirm-yes")
        await worker.wait()

    assert box["value"] is True


async def test_delegate_select_flows_choice_back() -> None:
    app = _DelegateHarness()
    delegate = _ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.select("Model?", ["fast", "smart"])

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()
        option_list = app.screen.query_one("#ext-select-list", OptionList)
        option_list.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await worker.wait()

    assert box["value"] == "smart"


async def test_delegate_input_cancel_returns_default() -> None:
    # The delegate contract is ``-> str``: a cancelled input resolves to the
    # default (the same value headless returns), never a fabricated "".
    app = _DelegateHarness()
    delegate = _ExtensionUIDelegate(app)  # type: ignore[arg-type]
    box: dict[str, object] = {}

    async with app.run_test() as pilot:
        await pilot.pause()

        async def call() -> None:
            box["value"] = await delegate.input("Branch name?", default="main")

        worker = app.run_worker(call(), exclusive=False)
        await pilot.pause()
        await pilot.press("escape")
        await worker.wait()

    assert box["value"] == "main"
