"""Smoke test for ``examples/24_budget.py`` — the running-cost guard (step S17).

Two layers, mirroring ``test_gatekeeper.py`` / ``test_reminders.py``:

* **full-loop** tests — only the network boundary (``stream_simple``) is faked, and
  the fake emits real tool calls whose ``message_end`` carries real token usage, so
  the ``message_end`` accumulation → ``tool_result`` durable-warn-then-abort chain
  runs through the genuine loop. They assert the reworked Verify clause: past the
  ceiling the guard APPENDS its ``<system-reminder>`` as a durable edit to the
  triggering ``tool_result`` node (persisted on the active path) and then aborts, so
  the loop stops at that turn instead of running to ``max_turns``. Covered in both
  USD mode (a priced ``cost`` block + ``max_usd``) and token mode (no price block +
  ``max_tokens``);
* **pure-unit** checks of the cost/token accounting, the one-shot trip, the
  Fail-Early threshold validation, and that ``make_budget_extension`` wires both
  hooks.

The ``message_end`` handler is registered on the session's notify ``EventBus`` (a
notify event, not a mutating hook); the ``tool_result`` handler is registered on the
session-owned ``ExtensionRunner`` — the wired mutating-hook dispatch surface (same
pattern as the other E2 demo tests). ``ctx.abort()`` reaches the live per-prompt
abort signal the session binds onto the ExtensionContext at the top of ``prompt()``.

Under the durable-hook invariant (E5 §3.2 / S32) the ``context`` hook is retired:
the warning is a durable node on the tripping ``tool_result``, and ``ctx.abort()``
halts the loop before the next LLM round-trip — so the tripping turn's wire payload
does NOT carry the warning (it is appended *after* that turn's completion), but the
persisted transcript does.

Reference: EXTENSIONS-IMPLEMENTATION.md §E4 (item 1), §E4.cost, §8 S17;
EXTENSIONS-E5-WIRING.md §3.3 / S32.
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
_BUDGET_PATH = _REPO_ROOT / "examples" / "24_budget.py"
_spec = importlib.util.spec_from_file_location("budget_example", _BUDGET_PATH)
assert _spec is not None and _spec.loader is not None
budget = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = budget
_spec.loader.exec_module(budget)


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


def _reloaded_warning_node(session: AgentSession, needle: str) -> dict[str, Any] | None:
    """Rebuild the tree from the persisted entries alone, as a reload from disk would.

    A *fresh* ``ConversationTree`` folded from ``session_log.entries()`` + its cursor —
    no in-memory session state — so a durable warning must be baked into the persisted
    nodes to survive. Returns the reloaded ``toolResult`` node carrying ``needle`` in its
    content (proving the warning rides a real tree node, not an ephemeral copy), or None.
    """
    log = session._session_log
    reloaded = ConversationTree(log.entries(), log.cursor).context_for()
    for message in reloaded:
        if message.get("role") != "toolResult":
            continue
        if needle in _message_text_blob([message]):
            return message
    return None


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
    # loop keeps taking turns — it only ever stops via the budget abort (or
    # max_turns, which a working abort must beat). Compaction is disabled: the
    # fake reports large per-completion usage (to cross the budget), which would
    # otherwise trip auto-compaction — an unrelated code path that needs a real
    # provider. This test isolates the budget guard.
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        extensions=[],
        compaction_settings=CompactionSettings(enabled=False),
    )


def _wire_guard(session: AgentSession, guard: Any) -> None:
    """Register the guard's handlers through the PUBLIC api.on surface (S24).

    Uses a bucket-bound ExtensionAPI (the surface a loaded extension is handed) so
    the routing itself is under test: ``message_end`` (a notify event) must reach
    the ``EventBus``, while ``tool_result`` (a mutating hook) must reach this
    extension's ``ExtensionRunner`` bucket. The guard is built externally so the
    assertions can read its running totals / tripped flag.
    """
    api = session._bind_extension_api("examples/24_budget.py")
    api.on("message_end", guard.on_message_end)  # notify event → EventBus
    api.on("tool_result", guard.on_tool_result)  # mutating hook → runner bucket


def _run_until_abort_fake(wire_payloads: list[list[Any]], per_completion: Usage):
    """A fake ``stream_simple`` that never stops on its own — only an abort ends it."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        final = _tool_call_assistant(f"call_{len(wire_payloads)}", per_completion)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


# ── the Verify clause, through the real loop (USD mode) ──────────────────────


