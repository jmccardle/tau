"""
Backend abstraction layer for tau-coding-agent.

Wraps tau-agent-core's AgentSession to provide Parley-compatible
Backend interfaces (chat, stream_chat).
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable
from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.sdk import create_agent_session, _resolve_tools


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

        # Discover tools from config. Defaults to all built-in tools.
        tool_names = config.get("tools", ["read", "write", "edit", "bash", "ls", "grep", "find"])
        if tool_names:
            tools = _resolve_tools(tool_names)
        else:
            tools = []

        self.agent_session = AgentSession(
            session_manager=self.session_manager,
            model=model,
            system_prompt=self.system_prompt,
            tools=tools,
        )

        # Create a new session in the session manager (required before use)
        self.session_manager.new_session()

        # Set the API key if provided
        if api_key and api_key != "not-needed":
            # Store for later use by the LLM provider
            self._api_key = api_key

    async def _extract_last_user_message(self, messages: list[dict]) -> str:
        """Extract the last user message text from a Parley messages list."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    return "\n".join(text_parts)
        return ""

    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        """Send a chat completion via tau-agent-core's AgentSession.

        Passes all messages as context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages).
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
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, []

        # Send through the agent loop with full conversation context
        # so the model sees prior tool calls and results
        result_messages = await self.agent_session.prompt(
            last_user_message, context=messages
        )

        # Extract the last assistant message text
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
        }, result_messages

    async def stream_chat(
        self, messages: list[dict], callback: Callable[[str], None]
    ) -> tuple[str, dict, list[dict]]:
        """Stream a chat completion via tau-agent-core's AgentSession.

        Passes ALL messages as context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages).
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
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, []

        # Capture streaming chunks — track the last accumulated text
        # per assistant message (reset on message_start for multi-turn loops)
        streaming_text: str = ""
        streaming_chunks: list[str] = []

        def capture_event(event) -> None:
            """Capture message_update events and extract delta text.

            This captures text deltas from the LLM response streaming.
            The agent loop emits message_start redundantly (once per
            TextDeltaEvent), so we don't reset on it. Instead we check
            whether full_text is a prefix extension of streaming_text;
            if not, it's a new message (multi-turn) so we treat the
            entire content as the delta.
            Tool execution events are emitted through the same event bus
            but we only forward text content to the TUI callback.
            """
            nonlocal streaming_text
            if not hasattr(event, "type"):
                return
            if event.type == "message_start":
                # Agent loop emits message_start once per TextDeltaEvent,
                # not just once per message. Ignore it; we track state
                # via the message_update content prefix check.
                pass
            elif event.type == "message_update":
                message = getattr(event, "message", None)
                if message:
                    content = message.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                full_text = block.get("text", "")
                                if not full_text:
                                    continue
                                # Check if full_text continues from where we left off.
                                # If streaming_text is empty or full_text doesn't
                                # start with it, this is a new message (multi-turn)
                                # so treat the entire full_text as the delta.
                                if not streaming_text or full_text.startswith(streaming_text):
                                    delta = full_text[len(streaming_text):]
                                else:
                                    delta = full_text
                                if not delta:
                                    continue  # no actual change
                                streaming_text = full_text
                                streaming_chunks.append(delta)
                                callback(delta)
            elif event.type == "message_end":
                # New turn in a multi-turn loop — reset for the next message.
                streaming_text = ""

        # Subscribe to events before running the prompt
        # This captures ALL events during the full agent loop (LLM calls + tool execution)
        unsubscribe = self.agent_session.subscribe(capture_event)

        # Send through the agent loop with full conversation context
        # The loop handles LLM calls, tool execution, and re-calls the LLM
        # for tool results — all streaming flows through the event bus
        new_messages = await self.agent_session.prompt(
            last_user_message, context=messages
        )

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
        }, new_messages


def create_backend(config: dict[str, Any]) -> Backend:
    """Factory function to create a tau-agent-core backend."""
    return TauBackend(config)
