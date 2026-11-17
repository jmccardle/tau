#!/usr/bin/env python3
"""A deterministic, stdlib-only OpenAI-compatible server that replays a script.

    fake_openai.py --script SCRIPT.json [--port 0] [--host 127.0.0.1]
                   [--paced | --ttft-ms MS --tokens-per-sec N]

SCRIPT.json is {"responses": [R, ...]} where R is {"content": str | null, "tool_calls": [{"name":
str, "arguments": object}, ...]}. The response to a request is chosen by how many assistant
messages the request already carries (its position in the conversation), so the same script
gives every client the identical sequence, whether it asks once or resumes a saved session, and
concurrent clients never disturb each other. A request past the end of the script is a 400 that
says so: a client that asks for more than the script holds has diverged.

Pacing. By default every response is instant: the whole stream goes out at once, so a run
measures the client. `--paced` (or `--ttft-ms` and `--tokens-per-sec` yourself) imitates a real
model: a wait of TTFT milliseconds before the first byte, then the reply streamed in pieces of
about four characters (one token) at N tokens a second; a tool call's arguments are paced the same
way. The pacing is a function of the script alone, so every client sees the identical timing.

Endpoints: POST /v1/chat/completions (streaming SSE when "stream" is true, JSON otherwise),
GET /v1/models, GET /_stats: requests served, how many asked for a stream, and four digests over
the requests received (`calls`, `results`, `system`, `tools`; see `views`), so two runs can be
shown to have been asked the same things. On start it prints one line,
`listening http://HOST:PORT`, on standard output.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "fake-model"


def text_of(content: Any) -> str:
    """A message's text: a string, or the text parts of a list of parts (one client sends each)."""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").rstrip()


def canonical_args(arguments: Any) -> str:
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments.strip() else {}
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def views(request: dict[str, Any]) -> dict[str, Any]:
    """The request as four views, each normalised so two clients that ask the same thing agree.

    calls   the dialogue the model sees, minus what a tool answered: each message's role, its
            text with trailing whitespace dropped, and its tool calls as (name, sorted-key JSON
            arguments). Tool-call ids are left out (a client may mint its own).
    results what the tools answered: each tool message's text, trailing whitespace dropped.
    system  the system messages' text. Legitimately different between versions.
    tools   the offered tools' names and schemas, sorted by name. Legitimately different too.
    """
    calls: list[Any] = []
    results: list[str] = []
    system: list[str] = []
    for m in request.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            system.append(text_of(m.get("content")))
        elif role == "tool":
            results.append(text_of(m.get("content")))
            calls.append(["tool"])
        else:
            tcs = [
                [t["function"]["name"], canonical_args(t["function"].get("arguments"))]
                for t in m.get("tool_calls") or []
            ]
            calls.append([role, text_of(m.get("content")), tcs])
    tools = sorted(
        json.dumps(t.get("function", t), sort_keys=True) for t in request.get("tools") or []
    )
    return {"calls": calls, "results": results, "system": system, "tools": tools}


class Script:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.lock = threading.Lock()
        self.served = 0
        self.streamed = 0
        self.plain = 0
        self.digests = {k: hashlib.sha256() for k in ("calls", "results", "system", "tools")}

    def pick(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        messages = request.get("messages") or []
        index = sum(1 for m in messages if m.get("role") == "assistant")
        if index >= len(self.responses):
            raise LookupError(
                f"script exhausted: request carries {index} assistant messages, "
                f"the script has {len(self.responses)} responses"
            )
        seen = views(request)
        with self.lock:
            self.served += 1
            if request.get("stream"):
                self.streamed += 1
            else:
                self.plain += 1
            for key, digest in self.digests.items():
                digest.update(json.dumps(seen[key], sort_keys=True).encode() + b"\n")
        return index, self.responses[index]

    def stats(self) -> dict[str, Any]:
        return {
            "served": self.served,
            "streamed": self.streamed,
            "plain": self.plain,
            **{k: d.hexdigest() for k, d in self.digests.items()},
        }


def tool_calls_of(index: int, response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"call_{index}_{j}",
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
        }
        for j, c in enumerate(response.get("tool_calls") or [])
    ]


def usage_of(messages: list[dict[str, Any]], response: dict[str, Any]) -> dict[str, int]:
    prompt = sum(len(json.dumps(m)) for m in messages) // 4
    completion = len(json.dumps(response)) // 4
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def completion(index: int, response: dict[str, Any], usage: dict[str, int]) -> dict[str, Any]:
    calls = tool_calls_of(index, response)
    message: dict[str, Any] = {"role": "assistant", "content": response.get("content")}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": f"chatcmpl-{index}",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
        ],
        "usage": usage,
    }


TOKEN_CHARS = 4


def pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def sse(
    index: int, response: dict[str, Any], usage: dict[str, int], paced: bool
) -> list[tuple[bytes, int]]:
    """The stream as (bytes, tokens): the tokens the model would take to produce that piece."""

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
        body = {
            "id": f"chatcmpl-{index}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
            if "usage" not in extra
            else [],
            **extra,
        }
        return b"data: " + json.dumps(body).encode() + b"\n\n"

    out: list[tuple[bytes, int]] = [(chunk({"role": "assistant", "content": ""}), 0)]
    content = response.get("content") or ""
    if paced:
        parts = pieces(content, TOKEN_CHARS)
    else:
        words = content.split(" ")
        parts = (
            [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]
            if content
            else []
        )
    out += [(chunk({"content": w}), 1) for w in parts]
    for j, c in enumerate(tool_calls_of(index, response)):
        fn = c["function"]
        head = {
            "index": j,
            "id": c["id"],
            "type": "function",
            "function": {"name": fn["name"], "arguments": ""},
        }
        out.append((chunk({"tool_calls": [head]}), 1))
        args = pieces(fn["arguments"], TOKEN_CHARS) if paced else [fn["arguments"]]
        for a in args:
            out.append((chunk({"tool_calls": [{"index": j, "function": {"arguments": a}}]}), 1))
    out.append((chunk({}, "tool_calls" if response.get("tool_calls") else "stop"), 0))
    out.append((chunk({}, usage=usage), 0))
    out.append((b"data: [DONE]\n\n", 0))
    return out


def write_stream(
    h: BaseHTTPRequestHandler, chunks: list[tuple[bytes, int]], ttft: float, tps: float
) -> None:
    """Send `chunks` as a chunked event stream: TTFT first, then each piece when its tokens are due."""
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()
    if ttft:
        time.sleep(ttft)
    # Pace against the clock, not with a sleep per piece, so the rate does not drift.
    start, tokens = time.perf_counter(), 0
    for data, cost in chunks:
        tokens += cost
        if tps:
            time.sleep(max(0.0, start + tokens / tps - time.perf_counter()))
        h.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        h.wfile.flush()
    h.wfile.write(b"0\r\n\r\n")


def handler_for(script: Script, ttft: float, tps: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Headers and body leave in separate writes; with Nagle on, the second waits for the
        # client's delayed ACK (~40 ms a request), which would be measured as the client's speed.
        disable_nagle_algorithm = True

        def log_message(self, *_: Any) -> None:
            pass

        def handle(self) -> None:
            # A client that closes a kept-alive connection is not an event.
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                super().handle()

        def reply(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/v1/models":
                data = [{"id": MODEL, "object": "model", "created": 0, "owned_by": "demo"}]
                self.reply(200, json.dumps({"object": "list", "data": data}).encode())
            elif self.path == "/_stats":
                self.reply(200, json.dumps(script.stats()).encode())
            else:
                self.reply(404, b'{"error": {"message": "not found"}}')

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.reply(404, b'{"error": {"message": "not found"}}')
                return
            request = json.loads(body)
            try:
                index, response = script.pick(request)
            except LookupError as e:
                self.reply(400, json.dumps({"error": {"message": str(e)}}).encode())
                return
            usage = usage_of(request["messages"], response)
            if request.get("stream"):
                write_stream(self, sse(index, response, usage, bool(tps)), ttft, tps)
            else:
                body = json.dumps(completion(index, response, usage)).encode()
                if ttft or tps:
                    tokens = len(json.dumps(response)) / TOKEN_CHARS
                    time.sleep(ttft + (tokens / tps if tps else 0))
                self.reply(200, body)

    return Handler


def exit_with_parent(parent: int) -> None:
    """A server started by a run that was killed must not outlive it."""
    while os.getppid() == parent:
        time.sleep(1)
    os._exit(0)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--script", required=True, help="the JSON script to replay")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="0 picks a free port (printed on start)")
    p.add_argument(
        "--paced", action="store_true", help="imitate a model: --ttft-ms 400 --tokens-per-sec 70"
    )
    p.add_argument(
        "--ttft-ms",
        type=float,
        default=None,
        help="wait before the first byte (default 0, or 400 with --paced)",
    )
    p.add_argument(
        "--tokens-per-sec",
        type=float,
        default=None,
        help="stream rate, a token being 4 characters (default unlimited, or 70 with --paced)",
    )
    args = p.parse_args()
    with open(args.script, encoding="utf-8") as f:
        script = Script(json.load(f)["responses"])
    ttft = args.ttft_ms if args.ttft_ms is not None else (400.0 if args.paced else 0.0)
    tps = args.tokens_per_sec if args.tokens_per_sec is not None else (70.0 if args.paced else 0.0)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(script, ttft / 1000, tps))
    server.daemon_threads = True
    threading.Thread(target=exit_with_parent, args=(os.getppid(),), daemon=True).start()
    print(f"listening http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
