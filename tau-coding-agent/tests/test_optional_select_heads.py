"""An optional select may be left unanswered in the TUI form and at the REPL (#101)."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from textual.app import App

from tau_coding_agent import modals
from tau_coding_agent.repl import ReplRenderer, ask_fields
from tau_coding_agent.repl_input import INTERRUPT, MemoryReader

_SPEC = {
    "fields": [
        {"name": "verdict", "kind": "select", "options": ["fine", "wrong"], "required": False},
        {"name": "note", "kind": "text"},
    ]
}


async def test_the_tui_form_submits_an_unanswered_optional_select() -> None:
    results: list[Any] = []

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(modals.ExtensionFormScreen(_SPEC), results.append)

    async with Host().run_test() as pilot:
        await pilot.pause()
        choice = pilot.app.screen.query_one("#ext-form-field-0", modals.ChoiceField)
        assert choice.value is None
        pilot.app.screen.action_submit()
        await pilot.pause()
    assert results == [{"verdict": None, "note": ""}]


async def test_the_tui_form_still_answers_a_picked_option() -> None:
    results: list[Any] = []

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(modals.ExtensionFormScreen(_SPEC), results.append)

    async with Host().run_test() as pilot:
        await pilot.pause()
        choice = pilot.app.screen.query_one("#ext-form-field-0", modals.ChoiceField)
        choice.query_one(modals.ChoiceList).highlighted = 1
        pilot.app.screen.action_submit()
        await pilot.pause()
    assert results == [{"verdict": "wrong", "note": ""}]


async def test_the_tui_form_refuses_a_required_select_with_nothing_highlighted() -> None:
    results: list[Any] = []
    spec = {"fields": [{"name": "v", "kind": "select", "options": ["a", "b"]}]}

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(modals.ExtensionFormScreen(spec), results.append)

    async with Host().run_test() as pilot:
        await pilot.pause()
        pilot.app.screen.query_one(modals.ChoiceList).highlighted = None
        pilot.app.screen.action_submit()
        await pilot.pause()
    assert results == []


async def test_the_repl_takes_a_blank_answer_as_none() -> None:
    from tau_agent_core.extension_types import validate_form_spec

    _, fields = validate_form_spec(_SPEC)
    reader = MemoryReader(answers=["", "hi"])
    renderer = ReplRenderer(Console(record=True, width=100), reader, model_name="m")
    assert await ask_fields(reader, renderer, fields) == {"verdict": None, "note": "hi"}


async def test_the_repl_still_cancels_on_interrupt() -> None:
    from tau_agent_core.extension_types import validate_form_spec

    _, fields = validate_form_spec(_SPEC)
    reader = MemoryReader(answers=[INTERRUPT])
    renderer = ReplRenderer(Console(record=True, width=100), reader, model_name="m")
    assert await ask_fields(reader, renderer, fields) is None
