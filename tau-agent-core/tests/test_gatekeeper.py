"""Smoke test for ``examples/22_gatekeeper.py`` — the ``tool_call`` veto (step S15).

Drives the gatekeeper's real decision logic through the FULL agent loop: only the
network boundary (``stream_simple``) is faked, and the fake emits a real tool call
so the ``tool_call`` hook fires on a genuinely prepared call. The gatekeeper hook
is registered on the session-owned ``ExtensionRunner`` — the wired mutating-hook
dispatch surface (the ``api.on`` → runner bridge for the four E2 hooks lands in
its own step, so every E2 hook test registers on the runner directly; this
mirrors ``test_tool_call_hook.py``).

Coverage per the Verify clause:

* an **out-of-scope write** is blocked (its ``path`` is under no ``.tau/scope.txt``
  prefix) and the tool never runs;
* a **held-out read** is blocked (its ``path`` is inside ``tests_heldout/``);

plus the two allow-paths (an in-scope write and an in-scope read are permitted)
so the veto is shown to *fence*, not to block everything, and unit checks of the
pure decision + scope loader (bash held-out guard, scope-file parsing).

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-2, §8 S15.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATEKEEPER_PATH = _REPO_ROOT / "examples" / "22_gatekeeper.py"
_spec = importlib.util.spec_from_file_location("gatekeeper_example", _GATEKEEPER_PATH)
assert _spec is not None and _spec.loader is not None
gatekeeper = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gatekeeper
_spec.loader.exec_module(gatekeeper)


# ── loop harness (a faked network boundary; everything else is real) ──────────


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
    """Minimal async stream matching the stream_simple contract."""

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
    """Emit one tool call, then a text stop once a toolResult for it appears.

    A blocked call still yields a (error) toolResult for ``tool_name``, so the
    loop terminates whether the call ran or was vetoed.
    """

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model, extensions=[])


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


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project cwd with a scope file and a held-out test dir.

    ``.tau/scope.txt`` allows writes under ``src/``; ``tests_heldout/`` holds a
    secret test the agent must not read. The gatekeeper's ``ExtensionContext``
    cwd defaults to ``"."``, so the process cwd IS the run cwd here.
    """
    (tmp_path / ".tau").mkdir()
    (tmp_path / ".tau" / "scope.txt").write_text("# allowed write roots\nsrc/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests_heldout").mkdir()
    (tmp_path / "tests_heldout" / "secret_test.py").write_text("assert True\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _session_with_gatekeeper() -> AgentSession:
    # Load the demo through its PUBLIC register(api) surface (S24): the example's
    # ``gatekeeper_extension`` calls ``api.on("tool_call", …)`` on a bucket-bound
    # api, so this exercises the real api.on → ExtensionRunner bridge — the path a
    # session actually uses — not the low-level runner.register_extension seam.
    session = _make_session()
    gatekeeper.gatekeeper_extension(session._bind_extension_api("examples/22_gatekeeper.py"))
    return session


# ── the Verify clause: out-of-scope write blocked; held-out read blocked ──────


async def test_out_of_scope_write_is_blocked(project) -> None:
    session = _session_with_gatekeeper()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": "/etc/passwd", "content": "x"}),
    ):
        messages = await session.prompt("write outside the sandbox")

    assert _tool_result_is_error(messages, "write")
    assert "outside the allowed scope" in _tool_result_text(messages, "write")


async def test_held_out_read_is_blocked(project) -> None:
    session = _session_with_gatekeeper()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("read", {"path": "tests_heldout/secret_test.py"}),
    ):
        messages = await session.prompt("read the held-out test")

    assert _tool_result_is_error(messages, "read")
    assert "held-out test set" in _tool_result_text(messages, "read")


# ── allow-paths: the gate fences, it does not block everything ────────────────


async def test_in_scope_write_is_allowed(project) -> None:
    session = _session_with_gatekeeper()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("write", {"path": "src/new.py", "content": "x"}),
    ):
        messages = await session.prompt("write inside the sandbox")

    # Not vetoed: the write tool is not registered here, so the loop reaches
    # execution and reports the unknown tool — an error whose text is NOT a
    # gatekeeper denial (the veto let it through to execution).
    text = _tool_result_text(messages, "write")
    assert "outside the allowed scope" not in text
    assert "held-out test set" not in text


async def test_in_scope_read_is_allowed(project) -> None:
    session = _session_with_gatekeeper()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("read", {"path": "src/new.py"}),
    ):
        messages = await session.prompt("read an in-scope file")

    text = _tool_result_text(messages, "read")
    assert "outside the allowed scope" not in text
    assert "held-out test set" not in text


# ── pure-unit checks of the decision + scope loader ───────────────────────────


def test_bash_touching_heldout_is_blocked(project) -> None:
    decision = gatekeeper.gatekeeper_decision(
        tool_name="bash",
        tool_input={"command": "cat tests_heldout/secret_test.py"},
        cwd=str(project),
        scope_prefixes=gatekeeper.load_scope_prefixes(str(project)),
    )
    assert decision is not None and decision["block"] is True
    assert "held-out test set" in decision["reason"]


def test_bash_not_touching_heldout_is_allowed(project) -> None:
    decision = gatekeeper.gatekeeper_decision(
        tool_name="bash",
        tool_input={"command": "ls src/"},
        cwd=str(project),
        scope_prefixes=gatekeeper.load_scope_prefixes(str(project)),
    )
    assert decision is None


def test_scope_loader_parses_and_skips_comments(project) -> None:
    prefixes = gatekeeper.load_scope_prefixes(str(project))
    assert prefixes == [str(project / "src")]


def test_missing_scope_file_denies_all_writes(tmp_path) -> None:
    # Fail-CLOSED: with no scope file declared, every write is out of scope.
    prefixes = gatekeeper.load_scope_prefixes(str(tmp_path))
    assert prefixes == []
    decision = gatekeeper.gatekeeper_decision(
        tool_name="write",
        tool_input={"path": "src/x.py", "content": "y"},
        cwd=str(tmp_path),
        scope_prefixes=prefixes,
    )
    assert decision is not None and decision["block"] is True
    assert "outside the allowed scope" in decision["reason"]


def test_write_into_heldout_is_blocked_by_heldout_rule(project) -> None:
    # A write INTO tests_heldout/ is caught by the held-out rule (checked first),
    # even though the scope rule would also reject it.
    decision = gatekeeper.gatekeeper_decision(
        tool_name="write",
        tool_input={"path": "tests_heldout/injected.py", "content": "z"},
        cwd=str(project),
        scope_prefixes=gatekeeper.load_scope_prefixes(str(project)),
    )
    assert decision is not None and decision["block"] is True
    assert "held-out test set" in decision["reason"]
