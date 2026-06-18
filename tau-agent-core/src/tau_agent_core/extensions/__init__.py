"""τ-agent-core extensions system.

Public exports:
- ExtensionAPI: API exposed to extension modules
- ExtensionContext: Context passed to extension handlers
- ExtensionUI: User interaction interface
- ExtensionLoader: Discovers and loads extensions
- ExtensionRegistry: Registry for extension tools
- ExtensionEvent: Events from the extension system

Reference: PHASE-3-SUBPHASE-0.md
"""

from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)
from tau_agent_core.extensions.loader import ExtensionLoader
from tau_agent_core.extensions.registry import ExtensionRegistry

__all__ = [
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionUI",
    "ExtensionLoader",
    "ExtensionRegistry",
]
