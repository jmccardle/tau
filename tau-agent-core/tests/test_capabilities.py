"""The capability registry: its self-consistency, and the vocabulary it projects.

``capabilities.py`` is a table of declarations, so most of what can go wrong with it
is a name that refers to nothing. ``_check_registry`` runs at import and catches that
for the shipped tables; these tests pin the guards themselves, and pin the one thing
downstream code observes — the exact list and order of ``FRONTEND_COMMANDS``, which
the completion popup and the RPC ``get_commands`` listing both render in place.
"""

from typing import get_args

import pytest

from tau_agent_core.capabilities import (
    CAPABILITIES,
    DOMAINS,
    FLOWS,
    VIEW_COMMANDS,
    Argument,
    Capability,
    Domain,
    Flow,
    _check_flow_matches_mutation,
    slash_vocabulary,
)
from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_agent_core.conversation_tree import MessageIdScope


class TestTheProjection:
    def test_the_slash_vocabulary_and_its_order(self):
        """The names and their order, which completion and get_commands render.

        ``compact`` is first because ``test_rpc`` asserts ``get_commands`` lists it
        there; the rest follow the flow table, with the view commands after
        ``compact``.
        """
        assert list(FRONTEND_COMMANDS) == [
            "compact",
            "tree",
            "extensions",
            "fork",
            "model",
            "resume",
            "name",
            "autocompact",
            "enable_extension",
            "disable_extension",
            "reload_extension",
        ]

    def test_frontend_commands_is_the_projection(self):
        assert FRONTEND_COMMANDS == slash_vocabulary()

    def test_the_caller_gets_a_fresh_dict(self):
        """Callers hold it; two of them must not share one object."""
        first = slash_vocabulary()
        first["compact"] = "mutated"
        assert slash_vocabulary()["compact"] != "mutated"

    def test_every_slash_name_is_a_flow_or_a_view(self):
        assert set(slash_vocabulary()) == {f.name for f in FLOWS} | set(VIEW_COMMANDS)


class TestDomainsSayExactlyOneThing:
    def test_a_domain_with_no_answer_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of free / values / enumerator"):
            Domain("nowhere", "no way to get a value")

    def test_a_domain_with_two_answers_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of free / values / enumerator"):
            Domain("both", "two answers", free=True, values=("a",))

    @pytest.mark.parametrize("name", sorted(DOMAINS))
    def test_every_shipped_domain_constructs(self, name):
        assert DOMAINS[name].name == name


class TestTheCrossChecks:
    def test_every_enumerator_is_a_declared_read(self):
        for domain in DOMAINS.values():
            if domain.enumerator is not None:
                assert CAPABILITIES[domain.enumerator].kind == "read"

    def test_every_flow_ends_in_one_declared_mutation(self):
        for flow in FLOWS:
            assert CAPABILITIES[flow.mutation].kind == "mutation"

    def test_every_argument_names_a_declared_domain(self):
        for flow in FLOWS:
            for argument in flow.arguments:
                assert argument.domain in DOMAINS
        for capability in CAPABILITIES.values():
            for argument in capability.arguments or ():
                assert argument.domain in DOMAINS

    def test_every_flow_argument_is_an_argument_of_its_mutation(self):
        """The two tables named the same arguments twice and nothing compared them.

        ``set_model``'s wire params and the ``model`` flow's arguments have always
        both said ``name``; renaming either one left the other describing a call the
        performer would reject.
        """
        for flow in FLOWS:
            declared = {a.name: a for a in CAPABILITIES[flow.mutation].arguments or ()}
            for argument in flow.arguments:
                found = declared[argument.name]
                assert (found.domain, found.cardinality) == (
                    argument.domain,
                    argument.cardinality,
                )

    def test_a_flow_may_skip_an_optional_argument_but_not_a_required_one(self):
        _check = _check_flow_matches_mutation
        optional = Capability(
            "trim",
            "mutation",
            "trim it",
            arguments=(
                Argument("target_id", "message_id", "which"),
                Argument("why", "text", "why", required=False),
            ),
        )
        _check(
            Flow("trim", "trim it", "trim", (Argument("target_id", "message_id", "which"),)),
            optional,
        )
        with pytest.raises(ValueError, match="never asks for"):
            _check(Flow("trim", "trim it", "trim", ()), optional)

    def test_a_flow_argument_the_mutation_does_not_take_is_refused(self):
        with pytest.raises(ValueError, match="does not take"):
            _check_flow_matches_mutation(
                Flow("trim", "trim it", "trim", (Argument("depth", "number", "how far"),)),
                Capability("trim", "mutation", "trim it"),
            )

    def test_a_flow_argument_read_from_the_wrong_domain_is_refused(self):
        with pytest.raises(ValueError, match="takes it as"):
            _check_flow_matches_mutation(
                Flow("pick", "pick one", "pick", (Argument("id", "text", "which"),)),
                Capability(
                    "pick",
                    "mutation",
                    "pick one",
                    arguments=(Argument("id", "message_id", "which"),),
                ),
            )

    def test_no_flow_may_end_in_a_capability_whose_arguments_are_unstated(self):
        """``submit`` carries images and a correlation object; no domain describes them.

        A flow ending there would ask a head to render fields the registry cannot
        describe, so the registry refuses instead of offering an empty argument list.
        """
        assert CAPABILITIES["submit"].arguments is None
        with pytest.raises(ValueError, match="not expressible as domains"):
            _check_flow_matches_mutation(Flow("send", "send it", "submit"), CAPABILITIES["submit"])

    def test_the_message_id_scope_domain_lists_the_scopes_the_tree_accepts(self):
        """A fixed value set copied from a Literal drifts silently unless pinned."""
        assert DOMAINS["message_id_scope"].values == get_args(MessageIdScope)

    def test_a_flow_that_ends_in_nothing_is_refused(self):
        """A gesture with no terminal mutation is a view, and views are head code."""
        with pytest.raises(ValueError, match="declares no mutation"):
            Flow(name="browse", description="look at things", mutation="")

    def test_view_commands_and_flows_do_not_share_a_name(self):
        assert not set(VIEW_COMMANDS) & {flow.name for flow in FLOWS}


