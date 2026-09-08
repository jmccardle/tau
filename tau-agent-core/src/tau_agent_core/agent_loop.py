"""τ-agent-core agent_loop: The core agent loop that drives conversations.

Reference: PHASE-2-SUBPHASE-1.md — Agent Loop.
Reference: SUBPHASE-0.0.md, "5. Agent Events (tau-agent-core)" section.

Implements AgentLoop — the direct port of pi's agent-loop.js logic.
It takes messages + context, calls the LLM via τ-llm, parses assistant
responses for text and tool calls, executes tool calls (sequential or
parallel), feeds results back to the LLM, and repeats until no more
tool calls or termination.

Usage:
    loop = AgentLoop(config=config, emit=emit_event)
    messages = await loop.run(prompts=[user_msg], context=[])
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from tau_llm.abort import AbortSignal
from tau_llm.client import stream_simple
from tau_llm.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.tools import validate_tool_arguments
from tau_llm.types import (
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
from tau_agent_core.events import AgentEndReason, AgentEvent
from tau_agent_core.messages import convert_to_llm, create_custom_message
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_agent_core.extensions.runner import ExtensionRunner


@agent_facing(topic="agent-loop")
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


@agent_facing(topic="agent-loop")
class ErrorCall:
    """A tool call that raised an error during preparation."""

    def __init__(self, call: PreparedToolCall, error: str) -> None:
        self.call = call
        self.error = error


ABORTED_TOOL_RESULT = "Operation aborted"

COMPLETED_MESSAGES_ATTR = "tau_completed_messages"


@agent_facing(topic="agent-loop")
def completed_messages(exc: BaseException) -> list[Any]:
    """The messages an agent-loop run had already completed when *exc* ended it.

    Empty for any other exception, so a caller can ask unconditionally. The
    caller that matters is
    :meth:`~tau_agent_core.agent_session.AgentSession._run_one_turn`, which
    persists these before re-raising — the requirement in docs/PLAN-0.9.4.md §3:
    *every complete message or tool result should be persisted*.
    """
    return list(getattr(exc, COMPLETED_MESSAGES_ATTR, []))


@agent_facing(topic="agent-loop")
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
        _steer_queue: The session's live steering queue — the delivery point for
            ``multitask_strategy="steer"`` (docs/SUBMISSION-LIFECYCLE.md phase 4;
            pi ``agent-loop.ts:166-186``). The LIST OBJECT itself is shared with
            :class:`~tau_agent_core.agent_session.AgentSession` rather than a
            drain callback, deliberately: the loop must both PEEK (to decide
            whether a would-be-final turn takes one more LLM call) and DRAIN
            (immediately before the call that carries the content), and a
            drain-only seam forces the loop to hold drained content in a local
            that a ``max_turns`` exit would strand. Sharing the list means
            nothing leaves the queue until the very statement that delivers it.
            ``None`` when the loop runs standalone (no session).
    """

    def __init__(
        self,
        config: AgentLoopConfig,
        emit: Callable[[AgentEvent], Awaitable[None]] | None = None,
        tools: list[AgentTool] | None = None,
        model: Any = None,
        abort_signal: AbortSignal | None = None,
        hook_dispatcher: ExtensionRunner | None = None,
        steer_queue: list[Any] | None = None,
    ) -> None:
        self.config = config
        self._emit = emit or (lambda e: asyncio.create_task(self._noop_emit(e)))
        self._turn_index = 0
        self._tools: dict[str, AgentTool] = {}
        for t in tools or []:
            self._tools[t.name] = t
        self._model = model
        self._abort_signal: AbortSignal | None = abort_signal
        self._hook_dispatcher: ExtensionRunner | None = hook_dispatcher
        self._steer_queue: list[Any] | None = steer_queue

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
        context = list(context) if context else []
        messages = list(context)
        messages.extend(prompts)

        await self._emit(AgentEvent(type="agent_start", timestamp=int(time.time() * 1000)))

        turn_index = 0
        final_messages: list[Any] = []
        prev_batch: tuple[str, bool] | None = None
        repeat_count = 1
        end_reason: AgentEndReason = "done"

        try:
            while not self._turn_ceiling_reached(turn_index):
                if self._abort_signal and self._abort_signal.is_aborted():
                    end_reason = "aborted"
                    break

                await self._emit(
                    AgentEvent(
                        type="turn_start",
                        timestamp=int(time.time() * 1000),
                        turn_index=turn_index,
                    )
                )

                await self._deliver_steer(messages, final_messages)

                # Stream response from LLM
                assistant = await self._stream_response(messages)
                final_messages.append(assistant)
                messages.append(assistant)

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
                    await self._run_mutating_turn_end(
                        turn_index,
                        self._turn_usage(assistant),
                        [self._serialize_message(assistant)],
                        messages,
                        final_messages,
                    )
                    turn_index += 1
                    if self._steer_queue:
                        continue
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
                    end_reason = "terminate"
                    break

                signature = self._batch_signature(tool_calls, batch)
                if prev_batch == signature and signature[1]:
                    repeat_count += 1
                else:
                    repeat_count = 1
                prev_batch = signature
                if (
                    self.config.repeat_tool_call_limit is not None
                    and repeat_count >= self.config.repeat_tool_call_limit
                ):
                    end_reason = "repeat_tool_calls"
                    break

                turn_index += 1

            else:
                end_reason = "max_turns"

        except BaseException as exc:
            setattr(exc, COMPLETED_MESSAGES_ATTR, list(final_messages))
            await self._emit_agent_end(final_messages, exc)
            raise

        await self._emit_agent_end(final_messages, end_reason=end_reason)

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
        prev_batch: tuple[str, bool] | None = None
        repeat_count = 1
        end_reason: AgentEndReason = "done"

        await self._emit(AgentEvent(type="agent_start", timestamp=int(time.time() * 1000)))

        try:
            while not self._turn_ceiling_reached(turn_index):
                if self._abort_signal and self._abort_signal.is_aborted():
                    end_reason = "aborted"
                    break

                await self._emit(
                    AgentEvent(
                        type="turn_start",
                        timestamp=int(time.time() * 1000),
                        turn_index=turn_index,
                    )
                )

                await self._deliver_steer(messages, final_messages)

                assistant = await self._stream_response(messages)
                final_messages.append(assistant)
                messages.append(assistant)

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
                    if self._steer_queue:
                        continue
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
                    end_reason = "terminate"
                    break

                # Repeat detection — see run() for the rule and why it sits here.
                signature = self._batch_signature(tool_calls, batch)
                if prev_batch == signature and signature[1]:
                    repeat_count += 1
                else:
                    repeat_count = 1
                prev_batch = signature
                if (
                    self.config.repeat_tool_call_limit is not None
                    and repeat_count >= self.config.repeat_tool_call_limit
                ):
                    end_reason = "repeat_tool_calls"
                    break

                turn_index += 1

            else:
                # See run(): falling out of the `while` condition means `max_turns`.
                end_reason = "max_turns"

        except BaseException as exc:
            setattr(exc, COMPLETED_MESSAGES_ATTR, list(final_messages))
            await self._emit_agent_end(final_messages, exc)
            raise

        await self._emit_agent_end(final_messages, end_reason=end_reason)

        return final_messages

    def _turn_ceiling_reached(self, turn_index: int) -> bool:
        """Whether ``max_turns`` forbids starting the turn at ``turn_index``.

        ``max_turns=None`` — the default — is no ceiling, so this is ``False``
        forever and the loop ends the way pi's does: no tool calls, an error, an
        abort, or a ``terminate``-ing tool. See ``AgentLoopConfig.max_turns`` for
        why the old hardcoded 50 is gone.

        The index is the session's, not this call's: ``run_continue`` starts from
        ``self._turn_index`` rather than 0, so a stated ceiling bounds the whole
        run and not each continuation separately.
        """
        return self.config.max_turns is not None and turn_index >= self.config.max_turns

    @staticmethod
    def _batch_signature(tool_calls: list[ToolCall], batch: ToolBatchResult) -> tuple[str, bool]:
        """Reduce one turn's tools to ``(what was asked for, did all of it fail)``.

        The signature is the turn's ``(name, arguments)`` pairs, sorted and
        JSON-encoded with sorted keys, so two turns compare equal when they ask
        for the same work — argument key order and call order do not matter,
        because neither changes what the tools do. ``default=str`` keeps an
        unserializable argument from raising here: this is a comparison key, and
        a key built from a repr beats a key that cannot be built.

        The names and arguments come from the CALLS and the error-ness from the
        RESULTS, because those live on different objects:
        :class:`~tau_agent_core.tools.base.AgentToolResult` records what a tool
        returned, never what it was passed.

        The second element is whether EVERY result is an error. It is what keeps
        the check off a model that is working: a turn with one success in it made
        progress, whatever else it repeated.

        Args:
            tool_calls: The calls the assistant asked for this turn.
            batch: The results of executing them.

        Returns:
            The comparison key, and whether every result errored. A turn with no
            results reports ``False`` — no results is not "all of them failed".
        """
        calls = sorted(
            json.dumps([tc.name, tc.arguments], sort_keys=True, default=str) for tc in tool_calls
        )
        all_errored = bool(batch.tool_results) and all(r.is_error for r in batch.tool_results)
        return "\n".join(calls), all_errored

    async def _emit_agent_end(
        self,
        final_messages: list[Any],
        error: BaseException | None = None,
        end_reason: AgentEndReason | None = None,
    ) -> None:
        """Close the ``agent_start`` bracket, however the loop ended.

        This used to be reachable only by falling out of the ``while``, so a loop
        that RAISED — a provider ``ErrorEvent`` becomes a ``RuntimeError``
        (:meth:`_stream_response`); a dropped connection — or one that was
        CANCELLED (``abort()`` cancels every forked task) emitted no ``agent_end``
        at all, leaving the bracket open for the rest of the session.

        pi never has this problem because a provider error is a *value* there:
        ``agent-loop.ts:342-353`` handles ``"error"`` in the same case as
        ``"done"``, returns a ``stopReason="error"`` message, and emits
        ``turn_end``/``agent_end`` on the way out normally. τ raises instead —
        deliberately, so a caller cannot read a failed turn as a successful one —
        which makes closing the bracket this loop's own job.

        No synthetic ``turn_end`` accompanies the error close. pi can emit one
        because it has a real final message to attach; here the turn was abandoned
        mid-flight and a ``turn_end`` carrying ``tool_results=[]`` would assert
        that a turn completed with no tools, which is a claim, not an observation.

        Args:
            final_messages: What the loop produced before it closed.
            error: The exception that ended the loop, if one did.
            end_reason: How a non-raising loop stopped. Ignored when ``error`` is
                set — that case is ``"error"`` by definition, and taking the
                caller's word for it would let the two fields disagree.
        """
        detail = str(error) if error is not None else ""
        await self._emit(
            AgentEvent(
                type="agent_end",
                timestamp=int(time.time() * 1000),
                messages=[
                    m.model_dump() if hasattr(m, "model_dump") else m for m in final_messages
                ],
                is_error=error is not None,
                error=(
                    None
                    if error is None
                    else (f"{type(error).__name__}: {detail}" if detail else type(error).__name__)
                ),
                end_reason=("error" if error is not None else end_reason),
            )
        )

    async def _deliver_steer(self, messages: list[Any], final_messages: list[Any]) -> int:
        """Weave every queued steering message into the context. Returns how many.

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 4 (``multitask_strategy=
        "steer"``); pi ``agent-loop.ts:172-186``.

        THE delivery point steer is defined by: called immediately before an
        ``_stream_response`` call, after the previous turn's tool calls have
        already appended their ``toolResult`` messages. Not the turn edge (that
        is ``enqueue``), not an abort (that is ``rollback``), and emphatically
        not a parked wait for input — the loop never blocks on this; it takes
        whatever is there and goes.

        Drain and delivery are the same statement, so nothing can be removed
        from the session's queue and then not sent. Each message is appended to
        BOTH the running context (``messages`` — what the next LLM call sees)
        and ``final_messages`` (what ``AgentSession._persist_loop_messages``
        writes to the log), which is how a steered utterance ends up in the
        transcript rather than reaching the model through a hidden channel.
        ``message_start``/``message_end`` bracket each one so a renderer can
        show it, exactly as pi emits them.
        """
        if not self._steer_queue:
            return 0
        pending = list(self._steer_queue)
        self._steer_queue.clear()
        for message in pending:
            payload = self._serialize_message(message)
            await self._emit(
                AgentEvent(
                    type="message_start",
                    timestamp=int(time.time() * 1000),
                    message=payload,
                )
            )
            messages.append(message)
            final_messages.append(message)
            await self._emit(
                AgentEvent(
                    type="message_end",
                    timestamp=int(time.time() * 1000),
                    message=payload,
                )
            )
        return len(pending)

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

        messages = convert_to_llm(list(context))
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

        options: dict[str, Any] = {}
        if self.config.temperature is not None:
            options["temperature"] = self.config.temperature
        if self.config.api_key:
            options["api_key"] = self.config.api_key
        if self.config.reasoning is not None:
            options["reasoning"] = self.config.reasoning
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
        started = False

        async def start_once(content: list[Any]) -> None:
            """Emit ``message_start`` for this completion, at most once.

            ``message_start``/``message_end`` bracket ONE assistant message — the
            contract `_drain_steer_queue` states in its own docstring, and what pi
            does: it emits the event on the stream's `start` event and never again
            (``agent-loop.ts:323``).

            τ's streaming vocabulary has no `start` event, so the bracket opens on
            the first content event of any kind. That is why the guard lives here
            rather than in one branch: emitting from the text branch alone opened
            the bracket once per text delta (a 2137-delta answer emitted 2137
            ``message_start`` events), and never opened it at all for a completion
            that produced only reasoning or only a tool call.
            """
            nonlocal started
            if started:
                return
            started = True
            await self._emit(
                AgentEvent(
                    type="message_start",
                    timestamp=int(time.time() * 1000),
                    message={"role": "assistant", "content": list(content)},
                )
            )

        async for event in stream:
            if isinstance(event, TextDeltaEvent):
                partial_text += event.delta
                partial_content_blocks = [{"type": "text", "text": partial_text}]
                await start_once(partial_content_blocks)
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
                partial_reasoning += event.delta
                thinking_blocks = [{"type": "thinking", "thinking": partial_reasoning}]
                await start_once(thinking_blocks)
                await self._emit(
                    AgentEvent(
                        type="message_update",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": thinking_blocks,
                        },
                    )
                )
            elif isinstance(event, ToolCallDeltaEvent):
                partial = event.partial
                if partial is not None:
                    partial_content_blocks = [c.model_dump() for c in partial.content]

                await start_once(partial_content_blocks)
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
                final_blocks = [
                    c.model_dump() if hasattr(c, "model_dump") else c for c in final_msg.content
                ]
                await start_once(final_blocks)
                await self._emit(
                    AgentEvent(
                        type="message_end",
                        timestamp=int(time.time() * 1000),
                        message={
                            "role": "assistant",
                            "content": final_blocks,
                            "usage": final_msg.usage.model_dump(),
                            "model": final_msg.model,
                            "stop_reason": final_msg.stop_reason,
                        },
                    )
                )
                return final_msg
            elif isinstance(event, ErrorEvent):
                detail = (event.message or "").strip()
                if not detail:
                    model_label = getattr(model, "id", model)
                    detail = (
                        f"provider emitted an error event with an empty message "
                        f"(model {model_label!r}); the upstream failure is unreported"
                    )
                error_msg = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {detail}"}],
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
                raise RuntimeError(detail)

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

        The batch runs sequentially if the config says so, OR if any tool call
        in the batch resolves to a registered tool declaring
        ``execution_mode == "sequential"`` (pi agent-loop.ts:381-384). A call
        whose name is not in ``self._tools`` contributes nothing to that check
        (pi's optional-chaining lookup yields undefined for an unknown tool,
        which is never "sequential") — unknown names are handled downstream by
        ``_prepare_tool_call``. Only when the config says "parallel" AND every
        resolvable tool in the batch is "parallel" does the batch gather.

        ``self._tools`` is **homogeneous** since B1 (`tau-004`): every entry is an
        ``AgentTool``, from ``sdk._resolve_tools`` and from
        ``AgentSession._resolve_extension_tools`` alike, so the declared
        ``dict[str, AgentTool]`` is true and ``.execution_mode`` resolves through
        the same alias property for both sources. It was not always so — the
        built-ins used to arrive as raw classes with no ``.definition``, which is
        what made `tau-001`'s ``.definition.execution_mode`` crash on every
        production tool call while mypy and the suite both stayed green. The read
        below is kept as a plain attribute read rather than reverted to
        ``.definition.execution_mode``: the alias is the narrower dependency, and
        it is the one the seven built-in classes' own plain ``execution_mode``
        attribute also satisfies, which keeps a directly-constructed loop honest.

        The read is deliberately unguarded: every τ built-in declares
        ``execution_mode`` and ``ToolDefinition`` declares it with a default,
        so a registered tool that lacks the attribute is an unanticipated
        shape — a construction gap, which raises rather than silently
        resolving to "parallel" and quietly resurrecting the dead-field defect
        this method exists to fix. This is a deliberate, narrow divergence
        from pi, whose ``executionMode?`` is optional (``types.ts:388``) and
        read through optional chaining (``agent-loop.ts:382``); τ's own type
        makes the field non-optional, so "absent" is not a state τ specifies.

        Args:
            assistant: The assistant message containing tool calls.
            tool_calls: List of ToolCall objects.

        Returns:
            ToolBatchResult with tool result messages.
        """
        if self._abort_signal and self._abort_signal.is_aborted():
            return await self._aborted_batch(tool_calls, prior=[])

        has_sequential_tool_call = any(
            self._tools[tc.name].execution_mode == "sequential"
            for tc in tool_calls
            if tc.name in self._tools
        )
        if self.config.tool_execution_mode == "sequential" or has_sequential_tool_call:
            return await self._execute_sequential(assistant, tool_calls)
        return await self._execute_parallel(assistant, tool_calls)

    async def _aborted_batch(
        self,
        tool_calls: list[ToolCall],
        prior: list[AgentToolResult],
    ) -> ToolBatchResult:
        """Answer every tool call the abort left outstanding (docs/PLAN-0.9.4.md §3).

        The defect this replaces: the sequential executor simply ``break``\\ed on
        abort and synthesized nothing. That was invisible while an aborted turn
        was discarded wholesale — but the turn is persisted now, so the same code
        would write an assistant message carrying ``tool_call_id``\\ s that no
        result ever answers. A validating provider rejects that transcript
        outright on the next request, which turns a cancelled turn into a
        conversation that cannot be resumed. pi does not have the problem: it
        returns an aborted error result for every outstanding call.

        *prior* holds the results a sequential batch already finished before the
        abort landed. Those keep their genuine outcome and their position; only
        the calls with no result yet are answered here.

        The ``tool_execution_start``/``tool_execution_end`` pair is emitted for
        each synthesized result for the reason the veto path states above: a
        front-end folds a result into the widget its *start* event created, so a
        result with no preceding start is silently dropped and the user sees a
        turn that simply stops.
        """
        results = list(prior)
        answered = {r.tool_call_id for r in prior}
        for tc in tool_calls:
            if tc.id in answered:
                continue
            await self._emit(
                AgentEvent(
                    type="tool_execution_start",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    args=tc.arguments if isinstance(tc.arguments, dict) else {},
                )
            )
            await self._emit(
                AgentEvent(
                    type="tool_execution_end",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    result=ABORTED_TOOL_RESULT,
                    is_error=True,
                )
            )
            results.append(AgentToolResult.from_error(tc.name, ABORTED_TOOL_RESULT, tc.id))
        return self._build_batch_result(results)

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
                return await self._aborted_batch(tool_calls, prior=all_results)

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

        return self._build_batch_result(all_results, terminate=terminated)

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
                        details=res.details,
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
            result_messages.append(
                ToolResultMessage.model_validate(
                    {
                        "role": "toolResult",
                        "tool_call_id": r.tool_call_id or "",
                        "tool_name": r.tool_name,
                        "content": content_list,
                        "details": r.details,
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

            input_args = call_args if isinstance(call_args, dict) else {}

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
                        blocked_by_extension=hook_result.get("extension"),
                    )
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
                details = result.get("details")
                content_list = (
                    content
                    if isinstance(content, list)
                    else [{"type": "text", "text": str(content)}]
                )
                return AgentToolResult(
                    tool_name=call.name,
                    tool_call_id=call.id,
                    content=content_list,
                    details=details if isinstance(details, dict) else None,
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

        All three patchable fields map back onto the result. ``?? existing``
        semantics (agent-loop.ts:697-701): a patched-to-``None`` field falls back
        to the original value, so a handler cannot clear ``details`` by patching
        it to ``None`` — it must patch an empty dict.

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
            "details": result.details,
            "is_error": result.is_error,
        }
        patch = await dispatcher.emit_tool_result(event)
        if patch is not None:
            if patch.get("content") is not None:
                result.content = patch["content"]
            if patch.get("details") is not None:
                result.details = patch["details"]
            if patch.get("is_error") is not None:
                result.is_error = patch["is_error"]
        return result
