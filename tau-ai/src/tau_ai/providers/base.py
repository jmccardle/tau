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
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

if TYPE_CHECKING:
    from tau_ai.tools import ToolDefinition
    from tau_ai.types import AssistantMessage, Model


class StreamEventStream(Protocol):
    """Structural return type for ``Provider.stream_chat``.

    A provider stream is async-iterable over typed streaming events
    (TextDeltaEvent / ToolCallDeltaEvent / DoneEvent / ErrorEvent) and
    exposes the terminal ``AssistantMessage`` via ``result()``. Both
    ``AssistantMessageEventStream`` implementations (streaming.py and the
    OpenAI provider) satisfy this Protocol structurally.
    """

    def __aiter__(self) -> AsyncIterator[Any]: ...

    async def result(self) -> AssistantMessage: ...


class Provider(ABC):
    """Abstract base class for LLM chat providers.

    All providers (OpenAI, Anthropic, etc.) must implement this interface.
    The primary method is ``stream_chat``, which returns an async-iterable
    stream of typed streaming events.

    Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 — Provider interface.

    Methods:
        stream_chat(model, messages, tools, options):
            Returns a StreamEventStream yielding typed StreamEvents.
    """

    @abstractmethod
    async def stream_chat(
        self,
        model: Model,
        messages: list[Any],
        tools: list[ToolDefinition] | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamEventStream:
        """Stream chat completions from the LLM.

        Args:
            model: The Model configuration to use for the request.
            messages: List of τ message objects (user/assistant/toolResult).
            tools: Optional list of τ ToolDefinitions.
            options: Optional provider-specific options (temperature, max_tokens, etc.).

        Returns:
            A StreamEventStream yielding TextDeltaEvent, ToolCallDeltaEvent,
            DoneEvent, and ErrorEvent instances, with the terminal
            AssistantMessage available via ``result()``.

        Raises:
            NotImplementedError: If the provider hasn't implemented this method.
        """
        ...
