"""W2/G0 — the config→Model seam carries the decode-capability fields.

``build_model_from_config`` is the single place a ``~/.tau/config.json`` models entry
(or a ``--model`` ad-hoc dict) becomes a ``Model``. Fail-Early on malformed values
rather than silently dropping them: a typo'd ``grammar`` key that got ignored would
leave the user believing constrained decoding was on when every call ran free.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §2.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.backends import build_model_from_config


class TestGrammarDialect:
    def test_absent_means_no_grammar_support(self):
        assert build_model_from_config({"model": "m"}).grammar_dialect is None

    @pytest.mark.parametrize("dialect", ["llguidance", "gbnf"])
    def test_valid_dialects_are_carried(self, dialect):
        model = build_model_from_config({"model": "m", "grammar": dialect})
        assert model.grammar_dialect == dialect

    def test_unknown_dialect_raises(self):
        """A typo must not degrade to 'no grammar support' — that reads as working."""
        with pytest.raises(ValueError, match="must be 'llguidance' or 'gbnf'"):
            build_model_from_config({"model": "m", "grammar": "llguidence"})


class TestExtraBody:
    def test_absent_is_empty(self):
        assert build_model_from_config({"model": "m"}).extra_body == {}

    def test_carried_through(self):
        model = build_model_from_config(
            {"model": "m", "extra_body": {"cache_prompt": True, "min_p": 0.05}}
        )
        assert model.extra_body == {"cache_prompt": True, "min_p": 0.05}

    def test_non_object_raises(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            build_model_from_config({"model": "m", "extra_body": ["cache_prompt"]})

    def test_config_dict_is_copied_not_aliased(self):
        """Mutating a Model must not write back into the user's loaded config."""
        config = {"model": "m", "extra_body": {"cache_prompt": True}}
        model = build_model_from_config(config)
        model.extra_body["min_p"] = 0.1

        assert config["extra_body"] == {"cache_prompt": True}


class TestServerFeatures:
    def test_absent_is_empty(self):
        assert build_model_from_config({"model": "m"}).server_features == []

    def test_carried_through(self):
        model = build_model_from_config(
            {"model": "m", "server_features": ["jump_forward", "slot_fork"]}
        )
        assert model.server_features == ["jump_forward", "slot_fork"]

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="must be a list of strings"):
            build_model_from_config({"model": "m", "server_features": "jump_forward"})


def test_a_realistic_local_llm_entry():
    """The shape the shipped local-llm config template will grow (§2)."""
    model = build_model_from_config(
        {
            "backend": "openai",
            "model": "qwen3-8b",
            "base_url": "http://192.168.1.100:8080/v1",
            "grammar": "llguidance",
            "extra_body": {"cache_prompt": True},
            "server_features": ["jump_forward"],
        }
    )

    assert model.grammar_dialect == "llguidance"
    assert model.extra_body == {"cache_prompt": True}
    assert model.server_features == ["jump_forward"]
    assert model.provider == "openai"
