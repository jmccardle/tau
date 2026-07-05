"""Cost at the emit boundary (E4.cost / step S7).

The optional per-model ``cost:{input, output, cache_read, cache_write}`` block
(USD per 1M tokens) on a ``~/.tau/config.json`` model entry prices a completed
exchange into ``cost_usd`` at the emit boundary — the ``TauBackend.stream_chat``
usage return (which the TUI finalizer and headless ``done`` both read). The
Fail-Early contract under test: ``cost_usd`` is emitted **only** when the block
is configured, so an *unknown* price (no ``cost_usd`` key) reads differently from
a genuinely free model (``cost:{…:0}`` → ``cost_usd: 0.0``).

The LLM boundary is patched (``agent_loop.stream_simple``) so the full loop runs
without a network call — the same technique tau-agent-core's ``fake_llm`` uses.

Reference: EXTENSIONS-IMPLEMENTATION.md §E4.cost, §8 S7.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.backends import TauBackend, compute_cost_usd


# --- the pure port of pi calculateCost, collapsed --------------------------


def test_absent_cost_block_is_unknown_not_zero():
    # Fail-Early: no configured prices → None (caller emits tokens only), NEVER 0.0.
    assert compute_cost_usd(None, input_tokens=1000, output_tokens=500, cache_read_tokens=0) is None


def test_present_cost_block_sums_priced_buckets():
    # input 2.5 $/M · 1000 = 0.0025 ; output 10 $/M · 500 = 0.005 ;
    # cache_read 1.25 $/M · 200 = 0.00025  → 0.00775.
    cost = {"input": 2.5, "output": 10.0, "cache_read": 1.25, "cache_write": 99.0}
    total = compute_cost_usd(cost, input_tokens=1000, output_tokens=500, cache_read_tokens=200)
    assert total == pytest.approx(0.00775)


def test_free_model_reads_zero_not_absent():
    # A real free/local model prices to 0.0 — DISTINCT from an unknown price (None).
    total = compute_cost_usd(
        {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
    )
    assert total == 0.0
    assert total is not None


def test_cache_write_is_inert_not_priced():
    # cache_write is commented out of the sum (provider never populates it); a
    # cache_write price must not change the total even with cache_write_tokens set.
    with_price = compute_cost_usd(
        {"input": 1.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1000.0},
        input_tokens=1000,
        output_tokens=0,
        cache_read_tokens=0,
    )
    assert with_price == pytest.approx(0.001)  # only the input bucket counts


# --- through the real backend loop -----------------------------------------


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1000, output_tokens=500, total_tokens=1500, cache_read_tokens=0),
    )


class _EventIterator:
    def __init__(self, events):
        self._events = events
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return _EventIterator(self._events)

    async def result(self):
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self):
        pass


async def _fake_stream_simple(model, context, options=None):
    text = "ok"
    return _Stream(
        [
            TextDeltaEvent(delta=text, partial=_assistant(text)),
            DoneEvent(final=_assistant(text), usage=_assistant(text).usage),
        ]
    )


@pytest.fixture
def fake_llm():
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream_simple):
        yield


def _cfg(**over) -> dict:
    base = {
        "backend": "openai",
        "model": "qwen",
        "base_url": "http://localhost/v1",
        "api_key": "not-needed",
        "tools": [],  # no tools → single completion, deterministic usage sum
    }
    base.update(over)
    return base


def _run_usage(cfg: dict) -> dict:
    backend = TauBackend(cfg)

    def _noop(_delta: str) -> None:
        pass

    _text, usage, _new, _tcs = asyncio.run(
        backend.stream_chat([{"role": "user", "content": "hi"}], _noop)
    )
    return usage


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_backend_emits_cost_usd_when_block_present(fake_llm):
    usage = _run_usage(
        _cfg(cost={"input": 2.5, "output": 10.0, "cache_read": 1.25, "cache_write": 0.0})
    )
    # Real summed tokens reached the emit boundary and were priced.
    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 500
    assert "cost_usd" in usage
    assert usage["cost_usd"] == pytest.approx(0.0075)  # 0.0025 + 0.005


def test_backend_omits_cost_usd_when_block_absent(fake_llm):
    usage = _run_usage(_cfg())  # no cost block
    # Tokens still flow; cost stays UNKNOWN (key absent), never a fabricated $0.
    assert usage["total_tokens"] == 1500
    assert "cost_usd" not in usage


def test_backend_free_model_emits_zero_distinct_from_absent(fake_llm):
    usage = _run_usage(
        _cfg(cost={"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0})
    )
    # A genuinely free model reads as 0.0 PRESENT — not the same as absent.
    assert "cost_usd" in usage
    assert usage["cost_usd"] == 0.0
