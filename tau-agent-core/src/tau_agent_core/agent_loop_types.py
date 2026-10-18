"""τ-agent-core agent_loop_types: Types for the agent loop execution pipeline.

Reference: docs/tau-agent-core.md, "Agent Loop Types" section.

Types:
- PreparedToolCall: A tool call prepared for execution (from LLM response)
- FinalizedToolCall: A tool call after execution has completed
- AgentLoopConfig: Configuration for the agent loop
- TurnSettings: What one request is sent with, read per request

These types bridge τ-llm (Phase 1) and the agent loop (Phase 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field
from tau_llm.docs import agent_facing


@agent_facing(topic="agent-loop")
class PreparedToolCall(BaseModel):
    """A tool call prepared for execution, extracted from LLM response.

    Created when the agent loop receives a tool call from the model.
    Before execution, the tool call is validated and wrapped in this type.

    Attributes:
        id: Unique tool call ID (from model response)
        name: Name of the tool to execute
        arguments: Validated arguments dict (parsed from model output)
    """

    id: str
    name: str
    arguments: dict[str, Any]


@agent_facing(topic="agent-loop")
class FinalizedToolCall(BaseModel):
    """A tool call after execution has completed.

    Created when the agent loop finishes executing a PreparedToolCall.

    Attributes:
        id: The tool call ID
        name: Name of the tool that was executed
        arguments: The arguments that were passed
        result: The execution result (from the tool)
        is_error: Whether the execution failed
    """

    id: str
    name: str
    arguments: dict[str, Any]
    result: Any | None = None
    is_error: bool = False


@agent_facing(topic="agent-loop")
class AgentLoopConfig(BaseModel):
    """Configuration for the agent loop.

    Attributes:
        model: Model identifier (e.g., "gpt-4o")
        system_prompt: System prompt for the agent, sent only when the context
            does not already open with a system message
        system_prompt_override: This run's prompt from a ``before_agent_start``
            handler; replaces the context's leading system message, or opens
            the context when there is none
        tool_execution_mode: How tools are executed
        max_retries: Maximum retry attempts for failed tool calls
        max_turns: Turn ceiling, or ``None`` (the default) for no ceiling
        repeat_tool_call_limit: How many turns in a row may repeat the same
            wholly-failing batch of tool calls before the loop stops itself.
            Default 3; ``None`` disables the check.
        temperature: Sampling temperature, or ``None`` (the default) to send none
        api_key: API key forwarded to the provider (None = use env/provider default)
        reasoning: Requested thinking level ("off".."xhigh"), or None to send no
            reasoning control (docs/REASONING-WIRE.md §4)

    ``temperature``, ``api_key`` and ``reasoning`` are ignored by a loop given a
    ``turn_settings`` source, which supplies them per request instead.
    """

    model: str | None = None
    system_prompt: str | None = None
    system_prompt_override: str | None = None
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel"
    max_retries: int = Field(default=3, ge=0)
    max_turns: int | None = Field(default=None, ge=1)
    repeat_tool_call_limit: int | None = Field(default=3, ge=2)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    api_key: str | None = None
    reasoning: str | None = None


@agent_facing(topic="agent-loop")
@dataclass(frozen=True)
class TurnSettings:
    """What one request is sent with, read immediately before it (docs/TURN-SETTINGS.md §2).

    Attributes:
        model: The ``Model`` the request calls.
        api_key: The key it is called with; ``None`` for the provider's default.
        reasoning: The requested thinking level, or ``None`` to send none.
        temperature: The sampling temperature, or ``None`` to send none.
    """

    model: Any
    api_key: str | None = None
    reasoning: str | None = None
    temperature: float | None = None


TurnSettingsSource = Callable[[], Awaitable[TurnSettings]]
"""Awaited by :class:`~tau_agent_core.agent_loop.AgentLoop` before every request.

May write to the tree (the session records a changed config there), so the loop
calls it only while no message entry is open.
"""
