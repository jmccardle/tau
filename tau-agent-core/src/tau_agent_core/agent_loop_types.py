"""τ-agent-core agent_loop_types: Types for the agent loop execution pipeline.

Reference: docs/tau-agent-core.md, "Agent Loop Types" section.

Types:
- PreparedToolCall: A tool call prepared for execution (from LLM response)
- FinalizedToolCall: A tool call after execution has completed
- AgentLoopConfig: Configuration for the agent loop

These types bridge τ-ai (Phase 1) and the agent loop (Phase 2.1).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class AgentLoopConfig(BaseModel):
    """Configuration for the agent loop.

    Attributes:
        model: Model identifier (e.g., "gpt-4o")
        system_prompt: System prompt for the agent
        tool_execution_mode: How tools are executed
        max_retries: Maximum retry attempts for failed tool calls
        max_turns: Maximum number of turns before termination
        temperature: Model temperature
        api_key: API key forwarded to the provider (None = use env/provider default)
        reasoning: Requested thinking level ("off".."xhigh"), or None
    """

    model: str | None = None
    system_prompt: str | None = None
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel"
    max_retries: int = Field(default=3, ge=0)
    max_turns: int = Field(default=50, ge=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # Forwarded to the provider via stream_simple's options. Kept out of the
    # Model (which is serialized to session JSON on disk) so the credential is
    # never persisted.
    api_key: str | None = None
    # Requested thinking/reasoning level ("off".."xhigh"), forwarded to the
    # provider as the `reasoning` option (→ `reasoning_effort`). None means
    # "don't request reasoning". The provider clamps it to the model's
    # capabilities and only sends it when the model declares reasoning support.
    reasoning: str | None = None
