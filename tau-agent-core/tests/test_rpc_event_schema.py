"""Tests for tau_agent_core.rpc_event_schema — the generated EVENT half of
the RPC capability document.

Reference: docs/REMOTE-CONTROL.md §6 recommendation item 3, K1/K3 (block
[8]), E1/E2/E3 (block [4]).

Two properties this unit exists to deliver (see the task's item 3 and 4):

1. Anti-drift: WireEvent's hand-declared ``type`` Literal must be fully
   enumerated relative to AgentEvent's ``type`` Literal, AND every field
   WireEvent shares with AgentEvent by name must also match it by type —
   a name-only check misses a field silently retyped out from under the
   projection.
2. The emitted schema is valid JSON Schema and deterministic — no set
   iteration leaking into output ordering, including across interpreter
   processes with different PYTHONHASHSEED values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import typing

import pytest
from pydantic import BaseModel

from tau_agent_core.event_projection import _DIFFABLE_FIELD
from tau_agent_core.events import AgentEvent
from tau_agent_core.rpc_event_schema import (
    WireEvent,
    event_capability_doc,
    event_types,
    wire_event_schema,
)

# Fields WireEvent declares that have no AgentEvent counterpart, because
# E1/E2 require them as bounded REPLACEMENTS for excluded unbounded fields
# rather than as projections of an existing field. See rpc_event_schema.py's
# field-by-field comment for the justification of each. Note submission_id/
# source/submitter/correlation (E4) are NOT here — they exist on AgentEvent
# already and are copied through 1:1, not derived. `cursor` (E5/F3, phase-2
# review B1) is also here: unlike delta/block_type/replace/message_count, it
# is not a REPLACEMENT for an excluded AgentEvent field — AgentEvent carries
# no cursor at all — but it is exactly as "extra" from AgentEvent's point of
# view, so it belongs in the same documented-extras set this test enforces.
DOCUMENTED_DERIVED_FIELDS = {"delta", "block_type", "replace", "message_count", "cursor"}

# Fields AgentEvent declares that WireEvent deliberately does NOT — the
# EXCLUDED-with-reason set from rpc_event_schema.py's field-by-field comment
# (G3: unbounded/untyped; E1/E2 name the bounded replacement declared in
# each one's place). Phase-2 review B3: before this set existed, a field
# could fall through BOTH "projected" and "declared excluded" silently —
# AgentEvent.error did, for the whole life of this module, until B3 added it
# to WireEvent. TestNoFieldSilentlyDropped below is what makes the next such
# field fail loudly instead of shipping unnoticed.
DOCUMENTED_EXCLUDED_FIELDS = {"message", "args", "result", "tool_results", "messages"}


def _type_literal_values(model: type[BaseModel]) -> set[str]:
    """The ``type`` field's Literal values on any model shaped like AgentEvent."""
    return set(typing.get_args(model.model_fields["type"].annotation))


class TestEventTypesReflectAgentEvent:
    """event_types() is derived from AgentEvent, not a hand-maintained copy."""

    def test_matches_agent_event_literal_exactly(self):
        agent_type_args = typing.get_args(AgentEvent.model_fields["type"].annotation)
        assert event_types() == agent_type_args

    def test_returns_a_tuple_not_a_set(self):
        # Ordering must be AgentEvent's declaration order, every call — a set
        # would make this test itself flaky.
        assert isinstance(event_types(), tuple)

    def test_ten_known_event_types_present(self):
        # Pinned to the current AgentEvent literal (events.py) as a readable
        # sanity check; the real drift guard is test below, which needs no
        # hand-maintained list.
        assert set(event_types()) == {
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }

    def test_raises_if_agent_event_type_is_no_longer_a_literal(self, monkeypatch):
        # Fail Early (project standing rule): a widened `type` annotation
        # must not silently degrade event_types() to an empty tuple, which
        # would make event_capability_doc() publish a well-formed but false
        # `events: []`.
        field_info = AgentEvent.model_fields["type"]
        monkeypatch.setattr(field_info, "annotation", str)
        with pytest.raises(TypeError, match="no longer a typing.Literal"):
            event_types()


