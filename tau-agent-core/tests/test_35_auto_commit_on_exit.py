"""Smoke test for ``examples/35_auto_commit_on_exit.py`` — exit-time commit (S61).

Proves:

* the pure helpers ``last_assistant_text`` / ``build_commit_message`` against
  pi's fixture shapes (assistant text extraction, truncation, the
  "Work in progress" fallback);
* ``session_shutdown`` is a real no-op outside a git repo and inside a clean
  one (pi parity: silent no-op on both);
* end-to-end through a REAL temp git repo + a REAL ``AgentSession.prompt``
  turn: an uncommitted change present at ``session_shutdown`` gets staged and
  committed with a message built from the last assistant reply, and the UI is
  notified.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "35_auto_commit_on_exit.py"
_spec = importlib.util.spec_from_file_location("auto_commit_on_exit_35_example", _PATH)
assert _spec is not None and _spec.loader is not None
auto_commit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = auto_commit
_spec.loader.exec_module(auto_commit)


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


def _fake_text_reply(text: str) -> Any:
    async def fake(model: Any, context: Any, options: Any = None) -> _Stream:
        final = _text_assistant(text)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


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


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, capture_output=True, text=True, check=True
    )


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_last_assistant_text_finds_the_most_recent_assistant_message() -> None:
    entries = [
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
        },
        {"type": "message", "message": {"role": "user", "content": "again"}},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "second reply"}],
            },
        },
    ]
    assert auto_commit.last_assistant_text(entries) == "second reply"


def test_last_assistant_text_empty_when_no_assistant_message() -> None:
    entries = [{"type": "message", "message": {"role": "user", "content": "hi"}}]
    assert auto_commit.last_assistant_text(entries) == ""


def test_build_commit_message_truncates_and_prefixes() -> None:
    long_text = "x" * 80
    message = auto_commit.build_commit_message(long_text)
    assert message == f"[tau] {'x' * 50}..."


def test_build_commit_message_short_text_untouched() -> None:
    assert auto_commit.build_commit_message("fix the bug") == "[tau] fix the bug"


def test_build_commit_message_falls_back_when_no_assistant_text() -> None:
    assert auto_commit.build_commit_message("") == "[tau] Work in progress"


# ── session_shutdown no-op cases ─────────────────────────────────────────────


class _Ctx:
    def __init__(self, cwd: str, entries: list[dict[str, Any]]) -> None:
        self.cwd = cwd
        self._entries = entries
        self.notified: list[tuple[str, str]] = []

    def entries(self) -> list[dict[str, Any]]:
        return self._entries

    class _UI:
        def __init__(self, sink: list[tuple[str, str]]) -> None:
            self._sink = sink

        def notify(self, message: str, level: str = "info") -> None:
            self._sink.append((message, level))

    @property
    def ui(self) -> "_Ctx._UI":
        return _Ctx._UI(self.notified)


def test_session_shutdown_silent_outside_a_git_repo(tmp_path) -> None:
    ctx = _Ctx(str(tmp_path), [])
    auto_commit.on_session_shutdown({}, ctx)
    assert ctx.notified == []


def test_session_shutdown_silent_when_repo_is_clean(tmp_path) -> None:
    _init_git_repo(tmp_path)
    ctx = _Ctx(str(tmp_path), [])
    auto_commit.on_session_shutdown({}, ctx)
    assert ctx.notified == []
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().splitlines()) == 1  # only the fixture's "initial" commit


# ── end-to-end: real repo + real AgentSession.prompt turn ───────────────────


async def test_session_shutdown_commits_uncommitted_work_end_to_end(tmp_path, monkeypatch) -> None:
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    auto_commit.auto_commit_on_exit_extension(
        session._bind_extension_api("examples/35_auto_commit_on_exit.py")
    )

    notified: list[tuple[str, str]] = []

    class _Delegate:
        def notify(self, message: str, level: str = "info") -> None:
            notified.append((message, level))

    session.set_ui_delegate(_Delegate())

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_text_reply("Added the new widget module."),
    ):
        await session.prompt("add a widget")

    # Simulate the agent's own edit landing on disk mid-session.
    (tmp_path / "widget.py").write_text("class Widget: ...\n")

    await session.emit_session_shutdown()

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[tau] Added the new widget module." in log.stdout
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == ""
    assert notified == [("Auto-committed: [tau] Added the new widget module.", "info")]
