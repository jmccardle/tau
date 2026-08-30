"""Finalizing a stream that ended mid-tool-call.

Reference: docs/PLAN-0.9.4.md §3 ("`Esc` loses the turn"),
docs/TRUNCATED-TOOL-CALLS.md.

Two causes, one branch. The user pressing Esc (``stop_reason="aborted"``) and the
server reaching the output cap τ sends as ``max_tokens``
(``stop_reason="length"``) leave the finalizer the same fact: this argument buffer
is a PREFIX. The second half of this file is the ``"length"`` half, added after
the same data loss was reported again against llama.cpp.

The reported symptom was "nothing persists to disk, due to a JSON parse
traceback". The traceback was not a side effect of the loss — it was the cause.
An abort stops the SSE reader at a line boundary; if a tool call's ``arguments``
were mid-flight, the finalizer joined a buffer it knew to be truncated and handed
it to the STRICT parser, which raised. That raise became an ``ErrorEvent``, then
a ``RuntimeError`` out of the agent loop, and the loop's local list of completed
messages died with the frame.

This file pins both halves of the fix, because either one alone is a defect:

* an ABORTED stream must not raise on a truncated tool-call buffer, and
* a COMPLETE stream must still raise on a malformed one.

The second is the load-bearing half. ``docs/TOOL-CALL-PARSING-BUG.md`` is the
corruption this repo already fixed once, and "parse leniently" is how it comes
back. The condition the finalizer branches on is ``stop_reason == "aborted"`` —
a fact it already had and did not read.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from tau_llm.abort import AbortSignal
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.streaming import DoneEvent, ErrorEvent
from tau_llm.types import Model, TextContent, UserMessage

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class _StreamCM:
    """``httpx.AsyncClient.stream(...)``: a sync call returning an async CM."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


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


def _tool_call_chunk(index: int, *, call_id=None, name=None, arguments=None) -> dict:
    """One SSE frame carrying a fragment of tool call *index*.

    OpenAI streams ``arguments`` as incremental fragments, one piece per chunk
    (CLAUDE.md, "Conventions & gotchas"), which is exactly what makes a truncated
    buffer possible: the abort lands between two of these.
    """
    function: dict = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    delta: dict = {"tool_calls": [{"index": index, "type": "function", "function": function}]}
    if call_id is not None:
        delta["tool_calls"][0]["id"] = call_id
    return {
        "id": "chatcmpl-abort",
        "model": "gpt-4o",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": delta}],
    }


def _finish_chunk(reason: str = "tool_calls") -> dict:
    return {
        "id": "chatcmpl-abort",
        "model": "gpt-4o",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _response(chunks: list[dict], *, abort_after: int | None = None, signal=None) -> MagicMock:
    """A mock 200 response whose ``aiter_lines`` yields *chunks* as SSE lines.

    ``abort_after`` trips *signal* once that many chunk lines have been yielded,
    which is how this file reproduces "the user pressed Esc mid-stream": the
    provider polls the signal once per SSE line, so tripping it between two lines
    is precisely the real timing.
    """
    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")

    async def _aiter():
        for i, line in enumerate(lines):
            if abort_after is not None and i == abort_after and signal is not None:
                signal.abort()
            yield line

    response = MagicMock()
    response.status_code = 200
    response.headers = {"x-request-id": "test-req-id"}
    response.aiter_lines = _aiter
    return response


def _client(response):
    class MockClient:
        def __init__(self, *args, **kwargs):
            self._response = response

        async def post(self, *args, **kwargs):
            return self._response

        def stream(self, *args, **kwargs):
            return _StreamCM(self._response)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return MockClient


def _run(monkeypatch, response, *, abort_signal=None) -> list:
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _client(response))
    provider = OpenAICompletionsProvider(api_key="sk-test")

    async def _go():
        # The signal rides in ``options``, which is how ``AgentLoop`` passes it
        # (agent_loop.py, "Forward the abort signal so an abort mid-completion
        # stops the LLM stream").
        stream = await provider.stream_chat(
            model=_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
            options={"abort_signal": abort_signal} if abort_signal is not None else None,
        )
        return [event async for event in stream]

    return asyncio.run(_go())


