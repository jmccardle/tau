"""Tests for ``examples/43_budget_ledger.py`` — metered ceiling + report (S65).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S65. Upgrade of ``examples/24_budget.py``
onto ``ext_kit.ledger`` end to end + an S46 ``/ledger`` report + a two-line
(warn/stop) ceiling instead of ``24``'s one-shot trip.

Three layers, mirroring ``test_budget.py``:

* **full-loop** — only ``stream_simple`` is faked; the fake emits real tool calls
  whose ``message_end`` carries real token usage, so ``message_end`` accumulation →
  the mutating ``turn_end`` warn/stop append → ``ctx.abort()`` chain runs through
  the genuine loop. Proves: the warn crossing appends a durable ``customMessage``
  BEFORE the next turn (so the model actually sees it, unlike a same-turn
  ``tool_result`` edit that never rides another wire), the stop crossing aborts,
  and both nodes survive a reload (rebuilt from the persisted entries alone).
* **command** — ``/ledger`` (S46) reports the live run's state + the cross-session
  ``CostLedger`` roll-up via ``run_extension_command``.
* **pure-unit** — Fail-Early threshold validation, the warn/stop message builders,
  and the ``CostLedger`` cross-session RELOAD-INVARIANCE (a fresh ``CostLedger``
  instance over the same file reports the same records — the S57 persistence this
  demo's ``/ledger`` depends on surviving a process restart).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOD_PATH = _REPO_ROOT / "examples" / "43_budget_ledger.py"
_spec = importlib.util.spec_from_file_location("budget_ledger_example", _MOD_PATH)
assert _spec is not None and _spec.loader is not None
demo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = demo
_spec.loader.exec_module(demo)


# ── loop harness (a faked network boundary; everything else is real) ──────────


def _tool_call_assistant(call_id: str, usage: Usage) -> AssistantMessage:
    """An assistant message with a single ``write`` tool call and real usage."""
    return AssistantMessage(
        content=[
            ToolCall(
                type="toolCall",
                id=call_id,
                name="write",
                arguments={"path": "f.py", "content": "x"},
            )
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=usage,
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


def _reloaded_transcript(session: AgentSession) -> list[Any]:
    """Rebuild the tree from the persisted entries alone, as a reload from disk would."""
    log = session._session_log
    return ConversationTree(log.entries(), log.cursor).context_for()


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
    # No tools registered: each `write` call yields an error tool result, so the
    # loop keeps taking turns until the guard aborts (or max_turns, which a
    # working abort must beat). Compaction disabled: the fake reports large
    # per-completion usage on purpose, which would otherwise trip unrelated
    # auto-compaction machinery this test isn't exercising.
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        extensions=[],
        compaction_settings=CompactionSettings(enabled=False),
    )


def _wire_guard(session: AgentSession, guard: Any) -> None:
    """Register the guard's handlers through the PUBLIC api.on surface (S24)."""
    api = session._bind_extension_api("examples/43_budget_ledger.py")
    api.on("message_end", guard.on_message_end)  # notify event → EventBus
    api.on("turn_end", guard.on_turn_end)  # mutating hook → runner bucket


