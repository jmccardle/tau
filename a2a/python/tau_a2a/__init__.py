"""An A2A 1.0 server for a τ session (`tau-a2a`). It is a client of `tau serve` through τ's public
API: each A2A context is a τ cursor, an open question is `input-required`, and the answer is the
next message on the same task. Auth is none or a standard bearer JWT (issuer, audience, keys)."""

from tau_a2a._app import Settings, agent_card, create_app
from tau_a2a._auth import JwtSettings
from tau_a2a._executor import FORK_EXTENSION

__all__ = ["FORK_EXTENSION", "JwtSettings", "Settings", "agent_card", "create_app"]
