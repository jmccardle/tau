"""The serve protocol's JSON Schema against the bytes a real daemon sends (docs/TAU-SERVE.md §5).

The daemon runs in this process on an ephemeral port, its provider stubbed, and
a raw WebSocket client records every frame. Each frame is validated against
``protocol.json_schema()`` with ``strict`` on, so a key the daemon sends and the
schema does not declare fails here; each result is validated against what the
schema's ``Results`` map names for its request.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, get_args, get_type_hints
from unittest.mock import MagicMock, patch

import pytest
from tau_agent_core.testing.schema_check import SchemaError, open_nodes, validate
from test_serve import _CONFIG, _fake_stream, _serve, _until
from websockets.asyncio.client import connect

from tau_llm.streaming import DoneEvent, TextDeltaEvent, ThinkingDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, ThinkingContent, Usage

from tau_coding_agent import __version__
from tau_coding_agent.serve import cli as serve_cli
from tau_coding_agent.serve import protocol as p

SCHEMA = p.json_schema()

_GATE = """
CONFIG_SCHEMA = {"title": "Gate", "fields": [{"name": "voice", "kind": "text"}]}

ASK = {
    "title": "Voice",
    "text": "Pick one before continuing.",
    "fields": [{"name": "voice", "kind": "select", "options": ["alto", "bass"]}],
    "actions": [{"label": "Use it", "command": "gate-release"}],
}


def register(api):
    async def arm(args, ctx):
        await api.request_user_action("Pick a voice.", lock=True, ask=ASK, release="gate-release")

    async def release(args, ctx):
        return "released " + args

    async def note(args, ctx):
        await api.send_message({"customType": "note", "content": "a note"})

    api.register_command("arm", {"description": "arm a lock", "handler": arm})
    api.register_command("note", {"description": "append a note", "handler": note})
    api.register_command("gate-release", {"description": "release", "handler": release})
