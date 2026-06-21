"""Reasoning/thinking streaming — gates 1-3 (make reasoning flow).

Empirically grounded: Qwen3-35B on llama.cpp emits reasoning on the
``reasoning_content`` delta field (NOT ``reasoning``), streams it BEFORE the
answer ``content``, and reports token ``usage`` in a trailing chunk whose
``choices`` is empty, AFTER the ``finish_reason`` chunk. These tests replay that
exact shape and assert:

  1. reasoning_content is extracted (was dropped — only ``reasoning`` was read),
  2. it is yielded live as ThinkingDeltaEvents (was accumulated but never yielded),
  3. usage from the trailing empty-choices chunk is captured (the loop used to
     return on finish_reason, before that chunk arrived).
"""

from __future__ import annotations

import asyncio
import json

from tau_ai.providers.openai import (
    OpenAICompletionsProvider,
    _extract_reasoning,
    _usage_from_openai,
)
from tau_ai.streaming import (
    AssistantMessageEventStream,
    DoneEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_ai.types import Model, TextContent, ThinkingContent, ToolCall, UserMessage


# ──────────────────────────────────────────────────────────────────────────
# SSE harness (feeds aiter_lines like real httpx)
# ──────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self._lines = lines
        self.headers = {"x-request-id": "test-req"}
        self.text = "\n".join(lines)

    def json(self):
        return {}

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response


def _model() -> Model:
    return Model(
        id="test-model", name="test-model", api="openai-completions",
        provider="openai", base_url="http://localhost/v1",
        context_window=8192, max_tokens=1024,
    )


def _sse(chunks: list[dict]) -> list[str]:
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


def _run(provider: OpenAICompletionsProvider, response: _FakeResponse) -> list:
    async def go():
        provider._get_client = lambda: _FakeClient(response)  # type: ignore[method-assign]
        stream = await provider.stream_chat(
            model=_model(),
            messages=[UserMessage(content=[TextContent(text="17*23?")], timestamp=0)],
        )
        return [e async for e in stream]
    return asyncio.run(go())


# The canonical llama.cpp / Qwen3 reasoning stream: role chunk, reasoning_content
# deltas, answer content, finish_reason, then a trailing usage-only chunk.
_LLAMACPP_SHAPE = [
    {"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"reasoning_content": "Hmm,"}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"reasoning_content": " 17*23 = 391."}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"content": "The answer"}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"content": " is 391."}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"id": "x", "choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 80, "total_tokens": 111}},
]


def test_reasoning_content_streams_live_and_finalizes():
    events = _run(OpenAICompletionsProvider(), _FakeResponse(_sse(_LLAMACPP_SHAPE)))

    # Gate 1+2: reasoning_content extracted AND yielded live, in order, distinct
    # from the answer text.
    thinking = [e for e in events if isinstance(e, ThinkingDeltaEvent)]
    assert [e.delta for e in thinking] == ["Hmm,", " 17*23 = 391."]
    text = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert [e.delta for e in text] == ["The answer", " is 391."]

    # Reasoning arrives before the answer (the whole point — collapse-on-content).
    assert events.index(thinking[0]) < events.index(text[0])

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    final = done[0].final
    assert "".join(c.thinking for c in final.content if isinstance(c, ThinkingContent)) == "Hmm, 17*23 = 391."
    assert "".join(c.text for c in final.content if isinstance(c, TextContent)) == "The answer is 391."
    assert final.stop_reason == "stop"


def test_final_thinking_block_captures_the_reasoning_signature():
    """The consolidated thinking block records which field reasoning streamed on
    (``reasoning_content`` here) so a follow-up turn can replay it under the same
    field. The capture is what makes the pi-style reasoning round-trip possible."""
    final = [e for e in _run(OpenAICompletionsProvider(), _FakeResponse(_sse(_LLAMACPP_SHAPE)))
             if isinstance(e, DoneEvent)][0].final
    thinking = [c for c in final.content if isinstance(c, ThinkingContent)]
    assert len(thinking) == 1
    assert thinking[0].thinking_signature == "reasoning_content"


def test_reasoning_field_fallback_signature_is_recorded():
    """When the server uses an alternate field (``reasoning``), that exact name is
    the captured signature — not hardcoded to reasoning_content."""
    shape = [
        {"id": "x", "choices": [{"index": 0, "delta": {"reasoning": "thinking…"}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {"content": "done"}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    final = [e for e in _run(OpenAICompletionsProvider(), _FakeResponse(_sse(shape)))
             if isinstance(e, DoneEvent)][0].final
    thinking = [c for c in final.content if isinstance(c, ThinkingContent)]
    assert thinking and thinking[0].thinking_signature == "reasoning"


def test_final_message_consolidates_blocks_thinking_before_text():
    """Fix A: a streamed message persists as ONE thinking + ONE text block (not
    one per fragment), with thinking before text — the stream order and pi's."""
    final = [e for e in _run(OpenAICompletionsProvider(), _FakeResponse(_sse(_LLAMACPP_SHAPE)))
             if isinstance(e, DoneEvent)][0].final
    thinking = [c for c in final.content if isinstance(c, ThinkingContent)]
    text = [c for c in final.content if isinstance(c, TextContent)]
    assert len(thinking) == 1 and len(text) == 1
    assert thinking[0].thinking == "Hmm, 17*23 = 391."
    assert text[0].text == "The answer is 391."
    # Thinking precedes the answer in block order.
    assert final.content.index(thinking[0]) < final.content.index(text[0])


# A reasoning-then-tool-call stream: this is the shape that produced the
# "reasoning shown N×" bug — every tool-arg fragment re-emitted a partial whose
# content was the full (bloated) reasoning trace. Consolidation (fix A) makes
# each partial carry exactly one thinking block, so the backend suffix-diff has
# nothing to re-emit.
_REASON_THEN_TOOL_SHAPE = [
    {"id": "x", "choices": [{"index": 0, "delta": {"reasoning_content": "I should"}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"reasoning_content": " run date."}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "function": {"name": "bash", "arguments": ""}}]}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{\"command\":"}}]}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": " \"date\"}"}}]}}]},
    {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
]


