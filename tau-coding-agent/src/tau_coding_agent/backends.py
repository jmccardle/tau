"""
Backend abstraction layer for tau-coding-agent.

Wraps tau-agent-core's AgentSession to provide Parley-compatible
Backend interfaces (chat, stream_chat).
"""

from abc import ABC, abstractmethod
from typing import Any, Callable
from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.sdk import create_agent_session


class Backend(ABC):
    """Abstract base class for LLM backends."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = config.get("model", "")

    @abstractmethod
    async def chat(self, messages: list[dict]) -> tuple[str, dict]:
        pass

    @abstractmethod
    async def stream_chat(
        self, messages: list[dict], callback: Callable[[str], None]
    ) -> tuple[str, dict]:
        pass


class TauBackend(Backend):
    """tau-agent-core backend adapter.

    Wraps tau-agent-core's AgentSession to provide Parley-compatible
    chat/stream_chat interfaces.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        # Build a tau-agent-core model config from the Parley config
        model_id = config.get("model", "gpt-4")
        base_url = config.get("base_url", "https://api.openai.com/v1")
        api_key = config.get("api_key", "not-needed")
        backend_type = config.get("backend", "openai").lower()

        # Map provider name (Parley's "backend" field) to tau-agent-core provider
        provider_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "gemini": "gemini",
        }
        provider = provider_map.get(backend_type, backend_type)

        self.model_name = model_id
        self.system_prompt = config.get("system_prompt", "")

        # Build the AgentSession
        self.session_manager = SessionManager()
        model = Model(
            id=model_id,
            name=model_id,
            api="openai-completions",
            provider=provider,
            base_url=base_url,
            context_window=128000,
            max_tokens=4096,
        )

        self.agent_session = AgentSession(
            session_manager=self.session_manager,
            model=model,
            system_prompt=self.system_prompt,
        )

        # Create a new session in the session manager (required before use)
        self.session_manager.new_session()

        # Set the API key if provided
        if api_key and api_key != "not-needed":
            # Store for later use by the LLM provider
            self._api_key = api_key

    async def chat(self, messages: list[dict]) -> tuple[str, dict]:
        """Send a chat completion via tau-agent-core's AgentSession.

        The messages are in OpenAI format (role/content dicts).
        We extract the last user message and send it through the agent loop.
        """
        # Extract the last user message text
        last_user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_message = content
                elif isinstance(content, list):
                    # Multi-modal: extract text blocks
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    last_user_message = "\n".join(text_parts)
                break

        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}

        # Send through the agent loop
        result_messages = await self.agent_session.prompt(last_user_message)

        # Extract the last assistant message content
        assistant_content = ""
        for msg in reversed(result_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    assistant_content = "\n".join(text_parts)
                elif isinstance(content, str):
                    assistant_content = content
                break

        # Approximate token count
        prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
        completion_tokens = len(assistant_content) // 4 if assistant_content else 0

        return assistant_content, {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    async def stream_chat(
        self, messages: list[dict], callback: Callable[[str], None]
    ) -> tuple[str, dict]:
        """Stream a chat completion via tau-agent-core's AgentSession.

        Subscribes to message_update events to get streaming text chunks.
        """
        # Extract the last user message text (same as chat())
        last_user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_message = content
                elif isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    last_user_message = "\n".join(text_parts)
                break

        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}

        # Capture streaming chunks
        streaming_chunks: list[str] = []

        def capture_event(event):
            """Capture message_update events for streaming."""
            if hasattr(event, "type") and event.type == "message_update":
                message = getattr(event, "message", None)
                if message:
                    content = message.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                chunk = block.get("text", "")
                                if chunk:
                                    streaming_chunks.append(chunk)
                                    callback(chunk)

        # Subscribe to events before running the prompt
        unsubscribe = self.agent_session.subscribe(capture_event)

        # Send through the agent loop (this will emit events that get captured)
        await self.agent_session.prompt(last_user_message)

        # Unsubscribe
        unsubscribe()

        # Combine all streaming chunks
        full_content = "".join(streaming_chunks)

        # Approximate token count
        prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
        completion_tokens = len(full_content) // 4 if full_content else 0

        return full_content, {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }


def create_backend(config: dict[str, Any]) -> Backend:
    """Factory function to create a tau-agent-core backend."""
    return TauBackend(config)