"""


async def _summary(model: Any, context: Any, options: Any = None, **_: Any) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="## Goal\nSUMMARY")],
        api="openai-completions",
        provider="openai",
        model=model.id,
        stop_reason="stop",
        usage=Usage(input_tokens=5, output_tokens=2, total_tokens=7),
    )


class Recorder:
    """A raw client that keeps every frame it sends and receives, for validation."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.sent: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self._ids = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def open(cls, address: Any) -> Recorder:
        """Connect and say hello."""
        recorder = cls(await connect(address.url, max_size=None))
        await recorder.request(p.Hello(protocol=p.PROTOCOL_VERSION, client="schema"))
        return recorder

    async def _read(self) -> None:
        async for raw in self.ws:
            frame = json.loads(raw)
            self.received.append(frame)
            if frame["type"] == "response" and frame["id"] in self._pending:
                kind, future = self._pending.pop(frame["id"])
                self.answers.append((kind, frame))
                future.set_result(frame)

    async def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Send one raw request frame and return its response frame."""
        self._ids += 1
        frame = {**frame, "id": self._ids}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[self._ids] = (str(frame.get("type")), future)
        self.sent.append(frame)
        await self.ws.send(json.dumps(frame))
        return await asyncio.wait_for(future, 10)

    async def request(self, message: Any) -> Any:
        """Send a request dataclass and return its result, failing on an error answer."""
        response = await self.frame(p.to_wire(message))
        assert response["ok"], response["error"]
        return response["result"]

    async def rpc(self, verb: str, session_id: str, cursor_id: str, **params: Any) -> Any:
        """An RPC verb at a cursor; its result."""
        return await self.request(p.RpcCall(verb, session_id, cursor_id, params))

    async def submit(
        self, session_id: str, cursor_id: str, text: str, verb: str = "submit", **params: Any
    ) -> Any:
        """``submit`` (or ``prompt``) with provenance; waits for the turn it admitted to end."""
        self._ids_submitted = getattr(self, "_ids_submitted", 0) + 1
        submission_id = f"sub-{self._ids_submitted}"
        fields = {"text": text, "submission_id": submission_id, **params}
        if verb == "submit":
            fields = {"source": "rpc", "submitter": "schema", **fields}
        answer = await self.rpc(verb, session_id, cursor_id, **fields)
        if answer["admitted"]:
            await _until(
                lambda: any(
                    s["submission"]["submission_id"] == submission_id
                    for s in self.channel("submission_end")
                ),
                timeout=10,
            )
        return answer

    async def refused(self, message: Any) -> str:
        """Send a request the daemon must refuse, and return the error code."""
        response = await self.frame(p.to_wire(message))
        assert not response["ok"]
        return str(response["error"]["code"])

    def events(self, kind: str) -> list[dict[str, Any]]:
        """Every event of ``kind`` received so far."""
        return [f for f in self.received if f["type"] == "event" and f["kind"] == kind]

    def channel(self, name: str) -> list[dict[str, Any]]:
        """Every channel event's payload named ``name``."""
        return [e["data"]["payload"] for e in self.events("channel") if e["data"]["name"] == name]

    async def close(self) -> None:
        """Close the socket and wait for the reader."""
        await self.ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    def check(self) -> set[str]:
        """Validate every frame both ways; return the result and event types covered."""
        covered = set()
        for frame in self.sent:
            validate(frame, SCHEMA["ClientFrame"], SCHEMA)
        for frame in self.received:
            validate(frame, SCHEMA["ServerFrame"], SCHEMA, strict=True)
            if frame["type"] == "event":
                covered.add(f"event:{frame['kind']}")
        for kind, response in self.answers:
            if response["ok"]:
                validate(response["result"], SCHEMA["Results"][kind], SCHEMA, strict=True)
                covered.add(f"result:{kind}")
        return covered


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".tau" / "extensions").mkdir(parents=True)
    (home / ".tau" / "extensions" / "gate.py").write_text(_GATE)
    monkeypatch.setenv("HOME", str(home))
    with (
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream),
        patch("tau_agent_core.compaction.complete_simple", side_effect=_summary),
        patch("tau_llm.client.complete_simple", side_effect=_summary),
    ):
        server, served = await _serve(tmp_path, _CONFIG)
        yield served
        server.close()
        await server.wait_closed()
        await served.daemon.shutdown()


