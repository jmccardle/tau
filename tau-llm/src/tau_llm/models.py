"""τ-llm models: thinking/reasoning level helpers.

Which levels a :class:`~tau_llm.types.Model` accepts, the clamp from a requested
level to a supported one, and the one rule every provider applies to decide what
it sends (:func:`requested_wire_level`). The vocabulary is common to every wire;
each provider translates it (docs/REASONING-WIRE.md §2).

Reference: pi ``packages/ai/src/models.ts:51-84`` for the clamp.
"""

from __future__ import annotations

from typing import Literal, cast

from tau_llm.types import Model
from tau_llm.docs import agent_facing

# User-facing levels (no "off"). pi: `ThinkingLevel` (types.ts:65).
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]
# Including "off". pi: `ModelThinkingLevel` (types.ts:66).
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]

EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)

DEFAULT_THINKING_LEVEL: ModelThinkingLevel = "medium"

_NATIVE_LEVELS: dict[str, frozenset[str]] = {
    "anthropic-messages": frozenset({"off", "low", "medium", "high"}),
    "google-generative-ai": frozenset({"off", "minimal", "low", "medium", "high"}),
}
"""Levels a wire has a name for without a map entry, by ``Model.api``.

Measured 400s: ``effort: "minimal"`` on Anthropic, ``thinkingLevel: "xhigh"`` on
Gemini (docs/REASONING-WIRE.md §1.1). An api not listed has every level but
``"xhigh"``."""

_DEFAULT_NATIVE_LEVELS = frozenset({"off", "minimal", "low", "medium", "high"})


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


@agent_facing(topic="messages")
def is_valid_thinking_level(level: str) -> bool:
    """Whether ``level`` names a known thinking level.

    Args:
        level: Any string.

    Returns:
        True for ``"off"``, ``"minimal"``, ``"low"``, ``"medium"``, ``"high"``
        and ``"xhigh"``.
    """
    return level in EXTENDED_THINKING_LEVELS


@agent_facing(topic="messages")
def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    """Return the levels ``model`` supports, least → most effort.

    A non-reasoning model supports only ``["off"]``. Otherwise a level is
    supported when its wire has a native name for it (``minimal`` has none on
    Anthropic, ``xhigh`` none anywhere) or ``thinking_level_map`` names it, and
    not when the map sets it to ``None``.

    Args:
        model: Reads ``api``, ``reasoning`` and ``thinking_level_map``.

    Returns:
        The supported levels in :data:`EXTENDED_THINKING_LEVELS` order.
    """
    if not model.reasoning:
        return ["off"]

    tlm = model.thinking_level_map or {}
    native = _NATIVE_LEVELS.get(model.api, _DEFAULT_NATIVE_LEVELS)
    supported: list[ModelThinkingLevel] = []
    for level in EXTENDED_THINKING_LEVELS:
        mapped = tlm.get(level, _UNSET)
        if mapped is None:
            continue
        if mapped is _UNSET and level not in native:
            continue
        supported.append(level)
    return supported


@agent_facing(topic="messages")
def clamp_thinking_level(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    """Clamp ``level`` to the nearest level ``model`` actually supports.

    If the exact level is supported it is returned unchanged. Otherwise search
    upward (more effort) first, then downward.

    Args:
        model: The model whose supported levels bound the result.
        level: The requested level. An unknown string clamps to the lowest
            supported level.

    Returns:
        A level from :func:`get_supported_thinking_levels`; ``"off"`` for a
        non-reasoning model.
    """
    available = get_supported_thinking_levels(model)
    if level in available:
        return level

    try:
        requested_index = EXTENDED_THINKING_LEVELS.index(level)
    except ValueError:
        return available[0] if available else "off"

    for i in range(requested_index, len(EXTENDED_THINKING_LEVELS)):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available:
            return candidate
    for i in range(requested_index - 1, -1, -1):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available:
            return candidate
    return available[0] if available else "off"


class ThinkingRefused(ValueError):
    """A thinking level the model's wire cannot honour; the request is not sent."""


@agent_facing(topic="messages")
def check_thinking_request(model: Model, requested: str | None) -> ModelThinkingLevel | None:
    """:func:`requested_wire_level`, plus the refusals that depend on the wire's off form.

    Pure, so a head or session can refuse before any request is sent and the
    provider applies the same rule (docs/REASONING-WIRE.md §4).

    Args:
        model: Reads ``api``, ``name``, ``reasoning`` and ``thinking_level_map``.
        requested: The ``reasoning`` option as the caller passed it.

    Returns:
        What :func:`requested_wire_level` returns.

    Raises:
        ThinkingRefused: ``requested`` is not a level, ``"off"`` is mapped to null,
            or ``"off"`` on an ``openai-completions`` model that neither declares
            reasoning nor maps ``"off"``: τ cannot know this endpoint's off form,
            and sending nothing would leave its default reasoning on.
    """
    try:
        level = requested_wire_level(model, requested)
    except ValueError as exc:
        raise ThinkingRefused(str(exc)) from exc
    if (
        level == "off"
        and model.api == "openai-completions"
        and "off" not in (model.thinking_level_map or {})
        and not model.reasoning
    ):
        raise ThinkingRefused(
            f"Model {model.id!r}: thinking 'off' was requested, but this "
            "OpenAI-compatible model neither declares reasoning nor maps 'off', so τ "
            "does not know what turns reasoning off on this endpoint. Set "
            f"models.{model.name}.thinking_level_map.off in τ's config.json: "
            '{"chat_template_kwargs": {"enable_thinking": false}} for a llama.cpp chat '
            'template, "none" for OpenAI\'s reasoning_effort, or {} if this model never '
            "reasons; or pass --thinking LEVEL (docs/REASONING-WIRE.md §4)."
        )
    return level


@agent_facing(topic="messages")
def requested_wire_level(model: Model, requested: str | None) -> ModelThinkingLevel | None:
    """The level whose wire form a provider sends for ``requested``, or ``None``.

    The one rule all three providers apply (docs/REASONING-WIRE.md §4): ``None``
    sends nothing; ``"off"`` is sent whatever ``model.reasoning`` says; any other
    level is clamped on a reasoning model and not sent on one that does not
    declare reasoning. The result is what ``AssistantMessage.reasoning_level``
    reports.

    Args:
        model: Reads ``api``, ``reasoning`` and ``thinking_level_map``.
        requested: The ``reasoning`` option as the caller passed it.

    Returns:
        A supported level, or ``None`` when no reasoning control is to be sent.

    Raises:
        ValueError: ``requested`` is not a thinking level, or it is ``"off"`` and
            the model's map marks ``"off"`` unsupported.
    """
    if requested is None:
        return None
    if not is_valid_thinking_level(requested):
        raise ValueError(
            f"Model {model.id!r}: {requested!r} is not a thinking level; expected one of "
            f"{', '.join(EXTENDED_THINKING_LEVELS)}."
        )
    level = cast(ModelThinkingLevel, requested)
    if level == "off":
        if (model.thinking_level_map or {}).get("off", _UNSET) is None:
            raise ValueError(
                f"Model {model.id!r}: thinking 'off' was requested, but "
                "thinking_level_map marks 'off' unsupported (null). Map it to the "
                "request that turns reasoning off on this endpoint, or request a level."
            )
        return "off"
    if not model.reasoning:
        return None
    return clamp_thinking_level(model, level)
