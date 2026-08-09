"""`tau_agent_core.rpc.capabilities` — K1's document, K2's version, K3's source.

Reference: docs/REMOTE-CONTROL.md §4[8] K1-K3, §6 recommendation, §9 R-T2.

`test_rpc.py::test_get_capabilities_matches_the_capabilities_module` pins the
wire handler to this module's output by literal equality; the tests here pin
what that output actually IS.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tau_agent_core.rpc import capabilities, protocol_doc
from tau_agent_core.rpc.commands import COMMAND_TABLE

#: The default line length of `asyncio.StreamReader` (`_DEFAULT_LIMIT`), and
#: of a good many other stream readers a host might frame with. Spelled out
#: because it appears on BOTH sides of this protocol: it is the bound T7 had
#: to raise on the inbound side after it killed the child, and it is the trap
#: the two tests below keep the OUTBOUND side honest about.
_COMMON_READLINE_DEFAULT_BYTES = 64 * 1024


def _capability_wire_bytes() -> int:
    """`get_capabilities`' result as `transport._write_stdout` serializes it."""
    return len(json.dumps(capabilities.build_capabilities(), separators=(",", ":")))


def test_protocol_version_is_a_major_minor_string():
    assert re.fullmatch(r"\d+\.\d+", capabilities.PROTOCOL_VERSION)


def test_dialect_is_the_native_jsonrpc_dialect():
    assert capabilities.DIALECT == "jsonrpc-2.0"


def test_the_document_has_exactly_k1s_keys():
    doc = capabilities.build_capabilities()
    assert set(doc) == {
        "protocol_version",
        "dialect",
        "commands",
        "events",
        "event_schema",
        "ui_methods",
        "declined",
        # T7 (review finding 9): what a host may SEND. Its contents, and the
        # tie between the advertised number and the reader that enforces it,
        # are pinned in test_rpc_transport.py's TestRequestLineBound —
        # alongside the enforcement, so neither half can move without the
        # other.
        "limits",
    }


def test_the_published_schema_describes_every_key_the_document_carries():
    """`get_capabilities`' own `result_schema` is what a host READS to learn
    the document's shape — it is published verbatim into RPC-PROTOCOL.md — so
    a key the payload carries and the schema omits is the generated reference
    under-describing its own verb.

    This is review finding 9's handoff, and the reason it is a TEST rather
    than a one-time correction: `limits` was added to `build_capabilities()`
    in one unit while the schema (in `commands.py`, a file that unit was
    forbidden to touch) kept listing seven properties, and nothing went red.
    Result schemas are documentation, and only Tier B verbs are validated
    against theirs by the conformance suite — so without this, the next
    additive key drifts exactly the same way.

    Asserted BOTH ways. `properties` must not omit a key the payload has
    (under-description) and must not name one it does not (a promise nothing
    keeps); `required` must equal that same set, because K1 describes a fixed
    document — every key is always present, none is conditional.
    """
    doc = capabilities.build_capabilities()
    schema = COMMAND_TABLE["get_capabilities"].result_schema

    assert set(schema["properties"]) == set(doc)
    assert set(schema["required"]) == set(doc)


def test_the_capability_document_really_is_past_the_common_readline_default():
    """The generated reference tells a host, as a fact and with a number,
    that this one response is over 64 KiB and that a capped `readline` will
    therefore fail on the FIRST verb K2 tells it to send. This test is what
    makes that a fact rather than a sentence.

    Found by measurement at this round's integration, not by reasoning: the
    document was 65,258 bytes on the wire at the start of the round and 65,762
    after one added schema description — τ shipped the whole Tier B review 278
    bytes from a cliff that takes out every default-configured `asyncio` host,
    and it went over on the next edit. Nine of this repo's own subprocess
    tests failed the moment it did, which is what surfaced it.

    If a later change shrinks the document back under the line, this test goes
    RED — correctly. The claim in `protocol_doc.render()` would have stopped
    being true, and a doc asserting a falsehood about a number is exactly the
    failure this round was told to hunt for.
    """
    assert _capability_wire_bytes() > _COMMON_READLINE_DEFAULT_BYTES


