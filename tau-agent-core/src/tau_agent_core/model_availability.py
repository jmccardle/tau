"""The core's name for a model that cannot be used (docs/SILENT-DEFAULTS.md §9)."""

from __future__ import annotations

from tau_llm.docs import agent_facing


@agent_facing(topic="sessions")
class ModelUnavailableError(ValueError):
    """A model name that cannot be used as configured; the message says why.

    ``set_model`` raises it for a config entry that does not build, so an
    extension catches it without importing a head. An unknown name raises
    ``KeyError`` instead.
    """
