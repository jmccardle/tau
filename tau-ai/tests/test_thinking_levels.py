"""Unit tests for the thinking-level helpers (pi models.ts parity).

Covers get_supported_thinking_levels / clamp_thinking_level across the cases
that matter: non-reasoning models, the default (no map) level set, xhigh gating
on a map entry, and explicit per-level nulling.

Reference: pi ``packages/ai/src/models.ts:51-84``.
"""

from __future__ import annotations

from tau_ai.models import (
    EXTENDED_THINKING_LEVELS,
    clamp_thinking_level,
    get_supported_thinking_levels,
    is_valid_thinking_level,
)
from tau_ai.types import Model


def _model(**overrides) -> Model:
    defaults: dict = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "u",
        "context_window": 1,
        "max_tokens": 1,
    }
    defaults.update(overrides)
    return Model(**defaults)


def test_levels_order():
    assert EXTENDED_THINKING_LEVELS == (
        "off", "minimal", "low", "medium", "high", "xhigh",
    )


def test_is_valid_thinking_level():
    assert is_valid_thinking_level("off")
    assert is_valid_thinking_level("xhigh")
    assert not is_valid_thinking_level("ultra")
    assert not is_valid_thinking_level("")


def test_non_reasoning_model_supports_only_off():
    m = _model(reasoning=False)
    assert get_supported_thinking_levels(m) == ["off"]
    # Any requested level clamps down to off.
    assert clamp_thinking_level(m, "high") == "off"
    assert clamp_thinking_level(m, "off") == "off"


def test_reasoning_model_no_map_excludes_xhigh():
    m = _model(reasoning=True)
    assert get_supported_thinking_levels(m) == [
        "off", "minimal", "low", "medium", "high",
    ]
    # xhigh isn't available without a map entry → clamps to the nearest (high).
    assert clamp_thinking_level(m, "xhigh") == "high"
    # Supported levels pass through unchanged.
    assert clamp_thinking_level(m, "medium") == "medium"


def test_xhigh_available_when_mapped():
    m = _model(reasoning=True, thinking_level_map={"xhigh": "max"})
    assert "xhigh" in get_supported_thinking_levels(m)
    assert clamp_thinking_level(m, "xhigh") == "xhigh"


def test_explicit_null_marks_level_unsupported():
    # Null out "minimal": a request for it clamps UP to the next supported (low).
    m = _model(reasoning=True, thinking_level_map={"minimal": None})
    supported = get_supported_thinking_levels(m)
    assert "minimal" not in supported
    assert clamp_thinking_level(m, "minimal") == "low"
