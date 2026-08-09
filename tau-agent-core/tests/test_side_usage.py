"""Tokens spent OUTSIDE the agent loop must still be counted (tau_agent_core.usage).

The bug these pin: the agent loop attaches usage to the ``message_end`` event it emits
per completion, and every meter sums those events. But compaction, branch summaries,
and ``ctx.complete()`` go through ``tau_ai.complete_simple``, which takes no event bus
and emits NOTHING. Their tokens were spent and then forgotten.

That made the cost τ displays **understated** — the direction that lets a session look
cheaper than it is. Worst of all for auto-compaction, which summarizes the entire
conversation (so its input is roughly a full context window) and fires *by itself*,
without the user asking. The most expensive automatic call in the system was the one
guaranteed to be invisible.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionPreparation, CompactionSettings
from tau_agent_core.compaction import compact as run_compaction
from tau_agent_core.compaction_utils import create_file_ops
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.usage import add_usage, usage_of, zero_usage
from tau_ai.types import AssistantMessage, Model, TextContent, Usage


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _reply(text: str, *, input_tokens: int, output_tokens: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _session() -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), system_prompt="", tools=[], api_key="k"
    )


# --- the arithmetic ---------------------------------------------------------


def test_a_message_with_no_reported_usage_reads_as_a_true_zero():
    """Fail-Early: never a `len(text) // 4` guess. A fabricated count that LOOKS real
    is worse than a zero, because it gets believed and cannot be distinguished from a
    measurement."""

    class _NoUsage:
        usage = None

    assert usage_of(_NoUsage()) == zero_usage()


def test_usage_sums_field_wise_without_mutating_the_ledger():
    ledger = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
    out = add_usage(ledger, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})
    assert out["input_tokens"] == 15 and out["total_tokens"] == 18
    assert ledger["input_tokens"] == 10, "add_usage must not mutate its first argument"


# --- compaction: the expensive, automatic, previously-invisible one ----------


async def test_compaction_reports_what_its_summarizer_spent(monkeypatch):
    async def _fake(model, context, options=None):
        return _reply("a summary", input_tokens=7000, output_tokens=300)

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake)

    prep = CompactionPreparation(
        first_kept_entry_id="e1",
        messages_to_summarize=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=7500,
        file_ops=create_file_ops(),
        settings=CompactionSettings(),
    )
    result = await run_compaction(prep, _model(), "k")

    assert result.usage["input_tokens"] == 7000, "compaction's input is ~a full context window"
    assert result.usage["total_tokens"] == 7300


async def test_a_split_turn_counts_BOTH_of_its_completions(monkeypatch):
    """A split turn summarizes the history AND the turn prefix, concurrently. Counting
    one would understate the compaction by roughly half."""
    calls: list[Any] = []

    async def _fake(model, context, options=None):
        calls.append(context)
        return _reply("s", input_tokens=1000, output_tokens=100)

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake)

    prep = CompactionPreparation(
        first_kept_entry_id="e1",
        messages_to_summarize=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        turn_prefix_messages=[{"role": "user", "content": [{"type": "text", "text": "prefix"}]}],
        is_split_turn=True,
        tokens_before=5000,
        file_ops=create_file_ops(),
        settings=CompactionSettings(),
    )
    result = await run_compaction(prep, _model(), "k")

    assert len(calls) == 2, "history + turn prefix"
    assert result.usage["total_tokens"] == 2200, "both completions, not one"


async def test_an_auto_compaction_lands_on_the_sessions_side_ledger(monkeypatch):
    """The end-to-end claim: a compaction driven BY THE SESSION shows up in the ledger
    the cost meter reads. Before this, those tokens reached nothing."""
    session = _session()

    async def _fake(model, context, options=None):
        return _reply("summary", input_tokens=6000, output_tokens=200)

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake)

    log = session.session_log
    # Each user message is padded to ~5000 estimated tokens so that the six turns
    # together exceed the SHIPPED keep_recent_tokens (20000) and the cut therefore
    # leaves a real prefix behind. Twelve two-character messages did not: under
    # default settings the cut kept everything, and `prepare_compaction` now
    # reports that as "nothing to compact" (None) instead of spending the
    # summariser call this test is about. The `result is not None` line below was
    # already the guard against exactly that, and it is what caught it.
    padding = "x" * 20_000
    for i in range(6):
        log.append_message(
            {"role": "user", "content": [{"type": "text", "text": f"m{i} {padding}"}]}
        )
        log.append_message({"role": "assistant", "content": [{"type": "text", "text": f"r{i}"}]})

    assert session.side_usage["total_tokens"] == 0, "nothing spent off-loop yet"

    result = await session._perform_compaction()
    assert result is not None, "the fixture must actually trigger a compaction"

    assert session.side_usage["input_tokens"] == 6000
    assert session.side_usage["total_tokens"] == 6200


# --- ctx.complete(): the fan-out case ---------------------------------------


async def test_ctx_complete_bills_its_tokens_to_the_session(monkeypatch):
    session = _session()

    async def _fake(model, context, options=None):
        return _reply("include", input_tokens=120, output_tokens=1)

    monkeypatch.setattr("tau_ai.client.complete_simple", _fake)

    await session._extension_api.context.complete(
        [{"role": "user", "content": [{"type": "text", "text": "include this doc?"}]}]
    )
    assert session.side_usage["total_tokens"] == 121


async def test_a_constrained_fan_out_accumulates_every_verdict(monkeypatch):
    """The demo ctx.complete() was BUILT for: N verdicts per invocation. Dropping these
    understates the cost by a factor of N — the case where forgetting hurts most."""
    session = _session()

    async def _fake(model, context, options=None):
        return _reply("include", input_tokens=100, output_tokens=1)

    monkeypatch.setattr("tau_ai.client.complete_simple", _fake)

    import asyncio

    await asyncio.gather(
        *[
            session._extension_api.context.complete(
                [{"role": "user", "content": [{"type": "text", "text": f"doc {i}?"}]}]
            )
            for i in range(50)
        ]
    )
    assert session.side_usage["total_tokens"] == 50 * 101


async def test_a_truncated_completion_is_still_billed(monkeypatch):
    """A completion we cannot USE is not a completion that was FREE.

    ``stop_reason="length"`` means the model generated a full ``max_tokens`` of output
    and the provider charged for every one of them — and ctx.complete() then discards
    the answer as a truncated prefix (Fail-Early) and raises. Billing only on the
    success path would silently undercount exactly the calls that went wrong, which is
    the same undercount this ledger exists to end.
    """
    session = _session()

    async def _fake(model, context, options=None):
        return AssistantMessage(
            role="assistant",
            content=[TextContent(type="text", text="a truncated pref")],
            api="openai-completions",
            provider="openai",
            model="m",
            stop_reason="length",
            timestamp=0,
            usage=Usage(input_tokens=500, output_tokens=256, total_tokens=756),
        )

    monkeypatch.setattr("tau_ai.client.complete_simple", _fake)

    with pytest.raises(RuntimeError, match="truncated PREFIX"):
        await session._extension_api.context.complete(
            [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        )

    assert session.side_usage["total_tokens"] == 756, "the provider charged for these"


async def test_a_failed_completion_bills_exactly_what_the_provider_reported(monkeypatch):
    """Not "errors are free" — "we report what we were told". The ledger mirrors the
    provider's own accounting rather than second-guessing it (tau_agent_core.usage)."""
    session = _session()

    async def _fake(model, context, options=None):
        return AssistantMessage(
            role="assistant",
            content=[],
            api="openai-completions",
            provider="openai",
            model="m",
            stop_reason="error",
            error_message="boom",
            timestamp=0,
            usage=Usage(input_tokens=99, output_tokens=0, total_tokens=99),
        )

    monkeypatch.setattr("tau_ai.client.complete_simple", _fake)

    with pytest.raises(RuntimeError, match="boom"):
        await session._extension_api.context.complete(
            [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        )
    assert session.side_usage["total_tokens"] == 99


# --- the ledger's own contract ----------------------------------------------


def test_the_ledger_hands_out_a_copy_not_a_live_alias():
    """Same bug class as SessionLog.entries()'s shallow copy: one reader's arithmetic
    must not silently rewrite the session's record of what it spent."""
    session = _session()
    session.record_side_usage({"input_tokens": 10, "total_tokens": 10})

    snapshot = session.side_usage
    snapshot["input_tokens"] = 999_999

    assert session.side_usage["input_tokens"] == 10
