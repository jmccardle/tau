"""τ-coding-agent: Interactive TUI + CLI for τ-agent-core.

Public API:
- AgentSession: Re-exported from τ-agent-core
- SessionManager: Re-exported from τ-agent-core
- AgentEvent: Re-exported from τ-agent-core

Reference: SUBPHASE-0.0.md, "AgentSession Interface" section.
"""

# Single source of truth for τ's release version. The CLI ``--version`` flag and
# ``package.sh`` both read this literal, so a release bumps one line.
__version__ = "0.9.0"

from tau_agent_core import (
    AgentSession,
    SessionManager,
    AgentEvent,
    ExtensionAPI,
    ExtensionContext,
)

__all__ = [
    "__version__",
    "AgentSession",
    "SessionManager",
    "AgentEvent",
    "ExtensionAPI",
    "ExtensionContext",
]