async def test_usd_budget_warns_then_aborts_through_the_loop() -> None:
    """A priced run trips after one completion: durable warn on that result, then abort.

    Each completion costs input 100k @ $3/M + output 100k @ $15/M = $1.80; the
    ceiling is $1.00. So:

    * turn 0's LLM call sees $0.00 on the wire (no warning); its completion's
      ``message_end`` accumulates $1.80;
    * turn 0's ``tool_result`` (the unregistered-``write`` error) fires with $1.80 ≥
      $1.00 → APPENDS the durable warning to that result and calls ``ctx.abort()``;
    * the loop's per-turn abort check then breaks before turn 1.

    So the run stops after exactly ONE LLM call (the warning is appended *after* that
    call's completion, so it never rides a wire) and the warning is a DURABLE node on
    the persisted active path — the honest tree-as-truth record of why the run ended.
    """
    wire_payloads: list[list[Any]] = []
    usage = Usage(input_tokens=100_000, output_tokens=100_000)

    session = _make_session()
    guard = budget.BudgetGuard(
        cost={"input": 3.0, "output": 15.0, "cache_read": 0.3}, max_usd=1.0
    )
    _wire_guard(session, guard)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_run_until_abort_fake(wire_payloads, usage),
    ):
        await session.prompt("do a lot of expensive work")

    # The abort stopped the loop after exactly one LLM call (not max_turns).
    assert len(wire_payloads) == 1
    # That one wire never carried the warning (it is appended post-completion).
    assert "Budget exceeded" not in _message_text_blob(wire_payloads[0])
    # But the warning IS a durable node on the persisted active path.
    assert "Budget exceeded" in _message_text_blob(session.messages)
    # …and it survives a reload: a fresh tree folded from the persisted entries alone
    # still carries the warning on a real toolResult node (the tripping result), so the
    # edit is baked into the durable tree — not an in-memory-only patch.
    warn_node = _reloaded_warning_node(session, "Budget exceeded")
    assert warn_node is not None
    assert warn_node["role"] == "toolResult"
    # The guard tripped and the live abort signal is set.
    assert guard.tripped is True
    assert session._abort_signal.is_aborted() is True
    # Running spend crossed the ceiling.
    assert guard.mode == "usd"
    assert guard.running_usd >= 1.0


async def test_token_budget_aborts_through_the_loop() -> None:
    """Token mode (no price block): trips on cumulative tokens, durable-warns, aborts."""
    wire_payloads: list[list[Any]] = []
    usage = Usage(input_tokens=100_000, output_tokens=100_000)  # 200k tokens/turn

    session = _make_session()
    guard = budget.BudgetGuard(max_tokens=150_000)
    _wire_guard(session, guard)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_run_until_abort_fake(wire_payloads, usage),
    ):
        await session.prompt("do a lot of work")

    assert len(wire_payloads) == 1
    assert "Budget exceeded" not in _message_text_blob(wire_payloads[0])
    assert "Budget exceeded" in _message_text_blob(session.messages)
    # Survives a reload: rebuilt from the persisted entries, the warning is still a
    # durable block on the tripping toolResult node.
    warn_node = _reloaded_warning_node(session, "Budget exceeded")
    assert warn_node is not None
    assert warn_node["role"] == "toolResult"
    assert guard.tripped is True
    assert session._abort_signal.is_aborted() is True
    assert guard.mode == "tokens"
    assert guard.running_tokens >= 150_000


# ── pure-unit: cost / token accounting ───────────────────────────────────────


def test_completion_cost_usd_matches_calculate_cost() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 2_000_000,
        "cache_read_tokens": 500_000,
        "cache_write_tokens": 999,  # inert: no price term
    }
    cost = {"input": 3.0, "output": 15.0, "cache_read": 0.3}
    # 3 + 30 + 0.15 = 33.15
    assert budget.completion_cost_usd(usage, cost) == pytest.approx(33.15)


def test_completion_cost_usd_missing_price_key_is_unbilled() -> None:
    # No cache_read price → that bucket is simply not billed (price 0), not fabricated.
    usage = {"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    assert budget.completion_cost_usd(usage, {"input": 2.0}) == pytest.approx(2.0)


def test_completion_tokens_sums_every_bucket() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 5,
        "cache_write_tokens": 3,
    }
    assert budget.completion_tokens(usage) == 38


# ── pure-unit: accumulation + one-shot trip ──────────────────────────────────


