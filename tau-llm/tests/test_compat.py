"""Tests for :mod:`tau_llm.compat` — per-endpoint wire quirks.

Two things are worth pinning here and neither is the happy path.

The first is **polarity**. pi's ``detectCompat`` names the servers that want
``max_tokens`` and gives everyone else ``max_completion_tokens``; τ names the
servers that want ``max_completion_tokens`` and gives everyone else the classic
key. Under pi's rule a local llama.cpp — τ's most common endpoint — receives a
spelling it rejects, so the inversion is the design and not an oversight.

The second is that **``provider`` decides nothing**.
``build_model_from_config`` defaults an entry with no ``backend`` key to
``provider="openai"``, so in τ that string usually means "the operator did not
say". Reading it as identification would switch every such config.
"""

from __future__ import annotations

import pytest

from tau_llm.compat import Compat, ResolvedCompat, detect_compat, resolve_compat
from tau_llm.types import Model


def _model(**overrides) -> Model:
    defaults: dict = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "local-llm",
        "base_url": "http://127.0.0.1:8080/v1",
        "context_window": 1000,
        "max_tokens": 100,
    }
    defaults.update(overrides)
    return Model(**defaults)


class TestMaxTokensFieldDetection:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.openai.com/v1",
            "https://API.OpenAI.com/v1",
            "https://my-resource.openai.azure.com/openai/deployments/gpt-5",
        ],
    )
    def test_openai_and_azure_get_the_completion_spelling(self, base_url):
        assert detect_compat("whatever", base_url).max_tokens_field == "max_completion_tokens"

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost:11434/v1",
            "https://api.groq.com/openai/v1",
            "https://openrouter.ai/api/v1",
            "https://gateway.example.internal/v1",
        ],
    )
    def test_everyone_else_keeps_the_classic_spelling(self, base_url):
        """The polarity decision. An unrecognised endpoint is far more often a
        local server than a proxy in front of OpenAI, and the local servers
        reject ``max_completion_tokens``."""
        assert detect_compat("whatever", base_url).max_tokens_field == "max_tokens"

    def test_the_provider_name_is_not_consulted(self):
        """``provider="openai"`` is τ's default filler for an unnamed backend."""
        assert detect_compat("openai", "http://127.0.0.1:8080/v1").max_tokens_field == "max_tokens"

    def test_a_url_with_surrounding_whitespace_still_matches(self):
        assert (
            detect_compat("x", "  https://api.openai.com/v1  ").max_tokens_field
            == "max_completion_tokens"
        )


class TestUsageInStreaming:
    def test_detection_always_allows_it(self):
        """No endpoint τ has met rejects ``stream_options``, so there is nothing
        to detect. The field exists to be STATED, not inferred."""
        assert detect_compat("x", "http://anywhere/v1").supports_usage_in_streaming is True


class TestResolve:
    def test_no_compat_is_the_detected_value(self):
        assert resolve_compat(_model()) == detect_compat("local-llm", "http://127.0.0.1:8080/v1")

    def test_a_stated_field_wins_over_detection(self):
        model = _model(
            base_url="https://api.openai.com/v1",
            compat=Compat(max_tokens_field="max_tokens"),
        )
        assert resolve_compat(model).max_tokens_field == "max_tokens"

    def test_an_unset_field_falls_through_rather_than_resetting(self):
        """Stating one quirk must not silently revert another to a type default.

        This is the whole reason ``Compat``'s fields are ``None``-by-default
        instead of carrying real defaults: an operator suppressing
        ``stream_options`` on an OpenAI endpoint would otherwise also flip the
        cap's spelling back to the one that endpoint rejects.
        """
        model = _model(
            base_url="https://api.openai.com/v1",
            compat=Compat(supports_usage_in_streaming=False),
        )
        resolved = resolve_compat(model)
        assert resolved.supports_usage_in_streaming is False
        assert resolved.max_tokens_field == "max_completion_tokens"

    def test_resolution_is_fully_decided(self):
        resolved = resolve_compat(_model())
        assert isinstance(resolved, ResolvedCompat)
        assert None not in resolved.model_dump().values()


class TestModelField:
    def test_compat_defaults_to_none(self):
        assert _model().compat is None

    def test_an_unknown_max_tokens_spelling_is_refused(self):
        """Fail-Early at config load: a typo here would otherwise be discovered
        as an upstream 400 mid-turn."""
        with pytest.raises(ValueError):
            Compat(max_tokens_field="max_output_tokens")
