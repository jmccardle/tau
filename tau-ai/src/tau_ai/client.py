"""τ-ai client: Simple streaming client for LLM chat.

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

stream_simple() is the primary client function that wraps Provider.stream_chat()
to provide a simple async generator interface for chat completions.

Usage:
    async for event in stream_simple(messages, model="gpt-4"):
        if event["type"] == "text_delta":
            print(event["delta"], end="")
        elif event["type"] == "done":
            print(f"\nUsage: {event['usage']}")
"""

from __future__ import annotations

from typing import Any, AsyncIterator


async def stream_simple(
    messages: list[dict[str, Any]],
    model: str = "gpt-4",
    provider: str = "openai",
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a simple chat completion.

    This is a convenience wrapper around the Provider.stream_chat() method.
    It selects the appropriate provider, calls stream_chat(), and returns
    an async generator of StreamEvent dicts.

    In subphase 0.3, this is a stub that raises NotImplementedError.
    Phase 1.3 will implement the full streaming client.

    Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

    Args:
        messages: List of message dicts (user/assistant/toolResult).
        model: Model identifier (default: "gpt-4").
        provider: Provider ID to use (default: "openai").
        tools: Optional list of tool definitions in JSON Schema format.
        options: Optional provider-specific options (temperature, etc.).

    Yields:
        dict: Streaming events with one of these types:
            - {"type": "text_delta", "delta": str, "partial": AssistantMessage}
            - {"type": "toolcall_delta", "delta": dict, "partial": AssistantMessage}
            - {"type": "done", "final": AssistantMessage, "usage": Usage}
            - {"type": "error", "message": str, "is_error": True}

    Raises:
        NotImplementedError: Always (stub for subphase 0.3).
    """
    raise NotImplementedError(
        "stream_simple: Not implemented (subphase 0.3)"
    )
