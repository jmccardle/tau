"""The wire's contract (D21 sections 5, 8), read from the public API and the checked-in schema:
`docs/protocol.schema.json` is generated, and says what `Client` and `Event` say."""

import inspect
import json
import types
import typing
from pathlib import Path

import tau

SCHEMA = Path(__file__).parents[2] / "docs" / "protocol.schema.json"


def schema() -> dict[str, typing.Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def client_calls() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(fn)
        for part in inspect.getmro(tau.Client)
        for name, fn in vars(part).items()
        if inspect.iscoroutinefunction(fn) and not name.startswith("_") and name != "close"
    }


def test_the_protocol_is_an_integer_and_the_schema_says_which() -> None:
    assert tau.PROTOCOL == 1
    assert schema()["x-protocol"] == tau.PROTOCOL
    assert schema()["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_a_protocol_mismatch_is_a_runtime_error_so_a_reconnect_does_not_loop_on_it() -> None:
    mismatch = tau.ProtocolMismatch("the server speaks 1", 1)
    assert isinstance(mismatch, tau.ProtocolError)
    assert isinstance(mismatch, RuntimeError)
    assert not isinstance(mismatch, ConnectionError)
    assert mismatch.server == 1
    assert not issubclass(tau.ProtocolError, ConnectionError)


def test_every_client_call_has_a_definition_with_its_params_closed() -> None:
    defs = schema()["$defs"]
    calls = client_calls()
    for name, signature in calls.items():
        params = [p for p in signature.parameters.values() if p.name != "self"]
        assert defs[name]["additionalProperties"] is False, name
        assert list(defs[name]["properties"]) == [p.name for p in params], name
        required = [p.name for p in params if p.default is inspect.Parameter.empty]
        assert defs[name].get("required", []) == required, name
        assert "x-result" in defs[name], name
    # the calls are the whole of it, plus the handshake, which is not a Client call
    asked = {n for n, d in defs.items() if "x-result" in d}
    assert asked == {*calls, "hello"}


def test_the_three_calls_of_stage_seven_are_in_the_schema() -> None:
    defs = schema()["$defs"]
    assert list(defs["parse_line"]["properties"]) == ["cursor", "line"]
    assert defs["parse_line"]["x-result"] == {"$ref": "#/$defs/Parsed"}
    assert list(defs["complete_line"]["properties"]) == ["cursor", "line"]
    assert list(defs["nodes"]["properties"]) == ["start", "limit"]
    assert defs["nodes"]["x-result"] == {"type": "array", "items": {"$ref": "#/$defs/Node"}}
    assert list(defs["Parsed"]["properties"]) == ["command", "args", "missing", "extension", "save"]


def test_every_event_is_one_of_the_event_definition_and_is_tagged_by_kind() -> None:
    defs = schema()["$defs"]
    events = typing.get_args(tau.Event)
    assert [m["$ref"] for m in defs["Event"]["oneOf"]] == [f"#/$defs/{e.__name__}" for e in events]
    for event in [*events, *typing.get_args(tau.LiveDelta)]:
        assert defs[event.__name__]["properties"]["kind"] == {"const": event.__name__}
        assert "kind" in defs[event.__name__]["required"]
    assert isinstance(tau.Event, types.UnionType)


def test_the_core_values_are_defined_by_the_logs_own_encoding() -> None:
    defs = schema()["$defs"]
    for name in ("Node", "Body", "Author", "Item", "ItemData", "Ending"):
        assert name in defs
    # serde tags a variant externally; events are tagged by `kind`: two styles, one schema
    assert {tuple(v["properties"]) for v in defs["Author"]["oneOf"][1:]} == {
        ("Frontend",),
        ("Extension",),
    }


def test_the_error_table_is_the_schemas_error_code_enum() -> None:
    codes = schema()["$defs"]["ErrorCode"]
    assert codes["type"] == "integer", "a generator types the code from this, not from the enum"
    assert dict(zip(codes["x-names"], codes["enum"], strict=True)) == {
        "ParseError": -32700,
        "InvalidRequest": -32600,
        "MethodNotFound": -32601,
        "InvalidParams": -32602,
        "Internal": -32603,
        "Busy": -32001,
        "NoSuchCursor": -32002,
        "CursorClosed": -32003,
        "NoSuchNode": -32004,
        "NoSuchLabel": -32005,
        "ProtocolMismatch": -32006,
        "NotFound": -32007,
        "UnknownCommand": -32008,
        "Refused": -32009,
        "NotYours": -32010,
        "CommandFailed": -32011,
        "ConfigError": -32012,
        "Stopped": -32013,
        "CutOff": -32014,
    }
    assert schema()["$defs"]["End"]["required"] == ["error"]


def test_a_call_parts_replay_is_a_replay_or_absent_never_null() -> None:
    """The log refuses `"replay": null` on a call, so the schema does not allow it."""
    parts = schema()["$defs"]["Part"]["oneOf"]
    (call,) = [p["properties"]["Call"] for p in parts if "Call" in p["properties"]]
    assert call["properties"]["replay"] == {"$ref": "#/$defs/Replay"}
    assert "replay" not in call["required"]


ID_NAMES = {
    "cursor",
    "cursors",
    "at",
    "node",
    "id",
    "to",
    "question",
    "item",
    "index",
    "start",
    "limit",
    "labels",
}
ID_RESULTS = {"open", "reopen", "input", "send", "children"}


def integers_without_minimum(name: str, schema: object) -> list[str]:
    """Where, in `schema`, an integer has no `minimum` of 0."""
    if isinstance(schema, list):
        return [p for item in schema for p in integers_without_minimum(name, item)]
    if not isinstance(schema, dict):
        return []
    found = [name] if schema.get("type") == "integer" and schema.get("minimum") != 0 else []
    for key in ("items", "anyOf", "prefixItems"):
        found += integers_without_minimum(name, schema.get(key))
    return found


def id_problems(defs: dict[str, typing.Any]) -> list[str]:
    """Every integer that holds a node, cursor, item or question id and may be negative."""
    problems: list[str] = []
    for owner, definition in defs.items():
        for field, schema in definition.get("properties", {}).items():
            if field in ID_NAMES:
                problems += integers_without_minimum(f"{owner}.{field}", schema)
        if owner in ID_RESULTS:
            problems += integers_without_minimum(f"{owner} result", definition["x-result"])
    return problems


def test_every_id_in_the_schema_is_non_negative() -> None:
    assert id_problems(schema()["$defs"]) == []


def test_an_id_without_a_minimum_is_found() -> None:
    defs = {"Live": {"properties": {"cursor": {"type": "integer"}, "text": {"type": "integer"}}}}
    assert id_problems(defs) == ["Live.cursor"]
    result = {"children": {"x-result": {"type": "array", "items": {"type": "integer"}}}}
    assert id_problems(result) == ["children result"]
