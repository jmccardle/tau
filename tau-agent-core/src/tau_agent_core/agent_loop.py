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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

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
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)

from tau_agent_core.agent_loop_types import (
    AgentLoopConfig,
    PreparedToolCall,
)
from tau_agent_core.events import AgentEvent
from tau_agent_core.messages import convert_to_llm, create_custom_message
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult

if TYPE_CHECKING:
    from tau_agent_core.extensions.runner import ExtensionRunner


class BlockedCall:
    """A tool call that was blocked (e.g., argument validation failed).

    ``blocked_by_extension`` names the extension that VETOED the call via a
    ``tool_call`` hook (S50, anchor G11); it is ``None`` for a block that is NOT an
    extension veto (argument-validation failure, fail-closed handler throw) — those
    stay a generic errored result rather than the "⛔ blocked by <ext>" render.
    """

    def __init__(
        self, call: PreparedToolCall, error: str, blocked_by_extension: str | None = None
    ) -> None:
        self.call = call
        self.error = error
        self.blocked_by_extension = blocked_by_extension


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
        emit: Callback to emit AgentEvents (fire-and-forget; returns None).
        _turn_index: Current turn counter.
        _tools: Mapping of tool names to AgentTool instances.
        _hook_dispatcher: The return-collecting extension hook dispatcher
            (an :class:`~tau_agent_core.extensions.runner.ExtensionRunner`),
            injected by :class:`~tau_agent_core.agent_session.AgentSession`.
            Unlike ``emit`` (fire-and-forget), its ``emit_*`` methods return
            results that the mutating-hook call-sites thread forward. ``None``
            when the loop runs standalone (no session / no extensions).
    """

    def __init__(
        self,
        config: AgentLoopConfig,
        emit: Callable[[AgentEvent], Awaitable[None]] | None = None,
        tools: list[AgentTool] | None = None,
        model: Any = None,
        abort_signal: AbortSignal | None = None,
        hook_dispatcher: ExtensionRunner | None = None,
    ) -> None:
        self.config = config
        self._emit = emit or (lambda e: asyncio.create_task(self._noop_emit(e)))
        self._turn_index = 0
        self._tools: dict[str, AgentTool] = {}
        for t in tools or []:
            self._tools[t.name] = t
        self._model = model
        self._abort_signal: AbortSignal | None = abort_signal
        # The mutating-hook dispatcher (E2). Held here so the four hook
        # call-sites (S11-S14: tool_call / tool_result / context, plus
        # before_agent_start above the loop) can reach it. S10 only threads it
        # in; the call-sites gate on has_hook_handlers() for the zero-extension
        # fast path.
        self._hook_dispatcher: ExtensionRunner | None = hook_dispatcher

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

    def has_hook_handlers(self, event: str) -> bool:
        """Whether any extension has a handler for the mutating hook ``event``.

        The zero-extension fast path (pi ``agent-session.ts:407-411``): the four
        hook call-sites (S11-S14) call this before dispatching so a session with
        no extensions — or a standalone loop with no injected dispatcher — does
        no hook work at all. Returns ``False`` when no dispatcher was injected.
        """
        return self._hook_dispatcher is not None and self._hook_dispatcher.has_handlers(event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        prompts: list[Any],
        context: list[Any] | None = None,
    ) -> list[Any]:
        """Run the full agent loop for one or more prompts.

        This is the main entry point. It:
        1. Emits agent_start
        2. Adds prompt messages to context
        3. Loops: call LLM, execute tool calls, repeat until done
        4. Emits agent_end with final messages

        Args:
            prompts: Messages to start with — user messages, and any
                extension-injected ``custom`` message dicts (serialized custom→user
                at the wire by ``_stream_response``).
            context: Existing message history.

        Returns:
            List of messages produced by the agent loop.
        """
        # pi parity (agent-loop.ts:103-106): the loop simply concatenates the
        # prior context with the new prompts — de-duplication is the caller's
        # responsibility. AgentSession.prompt() threads the user message exactly
        # once and never hands us a context that already ends with it. The old
        # strip-compare dedup that lived here was a tau divergence: redundant
        # with the session-layer check, blind to non-text (multimodal) content,
        # and crash-prone (it referenced prev_text, which was only bound when the
        # context tail was itself a user message).
        context = list(context) if context else []
        messages = list(context)
        messages.extend(prompts)

        await self._emit(AgentEvent(type="agent_start", timestamp=int(time.time() * 1000)))

        turn_index = 0
        final_messages: list[Any] = []

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
                # S43 — the MUTATING turn_end hook fires AFTER the notify AgentEvent:
                # a returned message is a durable append. This is the final turn (the
                # loop breaks below), so the node is persisted but the model only sees
                # it on the NEXT prompt() — the same reload-durable path.
                await self._run_mutating_turn_end(
                    turn_index,
                    self._turn_usage(assistant),
                    [self._serialize_message(assistant)],
                    messages,
                    final_messages,
                )
                turn_index += 1
                break

            # Emit message_end for the assistant's text/tool call response
            msg_content = [
                c.model_dump() if hasattr(c, "model_dump") else c for c in assistant.content
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

            # S43 — the MUTATING turn_end hook. A returned message is appended as a
            # durable ``custom`` node to BOTH the running context (so the next turn's
            # model sees it, custom→user on the wire) and ``final_messages`` (so
            # AgentSession persists it as a ``customMessage`` tree node — the single
            # durable artifact: persisted == rendered == sent). Append-only: it never
            # rewrites the assistant/tool nodes above it.
            await self._run_mutating_turn_end(
                turn_index,
                self._turn_usage(assistant),
                [
                    self._serialize_message(assistant),
                    *[self._serialize_message(m) for m in batch.messages],
                ],
                messages,
                final_messages,
            )

            if batch.terminate:
                break

            turn_index += 1

        await self._emit(
            AgentEvent(
                type="agent_end",
                timestamp=int(time.time() * 1000),
                messages=[
                    m.model_dump() if hasattr(m, "model_dump") else m for m in final_messages
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

        await self._emit(AgentEvent(type="agent_start", timestamp=int(time.time() * 1000)))

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
                # S43 — mutating turn_end (see run()); final turn, durable append.
                await self._run_mutating_turn_end(
                    turn_index,
                    self._turn_usage(assistant),
                    [self._serialize_message(assistant)],
                    messages,
                    final_messages,
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

            # S43 — mutating turn_end (see run()); durable append before next turn.
            await self._run_mutating_turn_end(
                turn_index,
                self._turn_usage(assistant),
                [
                    self._serialize_message(assistant),
                    *[self._serialize_message(m) for m in batch.messages],
                ],
                messages,
                final_messages,
            )

            if batch.terminate:
                break

            turn_index += 1

        await self._emit(
            AgentEvent(
                type="agent_end",
                timestamp=int(time.time() * 1000),
                messages=[
                    m.model_dump() if hasattr(m, "model_dump") else m for m in final_messages
                ],
            )
        )

        return final_messages

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_message(message: Any) -> Any:
        """Serialize a loop message (pydantic or dict) to a plain dict for a hook."""
        if hasattr(message, "model_dump"):
            return message.model_dump()
        return message

    @staticmethod
    def _turn_usage(assistant: AssistantMessage) -> dict[str, Any]:
        """This turn's per-completion token usage, as a dict (S43 ``turn_end`` event).

        Reads the real usage the provider filled on the assistant message (the same
        value the per-completion ``message_end`` carries). Fail-Early: a real 0 is
        surfaced as 0, never approximated — the accessor never fabricates a value.
        """
        usage: dict[str, Any] = assistant.usage.model_dump()
        return usage

    async def _run_mutating_turn_end(
        self,
        turn_index: int,
        usage: dict[str, Any] | None,
        turn_messages: list[Any],
        messages: list[Any],
        final_messages: list[Any],
    ) -> None:
        """Fire the mutating ``turn_end`` hook and weave returned messages durably (S43).

        Gated on ``has_handlers`` for the zero-extension fast path. Each message a
        handler returns becomes a durable ``custom`` node
        (:meth:`_turn_end_custom_node`) appended to BOTH the running loop context
        (``messages`` — so the next turn's model sees it) and ``final_messages`` (so
        :class:`~tau_agent_core.agent_session.AgentSession` persists it as a
        ``customMessage`` tree node). Append-only: it never rewrites the
        assistant/tool nodes produced this turn.
        """
        dispatcher = self._hook_dispatcher
        if dispatcher is None or not dispatcher.has_handlers("turn_end"):
            return
        injected = await dispatcher.emit_turn_end(
            turn_index=turn_index,
            usage=usage,
            messages=turn_messages,
        )
        for raw in injected:
            node = self._turn_end_custom_node(raw)
            messages.append(node)
            final_messages.append(node)

    @staticmethod
    def _turn_end_custom_node(message: dict[str, Any]) -> dict[str, Any]:
        """Build a durable ``custom`` node from a mutating ``turn_end`` return (S43).

        A handler's returned ``{customType, content, display?, details?}`` becomes an
        agent-level custom message (``role: "custom"``,
        :func:`~tau_agent_core.messages.create_custom_message`) — the same shape and
        validation as a ``before_agent_start`` message. Threaded into the loop this
        turn AND persisted as a ``customMessage`` tree node by the session, so a
        reload replays the exact path the model saw.

        Raises:
            ValueError: if the message lacks ``content`` (nothing to inject) or
                ``customType`` (extension-origin identity is not fabricated) —
                Fail-Early, no silent default.
        """
        if "content" not in message:
            raise ValueError("turn_end message is missing 'content' — nothing to inject")
        if "customType" not in message:
            raise ValueError(
                "turn_end message is missing 'customType' — the extension-origin type "
                "is required (Fail-Early, no fabricated default)"
            )
        return create_custom_message(
            custom_type=str(message["customType"]),
            content=message["content"],
            display=bool(message.get("display", True)),
            details=message.get("details"),
            timestamp=int(time.time() * 1000),
        )

    async def _stream_response(self, context: list[Any]) -> AssistantMessage:
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
        # E5 §3.2 / S30 — the `context` mutating hook is ELIMINATED (not
        # redefined). Under the durable-hook invariant (§1) the model's input for
        # every LLM call is exactly the system prompt (attached below) + the linear
        # active path — there is no ephemeral per-send transform. What `context`
        # used to do folds into durable nodes: reminders edit the triggering
        # `tool_result` in place (already durable), and pre-first-call injection
        # rides `before_agent_start` (S29). So `context` here is passed straight to
        # `convert_to_llm` with no interception; the on-disk path IS the wire.

        # Serialize agent-level `custom` messages (extension-injected durable
        # nodes, E5 §3.1 / S29) to the LLM-acceptable `user` role BEFORE the
        # provider sees them — pi `convertToLlm` custom→user. The node stays
        # `role: "custom"` in the tree / render; only the wire is remapped. A
        # no-op for the zero-custom-message common case (passes each through).
        messages = convert_to_llm(list(context))
        # Prepend system prompt as a system message if present.
        # Only add it if the context doesn't already start with a system message
        # (which it may have from the backend's conversation history).
        system_prompt = self.config.system_prompt
        if system_prompt:
            # Check if context already starts with a system message
            _first_role = (
                messages[0].get("role", "")
                if isinstance(messages[0], dict)
                else (getattr(messages[0], "role", ""))
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
        # Forward the requested thinking level; the provider clamps it and emits
        # `reasoning_effort`. Only when set, so None = "don't request reasoning".
        if self.config.reasoning is not None:
            options["reasoning"] = self.config.reasoning
        # Forward the abort signal so an abort mid-completion stops the LLM stream
        # cooperatively — not just at the turn boundaries checked in `run`. The
        # provider polls it per SSE line; client.py strips it from the request
        # body. Without this an aborted turn still drains the whole completion.
        if self._abort_signal is not None:
            options["abort_signal"] = self._abort_signal

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
                            "content": [{"type": "thinking", "thinking": partial_reasoning}],
                        },
                    )
                )
            elif isinstance(event, ToolCallDeltaEvent):
                # The provider owns tool-call accumulation; consume its
                # already-accumulated partial message rather than re-parsing the
                # raw per-chunk delta (which is only a fragment).
                partial = event.partial
                if partial is not None:
                    # partial.content holds pydantic blocks (TextContent /
                    # ThinkingContent / ToolCall), each with model_dump().
                    partial_content_blocks = [c.model_dump() for c in partial.content]

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
                            # model + stop_reason ride the SAME per-completion
                            # message_end so the pi-faithful ``--mode json`` serializer
                            # (E-json / step S8) can surface a message_end carrying
                            # usage/model/stop_reason — matching pi, where the full
                            # assistant message is emitted on message_end
                            # (agent-session.ts:639-644). Additive: existing consumers
                            # read ``.get("usage")``/content and ignore these keys.
                            "model": final_msg.model,
                            "stop_reason": final_msg.stop_reason,
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
        content_blocks: list[TextContent | ThinkingContent | ToolCall] = (
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

    def _emit_veto_record(self, tool_name: str, reason: str, extension: str | None) -> None:
        """Emit the JSON-stream veto record for an extension-blocked call (S50).

        Routed through the hook dispatcher (which owns the shared ``ExtensionUI``
        record sink). A no-op when the loop runs standalone (no dispatcher) or off the
        ``--mode json`` path (no sink) — the veto still surfaces on the
        ``tool_execution_end`` AgentEvent's ``blocked`` field there.
        """
        dispatcher = self._hook_dispatcher
        if dispatcher is None:
            return
        dispatcher.emit_veto_record(tool_name=tool_name, reason=reason, extension=extension)

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

            # Emit the start for EVERY call up front (pi agent-loop.ts:406-413) —
            # BEFORE prepareToolCall — so a call vetoed by a `tool_call` hook (or
            # blocked by arg validation) still surfaces a RENDERED node. A veto
            # emits only tool_execution_end(is_error=True); without a preceding
            # start the front-end has no widget to fold the blocked result into and
            # silently drops it (backends.py `_on_tool_result` → "no ToolBox").
            # The blocked node is already on the active path (its toolResult is
            # appended below); this only makes it visible (E5 §4 / S33).
            await self._emit(
                AgentEvent(
                    type="tool_execution_start",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    args=tc.arguments if isinstance(tc.arguments, dict) else {},
                )
            )

            prepared = await self._prepare_tool_call(tc)
            if isinstance(prepared, BlockedCall):
                # An extension VETO (S50, anchor G11) is a distinct presentation from
                # a generic error: mark the end event ``blocked`` + emit the JSON veto
                # record. A non-veto block (arg validation) has no attribution and
                # stays a plain errored result.
                blocked_by = prepared.blocked_by_extension
                if blocked_by is not None:
                    self._emit_veto_record(prepared.call.name, prepared.error, blocked_by)
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=prepared.call.id,
                        tool_name=prepared.call.name,
                        result=prepared.error,
                        is_error=True,
                        blocked=blocked_by is not None,
                        blocked_by=blocked_by,
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

            result = await self._execute_tool(prepared)
            result = await self._apply_after_hooks(result, prepared.arguments)

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

        # Emit start events for EVERY call (pi agent-loop.ts:459-466) — including
        # ones a `tool_call` hook vetoed or arg-validation blocked — using the
        # ORIGINAL tool call's id/name/args (order-aligned with prepared_calls), so
        # a vetoed call surfaces a rendered node whose is_error result the
        # front-end can fold in (E5 §4 / S33). Without this the blocked result had
        # no widget and was silently dropped.
        for tc in tool_calls:
            await self._emit(
                AgentEvent(
                    type="tool_execution_start",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    args=tc.arguments if isinstance(tc.arguments, dict) else {},
                )
            )

        # Execute all in parallel
        async def _run_tool(pc):
            if isinstance(pc, (BlockedCall, ErrorCall)):
                return AgentToolResult.from_error(pc.call.name, pc.error, pc.call.id)
            # pc is a PreparedToolCall
            result = await self._execute_tool(pc)
            result = await self._apply_after_hooks(result, pc.arguments)
            return result

        tasks = [_run_tool(pc) for pc in prepared_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_results: list[AgentToolResult] = []
        for i, res in enumerate(results):
            pc = prepared_calls[i]
            # gather(return_exceptions=True) yields BaseException (not just
            # Exception) for a failed/cancelled task — narrow on the broader type.
            if isinstance(res, BaseException):
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
                # An extension VETO (S50) surfaces distinctly: the blocked marker
                # rides the end event and a JSON veto record is emitted. Recovered
                # from the aligned prepared call (``from_error`` drops the attribution).
                blocked_by: str | None = None
                if isinstance(pc, BlockedCall) and pc.blocked_by_extension is not None:
                    blocked_by = pc.blocked_by_extension
                    self._emit_veto_record(res.tool_name, pc.error, blocked_by)
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=res.tool_call_id,
                        tool_name=res.tool_name,
                        result=res.content,
                        is_error=res.is_error,
                        blocked=blocked_by is not None,
                        blocked_by=blocked_by,
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
            # content_list holds raw block dicts; model_validate lets pydantic
            # coerce them into the TextContent | ImageContent union the field
            # declares (a plain constructor call can't be typed against dicts).
            result_messages.append(
                ToolResultMessage.model_validate(
                    {
                        "role": "toolResult",
                        "tool_call_id": r.tool_call_id or "",
                        "tool_name": r.tool_name,
                        "content": content_list,
                        "is_error": r.is_error,
                        "timestamp": int(time.time() * 1000),
                    }
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

            # The args dict the tool will execute with. The tool_call hook may
            # mutate it IN PLACE to patch args; because this is the SAME object
            # threaded into PreparedToolCall.arguments below, the patch reaches
            # the tool without any re-validation (pi parity, §7 decision E2-a).
            input_args = call_args if isinstance(call_args, dict) else {}

            # S11 — the `tool_call` mutating hook (E2). Gated on has_handlers for
            # the zero-extension fast path. pi wires this at agent-session's
            # beforeToolCall (agent-session.ts:405-424), consumed in agent-loop's
            # prepareToolCall (agent-loop.ts:581-602): a `block: true` result
            # short-circuits into an error tool result whose text is `reason`.
            dispatcher = self._hook_dispatcher
            if dispatcher is not None and dispatcher.has_handlers("tool_call"):
                event: dict[str, Any] = {
                    "type": "tool_call",
                    "tool_call_id": tool_call.id,
                    "tool_name": call_name,
                    "input": input_args,
                }
                try:
                    hook_result = await dispatcher.emit_tool_call(event)
                except Exception as hook_err:
                    # Fail-CLOSED (pi agent-session.ts:419-424): a throwing
                    # tool_call handler blocks execution rather than letting the
                    # tool run unguarded.
                    return BlockedCall(
                        call=PreparedToolCall(
                            id=tool_call.id,
                            name=call_name,
                            arguments={},
                        ),
                        error=f"Extension failed, blocking execution: {hook_err}",
                    )
                if hook_result and hook_result.get("block"):
                    return BlockedCall(
                        call=PreparedToolCall(
                            id=tool_call.id,
                            name=call_name,
                            arguments={},
                        ),
                        error=hook_result.get("reason") or "Tool execution was blocked",
                        # The runner attributed the veto to the blocking extension
                        # (S50); thread it so the call-site renders "⛔ blocked by
                        # <ext>" + emits the JSON veto record.
                        blocked_by_extension=hook_result.get("extension"),
                    )
                # No re-validation after mutation (pi parity): event["input"] is
                # the possibly-patched args object the tool executes with.
                input_args = event["input"]

            return PreparedToolCall(
                id=tool_call.id,
                name=call_name,
                arguments=input_args,
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
                    result if isinstance(result, list) else [{"type": "text", "text": str(result)}]
                )
                return AgentToolResult(
                    tool_name=call.name,
                    tool_call_id=call.id,
                    content=content_list,
                    is_error=False,
                )
        except Exception as e:
            return AgentToolResult.from_error(call.name, str(e), call.id)

    async def _apply_after_hooks(
        self,
        result: AgentToolResult,
        input_args: dict[str, Any] | None = None,
    ) -> AgentToolResult:
        """Apply the ``tool_result`` mutating hook (E2 / step S12) to ``result``.

        pi wires this at agent-session's ``afterToolCall`` (agent-session.ts:427-452),
        applied in agent-loop's ``finalizeExecutedToolCall`` (agent-loop.ts:682-707).
        Gated on ``has_handlers`` for the zero-extension fast path.

        The dispatcher clones the event once and lets each handler field-patch
        ``content`` / ``details`` / ``is_error`` (whole-value replace, later handler
        sees the earlier handler's patch); it returns those fields only when
        something changed, else ``None`` (pass the result through unchanged). Each
        handler's exception is swallowed-and-continued but surfaced via the runner's
        ``emit_error`` (never silently dropped, pi runner.ts:754-763).

        Only ``content`` and ``is_error`` map back onto the result — τ's
        ``AgentToolResult`` has no ``details`` field (a genuine model divergence
        from pi, not a swallowed value). ``details`` still rides the event so a
        handler can read it and chain a patch to a later handler.

        Applied pi-faithfully with ``?? existing`` semantics (agent-loop.ts:697-701):
        a patched-to-``None`` field falls back to the original value.

        Args:
            result: The tool execution result.
            input_args: The args the tool executed with (the ``input`` the event
                carries so handlers can correlate the result to its call).

        Returns:
            The (possibly patched) result.
        """
        dispatcher = self._hook_dispatcher
        if dispatcher is None or not dispatcher.has_handlers("tool_result"):
            return result

        event: dict[str, Any] = {
            "type": "tool_result",
            "tool_name": result.tool_name,
            "tool_call_id": result.tool_call_id,
            "input": input_args if input_args is not None else {},
            "content": result.content,
            "details": None,
            "is_error": result.is_error,
        }
        patch = await dispatcher.emit_tool_result(event)
        if patch is not None:
            # pi `afterResult.content ?? result.content`: only a non-None patch
            # replaces; a handler that patched a field to None falls back to the
            # original.
            if patch.get("content") is not None:
                result.content = patch["content"]
            if patch.get("is_error") is not None:
                result.is_error = patch["is_error"]
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
