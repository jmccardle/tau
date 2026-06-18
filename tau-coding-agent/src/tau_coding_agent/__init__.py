"""τ-coding-agent: Interactive TUI + CLI for τ-agent-core.

Public API:
- AgentSession: Re-exported from τ-agent-core
- SessionManager: Re-exported from τ-agent-core
- AgentEvent: Re-exported from τ-agent-core

Reference: SUBPHASE-0.0.md, "AgentSession Interface" section.
"""

from tau_agent_core import (
    AgentSession,
    SessionManager,
    AgentEvent,
    ExtensionAPI,
    ExtensionContext,
)

__all__ = [
    "AgentSession",
    "SessionManager",
    "AgentEvent",
    "ExtensionAPI",
    "ExtensionContext",
]
