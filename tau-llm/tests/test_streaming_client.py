"""The streaming client boundary — ``tau_llm.client.stream_simple`` and
``tau_llm.streaming.AssistantMessageEventStream``.

Previously ``test_subphase3.py`` (35 tests, 1617 lines — the worst LOC-per-test
ratio in the suite). That file drove almost everything through
``stream_simple`` with a hand-rolled ``MagicMock`` HTTP transport repeated
nearly verbatim in six classes, at one assertion per test, and never touched
``AssistantMessageEventStream`` directly — so the class's own contracts (its
``result()`` error paths, its "synthesize a ``DoneEvent`` if the provider
doesn't" fallback, its Fail-Early rejection of a non-event chunk, its
exception-identity preservation) were untested, because no real HTTP response
can produce them; only a hand-built fake provider stream can.

Consolidated to 20 test functions (26 cases) behind ONE parametrizable fake
transport (``_FakeResponse``/``_FakeClient``, real ``aiter_lines`` — a bare
``MagicMock`` silently yields zero lines and was the root cause of a "these
streaming tests don't test what they claim to" note in CODE-QUALITY-NOTES #11).

Coverage (this file alone, `--cov=tau_llm`): streaming.py 81% -> 99% (only the
structurally-unreachable ``RuntimeError`` branch noted below remains), client.py
80% -> 80% (unchanged — the provider-pool/``complete_simple`` logic this file
doesn't touch is already fully covered elsewhere: ``test_provider_lifetime.py``,
``test_complete_simple.py``), providers/openai.py 55% -> 55% (unchanged; the
uncovered lines are grammar/constraint payload-building and final-message/
json-repair internals that are this *provider's* business, already covered by
``test_constraint_verification.py``, ``test_grammar.py``, and
``test_tool_call_streaming_fix.py`` — duplicating them here would be scope
creep for a client/streaming-boundary suite).

Deliberately NOT duplicated here (already covered elsewhere, verified before
writing this file):
  * provider-internal message/tool conversion, per-field delta parsing,
    reasoning-replay scoping — ``test_openai_provider.py``.
  * the fragment-concatenation regression itself, driven directly against
    ``provider.stream_chat`` with index-only follow-up fragments and the
    json-repair/telemetry paths — ``test_tool_call_streaming_fix.py``.
  * the real ``abort_signal`` cooperative-cancellation mechanism —
    ``test_abort.py``.
  * the provider pool (keying, cross-routing, per-loop isolation) and
    ``complete_simple`` — ``test_provider_lifetime.py`` / ``test_complete_simple.py``.

Added: the synthetic chunking axis the parsing bug actually lived on
(docs/TOOL-CALL-PARSING-BUG.md). The old file, and ``test_tool_call_streaming_fix.py``,
each only ever exercised one fixed fragment size; a single-chunk response
happened to mask the bug (``last_args == ""`` on the one chunk that mattered),
which is exactly why the original single-chunk-fixture unit tests passed while
production, talking to a server that fragments char-by-char, corrupted every
tool call. ``test_tool_call_arguments_accumulate_correctly_regardless_of_chunk_shape``
parametrizes the SAME assertions over single-chunk / multi-fragment /
one-character-per-chunk arguments.

Found here and fixed in streaming.py: ``AssistantMessageEventStream.abort()``
documented itself as "abort the stream by propagating to the underlying
provider", but that branch (``hasattr(self._provider_stream, "abort")``) is dead
against the real stack — ``OpenAICompletionsProvider.stream_chat()`` returns a
bare async generator, which has no ``abort`` method, confirmed live in
``test_the_real_provider_stream_has_no_abort_method``. So ``abort()`` cancels the
local collector task and nothing else; it never reaches, and cannot stop, the
in-flight HTTP request. Real cancellation is a wholly separate mechanism: an
``AbortSignal`` threaded through ``options={"abort_signal": ...}`` that the
provider polls internally (``test_abort.py``). The branch was kept as a
duck-typed extension point; the docstring that misdescribed it was corrected.

Left uncovered: ``streaming.py``'s ``result()`` ``RuntimeError`` branch ("Stream
completed without producing a final AssistantMessage") appears structurally
unreachable through the public API — every path that sets ``_done = True``
also sets ``_final`` (or ``_error``/``_error_exc``) in the same branch — so
honestly exercising it would require reaching into private state rather than
driving real behaviour.

Reference: streaming.py, client.py, providers/openai.py;
docs/TOOL-CALL-PARSING-BUG.md, docs/TOOL-CALL-PIPELINE.md.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tau_llm.client import stream_simple
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.streaming import (
    AssistantMessageEventStream,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

# ══════════════════════════════════════════════════════════════════════════
# One fake transport, parametrizable, shared by every HTTP-level test below.
# ══════════════════════════════════════════════════════════════════════════


class _StreamCM:
    """Mimics ``httpx.AsyncClient.stream(...)``: a sync call returning an async
    context manager whose ``__aenter__`` yields the response. The provider does
    ``async with client.stream(...) as response:`` — the mock's ``stream`` must
    be a plain method returning this, NOT a coroutine."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    """A response with a REAL ``aiter_lines`` (unlike a bare ``MagicMock``,
    which silently yields nothing and was why the predecessor file's streaming
    tests never actually exercised the SSE parser)."""

    def __init__(self, chunks, *, status_code=200, error_msg=None, usage=None):
        self.status_code = status_code
        if status_code == 200:
            lines = ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]
            self._json_body = {
                "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
            }
        else:
            lines = []
            self._json_body = {"error": {"message": error_msg}}
        self._lines = lines
        self.text = "\n".join(lines)
        self.headers = {"x-request-id": "test-req"}

    def json(self):
        return self._json_body

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _client_factory(response: "_FakeResponse"):
    """Build a fake ``httpx.AsyncClient`` REPLACEMENT class bound to one
    response, patched in at ``tau_llm.providers.openai.httpx.AsyncClient`` so
    the real ``_get_client()`` still runs (and is covered) rather than being
    overridden itself."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self._response = response

        async def post(self, *args, **kwargs):
            return self._response

        def stream(self, *args, **kwargs):
            return _StreamCM(self._response)

    return _FakeClient


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _ctx(text: str = "hi") -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def _text_chunks(
    pieces: list[str], finish_reason: str = "stop", usage: dict | None = None
) -> list[dict]:
    chunks = [{"id": "c", "choices": [{"delta": {"content": p}}]} for p in pieces]
    chunks.append(
        {
            "id": "c",
            "choices": [{"delta": {}, "finish_reason": finish_reason}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    )
    return chunks


def _tool_call_chunks(
    calls: list[dict], chunking: str = "fragmented", frag_size: int = 4
) -> list[dict]:
    """Build SSE chunks streaming each call's arguments in one of three shapes
    — the axis docs/TOOL-CALL-PARSING-BUG.md lived on:

      "single"     — the whole arguments string in ONE chunk (the shape that
                     masked the original bug: ``last_args == ""`` on the one
                     chunk that mattered, so a wrong reconstruction still
                     happened to parse).
      "fragmented" — split into ``frag_size``-character pieces (the shape a
                     typical OpenAI-compatible server uses).
      "char"       — one character per chunk (the shape a local llama.cpp/vLLM
                     server aggressively fragments to; the worst case for any
                     "assume the cumulative string" mistake).

    A call's ``id`` arrives only on the FIRST delta for that call; follow-up
    deltas (name continuation, every argument fragment) carry only ``index`` —
    exactly like real OpenAI streaming (docs/TOOL-CALL-PIPELINE.md's
    "accumulation contract").
    """
    chunks: list[dict] = [{"id": "c", "choices": [{"delta": {"content": "ok"}}]}]
    for idx, call in enumerate(calls):
        name = call["name"]
        args_str = (
            call["arguments"]
            if isinstance(call["arguments"], str)
            else json.dumps(call["arguments"])
        )
        if chunking == "char":
            name_pieces, arg_pieces = list(name), list(args_str) or [""]
        elif chunking == "single":
            name_pieces, arg_pieces = [name], [args_str] if args_str else [""]
        else:
            name_pieces = [name]
            arg_pieces = [
                args_str[i : i + frag_size] for i in range(0, len(args_str), frag_size)
            ] or [""]

        for j, piece in enumerate(name_pieces):
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
                                        "function": {"name": piece, "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
        for piece in arg_pieces:
            chunks.append(
                {
                    "id": "c",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": idx, "function": {"name": None, "arguments": piece}}
                                ]
                            }
                        }
                    ],
                }
            )
    chunks.append({"id": "c", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    return chunks


def _collect(monkeypatch, response: _FakeResponse) -> list:
    """Drive ``stream_simple`` end to end and return every emitted event."""
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        stream = await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})
        return [e async for e in stream]

    return asyncio.run(go())