class TestWhatTheRecordsCarry:
    @pytest.mark.parametrize(
        "flow_name", ["enable_extension", "disable_extension", "reload_extension"]
    )
    def test_each_extension_action_is_its_own_flow(self, flow_name):
        """The three were one flow with a discriminator; they are three atomic flows.

        Identical argument lists, and still three: they differ in what they do to the
        module object and in how they fail — only ``reload`` raises on a file that no
        longer imports (``agent_session.py`` ``reload_extension``).
        """
        flow = next(flow for flow in FLOWS if flow.name == flow_name)
        assert flow.mutation == flow_name
        (argument,) = flow.arguments
        assert argument.name == "path"
        assert DOMAINS[argument.domain].enumerator == "list_managed_extensions"

    def test_no_flow_offers_a_choice_of_mutation(self):
        """A gesture that picks among mutations is a view; the core declares neither."""
        assert not hasattr(FLOWS[0], "mutations")
        assert not hasattr(FLOWS[0], "discriminator")

    def test_extensions_is_a_view_and_not_a_flow(self):
        assert "extensions" in VIEW_COMMANDS
        assert "extensions" not in {flow.name for flow in FLOWS}

    def test_cardinality_defaults_to_one(self):
        assert Argument("x", "text", "some text").cardinality == "one"

    def test_every_capability_is_on_the_wire(self):
        """As of 0.9.8 there is no capability a host cannot address by name.

        `on_wire` still means what it said — a statement about the wire, not about
        the capability, since every one of these is callable in-process either way
        — and the field stays because the next capability declared will be
        `False` until its verb is written. What changed is that the set it
        describes is currently empty, and that is worth failing on if it silently
        stops being true: a capability landing here with no verb is exactly the
        gap docs/VSCODE-HEAD.md §6 measured, and it should be a decision rather
        than a default.
        """
        off_wire = sorted(c.name for c in CAPABILITIES.values() if not c.on_wire)
        assert off_wire == [], (
            f"{off_wire} are declared capabilities with no RPC verb. That is "
            "allowed — say so here, with why — but it is not the default."
        )

    def test_the_wire_flag_matches_the_command_table(self):
        from tau_agent_core.rpc.commands import COMMAND_TABLE

        for capability in CAPABILITIES.values():
            assert capability.on_wire == (capability.name in COMMAND_TABLE), capability.name


class TestTheModelFlow:
    """§11's acceptance test: a command τ did not have, added as one row.

    If ``/model`` had cost more than a registry row and a backend passthrough, the
    record shape would have been wrong. These pin what the row buys.
    """

    def test_it_is_one_row_ending_in_one_mutation(self):
        model = next(flow for flow in FLOWS if flow.name == "model")
        assert model.mutation == "set_model"

    def test_its_one_argument_is_an_enumerable_domain(self):
        model = next(flow for flow in FLOWS if flow.name == "model")
        (argument,) = model.arguments
        assert argument.required
        assert DOMAINS[argument.domain].enumerator == "get_models"

    def test_the_argument_is_named_what_the_mutation_takes(self):
        """set_model's wire param is `name`, so Ready.arguments maps straight on."""
        model = next(flow for flow in FLOWS if flow.name == "model")
        assert model.arguments[0].name == "name"
