"""The flow loop: ``next_step`` until ``Ready``, and ``enumerate_domain`` beside it.

``next_step`` is pure, so most of it is exercised directly. ``enumerate_domain``
dispatches to live readers, so the tests that touch those use stand-ins shaped like
the objects the real readers get — the point being that the dispatch is right and
that a missing dependency RAISES rather than reporting an empty domain.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.capabilities import DOMAINS, FLOWS
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.capabilities import Argument
from tau_agent_core.flows import (
    DomainValues,
    FlowStep,
    Ready,
    UnknownFlowError,
    bind_text,
    enumerate_domain,
    next_step,
)


class TestNextStep:
    def test_an_unbound_required_argument_is_the_step(self):
        step = next_step("resume")
        assert isinstance(step, FlowStep)
        assert step.argument.name == "session_id"
        assert step.domain is DOMAINS["session_id"]
        assert step.bound == {}

    def test_binding_it_makes_the_flow_ready(self):
        ready = next_step("resume", {"session_id": "abc123"})
        assert ready == Ready(
            flow="resume", mutation="switch_session", arguments={"session_id": "abc123"}
        )

    def test_only_required_arguments_block(self):
        """/compact takes optional instructions, so bare /compact runs immediately."""
        ready = next_step("compact")
        assert isinstance(ready, Ready)
        assert ready.arguments == {}

    def test_an_optional_argument_rides_along_when_bound(self):
        ready = next_step("compact", {"custom_instructions": "focus on the tests"})
        assert isinstance(ready, Ready)
        assert ready.arguments == {"custom_instructions": "focus on the tests"}

    @pytest.mark.parametrize("flow", ["enable_extension", "disable_extension", "reload_extension"])
    def test_each_extension_flow_names_its_own_mutation(self, flow):
        """Three flows, not one flow with a verb argument. `/extensions` is the view."""
        first = next_step(flow)
        assert isinstance(first, FlowStep) and first.argument.name == "path"
        ready = next_step(flow, {"path": "/x/ext.py"})
        assert ready == Ready(flow=flow, mutation=flow, arguments={"path": "/x/ext.py"})

    def test_the_mutation_does_not_depend_on_what_was_bound(self):
        """Which mutation runs is a property of the flow named, never of the values."""
        for flow in FLOWS:
            outcomes = [
                next_step(flow.name, dict.fromkeys((a.name for a in flow.arguments), v))
                for v in ("a", "b")
            ]
            assert {o.mutation for o in outcomes} == {flow.mutation}

    def test_extensions_is_not_a_flow(self):
        with pytest.raises(UnknownFlowError, match="no flow named 'extensions'"):
            next_step("extensions")

    def test_an_unknown_flow_raises_rather_than_answering_none(self):
        """Fail-Early: None would be indistinguishable from a fully-bound flow."""
        with pytest.raises(UnknownFlowError, match="no flow named 'tree'"):
            next_step("tree")

    def test_the_cursor_is_echoed_onto_the_step(self):
        step = next_step("resume", {}, "entry-42")
        assert isinstance(step, FlowStep) and step.cursor == "entry-42"

    def test_it_mutates_nothing_the_caller_passed(self):
        bound: dict[str, Any] = {}
        next_step("resume", bound)
        assert bound == {}

    @pytest.mark.parametrize("flow", [f.name for f in FLOWS])
    def test_every_declared_flow_reports_a_first_step_or_is_ready(self, flow):
        assert isinstance(next_step(flow), (FlowStep, Ready))


class _Resolver:
    def __init__(self, names):
        self._names = names

    def model_names(self):
        return list(self._names)

    def __call__(self, name):  # pragma: no cover - not reached by these tests
        raise AssertionError


class _Log:
    def __init__(self, entries, cursor):
        self._entries = entries
        self.cursor = cursor

    def entries(self):
        return list(self._entries)


class _Session:
    def __init__(self, *, resolver=None, extensions=(), log=None):
        self.model_resolver = resolver
        self._extensions = list(extensions)
        self.session_log = log

    def list_managed_extensions(self):
        return list(self._extensions)


def _entries():
    return [
        {
            "id": "e1",
            "parentId": None,
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": "run the tests"},
        },
        {
            "id": "e2",
            "parentId": "e1",
            "type": "message",
            "timestamp": 2,
            "message": {"role": "assistant", "content": "they pass"},
        },
    ]


class TestEnumerateDomain:
    def test_a_free_domain_has_no_values(self):
        found = enumerate_domain("text")
        assert found == DomainValues(domain="text", values=(), total=0)

    def test_a_fixed_domain_needs_neither_session_nor_runtime(self):
        found = enumerate_domain("boolean")
        assert [v.value for v in found.values] == ["true", "false"]
        assert found.total == 2

    def test_model_names_come_from_the_bound_resolver(self):
        session = _Session(resolver=_Resolver(["local-llm", "gpt-4o"]))
        found = enumerate_domain("model_name", session=session)
        assert [v.value for v in found.values] == ["local-llm", "gpt-4o"]

    def test_a_session_with_no_resolver_raises(self):
        """set_model would raise here too — this refuses rather than saying 'none'."""
        with pytest.raises(ValueError, match="needs a model resolver"):
            enumerate_domain("model_name", session=_Session())

    def test_extensions_are_labelled_by_their_state(self):
        session = _Session(extensions=[("/a.py", True), ("/b.py", False)])
        found = enumerate_domain("extension_name", session=session)
        assert [(v.value, v.label) for v in found.values] == [
            ("/a.py", "/a.py (enabled)"),
            ("/b.py", "/b.py (disabled)"),
        ]

    def test_message_ids_come_with_their_text(self):
        session = _Session(log=_Log(_entries(), "e2"))
        found = enumerate_domain("message_id", session=session)
        assert [(v.value, v.label) for v in found.values] == [
            ("e1", "run the tests"),
            ("e2", "they pass"),
        ]

    def test_a_message_id_scope_is_honoured(self):
        session = _Session(log=_Log(_entries(), "e2"))
        found = enumerate_domain(
            "message_id", session=session, scope="descendants_of_cursor", cursor="e1"
        )
        assert [v.value for v in found.values] == ["e2"]

    def test_the_query_searches_labels_case_insensitively(self):
        session = _Session(log=_Log(_entries(), "e2"))
        found = enumerate_domain("message_id", session=session, query="PASS")
        assert [v.value for v in found.values] == ["e2"]

    def test_the_limit_bounds_values_while_total_tells_the_truth(self):
        session = _Session(resolver=_Resolver(["a", "b", "c", "d"]))
        found = enumerate_domain("model_name", session=session, limit=2)
        assert len(found.values) == 2
        assert found.total == 4

    def test_a_domain_whose_reader_needs_an_object_that_was_not_passed_raises(self):
        with pytest.raises(ValueError, match="needs a runtime"):
            enumerate_domain("session_id")

    def test_an_unknown_domain_raises(self):
        with pytest.raises(KeyError):
            enumerate_domain("colour")

    def test_the_tree_reader_is_the_same_one_the_tree_exposes(self):
        """No second implementation: the dispatch calls complete_message_id."""
        session = _Session(log=_Log(_entries(), "e2"))
        direct = ConversationTree(_entries(), "e2").complete_message_id()
        found = enumerate_domain("message_id", session=session)
        assert [v.value for v in found.values] == [m.entry_id for m in direct.matches]


class TestBindingTypedText:
    """`bind_text`: what a person typed, as the value the mutation takes.

    It exists so two heads cannot accept different words for the same flow. Every
    test here is about that: the accepted words are the domain's DECLARED values,
    and nothing is coerced into a default.
    """

    def test_a_boolean_domain_gives_back_a_real_bool(self):
        """`set_auto_compaction(enabled=...)` takes a bool, and the domain's two
        values are the words for Python's two.

        Mutation this kills: returning the matched STRING, which would hand
        `set_auto_compaction` the truthy `"false"`.
        """
        argument = Argument("enabled", "boolean", "The state to put it in.")
        assert bind_text(argument, "true") is True
        assert bind_text(argument, "FALSE") is False

    def test_a_word_the_domain_does_not_declare_is_refused(self):
        """Fail-Early: `/autocompact yes` must not quietly mean "off".

        Mutation this kills: `return text.strip().lower() == "true"`, which turns
        every unrecognised word into False and reports success.
        """
        argument = Argument("enabled", "boolean", "The state to put it in.")
        with pytest.raises(ValueError, match="true, false"):
            bind_text(argument, "yes")

    def test_free_text_passes_through_unchanged(self):
        """Including its spaces — a session name is what the reader typed."""
        argument = Argument("name", "text", "The name to give it.")
        assert bind_text(argument, "  my session ") == "  my session "

    def test_an_integer_domain_parses_and_refuses(self):
        argument = Argument("limit", "integer", "How many.")
        assert bind_text(argument, " 12 ") == 12
        with pytest.raises(ValueError, match="whole number"):
            bind_text(argument, "twelve")

    def test_an_enumerated_domain_is_not_validated_here(self):
        """Whether `a3f9c1` names an entry is a question about a live tree, and
        `enumerate_domain` is what answers it. This converts a TYPE."""
        argument = Argument("target_id", "message_id", "The entry.")
        assert bind_text(argument, "a3f9c1") == "a3f9c1"


class TestTheTwoSessionFlows:
    """`/name` and `/autocompact`: the mutations that were callable in process with
    no gesture in front of them."""

    def test_name_binds_straight_onto_set_session_name(self):
        flow = next(f for f in FLOWS if f.name == "name")
        assert flow.mutation == "set_session_name"
        outcome = next_step("name", {"name": "the refactor"})
        assert isinstance(outcome, Ready)
        assert outcome.arguments == {"name": "the refactor"}

    def test_autocompact_binds_straight_onto_set_auto_compaction(self):
        flow = next(f for f in FLOWS if f.name == "autocompact")
        assert flow.mutation == "set_auto_compaction"
        argument = flow.arguments[0]
        outcome = next_step("autocompact", {argument.name: bind_text(argument, "false")})
        assert isinstance(outcome, Ready)
        assert outcome.arguments == {"enabled": False}

    def test_neither_flow_asks_before_it_has_the_argument(self):
        """Both are required, so an unbound flow reports a step rather than running
        the mutation with a default — there is no sensible default for either."""
        for flow in ("name", "autocompact"):
            assert isinstance(next_step(flow), FlowStep)
