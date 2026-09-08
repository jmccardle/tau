"""τ-llm types: Core data types for LLM interaction.

Reference: SUBPHASE-0.0.md, "Core Data Type Contracts" section.

Message types (UserMessage, AssistantMessage, ToolResultMessage) and
ContentBlock types (TextContent, ThinkingContent, ImageContent, ToolCall)
form the foundation of the τ messaging protocol.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from tau_llm.compat import Compat
from tau_llm.docs import agent_facing


@agent_facing(topic="messages")
class TextContent(BaseModel):
    """A text content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["text"] = "text"
    text: str


@agent_facing(topic="messages")
class ThinkingContent(BaseModel):
    """A thinking/reasoning content block.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    cached_tokens: int = 0
    thinking_signature: str | dict[str, Any] = ""


@agent_facing(topic="messages")
class ImageContent(BaseModel):
    """An image content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["image"] = "image"
    data: str  # base64 encoded image data
    mime_type: str


@agent_facing(topic="messages")
class ToolCall(BaseModel):
    """A tool call content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]

    provider_signature: dict[str, Any] = Field(default_factory=dict)


@agent_facing(topic="messages")
class Usage(BaseModel):
    """Token usage information for an LLM response.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Usage is immutable (frozen) — once created, its fields cannot be modified.

    ``cache_reported`` says whether the SERVER accounted for a prompt cache on
    this completion at all, which a 0 in the two counters cannot: llama.cpp
    reports no cache fields, so its 0 means "no cache here" where a gateway's 0
    means "cached nothing". Only the second is a finding
    (docs/PROMPT-CACHING.md §7).
    """

    model_config = {"frozen": True}

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_reported: bool = False
    """Whether the server accounted for a prompt cache on this completion at all.

    False makes ``cache_read_tokens`` of 0 silence rather than a miss."""
    total_tokens: int = 0
    cost: dict[str, float] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


@agent_facing(topic="messages")
class UserMessage(BaseModel):
    """A user message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    role: Literal["user"] = "user"
    content: str | list[TextContent | ImageContent]
    timestamp: int = Field(ge=0)


@agent_facing(topic="messages")
class AssistantMessage(BaseModel):
    """An assistant message from the LLM.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    ``timestamp`` is epoch milliseconds at the moment this message stopped being
    written — the completion's end, or the abort for a partial. ``None`` means no
    clock applies, which is the case for a synthetic message an extension built
    (docs/MESSAGE-TIMESTAMPS.md §2). It is never 0: τ did not run in 1970, so a 0
    is legacy data and the session stores map it to ``None`` on load.
    """

    role: Literal["assistant"] = "assistant"
    content: list[TextContent | ThinkingContent | ToolCall]
    api: str
    provider: str
    model: str
    response_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"]
    error_message: str | None = None
    timestamp: int | None = Field(default=None, ge=0)

    def get_tool_calls(self) -> list[ToolCall]:
        """Extract all tool calls from this message's content.

        Returns:
            List of ToolCall objects found in content blocks.
        """
        return [c for c in self.content if isinstance(c, ToolCall)]


@agent_facing(topic="messages")
class Model(BaseModel):
    """LLM model configuration.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Represents a model with its provider and connection details.
    Serializes to OpenAI-compatible dict format.

    Two fields carry prompt caching and they answer different questions.
    ``prompt_cache`` asks for it, on every wire. ``prompt_cache_dialect`` says
    how an OpenAI-compatible endpoint has to be asked, and stays unset for a wire
    whose caching is native (``anthropic-messages``) or automatic (OpenAI's own),
    so a marker is only ever written into the request body where one is declared
    (docs/PROMPT-CACHING.md §5).
    """

    id: str
    name: str
    api: str
    provider: str
    base_url: str
    context_window: int
    max_tokens: int
    reasoning: bool = False
    thinking_level_map: dict[str, str | dict[str, Any] | None] | None = None
    reasoning_replay: Literal["all", "turn", "off"] = "turn"
    strict_reasoning_formats: bool = False
    requires_tool_call_id: bool = True
    supports_multimodal_function_response: bool = False
    grammar_dialect: Literal["llguidance", "gbnf"] | None = None
    prompt_cache: bool = True
    """Whether to ask this endpoint to cache the prompt prefix."""
    prompt_cache_dialect: Literal["anthropic"] | None = None
    """How an OpenAI-compatible endpoint must be asked; unset for a native wire."""
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    server_features: list[str] = Field(default_factory=list)
    request_timeout: float | None = Field(default=None, gt=0)
    stream: bool = True
    compat: Compat | None = None

    def to_openai_format(self) -> dict[str, Any]:
        """Serialize to OpenAI-compatible format.

        Returns:
            dict with keys compatible with OpenAI API:
            - id: model identifier
            - name: human-readable name
            - provider: provider name
            - base_url: API endpoint
            - max_completion_tokens: max tokens for completion
        """
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "max_completion_tokens": self.max_tokens,
            "context_window": self.context_window,
        }


@agent_facing(topic="messages")
class ToolResultMessage(BaseModel):
    """A tool result message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    details: dict[str, Any] | None = None
    is_error: bool = False
    timestamp: int = Field(ge=0)