def test_partial_messages_carry_single_thinking_block_through_tool_call_deltas():
    """Fix B (via A): every partial — including those emitted on each tool-call
    argument fragment — carries exactly one thinking block holding the full
    reasoning, never N fragment-blocks. That is what stops the backend from
    re-emitting the whole reasoning trace per fragment."""
    events = _run(OpenAICompletionsProvider(), _FakeResponse(_sse(_REASON_THEN_TOOL_SHAPE)))
    partials = [e.partial for e in events if isinstance(e, ToolCallDeltaEvent)]
    assert partials, "expected tool-call delta events"
    for partial in partials:
        thinking = [c for c in partial.content if isinstance(c, ThinkingContent)]
        assert len(thinking) == 1
        assert thinking[0].thinking == "I should run date."
    # And the final tool call decodes cleanly from the concatenated fragments.
    final = [e for e in events if isinstance(e, DoneEvent)][0].final
    calls = [c for c in final.content if isinstance(c, ToolCall)]
    assert len(calls) == 1 and calls[0].arguments == {"command": "date"}


def test_stream_payload_requests_usage():
    """Fix C: the streaming request carries stream_options.include_usage so the
    server emits the trailing usage chunk (else token counts come back 0)."""
    captured: dict = {}

    class _CapturingClient:
        async def post(self, *args, **kwargs):
            captured.update(kwargs.get("json", {}))
            return _FakeResponse(_sse(_LLAMACPP_SHAPE))

    provider = OpenAICompletionsProvider()
    provider._get_client = lambda: _CapturingClient()  # type: ignore[method-assign]

    async def go():
        stream = await provider.stream_chat(
            model=_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        )
        return [e async for e in stream]

    asyncio.run(go())
    assert captured.get("stream_options") == {"include_usage": True}


def test_usage_captured_from_trailing_empty_choices_chunk():
    """The bug "show the zero would always show zero": usage lived in a chunk
    AFTER finish_reason with empty choices, and the loop returned too early."""
    done = [e for e in _run(OpenAICompletionsProvider(), _FakeResponse(_sse(_LLAMACPP_SHAPE)))
            if isinstance(e, DoneEvent)]
    usage = done[0].usage
    assert usage.total_tokens == 111
    assert usage.input_tokens == 31
    assert usage.output_tokens == 80


def test_reasoning_field_fallback_names():
    """A server using the bare ``reasoning`` field (OpenRouter style) also works."""
    chunks = [
        {"id": "x", "choices": [{"index": 0, "delta": {"reasoning": "thinking…"}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {"content": "done"}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    events = _run(OpenAICompletionsProvider(), _FakeResponse(_sse(chunks)))
    assert [e.delta for e in events if isinstance(e, ThinkingDeltaEvent)] == ["thinking…"]


def test_no_reasoning_means_no_thinking_events():
    """A plain text response must not emit any thinking events or blocks."""
    chunks = [
        {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    events = _run(OpenAICompletionsProvider(), _FakeResponse(_sse(chunks)))
    assert not any(isinstance(e, ThinkingDeltaEvent) for e in events)
    final = [e for e in events if isinstance(e, DoneEvent)][0].final
    assert not any(isinstance(c, ThinkingContent) for c in final.content)


def test_wrapper_raw_dict_path_emits_thinking():
    """The streaming.py raw-dict accumulation path also surfaces reasoning."""
    raw = [
        {"delta": {"reasoning_content": "let me think"}},
        {"delta": {"content": "answer"}},
    ]

    async def raw_stream():
        for c in raw:
            yield c

    async def go():
        stream = AssistantMessageEventStream(provider_stream=raw_stream(), model=_model())
        events = [e async for e in stream]
        return events

    events = asyncio.run(go())
    assert [e.delta for e in events if isinstance(e, ThinkingDeltaEvent)] == ["let me think"]


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def test_extract_reasoning_priority():
    # First non-empty wins, in priority order; the field name (signature) is
    # returned alongside the text so a follow-up turn can replay reasoning.
    assert _extract_reasoning({"reasoning_content": "a", "reasoning": "b"}) == ("a", "reasoning_content")
    assert _extract_reasoning({"reasoning": "b", "reasoning_text": "c"}) == ("b", "reasoning")
    assert _extract_reasoning({"reasoning_text": "c"}) == ("c", "reasoning_text")
    assert _extract_reasoning({"reasoning_content": ""}) == ("", "")  # empty is not a hit
    assert _extract_reasoning({"content": "x"}) == ("", "")
    assert _extract_reasoning({"reasoning_content": None}) == ("", "")


def test_usage_from_openai_maps_and_computes_total():
    u = _usage_from_openai({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (10, 20, 30)

    # Missing total → computed from input+output, not fabricated.
    u2 = _usage_from_openai({"prompt_tokens": 5, "completion_tokens": 7})
    assert u2.total_tokens == 12

    # Real zero stays zero.
    u3 = _usage_from_openai({})
    assert u3.total_tokens == 0

    # cached prompt tokens map to cache_read.
    u4 = _usage_from_openai({"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": 4}})
    assert u4.cache_read_tokens == 4
