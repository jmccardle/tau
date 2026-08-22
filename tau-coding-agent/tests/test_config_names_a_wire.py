"""A ``~/.tau/config.json`` model entry can name its wire protocol.

``build_model_from_config`` hardcoded ``api="openai-completions"``. The api
registry existed, ``client._resolve_request`` dispatched through it, and no
config user could reach any of it — a knob only a library caller could turn.

Resolution order here, and the polarity rule from PLAN-0.9.3 §4.5: a stated
value wins, then the registered vendor's own protocol, then the historical
default. An unrecognised stated value raises against the registry rather than
falling through to the OpenAI wire, because a model silently served over the
wrong protocol is the exact failure that got ``openai-responses`` unregistered.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md §5.
"""

import pytest

from tau_coding_agent import backends


class TestNothingExistingChanges:
    """Every config that worked before must build the same Model."""

    def test_a_bare_config_still_gets_the_openai_wire(self):
        model = backends.build_model_from_config({"model": "gpt-4o"})
        assert model.api == "openai-completions"
        assert model.provider == "openai"
        assert model.base_url == "https://api.openai.com/v1"

    def test_a_local_server_keeps_its_own_base_url_and_the_openai_wire(self):
        model = backends.build_model_from_config(
            {
                "model": "local-llm",
                "backend": "local",
                "base_url": "http://127.0.0.1:8080/v1",
            }
        )
        assert model.api == "openai-completions"
        assert model.provider == "local"
        assert model.base_url == "http://127.0.0.1:8080/v1"

    def test_an_unregistered_vendor_still_defaults_to_the_openai_wire(self):
        """Groq, OpenRouter and every other OpenAI-compatible vendor τ does not
        ship a spec for."""
        model = backends.build_model_from_config(
            {"model": "llama-3", "backend": "groq", "base_url": "https://api.groq.com/openai/v1"}
        )
        assert model.api == "openai-completions"


class TestTheVendorSuppliesTheWire:
    def test_backend_anthropic_selects_the_anthropic_wire(self):
        model = backends.build_model_from_config(
            {"model": "claude-opus-5", "backend": "anthropic"}
        )
        assert model.api == "anthropic-messages"
        assert model.provider == "anthropic"

    def test_and_the_vendors_endpoint_rather_than_openais(self):
        """Defaulting every model to OpenAI's URL would point the Anthropic
        client at the wrong server."""
        model = backends.build_model_from_config(
            {"model": "claude-opus-5", "backend": "anthropic"}
        )
        assert model.base_url == "https://api.anthropic.com"

    def test_a_stated_base_url_still_wins(self):
        model = backends.build_model_from_config(
            {
                "model": "claude-opus-5",
                "backend": "anthropic",
                "base_url": "https://gateway.internal/anthropic",
            }
        )
        assert model.base_url == "https://gateway.internal/anthropic"


class TestAStatedApi:
    def test_it_wins_over_the_vendor_default(self):
        """A gateway that speaks the OpenAI wire while carrying an Anthropic
        model name is a real deployment, and this is how it says so."""
        model = backends.build_model_from_config(
            {
                "model": "claude-opus-5",
                "backend": "anthropic",
                "api": "openai-completions",
                "base_url": "https://gateway.internal/v1",
            }
        )
        assert model.api == "openai-completions"

    def test_an_unknown_wire_raises_and_names_what_is_registered(self):
        with pytest.raises(ValueError, match="openai-completions"):
            backends.build_model_from_config({"model": "m", "api": "openai-responses"})

    def test_the_unknown_case_never_falls_through_to_the_openai_wire(self):
        with pytest.raises(ValueError):
            backends.build_model_from_config({"model": "m", "api": "not-a-protocol"})


class TestStrictReasoningFormats:
    def test_it_defaults_off(self):
        model = backends.build_model_from_config({"model": "gpt-4o"})
        assert model.strict_reasoning_formats is False

    def test_it_is_reachable_from_config(self):
        model = backends.build_model_from_config(
            {"model": "gpt-4o", "strict_reasoning_formats": True}
        )
        assert model.strict_reasoning_formats is True

    def test_a_non_boolean_raises(self):
        with pytest.raises(ValueError, match="must be a boolean"):
            backends.build_model_from_config({"model": "gpt-4o", "strict_reasoning_formats": "yes"})
