"""Response headers and a structured error reach the bus, an extension and the wire.

Reference: docs/RESPONSE-HEADERS.md §5.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent, ErrorDetail
from tau_agent_core.rpc.wire_events import WireEventProjector, strip_cookies
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_llm.errors import ProviderConnectionError, ProviderHttpError
from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

_HEADERS = {
    "anthropic-ratelimit-requests-remaining": "7",
    "set-cookie": "__cf_bm=secret; path=/",
}


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """A stream_simple result that yields ``events`` in order."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for event in self._events:
                yield event

        return gen()

    def abort(self) -> None:
        pass


def _answer(headers: dict[str, str] | None) -> _Stream:
    final = _assistant("ok")
    return _Stream(
        [
            TextDeltaEvent(delta="ok", partial=final),
            DoneEvent(final=final, usage=final.usage, response_headers=headers),
        ]
    )


def _refusal(exc: Exception) -> _Stream:
    return _Stream([ErrorEvent(message=str(exc), exception=exc)])


def _sub(text: str) -> Submission:
    return Submission(text=text, source="interactive", submitter="human", submission_id=text)


def _session_with_extension() -> tuple[AgentSession, list[AgentEvent]]:
    """A session whose one extension records every message_end and agent_end it is handed."""
    seen: list[AgentEvent] = []

    def governor(api: Any) -> None:
        api.on("message_end", seen.append)
        api.on("agent_end", seen.append)

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[governor]
    )
    return session, seen


def _http_error(status: int, **kw: Any) -> ProviderHttpError:
    return ProviderHttpError(status, message="no", body="", model_id="m", base_url="u", **kw)


async def test_an_extension_reads_the_response_headers_on_message_end():
    session, seen = _session_with_extension()
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=lambda *a, **k: _answer(_HEADERS)
    ):
        await session.submit(_sub("go"))

    (end,) = [e for e in seen if e.type == "message_end"]
    assert end.response_headers == _HEADERS
    assert "response_headers" not in (end.message or {})
    (agent_end,) = [e for e in seen if e.type == "agent_end"]
    assert agent_end.error_detail is None


async def test_the_session_log_never_holds_the_headers():
    session, _ = _session_with_extension()
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=lambda *a, **k: _answer(_HEADERS)
    ):
        await session.submit(_sub("go"))

    assert "ratelimit" not in repr(session.session_log.entries())


async def test_headers_not_captured_stay_none_and_none_sent_stay_empty():
    for headers in (None, {}):
        session, seen = _session_with_extension()
        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=lambda *a, **k: _answer(headers)
        ):
            await session.submit(_sub("go"))
        (end,) = [e for e in seen if e.type == "message_end"]
        assert end.response_headers == headers


async def test_an_extension_reads_a_refusal_as_fields_on_agent_end():
    refusal = _http_error(429, retry_after=3600.0, headers=_HEADERS)
    session, seen = _session_with_extension()
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=lambda *a, **k: _refusal(refusal)
    ):
        with pytest.raises(ProviderHttpError):
            await session.submit(_sub("go"))

    (end,) = [e for e in seen if e.type == "agent_end"]
    assert end.error is not None and end.error.startswith("ProviderHttpError: HTTP 429")
    assert end.error_detail == ErrorDetail(
        type="ProviderHttpError", status=429, retry_after=3600.0, headers=_HEADERS, retryable=True
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(400), ErrorDetail(type="ProviderHttpError", status=400, retryable=False)),
        (_http_error(503), ErrorDetail(type="ProviderHttpError", status=503, retryable=True)),
        (
            ProviderConnectionError("refused", model_id="m", base_url="u"),
            ErrorDetail(type="ProviderConnectionError", retryable=True),
        ),
        (ValueError("bad tool-call JSON"), ErrorDetail(type="ValueError")),
    ],
)
async def test_every_failure_names_its_class_and_only_provider_failures_judge_retry(exc, expected):
    session, seen = _session_with_extension()
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=lambda *a, **k: _refusal(exc)
    ):
        with pytest.raises(type(exc)):
            await session.submit(_sub("go"))

    (end,) = [e for e in seen if e.type == "agent_end"]
    assert end.error_detail == expected


def test_the_wire_carries_headers_without_cookies():
    message_end = AgentEvent(
        type="message_end",
        timestamp=_TS,
        message={"role": "assistant", "content": [], "stop_reason": "stop", "usage": {}},
        response_headers=_HEADERS,
    )
    (wire,) = WireEventProjector().project(message_end)
    assert wire["response_headers"] == {"anthropic-ratelimit-requests-remaining": "7"}
    assert message_end.response_headers == _HEADERS


def test_the_wire_carries_the_error_detail_without_cookies():
    agent_end = AgentEvent(
        type="agent_end",
        timestamp=_TS,
        is_error=True,
        error="ProviderHttpError: HTTP 429",
        error_detail=ErrorDetail.of(_http_error(429, retry_after=2.0, headers=_HEADERS)),
        end_reason="error",
    )
    (wire,) = WireEventProjector().project(agent_end)
    assert wire["error_detail"] == {
        "type": "ProviderHttpError",
        "status": 429,
        "retry_after": 2.0,
        "headers": {"anthropic-ratelimit-requests-remaining": "7"},
        "retryable": True,
    }


def test_strip_cookies_keeps_none_and_returns_a_headerless_event_unchanged():
    bare = AgentEvent(type="message_end", timestamp=_TS)
    assert strip_cookies(bare) is bare
    detail_only = AgentEvent(
        type="agent_end",
        timestamp=_TS,
        error="ValueError",
        error_detail=ErrorDetail(type="ValueError"),
    )
    assert strip_cookies(detail_only) is detail_only
    (wire,) = WireEventProjector().project(bare)
    assert wire["response_headers"] is None and wire["error_detail"] is None
