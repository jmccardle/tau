"""Root conftest.py — Shared test utilities for the τ monorepo.

Provides fixtures for workspace-level tests (subphases 0.1, 0.2).
"""

import platform
import subprocess
import sys
from pathlib import Path
from typing import Generator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Path to the root of the τ monorepo."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def python_version() -> tuple[int, int, int]:
    """Return the Python version as (major, minor, micro)."""
    return sys.version_info


@pytest.fixture(scope="session")
def python_gte_311(python_version: tuple[int, int, int]) -> bool:
    """Check if Python is 3.11 or greater."""
    return python_version >= (3, 11)


@pytest.fixture
def venv_path(repo_root: Path) -> Path:
    """Path to the virtual environment directory."""
    return repo_root / "venv"


@pytest.fixture
def docs_dir(repo_root: Path) -> Path:
    """Path to the docs directory."""
    return repo_root / "docs"


@pytest.fixture
def tau_ai_dir(repo_root: Path) -> Path:
    """Path to the tau-ai package directory."""
    return repo_root / "tau-ai"


@pytest.fixture
def tau_agent_core_dir(repo_root: Path) -> Path:
    """Path to the tau-agent-core package directory."""
    return repo_root / "tau-agent-core"


@pytest.fixture
def tau_coding_agent_dir(repo_root: Path) -> Path:
    """Path to the tau-coding-agent package directory."""
    return repo_root / "tau-coding-agent"


@pytest.fixture
def all_package_dirs(tau_ai_dir, tau_agent_core_dir, tau_coding_agent_dir):
    """All three package directories."""
    return [tau_ai_dir, tau_agent_core_dir, tau_coding_agent_dir]


@pytest.fixture
def run_command():
    """Run a shell command and return (stdout, stderr, returncode)."""
    def _run(cmd: str, timeout: int = 30, cwd: Path | None = None) -> tuple[str, str, int]:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd or REPO_ROOT),
        )
        return result.stdout, result.stderr, result.returncode
    return _run