def _done(events) -> DoneEvent:
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1, f"expected exactly one DoneEvent, got {len(done)}"
    return done[0]


# ---------------------------------------------------------------------------
# The aborted stream
# ---------------------------------------------------------------------------


def test_an_abort_mid_arguments_finalizes_instead_of_erroring(monkeypatch):
    """The reported bug, at its source.

    The buffer at the moment of the abort is ``{"path": "/etc/pas`` — unterminated
    string, unclosed object. ``repair_json`` cannot help: it fixes control
    characters and bad escapes, not truncation. Before the fix this raised, and
    the raise is what cost the user the rest of the turn.
    """
    signal = AbortSignal()
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments=""),
        _tool_call_chunk(0, arguments='{"path"'),
        _tool_call_chunk(0, arguments=': "/etc/pas'),
        _tool_call_chunk(0, arguments='swd"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks, abort_after=3, signal=signal), abort_signal=signal)

    assert not [e for e in events if isinstance(e, ErrorEvent)], (
        "an aborted stream produced an ErrorEvent; this is the defect — the error "
        "becomes a RuntimeError in the agent loop and takes the whole turn with it"
    )
    assert _done(events).final.stop_reason == "aborted"


def test_the_truncated_call_is_dropped_rather_than_repaired(monkeypatch):
    """Not raising is only half of it: it must not become an executable call.

    A half-streamed ``{"path": "/etc/pas`` repaired into *something* would be a
    tool call the model never issued, run against arguments it never finished
    choosing — after the user asked for the turn to stop. Dropping loses nothing,
    because the call was never made.
    """
    signal = AbortSignal()
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments=""),
        _tool_call_chunk(0, arguments='{"path": "/etc/pas'),
        _tool_call_chunk(0, arguments='swd"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks, abort_after=2, signal=signal), abort_signal=signal)

    final = _done(events).final
    assert final.get_tool_calls() == []
    # …and the omission is stated rather than silent.
    assert final.usage.extra.get("dropped_partial_tool_calls") == 1


def test_a_tool_call_completed_before_the_abort_survives_it(monkeypatch):
    """ "To the extent possible" — the earlier call is complete, so it is kept.

    This is the difference between "the abort dropped what it had to" and "the
    abort threw the message away". A message with two calls, aborted during the
    second, keeps the first.
    """
    signal = AbortSignal()
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments='{"path": "a.txt"}'),
        _tool_call_chunk(1, call_id="call_2", name="write", arguments='{"path": "b'),
        _tool_call_chunk(1, arguments='.txt"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks, abort_after=2, signal=signal), abort_signal=signal)

    calls = _done(events).final.get_tool_calls()
    assert [c.name for c in calls] == ["read"]
    assert calls[0].arguments == {"path": "a.txt"}


def test_text_streamed_before_the_abort_survives_it(monkeypatch):
    """The assistant's partial answer is real content and is kept."""
    signal = AbortSignal()
    chunks = [
        {
            "id": "chatcmpl-abort",
            "model": "gpt-4o",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "choices": [{"index": 0, "delta": {"content": "Reading the file"}}],
        },
        _tool_call_chunk(0, call_id="call_1", name="read", arguments='{"pa'),
        _tool_call_chunk(0, arguments='th": "a.txt"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks, abort_after=2, signal=signal), abort_signal=signal)

    final = _done(events).final
    assert any(getattr(b, "text", "") == "Reading the file" for b in final.content)


def test_a_call_aborted_before_its_name_arrived_is_dropped_not_reported(monkeypatch):
    """The nameless-call guard must not fire on an abort.

    A missing ``function.name`` on a COMPLETE stream is a gateway violating the
    wire contract, and the finalizer raises a long diagnostic about it
    (PLAN-0.9.3.md §4.2). On an abort it is just a call whose first chunks had not
    arrived, and that diagnostic would blame a gateway for the user's Esc.
    """
    signal = AbortSignal()
    chunks = [
        _tool_call_chunk(0, call_id="call_1", arguments=""),
        _tool_call_chunk(0, name="read", arguments='{"path": "a.txt"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks, abort_after=1, signal=signal), abort_signal=signal)

    assert not [e for e in events if isinstance(e, ErrorEvent)]
    final = _done(events).final
    assert final.get_tool_calls() == []
    assert final.usage.extra.get("dropped_partial_tool_calls") == 1


# ---------------------------------------------------------------------------
# The complete stream: strictness is unchanged
# ---------------------------------------------------------------------------


def test_a_complete_stream_with_a_truncated_buffer_still_errors(monkeypatch):
    """The half that must not regress.

    Same unfinishable buffer, no abort. Here the model really did emit malformed
    JSON, and fabricating arguments for it is the corruption bug in
    docs/TOOL-CALL-PARSING-BUG.md. The finalizer's strictness is the fix for that
    and stays exactly as it was.
    """
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments='{"path": "/etc/pas'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks))

    assert [e for e in events if isinstance(e, ErrorEvent)], (
        "a COMPLETE stream carrying malformed tool arguments must still error; "
        "the abort branch is meant to be narrow, not a lenient parser"
    )


def test_a_complete_stream_with_a_nameless_call_still_errors(monkeypatch):
    """The other strict guard, likewise unchanged."""
    chunks = [
        _tool_call_chunk(0, call_id="call_1", arguments='{"path": "a.txt"}'),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors, "a nameless tool call on a complete stream must still be refused"


def test_an_empty_argument_buffer_still_means_no_arguments(monkeypatch):
    """``{}`` is a real answer on a complete stream and a guess on an aborted one.

    A tool taking no arguments streams an empty buffer and must keep working, so
    the abort branch cannot simply treat "empty" as "dropped" everywhere.
    """
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="list", arguments=""),
        _finish_chunk(),
    ]
    events = _run(monkeypatch, _response(chunks))

    calls = _done(events).final.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert "dropped_partial_tool_calls" not in _done(events).final.usage.extra


def test_a_clean_run_reports_no_dropped_calls(monkeypatch):
    """The counter is absent, not zero, when nothing was lost.

    "Absent" and "zero" answer different questions, and a reader of a persisted
    transcript can only tell them apart if the field is not written on the happy
    path.
    """
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments='{"path": "a.txt"}'),
        _finish_chunk(),
    ]
    final = _done(_run(monkeypatch, _response(chunks))).final
    assert "dropped_partial_tool_calls" not in final.usage.extra


