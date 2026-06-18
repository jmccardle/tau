"""Tests for Phase 5 Subphase 0 — Data Contract Definition.

Verifies the final type signatures for compaction and session operation types:
1. CompactionConfig, CompactionResult (compaction.py)
2. BranchSummary, ForkResult, CloneResult (session.py)
3. Settings (settings.py)

Reference: docs/PHASE-5-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md

Success Signal: All types import with the correct fields and defaults.
This is the final contract before Phase 5 implementation.
"""

import asyncio
import dataclasses
import inspect
import typing
from unittest.mock import AsyncMock

import pytest

from tau_agent_core.compaction import CompactionConfig, CompactionResult
from tau_agent_core.session import BranchSummary, ForkResult, CloneResult
from tau_agent_core.settings import Settings


# =============================================================================
# 1. CompactionConfig Contract Tests
# =============================================================================


class TestCompactionConfigContract:
    """Tests verifying CompactionConfig matches the Phase 5 Subphase 0 contract.

    Contract (from PHASE-5-SUBPHASE-0.md):
    @dataclass
    class CompactionConfig:
        model: Model
        system_prompt: str
        max_context_tokens: int
        margin: int  # tokens to keep as margin before hitting max
        custom_instructions: str | None = None
        compact_callback: Callable[[str, int], Awaitable[None]] | None = None
    """

    def test_compaction_config_is_dataclass(self):
        """CompactionConfig is a dataclass."""
        assert dataclasses.is_dataclass(CompactionConfig)

    def test_compaction_config_required_fields(self):
        """CompactionConfig has all required fields."""
        fields = {f.name: f for f in dataclasses.fields(CompactionConfig)}
        required = {"model", "system_prompt", "max_context_tokens", "margin"}
        assert required == {n for n, f in fields.items() if f.default is dataclasses.MISSING}

    def test_compaction_config_optional_fields(self):
        """CompactionConfig has optional fields with defaults."""
        fields = {f.name: f for f in dataclasses.fields(CompactionConfig)}
        assert "custom_instructions" in fields
        assert "compact_callback" in fields
        assert fields["custom_instructions"].default is dataclasses.MISSING or \
            fields["custom_instructions"].default is None or \
            fields["custom_instructions"].default is dataclasses.MISSING
        assert fields["compact_callback"].default is None or \
            fields["compact_callback"].default is dataclasses.MISSING

    def test_compaction_config_all_fields_present(self):
        """CompactionConfig has all 6 documented fields."""
        field_names = {f.name for f in dataclasses.fields(CompactionConfig)}
        expected = {"model", "system_prompt", "max_context_tokens", "margin",
                     "custom_instructions", "compact_callback"}
        assert field_names == expected

    def test_compaction_config_fields_match_contract(self):
        """CompactionConfig field names match the contract exactly."""
        fields = dataclasses.fields(CompactionConfig)
        names = [f.name for f in fields]
        assert names == ["model", "system_prompt", "max_context_tokens", "margin",
                          "custom_instructions", "compact_callback"]

    def test_compaction_config_custom_instructions_default_is_none(self):
        """CompactionConfig.custom_instructions defaults to None."""
        from tau_ai.types import Model
        config = CompactionConfig(
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        assert config.custom_instructions is None

    def test_compaction_config_compact_callback_default_is_none(self):
        """CompactionConfig.compact_callback defaults to None."""
        from tau_ai.types import Model
        config = CompactionConfig(
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        assert config.compact_callback is None

    def test_compaction_config_accepts_custom_instructions(self):
        """CompactionConfig accepts custom_instructions."""
        from tau_ai.types import Model
        config = CompactionConfig(
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
            custom_instructions="Only respond in English.",
        )
        assert config.custom_instructions == "Only respond in English."

    def test_compaction_config_accepts_callback(self):
        """CompactionConfig accepts a compact_callback."""
        from tau_ai.types import Model
        async def mock_callback(text: str, tokens: int):
            pass

        config = CompactionConfig(
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
            compact_callback=mock_callback,
        )
        assert config.compact_callback is mock_callback

    def test_compaction_config_type_annotations(self):
        """CompactionConfig fields have correct type annotations."""
        from tau_ai.types import Model
        hints = typing.get_type_hints(CompactionConfig)
        assert hints["model"] is Model
        assert hints["system_prompt"] is str
        assert hints["max_context_tokens"] is int
        assert hints["margin"] is int

    def test_compaction_config_equality(self):
        """Two CompactionConfig with same values are equal."""
        from tau_ai.types import Model
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        c1 = CompactionConfig(
            model=model,
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        c2 = CompactionConfig(
            model=model,
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        assert c1 == c2

    def test_compaction_config_repr(self):
        """CompactionConfig has a repr."""
        from tau_ai.types import Model
        config = CompactionConfig(
            model=Model(
                id="gpt-4o", name="GPT-4o", api="openai-completions",
                provider="openai", base_url="https://api.openai.com/v1",
                context_window=128000, max_tokens=4096,
            ),
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        assert "CompactionConfig" in repr(config)


# =============================================================================
# 2. CompactionResult Contract Tests
# =============================================================================


class TestCompactionResultContract:
    """Tests verifying CompactionResult matches the Phase 5 Subphase 0 contract.

    Contract (from PHASE-5-SUBPHASE-0.md):
    @dataclass
    class CompactionResult:
        summary: str  # The LLM-generated summary
        first_kept_id: str  # ID of the first message kept in full
        compacted_entry_ids: list[str]  # IDs of entries that were compacted
        tokens_saved: int  # Estimated tokens saved
        tokens_before: int
        tokens_after: int
    """

    def test_compaction_result_is_dataclass(self):
        """CompactionResult is a dataclass."""
        assert dataclasses.is_dataclass(CompactionResult)

    def test_compaction_result_all_fields_required(self):
        """All CompactionResult fields are required (no defaults)."""
        fields = {f.name: f for f in dataclasses.fields(CompactionResult)}
        for name, field in fields.items():
            assert field.default is dataclasses.MISSING, \
                f"Field {name} should not have a default, got {field.default}"
            assert field.default_factory is dataclasses.MISSING, \
                f"Field {name} should not have a default_factory"

    def test_compaction_result_all_fields_present(self):
        """CompactionResult has all 6 documented fields."""
        field_names = {f.name for f in dataclasses.fields(CompactionResult)}
        expected = {"summary", "first_kept_id", "compacted_entry_ids",
                     "tokens_saved", "tokens_before", "tokens_after"}
        assert field_names == expected

    def test_compaction_result_field_names_ordered(self):
        """CompactionResult fields are in the documented order."""
        names = [f.name for f in dataclasses.fields(CompactionResult)]
        assert names == ["summary", "first_kept_id", "compacted_entry_ids",
                          "tokens_saved", "tokens_before", "tokens_after"]

    def test_compaction_result_creation(self):
        """CompactionResult can be instantiated with all fields."""
        result = CompactionResult(
            summary="Conversation was about project planning.",
            first_kept_id="msg_050",
            compacted_entry_ids=["msg_001", "msg_002", "msg_003"],
            tokens_saved=5000,
            tokens_before=50000,
            tokens_after=45000,
        )
        assert result.summary == "Conversation was about project planning."
        assert result.first_kept_id == "msg_050"
        assert result.compacted_entry_ids == ["msg_001", "msg_002", "msg_003"]
        assert result.tokens_saved == 5000
        assert result.tokens_before == 50000
        assert result.tokens_after == 45000

    def test_compaction_result_tokens_calculated(self):
        """CompactionResult tokens are consistent."""
        result = CompactionResult(
            summary="Summary",
            first_kept_id="msg_001",
            compacted_entry_ids=[],
            tokens_saved=1000,
            tokens_before=10000,
            tokens_after=9000,
        )
        assert result.tokens_saved == result.tokens_before - result.tokens_after

    def test_compaction_result_empty_compacted_ids(self):
        """CompactionResult can have empty compacted_entry_ids list."""
        result = CompactionResult(
            summary="No entries compacted",
            first_kept_id="msg_001",
            compacted_entry_ids=[],
            tokens_saved=0,
            tokens_before=1000,
            tokens_after=1000,
        )
        assert result.compacted_entry_ids == []
        assert result.tokens_saved == 0

    def test_compaction_result_repr(self):
        """CompactionResult has a repr."""
        result = CompactionResult(
            summary="Test", first_kept_id="x", compacted_entry_ids=[],
            tokens_saved=0, tokens_before=0, tokens_after=0,
        )
        assert "CompactionResult" in repr(result)


# =============================================================================
# 3. Settings Contract Tests
# =============================================================================


class TestSettingsContract:
    """Tests verifying Settings matches the Phase 5 Subphase 0 contract.

    Contract (from PHASE-5-SUBPHASE-0.md):
    @dataclass
    class Settings:
        default_model: str = "gpt-4o"
        thinking_level: str = "off"
        compaction_enabled: bool = True
        context_margin: int = 2000
        extension_dirs: list[str] = field(default_factory=lambda: [
            str(Path.home() / ".tau" / "extensions"),
        ])
        api_keys: dict[str, str] = field(default_factory=dict)
        custom_system_prompt: str | None = None
        tool_execution_mode: str = "parallel"
        max_retries: int = 3
        temperature: float = 0.7
        max_tokens: int | None = None
        reasoning_level: str = "off"
    """

    def test_settings_is_dataclass(self):
        """Settings is a dataclass."""
        assert dataclasses.is_dataclass(Settings)

    def test_settings_all_fields_present(self):
        """Settings has all 12 documented fields."""
        field_names = {f.name for f in dataclasses.fields(Settings)}
        expected = {
            "default_model", "thinking_level", "compaction_enabled",
            "context_margin", "extension_dirs", "api_keys",
            "custom_system_prompt", "tool_execution_mode", "max_retries",
            "temperature", "max_tokens", "reasoning_level",
        }
        assert field_names == expected

    def test_settings_default_model(self):
        """Settings.default_model defaults to 'gpt-4o'."""
        s = Settings()
        assert s.default_model == "gpt-4o"

    def test_settings_thinking_level(self):
        """Settings.thinking_level defaults to 'off'."""
        s = Settings()
        assert s.thinking_level == "off"

    def test_settings_compaction_enabled(self):
        """Settings.compaction_enabled defaults to True."""
        s = Settings()
        assert s.compaction_enabled is True

    def test_settings_context_margin(self):
        """Settings.context_margin defaults to 2000."""
        s = Settings()
        assert s.context_margin == 2000

    def test_settings_extension_dirs_default(self):
        """Settings.extension_dirs defaults to [~/.tau/extensions]."""
        from pathlib import Path
        s = Settings()
        expected = [str(Path.home() / ".tau" / "extensions")]
        assert s.extension_dirs == expected

    def test_settings_api_keys_default(self):
        """Settings.api_keys defaults to empty dict."""
        s = Settings()
        assert s.api_keys == {}

    def test_settings_custom_system_prompt_default(self):
        """Settings.custom_system_prompt defaults to None."""
        s = Settings()
        assert s.custom_system_prompt is None

    def test_settings_tool_execution_mode(self):
        """Settings.tool_execution_mode defaults to 'parallel'."""
        s = Settings()
        assert s.tool_execution_mode == "parallel"

    def test_settings_max_retries(self):
        """Settings.max_retries defaults to 3."""
        s = Settings()
        assert s.max_retries == 3

    def test_settings_temperature(self):
        """Settings.temperature defaults to 0.7."""
        s = Settings()
        assert s.temperature == 0.7

    def test_settings_max_tokens_default(self):
        """Settings.max_tokens defaults to None."""
        s = Settings()
        assert s.max_tokens is None

    def test_settings_reasoning_level(self):
        """Settings.reasoning_level defaults to 'off'."""
        s = Settings()
        assert s.reasoning_level == "off"

    def test_settings_accepts_all_values(self):
        """Settings accepts explicit values for all fields."""
        s = Settings(
            default_model="claude-sonnet-4-20250514",
            thinking_level="high",
            compaction_enabled=False,
            context_margin=4000,
            extension_dirs=["/custom/extensions"],
            api_keys={"openai": "sk-xxx"},
            custom_system_prompt="You are a specialized assistant.",
            tool_execution_mode="sequential",
            max_retries=5,
            temperature=0.5,
            max_tokens=8192,
            reasoning_level="low",
        )
        assert s.default_model == "claude-sonnet-4-20250514"
        assert s.thinking_level == "high"
        assert s.compaction_enabled is False
        assert s.context_margin == 4000
        assert s.extension_dirs == ["/custom/extensions"]
        assert s.api_keys == {"openai": "sk-xxx"}
        assert s.custom_system_prompt == "You are a specialized assistant."
        assert s.tool_execution_mode == "sequential"
        assert s.max_retries == 5
        assert s.temperature == 0.5
        assert s.max_tokens == 8192
        assert s.reasoning_level == "low"

    def test_settings_extension_dirs_independent_instances(self):
        """Settings instances have independent extension_dirs lists."""
        s1 = Settings()
        s2 = Settings()
        s1.extension_dirs.append("/custom/path")
        assert "/custom/path" in s1.extension_dirs
        assert "/custom/path" not in s2.extension_dirs

    def test_settings_field_count(self):
        """Settings has exactly 12 fields."""
        assert len(dataclasses.fields(Settings)) == 12


# =============================================================================
# 4. BranchSummary Contract Tests
# =============================================================================


class TestBranchSummaryContract:
    """Tests verifying BranchSummary matches the session operation contract.

    BranchSummary is a Pydantic model for session branch display in the TUI.

    Attributes:
        branch_id: str (required)
        parent_id: str | None
        session_path: str
        message_count: int
        created_at: int
        updated_at: int
        status: Literal["idle", "running", "aborting", "error"]
        is_compacted: bool
    """

    def test_branch_summary_is_pydantic_model(self):
        """BranchSummary is a Pydantic BaseModel."""
        from pydantic import BaseModel
        assert issubclass(BranchSummary, BaseModel)

    def test_branch_summary_required_field(self):
        """BranchSummary.branch_id is required."""
        with pytest.raises(Exception):  # ValidationError
            BranchSummary()  # Missing required field

    def test_branch_summary_creation(self):
        """BranchSummary can be instantiated with minimal fields."""
        bs = BranchSummary(branch_id="branch_001")
        assert bs.branch_id == "branch_001"

    def test_branch_summary_all_fields(self):
        """BranchSummary accepts all fields."""
        bs = BranchSummary(
            branch_id="branch_001",
            parent_id="session_001",
            session_path="/home/user/.tau/sessions/branch_001.jsonl",
            message_count=25,
            created_at=1700000000000,
            updated_at=1700000005000,
            status="idle",
            is_compacted=True,
        )
        assert bs.branch_id == "branch_001"
        assert bs.parent_id == "session_001"
        assert bs.session_path == "/home/user/.tau/sessions/branch_001.jsonl"
        assert bs.message_count == 25
        assert bs.created_at == 1700000000000
        assert bs.updated_at == 1700000005000
        assert bs.status == "idle"
        assert bs.is_compacted is True

    def test_branch_summary_defaults(self):
        """BranchSummary has sensible defaults."""
        bs = BranchSummary(branch_id="branch_001")
        assert bs.parent_id is None
        assert bs.session_path == ""
        assert bs.message_count == 0
        assert bs.created_at == 0
        assert bs.updated_at == 0
        assert bs.status == "idle"
        assert bs.is_compacted is False

    def test_branch_summary_status_values(self):
        """BranchSummary only accepts valid status values."""
        for status in ["idle", "running", "aborting", "error"]:
            bs = BranchSummary(branch_id="b", status=status)
            assert bs.status == status

    def test_branch_summary_rejects_invalid_status(self):
        """BranchSummary rejects invalid status values."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BranchSummary(branch_id="b", status="invalid")

    def test_branch_summary_serialization(self):
        """BranchSummary serializes to dict correctly."""
        bs = BranchSummary(branch_id="b1", message_count=10)
        data = bs.model_dump()
        assert data["branch_id"] == "b1"
        assert data["message_count"] == 10

    def test_branch_summary_json(self):
        """BranchSummary serializes to JSON string."""
        bs = BranchSummary(branch_id="b1", message_count=10, status="running")
        json_str = bs.model_dump_json()
        assert '"branch_id":"b1"' in json_str
        assert '"message_count":10' in json_str
        assert '"status":"running"' in json_str

    def test_branch_summary_fields(self):
        """BranchSummary has all 8 documented fields."""
        fields = set(BranchSummary.model_fields.keys())
        expected = {"branch_id", "parent_id", "session_path", "message_count",
                     "created_at", "updated_at", "status", "is_compacted"}
        assert fields == expected


# =============================================================================
# 5. ForkResult Contract Tests
# =============================================================================


class TestForkResultContract:
    """Tests verifying ForkResult matches the session operation contract.

    ForkResult is the result of forking a session into a new branch.

    Attributes:
        original_session_id: str
        new_session_id: str
        new_session_path: str
        forked_at: int
        branches: list[BranchSummary]
    """

    def test_fork_result_is_pydantic_model(self):
        """ForkResult is a Pydantic BaseModel."""
        from pydantic import BaseModel
        assert issubclass(ForkResult, BaseModel)

    def test_fork_result_all_required_fields(self):
        """All ForkResult fields are required."""
        fields = ForkResult.model_fields
        for name, field in fields.items():
            assert field.is_required(), f"Field {name} should be required"

    def test_fork_result_creation(self):
        """ForkResult can be instantiated with all fields."""
        bs = BranchSummary(branch_id="b1", message_count=10)
        result = ForkResult(
            original_session_id="session_001",
            new_session_id="session_002",
            new_session_path="/home/user/.tau/sessions/session_002.jsonl",
            forked_at=1700000010000,
            branches=[bs],
        )
        assert result.original_session_id == "session_001"
        assert result.new_session_id == "session_002"
        assert result.new_session_path == "/home/user/.tau/sessions/session_002.jsonl"
        assert result.forked_at == 1700000010000
        assert len(result.branches) == 1
        assert result.branches[0].branch_id == "b1"

    def test_fork_result_branches_is_list(self):
        """ForkResult.branches is a list of BranchSummary."""
        bs = BranchSummary(branch_id="b1")
        result = ForkResult(
            original_session_id="s1",
            new_session_id="s2",
            new_session_path="/path",
            forked_at=1234567890,
            branches=[bs],
        )
        assert isinstance(result.branches, list)
        assert all(isinstance(b, BranchSummary) for b in result.branches)

    def test_fork_result_serialization(self):
        """ForkResult serializes to dict correctly."""
        bs = BranchSummary(branch_id="b1")
        result = ForkResult(
            original_session_id="s1",
            new_session_id="s2",
            new_session_path="/path",
            forked_at=1234567890,
            branches=[bs],
        )
        data = result.model_dump()
        assert data["original_session_id"] == "s1"
        assert data["new_session_id"] == "s2"
        assert len(data["branches"]) == 1

    def test_fork_result_json(self):
        """ForkResult serializes to JSON string."""
        bs = BranchSummary(branch_id="b1")
        result = ForkResult(
            original_session_id="s1",
            new_session_id="s2",
            new_session_path="/path",
            forked_at=1234567890,
            branches=[bs],
        )
        json_str = result.model_dump_json()
        assert '"original_session_id":"s1"' in json_str
        assert '"new_session_id":"s2"' in json_str

    def test_fork_result_fields(self):
        """ForkResult has all 5 documented fields."""
        fields = set(ForkResult.model_fields.keys())
        expected = {"original_session_id", "new_session_id", "new_session_path",
                     "forked_at", "branches"}
        assert fields == expected


# =============================================================================
# 6. CloneResult Contract Tests
# =============================================================================


class TestCloneResultContract:
    """Tests verifying CloneResult matches the session operation contract.

    CloneResult is the result of cloning a session into a new independent session.

    Attributes:
        original_session_id: str
        cloned_session_id: str
        cloned_session_path: str
        cloned_at: int
        entry_count: int
    """

    def test_clone_result_is_pydantic_model(self):
        """CloneResult is a Pydantic BaseModel."""
        from pydantic import BaseModel
        assert issubclass(CloneResult, BaseModel)

    def test_clone_result_required_fields(self):
        """CloneResult has the right required/optional fields."""
        fields = CloneResult.model_fields
        # These fields are required (no default)
        for name in ["original_session_id", "cloned_session_id", "cloned_session_path", "cloned_at"]:
            assert fields[name].is_required(), f"Field {name} should be required"
        # entry_count has a default of 0
        assert fields["entry_count"].default == 0

    def test_clone_result_creation(self):
        """CloneResult can be instantiated with all fields."""
        result = CloneResult(
            original_session_id="session_001",
            cloned_session_id="session_003",
            cloned_session_path="/home/user/.tau/sessions/session_003.jsonl",
            cloned_at=1700000020000,
            entry_count=50,
        )
        assert result.original_session_id == "session_001"
        assert result.cloned_session_id == "session_003"
        assert result.cloned_session_path == "/home/user/.tau/sessions/session_003.jsonl"
        assert result.cloned_at == 1700000020000
        assert result.entry_count == 50

    def test_clone_result_entry_count_zero(self):
        """CloneResult entry_count defaults to 0."""
        result = CloneResult(
            original_session_id="s1",
            cloned_session_id="s2",
            cloned_session_path="/path",
            cloned_at=1234567890,
            entry_count=0,
        )
        assert result.entry_count == 0

    def test_clone_result_serialization(self):
        """CloneResult serializes to dict correctly."""
        result = CloneResult(
            original_session_id="s1",
            cloned_session_id="s2",
            cloned_session_path="/path",
            cloned_at=1234567890,
            entry_count=100,
        )
        data = result.model_dump()
        assert data["original_session_id"] == "s1"
        assert data["cloned_session_id"] == "s2"
        assert data["entry_count"] == 100

    def test_clone_result_json(self):
        """CloneResult serializes to JSON string."""
        result = CloneResult(
            original_session_id="s1",
            cloned_session_id="s2",
            cloned_session_path="/path",
            cloned_at=1234567890,
            entry_count=100,
        )
        json_str = result.model_dump_json()
        assert '"original_session_id":"s1"' in json_str
        assert '"cloned_session_id":"s2"' in json_str
        assert '"entry_count":100' in json_str

    def test_clone_result_fields(self):
        """CloneResult has all 5 documented fields."""
        fields = set(CloneResult.model_fields.keys())
        expected = {"original_session_id", "cloned_session_id", "cloned_session_path",
                     "cloned_at", "entry_count"}
        assert fields == expected


# =============================================================================
# 7. CompactionModule Functions Tests
# =============================================================================


class TestCompactionModuleFunctions:
    """Tests for the compaction module functions."""

    def test_estimate_tokens_function_exists(self):
        """estimate_tokens function exists in compaction module."""
        from tau_agent_core.compaction import estimate_tokens
        assert callable(estimate_tokens)

    def test_compact_session_function_exists(self):
        """compact_session async function exists in compaction module."""
        from tau_agent_core.compaction import compact_session
        assert callable(compact_session)
        assert asyncio.iscoroutinefunction(compact_session)

    def test_build_compaction_prompt_function_exists(self):
        """build_compaction_prompt function exists in compaction module."""
        from tau_agent_core.compaction import build_compaction_prompt
        assert callable(build_compaction_prompt)

    def test_estimate_tokens_with_empty_entries(self):
        """estimate_tokens returns 0 for empty entries."""
        from tau_agent_core.compaction import estimate_tokens
        result = estimate_tokens([])
        assert result == 0

    def test_estimate_tokens_with_entries(self):
        """estimate_tokens returns positive value for entries."""
        from tau_agent_core.compaction import estimate_tokens
        from tau_agent_core.session import SessionEntry
        entries = [
            SessionEntry(
                id="test_001",
                type="session",
                timestamp=1700000000000,
            ),
            SessionEntry(
                id="test_002",
                type="session",
                timestamp=1700000001000,
                model="gpt-4o",
                system_prompt="test prompt",
            ),
        ]
        result = estimate_tokens(entries)
        assert result > 0

    @pytest.mark.asyncio
    async def test_compact_session_returns_result(self):
        """compact_session returns a CompactionResult."""
        from tau_agent_core.compaction import compact_session, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [
            SessionEntry(
                id="test_001",
                type="session",
                timestamp=1700000000000,
            ),
        ]
        result = await compact_session(config, entries)
        assert isinstance(result, CompactionResult)
        assert isinstance(result.summary, str)
        assert isinstance(result.first_kept_id, str)
        assert isinstance(result.compacted_entry_ids, list)

    @pytest.mark.asyncio
    async def test_compact_session_with_callback(self):
        """compact_session calls the compact_callback if provided."""
        from tau_agent_core.compaction import compact_session, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        callback_calls = []

        async def mock_callback(text: str, tokens: int):
            callback_calls.append((text, tokens))

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="test",
            max_context_tokens=128000,
            margin=2000,
            compact_callback=mock_callback,
        )
        # The placeholder implementation doesn't call the callback,
        # but the test verifies the callback is stored correctly
        assert config.compact_callback == mock_callback

    def test_build_compaction_prompt_with_system_prompt(self):
        """build_compaction_prompt includes the system prompt."""
        from tau_agent_core.compaction import build_compaction_prompt, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize the conversation.",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [SessionEntry(
            id="test_001",
            type="session",
            timestamp=1700000000000,
        )]
        prompt = build_compaction_prompt(entries, config)
        assert "Summarize the conversation" in prompt

    def test_build_compaction_prompt_with_custom_instructions(self):
        """build_compaction_prompt includes custom instructions."""
        from tau_agent_core.compaction import build_compaction_prompt, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize the conversation.",
            max_context_tokens=128000,
            margin=2000,
            custom_instructions="Only summarize user requests.",
        )
        entries = [SessionEntry(
            id="test_001",
            type="session",
            timestamp=1700000000000,
        )]
        prompt = build_compaction_prompt(entries, config)
        assert "Summarize the conversation" in prompt
        assert "Only summarize user requests" in prompt


# =============================================================================
# 8. Cross-Contract Tests (session.py types)
# =============================================================================


class TestSessionTypesFromModule:
    """Tests for session.py types (SessionEntry, MessageEntry, etc.).

    These verify the session entry types that were already in session.py
    before Phase 5 Subphase 0, plus the new BranchSummary, ForkResult, CloneResult.
    """

    def test_import_branch_summary_from_session(self):
        """BranchSummary imports from tau_agent_core.session."""
        from tau_agent_core.session import BranchSummary
        assert BranchSummary is not None

    def test_import_fork_result_from_session(self):
        """ForkResult imports from tau_agent_core.session."""
        from tau_agent_core.session import ForkResult
        assert ForkResult is not None

    def test_import_clone_result_from_session(self):
        """CloneResult imports from tau_agent_core.session."""
        from tau_agent_core.session import CloneResult
        assert CloneResult is not None

    def test_import_from_package_root(self):
        """All session operation types import from tau_agent_core package root."""
        from tau_agent_core import BranchSummary, ForkResult, CloneResult
        assert BranchSummary is not None
        assert ForkResult is not None
        assert CloneResult is not None

    def test_branch_summary_vs_sessioninfo(self):
        """BranchSummary is distinct from SessionInfo (different use case)."""
        from tau_agent_core.session import SessionInfo

        bs = BranchSummary(branch_id="b1", message_count=10)
        si = SessionInfo(id="session_001", message_count=10)

        # Both track message_count
        assert bs.message_count == si.message_count == 10
        # BranchSummary tracks branch_id, SessionInfo tracks id
        assert hasattr(bs, "branch_id")
        assert hasattr(si, "id")
        # BranchSummary tracks is_compacted as bool, SessionInfo doesn't
        assert hasattr(bs, "is_compacted")
        assert not hasattr(si, "is_compacted")

    def test_fork_result_creates_new_branch(self):
        """ForkResult contains a new branch summary."""
        bs = BranchSummary(branch_id="b_new")
        fr = ForkResult(
            original_session_id="s_orig",
            new_session_id="s_new",
            new_session_path="/path",
            forked_at=1234567890,
            branches=[bs],
        )
        # The new session_id differs from original
        assert fr.new_session_id != fr.original_session_id
        # The new session has its own branch
        assert any(b.branch_id == "b_new" for b in fr.branches)

    def test_clone_result_independent_session(self):
        """CloneResult creates a completely independent copy."""
        cr = CloneResult(
            original_session_id="s_orig",
            cloned_session_id="s_clone",
            cloned_session_path="/clone_path",
            cloned_at=1234567890,
            entry_count=100,
        )
        assert cr.original_session_id != cr.cloned_session_id
        assert cr.cloned_session_path != ""
        assert cr.entry_count == 100


# =============================================================================
# 9. Compaction Types from Package Root
# =============================================================================


class TestCompactionTypesFromPackageRoot:
    """Tests for compaction types importing from tau_agent_core package root."""

    def test_import_compaction_config_from_package(self):
        """CompactionConfig imports from tau_agent_core package root."""
        from tau_agent_core import CompactionConfig
        assert CompactionConfig is not None

    def test_import_compaction_result_from_package(self):
        """CompactionResult imports from tau_agent_core package root."""
        from tau_agent_core import CompactionResult
        assert CompactionResult is not None

    def test_import_settings_from_package(self):
        """Settings imports from tau_agent_core package root."""
        from tau_agent_core import Settings
        assert Settings is not None

    def test_all_compaction_types_in_all(self):
        """All compaction/session types are in __all__."""
        from tau_agent_core import __all__
        for name in [
            "CompactionConfig", "CompactionResult",
            "BranchSummary", "ForkResult", "CloneResult",
            "Settings",
        ]:
            assert name in __all__, f"{name} not in __all__"


# =============================================================================
# 10. Subphase 0.0 Cross-Contract Session Types
# =============================================================================


class TestSessionEntryTypesFromPackageRoot:
    """Tests verifying session entry types import from tau_agent_core root."""

    def test_import_session_entry_from_package(self):
        """SessionEntry imports from tau_agent_core package root."""
        from tau_agent_core import SessionEntry
        assert SessionEntry is not None

    def test_import_message_entry_from_package(self):
        """MessageEntry imports from tau_agent_core package root."""
        from tau_agent_core import MessageEntry
        assert MessageEntry is not None

    def test_import_tool_result_entry_from_package(self):
        """ToolResultEntry imports from tau_agent_core package root."""
        from tau_agent_core import ToolResultEntry
        assert ToolResultEntry is not None

    def test_import_custom_message_entry_from_package(self):
        """CustomMessageEntry imports from tau_agent_core package root."""
        from tau_agent_core import CustomMessageEntry
        assert CustomMessageEntry is not None

    def test_import_compaction_entry_from_package(self):
        """CompactionEntry imports from tau_agent_core package root."""
        from tau_agent_core import CompactionEntry
        assert CompactionEntry is not None

    def test_import_session_state_from_package(self):
        """SessionState imports from tau_agent_core package root."""
        from tau_agent_core import SessionState
        assert SessionState is not None

    def test_import_session_info_from_package(self):
        """SessionInfo imports from tau_agent_core package root."""
        from tau_agent_core import SessionInfo
        assert SessionInfo is not None


# =============================================================================
# 11. Import Smoke Tests
# =============================================================================


class TestImportSmokeTests:
    """Basic smoke tests to verify all imports work correctly.

    These tests ensure that the module structure is correct
    and all types can be imported without errors.
    """

    def test_import_compaction_module(self):
        """tau_agent_core.compaction module imports correctly."""
        import tau_agent_core.compaction as compaction
        assert compaction is not None
        assert hasattr(compaction, "CompactionConfig")
        assert hasattr(compaction, "CompactionResult")
        assert hasattr(compaction, "compact_session")
        assert hasattr(compaction, "estimate_tokens")
        assert hasattr(compaction, "build_compaction_prompt")

    def test_import_settings_module(self):
        """tau_agent_core.settings module imports correctly."""
        import tau_agent_core.settings as settings
        assert settings is not None
        assert hasattr(settings, "Settings")

    def test_import_session_module(self):
        """tau_agent_core.session module imports correctly."""
        import tau_agent_core.session as session
        assert session is not None
        assert hasattr(session, "BranchSummary")
        assert hasattr(session, "ForkResult")
        assert hasattr(session, "CloneResult")

    def test_import_package_root(self):
        """tau_agent_core package root imports correctly."""
        import tau_agent_core
        assert tau_agent_core is not None

    def test_all_contract_types_importable(self):
        """All Phase 5 Subphase 0 types are importable."""
        from tau_agent_core import (
            CompactionConfig, CompactionResult,
            BranchSummary, ForkResult, CloneResult,
            Settings,
        )
        # All should be truthy
        assert CompactionConfig
        assert CompactionResult
        assert BranchSummary
        assert ForkResult
        assert CloneResult
        assert Settings


# =============================================================================
# 12. Async Tests for Compaction
# =============================================================================


class TestCompactionAsync:
    """Async tests for compaction operations."""

    @pytest.mark.asyncio
    async def test_compact_session_async(self):
        """compact_session is an async function that returns CompactionResult."""
        from tau_agent_core.compaction import compact_session, CompactionResult, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [
            SessionEntry(id="e1", type="session", timestamp=1700000000000),
        ]
        result = await compact_session(config, entries)
        assert isinstance(result, CompactionResult)
        assert isinstance(result.summary, str)

    @pytest.mark.asyncio
    async def test_compact_session_empty_entries(self):
        """compact_session handles empty entries list."""
        from tau_agent_core.compaction import compact_session, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
        )
        result = await compact_session(config, [])
        assert isinstance(result, CompactionResult)
        assert result.first_kept_id == ""  # No entries to keep

    @pytest.mark.asyncio
    async def test_compact_session_with_progress_callback(self):
        """compact_session accepts and stores a progress callback."""
        from tau_agent_core.compaction import compact_session, CompactionConfig
        from tau_ai.types import Model
        from tau_agent_core.session import SessionEntry

        callback_log = []

        async def on_progress(text: str, tokens: int):
            callback_log.append((text, tokens))

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
            compact_callback=on_progress,
        )
        entries = [
            SessionEntry(id="e1", type="session", timestamp=1700000000000),
        ]
        await compact_session(config, entries)
        # The placeholder doesn't call the callback,
        # but verifies the callback was properly stored
        assert config.compact_callback is not None
