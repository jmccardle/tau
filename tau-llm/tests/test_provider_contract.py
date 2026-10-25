"""Provider contract suite, replayed from wire cassettes through each provider's REAL transport.

One behaviour, stated once, checked against all three wires. Each provider is driven
end to end over ``httpx.MockTransport``: the OpenAI provider's own ``httpx`` client,
the ``anthropic`` SDK's, and ``google-genai``'s. That is the difference from the
per-provider test files, which stub the SDK's event objects and so never run the
SDK's own SSE parser against τ's consumer.

The cassettes in ``tests/cassettes/<api>/`` are SYNTHETIC today (``meta.provenance``)
— written from each vendor's documented stream shape, not captured. The plan for
recording them, and for the opt-in live tier, is docs/PROVIDER-COVERAGE.md §5.
A cassette carries its own ``expect`` block, so a recorded replacement states what
it showed rather than what a test hoped for.

``xfail(strict=True)`` marks a contract clause a provider does not meet yet. Strict,
so the fix turns the suite red until the marker is removed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tau_llm.errors import ProviderHttpError
from tau_llm.providers.anthropic import AnthropicMessagesProvider
from tau_llm.providers.google import GoogleGenerativeAIProvider
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.types import Model, UserMessage

CASSETTES = Path(__file__).parent / "cassettes"


class _Tool:
    """The three members ``ToolSpec`` requires."""

    def __init__(self) -> None:
        self.name = "ls"
        self.description = "list a directory"
        self.parameters = {"type": "object", "properties": {"path": {"type": "string"}}}


def _load(api: str, name: str) -> dict[str, Any]:
    return json.loads((CASSETTES / api / f"{name}.json").read_text())


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Serialise cassette events as an SSE body: optional ``event:`` line, one ``data:`` line."""
    out: list[str] = []
    for ev in events:
        if ev.get("event"):
            out.append(f"event: {ev['event']}")
        data = ev["data"]
        out.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        out.append("")
    return ("\n".join(out) + "\n").encode()


class _Replay:
    """An ``httpx.MockTransport`` serving one cassette and keeping the request it got."""

    def __init__(self, cassette: dict[str, Any] | None, status: int = 200, body: Any = None):
        self.requests: list[httpx.Request] = []
        self._cassette = cassette
        self._status = status
        self._body = body
        self._http: Any = httpx

    def transport(self, http: Any = httpx) -> Any:
        """A ``MockTransport`` from ``http`` — ``httpx``, or ``httpx2`` for anthropic>=1.0."""
        self._http = http
        return http.MockTransport(self._handle)

    def _handle(self, request: Any) -> Any:
        self.requests.append(request)
        if self._cassette is None:
            return self._http.Response(self._status, json=self._body)
        resp = self._cassette["response"]
        return self._http.Response(
            resp["status"], headers=resp["headers"], content=_sse_body(resp["sse"])
        )

    def sent_json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


