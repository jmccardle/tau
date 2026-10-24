"""stdin EOF under ``tau --mode rpc``: prompt, exit 0, and never a parked suspension holding on (M1e round 2).

A host stops τ by closing its stdin and waiting a few seconds for exit 0 before
killing it (tectum's ``TauBackend.stop``). With no suspension parked, EOF must
behave as it did before the control lane; with one parked, it must close the
suspension as ``cursor_closed`` and return just as promptly.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

from test_rpc import _Recorder, _stdin_held_open
from test_rpc_suspensions import _commands, _user_entry_stepper
from test_suspend import _model, _Stream, _text

PROMPT_EXIT_S = 2.0
"""A parked suspension's EOF must return well inside a host's 5 s kill window."""

MASTER_IDLE_S = 1.0
"""Before the control lane (3b53759) an idle EOF returned in 0.49 s, measured; this keeps headroom."""

MASTER_IN_FLIGHT_S = 1.0
"""Before the control lane a 0.3 s in-flight turn's EOF returned in 0.29 s, measured."""


def _slow_script(delay: float, *texts: str) -> Any:
    queue = list(texts)

    async def fake(model: Any, context: Any, options: Any = None) -> Any:
        await asyncio.sleep(delay)
        return _Stream([DoneEvent(final=_text(queue.pop(0)), usage=Usage())])

    return fake


class _Host:
    """``handler.run()`` on a held-open stdin and a recording stdout."""

    def __init__(self, handler: RPCHandler, monkeypatch: Any) -> None:
        self.handler = handler
        stdin, self.writer = _stdin_held_open()
        self.stdout = _Recorder()
        monkeypatch.setattr("sys.stdin", stdin)
        monkeypatch.setattr("sys.stdout", self.stdout)
        self.task = asyncio.create_task(handler.run())

    def send(self, msg_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            request["params"] = params
        self.writer.write((json.dumps(request) + "\n").encode())
        self.writer.flush()

    async def until(self, predicate: Any, timeout: float = 2.0) -> dict[str, Any]:
        async def scan() -> dict[str, Any]:
            while True:
                for line in list(self.stdout.lines):
                    if predicate(line):
                        return line
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(scan(), timeout)

    async def eof(self) -> float:
        """Close stdin; return how long ``run()`` took to return."""
        started = time.monotonic()
        self.writer.close()
        await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
        elapsed = time.monotonic() - started
        self.stdout.close()
        self.stdout.join()
        return elapsed


def _event(kind: str) -> Any:
    return lambda line: line.get("method") == "event" and line["params"]["type"] == kind


def _session(*extensions: Any) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions)
    )


async def test_eof_on_an_idle_session_returns_at_once_with_no_exit_code(monkeypatch) -> None:
    host = _Host(RPCHandler(_session()), monkeypatch)
    host.send(1, "get_state")
    await host.until(lambda line: line.get("id") == 1)
    assert await host.eof() < MASTER_IDLE_S
    assert host.handler.exit_code is None


async def test_eof_with_a_turn_in_flight_lets_it_finish_within_the_grace(monkeypatch) -> None:
    session = _session()
    host = _Host(RPCHandler(session), monkeypatch)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.3, "ok")):
        host.send(1, "prompt", {"text": "hi"})
        await host.until(lambda line: line.get("id") == 1)
        elapsed = await host.eof()
    assert elapsed < MASTER_IN_FLIGHT_S
    assert host.handler.exit_code is None
    roles = [
        e["message"]["role"] for e in session.session_log.entries() if e.get("type") == "message"
    ]
    assert roles == ["user", "assistant"], "the in-flight turn finished, as before"


async def test_eof_with_a_background_fork_running_returns_at_once(monkeypatch) -> None:
    session = _session()
    host = _Host(RPCHandler(session), monkeypatch)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.0, "a", "b")):
        await session.prompt("first")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.5, "lane")):
        fork = Submission(
            text="lane", source="rpc", submitter="t", submission_id="f1", multitask_strategy="fork"
        )
        assert (await session.submit(fork)).accepted
        elapsed = await host.eof()
        assert elapsed < 0.5
        assert host.handler.exit_code is None
        await asyncio.wait_for(asyncio.gather(*session._forked_tasks.values()), timeout=2)


async def test_eof_closes_a_suspension_the_request_in_flight_waits_on(monkeypatch) -> None:
    session = _session(_user_entry_stepper, _commands)
    host = _Host(RPCHandler(session), monkeypatch)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.0, "ok")):
        host.send(1, "declare_capabilities", {"suspensions": True})
        await host.until(lambda line: line.get("id") == 1)
        host.send(2, "prompt", {"text": "/work", "expand_commands": True})
        await host.until(_event("suspension_open"))
        elapsed = await host.eof()
    assert elapsed < PROMPT_EXIT_S
    assert host.handler.exit_code is None
    [entry] = [e for e in session.session_log.entries() if e.get("type") == "suspension"]
    assert entry["close_reason"] == "cursor_closed"
    assert session.pending_suspensions(scope="session") == []
    assert session.suspension_responders == ()


async def test_eof_closes_a_suspension_a_background_turn_waits_on(monkeypatch) -> None:
    session = _session(_user_entry_stepper)
    host = _Host(RPCHandler(session), monkeypatch)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.0, "ok")):
        host.send(1, "declare_capabilities", {"suspensions": True})
        await host.until(lambda line: line.get("id") == 1)
        host.send(2, "prompt", {"text": "hi", "allow_user_input": True})
        await host.until(_event("suspension_open"))
        elapsed = await host.eof()
    assert elapsed < PROMPT_EXIT_S
    [entry] = [e for e in session.session_log.entries() if e.get("type") == "suspension"]
    assert entry["close_reason"] == "cursor_closed"
