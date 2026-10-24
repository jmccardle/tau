"""An accepted RPC submission always ends on the wire with ``agent_end`` (#93).

``prompt`` answers ``accepted`` the moment admission is decided, so a failure after
that cannot be an error response. It must reach the host as ``agent_end`` with
``end_reason: "error"``, or the host waits for its own timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog

from test_suspend import _model, _script, _text


def _bad_content_hook(api: Any) -> None:
    api.on(
        "before_agent_start",
        lambda event, ctx: {"message": {"customType": "bad_msg", "content": [{"type": "nope"}]}},
    )


def _missing_content_hook(api: Any) -> None:
    api.on("before_agent_start", lambda event, ctx: {"message": {"customType": "bad_msg"}})


async def _wire_events(handler: RPCHandler, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Send ``prompt``, then collect wire items until ``agent_end`` or 1 s of silence."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": params})
    items: list[dict[str, Any]] = []
    while True:
        try:
            item = await asyncio.wait_for(handler._output_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return items
        items.append(item)
        if item.get("method") == "event" and item["params"]["type"] == "agent_end":
            return items


@pytest.mark.parametrize(
    ("extension", "text"),
    [
        (_bad_content_hook, "is not a text or image block"),
        (_missing_content_hook, "missing 'content'"),
    ],
)
async def test_a_failure_after_admission_ends_on_agent_end_with_the_error(
    extension: Any, text: str
) -> None:
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[extension])
    handler = RPCHandler(session)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        items = await _wire_events(handler, {"text": "hi"})

    assert items[0]["result"]["accepted"] is True
    ends = [
        i["params"]
        for i in items
        if i.get("method") == "event" and i["params"]["type"] == "agent_end"
    ]
    assert len(ends) == 1, [i.get("params", i) for i in items]
    assert ends[0]["end_reason"] == "error"
    assert ends[0]["is_error"] is True
    assert text in ends[0]["error"]
    assert ends[0]["error_detail"]["type"] == "ValueError"


async def test_a_failure_after_admission_brackets_agent_end_with_agent_start() -> None:
    session = AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=[_bad_content_hook]
    )
    handler = RPCHandler(session)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        items = await _wire_events(handler, {"text": "hi"})
    kinds = [i["params"]["type"] for i in items if i.get("method") == "event"]
    assert kinds == ["agent_start", "agent_end"]


async def test_a_failure_before_the_turn_starts_still_ends_the_submission() -> None:
    """A queued ``next_turn`` message with a bad block fails before ``_run_one_turn`` (#93)."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model())
    session.cursor.next_turn_queue.append(5)
    handler = RPCHandler(session)
    ended: list[Any] = []
    session._events.on("submission_end", lambda **kw: ended.append(kw["submission"]))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        items = await _wire_events(handler, {"text": "hi"})
    ends = [
        i["params"]
        for i in items
        if i.get("method") == "event" and i["params"]["type"] == "agent_end"
    ]
    assert [e["end_reason"] for e in ends] == ["error"]
    assert len(ended) == 1
