"""``compare`` and ``end_compare`` over a real socket (docs/TAU-SERVE.md §8).

The daemon runs in this process on an ephemeral port with a file store under
``tmp_path``. The stub provider answers as the model it was called with, and a
model whose id starts ``slow`` streams until it is aborted, as a real provider
stops on its abort signal.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve as ws_serve

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import config_at

from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.client import Address, ServeClient, ServeError
from tau_coding_agent.serve.daemon import Daemon
from tau_coding_agent.session_store import FileSessionCatalog, Session


def _model(model_id: str) -> dict[str, Any]:
    return {
        "backend": "openai",
        "model": model_id,
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "x",
    }


CONFIG: dict[str, Any] = {
    "default_model": "base",
    "models": {name: _model(f"{name}-model") for name in ("base", "a", "b", "slow")},
}


class _Stream:
    def __init__(self, text: str, model_id: str, signal: Any) -> None:
        self._text = text
        self._model_id = model_id
        self._signal = signal

    def _message(self, text: str, stop_reason: str) -> AssistantMessage:
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model=self._model_id,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
        )

    def __aiter__(self):
        async def gen():
            partial = self._message(self._text, "stop")
            yield TextDeltaEvent(delta=self._text, partial=partial)
            while self._model_id.startswith("slow"):
                if self._signal is not None and self._signal.is_aborted():
                    final = self._message(self._text, "aborted")
                    yield DoneEvent(final=final, usage=final.usage)
                    return
                await asyncio.sleep(0.01)
            yield DoneEvent(final=partial, usage=partial.usage)

        return gen()

    def abort(self) -> None:
        pass


async def fake_stream(model: Any, context: Any, options: Any = None) -> _Stream:
    """Answer ``[model-id] <last user text>``; a ``slow`` model runs until aborted."""
    last = context["messages"][-1]
    content = last.get("content") if isinstance(last, dict) else last.content
    first = content if isinstance(content, str) else content[0]
    text = first if isinstance(first, str) else getattr(first, "text", None) or first["text"]
    signal = (options or {}).get("abort_signal")
    return _Stream(f"[{model.id}] {text}", model.id, signal)


async def until(predicate, timeout: float = 5.0) -> None:
    """Wait for ``predicate()`` to hold, failing after ``timeout`` seconds."""

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
async def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    out = io.StringIO()
    daemon = Daemon(CONFIG, FileSessionCatalog(tmp_path / "sessions"), out=out)
    server = await ws_serve(daemon.handle, "127.0.0.1", 0, max_size=None)
    address = Address("127.0.0.1", server.sockets[0].getsockname()[1])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream):
        yield daemon, address, out
    server.close()
    await server.wait_closed()
    await daemon.shutdown()


async def _session(client: ServeClient, tmp_path: Path, first: str = "first"):
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await client.attach(session_id)
    await client.submit_and_wait(
        session_id, replica.head_cursor_id, first, source="rpc", submitter="test"
    )
    return session_id, replica


def _on_disk(daemon: Daemon, session_id: str) -> list[dict[str, Any]]:
    log = daemon.hosts[session_id].log
    assert isinstance(log, Session) and log.path is not None
    return Session.load(log.path).entries()


async def test_compare_runs_each_model_from_one_leaf_and_keeping_one_moves_the_head(
    served, tmp_path
):
    daemon, address, out = served
    seen: list[dict[str, Any]] = []
    client = await ServeClient.connect(address, client="test", on_event=seen.append)
    session_id, replica = await _session(client, tmp_path)
    head = replica.head_cursor_id
    fork_point = replica.cursors[head]["leaf"]

    answer = await client.request(p.Compare(session_id=session_id, models=["a", "b"], text="hi"))

    cursors = {c["model"]: c["cursor_id"] for c in answer["cursors"]}
    assert list(cursors) == ["a", "b"]
    host = daemon.hosts[session_id]
    comparison = host.backend.comparisons[answer["comparison_id"]]
    await asyncio.gather(*comparison.turns)
    await until(lambda: replica.seq == host.seq)
    entries = _on_disk(daemon, session_id)
    assert replica.entries == entries
    for name, cursor_id in cursors.items():
        leaf = replica.cursors[cursor_id]["leaf"]
        path = ConversationTree(entries, leaf).path()
        assert fork_point in [e["id"] for e in path], "every branch grows from the head's leaf"
        assert config_at(entries, leaf)["model_spec"]["id"] == f"{name}-model"
        assert path[-1]["message"]["content"][0]["text"] == f"[{name}-model] hi"
        assert replica.cursors[cursor_id]["owner_id"] == head
        assert replica.cursors[cursor_id]["label"] == name
    starts = [
        e["data"]["payload"]["submission"]["correlation"]["compare"]
        for e in seen
        if e["kind"] == "channel"
        and e["data"]["name"] == "submission_start"
        and "compare" in e["data"]["payload"]["submission"]["correlation"]
    ]
    assert sorted(s["index"] for s in starts) == [0, 1]
    assert all(s["id"] == answer["comparison_id"] and s["models"] == ["a", "b"] for s in starts)
    kept_leaf = replica.cursors[cursors["b"]]["leaf"]

    ended = await client.request(
        p.EndCompare(
            session_id=session_id, comparison_id=answer["comparison_id"], keep=cursors["b"]
        )
    )

    assert ended == {"leaf": kept_leaf}
    await until(lambda: replica.seq == host.seq)
    assert set(replica.cursors) == {head}, "every compare cursor closed"
    assert replica.cursors[head]["leaf"] == kept_leaf
    assert _on_disk(daemon, session_id) == entries, "keeping writes nothing; both branches stay"
    log = out.getvalue()
    assert f"compare {answer['comparison_id']} at {fork_point}" in log
    assert f"compare {answer['comparison_id']} ended, kept {cursors['b']}" in log
    assert "left incomplete" not in log, "one turn ending does not report another's open entry"
    await client.close()


async def test_an_unknown_model_fails_before_anything_opens(served, tmp_path):
    daemon, address, _ = served
    client = await ServeClient.connect(address, client="test")
    session_id, replica = await _session(client, tmp_path)

    with pytest.raises(ServeError, match="not_found.*nope"):
        await client.request(p.Compare(session_id=session_id, models=["a", "nope"], text="hi"))

    assert len(daemon.hosts[session_id].agent_session.cursors) == 1
    with pytest.raises(ServeError, match="not_found"):
        await client.request(
            p.EndCompare(session_id=session_id, comparison_id="missing", keep=None)
        )
    await client.close()


async def test_a_typed_compare_command_runs_a_comparison(served, tmp_path):
    daemon, address, _ = served
    client = await ServeClient.connect(address, client="test")
    session_id, replica = await _session(client, tmp_path)

    result = await client.submit_and_wait(
        session_id,
        replica.head_cursor_id,
        "/compare a b -- from a command",
        source="rpc",
        submitter="test",
        expand_commands=True,
    )

    command = result["dispatched"]
    assert command["arm"] == "Performed" and command["mutation"] == "compare"
    comparison = daemon.hosts[session_id].backend.comparisons[command["data"]["comparison_id"]]
    results = await asyncio.gather(*comparison.turns)
    assert all(r.accepted for r in results)
    await client.close()


async def test_keeping_one_aborts_the_others_and_waits_for_the_kept_one(served, tmp_path):
    daemon, address, _ = served
    client = await ServeClient.connect(address, client="test")
    session_id, replica = await _session(client, tmp_path)
    host = daemon.hosts[session_id]

    answer = await client.request(
        p.Compare(session_id=session_id, models=["a", "slow"], text="race")
    )
    cursors = {c["model"]: c["cursor_id"] for c in answer["cursors"]}
    comparison = host.backend.comparisons[answer["comparison_id"]]
    await until(lambda: comparison.turns[0].done() and comparison.cursors[1].busy)

    with pytest.raises(ServeError, match="busy.*still running"):
        await client.request(
            p.EndCompare(
                session_id=session_id, comparison_id=answer["comparison_id"], keep=cursors["slow"]
            )
        )
    assert comparison.cursors[1].busy, "a refused keep aborts nothing"

    kept_leaf = comparison.cursors[0].leaf
    ended = await client.request(
        p.EndCompare(
            session_id=session_id, comparison_id=answer["comparison_id"], keep=cursors["a"]
        )
    )

    assert ended == {"leaf": kept_leaf}
    assert comparison.turns[1].done()
    entries = _on_disk(daemon, session_id)
    aborted = [e for e in entries if e.get("message", {}).get("model") == "slow-model"]
    assert aborted and aborted[-1]["message"]["stop_reason"] == "aborted"
    assert [c.id for c in host.agent_session.cursors] == [replica.head_cursor_id]
    await client.close()
