"""A request that fails before producing anything is re-sent, visibly and boundedly.

docs/PROVIDER-RETRY.md. Every test drives the real OpenAI provider through an
``httpx.MockTransport`` and counts the requests that reached the "server".
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import types
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest

from tau_llm import (
    AbortSignal,
    ProviderConnectionError,
    ProviderHttpError,
    RetryEvent,
    RetryPolicy,
    complete_simple,
    stream_simple,
)
from tau_llm.errors import connection_error_from, describe_causes, retry_after_seconds
from tau_llm.retry import DEFAULT_RETRY_POLICY, NO_RETRY, resolve_retry_policy
from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.types import Model

FAST = RetryPolicy(base_delay=0.0, jitter=0.0)


def _model(**overrides: Any) -> Model:
    fields: dict[str, Any] = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "local",
        "base_url": "http://llm.test/v1",
        "context_window": 8192,
        "max_tokens": 256,
    }
    fields.update(overrides)
    return Model(**fields)


def _sse(*texts: str) -> bytes:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]} for t in texts
    ]
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    return body.encode()


def _ok(text: str = "hello", *, stream: bool = True) -> httpx.Response:
    if not stream:
        message = {"role": "assistant", "content": text}
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
        )
    return httpx.Response(200, content=_sse(text), headers={"content-type": "text/event-stream"})


def _refusal(status: int, message: str = "busy", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


class _Server:
    """A scripted endpoint: each request takes the next reply, and is recorded."""

    def __init__(self, *replies: httpx.Response | Exception | Callable[[], Any]) -> None:
        self.replies = list(replies)
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Server]:
    """Install a scripted server under the OpenAI provider's httpx client."""
    real_client = httpx.AsyncClient

    def install(*replies: Any) -> _Server:
        server = _Server(*replies)

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(server)
            return real_client(*args, **kwargs)

        monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", client)
        return server

    return install


def _drive(options: dict[str, Any] | None = None, *, stream: bool = True) -> list[Any]:
    async def go() -> list[Any]:
        s = await stream_simple(
            _model(stream=stream),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "not-needed", **(options or {})},
        )
        events = [event async for event in s]
        failure = next((e.exception for e in events if isinstance(e, ErrorEvent)), None)
        if failure is not None:
            raise failure
        return events

    return asyncio.run(go())


@pytest.mark.parametrize("status", [408, 429, 502, 503, 504, 529])
@pytest.mark.parametrize("stream", [True, False])
def test_a_transient_status_is_retried_and_the_answer_arrives(serve, status, stream):
    server = serve(_refusal(status, "model is loading"), _ok("hello", stream=stream))
    events = _drive({"retry": FAST}, stream=stream)

    (retry,) = [e for e in events if isinstance(e, RetryEvent)]
    assert (retry.attempt, retry.max_retries) == (1, 3)
    assert isinstance(retry.error, ProviderHttpError) and retry.error.status_code == status
    assert events.index(retry) == 0
    assert "".join(e.delta for e in events if isinstance(e, TextDeltaEvent)) == "hello"
    assert isinstance(events[-1], DoneEvent)
    assert len(server.bodies) == 2


def test_every_attempt_sends_the_identical_request(serve):
    """A retry must not bust a prompt cache: the second body is byte-for-byte the first."""
    server = serve(_refusal(503), _refusal(503), _ok())
    _drive({"retry": FAST, "temperature": 0.3})
    assert len(server.bodies) == 3
    assert server.bodies[0] == server.bodies[1] == server.bodies[2]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500, 524])
def test_a_permanent_or_ambiguous_status_is_not_retried(serve, status):
    server = serve(_refusal(status, "exceeds the context size"), _ok())
    with pytest.raises(ProviderHttpError) as excinfo:
        _drive({"retry": FAST})
    assert excinfo.value.status_code == status
    assert len(server.bodies) == 1


def test_retries_are_bounded_and_the_last_error_is_raised(serve):
    server = serve(*[_refusal(503, f"try {n}") for n in range(4)])
    events: list[Any] = []

    async def go() -> None:
        s = await stream_simple(
            _model(),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "not-needed", "retry": FAST},
        )
        async for event in s:
            events.append(event)

    asyncio.run(go())
    retries = [e for e in events if isinstance(e, RetryEvent)]
    assert [r.attempt for r in retries] == [1, 2, 3]
    (error,) = [e for e in events if isinstance(e, ErrorEvent)]
    assert isinstance(error.exception, ProviderHttpError)
    assert error.exception.message == "try 3"
    assert len(server.bodies) == 4


def test_complete_simple_retries_and_raises_the_final_refusal_with_its_type(serve):
    server = serve(*[_refusal(429) for _ in range(3)])

    async def go() -> None:
        await complete_simple(
            _model(),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "not-needed", "retry": RetryPolicy(max_retries=2, base_delay=0.0)},
        )

    with pytest.raises(ProviderHttpError) as excinfo:
        asyncio.run(go())
    assert excinfo.value.status_code == 429
    assert len(server.bodies) == 3


