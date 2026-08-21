"""B2-b: the TUI performs a command the CORE decided — and says so when it cannot.

Reference: docs/SUBMISSION-LIFECYCLE.md ``submit()`` step 3 / phase 3.

``on_input_submitted`` used to own the whole slash-command block: ``/compact``,
``/tree``, ``/fork``, ``/extensions``, ``/extensions <verb> <target>``, and
extension-registered ``/name args`` were all intercepted inside a Textual event handler,
which is why no other input source had a command vocabulary at all. That block is gone.
The decision is now :func:`tau_agent_core.commands.resolve_command`, taken inside
``AgentSession.submit``; this app receives a typed
:class:`~tau_agent_core.commands.CommandOutcome` and does the half only a TUI can do.

What is pinned here:

* every slash command that worked before still works, END TO END through the real app
  (Pilot + a real ``TauBackend``, so the dispatch really runs through ``submit()``);
* an extension-registered command still dispatches, and its output still renders as
  display-only chrome that never enters the model-input working list;
* the FAILURE mode: an outcome this frontend cannot perform RAISES rather than returning
  as though it had. That is the Fail-Early half of the core-decides/frontend-performs
  split — without it, ``FRONTEND_COMMANDS`` would be a list of things that may or may
  not work depending on where you typed them.

The core-side dispatch (including the ``expand_commands`` security boundary: a bus
payload's "/compact" is literal prompt text) is
tau-agent-core/tests/test_submit_commands.py's.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from tau_agent_core.commands import CommandOutcome, UnsupportedCommandError

from tau_coding_agent.app import ChatDisplay, ChatInput, MessageBox
from tau_coding_agent.backends import create_backend

# An extension registering a command whose handler RETURNS a report string — the S46
# output channel, which is what a ``performer="core"`` outcome carries.
_TODOS_EXT = """
def register(api):
    def _todos(args, ctx):
        return "# Todos\\n- " + (args or "nothing")

    api.register_command("todos", {"description": "list todos", "handler": _todos})
"""


@pytest.fixture
def app(make_app):
    """A Parley wired to REAL TauBackends (TauBackend has no network in __init__)."""
    return make_app(create_backend=create_backend)


def _submit(app, text: str):
    """Type ``text`` into the chat input and submit it, exactly as a human would."""
    chat_input = app.query_one("#chat-input", ChatInput)
    return app.on_input_submitted(Input.Submitted(chat_input, text))


# ── every built-in that worked before still works ─────────────────────────────


async def test_slash_compact_reaches_action_compact(app):
    """The command that motivated the interception in the first place: without it,
    "/compact" was sent as a prompt and the model played along."""
    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _compact():
            calls.append("compact")

        app.action_compact = _compact  # type: ignore[method-assign]

        await _submit(app, "/compact")
        await pilot.pause()

        assert calls == ["compact"]
        # Chrome, not conversation: no user turn was appended to model input.
        assert all(m.get("content") != "/compact" for m in app.messages)


async def test_slash_tree_and_slash_fork_both_open_the_browser(app):
    """pi aliases the two (keybindings.ts:252-253); both must still land."""
    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.action_browse_tree = lambda: calls.append("browse")  # type: ignore[method-assign]

        await _submit(app, "/tree")
        await pilot.pause()
        await _submit(app, "/fork")
        await pilot.pause()

        assert calls == ["browse", "browse"]


async def test_slash_extensions_lists(app):
    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.action_show_extensions = lambda: calls.append("show")  # type: ignore[method-assign]

        await _submit(app, "/extensions")
        await pilot.pause()

        assert calls == ["show"]


async def test_slash_extensions_with_a_verb_manages(app):
    """``/extensions disable <name>`` — the verb/target split survives the move.

    Argument splitting now happens once, in the core's ``parse_command`` (first space)
    plus this app's verb/target split, rather than in a hand-rolled
    ``message[len("/extensions "):]`` slice.
    """
    calls: list[tuple[str, str]] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _manage(verb, target):
            calls.append((verb, target))

        app.action_manage_extensions = _manage  # type: ignore[method-assign]

        await _submit(app, "/extensions disable my_ext.py")
        await pilot.pause()

        assert calls == [("disable", "my_ext.py")]


# ── an extension-registered command still dispatches, output still chrome ─────


async def test_extension_command_dispatches_and_renders_display_only_output(app, tmp_path):
    ext = tmp_path / "todos_ext.py"
    ext.write_text(_TODOS_EXT)
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        messages_before = list(app.messages)

        await _submit(app, "/todos mine")
        await pilot.pause()

        boxes = [
            box
            for box in app.query(MessageBox)
            if box.role == "system" and box._content == "# Todos\n- mine"
        ]
        assert len(boxes) == 1, "the handler's returned report is not on screen"
        # Display-only: it did NOT enter the working list that becomes model input.
        assert app.messages == messages_before


async def test_a_built_in_wins_over_an_extension_that_registers_the_same_name(app, tmp_path):
    """Resolution order is the core's, and it matches what the old block did."""
    ext = tmp_path / "shadow_ext.py"
    ext.write_text(
        "def register(api):\n"
        "    api.register_command('compact', {'description': 'nope', "
        "'handler': lambda args, ctx: 'shadowed'})\n"
    )
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _compact():
            calls.append("compact")

        app.action_compact = _compact  # type: ignore[method-assign]

        await _submit(app, "/compact")
        await pilot.pause()

        assert calls == ["compact"], "an extension must not shadow a built-in"


# ── the Fail-Early half: an outcome this frontend cannot perform ──────────────


async def test_an_outcome_this_frontend_cannot_perform_raises(app):
    """The contract that makes the core/frontend split safe.

    The core is allowed to resolve a command a given frontend cannot perform — that is
    the whole reason ``FRONTEND_COMMANDS`` is a core constant rather than a per-frontend
    registry. What keeps that honest is this: the frontend says so out loud. A silent
    ``else: pass`` here would make "/tree works" depend on where you typed it, with no
    trace anywhere that it did not.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        with pytest.raises(UnsupportedCommandError, match="cannot perform it"):
            await app._perform_command_outcome(
                CommandOutcome(name="hologram", args="", performer="frontend")
            )


async def test_a_core_performed_outcome_with_no_output_shows_nothing(app):
    """A command that ran and had nothing to say is not an error and not a box."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        before = len(app.query(ChatDisplay).first().query(MessageBox))
        await app._perform_command_outcome(
            CommandOutcome(name="ping", args="", performer="core", output=None)
        )
        await pilot.pause()

        assert len(app.query(ChatDisplay).first().query(MessageBox)) == before
