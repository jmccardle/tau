"""Provider pool / HTTP-client lifetime tests (docs/PROVIDER-LIFETIME.md).

Before this pool existed, ``stream_simple`` built a brand-new
``OpenAICompletionsProvider`` (and therefore a brand-new ``httpx.AsyncClient``)
on EVERY call — no keep-alive, +42 ms/call measured (§3) — and the "obvious"
fix (a bare module-level cache keyed on ``provider_name``) was a SILENT
CROSS-ROUTING BUG: a second model with a different ``base_url``/``api_key``
would reuse the first model's provider, sending its prompt and credentials to
the wrong server (§5).

These tests assert on the real outgoing wire routing (the ``base_url`` and
``Authorization`` header an ``httpx.AsyncClient`` was actually constructed
with) rather than on provider-object identity alone — identity checks alone
would pass even if the pooling key ignored ``base_url``/``api_key`` entirely,
which is exactly the bug this suite exists to catch.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import weakref
from typing import Any
from unittest.mock import MagicMock

import pytest

from tau_llm.client import _POOL, aclose_providers, stream_simple
from tau_llm.types import Model, TextContent, UserMessage


def _sse_stream(text: str) -> str:
    chunks = [
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": text}}]},
        {
            "id": "c",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _ok_response(text: str = "ok") -> MagicMock:
    body = _sse_stream(text)
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


class _RecordingClient:
    """Fake ``httpx.AsyncClient``: records its OWN construction kwargs (the
    ``base_url``/``Authorization`` a real client would route/authenticate
    with) and every instance ever built, so a test can assert both "how many
    distinct clients were built" and "did THIS client see THAT base_url/key".
    """

    instances: list["_RecordingClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.base_url = kwargs.get("base_url")
        self.headers = kwargs.get("headers") or {}
        self.closed = False
        self.requests: list[dict] = []
        _RecordingClient.instances.append(self)

    def stream(self, *args: Any, **kwargs: Any) -> _StreamCM:
        self.requests.append(kwargs.get("json") or {})
        return _StreamCM(_ok_response())

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_pool_and_client(monkeypatch: pytest.MonkeyPatch):
    """Isolate every test: a fresh recorded-client log and an empty provider
    pool (tests run inside their own ``asyncio.run()``, so the loop-keyed pool
    would already be new per test — this also guards against leakage from a
    previous test's loop object being reused by CPython, which does happen)."""
    _RecordingClient.instances = []
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _RecordingClient)
    _POOL.clear()
    yield
    _POOL.clear()


def _model(*, base_url: str, model_id: str = "m") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url=base_url,
        context_window=1000,
        max_tokens=100,
    )


