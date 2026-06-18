"""τ-ai providers.base: Provider ABC for LLM integration.

Reference: SUBPHASE-0.0.md, "1. Messages" and Phase 1 Subphase 0 sections.

Provider is an abstract base class that all LLM providers must implement.
It defines the streaming chat interface that tau-agent-core consumes.

Usage:
    class MyProvider(Provider):
        async def stream_chat(self, model, messages, tools=None, options=None):
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


class Provider(ABC):
    """Abstract base class for LLM chat providers.

    All providers (OpenAI, Anthropic, etc.) must implement this interface.
    The primary method is ``stream_chat``, which returns an async iterator
    of streaming events.

    Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 — Provider interface.

    Attributes:
        model: The model identifier (e.g. "gpt-4").
        messages: List of message dicts (user/assistant/toolResult).
        tools: Optional list of tool definitions.
        options: Optional provider-specific options dict.

    Methods:
        stream_chat(model, messages, tools, options):
            Async generator yielding StreamEvent dicts.

    Example:
        >>> class MyProvider(Provider):
        ...     async def stream_chat(self, model, messages, tools=None, options=None):
        ...         yield {"type": "text_delta", "delta": "Hello"}
        ...         yield {"type": "done", "final": {...}, "usage": {...}}
    """

    @abstractmethod
    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completions from the LLM.

        Args:
            model: The model identifier to use (e.g. "gpt-4", "claude-3-opus").
            messages: List of message dicts with role, content, etc.
            tools: Optional list of tool definitions in JSON Schema format.
            options: Optional provider-specific options (temperature, max_tokens, etc.).

        Yields:
            dict: Streaming events with one of these types:
                - {"type": "text_delta", "delta": str, "partial": AssistantMessage}
                - {"type": "toolcall_delta", "delta": dict, "partial": AssistantMessage}
                - {"type": "done", "final": AssistantMessage, "usage": Usage}
                - {"type": "error", "message": str, "is_error": True}

        Raises:
            NotImplementedError: If the provider hasn't implemented this method.
        """
        ...
