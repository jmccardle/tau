"""G5 — the N-way fan-out identity contract test (§7.1 threat 3 of
docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md).

The retrieval-review shape (JMFTS §9, C1 ``ExtensionContext.complete()``) fans
out N concurrent completions that share a long common prefix (system prompt +
shared context) and differ only in a per-item suffix. That is *exactly* the
property KV-branching (path 1's slot-LCP match today, an explicit fork API
later) needs to actually fire: if the shared prefix is not byte-identical
across the N requests, the server sees N unrelated prompts and forks nothing.

This drives the REAL ``ctx.complete()`` (tau_agent_core/extension_types.py) —
not a stubbed ``complete_simple`` — through a real ``AgentSession``, with only
the HTTP client faked (the same ``_CapturingClient`` pattern as
``tau-llm/tests/test_reasoning_effort.py`` and
``tau-llm/tests/test_prefix_stability.py``, replicated here rather than
imported: tau-agent-core may depend on tau-llm, never the reverse, and tau-llm's
tests are not an importable package). Faking ``complete_simple`` instead would
bypass ``_convert_messages_to_openai`` entirely and prove nothing about the
wire payload.

The within-turn and reasoning_replay tests for the OTHER two §7.1 threats live
in ``tau-llm/tests/test_prefix_stability.py`` (no ``AgentSession`` needed there).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionContext
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

# ──────────────────────────────────────────────────────────────────────────
# Capturing-client harness — see test_prefix_stability.py's copy for the full
# rationale. Every concurrent ``ctx.complete()`` call gets its OWN
# OpenAICompletionsProvider + httpx client instance (tau_llm.client.stream_simple
# builds a fresh, unregistered ``Registry()`` per call — see client.py), so
# payload capture is a CLASS attribute shared across every fake-client
# instance, appended to under whatever interleaving asyncio.gather() produces.
# ──────────────────────────────────────────────────────────────────────────


def _mock_response(text: str) -> MagicMock:
    chunks = [
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": text}}]},
        {
            "id": "c",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = "\n".join(["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"])
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
    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _CapturingClient:
    """Fake httpx.AsyncClient: records every request's JSON payload (class-level
    list, since ``ctx.complete()`` builds a fresh provider+client per call —
    see the module docstring) and always replies with the same canned text
    (the fan-out property under test is about the REQUEST prefix, not the
    response)."""

    payloads: list[dict] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        _CapturingClient.payloads.append(kwargs.get("json"))
        return _mock_response("verdict")

    def stream(self, *args: Any, **kwargs: Any) -> _StreamCM:
        _CapturingClient.payloads.append(kwargs.get("json"))
        return _StreamCM(_mock_response("verdict"))

    async def __aenter__(self) -> "_CapturingClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


def _reset_capture() -> None:
    _CapturingClient.payloads = []


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _CapturingClient)


def _model() -> Model:
    return Model(
        id="primary",
        name="primary",
        api="openai-completions",
        provider="openai",
        base_url="http://x/v1",
        context_window=100_000,
        max_tokens=1000,
    )


def _messages_json(payload: dict, upto: int | None = None) -> str:
    msgs = payload["messages"] if upto is None else payload["messages"][:upto]
    return json.dumps(msgs, separators=(",", ":"))


# The shared prefix every fan-out branch carries verbatim: a system message plus
# a chunk of common retrieved context. `SHARED_PREFIX_LEN` messages of it.
SHARED_PREFIX: list[dict[str, Any]] = [
    {"role": "system", "content": "You are a document reviewer. Answer include or exclude."},
    {"role": "user", "content": "Shared corpus context: quarterly filings batch #42."},
    {"role": "assistant", "content": "Acknowledged; ready to review individual documents."},
]
SHARED_PREFIX_LEN = len(SHARED_PREFIX)


async def test_n_way_fan_out_shares_a_byte_identical_prefix(monkeypatch):
    """N concurrent ``ctx.complete()`` calls sharing ``SHARED_PREFIX`` and
    differing only in one trailing per-item message: every captured request's
    `messages[:SHARED_PREFIX_LEN]` must serialize to the EXACT SAME bytes, and
    the diverging suffix element must be genuinely distinct per item — i.e. the
    fan-out shares a byte-identical prefix up to precisely the point the items
    differ, and no earlier.
    """
    _patch_client(monkeypatch)
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="k")
    ctx = ExtensionContext()
    ctx._session = session
    _reset_capture()

    n = 8
    docs = [f"doc-{i}" for i in range(n)]

    async def _one(doc: str) -> Any:
        messages = [*SHARED_PREFIX, {"role": "user", "content": f"Include {doc}?"}]
        return await ctx.complete(messages)

    results = await asyncio.gather(*(_one(d) for d in docs))

    assert len(results) == n
    captured = _CapturingClient.payloads
    assert len(captured) == n, "every fan-out branch must be a real, separate request"

    # 1. Every request has exactly the shared prefix plus one diverging message.
    for payload in captured:
        assert len(payload["messages"]) == SHARED_PREFIX_LEN + 1

    # 2. The shared PREFIX ARRAY (as a single compact JSON string, i.e. byte for
    #    byte) is identical across every one of the N concurrent requests —
    #    this is the literal property server-side slot-LCP matching depends on.
    prefixes = {_messages_json(p, upto=SHARED_PREFIX_LEN) for p in captured}
    assert len(prefixes) == 1, f"the shared prefix diverged across branches: {prefixes}"

    # 3. The one diverging (per-item) message is genuinely per-item distinct —
    #    otherwise "the prefix matches" would be vacuous (all N requests
    #    identical, not merely prefix-sharing).
    suffixes = {
        json.dumps(p["messages"][SHARED_PREFIX_LEN], separators=(",", ":")) for p in captured
    }
    assert len(suffixes) == n, "the diverging suffix must differ per fan-out branch"


async def test_fan_out_prefix_matches_the_branch_point_serialization(monkeypatch):
    """§7.1 threat 3, stated precisely: the shared prefix every branch sends is
    not just mutually consistent, it is byte-identical to serializing
    ``SHARED_PREFIX`` in isolation — i.e. fanning out doesn't itself perturb the
    branch-point bytes (e.g. via shared mutable state between concurrent
    calls)."""
    _patch_client(monkeypatch)
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="k")
    ctx = ExtensionContext()
    ctx._session = session
    _reset_capture()

    async def _one(doc: str) -> Any:
        messages = [*SHARED_PREFIX, {"role": "user", "content": f"Include {doc}?"}]
        return await ctx.complete(messages)

    await asyncio.gather(*(_one(d) for d in ("alpha", "beta", "gamma")))

    branch_point_json = json.dumps(SHARED_PREFIX, separators=(",", ":"))
    for payload in _CapturingClient.payloads:
        assert _messages_json(payload, upto=SHARED_PREFIX_LEN) == branch_point_json
