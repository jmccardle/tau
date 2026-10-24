"""Suspensions on the RPC wire: the verbs, the events and the control lane (docs/STEP-DEBUGGER.md §3.4-§3.6, M1e).

Requests go through ``transport._dispatch_lines`` over a queue, which is what
the stdin reader feeds, so the control lane runs exactly as it does over stdio.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import CAPABILITIES
from tau_agent_core.rpc import RPCHandler, commands, transport
from tau_agent_core.session_log import InMemorySessionLog

from test_suspend import SPEC, _model, _script, _text

_suspended: list[Any] = []


def _user_entry_stepper(api: Any) -> None:
    async def on_written(event: Any, ctx: Any) -> None:
        if event["kind"] == "user":
            try:
                _suspended.append(await api.suspend(SPEC))
            except Exception as err:
                _suspended.append(err)

    api.on("entry_written", on_written)


def _commands(api: Any) -> None:
    async def work(args: str, ctx: Any) -> str:
        session = api._session
        cursor = await session.open_cursor(None, label="lane")
        result = await api.submit("go", cursor=cursor, allow_user_input=True)
        await session.close_cursor(cursor)
        return f"worked: {result.accepted}"

    async def slow(args: str, ctx: Any) -> str:
        await asyncio.sleep(0.1)
        return "slept"

    api.register_command("work", {"description": "work", "handler": work})
    api.register_command("slow", {"description": "slow", "handler": slow})


@pytest.fixture
def handler() -> RPCHandler:
    _suspended.clear()
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[_user_entry_stepper, _commands],
    )
    return RPCHandler(session)


def _line(msg_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        request["params"] = params
    return json.dumps(request)


class _Wire:
    """Feeds ``_dispatch_lines`` and collects everything the handler writes, in order."""

    def __init__(self, handler: RPCHandler) -> None:
        self.handler = handler
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.out: list[dict[str, Any]] = []
        self.task = asyncio.create_task(transport._dispatch_lines(handler, self.queue))

    def send(self, msg_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        self.queue.put_nowait(_line(msg_id, method, params))

    async def until(self, predicate: Any, timeout: float = 2.0) -> dict[str, Any]:
        async def scan() -> dict[str, Any]:
            seen = 0
            while True:
                while not self.handler._output_queue.empty():
                    item = await self.handler._output_queue.get()
                    item.pop("_cursor", None)
                    item.pop("_credited", None)
                    self.out.append(item)
                    self.handler._event_credits.release()
                for item in self.out[seen:]:
                    if predicate(item):
                        return item
                seen = len(self.out)
                await asyncio.sleep(0.005)

        return await asyncio.wait_for(scan(), timeout)

    def position(self, predicate: Any) -> int:
        return next(i for i, item in enumerate(self.out) if predicate(item))

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await asyncio.wait_for(self.task, 2)


def _response(msg_id: int) -> Any:
    return lambda item: item.get("id") == msg_id


def _event(kind: str) -> Any:
    return lambda item: item.get("method") == "event" and item["params"]["type"] == kind


def test_the_three_verbs_are_on_the_table() -> None:
    assert CAPABILITIES["get_pending_suspensions"].kind == "read"
    assert CAPABILITIES["answer_suspension"].kind == "mutation"
    assert CAPABILITIES["declare_capabilities"].kind == "mutation"
    for verb in ("get_pending_suspensions", "answer_suspension", "declare_capabilities"):
        entry = commands.COMMAND_TABLE[verb]
        assert entry.handler is not None and entry.since == "0.15.0"
    assert transport.CONTROL_VERBS == {
        "answer_suspension",
        "abort",
        "get_pending_suspensions",
        "get_state",
    }


async def test_a_host_that_never_declared_gets_no_responder(handler: RPCHandler) -> None:
    wire = _Wire(handler)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        wire.send(1, "prompt", {"text": "hi", "allow_user_input": True})
        await wire.until(_event("agent_end"))
    await wire.close()
    assert type(_suspended[0]).__name__ == "NoResponderError"


async def test_a_declared_host_sees_the_suspension_and_answers_it(handler: RPCHandler) -> None:
    wire = _Wire(handler)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        wire.send(1, "declare_capabilities", {"suspensions": True})
        assert (await wire.until(_response(1)))["result"]["suspensions"] is True
        wire.send(2, "prompt", {"text": "hi", "allow_user_input": True})
        opened = await wire.until(_event("suspension_open"))
        suspension = opened["params"]["suspension"]
        assert suspension["spec"]["title"] == "Break" and suspension["queued"] is False
        wire.send(3, "get_pending_suspensions")
        pending = (await wire.until(_response(3)))["result"]["suspensions"]
        assert [s["id"] for s in pending] == [suspension["id"]]
        wire.send(4, "answer_suspension", {"suspension_id": suspension["id"], "action": "nope"})
        assert (await wire.until(_response(4)))["error"]["code"] == -32602
        wire.send(
            5,
            "answer_suspension",
            {"suspension_id": suspension["id"], "action": "step", "values": {"note": "ok"}},
        )
        answered = (await wire.until(_response(5)))["result"]
        assert answered["accepted"] is True and answered["reason"] is None
        assert answered["resolution"]["answered_by"] == {"client": "rpc"}
        closed = await wire.until(_event("suspension_closed"))
        assert closed["params"]["close_reason"] == "answered"
        await wire.until(_event("agent_end"))
        wire.send(6, "answer_suspension", {"suspension_id": suspension["id"], "action": "step"})
        again = (await wire.until(_response(6)))["result"]
        assert again["accepted"] is False and again["reason"] == "already_resolved"
    await wire.close()
    assert _suspended[0]["values"] == {"note": "ok"}


async def test_the_lane_lets_an_answer_past_the_request_waiting_on_it(handler: RPCHandler) -> None:
    """Without the lane, the answer would sit behind the /work request it unblocks: a deadlock."""
    wire = _Wire(handler)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        wire.send(1, "declare_capabilities", {"suspensions": True})
        await wire.until(_response(1))
        wire.send(2, "prompt", {"text": "/work", "expand_commands": True})
        opened = await wire.until(_event("suspension_open"))
        sid = opened["params"]["suspension"]["id"]
        wire.send(3, "get_messages")
        wire.send(4, "get_state")
        wire.send(
            5, "answer_suspension", {"suspension_id": sid, "action": "step", "values": {"note": ""}}
        )
        await wire.until(_response(5))
        await wire.until(_response(2))
        await wire.until(_response(3))
    await wire.close()
    answer_at, state_at = wire.position(_response(5)), wire.position(_response(4))
    work_at, messages_at = wire.position(_response(2)), wire.position(_response(3))
    assert state_at < work_at and answer_at < work_at
    assert messages_at > work_at


async def test_with_no_suspension_open_the_reader_stays_serial(handler: RPCHandler) -> None:
    wire = _Wire(handler)
    wire.send(1, "prompt", {"text": "/slow", "expand_commands": True})
    wire.send(2, "get_state")
    await wire.until(_response(2))
    await wire.close()
    assert wire.position(_response(1)) < wire.position(_response(2))


async def test_the_default_scope_lists_every_cursors_suspension(handler: RPCHandler) -> None:
    """The host receives every cursor's suspension events, so the default read lists them all."""
    wire = _Wire(handler)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        wire.send(1, "declare_capabilities", {"suspensions": True})
        await wire.until(_response(1))
        wire.send(2, "prompt", {"text": "/work", "expand_commands": True})
        opened = await wire.until(_event("suspension_open"))
        assert opened["params"]["suspension"]["cursor_id"] != handler.session.cursor.id
        wire.send(3, "get_pending_suspensions")
        wire.send(4, "get_pending_suspensions", {"scope": "cursor"})
        default = (await wire.until(_response(3)))["result"]["suspensions"]
        own = (await wire.until(_response(4)))["result"]["suspensions"]
        assert len(default) == 1 and own == []
        sid = default[0]["id"]
        wire.send(
            5, "answer_suspension", {"suspension_id": sid, "action": "step", "values": {"note": ""}}
        )
        await wire.until(_response(2))
    await wire.close()


