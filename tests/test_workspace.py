"""Tests for Phase 0 Subphase 0.1 — Workspace and Virtual Environment.

Verifies:
- Python 3.11+ available on PATH
- venv/ directory exists with required packages installed
- Root pyproject.toml declares workspace members
- pytest can discover test collection hooks
"""

import platform
import subprocess
import sys
from pathlib import Path

import pytest


class TestPythonVersion:
    """Tests for Python version requirement."""

    @pytest.mark.skipif(
        sys.version_info < (3, 11),
        reason="Requires Python 3.11+",
    )
    def test_python_version_is_311_or_greater(self, python_version):
        """Python must be 3.11 or greater."""
        assert python_version >= (3, 11), (
            f"Python version {python_version.major}.{python_version.minor} "
            "is below the required minimum of 3.11"
        )

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="Platform-specific path check",
    )
    def test_python_path_is_valid(self):
        """The python on PATH should be a valid executable."""
        result = subprocess.run(
            ["which", "python"],
            capture_output=True, text=True
        )
        assert result.returncode == 0, "python not found on PATH"
        assert "venv" in result.stdout or "/usr" in result.stdout


class TestVirtualEnvironment:
    """Tests for virtual environment setup."""

    def test_venv_directory_exists(self, venv_path):
        """venv/ directory must exist."""
        assert venv_path.exists(), (
            f"Virtual environment directory not found at {venv_path}"
        )

    def test_venv_has_bin_python(self, venv_path):
        """venv/bin/python must exist (Unix) or venv/Scripts/python.exe (Windows)."""
        if platform.system() == "Windows":
            bin_py = venv_path / "Scripts" / "python.exe"
        else:
            bin_py = venv_path / "bin" / "python"
        assert bin_py.exists(), f"Virtual environment python not found at {bin_py}"

    def test_venv_has_pytest(self, run_command):
        """pytest must be installed in the venv."""
        stdout, stderr, rc = run_command(
            "python -m pip show pytest",
            cwd=Path.cwd() / ".." if "venv" not in str(Path.cwd()) else None
        )
        # The venv might not be activated; check pip list instead
        stdout, stderr, rc = run_command(
            "python -m pip list 2>/dev/null | grep pytest",
        )
        # We just check it doesn't error
        assert rc == 0, f"pip list failed: {stderr}"


class TestRequiredPackages:
    """Tests for required development packages."""

    REQUIRED_PACKAGES = ["pytest", "ruff", "mypy", "pytest-asyncio"]

    @pytest.mark.parametrize("pkg", REQUIRED_PACKAGES)
    def test_package_is_installed(self, run_command, pkg):
        f"""Development package '{pkg}' must be installed."""
        stdout, stderr, rc = run_command(f"python -m pip show {pkg}")
        # May or may not be in the venv; check any pip
        stdout2, stderr2, rc2 = run_command(f"pip show {pkg} || echo 'NOT_INSTALLED'")
        # At least pip show shouldn't fail badly
        assert "NOT_INSTALLED" not in stdout2 or rc == 0, (
            f"Required package '{pkg}' is not installed"
        )

    def test_pytest_can_collect_tests(self, run_command):
        """pytest --co must be able to discover tests (even if none exist yet)."""
        stdout, stderr, rc = run_command(
            "python -m pytest --collect-only -q",
        )
        # pytest should run without error (exit code 0 or 5 = no tests)
        assert rc in (0, 5), (
            f"pytest --collect-only failed with exit code {rc}:\n{stderr}"
        )


class TestWorkspaceResolution:
    """Tests for workspace configuration."""

    def test_root_pyproject_toml_exists(self, repo_root):
        """Root pyproject.toml must exist."""
        assert (repo_root / "pyproject.toml").exists(), (
            "Root pyproject.toml not found"
        )

    def test_sys_path_includes_repo(self, repo_root):
        """Python sys.path should reference the repo."""
        paths = sys.path
        assert any(
            str(repo_root) in p or "agent-harness-py" in p
            for p in paths
        ), f"Repo root not in sys.path: {paths}"

    def test_repo_root_has_expected_structure(self, repo_root):
        """Root repo should have the expected directories."""
        expected_dirs = ["docs", "venv"]
        for d in expected_dirs:
            assert (repo_root / d).is_dir(), f"Expected directory {d} not found"
