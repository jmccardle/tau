"""G5 — prefix-stability contract tests (§7.1 of docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md).

llama.cpp's KV-cache prefix reuse (and future KV-branching) only fires when
successive requests share a **byte-identical prefix** of the serialized request
body. Anything that perturbs an *earlier* message between calls silently kills
cache reuse server-side — nothing in τ used to detect that. These tests pin the
property directly against the wire payload (the harness pattern is
``tau-ai/tests/test_reasoning_effort.py``'s ``_CapturingClient`` /
``_patch_client``, replicated here since these tests need to capture *every*
request in a turn, not just the last one).

The core predicate, :func:`_assert_byte_prefix`, is deliberately **byte-level**:
it compares the compact ``json.dumps`` of the ``messages`` array as strings, not
parsed lists of dicts. A dict-equality check would silently accept a reordered
key or a differently-formatted float — either would still change the bytes sent
to the tokenizer and break the server's prefix match, so it is exactly the thing
this contract must catch.

Covers:
  - within-turn stability (assistant -> tool call -> tool result -> assistant,
    twice): §7.1 threat 2.
  - the ``reasoning_replay="turn"`` divergence: §7.1 threat 1. This is a
    **measured, deliberate** pi divergence (see the long comment on
    ``Model.reasoning_replay``, tau_ai/types.py:~153) — the test PINS where and
    why the prefix breaks, it does not "fix" it. Do not weaken these assertions
    to make the divergence look clean; that would document a design trade-off as
    a bug (or vice versa).

The N-way fan-out identity test (§7.1 threat 3, C1 ``ctx.complete()``) lives in
``tau-agent-core/tests/test_prefix_stability_fanout.py`` — it needs a real
``AgentSession``, which tau-ai must not depend on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.streaming import DoneEvent
from tau_ai.types import (
    AssistantMessage,
    Model,
    TextContent,
    ToolResultMessage,
    UserMessage,
)

# ──────────────────────────────────────────────────────────────────────────
# The capturing-client harness (test_reasoning_effort.py's _CapturingClient /
# _patch_client pattern, extended to record EVERY request of a turn — not just
# the last — and to serve a queue of canned SSE responses, one per request, so
# a multi-step tool-calling turn can be driven end to end).
# ──────────────────────────────────────────────────────────────────────────


def _mock_response(chunks: list[dict]) -> MagicMock:
    body = "\n".join(["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"])
    response = MagicMock()
    response.status_code = 200
    response.text = body
    response.headers = {"x-request-id": "rid"}

    async def _aiter():
        for line in body.split("\n"):
            yield line

    response.aiter_lines = _aiter
    return response


_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _text_reply(text: str) -> MagicMock:
    """A plain text completion (no thinking, no tool calls)."""
    return _mock_response(
        [
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": text}}]},
            {
                "id": "c",
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _USAGE,
            },
        ]
    )


def _tool_call_reply(call_id: str, name: str, arguments: dict) -> MagicMock:
    """A completion that emits a single tool call (no text, no thinking)."""
    return _mock_response(
        [
            {
                "id": "c",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": call_id, "function": {"name": name, "arguments": ""}}
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": json.dumps(arguments)}}
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": _USAGE,
            },
        ]
    )


def _thinking_tool_call_reply(call_id: str, name: str, arguments: dict, thinking: str) -> MagicMock:
    """A completion that streams reasoning on ``reasoning_content`` AND a tool call."""
    return _mock_response(
        [
            {"id": "c", "choices": [{"index": 0, "delta": {"reasoning_content": thinking}}]},
            {
                "id": "c",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": call_id, "function": {"name": name, "arguments": ""}}
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": json.dumps(arguments)}}
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": _USAGE,
            },
        ]
    )


def _thinking_text_reply(text: str, thinking: str) -> MagicMock:
    """A completion that streams reasoning on ``reasoning_content`` AND final text."""
    return _mock_response(
        [
            {"id": "c", "choices": [{"index": 0, "delta": {"reasoning_content": thinking}}]},
            {"id": "c", "choices": [{"index": 0, "delta": {"content": text}}]},
            {
                "id": "c",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _USAGE,
            },
        ]
    )


class _StreamCM:
    """Async context manager mimicking ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _CapturingClient:
    """A fake httpx.AsyncClient that records every request's JSON payload, in
    order, and serves canned responses from a FIFO queue (one per request).

    Class-level state on purpose: ``OpenAICompletionsProvider._get_client()``
    caches one client per provider instance and reuses it for every
    ``stream_chat`` call in a turn (mirroring the real client's connection
    reuse), so this must accumulate across calls rather than overwrite.
    """

    payloads: list[dict] = []
    _responses: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        _CapturingClient.payloads.append(kwargs.get("json"))
        return _CapturingClient._responses.pop(0)

    def stream(self, *args: Any, **kwargs: Any) -> _StreamCM:
        _CapturingClient.payloads.append(kwargs.get("json"))
        return _StreamCM(_CapturingClient._responses.pop(0))

    async def __aenter__(self) -> "_CapturingClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