async def test_each_refused_answer_names_its_reason(handler: RPCHandler) -> None:
    wire = _Wire(handler)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        wire.send(1, "declare_capabilities", {"suspensions": True})
        await wire.until(_response(1))
        wire.send(2, "prompt", {"text": "hi", "allow_user_input": True})
        sid = (await wire.until(_event("suspension_open")))["params"]["suspension"]["id"]
        cases = {
            10: ({"suspension_id": "nope", "action": "step"}, "unknown_id"),
            11: ({"suspension_id": sid, "action": "jump"}, "undeclared_action"),
            12: ({"suspension_id": sid, "action": "step", "values": {}}, "invalid_value"),
        }
        for msg_id, (params, _reason) in cases.items():
            wire.send(msg_id, "answer_suspension", params)
        for msg_id, (params, reason) in cases.items():
            error = (await wire.until(_response(msg_id)))["error"]
            assert error["code"] == -32602
            assert error["data"]["reason"] == reason
            assert error["data"]["suspension_id"] == params["suspension_id"]
        wire.send(
            13,
            "answer_suspension",
            {"suspension_id": sid, "action": "step", "values": {"note": ""}},
        )
        await wire.until(_response(13))
        await wire.until(_event("agent_end"))
    await wire.close()


async def test_a_queued_refusal_reaches_the_wire_as_queued() -> None:
    from unittest.mock import MagicMock

    from tau_agent_core.suspensions import SuspensionAnswerError

    session = MagicMock()
    session.answer_suspension.side_effect = SuspensionAnswerError("queued", "queued behind x")
    handler = RPCHandler(session)
    params = {"suspension_id": "s2", "action": "step"}
    with pytest.raises(commands.RPCError) as raised:
        await commands._handle_answer_suspension(handler, 1, params)
    assert raised.value.data == {**params, "reason": "queued"}


def test_the_event_fields_are_typed_records_and_the_close_reason_has_its_own_field() -> None:
    from tau_agent_core.rpc_event_schema import WireEvent

    properties = WireEvent.model_json_schema()["properties"]
    refs = {
        name: [branch.get("$ref") for branch in properties[name]["anyOf"]]
        for name in ("suspension", "resolution", "lock_holder")
    }
    assert refs == {
        "suspension": ["#/$defs/SuspensionState", None],
        "resolution": ["#/$defs/Resolution", None],
        "lock_holder": ["#/$defs/LockHolderRecord", None],
    }
    close = [b.get("enum") for b in properties["close_reason"]["anyOf"]]
    assert ["answered", "aborted", "cursor_closed", "abandoned"] in close
    reason = [b.get("enum") for b in properties["reason"]["anyOf"]]
    assert ["manual", "threshold", "navigate"] in reason
