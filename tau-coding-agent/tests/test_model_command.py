"""``/model`` — the acceptance test for the capability/flow model, in the TUI.

§11 step 6 set the bar: if a command τ did not have costs more than a registry row
and a backend passthrough, the record shape is wrong. This file is the head half of
that check — the slash, the palette entry and the empty-argument step, none of which
needed a new modal or a new completion path.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input

from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_coding_agent import chat_widgets
from tau_coding_agent.backends import create_backend, make_model_resolver


@pytest.fixture
def app(make_app):
    """A TauApp wired to a REAL TauBackend, so `set_model` is the real passthrough."""
    return make_app(create_backend=create_backend)


def _submit(app, text: str):
    """Type ``text`` into the chat input and submit it, exactly as a human would."""
    editor = app.query_one("#chat-input", chat_widgets.ChatInput)
    return app.on_input_submitted(Input.Submitted(editor, text))


async def test_model_is_in_the_slash_vocabulary_and_the_palette(app):
    """One row, two surfaces — neither of which was edited to add it."""
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "model" in FRONTEND_COMMANDS
        titles = {command.title for command in app.get_system_commands(app.screen)}
        assert "/model" in titles


async def test_bare_slash_model_offers_the_legal_names(app, wait_for_workers_settled):
    """The flow's first STEP, rendered for a terminal: it OFFERS what is valid.

    The same answer `next_step` + `enumerate_domain` give a host over the wire, now
    shaped by `flow_form_spec` into the one form every head renders. It used to be a
    notification listing the names — see test_flow_form_rendering.py for the rest.
    """
    from tau_coding_agent import modals

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await _submit(app, "/model")
        for _ in range(6):
            await pilot.pause()

        assert isinstance(app.screen, modals.ExtensionFormScreen)


async def test_a_named_model_switches_and_an_unknown_one_says_so(app, wait_for_workers_settled):
    """The mutation, and its Fail-Early: a name the resolver rejects is reported."""
    notes: list[tuple[str, str]] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        known = next(iter(app.config["models"]))
        app.notify = lambda message, **kw: notes.append(  # type: ignore[method-assign]
            (message, kw.get("severity", ""))
        )

        wanted = make_model_resolver(app.config["models"])(known)

        await _submit(app, f"/model {known}")
        await pilot.pause()
        assert notes[-1][1] != "error"
        assert app.current_backend.agent_session.get_model()["id"] == wanted.id

        await _submit(app, "/model no-such-model")
        await pilot.pause()
        assert notes[-1][1] == "error"
        assert "Cannot switch model" in notes[-1][0]
        assert app.current_backend.agent_session.get_model()["id"] == wanted.id
