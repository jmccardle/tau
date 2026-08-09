"""Tests for the reasoning/thinking `reasoning_effort` send-path.

Verifies that ``OpenAICompletionsProvider.stream_chat`` translates the τ-internal
``reasoning`` option into an OpenAI ``reasoning_effort`` request-body field,
gated on model capability and clamped to the model's supported levels (pi
``openai-completions.ts:441-442, 620-628`` + ``models.ts`` clamp).

Reference: ROADMAP.md Tier 3 #4; docs/CLI-PLAN.md (deferred → done).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.types import Model, TextContent, UserMessage


def _sse_stream(chunks: list[dict]) -> str:
    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _ok_response() -> MagicMock:
    """A minimal 200 SSE response (one text delta + finish + usage)."""
    chunks = [
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "ok"}}],
        },
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = _sse_stream(chunks)
    response = MagicMock()
    response.status_code = 200
    response.text = body
    response.headers = {"x-request-id": "rid"}

    async def _aiter():
        for line in body.split("\n"):
            yield line

    response.aiter_lines = _aiter
    return response


class _StreamCM:
    """Async context manager mimicking ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _CapturingClient:
    """A fake httpx.AsyncClient that records the JSON payload of the request."""

    last_payload: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def post(self, *args, **kwargs):
        _CapturingClient.last_payload = kwargs.get("json")
        return _ok_response()

    def stream(self, *args, **kwargs):
        _CapturingClient.last_payload = kwargs.get("json")
        return _StreamCM(_ok_response())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


def _model(**overrides) -> Model:
    defaults: dict = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "context_window": 1000,
        "max_tokens": 100,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _run(model: Model, options: dict | None) -> dict:
    """Drive stream_chat to completion and return the captured request payload."""
    _CapturingClient.last_payload = None
    provider = OpenAICompletionsProvider(api_key="sk-test")

    async def _go():
        stream = await provider.stream_chat(
            model=model,
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
            options=options,
        )
        async for _ in stream:
            pass

    asyncio.run(_go())
    assert _CapturingClient.last_payload is not None
    return _CapturingClient.last_payload


def _patch_client(monkeypatch) -> None:
    monkeypatch.setattr("tau_ai.providers.openai.httpx.AsyncClient", _CapturingClient)


def test_reasoning_effort_sent_when_model_reasoning(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=True), {"reasoning": "high"})
    assert payload["reasoning_effort"] == "high"
    # The τ-internal `reasoning` key must never leak into the request body.
    assert "reasoning" not in payload


def test_no_reasoning_effort_when_level_absent(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=True), {"temperature": 0.5})
    assert "reasoning_effort" not in payload


def test_no_reasoning_effort_when_model_not_reasoning(monkeypatch):
    """Fail-Early gate: never send reasoning_effort to a non-reasoning model."""
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=False), {"reasoning": "high"})
    assert "reasoning_effort" not in payload


def test_off_level_not_sent(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=True), {"reasoning": "off"})
    assert "reasoning_effort" not in payload


def test_xhigh_clamped_to_high_without_map(monkeypatch):
    """xhigh requires a thinking_level_map entry; otherwise it clamps to high."""
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=True), {"reasoning": "xhigh"})
    assert payload["reasoning_effort"] == "high"


