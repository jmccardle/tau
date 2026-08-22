"""Backend hardening: the four Fail-Early gaps in the OpenAI-completions provider.

Reference: docs/PLAN-0.9.3.md §4.2 and §4.3, whose diagnosis these tests pin down.
Each one fails against the provider as it stood before that section was built:

1. A tool call whose ``function.name`` never arrives was transcribed with
   ``name=""`` and executed, so the agent loop reported ``Unknown tool: `` —
   blaming the model for a gateway's wire-contract violation.
2. ``str(httpx.ReadTimeout())`` is ``""``, so a dropped connection surfaced as
   ``Streaming error: `` with nothing after the colon.
3. An SSE frame decoding to a list (``data: []``, a keepalive some proxies send)
   hit ``chunk.get(...)`` and raised ``AttributeError`` into the broad handler,
   i.e. straight back into (2).
4. The 300s/10s timeout was hardcoded at client construction with no override
   surface at all, so the operator whose connection was dropping could not tune
   the one number that governs it without editing provider source.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pathlib

import pytest
from pydantic import ValidationError

from tau_llm.providers.openai import (
    OpenAICompletionsProvider,
    _describe_exception,
    _truncate_error_text,
)
from tau_llm.streaming import DoneEvent, ErrorEvent
from tau_llm.types import Model, TextContent, ToolCall, UserMessage

# ──────────────────────────────────────────────────────────────────────────
# SSE test harness (feeds aiter_lines, the way real httpx does). Deliberately a
# copy of test_tool_call_streaming_fix.py's, minus the parts these tests do not
# need and plus a record of the kwargs `stream()` was called with — importing
# across test modules would couple two files that fail for unrelated reasons.
# ──────────────────────────────────────────────────────────────────────────


class _StreamCM:
    def __init__(self, response, raises=None):
        self._response = response
        self._raises = raises

    async def __aenter__(self):
        if self._raises is not None:
            raise self._raises
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, lines, status_code=200, json_body=None, text=None):
        self.status_code = status_code
        self._lines = lines
        self.headers = {"x-request-id": "test-req"}
        self._json_body = json_body if json_body is not None else {"usage": {"total_tokens": 7}}
        self.text = "\n".join(lines) if text is None else text

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """Records every ``stream()`` kwarg, and can raise instead of connecting."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.stream_kwargs: dict = {}

    def stream(self, *args, **kwargs):
        self.stream_kwargs = kwargs
        return _StreamCM(self._response, self._raises)


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


def _run_stream(provider: OpenAICompletionsProvider, client: _FakeClient, **options) -> list:
    async def go():
        provider._get_client = lambda: client  # type: ignore[method-assign]
        stream = await provider.stream_chat(
            model=_model(),
            messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
            options=options or None,
        )
        return [e async for e in stream]

    return asyncio.run(go())


def _provider(**kwargs) -> OpenAICompletionsProvider:
    # base_url matches ``_model()``'s: client.py builds the provider FROM the
    # model's base_url, so a provider pointed somewhere else is not a shape
    # production can produce.
    kwargs.setdefault("base_url", "http://localhost/v1")
    return OpenAICompletionsProvider(api_key="sk-test", **kwargs)


# ──────────────────────────────────────────────────────────────────────────
# (1) A tool call with no function name
# ──────────────────────────────────────────────────────────────────────────