class TestAntiDrift:
    """The property the unit exists to deliver.

    WireEvent.type is a deliberate, hand-maintained duplicate of
    AgentEvent.type (see WireEvent's field-by-field comment in
    rpc_event_schema.py for why it is not a shared type alias). This class
    is what makes that duplication safe: it fails the moment AgentEvent
    gains (or loses) a literal value that WireEvent.type was not updated to
    match, AND the moment a field WireEvent shares with AgentEvent by name
    is retyped on either side without the other following.
    """

    def test_wire_event_type_literal_matches_agent_event_type_literal(self):
        assert _type_literal_values(WireEvent) == _type_literal_values(AgentEvent), (
            "WireEvent.type has drifted from AgentEvent.type — a new event type "
            "was added to (or removed from) one Literal without updating the "
            "other. Update tau_agent_core.rpc_event_schema.WireEvent.type to match."
        )

    def test_event_types_output_matches_wire_event_declaration(self):
        # Belt-and-braces: the *generated* enumeration (event_types(), read
        # off AgentEvent) must also match what WireEvent itself declares —
        # the artifact fed to a host must describe the model that host will
        # actually receive.
        assert set(event_types()) == _type_literal_values(WireEvent)

    def test_drift_is_actually_detectable(self):
        # Prove the mechanism, not just today's values, by running the SAME
        # helper (_type_literal_values) the production tests above use
        # against a real, throwaway pydantic model standing in for "someone
        # widened AgentEvent.type" — rather than a set-theory tautology like
        # `X | {new} != X`, which passes no matter what the module does.
        extended_type = typing.Literal[
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
            "a_type_nobody_added_yet",
        ]

        class _ExtendedAgentEventStandIn(BaseModel):
            type: extended_type  # type: ignore[valid-type]

        assert _type_literal_values(_ExtendedAgentEventStandIn) != _type_literal_values(WireEvent)

    def test_shared_scalar_fields_have_matching_types(self):
        # Closes the mutation-testing hole: a NAME shared between WireEvent
        # and AgentEvent is not enough — retyping `blocked` from `bool` to
        # `str | None` on one side and not the other must fail here even
        # though every set-of-names comparison in this file stays green.
        agent_fields = AgentEvent.model_fields
        wire_fields = WireEvent.model_fields
        shared_names = (set(wire_fields) & set(agent_fields)) - {"type"}
        assert shared_names, "sanity: WireEvent and AgentEvent must share some scalar fields"
        for name in sorted(shared_names):
            assert wire_fields[name].annotation == agent_fields[name].annotation, (
                f"WireEvent.{name} has drifted in TYPE from AgentEvent.{name}: "
                f"{wire_fields[name].annotation!r} != {agent_fields[name].annotation!r}"
            )