def _run_until_abort_fake(wire_payloads: list[list[Any]], per_completion: Usage):
    """A fake ``stream_simple`` that never stops on its own — only an abort ends it."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        final = _tool_call_assistant(f"call_{len(wire_payloads)}", per_completion)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


# ── full-loop: warn then stop across two turns ────────────────────────────────


async def test_token_mode_warns_then_stops_across_turns(tmp_path) -> None:
    """200k tokens/turn, warn at 150k (ratio .5 of 300k), stop at 300k.

    Turn 0's completion lands 200k >= warn(150k) but < stop(300k): the mutating
    ``turn_end`` appends a durable WARN node before turn 1 (so the model sees it
    on turn 1's wire — the whole point of using ``turn_end`` instead of a
    same-turn ``tool_result`` edit). Turn 1's completion pushes the running total
    to 400k >= stop(300k): ``turn_end`` appends the STOP node and calls
    ``ctx.abort()``, so the loop halts before turn 2.
    """
    wire_payloads: list[list[Any]] = []
    usage = Usage(input_tokens=100_000, output_tokens=100_000)  # 200k tokens/turn

    session = _make_session()
    cost_ledger = demo.ledger.CostLedger("test-run", base_dir=tmp_path)
    guard = demo.LedgerGuard(max_tokens=300_000, warn_ratio=0.5, cost_ledger=cost_ledger)
    _wire_guard(session, guard)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_run_until_abort_fake(wire_payloads, usage),
    ):
        await session.prompt("do a lot of work")

    # Exactly two LLM calls: the abort stopped the loop after turn 1, not max_turns.
    assert len(wire_payloads) == 2
    # Turn 0's wire carried no warning yet (it fires after that turn's completion).
    assert "Budget warning" not in _message_text_blob(wire_payloads[0])
    # Turn 1's wire DOES carry the warning — the durable turn_end append reached
    # the very next LLM call, unlike a tool_result edit which never rides another wire.
    assert "Budget warning" in _message_text_blob(wire_payloads[1])
    assert "Budget exceeded" not in _message_text_blob(wire_payloads[1])

    # Both durable nodes are on the persisted active path.
    blob = _message_text_blob(session.messages)
    assert "Budget warning" in blob
    assert "Budget exceeded" in blob

    # Survives a reload: rebuilt from the persisted entries alone (no in-memory
    # session state), both nodes are still real customMessage nodes on the path.
    reloaded = _reloaded_transcript(session)
    reloaded_blob = _message_text_blob(reloaded)
    assert "Budget warning" in reloaded_blob
    assert "Budget exceeded" in reloaded_blob
    custom_roles = [m for m in reloaded if isinstance(m, dict) and m.get("role") == "custom"]
    assert any(m.get("customType") == "budget_warning" for m in custom_roles)
    assert any(m.get("customType") == "budget_stop" for m in custom_roles)

    assert session._abort_signal.is_aborted() is True

    # Both crossings landed in the cross-session CostLedger.
    records = cost_ledger.records()
    outcomes = sorted(r["outcome"] for r in records)
    assert outcomes == ["stop", "warn"]


async def test_usd_mode_warns_then_stops(tmp_path) -> None:
    """Same shape as the token-mode test, priced (USD mode)."""
    wire_payloads: list[list[Any]] = []
    usage = Usage(input_tokens=100_000, output_tokens=100_000)  # $1.80/turn

    session = _make_session()
    cost_ledger = demo.ledger.CostLedger("test-run-usd", base_dir=tmp_path)
    guard = demo.LedgerGuard(
        cost={"input": 3.0, "output": 15.0},
        max_usd=3.0,
        warn_ratio=0.5,
        cost_ledger=cost_ledger,
    )
    _wire_guard(session, guard)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_run_until_abort_fake(wire_payloads, usage),
    ):
        await session.prompt("do a lot of expensive work")

    assert len(wire_payloads) == 2
    assert "Budget warning" in _message_text_blob(wire_payloads[1])
    assert "Budget exceeded" in _message_text_blob(session.messages)
    assert guard.mode == "usd"

    records = cost_ledger.records()
    assert sorted(r["outcome"] for r in records) == ["stop", "warn"]
    assert all(r["usd"] is not None for r in records)


# ── /ledger command (S46) ─────────────────────────────────────────────────────


async def test_ledger_command_reports_live_and_all_time(tmp_path) -> None:
    session = _make_session()
    cfg = {"43_budget_ledger": {"max_tokens": 300_000, "warn_ratio": 0.5, "ledger_name": "cli-run"}}
    result = await session.load_extensions(
        [str(_MOD_PATH)],
        discover=False,
        extensions_config=cfg,
    )
    assert result.extensions and not result.errors

    # No events yet: live totals are zero, no all-time history.
    report = await session.run_extension_command("ledger", "")
    assert "This run: 0 tokens (ok), ceiling 300000 tokens." in report.output
    assert "All-time: no events recorded yet." in report.output


async def test_ledger_command_after_a_warn_shows_all_time_roll_up(tmp_path) -> None:
    wire_payloads: list[list[Any]] = []
    usage = Usage(input_tokens=100_000, output_tokens=100_000)

    session = _make_session()
    cost_ledger = demo.ledger.CostLedger("cli-warn", base_dir=tmp_path)
    guard = demo.LedgerGuard(max_tokens=1_000_000, warn_ratio=0.1, cost_ledger=cost_ledger)
    _wire_guard(session, guard)
    api = session._bind_extension_api("examples/43_budget_ledger.py")

    async def ledger_command(args: str, ctx: Any) -> str:
        return demo._ledger_report(guard, cost_ledger)

    api.register_command("ledger", {"description": "report", "handler": ledger_command})

    async def one_shot_fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        if len(wire_payloads) >= 2:
            # Stop the run after the warn has been recorded, without the demo's
            # own hard-stop firing (limit is 1M, one turn is 200k).
            final = AssistantMessage(
                content=[{"type": "text", "text": "done"}],
                api="openai-completions",
                provider="openai",
                model="gpt-4o",
                stop_reason="stop",
                timestamp=0,
                usage=Usage(),
            )
            return _Stream([DoneEvent(final=final, usage=Usage())])
        final = _tool_call_assistant("call_1", usage)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=one_shot_fake):
        await session.prompt("go")

    result = await session.run_extension_command("ledger", "")
    assert "warn" in result.output
    assert "1 event(s)" in result.output


# ── pure-unit: Fail-Early threshold validation ───────────────────────────────


def test_cost_block_requires_max_usd(tmp_path) -> None:
    cost_ledger = demo.ledger.CostLedger("t", base_dir=tmp_path)
    with pytest.raises(ValueError, match="requires max_usd"):
        demo.LedgerGuard(cost={"input": 1.0}, cost_ledger=cost_ledger)


def test_cost_block_rejects_max_tokens(tmp_path) -> None:
    cost_ledger = demo.ledger.CostLedger("t", base_dir=tmp_path)
    with pytest.raises(ValueError, match="pass max_usd, not max_tokens"):
        demo.LedgerGuard(cost={"input": 1.0}, max_usd=1.0, max_tokens=100, cost_ledger=cost_ledger)


def test_no_cost_block_requires_max_tokens(tmp_path) -> None:
    cost_ledger = demo.ledger.CostLedger("t", base_dir=tmp_path)
    with pytest.raises(ValueError, match="max_tokens .* is required"):
        demo.LedgerGuard(cost_ledger=cost_ledger)


def test_max_usd_requires_cost_block(tmp_path) -> None:
    cost_ledger = demo.ledger.CostLedger("t", base_dir=tmp_path)
    with pytest.raises(ValueError, match="max_usd needs a cost block"):
        demo.LedgerGuard(max_usd=1.0, cost_ledger=cost_ledger)


# ── pure-unit: warn/stop message builders ────────────────────────────────────


def test_warn_message_is_a_valid_turn_end_return() -> None:
    result = demo.warn_message(mode="tokens", value=150_000, limit=300_000)
    message = result["message"]
    assert message["customType"] == "budget_warning"
    assert "150000 tokens used" in message["content"][0]["text"]
    assert "approaching the ceiling" in message["content"][0]["text"]


def test_stop_message_is_a_valid_turn_end_return() -> None:
    result = demo.stop_message(mode="usd", value=3.5, limit=3.0)
    message = result["message"]
    assert message["customType"] == "budget_stop"
    assert "Budget exceeded" in message["content"][0]["text"]
    assert "$3.5000 spent" in message["content"][0]["text"]


# ── pure-unit: register wires both hooks + the command ───────────────────────


def test_budget_ledger_extension_registers_hooks_and_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registered_hooks: list[str] = []
    registered_commands: list[str] = []

    class _RecordingApi:
        config: dict[str, Any] = {"max_tokens": 1000}

        def on(self, event: str, handler: Any) -> None:
            registered_hooks.append(event)

        def register_command(self, name: str, command: dict[str, Any]) -> None:
            registered_commands.append(name)

    demo.budget_ledger_extension(_RecordingApi())
    assert registered_hooks == ["message_end", "turn_end"]
    assert registered_commands == ["ledger"]


def test_default_config_is_token_mode_with_documented_ceiling(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    class _RecordingApi:
        config: dict[str, Any] = {}
        captured: dict[str, Any] = {}

        def on(self, event: str, handler: Any) -> None:
            if event == "message_end":
                self.captured["guard"] = handler.__self__

        def register_command(self, name: str, command: dict[str, Any]) -> None:
            pass

    api = _RecordingApi()
    demo.budget_ledger_extension(api)
    guard = api.captured["guard"]
    assert guard.mode == "tokens"
    assert guard._limit == demo.DEFAULT_MAX_TOKENS


# ── pure-unit: CostLedger cross-session RELOAD-INVARIANCE ───────────────────


def test_cost_ledger_survives_a_fresh_instance_over_the_same_file(tmp_path) -> None:
    """The persistence ``/ledger`` depends on: a brand-new ``CostLedger`` bound to
    the SAME name/dir (as a restarted process would construct) reports the exact
    records an earlier instance wrote — the reload-invariance the roadmap asks
    for wherever a step touches persistence."""
    first = demo.ledger.CostLedger("reload-check", base_dir=tmp_path)
    first.append(outcome="warn", tokens=150_000)
    first.append(outcome="stop", tokens=300_000)

    second = demo.ledger.CostLedger("reload-check", base_dir=tmp_path)
    records = second.records()
    assert [r["outcome"] for r in records] == ["warn", "stop"]
    assert second.total_tokens() == 450_000
    assert dict(second.by_outcome()["warn"].as_dict())["tokens"] == 150_000
