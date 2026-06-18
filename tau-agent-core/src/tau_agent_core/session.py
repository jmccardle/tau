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


class SessionState(BaseModel):
    """Read-only session state.

    Represents the current state of a session, including metadata
    about the session's lifecycle and current condition.

    Attributes:
        session_id: The session's unique identifier
        status: Current status ("idle", "running", "aborting", "error")
        message_count: Number of messages in the session
        turn_count: Number of turns completed
        is_compacted: Whether the session has been compacted
        created_at: Session creation timestamp (ms since epoch)
        updated_at: Last update timestamp (ms since epoch)
    """

    session_id: str
    status: Literal["idle", "running", "aborting", "error"] = "idle"
    message_count: int = 0
    turn_count: int = 0
    is_compacted: bool = False
    created_at: int = 0
    updated_at: int = 0


class SessionInfo(BaseModel):
    """Session metadata and info.

    Lightweight info about a session, used for listing and display.
    Does not include the full session contents.

    Attributes:
        id: Session identifier (optional, auto-generated if not provided)
        session_path: Path to the JSONL session file
        cwd: Working directory for this session
        model: Model identifier used for this session
        model_name: Human-readable model name
        created_at: Creation timestamp (ms since epoch)
        updated_at: Last update timestamp (ms since epoch)
        message_count: Total number of messages
        turn_count: Total number of turns
        status: Current session status
    """

    model_config = {"extra": "allow"}

    id: str = ""
    name: str | None = None
    session_path: str = ""
    cwd: str | None = None
    model: str | None = None
    model_name: str | None = None
    created_at: int = 0
    updated_at: int = 0
    message_count: int = 0
    turn_count: int = 0
    tool_count: int = 0
    status: Literal["idle", "running", "aborting", "error"] = "idle"



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
