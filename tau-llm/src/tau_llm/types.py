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

    Attributes:
        type: Discriminator, always ``"text"``.
        text: The text itself.
    """

    type: Literal["text"] = "text"
    text: str


@agent_facing(topic="messages")
class ThinkingContent(BaseModel):
    """A thinking/reasoning content block.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Attributes:
        type: Discriminator, always ``"thinking"``.
        thinking: The reasoning text.
        cached_tokens: Not set by any τ provider; stays 0 unless a caller fills it.
        thinking_signature: What a provider needs to replay this block. On the
            OpenAI wire, the name of the response field the reasoning arrived in
            (e.g. ``"reasoning_content"``); on Anthropic, a namespaced dict holding
            the signature or redacted payload. ``""`` when there is none.
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    cached_tokens: int = 0
    thinking_signature: str | dict[str, Any] = ""


@agent_facing(topic="messages")
class ImageContent(BaseModel):
    """An image content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Attributes:
        type: Discriminator, always ``"image"``.
        data: The image bytes, base64-encoded, without a ``data:`` prefix.
        mime_type: The image's media type, e.g. ``"image/png"``.
    """

    type: Literal["image"] = "image"
    data: str
    mime_type: str


@agent_facing(topic="messages")
class ToolCall(BaseModel):
    """A tool call content block in a message.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Attributes:
        type: Discriminator, always ``"toolCall"``.
        id: The provider's call id; the answering result carries it as
            ``tool_call_id``.
        name: The tool the model asked for.
        arguments: The parsed argument object.
        provider_signature: Vendor-namespaced data a provider must send back with
            this call, e.g. Gemini's thought signature. Persisted with the session.
    """

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]

    provider_signature: dict[str, Any] = Field(default_factory=dict)


@agent_facing(topic="messages")
class Usage(BaseModel):
    """Token usage for one LLM response. Frozen: use ``model_copy(update=...)``.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    ``cache_reported`` exists because a 0 in the cache counters is ambiguous:
    llama.cpp reports no cache fields, while a gateway's 0 means "cached
    nothing" (docs/PROMPT-CACHING.md §7).

    Attributes:
        model_config: Pydantic configuration; makes instances frozen.
        input_tokens: Prompt tokens, as the provider reported them.
        output_tokens: Completion tokens, as the provider reported them.
        cache_read_tokens: Prompt tokens served from the provider's cache.
        cache_write_tokens: Prompt tokens written to the provider's cache.
        total_tokens: The server's total, or input plus output when it sent none.
        cost: Not set by any τ provider; prices are applied at the head's emit
            boundary, not stored here.
        extra: Provider-specific additions, e.g. llama.cpp ``"timings"`` or the
            OpenAI provider's ``"repairs"`` count for tool-call arguments.
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

    Attributes:
        role: Discriminator, always ``"user"``.
        content: Plain text, or a list of text and image blocks.
        timestamp: Epoch milliseconds when the message was created.
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

    Attributes:
        role: Discriminator, always ``"assistant"``.
        content: Thinking, text and tool-call blocks.
        api: The wire protocol that produced it, e.g. ``"openai-completions"``.
        provider: The provider id the request went to.
        model: The model id the request named.
        response_id: The server's id for the completion, when it sent one.
        usage: Token counts for this completion.
        stop_reason: Why generation ended.
        error_message: The failure text when ``stop_reason`` is ``"error"``.
        timestamp: See above.
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
    """One model: which endpoint to call, how to speak to it, what it can do.

    Reference: SUBPHASE-0.0.md, "1. Messages" section. Prompt caching:
    docs/PROMPT-CACHING.md §5.

    Attributes:
        id: The model id sent on the wire.
        name: Human-readable name.
        api: The wire protocol, e.g. ``"openai-completions"``; selects the
            provider class.
        provider: Provider id. Must agree with ``api`` when the provider is
            registered, and supplies the endpoint and credential defaults.
        base_url: The endpoint. Empty falls back to the registered provider's
            default; with neither, a request raises.
        context_window: The model's context size in tokens.
        max_tokens: Completion-token cap, sent unless the call sets its own.
        reasoning: Whether the model accepts a reasoning level. False supports
            only ``"off"``.
        thinking_level_map: Per-level override of what a reasoning level sends:
            a string becomes ``reasoning_effort``, a dict is merged into the
            body, ``None`` marks the level unsupported. ``"xhigh"`` exists only
            when mapped. See :func:`tau_llm.models.get_supported_thinking_levels`.
        reasoning_replay: Which past thinking is sent back: ``"turn"`` (only
            after the last user message), ``"all"``, or ``"off"``
            (docs/ANTHROPIC-GOOGLE-CLIENTS.md §S1).
        strict_reasoning_formats: Raise on a thinking signature from another
            provider instead of warning once and dropping it.
        requires_tool_call_id: Send tool-call ids on the Google wire.
        supports_multimodal_function_response: Put tool-result images inside
            the tool result rather than in a separate image turn.
        grammar_dialect: The decode-constraint grammar the server accepts.
            ``None`` makes any constrained request raise.
        temperature: Sampling temperature, 0.0–2.0; ``None`` sends none.
        extra_body: Static request-body fields merged into every request;
            per-call options win.
        server_features: Advisory tags from config (e.g. ``"jump_forward"``).
            Nothing in tau-llm reads them; never used for gating.
        request_timeout: Completion timeout in seconds; a per-call
            ``request_timeout`` option wins, ``None`` keeps the provider's.
        stream: Use SSE streaming. False talks to a backend without SSE; the
            same events are produced either way. A per-call option wins.
        compat: Stated OpenAI-wire quirks; ``None`` means detect them
            (:mod:`tau_llm.compat`).
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
    tokenizer: str | None = None
    """A ``tokenizer.json`` path or HuggingFace repo id, for exact token counts.

    Unset means token counts for this model are estimated from character
    composition and labelled as estimates (:mod:`tau_llm.tokens`).
    """
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
            ``id``, ``name``, ``provider``, ``base_url``,
            ``max_completion_tokens`` (from ``max_tokens``) and ``context_window``.
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

    Attributes:
        role: Discriminator, always ``"toolResult"``.
        tool_call_id: The ``ToolCall.id`` this answers.
        tool_name: The tool that ran.
        content: What the model sees: text and image blocks.
        details: Structured data for heads and hooks; no provider sends it.
        is_error: Whether the tool failed; the content then holds the error.
        timestamp: Epoch milliseconds when the result was created.
    """

    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    details: dict[str, Any] | None = None
    is_error: bool = False
    timestamp: int = Field(ge=0)
