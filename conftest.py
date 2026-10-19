"""Repo-wide test setup: no test may resolve the tester's own τ home.

``TAU_HOME`` and ``TAU_PORTABLE_BASE`` are removed from ``os.environ`` when
this file is imported, which pytest does before it imports any package's
``tests/conftest.py`` or test module. A session fixture would be too late:
``tau_coding_agent.config.TAU_DIR`` resolves the home at import, during
collection. Child processes inherit the cleaned environment; a subprocess
test still builds its env with ``tau_agent_core.testing.child_env``.

Rationale: docs/PORTABLE-HOME.md §7.
"""

from __future__ import annotations

import os

from tau_agent_core.tau_home import HOME_ENV_VARS

for _name in HOME_ENV_VARS:
    os.environ.pop(_name, None)
