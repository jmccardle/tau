"""stdin EOF with a parked suspension releases a file-store session's write lock (M1e round 2).

Self-contained, as ``tau-agent-core/tests/test_rpc_eof.py``'s twin for the file store.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core import session_lock
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.rpc import RPCHandler

from tau_coding_agent.session_store import FileSessionCatalog

from test_session_write_lock import can_lock


class _FakeStdin:
    def __init__(self, buffer: Any) -> None:
        self.buffer = buffer


def _stdin_held_open() -> tuple[_FakeStdin, Any]:
    read_fd, write_fd = os.pipe()
    return _FakeStdin(os.fdopen(read_fd, "rb")), os.fdopen(write_fd, "wb")


class _Recorder:
    """A real-pipe stdout whose lines a thread parses into ``lines``."""

    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self._write_file = os.fdopen(write_fd, "w")
        self._read_file = os.fdopen(read_fd, "r")
        self.lines: list[dict[str, Any]] = []
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    @property
    def buffer(self) -> Any:
        return self._write_file.buffer

    def _drain(self) -> None:
        for line in self._read_file:
            if line.strip():
                self.lines.append(json.loads(line))

    def close(self) -> None:
        self._write_file.close()

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for event in self._events:
                yield event

        return gen()


def _stepper(api: Any) -> None:
    async def on_written(event: Any, ctx: Any) -> None:
        if event["kind"] == "user":
            await api.suspend({"title": "Break", "actions": [{"label": "step"}]})

    api.on("entry_written", on_written)


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


def _event(kind: str) -> Any:
    return lambda line: line.get("method") == "event" and line["params"]["type"] == kind


async def test_a_host_that_closes_stdin_on_a_parked_suspension_leaves_the_session_free(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)
    log = FileSessionCatalog(tmp_path / "sessions").create(os.getcwd(), "m", "openai")
    session = AgentSession(cursor=Cursor.newest(log), model=_model(), extensions=[_stepper])
    host = _Host(RPCHandler(session), monkeypatch)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_slow_script(0.0, "ok")):
        host.send(1, "declare_capabilities", {"suspensions": True})
        await host.until(lambda line: line.get("id") == 1)
        host.send(2, "prompt", {"text": "hi", "allow_user_input": True})
        await host.until(_event("suspension_open"))
        assert not can_lock(log)
        assert await host.eof() < 2.0
    await asyncio.wait_for(session.emit_session_shutdown(), timeout=2)
    assert can_lock(log)
