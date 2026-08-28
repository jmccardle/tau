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
    # How this reasoning gets replayed to the model that produced it. The two
    # shapes mean different things, and the writer that consumes this field must
    # branch on the type rather than assume one (docs/ANTHROPIC-GOOGLE-CLIENTS.md
    # S4 — "signature" names four different things across the three APIs):
    #
    #   str  — the streaming FIELD NAME this reasoning arrived on
    #          (``reasoning_content`` / ``reasoning`` / ``reasoning_text``), used
    #          as a dictionary key when replaying to the SAME model under the
    #          exact field its chat template reads. pi calls this the
    #          ``thinkingSignature``. Empty when the block predates this capture
    #          (e.g. older persisted chats) — in which case reasoning is NOT
    #          replayed, never guessed (Fail-Early; mirrors pi, which only
    #          replays when a signature is present).
    #
    #   dict — a provider-peculiar payload that is NOT a field name, e.g.
    #          ``{"anthropic": {"signature": "...", "redacted": false}}`` for an
    #          opaque cryptographic signature over the thinking text. Writing one
    #          of these as a JSON field name would not raise; it would send a
    #          valid-looking request that means nothing, which is exactly the
    #          silent failure this branch exists to prevent.
    #
    # A dict reaching an OpenAI-path writer means the block came from another
    # provider — model mixing — and is handled there (warn, keep the thinking as
    # text, never as a key; raise under ``Model.strict_reasoning_formats``).
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

    # A provider-namespaced token that must travel back WITH this tool call when
    # the turn is replayed, e.g.::
    #
    #     {"google": {"thought_signature": "Cs4BAdHtim..."}}
    #
    # Not reasoning content, despite being reasoning-derived: Gemini 3 VALIDATES
    # it. The first functionCall part of each step must carry its
    # thought_signature or the request fails with 400 ("Function call `FC1` in
    # the `1.` content block is missing a thought_signature"). So this field is
    # replayed regardless of ``reasoning_replay`` — including "off", which
    # governs chain-of-thought and has no say over a token the API checks.
    #
    # Namespaced by vendor because a signature is meaningless, and sometimes
    # rejected, off the wire that minted it. A writer for another api MUST NOT
    # forward it (the OpenAI writer refuses it, mirroring S4).
    #
    # Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S8, O4.
    provider_signature: dict[str, Any] = Field(default_factory=dict)


