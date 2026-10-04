"""A turn after ``set_model`` sends the key of the model it switched to."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_coding_agent.serve import protocol as p

from test_serve import _fake_stream, _serve, extensions_off  # noqa: F401

_KEYED: dict[str, Any] = {
    "default_model": "one",
    "models": {
        name: {
            "backend": "openai",
            "model": f"{name}-model",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": f"key-{name}",
        }
        for name in ("one", "two")
    },
}


@pytest.mark.parametrize(
    ("created_on", "switched_to"),
    [(None, "two"), ("two", "one"), ("one", "one")],
    ids=["default→two", "two→one", "one→one"],
)
async def test_the_next_turn_uses_the_switched_models_key(
    tmp_path, extensions_off, created_on, switched_to
):
    keys: list[Any] = []

    async def _recording(model: Any, context: Any, options: Any = None) -> Any:
        keys.append((model.id, (options or {}).get("api_key")))
        return await _fake_stream(model, context, options)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_recording):
        server, served = await _serve(tmp_path, _KEYED)
        try:
            client = await served.client()
            opened = await client.request(p.NewSession(cwd=str(tmp_path), model=created_on))
            session_id = opened["session"]["session_id"]
            replica = await client.attach(session_id)
            head = replica.head_cursor_id
            await client.request(p.RpcCall("set_model", session_id, head, {"name": switched_to}))
            await client.submit_and_wait(session_id, head, "hi", source="rpc", submitter="t")
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
            await served.daemon.shutdown()

    assert keys == [(f"{switched_to}-model", f"key-{switched_to}")]


async def test_compare_runs_each_model_with_its_own_key(tmp_path, extensions_off):
    keys: list[Any] = []

    async def _recording(model: Any, context: Any, options: Any = None) -> Any:
        keys.append((model.id, (options or {}).get("api_key")))
        return await _fake_stream(model, context, options)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_recording):
        server, served = await _serve(tmp_path, _KEYED)
        try:
            client = await served.client()
            opened = await client.request(p.NewSession(cwd=str(tmp_path)))
            session_id = opened["session"]["session_id"]
            await client.attach(session_id)
            answer = await client.request(
                p.Compare(session_id=session_id, models=["one", "two"], text="hi")
            )
            host = served.daemon.hosts[session_id]
            await asyncio.gather(*host.backend.comparisons[answer["comparison_id"]].turns)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
            await served.daemon.shutdown()

    assert sorted(keys) == [("one-model", "key-one"), ("two-model", "key-two")]