def test_xhigh_sent_when_mapped(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(
        _model(reasoning=True, thinking_level_map={"xhigh": "max"}),
        {"reasoning": "xhigh"},
    )
    assert payload["reasoning_effort"] == "max"


def test_level_remapped_via_thinking_level_map(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(
        _model(reasoning=True, thinking_level_map={"high": "H"}),
        {"reasoning": "high"},
    )
    assert payload["reasoning_effort"] == "H"


def test_off_value_mapping_sent_for_default_on_model(monkeypatch):
    """A model that thinks by default can be quieted by mapping off→a string."""
    _patch_client(monkeypatch)
    payload = _run(
        _model(reasoning=True, thinking_level_map={"off": "none"}),
        {"reasoning": "off"},
    )
    assert payload["reasoning_effort"] == "none"


# ── Body-fragment level values ───────────────────────────────────────────────
# `reasoning_effort` is one vendor's spelling of "think this hard". A server that
# spells it differently ignores it in silence -- measured against llama.cpp
# b1061-2da6686, where high/low/banana all produced byte-identical generations
# while `thinking_budget_tokens` tracked the budget (docs/probe-results/). A map
# value may therefore be a request-body fragment instead of a string, so the level
# enum stays the stable abstraction while the FIELD carrying it is per-endpoint.


def test_a_fragment_value_lands_as_its_own_keys_not_as_reasoning_effort(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(
        _model(reasoning=True, thinking_level_map={"high": {"thinking_budget_tokens": 4096}}),
        {"reasoning": "high"},
    )
    assert payload["thinking_budget_tokens"] == 4096
    # The point of the whole feature: the level did NOT go out under the key this
    # endpoint ignores.
    assert "reasoning_effort" not in payload


def test_a_fragment_may_be_nested_which_is_why_it_is_not_a_field_name(monkeypatch):
    """The knob that reaches a true zero on llama.cpp is two levels deep.

    `chat_template_kwargs.enable_thinking` is the only measured knob that took
    reasoning to 0 tokens. No flat "name the field" config could address it.
    """
    _patch_client(monkeypatch)
    payload = _run(
        _model(
            reasoning=True,
            thinking_level_map={"off": {"chat_template_kwargs": {"enable_thinking": False}}},
        ),
        {"reasoning": "off"},
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_nested_fragment_merges_with_extra_body_rather_than_replacing_it(monkeypatch):
    """Both settings survive. A flat assignment would drop the static one silently,
    which is the same silent-loss this shape exists to fix one level up."""
    _patch_client(monkeypatch)
    payload = _run(
        _model(
            reasoning=True,
            extra_body={"chat_template_kwargs": {"some_other_flag": True}},
            thinking_level_map={"off": {"chat_template_kwargs": {"enable_thinking": False}}},
        ),
        {"reasoning": "off"},
    )
    assert payload["chat_template_kwargs"] == {"some_other_flag": True, "enable_thinking": False}


def test_the_fragment_wins_over_extra_body_on_a_collision(monkeypatch):
    """Asking for a level is an explicit per-call act; extra_body is the model's
    static default. Matches what the string path always did to reasoning_effort."""
    _patch_client(monkeypatch)
    payload = _run(
        _model(
            reasoning=True,
            extra_body={"thinking_budget_tokens": 99},
            thinking_level_map={"low": {"thinking_budget_tokens": 256}},
        ),
        {"reasoning": "low"},
    )
    assert payload["thinking_budget_tokens"] == 256


def test_a_fragment_cannot_smuggle_a_constraint_key_past_the_gates(monkeypatch):
    """A fragment is a third door into the payload, and a door the constraint gates
    do not watch is a door around them (see _guard_body_keys)."""
    _patch_client(monkeypatch)
    with pytest.raises(ValueError, match="decode-constraint fields"):
        _run(
            _model(reasoning=True, thinking_level_map={"high": {"grammar": "root ::= ok"}}),
            {"reasoning": "high"},
        )


def test_a_string_value_still_means_reasoning_effort(monkeypatch):
    """Every config written before fragments existed is unaffected."""
    _patch_client(monkeypatch)
    payload = _run(_model(reasoning=True, thinking_level_map={"high": "H"}), {"reasoning": "high"})
    assert payload["reasoning_effort"] == "H"
    assert "thinking_budget_tokens" not in payload


def test_a_fragment_is_still_gated_on_declared_reasoning_support(monkeypatch):
    """`Model.reasoning` remains the single capability gate. A map alone does not
    assert the endpoint supports thinking -- that is the inference A2 removed."""
    _patch_client(monkeypatch)
    payload = _run(
        _model(reasoning=False, thinking_level_map={"high": {"thinking_budget_tokens": 4096}}),
        {"reasoning": "high"},
    )
    assert "thinking_budget_tokens" not in payload
