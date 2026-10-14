"""Experiment: a minimal OpenAI-compatible streaming endpoint that always answers "pong".

Usage: python stub_openai.py <port>
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    """Encode one chat.completion.chunk as an SSE data line."""
    body = {
        "id": "stub",
        "object": "chat.completion.chunk",
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    """Answers every POST with a streamed "pong"."""

    def do_POST(self) -> None:
        """Stream a fixed completion."""
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_chunk({"role": "assistant", "content": "po"}))
        self.wfile.write(_chunk({"content": "ng"}))
        self.wfile.write(_chunk({}, "stop"))
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args: object) -> None:
        """Silence the request log."""


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
