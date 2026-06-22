"""τ-ai models: thinking/reasoning level helpers.

Port of the thinking-level portion of pi's ``packages/ai/src/models.ts``
(``EXTENDED_THINKING_LEVELS``, ``getSupportedThinkingLevels``,
``clampThinkingLevel``). These decide which reasoning effort a given
:class:`~tau_ai.types.Model` actually accepts and clamp a requested level to the
nearest supported one, so a caller can ask for "xhigh" against a model that tops
out at "high" and get "high" rather than an upstream 400.

Reference: pi ``packages/ai/src/models.ts:51-84``.
"""

from __future__ import annotations

from typing import Literal

from tau_ai.types import Model

# User-facing levels (no "off"). pi: `ThinkingLevel` (types.ts:65).
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]
# Including "off". pi: `ModelThinkingLevel` (types.ts:66).
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]

# Ordered from least to most effort. pi: `EXTENDED_THINKING_LEVELS`
# (models.ts:51). Order is load-bearing — clamping walks it.
EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)

# Default level when reasoning is requested without a specific level
# (pi `DEFAULT_THINKING_LEVEL`).
DEFAULT_THINKING_LEVEL: ModelThinkingLevel = "medium"


# Sentinel distinguishing "key absent from the map" from "key present with value
# None" — the two mean different things (pass-through vs. explicitly
# unsupported), so a plain ``.get(level)`` returning None would conflate them.
class _Unset:
    __slots__ = ()


_UNSET = _Unset()


def is_valid_thinking_level(level: str) -> bool:
    """True if ``level`` is one of the known levels ("off".."xhigh")."""
    return level in EXTENDED_THINKING_LEVELS


def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    """Return the levels ``model`` supports, least → most effort.

    A non-reasoning model supports only ``["off"]``. Otherwise every level is
    supported except those the ``thinking_level_map`` explicitly nulls out, with
    one special case: ``"xhigh"`` is available only when the map provides an
    entry for it (it is a non-standard extension). Mirrors pi
    ``getSupportedThinkingLevels`` (models.ts:53-63).
    """
    if not model.reasoning:
        return ["off"]

    tlm = model.thinking_level_map or {}
    supported: list[ModelThinkingLevel] = []
    for level in EXTENDED_THINKING_LEVELS:
        mapped = tlm.get(level, _UNSET)
        if mapped is None:
            # Explicitly marked unsupported.
            continue
        if level == "xhigh" and mapped is _UNSET:
            # xhigh only exists when the map names a concrete value for it.
            continue
        supported.append(level)
    return supported


def clamp_thinking_level(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    """Clamp ``level`` to the nearest level ``model`` actually supports.

    If the exact level is supported it is returned unchanged. Otherwise search
    upward (more effort) first, then downward, falling back to the lowest
    supported level (``"off"`` for a non-reasoning model). Mirrors pi
    ``clampThinkingLevel`` (models.ts:64-83).
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
