"""Smoke test for ``examples/21_reminders.py`` — the four-rule reminder bank (S16).

Two layers, mirroring ``test_gatekeeper.py``:

* a **full-loop** test — only the network boundary (``stream_simple``) is faked, and
  the fake emits real ``write`` tool calls so the ``tool_call`` → ``tool_result``
  hook chain runs through the genuine loop. It asserts that ``tests-readonly`` edits
  its ``<system-reminder>`` into the triggering ``tool_result`` (which the follow-up
  wire payload then carries), goes silent on the next result (cooldown), and that
  ``root-cause-after-2-failures`` fires once the same tool has errored twice;
* **pure-unit** checks of :class:`ReminderBank` — each of the four rules fires once
  then cools down for exactly its ``COOLDOWNS`` window (3 / 4 / 2 / 1), the rules
  trip off the right ``event["input"]`` field, and ``reminders_extension`` wires
  both hooks.

The bank's handlers are registered on the session-owned ``ExtensionRunner`` — the
wired mutating-hook dispatch surface (same pattern as ``test_gatekeeper.py``). The
retired ``context`` hook is gone (E5 §3.2 / S31): the reminder is a DURABLE edit to
the triggering ``tool_result`` node, not an ephemeral per-call message injection.

The S31 **proving test**
(``test_both_reminder_channels_are_durable_in_tree_transcript_and_reload``) drives one
real prompt and asserts BOTH durable channels — the ``before_agent_start`` discipline
preamble (a persisted ``customMessage`` node, S29) and the ``tool_result`` edit —
agree across three surfaces that must be identical under the durable-hook invariant:
the emitted transcript (the wire payload), the persisted tree (the ``session_log``
entries), and a reload (a fresh ``ConversationTree`` fold over those entries). That
rules out an ephemeral injection that would show on the wire but be absent from the
tree / a reload.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-2, §8 S16; EXTENSIONS-E5-WIRING.md
§3.3 / S31.
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
from tau_agent_core.conversation_tree import ConversationTree
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
    tool result, since ``write`` is unregistered), then stops with text. Each write
    is unknown but still runs through ``_apply_after_hooks`` (an unregistered tool
    yields an error *result*, not a prepare-time error), so the ``tool_result`` hook
    fires each turn and edits that result's content.

    Under the durable-hook invariant the edit is PERMANENT: once ``tests-readonly``
    is appended to call 1's ``tool_result`` it stays on the active path, so every
    later wire payload carries it (this is the honesty property — no ephemeral
    injection that vanishes). "Fires once" therefore means the reminder is *appended
    exactly once* (to call 1's result), NOT that it disappears from later payloads.
    Across the three resulting LLM calls:

    * call 1 payload has NO reminder (nothing has been edited yet);
    * call 2 payload carries the ``tests-readonly`` reminder — durably appended to
      call 1's ``tool_result`` (tripped by call 1's write) — appearing exactly once;
    * call 3 payload still carries that one ``tests-readonly`` reminder (it is durable
      on call 1's result) but the rule did NOT fire again on call 2's result (it was
      cooling down), so the count stays 1; call 2's result instead carries the
      ``root-cause-after-2-failures`` reminder (two write errors).
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
    # ``reminders_extension`` wires both handlers via ``api.on(…)`` on a
    # bucket-bound api, so this drives the real api.on → ExtensionRunner bridge.
    reminders.reminders_extension(session._bind_extension_api("examples/21_reminders.py"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("edit the test until it passes")

    assert len(wire_payloads) == 3
    blobs = [_message_text_blob(p) for p in wire_payloads]

    tests_ro = reminders.REMINDER_TEXT["tests-readonly"]
    root_cause = reminders.REMINDER_TEXT["root-cause-after-2-failures"]

    # call 1: nothing edited yet.
    assert tests_ro not in blobs[0]
    # call 2: tests-readonly is now durable on call 1's result — appended once.
    assert blobs[1].count(tests_ro) == 1
    # call 3: the reminder is DURABLE so it is still present (count stays 1); it was
    # NOT re-appended to call 2's result (cooling down). root-cause now fires there.
    assert blobs[2].count(tests_ro) == 1
    assert root_cause in blobs[2]
    # and root-cause did not appear before the second failure.
    assert root_cause not in blobs[0]
    assert root_cause not in blobs[1]


# ── the S31 proving test: both channels are durable in tree + transcript + reload ─


def _entry_message_text(entry: dict[str, Any]) -> str:
    """Concatenated text of an entry's stored ``message`` content blocks."""
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


