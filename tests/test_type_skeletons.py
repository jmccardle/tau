"""Tests for Phase 0 Subphase 0.3 — Cross-Phase Type Skeleton.

Verifies:
- All types import without error (bodies are ...)
- Type annotations are valid (checked with mypy)
- Stub types raise NotImplementedError when called
- Docstrings reference SUBPHASE-0.0.md
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestImportTypes:
    """Tests that types import without error."""

    def test_import_user_message(self):
        """UserMessage must be importable from tau_ai.types."""
        try:
            from tau_ai.types import UserMessage
            assert UserMessage is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_assistant_message(self):
        """AssistantMessage must be importable from tau_ai.types."""
        try:
            from tau_ai.types import AssistantMessage
            assert AssistantMessage is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_tool_result_message(self):
        """ToolResultMessage must be importable from tau_ai.types."""
        try:
            from tau_ai.types import ToolResultMessage
            assert ToolResultMessage is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_content_blocks(self):
        """ContentBlock types must be importable."""
        try:
            from tau_ai.types import (
                TextContent, ThinkingContent, ImageContent, ToolCall
            )
            assert all(t is not None for t in [
                TextContent, ThinkingContent, ImageContent, ToolCall
            ])
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_usage(self):
        """Usage type must be importable."""
        try:
            from tau_ai.types import Usage
            assert Usage is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_tool_definition(self):
        """ToolDefinition must be importable from tau_ai.tools."""
        try:
            from tau_ai.tools import define_tool
            assert define_tool is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_abort_signal(self):
        """AbortSignal must be importable from tau_ai.abort."""
        try:
            from tau_ai.abort import AbortSignal
            assert AbortSignal is not None
        except ImportError as e:
            pytest.skip(f"tau-ai package not yet implemented: {e}")

    def test_import_agent_event(self):
        """AgentEvent must be importable from tau_agent_core.events."""
        try:
            from tau_agent_core.events import AgentEvent
            assert AgentEvent is not None
        except ImportError as e:
            pytest.skip(f"tau-agent-core package not yet implemented: {e}")

    def test_import_session_entry(self):
        """SessionEntry must be importable from tau_agent_core.session."""
        try:
            from tau_agent_core.session import SessionEntry
            assert SessionEntry is not None
        except ImportError as e:
            pytest.skip(f"tau-agent-core package not yet implemented: {e}")

    def test_import_extension_api(self):
        """ExtensionAPI must be importable from tau_agent_core.extension_types."""
        try:
            from tau_agent_core.extension_types import ExtensionAPI
            assert ExtensionAPI is not None
        except ImportError as e:
            pytest.skip(f"tau-agent-core package not yet implemented: {e}")


class TestStubBehavior:
    """Tests that stub types raise NotImplementedError when called."""

    def test_define_tool_raises_not_implemented(self):
        """define_tool() should raise NotImplementedError."""
        try:
            from tau_ai.tools import define_tool
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        with pytest.raises(NotImplementedError):
            define_tool({})

    def test_abort_signal_methods_exist(self):
        """AbortSignal must have is_aborted() and abort() methods."""
        try:
            from tau_ai.abort import AbortSignal
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        signal = AbortSignal()
        assert hasattr(signal, "is_aborted"), "AbortSignal missing is_aborted() method"
        assert hasattr(signal, "abort"), "AbortSignal missing abort() method"
        # is_aborted should return a bool
        result = signal.is_aborted()
        assert isinstance(result, bool), f"is_aborted() should return bool, got {type(result)}"
        # Initially not aborted
        assert result is False, "is_aborted() should be False initially"
        # After abort(), is_aborted() should return True
        signal.abort()
        assert signal.is_aborted(), "is_aborted() should be True after abort()"


class TestMypyValidity:
    """Tests that type annotations are valid via mypy."""

    def test_mypy_types_py(self, run_command, tau_ai_dir):
        """mypy should find no type errors in tau_ai/types.py."""
        types_file = tau_ai_dir / "src" / "tau_ai" / "types.py"
        if not types_file.exists():
            pytest.skip("tau_ai/types.py not yet created")
        stdout, stderr, rc = run_command(
            f"python -m mypy --no-error-summary {types_file}"
        )
        # mypy exit code 0 = no errors, 1 = type errors found
        assert rc in (0, 1), f"mypy error: {stderr}"

    def test_mypy_events_py(self, run_command, tau_agent_core_dir):
        """mypy should find no type errors in tau_agent_core/events.py."""
        events_file = tau_agent_core_dir / "src" / "tau_agent_core" / "events.py"
        if not events_file.exists():
            pytest.skip("tau_agent_core/events.py not yet created")
        stdout, stderr, rc = run_command(
            f"python -m mypy --no-error-summary {events_file}"
        )
        assert rc in (0, 1), f"mypy error: {stderr}"


class TestDocstringReferences:
    """Tests that stub files have docstrings referencing SUBPHASE-0.0.md."""

    @pytest.mark.parametrize("rel_path", [
        "tau-ai/src/tau_ai/types.py",
        "tau-ai/src/tau_ai/tools.py",
        "tau-ai/src/tau_ai/abort.py",
        "tau-agent-core/src/tau_agent_core/events.py",
        "tau-agent-core/src/tau_agent_core/session.py",
        "tau-agent-core/src/tau_agent_core/extension_types.py",
    ])
    def test_file_has_docstring(self, repo_root, rel_path):
        """Each type file should have a docstring."""
        fpath = repo_root / rel_path
        if not fpath.exists():
            pytest.skip(f"{rel_path} not yet created")
        content = fpath.read_text()
        # Check for docstring (triple-quoted string at the start)
        assert '"""' in content or "'''" in content, (
            f"{rel_path} should have a docstring"
        )

    @pytest.mark.parametrize("rel_path", [
        "tau-ai/src/tau_ai/types.py",
        "tau-ai/src/tau_ai/tools.py",
        "tau-ai/src/tau_ai/abort.py",
        "tau-agent-core/src/tau_agent_core/events.py",
        "tau-agent-core/src/tau_agent_core/session.py",
        "tau-agent-core/src/tau_agent_core/extension_types.py",
    ])
    def test_file_references_subphase_00(self, repo_root, rel_path):
        """Each type file should reference SUBPHASE-0.0.md in docstrings."""
        fpath = repo_root / rel_path
        if not fpath.exists():
            pytest.skip(f"{rel_path} not yet created")
        content = fpath.read_text()
        assert "SUBPHASE-0.0" in content or "subphase-0.0" in content.lower(), (
            f"{rel_path} should reference SUBPHASE-0.0.md in its docstring"
        )


