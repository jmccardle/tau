"""pi-faithful ``--mode json`` (E-json / step S8, D-delegate).

pi's ``--mode json`` writes the session header line FIRST, then every
session-subscribe event as a ``type``-discriminated ``AgentSessionEvent``
(``print-mode.ts:104-116``). τ pulls this forward so the delegate (step S9) can
read real per-child limit / failure signals off each ``message_end`` (which
carries usage/model/stop_reason).

Two levels of coverage:

* the pure serializer :func:`tau_event_to_pi_event` — ``type`` discriminator, the
  deduped double ``message_end``;
* the whole path through the REAL ``TauBackend`` bus + ``run_print``, with the LLM
  boundary patched (``agent_loop.stream_simple``) exactly like ``test_cost.py`` so
  the real loop runs without a network call.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-json, §8 S8.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import tau_coding_agent.session_store as store
from tau_agent_core.events import AgentEvent
from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.backends import tau_event_to_pi_event
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print


# --- the pure serializer ----------------------------------------------------


def test_serializer_uses_type_discriminator_not_kind():
    event = AgentEvent(type="turn_start", timestamp=0, turn_index=0)
    out = tau_event_to_pi_event(event)
    assert out is not None
    assert out["type"] == "turn_start"
    assert out["turn_index"] == 0
    assert "kind" not in out


def test_serializer_keeps_usage_bearing_message_end():
    # The per-completion message_end (agent_loop.py:485) carries usage/model/
    # stop_reason — pi's one-per-message message_end.
    event = AgentEvent(
        type="message_end",
        timestamp=0,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"total_tokens": 5},
            "model": "qwen",
            "stop_reason": "stop",
        },
    )
    out = tau_event_to_pi_event(event)
    assert out is not None
    assert out["type"] == "message_end"
    assert out["message"]["usage"] == {"total_tokens": 5}
    assert out["message"]["model"] == "qwen"
    assert out["message"]["stop_reason"] == "stop"


def test_serializer_drops_duplicate_content_only_message_end():
    # The run()/run_continue message_end (no usage) is the duplicate pi never
    # emits — dedup to None so each assistant message yields exactly one.
    event = AgentEvent(
        type="message_end",
        timestamp=0,
        message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    )
    assert tau_event_to_pi_event(event) is None


# --- through the real backend bus + run_print -------------------------------


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

    # ``type`` discriminator everywhere; never the legacy ``kind`` schema, and no
    # synthetic ``done`` line.
    assert all("kind" not in e for e in lines)
    assert all(e.get("type") != "done" for e in lines)

    # Exactly one message_end (the per-completion one), carrying the real
    # usage/model/stop_reason the delegate reads — the content-only duplicate the
    # loop also emits is deduped away.
    message_ends = [e for e in lines if e["type"] == "message_end"]
    assert len(message_ends) == 1
    message = message_ends[0]["message"]
    assert message["usage"]["total_tokens"] == 1500
    assert message["usage"]["input_tokens"] == 1000
    assert message["usage"]["output_tokens"] == 500
    assert message["model"] == "qwen"
    assert message["stop_reason"] == "stop"

    # The lifecycle bus flows through: agent_end terminates the stream (pi has no
    # synthetic ``done``).
    assert lines[-1]["type"] == "agent_end"
