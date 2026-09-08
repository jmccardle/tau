"""An extension declaring what its command TAKES, and every head reading it.

Reference: docs/EXTENSION-FLOWS.md.

``register_command`` gives a command a name and a handler and says nothing about
its argument, so every head could only show the name and hand the handler whatever
was typed. ``register_flow`` adds the statement, in the vocabulary τ's own gestures
already use.

Two properties carry the design and most of this file is about them.

**The registry stays frozen; the extension arrives as a LAYER.** ``FLOWS`` and
``DOMAINS`` are module constants and ``BUILTIN`` is one immutable value over them;
a session's :attr:`AgentSession.vocabulary` is ``BUILTIN`` plus its own extensions.
A fork, a ``switch_session`` and a sub-agent are separate sessions in one process,
so a mutable global would have let one see another's flows — and ``next_step``
would have stopped being the pure function ``resolve_command``'s docstring says it
is.

**An overlay is checked by the same rules as τ's own tables.** ``extended_with``
synthesises each added flow's mutation as a private ``Capability`` and runs
``_check_registry`` over the combined tables, so an extension cannot declare
something a built-in could not.
"""

from __future__ import annotations

import pytest

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import BUILTIN, Argument, Domain
from tau_agent_core.commands import complete_command_argument, resolve_command
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.flows import FlowStep, Performed, Ready, enumerate_domain, next_step
from tau_agent_core.session_log import InMemorySessionLog

TODOS = [("t1", "Buy milk"), ("t2", "Ship the release"), ("t3", "Book flights")]


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


def _todo_domain() -> Domain:
    return Domain("todo_item", "An open todo.", enumerator="extension", field_kind="select")


def _register_todo(session: AgentSession, calls: list[str]) -> ExtensionAPI:
    """The worked example: one command, one argument, one enumerator."""
    api = session._bind_extension_api("/x/todo.py")

    def handler(args, ctx):
        calls.append(args)
        return f"marked {args} done"

    api.register_flow(
        "done",
        "mark a todo done",
        handler,
        argument=Argument("item", "todo_item", "Which todo."),
        domain=_todo_domain(),
        values=lambda query, limit: [t for t in TODOS if query.lower() in t[1].lower()][:limit],
    )
    return api


class TestTheLayerIsAdditiveAndIsolated:
    def test_the_built_in_registry_is_untouched(self):
        """The property a mutable global would have lost. ``BUILTIN`` is what a
        session with no extensions reads, and it is the same object every time."""
        before = len(BUILTIN.flows)
        session = _session()
        _register_todo(session, [])

        assert len(session.vocabulary.flows) == before + 1
        assert len(BUILTIN.flows) == before
        assert BUILTIN.extension_flows == frozenset()

    def test_two_sessions_do_not_see_each_other(self):
        """A fork, a switch_session and a sub-agent are separate sessions in one
        process. This is the reason the vocabulary is a value and not a global."""
        mine, theirs = _session(), _session()
        _register_todo(mine, [])

        assert mine.vocabulary.flow("done") is not None
        assert theirs.vocabulary.flow("done") is None
        assert theirs.vocabulary is BUILTIN

    def test_a_session_with_no_flows_pays_nothing(self):
        assert _session().vocabulary is BUILTIN

    def test_the_vocabulary_follows_a_registration(self):
        session = _session()
        assert session.vocabulary is BUILTIN
        _register_todo(session, [])
        assert session.vocabulary is not BUILTIN
        assert "done" in session.vocabulary.extension_flows

    def test_unregistering_the_command_drops_the_flow(self):
        """A disabled extension must not leave a flow a head can step and nothing
        can perform."""
        session = _session()
        _register_todo(session, [])
        session._registry.unregister_command("done")

        assert session.vocabulary.flow("done") is None
        assert session.vocabulary is BUILTIN


class TestTheOverlayIsCheckedLikeTheBuiltIns:
    def test_a_name_tau_already_declares_is_refused(self):
        """``resolve_command`` gives a collision to the built-in, so the added flow
        would be unreachable by name — advertised and undispatchable."""
        session = _session()
        api = session._bind_extension_api("/x/rogue.py")
        with pytest.raises(ValueError, match="already declared by"):
            api.register_flow("compact", "not this one", lambda args, ctx: None)

    def test_a_view_name_is_refused_too(self):
        session = _session()
        api = session._bind_extension_api("/x/rogue.py")
        with pytest.raises(ValueError, match="already declared by"):
            api.register_flow("tree", "not this one", lambda args, ctx: None)

    def test_an_enumerated_domain_needs_its_callable(self):
        """Fail-Early: without one the step offers nothing, and an empty list reads
        as 'there are none' rather than as a missing registration."""
        session = _session()
        api = session._bind_extension_api("/x/todo.py")
        with pytest.raises(ValueError, match="needs a 'values' callable"):
            api.register_flow(
                "done",
                "mark a todo done",
                lambda args, ctx: None,
                argument=Argument("item", "todo_item", "Which todo."),
                domain=_todo_domain(),
            )

    def test_the_domain_must_be_the_one_the_argument_names(self):
        session = _session()
        api = session._bind_extension_api("/x/todo.py")
        with pytest.raises(ValueError, match="argument names"):
            api.register_flow(
                "done",
                "mark a todo done",
                lambda args, ctx: None,
                argument=Argument("item", "todo_item", "Which todo."),
                domain=Domain("other", "Something else.", free=True),
                values=None,
            )

    def test_a_handler_is_required(self):
        session = _session()
        api = session._bind_extension_api("/x/todo.py")
        with pytest.raises(ValueError, match="callable 'handler'"):
            api.register_flow("done", "mark a todo done", "not callable")

    def test_a_built_in_domain_may_be_reused(self):
        """An extension acting on a model or a session need not declare a domain."""
        session = _session()
        api = session._bind_extension_api("/x/bench.py")
        api.register_flow(
            "bench",
            "benchmark a configured model",
            lambda args, ctx: f"benched {args}",
            argument=Argument("name", "model_name", "Which model."),
        )
        assert session.vocabulary.flow("bench").arguments[0].domain == "model_name"