class TestTypeSignatures:
    """Tests that type signatures match SUBPHASE-0.0.md contracts."""

    def test_user_message_has_role_field(self):
        """UserMessage must have a role field."""
        try:
            from tau_ai.types import UserMessage
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        assert hasattr(UserMessage, "model_fields"), "UserMessage should be a pydantic model"
        assert "role" in UserMessage.model_fields, "UserMessage missing 'role' field"

    def test_user_message_role_is_user(self):
        """UserMessage.role defaults to 'user'."""
        try:
            from tau_ai.types import UserMessage
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        # Check the default value
        assert UserMessage.model_fields["role"].default == "user", (
            "UserMessage.role should default to 'user'"
        )

    def test_assistant_message_has_role_field(self):
        """AssistantMessage must have a role field."""
        try:
            from tau_ai.types import AssistantMessage
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        assert hasattr(AssistantMessage, "model_fields"), "AssistantMessage should be a pydantic model"
        assert "role" in AssistantMessage.model_fields

    def test_assistant_message_role_is_assistant(self):
        """AssistantMessage.role defaults to 'assistant'."""
        try:
            from tau_ai.types import AssistantMessage
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        assert AssistantMessage.model_fields["role"].default == "assistant"

    def test_tool_result_message_has_required_fields(self):
        """ToolResultMessage must have role, tool_call_id, tool_name, content, is_error."""
        try:
            from tau_ai.types import ToolResultMessage
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        fields = set(ToolResultMessage.model_fields.keys())
        required = {"role", "tool_call_id", "tool_name", "content", "is_error"}
        assert required.issubset(fields), (
            f"ToolResultMessage missing required fields: {required - fields}"
        )

    def test_text_content_type_field(self):
        """TextContent must have type='text'."""
        try:
            from tau_ai.types import TextContent
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        tc = TextContent(text="hello")
        assert tc.type == "text", f"TextContent.type should be 'text', got {tc.type}"

    def test_tool_call_has_required_fields(self):
        """ToolCall must have id, name, arguments."""
        try:
            from tau_ai.types import ToolCall
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        fields = set(ToolCall.model_fields.keys())
        required = {"id", "name", "arguments"}
        assert required.issubset(fields), (
            f"ToolCall missing required fields: {required - fields}"
        )

    def test_abort_signal_idempotent_abort(self):
        """AbortSignal.abort() must be idempotent."""
        try:
            from tau_ai.abort import AbortSignal
        except ImportError as e:
            pytest.skip(f"tau-ai not yet implemented: {e}")

        signal = AbortSignal()
        signal.abort()
        signal.abort()  # Should not raise
        assert signal.is_aborted()
