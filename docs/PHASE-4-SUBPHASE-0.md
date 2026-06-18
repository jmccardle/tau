# Phase 4 Subphase 0 — Data Contract Definition

> **Topic**: Finalize the TUI widget contracts and the agent event → widget mapping.

## Scope

This subphase locks the contracts between the TUI and τ-agent-core. The TUI consumes `AgentSession` and `AgentEvent` — these are already defined in Phase 2. The TUI also defines its own internal types (widget data classes, render props).

## Done Criteria

The following files contain the final type signatures:

1. **`tau_coding_agent/widgets/chat_display.py`**: `ChatMessageData` dataclass
2. **`tau_coding_agent/widgets/tool_call_widget.py`**: `ToolCallData` dataclass
3. **`tau_coding_agent/widgets/tool_result_widget.py`**: `ToolResultData` dataclass
4. **`tau_coding_agent/widgets/footer.py`**: `FooterData` dataclass
5. **`tau_coding_agent/app.py`**: `ParleyApp` stub (fork of parley.py, importable)
6. **`tau_coding_agent/cli.py`**: CLI argument types

### ChatMessageData Contract

```python
@dataclass
class ChatMessageData:
    """Data passed to a chat message widget."""
    role: str  # "user" | "assistant" | "toolResult"
    content: list[dict]  # serialized ContentBlock list
    timestamp: int | None = None
    streaming: bool = False
    # Tool-specific:
    tool_name: str | None = None
    tool_call_id: str | None = None
    is_error: bool = False
```

### ToolCallData Contract

```python
@dataclass
class ToolCallData:
    """Data for a tool call widget."""
    tool_name: str
    tool_call_id: str
    arguments: dict
    status: Literal["pending", "running", "done", "error"]
    result_preview: str | None = None
```

### FooterData Contract

```python
@dataclass
class FooterData:
    """Data for the footer widget."""
    model: str
    tokens: int | None = None
    context_percent: float | None = None
    thinking_level: str = "off"
    session_name: str | None = None
```

## Reference

- `SUBPHASE-0.0.md` lines 260-340: AgentSession and AgentEvent contracts
- `docs/tau-coding-agent.md` lines 100-200: widget designs
- `docs/tau-coding-agent.md` lines 200-280: TUI event flow
- `MONOREPO-STRUCTURE.md` lines 60-80: TUI file layout

## Testing

```python
# 1. All types import
from tau_coding_agent.widgets.chat_display import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultData
from tau_coding_agent.widgets.footer import FooterData
from tau_coding_agent.app import ParleyApp

# 2. ChatMessageData is a dataclass
import dataclasses
assert dataclasses.is_dataclass(ChatMessageData)
assert "role" in {f.name for f in dataclasses.fields(ChatMessageData)}
assert "content" in {f.name for f in dataclasses.fields(ChatMessageData)}

# 3. FooterData has all required fields
assert dataclasses.is_dataclass(FooterData)
assert "model" in {f.name for f in dataclasses.fields(FooterData)}
assert "tokens" in {f.name for f in dataclasses.fields(FooterData)}
```

## Success Signal

All types import and have the correct fields. The TUI widget data contracts match what τ-agent-core produces (AgentEvent fields → widget data). The ParleyApp stub is importable. The CLI argument types are defined. This is the final contract before TUI implementation begins.