def test_retry_false_sends_exactly_once(serve):
    server = serve(_refusal(503), _ok())
    with pytest.raises(ProviderHttpError):
        _drive({"retry": False})
    assert len(server.bodies) == 1


def test_retry_after_replaces_the_backoff(serve):
    serve(_refusal(429, **{"retry-after": "0.05"}), _ok())
    events = _drive({"retry": RetryPolicy(base_delay=5.0)})
    (retry,) = [e for e in events if isinstance(e, RetryEvent)]
    assert retry.delay_seconds == pytest.approx(0.05)


def test_a_retry_after_longer_than_the_cap_fails_at_once(serve):
    server = serve(_refusal(429, "per-minute quota", **{"retry-after": "3600"}), _ok())
    with pytest.raises(ProviderHttpError) as excinfo:
        _drive({"retry": DEFAULT_RETRY_POLICY})
    assert excinfo.value.retry_after == 3600.0
    assert len(server.bodies) == 1


def test_an_abort_during_the_wait_ends_the_stream_as_aborted(serve):
    server = serve(_refusal(503), _ok())
    signal = AbortSignal()

    async def go() -> list[Any]:
        s = await stream_simple(
            _model(),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "not-needed", "abort_signal": signal, "retry": RetryPolicy(base_delay=30)},
        )
        events = []
        async for event in s:
            events.append(event)
            if isinstance(event, RetryEvent):
                signal.abort()
        return events

    events = asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].final.stop_reason == "aborted"
    assert events[-1].final.content == []
    assert len(server.bodies) == 1


def test_an_abort_set_before_the_failure_announces_no_retry(serve):
    """The user already stopped the turn; a "retrying in Ns" notice would contradict them."""
    server = serve(_refusal(503), _ok())
    signal = AbortSignal()
    signal.abort()
    events = _drive({"abort_signal": signal, "retry": FAST})
    assert not [e for e in events if isinstance(e, RetryEvent)]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].final.stop_reason == "aborted"
    assert len(server.bodies) == 1


def test_a_refused_connection_is_retried(serve):
    server = serve(httpx.ConnectError("Connection refused"), _ok("back"))
    events = _drive({"retry": FAST})
    (retry,) = [e for e in events if isinstance(e, RetryEvent)]
    assert isinstance(retry.error, ProviderConnectionError)
    assert "ConnectError: Connection refused" in str(retry.error)
    assert "".join(e.delta for e in events if isinstance(e, TextDeltaEvent)) == "back"
    assert len(server.bodies) == 2


def test_a_read_timeout_is_not_retried(serve):
    """The server may still be computing the first answer; a retry would double it."""
    server = serve(httpx.ReadTimeout("slow"), _ok())
    events = _drive({"retry": FAST})
    assert not [e for e in events if isinstance(e, RetryEvent)]
    (error,) = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.exception is None and "ReadTimeout" in error.message
    assert len(server.bodies) == 1


class _BreaksAfter(httpx.AsyncByteStream):
    """An SSE body that delivers one text frame and then drops the connection."""

    async def __aiter__(self):  # type: ignore[override]
        frame = {"choices": [{"index": 0, "delta": {"content": "half"}, "finish_reason": None}]}
        yield f"data: {json.dumps(frame)}\n\n".encode()
        raise httpx.ReadError("connection reset by peer")


def test_a_failure_after_output_has_streamed_is_not_retried(serve):
    """The caller already holds 'half'; a second attempt would duplicate or contradict it."""
    broken = httpx.Response(
        200, stream=_BreaksAfter(), headers={"content-type": "text/event-stream"}
    )
    server = serve(broken, _ok())
    events = _drive({"retry": FAST})
    assert not [e for e in events if isinstance(e, RetryEvent)]
    assert [e.delta for e in events if isinstance(e, TextDeltaEvent)] == ["half"]
    assert isinstance(events[-1], ErrorEvent)
    assert len(server.bodies) == 1


def test_the_retry_option_never_reaches_the_request_body(serve):
    server = serve(_ok())
    _drive({"retry": FAST})
    assert "retry" not in json.loads(server.bodies[0])


def test_resolve_retry_policy():
    assert resolve_retry_policy(None) is DEFAULT_RETRY_POLICY
    assert resolve_retry_policy(False) is NO_RETRY
    assert resolve_retry_policy(FAST) is FAST
    with pytest.raises(TypeError, match="RetryPolicy or False"):
        resolve_retry_policy(3)
    with pytest.raises(TypeError):
        resolve_retry_policy(True)


def test_a_policy_that_cannot_be_followed_is_refused():
    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError, match="base_delay"):
        RetryPolicy(base_delay=-1.0)