@agent_facing(topic="messages")
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
    """

    role: Literal["assistant"] = "assistant"
    content: list[TextContent | ThinkingContent | ToolCall]
    # Copied verbatim from the Model that produced this message
    # (streaming.py:301). Both are ``str`` rather than a Literal: this is a
    # RECORD of which endpoint answered, and a record that cannot hold the true
    # answer is worse than no record. ``provider`` was pinned to
    # Literal["openai"], which meant a perfectly legal Model — Model.provider
    # has always been ``str`` — raised ValidationError here the moment it named
    # anyone else. The value that DECIDES anything is ``Model.api``, and it is
    # checked where it is used, against the registered wire protocols
    # (tau_llm.providers.get_api_factory), not against a type.
    api: str
    provider: str
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


@agent_facing(topic="messages")
class Model(BaseModel):
    """LLM model configuration.

    Reference: SUBPHASE-0.0.md, "1. Messages" section.

    Represents a model with its provider and connection details.
    Serializes to OpenAI-compatible dict format.
    """

    id: str
    name: str
    # The wire protocol this endpoint speaks, and the vendor it belongs to.
    #
    # ``api`` selects the provider class at request time
    # (``tau_llm.client._resolve_request`` → ``providers.get_api_factory``).
    # It is a ``str``, not a Literal, because the set of implementations is a
    # RUNTIME registry an embedding application extends — and because the
    # Literal was never the gate it looked like: it admitted
    # "openai-responses", which τ does not implement and used to serve over the
    # completions wire regardless. An unregistered api now raises, naming what
    # is registered.
    #
    # ``provider`` is the vendor and stays free-form: a Model carries its own
    # base_url, so "local-llm" or an internal gateway name needs no
    # registration. Registering one (``tau_llm.providers.register_provider``)
    # adds a default endpoint and the environment variables its credential
    # lives in.
    api: str
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
    # What τ does when a reasoning-format quirk shows up mid-conversation.
    # ``False`` (default) — warn and degrade to the behaviour that still works:
    # a thinking block whose signature belongs to another provider keeps its text
    # but is not replayed under a signature field; a reasoning-replay setting a
    # provider cannot honour is overridden with a logged reason. ``True`` turns
    # each of those same conditions into a raise.
    #
    # The default is permissive because the cause is almost always legitimate —
    # mixing models within one session, or a message an extension synthesised —
    # and the data structure stays compatible with returning to the model that
    # produced it. Crashing the program over a provider quirk costs the user the
    # whole session; degrading costs a signature the provider would have rejected
    # anyway. The flag exists so an operator running a single-model,
    # signature-clean pipeline can find out when that stops being true, without
    # imposing that crash on everyone else.
    #
    # One flag, one meaning: "a provider quirk I was willing to work around is
    # now an error." Set per-model in ~/.tau/config.json
    # (``models.<name>.strict_reasoning_formats``).
    # Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S3.
    strict_reasoning_formats: bool = False
    # Two Google converter capabilities. Both defaults come from a measurement,
    # not from a model-name table — O2 asked for a vendored table and the
    # measurement removed the need for one, which is why these are plain fields
    # with an operator override and nothing consults a list of model names.
    #
    # MEASURED 2026-08-22 (docs/probe-results/README-gemini-2026-08-22.md):
    # sending a tool call id on a functionResponse was ACCEPTED by every model
    # tried, including gemma-4-26b-a4b-it, which pi's requiresToolCallId answers
    # FALSE for. So the permissive branch is the safe one and an unknown model
    # sends the id. The opposite of what O2 assumed when it called this "not
    # obvious".
    requires_tool_call_id: bool = True
    # The conservative branch, which O2 already called clearly safe: send images
    # in a separate user turn rather than nested in functionResponse.parts. Only
    # one model was measured accepting the nested form, and one permissive data
    # point does not earn a permissive default when the fallback always works.
    # Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md O2.
    supports_multimodal_function_response: bool = False
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
    # Sampling temperature for this model. ``None`` (default) means τ sends NO
    # temperature and the endpoint applies its own — pi parity
    # (``simple-options.ts:32`` passes ``options?.temperature``, which is
    # undefined unless a caller sets one, and pi's agent never does).
    #
    # It is a field rather than a bare number in the agent config because the
    # right value is a property of the endpoint, not of the run: llama.cpp
    # defaults to 0.8, the OpenAI wire to 1.0, and the Anthropic Messages API
    # removed the parameter outright on Opus 5 / Opus 4.8 / Opus 4.7 / Sonnet 5
    # / Fable 5, where sending any value is a 400. A single harness-wide default
    # cannot be right for all three, and the previous one (0.7, fabricated in
    # ``agent_session`` by a ``getattr`` against a field that did not exist) was
    # both unreachable from config and fatal on the Anthropic wire.
    # Set per-model in ~/.tau/config.json (``models.<name>.temperature``).
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
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
    # Completion timeout in seconds. ``None`` keeps the provider's own default
    # (300s read / 10s connect). This exists because the value was hardcoded at
    # client construction with no override anywhere, and a gateway that drops
    # connections is exactly a timeout question — an operator debugging one had
    # to edit provider source to change it.
    # Set per-model in ~/.tau/config.json (``models.<name>.request_timeout``).
    request_timeout: float | None = Field(default=None, gt=0)
    # Whether this endpoint is talked to over SSE (``stream: true``, the default)
    # or as one buffered completion (``stream: false``).
    #
    # A deliberate divergence from pi, which is streaming-only: every chat-capable
    # pi API hardcodes `stream: true`, and its `complete()`/`completeSimple()` just
    # await the same streaming request (PLAN-0.9.3 §4.1). τ has met OpenAI-shaped
    # gateways that do not implement SSE at all, and to those τ was simply unusable
    # — `stream` was hardcoded in the payload AND reserved against `extra_body`, so
    # there was no override anywhere.
    #
    # This is a TRANSPORT knob, not a behaviour knob: the provider adapts a
    # buffered response into the same TextDelta/ToolCallDelta/Done event sequence,
    # built by the same finalize path, so nothing above the provider can tell which
    # mode ran. What it cannot adapt is granularity — a non-streaming turn produces
    # its text in one delta and cannot be interrupted mid-completion — so streaming
    # stays the default and this is opt-out per endpoint.
    # Set per-model in ~/.tau/config.json (``models.<name>.stream``).
    stream: bool = True
    # Wire quirks of THIS endpoint that no other field can express: which spelling
    # of the output cap it accepts, and whether it tolerates `stream_options`.
    # ``None`` (default) means τ infers both from ``provider``/``base_url``
    # (:func:`tau_llm.compat.detect_compat`); a stated field wins over the
    # inference, field by field.
    #
    # Kept deliberately small. Most of what pi's `compat` carries is already said
    # by a field above — `reasoning`, `thinking_level_map`, `extra_body` — and
    # saying it twice invites the two to disagree. ``tau_llm.compat``'s module
    # docstring lists every pi field that did not port and why.
    # Set per-model in ~/.tau/config.json (``models.<name>.compat``).
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
