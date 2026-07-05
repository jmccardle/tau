"""Smoke test for ``examples/23_context_surgeon.py`` — the capstone session-control
tools (step S22 / E-demo-3).

Exercises each of the three agent tools headlessly, most through the FULL agent
loop (only the network boundary ``stream_simple`` is faked, and the fake emits a
real tool call so the tool executes on a genuinely prepared call):

* **compact_now** — its ``ctx.compact(defer=True)`` records intent mid-turn and the
  compaction lands exactly once at the tail of ``prompt()`` (never mid-turn).
* **summarize_history** — ``ctx.summarize_branch`` appends a ``branch_summary`` for
  the requested entry (LLM summarizer patched, no network).
* **fork_session** — ``ctx.fork(mode="export")`` writes a NEW session file and the
  tool returns its path; with a ``delegate_task`` it also spawns a real isolated
  ``tau -p`` child (composition of demo 20) against a fake provider.

Plus the demo-22 composition: the surgeon tools registered alongside the gatekeeper
``tool_call`` veto — an out-of-scope write is still fenced while the surgeon tools
are live, proving the E2 safety composes.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-3, §8 S22.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog
from tau_coding_agent.session_store import Session

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SURGEON_PATH = _REPO_ROOT / "examples" / "23_context_surgeon.py"
_spec = importlib.util.spec_from_file_location("context_surgeon_example", _SURGEON_PATH)
assert _spec is not None and _spec.loader is not None
surgeon = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = surgeon
_spec.loader.exec_module(surgeon)


# ── loop harness (a faked network boundary; everything else is real) ──────────


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    """Emit one tool call, then a text stop once a toolResult for it appears."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _summary_response(text: str):
    async def _impl(model, context, options=None):
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",  # type: ignore[arg-type]
            timestamp=0,
        )

    return _impl


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            content = m["content"] if isinstance(m, dict) else m.content
            block = content[0]
            return block["text"] if isinstance(block, dict) else block.text
    raise AssertionError(f"no toolResult for {tool_name}")


def _tool_result_is_error(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return bool(m["is_error"] if isinstance(m, dict) else getattr(m, "is_error", False))
    raise AssertionError(f"no toolResult for {tool_name}")


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


# ── compact_now: deferral through the real loop ───────────────────────────────


async def test_compact_now_defers_and_applies_at_end_of_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple", _summary_response("SURGEON-COMPACT")
    )
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        # keep_recent_tokens=1 makes the deferred compaction cut almost everything
        # so it definitely appends; the large window keeps auto-compaction dormant.
        compaction_settings=CompactionSettings(enabled=True, keep_recent_tokens=1),
    )
    log = session.session_log
    for i in range(2):
        log.append_message(_msg("user", f"u{i}"))
        log.append_message(_msg("assistant", f"a{i}"))

    def compaction_count() -> int:
        return sum(1 for e in log.entries() if e["type"] == "compaction")

    assert compaction_count() == 0

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("compact_now", {}),
    ):
        messages = await session.prompt("please tidy up the context")

    # The tool returned a normal (non-error) result mid-turn …
    assert _tool_result_is_error(messages, "compact_now") is False
    assert "end of this turn" in _tool_result_text(messages, "compact_now")
    # … and exactly one compaction landed at the tail of prompt().
    assert compaction_count() == 1
    assert log.entries()[-1]["type"] == "compaction"
    assert "SURGEON-COMPACT" in log.entries()[-1]["summary"]


# ── summarize_history: branch summary through the real loop ───────────────────


async def test_summarize_history_appends_a_branch_summary(monkeypatch) -> None:
    monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("SURGEON-BRANCH"))
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    log = session.session_log
    log.append_message(_msg("user", "u0"))
    first_asst = log.append_message(_msg("assistant", "a0"))
    log.append_message(_msg("user", "u1"))
    log.append_message(_msg("assistant", "a1"))

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("summarize_history", {"from_entry": first_asst}),
    ):
        messages = await session.prompt("summarize the earlier work")

    assert _tool_result_is_error(messages, "summarize_history") is False
    # A branch_summary was appended for the requested entry.
    branch_summaries = [e for e in log.entries() if e["type"] == "branch_summary"]
    assert len(branch_summaries) == 1
    assert branch_summaries[0]["fromId"] == first_asst
    assert "SURGEON-BRANCH" in branch_summaries[0]["summary"]


async def test_summarize_history_requires_from_entry() -> None:
    # Fail-Early: a blank from_entry raises rather than summarizing nothing.
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    ctx = session._extension_api.context
    with pytest.raises(ValueError, match="from_entry"):
        await surgeon._summarize_history_execute("call", {"from_entry": "  "}, None, None, ctx)


# ── fork_session: export a new file (file-backed Session) ─────────────────────


