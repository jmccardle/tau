# Phase 2 Subphase 0 — Data Contract Definition

> **Topic**: Finalize the agent loop types, session entry formats, and tool execution types.

## Scope

This subphase locks the types that τ-agent-core defines and that τ-coding-agent consumes. These types bridge τ-ai (Phase 1) and the agent loop (Phase 2.1).

## Done Criteria

The following files are written with final type signatures:

1. **`tau_agent_core/events.py`**: `AgentEvent` dataclass with all variant fields
2. **`tau_agent_core/tools/base.py`**: `ToolDefinition` (agent-core version), `AgentTool`, `AgentToolResult`, `ToolBatchResult`
3. **`tau_agent_core/session.py`**: `SessionEntry`, `SessionState`, `SessionInfo` dataclasses and JSON serialization
4. **`tau_agent_core/agent_loop_types.py`**: `PreparedToolCall`, `FinalizedToolCall`, `AgentLoopConfig`
5. **`tau_agent_core/extension_types.py`**: Stub types for Phase 3

### AgentEvent Field Map

This is the contract that Phase 3 (extensions) and Phase 4 (TUI) depend on:

```
AgentEvent fields:
  type: Literal[
    "agent_start", "agent_end",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
  ]
  # Always present:
  timestamp: int  # ms since epoch
  # By type:
  message: Message | None         # agent_start/end, message_*
  turn_index: int | None          # turn_*
  tool_call_id: str | None        # tool_*
  tool_name: str | None           # tool_*
  args: dict | None               # tool_execution_start
  result: Any | None              # tool_execution_*
  is_error: bool = False          # all
  tool_results: list[ToolResultMessage] | None  # turn_end
  messages: list[Message] | None  # agent_end
```

### SessionEntry Discriminated Union

```
SessionEntry variants (type field):
  - "session"      : id, timestamp, parent_id, model, model_name, cwd, system_prompt, session_name
  - "message"      : id, timestamp, parent_id, message
  - "toolResult"   : id, timestamp, parent_id, tool_call_id, tool_name, content, is_error
  - "customMessage": id, timestamp, parent_id, custom_type, message
  - "compaction"   : id, timestamp, parent_id, first_kept_id, summary, tokens_saved, compacted_entries
```

## Reference

- `SUBPHASE-0.0.md` lines 200-260: event and session contracts
- `docs/tau-agent-core.md` lines 200-350: agent loop types
- `docs/tau-agent-core.md` lines 350-450: session types
- `MONOREPO-STRUCTURE.md` lines 30-50: file layout

## Testing

```python
# 1. All types import
from tau_agent_core.events import AgentEvent
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult
from tau_agent_core.session import SessionEntry, SessionState, SessionInfo
from tau_agent_core.agent_loop_types import PreparedToolCall, FinalizedToolCall, AgentLoopConfig
from tau_agent_core.extension_types import ExtensionAPI

# 2. AgentEvent is a dataclass with all fields
import dataclasses
assert dataclasses.is_dataclass(AgentEvent)
field_names = {f.name for f in dataclasses.fields(AgentEvent)}
assert "type" in field_names
assert "timestamp" in field_names
assert "message" in field_names
assert "tool_call_id" in field_names

# 3. SessionEntry has discriminated union (oneOf)
# Verify with JSON schema or isinstance checks
session_entry = SessionEntry(
    id="test", type="session", timestamp=1718668800000,
    model="gpt-4o", model_name="GPT-4o", cwd="/tmp"
)
assert session_entry.type == "session"

# 4. ToolBatchResult is serializable
result = ToolBatchResult(
    messages=[],
    terminate=False,
)
assert result.terminate is False

# 5. AgentLoopConfig has all optional fields
config = AgentLoopConfig(
    model=model,
    system_prompt="test",
    tool_execution_mode="parallel",
)
assert config.tool_execution_mode == "parallel"
assert config.max_retries == 3  # default
```

## Success Signal

All types import. All fields match the contract in `SUBPHASE-0.0.md`. An agent reading `AgentEvent` can determine which fields are present by checking `type`. This is the final contract before implementation begins.
