"""τ-coding-agent: Interactive TUI + CLI for τ-agent-core.

Public API:
- AgentSession: Re-exported from τ-agent-core
- SessionManager: Re-exported from τ-agent-core
- AgentEvent: Re-exported from τ-agent-core

Reference: SUBPHASE-0.0.md, "AgentSession Interface" section.
"""

# This package's release version. The CLI ``--version`` flag, ``package.sh`` and
# this distribution's own ``pyproject.toml`` (``[tool.setuptools.dynamic]``) all
# read this literal, so the number a user sees and the number on the wheel cannot
# disagree. The other three packages carry the same line; tests/test_packaging.py
# holds all four against each other, because they are released in lockstep.
__version__ = "0.9.2"

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
