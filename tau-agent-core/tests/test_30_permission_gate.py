"""Smoke test for ``examples/30_permission_gate.py`` — a veto that also STOPS (S60).

Drives the real ``tool_call`` hook through the FULL agent loop (only the network
boundary is faked, exactly like ``test_gatekeeper.py``), proving:

* a non-dangerous ``bash`` command runs unimpeded, and leaves no request;
* a dangerous command is blocked AND locks the session, with the ask on the tree;
* answering "allow" releases the lock and lets the same command through next time;
* the lock needs no delegate, no policy and no human present: it is a tree node,
  which is the whole difference from the ``ctx.ui.confirm`` this demo used to use
  (docs/EXTENSION-LOCKS.md §1).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60; docs/EXTENSION-LOCKS.md.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "30_permission_gate.py"
_spec = importlib.util.spec_from_file_location("permission_gate_30_example", _PATH)
assert _spec is not None and _spec.loader is not None
permission_gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = permission_gate
_spec.loader.exec_module(permission_gate)


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=_TS,
        usage=Usage(),
    )


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=_TS,
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
    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream([DoneEvent(final=final, usage=Usage())])
        final = _tool_call_assistant("call_1", tool_name, tool_args)
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


def _tool_result(messages: list[Any], tool_name: str) -> Any:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return m
    raise AssertionError(f"no toolResult for {tool_name}")


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    m = _tool_result(messages, tool_name)
    content = m["content"] if isinstance(m, dict) else m.content
    block = content[0]
    return block["text"] if isinstance(block, dict) else block.text


def _tool_result_is_error(messages: list[Any], tool_name: str) -> bool:
    m = _tool_result(messages, tool_name)
    return bool(m["is_error"] if isinstance(m, dict) else getattr(m, "is_error", False))


def _session_with_gate() -> AgentSession:
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[])
    permission_gate.permission_gate_extension(
        session._bind_extension_api("examples/30_permission_gate.py")
    )
    return session


# ── pure decision unit tests ─────────────────────────────────────────────────


def test_dangerous_patterns_match() -> None:
    assert permission_gate.is_dangerous("rm -rf /tmp/x")
    assert permission_gate.is_dangerous("sudo apt-get update")
    assert permission_gate.is_dangerous("chmod 777 /etc/passwd")


def test_safe_commands_do_not_match() -> None:
    assert not permission_gate.is_dangerous("ls -la")
    assert not permission_gate.is_dangerous("git status")


# ── integration: a safe command never triggers a dialog at all ──────────────


async def test_safe_command_runs_without_any_dialog() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "ls -la"}),
    ):
        messages = await session.prompt("list files")
    text = _tool_result_text(messages, "bash")
    assert "Extension failed" not in text
    assert "Blocked by user" not in text


# ── integration: dangerous command → blocked call AND a locked request ──────


async def test_dangerous_command_is_blocked_and_locks_the_session() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        messages = await session.prompt("clean up")

    assert _tool_result_is_error(messages, "bash")
    assert "Blocked pending human approval" in _tool_result_text(messages, "bash")

    request = session.pending_request
    assert request is not None
    assert request.lock is True
    assert "sudo rm file" in request.sentence
    assert [a["command"] for a in request.ask["actions"]] == ["gate-allow", "gate-deny"]


async def test_a_safe_command_leaves_no_request() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "ls -la"}),
    ):
        await session.prompt("list files")
    assert session.pending_request is None


async def test_the_lock_refuses_the_next_prompt() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        await session.prompt("clean up")

    result = await session.submit(
        Submission(
            text="carry on",
            source="interactive",
            submitter="human",
            submission_id="after-lock",
        )
    )
    assert result.accepted is False
    assert result.lock is not None


async def test_allowing_releases_the_lock_and_lets_the_command_through() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        await session.prompt("clean up")

    outcome = await session.answer_request(
        session.pending_request.entry_id, "Allow this command", {}
    )
    assert "Allowed: sudo rm file" in str(outcome.output)
    assert session.pending_request is None

    # The veto itself is what changed: the same command no longer matches.
    verdict = await session._extension_runner.emit_tool_call(
        {"tool_name": "bash", "input": {"command": "sudo rm file"}}
    )
    assert verdict is None or not verdict.get("block")


async def test_denying_leaves_it_blocked_but_still_unlocks() -> None:
    """The lock is released by the ANSWER, not by which answer it was (§3)."""
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        await session.prompt("clean up")

    outcome = await session.answer_request(session.pending_request.entry_id, "Deny", {})
    assert "Denied" in str(outcome.output)
    assert session.pending_request is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
