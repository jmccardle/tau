"""TauBackend reasoning/thinking wiring.

A ``thinking`` level in the model config (set by --thinking or a model:level
suffix) is threaded into the AgentSession as the REQUESTED level; a
config-declared ``reasoning`` capability and ``thinking_level_map`` flow onto the
Model. Requesting a level does NOT declare the capability — see the A2 block at
the bottom for why that inference was removed. Verified by inspecting the
constructed AgentSession (no LLM call).
"""

from __future__ import annotations

import pytest

from tau_coding_agent import backends
from tau_coding_agent.backends import TauBackend


def _cfg(**over) -> dict:
    base = {
        "backend": "openai",
        "model": "qwen",
        "base_url": "http://localhost/v1",
        "api_key": "not-needed",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # TauBackend now persists nothing itself (its AgentSession runs against a
    # scratch InMemorySessionLog — §2.6). Chdir to a temp dir anyway so any
    # incidental cwd-relative work stays out of the repo.
    monkeypatch.chdir(tmp_path)


def test_thinking_level_threads_but_no_longer_enables_reasoning():
    """The level reaches the session; the CAPABILITY is not inferred from it.

    Previously this asserted ``_model.reasoning is True`` — a requested level
    asserted support on the model's behalf, so ``reasoning_effort`` went out to
    endpoints that ignore it and nothing said so (A2). The requested level still
    threads, because a model that DOES declare support must still receive it.
    """
    b = TauBackend(_cfg(thinking="high"))
    assert b.agent_session._reasoning == "high"
    assert b.agent_session._model.reasoning is False


def test_off_does_not_enable_reasoning():
    b = TauBackend(_cfg(thinking="off"))
    assert b.agent_session._reasoning is None
    assert b.agent_session._model.reasoning is False


def test_no_thinking_means_no_reasoning():
    b = TauBackend(_cfg())
    assert b.agent_session._reasoning is None
    assert b.agent_session._model.reasoning is False


def test_config_reasoning_capability_and_map_flow_to_model():
    b = TauBackend(_cfg(reasoning=True, thinking_level_map={"xhigh": "max"}))
    assert b.agent_session._model.reasoning is True
    assert b.agent_session._model.thinking_level_map == {"xhigh": "max"}
    # Capability without a requested level → no reasoning option threaded.
    assert b.agent_session._reasoning is None


# ── A2: reasoning capability is declared, never inferred ─────────────────────


def test_a_requested_level_does_not_assert_reasoning_support(capsys):
    """``--thinking high`` against a config with no ``reasoning`` key leaves the
    model non-reasoning, and says so.

    This is the inference that made a dead parameter look alive: the level was
    asserted onto the model, `reasoning_effort` went out, and the endpoint ignored
    it in silence. ``Model.reasoning``'s own docstring has always said "opt in,
    don't guess capability"; this is the code finally agreeing with it.
    """
    backends._WARNED_UNDECLARED_REASONING.clear()
    model = backends.build_model_from_config(
        {"model": "undeclared", "backend": "openai", "thinking": "high"}
    )
    assert model.reasoning is False
    err = capsys.readouterr().err
    assert "undeclared" in err and '"reasoning": true' in err


def test_the_undeclared_warning_is_printed_once_per_model(capsys):
    """A resolver rebuilds the same Model on every ``set_model``; one
    misconfiguration is one finding, not one per call."""
    backends._WARNED_UNDECLARED_REASONING.clear()
    config = {"model": "undeclared", "backend": "openai", "thinking": "high"}
    for _ in range(3):
        backends.build_model_from_config(config)
    assert capsys.readouterr().err.count("does not declare reasoning support") == 1


def test_an_explicit_reasoning_declaration_still_enables_it(capsys):
    backends._WARNED_UNDECLARED_REASONING.clear()
    model = backends.build_model_from_config(
        {"model": "declared", "backend": "openai", "reasoning": True, "thinking": "high"}
    )
    assert model.reasoning is True
    assert capsys.readouterr().err == ""


# ── A1: thinking_level_map validation at config load ─────────────────────────


def test_a_fragment_carrying_a_numeric_string_is_refused():
    """Measured: llama.cpp accepts ``"0"`` and discards it, returning HTTP 200 and a
    generation identical to not sending the field. Only a JSON number is read, so a
    quoted number is a setting that silently does nothing."""
    with pytest.raises(ValueError, match="not the number"):
        backends.build_model_from_config(
            {
                "model": "x",
                "backend": "openai",
                "thinking_level_map": {"high": {"thinking_budget_tokens": "4096"}},
            }
        )


def test_a_key_that_names_no_thinking_level_is_refused():
    """A typo'd level maps nothing, and would do so in silence."""
    with pytest.raises(ValueError, match="not a thinking level"):
        backends.build_model_from_config(
            {"model": "x", "backend": "openai", "thinking_level_map": {"hgih": "high"}}
        )


def test_a_level_value_that_is_neither_string_nor_fragment_is_refused():
    with pytest.raises(ValueError, match="must be a string"):
        backends.build_model_from_config(
            {"model": "x", "backend": "openai", "thinking_level_map": {"high": 5}}
        )


def test_a_well_formed_fragment_map_reaches_the_model():
    model = backends.build_model_from_config(
        {
            "model": "x",
            "backend": "openai",
            "reasoning": True,
            "thinking_level_map": {
                "off": {"chat_template_kwargs": {"enable_thinking": False}},
                "high": {"thinking_budget_tokens": 4096},
                "low": "low",
            },
        }
    )
    assert model.thinking_level_map is not None
    assert model.thinking_level_map["high"] == {"thinking_budget_tokens": 4096}
