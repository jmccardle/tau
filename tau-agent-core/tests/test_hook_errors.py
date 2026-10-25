"""A turn-ending hook's handler failure ends the turn; an observing hook's does not (#29).

docs/HOOK-ERRORS.md. Each case runs a real session with only ``stream_simple``
faked, a handler with no policy that raises, and an ``on_error`` listener, so
it shows the three things a failure must do: reach the listener, end the turn
with ``end_reason="error"``, and leave a tree in which every tool call is answered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extensions.runner import (
    OBSERVING_HOOKS,
    TURN_ENDING_HOOKS,
    ExtensionError,
    ExtensionRunner,
)
from tau_agent_core.policies import HookHandlerError

from test_handler_policies import _agent_ends, _calls, _messages, _script, _session, _text, _tool


def _boom(*_args: Any) -> None:
    raise RuntimeError("boom")


def _raising_on(hook: str):
    def gate(api: Any) -> None:
        api.on(hook, _boom)

    return gate


def _listen(session: AgentSession) -> list[ExtensionError]:
    errors: list[ExtensionError] = []
    session._extension_runner.on_error(errors.append)
    return errors


def _roles(session: AgentSession) -> list[str]:
    return [m["message"]["role"] for m in _messages(session)]


def _tool_results(session: AgentSession) -> list[dict[str, Any]]:
    return [m["message"] for m in _messages(session) if m["message"]["role"] == "toolResult"]


def _assert_every_call_answered(session: AgentSession) -> None:
    called = [
        block["id"]
        for m in _messages(session)
        if m["message"]["role"] == "assistant"
        for block in m["message"]["content"]
        if block.get("type") == "toolCall"
    ]
    assert sorted(called) == sorted(r["tool_call_id"] for r in _tool_results(session))


def test_every_hook_is_classified_exactly_once() -> None:
    every = set(ExtensionRunner.HOOK_EVENTS) | set(ExtensionRunner.LIFECYCLE_EVENTS)
    assert TURN_ENDING_HOOKS | OBSERVING_HOOKS == every
    assert not TURN_ENDING_HOOKS & OBSERVING_HOOKS


async def _run(session: AgentSession, *finals: Any) -> HookHandlerError:
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*finals)):
        with pytest.raises(HookHandlerError) as raised:
            await session.prompt("go")
    return raised.value


def _assert_failure(err: HookHandlerError, session: AgentSession, hook: str) -> None:
    name = session._extension_runner._extensions[0].name
    assert (err.extension, err.event, err.detail) == (name, hook, "RuntimeError: boom")
    assert str(err) == f"extension {name!r} {hook!r} handler raised RuntimeError: boom"
    assert isinstance(err.__cause__, RuntimeError)


async def test_input_ends_the_submission_before_anything_is_written() -> None:
    ends: list[Any] = []
    session = _session(_raising_on("input"), _agent_ends(ends))
    errors = _listen(session)
    err = await _run(session, _text("never sent"))
    _assert_failure(err, session, "input")
    assert [e.event for e in errors] == ["input"]
    assert ends == []
    assert _roles(session) == []


async def test_before_agent_start_ends_the_turn_with_end_reason_error() -> None:
    ends: list[Any] = []
    session = _session(_raising_on("before_agent_start"), _agent_ends(ends))
    errors = _listen(session)
    err = await _run(session, _text("never sent"))
    _assert_failure(err, session, "before_agent_start")
    assert [e.event for e in errors] == ["before_agent_start"]
    assert [e.end_reason for e in ends] == ["error"]
    assert "HookHandlerError: extension" in ends[0].error
    assert "'before_agent_start' handler raised RuntimeError: boom" in ends[0].error
    assert _roles(session) == []


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_tool_call_answers_every_call_and_ends_the_turn(mode: str) -> None:
    ends: list[Any] = []
    ran: list[str] = []
    session = _session(
        _raising_on("tool_call"),
        _agent_ends(ends),
        tools=[_tool("one", ran), _tool("two", ran)],
        tool_execution_mode=mode,
    )
    errors = _listen(session)
    err = await _run(session, _calls("one", "two"), _text("never sent"))
    _assert_failure(err, session, "tool_call")
    assert [e.event for e in errors] == ["tool_call"]
    assert [e.end_reason for e in ends] == ["error"]
    assert "'tool_call' handler raised RuntimeError: boom" in ends[0].error
    assert ran == []
    _assert_every_call_answered(session)
    first, second = _tool_results(session)
    assert first["is_error"] is True
    assert "Extension failed, blocking execution" in first["content"][0]["text"]
    assert second["is_error"] is True
    assert second["content"][0]["text"] == ABORTED_TOOL_RESULT


async def test_tool_result_writes_an_error_result_and_ends_the_turn() -> None:
    ends: list[Any] = []
    ran: list[str] = []
    session = _session(_raising_on("tool_result"), _agent_ends(ends), tools=[_tool("one", ran)])
    errors = _listen(session)
    err = await _run(session, _calls("one"), _text("never sent"))
    _assert_failure(err, session, "tool_result")
    assert [e.event for e in errors] == ["tool_result"]
    assert [e.end_reason for e in ends] == ["error"]
    assert ran == ["one"]
    _assert_every_call_answered(session)
    [result] = _tool_results(session)
    assert result["is_error"] is True
    assert "one ok" not in result["content"][0]["text"]


async def test_turn_end_ends_the_turn() -> None:
    ends: list[Any] = []
    ran: list[str] = []
    session = _session(_raising_on("turn_end"), _agent_ends(ends), tools=[_tool("one", ran)])
    errors = _listen(session)
    err = await _run(session, _calls("one"), _text("never sent"))
    _assert_failure(err, session, "turn_end")
    assert [e.event for e in errors] == ["turn_end"]
    assert [e.end_reason for e in ends] == ["error"]
    assert ran == ["one"]
    _assert_every_call_answered(session)


async def test_user_turn_end_raises_after_the_loop_has_ended() -> None:
    ends: list[Any] = []
    session = _session(_raising_on("user_turn_end"), _agent_ends(ends))
    errors = _listen(session)
    err = await _run(session, _text("answered"))
    _assert_failure(err, session, "user_turn_end")
    assert [e.event for e in errors] == ["user_turn_end"]
    assert [e.end_reason for e in ends] == ["done"]
    assert _roles(session) == ["user", "assistant"]


@pytest.mark.parametrize("hook", ["entry_written", "turn_setting_changed"])
async def test_an_observing_hook_is_surfaced_and_the_turn_goes_on(hook: str) -> None:
    ends: list[Any] = []
    session = _session(_raising_on(hook), _agent_ends(ends))
    errors = _listen(session)
    await session.start()
    session.set_reasoning("high", source="command")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")
    assert errors and {e.event for e in errors} == {hook}
    assert [e.end_reason for e in ends] == ["done"]
    assert _roles(session) == ["user", "assistant"]