def _result(monkeypatch, response: _FakeResponse):
    """Drive ``stream_simple`` and call ``.result()`` without iterating first."""
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        stream = await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})
        return await stream.result()

    return asyncio.run(go())


# ══════════════════════════════════════════════════════════════════════════
# stream_simple: the entry point
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(_text_chunks(["hi"])),
        _FakeResponse(
            _tool_call_chunks([{"id": "c1", "name": "bash", "arguments": {"command": "ls"}}])
        ),
        _FakeResponse([], status_code=401, error_msg="Invalid API key"),
    ],
    ids=["text", "tool-call", "error"],
)
def test_stream_simple_always_returns_an_event_stream(monkeypatch, response):
    """Whatever the provider does — text, tool calls, or an HTTP error —
    ``stream_simple`` always returns the same wrapper type; callers branch on
    events, not on the return type."""
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        return await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})

    assert isinstance(asyncio.run(go()), AssistantMessageEventStream)


# ══════════════════════════════════════════════════════════════════════════
# Text-only streams
# ══════════════════════════════════════════════════════════════════════════


def test_text_only_stream_yields_ordered_deltas_then_a_done_with_usage(monkeypatch):
    """One test standing in for what were six: ordering, delta content, partial
    shape, final text, and usage are all facets of one response, not six."""
    events = _collect(monkeypatch, _FakeResponse(_text_chunks(["Hello", ", ", "world!"])))

    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    done_events = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done_events) == 1
    assert not any(isinstance(e, ToolCallDeltaEvent) for e in events)
    assert events.index(text_events[0]) < events.index(done_events[0])

    assert "".join(e.delta for e in text_events) == "Hello, world!"
    assert isinstance(text_events[0].partial, AssistantMessage)
    assert text_events[0].partial.role == "assistant"

    done = done_events[0]
    final_text = "".join(c.text for c in done.final.content if isinstance(c, TextContent))
    assert final_text == "Hello, world!"
    assert isinstance(done.usage, Usage)
    assert done.usage.total_tokens == 30