def _reset_capture() -> None:
    _CapturingClient.payloads = []
    _CapturingClient._responses = []


def _queue(*responses: Any) -> None:
    _CapturingClient._responses.extend(responses)


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tau_ai.providers.openai.httpx.AsyncClient", _CapturingClient)


def _model(**overrides: Any) -> Model:
    defaults: dict = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "context_window": 100_000,
        "max_tokens": 1000,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _run_stream_chat(provider: OpenAICompletionsProvider, model: Model, messages: list) -> AssistantMessage:
    """Drive one ``stream_chat`` call to completion; return the final message."""

    async def _go() -> AssistantMessage:
        stream = await provider.stream_chat(model=model, messages=messages)
        events = [e async for e in stream]
        done = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done) == 1, "expected exactly one DoneEvent"
        return done[0].final

    return asyncio.run(_go())


def _messages_json(payload: dict) -> str:
    """Compact, deterministic serialization of a captured payload's ``messages``."""
    return json.dumps(payload["messages"], separators=(",", ":"))


def _assert_byte_prefix(earlier: str, later: str) -> None:
    """``earlier`` (a compact ``messages`` array JSON string) must be a strict
    BYTE prefix of ``later`` — i.e. ``later`` only ever *appends* array
    elements; it never rewrites one of ``earlier``'s.

    Implemented by trimming ``earlier``'s closing ``]`` and requiring ``later``
    to start with the trimmed string, then checking what follows is either
    nothing (identical arrays) or a fresh ``,`` introducing a new element —
    never a byte that continues rewriting the last shared element.
    """
    assert earlier.startswith("[") and earlier.endswith("]"), earlier
    assert later.startswith("[") and later.endswith("]"), later
    trimmed = earlier[:-1]
    assert later.startswith(trimmed), (
        "prefix broken: the earlier request's `messages` is not a byte-prefix "
        f"of the later request's.\nEARLIER: {earlier}\nLATER:   {later}"
    )
    rest = later[len(trimmed) :]
    assert rest == "]" or rest.startswith(","), f"unexpected continuation after the shared prefix: {rest!r}"


# ──────────────────────────────────────────────────────────────────────────
# 1. Within-turn stability (§7.1 threat 2)
# ──────────────────────────────────────────────────────────────────────────


def test_within_turn_stability_across_tool_call_round_trips(monkeypatch):
    """A multi-step turn — assistant -> tool call -> tool result -> assistant,
    TWICE in a row — must only ever APPEND to the wire `messages` array.

    Drives the real ``OpenAICompletionsProvider.stream_chat`` three times in a
    row (the same provider instance, so the same cached fake client), growing
    the message list exactly as the agent loop does between LLM calls, and
    asserts each request's serialized `messages` is a strict byte prefix of the
    next one's.
    """
    _patch_client(monkeypatch)
    model = _model()
    provider = OpenAICompletionsProvider(api_key="sk-test")

    _reset_capture()
    _queue(
        _tool_call_reply("call_1", "search", {"query": "alpha"}),
        _tool_call_reply("call_2", "search", {"query": "beta"}),
        _text_reply("done: found it"),
    )

    messages: list[Any] = [UserMessage(content=[TextContent(text="please investigate")], timestamp=0)]

    # Request #1: user only -> assistant emits a tool call.
    reply1 = _run_stream_chat(provider, model, messages)
    messages = messages + [
        reply1,
        ToolResultMessage(
            tool_call_id="call_1",
            tool_name="search",
            content=[TextContent(text="alpha result")],
            timestamp=0,
        ),
    ]

    # Request #2: ... + the first tool round trip -> a SECOND tool call.
    reply2 = _run_stream_chat(provider, model, messages)
    messages = messages + [
        reply2,
        ToolResultMessage(
            tool_call_id="call_2",
            tool_name="search",
            content=[TextContent(text="beta result")],
            timestamp=0,
        ),
    ]

    # Request #3: ... + the second tool round trip -> the final answer.
    reply3 = _run_stream_chat(provider, model, messages)
    assert reply3.stop_reason == "stop"

    assert len(_CapturingClient.payloads) == 3
    json1, json2, json3 = (_messages_json(p) for p in _CapturingClient.payloads)

    # The whole point: request N's bytes are a strict prefix of request N+1's.
    _assert_byte_prefix(json1, json2)
    _assert_byte_prefix(json2, json3)


# ──────────────────────────────────────────────────────────────────────────
# 3. The reasoning_replay="turn" divergence (§7.1 threat 1) — KNOWN, DELIBERATE
# ──────────────────────────────────────────────────────────────────────────


