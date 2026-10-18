"""The flow form's single-choice field: buttons that stay visible, and Enter that chooses.

Driven through the real Textual runtime with ``tau.tcss`` loaded, because the
original fault was layout: 22 options in a radio column pushed the button row
out of a 90%-high dialog on any terminal under 38 rows, while the hint stayed.

Reference: docs/MODEL-PICKER.md §3–§5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.app import App
from textual.widgets import Button, Input

import tau_coding_agent
from tau_agent_core.extension_locks import REQUEST_ENTRY_TYPE, build_request_data, read_request
from tau_agent_core.extension_types import CHOICE_FILTER_THRESHOLD, validate_ask_spec
from tau_coding_agent import extension_ui, modals
from tau_coding_agent.themes import install_themes

MODELS = [f"model-{i:02d}" for i in range(22)]


def _spec(options: list[str], *extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Switch the active model",
        "fields": [{"name": "name", "kind": "select", "label": "model", "options": options}, *extra],
    }


class _Harness(App):
    """One modal over ``tau.tcss``, recording what it dismissed with."""

    CSS_PATH = str(Path(tau_coding_agent.__file__).with_name("tau.tcss"))

    def __init__(self, modal) -> None:
        super().__init__()
        install_themes(self)
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


def _inside(screen, frame_id: str, selector: str) -> bool:
    return screen.query_one(f"#{frame_id}").region.contains_region(
        screen.query_one(selector).region
    )


@pytest.mark.parametrize("rows", [16, 24, 30])
async def test_the_buttons_stay_on_screen_however_long_the_list(rows: int) -> None:
    harness = _Harness(modals.ExtensionFormScreen(_spec(MODELS)))
    async with harness.run_test(size=(100, rows)) as pilot:
        await pilot.pause()
        screen = harness.screen
        for selector in ("#ext-form-submit", "#ext-form-cancel", ".tau-dialog-hint"):
            assert _inside(screen, "ext-form-dialog", selector), (rows, selector)
        assert _inside(screen, "ext-form-dialog", ".tau-dialog-title")


async def test_a_short_list_has_no_filter_and_enter_on_it_submits() -> None:
    harness = _Harness(modals.ExtensionFormScreen(_spec(["a", "b", "c"])))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not harness.screen.query(".tau-choice-filter")
        assert isinstance(harness.screen.focused, modals.ChoiceList)
        await pilot.press("down", "enter")
        await pilot.pause()
    assert harness.result == {"name": "b"}


async def test_a_click_chooses_and_enter_then_submits() -> None:
    """The reported fault: click a model, press Enter, nothing happened."""
    harness = _Harness(modals.ExtensionFormScreen(_spec(["a", "b", "c"])))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        choices = harness.screen.query_one(modals.ChoiceList)
        await pilot.click(modals.ChoiceList, offset=(4, 3))
        await pilot.pause()
        assert choices.highlighted == 2
        assert harness.result == "UNSET", "a click chooses; it does not submit"
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == {"name": "c"}


async def test_enter_on_the_frame_submits() -> None:
    harness = _Harness(modals.ExtensionFormScreen(_spec(["a", "b"])))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        harness.screen.set_focus(None)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == {"name": "a"}


async def test_a_long_list_filters_as_you_type_and_enter_takes_the_first_match() -> None:
    assert len(MODELS) > CHOICE_FILTER_THRESHOLD
    harness = _Harness(modals.ExtensionFormScreen(_spec(MODELS)))
    async with harness.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        screen = harness.screen
        assert isinstance(screen.focused, Input), "a long list opens on its filter"
        await pilot.press(*"1")
        await pilot.pause()
        field = screen.query_one(modals.ChoiceField)
        assert field.shown == [m for m in MODELS if "1" in m]
        await pilot.press("down", "enter")
        await pilot.pause()
    assert harness.result == {"name": "model-10"}


async def test_a_filter_matching_nothing_submits_nothing() -> None:
    """Fail-Early in the UI: no fallback to the default the filter hid."""
    harness = _Harness(modals.ExtensionFormScreen(_spec(MODELS)))
    async with harness.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press(*"zzz")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.click("#ext-form-submit")
        await pilot.pause()
        assert isinstance(harness.screen, modals.ExtensionFormScreen)
    assert harness.result == "UNSET"


async def test_with_several_fields_enter_on_the_list_moves_on() -> None:
    spec = _spec(["a", "b"], {"name": "note", "kind": "text"})
    harness = _Harness(modals.ExtensionFormScreen(spec))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert harness.result == "UNSET"
        assert harness.screen.focused is harness.screen.query_one("#ext-form-field-1", Input)
        await pilot.press(*"hi", "enter")
        await pilot.pause()
        assert harness.result == "UNSET", "the last field moves to OK; it does not submit"
        assert isinstance(harness.screen.focused, Button)
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == {"name": "b", "note": "hi"}


def _ask(actions: list[str]):
    ask = validate_ask_spec(
        {
            "text": "Pick one.",
            "fields": [{"name": "env", "kind": "select", "options": ["dev", "prod"]}],
            "actions": [{"label": label, "command": f"go-{label}"} for label in actions],
        }
    )
    return read_request(
        {
            "type": "customEntry",
            "customType": REQUEST_ENTRY_TYPE,
            "id": "req-1",
            "data": build_request_data("/x/gate.py", "Choose an env.", lock=True, ask=ask),
        }
    )


async def test_an_ask_with_one_action_submits_on_enter() -> None:
    harness = _Harness(extension_ui.ExtensionAskScreen(_ask(["Go"])))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
    assert harness.result == ("Go", {"env": "prod"})


async def test_an_ask_with_two_actions_moves_to_them_rather_than_pick_one() -> None:
    harness = _Harness(extension_ui.ExtensionAskScreen(_ask(["Go", "Stop"])))
    async with harness.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert harness.result == "UNSET"
        assert isinstance(harness.screen.focused, extension_ui._AskActionButton)