# ══════════════════════════════════════════════════════════════════════════
# Tool-call streams: the accumulation regression
#
# docs/TOOL-CALL-PARSING-BUG.md: OpenAI-compatible servers stream tool-call
# arguments as incremental FRAGMENTS that must be concatenated. A prior
# implementation treated each chunk as the complete cumulative string,
# corrupting every multi-chunk tool call while single-chunk unit tests kept
# passing. This is the load-bearing test in this file.
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("chunking", ["single", "fragmented", "char"])
def test_tool_call_arguments_accumulate_correctly_regardless_of_chunk_shape(monkeypatch, chunking):
    """The SAME assertions driven over all three chunk shapes a real server can
    use. The predecessor suite (and the regression suite in
    test_tool_call_streaming_fix.py) each only ever exercised one fixed
    fragment size — this is the axis itself, parametrized."""
    call = {"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la", "cwd": "/tmp"}}
    events = _collect(monkeypatch, _FakeResponse(_tool_call_chunks([call], chunking=chunking)))

    text = [e for e in events if isinstance(e, TextDeltaEvent)]
    toolcalls = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
    done_events = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done_events) == 1
    # Ordering contract (SUBPHASE-0.0 §4): text, then tool-call deltas, then done.
    assert events.index(text[0]) < events.index(toolcalls[0]) < events.index(done_events[0])

    tcs = [c for c in done_events[0].final.content if isinstance(c, ToolCall)]
    assert len(tcs) == 1
    assert tcs[0].id == "call_abc123"
    assert tcs[0].name == "bash"
    assert tcs[0].arguments == {"command": "ls -la", "cwd": "/tmp"}


