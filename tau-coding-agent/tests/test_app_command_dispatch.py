"""B2-b: the TUI performs a command the CORE decided — and says so when it cannot.

Reference: docs/SUBMISSION-LIFECYCLE.md ``submit()`` step 3 / phase 3.

``on_input_submitted`` used to own the whole slash-command block: ``/compact``,
``/tree``, ``/fork``, ``/extensions``, ``/extensions <verb> <target>``, and
extension-registered ``/name args`` were all intercepted inside a Textual event handler,
which is why no other input source had a command vocabulary at all. That block is gone.
The decision is now :func:`tau_agent_core.commands.resolve_command`, taken inside
``AgentSession.submit``; this app receives a typed
:class:`~tau_agent_core.flows.Dispatched` and does the half only a TUI can do.

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

from tau_agent_core.commands import UnsupportedCommandError
from tau_agent_core.flows import Performed, View

from tau_coding_agent.backends import create_backend
from tau_coding_agent import chat_widgets, transcript

_TODOS_EXT = """
def register(api):
    def _todos(args, ctx):
        return "# Todos\\n- " + (args or "nothing")

    api.register_command("todos", {"description": "list todos", "handler": _todos})
"""


@pytest.fixture
def app(make_app):
    """A TauApp wired to REAL TauBackends (TauBackend has no network in __init__)."""
    return make_app(create_backend=create_backend)


def _performed(mutation: str, data: dict) -> Performed:
    """A stub backend's answer, shaped the way a real one now answers.

    The heads require the record rather than stringifying whatever came back, so a
    double that returns None or a bare value is refused — which is the check these
    tests are standing in front of.
    """
    return Performed(flow=None, mutation=mutation, data={**data, "cursor": None}, cursor=None)


def _submit(app, text: str):
    """Type ``text`` into the chat input and submit it, exactly as a human would."""
    chat_input = app.query_one("#chat-input", chat_widgets.ChatInput)
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

        async def _compact(custom_instructions: str = ""):
            calls.append(custom_instructions)

        app.action_compact = _compact  # type: ignore[method-assign]

        await _submit(app, "/compact")
        await pilot.pause()

        assert calls == [""]
        # Chrome, not conversation: no user turn was appended to model input.
        assert all(m.get("content") != "/compact" for m in app.messages)


async def test_slash_compact_carries_its_argument_to_the_action(app):
    """The ``compact`` flow declares an optional ``custom_instructions``; until
    2026-09-04 the dispatch called ``action_compact()`` with nothing, so the
    registry said the command took an argument and the head threw it away."""
    seen: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _compact(custom_instructions: str = ""):
            seen.append(custom_instructions)

        app.action_compact = _compact  # type: ignore[method-assign]

        await _submit(app, "/compact focus on the auth bug")
        await pilot.pause()

        assert seen == ["focus on the auth bug"]


async def test_compact_hands_the_focus_to_the_backend_and_strips_it_to_none(app):
    """Both halves of the conversion, at the one seam that performs it.

    Empty stays ``None`` rather than becoming ``""``: the summarizer appends an
    "Additional focus" paragraph for any truthy value, so an empty focus line
    would be a paragraph saying nothing (``compaction.py:497``).
    """
    seen: list[tuple[int, str | None]] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _compact_messages(messages, custom_instructions=None):
            seen.append((len(messages), custom_instructions))
            return None

        app.current_backend.compact_messages = _compact_messages  # type: ignore[method-assign]

        await app.action_compact("  keep the API decisions  ")
        await app.action_compact()
        await pilot.pause()

        assert [focus for _, focus in seen] == ["keep the API decisions", None]


async def test_slash_tree_opens_the_browser_and_slash_fork_no_longer_does(app):
    """The two were aliases; `fork` now means what the RPC verb of that name means.

    One word for two capabilities was the collision the flow model could not carry:
    the slash opened a browser while `COMMAND_TABLE["fork"]` branched the session.
    The wire verb keeps the name because hosts depend on it, so the slash moved onto
    the same meaning rather than to a third name.
    """
    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.action_browse_tree = lambda: calls.append("browse")  # type: ignore[method-assign]

        async def _fork():
            calls.append("fork")

        app.action_fork_session = _fork  # type: ignore[method-assign]

        await _submit(app, "/tree")
        await pilot.pause()
        await _submit(app, "/fork")
        await pilot.pause()

        assert calls == ["browse", "fork"]


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

    The split now happens once, in the CORE: ``/extensions`` resolves to a ``View``,
    which carries no argument string, so the sugar had to move there or lose its
    target. What arrives here is the ``disable_extension`` flow, already bound.
    """
    calls: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _disable(path):
            calls.append(path)
            return _performed(
                "disable_extension",
                {"action": "disable", "path": path, "ok": True, "message": "off"},
            )

        app.current_backend.disable_extension = _disable  # type: ignore[method-assign]

        await _submit(app, "/extensions disable my_ext.py")
        await pilot.pause()

        assert calls == ["my_ext.py"]


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
            for box in app.query(chat_widgets.MessageBox)
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

        async def _compact(custom_instructions: str = ""):
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
                View(name="hologram", unavailable_because="this app has no such surface")
            )


