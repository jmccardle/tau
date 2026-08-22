"""0.9.3 §1 — TauBackend BUILDS the system prompt instead of copying a config key.

The §1 diagnosis in docs/PLAN-0.9.3.md is that a config default shadowed a live
loader. Half of that is right and half is not: ``_build_system_prompt`` sits
behind ``create_agent_session``, and **nothing in this package calls it** —
``TauBackend`` constructs ``AgentSession`` directly. So dropping
``system_prompt`` from ``tau_default_config.json`` (133e74d) left the TUI and
every headless run sending the empty string: not τ's base prompt, and no context
files either. These tests pin the seam that fixes it.

``TauBackend.__init__`` does no network — it builds the model, resolves tools and
composes the prompt — so they assert directly on the constructed backend.

Every test ``chdir``s into ``tmp_path``: discovery reads the real filesystem from
cwd upwards, and assertions are written on markers rather than on the whole
prompt so a developer's own ``~/.tau/AGENTS.md`` cannot decide the result.
"""

from __future__ import annotations

import pytest

from tau_agent_core.sdk import BASE_SYSTEM_PROMPT
from tau_coding_agent.backends import TauBackend


def _cfg(**extra) -> dict:
    return {
        "backend": "openai",
        "model": "m",
        "api_key": "not-needed",
        "tools": ["read"],
        **extra,
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with an AGENTS.md, and cwd pointed at it."""
    (tmp_path / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_default_install_gets_tau_s_base_prompt(tmp_path, monkeypatch):
    """The regression 133e74d could not fix on its own: with no ``system_prompt``
    key anywhere, the backend used to send ``""``."""
    monkeypatch.chdir(tmp_path)

    backend = TauBackend(_cfg())

    assert backend.agent_session._system_prompt.startswith(BASE_SYSTEM_PROMPT)


def test_a_projects_agents_md_reaches_the_model(project):
    backend = TauBackend(_cfg())

    prompt = backend.agent_session._system_prompt
    assert "PROJECT MARKER" in prompt
    assert str(project / "AGENTS.md") in prompt


def test_an_ancestor_agents_md_reaches_the_model_from_a_subdirectory(project, monkeypatch):
    """Running ``tau`` from ``src/`` must not lose the repo's instructions."""
    sub = project / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)

    assert "PROJECT MARKER" in TauBackend(_cfg()).agent_session._system_prompt


def test_a_configured_system_prompt_replaces_the_base_text_only(project):
    """Composition: setting ``system_prompt`` used to switch context files off."""
    backend = TauBackend(_cfg(system_prompt="CUSTOM VOICE"))

    prompt = backend.agent_session._system_prompt
    assert prompt.startswith("CUSTOM VOICE")
    assert BASE_SYSTEM_PROMPT not in prompt
    assert "PROJECT MARKER" in prompt


def test_no_context_files_suppresses_discovery(project):
    """``--no-context-files``/``-nc`` arrives on the model config as this key."""
    backend = TauBackend(_cfg(no_context_files=True))

    prompt = backend.agent_session._system_prompt
    assert "PROJECT MARKER" not in prompt
    assert "project_context" not in prompt
    assert prompt.startswith(BASE_SYSTEM_PROMPT)


def test_no_context_files_still_leaves_the_tools_section(project):
    backend = TauBackend(_cfg(no_context_files=True))

    assert "Available tools:" in backend.agent_session._system_prompt
