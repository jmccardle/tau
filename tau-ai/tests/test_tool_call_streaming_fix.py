"""Regression tests for streaming tool-call argument accumulation.

Covers the bug documented in docs/TOOL-CALL-PARSING-BUG.md: OpenAI streams
tool-call name/arguments as incremental fragments that must be concatenated and
routed by stream `index`. These tests drive the real provider with a fragmenting
SSE feed (unlike the older MagicMock fixtures, this mock implements aiter_lines)
so the accumulation path is actually exercised.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tau_ai.json_parse import (
    parse_json_with_repair,
    parse_json_with_repair_info,
    parse_streaming_json,
    repair_json,
)
from tau_ai.client import stream_simple
from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.streaming import DoneEvent, ErrorEvent
from tau_ai.types import Model, TextContent, ToolCall, UserMessage


# ──────────────────────────────────────────────────────────────────────────
# SSE test harness (feeds aiter_lines, the way real httpx does)
# ──────────────────────────────────────────────────────────────────────────


class _StreamCM:
    """Async context manager mimicking ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, lines, status_code=200, json_body=None):
        self.status_code = status_code
        self._lines = lines
        self.headers = {"x-request-id": "test-req"}
        self._json_body = json_body or {"usage": {"total_tokens": 7}}
        self.text = "\n".join(lines)

    def json(self):
        return self._json_body

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response

    def stream(self, *args, **kwargs):
        return _StreamCM(self._response)


def _model() -> Model:
    return Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost/v1",
        context_window=8192,
        max_tokens=1024,
    )


def _sse(chunks: list[dict]) -> list[str]:
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