async def test_a_core_performed_outcome_with_no_output_shows_nothing(app):
    """A command that ran and had nothing to say is not an error and not a box."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        before = len(app.query(transcript.ChatDisplay).first().query(chat_widgets.MessageBox))
        await app._perform_command_outcome(
            Performed(flow=None, mutation="ping", data={"output": None})
        )
        await pilot.pause()

        assert (
            len(app.query(transcript.ChatDisplay).first().query(chat_widgets.MessageBox)) == before
        )


# ── the three extension flows, and the view that is sugar over them ───────────


async def test_each_extension_flow_has_its_own_slash_name(app):
    """`/extensions <verb>` was one command with a verb argument; now there are three.

    The verb form is kept as head-local sugar, so both spellings reach the same flow.
    """
    ran: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _disable(path):
            ran.append(path)
            return _performed(
                "disable_extension",
                {"action": "disable", "path": path, "ok": True, "message": "off"},
            )

        app.current_backend.disable_extension = _disable  # type: ignore[method-assign]

        await _submit(app, "/disable_extension my_ext.py")
        await pilot.pause()
        await _submit(app, "/extensions disable my_ext.py")
        await pilot.pause()

        assert ran == ["my_ext.py", "my_ext.py"]


async def test_a_bound_flow_calls_the_mutation_by_its_own_parameter_name(app):
    """`Ready.arguments` is keyed for the mutation, so the head splats it."""
    called: list[dict] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        async def _disable(path):
            called.append({"path": path})
            return _performed(
                "disable_extension",
                {
                    "action": "disable",
                    "path": path,
                    "ok": True,
                    "message": "disabled my_ext",
                },
            )

        app.current_backend.disable_extension = _disable  # type: ignore[method-assign]
        await app.action_run_extension_flow("disable_extension", "my_ext.py")
        await pilot.pause()

        assert called == [{"path": "my_ext.py"}]


async def test_a_bare_flow_offers_the_domain_instead_of_failing(app):
    """An unbound required argument is a STEP, and the head renders it as a form.

    The labels are the enumerator's, not the head's: ``extension_name`` says whether
    each one is currently enabled, and that string is what the reader picks.
    """
    from textual.widgets import RadioButton, RadioSet

    from tau_coding_agent import modals

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.current_backend.agent_session.list_managed_extensions = lambda: [  # type: ignore[method-assign]
            ("/x/a.py", True),
            ("/x/b.py", False),
        ]

        await app.action_run_extension_flow("reload_extension")
        for _ in range(6):
            await pilot.pause()

        screen = app.screen
        assert isinstance(screen, modals.ExtensionFormScreen)
        buttons = screen.query_one("#ext-form-field-0", RadioSet).query(RadioButton)
        assert [str(button.label) for button in buttons] == [
            "/x/a.py (enabled)",
            "/x/b.py (disabled)",
        ]


async def test_an_unknown_verb_names_the_legal_ones_from_the_registry(app):
    """The view's verb table is derived, so it cannot offer one the core lacks."""
    said: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.notify = lambda message, **kw: said.append(message)  # type: ignore[method-assign]
        await app.action_manage_extensions("explode", "my_ext.py")
        await pilot.pause()

        assert said == ["Unknown /extensions action 'explode' (use: disable | enable | reload)"]


