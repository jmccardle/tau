"""A refused reply under `tau -p` (D22 section 6): it is a failure, exit 1, said on stderr, and
the refusal's own text is still printed. The model is an OpenAI-compatible endpoint that refuses
with a `refusal` delta or a `content_filter` finish."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from world import Result, World, tau


class Refuser(BaseHTTPRequestHandler):
    """Answers every request with one chunk: `delta`, finishing with `ends`."""

    delta: ClassVar[dict[str, str]] = {}
    ends: ClassVar[str] = "stop"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [{"index": 0, "delta": Refuser.delta, "finish_reason": Refuser.ends}],
        }
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model(world: World) -> Iterator[World]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Refuser)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    world.user_config({"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}})
    try:
        yield world
    finally:
        server.shutdown()
        server.server_close()


def p(world: World, *argv: str) -> Result:
    return tau(world, "-p", "--no-context-files", *argv, "go")


def test_a_refusal_fails_says_so_and_still_prints_its_text(model: World) -> None:
    Refuser.delta = {"role": "assistant", "refusal": "I can't help with that"}
    Refuser.ends = "stop"
    result = p(model)
    assert result.code == 1, result.err
    assert result.out == "I can't help with that\n"
    assert "tau: the model refused (finish reason stop)" in result.err


def test_a_filtered_reply_is_a_refusal_too(model: World) -> None:
    Refuser.delta, Refuser.ends = {"role": "assistant", "content": ""}, "content_filter"
    result = p(model)
    assert result.code == 1
    assert result.out == ""
    assert "tau: the model refused (finish reason content_filter)" in result.err


def test_under_json_a_refusal_ends_stdout_with_its_error(model: World) -> None:
    Refuser.delta, Refuser.ends = {"role": "assistant", "content": ""}, "content_filter"
    result = p(model, "--json")
    assert result.code == 1
    last = json.loads(result.out.splitlines()[-1])
    assert last == {
        "error": {
            "type": "Refused",
            "message": "the model refused (finish reason content_filter)",
        }
    }


def test_an_ordinary_answer_is_not_a_refusal(model: World) -> None:
    Refuser.delta, Refuser.ends = {"role": "assistant", "content": "hello"}, "stop"
    result = p(model)
    assert result.code == 0, result.err
    assert result.out == "hello\n"