def _drive_two_turn_tool_call_flow(provider: OpenAICompletionsProvider, model: Model) -> None:
    """Turn 1 (2 requests, a tool round trip) then turn 2 opens (1 more
    request). Populates ``_CapturingClient.payloads`` with exactly 3 entries:

      [0] turn 1, request 1: [user1]                         -- no assistant yet
      [1] turn 1, request 2: [user1, reply1, toolResult1]     -- reply1 IN-TURN
      [2] turn 2, request 1: [user1, reply1, toolResult1,
                               reply2, user2]                 -- reply1 now OUT of turn
    """
    _queue(
        _thinking_tool_call_reply("call_1", "search", {"query": "x"}, thinking="let me think about x"),
        _thinking_text_reply("turn 1 answer", thinking="ok, answering now"),
        _text_reply("turn 2 answer"),
    )

    messages: list[Any] = [UserMessage(content=[TextContent(text="turn 1 question")], timestamp=0)]
    reply1 = _run_stream_chat(provider, model, messages)  # payloads[0]
    messages = messages + [
        reply1,
        ToolResultMessage(
            tool_call_id="call_1", tool_name="search", content=[TextContent(text="x result")], timestamp=0
        ),
    ]

    reply2 = _run_stream_chat(provider, model, messages)  # payloads[1]
    messages = messages + [reply2]

    # Turn 1 is over (reply2 carried no tool call). A new user message opens turn 2.
    messages = messages + [UserMessage(content=[TextContent(text="turn 2 question")], timestamp=1)]
    _run_stream_chat(provider, model, messages)  # payloads[2]


def test_reasoning_replay_turn_breaks_prefix_at_the_dropped_thinking_block(monkeypatch):
    """DELIBERATE divergence, pinned exactly: with ``reasoning_replay="turn"``
    (τ's default), the assistant message that carried turn 1's thinking is
    REWRITTEN — not appended past — the moment turn 2's user message lands,
    because its ``reasoning_content`` field is dropped once it is no longer
    "in the current turn" (index <= the last user message's index). Every
    OTHER shared message stays byte-identical.

    This is documented, not a bug: see the ``Model.reasoning_replay`` docstring
    (tau_ai/types.py:~153) and docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §7.1
    threat 1 / §9 decision 5. Do NOT "fix" this by changing the default to pi
    parity ("all") — that is an explicit standing decision, traded off against
    the context bloat "all" causes on long tool-driven sessions.
    """
    _patch_client(monkeypatch)
    model = _model(reasoning=True, reasoning_replay="turn")
    provider = OpenAICompletionsProvider(api_key="sk-test")
    _reset_capture()

    _drive_two_turn_tool_call_flow(provider, model)

    assert len(_CapturingClient.payloads) == 3
    turn1_req2 = _CapturingClient.payloads[1]["messages"]  # reply1 IN-TURN here
    turn2_req1 = _CapturingClient.payloads[2]["messages"]  # reply1 OUT-of-turn here

    j_before = [json.dumps(m, separators=(",", ":")) for m in turn1_req2]
    j_after = [json.dumps(m, separators=(",", ":")) for m in turn2_req1]

    # index 0 = user1, index 1 = reply1 (the thinking-bearing tool call), index 2 = toolResult1.
    assert j_after[0] == j_before[0], "user1 must survive unchanged"
    assert j_after[2] == j_before[2], "toolResult1 must survive unchanged"

    # reply1 is REWRITTEN, not preserved: its reasoning field disappears.
    assert "reasoning_content" in j_before[1]
    assert "reasoning_content" not in j_after[1]
    assert j_after[1] != j_before[1]

    # Pin it with the same whole-array predicate test (1) uses: turn 1's second
    # request is NOT a byte-prefix of turn 2's first request.
    whole_before = _messages_json(_CapturingClient.payloads[1])
    whole_after = _messages_json(_CapturingClient.payloads[2])
    with pytest.raises(AssertionError):
        _assert_byte_prefix(whole_before, whole_after)


def test_reasoning_replay_all_keeps_the_same_turn_boundary_prefix_stable(monkeypatch):
    """The other arm of the trade-off: ``reasoning_replay="all"`` (pi parity)
    pays the payload-bloat cost precisely to keep this prefix-stable across the
    turn boundary that "turn" deliberately breaks. Same flow as the "turn" test
    above, opposite outcome, on the exact same message shape.
    """
    _patch_client(monkeypatch)
    model = _model(reasoning=True, reasoning_replay="all")
    provider = OpenAICompletionsProvider(api_key="sk-test")
    _reset_capture()

    _drive_two_turn_tool_call_flow(provider, model)

    assert len(_CapturingClient.payloads) == 3
    turn1_req2 = _CapturingClient.payloads[1]["messages"]
    turn2_req1 = _CapturingClient.payloads[2]["messages"]

    j_before = [json.dumps(m, separators=(",", ":")) for m in turn1_req2]
    j_after = [json.dumps(m, separators=(",", ":")) for m in turn2_req1]

    # reply1's reasoning field survives the turn boundary byte-for-byte under "all".
    assert "reasoning_content" in j_before[1]
    assert "reasoning_content" in j_after[1]
    assert j_after[1] == j_before[1]

    whole_before = _messages_json(_CapturingClient.payloads[1])
    whole_after = _messages_json(_CapturingClient.payloads[2])
    _assert_byte_prefix(whole_before, whole_after)
