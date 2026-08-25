"""Non-streaming backend support: one buffered completion, same events.

Reference: docs/PLAN-0.9.3.md §4.1.

τ hardcoded ``"stream": True`` in the request body AND listed ``stream`` in
``_RESERVED_BODY_KEYS``, so an OpenAI-shaped gateway that does not implement SSE
could not be reached at all — not through config, not through ``extra_body``, not
through per-call options. pi is no help (every chat-capable pi API hardcodes
``stream: true``), so this is a deliberate τ divergence rather than a port.

The contract these tests pin down is *indistinguishability*: for the same logical
response, the non-streaming transport must produce the SAME ``AssistantMessage``
— text, thinking, tool calls, usage, stop reason — through the SAME finalize
path, so the agent loop, the TUI and the RPC layer cannot tell which mode ran.
Including the Fail-Early guards on that path: a nameless tool call and an
undecodable argument buffer must still refuse to become a message.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from tau_llm.constraints import DecodeConstraints
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.types import Model, TextContent, UserMessage

# ──────────────────────────────────────────────────────────────────────────
# Harness. One fake client serves BOTH transports: `stream()` feeds SSE lines,
# `post()` returns a buffered body. Both record the request payload, which is
# what the "`stream` never arrives as a caller-supplied body key" tests read.
# ──────────────────────────────────────────────────────────────────────────


class _StreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, *, lines=None, body=None, status_code=200, text=None):
        self.status_code = status_code
        self._lines = lines or []
        self._body = body
        self.text = (
            text if text is not None else json.dumps(body) if body else "\n".join(self._lines)
        )

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """Serves whichever transport the provider chooses, and records both calls."""

    def __init__(self, stream_response=None, post_response=None):
        self._stream_response = stream_response
        self._post_response = post_response
        self.payload: dict | None = None
        self.stream_calls = 0
        self.post_calls = 0
        self.kwargs: dict = {}

    def stream(self, *args, **kwargs):
        self.stream_calls += 1
        self.kwargs = kwargs
        self.payload = kwargs.get("json")
        return _StreamCM(self._stream_response)

    async def post(self, *args, **kwargs):
        self.post_calls += 1
        self.kwargs = kwargs
        self.payload = kwargs.get("json")
        return self._post_response


def _model(**kwargs) -> Model:
    return Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost/v1",
        context_window=8192,
        max_tokens=1024,
        **kwargs,
    )


def _provider() -> OpenAICompletionsProvider:
    return OpenAICompletionsProvider(api_key="sk-test", base_url="http://localhost/v1")


def _sse(chunks: list[dict]) -> list[str]:
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


def _run(client: _FakeClient, model: Model, **options) -> list:
    provider = _provider()

    async def go():
        provider._get_client = lambda: client  # type: ignore[method-assign]
        stream = await provider.stream_chat(
            model=model,
            messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
            tools=options.pop("tools", None),
            options=options or None,
        )
        return [e async for e in stream]

    return asyncio.run(go())


def _final(events: list) -> DoneEvent:
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1, f"expected exactly one DoneEvent, got {events}"
    return done[0]


def _text(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


# ──────────────────────────────────────────────────────────────────────────
# One logical response, expressed both ways.
#
# Text, reasoning, two parallel tool calls, a finish_reason and a usage block —
# streamed as fragments (the shape a local server actually emits) and buffered
# as one completion object. Everything a consumer sees must match.
# ──────────────────────────────────────────────────────────────────────────

_USAGE = {
    "prompt_tokens": 31,
    "completion_tokens": 12,
    "total_tokens": 43,
    "prompt_tokens_details": {"cached_tokens": 8},
}

_STREAM_CHUNKS = [
    {"id": "resp-1", "choices": [{"delta": {"reasoning_content": "let me "}}]},
    {"id": "resp-1", "choices": [{"delta": {"reasoning_content": "think"}}]},
    {"id": "resp-1", "choices": [{"delta": {"content": "Reading "}}]},
    {"id": "resp-1", "choices": [{"delta": {"content": "both files."}}]},
    {
        "id": "resp-1",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_a", "function": {"name": "read", "arguments": ""}}
                    ]
                }
            }
        ],
    },
    {
        "id": "resp-1",
        "choices": [
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a'}}]}}
        ],
    },
    {
        "id": "resp-1",
        "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]}}],
    },
    {
        "id": "resp-1",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_b",
                            "function": {"name": "read", "arguments": '{"path": "b.txt"}'},
                        }
                    ]
                }
            }
        ],
    },
    {"id": "resp-1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "resp-1", "choices": [], "usage": _USAGE},
]

_COMPLETION_BODY = {
    "id": "resp-1",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Reading both files.",
                "reasoning_content": "let me think",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
                    },
                    {
                        "index": 1,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "b.txt"}'},
                    },
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": _USAGE,
}


def _streamed_events() -> list:
    client = _FakeClient(stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS)))
    return _run(client, _model(stream=True))


def _buffered_events() -> list:
    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    return _run(client, _model(stream=False))


def test_non_streaming_produces_the_same_assistant_message_as_streaming():
    """The whole point: nothing above the provider can tell which mode ran.

    Compared as a full ``model_dump()`` rather than field by field, so a field
    added later is covered by this test on the day it is added — the failure mode
    being guarded against is a SECOND construction site drifting from the first.
    """
    streamed = _final(_streamed_events()).final
    buffered = _final(_buffered_events()).final

    assert buffered.model_dump() == streamed.model_dump()


def test_non_streaming_carries_text_thinking_tool_calls_usage_and_stop_reason():
    """Spelled out, so a failure of the dump comparison above says WHICH half moved."""
    final = _final(_buffered_events()).final

    assert _text(final) == "Reading both files."
    thinking = [b for b in final.content if getattr(b, "type", "") == "thinking"]
    assert [b.thinking for b in thinking] == ["let me think"]
    assert thinking[0].thinking_signature == "reasoning_content"

    calls = final.get_tool_calls()
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("call_a", "read", {"path": "a.txt"}),
        ("call_b", "read", {"path": "b.txt"}),
    ]

    assert final.stop_reason == "toolUse"
    # prompt_tokens 31 includes the 8 cached, so input_tokens is the uncached 23.
    assert final.usage.input_tokens == 23
    assert final.usage.output_tokens == 12
    assert final.usage.total_tokens == 43
    assert final.usage.cache_read_tokens == 8
    assert final.response_id == "resp-1"


def test_non_streaming_usage_matches_streaming_including_llama_timings():
    """Usage arrives in a `stream_options` chunk one way and on the body the other.

    Both go through ``_usage_from_openai``, including llama.cpp's top-level
    ``timings`` sibling — which is exactly the kind of field a second parser
    would have quietly dropped on one path only.
    """
    timings = {"predicted_per_second": 42.5, "prompt_ms": 12.0}

    streamed = _run(
        _FakeClient(
            stream_response=_FakeResponse(
                lines=_sse(
                    [
                        {"id": "r", "choices": [{"delta": {"content": "hi"}}]},
                        {"id": "r", "choices": [], "usage": _USAGE, "timings": timings},
                    ]
                )
            )
        ),
        _model(stream=True),
    )
    buffered = _run(
        _FakeClient(
            post_response=_FakeResponse(
                body={
                    "id": "r",
                    "choices": [
                        {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": _USAGE,
                    "timings": timings,
                }
            )
        ),
        _model(stream=False),
    )

    assert _final(buffered).usage == _final(streamed).usage
    assert _final(buffered).usage.extra["timings"] == timings


def test_non_streaming_emits_the_same_event_vocabulary():
    """A buffered response is adapted into deltas — one per channel, not zero.

    The consumer contract is the event sequence, not just the terminal message:
    the TUI renders from ``TextDeltaEvent``s. A non-streaming turn is exactly the
    shape of a stream that delivered everything in one chunk.
    """
    events = _buffered_events()

    assert [e.delta for e in events if isinstance(e, ThinkingDeltaEvent)] == ["let me think"]
    assert [e.delta for e in events if isinstance(e, TextDeltaEvent)] == ["Reading both files."]
    # One per call as it accumulates, plus the closing one per call that the
    # shared tail emits from the parsed blocks (the streaming path does both too).
    tool_events = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
    assert len(tool_events) == 4
    assert isinstance(events[-1], DoneEvent)
    assert not [e for e in events if isinstance(e, ErrorEvent)]


def test_non_streaming_keeps_thinking_before_text_like_the_stream():
    order = [type(e).__name__ for e in _buffered_events()]
    assert order.index("ThinkingDeltaEvent") < order.index("TextDeltaEvent")


# ──────────────────────────────────────────────────────────────────────────
# The Fail-Early guards on the finalize path must hold in BOTH transports.
# They are the reason there is no second message builder.
# ──────────────────────────────────────────────────────────────────────────


def test_nameless_tool_call_is_refused_in_non_streaming_mode_too():
    """§4.2's guard is not streaming-specific — the gateway that omits
    ``function.name`` omits it on a buffered response as well."""
    events = _run(
        _FakeClient(
            post_response=_FakeResponse(
                body={
                    "id": "r",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_x",
                                        "type": "function",
                                        "function": {"name": "", "arguments": '{"path": "a"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            )
        ),
        _model(stream=False),
    )

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert not [e for e in events if isinstance(e, DoneEvent)]
    assert "no function name" in errors[0].message
    assert "call_x" in errors[0].message
    assert "Unknown tool" not in errors[0].message


def test_undecodable_tool_arguments_are_refused_in_non_streaming_mode_too():
    """The other half of the finalize path's Fail-Early pair. Asserted as PARITY
    with the streaming path rather than against a literal message, because the
    point is that one piece of code produces both."""
    bad_args = "not json at all"
    buffered = _run(
        _FakeClient(
            post_response=_FakeResponse(
                body={
                    "id": "r",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_x",
                                        "function": {"name": "read", "arguments": bad_args},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            )
        ),
        _model(stream=False),
    )
    streamed = _run(
        _FakeClient(
            stream_response=_FakeResponse(
                lines=_sse(
                    [
                        {
                            "id": "r",
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_x",
                                                "function": {"name": "read", "arguments": bad_args},
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        }
                    ]
                )
            )
        ),
        _model(stream=True),
    )

    (buffered_error,) = [e for e in buffered if isinstance(e, ErrorEvent)]
    (streamed_error,) = [e for e in streamed if isinstance(e, ErrorEvent)]
    assert not [e for e in buffered if isinstance(e, DoneEvent)]
    assert buffered_error.message == streamed_error.message


def test_a_buffered_response_with_no_choices_raises_rather_than_returning_silence():
    """A buffered response has no 'maybe the next chunk carries it' excuse."""
    events = _run(
        _FakeClient(post_response=_FakeResponse(body={"id": "r", "choices": []})),
        _model(stream=False),
    )
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "returned no choices" in errors[0].message
    assert not [e for e in events if isinstance(e, DoneEvent)]


def test_a_buffered_choice_with_no_message_object_raises():
    events = _run(
        _FakeClient(
            post_response=_FakeResponse(body={"id": "r", "choices": [{"finish_reason": "stop"}]})
        ),
        _model(stream=False),
    )
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "no `message` object" in errors[0].message


def test_a_buffered_content_that_is_not_a_string_raises_rather_than_being_guessed_at():
    events = _run(
        _FakeClient(
            post_response=_FakeResponse(
                body={
                    "id": "r",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": [{"type": "text"}]},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        ),
        _model(stream=False),
    )
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "message.content" in errors[0].message


def test_a_non_200_reads_the_same_in_both_transports():
    body = {"error": {"message": "model not found"}}
    streamed = _run(
        _FakeClient(
            stream_response=_FakeResponse(
                lines=[], body=body, status_code=404, text=json.dumps(body)
            )
        ),
        _model(stream=True),
    )
    buffered = _run(
        _FakeClient(post_response=_FakeResponse(body=body, status_code=404, text=json.dumps(body))),
        _model(stream=False),
    )

    (streamed_error,) = [e for e in streamed if isinstance(e, ErrorEvent)]
    (buffered_error,) = [e for e in buffered if isinstance(e, ErrorEvent)]
    assert buffered_error.message == streamed_error.message
    assert "HTTP 404" in buffered_error.message
    assert "model not found" in buffered_error.message


# ──────────────────────────────────────────────────────────────────────────
# Mode selection: precedence, validation, and the reserved body key.
# ──────────────────────────────────────────────────────────────────────────


def test_streaming_is_the_default_and_model_stream_false_switches_transport():
    client = _FakeClient(
        stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS)),
        post_response=_FakeResponse(body=_COMPLETION_BODY),
    )
    _run(client, _model())
    assert (client.stream_calls, client.post_calls) == (1, 0)
    assert client.payload["stream"] is True

    client = _FakeClient(
        stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS)),
        post_response=_FakeResponse(body=_COMPLETION_BODY),
    )
    _run(client, _model(stream=False))
    assert (client.stream_calls, client.post_calls) == (0, 1)
    assert client.payload["stream"] is False


def test_the_per_call_option_wins_over_the_model_in_both_directions():
    """Narrowest first — the same precedence ``request_timeout`` uses."""
    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    _run(client, _model(stream=True), stream=False)
    assert (client.stream_calls, client.post_calls) == (0, 1)

    client = _FakeClient(stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS)))
    _run(client, _model(stream=False), stream=True)
    assert (client.stream_calls, client.post_calls) == (1, 0)


def test_stream_options_is_sent_only_on_a_stream():
    """`stream_options.include_usage` is meaningful only ON a stream; OpenAI
    rejects it alongside ``stream: false``, and a buffered body carries usage
    unconditionally."""
    client = _FakeClient(stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS)))
    _run(client, _model(stream=True))
    assert client.payload["stream_options"] == {"include_usage": True}

    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    _run(client, _model(stream=False))
    assert "stream_options" not in client.payload


def test_stream_never_reaches_the_body_as_a_caller_supplied_key():
    """``stream`` stays reserved: the mode is a knob, not a body field.

    A caller-supplied ``stream`` would change the wire format underneath the
    parser that reads it — the request would say one thing and the reader expect
    another. The per-call option is stripped from the body and RESOLVED instead,
    so the value on the wire is always the one τ chose a transport for.
    """
    # Through Model.extra_body: refused, and the message names the real knob.
    with pytest.raises(ValueError) as exc:
        _run(
            _FakeClient(stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS))),
            _model(extra_body={"stream": False}),
        )
    assert "Model.stream" in str(exc.value)

    # Through DecodeConstraints.extra_body: the same door, the same guard.
    with pytest.raises(ValueError) as exc:
        _run(
            _FakeClient(stream_response=_FakeResponse(lines=_sse(_STREAM_CHUNKS))),
            _model(grammar_dialect="llguidance"),
            constraints=DecodeConstraints(choices=["yes", "no"], extra_body={"stream": False}),
        )
    assert "stream" in str(exc.value)

    # Through per-call options: accepted as a MODE, and what lands in the body is
    # τ's resolved boolean — never the caller's object under the same name.
    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    _run(client, _model(), stream=False)
    assert client.payload["stream"] is False
    assert client.post_calls == 1


@pytest.mark.parametrize("bad", ["false", 0, 1, None.__class__])
def test_a_non_bool_stream_setting_raises_instead_of_being_coerced(bad):
    """``stream="false"`` is truthy in Python: coercion would keep streaming
    against a backend that cannot stream, and surface as an unreadable body
    rather than as the bad setting it is."""
    with pytest.raises(ValueError, match="must be a bool"):
        _run(_FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY)), _model(), stream=bad)


def test_model_stream_rejects_a_value_that_cannot_mean_a_mode():
    """The model tier is validated by pydantic at config-load time — a typo in
    ``models.<name>.stream`` names itself there rather than half a turn later."""
    with pytest.raises(ValidationError):
        _model(stream="banana")
    # pydantic's documented lax coercion still accepts the JSON-ish spellings a
    # config file realistically contains, and coerces them to a real bool — so
    # `_resolve_stream_mode` sees a bool from this tier by construction.
    assert _model(stream="false").stream is False


# ──────────────────────────────────────────────────────────────────────────
# The rest of the provider's machinery is transport-agnostic.
# ──────────────────────────────────────────────────────────────────────────


def test_constraints_still_apply_and_are_verified_without_a_stream():
    """Grammars are not streaming-specific. Both gates and the output
    verification sit on the shared path, so a buffered call gets all three."""
    client = _FakeClient(
        post_response=_FakeResponse(
            body={
                "id": "r",
                "choices": [
                    {"message": {"role": "assistant", "content": "yes"}, "finish_reason": "stop"}
                ],
                "usage": _USAGE,
            }
        )
    )
    events = _run(
        client,
        _model(stream=False, grammar_dialect="llguidance"),
        constraints=DecodeConstraints(choices=["yes", "no"]),
    )
    assert "grammar" in client.payload
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert _text(_final(events).final) == "yes"

    # An answer outside the grammar is a ConstraintViolation on this path too —
    # a server that dropped the constraint must not return an unconstrained
    # generation as a constrained one.
    client = _FakeClient(
        post_response=_FakeResponse(
            body={
                "id": "r",
                "choices": [
                    {"message": {"role": "assistant", "content": "maybe"}, "finish_reason": "stop"}
                ],
            }
        )
    )
    with pytest.raises(Exception):
        _run(
            client,
            _model(stream=False, grammar_dialect="llguidance"),
            constraints=DecodeConstraints(choices=["yes", "no"]),
        )


def test_max_tokens_and_the_request_timeout_still_reach_a_buffered_call():
    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    _run(client, _model(stream=False, request_timeout=12.5))
    assert client.payload["max_tokens"] == 1024
    assert client.kwargs["timeout"] == httpx.Timeout(12.5, connect=10.0)


def test_an_already_aborted_turn_is_never_sent():
    """Cancellation, as far as a single round trip can honour it."""

    class _Aborted:
        def is_aborted(self):
            return True

    client = _FakeClient(post_response=_FakeResponse(body=_COMPLETION_BODY))
    events = _run(client, _model(stream=False), abort_signal=_Aborted())

    assert client.post_calls == 0
    final = _final(events).final
    assert final.stop_reason == "aborted"
    assert final.content == []


def test_a_finish_reason_of_length_maps_the_same_way_without_a_stream():
    events = _run(
        _FakeClient(
            post_response=_FakeResponse(
                body={
                    "id": "r",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "trunca"},
                            "finish_reason": "length",
                        }
                    ],
                }
            )
        ),
        _model(stream=False),
    )
    assert _final(events).final.stop_reason == "length"