@pytest.mark.parametrize("abort_after", [0, 1, 2, 3, 4])
def test_no_abort_position_produces_an_error_event(monkeypatch, abort_after):
    """Sweep the whole stream: there is no line where Esc costs the turn.

    The bug only appeared when the abort landed inside ``arguments``, which is a
    narrow window — narrow enough that a single-position test would pass while
    the defect was still there for a different-length tool name.
    """
    signal = AbortSignal()
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments=""),
        _tool_call_chunk(0, arguments='{"path"'),
        _tool_call_chunk(0, arguments=': "/etc/pas'),
        _tool_call_chunk(0, arguments='swd"}'),
        _finish_chunk(),
    ]
    events = _run(
        monkeypatch,
        _response(chunks, abort_after=abort_after, signal=signal),
        abort_signal=signal,
    )
    assert not [e for e in events if isinstance(e, ErrorEvent)], (
        f"aborting after SSE line {abort_after} produced an ErrorEvent"
    )


# ---------------------------------------------------------------------------
# The stream the SERVER cut off: stop_reason == "length"
# ---------------------------------------------------------------------------


def test_a_length_truncation_mid_arguments_finalizes_instead_of_erroring(monkeypatch):
    """The same loss as the abort bug, reached by the other road.

    Reported against llama.cpp: a repeating
    ``JSONDecodeError: Unterminated string starting at: line 1 column 12`` that
    killed the turn. Column 12 is the opening quote of a 7-character first key
    (``{"command":"…``), so the buffer ends inside the bash tool's command string.

    Nothing about that payload is malformed. The server stopped because
    generation reached the cap τ sends as ``max_tokens``, which makes the buffer a
    PREFIX — the same thing ``stop_reason="length"`` already means for a
    constrained generation, where ``stream_chat`` raises ConstraintViolation
    rather than pretending the constraint completed.
    """
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="bash", arguments='{"command":"find / -na'),
        _finish_chunk("length"),
    ]
    events = _run(monkeypatch, _response(chunks))

    assert not [e for e in events if isinstance(e, ErrorEvent)], (
        "a generation the server cut off at the output cap must not kill the turn"
    )
    final = _done(events).final
    assert final.stop_reason == "length"
    assert final.get_tool_calls() == []
    assert final.usage.extra["dropped_partial_tool_calls"] == 1


