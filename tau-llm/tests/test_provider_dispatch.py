"""Provider dispatch: which class serves a model, and which endpoint it reaches.

Before this, ``tau_llm.client`` constructed an ``OpenAICompletionsProvider`` for
every model there has ever been. ``model.provider`` was a cache-key component
and nothing else, and ``model.api`` was not read at all — so a model declaring
``api="openai-responses"``, a protocol τ has never implemented, was quietly
served over the completions wire. The type system was no help either: an
``AssistantMessage`` could only ever say ``provider="openai"``, so τ could not
even record honestly which vendor had answered.

These tests hold the three properties that fix is worth having for:

* a second vendor, named something other than "openai", is reachable end to end
  — its own endpoint, its own credential, its own name on the answer;
* an api τ has no implementation for RAISES, naming what is registered, and
  builds nothing (the old silent fall-through is the bug, not the safety net);
* a registered vendor whose credential is missing raises rather than letting
  ``OPENAI_API_KEY`` leave for someone else's server.

They assert on the wire — the ``base_url`` and ``Authorization`` an
``httpx.AsyncClient`` was actually constructed with — for the same reason
``test_provider_lifetime.py`` does: object identity alone would pass even if
the routing were wrong.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from unittest.mock import MagicMock

import pytest

from tau_llm.client import _POOL, stream_simple
from tau_llm.providers import (
    Provider,
    ProviderSpec,
    get_provider_spec,
    register_api,
    register_provider,
    registered_apis,
    registered_providers,
    unregister_api,
    unregister_provider,
)
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage, UserMessage

# ──────────────────────────────────────────────────────────────────────────
# A recording HTTP transport, so "did vendor B's request carry vendor B's
# base_url and key" is answerable.
# ──────────────────────────────────────────────────────────────────────────


def _sse(text: str) -> str:
    chunks = [
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": text}}]},
        {
            "id": "c",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    return "\n".join(["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"])


class _StreamCM:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _RecordingClient:
    """Fake ``httpx.AsyncClient`` that records its own construction kwargs."""

    instances: list["_RecordingClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.base_url = kwargs.get("base_url")
        self.headers = kwargs.get("headers") or {}
        self.closed = False
        _RecordingClient.instances.append(self)

    def stream(self, *args: Any, **kwargs: Any) -> _StreamCM:
        body = _sse("ok")
        response = MagicMock()
        response.status_code = 200
        response.text = body
        response.headers = {"x-request-id": "rid"}

        async def _aiter() -> AsyncIterator[str]:
            for line in body.split("\n"):
                yield line

        response.aiter_lines = _aiter
        return _StreamCM(response)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Empty pool, empty transport log, and no ambient credentials.

    ``OPENAI_API_KEY`` is deleted rather than ignored: half of what is asserted
    here is that one vendor's key never reaches another vendor's server, and a
    developer machine that happens to export one would otherwise make those
    tests pass for the wrong reason.
    """
    _RecordingClient.instances = []
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _RecordingClient)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _POOL.clear()
    yield
    _POOL.clear()


@pytest.fixture
def acme() -> Any:
    """A second vendor — the six declarative lines a thin vendor costs."""
    register_provider(
        ProviderSpec(
            id="acme",
            name="Acme Inference",
            api="openai-completions",
            base_url="https://api.acme.example/v1",
            api_key_env=("ACME_API_KEY",),
        )
    )
    yield
    unregister_provider("acme")


def _model(**overrides: Any) -> Model:
    fields: dict[str, Any] = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "http://host.example/v1",
        "context_window": 1000,
        "max_tokens": 100,
    }
    fields.update(overrides)
    return Model(**fields)


