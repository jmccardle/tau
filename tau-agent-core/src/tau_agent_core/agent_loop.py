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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from tau_llm.abort import AbortSignal
from tau_llm.client import stream_simple
from tau_llm.errors import ProviderStreamError
from tau_llm.streaming import (
    DoneEvent,
    ErrorEvent,
    RetryEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.types import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
)

from tau_agent_core.agent_loop_types import (
    AgentLoopConfig,
    PreparedToolCall,
    TurnSettings,
    TurnSettingsSource,
)
from tau_agent_core.entry_annotations import AnnotationError
from tau_agent_core.events import AgentEndReason, AgentEvent, ErrorDetail
from tau_agent_core.provider_retry import provider_retry_event
from tau_agent_core.suspension import TurnSuspended
from tau_agent_core.messages import convert_to_llm, create_custom_message
from tau_agent_core.tool_pipeline import (
    BlockedCall,
    ErrorCall,
    apply_tool_result_hooks,
    execute_prepared_call,
    prepare_tool_call,
)
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult
from tau_agent_core.truncation import (
    TRUNCATED_STOP_REASON,
    unrun_tool_result_text,
    with_unrun_tool_calls_counted,
)
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_agent_core.extensions.runner import ExtensionRunner

MidTurnCompactor = Callable[[list[Any], list[Any]], Awaitable[list[Any] | None]]
"""Called at every agent-loop turn boundary; may replace the loop's context.

Receives ``(messages, produced)`` — the full context the loop is about to send
again, and the messages this loop has produced so far — and returns a REPLACEMENT
context, or None to leave it alone. :meth:`AgentSession._compact_mid_turn` is the
implementation; the loop itself holds no compaction policy, because the window,
the settings and the session log all live a layer up.
"""


@agent_facing(topic="agent-loop")
class TurnWriter(Protocol):
    """Where a loop writes each message the moment it exists (docs/TAU-SERVE.md §4.2).

    :class:`~tau_agent_core.agent_session.AgentSession` implements it over the
    turn's cursor. A loop with no writer writes nothing and only returns what it
    produced.
    """

    async def open(self, message: dict[str, Any]) -> Any:
        """Record that an assistant message has started.

        Args:
            message: The empty assistant message dict, before any content streams.

        Returns:
            An opaque handle the loop passes back to :meth:`finalize`.
        """
        ...

    async def finalize(self, handle: Any, message: Any) -> None:
        """Complete the message :meth:`open` started.

        Args:
            handle: What :meth:`open` returned for this message.
            message: The finished assistant message.

        Raises:
            AnnotationError: after the write, when an ``annotate`` handler failed.
        """
        ...

    async def append(self, message: Any) -> None:
        """Write a message that is finished when it exists: a tool result, a steer, a node.

        Args:
            message: The finished message, a dict or a pydantic message model.

        Raises:
            AnnotationError: after the write, when an ``annotate`` handler failed.
        """
        ...

    async def close_group(self) -> None:
        """End a write group: dispatch ``entry_written`` for each entry written since the last call.

        docs/STEP-DEBUGGER.md §3.2. The loop calls it after a group's last write.
        """
        ...


ABORTED_TOOL_RESULT = "Operation aborted"

UNRUN_TOOL_RESULT = "Not run: the response stream failed before the turn could run its tools"


STREAM_RECORDED_ATTR = "tau_stream_recorded"
"""Set on the exception a failed stream raises: the messages written for it.

Empty when nothing streamed, and otherwise the partial assistant message plus an
error result for each of its tool calls (docs/TURN-FAILURES.md §2). The exception
keeps its own type, so a caller catching it specifically keeps working.
"""


COMPLETED_MESSAGES_ATTR = "tau_completed_messages"


