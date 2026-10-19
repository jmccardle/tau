"""An environment for a τ child process whose home is only the one you name.

Rationale: docs/PORTABLE-HOME.md §7.
"""

from __future__ import annotations

import os
from pathlib import Path

from tau_agent_core.tau_home import HOME_ENV_VARS

__all__ = ["child_env"]


def child_env(home: str | Path, **overrides: str) -> dict[str, str]:
    """This process's environment with ``$HOME`` set and no inherited τ home.

    Drops every variable in :data:`~tau_agent_core.tau_home.HOME_ENV_VARS`, so
    the child resolves ``<home>/.tau`` whatever the parent shell set.

    Args:
        home: The child's ``$HOME``.
        **overrides: Further variables, applied last; pass ``TAU_HOME`` here to
            test it deliberately.

    Returns:
        A fresh dict, ready for ``subprocess``'s ``env=``.
    """
    env = {k: v for k, v in os.environ.items() if k not in HOME_ENV_VARS}
    env["HOME"] = str(home)
    env.update(overrides)
    return env
