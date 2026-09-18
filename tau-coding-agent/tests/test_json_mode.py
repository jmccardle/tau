"""``tau -p --mode json``: the session header, then one JSON object per line.

The header line FIRST, then every bus event as a ``type``-discriminated object
in τ's own snake-case field names — so a supervising parent reads per-child
limit and failure signals off each ``message_end``, which carries
usage/model/stop_reason.

Two levels of coverage:

* the pure serializer :func:`json_mode_payloads` — the ``type`` discriminator,
  the deduped double ``message_end``, and the delta projection that keeps the
  stream LINEAR in the answer's length (docs/JSON-MODE-DELTAS.md);
* the whole path through the REAL ``TauBackend`` bus + ``run_print``, with the LLM
  boundary patched (``agent_loop.stream_simple``) exactly like ``test_cost.py`` so
  the real loop runs without a network call.

Reference: docs/JSON-MODE-DELTAS.md; EXTENSIONS-IMPLEMENTATION.md §8 S8.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import tau_coding_agent.session_store as store
from tau_agent_core.events import AgentEvent
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage
from tau_agent_core.event_projection import MessageDeltaProjector
from tau_coding_agent.backends import json_mode_payloads
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

# --- the pure serializer ----------------------------------------------------


def _one(event: AgentEvent) -> dict | None:
    """The single line ``event`` produces, or None when it produces none."""
    lines = json_mode_payloads(MessageDeltaProjector(), event)
    assert len(lines) <= 1, lines
    return lines[0] if lines else None


def test_serializer_uses_type_discriminator_not_kind():
    event = AgentEvent(type="turn_start", timestamp=_TS, turn_index=0)
    out = _one(event)
    assert out is not None
    assert out["type"] == "turn_start"
    assert out["turn_index"] == 0
    assert "kind" not in out


def test_an_always_false_flag_is_not_a_field():
    """`blocked`/`is_error` are bool-defaulted, so `exclude_none` cannot drop them.

    Every event of every type carried two dead keys until they were popped
    explicitly. A TRUE one still ships — it is the only one that says anything.
    """
    clean = _one(AgentEvent(type="turn_start", timestamp=_TS, turn_index=0))
    assert clean is not None
    assert "blocked" not in clean and "is_error" not in clean

    failed = _one(
        AgentEvent(type="tool_execution_end", timestamp=_TS, tool_call_id="c1", is_error=True)
    )
    assert failed is not None and failed["is_error"] is True


def test_serializer_keeps_usage_bearing_message_end():
    event = AgentEvent(
        type="message_end",
        timestamp=_TS,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"total_tokens": 5},
            "model": "qwen",
            "stop_reason": "stop",
        },
    )
    out = _one(event)
    assert out is not None
    assert out["type"] == "message_end"
    assert out["message"]["usage"] == {"total_tokens": 5}
    assert out["message"]["model"] == "qwen"
    assert out["message"]["stop_reason"] == "stop"


def test_serializer_drops_duplicate_content_only_message_end():
    event = AgentEvent(
        type="message_end",
        timestamp=_TS,
        message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    )
    assert _one(event) is None


STOCK_TIMINGS = {
    "prompt_n": 12,
    "prompt_ms": 40.5,
    "predicted_n": 20,
    "predicted_ms": 250.0,
    "predicted_per_second": 80.0,
}


def test_serializer_preserves_usage_extra_timings_and_repairs():
    usage = Usage(
        input_tokens=12,
        output_tokens=20,
        total_tokens=32,
        extra={"timings": STOCK_TIMINGS, "repairs": 0},
    )
    event = AgentEvent(
        type="message_end",
        timestamp=_TS,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": usage.model_dump(),
            "model": "qwen",
            "stop_reason": "stop",
        },
    )
    out = _one(event)
    assert out is not None
    extra = out["message"]["usage"]["extra"]
    # exclude_none must NOT drop the nested telemetry — it is real measured data.
    assert extra["timings"] == STOCK_TIMINGS
    assert extra["repairs"] == 0


def test_serializer_does_not_fabricate_an_empty_extra_when_absent():
    event = AgentEvent(
        type="message_end",
        timestamp=_TS,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"total_tokens": 5},
            "model": "qwen",
            "stop_reason": "stop",
        },
    )
    out = _one(event)
    assert out is not None
    assert "extra" not in out["message"]["usage"]


# --- through the real backend bus + run_print -------------------------------


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason="stop",
        timestamp=_TS,
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


def _config() -> dict:
    return {
        "models": {
            "local-llm": {
                "backend": "openai",
                "model": "qwen",
                "base_url": "http://localhost:8080/v1",
                "api_key": "not-needed",
                "tools": [],  # no tools → single completion, one message_end
            },
        },
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)


async def test_run_print_json_is_pi_faithful(fake_llm, capsys):
    # The REAL TauBackend bus drives the serializer end-to-end through run_print.
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config())
    assert rc == 0

    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]

    # Header FIRST (pi print-mode.ts:113-116).
    assert lines[0]["type"] == "session"

    assert all("kind" not in e for e in lines)
    assert all(e.get("type") != "done" for e in lines)

    message_ends = [e for e in lines if e["type"] == "message_end"]
    assert len(message_ends) == 1
    message = message_ends[0]["message"]
    assert message["usage"]["total_tokens"] == 1500
    assert message["usage"]["input_tokens"] == 1000
    assert message["usage"]["output_tokens"] == 500
    assert message["model"] == "qwen"
    assert message["stop_reason"] == "stop"

    assert lines[-1]["type"] == "agent_end"


# --- the stream stays linear ------------------------------------------------


def _update(text: str) -> AgentEvent:
    """One `message_update` carrying the CUMULATIVE text, as the loop emits it."""
    return AgentEvent(
        type="message_update",
        timestamp=_TS,
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
    )


def test_a_message_update_carries_the_delta_not_the_accumulated_message():
    projector = MessageDeltaProjector()
    first = json_mode_payloads(projector, _update("Hel"))
    second = json_mode_payloads(projector, _update("Hello"))

    assert [line["delta"] for line in first] == ["Hel"]
    assert [line["delta"] for line in second] == ["lo"]
    assert all("message" not in line for line in first + second)
    assert second[0]["block_type"] == "text"
    assert second[0]["replace"] is False


def test_an_unchanged_snapshot_produces_no_line():
    """The loop re-emits a snapshot whenever any block changes, including this one's
    siblings. A re-sent identical block is not news."""
    projector = MessageDeltaProjector()
    json_mode_payloads(projector, _update("Hello"))
    assert json_mode_payloads(projector, _update("Hello")) == []


def test_a_growing_tool_call_produces_no_line():
    """Its id and name ride `tool_execution_start`; its arguments ride `_end`.

    Passing the partial block through as well would be a third transmission of
    what two other events already carry, and it is the O(n^2) one.
    """
    projector = MessageDeltaProjector()
    event = AgentEvent(
        type="message_update",
        timestamp=_TS,
        message={
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"c": "l"}}],
        },
    )
    assert json_mode_payloads(projector, event) == []


def test_the_stream_scales_linearly_with_the_answer():
    """The gate for the defect this serializer was rewritten to fix.

    `AgentEvent.message` holds the whole assistant message so far and the loop
    re-sends it per fragment, so dumping it made the stream quadratic: 10,000
    characters of answer wrote 10.6 MB (docs/JSON-MODE-DELTAS.md §1). Doubling the
    answer must roughly double the bytes, not quadruple them. The 2.2 bound is
    pi's, from its own regression #7290 — the same defect, found independently.
    """

    def bytes_for(chars: int) -> int:
        projector = MessageDeltaProjector()
        accumulated, total = "", 0
        for _ in range(chars // 10):
            accumulated += "x" * 10
            for line in json_mode_payloads(projector, _update(accumulated)):
                total += len(json.dumps(line)) + 1
        return total

    small = bytes_for(2_000)
    large = bytes_for(4_000)
    assert small > 0
    assert large / small < 2.2, f"{large} / {small} = {large / small:.1f}x — not linear"
