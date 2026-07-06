# Phase 5 Subphase 0 — Data Contract Definition

> **Topic**: Finalize compaction and session operation types.

## Scope

This subphase locks the types for compaction and session operations. These types are consumed by Phase 5 implementation and by the TUI (for compaction display in the session tree).

## Done Criteria

The following files contain the final type signatures:

1. **`tau_agent_core/compaction.py`**: `CompactionConfig`, `CompactionResult`, compaction types
2. **`tau_agent_core/session.py`**: `BranchSummary`, `ForkResult`, `CloneResult` types
3. **`tau_agent_core/settings.py`**: `Settings` dataclass

### CompactionConfig Contract

```python
@dataclass
class CompactionConfig:
    model: Model
    system_prompt: str
    max_context_tokens: int
    margin: int  # tokens to keep as margin before hitting max
    custom_instructions: str | None = None
    compact_callback: Callable[[str, int], Awaitable[None]] | None = None  # for progress
```

### CompactionResult Contract

```python
@dataclass
class CompactionResult:
    """Result of a compaction operation."""
    summary: str  # The LLM-generated summary
    first_kept_id: str  # ID of the first message kept in full
    compacted_entry_ids: list[str]  # IDs of entries that were compacted
    tokens_saved: int  # Estimated tokens saved
    tokens_before: int
    tokens_after: int
```

### Settings Contract

```python
@dataclass
class Settings:
    """τ settings (from ~/.tau/settings.json)."""
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
```

## Reference

- `SUBPHASE-0.0.md` lines 220-260: session entry JSON schema
- `docs/tau-agent-core.md` lines 350-450: compaction design
- `docs/IMPLEMENTATION-PLAN.md` lines 360-420: compaction spec

## Testing

```python
from tau_agent_core.compaction import CompactionConfig, CompactionResult
from tau_agent_core.session import BranchSummary, ForkResult, CloneResult
from tau_agent_core.settings import Settings

# All types import
assert dataclasses.is_dataclass(CompactionConfig)
assert dataclasses.is_dataclass(CompactionResult)
assert dataclasses.is_dataclass(Settings)

# Settings has expected defaults
s = Settings()
assert s.default_model == "gpt-4o"
assert s.thinking_level == "off"
assert s.compaction_enabled is True
```

## Success Signal

All types import with the correct fields and defaults. This is the final contract before Phase 5 implementation.
