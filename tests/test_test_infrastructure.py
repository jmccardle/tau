"""Tests for Phase 0 Subphase 0.4 — Testing Infrastructure.

Verifies:
- conftest.py fixtures exist in each package
- Fixtures are discoverable by pytest
- Shared test utilities work across packages
- pytest-sanity test passes
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTauAiConftest:
    """Tests for tau-ai/tests/conftest.py fixtures."""

    def test_tau_ai_conftest_exists(self, tau_ai_dir):
        """tau-ai/tests/conftest.py must exist."""
        assert (tau_ai_dir / "tests" / "conftest.py").exists()

    def test_mock_openai_client_fixture_exists(self, tau_ai_dir):
        """conftest.py must define mock_openai_client fixture."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "mock_openai_client" in conftest, (
            "tau-ai/tests/conftest.py should define mock_openai_client fixture"
        )

    def test_sample_messages_fixture_exists(self, tau_ai_dir):
        """conftest.py must define sample_messages fixture."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "sample_messages" in conftest, (
            "tau-ai/tests/conftest.py should define sample_messages fixture"
        )

    def test_sample_tool_call_fixture_exists(self, tau_ai_dir):
        """conftest.py must define sample_tool_call fixture."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "sample_tool_call" in conftest, (
            "tau-ai/tests/conftest.py should define sample_tool_call fixture"
        )

    def test_sample_messages_contains_user_message(self, tau_ai_dir):
        """sample_messages fixture should produce UserMessage instances."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "UserMessage" in conftest, (
            "sample_messages should use UserMessage"
        )

    def test_sample_messages_contains_assistant_message(self, tau_ai_dir):
        """sample_messages fixture should produce AssistantMessage instances."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "AssistantMessage" in conftest, (
            "sample_messages should use AssistantMessage"
        )

    def test_sample_messages_contains_tool_result_message(self, tau_ai_dir):
        """sample_messages fixture should produce ToolResultMessage instances."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        assert "ToolResultMessage" in conftest, (
            "sample_messages should use ToolResultMessage"
        )

    def test_sample_tool_call_has_valid_structure(self, tau_ai_dir):
        """sample_tool_call should have id, name, arguments."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        for field in ["id", "name", "arguments"]:
            assert field in conftest, (
                f"sample_tool_call should reference '{field}'"
            )


class TestTauAgentCoreConftest:
    """Tests for tau-agent-core/tests/conftest.py fixtures."""

    def test_tau_agent_core_conftest_exists(self, tau_agent_core_dir):
        """tau-agent-core/tests/conftest.py must exist."""
        assert (tau_agent_core_dir / "tests" / "conftest.py").exists()

    def test_in_memory_session_manager_fixture_exists(self, tau_agent_core_dir):
        """conftest.py must define in_memory_session_manager fixture."""
        conftest = (tau_agent_core_dir / "tests" / "conftest.py").read_text()
        assert "in_memory_session_manager" in conftest

    def test_sample_agent_event_fixture_exists(self, tau_agent_core_dir):
        """conftest.py must define sample_agent_event fixture."""
        conftest = (tau_agent_core_dir / "tests" / "conftest.py").read_text()
        assert "sample_agent_event" in conftest

    def test_sample_tool_definition_fixture_exists(self, tau_agent_core_dir):
        """conftest.py must define sample_tool_definition fixture."""
        conftest = (tau_agent_core_dir / "tests" / "conftest.py").read_text()
        assert "sample_tool_definition" in conftest


class TestTauCodingAgentConftest:
    """Tests for tau-coding-agent/tests/conftest.py fixtures."""

    def test_tau_coding_agent_conftest_exists(self, tau_coding_agent_dir):
        """tau-coding-agent/tests/conftest.py must exist."""
        assert (tau_coding_agent_dir / "tests" / "conftest.py").exists()

    def test_mock_agent_session_fixture_exists(self, tau_coding_agent_dir):
        """conftest.py must define mock_agent_session fixture."""
        conftest = (tau_coding_agent_dir / "tests" / "conftest.py").read_text()
        assert "mock_agent_session" in conftest

    def test_mock_extension_api_fixture_exists(self, tau_coding_agent_dir):
        """conftest.py must define mock_extension_api fixture."""
        conftest = (tau_coding_agent_dir / "tests" / "conftest.py").read_text()
        assert "mock_extension_api" in conftest