async def test_every_frame_a_session_produces_validates_against_the_schema(daemon, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("THE NOTES BODY\n")
    client = await Recorder.open(daemon.address)
    assert (await client.request(p.ListSessions()))["sessions"] == []
    sid = (await client.request(p.NewSession(cwd=str(project))))["session"]["session_id"]
    attached = await client.request(p.Attach(session_id=sid))
    head = attached["head_cursor_id"]
    host = daemon.daemon.hosts[sid]

    await client.submit(sid, head, "hello")
    await client.submit(sid, head, "again", verb="prompt")
    await client.submit(sid, head, "read @notes.txt and @missing.txt", expand_attachments=True)
    for command in ("/model", "/tree", "/name named", "/fork", "/note"):
        await client.submit(sid, head, command, expand_commands=True)

    step = await client.rpc("next_step", sid, head, flow="model")
    await client.rpc("enumerate_domain", sid, head, domain="model_name")
    ready = (await client.rpc("next_step", sid, head, flow="model", bound={"name": "other"}))[
        "ready"
    ]
    assert step["status"] == "step"
    performed = await client.request(
        p.PerformReady(session_id=sid, cursor_id=head, ready=p.Ready(**ready))
    )
    assert performed["arm"] == "Performed" and performed["mutation"] == "set_model"
    await _until(lambda: host.cursor_states()[0].model == "other")

    side = (await client.request(p.OpenCursor(session_id=sid, leaf=None, label="s")))["cursor_id"]
    await client.rpc("set_model", sid, side, name="fake")
    await client.request(p.MoveCursor(session_id=sid, cursor_id=side, leaf=None))
    await client.rpc("abort", sid, side)
    await client.request(p.CloseCursor(session_id=sid, cursor_id=side))
    await client.request(p.Describe(session_id=sid))
    for verb in (
        "get_state",
        "get_messages",
        "get_commands",
        "get_tools",
        "get_models",
        "get_session_name",
        "get_session_stats",
        "get_last_assistant_text",
        "get_pending_request",
        "list_managed_extensions",
        "get_extension_state",
    ):
        await client.rpc(verb, sid, head)
    await client.rpc("complete_path", sid, head, text="@no", offset=3)
    await client.rpc("complete_path", sid, head, text="plain", offset=5)
    await client.rpc("complete_message_id", sid, head, query="")
    tree = await client.rpc("get_tree", sid, head)
    path = [n["entry_id"] for n in tree["nodes"] if n["role"] in ("user", "assistant")]
    user = next(n["entry_id"] for n in tree["nodes"] if n["role"] == "user")
    await client.rpc("get_entry", sid, head, entry_id=user)
    gate = next(path for path, _ in host.agent_session.list_managed_extensions())
    await client.rpc("get_extension_config", sid, head, path=gate)
    await client.rpc("set_extension_config", sid, head, path=gate, values={"voice": "alto"})
    await client.rpc("disable_extension", sid, head, path=gate)
    await client.rpc("enable_extension", sid, head, path=gate)
    await client.rpc("reload_extension", sid, head, path=gate)
    await client.rpc("set_session_name", sid, head, name="renamed")
    await client.rpc("set_auto_compaction", sid, head, enabled=True)
    await client.rpc("paste_subtree", sid, head, source_id=path[1], target_id=path[0])
    await client.rpc("commit_branch", sid, head, ids=[path[0]], drop_context=False)
    await client.rpc("navigate", sid, head, target_id=path[-1])
    await client.rpc("elide_span", sid, head, anchor_id=path[-1], first_kept_id=path[-2])
    await client.rpc("summarize_and_navigate", sid, head, target_id=user)
    await client.submit(sid, head, "again", multitask_strategy="rollback")
    compacting = await client.rpc("compact", sid, head, custom_instructions="short")
    await _until(lambda: bool(client.events("compaction_end")))
    assert (
        client.events("compaction_end")[0]["data"]["compaction_id"] == (compacting["compaction_id"])
    )
    await client.request(p.Fork(session_id=sid, cursor_id=head))

    compare = await client.request(p.Compare(session_id=sid, models=["fake", "other"], text="both"))
    await _until(lambda: not any(c.busy for c in host.cursor_states()))
    await client.request(
        p.EndCompare(
            session_id=sid,
            comparison_id=compare["comparison_id"],
            keep=compare["cursors"][0]["cursor_id"],
        )
    )
    await client.submit(sid, head, "/arm", expand_commands=True)
    locked = host.cursor_states()[0].request
    assert locked is not None and locked.lock
    await client.rpc(
        "answer_request",
        sid,
        head,
        request_id=locked.entry_id,
        action="Use it",
        values={"voice": "alto"},
    )
    asking = asyncio.create_task(
        host.ui.form({"title": "t", "fields": [{"name": "n", "kind": "text"}]})
    )
    await _until(lambda: bool(client.events("request")))
    await client.request(
        p.Answer(
            session_id=sid,
            request_id=client.events("request")[0]["data"]["request_id"],
            value={"n": "x"},
        )
    )
    await asking
    host.ui.notify("hi")
    host.ui.set_status("k", "text")
    host.ui.panel("k", {"title": "P", "body": {"kind": "list", "items": ["a"]}, "actions": []})
    await client.request(p.Detach(session_id=sid))
    await client.close()

    covered = client.check()
    answered = {f"result:{p._type_tag(cls)}" for cls in p.REQUESTS}
    answered |= {f"result:{verb}" for verb in p.RPC_VERBS}
    events = {f"event:{kind}" for kind in p.EVENT_KINDS}
    assert answered | events <= covered, sorted((answered | events) - covered)
    names = {e["data"]["name"] for e in client.events("channel")}
    assert names == {"submission_start", "submission_end", "custom_message"}
    ops = {e["data"]["op"] for e in client.events("ui")}
    assert ops == {"notify", "status", "panel"}
    compare_starts = [
        s["submission"]["correlation"]
        for s in client.channel("submission_start")
        if s["submission"]["correlation"]
    ]
    assert sorted(c["compare"]["index"] for c in compare_starts) == [0, 1]


async def test_expanded_attachments_reach_the_model_and_the_report_rides_on_submission_start(
    daemon, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("THE NOTES BODY\n")
    client = await Recorder.open(daemon.address)
    session_id = (await client.request(p.NewSession(cwd=str(project))))["session"]["session_id"]
    head = (await client.request(p.Attach(session_id=session_id)))["head_cursor_id"]

    await client.submit(
        session_id, head, "read @notes.txt and @missing.txt", expand_attachments=True
    )
    await client.submit(session_id, head, "plain")

    expanded, plain = client.channel("submission_start")
    assert expanded["attachments"] == {
        "expanded": 1,
        "images": 0,
        "unresolved": ["missing.txt"],
        "failures": [],
    }
    assert plain["attachments"] is None, "null when expansion was not asked"
    answers = [
        e["message"]["content"][0]["text"]
        for e in daemon.daemon.hosts[session_id].log.entries()
        if e.get("message", {}).get("role") == "assistant"
    ]
    assert "THE NOTES BODY" in answers[0] and "THE NOTES BODY" not in answers[1]
    await client.close()
    client.check()


async def test_a_lock_shows_on_the_cursor_and_clears_when_answered(daemon, tmp_path):
    client = await Recorder.open(daemon.address)
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    head = (await client.request(p.Attach(session_id=session_id)))["head_cursor_id"]

    await client.submit(session_id, head, "/arm", expand_commands=True)

    states = client.events("cursors")[-1]["data"]["cursors"]
    request = next(s for s in states if s["cursor_id"] == head)["request"]
    assert request["lock"] is True and request["label"] == "Extension gate requires a response"
    assert request["ask"]["actions"] == [{"label": "Use it", "command": "gate-release"}]
    answered = await client.rpc(
        "answer_request",
        session_id,
        head,
        request_id=request["entry_id"],
        action="Use it",
        values={"voice": "alto"},
    )
    assert answered["handled"] is True
    states = client.events("cursors")[-1]["data"]["cursors"]
    assert next(s for s in states if s["cursor_id"] == head)["request"] is None
    await client.close()
    client.check()


async def test_a_client_attaching_while_a_form_is_open_sees_it_and_can_answer_it(daemon, tmp_path):
    first = await Recorder.open(daemon.address)
    session_id = (await first.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    await first.request(p.Attach(session_id=session_id))
    host = daemon.daemon.hosts[session_id]
    spec = {"title": "pick", "fields": [{"name": "n", "kind": "text", "default": "d"}]}
    asking = asyncio.create_task(host.ui.form(spec))
    await _until(lambda: bool(first.events("request")))

    late = await Recorder.open(daemon.address)
    attached = await late.request(p.Attach(session_id=session_id))
    [open_form] = attached["requests"]
    assert open_form["spec"] == spec
    await late.request(
        p.Answer(session_id=session_id, request_id=open_form["request_id"], value={"n": "late"})
    )

    assert await asking == {"n": "late"}
    assert (await late.request(p.Attach(session_id=session_id)))["requests"] == []
    await first.close()
    await late.close()
    first.check()
    late.check()


async def test_perform_ready_checks_the_mutation_and_hands_a_fork_back(daemon, tmp_path):
    client = await Recorder.open(daemon.address)
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    head = (await client.request(p.Attach(session_id=session_id)))["head_cursor_id"]
    await client.submit(session_id, head, "hi")

    fork = p.Ready(flow="fork", mutation="fork", arguments={})
    answer = await client.request(p.PerformReady(session_id=session_id, cursor_id=head, ready=fork))
    mismatched = p.Ready(flow="model", mutation="fork", arguments={"name": "other"})
    unknown = p.Ready(flow="no-such-flow", mutation="x", arguments={})

    assert answer == {"arm": "Ready", "flow": "fork", "mutation": "fork", "arguments": {}}
    assert len(daemon.daemon.catalog.list(None)) == 1, "nothing was forked"
    assert (
        await client.refused(
            p.PerformReady(session_id=session_id, cursor_id=head, ready=mismatched)
        )
        == "bad_request"
    )
    assert (
        await client.refused(p.PerformReady(session_id=session_id, cursor_id=head, ready=unknown))
        == "not_found"
    )
    await client.close()
    client.check()


async def test_the_hello_names_the_daemon(daemon):
    client = await Recorder.open(daemon.address)
    [(_, hello)] = client.answers
    assert hello["result"] == {
        "protocol": p.PROTOCOL_VERSION,
        "client_id": hello["result"]["client_id"],
        "pid": os.getpid(),
        "version": __version__,
        "cwd": os.getcwd(),
    }
    await client.close()
    client.check()


@pytest.mark.parametrize(
    "frame",
    [
        {
            "type": "submit",
            "session_id": "s",
            "cursor_id": "c",
            "text": "t",
            "source": "rpc",
            "submitter": "x",
            "submission_id": "i",
            "expand_attachment": True,
        },
        {"type": "submit", "session_id": "s", "cursor_id": "c", "text": "t"},
        {"type": "get_state", "session_id": "s"},
        {"type": "set_model", "session_id": "s", "cursor_id": "c", "model": "m"},
        {"type": "hello", "protocol": "0", "client": "x", "extra": 1},
        {"type": "perform_ready", "session_id": "s", "cursor_id": "c", "ready": {"flow": "f"}},
        {
            "type": "perform_ready",
            "session_id": "s",
            "cursor_id": "c",
            "ready": {"flow": "f", "mutation": "m", "arguments": {}, "arm": "Ready"},
        },
        {"type": "no_such_request"},
    ],
)
def test_the_schema_and_parse_request_refuse_the_same_bad_frames(frame):
    with pytest.raises(SchemaError):
        validate({**frame, "id": 1}, SCHEMA["ClientFrame"], SCHEMA)
    with pytest.raises(ValueError):
        p.parse_request({**frame, "id": 1})


@pytest.mark.parametrize(
    "frame",
    [
        {
            "type": "submit",
            "session_id": "s",
            "cursor_id": "c",
            "text": "t",
            "source": "rpc",
            "submitter": "x",
            "submission_id": "i",
            "expand_attachments": True,
        },
        {"type": "prompt", "session_id": "s", "cursor_id": "c", "text": "t"},
        {"type": "get_state", "session_id": "s", "cursor_id": "c"},
        {"type": "set_model", "session_id": "s", "cursor_id": "c", "name": "m"},
        {
            "type": "perform_ready",
            "session_id": "s",
            "cursor_id": "c",
            "ready": {"flow": "f", "mutation": "m", "arguments": {"a": 1}},
        },
        {"type": "list_sessions"},
    ],
)
def test_the_schema_and_parse_request_accept_the_same_good_frames(frame):
    validate({**frame, "id": 1}, SCHEMA["ClientFrame"], SCHEMA)
    p.parse_request({**frame, "id": 1})


def test_strict_validation_refuses_a_key_the_schema_does_not_declare():
    state = {
        "cursor_id": "c",
        "leaf": None,
        "label": "",
        "owner_id": None,
        "busy": False,
        "model": "m",
        "request": None,
    }
    cursor_state = {"$ref": "#/$defs/CursorState"}
    validate(state, cursor_state, SCHEMA, strict=True)
    validate({**state, "later": 1}, cursor_state, SCHEMA)
    with pytest.raises(SchemaError, match="later"):
        validate({**state, "later": 1}, cursor_state, SCHEMA, strict=True)


def test_an_unfinished_entry_matches_only_the_incomplete_shape():
    entry = {"$ref": "#/$defs/Entry"}
    common = {"id": "e", "parentId": None, "timestamp": "2026-10-03T00:00:00.000Z"}
    info = {**common, "type": "session_info", "name": "n"}
    validate(info, entry, SCHEMA, strict=True)
    validate({**info, "status": "incomplete"}, entry, SCHEMA, strict=True)
    with pytest.raises(SchemaError):
        validate({**info, "status": "done"}, entry, SCHEMA)
    validate({**common, "type": "jmfts:document", "body": 1}, entry, SCHEMA, strict=True)


def test_every_request_names_its_result_and_every_kind_its_data():
    defs = SCHEMA["$defs"]
    for cls in p.REQUESTS:
        tag = p._type_tag(cls)
        assert defs[cls.__name__]["x-result"] == SCHEMA["Results"][tag]
        assert defs[cls.__name__]["additionalProperties"] is False
    for verb in p.RPC_VERBS:
        assert defs[p._camel(verb)]["x-result"] == SCHEMA["Results"][verb]
        assert defs[p._camel(verb)]["additionalProperties"] is False
    assert set(SCHEMA["Results"]) == {p._type_tag(cls) for cls in p.REQUESTS} | set(p.RPC_VERBS)
    assert set(p.RESULTS) == set(p.REQUESTS)
    assert list(defs["Event"]["x-data"]) == list(p.EVENT_KINDS)
    assert set(p.EVENT_KINDS) == set(get_args(get_type_hints(p.Event)["kind"]))
    assert SCHEMA["x-protocol-version"] == p.PROTOCOL_VERSION
    assert SCHEMA["x-default-port"] == p.DEFAULT_PORT


def test_every_answer_and_event_payload_is_typed_to_its_leaves():
    """A client generates its types from this schema: no RPC request or answer may leave a shape as prose."""
    defs = SCHEMA["$defs"]
    named = [f"{p._camel(verb)}{half}" for verb in p.RPC_VERBS for half in ("", "Result")]
    named.append("CompactionEnd")
    assert [path for name in named for path in open_nodes(defs[name], name)] == []


def test_tau_serve_schema_prints_the_checked_in_schema(capsys):
    from tau_coding_agent.cli import main

    assert main(["serve", "--schema"]) == 0
    root = Path(__file__).resolve().parents[2]
    assert capsys.readouterr().out == (root / "docs" / "serve-protocol.schema.json").read_text()


async def test_dash_d_finds_a_running_daemon_and_starts_none(daemon, capsys):
    argv = ["-d", "--json", "--listen", str(daemon.address)]
    with patch.object(serve_cli.subprocess, "Popen") as popen:
        code = await asyncio.to_thread(serve_cli.run_serve, argv)

    assert code == 0 and not popen.called
    printed = json.loads(capsys.readouterr().out)
    validate(printed, SCHEMA["$defs"]["ServeStarted"], SCHEMA, strict=True)
    assert printed["started"] is False and printed["pid"] == os.getpid()
    assert printed["address"] == str(daemon.address)


async def test_dash_d_that_loses_the_bind_reports_the_winner(daemon, capsys):
    """Several ``-d`` at once: each passes the first probe, one binds, the rest exit.

    A loser reports the daemon that won, ``started: false``, rather than failing.
    """
    real_probe = serve_cli.hello_probe
    calls = {"n": 0}

    def probe(address, token):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_probe(address, token)

    lost = MagicMock()
    lost.poll.return_value = 1
    lost.pid = -1
    argv = ["-d", "--json", "--listen", str(daemon.address)]
    with (
        patch.object(serve_cli, "hello_probe", side_effect=probe),
        patch.object(serve_cli.subprocess, "Popen", return_value=lost),
    ):
        code = await asyncio.to_thread(serve_cli.run_serve, argv)

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["started"] is False and printed["pid"] == os.getpid()


def test_web_root_flag_overrides_the_config_and_a_file_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    page = tmp_path / "web"
    page.mkdir()
    assert serve_cli.serve_settings(
        str(page), {"serve": {"web_root": "/elsewhere", "token": "t"}}
    ) == {
        "web_root": str(page),
        "token": "t",
    }
    assert serve_cli.serve_settings(None, {"serve": {"web_root": "w"}}) == {"web_root": "w"}
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    address = serve_cli.Address(None, None, str(tmp_path / "t.sock"))
    code = asyncio.run(serve_cli.serve(address, {"serve": {"web_root": str(not_a_dir)}}))
    assert code == 2 and "is not a directory" in capsys.readouterr().err


def _chunked(chunks: int):
    """A provider streaming ``chunks`` ten-character text deltas after a quarter as many of reasoning."""

    class _Stream:
        def __init__(self, model_id: str) -> None:
            self.final = AssistantMessage(
                content=[
                    ThinkingContent(thinking="ponder it " * (chunks // 4)),
                    TextContent(text="lorem ips " * chunks),
                ],
                api="openai-completions",
                provider="openai",
                model=model_id,
                stop_reason="stop",
                usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
            )

        def __aiter__(self):
            async def gen():
                for _ in range(chunks // 4):
                    yield ThinkingDeltaEvent(delta="ponder it ", partial=self.final)
                for _ in range(chunks):
                    yield TextDeltaEvent(delta="lorem ips ", partial=self.final)
                yield DoneEvent(final=self.final, usage=self.final.usage)

            return gen()

        def abort(self) -> None:
            pass

    async def stream(model: Any, context: Any, options: Any = None) -> _Stream:
        return _Stream(model.id)

    return stream


async def _long_reply(daemon: Any, tmp_path: Path, chunks: int) -> tuple[Recorder, Any]:
    client = await Recorder.open(daemon.address)
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    head = (await client.request(p.Attach(session_id=session_id)))["head_cursor_id"]
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_chunked(chunks)):
        await client.submit(session_id, head, "go")
        await _until(lambda: bool(client.channel("submission_end")), timeout=30)
    await client.close()
    return client, daemon.daemon.hosts[session_id]


async def test_a_long_reply_streams_bounded_deltas_that_rebuild_the_answer(daemon, tmp_path):
    """agent_event is RPC's WireEvent: a message_update carries a chunk, not the
    message so far, so its size does not grow with the reply and the total is linear."""
    measured = {}
    for chunks in (200, 2000):
        client, host = await _long_reply(daemon, tmp_path, chunks)
        client.check()
        frames = client.events("agent_event")
        updates = [f for f in frames if f["data"]["type"] == "message_update"]
        text = "".join(f["data"]["delta"] for f in updates if f["data"]["block_type"] == "text")
        thinking = "".join(
            f["data"]["delta"] for f in updates if f["data"]["block_type"] == "thinking"
        )
        assert text == "lorem ips " * chunks
        assert thinking == "ponder it " * (chunks // 4)
        assert all("message" not in f["data"] for f in frames)
        measured[chunks] = (
            max(len(json.dumps(f)) for f in updates),
            sum(len(json.dumps(f)) for f in frames),
            sum(len(json.dumps(f)) for f in host.history if f["kind"] == "agent_event"),
        )
    (small_max, small_total, small_kept), (big_max, big_total, big_kept) = (
        measured[200],
        measured[2000],
    )
    assert big_max <= small_max + 8, "a frame grows by its seq's digits, not by the reply"
    assert big_total < 12 * small_total, "ten times the reply is about ten times the bytes"
    assert big_kept < 12 * small_kept, "the replay history holds bounded items"
