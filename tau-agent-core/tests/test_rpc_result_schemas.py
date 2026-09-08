"""The result half of the command table, derived rather than written twice.

Covers `capabilities.Capability.returns`, `rpc.schema.result_schema_for`, and the
three array item shapes that used to live in English inside a `description`.

Reference: docs/REMOTE-CONTROL.md §6, "the result half is generated too".
"""

from __future__ import annotations

from typing import Any

import pytest
from tau_agent_core.capabilities import CAPABILITIES
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.rpc.schema import result_schema_for
from tau_agent_core.sdk import AgentSession, InMemorySessionLog, _resolve_tools
from tau_llm.types import AssistantMessage, Model, TextContent, Usage, UserMessage

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="you are a test",
        tools=_resolve_tools(["read", "bash"]),
    )


def test_every_capability_declares_what_it_returns() -> None:
    """`returns` covers reads as well as mutations — the decision §16 records.

    The rejected alternative was thirteen read-shaped exceptions living in the RPC
    table, which is a rule a head author would have had to learn twice.
    """
    undeclared = sorted(name for name, cap in CAPABILITIES.items() if cap.returns is None)
    assert undeclared == []


def test_every_capability_backed_verb_publishes_its_capabilitys_returns() -> None:
    """The table's `result_schema` IS the capability's `returns`, for all thirty-two.

    The four verbs with no capability behind them — `prompt`, `get_capabilities`,
    `next_step`, `enumerate_domain` — are exempt, which is §6 A2 holding.
    """
    exempt = {"prompt", "get_capabilities", "next_step", "enumerate_domain"}
    checked = 0
    for name, entry in commands.COMMAND_TABLE.items():
        if entry.result_schema is None or name in exempt:
            continue
        assert name in CAPABILITIES, f"{name} has a result schema but no capability"
        assert entry.result_schema == result_schema_for(name), name
        checked += 1
    assert checked == 32


def test_result_schema_for_hands_back_a_copy() -> None:
    """Three capabilities share one declaration, so a caller must not reach it."""
    first = result_schema_for("navigate")
    first["properties"]["cursor"]["description"] = "clobbered"
    assert result_schema_for("navigate")["properties"]["cursor"]["description"] != "clobbered"
    assert CAPABILITIES["elide_span"].returns is CAPABILITIES["navigate"].returns


def test_an_override_for_a_field_that_is_not_returned_raises() -> None:
    with pytest.raises(ValueError, match="does not return"):
        result_schema_for("get_session_name", overrides={"nmae": {"description": "typo"}})


def test_an_override_merges_over_the_declared_prose() -> None:
    schema = result_schema_for("get_session_name", overrides={"name": {"description": "wire"}})
    assert schema["properties"]["name"]["description"] == "wire"
    assert schema["properties"]["name"]["type"] == ["string", "null"]
    assert CAPABILITIES["get_session_name"].returns is not None
    declared = CAPABILITIES["get_session_name"].returns["properties"]["name"]["description"]
    assert declared != "wire"


def test_items_without_an_array_type_is_refused_at_import_time() -> None:
    """`items` is checked, not ignored — the rule _assert_supported_schema states."""
    with pytest.raises(ValueError, match="without type 'array'"):
        commands._assert_supported_schema(
            {
                "type": "object",
                "properties": {"rows": {"type": "object", "items": {"type": "object"}}},
            },
            "made up",
        )


def test_a_bad_element_in_a_declared_array_is_reported_with_its_index() -> None:
    schema = result_schema_for("get_tools")
    violation = commands.validate_params(
        schema, {"tools": [{"name": "read", "description": "d", "parameters": {}}, {"name": "x"}]}
    )
    assert violation is not None
    assert "tools[1]" in violation
    assert "description" in violation


async def test_the_three_array_shapes_describe_what_the_verbs_really_return(
    session: AgentSession,
) -> None:
    """The item shapes are a claim about live results, so check them against some.

    Until this change `get_messages`, `get_tools` and `get_commands` each declared
    `array` and put the element shape in the property's English, where nothing could
    disagree with it because nothing read it.
    """
    session._session_log.append_message({"role": "system", "content": "you are a test"})
    session._session_log.append_message(
        UserMessage(content=[TextContent(text="hello")], timestamp=0).model_dump()
    )
    session._session_log.append_message(
        AssistantMessage(
            content=[TextContent(text="hi")],
            api="openai-completions",
            provider="openai",
            model="m",
            stop_reason="stop",
            timestamp=_TS,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        ).model_dump()
    )
    handler = RPCHandler(session)
    for verb in ("get_messages", "get_tools", "get_commands"):
        result: dict[str, Any] = await getattr(commands, f"_handle_{verb}")(handler, None, {})
        violation = commands.validate_params(result_schema_for(verb), result)
        assert violation is None, f"{verb}: {violation}\nresult={result}"
    assert [m["role"] for m in session.messages] == ["system", "user", "assistant"]
    assert len(session.tools) == 2


def test_the_three_shared_aliases_still_describe_every_verb_that_uses_them() -> None:
    """`_check_result_families` runs at import; this is what it would have caught."""
    for alias, members in commands._RESULT_FAMILIES.items():
        for member in members:
            assert result_schema_for(member) == getattr(commands, alias), (alias, member)