def test_parallel_tool_calls_route_argument_fragments_by_index(monkeypatch):
    """Follow-up argument fragments carry only `index` (no `id`, no `name`) —
    accumulation must key on `index`, or a second call's fragments corrupt the
    first's in-progress buffer (docs/TOOL-CALL-PIPELINE.md's "accumulation
    contract")."""
    calls = [
        {"id": "call_1", "name": "read_file", "arguments": {"path": "main.py"}},
        {"id": "call_2", "name": "bash", "arguments": {"command": "npm test"}},
    ]
    events = _collect(
        monkeypatch, _FakeResponse(_tool_call_chunks(calls, chunking="fragmented", frag_size=3))
    )
    (done,) = [e for e in events if isinstance(e, DoneEvent)]
    tcs = [c for c in done.final.content if isinstance(c, ToolCall)]
    assert [(t.id, t.name, t.arguments) for t in tcs] == [
        ("call_1", "read_file", {"path": "main.py"}),
        ("call_2", "bash", {"command": "npm test"}),
    ]


def test_tool_call_stream_result_matches_the_done_events_final_message(monkeypatch):
    """``stream.result()`` and the ``DoneEvent`` reached via iteration must agree
    — a caller that only awaits ``result()`` (never iterates) gets the same
    tool call the display path saw."""
    call = {"id": "call_abc123", "name": "bash", "arguments": {"command": "ls"}}
    final = _result(monkeypatch, _FakeResponse(_tool_call_chunks([call])))
    tcs = [c for c in final.content if isinstance(c, ToolCall)]
    assert len(tcs) == 1
    assert tcs[0].arguments == {"command": "ls"}


# ══════════════════════════════════════════════════════════════════════════
# Error events
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "status,error_msg,expect",
    [
        (401, "Invalid API key", "Invalid API key"),
        (429, "Rate limit exceeded.", "Rate limit"),
        (500, "Internal Server Error", "500"),
    ],
    ids=["401-auth", "429-rate-limit", "500-server"],
)
def test_an_http_error_produces_exactly_one_error_event(monkeypatch, status, error_msg, expect):
    """Collapses what were six single-assertion tests (is_error flag, .type,
    message-per-status, "no other events") into one parametrized behaviour: an
    HTTP error is reported as a single ErrorEvent and nothing else."""
    events = _collect(monkeypatch, _FakeResponse([], status_code=status, error_msg=error_msg))
    assert len(events) == 1
    (error,) = events
    assert isinstance(error, ErrorEvent)
    assert error.type == "error"
    assert error.is_error is True
    assert expect in error.message


# ══════════════════════════════════════════════════════════════════════════
# stream.result(): blocks until done, is idempotent, agrees with iteration
# ══════════════════════════════════════════════════════════════════════════


def test_result_blocks_until_done_without_ever_iterating(monkeypatch):
    """The common non-streaming caller (``complete_simple``): await result()
    directly, no ``async for`` in sight."""
    final = _result(monkeypatch, _FakeResponse(_text_chunks(["Hello, world!"])))
    assert isinstance(final, AssistantMessage)
    text = "".join(c.text for c in final.content if isinstance(c, TextContent))
    assert text == "Hello, world!"


def test_result_after_full_iteration_is_idempotent_and_matches_the_done_event(monkeypatch):
    """Whether ``result()`` is awaited before, during, or after iteration, every
    call must return the SAME object as the ``DoneEvent`` the iterator saw —
    not a fresh re-accumulation each time."""
    response = _FakeResponse(_text_chunks(["Hello"]))
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        stream = await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})
        events = [e async for e in stream]
        done = next(e for e in events if isinstance(e, DoneEvent))
        first = await stream.result()
        second = await stream.result()
        return done, first, second

    done, first, second = asyncio.run(go())
    assert done.final is first is second


def test_result_raises_when_the_stream_ended_in_an_http_error(monkeypatch):
    """The ordinary HTTP-error path yields a well-formed ``ErrorEvent`` (no
    exception raised mid-stream) — ``result()`` must still surface it as an
    exception rather than returning ``None`` or a placeholder message."""
    response = _FakeResponse([], status_code=401, error_msg="Invalid API key")
    with pytest.raises(Exception, match="Invalid API key"):
        _result(monkeypatch, response)