class TestWireEventIsAProjection:
    """WireEvent is a curated subset (plus two E1/E2-mandated additions) of
    AgentEvent's fields, not a raw dump."""

    def test_excludes_unbounded_fields(self):
        wire_fields = set(WireEvent.model_fields.keys())
        # G3 "nothing unbounded is ever pushed" / E1 (message, replaced by
        # `delta`) / E2 (messages, replaced by `message_count`; tool_results,
        # excluded with no replacement — see rpc_event_schema.py's comment)
        # / G3 (args, result — Any-typed, unbounded).
        for excluded in ("message", "args", "result", "tool_results", "messages"):
            assert excluded not in wire_fields, (
                f"{excluded!r} crossed into WireEvent — REMOTE-CONTROL.md D3/E1/E2/G3 "
                "require this to be a deliberate decision, not a raw AgentEvent dump."
            )

    def test_includes_bounded_scalar_fields(self):
        wire_fields = set(WireEvent.model_fields.keys())
        for included in (
            "type",
            "timestamp",
            "turn_index",
            "tool_call_id",
            "tool_name",
            "is_error",
            "error",
            "blocked",
            "blocked_by",
        ):
            assert included in wire_fields

    def test_includes_provenance_quad(self):
        # E4/G6: every wire event carries the submission provenance quad
        # when a submission drove it, null otherwise. These are NOT derived
        # (DOCUMENTED_DERIVED_FIELDS) — they exist on AgentEvent already and
        # are projected 1:1.
        wire_fields = set(WireEvent.model_fields.keys())
        for included in ("submission_id", "source", "submitter", "correlation"):
            assert included in wire_fields

    def test_includes_e1_e2_replacement_fields(self):
        # E1: message_update carries a delta (plus block_type/replace to
        # interpret it), never the cumulative message.
        # E2: agent_end carries counts, not the pushed message array.
        wire_fields = WireEvent.model_fields
        assert "delta" in wire_fields
        assert "block_type" in wire_fields
        assert "replace" in wire_fields
        assert "message_count" in wire_fields

    def test_block_type_matches_diffable_field_kinds(self):
        # Anti-drift for WireEvent.block_type — a hand-copied Literal (see
        # rpc_event_schema.py's field-by-field comment for why it can't be a
        # shared alias the way `source` is): must enumerate exactly the
        # block kinds event_projection.MessageDeltaProjector treats as
        # diffable, no more and no fewer. A new diffable kind added to
        # _DIFFABLE_FIELD without updating WireEvent.block_type would let a
        # real delta silently fail WireEvent's own validation, or (the
        # opposite drift) block_type could name a kind that never actually
        # appears in a delta.
        # block_type's annotation is `Literal[...] | None` — a Union of the
        # Literal and NoneType, not a single flattened Literal — so unwrap
        # the Union first and read the Literal's own args.
        union_args = typing.get_args(WireEvent.model_fields["block_type"].annotation)
        (literal_type,) = (a for a in union_args if a is not type(None))
        block_type_values = set(typing.get_args(literal_type))
        assert block_type_values == set(_DIFFABLE_FIELD.keys())

    def test_extra_wire_fields_are_the_documented_derived_set(self):
        # The projection is a SUBSET of AgentEvent's real fields, except for
        # the two fields E1/E2 require as bounded replacements for excluded
        # unbounded ones. No other invented field is permitted — a new name
        # showing up here that isn't in DOCUMENTED_DERIVED_FIELDS means
        # someone added a field to WireEvent without the E1/E2-style
        # justification this test enforces.
        agent_fields = set(AgentEvent.model_fields.keys())
        wire_fields = set(WireEvent.model_fields.keys())
        extra = wire_fields - agent_fields
        assert extra == DOCUMENTED_DERIVED_FIELDS

    def test_is_not_agent_event_itself(self):
        # D3: "not AgentEvent itself." Guards against a future edit that
        # collapses the projection back into an alias.
        assert WireEvent is not AgentEvent


class TestNoFieldSilentlyDropped:
    """Phase-2 review B3: the agent->wire direction of the anti-drift check.

    Every prior check in this file walks wire_fields - agent_fields (does
    WireEvent invent something undocumented?) or compares fields the two
    models already share by name. Nothing walked the OTHER direction —
    agent_fields - wire_fields — against a documented set, which is exactly
    how AgentEvent.error went missing from the wire for this module's entire
    life: it is real (not excluded on purpose) and not projected (not
    included on purpose), so no existing assertion ever looked at it. This
    class is the one that would have failed on that bug, and fails on the
    next one exactly like it: any new AgentEvent field must be added to
    WireEvent (and reflected in DOCUMENTED_DERIVED_FIELDS/the shared-field
    tests above) OR added to DOCUMENTED_EXCLUDED_FIELDS with a reason in
    rpc_event_schema.py's field-by-field comment — silence is no longer an
    option.
    """

    def test_agent_fields_minus_wire_fields_is_the_documented_excluded_set(self):
        agent_fields = set(AgentEvent.model_fields.keys())
        wire_fields = set(WireEvent.model_fields.keys())
        dropped = agent_fields - wire_fields
        assert dropped == DOCUMENTED_EXCLUDED_FIELDS, (
            f"AgentEvent field(s) {dropped - DOCUMENTED_EXCLUDED_FIELDS!r} reach "
            "neither WireEvent nor DOCUMENTED_EXCLUDED_FIELDS — added to "
            "AgentEvent and never triaged onto the wire (phase-2 review B3). "
            "Either project it onto WireEvent (and update "
            "_wire_event/wire_events.py to copy it), or add it to "
            "DOCUMENTED_EXCLUDED_FIELDS here with a reason in "
            "rpc_event_schema.py's field-by-field EXCLUDED comment."
        )

    def test_error_reaches_the_wire(self):
        # The concrete instance of the bug B3 fixed, pinned so a regression
        # that re-drops just this one field (rather than the whole mechanism
        # above) still fails on its own.
        assert "error" in WireEvent.model_fields
        assert (
            WireEvent.model_fields["error"].annotation
            == AgentEvent.model_fields["error"].annotation
        )


