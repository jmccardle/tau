"""`tau -p` while a provider retries (D24 section 4, D22 section 3): each wait is a stderr line,
and a sample that runs out of retries says how many it made. The model is an OpenAI-compatible
endpoint that answers 429 a set number of times."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from world import World, tau


class Busy(BaseHTTPRequestHandler):
    """Answers 429 (`Retry-After: 1`) `refuse` times, then says `ok`."""

    refuse: ClassVar[int] = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        if Busy.refuse > 0:
            Busy.refuse -= 1
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "slow down"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
        }
        done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        for item in (chunk, done):
            self.wfile.write(b"data: " + json.dumps(item).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def busy(world: World) -> Iterator[World]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    world.user_config({"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}})
    try:
        yield world
    finally:
        server.shutdown()
        server.server_close()


def test_a_wait_before_a_retry_is_a_stderr_line_without_verbose(busy: World) -> None:
    Busy.refuse = 1
    result = tau(busy, "-p", "--no-context-files", "go")
    assert result.code == 0, result.err
    assert result.out == "ok\n"
    assert "tau: HTTP 429" in result.err
    assert ": retry 1 of 3 in 1 s\n" in result.err


def test_a_sample_that_runs_out_of_retries_says_how_many_it_made(busy: World) -> None:
    Busy.refuse = 99
    result = tau(busy, "-p", "--no-context-files", "go")
    assert result.code == 1
    assert result.err.count(" of 3 in 1 s") == 3
    assert "still failing after 3 retries" in result.err
