"""τ-llm types: Core data types for LLM interaction.

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
    # Server-reported, non-portable per-completion telemetry: llama.cpp's
    # ``timings`` block (prompt/predicted token+ms stats, and — fork-only —
    # ``n_ff_total``, the forced-token count under jump-forward decoding),
    # plus τ's own tool-arg JSON-repair count. Shape varies by server; τ never
    # invents keys here — only what the server (or τ itself) actually reported
    # lands in this dict. Populated at construction / via ``model_copy``
    # (Usage stays frozen).
    extra: dict[str, Any] = Field(default_factory=dict)


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
    # Maps τ thinking levels ("off".."xhigh") to what this endpoint wants on the
    # wire. Missing keys pass the level through unchanged; a ``None`` value marks a
    # level unsupported; an entry is what makes "xhigh" available at all. pi:
    # `Model.thinkingLevelMap` (types.ts:589). None = no remapping.
    #
    # A value is EITHER a string or a request-body fragment:
    #
    #   "high": "high"                                   → reasoning_effort: "high"
    #   "high": {"thinking_budget_tokens": 4096}         → that key, that value
    #   "off":  {"chat_template_kwargs": {"enable_thinking": false}}
    #
    # A string keeps the original meaning — it sets `reasoning_effort` — so every
    # config written before fragments existed is unaffected.
    #
    # Fragments exist because `reasoning_effort` is ONE vendor's spelling of this
    # idea, and a server that spells it differently ignores it in silence. Measured
    # against llama.cpp b1061-2da6686 (docs/probe-results/): `reasoning_effort` at
    # "high", "low", and the invalid "banana" all returned byte-identical
    # generations, while `thinking_budget_tokens` tracked the requested budget and
    # `chat_template_kwargs.enable_thinking` suppressed thinking outright. τ's level
    # enum stays the stable abstraction a caller depends on; WHICH field carries it
    # is a property of the endpoint, so it belongs in the endpoint's config.
    #
    # A fragment rather than a "name the field" string because the third example
    # above is NESTED — no flat field name can express it — and because a server
    # whose knob takes two keys is then expressible without another schema change.
    # Set per-model in ~/.tau/config.json (``models.<name>.thinking_level_map``).
    thinking_level_map: dict[str, str | dict[str, Any] | None] | None = None
    # How much historical chain-of-thought to replay to the model. A reasoning
    # model streams its thinking on a signature field (e.g. Qwen3's
    # ``reasoning_content``); τ can replay prior thinking blocks back on follow-up
    # turns under that field so the model keeps its chain-of-thought. Replaying
    # ALL of it (pi's behaviour, openai-completions.ts:880) bloats the context
    # with stale, self-referential reasoning — on a tool-driven session it can
    # dominate the payload and degrade comprehension. This knob scopes the replay:
    #   "all"  — replay every historical thinking block (pi-faithful).
    #   "turn" — replay thinking only for the in-progress turn (assistant
    #            messages after the last user message), keeping within-turn
    #            chain-of-thought across tool calls while dropping the cross-turn
    #            accretion. τ default — a deliberate, configurable divergence.
    #   "off"  — never replay historical thinking.
    # Set per-model in ~/.tau/config.json (``models.<name>.reasoning_replay``)
    # over a top-level default; "turn" when unset.
    reasoning_replay: Literal["all", "turn", "off"] = "turn"
    # Which constrained-decoding dialect this endpoint accepts. ``None`` (default)
    # means the endpoint declares NO grammar support, and any constraint-carrying
    # call raises rather than shipping a param that OpenAI-the-company would 400 on
    # and that other servers would silently IGNORE. Silent-ignore is the worse
    # failure: an unconstrained generation masquerading as constrained. Same
    # opt-in-don't-guess discipline as ``reasoning`` above (Fail-Early).
    #
    # τ standardizes on llguidance Lark-style grammars (the ``%llguidance {}\n``
    # prefix, dispatched at llama.cpp common/sampling.cpp:201) because that is what
    # jump-forward decoding accelerates. "gbnf" is accepted and passed through
    # as-is, but is second-class: no τ-side grammar helpers target it.
    # Set per-model in ~/.tau/config.json (``models.<name>.grammar``).
    grammar_dialect: Literal["llguidance", "gbnf"] | None = None
    # Static request-body params merged into every payload for this model, BELOW
    # per-call options (per-call wins). Standalone value even without grammars:
    # llama-server knobs like ``cache_prompt``, ``min_p``, sampler settings are
    # otherwise unreachable without editing τ source.
    # Set per-model in ~/.tau/config.json (``models.<name>.extra_body``).
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Advisory capability tags for telemetry/UX only (e.g. "jump_forward",
    # "slot_fork") — deliberately NOT used for gating. The gate for grammars is
    # ``grammar_dialect``; a fork-API gate arrives with the fork API.
    server_features: list[str] = Field(default_factory=list)

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