# ══════════════════════════════════════════════════════════════════════════
# abort(): what it actually does, and what it doesn't
# ══════════════════════════════════════════════════════════════════════════


def test_abort_after_the_stream_is_exhausted_preserves_partial_and_is_idempotent(monkeypatch):
    response = _FakeResponse(_text_chunks(["Hello"]))
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        stream = await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})
        async for _ in stream:
            pass
        stream.abort()
        stream.abort()  # idempotent — must not raise a second time
        return stream._partial

    assert asyncio.run(go()) is not None


def test_abort_mid_stream_does_not_disrupt_events_already_in_flight(monkeypatch):
    response = _FakeResponse(_text_chunks(["Hello", ", ", "world!"]))
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        stream = await stream_simple(model=_model(), context=_ctx(), options={"api_key": "sk-test"})
        first = await stream.__anext__()
        stream.abort()
        remaining = [e async for e in stream]
        return first, remaining

    first, remaining = asyncio.run(go())
    assert first.type == "text_delta"
    assert any(isinstance(e, DoneEvent) for e in remaining)


def test_the_real_provider_stream_has_no_abort_method(monkeypatch):
    """The finding this section is built around, checked live rather than
    asserted from memory: ``AssistantMessageEventStream.abort()`` only calls
    through to the provider stream when ``hasattr(provider_stream, "abort")``.
    The real provider's stream is a bare async generator (``event_generator()``
    in providers/openai.py), and a bare async generator has no such attribute —
    so that branch is dead code against the real stack. Real cancellation goes
    through ``options={"abort_signal": ...}`` instead (test_abort.py).

    Adjudicated: the branch stays — it is a duck-typed extension point, and the
    fake below proves it works when a provider stream does expose ``abort`` — but
    the method's docstring claimed propagation as its behaviour, which would lead
    a caller to believe ``abort()`` stops the request. That claim is now corrected
    in streaming.py; this test pins the fact it was corrected about."""
    response = _FakeResponse(_text_chunks(["hi"]))
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client_factory(response))

    async def go():
        provider = OpenAICompletionsProvider(api_key="sk-test")
        return await provider.stream_chat(
            model=_model(), messages=[{"role": "user", "content": "hi"}]
        )

    provider_stream = asyncio.run(go())
    assert not hasattr(provider_stream, "abort")


# ══════════════════════════════════════════════════════════════════════════
# AssistantMessageEventStream internals, driven directly.
#
# No real HTTP response can produce these cases — the real provider always
# frames a well-formed terminal DoneEvent/ErrorEvent itself. A hand-built fake
# provider stream is the only way to reach the wrapper's OWN fallback and
# error-preservation logic.
# ══════════════════════════════════════════════════════════════════════════


class _FakeProviderStream:
    """A minimal stand-in for what ``provider.stream_chat()`` returns: an
    async-iterable of typed StreamEvents (or, for the rejection test, an
    intentionally malformed one)."""

    def __init__(self, items, *, raise_after: BaseException | None = None, has_abort: bool = False):
        self._items = list(items)
        self._raise_after = raise_after
        self.abort_calls = 0
        if has_abort:
            self.abort = self._abort  # attached only when requested — hasattr()
            # must observe its absence otherwise (see the abort tests above).

    def _abort(self) -> None:
        self.abort_calls += 1

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item
        if self._raise_after is not None:
            raise self._raise_after


def _partial_message(text: str = "") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        usage=Usage(),
        stop_reason="stop",
        timestamp=0,
    )


async def _drain(stream: AssistantMessageEventStream) -> list:
    return [e async for e in stream]


def test_a_non_event_chunk_is_rejected_rather_than_silently_reinterpreted():
    """``_process_chunk``'s Fail-Early guard: the sole provider never emits a
    bare dict, so one arriving is a contract violation, not data to coerce.
    Surfaces as an ErrorEvent (via ``_collect``'s exception handling), and
    ``result()`` re-raises the ORIGINAL TypeError (see the next test) rather
    than swallowing it."""
    provider_stream = _FakeProviderStream(
        [TextDeltaEvent(delta="hi", partial=_partial_message("hi")), {"not": "an event"}]
    )
    stream = AssistantMessageEventStream(provider_stream, _model())
    events = asyncio.run(_drain(stream))
    assert isinstance(events[-1], ErrorEvent)
    assert "non-event chunk" in events[-1].message


