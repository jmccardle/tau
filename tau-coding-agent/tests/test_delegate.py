"""Smoke test for ``examples/20_delegate.py`` — the ``delegate`` subagent tool
(step S9 / E-demo-1).

The delegate spawns a *real* ``tau -p --mode json`` child process per task, so
this test stands up a real fake OpenAI-compatible HTTP server (stdlib
``http.server``) that streams a canned SSE completion, and points the child's
``~/.tau/config.json`` at it via a temp ``$HOME``. That exercises the whole path
the delegate depends on — spawn → E-json stream → usage roll-up — end to end,
against the fake provider (no network, no API key).

Coverage per the Verify clause:

* **single** — one child spawns, finishes clean, and its usage rolls into
  ``details`` (tokens the fake server reports).
* **parallel** — two children run concurrently and both succeed.
* **forced read-only** — the HARD CODE-GUARD: a parallel task naming a write
  tool *raises* (Fail-Early, not silently stripped), and the argv built for a
  parallel child carries only read-only ``--tools``.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-1, §8 S9.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DELEGATE_PATH = _REPO_ROOT / "examples" / "20_delegate.py"
_spec = importlib.util.spec_from_file_location("delegate_example", _DELEGATE_PATH)
assert _spec is not None and _spec.loader is not None
delegate = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses can resolve the module for its annotations
# (``from __future__ import annotations`` makes dataclass consult sys.modules).
sys.modules[_spec.name] = delegate
_spec.loader.exec_module(delegate)


# ── fake OpenAI-compatible provider (real HTTP server the child talks to) ────

_SSE_BODY = (
    'data: {"id":"cmpl-1","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Delegated result: done."},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":5,"total_tokens":16}}\n\n'
    "data: [DONE]\n\n"
)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # drain the request body
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        body = _SSE_BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request stderr logging
        pass


@pytest.fixture
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fake_home(tmp_path, fake_provider, monkeypatch):
    """A temp ``$HOME`` whose ``~/.tau/config.json`` default model points at the
    fake provider. The spawned child inherits this env and resolves its model
    from it.
    """
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider,
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
        "system_prompt": "You are a helpful subagent.",
    }
    (tau_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class _Ctx:
    """Minimal ExtensionContext stand-in (the delegate only reads ``ctx.cwd``)."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd


# ── forced read-only guard (pure; no spawn) ──────────────────────────────────


def test_parallel_guard_refuses_write_tool_failearly():
    # Fail-Early: a write tool in a parallel child RAISES, it is not stripped.
    for write_tool in ("write", "edit", "bash"):
        with pytest.raises(ValueError, match="read-only"):
            delegate._guard_parallel_tools([write_tool, "read"])


def test_parallel_guard_forces_readonly_allowlist_when_unspecified():
    # No tools named → forced to the read-only default allowlist.
    forced = delegate._guard_parallel_tools(None)
    assert forced == list(delegate.PARALLEL_READONLY_TOOLS)
    assert not (set(forced) & delegate.WRITE_TOOLS)


def test_parallel_child_argv_carries_only_readonly_tools():
    # The argv actually handed to a parallel child never names a write tool.
    forced = delegate._guard_parallel_tools(["read", "grep"])
    args = delegate._child_cli_args(
        model=None, tools=forced, system_prompt_path=None, task="scan the repo"
    )
    assert "--no-extensions" in args and "--no-session" in args
    assert args[args.index("--tools") + 1] == "read,grep"
    tools_field = args[args.index("--tools") + 1].split(",")
    assert not (set(tools_field) & delegate.WRITE_TOOLS)


# ── single mode: real spawn against the fake provider ────────────────────────


async def test_single_spawns_child_and_rolls_usage(fake_home):
    result = await delegate._delegate_execute(
        "call-1",
        {"task": "summarize the plan"},
        signal=None,
        on_update=None,
        ctx=_Ctx(str(fake_home)),
    )
    assert result.get("is_error") is not True
    details = result["details"]
    assert details["mode"] == "single"
    child = details["results"][0]
    # The child finished clean on the child-reported stop_reason...
    assert child["exit_code"] == 0
    assert child["stop_reason"] == "stop"
    # ...and its usage rolled up from the fake provider's message_end tokens.
    assert child["usage"]["turns"] == 1
    assert child["usage"]["input"] == 11
    assert child["usage"]["output"] == 5
    assert child["usage"]["context_tokens"] == 16
    # The final assistant text is surfaced as the tool content.
    assert "Delegated result: done." in result["content"][0]["text"]


# ── parallel mode: two real children, both succeed ───────────────────────────


async def test_parallel_spawns_children_and_forces_readonly(fake_home):
    result = await delegate._delegate_execute(
        "call-2",
        {
            "tasks": [
                {"task": "audit module A", "tools": ["read"]},
                {"task": "audit module B"},  # no tools → forced read-only allowlist
            ]
        },
        signal=None,
        on_update=None,
        ctx=_Ctx(str(fake_home)),
    )
    details = result["details"]
    assert details["mode"] == "parallel"
    assert len(details["results"]) == 2
    for child in details["results"]:
        assert child["exit_code"] == 0
        assert child["stop_reason"] == "stop"
        # Every parallel child is read-only — no write tool ever reached one.
        assert not (set(child["tools"]) & delegate.WRITE_TOOLS)
    assert "2/2 succeeded" in result["content"][0]["text"]


async def test_parallel_rejects_write_tool_endtoend(fake_home):
    # A write tool in a parallel task aborts the whole call (Fail-Early), before
    # any child is spawned.
    with pytest.raises(ValueError, match="read-only"):
        await delegate._delegate_execute(
            "call-3",
            {"tasks": [{"task": "patch it", "tools": ["write"]}]},
            signal=None,
            on_update=None,
            ctx=_Ctx(str(fake_home)),
        )
