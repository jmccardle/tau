"""τ-agent-core agent_loop_types: Types for the agent loop execution pipeline.

Reference: docs/tau-agent-core.md, "Agent Loop Types" section.

Types:
- PreparedToolCall: A tool call prepared for execution (from LLM response)
- FinalizedToolCall: A tool call after execution has completed
- AgentLoopConfig: Configuration for the agent loop

These types bridge τ-llm (Phase 1) and the agent loop (Phase 2.1).
"""

from __future__ import annotations

from typing import Any, Literal

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
        system_prompt: System prompt for the agent
        tool_execution_mode: How tools are executed
        max_retries: Maximum retry attempts for failed tool calls
        max_turns: Turn ceiling, or ``None`` (the default) for no ceiling
        repeat_tool_call_limit: How many turns in a row may repeat the same
            wholly-failing batch of tool calls before the loop stops itself.
            Default 3; ``None`` disables the check.
        temperature: Sampling temperature, or ``None`` (the default) to send none
        api_key: API key forwarded to the provider (None = use env/provider default)
        reasoning: Requested thinking level ("off".."xhigh"), or None
    """

    model: str | None = None
    system_prompt: str | None = None
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel"
    max_retries: int = Field(default=3, ge=0)
    # No ceiling by default. This used to be 50, and 50 was a number nobody could
    # change: `create_agent_session` has no such parameter, no CLI flag set it, and
    # no config key was read, so every TUI and every `tau -p` run stopped at turn
    # 50 whether or not the work was done -- and stopped SILENTLY (see
    # AgentLoop._emit_agent_end). A cap the operator cannot see, cannot raise, and
    # is not told about is not a safeguard.
    #
    # pi has no turn bound at all (`agent-loop.ts:155-275` exits on error, on
    # no-more-tool-calls, or via a host-supplied `shouldStopAfterTurn`), so `None`
    # is also the parity position. What bounds a runaway run instead is the
    # machinery built for it: an extension's budget guard tripping the abort
    # signal (`max_usd`/`max_seconds`, docs/EXTENSIONS-WALKTHROUGH.md), Escape in
    # the TUI, and the ceiling itself once someone states one.
    #
    # `ge=1` still holds for a stated ceiling: 0 would mean "run no turns", which
    # is a way of spelling "do nothing" that no caller means on purpose.
    max_turns: int | None = Field(default=None, ge=1)
    # The bound that `max_turns=None` left uncovered (docs/PLAN-0.9.3.md §4.2
    # item 2, promoted out of PLAN-0.9.4 §8). The AskSage report showed turns
    # 2-50 being the identical failure, and the old ceiling stopped that run only
    # because 50 happened to arrive. Now nothing does, so the loop has to notice.
    #
    # The rule is deliberately narrow, so it cannot fire on a model that is
    # making progress: the WHOLE batch must repeat -- same tool names, same
    # arguments -- AND every result in both batches must be an error. A model
    # that reads a missing file, writes it, then reads it again has a different
    # batch in between and never trips this. A model that alternates between two
    # failing calls does not trip it either; that is a wider claim ("no progress")
    # than this check makes, and a wider check is a wider way to be wrong.
    #
    # 3, not 2, and the reason is an extension. A `tool_result` hook that reacts
    # to repeated failures — examples/reminders' "root-cause-after-2-failures" is
    # the one in this tree — can only fire ON the second failure, and its whole
    # purpose is to change what the model sees before the third attempt. A limit
    # of 2 ends the run in the same turn that guidance was appended, so the
    # guidance is written and never read. 3 leaves exactly one turn for an
    # intervention to have an effect, and still stops a genuine runaway at three
    # identical failing batches rather than at fifty or at never.
    #
    # `None` disables the check for a caller who wants pi's exact behaviour, and
    # `ge=2` holds because a limit of 1 would stop on a batch that has not
    # repeated anything yet.
    repeat_tool_call_limit: int | None = Field(default=3, ge=2)
    # `None` means "send no temperature and let the endpoint apply its own".
    #
    # This used to default to 0.7, and 0.7 was sent on EVERY request. It came
    # from nowhere an operator could reach: `Model` had no temperature field, so
    # `agent_session`'s `getattr(self._model, "temperature", 0.7)` always fell
    # through to the literal, and a `temperature` written into ~/.tau/config.json
    # was dropped by pydantic without a word. It was also fatal on the Anthropic
    # wire, where the SDK removed the parameter from `messages.stream()`
    # entirely — every anthropic-messages call raised TypeError before reaching
    # the network.
    #
    # `None` is also the pi position: `simple-options.ts:32` forwards
    # `options?.temperature`, which is undefined unless a caller sets one, and
    # pi's agent never sets one. Set it per-model in ~/.tau/config.json
    # (`models.<name>.temperature`, now a real `Model` field) or per-run here.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # Forwarded to the provider via stream_simple's options. Kept out of the
    # Model (which is serialized to session JSON on disk) so the credential is
    # never persisted.
    api_key: str | None = None
    # Requested thinking/reasoning level ("off".."xhigh"), forwarded to the
    # provider as the `reasoning` option (→ `reasoning_effort`). None means
    # "don't request reasoning". The provider clamps it to the model's
    # capabilities and only sends it when the model declares reasoning support.
    reasoning: str | None = None