def test_text_written_before_the_cap_survives_it(monkeypatch):
    """What the raise used to cost. The answer already streamed is completed work."""
    chunks = [
        {
            "id": "chatcmpl-abort",
            "model": "gpt-4o",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "choices": [{"index": 0, "delta": {"content": "Let me search for it."}}],
        },
        _tool_call_chunk(0, call_id="call_1", name="bash", arguments='{"command":"find / -na'),
        _finish_chunk("length"),
    ]
    final = _done(_run(monkeypatch, _response(chunks))).final

    assert "Let me search for it." in "".join(
        b.text for b in final.content if getattr(b, "type", "") == "text"
    )


def test_a_call_completed_before_the_cap_still_runs(monkeypatch):
    """Dropping is per-call. A call whose arguments closed is a call the model made."""
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="read", arguments='{"path": "a.txt"}'),
        _tool_call_chunk(1, call_id="call_2", name="bash", arguments='{"command":"find / -na'),
        _finish_chunk("length"),
    ]
    final = _done(_run(monkeypatch, _response(chunks))).final

    calls = final.get_tool_calls()
    assert [c.name for c in calls] == ["read"]
    assert final.usage.extra["dropped_partial_tool_calls"] == 1


def test_a_call_cut_off_before_its_name_arrived_is_dropped_too(monkeypatch):
    """A cap can land before ``function.name``, and an unroutable call is not a
    gateway violating the wire contract here — it is the same truncation."""
    chunks = [
        _tool_call_chunk(0, call_id="call_1", arguments='{"comm'),
        _finish_chunk("length"),
    ]
    events = _run(monkeypatch, _response(chunks))

    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert _done(events).final.usage.extra["dropped_partial_tool_calls"] == 1


def test_a_complete_stream_names_the_tool_call_it_refuses(monkeypatch):
    """The strict path stays strict, and now says what it refused.

    ``str(JSONDecodeError)`` is "Unterminated string starting at: line 1 column
    12" — it names neither the tool nor the buffer, so a run of these could not be
    attributed to a tool at all. Every other guard in the finalizer quotes both;
    this one used to be the exception.
    """
    chunks = [
        _tool_call_chunk(0, call_id="call_1", name="bash", arguments='{"command":"find / -na'),
        _finish_chunk("tool_calls"),
    ]
    errors = [e for e in _run(monkeypatch, _response(chunks)) if isinstance(e, ErrorEvent)]

    assert len(errors) == 1
    message = errors[0].message
    assert "call_1" in message
    assert "bash" in message
    assert '{"command":"find / -na' in message
    assert "'toolUse'" in message, "the stop_reason is the evidence that it was NOT cut off"
