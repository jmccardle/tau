"""Smoke test for ``examples/33_claude_rules.py`` — rules dir -> prompt (S60).

Proves, through real ``session_start`` + ``before_agent_start`` dispatch and the
FULL ``AgentSession.prompt`` path (only the network boundary faked):

* the pure ``find_markdown_files`` scanner (sorted, recursive, absent-dir-safe);
* ``session_start`` scans ``.claude/rules/`` under ``ctx.cwd`` and notifies once
  when rules exist;
* with rules present, the system prompt reaching the model (the injected
  ``role: "system"`` message, matching ``test_32_pirate.py``'s technique) lists
  every ``.md`` file found;
* with NO rules directory, the base system prompt is unchanged (no addendum,
  no notify) — the absent-folder case is silent, not an error.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "33_claude_rules.py"
_spec = importlib.util.spec_from_file_location("claude_rules_33_example", _PATH)
assert _spec is not None and _spec.loader is not None
claude_rules = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = claude_rules
_spec.loader.exec_module(claude_rules)


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


def _fake_text_reply(captured: list[dict[str, Any]]):
    async def fake(model, context, options=None):
        if isinstance(context, dict):
            captured.append(context)
        final = _text_assistant("ok")
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


def _system_prompt_text(context: dict[str, Any]) -> str:
    for m in context.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content") or "")
    return ""


def _session_with_rules() -> AgentSession:
    # ExtensionContext.cwd defaults to "." (AgentSession does not thread a cwd
    # param), so the run cwd IS the process cwd here — the same convention
    # ``test_gatekeeper.py``'s ``project`` fixture uses (``monkeypatch.chdir``);
    # callers of this helper chdir into a fixture directory first.
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    claude_rules.claude_rules_extension(session._bind_extension_api("examples/33_claude_rules.py"))
    return session


# ── pure scanner ──────────────────────────────────────────────────────────────


def test_find_markdown_files_recursive_and_sorted(tmp_path) -> None:
    (tmp_path / "b.md").write_text("# b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text("# a")
    (tmp_path / "notes.txt").write_text("not markdown")

    found = claude_rules.find_markdown_files(str(tmp_path))

    assert found == ["b.md", str(Path("sub") / "a.md")]


def test_find_markdown_files_missing_dir_is_empty(tmp_path) -> None:
    assert claude_rules.find_markdown_files(str(tmp_path / "does-not-exist")) == []


# ── session_start scan + notify ──────────────────────────────────────────────


async def test_session_start_scans_and_notifies_when_rules_exist(tmp_path, monkeypatch) -> None:
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "testing.md").write_text("# testing rules")
    monkeypatch.chdir(tmp_path)

    session = _session_with_rules()
    calls: list[tuple[str, str]] = []

    class _Delegate:
        def notify(self, message: str, level: str = "info") -> None:
            calls.append((message, level))

    session.set_ui_delegate(_Delegate())
    await session.emit_session_start()

    assert calls == [("Found 1 rule(s) in .claude/rules/", "info")]


async def test_session_start_silent_when_no_rules_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    session = _session_with_rules()
    calls: list[tuple[str, str]] = []

    class _Delegate:
        def notify(self, message: str, level: str = "info") -> None:
            calls.append((message, level))

    session.set_ui_delegate(_Delegate())
    await session.emit_session_start()

    assert calls == []


# ── before_agent_start folds the scanned list into the system prompt ────────


async def test_rules_are_folded_into_the_system_prompt_the_model_sees(
    tmp_path, monkeypatch
) -> None:
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "testing.md").write_text("# testing rules")
    (rules_dir / "api-design.md").write_text("# api design rules")
    monkeypatch.chdir(tmp_path)

    session = _session_with_rules()
    await session.emit_session_start()

    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("what rules apply?")

    system_text = _system_prompt_text(captured[0])
    assert "You are a helpful assistant." in system_text
    assert ".claude/rules/api-design.md" in system_text
    assert ".claude/rules/testing.md" in system_text


async def test_no_rules_dir_leaves_the_base_prompt_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    session = _session_with_rules()
    await session.emit_session_start()

    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("what rules apply?")

    assert _system_prompt_text(captured[0]) == "You are a helpful assistant."
