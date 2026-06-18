"""tau-coding-agent test fixtures.

Provides shared fixtures for tau-coding-agent package tests:
- mock_agent_session: Mock AgentSession for TUI testing
- mock_extension_api: Mock ExtensionAPI for extension testing

Reference: SUBPHASE-0.0.md AgentSession + ExtensionAPI sections
"""

import pytest


@pytest.fixture
def mock_agent_session():
    """Fixture providing a mock AgentSession for testing.

    AgentSession is the public API between τ-coding-agent and τ-agent-core.
    This mock implements the required interface:

    - messages: list[Message]
    - state: SessionState
    - is_streaming: bool
    - subscribe(handler) -> Callable[[], None]
    - prompt(text, images=None) -> list[Message]
    - continue_conversation() -> list[Message]
    - compact(custom_instructions=None)
    - abort()

    Usage in tests:
        async def test_prompt(mock_agent_session):
            messages = await mock_agent_session.prompt("Hello")
            assert len(messages) > 0
    """
    from collections import defaultdict
    from unittest.mock import MagicMock, AsyncMock

    class MockAgentSession:
        """Mock AgentSession for testing."""

        def __init__(self):
            self.messages: list[dict] = []
            self.is_streaming: bool = False
            self.state = "idle"
            self._subscribers: list = []
            self._prompt_history: list[dict] = []

        @property
        def state(self):
            return self._state

        @state.setter
        def state(self, value):
            self._state = value

        def subscribe(self, handler):
            """Subscribe to agent events. Returns unsubscribe function."""
            self._subscribers.append(handler)

            def unsubscribe():
                self._subscribers.remove(handler)
            return unsubscribe

        async def prompt(self, text: str, images: list | None = None) -> list[dict]:
            """Send a prompt and run the agent loop."""
            self.is_streaming = True
            self.state = "running"

            self._prompt_history.append({
                "text": text,
                "images": images,
                "timestamp": 0,  # Would be real timestamp
            })

            mock_msg = {
                "role": "assistant",
                "content": [{"type": "text", "text": f"Response to: {text}"}],
            }
            self.messages.append(mock_msg)

            self.is_streaming = False
            self.state = "idle"
            return self.messages

        async def continue_conversation(self) -> list[dict]:
            """Run another agent turn without adding a new prompt."""
            self.is_streaming = True
            self.state = "running"

            mock_msg = {
                "role": "assistant",
                "content": [{"type": "text", "text": "Continuation response"}],
            }
            self.messages.append(mock_msg)

            self.is_streaming = False
            self.state = "idle"
            return self.messages

        async def compact(self, custom_instructions: str | None = None):
            """Trigger manual compaction."""
            pass

        def abort(self) -> None:
            """Abort the current agent turn."""
            self.is_streaming = False
            self.state = "aborting"

        @property
        def prompt_history(self):
            return self._prompt_history

    return MockAgentSession()


@pytest.fixture
def mock_extension_api():
    """Fixture providing a mock ExtensionAPI for testing.

    ExtensionAPI is the public API exposed to extension modules.
    This mock implements the required interface from SUBPHASE-0.0.md:

    - on(event, handler)
    - register_tool(definition)
    - get_all_tools() -> list[ToolInfo]
    - set_active_tools(names)
    - register_command(name, command)
    - append_entry(custom_type, data)
    - set_session_name(name)
    - send_user_message(content, deliver_as)
    - send_message(message, options)
    - register_flag(name, options)
    - get_flag(name)
    - ui (property -> ExtensionUI)
    """
    from unittest.mock import MagicMock, AsyncMock

    class MockExtensionUI:
        """Mock ExtensionUI for headless testing."""

        async def confirm(self, title: str, message: str) -> bool:
            return True

        async def select(self, title: str, items: list[str]) -> str | None:
            return items[0] if items else None

        async def input(self, title: str, default: str = "") -> str:
            return default

        def notify(self, message: str, level: str = "info") -> None:
            pass

    class MockExtensionAPI:
        """Mock ExtensionAPI for testing."""

        def __init__(self):
            self._handlers = {}
            self._tools = []
            self._commands = {}
            self._flags = {}
            self._entries = []
            self._session_name = ""
            self._active_tools = []

        @property
        def ui(self) -> MockExtensionUI:
            return MockExtensionUI()

        def on(self, event: str, handler) -> None:
            if event not in self._handlers:
                self._handlers[event] = []
            self._handlers[event].append(handler)

        def register_tool(self, definition: dict) -> None:
            self._tools.append(definition)

        def get_all_tools(self) -> list[dict]:
            return self._tools

        def set_active_tools(self, names: list[str]) -> None:
            self._active_tools = names

        def register_command(self, name: str, command: dict) -> None:
            self._commands[name] = command

        def append_entry(self, custom_type: str, data: dict) -> None:
            self._entries.append({"type": custom_type, "data": data})

        def set_session_name(self, name: str) -> None:
            self._session_name = name

        def send_user_message(self, content: str, deliver_as: str = "steer") -> None:
            pass

        def send_message(self, message: dict, options: dict) -> None:
            pass

        def register_flag(self, name: str, options: dict) -> None:
            self._flags[name] = options

        def get_flag(self, name: str):
            return self._flags.get(name)

        def get_handlers(self, event: str) -> list:
            return self._handlers.get(event, [])

        def get_session_name(self) -> str:
            return self._session_name

        def get_entries(self) -> list:
            return self._entries

        def get_active_tools(self) -> list[str]:
            return self._active_tools

    return MockExtensionAPI()