class ProviderContract:
    """What every wire must do. A subclass names its ``api`` and builds a provider on a transport."""

    api: str

    def model(self) -> Model:
        raise NotImplementedError

    def provider(self, replay: _Replay) -> Any:
        raise NotImplementedError

    def _run(self, replay: _Replay, tools: list | None = None) -> list[Any]:
        provider = self.provider(replay)
        messages = [UserMessage(content="hi", timestamp=1)]

        async def drive() -> list[Any]:
            stream = await provider.stream_chat(self.model(), messages, tools, {})
            events = [event async for event in stream]
            await provider.aclose()
            return events

        return asyncio.run(drive())

    def _final(self, name: str, tools: list | None = None) -> tuple[Any, dict[str, Any]]:
        cassette = _load(self.api, name)
        events = self._run(_Replay(cassette), tools)
        done = [e for e in events if getattr(e, "type", None) == "done"]
        assert len(done) == 1, f"expected one done event, got {[e.type for e in events]}"
        return done[0].final, cassette["expect"]

    def test_text_deltas_concatenate_to_the_final_text(self) -> None:
        cassette = _load(self.api, "text")
        events = self._run(_Replay(cassette))
        deltas = "".join(e.delta for e in events if getattr(e, "type", None) == "text_delta")
        final = next(e.final for e in events if getattr(e, "type", None) == "done")
        text = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")
        assert deltas == text == cassette["expect"]["text"]
        assert final.stop_reason == cassette["expect"]["stop_reason"]

    def test_usage_partitions_the_prompt(self) -> None:
        final, expect = self._final("text")
        u = final.usage
        assert (
            u.input_tokens + u.cache_read_tokens + u.cache_write_tokens == expect["prompt_tokens"]
        )
        assert u.cache_read_tokens == expect["cache_read_tokens"]
        assert u.cache_reported is True

    def test_reasoning_tokens_are_reported_where_the_server_reports_them(self) -> None:
        final, expect = self._final("text")
        if "reasoning_tokens" not in expect:
            pytest.skip("this wire's cassette reports no reasoning-token count")
        assert getattr(final.usage, "reasoning_tokens", None) == expect["reasoning_tokens"]

    def test_tool_call_arguments_decode_and_stop_is_tool_use(self) -> None:
        final, expect = self._final("tool_call", tools=[_Tool()])
        calls = [b for b in final.content if getattr(b, "type", None) == "toolCall"]
        assert [(c.name, c.arguments) for c in calls] == [(expect["tool"], expect["arguments"])]
        assert final.stop_reason == expect["stop_reason"]

    def test_output_cap_maps_to_length(self) -> None:
        final, expect = self._final("length")
        assert final.stop_reason == expect["stop_reason"]

    def test_an_unmapped_stop_reason_is_an_error_not_a_stop(self) -> None:
        final, expect = self._final("unmapped_stop")
        assert final.stop_reason == "error", (
            f"{expect['raw_stop']!r} was reported as {final.stop_reason!r}: an answer τ "
            "cannot vouch for is returned as complete"
        )

    def test_a_429_raises_provider_http_error_with_its_status(self) -> None:
        replay = _Replay(None, status=429, body={"error": {"message": "slow down"}})
        with pytest.raises(ProviderHttpError) as excinfo:
            self._run(replay)
        assert excinfo.value.status_code == 429
        assert len(replay.requests) == 1, (
            f"{len(replay.requests)} requests for one call: a retry τ did not decide on"
        )


class TestOpenAICompletionsContract(ProviderContract):
    api = "openai-completions"

    def model(self) -> Model:
        return Model(
            id="gpt-test",
            name="t",
            api=self.api,
            provider="openai",
            base_url="http://llm.test/v1",
            context_window=8192,
            max_tokens=256,
        )

    def provider(self, replay: _Replay) -> Any:
        provider = OpenAICompletionsProvider(api_key="test", base_url="http://llm.test/v1")
        provider._client = httpx.AsyncClient(
            base_url="http://llm.test/v1", transport=replay.transport()
        )
        return provider


class TestAnthropicMessagesContract(ProviderContract):
    api = "anthropic-messages"

    def model(self) -> Model:
        return Model(
            id="claude-test",
            name="t",
            api=self.api,
            provider="anthropic",
            base_url="http://llm.test",
            context_window=200_000,
            max_tokens=256,
        )

    def provider(self, replay: _Replay) -> Any:
        pytest.importorskip("anthropic")
        http = pytest.importorskip("httpx2")
        provider = AnthropicMessagesProvider(api_key="test", base_url="http://llm.test")
        provider._client = provider._get_client().with_options(
            http_client=http.AsyncClient(transport=replay.transport(http))
        )
        return provider

    def test_a_stream_cut_before_message_stop_is_an_error_not_a_done(self) -> None:
        cassette = _load(self.api, "truncated")
        events = self._run(_Replay(cassette))
        kinds = [e.type for e in events]
        assert kinds[-1] == "error" and "done" not in kinds, (
            f"a stream with no message_stop ended as {kinds}: a cut answer is reported complete"
        )
        assert "message_stop" in events[-1].message
        deltas = "".join(e.delta for e in events if e.type == "text_delta")
        assert deltas == cassette["expect"]["partial_text"]


class TestGoogleGenerativeAIContract(ProviderContract):
    api = "google-generative-ai"

    def model(self) -> Model:
        return Model(
            id="gemini-test",
            name="t",
            api=self.api,
            provider="google",
            base_url="http://llm.test",
            context_window=1_000_000,
            max_tokens=256,
        )

    def provider(self, replay: _Replay) -> Any:
        Client = pytest.importorskip("google.genai").Client
        provider = GoogleGenerativeAIProvider(api_key="test", base_url="http://llm.test")
        provider._client = Client(
            api_key="test",
            http_options={
                "base_url": "http://llm.test",
                "httpx_async_client": httpx.AsyncClient(transport=replay.transport()),
            },
        )
        return provider