async def test_both_reminder_channels_are_durable_in_tree_transcript_and_reload() -> None:
    """S31 Verify: the reminders are DURABLE nodes, not ephemeral injections.

    Drive one prompt that exercises both channels: the ``before_agent_start`` hook
    seeds the discipline preamble before the first LLM call, and a ``write`` to a test
    file trips ``tests-readonly`` so the ``tool_result`` hook edits the *triggering*
    result in place. The fake emits one ``write`` (an error, since ``write`` is
    unregistered) then stops, so there are exactly two LLM calls: call 1 sees only the
    preamble; call 2 additionally carries the ``tests-readonly`` edit on call 1's
    ``tool_result``.

    Each reminder must then be present on THREE surfaces that the durable-hook
    invariant forces to agree:

    * the emitted TRANSCRIPT — the wire payload the model actually received;
    * the persisted TREE — the ``session_log`` entries (the on-disk nodes): the
      preamble is a ``customMessage`` entry, the reminder is content on a
      ``toolResult`` message entry;
    * a RELOAD — a fresh ``ConversationTree`` folded over those persisted entries,
      whose ``context_for`` rebuilds the model context from the durable path alone.

    An ephemeral injection would appear on the wire but be absent from the tree and a
    reload — so all three agreeing is the honesty/reload-fidelity proof, not a tautology.
    """
    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        if _count_tool_results(messages, "write") >= 1:
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
    reminders.reminders_extension(session._bind_extension_api("examples/21_reminders.py"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("edit the test until it passes")

    preamble = reminders.PREAMBLE_TEXT
    tests_ro = reminders.REMINDER_TEXT["tests-readonly"]

    # ── surface 1: the TRANSCRIPT (the last wire payload carries the full path) ──
    assert len(wire_payloads) == 2
    wire_blob = _message_text_blob(wire_payloads[-1])
    assert preamble in wire_blob
    assert tests_ro in wire_blob
    # The preamble rode the FIRST call (pre-first-call), before any tool_result.
    assert preamble in _message_text_blob(wire_payloads[0])

    # ── surface 2: the TREE (the persisted session_log entries) ──
    entries = session._session_log.entries()

    # the preamble is a durable customMessage node with the reminder-bank origin type.
    custom_entries = [e for e in entries if e.get("type") == "customMessage"]
    assert len(custom_entries) == 1
    assert custom_entries[0]["customType"] == reminders.PREAMBLE_CUSTOM_TYPE
    assert preamble in _entry_message_text(custom_entries[0])

    # the tests-readonly reminder is durable content on a toolResult message node.
    tool_result_entries = [
        e
        for e in entries
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "toolResult"
    ]
    assert tool_result_entries, "expected a persisted toolResult node"
    assert any(tests_ro in _entry_message_text(e) for e in tool_result_entries)

    # ── surface 3: a RELOAD (fold a fresh tree over the persisted entries) ──
    reloaded = ConversationTree(session._session_log.entries(), session._session_log.cursor)
    reload_blob = _message_text_blob(reloaded.context_for())
    assert preamble in reload_blob
    assert tests_ro in reload_blob


# ── pure-unit: the before_agent_start preamble seeds once, then falls silent ──


class _CtxCwd:
    """A minimal ExtensionContext stand-in (the preamble handler ignores ctx)."""

    cwd = "/project"


def test_before_agent_start_seeds_the_preamble_exactly_once() -> None:
    bank = reminders.ReminderBank()
    first = bank.on_before_agent_start({"type": "before_agent_start"}, _CtxCwd())
    assert first is not None
    message = first["message"]
    assert message["customType"] == reminders.PREAMBLE_CUSTOM_TYPE
    assert reminders.PREAMBLE_TEXT in message["content"]
    assert message["content"].startswith("<system-reminder>")
    # Pre-first-call means ONCE: a second before_agent_start injects nothing.
    assert bank.on_before_agent_start({"type": "before_agent_start"}, _CtxCwd()) is None


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


def _err_result(tool_name: str = "bash") -> dict[str, Any]:
    """A ``tool_result`` event dict for a failed tool with a content block."""
    return {
        "type": "tool_result",
        "tool_name": tool_name,
        "is_error": True,
        "content": [{"type": "text", "text": f"{tool_name} failed"}],
    }


def _reminder_text_in(patch: dict[str, Any] | None, rule: str) -> bool:
    """True if a ``tool_result`` patch appended ``rule``'s ``<system-reminder>``."""
    if patch is None:
        return False
    return any(reminders.REMINDER_TEXT[rule] in str(block) for block in patch["content"])


def test_two_same_tool_errors_trip_root_cause() -> None:
    # on_tool_result now BOTH counts failures AND drains into the durable edit.
    bank = reminders.ReminderBank()
    # One failure is not enough — nothing drains, so the result passes through.
    assert bank.on_tool_result(_err_result(), _Ctx()) is None
    # The second consecutive failure trips root-cause, which drains on this same
    # result: the returned patch appends the reminder to the result's content.
    patch = bank.on_tool_result(_err_result(), _Ctx())
    assert _reminder_text_in(patch, "root-cause-after-2-failures")


def test_success_resets_the_failure_streak() -> None:
    bank = reminders.ReminderBank()
    ok = {
        "type": "tool_result",
        "tool_name": "bash",
        "is_error": False,
        "content": [{"type": "text", "text": "ok"}],
    }
    assert bank.on_tool_result(_err_result(), _Ctx()) is None  # streak 1
    assert bank.on_tool_result(ok, _Ctx()) is None  # streak reset
    # Only one error since the reset → root-cause does not fire.
    assert bank.on_tool_result(_err_result(), _Ctx()) is None


def test_reminder_is_appended_below_the_tool_output() -> None:
    # The durable edit APPENDS — the tool's own output block survives beneath the nag.
    bank = reminders.ReminderBank()
    bank.on_tool_call(
        {"type": "tool_call", "tool_name": "write", "input": {"path": "tests/test_a.py"}},
        _Ctx(),
    )
    original = {"type": "text", "text": "Unknown tool: write"}
    patch = bank.on_tool_result(
        {"type": "tool_result", "tool_name": "write", "is_error": True, "content": [original]},
        _Ctx(),
    )
    assert patch is not None
    assert patch["content"][0] == original  # original output preserved
    assert _reminder_text_in(patch, "tests-readonly")  # reminder appended after it


def test_tool_call_never_vetoes() -> None:
    # tool_call observes; it must never return a block/patch, even when it triggers.
    bank = reminders.ReminderBank()
    assert (
        bank.on_tool_call(
            {"type": "tool_call", "tool_name": "write", "input": {"path": "tests/test_a.py"}},
            _Ctx(),
        )
        is None
    )


def test_a_quiet_result_is_untouched() -> None:
    # A fresh bank: a single error (streak 1) trips no rule and nothing is pending,
    # so the tool_result passes through unpatched.
    bank = reminders.ReminderBank()
    assert bank.on_tool_result(_err_result(), _Ctx()) is None


# ── pure-unit: the extension entry point wires both hooks ────────────────────


def test_extension_registers_all_three_hooks() -> None:
    registered: list[str] = []

    class _RecordingApi:
        def on(self, event: str, handler: Any) -> None:
            registered.append(event)

    reminders.reminders_extension(_RecordingApi())
    assert registered == ["before_agent_start", "tool_call", "tool_result"]


# ── S59: the demo now consumes ext_kit.steer (the refactor's regression guard) ─


def test_reminder_bank_wraps_steer_reminder_bank() -> None:
    # The threshold/cooldown/drain state machine is now an ext_kit.steer.ReminderBank
    # holding the four rules as data (each with its COOLDOWNS cooldown + REMINDER_TEXT).
    bank = reminders.ReminderBank()
    assert isinstance(bank._bank, reminders.steer.ReminderBank)
    for rule in reminders.RULE_ORDER:
        # is_pending resolves the rule (raising on an unknown one), so a clean
        # False proves every demo rule is registered in the kit bank.
        assert bank._bank.is_pending(rule) is False


def test_drain_and_patch_delegate_to_the_kit_bank() -> None:
    # _drain delegates to the kit bank's drain, and on_tool_result to patch_result:
    # a triggered rule drains in RULE_ORDER and its <system-reminder> is appended.
    bank = reminders.ReminderBank()
    bank.trigger("scope-guard")
    assert bank._drain() == ["scope-guard"]  # kit drain, registration order
    bank.trigger("tests-readonly")
    patch = bank.on_tool_result(
        {"type": "tool_result", "tool_name": "read", "is_error": False, "content": []},
        _Ctx(),
    )
    assert patch is not None
    assert reminders.REMINDER_TEXT["tests-readonly"] in str(patch["content"])
