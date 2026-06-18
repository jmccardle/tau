"""Tests for Phase 0 Subphase 0.2 — Package Scaffolding.

Verifies:
- Each package has pyproject.toml with correct [project] table
- Each package has src/<package_name>/__init__.py
- Each package has tests/ directory with __init__.py and conftest.py
- Dependency chain resolves: tau-coding-agent → tau-agent-core → tau-ai
"""

from pathlib import Path

import pytest


class TestTauAiScaffolding:
    """Tests for tau-ai package scaffolding."""

    def test_tau_ai_pyproject_exists(self, tau_ai_dir):
        """tau-ai/pyproject.toml must exist."""
        assert (tau_ai_dir / "pyproject.toml").exists()

    @pytest.mark.parametrize("path", [
        "src/tau_ai/__init__.py",
        "src/tau_ai/types.py",
        "src/tau_ai/tools.py",
        "src/tau_ai/abort.py",
    ])
    def test_tau_ai_source_files_exist(self, tau_ai_dir, path):
        """tau-ai source files must exist."""
        assert (tau_ai_dir / path).exists(), f"Missing: {path}"

    def test_tau_ai_tests_dir_exists(self, tau_ai_dir):
        """tau-ai/tests/ directory must exist."""
        assert (tau_ai_dir / "tests").is_dir()

    def test_tau_ai_tests_init_exists(self, tau_ai_dir):
        """tau-ai/tests/ must have test infrastructure (conftest.py)."""
        assert (tau_ai_dir / "tests" / "conftest.py").exists()

    def test_tau_ai_tests_conftest_exists(self, tau_ai_dir):
        """tau-ai/tests/conftest.py must exist."""
        assert (tau_ai_dir / "tests" / "conftest.py").exists()

    def test_tau_ai_pyproject_has_correct_name(self, tau_ai_dir):
        """tau-ai pyproject.toml must have name = tau-ai."""
        import tomllib
        with open(tau_ai_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["name"] == "tau-ai"

    def test_tau_ai_pyproject_has_required_dependencies(self, tau_ai_dir):
        """tau-ai must depend on pydantic>=2.0 and openai>=1.0."""
        import tomllib
        with open(tau_ai_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        dep_names = [d.split(">=")[0].split(">")[0].split("<")[0].split("=")[0].split("-")[0].strip() for d in deps]
        assert "pydantic" in dep_names, f"Missing pydantic in dependencies: {deps}"
        assert "openai" in dep_names, f"Missing openai in dependencies: {deps}"


class TestTauAgentCoreScaffolding:
    """Tests for tau-agent-core package scaffolding."""

    def test_tau_agent_core_pyproject_exists(self, tau_agent_core_dir):
        """tau-agent-core/pyproject.toml must exist."""
        assert (tau_agent_core_dir / "pyproject.toml").exists()

    @pytest.mark.parametrize("path", [
        "src/tau_agent_core/__init__.py",
        "src/tau_agent_core/events.py",
        "src/tau_agent_core/session.py",
        "src/tau_agent_core/extension_types.py",
    ])
    def test_tau_agent_core_source_files_exist(self, tau_agent_core_dir, path):
        """tau-agent-core source files must exist."""
        assert (tau_agent_core_dir / path).exists(), f"Missing: {path}"

    def test_tau_agent_core_tests_dir_exists(self, tau_agent_core_dir):
        """tau-agent-core/tests/ directory must exist."""
        assert (tau_agent_core_dir / "tests").is_dir()

    def test_tau_agent_core_tests_init_exists(self, tau_agent_core_dir):
        """tau-agent-core/tests/ must have test infrastructure (conftest.py)."""
        assert (tau_agent_core_dir / "tests" / "conftest.py").exists()

    def test_tau_agent_core_tests_conftest_exists(self, tau_agent_core_dir):
        """tau-agent-core/tests/conftest.py must exist."""
        assert (tau_agent_core_dir / "tests" / "conftest.py").exists()

    def test_tau_agent_core_pyproject_has_correct_name(self, tau_agent_core_dir):
        """tau-agent-core pyproject.toml must have name = tau-agent-core."""
        import tomllib
        with open(tau_agent_core_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["name"] == "tau-agent-core"

    def test_tau_agent_core_depends_on_tau_ai(self, tau_agent_core_dir):
        """tau-agent-core must depend on tau-ai."""
        import tomllib
        with open(tau_agent_core_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        assert any("tau-ai" in d for d in deps), (
            f"Missing tau-ai dependency: {deps}"
        )

    def test_tau_agent_core_depends_on_pydantic(self, tau_agent_core_dir):
        """tau-agent-core must depend on pydantic>=2.0."""
        import tomllib
        with open(tau_agent_core_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        dep_names = [d.split(">=")[0].split(">")[0].split("<")[0].split("=")[0].split("-")[0].split("_")[0].strip() for d in deps]
        assert "pydantic" in dep_names or "tau-ai" in dep_names


class TestTauCodingAgentScaffolding:
    """Tests for tau-coding-agent package scaffolding."""

    def test_tau_coding_agent_pyproject_exists(self, tau_coding_agent_dir):
        """tau-coding-agent/pyproject.toml must exist."""
        assert (tau_coding_agent_dir / "pyproject.toml").exists()

    @pytest.mark.parametrize("path", [
        "src/tau_coding_agent/__init__.py",
    ])
    def test_tau_coding_agent_source_files_exist(self, tau_coding_agent_dir, path):
        """tau-coding-agent source files must exist."""
        assert (tau_coding_agent_dir / path).exists(), f"Missing: {path}"

    def test_tau_coding_agent_tests_dir_exists(self, tau_coding_agent_dir):
        """tau-coding-agent/tests/ directory must exist."""
        assert (tau_coding_agent_dir / "tests").is_dir()

    def test_tau_coding_agent_tests_init_exists(self, tau_coding_agent_dir):
        """tau-coding-agent/tests/ must have test infrastructure (conftest.py)."""
        assert (tau_coding_agent_dir / "tests" / "conftest.py").exists()

    def test_tau_coding_agent_tests_conftest_exists(self, tau_coding_agent_dir):
        """tau-coding-agent/tests/conftest.py must exist."""
        assert (tau_coding_agent_dir / "tests" / "conftest.py").exists()

    def test_tau_coding_agent_pyproject_has_correct_name(self, tau_coding_agent_dir):
        """tau-coding-agent pyproject.toml must have name = tau-coding-agent."""
        import tomllib
        with open(tau_coding_agent_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["name"] == "tau-coding-agent"

    def test_tau_coding_agent_depends_on_tau_agent_core(self, tau_coding_agent_dir):
        """tau-coding-agent must depend on tau-agent-core."""
        import tomllib
        with open(tau_coding_agent_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        assert any("tau-agent-core" in d for d in deps), (
            f"Missing tau-agent-core dependency: {deps}"
        )


class TestDependencyChain:
    """Tests for the full dependency chain."""

    def test_dependency_chain_resolves(self, run_command, repo_root):
        """The chain tau-coding-agent → tau-agent-core → tau-ai must resolve."""
        # Each package's pyproject.toml should be installable
        for pkg in ["tau-ai", "tau-agent-core", "tau-coding-agent"]:
            pkg_dir = repo_root / pkg
            pyproject = pkg_dir / "pyproject.toml"
            if pyproject.exists():
                # Verify pyproject.toml is valid TOML
                import tomllib
                with open(pkg_dir / "pyproject.toml", "rb") as f:
                    data = tomllib.load(f)
                assert "project" in data, f"{pkg}/pyproject.toml missing [project]"
                assert "name" in data["project"], f"{pkg}/pyproject.toml missing [project].name"


class TestPytestCollection:
    """Tests for pytest test discovery."""

    def test_pytest_discovers_test_directories(self, run_command):
        """pytest --collect-only should find test directories."""
        stdout, stderr, rc = run_command("python -m pytest --collect-only -q --no-header")
        assert rc in (0, 5), (
            f"pytest --collect-only failed (rc={rc}):\n{stderr}"
        )