@agent_facing(topic="agent-loop")
def completed_messages(exc: BaseException) -> list[Any]:
    """The messages an agent-loop run had already completed when *exc* ended it.

    The caller that matters is
    :meth:`~tau_agent_core.agent_session.AgentSession._run_one_turn`, which
    persists these before re-raising (docs/PLAN-0.9.4.md §3).

    Args:
        exc: Any exception; only one raised out of an agent-loop run carries
            messages.

    Returns:
        A copy of the completed messages, or an empty list for any other
        exception, so a caller can ask unconditionally.
    """
    return list(getattr(exc, COMPLETED_MESSAGES_ATTR, []))


@agent_facing(topic="agent-loop")
class AgentLoop:
    """The core agent loop: drives conversations, executes tools, emits events.

    Events follow docs/SUBPHASE-0.0.md, "5. Agent Events".

    Args:
        config: Agent loop configuration.
        emit: Awaited with each AgentEvent; ``None`` emits nothing.
        tools: The tools the model may call, keyed by name (a later duplicate
            name wins).
        model: Overrides ``config.model`` when truthy. Ignored with ``turn_settings``.
        abort_signal: Checked between turns and passed to the stream and tools;
            ``None`` means the run cannot be aborted.
        hook_dispatcher: The return-collecting extension hook dispatcher, whose
            ``emit_*`` results the mutating-hook call-sites thread forward.
            ``None`` when standalone: no hook work at all.
        steer_queue: The session's live steering queue (docs/SUBMISSION-LIFECYCLE.md
            phase 4). The list object itself is shared, not a drain callback, so
            the loop can peek before a would-be-final turn and drain only in the
            statement that delivers; a ``max_turns`` exit strands nothing.
        mid_turn_compactor: Offered the context at each turn boundary; see
            :data:`MidTurnCompactor`. ``None`` never compacts mid-turn.
        writer: Where each message is written as it exists; ``None`` writes
            nothing and the loop only returns what it produced.
        call_gate: Given a model-issued call's tool name, the reason to refuse
            it, or ``None`` to let it run; how a masked tool stays declared, so
            the ``tools`` block and the prompt cache hold. ``None`` gates nothing.
        turn_settings: Awaited before every request for its model, key,
            reasoning and temperature, so a switch made between two requests
            reaches the second (docs/TURN-SETTINGS.md §2). ``None`` sends every
            request with ``model`` and ``config``'s values.
        input_failure: An ``annotate`` failure on an input written before the
            run; the run raises it after ``agent_start``, before any request.

    Attributes:
        config: The configuration passed in.
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
        mid_turn_compactor: MidTurnCompactor | None = None,
        writer: TurnWriter | None = None,
        call_gate: Callable[[str], str | None] | None = None,
        turn_settings: TurnSettingsSource | None = None,
        input_failure: AnnotationError | None = None,
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
        self._mid_turn_compactor: MidTurnCompactor | None = mid_turn_compactor
        self._writer: TurnWriter | None = writer
        self._call_gate = call_gate
        self._turn_settings = turn_settings
        self._recorded: dict[int, dict[str, Any]] = {}
        self._request_max_tokens: int | None = None
        self._annotation_failure: AnnotationError | None = input_failure

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

        The zero-extension fast path: the hook call-sites (S11-S14) check this
        before dispatching, so a loop with no extensions does no hook work.

        Args:
            event: The hook name.

        Returns:
            True if a dispatcher was injected and has a handler for ``event``.
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

                self._raise_annotation_failure()
                assistant = await self._stream_response(messages)
                final_messages.append(assistant)
                messages.append(assistant)

                tool_calls = assistant.get_tool_calls()
                await self._abort_on_annotation_failure(tool_calls, final_messages)

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
                    if self._abort_signal and self._abort_signal.is_aborted():
                        end_reason = "aborted"
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

                cut_off = assistant.stop_reason == TRUNCATED_STOP_REASON
                if cut_off:
                    batch = await self._refuse_cut_off_calls(assistant, tool_calls)
                else:
                    batch = await self._execute_tool_calls(assistant, tool_calls)
                await self._close_group()

                for msg in batch.messages:
                    messages.append(msg)
                    final_messages.append(msg)
                self._raise_annotation_failure()

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

                if cut_off:
                    turn_index += 1
                    if self._steer_queue:
                        continue
                    if self._abort_signal and self._abort_signal.is_aborted():
                        end_reason = "aborted"
                    break

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

                messages = await self._compact_if_needed(messages, final_messages)
                turn_index += 1

            else:
                end_reason = "max_turns"
            self._raise_annotation_failure()

        except TurnSuspended:
            await self._emit_agent_end(final_messages, end_reason="suspended")
            raise
        except BaseException as exc:
            final_messages.extend(getattr(exc, STREAM_RECORDED_ATTR, []))
            setattr(exc, COMPLETED_MESSAGES_ATTR, list(final_messages))
            await self._emit_agent_end(final_messages, exc)
            raise

        await self._emit_agent_end(final_messages, end_reason=end_reason)

        return final_messages

    async def _compact_if_needed(self, messages: list[Any], produced: list[Any]) -> list[Any]:
        """Offer the turn boundary to the mid-turn compactor; return the context to use.

        This is the ONE point inside a turn where replacing the context is safe:
        the tool results for the turn just finished are in ``messages``, the
        ``turn_end`` hooks have run, and the next statement is the next request.
        Compacting anywhere else would either drop a tool result its own tool call
        still refers to, or rewrite a list the loop is midway through reading.

        Returns ``messages`` unchanged when no compactor is injected or the
        compactor declined. A compactor that raises is NOT caught — a compaction
        that cannot run is the loop's problem, because the request it was
        protecting is the next thing that happens.
        """
        if self._mid_turn_compactor is None:
            return messages
        replacement = await self._mid_turn_compactor(messages, produced)
        return messages if replacement is None else replacement

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

                self._raise_annotation_failure()
                assistant = await self._stream_response(messages)
                final_messages.append(assistant)
                messages.append(assistant)

                tool_calls = assistant.get_tool_calls()
                await self._abort_on_annotation_failure(tool_calls, final_messages)
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
                    if self._abort_signal and self._abort_signal.is_aborted():
                        end_reason = "aborted"
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

                cut_off = assistant.stop_reason == TRUNCATED_STOP_REASON
                if cut_off:
                    batch = await self._refuse_cut_off_calls(assistant, tool_calls)
                else:
                    batch = await self._execute_tool_calls(assistant, tool_calls)
                await self._close_group()

                for msg in batch.messages:
                    messages.append(msg)
                    final_messages.append(msg)
                self._raise_annotation_failure()

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

                if cut_off:
                    turn_index += 1
                    if self._steer_queue:
                        continue
                    if self._abort_signal and self._abort_signal.is_aborted():
                        end_reason = "aborted"
                    break

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

                messages = await self._compact_if_needed(messages, final_messages)
                turn_index += 1

            else:
                # See run(): falling out of the `while` condition means `max_turns`.
                end_reason = "max_turns"
            self._raise_annotation_failure()

        except TurnSuspended:
            await self._emit_agent_end(final_messages, end_reason="suspended")
            raise
        except BaseException as exc:
            final_messages.extend(getattr(exc, STREAM_RECORDED_ATTR, []))
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
        that RAISED — a provider ``ErrorEvent`` is raised as its ``exception``, or
        as a ``ProviderStreamError`` (:meth:`_stream_response`); a dropped
        connection — or one that was CANCELLED (``abort()`` cancels every forked task) emitted no ``agent_end``
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
                error_detail=None if error is None else ErrorDetail.of(error),
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
            await self._write(message)
            await self._emit(
                AgentEvent(
                    type="message_end",
                    timestamp=int(time.time() * 1000),
                    message=payload,
                )
            )
        await self._close_group()
        return len(pending)

    async def _write(self, message: Any) -> None:
        """Hand a finished message to the writer, if there is one.

        An ``annotate`` failure is noted, not raised: the entry is written, and
        the turn ends on it at the next checkpoint (:meth:`_raise_annotation_failure`).
        """
        if self._writer is not None:
            try:
                await self._writer.append(message)
            except AnnotationError as err:
                self._note_annotation_failure(err)

    async def _close_group(self) -> None:
        """End the current write group (:meth:`TurnWriter.close_group`), if there is a writer."""
        if self._writer is not None:
            await self._writer.close_group()

    def _note_annotation_failure(self, err: AnnotationError) -> None:
        """Keep the first ``annotate`` failure; a later one is already on its entry.

        Only the first ends the turn, so a failure on a result written while the
        turn is aborting does not abort it again (docs/ENTRY-ANNOTATIONS.md §4.3).
        """
        if self._annotation_failure is None:
            self._annotation_failure = err

    def _raise_annotation_failure(self) -> None:
        """End the turn on a noted ``annotate`` failure, once its entry is written.

        Raises:
            AnnotationError: one was noted.
        """
        if self._annotation_failure is not None:
            raise self._annotation_failure

    async def _abort_on_annotation_failure(
        self, tool_calls: list[ToolCall], final_messages: list[Any]
    ) -> None:
        """End the turn on a noted failure, answering the assistant's calls first.

        Each call gets ``ABORTED_TOOL_RESULT`` and none runs, so no call is left
        unanswered in the tree (docs/ENTRY-ANNOTATIONS.md §4.3).

        Raises:
            AnnotationError: one was noted.
        """
        failure = self._annotation_failure
        if failure is None:
            return
        if tool_calls:
            self._recorded = {}
            batch = await self._aborted_batch(tool_calls, prior=[])
            final_messages.extend(batch.messages)
            await self._close_group()
        raise failure

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

        Raises:
            AnnotationError: an ``annotate`` failure on a node it wrote, or one
                noted earlier, so nothing after this turn (compaction, the next
                ``turn_start``, a steer) runs first.
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
            await self._write(node)
        await self._close_group()
        self._raise_annotation_failure()

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

    async def _request_settings(self) -> TurnSettings:
        """The settings the next request is sent with: the source's, else the static ones."""
        if self._turn_settings is not None:
            return await self._turn_settings()
        return TurnSettings(
            model=self._model or self.config.model,
            api_key=self.config.api_key,
            reasoning=self.config.reasoning,
            temperature=self.config.temperature,
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
        opens_with_system = (
            bool(messages)
            and (
                messages[0].get("role", "")
                if isinstance(messages[0], dict)
                else getattr(messages[0], "role", "")
            )
            == "system"
        )
        override = self.config.system_prompt_override
        if override is not None:
            frame = {"role": "system", "content": override}
            if opens_with_system:
                messages[0] = frame
            else:
                messages.insert(0, frame)
        elif self.config.system_prompt and not opens_with_system:
            messages.insert(0, {"role": "system", "content": self.config.system_prompt})

        context_dict = {
            "messages": messages,
            "tools": list(self._tools.values()) if self._tools else None,
        }

        settings = await self._request_settings()
        model = settings.model
        self._request_max_tokens = getattr(model, "max_tokens", None)

        options: dict[str, Any] = {}
        if settings.temperature is not None:
            options["temperature"] = settings.temperature
        if settings.api_key:
            options["api_key"] = settings.api_key
        if settings.reasoning is not None:
            options["reasoning"] = settings.reasoning
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
        handle: Any = None
        latest: AssistantMessage | None = None

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
            nonlocal started, handle
            if started:
                return
            started = True
            if self._writer is not None:
                handle = await self._writer.open(
                    {"role": "assistant", "content": [], "model": getattr(model, "id", None)}
                )
            await self._emit(
                AgentEvent(
                    type="message_start",
                    timestamp=int(time.time() * 1000),
                    message={"role": "assistant", "content": list(content)},
                )
            )

        try:
            async for event in stream:
                if isinstance(event, RetryEvent):
                    await self._emit(provider_retry_event(event))
                elif isinstance(event, TextDeltaEvent):
                    latest = event.partial
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
                    latest = event.partial
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
                    latest = event.partial
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
                    final_msg = with_unrun_tool_calls_counted(event.final)
                    final_blocks = [
                        c.model_dump() if hasattr(c, "model_dump") else c for c in final_msg.content
                    ]
                    await start_once(final_blocks)
                    if self._writer is not None:
                        try:
                            await self._writer.finalize(handle, final_msg)
                        except AnnotationError as err:
                            self._note_annotation_failure(err)
                    await self._close_group()
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
                                "reasoning_level": final_msg.reasoning_level,
                            },
                            response_headers=event.response_headers,
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
                    failure = (
                        event.exception
                        if isinstance(event.exception, Exception)
                        else ProviderStreamError(
                            detail,
                            model_id=str(getattr(model, "id", model)),
                            base_url=getattr(model, "base_url", None),
                        )
                    )
                    await self._stream_failed(failure, handle, latest, detail)
                    raise failure
        except Exception as exc:
            if not hasattr(exc, STREAM_RECORDED_ATTR):
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                await self._stream_failed(exc, handle, latest, detail)
            raise

        detail = (
            f"the stream for model {getattr(model, 'id', model)!r} ended without a "
            "DoneEvent, so the message is cut short where the stream stopped"
        )
        failure = RuntimeError(detail)
        await self._stream_failed(failure, handle, latest, detail)
        raise failure

    async def _stream_failed(
        self, failure: Exception, handle: Any, partial: AssistantMessage | None, detail: str
    ) -> None:
        """Record what a failed stream produced, onto ``failure``'s ``STREAM_RECORDED_ATTR``.

        Nothing streamed means nothing to record. Otherwise the opened entry is
        finalized as the provider's last partial message with ``stop_reason``
        ``"error"``, and each tool call in it gets an error result, because a call
        with no result makes the next request invalid (docs/TURN-FAILURES.md §2).
        """
        setattr(failure, STREAM_RECORDED_ATTR, [])
        if handle is None and partial is None:
            return
        if partial is None:
            raise RuntimeError(f"a message entry was opened with no partial message: {detail}")
        message = partial.model_copy(
            update={
                "stop_reason": "error",
                "error_message": detail,
                "timestamp": int(time.time() * 1000),
            }
        )
        if self._writer is not None:
            try:
                await self._writer.finalize(handle, message)
            except AnnotationError as err:
                self._note_annotation_failure(err)
        blocks = [c.model_dump() if hasattr(c, "model_dump") else c for c in message.content]
        await self._emit(
            AgentEvent(
                type="message_end",
                timestamp=int(time.time() * 1000),
                message={
                    "role": "assistant",
                    "content": blocks,
                    "usage": message.usage.model_dump(),
                    "model": message.model,
                    "stop_reason": "error",
                    "error_message": detail,
                },
            )
        )
        recorded: list[Any] = [message]
        tool_calls = message.get_tool_calls()
        if tool_calls:
            batch = await self._aborted_batch(tool_calls, prior=[], text=UNRUN_TOOL_RESULT)
            recorded.extend(batch.messages)
        setattr(failure, STREAM_RECORDED_ATTR, recorded)
        await self._close_group()

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
        self._recorded = {}
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

    async def _refuse_cut_off_calls(
        self, assistant: AssistantMessage, tool_calls: list[ToolCall]
    ) -> ToolBatchResult:
        """Answer every call in a completion the output cap cut off, running none.

        Each gets an error result saying why (docs/TRUNCATED-TOOL-CALLS.md §3.2),
        so the session stays valid for the next request and the model reads
        whatever of its arguments the provider recovered.
        """
        self._recorded = {}
        cap = self._request_max_tokens
        return await self._aborted_batch(
            tool_calls,
            prior=[],
            text=lambda tc: unrun_tool_result_text(assistant, tc, max_tokens=cap),
        )

    async def _aborted_batch(
        self,
        tool_calls: list[ToolCall],
        prior: list[AgentToolResult],
        text: str | Callable[[ToolCall], str] = ABORTED_TOOL_RESULT,
    ) -> ToolBatchResult:
        """Answer every outstanding tool call with an error ``text`` (docs/PLAN-0.9.4.md §3).

        ``text`` is one string for every call, or a function giving each its own.

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
            await self._emit_execution_start(tc)
            results.append(
                await self._answer_unrun(tc, text if isinstance(text, str) else text(tc))
            )
        return self._build_batch_result(results)

    async def _emit_execution_start(self, tc: ToolCall) -> None:
        """Emit ``tool_execution_start`` for ``tc``."""
        await self._emit(
            AgentEvent(
                type="tool_execution_start",
                timestamp=int(time.time() * 1000),
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments if isinstance(tc.arguments, dict) else {},
            )
        )

    async def _answer_unrun(self, tc: ToolCall, reason: str) -> AgentToolResult:
        """Answer a call that will not run with an error ``reason``: its end event, then its write."""
        await self._emit(
            AgentEvent(
                type="tool_execution_end",
                timestamp=int(time.time() * 1000),
                tool_call_id=tc.id,
                tool_name=tc.name,
                result=reason,
                is_error=True,
            )
        )
        result = AgentToolResult.from_error(tc.name, reason, tc.id)
        await self._record(result)
        return result

    async def _answer_refused(self, prepared: BlockedCall | ErrorCall) -> AgentToolResult:
        """Answer a call its preparation refused, keeping why: veto record, end event, write.

        Shared by both executors and by the parallel abort path, so a call
        blocked in the dispatch that trips the abort keeps its block reason.
        """
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
            result = AgentToolResult.from_error(
                prepared.call.name, prepared.error, prepared.call.id
            )
        else:
            result = prepared.result()
            await self._emit(
                AgentEvent(
                    type="tool_execution_end",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    result=result.content,
                    details=result.details,
                    is_error=True,
                )
            )
        await self._record(result)
        return result

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
            aborted = self._abort_signal is not None and self._abort_signal.is_aborted()
            if aborted or self._annotation_failure is not None:
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
            if isinstance(prepared, (BlockedCall, ErrorCall)):
                all_results.append(await self._answer_refused(prepared))
                await self._close_group()
                continue

            if self._abort_signal is not None and self._abort_signal.is_aborted():
                all_results.append(await self._answer_unrun(tc, ABORTED_TOOL_RESULT))
                return await self._aborted_batch(tool_calls, prior=all_results)

            result = await self._execute_tool(prepared)
            result = await self._apply_after_hooks(result, prepared.arguments)

            await self._emit(
                AgentEvent(
                    type="tool_execution_end",
                    timestamp=int(time.time() * 1000),
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    result=result.content,
                    details=result.details,
                    is_error=result.is_error,
                )
            )

            await self._record(result)
            await self._close_group()
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
        prepared_calls: list[PreparedToolCall | BlockedCall | ErrorCall] = []
        for tc in tool_calls:
            prepared_calls.append(await self._prepare_tool_call(tc))
            if self._abort_signal is not None and self._abort_signal.is_aborted():
                return await self._abort_prepared(tool_calls, prepared_calls)

        for tc in tool_calls:
            await self._emit_execution_start(tc)

        finished: list[AgentToolResult | None] = [None] * len(prepared_calls)
        flushed = 0
        flush_lock = asyncio.Lock()

        async def _flush_prefix() -> None:
            """Write every finished result whose earlier calls have all finished."""
            nonlocal flushed
            async with flush_lock:
                while flushed < len(finished):
                    ready = finished[flushed]
                    if ready is None:
                        return
                    await self._record(ready)
                    flushed += 1

        def _raised(pc: Any, exc: BaseException) -> AgentToolResult:
            return AgentToolResult.from_exception(
                pc.name if isinstance(pc, PreparedToolCall) else pc.call.name,
                exc,
                pc.id if isinstance(pc, PreparedToolCall) else pc.call.id,
            )

        async def _run_tool(index: int, pc: Any) -> AgentToolResult:
            if isinstance(pc, ErrorCall):
                result = pc.result()
            elif isinstance(pc, BlockedCall):
                result = AgentToolResult.from_error(pc.call.name, pc.error, pc.call.id)
            else:
                try:
                    result = await self._execute_tool(pc)
                    result = await self._apply_after_hooks(result, pc.arguments)
                except Exception as exc:
                    finished[index] = _raised(pc, exc)
                    await _flush_prefix()
                    raise
            finished[index] = result
            await _flush_prefix()
            return result

        results = await asyncio.gather(
            *(_run_tool(i, pc) for i, pc in enumerate(prepared_calls)), return_exceptions=True
        )
        for res in results:
            if isinstance(res, TurnSuspended):
                raise res

        all_results: list[AgentToolResult] = []
        for i, res in enumerate(results):
            pc = prepared_calls[i]
            if isinstance(res, BaseException):
                error_result = finished[i] or _raised(pc, res)
                finished[i] = error_result
                all_results.append(error_result)
                await self._emit(
                    AgentEvent(
                        type="tool_execution_end",
                        timestamp=int(time.time() * 1000),
                        tool_call_id=error_result.tool_call_id,
                        tool_name=error_result.tool_name,
                        result=error_result.content,
                        details=error_result.details,
                        is_error=True,
                    )
                )
            else:
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
        await _flush_prefix()
        await self._close_group()

        terminated = any(getattr(r, "terminate", False) for r in all_results)
        return self._build_batch_result(all_results, terminate=terminated)

    async def _abort_prepared(
        self,
        tool_calls: list[ToolCall],
        prepared_calls: list[PreparedToolCall | BlockedCall | ErrorCall],
    ) -> ToolBatchResult:
        """Answer a parallel batch whose abort tripped in a ``tool_call`` dispatch; run nothing.

        Each call prepared so far keeps its block or error reason; one that would
        have run, and every call not yet prepared, gets ``ABORTED_TOOL_RESULT``
        (docs/STEP-DEBUGGER.md §3.6).
        """
        prior: list[AgentToolResult] = []
        for tc, prepared in zip(tool_calls, prepared_calls):
            await self._emit_execution_start(tc)
            if isinstance(prepared, (BlockedCall, ErrorCall)):
                prior.append(await self._answer_refused(prepared))
            else:
                prior.append(await self._answer_unrun(tc, ABORTED_TOOL_RESULT))
        return await self._aborted_batch(tool_calls, prior=prior)

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
        return ToolBatchResult(
            messages=[self._recorded[id(r)] for r in results],
            tool_results=results,
            terminate=terminate,
        )

    async def _record(self, result: AgentToolResult) -> None:
        """Build ``result``'s toolResult message, keep it for the batch, and write it.

        The timestamp is when the result was collected, and the write happens then,
        so a crash keeps every result that finished before it (docs/TAU-SERVE.md §4.2).
        """
        content_list = (
            result.content
            if isinstance(result.content, list)
            else [{"type": "text", "text": str(result.content)}]
        )
        message = ToolResultMessage.model_validate(
            {
                "role": "toolResult",
                "tool_call_id": result.tool_call_id or "",
                "tool_name": result.tool_name,
                "content": content_list,
                "details": result.details,
                "is_error": result.is_error,
                "timestamp": int(time.time() * 1000),
            }
        ).model_dump()
        self._recorded[id(result)] = message
        await self._write(message)

    async def _prepare_tool_call(
        self, tool_call: ToolCall
    ) -> PreparedToolCall | BlockedCall | ErrorCall:
        """Validate args and run the ``tool_call`` hook (:func:`prepare_tool_call`)."""
        return await prepare_tool_call(
            self._tools, self._hook_dispatcher, tool_call, gate=self._call_gate
        )

    async def _execute_tool(self, call: PreparedToolCall) -> AgentToolResult:
        """Execute one prepared call (:func:`execute_prepared_call`)."""
        return await execute_prepared_call(self._tools, call, self._abort_signal)

    async def _apply_after_hooks(
        self,
        result: AgentToolResult,
        input_args: dict[str, Any] | None = None,
    ) -> AgentToolResult:
        """Apply the ``tool_result`` hook (:func:`apply_tool_result_hooks`)."""
        return await apply_tool_result_hooks(self._hook_dispatcher, result, input_args)
