"""``get_tools`` and every other tool listing include extension tools (issue #46).

Extension tools join each turn's tool list, so a listing read from the
constructor's tools alone told an RPC host ``[]`` under ``--no-builtin-tools``
while the model was offered the extension's tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compare import start_comparison
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.sdk import create_agent_session

_PROBE = """
async def _run(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}


def register(api):
    for name in NAMES:
        api.register_tool(
            {
                "name": name,
                "label": name,
                "description": f"extension {name}",
                "parameters": {"type": "object", "properties": {}},
                "execute": _run,
            }
        )
"""


def _model(model_id: str = "gpt-4o") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


async def _load(session: AgentSession, tmp_path: Path, *names: str) -> str:
    """Load one file extension registering a tool per name; returns its path."""
    path = tmp_path / "probe_ext.py"
    path.write_text(f"NAMES = {list(names)!r}\n{_PROBE}")
    await session.load_extensions([str(path)], discover=False)
    return str(path)


async def _get_tools(session: AgentSession) -> list[dict[str, Any]]:
    result = await commands._handle_get_tools(RPCHandler(session), None, {})
    violation = commands.validate_params(commands.GET_TOOLS_RESULT_SCHEMA, result)
    assert violation is None, violation
    tools: list[dict[str, Any]] = result["tools"]
    return tools


async def test_no_builtin_tools_and_one_extension_tool_lists_exactly_that_tool(tmp_path):
    session = create_agent_session(model=_model(), no_tools="builtin")
    await _load(session, tmp_path, "probe")

    (tool,) = await _get_tools(session)

    assert tool["name"] == "probe"
    assert tool["description"] == "extension probe"
    assert tool["exposure"] == "direct"
    assert tool["declared"] is True


async def test_builtins_and_extensions_list_in_the_order_the_loop_sees_them(tmp_path):
    session = create_agent_session(model=_model(), tools=["read", "bash"])
    await _load(session, tmp_path, "probe", "read")

    listed = [t["name"] for t in await _get_tools(session)]

    assert listed == ["read", "bash", "probe"]
    assert listed == [t.name for t in session._turn_tools()]
    assert [t.name for t in session.tools] == listed
    read = next(t for t in session.tools if t.name == "read")
    assert read.definition.description == "extension read"


async def test_a_disabled_extensions_tool_disappears(tmp_path):
    session = create_agent_session(model=_model(), tools=["read"])
    path = await _load(session, tmp_path, "probe")
    assert [t["name"] for t in await _get_tools(session)] == ["read", "probe"]

    assert (await session.disable_extension(path)).ok

    assert [t["name"] for t in await _get_tools(session)] == ["read"]


async def test_exposure_overrides_and_withdrawals_are_reported(tmp_path):
    session = create_agent_session(model=_model(), tools=["read"])
    await _load(session, tmp_path, "probe")
    await session.set_tool_exposure("probe", "hidden")
    await session.redeclare_tools({"read": False})

    by_name = {t["name"]: t for t in await _get_tools(session)}

    assert (by_name["probe"]["exposure"], by_name["probe"]["declared"]) == ("hidden", True)
    assert (by_name["read"]["exposure"], by_name["read"]["declared"]) == ("direct", False)


async def test_recorded_config_tools_include_extension_tools(tmp_path):
    session = create_agent_session(model=_model(), tools=["read"])
    await _load(session, tmp_path, "probe")

    assert session._frame_config()["tools"] == ["read", "probe"]


async def test_a_comparison_frame_offers_extension_tools(tmp_path, monkeypatch):
    session = create_agent_session(
        model=_model(), no_tools="builtin", model_resolver=lambda name: _model(name)
    )
    await _load(session, tmp_path, "probe")

    async def _no_turn(submission: Any, cursor: Any = None) -> None:
        return None

    monkeypatch.setattr(session, "submit", _no_turn)
    comparison = await start_comparison(session, session.cursor.leaf, ["a", "b"], "hi")
    for turn in comparison.turns:
        await turn

    assert [c.frame.tools for c in comparison.cursors] == [("probe",), ("probe",)]
