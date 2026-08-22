"""τ-llm providers: LLM provider abstraction, and τ's built-in registrations.

Exports:
    Provider: Abstract base class for LLM providers.
    ProviderSpec: One vendor, as data.
    register_api / unregister_api / registered_apis / get_api_factory:
        the wire-protocol registry — which client class serves a ``Model.api``.
    register_provider / unregister_provider / registered_providers /
    get_provider_spec:
        the vendor registry — display name, wire protocol, default base URL and
        credential environment variables for a ``Model.provider``.

See ``base.py`` for what each registry is for and why there are two of them, and
``docs/PROVIDER-LIFETIME.md`` for the pooling that sits on top (the "which
provider INSTANCE serves this call" question, which ``tau_llm.client`` owns).

A prior ``ProviderRegistry`` class lived here and was removed as dead code:
``stream_simple`` built a fresh, throwaway one on every call and dispatched
through none of it (PROVIDER-LIFETIME.md §1). The registries below are the
opposite case — ``tau_llm.client`` cannot construct a provider without them, so
deleting either one breaks every completion.

**Adding an OpenAI-compatible vendor.** τ registers one vendor per wire protocol
it implements — ``openai``, ``anthropic`` and ``gemini`` — and no model catalogs.
An OpenAI-compatible vendor is six lines, in your own
code, at import time::

    from tau_llm.providers import ProviderSpec, register_provider

    register_provider(ProviderSpec(
        id="groq",
        name="Groq",
        api="openai-completions",
        base_url="https://api.groq.com/openai/v1",
        api_key_env=("GROQ_API_KEY",),
    ))

then point a model at it with ``provider="groq"``. τ deliberately does not ship
a vendor list: every URL and environment-variable name in one is a claim τ would
have to keep true as vendors move them, and the registry exists precisely so
that yours does not have to be in τ.
"""

from tau_llm.providers.base import (
    ApiFactory,
    Provider,
    ProviderSpec,
    StreamEventStream,
    get_api_factory,
    get_provider_spec,
    register_api,
    register_provider,
    registered_apis,
    registered_providers,
    unregister_api,
    unregister_provider,
)


def _build_openai_completions(
    *,
    provider_id: str,
    name: str,
    base_url: str,
    api_key: str | None,
) -> Provider:
    """Factory for the ``openai-completions`` wire protocol.

    Imported lazily so that ``import tau_llm.providers`` — and therefore
    ``import tau_llm`` — does not drag in httpx and the whole provider module
    for a caller that only wanted the registry or the message types.

    ``id``/``name`` are stamped after construction rather than passed in:
    ``OpenAICompletionsProvider.__init__`` takes the endpoint only, and the
    vendor identity is the factory's business, not the wire protocol's — the
    same instance shape serves ``openai``, ``groq`` and an unnamed gateway.
    """
    from tau_llm.providers.openai import OpenAICompletionsProvider

    provider = OpenAICompletionsProvider(api_key=api_key, base_url=base_url)
    provider.id = provider_id
    provider.name = name
    return provider


def _build_anthropic_messages(
    *,
    provider_id: str,
    name: str,
    base_url: str,
    api_key: str | None,
) -> Provider:
    """Factory for the ``anthropic-messages`` wire protocol.

    Imported lazily for the same reason as the OpenAI factory, and for a second
    one: this client is built on the official ``anthropic`` SDK, which τ declares
    as the optional extra ``tau-llm[anthropic]``. A module-scope import would
    make ``import tau_llm`` fail for every install that never talks to Anthropic.
    The SDK import itself happens later still — inside the provider, on first
    request — so that constructing one raises nothing and the missing-extra error
    names the extra.
    """
    from tau_llm.providers.anthropic import AnthropicMessagesProvider

    provider = AnthropicMessagesProvider(api_key=api_key, base_url=base_url)
    provider.id = provider_id
    provider.name = name
    return provider


def _build_google_generative_ai(
    *,
    provider_id: str,
    name: str,
    base_url: str,
    api_key: str | None,
) -> Provider:
    """Factory for the ``google-generative-ai`` wire protocol.

    Lazy for the same two reasons as the Anthropic factory: the module import
    cost, and the ``google-genai`` SDK being the optional extra
    ``tau-llm[google]``. The SDK import happens later still, inside the provider
    on first request, so the missing-extra error names the extra.
    """
    from tau_llm.providers.google import GoogleGenerativeAIProvider

    provider = GoogleGenerativeAIProvider(api_key=api_key, base_url=base_url)
    provider.id = provider_id
    provider.name = name
    return provider


# The wire protocols τ implements. "openai-responses" is deliberately NOT
# registered: τ has no Responses client, and a model declaring that api used to
# be served silently over the completions wire (PLAN-0.9.3 §4.4). It now raises.
register_api("openai-completions", _build_openai_completions)
register_api("anthropic-messages", _build_anthropic_messages)
register_api("google-generative-ai", _build_google_generative_ai)

# The three vendors τ ships, because implementing their wire already claimed them.
#
# This is narrower than it looks, and it is not the vendor list this module's
# docstring refuses. The refusal is about vendors τ does NOT implement — a Groq
# or an OpenRouter, whose URL and credential variable τ would then have to keep
# true as they move. These two are different: "openai-completions" and
# "anthropic-messages" are wire protocols named after their author, so the api
# registration above is already the claim. Declining to also state the endpoint
# would only mean every user retyping the same URL.
#
# Bedrock, Vertex and Foundry speak Anthropic-shaped protocols behind different
# endpoints and entirely different auth. They are deliberately absent: each is a
# vendor τ has not implemented, and each is a `register_provider` call in the
# embedding application, exactly as the docstring describes.
register_provider(
    ProviderSpec(
        id="openai",
        name="OpenAI",
        api="openai-completions",
        base_url="https://api.openai.com/v1",
        api_key_env=("OPENAI_API_KEY",),
    )
)
register_provider(
    ProviderSpec(
        id="anthropic",
        name="Anthropic",
        api="anthropic-messages",
        base_url="https://api.anthropic.com",
        api_key_env=("ANTHROPIC_API_KEY",),
    )
)
# Registered as "gemini" rather than "google" because that is what
# ~/.tau/config.json entries already say (`"backend": "gemini"`), and the
# shipped config template has said it since before any Google client existed.
# Renaming the vendor would break those entries to gain nothing.
#
# GEMINI_API_KEY is listed before GOOGLE_API_KEY for the same reason the SDK
# reads them in that order: GOOGLE_API_KEY is set for many Google services, and
# the more specific name is the one that means this API.
register_provider(
    ProviderSpec(
        id="gemini",
        name="Google Gemini",
        api="google-generative-ai",
        base_url="https://generativelanguage.googleapis.com",
        api_key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
)

__all__ = [
    "ApiFactory",
    "Provider",
    "ProviderSpec",
    "StreamEventStream",
    "get_api_factory",
    "get_provider_spec",
    "register_api",
    "register_provider",
    "registered_apis",
    "registered_providers",
    "unregister_api",
    "unregister_provider",
]
