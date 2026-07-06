"""E6 §2 / S45 — the frontend model-name resolver seam.

``ctx.set_model(name)`` resolves a config model NAME through a resolver the
frontend binds onto the live ``AgentSession``. This checks the shared builder
(:func:`build_model_from_config`), the resolver factory
(:func:`make_model_resolver`), and that a ``TauBackend`` created straight from a
config resolves + switches models by name (the seam both frontends bind).
"""

from __future__ import annotations

import pytest

from tau_ai.types import Model

from tau_coding_agent.backends import (
    TauBackend,
    build_model_from_config,
    make_model_resolver,
)


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _models() -> dict:
    return {
        "local-llm": {"backend": "openai", "model": "qwen", "base_url": "http://localhost/v1"},
        "cloud": {"backend": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
    }


def test_build_model_from_config_maps_fields():
    model = build_model_from_config(_models()["cloud"])
    assert isinstance(model, Model)
    assert model.id == "gpt-4o"
    assert model.provider == "openai"
    assert model.base_url == "https://api.openai.com/v1"


def test_build_model_reasoning_replay_defaults_to_turn():
    """No ``reasoning_replay`` in the entry → the τ default scope ``turn``."""
    model = build_model_from_config(_models()["cloud"])
    assert model.reasoning_replay == "turn"


def test_build_model_reasoning_replay_per_model_value_respected():
    """A per-model ``reasoning_replay`` entry is carried onto the Model verbatim."""
    for scope in ("all", "turn", "off"):
        entry = {**_models()["cloud"], "reasoning_replay": scope}
        assert build_model_from_config(entry).reasoning_replay == scope


def test_build_model_reasoning_replay_invalid_raises():
    """An unknown scope is a config error, not a silent fallback (Fail-Early)."""
    entry = {**_models()["cloud"], "reasoning_replay": "sometimes"}
    with pytest.raises(ValueError, match="reasoning_replay must be one of"):
        build_model_from_config(entry)


def test_make_model_resolver_resolves_known_name():
    resolve = make_model_resolver(_models())
    model = resolve("local-llm")
    assert model.id == "qwen"
    assert model.base_url == "http://localhost/v1"


def test_make_model_resolver_unknown_name_raises():
    resolve = make_model_resolver(_models())
    with pytest.raises(KeyError, match="unknown model"):
        resolve("does-not-exist")


def test_backend_session_switches_model_by_name():
    """A resolver bound onto a TauBackend's session drives ctx.set_model end-to-end."""
    backend = TauBackend(_models()["local-llm"])
    session = backend.agent_session
    session.set_model_resolver(make_model_resolver(_models()))

    assert session.get_model()["id"] == "qwen"
    result = session._extension_api.context.set_model("cloud")
    assert result["id"] == "gpt-4o"
    assert session.get_model()["id"] == "gpt-4o"
    # The Model actually swapped on the loop-facing field (next-turn effect).
    assert session._model.base_url == "https://api.openai.com/v1"
