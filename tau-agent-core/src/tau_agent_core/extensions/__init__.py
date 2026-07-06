"""τ-agent-core extensions system.

Public exports:
- ExtensionAPI: API exposed to extension modules
- ExtensionContext: Context passed to extension handlers
- ExtensionUI: User interaction interface
- ExtensionRegistry: Registry for extension tools

The single extension loader lives in ``tau_agent_core.sdk`` (``_load_extensions``)
— there is no separate ExtensionLoader class (removed in E0/S1).

Reference: PHASE-3-SUBPHASE-0.md, docs/EXTENSIONS-IMPLEMENTATION.md E0.1.
"""

from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import (
    ExtensionError,
    ExtensionHandlers,
    ExtensionRunner,
)

__all__ = [
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionUI",
    "ExtensionRegistry",
    "ExtensionRunner",
    "ExtensionHandlers",
    "ExtensionError",
]
