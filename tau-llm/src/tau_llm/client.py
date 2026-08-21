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
from typing import Any

from tau_llm.streaming import AssistantMessageEventStream
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.types import AssistantMessage

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
# server (§5). Keying on (provider_name, base_url, sha256(api_key)) makes
# that impossible: a distinct endpoint or key is always a distinct cache
# entry. The key hashes the api_key so the cache dict never holds a raw
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

_PoolKey = tuple[str, str, str]

_POOL: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_PoolKey, OpenAICompletionsProvider]]" = weakref.WeakKeyDictionary()


def _pool_key(provider_name: str, base_url: str | None, api_key: str | None) -> _PoolKey:
    """Cache key for one provider: (provider_name, resolved base_url, key hash).

    ``base_url`` is resolved against the SAME default the provider itself
    falls back to (``OpenAICompletionsProvider.DEFAULT_BASE_URL``) so an
    explicit ``base_url="https://api.openai.com/v1"`` and an omitted one
    collide on purpose — they name the same server. ``api_key`` is hashed,
    never stored raw, including the ``None`` case (empty string hashes to a
    fixed digest distinct from any real key's digest).
    """
    resolved_base_url = base_url or OpenAICompletionsProvider.DEFAULT_BASE_URL
    key_hash = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()
    return (provider_name, resolved_base_url, key_hash)


def _get_or_create_provider(
    provider_name: str, base_url: str | None, api_key: str | None
) -> OpenAICompletionsProvider:
    """Look up (or build and cache) the provider for this loop + key.

    Construction is synchronous (no ``await`` between the dict lookup and
    the insert), so two tasks racing this on the same loop cannot both win —
    asyncio is cooperative, and nothing here yields control mid-check.
    """
    loop = asyncio.get_running_loop()
    providers = _POOL.get(loop)
    if providers is None:
        providers = {}
        _POOL[loop] = providers

    key = _pool_key(provider_name, base_url, api_key)
    provider = providers.get(key)
    if provider is None:
        provider = OpenAICompletionsProvider(api_key=api_key, base_url=base_url)
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
        options: Optional provider-specific options (temperature, etc.).

    Returns:
        AssistantMessageEventStream yielding TextDeltaEvent, ToolCallDeltaEvent,
        DoneEvent, and ErrorEvent instances.
    """
    if options is None:
        options = {}

    provider_name = getattr(model, "provider", "openai")
    base_url = getattr(model, "base_url", None)
    provider = _get_or_create_provider(provider_name, base_url, options.get("api_key"))

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
    """Non-streaming completion — drive a stream to its terminal message.

    Faithful port of pi's ``completeSimple`` (stream.ts:67), which is simply
    ``stream(...).result()``. Used where the caller wants the whole
    AssistantMessage and not the intermediate deltas — e.g. compaction's
    summary generation, which has no streaming UI to feed.

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
