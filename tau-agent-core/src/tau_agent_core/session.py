"""τ-agent-core session: SessionEntry types for JSONL persistence.

Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.

Entry types:
- SessionEntry: Root session entry
- MessageEntry: User/assistant message
- ToolResultEntry: Tool execution result
- CustomMessageEntry: Custom extension messages
- CompactionEntry: Session compaction record

Constraint: JSONL format is append-only. No in-place edits.
Sessions are rebuilt by replaying entries.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionEntry(BaseModel):
    """Root session entry.

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    id: str
    type: Literal["session"] = "session"
    timestamp: int = Field(ge=0)
    parent_id: str | None = None
    model: str | None = None
    model_name: str | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    session_name: str | None = None


class MessageEntry(BaseModel):
    """Message entry: stores a single message.

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    id: str
    type: Literal["message"] = "message"
    timestamp: int = Field(ge=0)
    parent_id: str | None = None
    message: dict[str, Any]


class ToolResultEntry(BaseModel):
    """Tool result entry: stores tool execution result.

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    id: str
    type: Literal["toolResult"] = "toolResult"
    timestamp: int = Field(ge=0)
    parent_id: str | None = None
    tool_call_id: str
    tool_name: str
    content: list[dict[str, Any]]
    is_error: bool = False


class CustomMessageEntry(BaseModel):
    """Custom message entry: extension-generated messages.

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    id: str
    type: Literal["customMessage"] = "customMessage"
    timestamp: int = Field(ge=0)
    parent_id: str | None = None
    custom_type: str
    message: dict[str, Any]


class CompactionEntry(BaseModel):
    """Compaction entry: records session compaction.

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    id: str
    type: Literal["compaction"] = "compaction"
    timestamp: int = Field(ge=0)
    parent_id: str | None = None
    first_kept_id: str
    summary: str
    tokens_saved: int = 0
    compacted_entries: list[str] = Field(default_factory=list)
