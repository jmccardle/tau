"""TauBackend reasoning/thinking wiring.

A ``thinking`` level in the model config (set by --thinking or a model:level
suffix) must enable ``Model.reasoning`` and thread the level into the
AgentSession; a config-declared ``reasoning`` capability and
``thinking_level_map`` must flow onto the Model. Verified by inspecting the
constructed AgentSession (no LLM call).
"""

from __future__ import annotations

import pytest

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


def test_thinking_level_enables_reasoning_and_threads():
    b = TauBackend(_cfg(thinking="high"))
    assert b.agent_session._reasoning == "high"
    assert b.agent_session._model.reasoning is True


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
