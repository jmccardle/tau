"""tau-agent-core test fixtures.

Provides shared fixtures for tau-agent-core package tests:
- in_memory_session_manager: In-memory session manager for testing
- sample_agent_event: Sample AgentEvent instances for all event types
- sample_tool_definition: Sample ToolDefinition for testing

Reference: SUBPHASE-0.0.md lines 260-340
"""

import pytest

from tau_agent_core.events import AgentEvent
from tau_agent_core.session import SessionEntry


@pytest.fixture
def in_memory_session_manager():
    """Fixture providing an in-memory SessionManager for testing.

    The SessionManager manages JSONL session persistence. This fixture
    provides an in-memory implementation for testing without file I/O.

    Expected interface:
    - append_entry(entry: dict) -> None
    - get_entries() -> list[dict]
    - get_session(session_id: str) -> dict | None
    - list_sessions() -> list[str]
    - fork_session(session_id: str, new_id: str) -> None
    - compact(session_id: str) -> None
    """
    from collections import defaultdict

    class InMemorySessionManager:
        """In-memory implementation of SessionManager for testing."""

        def __init__(self):
            self._sessions = defaultdict(list)  # session_id -> [entries]
            self._all_entries = []  # Global entry list
            self._entry_counter = 0

        def append_entry(self, entry: dict) -> str:
            """Append an entry and return its ID."""
            self._entry_counter += 1
            entry_id = f"entry_{self._entry_counter:06d}"
            entry = {
                "id": entry_id,
                "parent_id": entry.get("parent_id"),
                "timestamp": entry.get("timestamp", 0),
                **entry,
            }
            session_id = entry.get("session_id", "default")
            self._sessions[session_id].append(entry)
            self._all_entries.append(entry)
            return entry_id

        def get_entries(self, session_id: str | None = None) -> list[dict]:
            """Get all entries, optionally filtered by session."""
            if session_id:
                return self._sessions.get(session_id, [])
            return self._all_entries

        def get_session(self, session_id: str) -> dict | None:
            """Get session metadata."""
            entries = self._sessions.get(session_id, [])
            if not entries:
                return None
            return {
                "id": session_id,
                "entry_count": len(entries),
                "entries": entries,
            }

        def list_sessions(self) -> list[str]:
            """List all session IDs."""
            return list(self._sessions.keys())

        def fork_session(self, session_id: str, new_id: str) -> None:
            """Fork a session, creating a new branch."""
            entries = self._sessions.get(session_id, [])
            if entries:
                last_entry = entries[-1]
                # New session inherits entries with updated parent_id
                for entry in entries:
                    entry["parent_id"] = session_id
                    entry["id"] = entry["id"].replace(session_id, new_id)
                self._sessions[new_id] = entries

        def compact(self, session_id: str) -> None:
            """Compact a session, removing old entries."""
            pass  # Minimal implementation for testing

    return InMemorySessionManager()


@pytest.fixture
def sample_agent_event():
    """Fixture providing sample AgentEvent instances.

    Returns a dict of event types to sample event dicts.
    Each event has:
    - type: Literal[event_type]
    - timestamp: int (ms since epoch)
    - is_error: bool = False
    - type-specific fields

    Reference: SUBPHASE-0.0.md AgentEvent section
    """
    import time

    base_ts = int(time.time() * 1000)

    return {
        "agent_start": {
            "type": "agent_start",
            "timestamp": base_ts,
            "message": None,
            "turn_index": None,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "agent_end": {
            "type": "agent_end",
            "timestamp": base_ts + 1000,
            "message": None,
            "turn_index": None,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": [],
        },
        "turn_start": {
            "type": "turn_start",
            "timestamp": base_ts,
            "message": None,
            "turn_index": 0,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "turn_end": {
            "type": "turn_end",
            "timestamp": base_ts + 5000,
            "message": None,
            "turn_index": 0,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": [],
            "messages": None,
        },
        "message_start": {
            "type": "message_start",
            "timestamp": base_ts + 100,
            "message": {"role": "assistant", "content": []},
            "turn_index": 0,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "message_update": {
            "type": "message_update",
            "timestamp": base_ts + 200,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "H"}]},
            "turn_index": 0,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "message_end": {
            "type": "message_end",
            "timestamp": base_ts + 300,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "turn_index": 0,
            "tool_call_id": None,
            "tool_name": None,
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "tool_execution_start": {
            "type": "tool_execution_start",
            "timestamp": base_ts + 400,
            "message": None,
            "turn_index": 0,
            "tool_call_id": "call_123",
            "tool_name": "ls",
            "args": {"path": "."},
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "tool_execution_update": {
            "type": "tool_execution_update",
            "timestamp": base_ts + 500,
            "message": None,
            "turn_index": 0,
            "tool_call_id": "call_123",
            "tool_name": "ls",
            "args": None,
            "result": None,
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
        "tool_execution_end": {
            "type": "tool_execution_end",
            "timestamp": base_ts + 600,
            "message": None,
            "turn_index": 0,
            "tool_call_id": "call_123",
            "tool_name": "ls",
            "args": None,
            "result": "file1.txt\nfile2.py",
            "is_error": False,
            "tool_results": None,
            "messages": None,
        },
    }


@pytest.fixture
def sample_model():
    """Fixture providing a sample Model for testing."""
    from tau_ai.types import Model
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


@pytest.fixture
def sample_session_manager():
    """Fixture providing an in-memory SessionManager for testing."""
    from tau_agent_core.session_manager import SessionManager
    return SessionManager.in_memory()


@pytest.fixture
def sample_agent_session(sample_session_manager, sample_model):
    """Fixture providing an AgentSession for testing."""
    from tau_agent_core.agent_session import AgentSession
    return AgentSession(
        session_manager=sample_session_manager,
        model=sample_model,
    )


@pytest.fixture
def sample_tool_definition():
    """Fixture providing a sample ToolDefinition.

    Returns a dict representing a minimal tool definition.
    Reference: SUBPHASE-0.0.md ToolDefinitions section
    """
    return {
        "name": "ls",
        "label": "List Directory",
        "description": "List files and directories in a path",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to list",
                },
            },
            "required": ["path"],
        },
        "execute": lambda ctx: "test_output",
        "prompt_snippet": "ls: List directory contents",
        "prompt_guidelines": ["Use absolute paths"],
        "execution_mode": "parallel",
    }
