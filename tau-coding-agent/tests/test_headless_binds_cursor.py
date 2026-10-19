"""``tau -p`` binds its cursor, so every entry the core writes reaches the session file.

Print mode used to run the turn on a scratch in-memory log and copy only the
produced *messages* back, so an extension's ``api.append_entry`` (a sandbox
snapshot, a tool-exposure change) was lost, and ``--continue`` resumed without
it. Exercised through a real ``tau -p`` subprocess against an in-process fake
OpenAI-compatible server (docs/REPL-HEAD.md §3, "bind, do not append").
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tau_agent_core.testing import child_env
from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]

_EXTENSION = (
    "def register(api):\n"
    "    async def hook(event, ctx):\n"
    "        await api.append_entry('probe', {'prompt': event['prompt']})\n"
    "    api.on('before_agent_start', hook)\n"
    "    async def spawn(args, ctx):\n"
    "        result = await ctx.spawn_branch(None, 'branch task', tools=[], label='probe')\n"
    "        return f'ok={result.ok}'\n"
    "    api.register_command('spawn', {'description': 'spawn a branch', 'handler': spawn})\n"
)

_SSE_BODY = (
    'data: {"id":"c","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"c","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = _SSE_BODY.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def tau_p(tmp_path):
    """A ``tau -p`` runner with an isolated HOME, the fake server and the probe extension."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
    }
    (home / ".tau" / "config.json").write_text(json.dumps(config))
    ext = tmp_path / "probe_ext.py"
    ext.write_text(_EXTENSION)
    env = child_env(home)

    def run(*args: str) -> list[dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "tau_coding_agent.cli", "-p", "-e", str(ext), *args],
            env=env,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, proc.stderr
        files = list((home / ".tau" / "sessions").rglob("*.jsonl"))
        assert len(files) == 1, files
        return Session.load(files[0]).entries()

    try:
        yield run
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_an_extension_entry_survives_print_mode_and_continue(tau_p):
    """Both runs' ``customEntry`` writes are on disk, in order, on one path."""
    tau_p("first")
    entries = tau_p("--continue", "second")

    probes = [e["data"]["prompt"] for e in entries if e.get("customType") == "probe"]
    assert probes == ["first", "second"]
    roles = [e["message"]["role"] for e in entries if e.get("type") == "message"]
    assert roles.count("user") == 2, roles
    assert roles.count("assistant") == 2, roles


def test_a_branch_a_command_spawns_reaches_the_session_file(tau_p):
    """docs/SEEDED-BRANCHES.md §8: a command returns before any head-turn copy-back, so
    without the bound cursor its branch stayed in memory and the file held only the
    header, the config and the system message."""
    entries = tau_p("/spawn")

    messages = [e["message"] for e in entries if e.get("type") == "message"]
    users = [m for m in messages if m["role"] == "user"]
    assert [m["content"][0]["text"] for m in users] == ["branch task"]
    assert [m["role"] for m in messages].count("assistant") == 1
