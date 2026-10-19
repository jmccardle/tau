"""A model that cannot be used: its error, and a session's recorded one (docs/SILENT-DEFAULTS.md §9-10)."""

from __future__ import annotations

from dataclasses import dataclass

from tau_llm.docs import agent_facing


@agent_facing(topic="sessions")
class ModelUnavailableError(ValueError):
    """A model name that cannot be used as configured; the message says why.

    ``set_model`` raises it for a config entry that does not build, so an
    extension catches it without importing a head. An unknown name raises
    ``KeyError`` instead.
    """


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class UnavailableModel:
    """A resumed session's recorded model that no longer resolves or builds (§10).

    Stands where a ``Model`` would, so the session opens and its tree reads; a
    turn on it is refused with :meth:`message`. It is never a substitute model.

    Attributes:
        name: The recorded config name.
        reason: Why it does not resolve, as the head's resolution said.
    """

    name: str
    reason: str

    def message(self) -> str:
        """What a turn on this model is refused with: the model, why, and the fix."""
        return (
            f"this session's model {self.name!r} is unavailable: {self.reason}. Switch "
            "to a configured model (/model, the set_model verb, or ctx.set_model), or "
            f"add {self.name!r} back to config.json"
        )
