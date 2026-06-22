"""τ-ai types: Core data types for LLM interaction.

Reference: SUBPHASE-0.0.md, "Core Data Type Contracts" section.

Message types (UserMessage, AssistantMessage, ToolResultMessage) and
ContentBlock types (TextContent, ThinkingContent, ImageContent, ToolCall)
form the foundation of the τ messaging protocol.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TextContent(BaseModel):
    """A text content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["text"] = "text"
    text: str


class ThinkingContent(BaseModel):
    """A thinking/reasoning content block.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    cached_tokens: int = 0
    # The streaming field this reasoning arrived on (``reasoning_content`` /
    # ``reasoning`` / ``reasoning_text``). Recorded so a follow-up turn can send
    # the reasoning back to the SAME model under the exact field its chat
    # template reads (pi calls this the ``thinkingSignature``). Empty when the
    # block predates this capture (e.g. older persisted chats) — in which case
    # reasoning is NOT replayed, never guessed (Fail-Early; mirrors pi, which
    # only replays when a signature is present).
    thinking_signature: str = ""


class ImageContent(BaseModel):
    """An image content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["image"] = "image"
    data: str  # base64 encoded image data
    mime_type: str


class ToolCall(BaseModel):
    """A tool call content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]


class Usage(BaseModel):
    """Token usage information for an LLM response.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Usage is immutable (frozen) — once created, its fields cannot be modified.
    """

    model_config = {"frozen": True}

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost: dict[str, float] = Field(default_factory=dict)


class UserMessage(BaseModel):
    """A user message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    role: Literal["user"] = "user"
    content: str | list[TextContent | ImageContent]
    timestamp: int = Field(ge=0)


class AssistantMessage(BaseModel):
    """An assistant message from the LLM.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.
    """

    role: Literal["assistant"] = "assistant"
    content: list[TextContent | ThinkingContent | ToolCall]
    api: Literal["openai-completions", "openai-responses"]
    provider: Literal["openai"]
    model: str
    response_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"]
    error_message: str | None = None
    timestamp: int = Field(ge=0)

    def get_tool_calls(self) -> list[ToolCall]:
        """Extract all tool calls from this message's content.

        Returns:
            List of ToolCall objects found in content blocks.
        """
        return [c for c in self.content if isinstance(c, ToolCall)]


class Model(BaseModel):
    """LLM model configuration.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Represents a model with its provider and connection details.
    Serializes to OpenAI-compatible dict format.
    """

    id: str
    name: str
    api: Literal["openai-completions", "openai-responses"]
    provider: str
    base_url: str
    context_window: int
    max_tokens: int
    # Whether this model accepts a request-side reasoning/thinking effort
    # (OpenAI o-series, Qwen3, DeepSeek-R1, …). False means the provider never
    # sends `reasoning_effort`, so a requested level is clamped to "off" and
    # dropped — sending it to a non-reasoning model is an upstream 400. pi calls
    # this `Model.reasoning` (types.ts:585). Default False (Fail-Early: opt in,
    # don't guess capability).
    reasoning: bool = False
    # Maps τ thinking levels ("off".."xhigh") to provider/model-specific
    # `reasoning_effort` values. Missing keys pass the level through unchanged; a
    # ``None`` value marks a level unsupported; an entry is what makes "xhigh"
    # available at all. pi: `Model.thinkingLevelMap` (types.ts:589). None = no
    # remapping.
    thinking_level_map: dict[str, str | None] | None = None

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
