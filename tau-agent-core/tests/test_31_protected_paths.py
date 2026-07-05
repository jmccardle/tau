"""Smoke test for ``examples/31_protected_paths.py`` — pure policy gate (S60).

Drives the real ``tool_call`` hook through the FULL agent loop, proving:

* a write to a protected path is blocked (default list — no config);
* a write to a plain path is allowed;
* ``read`` calls are never governed (only ``write``/``edit``);
* the S40 ``api.config`` path list REPLACES the default when configured.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "31_protected_paths.py"
_spec = importlib.util.spec_from_file_location("protected_paths_31_example", _PATH)
assert _spec is not None and _spec.loader is not None
protected_paths = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protected_paths
_spec.loader.exec_module(protected_paths)


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


def _session_with_gate(extensions_config: dict[str, Any] | None = None) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[],
        extensions_config=extensions_config,
    )
    protected_paths.protected_paths_extension(
        session._bind_extension_api("examples/31_protected_paths.py")
    )
    return session


# ── pure decision unit tests ─────────────────────────────────────────────────


def test_env_write_is_protected() -> None:
    decision = protected_paths.protected_paths_decision(
        tool_name="write",
        tool_input={"path": ".env"},
        protected_paths=protected_paths.DEFAULT_PROTECTED_PATHS,
    )
    assert decision is not None and decision["block"] is True


def test_plain_write_is_allowed() -> None:
    decision = protected_paths.protected_paths_decision(
        tool_name="write",
        tool_input={"path": "src/app.py"},
        protected_paths=protected_paths.DEFAULT_PROTECTED_PATHS,
    )
    assert decision is None


def test_read_is_never_governed() -> None:
    decision = protected_paths.protected_paths_decision(
        tool_name="read",
        tool_input={"path": ".env"},
        protected_paths=protected_paths.DEFAULT_PROTECTED_PATHS,
    )
    assert decision is None


# ── integration: full loop, default list ─────────────────────────────────────


async def test_write_to_env_is_blocked_through_the_loop() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": ".env", "content": "SECRET=1"}),
    ):
        messages = await session.prompt("write a secret")
    assert _tool_result_is_error(messages, "write")
    assert '".env" is protected' in _tool_result_text(messages, "write")


async def test_write_to_plain_path_is_allowed_through_the_loop() -> None:
    session = _session_with_gate()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": "src/app.py", "content": "x"}),
    ):
        messages = await session.prompt("write a file")
    # Not vetoed: the write tool is not registered here, so the loop reaches
    # execution and reports an unknown-tool error, distinct from the veto text.
    assert '"src/app.py" is protected' not in _tool_result_text(messages, "write")


# ── S40 api.config REPLACES the default list ─────────────────────────────────


async def test_configured_path_list_replaces_the_default() -> None:
    session = _session_with_gate(extensions_config={"31_protected_paths": {"paths": ["secrets/"]}})
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": ".env", "content": "x"}),
    ):
        # .env is no longer protected once a config list is given.
        messages = await session.prompt("write a secret")
    assert '".env" is protected' not in _tool_result_text(messages, "write")

    session2 = _session_with_gate(extensions_config={"31_protected_paths": {"paths": ["secrets/"]}})
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": "secrets/creds.txt", "content": "x"}),
    ):
        messages2 = await session2.prompt("write a secret")
    assert _tool_result_is_error(messages2, "write")
    assert '"secrets/creds.txt" is protected' in _tool_result_text(messages2, "write")
