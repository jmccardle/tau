"""``tau serve`` end to end over a real socket (docs/TAU-SERVE.md §5, §6).

The daemon runs in this process on an ephemeral port with a file store under
``tmp_path``; the provider is stubbed, so a turn is fast and deterministic.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from websockets.asyncio.server import serve as ws_serve

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage

from tau_coding_agent.serve import daemon as daemon_module
from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.client import Address, ServeClient, ServeError, parse_address
from tau_coding_agent.serve.daemon import Client, Daemon
from tau_coding_agent.session_store import FileSessionCatalog, Session

_CONFIG: dict[str, Any] = {
    "default_model": "fake",
    "models": {
        "fake": {
            "backend": "openai",
            "model": "fake-model",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "x",
        },
        "other": {
            "backend": "openai",
            "model": "other-model",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "x",
        },
    },
}


class _Stream:
    def __init__(self, text: str, model_id: str) -> None:
        self._message = AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model=model_id,
            stop_reason="stop",
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
        )

    def __aiter__(self):
        async def gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return gen()

    def abort(self) -> None:
        pass


async def _fake_stream(model: Any, context: Any, options: Any = None) -> _Stream:
    last = context["messages"][-1]
    content = last.get("content") if isinstance(last, dict) else last.content
    first = content if isinstance(content, str) else content[0]
    text = first if isinstance(first, str) else getattr(first, "text", None) or first["text"]
    return _Stream(f"re: {text}", model.id)


@pytest.fixture
def provider():
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream):
        yield


@pytest.fixture
def extensions_off(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


class _Served:
    def __init__(self, daemon: Daemon, address: Address, out: io.StringIO) -> None:
        self.daemon = daemon
        self.address = address
        self.out = out

    async def client(self, name: str = "test", **kwargs: Any) -> ServeClient:
        return await ServeClient.connect(self.address, client=name, **kwargs)


async def _serve(tmp_path: Path, config: dict[str, Any] | None = None):
    out = io.StringIO()
    daemon = Daemon(config or _CONFIG, FileSessionCatalog(tmp_path / "sessions"), out=out)
    server = await ws_serve(daemon.handle, "127.0.0.1", 0, max_size=None)
    port = server.sockets[0].getsockname()[1]
    return server, _Served(daemon, Address("127.0.0.1", port), out)


@pytest.fixture
async def served(tmp_path, provider, extensions_off):
    server, handle = await _serve(tmp_path)
    yield handle
    server.close()
    await server.wait_closed()
    await handle.daemon.shutdown()


async def _until(predicate, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


def _file_entries(daemon: Daemon, session_id: str) -> list[dict[str, Any]]:
    log = daemon.hosts[session_id].log
    assert isinstance(log, Session) and log.path is not None
    return Session.load(log.path).entries()


async def test_a_turn_reaches_the_file_and_every_client_replica(served, tmp_path):
    seen: list[dict[str, Any]] = []
    one = await served.client(on_event=seen.append)
    two = await served.client()
    created = await one.request(p.CreateSession(cwd=str(tmp_path)))
    session_id = created["session_id"]
    replica_one = await one.attach(session_id)
    replica_two = await two.attach(session_id)

    result = await one.request(
        p.Submit(session_id=session_id, cursor_id=replica_one.head_cursor_id, text="hello")
    )

    assert result["accepted"] is True
    await _until(lambda: replica_two.seq == replica_one.seq == served.daemon.hosts[session_id].seq)
    on_disk = _file_entries(served.daemon, session_id)
    assert replica_one.entries == on_disk
    assert replica_two.entries == on_disk
    kinds = [e["kind"] for e in seen]
    assert "entry_open" in kinds and "entry_final" in kinds and "agent_event" in kinds
    names = [e["data"]["name"] for e in seen if e["kind"] == "channel"]
    assert names[:1] == ["submission_start"] and names[-1] == "submission_end"
    assert on_disk[-1]["message"]["content"][0]["text"] == "re: hello"
    log = served.out.getvalue()
    assert "turn started (fake-model): 'hello'" in log
    assert "ended: 5 tokens" in log
    await one.close()
    await two.close()


async def test_listing_spans_every_cwd(served, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    client = await served.client()
    for cwd in ("a", "b"):
        await client.request(p.CreateSession(cwd=str(tmp_path / cwd), name=cwd))
    rows = (await client.request(p.ListSessions()))["sessions"]
    assert {row["cwd"] for row in rows} == {str(tmp_path / "a"), str(tmp_path / "b")}
    with pytest.raises(ServeError, match="not_found"):
        await client.request(p.CreateSession(cwd=str(tmp_path / "missing")))
    await client.close()


async def test_a_reconnect_replays_what_it_missed(served, tmp_path):
    first = await served.client()
    session_id = (await first.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    held = await first.attach(session_id)
    await first.close()

    driver = await served.client()
    replica = await driver.attach(session_id)
    await driver.request(
        p.Submit(session_id=session_id, cursor_id=replica.head_cursor_id, text="x")
    )

    again = await served.client()
    again.replicas[session_id] = held
    resumed = await again.attach(session_id)
    assert resumed is held, "the held replica is resumed, not replaced"
    await _until(lambda: held.seq == served.daemon.hosts[session_id].seq)
    assert held.entries == _file_entries(served.daemon, session_id)
    await driver.close()
    await again.close()


async def test_a_token_is_required_only_when_configured(tmp_path, provider, extensions_off):
    server, served = await _serve(tmp_path, {**_CONFIG, "serve": {"token": "s3cret"}})
    try:
        with pytest.raises(ServeError, match="unauthorized"):
            await served.client(token="wrong")
        client = await served.client(token="s3cret")
        assert (await client.request(p.ListSessions()))["sessions"] == []
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_two_cursors_stream_at_once_to_one_client(served, tmp_path):
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    replica = await client.attach(session_id)
    head = replica.head_cursor_id
    other = (await client.request(p.OpenCursor(session_id=session_id, leaf=None, label="side")))[
        "cursor_id"
    ]
    await client.request(p.SetModel(session_id=session_id, cursor_id=other, model="other"))

    await asyncio.gather(
        client.request(p.Submit(session_id=session_id, cursor_id=head, text="a")),
        client.request(p.Submit(session_id=session_id, cursor_id=other, text="b")),
    )

    await _until(lambda: replica.seq == served.daemon.hosts[session_id].seq)
    states = replica.cursors
    assert states[other]["model"] == "other-model"
    texts = {
        e["message"]["content"][0]["text"]
        for e in replica.entries
        if e.get("message", {}).get("role") == "assistant"
    }
    assert texts == {"re: a", "re: b"}
    await client.close()


async def test_a_form_goes_to_clients_and_the_first_answer_wins(served, tmp_path):
    requests: list[dict[str, Any]] = []
    client = await served.client(
        on_event=lambda e: requests.append(e) if e["kind"] == "request" else None
    )
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    await client.attach(session_id)
    host = served.daemon.hosts[session_id]
    spec = {"title": "pick", "fields": [{"name": "n", "kind": "text", "default": "d"}]}

    asking = asyncio.create_task(host.ui.form(spec))
    await _until(lambda: bool(requests))
    request_id = requests[0]["data"]["request_id"]
    await client.request(p.Answer(session_id=session_id, request_id=request_id, value={"n": "x"}))
    assert await asking == {"n": "x"}
    with pytest.raises(ServeError, match="not_found"):
        await client.request(p.Answer(session_id=session_id, request_id=request_id, value=None))

    await client.request(p.Detach(session_id=session_id))
    assert await host.ui.form(spec) == {"n": "d"}, "no client attached: the declared default"
    await client.close()


async def test_a_client_that_falls_behind_is_dropped(served, tmp_path, monkeypatch):
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    await client.attach(session_id)
    host = served.daemon.hosts[session_id]

    monkeypatch.setattr(daemon_module, "QUEUE_BOUND", 2)
    stuck_ws = AsyncMock()
    stuck = Client(stuck_ws, 99)
    served.daemon.clients.add(stuck)
    host.attach(stuck, None, None)
    for n in range(5):
        host.publish("ui", {"op": "notify", "message": str(n), "level": "info"})

    assert stuck.dropped and stuck not in host.clients
    await _until(lambda: stuck_ws.close.await_count == 1)
    assert stuck_ws.close.await_args.args[0] == daemon_module.SLOW_CLIENT_CLOSE
    await client.close()


def test_addresses_parse_with_the_default_port():
    assert parse_address("buildbox") == Address("buildbox", p.DEFAULT_PORT)
    assert parse_address("ws://buildbox:9000/") == Address("buildbox", 9000)
    assert parse_address(":7") == Address("127.0.0.1", 7)
    assert parse_address("unix:/tmp/t.sock").path == "/tmp/t.sock"
    with pytest.raises(ValueError):
        parse_address("host:port")


def test_the_schema_names_every_request_and_the_checked_in_copy_is_current():
    schema = p.json_schema()
    for cls in p.REQUESTS:
        assert cls.__name__ in schema["$defs"]
    from tau_coding_agent.serve.protocol_doc import render_markdown, render_schema

    root = Path(__file__).resolve().parents[2]
    assert (root / "docs" / "SERVE-PROTOCOL.md").read_text() == render_markdown()
    assert (root / "docs" / "serve-protocol.schema.json").read_text() == render_schema()


async def _session_with_a_turn(client: ServeClient, cwd: Path, text: str = "hello"):
    session_id = (await client.request(p.CreateSession(cwd=str(cwd))))["session_id"]
    replica = await client.attach(session_id)
    await client.request(
        p.Submit(session_id=session_id, cursor_id=replica.head_cursor_id, text=text)
    )
    return session_id, replica


def _user_entry(entries: list[dict[str, Any]]) -> str:
    return next(e["id"] for e in entries if e.get("message", {}).get("role") == "user")


async def test_the_attach_answer_carries_the_whole_vocabulary_models_and_load_errors(
    served, tmp_path
):
    extensions = tmp_path / "home" / ".tau" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "broken.py").write_text("raise RuntimeError('nope')\n")
    (extensions / "greet.py").write_text(
        "def register(api):\n"
        "    api.register_command('greet', {'description': 'say hi', 'handler': "
        "lambda args, ctx: None})\n"
    )
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    replica = await client.attach(session_id)

    commands = {c["name"]: c for c in replica.surface["commands"]}
    assert commands["model"] == {
        "name": "model",
        "description": commands["model"]["description"],
        "origin": "builtin",
        "flow": True,
        "hidden": False,
    }
    assert commands["tree"]["flow"] is False, "a view is not a flow"
    assert commands["greet"]["origin"] == "extension" and not commands["greet"]["hidden"]
    assert any(c["hidden"] and c["name"].endswith("greet") for c in commands.values())
    [(path, error)] = replica.surface["load_errors"]
    assert path.endswith("broken.py") and "nope" in error
    assert [info["name"] for info in replica.surface["loaded"]] == ["greet"]
    assert replica.models == [
        {
            "name": "fake",
            "model": {"id": "fake-model", "provider": "openai", "context_window": 128000},
        },
        {
            "name": "other",
            "model": {"id": "other-model", "provider": "openai", "context_window": 128000},
        },
    ]
    await client.close()


async def test_next_step_and_enumerate_domain_answer_as_rpc_does(served, tmp_path):
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    await client.attach(session_id)

    step = await client.request(p.NextStep(session_id=session_id, flow="model"))
    assert step["status"] == "step" and step["ready"] is None
    assert step["step"]["argument"]["name"] == "name"
    assert step["step"]["domain"]["name"] == "model_name"
    values = await client.request(p.EnumerateDomain(session_id=session_id, domain="model_name"))
    assert values == {
        "domain": "model_name",
        "values": [{"value": "fake", "label": "fake"}, {"value": "other", "label": "other"}],
        "total": 2,
    }
    ready = await client.request(
        p.NextStep(session_id=session_id, flow="model", bound={"name": "other"})
    )
    assert ready["ready"] == {
        "flow": "model",
        "mutation": "set_model",
        "arguments": {"name": "other"},
    }
    with pytest.raises(ServeError, match="not_found"):
        await client.request(p.NextStep(session_id=session_id, flow="no-such-flow"))
    with pytest.raises(ServeError, match="not_found"):
        await client.request(p.EnumerateDomain(session_id=session_id, domain="no-such-domain"))
    await client.close()


async def test_a_command_missing_its_argument_answers_its_flow_step(served, tmp_path):
    from tau_agent_core.flows import FlowStep

    from tau_coding_agent.serve.remote import submission_result_from_wire

    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    replica = await client.attach(session_id)

    answer = await client.request(
        p.Submit(session_id=session_id, cursor_id=replica.head_cursor_id, text="/model")
    )

    assert answer["command"]["arm"] == "FlowStep"
    step = submission_result_from_wire(answer, "x").command
    assert isinstance(step, FlowStep)
    assert (step.flow, step.argument.name, step.domain.name) == ("model", "name", "model_name")
    assert step.domain.field_kind == "select"
    await client.close()


async def test_complete_path_reads_the_sessions_cwd_not_the_daemons(served, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("x")
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(project))))["session_id"]

    found = await client.request(p.CompletePath(session_id=session_id, text="see @no", offset=7))
    assert found["completion"]["token"] == "no"
    assert [m["name"] for m in found["completion"]["matches"]] == ["notes.txt"]
    nothing = await client.request(p.CompletePath(session_id=session_id, text="plain", offset=5))
    assert nothing == {"completion": None}
    await client.close()


async def test_get_tree_is_read_at_the_named_cursor(served, tmp_path):
    client = await served.client()
    session_id, replica = await _session_with_a_turn(client, tmp_path)
    user = _user_entry(replica.entries)
    side = (await client.request(p.OpenCursor(session_id=session_id, leaf=user)))["cursor_id"]

    tree = await client.request(p.GetTree(session_id=session_id, cursor_id=side))
    head = await client.request(p.GetTree(session_id=session_id, cursor_id=replica.head_cursor_id))

    assert tree["leaf"] == user and tree["count"] == len(tree["nodes"]) == len(replica.entries)
    assert [n["entry_id"] for n in tree["nodes"] if n["is_leaf"]] == [user]
    assert head["leaf"] != user
    assert set(tree["nodes"][0]) == set(p.TreeRow.__dataclass_fields__)
    await client.close()


async def test_fork_session_copies_into_a_new_session_and_leaves_the_source(served, tmp_path):
    client = await served.client()
    session_id, replica = await _session_with_a_turn(client, tmp_path)
    user = _user_entry(replica.entries)
    before = [e["id"] for e in replica.entries]

    whole = (await client.request(p.ForkSession(session_id=session_id, at=None)))["session_id"]
    at_user = (await client.request(p.ForkSession(session_id=session_id, at=user)))["session_id"]

    assert len({session_id, whole, at_user}) == 3
    copy = await client.attach(whole)
    assert [e["id"] for e in copy.entries] == before
    assert copy.cwd == replica.cwd
    cut = await client.attach(at_user)
    assert cut.entries[-1]["id"] == user
    assert len(cut.entries) < len(before)
    assert [e["id"] for e in replica.entries] == before, "the source is unchanged"
    with pytest.raises(ServeError, match="not_found"):
        await client.request(p.ForkSession(session_id=session_id, at="no-such-entry"))
    rows = (await client.request(p.ListSessions()))["sessions"]
    assert {whole, at_user} <= {row["id"] for row in rows}
    await client.close()


async def test_a_fork_command_answers_ready_for_the_client_to_perform(served, tmp_path):
    client = await served.client()
    session_id, replica = await _session_with_a_turn(client, tmp_path)

    answer = await client.request(
        p.Submit(session_id=session_id, cursor_id=replica.head_cursor_id, text="/fork")
    )

    assert answer["command"] == {
        "arm": "Ready",
        "flow": "fork",
        "mutation": "fork",
        "arguments": {},
    }
    await client.close()


async def test_perform_acts_at_the_named_cursor_not_the_head(served, tmp_path):
    client = await served.client()
    session_id, replica = await _session_with_a_turn(client, tmp_path)
    head = replica.head_cursor_id
    head_leaf = replica.cursors[head]["leaf"]
    user = _user_entry(replica.entries)
    side = (await client.request(p.OpenCursor(session_id=session_id, leaf=head_leaf)))["cursor_id"]

    await client.request(
        p.Perform(
            session_id=session_id,
            cursor_id=side,
            method="navigate_tree",
            arguments={"target_id": user},
        )
    )
    named = await client.request(
        p.Perform(
            session_id=session_id,
            cursor_id=side,
            method="set_session_name",
            arguments={"name": "from the side"},
        )
    )

    await _until(lambda: replica.seq == served.daemon.hosts[session_id].seq)
    assert replica.cursors[head]["leaf"] == head_leaf, "the head did not move"
    info = replica.entries[-1]
    assert info["type"] == "session_info" and info["parentId"] == user
    assert replica.cursors[side]["leaf"] == info["id"]
    assert named["kind"] == "Performed" and named["fields"]["cursor"] == info["id"]
    with pytest.raises(ServeError, match="bad_request"):
        await client.request(
            p.Perform(session_id=session_id, cursor_id=head, method="set_model", arguments={})
        )
    await client.close()


async def test_a_busy_cursor_enqueues_a_second_prompt_with_the_cores_own_strategy(served, tmp_path):
    """A prompt to a busy cursor waits for the turn and runs after it (docs/TAU-SERVE.md §7.3).

    The strategy is the core's ``MultitaskStrategy``; 0.3 offered a ``follow_up``
    that ``AgentSession.submit`` has no branch for, so every such prompt failed.
    """
    client = await served.client()
    session_id = (await client.request(p.CreateSession(cwd=str(tmp_path))))["session_id"]
    replica = await client.attach(session_id)
    head = replica.head_cursor_id

    first, second = await asyncio.gather(
        client.request(p.Submit(session_id=session_id, cursor_id=head, text="one")),
        client.request(
            p.Submit(
                session_id=session_id, cursor_id=head, text="two", multitask_strategy="enqueue"
            )
        ),
    )

    assert first["accepted"] and second["accepted"]
    await _until(lambda: replica.seq == served.daemon.hosts[session_id].seq)
    users = [
        e["message"]["content"][0]["text"]
        for e in replica.entries
        if e.get("message", {}).get("role") == "user"
    ]
    assert users == ["one", "two"], "the second prompt ran after the first, on the same path"
    with pytest.raises(ValueError, match="multitask_strategy|follow_up"):
        p.parse_request(
            {
                "id": 1,
                "type": "submit",
                "session_id": session_id,
                "cursor_id": head,
                "text": "x",
                "multitask_strategy": "follow_up",
            }
        )
    await client.close()