class _RecordingCtx:
    """Minimal ExtensionContext stand-in that records ``abort()`` calls."""

    def __init__(self) -> None:
        self.aborted = 0

    def abort(self) -> None:
        self.aborted += 1


class _Event:
    """Minimal AgentEvent stand-in exposing ``.message``."""

    def __init__(self, message: Any) -> None:
        self.message = message


def _usage_message(**buckets: int) -> _Event:
    return _Event({"role": "assistant", "content": [], "usage": dict(buckets)})


def _tool_result_event(*blocks: dict[str, Any]) -> dict[str, Any]:
    """A ``tool_result`` event dict carrying ``content`` blocks (the durable node)."""
    return {
        "type": "tool_result",
        "tool_name": "write",
        "is_error": True,
        "content": list(blocks),
    }


def test_usd_guard_accumulates_and_trips_once() -> None:
    guard = budget.BudgetGuard(cost={"input": 3.0, "output": 15.0}, max_usd=1.0)
    ctx = _RecordingCtx()

    # $0.00 so far — the tool_result hook does nothing (under the ceiling).
    assert guard.on_tool_result(_tool_result_event(), ctx) is None
    assert ctx.aborted == 0

    # One completion: input 100k @ $3/M + output 100k @ $15/M = $1.80 ≥ $1.00.
    guard.on_message_end(_usage_message(input_tokens=100_000, output_tokens=100_000))
    assert guard.running_usd == pytest.approx(1.8)
    assert guard.is_over() is True

    original = {"type": "text", "text": "Unknown tool: write"}
    result = guard.on_tool_result(_tool_result_event(original), ctx)
    assert result is not None
    # The durable edit APPENDS the warning below the tool's own output.
    assert result["content"][0] == original
    assert any("Budget exceeded" in str(block) for block in result["content"])
    assert ctx.aborted == 1
    assert guard.tripped is True

    # A same-turn re-entry neither re-appends nor re-aborts.
    assert guard.on_tool_result(_tool_result_event(), ctx) is None
    assert ctx.aborted == 1


def test_token_guard_accumulates_and_trips() -> None:
    guard = budget.BudgetGuard(max_tokens=150_000)
    ctx = _RecordingCtx()

    guard.on_message_end(_usage_message(input_tokens=100_000, output_tokens=100_000))
    assert guard.running_tokens == 200_000
    assert guard.running_usd == 0.0
    assert guard.is_over() is True

    result = guard.on_tool_result(_tool_result_event(), ctx)
    assert result is not None
    assert any("Budget exceeded" in str(block) for block in result["content"])
    assert ctx.aborted == 1


def test_message_end_without_usage_contributes_nothing() -> None:
    # The duplicate tool-turn message_end run() emits has no "usage" key.
    guard = budget.BudgetGuard(max_tokens=10)
    guard.on_message_end(_Event({"role": "assistant", "content": []}))
    guard.on_message_end(_Event(None))
    assert guard.running_tokens == 0
    assert guard.is_over() is False


# ── pure-unit: Fail-Early threshold validation ───────────────────────────────


def test_cost_block_requires_max_usd() -> None:
    with pytest.raises(ValueError, match="requires max_usd"):
        budget.BudgetGuard(cost={"input": 1.0})


def test_cost_block_rejects_max_tokens() -> None:
    with pytest.raises(ValueError, match="pass max_usd, not max_tokens"):
        budget.BudgetGuard(cost={"input": 1.0}, max_usd=1.0, max_tokens=100)


def test_no_cost_block_requires_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens .* is required"):
        budget.BudgetGuard()


def test_max_usd_requires_cost_block() -> None:
    with pytest.raises(ValueError, match="max_usd needs a cost block"):
        budget.BudgetGuard(max_usd=1.0)


# ── pure-unit: the entry point wires both hooks ──────────────────────────────


def test_make_budget_extension_registers_both_hooks() -> None:
    registered: list[str] = []

    class _RecordingApi:
        def on(self, event: str, handler: Any) -> None:
            registered.append(event)

    ext = budget.make_budget_extension(max_tokens=1000)
    ext(_RecordingApi())
    assert registered == ["message_end", "tool_result"]


def test_default_budget_extension_is_token_mode() -> None:
    # The deployable default carries no cost block, so it cannot fabricate a price.
    registered: list[str] = []

    class _RecordingApi:
        def on(self, event: str, handler: Any) -> None:
            registered.append(event)

    budget.budget_extension(_RecordingApi())
    assert registered == ["message_end", "tool_result"]
