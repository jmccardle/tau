"""Minimal OpenAI-compatible streaming server for offline τ runs.

Turn 1 (no tool result in the request): emits parallel tool calls (find, grep, read, ls).
Turn 2 (tool results present): emits a text answer naming how many tool results it saw.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_CALLS = [
    ("find", {"name": "*.toml", "path": "."}),
    ("grep", {"pattern": "def ", "path": "tau-llm/src/tau_llm/abort.py"}),
    ("read", {"path": "tau-meta/src/tau_meta/__init__.py"}),
    ("ls", {"path": "."}),
]


def chunk(delta, finish=None):
    return {
        "id": "x", "object": "chat.completion.chunk", "created": int(time.time()),
        "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"fake"}]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body.get("messages", [])
        tool_results = [m for m in msgs if m.get("role") == "tool"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        out = []
        if not tool_results:
            out.append(chunk({"role": "assistant", "content": ""}))
            for i, (name, args) in enumerate(TOOL_CALLS):
                out.append(chunk({"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                                  "function": {"name": name, "arguments": ""}}]}))
                s = json.dumps(args)
                for j in range(0, len(s), 7):
                    out.append(chunk({"tool_calls": [{"index": i, "function": {"arguments": s[j:j + 7]}}]}))
            out.append(chunk({}, "tool_calls"))
        else:
            ok = sum(1 for m in tool_results if m.get("content"))
            text = f"FAKE-DONE saw {len(tool_results)} tool results ({ok} non-empty)."
            for w in text.split(" "):
                out.append(chunk({"content": w + " "}))
            out.append(chunk({}, "stop"))
        for c in out:
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.flush()
        usage = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake", "choices": [],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer.request_queue_size = 512
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