class TestSchemaIsValidJSONSchema:
    """Structural checks that wire_event_schema() is well-formed JSON Schema."""

    def test_is_json_serializable(self):
        schema = wire_event_schema()
        # Round-trips cleanly; no non-JSON types (e.g. a bare set, a Python
        # type object) leaked into the structure.
        reparsed = json.loads(json.dumps(schema))
        assert reparsed == schema

    def test_has_object_shape(self):
        schema = wire_event_schema()
        assert schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict)
        assert isinstance(schema.get("title"), str)

    def test_required_are_real_properties(self):
        schema = wire_event_schema()
        required = schema.get("required", [])
        assert isinstance(required, list)
        properties = schema["properties"]
        for name in required:
            assert name in properties

    def test_every_property_declares_a_type_shape(self):
        schema = wire_event_schema()
        for name, prop in schema["properties"].items():
            has_shape = (
                "type" in prop
                or "$ref" in prop
                or "anyOf" in prop
                or "allOf" in prop
                or "enum" in prop
                or "const" in prop
            )
            assert has_shape, (
                f"property {name!r} has no recognizable JSON Schema type shape: {prop!r}"
            )

    def test_type_property_enumerates_all_event_types(self):
        schema = wire_event_schema()
        type_prop = schema["properties"]["type"]
        assert set(type_prop.get("enum", [])) == set(event_types())


class TestSchemaIsDeterministic:
    """No set iteration leaks into output ordering (task item 4)."""

    def test_repeated_calls_are_byte_identical(self):
        first = json.dumps(wire_event_schema(), sort_keys=False)
        second = json.dumps(wire_event_schema(), sort_keys=False)
        third = json.dumps(wire_event_schema(), sort_keys=False)
        assert first == second == third

    def test_property_key_order_is_stable(self):
        first_keys = list(wire_event_schema()["properties"].keys())
        second_keys = list(wire_event_schema()["properties"].keys())
        assert first_keys == second_keys
        # And it matches WireEvent's own declaration order (pydantic walks
        # model_fields in order), not an incidentally-stable dict/set order.
        assert first_keys == list(WireEvent.model_fields.keys())

    def test_event_types_order_is_stable_across_calls(self):
        assert event_types() == event_types()

    def test_type_enum_order_is_stable(self):
        first = wire_event_schema()["properties"]["type"]["enum"]
        second = wire_event_schema()["properties"]["type"]["enum"]
        assert first == second

    def test_property_and_enum_order_stable_across_hash_seeds(self):
        # The in-process comparisons above cannot see PYTHONHASHSEED-
        # dependent set-iteration drift: CPython's set order for a fixed
        # string set is stable within one process and varies only with the
        # hash seed across processes. Spawn two interpreters with different
        # seeds and require byte-identical output — this is the check that
        # would actually fail if the generator's ordering secretly rode on
        # set iteration instead of the Literal's/model_fields' declared order.
        script = (
            "import json; "
            "from tau_agent_core.rpc_event_schema import event_capability_doc; "
            "print(json.dumps(event_capability_doc()))"
        )
        outputs = []
        for seed in ("1", "999"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            result = subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            outputs.append(result.stdout.strip())
        assert outputs[0] == outputs[1], (
            "event_capability_doc() output differs across PYTHONHASHSEED values "
            "— ordering is leaking from set iteration somewhere in the generator."
        )


class TestEventCapabilityDoc:
    """event_capability_doc() assembles the two generated artifacts."""

    def test_shape(self):
        doc = event_capability_doc()
        assert set(doc.keys()) == {"events", "event_schema"}
        assert doc["events"] == list(event_types())
        assert doc["event_schema"] == wire_event_schema()

    def test_is_json_serializable(self):
        doc = event_capability_doc()
        reparsed = json.loads(json.dumps(doc))
        assert reparsed == doc


class TestPureNoIO:
    """Module-level contract: pure, no I/O, no side effects at import."""

    def test_functions_are_side_effect_free_and_repeatable(self):
        # Calling twice must not mutate any module-level state (e.g. a
        # cached/memoized dict a caller could then mutate).
        doc1 = event_capability_doc()
        doc1["events"].append("mutated")
        doc1["event_schema"]["properties"]["type"]["enum"].append("mutated")
        doc2 = event_capability_doc()
        assert "mutated" not in doc2["events"]
        assert "mutated" not in doc2["event_schema"]["properties"]["type"]["enum"]