@pytest.fixture
def isolate_tau_dir(tmp_path, monkeypatch):
    """Point the default sessions base at tmp so an export fork writes there."""
    monkeypatch.setattr("tau_coding_agent.session_store.TAU_DIR", tmp_path / "tau")
    return tmp_path


def _file_session(tmp_path) -> tuple[AgentSession, Session]:
    live = Session.create(str(tmp_path), "gpt-4o", "openai", base_dir=tmp_path / "tau" / "sessions")
    agent = AgentSession(
        session_log=live,
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    return agent, live


async def test_fork_session_exports_a_new_file(isolate_tau_dir) -> None:
    tmp_path = isolate_tau_dir
    agent, live = _file_session(tmp_path)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("fork_session", {}),
    ):
        messages = await agent.prompt("fork the session before we diverge")

    assert _tool_result_is_error(messages, "fork_session") is False
    text = _tool_result_text(messages, "fork_session")
    assert text.startswith("Forked the session to ")
    forked_path = text[len("Forked the session to ") :].split(".\n")[0].rstrip(".")
    assert Path(forked_path).exists()
    assert forked_path != str(live.path)
    # The source log is not touched by the export fork.
    assert Path(forked_path).read_text() != ""


async def test_fork_session_export_on_in_memory_log_raises() -> None:
    # Fail-Early: an in-memory SDK log has no file to export to.
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    ctx = session._extension_api.context
    with pytest.raises(RuntimeError, match="not file-backed"):
        await surgeon._fork_session_execute("call", {}, None, None, ctx)


# ── fork_session + delegate: real isolated child against a fake provider ──────

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
        self.rfile.read(length)
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

    def log_message(self, *_args) -> None:
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
    """A temp ``$HOME`` whose ``~/.tau/config.json`` points at the fake provider,
    so the spawned delegate child resolves its model from it. Also redirects the
    session store base under it so the export fork writes there too.
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
    monkeypatch.setattr("tau_coding_agent.session_store.TAU_DIR", tau_dir)
    return tmp_path


async def test_fork_session_spawns_a_delegate(fake_home) -> None:
    live = Session.create(
        str(fake_home), "gpt-4o", "openai", base_dir=fake_home / ".tau" / "sessions"
    )
    live.append_message({"role": "user", "content": "hello"})
    live.append_message({"role": "assistant", "content": "hi"})
    agent = AgentSession(
        session_log=live,
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    ctx = agent._extension_api.context
    ctx._cwd = str(fake_home)

    result = await surgeon._fork_session_execute(
        "call-fork",
        {"delegate_task": "audit the forked branch", "delegate_tools": ["read"]},
        None,
        None,
        ctx,
    )

    assert result.get("is_error") is not True
    details = result["details"]
    # The fork produced a real new file distinct from the source …
    forked_path = details["forked_path"]
    assert Path(forked_path).exists()
    assert forked_path != str(live.path)
    # … and the spawned delegate ran to completion against the fake provider.
    assert details["delegate"] is not None
    assert details["delegate"]["mode"] == "single"
    child = details["delegate"]["results"][0]
    assert child["exit_code"] == 0
    assert child["stop_reason"] == "stop"
    assert "Delegated result: done." in result["content"][0]["text"]


# ── composition with demo 22: the gatekeeper veto still fences ────────────────


def test_surgeon_registers_the_three_tools() -> None:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    active = session._registry.get_active_tools()
    assert set(active) == {"compact_now", "summarize_history", "fork_session"}
    # The demo-22 veto is re-exported for the E2-safety composition.
    assert surgeon.context_surgeon_gatekeeper is not None


async def test_gatekeeper_veto_composes_with_surgeon_tools(tmp_path, monkeypatch) -> None:
    """The surgeon tools and the demo-22 veto load together; an out-of-scope write
    is still blocked (E2 safety composes), while the surgeon tools stay registered.
    """
    (tmp_path / ".tau").mkdir()
    (tmp_path / ".tau" / "scope.txt").write_text("src/\n")
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[surgeon.context_surgeon_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    # Wire the demo-22 veto through the PUBLIC api.on surface (S24): a small
    # extension factory calls ``api.on("tool_call", …)`` on a bucket-bound api, so
    # the veto reaches the runner via the real api.on → ExtensionRunner bridge —
    # the same path a session uses to load an extension.
    def _veto_extension(api: object) -> None:
        api.on("tool_call", surgeon.context_surgeon_gatekeeper)  # type: ignore[attr-defined]

    _veto_extension(session._bind_extension_api("examples/22_gatekeeper.py"))
    # The surgeon tools remain registered.
    assert set(session._registry.get_active_tools()) == {
        "compact_now",
        "summarize_history",
        "fork_session",
    }

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": "/etc/passwd", "content": "x"}),
    ):
        messages = await session.prompt("write outside the sandbox")

    assert _tool_result_is_error(messages, "write")
    assert "outside the allowed scope" in _tool_result_text(messages, "write")