# ── the two session flows, driven by the generic performer ────────────────────


async def test_slash_name_reaches_set_session_name_with_what_was_typed(app):
    """`/name` is a registry row plus a backend passthrough, and nothing else.

    Mutation this kills: binding `outcome.args.split()[0]`, which would name a
    session "the" instead of "the refactor".
    """
    named: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        def _name(name):
            named.append(name)
            return _performed("set_session_name", {"name": name})

        app.current_backend.set_session_name = _name  # type: ignore[method-assign]
        app.notify = lambda message, **kw: None  # type: ignore[method-assign]

        await _submit(app, "/name the refactor")
        await pilot.pause()

        assert named == ["the refactor"]


async def test_slash_name_redraws_the_sidebar(app):
    """The rename is persisted, so the sidebar was correct again after a restart
    and wrong until then — the toast said it had worked and the one surface
    showing session names went on showing the old one."""
    refreshed: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.current_backend.set_session_name = lambda name: _performed(  # type: ignore[method-assign]
            "set_session_name", {"name": name}
        )
        app.notify = lambda message, **kw: None  # type: ignore[method-assign]
        sidebar = app.query_one(chat_widgets.ChatSidebar)
        sidebar.refresh_chats = lambda: refreshed.append(True)  # type: ignore[method-assign]

        await _submit(app, "/name the refactor")
        await pilot.pause()

        assert refreshed == [True]


async def test_slash_autocompact_hands_the_mutation_a_real_bool(app):
    """`bind_text` converts, so the backend is not handed the truthy string "false".

    Mutation this kills: passing `outcome.args` through unconverted, which turns
    `/autocompact false` into `set_auto_compaction(enabled="false")` — on.
    """
    got: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        def _autocompact(enabled):
            got.append(enabled)
            return _performed("set_auto_compaction", {"enabled": enabled})

        app.current_backend.set_auto_compaction = _autocompact  # type: ignore[method-assign]
        app.notify = lambda message, **kw: None  # type: ignore[method-assign]

        await _submit(app, "/autocompact false")
        await pilot.pause()

        assert got == [False]
        assert got[0] is False


async def test_a_word_the_boolean_domain_does_not_declare_is_reported_not_coerced(app):
    """Fail-Early at the head: `/autocompact yes` must not quietly mean off."""
    said: list[str] = []
    got: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.current_backend.set_auto_compaction = lambda enabled: got.append(enabled)  # type: ignore[method-assign]
        app.notify = lambda message, **kw: said.append(message)  # type: ignore[method-assign]

        await _submit(app, "/autocompact yes")
        await pilot.pause()

        assert got == []
        assert said == ["'enabled' takes one of true, false — got 'yes'"]


async def test_a_bare_session_flow_asks_for_its_argument_in_the_domains_widget(app):
    """An empty argument is a STEP rendered as a field, never a guessed default.

    The widget comes from the domain: ``boolean`` is a checkbox, ``text`` is a box
    to type in. Neither mapping is written in this head.
    """
    from textual.widgets import Checkbox, Input

    from tau_coding_agent import modals

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        await app.action_run_session_flow("autocompact")
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, modals.ExtensionFormScreen)
        assert app.screen.query_one("#ext-form-field-0", Checkbox)
        app.screen.dismiss(None)
        await pilot.pause()

        await app.action_run_session_flow("name")
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, modals.ExtensionFormScreen)
        assert app.screen.query_one("#ext-form-field-0", Input)


async def test_a_backend_without_the_mutation_says_so_out_loud(app, monkeypatch):
    """The Fail-Early half of the command seam, at the generic performer.

    A `/name` that returned as though it had worked is exactly the failure class
    `tau_agent_core.commands` exists to remove.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        monkeypatch.delattr(type(app.current_backend), "set_session_name")

        with pytest.raises(UnsupportedCommandError):
            await app.action_run_session_flow("name", "anything")