def _tool_call_chunks(calls: list[dict], *, arg_fragment: int = 4) -> list[dict]:
    """Build chunks that stream each call's name char-by-char and arguments in
    small fragments — the follow-up argument fragments carry only `index`
    (no `id`), exactly like real OpenAI streaming."""
    chunks: list[dict] = [{"id": "c", "choices": [{"delta": {"content": "ok"}}]}]
    for idx, call in enumerate(calls):
        for j, ch in enumerate(call["name"]):
            chunks.append(
                {
                    "id": "c",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "id": call["id"] if j == 0 else None,
                                        "function": {"name": ch, "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
        args_str = (
            call["arguments"]
            if isinstance(call["arguments"], str)
            else json.dumps(call["arguments"])
        )
        for k in range(0, len(args_str), arg_fragment):
            chunks.append(
                {
                    "id": "c",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "function": {
                                            "name": None,
                                            "arguments": args_str[k : k + arg_fragment],
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
    chunks.append({"id": "c", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    return chunks


def _run_stream(provider: OpenAICompletionsProvider, response: _FakeResponse) -> list:
    async def go():
        # patch the client factory to return our fake
        provider._get_client = lambda: _FakeClient(response)  # type: ignore[method-assign]
        stream = await provider.stream_chat(
            model=_model(),
            messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
        )
        return [e async for e in stream]

    return asyncio.run(go())


# ──────────────────────────────────────────────────────────────────────────
# Provider streaming: the regression
# ──────────────────────────────────────────────────────────────────────────


def test_fragmented_arguments_accumulate_to_valid_json():
    """The exact failure case: multi-fragment arguments must concatenate."""
    chunks = _tool_call_chunks(
        [{"id": "call_1", "name": "bash", "arguments": {"command": "ls -la", "cwd": "/tmp"}}],
        arg_fragment=3,
    )
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    tcs = [c for c in done[0].final.content if isinstance(c, ToolCall)]
    assert len(tcs) == 1
    assert tcs[0].id == "call_1"
    assert tcs[0].name == "bash"
    assert tcs[0].arguments == {"command": "ls -la", "cwd": "/tmp"}


def test_parallel_tool_calls_routed_by_index():
    """Two calls whose argument fragments carry only `index` (no id)."""
    chunks = _tool_call_chunks(
        [
            {"id": "call_a", "name": "read", "arguments": {"path": "main.py"}},
            {"id": "call_b", "name": "bash", "arguments": {"command": "npm test"}},
        ],
        arg_fragment=2,
    )
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    tcs = [c for c in done[0].final.content if isinstance(c, ToolCall)]
    assert [(t.id, t.name, t.arguments) for t in tcs] == [
        ("call_a", "read", {"path": "main.py"}),
        ("call_b", "bash", {"command": "npm test"}),
    ]


def test_empty_arguments_become_empty_dict():
    chunks = _tool_call_chunks([{"id": "call_x", "name": "now", "arguments": {}}])
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    tcs = [c for c in done[0].final.content if isinstance(c, ToolCall)]
    assert tcs[0].arguments == {}


def test_complete_but_invalid_final_arguments_raise_error_event():
    """Fail-early: a finished tool call with unparseable JSON surfaces an
    ErrorEvent — it does NOT fabricate {"raw": ...} or run the tool with {}."""
    chunks = _tool_call_chunks([{"id": "call_bad", "name": "bash", "arguments": '{"command": '}])
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert not any(isinstance(e, DoneEvent) for e in events)


# ──────────────────────────────────────────────────────────────────────────
# End-to-end through stream_simple (the path the agent loop actually uses):
# provider yields typed events → the streaming.py wrapper forwards them and
# adopts the provider's DoneEvent.final.
# ──────────────────────────────────────────────────────────────────────────


def test_stream_simple_end_to_end_tool_call(monkeypatch):
    chunks = _tool_call_chunks(
        [{"id": "call_e2e", "name": "bash", "arguments": {"command": "echo hi", "n": 3}}],
        arg_fragment=3,
    )
    resp = _FakeResponse(_sse(chunks))
    monkeypatch.setattr(OpenAICompletionsProvider, "_get_client", lambda self: _FakeClient(resp))

    async def go():
        stream = await stream_simple(
            model=_model(),
            context={"messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}]},
            options={"api_key": "sk-test"},
        )
        return await stream.result()

    final = asyncio.run(go())
    tcs = [c for c in final.content if isinstance(c, ToolCall)]
    assert len(tcs) == 1
    assert tcs[0].name == "bash"
    assert tcs[0].arguments == {"command": "echo hi", "n": 3}


# ──────────────────────────────────────────────────────────────────────────
# json_parse unit tests
# ──────────────────────────────────────────────────────────────────────────


def test_repair_json_escapes_raw_control_chars():
    raw = '{"text": "line1\nline2"}'  # raw newline inside the string literal
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert json.loads(repair_json(raw)) == {"text": "line1\nline2"}


def test_parse_json_with_repair_raises_on_incomplete():
    with pytest.raises(json.JSONDecodeError):
        parse_json_with_repair('{"command": ')


@pytest.mark.parametrize(
    "partial,expected",
    [
        ('{"command": "ls -l', {"command": "ls -l"}),
        ('{"a": 1, "b": ', {"a": 1}),
        ('{"a": 1, "b": 2}', {"a": 1, "b": 2}),
        ("", {}),
        ("   ", {}),
        ("not json at all", {}),
        ('{"nested": {"x": [1, 2', {"nested": {"x": [1, 2]}}),
    ],
)
def test_parse_streaming_json_best_effort(partial, expected):
    assert parse_streaming_json(partial) == expected


def test_parse_json_with_repair_info_clean_parse_not_repaired():
    value, repaired = parse_json_with_repair_info('{"a": 1}')
    assert value == {"a": 1}
    assert repaired is False


def test_parse_json_with_repair_info_control_char_is_repaired():
    raw = '{"text": "line1\nline2"}'  # raw newline inside the string literal
    value, repaired = parse_json_with_repair_info(raw)
    assert value == {"text": "line1\nline2"}
    assert repaired is True


def test_parse_json_with_repair_info_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_json_with_repair_info('{"command": ')


# ──────────────────────────────────────────────────────────────────────────
# W8/G4 telemetry: llama.cpp `timings` sibling chunk + repair count on Usage
# ──────────────────────────────────────────────────────────────────────────


def test_timings_sibling_chunk_lands_on_usage_extra():
    """llama.cpp emits `timings` as a top-level SIBLING of `usage` on the trailing
    chunk, whose `choices` is empty — both must be read before the empty-choices
    guard or they (and the token counts) are silently dropped."""
    chunks = [
        {"id": "c", "choices": [{"delta": {"content": "hi"}}]},
        {"id": "c", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {
            "id": "c",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "timings": {
                "prompt_n": 5,
                "prompt_ms": 12.3,
                "prompt_per_token_ms": 2.46,
                "prompt_per_second": 406.5,
                "predicted_n": 3,
                "predicted_ms": 45.6,
                "predicted_per_token_ms": 15.2,
                "predicted_per_second": 65.8,
            },
        },
    ]
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    final = done[0].final
    assert final.usage.output_tokens == 3
    assert final.usage.input_tokens == 5
    assert final.usage.extra["timings"] == chunks[2]["timings"]


def test_repair_counted_for_tool_call_with_control_char_in_arguments():
    """A complete tool-arg buffer with a raw control char repairs — and the
    repair is COUNTED (this is the complete-buffer path, not the display-only
    partial-buffer path, where a repair is normal and uncounted)."""
    chunks = _tool_call_chunks(
        [{"id": "call_r", "name": "bash", "arguments": '{"command": "a\nb"}'}],
        arg_fragment=3,
    )
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    final = done[0].final
    tcs = [c for c in final.content if isinstance(c, ToolCall)]
    assert tcs[0].arguments == {"command": "a\nb"}
    assert final.usage.extra["repairs"] == 1


def test_repairs_zero_for_clean_tool_call_arguments():
    """A tool call whose arguments needed no repair reports the real zero — the
    interesting datum a constrained-decoding hypothesis is built on."""
    chunks = _tool_call_chunks(
        [{"id": "call_c", "name": "bash", "arguments": {"command": "ls"}}],
        arg_fragment=3,
    )
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    final = done[0].final
    assert final.usage.extra["repairs"] == 0


def test_no_repairs_key_when_message_has_no_tool_calls():
    """A plain text completion has no tool-arg buffer to measure — `repairs`
    must be ABSENT, not a fabricated 0 (Fail-Early: no lie by omission-of-context)."""
    chunks = [
        {"id": "c", "choices": [{"delta": {"content": "hi"}}]},
        {
            "id": "c",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    events = _run_stream(OpenAICompletionsProvider(api_key="sk-test"), _FakeResponse(_sse(chunks)))
    done = [e for e in events if isinstance(e, DoneEvent)]
    final = done[0].final
    assert "repairs" not in final.usage.extra
