"""Tests for Phase 6 Subphase 3 — Documentation Verification.

Verifies that all documentation items required by the Done Criteria
are present and contain the expected content:
1. README.md for each package (tau-ai, tau-agent-core, tau-coding-agent)
2. 5 example extensions documented
3. SDK usage examples provided (4+)
4. RPC protocol documentation complete
5. Migration guide from parley written

Reference: docs/PHASE-6-SUBPHASE-3.md — Done Criteria
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Root of the tau monorepo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================================
# Helper Functions
# ============================================================================


def _read_file(path: Path) -> str:
    """Read a file and return its content."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _assert_file_exists(path: Path, name: str):
    """Assert that a file exists and is non-empty."""
    assert path.exists(), f"{name} does not exist at {path}"
    assert path.is_file(), f"{name} is not a file: {path}"
    content = _read_file(path)
    assert len(content) > 100, f"{name} is too short ({len(content)} chars)"


def _assert_contains(content: str, pattern: str, name: str):
    """Assert that content contains a pattern."""
    assert re.search(pattern, content, re.DOTALL | re.IGNORECASE), (
        f"{name} does not contain expected pattern: {pattern}"
    )


# ============================================================================
# Test 1: README files for each package
# ============================================================================


class TestPackageReadmes:
    """Test 1: README.md exists for each package.

    Done Criteria: "README.md exists for each package"
    """

    def test_tau_ai_readme_exists(self):
        """tau-ai/README.md exists and is non-empty."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        _assert_file_exists(path, "tau-ai/README.md")

    def test_tau_agent_core_readme_exists(self):
        """tau-agent-core/README.md exists and is non-empty."""
        path = REPO_ROOT / "tau-agent-core" / "README.md"
        _assert_file_exists(path, "tau-agent-core/README.md")

    def test_tau_coding_agent_readme_exists(self):
        """tau-coding-agent/README.md exists and is non-empty."""
        path = REPO_ROOT / "tau-coding-agent" / "README.md"
        _assert_file_exists(path, "tau-coding-agent/README.md")

    def test_tau_ai_readme_contains_quick_start(self):
        """tau-ai/README.md contains a quick start section."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "quick.start|Quick.Start|Quick Start", "tau-ai/README.md")

    def test_tau_ai_readme_contains_architecture(self):
        """tau-ai/README.md contains an architecture section."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "architecture|package.structure|Package Structure",
                         "tau-ai/README.md")

    def test_tau_ai_readme_mentions_key_types(self):
        """tau-ai/README.md mentions Model, TextContent, ToolDefinition."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        content = _read_file(path)
        for type_name in ["Model", "TextContent", "ToolDefinition"]:
            _assert_contains(content, re.escape(type_name), f"tau-ai/README.md ({type_name})")

    def test_tau_ai_readme_mentions_abort_signal(self):
        """tau-ai/README.md mentions AbortSignal."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "AbortSignal", "tau-ai/README.md")

    def test_tau_ai_readme_mentions_streaming(self):
        """tau-ai/README.md mentions streaming events."""
        path = REPO_ROOT / "tau-ai" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "stream|StreamEvent|TextDeltaEvent", "tau-ai/README.md")

    def test_tau_agent_core_readme_contains_quick_start(self):
        """tau-agent-core/README.md contains a quick start section."""
        path = REPO_ROOT / "tau-agent-core" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "quick.start|Quick.Start|Quick Start",
                         "tau-agent-core/README.md")

    def test_tau_agent_core_readme_mentions_agent_session(self):
        """tau-agent-core/README.md mentions AgentSession."""
        path = REPO_ROOT / "tau-agent-core" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "AgentSession", "tau-agent-core/README.md")

    def test_tau_agent_core_readme_mentions_extension_system(self):
        """tau-agent-core/README.md mentions the extension system."""
        path = REPO_ROOT / "tau-agent-core" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "extension|Extension|ExtensionAPI", "tau-agent-core/README.md")

    def test_tau_agent_core_readme_mentions_built_in_tools(self):
        """tau-agent-core/README.md mentions built-in tools."""
        path = REPO_ROOT / "tau-agent-core" / "README.md"
        content = _read_file(path)
        for tool in ["read", "write", "bash", "edit"]:
            _assert_contains(content, re.escape(tool),
                             f"tau-agent-core/README.md ({tool})")

    def test_tau_coding_agent_readme_contains_quick_start(self):
        """tau-coding-agent/README.md contains a quick start section."""
        path = REPO_ROOT / "tau-coding-agent" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "quick.start|Quick.Start|Quick Start",
                         "tau-coding-agent/README.md")

    def test_tau_coding_agent_readme_mentions_textual(self):
        """tau-coding-agent/README.md mentions Textual TUI."""
        path = REPO_ROOT / "tau-coding-agent" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "Textual", "tau-coding-agent/README.md")

    def test_tau_coding_agent_readme_mentions_widgets(self):
        """tau-coding-agent/README.md mentions widgets."""
        path = REPO_ROOT / "tau-coding-agent" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "chat.display|ChatDisplay|tool.widget|ToolWidget|session.tree|SessionTree",
                         "tau-coding-agent/README.md")

    def test_tau_coding_agent_readme_mentions_rpc(self):
        """tau-coding-agent/README.md mentions RPC mode."""
        path = REPO_ROOT / "tau-coding-agent" / "README.md"
        content = _read_file(path)
        _assert_contains(content, "RPC|rpc|RPC.mode", "tau-coding-agent/README.md")


# ============================================================================
# Test 2: Example extensions
# ============================================================================


class TestExampleExtensions:
    """Test 2: 5 example extensions are documented.

    Done Criteria: "5 example extensions are documented"
    """

    @pytest.fixture
    def examples_dir(self):
        """Path to the examples directory."""
        return REPO_ROOT / "examples"

    def test_examples_directory_exists(self, examples_dir):
        """Examples directory exists."""
        assert examples_dir.exists(), f"Examples directory not found: {examples_dir}"
        assert examples_dir.is_dir(), "Examples path is not a directory"

    def test_permission_gate_extension_exists(self, examples_dir):
        """Permission gate extension exists."""
        path = examples_dir / "01_permission_gate.py"
        _assert_file_exists(path, "permission_gate extension")

    def test_permission_gate_extension_has_blocked_patterns(self, examples_dir):
        """Permission gate extension has blocked command patterns."""
        path = examples_dir / "01_permission_gate.py"
        content = _read_file(path)
        for pattern in ["rm -rf", "chmod 777", "dd "]:
            _assert_contains(content, re.escape(pattern),
                             "permission_gate.py (blocked pattern)")

    def test_permission_gate_extension_has_function(self, examples_dir):
        """Permission gate extension has an extend function."""
        path = examples_dir / "01_permission_gate.py"
        content = _read_file(path)
        _assert_contains(content, "def permission_gate_extension\\(api\\)",
                         "permission_gate.py (function)")

    def test_git_checkpoint_extension_exists(self, examples_dir):
        """Git checkpoint extension exists."""
        path = examples_dir / "02_git_checkpoint.py"
        _assert_file_exists(path, "git_checkpoint extension")

    def test_git_checkpoint_extension_runs_git(self, examples_dir):
        """Git checkpoint extension runs git commands."""
        path = examples_dir / "02_git_checkpoint.py"
        content = _read_file(path)
        _assert_contains(content, "git\\s+(add|commit|status|rev-parse)",
                         "git_checkpoint.py (git commands)")

    def test_git_checkpoint_extension_has_function(self, examples_dir):
        """Git checkpoint extension has an extend function."""
        path = examples_dir / "02_git_checkpoint.py"
        content = _read_file(path)
        _assert_contains(content, "def git_checkpoint_extension\\(api\\)",
                         "git_checkpoint.py (function)")

    def test_dynamic_env_tool_extension_exists(self, examples_dir):
        """Dynamic env tool extension exists."""
        path = examples_dir / "03_dynamic_env_tool.py"
        _assert_file_exists(path, "dynamic_env_tool extension")

    def test_dynamic_env_tool_registers_tool(self, examples_dir):
        """Dynamic env tool registers a tool via api.register_tool()."""
        path = examples_dir / "03_dynamic_env_tool.py"
        content = _read_file(path)
        _assert_contains(content, "register_tool",
                         "dynamic_env_tool.py (register_tool)")

    def test_dynamic_env_tool_reads_environ(self, examples_dir):
        """Dynamic env tool reads os.environ."""
        path = examples_dir / "03_dynamic_env_tool.py"
        content = _read_file(path)
        _assert_contains(content, "os\\.environ",
                         "dynamic_env_tool.py (os.environ)")

    def test_session_logger_extension_exists(self, examples_dir):
        """Session logger extension exists."""
        path = examples_dir / "04_session_logger.py"
        _assert_file_exists(path, "session_logger extension")

    def test_session_logger_logs_to_file(self, examples_dir):
        """Session logger writes events to a file."""
        path = examples_dir / "04_session_logger.py"
        content = _read_file(path)
        _assert_contains(content, "json\\.dumps|open\\(",
                         "session_logger.py (file writing)")

    def test_session_logger_subscribes_to_events(self, examples_dir):
        """Session logger subscribes to 'all' events."""
        path = examples_dir / "04_session_logger.py"
        content = _read_file(path)
        _assert_contains(content, 'api\\.on\\("all"',
                         "session_logger.py (event subscription)")

    def test_custom_tool_extension_exists(self, examples_dir):
        """Custom tool extension exists."""
        path = examples_dir / "05_custom_tool.py"
        _assert_file_exists(path, "custom_tool extension")

    def test_custom_tool_has_definition(self, examples_dir):
        """Custom tool has a complete tool definition."""
        path = examples_dir / "05_custom_tool.py"
        content = _read_file(path)
        for field in ["name", "label", "description", "parameters", "execute"]:
            _assert_contains(content, re.escape(field),
                             f"custom_tool.py (field: {field})")

    def test_all_five_extensions_exist(self, examples_dir):
        """All five example extensions exist as files."""
        expected_files = [
            "01_permission_gate.py",
            "02_git_checkpoint.py",
            "03_dynamic_env_tool.py",
            "04_session_logger.py",
            "05_custom_tool.py",
        ]
        for filename in expected_files:
            path = examples_dir / filename
            _assert_file_exists(path, filename)

    def test_all_extensions_have_examples_section(self, examples_dir):
        """All extension files have a Usage section."""
        expected_files = [
            "01_permission_gate.py",
            "02_git_checkpoint.py",
            "03_dynamic_env_tool.py",
            "04_session_logger.py",
            "05_custom_tool.py",
        ]
        for filename in expected_files:
            path = examples_dir / filename
            content = _read_file(path)
            _assert_contains(content, "## Usage|usage:|Usage:",
                             f"{filename} (Usage section)")

    def test_extensions_use_api_register_tool(self, examples_dir):
        """At least some extensions register tools via api.register_tool()."""
        # Check the tools-based extensions
        for filename in ["03_dynamic_env_tool.py", "05_custom_tool.py"]:
            path = examples_dir / filename
            content = _read_file(path)
            _assert_contains(content, "register_tool",
                             f"{filename} (register_tool usage)")


# ============================================================================
# Test 3: SDK usage examples
# ============================================================================


class TestSDKUsageExamples:
    """Test 3: SDK usage examples are provided (4+).

    Done Criteria: "SDK usage examples are provided"
    """

    @pytest.fixture
    def examples_dir(self):
        """Path to the examples directory."""
        return REPO_ROOT / "examples"

    def test_sdk_create_session_example_exists(self, examples_dir):
        """SDK create session example exists."""
        path = examples_dir / "10_sdk_create_session.py"
        _assert_file_exists(path, "SDK create session example")

    def test_sdk_create_session_uses_create_agent_session(self, examples_dir):
        """SDK create session example uses create_agent_session()."""
        path = examples_dir / "10_sdk_create_session.py"
        content = _read_file(path)
        _assert_contains(content, "create_agent_session",
                         "SDK create session example")

    def test_sdk_subscribe_events_example_exists(self, examples_dir):
        """SDK subscribe events example exists."""
        path = examples_dir / "11_sdk_subscribe_events.py"
        _assert_file_exists(path, "SDK subscribe events example")

    def test_sdk_subscribe_events_uses_subscribe(self, examples_dir):
        """SDK subscribe events example uses session.subscribe()."""
        path = examples_dir / "11_sdk_subscribe_events.py"
        content = _read_file(path)
        _assert_contains(content, "session\\.subscribe|subscribe\\(",
                         "SDK subscribe events example")

    def test_sdk_subscribe_events_unsubscribes(self, examples_dir):
        """SDK subscribe events example shows unsubscribe."""
        path = examples_dir / "11_sdk_subscribe_events.py"
        content = _read_file(path)
        _assert_contains(content, "unsub|unsubscribe|unsub\\(\\)",
                         "SDK subscribe events example (unsub)")

    def test_sdk_in_memory_mode_example_exists(self, examples_dir):
        """SDK in-memory mode example exists."""
        path = examples_dir / "12_sdk_in_memory_mode.py"
        _assert_file_exists(path, "SDK in-memory mode example")

    def test_sdk_in_memory_mode_uses_in_memory_manager(self, examples_dir):
        """SDK in-memory mode example uses SessionManager.in_memory()."""
        path = examples_dir / "12_sdk_in_memory_mode.py"
        content = _read_file(path)
        _assert_contains(content, "SessionManager\\.in_memory",
                         "SDK in-memory mode example")

    def test_sdk_custom_system_prompt_example_exists(self, examples_dir):
        """SDK custom system prompt example exists."""
        path = examples_dir / "13_sdk_custom_system_prompt.py"
        _assert_file_exists(path, "SDK custom system prompt example")

    def test_sdk_custom_system_prompt_uses_system_prompt(self, examples_dir):
        """SDK custom system prompt example uses system_prompt parameter."""
        path = examples_dir / "13_sdk_custom_system_prompt.py"
        content = _read_file(path)
        _assert_contains(content, "system_prompt",
                         "SDK custom system prompt example")

    def test_four_or_more_sdk_examples_exist(self, examples_dir):
        """At least 4 SDK usage examples exist."""
        sdk_examples = [f for f in examples_dir.glob("1*_sdk_*.py")]
        assert len(sdk_examples) >= 4, (
            f"Expected at least 4 SDK examples, found {len(sdk_examples)}: "
            f"{[f.name for f in sdk_examples]}"
        )


# ============================================================================
# Test 4: RPC protocol documentation
# ============================================================================


class TestRPCProtocolDocumentation:
    """Test 4: RPC protocol documentation is complete.

    Done Criteria: "RPC protocol documentation is complete"
    """

    @pytest.fixture
    def rpc_doc(self):
        """Path to RPC protocol documentation."""
        return REPO_ROOT / "docs" / "RPC-PROTOCOL.md"

    def test_rpc_protocol_doc_exists(self, rpc_doc):
        """RPC protocol documentation exists."""
        _assert_file_exists(rpc_doc, "RPC protocol documentation")

    def test_rpc_doc_contains_request_response_format(self, rpc_doc):
        """RPC documentation covers request/response format."""
        content = _read_file(rpc_doc)
        _assert_contains(content, "request|Request|JSON-?RPC|jsonrpc",
                         "RPC protocol documentation (request/response)")

    def test_rpc_doc_contains_available_methods(self, rpc_doc):
        """RPC documentation lists available methods."""
        content = _read_file(rpc_doc)
        for method in ["send_prompt", "send_tool_result", "abort",
                       "get_commands", "get_tools", "get_session_info"]:
            _assert_contains(content, re.escape(method),
                             f"RPC documentation (method: {method})")

    def test_rpc_doc_contains_example_client_code(self, rpc_doc):
        """RPC documentation includes example client code."""
        content = _read_file(rpc_doc)
        _assert_contains(content, "import asyncio|class.*Client|async def|def.*Client",
                         "RPC documentation (example client code)")

    def test_rpc_doc_contains_event_format(self, rpc_doc):
        """RPC documentation describes event notifications."""
        content = _read_file(rpc_doc)
        _assert_contains(content, "event|Event|notification",
                         "RPC documentation (events)")

    def test_rpc_doc_contains_error_codes(self, rpc_doc):
        """RPC documentation lists error codes."""
        content = _read_file(rpc_doc)
        _assert_contains(content, "-3260[0-9]|-32700|-32000",
                         "RPC documentation (error codes)")

    def test_rpc_doc_contains_framing_info(self, rpc_doc):
        """RPC documentation describes the LF-delimited framing."""
        content = _read_file(rpc_doc)
        _assert_contains(content, "LF-delimited|newline|stdin|stdout",
                         "RPC documentation (framing)")


# ============================================================================
# Test 5: Migration guide from parley
# ============================================================================


class TestMigrationGuide:
    """Test 5: Migration guide from parley is written.

    Done Criteria: "Migration guide from parley is written"
    """

    @pytest.fixture
    def migration_doc(self):
        """Path to migration guide."""
        return REPO_ROOT / "docs" / "PI-TO-TAU-MIGRATION-GUIDE.md"

    def test_migration_guide_exists(self, migration_doc):
        """Migration guide exists."""
        _assert_file_exists(migration_doc, "Migration guide")

    def test_migration_guide_covers_what_changes(self, migration_doc):
        """Migration guide covers what changes."""
        content = _read_file(migration_doc)
        _assert_contains(content, "changes|Changes|What Changes",
                         "Migration guide (what changes)")

    def test_migration_guide_covers_what_stays_the_same(self, migration_doc):
        """Migration guide covers what stays the same."""
        content = _read_file(migration_doc)
        _assert_contains(content, "stays.*same|same.*format|compatible|Same",
                         "Migration guide (what stays the same)")

    def test_migration_guide_covers_how_to_migrate(self, migration_doc):
        """Migration guide covers how to migrate existing code."""
        content = _read_file(migration_doc)
        _assert_contains(content, "migration|Migration|Step.*[0-9]|Before.*After|Before:|After:",
                         "Migration guide (how to migrate)")

    def test_migration_guide_has_code_examples(self, migration_doc):
        """Migration guide contains code examples."""
        content = _read_file(migration_doc)
        # Should have at least one code block
        assert "```python" in content or "```typescript" in content or "```tsx" in content, (
            "Migration guide should contain code examples"
        )

    def test_migration_guide_has_section_headers(self, migration_doc):
        """Migration guide has section headers for organization."""
        content = _read_file(migration_doc)
        # Should have multiple ## sections
        sections = re.findall(r"^## .+", content, re.MULTILINE)
        assert len(sections) >= 3, (
            f"Migration guide should have at least 3 sections, found {len(sections)}"
        )

    def test_migration_guide_mentions_sessions(self, migration_doc):
        """Migration guide mentions sessions."""
        content = _read_file(migration_doc)
        _assert_contains(content, "session|Session",
                         "Migration guide (sessions)")

    def test_migration_guide_mentions_tools(self, migration_doc):
        """Migration guide mentions tools."""
        content = _read_file(migration_doc)
        _assert_contains(content, "tool|Tool",
                         "Migration guide (tools)")

    def test_migration_guide_mentions_extensions(self, migration_doc):
        """Migration guide mentions extensions."""
        content = _read_file(migration_doc)
        _assert_contains(content, "extension|Extension",
                         "Migration guide (extensions)")

    def test_migration_guide_mentions_events(self, migration_doc):
        """Migration guide mentions event subscriptions."""
        content = _read_file(migration_doc)
        _assert_contains(content, "event|Event|subscribe|Subscribe",
                         "Migration guide (events)")

    def test_migration_guide_is_not_the_compatibility_map(self, migration_doc):
        """Migration guide is distinct from the compatibility map."""
        # The migration guide should NOT be the same as PI-TO-TAU-COMPATIBILITY.md
        compatibility_path = REPO_ROOT / "docs" / "PI-TO-TAU-COMPATIBILITY.md"
        if compatibility_path.exists():
            compat_content = _read_file(compatibility_path)
            migration_content = _read_file(migration_doc)
            assert migration_content != compat_content, (
                "Migration guide should be distinct from compatibility map"
            )
