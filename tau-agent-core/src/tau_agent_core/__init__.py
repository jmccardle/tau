"""τ-agent-core: Agent runtime, loop, tools, sessions, extensions.

Public API:
- AgentSession: The main session/loop API
- SessionManager: Session persistence
- AgentEvent: Event types from the agent loop

Reference: SUBPHASE-0.0.md
"""

from tau_agent_core.events import AgentEvent
from tau_agent_core.session import (
    SessionEntry,
    MessageEntry,
    ToolResultEntry,
    CustomMessageEntry,
    CompactionEntry,
)
from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)

__all__ = [
    "AgentSession",
    "SessionManager",
    "AgentEvent",
    "SessionEntry",
    "MessageEntry",
    "ToolResultEntry",
    "CustomMessageEntry",
    "CompactionEntry",
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionUI",
]


class AgentSession:
    """Public API for agent sessions.

    This is the ONLY interface that τ-coding-agent uses to interact
    with τ-agent-core.

    Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.
    """

    def __init__(self) -> None:
        self._messages: list[dict] = []
        self._state: str = "idle"
        self._is_streaming: bool = False
        self._subscribers: list = []

    @property
    def messages(self) -> list[dict]:
        return self._messages

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        self._state = value

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    def subscribe(self, handler) -> callable:
        """Subscribe to agent events. Returns unsubscribe function."""
        self._subscribers.append(handler)

        def unsubscribe():
            self._subscribers.remove(handler)

        return unsubscribe

    async def prompt(self, text: str, images: list | None = None) -> list[dict]:
        """Send a prompt and run the agent loop."""
        self._is_streaming = True
        self.state = "running"

        mock_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": f"Response to: {text}"}],
        }
        self._messages.append(mock_msg)

        self._is_streaming = False
        self.state = "idle"
        return self._messages

    async def continue_conversation(self) -> list[dict]:
        """Run another agent turn without adding a new prompt."""
        self._is_streaming = True
        self.state = "running"

        mock_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "Continuation response"}],
        }
        self._messages.append(mock_msg)

        self._is_streaming = False
        self.state = "idle"
        return self._messages

    async def compact(self, custom_instructions: str | None = None) -> None:
        """Trigger manual compaction."""
        pass

    def abort(self) -> None:
        """Abort the current agent turn."""
        self._is_streaming = False
        self.state = "aborting"


class SessionManager:
    """Session persistence manager (stub).

    Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section.
    """

    def __init__(self, path: str = "") -> None:
        pass

    def append_entry(self, entry: dict) -> str:
        """Append an entry and return its ID."""
        return ""

    def get_entries(self, session_id: str = "") -> list:
        """Get all entries for a session."""
        return []

    def list_sessions(self) -> list[str]:
        """List all session IDs."""
        return []
