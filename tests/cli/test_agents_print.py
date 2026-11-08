"""Agents under `tau -p` (D24 section 8): a model that hands a task to a sub-agent, an
OpenAI-compatible endpoint that answers as the parent and as the agent, and the tree it leaves."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from world import World, tau

from tau import Body, Part, Session, list_sessions


class Model(BaseHTTPRequestHandler):
    """The parent calls `agent("sub")`, the agent says `child says hi`, the parent then says
    `done`. `told` collects the tool messages the parent read."""

    told: ClassVar[list[str]] = []
    offered: ClassVar[list[list[str]]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Model.offered.append([t["function"]["name"] for t in body.get("tools", [])])
        last = body["messages"][-1]
        if last["role"] == "tool":
            Model.told.append(last["content"])
            delta, finish = {"role": "assistant", "content": "done"}, "stop"
        elif last["content"] == "sub":
            delta, finish = {"role": "assistant", "content": "child says hi"}, "stop"
        else:
            call = {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {"name": "agent", "arguments": json.dumps({"task": "sub"})},
            }
            delta, finish = {"tool_calls": [call]}, "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model(world: World) -> Iterator[World]:
    Model.told, Model.offered = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = {"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}}
    world.user_config({**config, "load": {"agents": True}})
    try:
        yield world
    finally:
        server.shutdown()
        server.server_close()


def test_the_model_delegates_and_reads_the_agents_answer(model: World) -> None:
    result = tau(model, "-p", "--no-context-files", "go")
    assert result.code == 0, result.err
    assert result.out == "done\n"
    assert Model.told == ["agent:1 answered:\nchild says hi"]
    assert "agent" in Model.offered[0]
    assert "agent" not in Model.offered[1], "the agent is not offered `agent`"


def test_without_load_the_model_is_never_offered_the_tool(model: World) -> None:
    config = json.loads((model.tau / "config.json").read_text())
    config.pop("load")
    model.user_config(config)
    result = tau(model, "-p", "--no-context-files", "go")
    assert result.code == 0, result.err
    assert all("agent" not in names for names in Model.offered)


def test_the_session_keeps_the_agent_as_a_closed_cursor_with_its_label(model: World) -> None:
    assert tau(model, "-p", "--no-context-files", "go").code == 0
    (found,) = list_sessions(model.tau / "sessions")
    session = Session.load(model.tau / "sessions" / found.id)
    try:
        child = session.label("agent:1")
        assert child not in session.cursors()
        last = dict(session.closed())[child]
        reply = session.tree.node(last).body
        assert isinstance(reply, Body.Assistant)
        assert reply.parts == [Part.Text("child says hi")]
    finally:
        session.close()


def test_a_tools_name_nothing_provides_is_a_warning_at_the_start_and_the_check(
    model: World,
) -> None:
    config = json.loads((model.tau / "config.json").read_text())
    config["extensions"] = {"agents": {"tools": ["read", "grpe"]}}
    model.user_config(config)
    result = tau(model, "-p", "--no-context-files", "go")
    assert result.code == 0, result.err
    assert "tau: config: warning: extensions.agents: tools names 'grpe'" in result.err
    assert Model.offered, "the start went on"
    checked = tau(model, "config", "check")
    assert checked.code == 0
    assert "warning: extensions.agents: tools names 'grpe'" in checked.both
    config["extensions"] = {"agents": {"tools": ["read", "grep"]}}
    model.user_config(config)
    assert tau(model, "-p", "--no-context-files", "go").code == 0
