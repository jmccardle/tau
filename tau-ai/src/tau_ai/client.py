"""τ-ai client: Simple streaming client for LLM chat.

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.
PHASE-1-SUBPHASE-3.md — Streaming Protocol and Client.

stream_simple() is the primary client function that wraps Provider.stream_chat()
to provide a simple async interface for chat completions.

This is the ONLY entry point that τ-agent-core uses to talk to τ-ai.

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

from typing import Any

from tau_ai.streaming import AssistantMessageEventStream
from tau_ai.providers.registry import Registry
from tau_ai.providers.openai import OpenAICompletionsProvider


async def stream_simple(
    model: Any,
    context: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Simple streaming client for the agent loop.

    This is the ONLY entry point that τ-agent-core uses to talk to τ-ai.

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

    Raises:
        KeyError: If the model's provider is not registered.
    """
    if options is None:
        options = {}

    provider_name = getattr(model, "provider", "openai")
    registry = Registry()
    try:
        provider = registry.get(provider_name)
    except KeyError:
        # Auto-register default provider if not found
        provider = OpenAICompletionsProvider(
            api_key=options.get("api_key"),
        )
        registry.register(provider_name, provider)

    messages = context.get("messages", [])
    tools = context.get("tools", None)

    provider_stream = await provider.stream_chat(
        model=model,
        messages=messages,
        tools=tools,
        options=options,
    )

    # The provider's stream_chat returns an AssistantMessageEventStream
    # that is itself async-iterable and has .result() and .abort().
    # We wrap it in our streaming.py AssistantMessageEventStream so that
    # the outer stream proxies the inner stream's events and provides
    # the unified API expected by τ-agent-core.
    return AssistantMessageEventStream(
        provider_stream=provider_stream,
        model=model,
        context=context,
    )
