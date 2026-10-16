"""A provider retry reaches the bus, the wire and the session log as docs/PROVIDER-RETRY.md §5 says.

Drives a real ``AgentSession`` through the real OpenAI provider over an
``httpx.MockTransport``, so the event is the one a head would receive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.provider_retry import retry_notice
from tau_agent_core.rpc.wire_events import WireEventProjector
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm import RetryPolicy
from tau_llm.types import Model


def _model() -> Model:
    return Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="local",
        base_url="http://llm.test/v1",
        context_window=8192,
        max_tokens=256,
    )


def _sse(text: str) -> bytes:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n").encode()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """503 'loading' once, then an answer; the default policy made instant."""
    monkeypatch.setattr("tau_llm.retry.DEFAULT_RETRY_POLICY", RetryPolicy(base_delay=0.0))
    replies = [
        httpx.Response(
            503,
            json={"error": {"message": "Loading model"}},
            headers={"set-cookie": "__cf_bm=x", "x-ratelimit-remaining-requests": "0"},
        ),
        httpx.Response(
            200,
            content=_sse("ready"),
            headers={"content-type": "text/event-stream", "x-ratelimit-remaining-requests": "4"},
        ),
    ]
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", client)
    return seen


def _run(session: AgentSession) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    session.subscribe(events.append)
    asyncio.run(session.prompt("hello"))
    return events


def test_a_retried_turn_emits_provider_retry_and_finishes(server):
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="x")
    events = _run(session)

    (retry,) = [e for e in events if e.type == "provider_retry"]
    assert (retry.attempt, retry.max_retries) == (1, 3)
    assert retry.error_detail is not None and retry.error_detail.status == 503
    assert retry.delay_seconds == 0.0
    assert retry.error is not None and "HTTP 503" in retry.error and "Loading model" in retry.error
    assert retry.is_error is False
    assert retry.submission_id is not None and retry.cursor_id is not None
    types = [e.type for e in events]
    assert types.index("provider_retry") < types.index("message_start")
    assert len(server) == 2

    assistants = [
        e
        for e in session.session_log.entries()
        if (e.get("message") or {}).get("role") == "assistant"
    ]
    assert len(assistants) == 1


def test_the_wire_carries_the_retry_fields(server):
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="x")
    events = _run(session)
    (retry,) = [e for e in events if e.type == "provider_retry"]

    (wire,) = WireEventProjector().project(retry)
    assert wire["type"] == "provider_retry"
    assert (wire["attempt"], wire["max_retries"]) == (1, 3)
    assert "status_code" not in wire and wire["error_detail"]["status"] == 503
    assert wire["delay_seconds"] == 0.0
    assert wire["error"] == retry.error
    assert retry_notice(wire) == retry_notice(retry.model_dump())


def test_retry_notice_reads_as_one_line():
    notice = retry_notice(
        {
            "error": "HTTP 503 from model 'm' at 'u': Loading model",
            "attempt": 2,
            "max_retries": 3,
            "delay_seconds": 2.04,
        }
    )
    assert notice == "HTTP 503 from model 'm' at 'u': Loading model — retry 2 of 3 in 2.0s"


def test_the_headers_reach_the_bus_whole_and_the_wire_without_cookies(server):
    """docs/RESPONSE-HEADERS.md §5, through the real OpenAI provider."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="x")
    events = _run(session)
    (retry,) = [e for e in events if e.type == "provider_retry"]
    (end,) = [e for e in events if e.type == "message_end" and e.response_headers is not None]

    assert retry.error_detail is not None and retry.error_detail.headers is not None
    assert retry.error_detail.headers["set-cookie"] == "__cf_bm=x"
    assert (retry.error_detail.status, retry.error_detail.retryable) == (503, True)
    assert end.response_headers["x-ratelimit-remaining-requests"] == "4"

    projector = WireEventProjector()
    (wire_retry,) = projector.project(retry)
    assert "set-cookie" not in wire_retry["error_detail"]["headers"]
    assert wire_retry["error_detail"]["headers"]["x-ratelimit-remaining-requests"] == "0"
    (wire_end,) = projector.project(end)
    assert wire_end["response_headers"]["x-ratelimit-remaining-requests"] == "4"
