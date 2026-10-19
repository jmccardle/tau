"""The coding agent's TAU_DIR honours a bundle's portable ``.tau/`` (docs/PORTABLE-HOME.md).

``config.TAU_DIR`` is fixed at import, so each case runs in a fresh interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tau_agent_core.tau_home import HOME_ENV, PORTABLE_BASE_ENV
from tau_agent_core.testing import child_env


def _tau_dir(home: Path, **env: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", "import tau_coding_agent.config as c; print(c.TAU_DIR)"],
        env=child_env(home, **env),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_portable_base_with_dot_tau_is_tau_dir(tmp_path: Path) -> None:
    (tmp_path / "drive" / ".tau").mkdir(parents=True)
    env = {PORTABLE_BASE_ENV: str(tmp_path / "drive")}
    assert _tau_dir(tmp_path / "home", **env) == str((tmp_path / "drive" / ".tau").resolve())


def test_no_portable_base_is_home(tmp_path: Path) -> None:
    assert _tau_dir(tmp_path / "home", **{PORTABLE_BASE_ENV: ""}) == str(tmp_path / "home" / ".tau")


def test_tau_home_is_tau_dir(tmp_path: Path) -> None:
    (tmp_path / "drive" / ".tau").mkdir(parents=True)
    (tmp_path / "throwaway").mkdir()
    env = {PORTABLE_BASE_ENV: str(tmp_path / "drive"), HOME_ENV: str(tmp_path / "throwaway")}
    assert _tau_dir(tmp_path / "home", **env) == str((tmp_path / "throwaway").resolve())


def test_cli_refuses_a_missing_tau_home_in_one_line(tmp_path: Path) -> None:
    missing = tmp_path / "typo"
    env = child_env(tmp_path / "home", **{HOME_ENV: str(missing)})
    out = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-p", "hi"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert out.returncode == 2
    assert out.stdout == ""
    assert out.stderr == (
        f"tau: error: TAU_HOME={missing} is not a directory; create it or unset the variable\n"
    )
