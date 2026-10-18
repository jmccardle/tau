"""RPC ``set_reasoning`` and the ``/thinking`` flow (docs/REASONING-WIRE.md §10).

Every head reaches ``AgentSession.set_reasoning``: the wire verb here, the
``thinking`` flow the TUI and the REPL perform through the backend method the
mutation names. The level lands on the next request's ``reasoning`` option, and
the level the provider sent comes back on ``message_end`` as ``reasoning_level``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import CAPABILITIES, DOMAINS, FLOWS
from tau_agent_core.rpc import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=100_000,
        max_tokens=256,
        reasoning=True,
    )


class _Stream:
    """One completion that reports ``reasoning_level`` as the provider would."""

    def __init__(self, level: str | None) -> None:
        self._level = level

    def __aiter__(self):
        async def _gen():
            final = AssistantMessage(
                content=[TextContent(text="ok")],
                api="openai-completions",
                provider="openai",
                model="m",
                stop_reason="stop",
                reasoning_level=self._level,  # type: ignore[arg-type]
                timestamp=_TS,
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            )
            yield TextDeltaEvent(delta="ok", partial=final)
            yield DoneEvent(final=final, usage=final.usage)

        return _gen()

    def abort(self) -> None:
        pass


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def handler(session: AgentSession) -> RPCHandler:
    return RPCHandler(session)


async def _drain(handler: RPCHandler) -> list[dict[str, Any]]:
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out


async def test_set_reasoning_sets_the_level_and_returns_the_unchanged_tip(
    handler: RPCHandler, session: AgentSession
) -> None:
    await session.start()
    tip = session.cursor.leaf
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_reasoning", "params": {"level": "high"}}
    )
    (response,) = [item for item in await _drain(handler) if item.get("id") == 1]
    assert response["result"] == {"level": "high", "leaf": tip, "method": "set_reasoning"}
    assert session._turn_reasoning() == "high"
    assert session.cursor.leaf == tip


async def test_an_unknown_level_is_refused_and_changes_nothing(
    handler: RPCHandler, session: AgentSession
) -> None:
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_reasoning", "params": {"level": "extreme"}}
    )
    (response,) = [item for item in await _drain(handler) if item.get("id") == 1]
    assert response["error"]["code"] == -32602
    assert "must be one of" in response["error"]["message"]
    assert session._turn_reasoning() is None


async def test_the_next_request_carries_the_level_and_message_end_reports_it(
    handler: RPCHandler, session: AgentSession
) -> None:
    """The verb reaches the provider, and the level sent comes back on the wire."""
    seen: list[Any] = []

    async def _stream_simple(model: Any, context: Any, options: Any = None, **_) -> _Stream:
        seen.append((options or {}).get("reasoning"))
        return _Stream("medium")

    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_reasoning", "params": {"level": "medium"}}
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
        await handler._handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": "hi"}}
        )
        items: list[dict[str, Any]] = []
        for _ in range(200):
            item = await asyncio.wait_for(handler._output_queue.get(), timeout=5.0)
            items.append(item)
            if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                break

    assert seen == ["medium"]
    ends = [
        i["params"]
        for i in items
        if i.get("method") == "event"
        and i["params"].get("type") == "message_end"
        and i["params"].get("stop_reason") is not None
    ]
    assert [e["reasoning_level"] for e in ends] == ["medium"]
    persisted = [m for m in session.cursor.context() if _role(m) == "assistant"]
    assert _field(persisted[-1], "reasoning_level") == "medium"


def _role(message: Any) -> str:
    return message.get("role", "") if isinstance(message, dict) else message.role


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name)


def test_the_thinking_flow_ends_in_set_reasoning_over_the_six_levels() -> None:
    """One declaration gives the TUI and the REPL ``/thinking <level>``."""
    (flow,) = [f for f in FLOWS if f.name == "thinking"]
    assert flow.mutation == "set_reasoning"
    assert [a.domain for a in flow.arguments] == ["thinking_level"]
    assert DOMAINS["thinking_level"].values == ("off", "minimal", "low", "medium", "high", "xhigh")
    assert CAPABILITIES["set_reasoning"].on_wire is True
