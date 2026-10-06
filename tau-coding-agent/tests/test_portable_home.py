"""The coding agent's TAU_DIR honours a bundle's portable ``.tau/`` (docs/PORTABLE-HOME.md).

``config.TAU_DIR`` is fixed at import, so each case runs in a fresh interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tau_agent_core.tau_home import PORTABLE_BASE_ENV


def _tau_dir(env: dict[str, str]) -> str:
    out = subprocess.run(
        [sys.executable, "-c", "import tau_coding_agent.config as c; print(c.TAU_DIR)"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_portable_base_with_dot_tau_is_tau_dir(tmp_path: Path) -> None:
    (tmp_path / "drive" / ".tau").mkdir(parents=True)
    env = {"HOME": str(tmp_path / "home"), PORTABLE_BASE_ENV: str(tmp_path / "drive")}
    assert _tau_dir(env) == str((tmp_path / "drive" / ".tau").resolve())


def test_no_portable_base_is_home(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home"), PORTABLE_BASE_ENV: ""}
    assert _tau_dir(env) == str(tmp_path / "home" / ".tau")
