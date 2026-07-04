"""Smoke test for ``examples/21_reminders.py`` — the four-rule reminder bank (S16).

Two layers, mirroring ``test_gatekeeper.py``:

* a **full-loop** test — only the network boundary (``stream_simple``) is faked, and
  the fake emits real ``write`` tool calls so the ``tool_call`` → ``tool_result`` →
  ``context`` hook chain runs through the genuine loop. It asserts that
  ``tests-readonly`` injects its ``<system-reminder>`` on the follow-up wire payload,
  goes silent on the next call (cooldown), and that ``root-cause-after-2-failures``
  fires once the same tool has errored twice;
* **pure-unit** checks of :class:`ReminderBank` — each of the four rules fires once
  then cools down for exactly its ``COOLDOWNS`` window (3 / 4 / 2 / 1), the rules
  trip off the right ``event["input"]`` field, and ``reminders_extension`` wires all
  three hooks.

The bank's handlers are registered on the session-owned ``ExtensionRunner`` — the
wired mutating-hook dispatch surface (same pattern as ``test_context_hook.py`` /
``test_gatekeeper.py``).

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-2, §8 S16.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REMINDERS_PATH = _REPO_ROOT / "examples" / "21_reminders.py"
_spec = importlib.util.spec_from_file_location("reminders_example", _REMINDERS_PATH)
assert _spec is not None and _spec.loader is not None
reminders = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reminders
_spec.loader.exec_module(reminders)


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


def _count_tool_results(messages: list[Any], tool_name: str) -> int:
    n = 0
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            n += 1
    return n


def _message_text_blob(messages: list[Any]) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    out.append(str(block.get("text", "")))
                else:
                    out.append(str(getattr(block, "text", "")))
    return "\n".join(out)


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
    # No tools registered: `write` is unknown, so each call yields an *error*
    # tool result — exactly the signal the root-cause rule counts.
    return AgentSession(session_log=InMemorySessionLog(), model=model, extensions=[])


# ── the Verify clause, through the real loop ─────────────────────────────────


async def test_rules_fire_once_then_cool_down_through_the_loop(tmp_path, monkeypatch) -> None:
    """Drive write-to-a-test-file calls through the loop and watch the reminders.

    The fake emits ``write("tests/test_x.py")`` twice (each producing an *error*
    tool result, since ``write`` is unregistered), then stops with text. Across the
    three resulting LLM calls:

    * call 1 payload has NO reminder (nothing triggered yet);
    * call 2 payload carries the ``tests-readonly`` reminder (tripped by call 1's
      write) — it fires ONCE;
    * call 3 payload has NO ``tests-readonly`` reminder (it is cooling down) but
      carries the ``root-cause-after-2-failures`` reminder (two write errors).
    """
    monkeypatch.chdir(tmp_path)
    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        if _count_tool_results(messages, "write") >= 2:
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", "write", {"path": "tests/test_x.py", "content": "x"})
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = _make_session()
    # Load the demo through its PUBLIC register(api) surface (S24): the example's
    # ``reminders_extension`` wires all three handlers via ``api.on(…)`` on a
    # bucket-bound api, so this drives the real api.on → ExtensionRunner bridge.
    reminders.reminders_extension(session._bind_extension_api("examples/21_reminders.py"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("edit the test until it passes")

    assert len(wire_payloads) == 3
    blobs = [_message_text_blob(p) for p in wire_payloads]

    tests_ro = reminders.REMINDER_TEXT["tests-readonly"]
    root_cause = reminders.REMINDER_TEXT["root-cause-after-2-failures"]

    # call 1: nothing tripped yet.
    assert tests_ro not in blobs[0]
    # call 2: tests-readonly fires exactly once.
    assert tests_ro in blobs[1]
    # call 3: tests-readonly is cooling down (silent); root-cause now fires.
    assert tests_ro not in blobs[2]
    assert root_cause in blobs[2]
    # and root-cause did not appear before the second failure.
    assert root_cause not in blobs[0]
    assert root_cause not in blobs[1]


# ── pure-unit: each rule fires once then cools down for its window ────────────


def _fire_gap(rule: str) -> None:
    bank = reminders.ReminderBank()
    cooldown = reminders.COOLDOWNS[rule]

    bank.trigger(rule)
    assert bank._drain() == [rule]  # first fire

    # Silent for exactly `cooldown` context calls, even while re-triggered.
    for _ in range(cooldown):
        bank.trigger(rule)
        assert bank._drain() == []

    # Now off cooldown: it may fire again.
    bank.trigger(rule)
    assert bank._drain() == [rule]


def test_tests_readonly_cooldown_is_three() -> None:
    assert reminders.COOLDOWNS["tests-readonly"] == 3
    _fire_gap("tests-readonly")


def test_root_cause_cooldown_is_four() -> None:
    assert reminders.COOLDOWNS["root-cause-after-2-failures"] == 4
    _fire_gap("root-cause-after-2-failures")


def test_scope_guard_cooldown_is_two() -> None:
    assert reminders.COOLDOWNS["scope-guard"] == 2
    _fire_gap("scope-guard")


def test_no_new_deps_cooldown_is_one() -> None:
    assert reminders.COOLDOWNS["no-new-deps"] == 1
    _fire_gap("no-new-deps")


def test_idle_context_call_fires_nothing() -> None:
    bank = reminders.ReminderBank()
    assert bank._drain() == []


def test_trigger_rejects_unknown_rule() -> None:
    bank = reminders.ReminderBank()
    try:
        bank.trigger("no-such-rule")
    except ValueError as exc:
        assert "unknown reminder rule" in str(exc)
    else:  # pragma: no cover - the trigger must raise
        raise AssertionError("trigger accepted an unknown rule")


# ── pure-unit: rules trip off the right event["input"] field ─────────────────


class _Ctx:
    """A minimal ExtensionContext stand-in exposing a working-scope ``cwd``."""

    def __init__(self, cwd: str = "/project") -> None:
        self.cwd = cwd


def test_write_to_test_file_trips_tests_readonly() -> None:
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "write", "input": {"path": "tests/test_a.py"}},
        _Ctx(),
    )
    assert bank._drain() == ["tests-readonly"]


def test_write_outside_cwd_trips_scope_guard() -> None:
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "write", "input": {"path": "/etc/passwd"}},
        _Ctx(cwd="/project"),
    )
    assert bank._drain() == ["scope-guard"]


def test_write_inside_cwd_does_not_trip_scope_guard() -> None:
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "write", "input": {"path": "src/mod.py"}},
        _Ctx(cwd="/project"),
    )
    assert bank._drain() == []


def test_bash_install_trips_no_new_deps() -> None:
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "bash", "input": {"command": "pip install requests"}},
        _Ctx(),
    )
    assert bank._drain() == ["no-new-deps"]


def test_bash_uninstall_does_not_trip_no_new_deps() -> None:
    # `uninstall` has no word break before "install"; the installer regex misses it.
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "bash", "input": {"command": "pip uninstall requests"}},
        _Ctx(),
    )
    assert bank._drain() == []


def test_write_to_manifest_trips_no_new_deps() -> None:
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "edit", "input": {"path": "requirements.txt"}},
        _Ctx(cwd="/project"),
    )
    assert bank._drain() == ["no-new-deps"]


def test_two_same_tool_errors_trip_root_cause() -> None:
    bank = reminders.ReminderBank()
    err = {"type": "tool_result", "tool_name": "bash", "is_error": True}
    bank.on_tool_result(err, _Ctx())
    assert bank._drain() == []  # one failure is not enough
    bank.on_tool_result(err, _Ctx())
    assert bank._drain() == ["root-cause-after-2-failures"]


def test_success_resets_the_failure_streak() -> None:
    bank = reminders.ReminderBank()
    err = {"type": "tool_result", "tool_name": "bash", "is_error": True}
    ok = {"type": "tool_result", "tool_name": "bash", "is_error": False}
    bank.on_tool_result(err, _Ctx())
    bank.on_tool_result(ok, _Ctx())  # streak reset
    bank.on_tool_result(err, _Ctx())
    assert bank._drain() == []  # only one error since the reset


def test_hooks_never_veto_or_patch() -> None:
    # tool_call must not block; tool_result must not patch.
    bank = reminders.ReminderBank()
    assert (
        bank.on_tool_call(
            {"type": "tool_call", "tool_name": "write", "input": {"path": "tests/test_a.py"}},
            _Ctx(),
        )
        is None
    )
    assert (
        bank.on_tool_result(
            {"type": "tool_result", "tool_name": "bash", "is_error": True}, _Ctx()
        )
        is None
    )


# ── pure-unit: the extension entry point wires all three hooks ───────────────


def test_extension_registers_the_three_hooks() -> None:
    registered: list[str] = []

    class _RecordingApi:
        def on(self, event: str, handler: Any) -> None:
            registered.append(event)

    reminders.reminders_extension(_RecordingApi())
    assert registered == ["tool_call", "tool_result", "context"]
