"""τ-agent-core agent_loop: The core agent loop that drives conversations.

Reference: PHASE-2-SUBPHASE-1.md — Agent Loop.
Reference: SUBPHASE-0.0.md, "5. Agent Events (tau-agent-core)" section.

Implements AgentLoop — the direct port of pi's agent-loop.js logic.
It takes messages + context, calls the LLM via τ-ai, parses assistant
responses for text and tool calls, executes tool calls (sequential or
parallel), feeds results back to the LLM, and repeats until no more
tool calls or termination.

Usage:
    loop = AgentLoop(config=config, emit=emit_event)
    messages = await loop.run(prompts=[user_msg], context=[])
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from tau_ai.abort import AbortSignal
from tau_ai.client import stream_simple
from tau_ai.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_ai.tools import validate_tool_arguments
from tau_ai.types import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    Usage,
)

from tau_agent_core.agent_loop_types import (
    AgentLoopConfig,
    FinalizedToolCall,
    PreparedToolCall,
)
from tau_agent_core.events import AgentEvent
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult


class BlockedCall:
    """A tool call that was blocked (e.g., argument validation failed)."""

    def __init__(self, call: PreparedToolCall, error: str) -> None:
        self.call = call
        self.error = error


class ErrorCall:
    """A tool call that raised an error during preparation."""

    def __init__(self, call: PreparedToolCall, error: str) -> None:
        self.call = call
        self.error = error


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class AgentLoop:
    """The core agent loop.

    Drives conversations, executes tools, and emits events.

    Reference: PHASE-2-SUBPHASE-1.md, "Implementation Outline" section.
    Reference: SUBPHASE-0.0.md, "5. Agent Events" section.

    Attributes:
        config: Agent loop configuration.
        emit: Callback to emit AgentEvents.
        _turn_index: Current turn counter.
        _tools: Mapping of tool names to AgentTool instances.
    """

    def __init__(
        self,
        config: AgentLoopConfig,
        emit: Callable[[AgentEvent], Awaitable[None]] | None = None,
        tools: list[AgentTool] | None = None,
        model: Any = None,
        abort_signal: AbortSignal | None = None,
    ) -> None:
        self.config = config
        self._emit = emit or (lambda e: asyncio.create_task(self._noop_emit(e)))
        self._turn_index = 0
        self._tools: dict[str, AgentTool] = {}
        for t in (tools or []):
            self._tools[t.name] = t
        self._model = model
        self._abort_signal: AbortSignal | None = abort_signal

    @staticmethod
    async def _noop_emit(event: AgentEvent) -> None:
        """No-op emit for when no emit callback is provided."""
        pass

    def add_tool(self, tool: AgentTool) -> None:
        """Add a tool to the agent loop.

        Args:
            tool: The AgentTool to register.
        """
        self._tools[tool.name] = tool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        prompts: list[UserMessage],
        context: list[Any] | None = None,
    ) -> list[Any]:
        """Run the full agent loop for one or more prompts.

        This is the main entry point. It:
        1. Emits agent_start
        2. Adds prompt messages to context
        3. Loops: call LLM, execute tool calls, repeat until done
        4. Emits agent_end with final messages

        Args:
            prompts: List of user messages to start with.
            context: Existing message history.

        Returns:
            List of messages produced by the agent loop.
        """
        context = list(context) if context else []
        messages = list(context)
        for prompt in prompts:
            # Check if context already ends with a user message
            # matching this prompt — if so, skip the duplicate
            if messages:
                last = messages[-1]
                # Handle both dict and Pydantic UserMessage objects
                if hasattr(last, "role"):
                    last_role = last.role
                elif isinstance(last, dict):
                    last_role = last.get("role", "")
                else:
                    last_role = ""

                if last_role == "user":
                    prev_content = messages[-1].content if hasattr(messages[-1], "content") else messages[-1].get("content", "")
                    if isinstance(prev_content, str):
                        prev_text = prev_content
                    elif isinstance(prev_content, list):
                        prev_text = " ".join(
                            (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                            for b in prev_content
                            if (isinstance(b, dict) and b.get("type") == "text") or (hasattr(b, "text"))
                        )
                    else:
                        prev_text = ""

                prompt_content = prompt.content
                if isinstance(prompt_content, str):
                    prompt_text = prompt_content
                elif isinstance(prompt_content, list):
                    prompt_text = " ".join(
                        (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                        for b in prompt_content
                        if (isinstance(b, dict) and b.get("type") == "text") or (hasattr(b, "text"))
                    )
                else:
                    prompt_text = ""

                if prev_text.strip() == prompt_text.strip():
                    continue  # already in context, skip
            messages.append(prompt)

        await self._emit(
            AgentEvent(type="agent_start", timestamp=int(time.time() * 1000))
        )

        turn_index = 0
        final_messages: list[Any] = []
        terminated = False

        while turn_index < self.config.max_turns:
            if self._abort_signal and self._abort_signal.is_aborted():
                break

            await self._emit(
                AgentEvent(
                    type="turn_start",
                    timestamp=int(time.time() * 1000),
                    turn_index=turn_index,
                )
            )

            # Stream response from LLM
            assistant = await self._stream_response(messages)
            final_messages.append(assistant)

            tool_calls = assistant.get_tool_calls()

            if not tool_calls:
                # Text-only response — turn ends
                await self._emit(
                    AgentEvent(
                        type="turn_end",
                        timestamp=int(time.time() * 1000),
                        turn_index=turn_index,
                        tool_results=[],
                    )
                )
                turn_index += 1
                break

            # Emit message_end for the assistant's text/tool call response
            msg_content = [
                c.model_dump() if hasattr(c, "model_dump") else c
                for c in assistant.content
            ]
            await self._emit(
                AgentEvent(
                    type="message_end",
                    timestamp=int(time.time() * 1000),
                    message={
                        "role": "assistant",
                        "content": msg_content,
                    },
                )
            )

            # Execute tool calls
            batch = await self._execute_tool_calls(assistant, tool_calls)

            # Add tool results to messages
            for msg in batch.messages:
                messages.append(msg)
                final_messages.append(msg)

            # Emit turn_end with tool results
            tool_result_dicts = []
            for tr in batch.tool_results:
                tool_result_dicts.append(
                    {
                        "tool_call_id": tr.tool_call_id,
                        "tool_name": tr.tool_name,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                )
            await self._emit(
                AgentEvent(
                    type="turn_end",
                    timestamp=int(time.time() * 1000),
                    turn_index=turn_index,
                    tool_results=tool_result_dicts,
                )
            )

            if batch.terminate:
                terminated = True
                break

            turn_index += 1

        await self._emit(
            AgentEvent(
                type="agent_end",
                timestamp=int(time.time() * 1000),
                messages=[
                    m.model_dump() if hasattr(m, "model_dump") else m
                    for m in final_messages
                ],
            )
        )

        return final_messages

    async def run_continue(
        self,
        context: list[Any] | None = None,
    ) -> list[Any]:
        """Run another agent turn without adding new messages.

        Similar to run() but does not add new prompts.
        Used for follow-up turns.

        Args:
            context: Existing message history.

        Returns:
            List of messages produced.
        """
        context = list(context) if context else []
        messages = list(context)
        turn_index = self._turn_index
        final_messages: list[Any] = []
        terminated = False

        await self._emit(
            AgentEvent(type="agent_start", timestamp=int(time.time() * 1000))
        )

        while turn_index < self.config.max_turns:
            if self._abort_signal and self._abort_signal.is_aborted():
                break

            await self._emit(
                AgentEvent(
                    type="turn_start",
                    timestamp=int(time.time() * 1000),
                    turn_index=turn_index,
                )
            )

            assistant = await self._stream_response(messages)
            final_messages.append(assistant)

            tool_calls = assistant.get_tool_calls()
            if not tool_calls:
                await self._emit(
                    AgentEvent(
                        type="turn_end",
                        timestamp=int(time.time() * 1000),
                        turn_index=turn_index,
                        tool_results=[],
                    )
                )
                turn_index += 1
                break

            await self._emit(
                AgentEvent(
                    type="message_end",
                    timestamp=int(time.time() * 1000),
                    message={
                        "role": "assistant",
                        "content": [
                            c.model_dump() if hasattr(c, "model_dump") else c
                            for c in assistant.content
                        ],
                    },
                )
            )

            batch = await self._execute_tool_calls(assistant, tool_calls)

            for msg in batch.messages:
                messages.append(msg)
                final_messages.append(msg)

            tool_result_dicts = []
            for tr in batch.tool_results:
                tool_result_dicts.append(
                    {
                        "tool_call_id": tr.tool_call_id,
                        "tool_name": tr.tool_name,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                )
            await self._emit(
                AgentEvent(
                    type="turn_end",
                    timestamp=int(time.time() * 1000),
                    turn_index=turn_index,
                    tool_results=tool_result_dicts,
                )
            )

            if batch.terminate:
                terminated = True
                break

            turn_index += 1

        await self._emit(
            AgentEvent(
                type="agent_end",
                timestamp=int(time.time() * 1000),
                messages=[
                    m.model_dump() if hasattr(m, "model_dump") else m
                    for m in final_messages
                ],
            )
        )

        return final_messages

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    async def _stream_response(
        self, context: list[Any]
    ) -> AssistantMessage:
        """Stream assistant response from LLM.

        1. Convert context to LLM format
        2. Call stream_simple()
        3. Process events -> emit AgentEvents
        4. Return final AssistantMessage

        Args:
            context: List of messages to send to the LLM.

        Returns:
            The final AssistantMessage.
        """
        # Prepend system prompt as a system message if present.
        # Only add it if the context doesn't already start with a system message
        # (which it may have from the backend's conversation history).
        messages = list(context)
        system_prompt = self.config.system_prompt
        if system_prompt:
            # Check if context already starts with a system message
            _first_role = messages[0].get("role", "") if isinstance(messages[0], dict) else (
                getattr(messages[0], "role", "")
            )
            if _first_role != "system":
                messages.insert(0, {"role": "system", "content": system_prompt})

        context_dict = {
            "messages": messages,
            "tools": list(self._tools.values()) if self._tools else None,
        }

        model = self._model or self.config.model

        # Forward the API key to the provider via options. client.py reads
        # options["api_key"] to construct the provider, which then strips it from
        # the request body. Only included when set, so None means "rely on the
        # env/provider default" rather than sending an empty override.
        options: dict[str, Any] = {"temperature": self.config.temperature}
        if self.config.api_key:
            options["api_key"] = self.config.api_key

        stream = await stream_simple(
            model,
            context_dict,
            options,
        )

        partial_text = ""
        partial_reasoning = ""
        partial_content_blocks: list[dict[str, Any]] = []

        async for event in stream:
            if isinstance(event, TextDeltaEvent):
                partial_text += event.delta
                partial_content_blocks = [{"type": "text", "text": partial_text}]
                await self._emit(
                    AgentEvent(
                        type="message_start",
                        timestamp=int(time.time() * 1000),
                        message={"role": "assistant", "content": partial_content_blocks},
                    )
                )
                await self._emit(
                    AgentEvent(
                        type="message_update",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": [{"type": "text", "text": partial_text}],
                        },
                    )
                )
            elif isinstance(event, ThinkingDeltaEvent):
                # Reasoning streams on its own channel. Mirror the text path:
                # accumulate and re-emit the full reasoning as a single thinking
                # block so the backend can suffix-diff it exactly like text. Kept
                # distinct from the answer text so the UI can render and collapse
                # it separately.
                partial_reasoning += event.delta
                await self._emit(
                    AgentEvent(
                        type="message_update",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": partial_reasoning}
                            ],
                        },
                    )
                )
            elif isinstance(event, ToolCallDeltaEvent):
                # The provider owns tool-call accumulation; consume its
                # already-accumulated partial message rather than re-parsing the
                # raw per-chunk delta (which is only a fragment).
                partial = event.partial
                if partial is not None:
                    partial_content_blocks = [
                        c.model_dump() if hasattr(c, "model_dump") else c
                        for c in partial.content
                    ]

                await self._emit(
                    AgentEvent(
                        type="message_update",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": partial_content_blocks,
                        },
                    )
                )
            elif isinstance(event, DoneEvent):
                final_msg = event.final
                await self._emit(
                    AgentEvent(
                        type="message_end",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": [
                                c.model_dump() if hasattr(c, "model_dump") else c
                                for c in final_msg.content
                            ],
                            # Real token usage for THIS completion. Attached to the
                            # per-completion message_end (emitted exactly once here,
                            # in _stream_response) rather than the duplicate
                            # message_end run() emits for tool-bearing turns — so a
                            # consumer can sum usage across turns without double-
                            # counting. The provider fills final_msg.usage from the
                            # stream's terminal usage chunk (Fail-Early: a real 0 is
                            # surfaced as 0, never approximated).
                            "usage": final_msg.usage.model_dump(),
                        },
                    )
                )
                return final_msg
            elif isinstance(event, ErrorEvent):
                error_msg = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {event.message}"}],
                }
                await self._emit(
                    AgentEvent(
                        type="message_start",
                        timestamp=int(time.time() * 1000),
                        message=error_msg,
                    )
                )
                await self._emit(
                    AgentEvent(
                        type="message_end",
                        timestamp=int(time.time() * 1000),
                        message=error_msg,
                    )
                )
                raise RuntimeError(event.message)

        # Stream completed without DoneEvent
        content_blocks = (
            [TextContent(text=partial_text)] if partial_text else []
        )
        model_id = model if isinstance(model, str) else "unknown"
        return AssistantMessage(
            content=content_blocks,
            api="openai-completions",
            provider="openai",
            model=model_id if isinstance(model_id, str) else getattr(model, "id", "unknown"),
            usage=Usage(),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

    async def _execute_tool_calls(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls (sequential or parallel).

        Args:
            assistant: The assistant message containing tool calls.
            tool_calls: List of ToolCall objects.

        Returns:
            ToolBatchResult with tool result messages.
        """
        if self.config.tool_execution_mode == "parallel":
            return await self._execute_parallel(assistant, tool_calls)
        else:
            return await self._execute_sequential(assistant, tool_calls)

    async def _execute_sequential(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls one at a time.

        Stops if any tool returns terminate=True.

        Args:
            assistant: The assistant message containing tool calls.
            tool_calls: List of ToolCall objects.

        Returns:
            ToolBatchResult with tool result messages.
        """
        all_results: list[AgentToolResult] = []
        terminated = False

        for tc in tool_calls:
            if terminated:
                break
            if self._abort_signal and self._abort_signal.is_aborted():
                break

            prepared = await self._prepare_tool_call(tc)
            if isinstance(prepared, BlockedCall):
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=prepared.call.id,
                        tool_name=prepared.call.name,
                        result=prepared.error,
                        is_error=True,
                    )
                )
                all_results.append(
                    AgentToolResult.from_error(
                        prepared.call.name,
                        prepared.error,
                        prepared.call.id,
                    )
                )
                continue
            elif isinstance(prepared, ErrorCall):
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=prepared.call.id,
                        tool_name=prepared.call.name,
                        result=prepared.error,
                        is_error=True,
                    )
                )
                all_results.append(
                    AgentToolResult.from_error(
                        prepared.call.name,
                        prepared.error,
                        prepared.call.id,
                    )
                )
                continue

            await self._emit(
                AgentEvent(
                    type="tool_execution_start",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=prepared.id,
                    tool_name=prepared.name,
                    args=prepared.arguments,
                )
            )

            result = await self._execute_tool(prepared)
            result = await self._apply_after_hooks(result)

            await self._emit(
                AgentEvent(
                    type="tool_execution_end",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    result=result.content,
                    is_error=result.is_error,
                )
            )

            all_results.append(result)
            if result.terminate:
                terminated = True

        return self._build_batch_result(all_results)

    async def _execute_parallel(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls concurrently.

        Args:
            assistant: The assistant message containing tool calls.
            tool_calls: List of ToolCall objects.

        Returns:
            ToolBatchResult with tool result messages.
        """
        prepared_calls = []
        for tc in tool_calls:
            prepared = await self._prepare_tool_call(tc)
            prepared_calls.append(prepared)

        # Emit start events for all (PreparedToolCalls only)
        for pc in prepared_calls:
            if isinstance(pc, PreparedToolCall):
                await self._emit(
                    AgentEvent(
                        type="tool_execution_start",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=pc.id,
                        tool_name=pc.name,
                        args=pc.arguments,
                    )
                )

        # Execute all in parallel
        async def _run_tool(pc):
            if isinstance(pc, (BlockedCall, ErrorCall)):
                return AgentToolResult.from_error(
                    pc.call.name, pc.error, pc.call.id
                )
            # pc is a PreparedToolCall
            result = await self._execute_tool(pc)
            result = await self._apply_after_hooks(result)
            return result

        tasks = [_run_tool(pc) for pc in prepared_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_results: list[AgentToolResult] = []
        for i, res in enumerate(results):
            pc = prepared_calls[i]
            if isinstance(res, Exception):
                # Task raised an exception
                error_result = AgentToolResult(
                    tool_name=pc.name if isinstance(pc, PreparedToolCall) else pc.call.name,
                    tool_call_id=pc.id if isinstance(pc, PreparedToolCall) else pc.call.id,
                    content=[{"type": "text", "text": str(res)}],
                    is_error=True,
                    error_message=str(res),
                )
                all_results.append(error_result)
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=error_result.tool_call_id,
                        tool_name=error_result.tool_name,
                        result=str(res),
                        is_error=True,
                    )
                )
            else:
                # Normal result (including from_error for BlockedCall/ ErrorCall)
                all_results.append(res)
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=res.tool_call_id,
                        tool_name=res.tool_name,
                        result=res.content,
                        is_error=res.is_error,
                    )
                )

        terminated = any(getattr(r, "terminate", False) for r in all_results)
        return self._build_batch_result(all_results, terminate=terminated)

    def _build_batch_result(
        self,
        results: list[AgentToolResult],
        terminate: bool = False,
    ) -> ToolBatchResult:
        """Build a ToolBatchResult from individual results.

        Args:
            results: List of AgentToolResult instances.
            terminate: Whether the batch should signal termination.

        Returns:
            ToolBatchResult with messages and metadata.
        """
        result_messages = []
        for r in results:
            content_list = (
                r.content
                if isinstance(r.content, list)
                else [{"type": "text", "text": str(r.content)}]
            )
            result_messages.append(
                ToolResultMessage(
                    role="toolResult",
                    tool_call_id=r.tool_call_id or "",
                    tool_name=r.tool_name,
                    content=content_list,
                    is_error=r.is_error,
                    timestamp=int(time.time() * 1000),
                )
            )
        return ToolBatchResult(
            messages=[m.model_dump() for m in result_messages],
            tool_results=results,
            terminate=terminate,
        )

    async def _prepare_tool_call(
        self, tool_call: ToolCall
    ) -> PreparedToolCall | BlockedCall | ErrorCall:
        """Prepare a tool call: validate args, run before hooks.

        Args:
            tool_call: ToolCall from the LLM response.

        Returns:
            PreparedToolCall if ready, BlockedCall if validation failed,
            or ErrorCall if an error occurred during preparation.
        """
        try:
            call_name = tool_call.name
            call_args = tool_call.arguments

            if call_name in self._tools:
                tool = self._tools[call_name]
                validate_tool_arguments(tool, call_args)

            return PreparedToolCall(
                id=tool_call.id,
                name=call_name,
                arguments=call_args if isinstance(call_args, dict) else {},
            )
        except ValueError as e:
            return BlockedCall(
                call=PreparedToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments={},
                ),
                error=str(e),
            )
        except Exception as e:
            return ErrorCall(
                call=PreparedToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments={},
                ),
                error=str(e),
            )

    async def _execute_tool(self, call: PreparedToolCall) -> AgentToolResult:
        """Execute a single tool with error handling.

        Args:
            call: The PreparedToolCall to execute.

        Returns:
            AgentToolResult with the tool's result.
        """
        try:
            tool = self._tools.get(call.name)
            if tool is None:
                return AgentToolResult.from_error(
                    call.name,
                    f"Unknown tool: {call.name}",
                    call.id,
                )

            result = await tool.execute(
                tool_call_id=call.id,
                args=call.arguments,
                signal=self._abort_signal,
            )

            # If the tool returned an AgentToolResult, preserve its terminate flag
            if isinstance(result, AgentToolResult):
                result.tool_name = call.name
                result.tool_call_id = call.id
                return result

            # Otherwise wrap the raw result (dict from tool.model_dump(), etc.)
            if isinstance(result, dict):
                # Extract content from the result dict
                content = result.get("content", "")
                is_error = result.get("is_error", False)
                content_list = (
                    content
                    if isinstance(content, list)
                    else [{"type": "text", "text": str(content)}]
                )
                return AgentToolResult(
                    tool_name=call.name,
                    tool_call_id=call.id,
                    content=content_list,
                    is_error=is_error,
                    terminate=result.get("terminate", False),
                )
            else:
                content_list = (
                    result
                    if isinstance(result, list)
                    else [{"type": "text", "text": str(result)}]
                )
                return AgentToolResult(
                    tool_name=call.name,
                    tool_call_id=call.id,
                    content=content_list,
                    is_error=False,
                )
        except Exception as e:
            return AgentToolResult.from_error(call.name, str(e), call.id)

    async def _apply_after_hooks(self, result: AgentToolResult) -> AgentToolResult:
        """Apply after-tool-call hooks (extensions).

        Currently a no-op — extensions will use this in Phase 3.

        Args:
            result: The tool execution result.

        Returns:
            The (possibly modified) result.
        """
        return result

    def _to_llm_tool(self, tool: AgentTool) -> dict:
        """Convert AgentTool to LLM tool format.

        Args:
            tool: The AgentTool to convert.

        Returns:
            OpenAI-format tool dict.
        """
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.definition.description,
                "parameters": tool.definition.parameters,
            },
        }