async def _drive(model: Model, api_key: str | None) -> None:
    stream = await stream_simple(
        model,
        {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
        {"api_key": api_key} if api_key is not None else {},
    )
    await stream.result()


def test_cross_routing_two_models_use_different_providers_and_right_wire_identity():
    """The headline regression test: two models with different base_url/api_key
    in ONE process/loop must never share a provider, and each request must
    have been sent through a client carrying THAT model's base_url and
    Authorization header — not the other model's.

    This is the exact trap in docs/PROVIDER-LIFETIME.md §5: a naive
    provider_name-only cache would silently route model B's prompt (and API
    key) to model A's server. Asserting only "two provider objects" would not
    catch that (a buggy keying scheme could still build two providers that
    both point at A). Asserting the actual httpx.AsyncClient construction
    kwargs does.
    """

    async def _go() -> None:
        model_a = _model(base_url="http://host-a.example/v1", model_id="model-a")
        model_b = _model(base_url="http://host-b.example/v1", model_id="model-b")
        await _drive(model_a, "sk-AAA")
        await _drive(model_b, "sk-BBB")

    asyncio.run(_go())

    assert len(_RecordingClient.instances) == 2
    client_a, client_b = _RecordingClient.instances

    assert client_a.base_url == "http://host-a.example/v1"
    assert client_a.headers["Authorization"] == "Bearer sk-AAA"

    assert client_b.base_url == "http://host-b.example/v1"
    assert client_b.headers["Authorization"] == "Bearer sk-BBB"

    # Neither client's wire identity leaked into the other's.
    assert client_a.base_url != client_b.base_url
    assert client_a.headers["Authorization"] != client_b.headers["Authorization"]


def test_same_model_twice_reuses_one_provider_and_one_client():
    """The keep-alive win this pool exists for: identical (provider_name,
    base_url, api_key) across two calls must reuse ONE httpx.AsyncClient, not
    build a fresh one (which is what cost +42 ms/call, §3)."""

    async def _go() -> None:
        model = _model(base_url="http://host.example/v1")
        await _drive(model, "sk-same")
        await _drive(model, "sk-same")

    asyncio.run(_go())

    assert len(_RecordingClient.instances) == 1
    assert len(_RecordingClient.instances[0].requests) == 2


def test_different_api_key_same_endpoint_still_gets_a_distinct_provider():
    """Same base_url, different api_key — must NOT be pooled together, or a
    second caller's request could ride on the first caller's credentials."""

    async def _go() -> None:
        model = _model(base_url="http://host.example/v1")
        await _drive(model, "sk-one")
        await _drive(model, "sk-two")

    asyncio.run(_go())

    assert len(_RecordingClient.instances) == 2
    keys = {c.headers["Authorization"] for c in _RecordingClient.instances}
    assert keys == {"Bearer sk-one", "Bearer sk-two"}


def test_aclose_providers_closes_client_and_empties_pool_then_rebuilds_cleanly():
    async def _go() -> None:
        model = _model(base_url="http://host.example/v1")
        await _drive(model, "sk-x")
        assert len(_RecordingClient.instances) == 1
        first_client = _RecordingClient.instances[0]
        assert first_client.closed is False

        loop = asyncio.get_running_loop()
        assert loop in _POOL
        assert len(_POOL[loop]) == 1

        await aclose_providers()

        assert first_client.closed is True
        # The pool's entry for this loop is gone entirely (not just emptied),
        # matching aclose_providers' contract ("close and DROP").
        assert loop not in _POOL or len(_POOL[loop]) == 0

        # A subsequent call must rebuild cleanly — a fresh provider, a fresh
        # (unclosed) client — not raise or hand back the closed one.
        await _drive(model, "sk-x")
        assert len(_RecordingClient.instances) == 2
        second_client = _RecordingClient.instances[1]
        assert second_client is not first_client
        assert second_client.closed is False

    asyncio.run(_go())


def test_aclose_providers_with_nothing_pooled_is_a_noop():
    async def _go() -> None:
        await aclose_providers()  # must not raise

    asyncio.run(_go())


def test_two_event_loops_do_not_share_a_client():
    """The test suite's per-test ``asyncio.run(...)`` pattern must never hand a
    second loop a client bound to a closed first loop. Two separate
    ``asyncio.run()`` calls (two distinct loops) driving the SAME model must
    each get their OWN client instance."""
    model = _model(base_url="http://host.example/v1")

    asyncio.run(_drive(model, "sk-loop"))
    assert len(_RecordingClient.instances) == 1

    asyncio.run(_drive(model, "sk-loop"))
    assert len(_RecordingClient.instances) == 2

    # A pool-size assertion used to sit here (``len(_POOL) <= 1``), reasoning
    # that the first loop is dead so its entry should be gone. It measured the
    # wrong thing: not what this pool retains, but how soon CPython frees a
    # finished loop — and that changed underneath us. Measured on this tree,
    # after two ``asyncio.run()`` calls: 3.11 leaves 0 loops alive (refcounting
    # frees each one the instant ``run()`` returns), 3.13 leaves 1, and 3.14
    # leaves 2 until a gc pass. Under pytest both survive even a forced
    # ``gc.collect()``, held from C. So the assertion failed on 3.13 and 3.14
    # while the pool was behaving perfectly correctly.
    #
    # The guarantee itself — that the pool cannot keep a dead loop alive — is
    # asserted below, where the test holds the only reference and the timing is
    # ours to control.


def test_the_pool_cannot_keep_a_dead_loop_alive():
    """The pool is keyed weakly, so a loop's entry goes away with the loop.

    This is the property the caller depends on — a long-lived process that runs
    many short-lived loops must not accumulate one provider set per loop
    forever. It is asserted here rather than after ``asyncio.run()`` because
    THIS test holds the only reference to the loop, so dropping it is a decision
    rather than a guess about the collector's schedule. That distinction is what
    makes this pass identically on 3.11, 3.13 and 3.14.
    """
    before = len(_POOL)
    loop = asyncio.new_event_loop()
    _POOL[loop] = {}
    assert len(_POOL) == before + 1

    gone = weakref.ref(loop)
    loop.close()
    del loop
    gc.collect()

    # Checked first: if the loop were still alive, the entry surviving would
    # prove nothing about weak keying, and the assertion below would pass for
    # the wrong reason.
    assert gone() is None, "the loop outlived the test's own reference"
    assert len(_POOL) == before


def test_pool_key_hashes_the_api_key_never_stores_it_raw():
    """§1 of the design: the cache dict must never hold the raw secret as (or
    inside) a key. Every key's third element must be a sha256 hex digest, not
    the plaintext api_key.

    Asserted from INSIDE the running loop, before ``asyncio.run`` returns and
    the loop object goes unreferenced — the pool is a WeakKeyDictionary keyed
    on the loop, so checking after ``asyncio.run`` returns is a race against
    the loop's own collection, not a test of this pool's keying.
    """

    async def _go() -> None:
        model = _model(base_url="http://host.example/v1")
        await _drive(model, "sk-super-secret")

        loop = asyncio.get_running_loop()
        assert len(_POOL) == 1
        providers = _POOL[loop]
        assert len(providers) == 1
        (key,) = providers.keys()
        # Four elements since dispatch landed: ``api`` selects the provider
        # CLASS, so it varies what gets constructed and belongs in the key
        # alongside the vendor, the endpoint and the credential.
        provider_name, api, base_url, key_hash = key
        assert provider_name == "openai"
        assert api == "openai-completions"
        assert base_url == "http://host.example/v1"
        assert key_hash == hashlib.sha256(b"sk-super-secret").hexdigest()
        assert "sk-super-secret" not in key_hash

    asyncio.run(_go())