def test_the_generated_reference_says_responses_are_not_line_bounded():
    """K3's half of the same fact. Asserted on `render()` rather than on the
    checked-in file, which is regenerated once at the end of a round.

    The number is asserted to be the MEASURED one and to appear nowhere in the
    generator's own source — a hand-copied literal reads identically in the
    rendered page and is wrong the moment a verb is added, which is the drift
    `build_limits()` was written to make impossible on the inbound side.
    """
    rendered = protocol_doc.render()
    measured = _capability_wire_bytes()

    assert "What a host must be prepared to RECEIVE" in rendered
    assert "must not impose one" in rendered
    assert f"{measured:,} bytes" in rendered

    source = Path(protocol_doc.__file__).read_text()
    assert str(measured) not in source and f"{measured:,}" not in source


def test_ui_methods_is_present_and_empty():
    """RC3: '[] in v1' — present (not omitted) AND empty, the honest
    statement that the reverse channel does not exist yet."""
    doc = capabilities.build_capabilities()
    assert doc["ui_methods"] == []


def test_commands_and_declined_partition_the_table_with_no_overlap():
    doc = capabilities.build_capabilities()
    command_names = {c["name"] for c in doc["commands"]}
    declined_names = {d["name"] for d in doc["declined"]}

    assert command_names & declined_names == set()
    assert command_names | declined_names == set(COMMAND_TABLE)


def test_every_command_row_carries_the_promised_fields():
    doc = capabilities.build_capabilities()
    for row in doc["commands"]:
        assert row.keys() >= {"name", "tier", "since", "params_schema"}
        assert row["tier"] in {"A", "B", "C", "D"}
        assert isinstance(row["params_schema"], dict)


def test_every_declined_row_carries_a_one_line_reason():
    """C1: 'Every declined verb carries a one-line reason.' Cheap, mechanical
    guard against a decline() call that snuck in a blank/placeholder reason."""
    doc = capabilities.build_capabilities()
    assert doc["declined"], "expected at least one declined verb (Tier D)"
    for row in doc["declined"]:
        assert row.keys() == {"name", "reason"}
        assert isinstance(row["reason"], str) and len(row["reason"]) >= 20


def test_declined_names_match_tier_d_verbs_named_in_the_design_doc():
    doc = capabilities.build_capabilities()
    declined_names = {d["name"] for d in doc["declined"]}
    assert declined_names == {
        "send_tool_result",
        "cycle_model",
        "cycle_thinking_level",
        "set_steering_mode",
        "set_follow_up_mode",
        "export_html",
        "bash",
    }


def test_get_capabilities_itself_is_a_listed_command():
    """The verb answering this call is itself a row in its own answer —
    walking the table rather than hand-copying means it cannot forget
    itself."""
    doc = capabilities.build_capabilities()
    names = {c["name"] for c in doc["commands"]}
    assert "get_capabilities" in names


def test_commands_are_sorted_by_tier_then_name():
    doc = capabilities.build_capabilities()
    keys = [(c["tier"], c["name"]) for c in doc["commands"]]
    assert keys == sorted(keys)


def test_declined_are_sorted_by_name():
    doc = capabilities.build_capabilities()
    names = [d["name"] for d in doc["declined"]]
    assert names == sorted(names)


def test_events_and_event_schema_match_rpc_event_schema_unchanged():
    """§6 point 3: the event half is generated elsewhere and passed through,
    never re-derived or hand-copied here."""
    from tau_agent_core.rpc_event_schema import event_capability_doc

    doc = capabilities.build_capabilities()
    expected = event_capability_doc()
    assert doc["events"] == expected["events"]
    assert doc["event_schema"] == expected["event_schema"]


def test_build_capabilities_is_deterministic():
    assert capabilities.build_capabilities() == capabilities.build_capabilities()