def test_backoff_doubles_up_to_the_ceiling_and_jitter_only_stretches_it():
    policy = RetryPolicy(base_delay=1.0, max_delay=3.0, jitter=0.5)
    err = ProviderHttpError(503, message="", body="", model_id="m", base_url="u")
    assert [policy.delay(n, err, lambda: 0.0) for n in (1, 2, 3, 4)] == [1.0, 2.0, 3.0, 3.0]
    assert policy.delay(1, err, lambda: 0.999) == pytest.approx(1.4995)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, None),
        ({"retry-after": "7"}, 7.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after": "soon"}, None),
        ({"retry-after": "-3"}, 0.0),
        ({"retry-after": "inf"}, None),
        ({"retry-after": "nan"}, None),
        ({"retry-after-ms": "soon", "retry-after": "2"}, 2.0),
    ],
)
def test_retry_after_seconds(headers, expected):
    assert retry_after_seconds(httpx.Headers(headers)) == expected


def test_retry_after_as_an_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    seconds = retry_after_seconds(
        httpx.Headers({"retry-after": format_datetime(when, usegmt=True)})
    )
    assert seconds is not None and 25 < seconds <= 30


def test_a_zoneless_http_date_is_read_as_gmt():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    zoneless = when.strftime("%a, %d %b %Y %H:%M:%S -0000")
    seconds = retry_after_seconds(httpx.Headers({"retry-after": zoneless}))
    assert seconds is not None and 25 < seconds <= 30


def test_the_status_error_carries_retry_after_from_the_response(serve):
    serve(_refusal(503, **{"retry-after": "12"}))
    with pytest.raises(ProviderHttpError) as excinfo:
        _drive({"retry": False})
    assert excinfo.value.retry_after == 12.0


def test_an_anthropic_connection_error_is_recognised_but_its_timeout_is_not(monkeypatch):
    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    sdk = types.SimpleNamespace(
        APIConnectionError=APIConnectionError, APITimeoutError=APITimeoutError
    )
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    kwargs = {"message": "m", "model_id": "m", "base_url": "u"}
    assert connection_error_from(APIConnectionError("down"), **kwargs) is not None
    assert connection_error_from(APITimeoutError("slow"), **kwargs) is None
    assert connection_error_from(ValueError("x"), **kwargs) is None


def _raised_from(outer: BaseException, cause: BaseException) -> BaseException:
    try:
        raise outer from cause
    except BaseException as exc:
        return exc


def test_an_anthropic_connection_error_caused_by_an_import_is_not_retried(monkeypatch):
    """The SDK wraps any send failure; a stale user-site import is not a dropped link."""

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    sdk = types.SimpleNamespace(
        APIConnectionError=APIConnectionError, APITimeoutError=APITimeoutError
    )
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    kwargs = {"message": "m", "model_id": "m", "base_url": "u"}
    imported = _raised_from(ImportError("cannot import name 'sentinel'"), KeyError("asyncio"))
    wrapped = _raised_from(APIConnectionError("Connection error."), imported)
    dropped = _raised_from(APIConnectionError("Connection error."), httpx.ConnectError("refused"))

    assert connection_error_from(wrapped, **kwargs) is None
    assert connection_error_from(dropped, **kwargs) is not None
    assert describe_causes(wrapped) == (
        " <- ImportError: cannot import name 'sentinel' <- KeyError: 'asyncio'"
    )
    assert describe_causes(ValueError("x")) == ""


def _refused_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def _real_sdk_failure(send: Any = None) -> BaseException:
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.AsyncAnthropic(api_key="k", base_url=_refused_url(), max_retries=0)
    if send is not None:
        client._client.send = send
    try:
        await client.messages.create(
            model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
        )
    except anthropic.APIConnectionError as exc:
        return exc
    raise AssertionError("a refused port answered")


async def test_the_real_sdk_refused_connection_is_a_connection_error():
    """anthropic>=1.0 wraps an ``httpx2`` error, not an ``httpx`` one; it must still retry."""
    exc = await _real_sdk_failure()
    kwargs = {"message": "m", "model_id": "m", "base_url": "u"}
    assert connection_error_from(exc, **kwargs) is not None
    links = describe_causes(exc).split(" <- ")[1:]
    assert links and all(a != b for a, b in zip(links, links[1:]))


async def test_the_real_sdk_import_failure_is_not_a_connection_error():
    async def send(*args: Any, **kwargs: Any) -> Any:
        raise ImportError("cannot import name 'sentinel'")

    exc = await _real_sdk_failure(send)
    kwargs = {"message": "m", "model_id": "m", "base_url": "u"}
    assert connection_error_from(exc, **kwargs) is None
    assert "<- ImportError: cannot import name 'sentinel'" in describe_causes(exc)


def test_the_anthropic_sdk_client_is_built_with_its_own_retries_off(monkeypatch):
    """τ's retry is the only one, so every retry is visible (docs/PROVIDER-RETRY.md §4)."""
    built: dict[str, Any] = {}

    class AsyncAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            built.update(kwargs)

    monkeypatch.setitem(
        sys.modules, "anthropic", types.SimpleNamespace(AsyncAnthropic=AsyncAnthropic)
    )
    from tau_llm.providers.anthropic import AnthropicMessagesProvider

    provider = AnthropicMessagesProvider(api_key="k", base_url="https://a.test")
    provider._get_client()
    assert built["max_retries"] == 0
