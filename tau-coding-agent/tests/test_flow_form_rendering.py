"""A flow's missing arguments are asked for in the same form an extension gets.

Before this, ``_render_flow_step`` resolved an enumerable domain to its legal values
and printed them in a notification, while an extension calling ``ui.form`` got a
real modal. The registry knew every model name and rendered them as prose.

Reference: docs/TUI-STYLE-GUIDE.md §2.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input, RadioButton, RadioSet

from tau_coding_agent import chat_widgets, modals
from tau_coding_agent.backends import create_backend, make_model_resolver


@pytest.fixture
def app(make_app):
    """An app wired to a REAL TauBackend, so `set_model` is the real passthrough."""
    return make_app(create_backend=create_backend)


def _model_id(app, name: str) -> str:
    """The model id `name` resolves to, read the way the backend resolves it."""
    return make_model_resolver(app.config["models"])(name).id


def _active_model_id(app) -> str:
    return app.current_backend.agent_session.get_model()["id"]


def _submit(app, text: str):
    editor = app.query_one("#chat-input", chat_widgets.ChatInput)
    return app.on_input_submitted(Input.Submitted(editor, text))


async def _settle_on_modal(pilot):
    """Let the flow-step worker reach its modal.

    Never `wait_for_workers_settled` here: that worker is PARKED on the screen the
    test has not answered yet, so settling would wait for the test itself.
    """
    for _ in range(6):
        await pilot.pause()


async def _ready(app, pilot, wait_for_workers_settled):
    await pilot.pause()
    await app.action_new_chat()
    await pilot.pause()
    await wait_for_workers_settled(app)


async def test_bare_slash_model_opens_a_form_offering_the_legal_names(
    app, wait_for_workers_settled
):
    """The values the registry can compute are OFFERED, not described."""
    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        resolver = app.current_backend.agent_session.model_resolver
        expected = list(resolver.model_names())

        await _submit(app, "/model")
        await _settle_on_modal(pilot)

        screen = app.screen
        assert isinstance(screen, modals.ExtensionFormScreen)
        buttons = screen.query_one("#ext-form-field-0", RadioSet).query(RadioButton)
        assert [str(button.label) for button in buttons] == expected


async def test_the_forms_title_and_label_come_from_the_registry(app, wait_for_workers_settled):
    """Nothing in the head writes this text; the flow and its argument do."""
    from tau_agent_core.capabilities import FLOWS

    flow = next(f for f in FLOWS if f.name == "model")
    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        await _submit(app, "/model")
        await _settle_on_modal(pilot)

        screen = app.screen
        assert screen.dialog_title() == flow.description
        assert screen._fields[0]["label"] == flow.arguments[0].description


async def test_answering_the_form_performs_the_mutation(app, wait_for_workers_settled):
    """The form's answer is bound and the flow reaches Ready, in one gesture."""
    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        resolver = app.current_backend.agent_session.model_resolver
        target = list(resolver.model_names())[-1]
        wanted = _model_id(app, target)

        await _submit(app, "/model")
        await _settle_on_modal(pilot)

        screen = app.screen
        assert isinstance(screen, modals.ExtensionFormScreen)
        index = list(resolver.model_names()).index(target)
        screen.query_one("#ext-form-field-0", RadioSet).query(RadioButton)[index].value = True
        screen.dismiss(screen._collect())
        await pilot.pause()
        await wait_for_workers_settled(app)

        assert _active_model_id(app) == wanted


async def test_cancelling_the_form_performs_nothing(app, wait_for_workers_settled):
    """Fail-Early: a cancelled form is not a fabricated answer."""
    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        before = _active_model_id(app)

        await _submit(app, "/model")
        await _settle_on_modal(pilot)

        app.screen.dismiss(None)
        await pilot.pause()
        await wait_for_workers_settled(app)

        assert _active_model_id(app) == before


async def test_a_named_model_still_skips_the_form(app, wait_for_workers_settled):
    """A bound argument is never asked for; `/model <name>` is still one gesture."""
    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        target = list(app.current_backend.agent_session.model_resolver.model_names())[-1]
        wanted = _model_id(app, target)

        await _submit(app, f"/model {target}")
        await pilot.pause()
        await wait_for_workers_settled(app)

        assert not isinstance(app.screen, modals.ExtensionFormScreen)
        assert _active_model_id(app) == wanted


async def test_session_id_still_opens_the_picker_not_a_text_field(app, wait_for_workers_settled):
    """The permitted substitution: a head may offer something RICHER than the kind."""
    from tau_coding_agent import session_picker

    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        await _submit(app, "/resume")
        await _settle_on_modal(pilot)

        assert isinstance(app.screen, session_picker.SessionPickerModal)


async def test_autocompact_asks_with_a_checkbox(app, wait_for_workers_settled):
    """The boolean domain renders as `confirm`, so the question is a checkbox."""
    from textual.widgets import Checkbox

    async with app.run_test() as pilot:
        await _ready(app, pilot, wait_for_workers_settled)
        await _submit(app, "/autocompact")
        await _settle_on_modal(pilot)

        screen = app.screen
        assert isinstance(screen, modals.ExtensionFormScreen)
        assert screen.query_one("#ext-form-field-0", Checkbox)