def _nameless_tool_call_chunks() -> list[dict]:
    """The AskSage shape: id and arguments arrive, ``function.name`` never does."""
    return [
        {
            "id": "c",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_nameless",
                                "function": {"name": "", "arguments": '{"command":'},
                            }
                        ]
                    }
                }
            ],
        },
        {
            "id": "c",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": None, "arguments": ' "ls -la"}'}}
                        ]
                    }
                }
            ],
        },
        {"id": "c", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def test_tool_call_with_no_name_is_refused_and_the_message_names_the_cause():
    """Fail-Early: a nameless tool call must not reach the agent loop at all.

    The old behaviour built ``ToolCall(name="")``, which the loop looked up,
    missed, and reported as ``Unknown tool: `` — a message that names neither
    the fault (a gateway that never sent ``function.name``) nor the party at
    fault. The error must be attributable without a packet capture, so it also
    has to carry the call id and the endpoint.
    """
    events = _run_stream(
        _provider(), _FakeClient(_FakeResponse(_sse(_nameless_tool_call_chunks())))
    )

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert not any(isinstance(e, DoneEvent) for e in events)

    message = errors[0].message
    assert "Unknown tool" not in message
    assert "no function name" in message
    assert "call_nameless" in message  # which call
    assert "test-model" in message  # which model
    assert "http://localhost/v1" in message  # which gateway


def test_a_named_tool_call_is_unaffected():
    """The guard must not cost the normal path — the control for the test above."""
    chunks = [
        {
            "id": "c",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ok",
                                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                            }
                        ]
                    }
                }
            ],
        },
        {"id": "c", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _run_stream(_provider(), _FakeClient(_FakeResponse(_sse(chunks))))
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    calls = [c for c in done[0].final.content if isinstance(c, ToolCall)]
    assert [(c.id, c.name, c.arguments) for c in calls] == [("call_ok", "bash", {"command": "ls"})]


def test_a_whitespace_only_tool_call_name_is_refused_too():
    """``" "`` is not a function name; only ``strip()`` catches it."""
    chunks = [
        {
            "id": "c",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ws",
                                "function": {"name": " ", "arguments": ""},
                            }
                        ]
                    }
                }
            ],
        },
        {"id": "c", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _run_stream(_provider(), _FakeClient(_FakeResponse(_sse(chunks))))
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "no function name" in errors[0].message


# ──────────────────────────────────────────────────────────────────────────
# (2) A content-free error message
# ──────────────────────────────────────────────────────────────────────────


def test_httpx_transport_errors_really_do_stringify_to_nothing():
    """The premise of the whole section, asserted rather than assumed."""
    assert str(httpx.ReadTimeout("")) == ""
    assert str(httpx.ConnectError("")) == ""
    assert str(httpx.RemoteProtocolError("")) == ""


def test_a_bare_read_timeout_still_produces_a_message_naming_the_type():
    """A dropped connection used to surface as ``Streaming error: `` — nothing
    after the colon, no type, no endpoint. The type alone is already actionable:
    a ReadTimeout says the body stalled (raise ``request_timeout``), where a
    ConnectError says the endpoint is wrong."""
    events = _run_stream(_provider(), _FakeClient(raises=httpx.ReadTimeout("")))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    message = errors[0].message
    assert message.strip()
    assert not message.rstrip().endswith(":")
    assert "ReadTimeout" in message
    assert "test-model" in message
    assert "http://localhost/v1" in message


def test_describe_exception_always_leads_with_the_type():
    assert _describe_exception(httpx.ConnectError("")) == "ConnectError"
    assert _describe_exception(ValueError("boom")) == "ValueError: boom"


def test_describe_exception_carries_status_and_body_when_they_exist():
    """pi's ``formatProviderError`` shape (utils/error-body.ts:38-135), adapted:
    httpx hangs the status and body off ``e.response``, so one probe does what
    pi needs four SDK-specific field orders for."""
    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    response = httpx.Response(502, text="upstream connect error", request=request)
    described = _describe_exception(
        httpx.HTTPStatusError("boom", request=request, response=response)
    )
    assert "HTTPStatusError" in described
    assert "HTTP 502" in described
    assert "upstream connect error" in described


def test_error_body_is_truncated_not_elided_silently():
    truncated = _truncate_error_text("x" * 5000, max_chars=4000)
    assert truncated.startswith("x" * 4000)
    assert "truncated 1000 chars" in truncated
    assert _truncate_error_text("short", max_chars=4000) == "short"


def test_a_non_json_http_error_body_is_quoted_rather_than_restating_the_status():
    """``HTTP 502: HTTP 502`` said nothing twice. A proxy's HTML error page names
    the hop that failed, which is the only clue an operator gets."""
    response = _FakeResponse([], status_code=502, json_body=None, text="<html>bad gateway</html>")
    events = _run_stream(_provider(), _FakeClient(response))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "502" in errors[0].message
    assert "bad gateway" in errors[0].message


def test_a_string_valued_error_field_does_not_become_an_opaque_streaming_error():
    """Some gateways send ``{"error": "..."}`` rather than ``{"error": {...}}``.
    ``.get()`` on the string raised AttributeError into the broad handler, so a
    perfectly readable 400 arrived as a generic transport failure."""
    response = _FakeResponse([], status_code=400, json_body={"error": "model not deployed"})
    events = _run_stream(_provider(), _FakeClient(response))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "model not deployed" in errors[0].message
    assert "Streaming error" not in errors[0].message


# ──────────────────────────────────────────────────────────────────────────
# (3) A non-object SSE frame
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("keepalive", ["[]", "42", '"ping"', "null"])
def test_a_non_object_sse_frame_is_skipped_not_fatal(keepalive):
    """A keepalive is not an error. pi skips the same shape
    (openai-completions.ts:510); τ used to raise AttributeError on it."""
    lines = [
        'data: {"id": "c", "choices": [{"delta": {"content": "hi"}}]}',
        f"data: {keepalive}",
        'data: {"id": "c", "choices": [{"delta": {}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]
    events = _run_stream(_provider(), _FakeClient(_FakeResponse(lines)))

    assert not any(isinstance(e, ErrorEvent) for e in events)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert "".join(c.text for c in done[0].final.content if isinstance(c, TextContent)) == "hi"


def test_skipped_frames_are_reported_at_debug_level(caplog):
    """Skipping is not swallowing: the same path catches genuinely malformed
    output from a broken gateway, and a turn that produced nothing with no
    record of why is the failure this whole section exists to remove."""
    lines = [
        "data: []",
        "data: {not json at all",
        'data: {"id": "c", "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]
    with caplog.at_level("DEBUG", logger="tau_llm.providers.openai"):
        events = _run_stream(_provider(), _FakeClient(_FakeResponse(lines)))

    assert any(isinstance(e, DoneEvent) for e in events)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "non-object SSE frame" in logged
    assert "undecodable SSE frame" in logged


# ──────────────────────────────────────────────────────────────────────────
# (4) A configurable timeout
# ──────────────────────────────────────────────────────────────────────────


def test_the_default_timeout_is_unchanged():
    """300s read / 10s connect, as hardcoded before — the knob adds an override,
    it does not retime anyone who never sets it."""
    assert _provider().request_timeout == httpx.Timeout(300.0, connect=10.0)


def test_a_constructor_timeout_reaches_the_request():
    client = _FakeClient(_FakeResponse(_sse([{"id": "c", "choices": []}])))
    _run_stream(_provider(request_timeout=45.0), client)
    assert client.stream_kwargs["timeout"] == httpx.Timeout(45.0, connect=10.0)


def test_a_per_call_timeout_option_wins_over_the_provider_default():
    """Per-call rather than stored on the instance: providers are POOLED and
    shared across models by (provider, base_url, key hash), so a stored override
    would silently retime every other caller's requests."""
    provider = _provider(request_timeout=45.0)
    client = _FakeClient(_FakeResponse(_sse([{"id": "c", "choices": []}])))
    _run_stream(provider, client, api_key="sk-test", request_timeout=7.5)

    assert client.stream_kwargs["timeout"] == httpx.Timeout(7.5, connect=10.0)
    assert provider.request_timeout == httpx.Timeout(45.0, connect=10.0)


def test_an_httpx_timeout_object_is_taken_verbatim():
    explicit = httpx.Timeout(20.0, connect=1.0, write=2.0)
    client = _FakeClient(_FakeResponse(_sse([{"id": "c", "choices": []}])))
    _run_stream(_provider(request_timeout=explicit), client)
    assert client.stream_kwargs["timeout"] == explicit


def test_the_timeout_option_never_reaches_the_request_body():
    """``request_timeout`` is an HTTP-client setting; on the wire it would be an
    unknown body field that a strict server 400s on."""
    captured: dict = {}

    class _CapturingClient(_FakeClient):
        def stream(self, *args, **kwargs):
            captured.update(kwargs.get("json", {}))
            return super().stream(*args, **kwargs)

    _run_stream(
        _provider(),
        _CapturingClient(_FakeResponse(_sse([{"id": "c", "choices": []}]))),
        api_key="sk-test",
        request_timeout=12.0,
    )
    assert "request_timeout" not in captured


def test_the_timeout_really_bounds_a_hanging_server_and_says_so():
    """The one test with no fake client in it.

    Every other timeout test asserts the number reaches ``client.stream(...)``;
    this one connects a REAL ``httpx.AsyncClient`` to a socket that accepts and
    then says nothing forever, which is the shape the field report describes.
    It proves the two halves together — the knob bounds the wait, and what comes
    back afterwards is a sentence rather than an empty string.
    """

    async def go():
        # The handler must stall for the whole request and then be releasable,
        # which is not the same thing as stalling forever. From CPython 3.12,
        # ``Server.wait_closed()`` waits for open connections' handlers to
        # finish; a handler awaiting an Event nobody sets therefore hangs the
        # TEARDOWN, not the code under test, and the outer wait_for below turns
        # that into a TimeoutError that reads like the timeout knob failing.
        # Measured directly: wait_closed() returns on 3.11 and hangs on 3.13.
        # This cost the 0.9.3 tag a red matrix.
        release = asyncio.Event()

        async def _accept_and_stall(reader, writer):
            await release.wait()  # never responds while the request is in flight
            writer.close()

        server = await asyncio.start_server(_accept_and_stall, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider = OpenAICompletionsProvider(
            api_key="sk-test", base_url=f"http://127.0.0.1:{port}/v1", request_timeout=0.25
        )
        try:
            stream = await provider.stream_chat(
                model=_model(),
                messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
            )
            return [e async for e in stream]
        finally:
            await provider.aclose()
            release.set()
            server.close()
            await server.wait_closed()

    events = asyncio.run(asyncio.wait_for(go(), timeout=10))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "Timeout" in errors[0].message  # ReadTimeout / ConnectTimeout, not ""
    assert not any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.parametrize("bad", [0, -1, True, "30", object()])
def test_an_unusable_timeout_raises_instead_of_reverting_to_the_default(bad):
    """A silently-ignored timeout is exactly the failure this knob exists to fix:
    the operator tunes a number, sees no change, and looks for the hang
    elsewhere. ``True`` is called out because ``bool`` is an ``int`` subclass and
    would otherwise arrive as a one-second timeout."""
    with pytest.raises(ValueError, match="request_timeout"):
        _provider(request_timeout=bad)


# ── The two cross-agent stitches ─────────────────────────────────────────
#
# Backend hardening and multi-vendor dispatch were built in parallel, each
# owning different files, so each reported a change it needed from the other
# rather than reaching across. These are those two changes.


def test_a_model_can_set_its_own_timeout_without_touching_the_pool() -> None:
    """``Model.request_timeout`` sits between the per-call option and the
    provider default. It is the tier that makes the knob usable: a slow local
    model and a flaky gateway want different patience, and both are configured
    per-model in ~/.tau/config.json."""
    from tau_llm.types import Model

    m = Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="http://x/v1",
        context_window=1,
        max_tokens=1,
        request_timeout=12.5,
    )
    assert m.request_timeout == 12.5

    # Fail Early: a non-positive timeout is unusable, so it is refused at the
    # model rather than silently becoming "no timeout" at the socket.
    with pytest.raises(ValidationError):
        m.model_copy(update={"request_timeout": 0}).model_validate(
            {**m.model_dump(), "request_timeout": 0}
        )


def test_the_answer_is_labelled_with_the_vendor_that_gave_it() -> None:
    """These two fields were hardcoded to openai-completions/openai, so a reply
    from Groq or a private gateway was labelled OpenAI in the transcript and in
    every export. The multi-vendor work widened the types; this is the provider
    honouring them."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "tau_llm"
        / "providers"
        / "openai.py"
    ).read_text()
    # Every construction site reads the model. There used to be one exception —
    # a hardcoded pair inside _convert_openai_choice_to_message, which had no
    # Model in scope and which nothing but tests called. That method has been
    # deleted, so the exception is gone and this asserts zero rather than one.
    assert src.count('api="openai-completions",\n            provider="openai",') == 0
    assert "api=model.api," in src and "provider=model.provider," in src
