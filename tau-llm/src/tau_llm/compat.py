"""Per-endpoint wire quirks: what THIS OpenAI-compatible server wants.

"OpenAI-compatible" is a family, not a specification. Servers in it disagree
about the spelling of fields that mean the same thing, and about whether a field
may be sent at all. τ has always had a place to put a vendor's *body* params
(``Model.extra_body``), but two of the keys that differ are ones ``extra_body``
cannot reach: ``stream_options`` is reserved (``providers/openai.py``'s
``_RESERVED_BODY_KEYS``), and ``max_tokens`` is written after the spreads.

This module is that place. :class:`Compat` is what an operator states,
:func:`detect_compat` is what τ infers from the endpoint when they state
nothing, and :func:`resolve_compat` layers the first over the second.

Adapted from pi's ``detectCompat`` / ``getCompat``
(``packages/ai/src/api/openai-completions.ts:1534-1670``, pi ``5cd93f688``),
MIT, Copyright (c) 2025 Mario Zechner. See the repository README for the
attribution τ carries.

## Why this is three fields and not pi's twenty-six

pi's ``ResolvedOpenAICompletionsCompat`` carries 26 fields. Most of them do not
port, and their absence is a statement rather than an omission:

* **τ's Model already says it, more precisely.** ``supportsReasoningEffort`` is
  ``Model.reasoning`` — declared per model, which is the granularity the fact
  actually has (one endpoint serves reasoning and non-reasoning models).
  ``thinkingFormat`` and ``supportsThinkingTokenBudget`` are
  ``Model.thinking_level_map``, whose fragment form names the field AND its
  value instead of selecting from an enum τ would have to keep current.
  ``chatTemplateKwargs`` / ``chatTemplateArgs`` / the two routing dicts are
  ``Model.extra_body``.
* **τ does not send the field at all.** ``supportsStore``, ``supportsStrictMode``,
  ``supportsDeveloperRole``, ``cacheControlFormat``, ``sessionAffinityFormat``,
  ``deferredToolsMode``, ``supportsLongCacheRetention``. A compat flag guarding a
  request τ never makes is a knob wired to nothing.
* **τ does not implement the machinery.** ``zaiToolStream``,
  ``requiresAssistantAfterToolResult``, ``requiresToolResultName``.

What is left is the set with a live consumer and no other way to say it.

## Why inferring here does not break the "don't guess" rule

``Model.reasoning`` and ``Model.grammar_dialect`` both refuse to infer capability
from a URL, and say why: guessing wrong there produces a server that *silently
ignores* the parameter and returns an unconstrained generation dressed as a
constrained one. That reasoning is about failures that hide.

Neither of the two DETECTED fields can hide. Sending ``max_tokens`` where
``max_completion_tokens`` is required is a 400 with the field named in it, and
τ *already guesses* — it sends the classic spelling unconditionally, which is a
blind constant rather than an informed one. Detection narrows an existing guess;
it does not introduce one. The same holds for ``stream_options``: a server that
rejects it rejects the request out loud.

Detection is also always overridable, and an operator's ``compat`` entry wins
field by field.

``tool_call_schema`` is the exception that proves the rule: it CAN hide — it
rewrites a response τ was handed — so nothing detects it, and it does something
only when an operator states it. Its own comment says why.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel
from tau_llm.docs import agent_facing

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tau_llm.types import Model

MaxTokensField = Literal["max_tokens", "max_completion_tokens"]
ToolCallSchema = Literal["openai", "anthropic"]


@agent_facing(topic="providers")
class Compat(BaseModel):
    """Wire quirks an operator STATES for one endpoint.

    Every field is optional, and ``None`` means "not stated" rather than "false"
    — :func:`resolve_compat` falls through to the detected value for anything
    left unset, so an operator can correct one field without restating the rest.

    Set per-model in ``~/.tau/config.json`` (``models.<name>.compat``).
    """

    max_tokens_field: MaxTokensField | None = None
    supports_usage_in_streaming: bool | None = None
    tool_call_schema: ToolCallSchema | None = None


@agent_facing(topic="providers")
class ResolvedCompat(BaseModel):
    """:class:`Compat` with every field decided. What the provider reads."""

    max_tokens_field: MaxTokensField
    supports_usage_in_streaming: bool
    tool_call_schema: ToolCallSchema


_REQUIRES_MAX_COMPLETION_TOKENS_HOSTS = ("api.openai.com", "openai.azure.com")


@agent_facing(topic="providers")
def detect_compat(provider: str, base_url: str) -> ResolvedCompat:
    """Infer wire quirks from the endpoint URL.

    Only the URL decides anything today. pi also matches on the provider NAME,
    and τ cannot: ``build_model_from_config`` defaults an entry with no
    ``backend`` key to ``provider="openai"``, so in τ that string means "the
    operator did not say" far more often than it means OpenAI. Matching it would
    switch a local llama.cpp to a spelling llama.cpp rejects, which is the
    regression this whole module is arranged to avoid.

    A proxy in front of real OpenAI therefore goes undetected. That is the
    correct trade: it is one explicit ``compat.max_tokens_field`` in the config,
    where the alternative silently breaks endpoints that work today.

    Args:
        provider: ``Model.provider``. Accepted for signature stability and to
            keep the pi correspondence readable; not consulted, for the reason
            above.
        base_url: ``Model.base_url`` — the endpoint the request goes to.

    Returns:
        A fully-decided :class:`ResolvedCompat`.
    """
    url = base_url.strip().lower()

    requires_completion_spelling = any(
        host in url for host in _REQUIRES_MAX_COMPLETION_TOKENS_HOSTS
    )

    return ResolvedCompat(
        max_tokens_field=(
            "max_completion_tokens" if requires_completion_spelling else "max_tokens"
        ),
        supports_usage_in_streaming=True,
        tool_call_schema="openai",
    )


@agent_facing(topic="providers")
def resolve_compat(model: Model) -> ResolvedCompat:
    """The compat τ will actually use for ``model``.

    Detection first, then the operator's stated :class:`Compat` over the top,
    field by field — an unset field falls through to the detected value rather
    than to a type default, so stating one quirk never silently resets another.
    Mirrors pi's ``getCompat`` (``openai-completions.ts:1631``).
    """
    detected = detect_compat(model.provider, model.base_url)
    stated = model.compat
    if stated is None:
        return detected

    return ResolvedCompat(
        max_tokens_field=(
            stated.max_tokens_field
            if stated.max_tokens_field is not None
            else detected.max_tokens_field
        ),
        supports_usage_in_streaming=(
            stated.supports_usage_in_streaming
            if stated.supports_usage_in_streaming is not None
            else detected.supports_usage_in_streaming
        ),
        tool_call_schema=(
            stated.tool_call_schema
            if stated.tool_call_schema is not None
            else detected.tool_call_schema
        ),
    )
