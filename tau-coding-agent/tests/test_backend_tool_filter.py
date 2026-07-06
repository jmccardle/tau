"""E5 §2.3 (S28) — TauBackend consumes the --exclude-tools denylist.

``--exclude-tools`` was parsed and staged onto the run config (resolve_model_config)
but read by nobody. TauBackend now applies it to the resolved built-in tool set, so
both run paths (headless + TUI go through create_backend) honour the flag. Extension
tools are NOT subject to this built-in denylist (pi excludeTools targets built-ins).

TauBackend.__init__ does no network — it only builds the model + resolves tools —
so these assert directly on the constructed session's tool set.
"""

from __future__ import annotations

from tau_coding_agent.backends import TauBackend


def _tool_names(backend: TauBackend) -> set[str]:
    return {t.name for t in backend.agent_session._tools}


def test_exclude_tools_drops_named_builtins():
    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "api_key": "not-needed",
            "tools": ["read", "write", "edit", "bash", "ls"],
            "exclude_tools": ["bash", "write"],
        }
    )
    assert _tool_names(backend) == {"read", "edit", "ls"}


def test_exclude_tools_absent_keeps_all():
    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "api_key": "not-needed",
            "tools": ["read", "bash"],
        }
    )
    assert _tool_names(backend) == {"read", "bash"}


def test_exclude_all_configured_tools_yields_empty():
    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "api_key": "not-needed",
            "tools": ["read", "bash"],
            "exclude_tools": ["read", "bash"],
        }
    )
    assert _tool_names(backend) == set()
