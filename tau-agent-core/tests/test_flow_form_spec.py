"""The join between the flow argument vocabulary and the form field vocabulary.

Covers :attr:`tau_agent_core.capabilities.Domain.field_kind` and
:func:`tau_agent_core.flows.flow_form_spec`.

Reference: docs/TUI-STYLE-GUIDE.md §2.
"""

import pytest

from tau_agent_core.capabilities import DOMAINS, FLOWS, Domain
from tau_agent_core.extension_types import FORM_FIELD_KINDS, validate_form_spec
from tau_agent_core.flows import UnknownFlowError, flow_form_spec, next_step


def test_every_domain_field_kind_is_a_real_form_field_kind():
    """The two vocabularies live in modules that cannot import each other."""
    declared = {domain.field_kind for domain in DOMAINS.values()}
    assert declared <= FORM_FIELD_KINDS, sorted(declared - FORM_FIELD_KINDS)


def test_multiselect_is_not_sayable_by_a_domain():
    """It is what cardinality 'many' makes an argument, not a property of a domain."""
    with pytest.raises(ValueError, match="not a property of the domain"):
        Domain("d", "desc", free=True, field_kind="multiselect")


@pytest.mark.parametrize(
    "kwargs,kind",
    [
        ({"free": True}, "select"),
        ({"free": True}, "confirm"),
        ({"values": ("a", "b")}, "text"),
        ({"values": ("a", "b")}, "number"),
        ({"enumerator": "get_models"}, "confirm"),
        ({"enumerator": "get_models"}, "number"),
    ],
)
def test_field_kind_must_agree_with_how_values_are_found(kwargs, kind):
    """A free domain cannot be a select; a computed one cannot be a checkbox."""
    with pytest.raises(ValueError, match="field_kind must be one of"):
        Domain("d", "desc", field_kind=kind, **kwargs)


def test_a_large_computed_domain_is_text_not_select():
    """session_id and message_id compute their values and still cannot be offered."""
    assert DOMAINS["session_id"].enumerator == "list_sessions"
    assert DOMAINS["session_id"].field_kind == "text"
    assert DOMAINS["message_id"].field_kind == "text"
    assert DOMAINS["model_name"].field_kind == "select"


def _options_for(flow):
    """Every select argument in *flow*, given two plausible values each."""
    return {
        argument.name: ["one", "two"]
        for argument in flow.arguments
        if DOMAINS[argument.domain].field_kind == "select"
    }


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.name)
def test_every_declared_flow_produces_a_valid_form_spec(flow):
    """Whatever the registry declares, the shared validator accepts."""
    spec = flow_form_spec(flow.name, options=_options_for(flow))
    if not spec:
        assert not [a for a in flow.arguments if a.required]
        return
    title, fields = validate_form_spec(spec)
    assert title == flow.description
    assert [f["name"] for f in fields] == [a.name for a in flow.arguments if a.required]


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.name)
def test_the_form_asks_for_exactly_what_next_step_blocks_on(flow):
    """An empty spec and a Ready are the same statement about the same flow."""
    spec = flow_form_spec(flow.name, options=_options_for(flow))
    step = next_step(flow.name)
    if spec:
        assert [f["name"] for f in spec["fields"]][0] == step.argument.name
    else:
        assert not hasattr(step, "argument")


def test_a_bound_argument_is_not_asked_for_again():
    assert flow_form_spec("name", {"name": "already set"}) == {}


def test_boolean_becomes_a_checkbox_carrying_no_options():
    spec = flow_form_spec("autocompact")
    assert spec["fields"] == [
        {"name": "enabled", "kind": "confirm", "label": "The state to put it in."}
    ]


def test_a_select_without_options_raises_rather_than_degrading_to_text():
    """Fail-Early: a free text box would accept values the domain does not admit."""
    with pytest.raises(ValueError, match="no options were supplied"):
        flow_form_spec("model")
    with pytest.raises(ValueError, match="no options were supplied"):
        flow_form_spec("model", options={"name": []})


def test_the_raise_names_the_enumerator_that_would_fix_it():
    with pytest.raises(ValueError, match="enumerator 'get_models'"):
        flow_form_spec("model")


def test_an_unknown_flow_raises():
    with pytest.raises(UnknownFlowError):
        flow_form_spec("no-such-flow")


def test_the_spec_is_json_shaped():
    """A head on the wire gets this as JSON; nothing in it may be a dataclass."""
    import json

    spec = flow_form_spec("model", options={"name": ["a", "b"]})
    assert json.loads(json.dumps(spec)) == spec
