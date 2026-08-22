"""τ-llm client: Simple streaming client for LLM chat.

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.
PHASE-1-SUBPHASE-3.md — Streaming Protocol and Client.
docs/PROVIDER-LIFETIME.md — the provider pool this module owns (§5/§6).

stream_simple() is the primary client function that wraps Provider.stream_chat()
to provide a simple async interface for chat completions.

This is the ONLY entry point that τ-agent-core uses to talk to τ-llm.

Usage:
    stream = await stream_simple(model, context, options)
    async for event in stream:
        if event.type == "text_delta":
            print(event.delta, end="")
        elif event.type == "done":
            print(f"\nUsage: {event.usage}")
    final = await stream.result()
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from dataclasses import dataclass
from typing import Any

from tau_llm.streaming import AssistantMessageEventStream
from tau_llm.providers import Provider, get_api_factory, get_provider_spec
from tau_llm.types import AssistantMessage

# ──────────────────────────────────────────────────────────────────────────
# Dispatch (docs/PLAN-0.9.3.md §4.4).
#
# Which provider CLASS serves a call is decided by ``model.api`` — the wire
# protocol — through the api registry in ``tau_llm.providers``. It used to be
# decided by nothing at all: this module constructed an
# ``OpenAICompletionsProvider`` unconditionally and used ``model.provider``
# only as a cache key, so a model declaring ``api="openai-responses"`` (a
# protocol τ has never implemented) was served over the completions wire and
# nothing said so.
#
# ``model.provider`` is the VENDOR, and stays free-form: a Model carries its
# own base_url, so "local-llm" or an internal gateway name needs no
# registration. Registering one (``tau_llm.providers.register_provider``) adds
# defaults — endpoint, credential environment variables, display name — and
# lets τ refuse to send one vendor's prompt with another vendor's key.
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ProviderRequest:
    """One fully-resolved provider construction: who, where, with what.

    Every field is settled BEFORE anything is built or looked up in the pool,
    so the cache key and the constructed provider can never disagree about the
    endpoint or the credential (the cross-routing trap in PROVIDER-LIFETIME.md
    §5 is exactly that disagreement).
    """

    provider_id: str
    name: str
    api: str
    base_url: str
    api_key: str | None


def _resolve_request(model: Any, options: dict[str, Any]) -> _ProviderRequest:
    """Work out which provider a model wants, or raise saying why it cannot.

    Fail-Early throughout: every branch below either produces a complete answer
    or raises naming what was asked for and what is available. Nothing here
    substitutes a default vendor, endpoint or credential.
    """
    provider_id = getattr(model, "provider", None)
    if not provider_id:
        raise ValueError(
            f"Model {getattr(model, 'id', model)!r} has no provider; "
            f"a provider name is required to choose an endpoint."
        )
    api = getattr(model, "api", None)
    if not api:
        raise ValueError(
            f"Model {getattr(model, 'id', model)!r} (provider {provider_id!r}) has no api; "
            f"an api names the wire protocol to speak (e.g. 'openai-completions')."
        )

    # Unknown wire protocol → raise. Checked before the vendor so that an
    # unimplemented api reports itself as unimplemented rather than as a
    # mismatch with whatever the vendor happens to speak.
    get_api_factory(api)

    spec = get_provider_spec(provider_id)
    if spec is not None and spec.api != api:
        raise ValueError(
            f"Provider {provider_id!r} speaks {spec.api!r}, but model "
            f"{getattr(model, 'id', model)!r} declares api {api!r}. "
            f"A vendor that genuinely speaks both registers a second provider id."
        )

    base_url = getattr(model, "base_url", None) or (spec.base_url if spec else None)
    if not base_url:
        raise ValueError(
            f"Model {getattr(model, 'id', model)!r} has no base_url and provider "
            f"{provider_id!r} declares no default endpoint. Set base_url on the model, "
            f"or register the provider with one."
        )

    api_key = options.get("api_key") or (spec.resolve_api_key() if spec else None)
    if not api_key and spec is not None and spec.api_key_env:
        # The vendor told us where its credential lives and it is not there.
        # Continuing would hand the request to the provider with no key, and
        # OpenAICompletionsProvider then falls back to OPENAI_API_KEY — i.e.
        # one vendor's secret sent to another vendor's endpoint.
        raise ValueError(
            f"No API key for provider {provider_id!r}. Set one of "
            f"{', '.join(spec.api_key_env)}, or pass api_key in the call options."
        )

    return _ProviderRequest(
        provider_id=provider_id,
        name=spec.name if spec else provider_id,
        api=api,
        base_url=base_url,
        api_key=api_key,
    )


# ──────────────────────────────────────────────────────────────────────────
# Provider pool (docs/PROVIDER-LIFETIME.md).
#
# A fresh provider (and therefore a fresh httpx.AsyncClient) on every call
# means no HTTP keep-alive between completions — measured at +42 ms/call
# (51% slower) on a LAN plaintext endpoint (§3). The naive fix — a
# module-level cache keyed on provider_name alone — is a SILENT
# CROSS-ROUTING BUG: the provider bakes base_url + api_key in at
# construction, so a second model with a different endpoint would reuse the
# first model's provider, sending model B's prompt AND api key to A's
# server (§5). Keying on (provider_name, api, base_url, sha256(api_key))
# makes that impossible: a distinct endpoint or key is always a distinct
# cache entry. The key hashes the api_key so the cache dict never holds a raw
# secret as a dict key (the provider object itself still holds it — that
# part is unavoidable).
#
# The pool is ALSO keyed per event loop. An httpx.AsyncClient is bound to
# the loop it was built on; a bare module-level dict would hand back a
# client bound to a *closed* loop the moment one asyncio.run() ends and
# another begins (exactly what the test suite does, once per test). A
# WeakKeyDictionary keyed on the running loop means a loop's pool entry
# disappears with the loop — no separate cleanup required for that part.
# Providers still need an EXPLICIT aclose() (below) — GC does not run it.
# ──────────────────────────────────────────────────────────────────────────

_PoolKey = tuple[str, str, str, str]

_POOL: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_PoolKey, Provider]]" = (
    weakref.WeakKeyDictionary()
)


def _pool_key(request: _ProviderRequest) -> _PoolKey:
    """Cache key for one provider: (provider, api, base_url, key hash).

    Every input that varies what gets CONSTRUCTED is in the key. ``base_url``
    arrives already resolved (``_resolve_request`` applied the model's value
    and any vendor default), so an explicit
    ``base_url="https://api.openai.com/v1"`` on an ``openai`` model and an
    omitted one collide on purpose — they name the same server. ``api`` joined
    the key when it started selecting the provider CLASS: two apis are two
    different clients, and pooling them together would be the cross-routing bug
    of §5 with a different first cause. ``api_key`` is hashed, never stored raw,
    including the ``None`` case (empty string hashes to a fixed digest distinct
    from any real key's digest).
    """
    key_hash = hashlib.sha256((request.api_key or "").encode("utf-8")).hexdigest()
    return (request.provider_id, request.api, request.base_url, key_hash)


def _get_or_create_provider(request: _ProviderRequest) -> Provider:
    """Look up (or build and cache) the provider for this loop + key.

    Construction is synchronous (no ``await`` between the dict lookup and
    the insert), so two tasks racing this on the same loop cannot both win —
    asyncio is cooperative, and nothing here yields control mid-check. The
    registry lookup and the factory call are synchronous for the same reason:
    dispatch was added inside this property, not around it.
    """
    loop = asyncio.get_running_loop()
    providers = _POOL.get(loop)
    if providers is None:
        providers = {}
        _POOL[loop] = providers

    key = _pool_key(request)
    provider = providers.get(key)
    if provider is None:
        provider = get_api_factory(request.api)(
            provider_id=request.provider_id,
            name=request.name,
            base_url=request.base_url,
            api_key=request.api_key,
        )
        providers[key] = provider
    return provider


async def aclose_providers() -> None:
    """Close and drop every pooled provider for the CURRENT event loop.

    Explicit teardown (docs/PROVIDER-LIFETIME.md §6.3: "closed explicitly,
    not by GC"). Call this from a shutdown path that still runs on the loop
    the providers were built on — a TUI's unmount handler, or a headless
    run's ``finally`` before the driving ``asyncio.run()`` returns. Calling
    it again (or with nothing pooled) is a no-op; a subsequent
    ``stream_simple`` call rebuilds providers on demand.
    """
    loop = asyncio.get_running_loop()
    providers = _POOL.pop(loop, None)
    if not providers:
        return
    for provider in providers.values():
        await provider.aclose()


async def stream_simple(
    model: Any,
    context: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Simple streaming client for the agent loop.

    This is the ONLY entry point that τ-agent-core uses to talk to τ-llm.

    Args:
        model: The Model configuration (has provider, id, etc.).
        context: Context dict with keys:
            - messages: List of message dicts (user/assistant/toolResult).
            - tools: Optional list of tool definitions.
            - system_prompt: Optional system prompt string.
        options: Optional provider-specific options (temperature, etc.). Two of
            them are TRANSPORT settings the provider strips from the request
            body rather than sending: ``request_timeout`` and ``stream``
            (False = talk to a backend that does not implement SSE; the events
            below are produced either way — PLAN-0.9.3 §4.1).

    Returns:
        AssistantMessageEventStream yielding TextDeltaEvent, ToolCallDeltaEvent,
        DoneEvent, and ErrorEvent instances.

    Raises:
        ValueError: If the model names a wire protocol τ has no implementation
            for, contradicts its own vendor's protocol, resolves to no endpoint,
            or to no credential for a vendor that declares where its credential
            lives. See ``_resolve_request``.
    """
    if options is None:
        options = {}

    provider = _get_or_create_provider(_resolve_request(model, options))

    messages = context.get("messages", [])
    tools = context.get("tools", None)

    provider_stream = await provider.stream_chat(
        model=model,
        messages=messages,
        tools=tools,
        options=options,
    )

    # The provider yields typed streaming events (a bare async iterator). Wrap it
    # once in AssistantMessageEventStream, which runs a background collector so
    # ``result()`` and ``async for`` can be awaited independently — the single
    # stream type τ-agent-core consumes.
    return AssistantMessageEventStream(
        provider_stream=provider_stream,
        model=model,
    )


async def complete_simple(
    model: Any,
    context: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> AssistantMessage:
    """Whole-message completion — drive a stream to its terminal message.

    Faithful port of pi's ``completeSimple`` (stream.ts:67), which is simply
    ``stream(...).result()``. Used where the caller wants the whole
    AssistantMessage and not the intermediate deltas — e.g. compaction's
    summary generation, which has no streaming UI to feed.

    This is about the CALLER's shape, not the transport: it collapses the event
    stream for a caller that has no use for deltas, and the request underneath is
    still whatever ``Model.stream`` / ``options["stream"]`` selected (PLAN-0.9.3
    §4.1). A non-streaming BACKEND is a separate axis — set ``stream=False`` and
    every entry point here, this one included, keeps working unchanged.

    Args:
        model: The Model configuration (has provider, id, etc.).
        context: Context dict (same shape as ``stream_simple``): ``messages``
            and optional ``tools``. A leading ``{"role": "system", ...}`` message
            sets the system prompt (client.py does not read ``system_prompt``).
        options: Optional provider options (``max_tokens``, ``api_key``,
            ``reasoning``, ``temperature``, …).

    Returns:
        The fully accumulated AssistantMessage.

    Raises:
        Exception: If the stream produced an ErrorEvent (propagated by
            ``AssistantMessageEventStream.result``). Fail-Early: no fabricated
            fallback message.
    """
    stream = await stream_simple(model, context, options)
    return await stream.result()
