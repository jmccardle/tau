"""The config→Model seam carries the model's own limits, and its wire quirks.

``context_window`` and ``max_tokens`` were **hardcoded** in
``build_model_from_config`` — 128000 and 4096 for every model in existence, with
no config key reaching either. A 32k local model therefore told the compactor it
had a 128k window, and a model able to emit 128k tokens was capped at 4096. Both
numbers are per-model facts, and ``python -m tau_llm.catalog config`` now emits
them from models.dev, so the seam has to read them.

``compat`` is the other half: which spelling of the output cap this endpoint
accepts, and whether it tolerates ``stream_options``. Absent means τ infers both
from the base URL (:func:`tau_llm.compat.detect_compat`).

Reference: docs/PLAN-0.9.3.md §4.4.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.backends import build_model_from_config


class TestLimits:
    def test_absent_limits_keep_the_historical_defaults(self):
        """Changing what an EXISTING config resolves to is a separate decision
        from making the keys reachable, so the old numbers still stand."""
        model = build_model_from_config({"model": "m"})
        assert model.context_window == 128000
        assert model.max_tokens == 4096

    def test_both_limits_are_carried(self):
        model = build_model_from_config(
            {"model": "m", "context_window": 262144, "max_tokens": 8192}
        )
        assert model.context_window == 262144
        assert model.max_tokens == 8192

    @pytest.mark.parametrize("value", [0, -1, "128000", 1.5, None])
    def test_a_nonsense_context_window_raises(self, value):
        with pytest.raises(ValueError, match="context_window"):
            build_model_from_config({"model": "m", "context_window": value})

    @pytest.mark.parametrize("value", [0, -1, "4096", 1.5, None])
    def test_a_nonsense_max_tokens_raises(self, value):
        """Fail-Early: a string here would reach the wire as a string and the
        server's complaint would name the field, not the config line."""
        with pytest.raises(ValueError, match="max_tokens"):
            build_model_from_config({"model": "m", "max_tokens": value})

    def test_a_bool_is_not_a_limit(self):
        """``True`` is an ``int`` in Python, and 1 token is not a context window."""
        with pytest.raises(ValueError):
            build_model_from_config({"model": "m", "context_window": True})


class TestCompat:
    def test_absent_compat_leaves_detection_in_charge(self):
        assert build_model_from_config({"model": "m"}).compat is None

    def test_a_stated_spelling_is_carried(self):
        model = build_model_from_config(
            {"model": "m", "compat": {"max_tokens_field": "max_completion_tokens"}}
        )
        assert model.compat is not None
        assert model.compat.max_tokens_field == "max_completion_tokens"

    def test_suppressing_usage_in_streaming_is_carried(self):
        model = build_model_from_config(
            {"model": "m", "compat": {"supports_usage_in_streaming": False}}
        )
        assert model.compat is not None
        assert model.compat.supports_usage_in_streaming is False

    def test_a_non_object_compat_raises(self):
        with pytest.raises(ValueError, match="compat"):
            build_model_from_config({"model": "m", "compat": "max_tokens"})

    def test_an_unknown_spelling_raises_at_config_load(self):
        """Not at the first request, and not as an upstream 400 three files away."""
        with pytest.raises(ValueError):
            build_model_from_config({"model": "m", "compat": {"max_tokens_field": "max_out"}})

    def test_an_empty_compat_object_is_the_same_as_none(self):
        """``"compat": {}`` states nothing, so detection still decides everything."""
        assert build_model_from_config({"model": "m", "compat": {}}).compat is None


class TestTheDefaultLocalEndpointStillGetsTheClassicSpelling:
    def test_a_config_without_a_backend_key_is_not_treated_as_openai(self):
        """``build_model_from_config`` defaults ``backend`` to ``"openai"``, so
        that string means "unstated" far more often than it means OpenAI. This
        pins that the resolved compat follows the URL and not that default —
        the case that would otherwise break every local llama.cpp config."""
        from tau_llm.compat import resolve_compat

        model = build_model_from_config({"model": "m", "base_url": "http://127.0.0.1:8080/v1"})
        assert model.provider == "openai"
        assert resolve_compat(model).max_tokens_field == "max_tokens"