def test_result_reraises_the_original_exception_not_a_flattened_one():
    """The ORIGINAL exception must survive ``result()`` intact (type and
    attributes), not get flattened to ``Exception(str(e))`` — a
    ``ConstraintViolation`` would otherwise lose its ``.output``, and an
    ``httpx`` timeout would become indistinguishable from any other failure."""
    boom = ValueError("upstream exploded")
    stream = AssistantMessageEventStream(_FakeProviderStream([], raise_after=boom), _model())
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(stream.result())
    assert exc_info.value is boom


def test_a_provider_stream_that_never_sends_its_own_done_event_still_gets_one():
    """A well-behaved provider always frames a terminal DoneEvent, but the
    wrapper must not hang (or drop the last partial) if one doesn't arrive —
    it synthesizes one from the last partial seen."""
    partial = _partial_message("partial only")
    provider_stream = _FakeProviderStream([TextDeltaEvent(delta="partial only", partial=partial)])
    stream = AssistantMessageEventStream(provider_stream, _model())
    events = asyncio.run(_drain(stream))
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].final is partial


def test_an_entirely_empty_provider_stream_synthesizes_an_empty_final_message():
    """No chunks at all (not even a partial) — the synthesized DoneEvent must
    still carry a valid, empty AssistantMessage, not None."""
    stream = AssistantMessageEventStream(_FakeProviderStream([]), _model())
    final = asyncio.run(stream.result())
    assert final.content == []
    assert (final.api, final.provider, final.model) == ("openai-completions", "openai", "gpt-4o")


def test_empty_partial_falls_back_to_defaults_when_the_model_lacks_the_usual_fields():
    """The defensive ``hasattr`` fallbacks in ``_make_empty_partial`` are dead
    against every real ``Model`` (which always has ``api``/``provider``/``id``)
    — exercised here with a bare stand-in so the fallback values themselves are
    actually checked at least once."""

    class _BareModel:
        pass

    stream = AssistantMessageEventStream(_FakeProviderStream([]), _BareModel())
    final = asyncio.run(stream.result())
    assert (final.api, final.provider, final.model) == ("openai-completions", "openai", "unknown")


def test_abort_invokes_the_provider_streams_abort_method_when_one_exists():
    """The mechanism as coded: IF the provider stream exposes ``.abort()``,
    calling ``AssistantMessageEventStream.abort()`` invokes it. Nothing in the
    current stack provides such a stream (see
    ``test_the_real_provider_stream_has_no_abort_method`` above) — this proves
    the mechanism would work if one did."""
    provider_stream = _FakeProviderStream([], has_abort=True)
    stream = AssistantMessageEventStream(provider_stream, _model())
    stream.abort()
    stream.abort()
    assert provider_stream.abort_calls == 2


def test_abort_on_a_bare_stream_without_an_abort_method_does_not_raise():
    """Matches the real shape (a plain async generator, no ``.abort``): the
    call must be a safe no-op, not an ``AttributeError``."""

    async def gen():
        if False:  # pragma: no cover - never executes; makes this an async generator
            yield None

    stream = AssistantMessageEventStream(gen(), _model())
    stream.abort()


async def test_abort_cancels_a_still_running_collector_task():
    """Every abort() call above happens after the collector task has already
    finished producing (nothing here does real I/O, so the background task
    outruns the consumer) — none of them reach the task-cancellation branch.
    Block the fake provider stream mid-iteration so the collector is
    genuinely still in flight when abort() is called."""
    never = asyncio.Event()

    async def _gen():
        yield TextDeltaEvent(delta="first", partial=_partial_message("first"))
        await never.wait()  # blocks forever; only cancellation ends this

    class _BlockingProviderStream:
        def __aiter__(self):
            return _gen()

    stream = AssistantMessageEventStream(_BlockingProviderStream(), _model())
    first = await stream.__anext__()
    assert first.delta == "first"
    assert stream._collector_task is not None
    assert not stream._collector_task.done()

    stream.abort()
    await asyncio.sleep(0)  # let the cancellation land
    assert stream._collector_task.cancelled()
