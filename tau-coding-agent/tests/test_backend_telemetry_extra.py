"""G4/A — ``TauBackend.stream_chat`` carries the last completion's ``usage.extra``.

The exchange-summary telemetry readout (t/s · repairs · forced-share) rides the
LAST completion's ``usage.extra`` — the server-reported per-completion telemetry τ
folds onto ``Usage.extra`` (llama.cpp ``timings`` + τ's JSON-repair count). t/s and
forced-share are per-completion, not summable like tokens, so the backend keeps only
the final completion's dict.

Fail-Early contract under test: the ``extra`` key reaches the emit-boundary usage
dict ONLY when a provider actually reported something — a stock/non-llama server that
sent no telemetry leaves ``extra`` off entirely, never ``extra: {}``.

The LLM boundary is patched (``agent_loop.stream_simple``) so the full loop runs
without a network call — the same technique ``test_cost.py`` uses.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.backends import TauBackend


STOCK_TIMINGS = {
    "prompt_n": 12,
    "prompt_ms": 40.5,
    "predicted_n": 20,
    "predicted_ms": 250.0,
    "predicted_per_second": 80.0,
}


def _assistant(text: str, usage: Usage) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason="stop",
        timestamp=0,
        usage=usage,
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


def _stream_with_usage(usage: Usage):
    async def _fake_stream_simple(model, context, options=None):
        text = "ok"
        msg = _assistant(text, usage)
        return _Stream([TextDeltaEvent(delta=text, partial=msg), DoneEvent(final=msg, usage=usage)])

    return _fake_stream_simple


def _cfg() -> dict:
    return {
        "backend": "openai",
        "model": "qwen",
        "base_url": "http://localhost/v1",
        "api_key": "not-needed",
        "tools": [],  # no tools → a single completion, deterministic message_end
    }


def _run_usage(usage: Usage) -> dict:
    backend = TauBackend(_cfg())
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_with_usage(usage)):
        _text, usage_out, _new, _tcs = asyncio.run(
            backend.stream_chat([{"role": "user", "content": "hi"}], lambda _d: None)
        )
    return usage_out


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_extra_from_the_completion_reaches_the_usage_dict():
    usage = Usage(
        input_tokens=12,
        output_tokens=20,
        total_tokens=32,
        extra={"timings": STOCK_TIMINGS, "repairs": 0},
    )
    usage_out = _run_usage(usage)
    assert usage_out["extra"] == {"timings": STOCK_TIMINGS, "repairs": 0}


def test_extra_is_omitted_when_the_provider_reported_nothing():
    # A stock/non-llama server: Usage.extra defaults to {}. The emit boundary must
    # NOT carry an empty `extra: {}` — the key is absent (Fail-Early).
    usage = Usage(input_tokens=12, output_tokens=20, total_tokens=32)
    usage_out = _run_usage(usage)
    assert "extra" not in usage_out
    # Tokens still flow — omitting extra never hides the real counts.
    assert usage_out["total_tokens"] == 32
