"""`tau -p` when the run does not finish (D22 section 6): what the model wrote in the turn so far
is still printed on stdout, then the stderr note says why it stopped. The model is an
OpenAI-compatible endpoint that replays a script: each request gets the next reply, which may
carry a tool call."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from world import Result, World, tau

PLAN = "Plan: add the retry to the fetch tool."
LOOK = "Let me look at the file."
CALL = {
    "index": 0,
    "id": "c1",
    "type": "function",
    "function": {"name": "read", "arguments": '{"path": "f.txt"}'},
}

HALT = """
class Halt:
    name = "halt"

    async def post_reply(self, cursor, subject):
        return "enough"

extensions = [Halt()]
"""
REFUSE = """
class Refuse:
    name = "refuse"

    async def pre_input(self, cursor, subject):
        return "no input today"

extensions = [Refuse()]
"""
ASK = """
from typing import Literal

from tau import Body, Stop


class Approve:
    name = "approve"

    async def post_reply(self, cursor, subject):
        reply = subject.node.body if subject.node else None
        if isinstance(reply, Body.Assistant) and reply.stop == Stop.End:
            await cursor.ask("Approve this plan?", Literal["approve", "reject"])

extensions = [Approve()]
"""
"""A question when a reply ends, as a plan-approval step would ask it."""
BOOM = """
class Boom:
    name = "boom"

    async def post_reply(self, cursor, subject):
        raise RuntimeError("it broke")

extensions = [Boom()]
"""


class Script(BaseHTTPRequestHandler):
    """Reply `n` to request `n`: `(text, calls)`; the last reply repeats."""

    replies: ClassVar[list[tuple[str, list[dict[str, object]]]]] = []
    seen: ClassVar[int] = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        text, calls = Script.replies[min(Script.seen, len(Script.replies) - 1)]
        Script.seen += 1
        delta: dict[str, object] = {"role": "assistant", "content": text}
        if calls:
            delta["tool_calls"] = calls
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}
            ],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model(world: World) -> Iterator[World]:
    Script.seen = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Script)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    world.user_config({"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}})
    (world.home / "f.txt").write_text("hello\n", encoding="utf-8")
    try:
        yield world
    finally:
        server.shutdown()
        server.server_close()


def p(world: World, *argv: str) -> Result:
    return tau(world, "-p", "--no-context-files", *argv)


def ext(world: World, source: str) -> str:
    path = world.home / "ext.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


def last(result: Result) -> dict[str, object]:
    return json.loads(result.out.splitlines()[-1])


def test_a_plan_nobody_approves_is_still_printed(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "-e", ext(model, ASK), "add retry")
    assert result.code == 3, result.err
    assert result.out == PLAN + "\n"
    assert 'tau: question 0 from approve on cursor 0 has no rule: "Approve this plan?"' in (
        result.err
    )
    assert "tau: stopped before finishing: question 0 has no --answer or --decline rule" in (
        result.err
    )


def test_every_message_of_the_turn_is_printed_not_its_calls(model: World) -> None:
    Script.replies = [(LOOK, [CALL]), (PLAN, [])]
    result = p(model, "-e", ext(model, ASK), "add retry")
    assert result.code == 3, result.err
    assert result.out == f"{LOOK}\n\n{PLAN}\n"


def test_a_step_limit_that_cuts_off_a_call_still_prints_what_came_before(model: World) -> None:
    Script.replies = [(LOOK, [CALL]), (PLAN, [])]
    result = p(model, "--set", "step_limit.limit=1", "go")
    assert result.code == 3, result.err
    assert result.out == LOOK + "\n"
    assert "tau: stopped before finishing: auto-step is off" in result.err


def test_a_halt_after_the_reply_prints_it(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "-e", ext(model, HALT), "go")
    assert result.code == 3, result.err
    assert result.out == PLAN + "\n"
    assert "tau: stopped before finishing: halted: enough" in result.err


def test_a_failure_after_the_model_spoke_prints_what_it_said(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "-e", ext(model, BOOM), "go")
    assert result.code == 1
    assert result.out == PLAN + "\n"
    assert "it broke" in result.err


def test_json_a_stop_ends_with_a_stopped_line_that_holds_the_text(model: World) -> None:
    Script.replies = [(LOOK, [CALL]), (PLAN, [])]
    result = p(model, "--json", "-e", ext(model, ASK), "add retry")
    assert result.code == 3, result.err
    assert last(result) == {
        "stopped": {
            "message": "stopped before finishing: question 0 has no --answer or --decline rule",
            "partial": f"{LOOK}\n\n{PLAN}",
        }
    }


def test_json_a_failure_adds_the_text_to_the_error(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "--json", "-e", ext(model, BOOM), "go")
    assert result.code == 1
    error = last(result)["error"]
    assert isinstance(error, dict)
    assert error["type"] == "Failed"
    assert error["partial"] == PLAN


def test_json_a_stop_before_the_model_spoke_has_no_partial(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "--json", "-e", ext(model, REFUSE), "go")
    assert result.code == 3, result.err
    stop = last(result)["stopped"]
    assert isinstance(stop, dict)
    assert "partial" not in stop


def test_a_clean_run_prints_only_the_last_reply(model: World) -> None:
    Script.replies = [(LOOK, [CALL]), (PLAN, [])]
    result = p(model, "go")
    assert result.code == 0, result.err
    assert result.out == PLAN + "\n"
    assert result.err == ""


def test_a_clean_run_under_json_has_no_extra_line(model: World) -> None:
    Script.replies = [(PLAN, [])]
    result = p(model, "--json", "go")
    assert result.code == 0, result.err
    assert "stopped" not in last(result)
    assert "error" not in last(result)
