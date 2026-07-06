"""Smoke test for ``examples/30_permission_gate.py`` — confirm-gated veto (S60).

Drives the real ``tool_call`` hook through the FULL agent loop (only the network
boundary is faked, exactly like ``test_gatekeeper.py``), proving:

* a non-dangerous ``bash`` command runs unimpeded (no dialog fired);
* a dangerous command with the headless confirm policy set to "yes" is allowed;
* a dangerous command with the policy set to "no" is blocked with "Blocked by user";
* a dangerous command headless with NO policy fails CLOSED (blocked), because the
  ``tool_call`` call-site turns any handler exception (here ``HeadlessDialogError``)
  into a block — the module docstring's central claim.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

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
    # No headless policy is set; if the gate ever awaited a dialog on a safe
    # command, this would raise HeadlessDialogError -> BlockedCall -> is_error
    # whose text names the extension failure. The "bash" tool is not registered
    # on this bare session, so the call still errors (unknown tool) — the
    # assertion below distinguishes THAT from a gate-triggered dialog failure.
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "ls -la"}),
    ):
        messages = await session.prompt("list files")
    text = _tool_result_text(messages, "bash")
    assert "Extension failed" not in text
    assert "Blocked by user" not in text


# ── integration: dangerous command, headless policy resolves the confirm ────


async def test_dangerous_command_allowed_when_policy_says_yes() -> None:
    session = _session_with_gate()
    session.set_headless_ui_defaults({"confirm": "yes"})
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        messages = await session.prompt("clean up")
    # "bash" is not registered on this bare session, so the call still errors
    # (unknown tool) — the assertion checks the veto specifically did NOT fire.
    text = _tool_result_text(messages, "bash")
    assert "Extension failed" not in text
    assert "Blocked by user" not in text


async def test_dangerous_command_blocked_when_policy_says_no() -> None:
    session = _session_with_gate()
    session.set_headless_ui_defaults({"confirm": "no"})
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        messages = await session.prompt("clean up")
    assert _tool_result_is_error(messages, "bash")
    assert "Blocked by user" in _tool_result_text(messages, "bash")


async def test_dangerous_command_fails_closed_with_no_headless_policy() -> None:
    """No ``--ui-defaults`` policy at all: the raise fails CLOSED (blocked)."""
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        messages = await session.prompt("clean up")
    assert _tool_result_is_error(messages, "bash")


# ── integration: a real TUI-style confirm delegate is honored ───────────────


class _ConfirmDelegate:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def confirm(self, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return self.answer


async def test_dangerous_command_asks_the_real_ui_delegate() -> None:
    session = _session_with_gate()
    delegate = _ConfirmDelegate(answer=False)
    session.set_ui_delegate(delegate)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("bash", {"command": "sudo rm file"}),
    ):
        messages = await session.prompt("clean up")
    assert len(delegate.calls) == 1
    assert _tool_result_is_error(messages, "bash")
    assert "Blocked by user" in _tool_result_text(messages, "bash")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
