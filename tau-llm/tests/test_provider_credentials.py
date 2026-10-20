"""Which key a request carries, and what a 401 says to set (docs/PROVIDER-CREDENTIALS.md)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tau_llm import ProviderHttpError, complete_simple
from tau_llm.client import _resolve_request
from tau_llm.providers import (
    api_sends_without_key,
    credential_hint,
    register_api,
    resolve_request_key,
    resolves_api_key,
    unregister_api,
)
from tau_llm.providers.anthropic import AnthropicMessagesProvider
from tau_llm.types import Model

_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture(autouse=True)
def _no_key_env(monkeypatch):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def _model(**overrides: Any) -> Model:
    fields: dict[str, Any] = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "http://llm.test/v1",
        "context_window": 8192,
        "max_tokens": 256,
    }
    fields.update(overrides)
    return Model(**fields)


def _anthropic_model(base_url: str = "http://llm.test") -> Model:
    return _model(api="anthropic-messages", provider="anthropic", base_url=base_url)


def _gemini_model() -> Model:
    return _model(api="google-generative-ai", provider="gemini", base_url="http://llm.test")


def test_two_wires_resolve_with_no_key_and_the_google_wire_refuses():
    assert _resolve_request(_anthropic_model(), {}).api_key is None
    assert _resolve_request(_model(), {}).api_key is None
    with pytest.raises(ValueError, match="No API key for provider 'gemini'") as excinfo:
        _resolve_request(_gemini_model(), {})
    assert str(excinfo.value).endswith(credential_hint("gemini", "http://llm.test"))


def test_an_environment_key_reaches_only_the_vendors_own_endpoint(monkeypatch):
    """#75: ANTHROPIC_API_KEY is api.anthropic.com's, not a proxy's."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert _resolve_request(_anthropic_model(), {}).api_key is None
    official = _anthropic_model("https://api.anthropic.com/")
    assert _resolve_request(official, {}).api_key == "from-env"
    assert _resolve_request(official, {"api_key": "explicit"}).api_key == "explicit"


def test_registration_sets_and_clears_the_flag():
    def factory(**kwargs: Any) -> Any:
        raise AssertionError("never built")

    register_api("test-keyless", factory, sends_without_key=True)
    try:
        assert api_sends_without_key("test-keyless")
        register_api("test-keyless", factory, replace=True)
        assert not api_sends_without_key("test-keyless")
        register_api("test-keyless", factory, replace=True, sends_without_key=True)
    finally:
        unregister_api("test-keyless")
    assert not api_sends_without_key("test-keyless")
    assert api_sends_without_key("anthropic-messages")
    assert api_sends_without_key("openai-completions")
    assert not api_sends_without_key("google-generative-ai")


def test_resolve_request_key_reads_the_key_then_the_vendor_env_at_its_endpoint(monkeypatch):
    assert resolve_request_key("anthropic", "k", "http://proxy") == "k"
    assert resolve_request_key("anthropic", "", None) is None
    assert resolve_request_key("local-llm", None, None) is None
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    assert resolve_request_key("gemini", None, None) == "g"
    assert resolve_request_key("gemini", None, "https://generativelanguage.googleapis.com") == "g"
    assert resolve_request_key("gemini", None, "http://proxy") is None
    assert resolves_api_key("gemini", None, None)
    assert not resolves_api_key("gemini", None, "http://proxy")


def test_the_hint_names_env_vars_only_where_they_would_be_read():
    config = "on the model's \"models\" entry in τ's config.json."
    assert credential_hint("gemini", None) == (
        f'To send a key, set GEMINI_API_KEY or GOOGLE_API_KEY or "api_key" {config}'
    )
    assert credential_hint("gemini", "http://proxy") == f'To send a key, set "api_key" {config}'
    assert credential_hint("local-llm", None) == f'To send a key, set "api_key" {config}'


def test_only_a_401_with_a_provider_carries_the_hint():
    def err(status: int, provider_id: str | None) -> ProviderHttpError:
        return ProviderHttpError(
            status, message="nope.", body="", model_id="q", base_url="u", provider_id=provider_id
        )

    hint = credential_hint("openai", "u")
    assert str(err(401, "openai")) == "HTTP 401 from model 'q' at 'u': nope. " + hint
    assert err(401, "openai").hint == hint
    for quiet in (err(403, "openai"), err(500, "openai"), err(401, None)):
        assert quiet.hint == ""
        assert str(quiet) == f"HTTP {quiet.status_code} from model 'q' at 'u': nope."


def test_an_openai_wire_401_names_openai_api_key(monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
    )
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", client)

    async def drive() -> None:
        await complete_simple(
            _model(stream=False),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "x"},
        )

    with pytest.raises(ProviderHttpError) as excinfo:
        asyncio.run(drive())
    hint = credential_hint("openai", "http://llm.test/v1")
    assert str(excinfo.value).endswith("Incorrect API key. " + hint)


def test_an_anthropic_401_names_anthropic_api_key():
    anthropic = pytest.importorskip("anthropic")
    body = {"type": "error", "error": {"type": "authentication_error", "message": "no key"}}
    response = httpx.Response(401, json=body, request=httpx.Request("POST", "http://llm.test"))
    sdk_error = anthropic.AuthenticationError("Error code: 401", response=response, body=body)
    provider = AnthropicMessagesProvider(api_key=None)

    class _Refusing:
        class messages:  # noqa: N801 — mirrors the SDK's attribute name
            @staticmethod
            def stream(**kwargs: Any) -> Any:
                raise sdk_error

    provider._client = _Refusing()

    async def drive() -> None:
        stream = await provider.stream_chat(_anthropic_model(), [{"role": "user", "content": "hi"}])
        async for _ in stream:
            pass

    with pytest.raises(ProviderHttpError) as excinfo:
        asyncio.run(drive())
    assert excinfo.value.status_code == 401
    assert "ANTHROPIC_API_KEY" in excinfo.value.hint
    assert str(excinfo.value).endswith("no key. " + credential_hint("anthropic", None))
