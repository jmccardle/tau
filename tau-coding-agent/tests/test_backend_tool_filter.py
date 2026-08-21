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


# ── the no_tools policy reaches AgentSession (pi #3592's third case) ────────


def _backend_with_an_extension_tool(**extra) -> TauBackend:
    """A backend whose session has one extension-registered tool.

    ``tools: []`` because BOTH suppression flags empty the built-in set — which
    is precisely why the built-in list cannot tell them apart, and why the
    forwarded ``no_tools`` has to.
    """
    backend = TauBackend(
        {"backend": "openai", "model": "m", "api_key": "not-needed", "tools": [], **extra}
    )
    backend.agent_session._registry.register_tool(
        {
            "name": "dynamic_tool",
            "label": "Dynamic Tool",
            "description": "Tool registered by an extension",
            "parameters": {"type": "object", "properties": {}},
            "execute": lambda *a, **k: {"content": [{"type": "text", "text": "ok"}]},
        }
    )
    return backend


def test_no_tools_all_reaches_the_session_and_suppresses_extension_tools():
    """The propagation seam: without this forwarding, ``--no-tools`` IS ``-nbt``."""
    backend = _backend_with_an_extension_tool(no_tools="all")
    assert backend.agent_session._no_tools == "all"
    assert backend.agent_session._build_turn_tools() == []


def test_no_tools_builtin_leaves_the_extension_tool_offered():
    backend = _backend_with_an_extension_tool(no_tools="builtin")
    assert [t.name for t in backend.agent_session._build_turn_tools()] == ["dynamic_tool"]


def test_no_policy_on_the_config_is_no_policy_on_the_session():
    """A config entry without the key leaves the session at today's default."""
    backend = _backend_with_an_extension_tool()
    assert backend.agent_session._no_tools is None
    assert [t.name for t in backend.agent_session._build_turn_tools()] == ["dynamic_tool"]
