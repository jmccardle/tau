"""TauBackend forwards the turn ceiling, and invents one when nobody states it.

``max_turns`` bounds how many LLM calls one run may make. It used to be a
hardcoded 50 inside ``AgentLoopConfig`` that no caller τ ships could reach:
``create_agent_session`` had no such parameter, no CLI flag set it, and no config
key was read. Every TUI session and every ``tau -p`` run therefore stopped at turn
50 whether or not the work was finished.

The default is now ``None`` — no ceiling, which is also pi's behaviour
(``agent-loop.ts:155-275``) — and the number is stated by ``--max-turns``, a model
entry, or config.json's top-level ``max_turns``. This file covers the last hop:
model config → ``AgentSession`` → ``AgentLoopConfig``. The precedence between the
three sources is covered in ``test_cli.py`` (headless) and
``test_app_extension_loading.py`` (TUI). No LLM call is made.
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
    monkeypatch.chdir(tmp_path)


def test_no_ceiling_in_the_config_means_no_ceiling_in_the_loop():
    """The whole point of the change: a default run is not cut off at any turn."""
    session = TauBackend(_cfg()).agent_session
    assert session._max_turns is None
    # An empty kwargs dict is how the session declines to name a number, leaving
    # AgentLoopConfig's own default as the single definition.
    assert session._turn_cap() == {}


def test_a_stated_ceiling_reaches_the_loop_config():
    session = TauBackend(_cfg(max_turns=12)).agent_session
    assert session._max_turns == 12
    assert session._turn_cap() == {"max_turns": 12}


def test_a_nonsense_ceiling_in_the_config_fails_at_construction():
    """``"max_turns": 0`` in ``~/.tau/config.json`` must fail when the backend is
    built, not as a pydantic error on the first prompt with no mention of the
    config file. ``--max-turns 0`` is caught earlier still, at the argv boundary.
    """
    with pytest.raises(ValueError, match="max_turns must be at least 1"):
        TauBackend(_cfg(max_turns=0))