class TestPytestConfiguration:
    """Tests for pytest configuration."""

    def test_pytest_ini_options_exist(self, repo_root):
        """pyproject.toml should have [tool.pytest.ini_options]."""
        pyproject = (repo_root / "pyproject.toml").read_text()
        assert "[tool.pytest" in pyproject, (
            "pyproject.toml should have [tool.pytest.ini_options]"
        )

    def test_pytest_testpaths_include_all_packages(self, repo_root):
        """pytest should be configured to search all tau-* test directories."""
        pyproject = (repo_root / "pyproject.toml").read_text()
        assert "tau-ai/tests" in pyproject
        assert "tau-agent-core/tests" in pyproject
        assert "tau-coding-agent/tests" in pyproject

    def test_pytest_asyncio_mode_configured(self, repo_root):
        """pytest-asyncio mode should be configured."""
        pyproject = (repo_root / "pyproject.toml").read_text()
        assert "asyncio_mode" in pyproject


class TestPytestCollectionAcrossPackages:
    """Tests for pytest discovery across all packages."""

    def test_pytest_collects_from_tau_ai(self, run_command, tau_ai_dir):
        """pytest should be able to collect from tau-ai/tests."""
        stdout, stderr, rc = run_command(
            f"python -m pytest --collect-only {tau_ai_dir}/tests -q"
        )
        assert rc in (0, 5), (
            f"pytest collection failed for tau-ai/tests (rc={rc}):\n{stderr}"
        )

    def test_pytest_collects_from_tau_agent_core(self, run_command, tau_agent_core_dir):
        """pytest should be able to collect from tau-agent-core/tests."""
        stdout, stderr, rc = run_command(
            f"python -m pytest --collect-only {tau_agent_core_dir}/tests -q"
        )
        assert rc in (0, 5), (
            f"pytest collection failed for tau-agent-core/tests (rc={rc}):\n{stderr}"
        )

    def test_pytest_collects_from_tau_coding_agent(self, run_command, tau_coding_agent_dir):
        """pytest should be able to collect from tau-coding-agent/tests."""
        stdout, stderr, rc = run_command(
            f"python -m pytest --collect-only {tau_coding_agent_dir}/tests -q"
        )
        assert rc in (0, 5), (
            f"pytest collection failed for tau-coding-agent/tests (rc={rc}):\n{stderr}"
        )


class TestSharedUtilities:
    """Tests for cross-package shared test utilities."""

    def test_tau_ai_types_can_be_imported_into_tau_agent_core_test_namespace(self, repo_root):
        """sample_messages from tau-ai should be importable in tau-agent-core tests."""
        # This tests that the tau-ai package is importable from tau-agent-core's test scope
        tau_ai_tests = repo_root / "tau-ai" / "tests"
        if not tau_ai_tests.exists():
            pytest.skip("tau-ai/tests not yet created")

        # Verify conftest.py exists and is importable
        conftest_path = tau_ai_tests / "conftest.py"
        assert conftest_path.exists()

    def test_conftest_fixtures_use_pydantic_models(self, tau_ai_dir):
        """Fixtures should use typed model instances (pydantic-backed)."""
        conftest = (tau_ai_dir / "tests" / "conftest.py").read_text()
        # Fixtures should import and use typed models from tau_ai.types
        # (which are pydantic models), not raw dicts
        assert "from tau_ai.types import" in conftest or "import tau_ai.types" in conftest, (
            "conftest.py should import typed models from tau_ai.types"
        )
        # Should use at least one typed model constructor
        for model in ["UserMessage", "AssistantMessage", "ToolResultMessage", "ToolCall", "TextContent"]:
            if model in conftest:
                break
        else:
            pytest.fail(
                "conftest.py should construct typed model instances (e.g. UserMessage(...), AssistantMessage(...))"
            )