def _drive(model: Model, options: dict[str, Any] | None = None) -> AssistantMessage:
    async def _go() -> AssistantMessage:
        stream = await stream_simple(
            model,
            {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
            options or {},
        )
        return await stream.result()

    return asyncio.run(_go())


# ──────────────────────────────────────────────────────────────────────────
# 1. A second vendor, end to end.
# ──────────────────────────────────────────────────────────────────────────


def test_a_second_vendor_is_reachable_end_to_end(acme, monkeypatch):
    """The headline: a model whose provider is NOT "openai" reaches that
    vendor's endpoint with that vendor's credential, and the answer says so."""
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")

    final = _drive(_model(provider="acme", base_url="https://api.acme.example/v1"))

    (client,) = _RecordingClient.instances
    assert client.base_url == "https://api.acme.example/v1"
    assert client.headers["Authorization"] == "Bearer acme-secret"
    assert final.api == "openai-completions"


def test_an_assistant_message_can_name_a_vendor_that_is_not_openai():
    """The widening, on its own. ``AssistantMessage.provider`` was
    ``Literal["openai"]`` while ``Model.provider`` has always been ``str``, so
    the one place a Model's vendor flows onto a message (streaming.py:301)
    raised ValidationError for every legal non-OpenAI Model. Constructing this
    message was impossible before; it is the record of who answered."""
    message = AssistantMessage(
        content=[TextContent(text="hi")],
        api="acme-chat",
        provider="acme",
        model="acme-1",
        stop_reason="stop",
        timestamp=0,
    )
    assert (message.provider, message.api) == ("acme", "acme-chat")


def test_the_openai_client_labels_the_answer_with_the_vendor_that_gave_it(acme, monkeypatch):
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")
    final = _drive(_model(provider="acme", base_url="https://api.acme.example/v1"))
    assert final.provider == "acme"


def test_a_registered_vendor_supplies_the_endpoint_a_model_omits(acme, monkeypatch):
    """``base_url`` on the Model wins; the vendor's default fills the gap. It is
    a gap that used to be filled with ``https://api.openai.com/v1`` — an Acme
    model pointed at OpenAI."""
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")

    _drive(_model(provider="acme", base_url=""))

    (client,) = _RecordingClient.instances
    assert client.base_url == "https://api.acme.example/v1"


def test_an_unregistered_vendor_is_still_just_a_label(monkeypatch):
    """Registration is optional and must stay optional: a Model carries its own
    endpoint, so "local-llm" and every private gateway keep working with no
    entry in any registry."""
    _drive(
        _model(provider="local-llm", base_url="http://127.0.0.1:8080/v1"),
        {"api_key": "not-needed"},
    )

    (client,) = _RecordingClient.instances
    assert client.base_url == "http://127.0.0.1:8080/v1"
    assert client.headers["Authorization"] == "Bearer not-needed"
    assert get_provider_spec("local-llm") is None


def test_a_vendor_with_its_own_wire_protocol_gets_its_own_client():
    """Dispatch really does choose a CLASS, not just an endpoint: an api
    registered to something that is not the OpenAI client routes there, and the
    OpenAI client is never constructed."""

    class _AcmeProvider(Provider):
        seen: list[tuple[str, str | None]] = []

        def __init__(self, base_url: str, api_key: str | None) -> None:
            self.base_url = base_url
            self.api_key = api_key

        async def stream_chat(self, model, messages, tools=None, options=None):
            _AcmeProvider.seen.append((self.base_url, self.api_key))
            final = AssistantMessage(
                content=[TextContent(text="from acme")],
                api=model.api,
                provider=model.provider,
                model=model.id,
                stop_reason="stop",
                timestamp=0,
            )

            async def _events() -> AsyncIterator[Any]:
                yield TextDeltaEvent(delta="from acme", partial=final)
                yield DoneEvent(final=final, usage=Usage())

            return _events()

    register_api(
        "acme-chat",
        lambda *, provider_id, name, base_url, api_key: _AcmeProvider(base_url, api_key),
    )
    register_provider(ProviderSpec(id="acme-native", api="acme-chat"))
    try:
        final = _drive(
            _model(provider="acme-native", api="acme-chat", base_url="https://acme.example"),
            {"api_key": "acme-secret"},
        )
    finally:
        unregister_provider("acme-native")
        unregister_api("acme-chat")

    assert _AcmeProvider.seen == [("https://acme.example", "acme-secret")]
    assert _RecordingClient.instances == [], "the OpenAI client was built for a foreign api"
    assert final.provider == "acme-native"
    assert final.api == "acme-chat"


def test_the_provider_instance_knows_which_vendor_it_serves(acme, monkeypatch):
    """``Provider.id``/``.name`` — pi's ``Provider.id``/``.name``
    (models.ts:98). A pooled instance is bound to one vendor's endpoint; being
    able to say which one is the difference between a pool and a pile."""
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")
    model = _model(provider="acme", base_url="https://api.acme.example/v1")

    async def _go() -> None:
        stream = await stream_simple(
            model,
            {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
            {},
        )
        await stream.result()
        # Read from INSIDE the loop: the pool is weakly keyed on it, so after
        # asyncio.run returns the entry is a race with the collector, not a
        # fact (test_provider_lifetime.py makes the same point).
        (provider,) = _POOL[asyncio.get_running_loop()].values()
        assert provider.id == "acme"
        assert provider.name == "Acme Inference"

    asyncio.run(_go())


# ──────────────────────────────────────────────────────────────────────────
# 2. Fail-Early: unknown dispatch raises, and builds nothing.
# ──────────────────────────────────────────────────────────────────────────


def test_an_api_tau_does_not_implement_raises_instead_of_using_openai():
    """``openai-responses`` was a legal value of the old Literal and τ has never
    had a Responses client. It was served over the completions wire — a
    different protocol, silently. The whole point of the change is that this
    line now raises."""
    with pytest.raises(ValueError) as excinfo:
        _drive(_model(api="openai-responses"))

    message = str(excinfo.value)
    assert "openai-responses" in message, "the error must name what was asked for"
    assert "openai-completions" in message, "…and what is available instead"
    assert _RecordingClient.instances == [], "a refused api must construct nothing"
    assert not _POOL, "a refused api must not leave a provider pooled"


def test_an_unknown_api_raises_naming_the_registry():
    with pytest.raises(ValueError, match="anthropic-messages"):
        _drive(_model(api="anthropic-messages"))


def test_a_model_that_contradicts_its_own_vendor_raises(acme, monkeypatch):
    """A registered vendor speaks one protocol. A model claiming a different one
    for that vendor is a configuration error — attempting it would send an
    Acme-shaped request to whatever the other protocol's client is."""
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")
    register_api("acme-chat", lambda *, provider_id, name, base_url, api_key: MagicMock())
    try:
        with pytest.raises(ValueError, match="speaks 'openai-completions'"):
            _drive(_model(provider="acme", api="acme-chat"))
    finally:
        unregister_api("acme-chat")

    assert _RecordingClient.instances == []


def test_a_registered_vendor_with_no_credential_will_not_borrow_openais(acme, monkeypatch):
    """The leak this guard exists for: ``OpenAICompletionsProvider`` falls back
    to ``OPENAI_API_KEY`` when handed no key, so an Acme model with no
    ``ACME_API_KEY`` would have posted OpenAI's secret to Acme's server. A
    vendor that says where its credential lives and has none must raise."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.delenv("ACME_API_KEY", raising=False)

    with pytest.raises(ValueError) as excinfo:
        _drive(_model(provider="acme", base_url="https://api.acme.example/v1"))

    assert "acme" in str(excinfo.value)
    assert "ACME_API_KEY" in str(excinfo.value), "the error must say where to put the key"
    assert _RecordingClient.instances == [], "not one byte of OpenAI's key left the process"


def test_an_explicit_key_in_the_options_beats_the_environment(acme, monkeypatch):
    monkeypatch.setenv("ACME_API_KEY", "from-env")

    _drive(_model(provider="acme", base_url="https://api.acme.example/v1"), {"api_key": "explicit"})

    (client,) = _RecordingClient.instances
    assert client.headers["Authorization"] == "Bearer explicit"


def test_a_model_with_no_endpoint_and_no_registered_vendor_raises():
    """No silent ``https://api.openai.com/v1``. Guessing an endpoint is how a
    private gateway's prompt ends up at a third party."""
    with pytest.raises(ValueError, match="no base_url"):
        _drive(_model(provider="mystery", base_url=""), {"api_key": "k"})

    assert _RecordingClient.instances == []


def test_a_model_object_missing_provider_or_api_raises():
    """``model`` is typed ``Any`` at this seam and duck-typed objects do reach
    it. A defaulted ``"openai"`` used to paper over that."""

    class _Bare:
        id = "bare"
        base_url = "http://host.example/v1"

    with pytest.raises(ValueError, match="no provider"):
        _drive(_Bare(), {"api_key": "k"})  # type: ignore[arg-type]

    class _NoApi(_Bare):
        provider = "openai"

    with pytest.raises(ValueError, match="no api"):
        _drive(_NoApi(), {"api_key": "k"})  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────
# 3. Pooling still keys on everything that varies the construction.
# ──────────────────────────────────────────────────────────────────────────


def test_two_vendors_sharing_an_endpoint_and_key_are_still_two_providers(monkeypatch):
    """The pool key keeps ``provider`` even when nothing else differs: two
    vendor ids are two identities, and a shared instance would report the wrong
    one (and would silently merge two vendors' registrations tomorrow)."""

    async def _go() -> None:
        base = {"base_url": "http://gateway.example/v1"}
        for provider in ("vendor-a", "vendor-b"):
            stream = await stream_simple(
                _model(provider=provider, **base),
                {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
                {"api_key": "same-key"},
            )
            await stream.result()
        loop = asyncio.get_running_loop()
        assert len(_POOL[loop]) == 2

    asyncio.run(_go())
    assert len(_RecordingClient.instances) == 2


def test_the_same_vendor_twice_still_reuses_one_client(acme, monkeypatch):
    """The keep-alive property dispatch must not have cost (PROVIDER-LIFETIME §3)."""
    monkeypatch.setenv("ACME_API_KEY", "acme-secret")
    model = _model(provider="acme", base_url="https://api.acme.example/v1")

    async def _go() -> None:
        for _ in range(2):
            stream = await stream_simple(
                model,
                {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
                {},
            )
            await stream.result()

    asyncio.run(_go())
    assert len(_RecordingClient.instances) == 1


# ──────────────────────────────────────────────────────────────────────────
# 4. The registries themselves.
# ──────────────────────────────────────────────────────────────────────────


def test_tau_ships_three_apis_and_their_three_vendors():
    """What τ claims out of the box: one entry per wire protocol it implements,
    and the vendor that authored each.

    "openai-responses" stays deliberately absent — τ has no client for it, and
    registering a name τ cannot honour is exactly the fiction this work removed.
    That is also why "anthropic-messages" and "google-generative-ai" are present
    and were not before: each claim followed its client, rather than preceding
    it.

    Bedrock, Vertex and Foundry are absent for the other reason in the module
    docstring — τ has not implemented them, each needs different auth, and each
    is a `register_provider` call in the embedding application. Vertex is the
    sharpest case now that Google is here: it serves the same models over a
    different endpoint with Google Cloud auth, which is a vendor τ has not
    written, not a spelling of this one."""
    assert set(registered_apis()) == {
        "openai-completions",
        "anthropic-messages",
        "google-generative-ai",
    }
    assert "openai" in registered_providers()
    assert "anthropic" in registered_providers()
    assert "gemini" in registered_providers()

    spec = get_provider_spec("openai")
    assert spec is not None
    assert spec.base_url == "https://api.openai.com/v1"
    assert spec.api_key_env == ("OPENAI_API_KEY",)

    spec = get_provider_spec("anthropic")
    assert spec is not None
    assert spec.api == "anthropic-messages"
    assert spec.base_url == "https://api.anthropic.com"
    assert spec.api_key_env == ("ANTHROPIC_API_KEY",)

    # Registered as "gemini", not "google": that is the `backend` value
    # ~/.tau/config.json entries have carried since before this client existed.
    spec = get_provider_spec("gemini")
    assert spec is not None
    assert spec.api == "google-generative-ai"
    assert spec.base_url == "https://generativelanguage.googleapis.com"
    assert spec.api_key_env == ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    for absent in ("bedrock", "vertex", "foundry", "google"):
        assert get_provider_spec(absent) is None


def test_registering_over_something_must_be_deliberate(acme):
    """Two libraries quietly fighting over "which client serves this protocol"
    is the same class of invisible mis-routing as the rest of this file."""
    with pytest.raises(ValueError, match="already registered"):
        register_provider(ProviderSpec(id="acme", api="openai-completions"))

    register_provider(
        ProviderSpec(id="acme", api="openai-completions", base_url="https://other.example"),
        replace=True,
    )
    spec = get_provider_spec("acme")
    assert spec is not None and spec.base_url == "https://other.example"


def test_unregistering_something_that_was_never_registered_says_so():
    with pytest.raises(KeyError):
        unregister_provider("never-registered")
    with pytest.raises(KeyError):
        unregister_api("never-registered")


def test_a_spec_defaults_its_name_to_its_id_and_validates_its_own_fields():
    assert ProviderSpec(id="groq", api="openai-completions").name == "groq"
    with pytest.raises(ValueError, match="non-empty vendor id"):
        ProviderSpec(id="", api="openai-completions")
    with pytest.raises(ValueError, match="wire protocol"):
        ProviderSpec(id="groq", api="")


def test_a_spec_searches_its_environment_variables_in_order(monkeypatch):
    spec = ProviderSpec(id="v", api="openai-completions", api_key_env=("FIRST", "SECOND"))
    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.setenv("SECOND", "second-key")
    assert spec.resolve_api_key() == "second-key"

    monkeypatch.setenv("FIRST", "first-key")
    assert spec.resolve_api_key() == "first-key"

    monkeypatch.delenv("FIRST")
    monkeypatch.delenv("SECOND")
    assert spec.resolve_api_key() is None