class TestTheFlowLoopRunsIt:
    def test_a_bare_command_is_a_step_not_a_turn(self):
        session = _session()
        _register_todo(session, [])
        step = next_step("done", {}, vocabulary=session.vocabulary)

        assert isinstance(step, FlowStep)
        assert step.argument.name == "item"
        assert step.domain.name == "todo_item"

    def test_the_declared_callable_answers_the_domain(self):
        session = _session()
        _register_todo(session, [])
        found = enumerate_domain("todo_item", vocabulary=session.vocabulary)

        assert [(v.value, v.label) for v in found.values] == TODOS

    def test_the_enumerator_receives_the_query(self):
        session = _session()
        _register_todo(session, [])
        found = enumerate_domain("todo_item", query="ship", vocabulary=session.vocabulary)

        assert [v.value for v in found.values] == ["t2"]

    def test_a_bound_argument_is_ready(self):
        session = _session()
        _register_todo(session, [])
        ready = next_step("done", {"item": "t1"}, vocabulary=session.vocabulary)

        assert isinstance(ready, Ready)
        assert ready.arguments == {"item": "t1"}

    async def test_dispatch_runs_the_handler_with_the_bound_value(self):
        """The handler contract does not change: it still takes ``(args, ctx)``."""
        session = _session()
        calls: list[str] = []
        _register_todo(session, calls)

        invocation = resolve_command("/done t1", session._registry.get_commands().keys())
        outcome = await session._perform_command(invocation)

        assert calls == ["t1"]
        assert isinstance(outcome, Performed)
        assert outcome.data["output"] == "marked t1 done"

    async def test_a_bare_command_reaches_the_head_as_a_step(self):
        """The whole point: ``/done`` with no argument no longer runs the handler
        with an empty string — it asks."""
        session = _session()
        calls: list[str] = []
        _register_todo(session, calls)

        invocation = resolve_command("/done", session._registry.get_commands().keys())
        outcome = await session._perform_command(invocation)

        assert isinstance(outcome, FlowStep)
        assert calls == []

    async def test_a_command_with_no_declaration_is_unchanged(self):
        """``register_command`` keeps its old behaviour exactly: the raw line goes
        to the handler and the handler runs immediately."""
        session = _session()
        seen: list[str] = []
        api = session._bind_extension_api("/x/note.py")
        api.register_command(
            "note", {"description": "jot", "handler": lambda args, ctx: seen.append(args)}
        )

        invocation = resolve_command("/note buy milk", session._registry.get_commands().keys())
        outcome = await session._perform_command(invocation)

        assert seen == ["buy milk"]
        assert isinstance(outcome, Performed)


class TestCompletionSeesIt:
    def test_the_argument_completes_like_a_built_in(self):
        session = _session()
        _register_todo(session, [])
        slot = complete_command_argument("/done Buy", session.vocabulary)

        assert slot is not None
        assert (slot.command, slot.domain.name, slot.query) == ("done", "todo_item", "Buy")

    def test_a_chosen_value_resolves_to_that_value(self):
        session = _session()
        _register_todo(session, [])
        text = "/done Buy"
        slot = complete_command_argument(text, session.vocabulary)
        chosen = text[: slot.start] + "t1" + text[slot.end :]

        invocation = resolve_command(chosen, session._registry.get_commands().keys())
        assert (invocation.name, invocation.args) == ("done", "t1")

    def test_an_undeclared_command_offers_nothing(self):
        """No ``register_flow``, nothing to say about the argument — the same
        ``None`` as before this existed."""
        session = _session()
        api = session._bind_extension_api("/x/note.py")
        api.register_command("note", {"description": "jot", "handler": lambda a, c: None})

        assert complete_command_argument("/note buy", session.vocabulary) is None

    def test_the_default_vocabulary_knows_nothing_of_it(self):
        """A caller that does not pass the session's vocabulary reads τ's own, which
        is what makes every existing call site keep working."""
        session = _session()
        _register_todo(session, [])

        assert complete_command_argument("/done Buy") is None
