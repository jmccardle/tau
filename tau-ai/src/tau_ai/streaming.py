"""τ-ai streaming: Streaming event protocol for LLM chat completion.

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.
PHASE-1-SUBPHASE-3.md — Streaming Protocol and Client.

Streaming events:
    - TextDeltaEvent: text content arriving in the stream
    - ToolCallDeltaEvent: tool call data arriving in the stream
    - DoneEvent: stream complete with final message and usage
    - ErrorEvent: an error occurred during the stream

AssistantMessageEventStream:
    Async iterator that yields the above event types.
    Collects provider stream chunks in a background coroutine,
    and exposes the fully accumulated message via .result().

Usage:
    stream = await stream_simple(model, context, options)
    async for event in stream:
        if event.type == "text_delta":
            print(event.delta, end="")
        elif event.type == "toolcall_delta":
            print(f"\\nTool: {event.tool_call.name}")
    final = await stream.result()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from tau_ai.json_parse import parse_streaming_json
from tau_ai.types import AssistantMessage, TextContent, ThinkingContent, ToolCall, Usage

# Forward reference for type hints
try:
    from types import UnionType
    _HAS_UNION_TYPE = True
except ImportError:
    _HAS_UNION_TYPE = False


@dataclass
class TextDeltaEvent:
    """A text delta event from the LLM stream.

    Carries a partial text chunk and the partially accumulated message.
    The consumer should append delta to the partial message's text content.

    Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

    Attributes:
        type: Always "text_delta".
        delta: The text chunk from this event.
        partial: The partially accumulated AssistantMessage.
    """
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = "text_delta"


@dataclass
class ThinkingDeltaEvent:
    """A thinking/reasoning delta event from the LLM stream.

    Mirrors :class:`TextDeltaEvent` but carries *reasoning* content — the
    OpenAI-compatible ``reasoning_content`` / ``reasoning`` / ``reasoning_text``
    delta fields (llama.cpp, vLLM, DeepSeek, OpenRouter). Kept as a distinct
    event so consumers can render reasoning separately from the answer and
    collapse it once the answer/tool content begins.

    Reference: pi ``openai-completions.ts`` ``thinking_delta`` event.

    Attributes:
        type: Always "thinking_delta".
        delta: The reasoning chunk from this event.
        partial: The partially accumulated AssistantMessage.
    """
    delta: str
    partial: AssistantMessage
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ToolCallDeltaEvent:
    """A tool call delta event from the LLM stream.

    Carries a partial tool call update and the partially accumulated message.
    Multiple deltas for the same tool call are accumulated until DoneEvent.

    Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

    Attributes:
        type: Always "toolcall_delta".
        delta: The OpenAI-style tool call delta dict.
        partial: The partially accumulated AssistantMessage.
    """
    delta: dict[str, Any]
    partial: AssistantMessage
    type: Literal["toolcall_delta"] = "toolcall_delta"


@dataclass
class DoneEvent:
    """A done event signaling the stream is complete.

    Carries the fully accumulated AssistantMessage and token usage information.

    Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

    Attributes:
        type: Always "done".
        final: The fully accumulated AssistantMessage.
        usage: Token usage information for the response.
    """
    final: AssistantMessage
    usage: Usage
    type: Literal["done"] = "done"


@dataclass
class ErrorEvent:
    """An error event from the LLM stream.

    Carries an error message. When the stream produces an ErrorEvent,
    no further events will be produced.

    Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

    Attributes:
        type: Always "error".
        message: Description of the error.
        is_error: Always True.
    """
    message: str
    is_error: Literal[True] = True
    type: Literal["error"] = "error"


class AssistantMessageEventStream:
    """Async iterator over streaming events from the LLM.

    Yields: TextDeltaEvent, ToolCallDeltaEvent, DoneEvent, ErrorEvent

    This class wraps an underlying provider stream (an async iterator of
    dicts) and produces typed StreamEvent objects. A background ``_collect``
    coroutine processes the provider stream chunks and puts events into an
    internal queue.  The main coroutine yields events from that queue.

    Usage:
        stream = await stream_simple(model, context, options)
        async for event in stream:
            if event.type == "text_delta":
                print(event.delta, end="")
            elif event.type == "toolcall_delta":
                print(f"\\nTool: {event.delta.get('id', '')}")
        final = await stream.result()

    Attributes:
        _provider_stream: The underlying provider async iterator.
        _model: The Model configuration.
        _context: The context dict (messages, tools, system_prompt).
        _partial: Accumulating AssistantMessage.
        _done: Whether the stream has completed.
        _final: The final AssistantMessage (set on DoneEvent).
        _usage: Token usage information.
        _error: Error message if an error occurred.
        _event_queue: Internal asyncio.Queue for event distribution.
        _collector_task: Background coroutine processing the provider stream.
    """

    def __init__(
        self,
        provider_stream: AsyncIterator[Any],
        model: Any,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the event stream.

        Args:
            provider_stream: Async iterator yielding raw provider dicts.
            model: The Model configuration.
            context: Context dict with messages, tools, etc.
        """
        self._provider_stream = provider_stream
        self._model = model
        self._context = context or {}
        self._partial: AssistantMessage | None = None
        self._done = False
        self._final: AssistantMessage | None = None
        self._usage = Usage()
        self._error: str | None = None
        self._event_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._collector_task: asyncio.Task[None] | None = None
        # Raw-dict accumulation path only: per-index tool-call block and the
        # growing raw argument-fragment buffer (concatenated, then parsed).
        self._tool_blocks_by_index: dict[int, ToolCall] = {}
        self._tool_args_by_index: dict[int, str] = {}

    def __aiter__(self) -> "AssistantMessageEventStream":
        """Return self as the async iterator."""
        return self

    async def _ensure_collector(self) -> None:
        """Lazily start the collector task if not already running."""
        if self._collector_task is None or self._collector_task.done():
            self._collector_task = asyncio.create_task(self._collect())

    async def __anext__(self) -> Any:
        """Yield the next event from the internal queue.

        If the stream is done and the queue is empty, raises
        StopAsyncIteration.

        Returns:
            The next StreamEvent (TextDeltaEvent, ToolCallDeltaEvent,
            DoneEvent, or ErrorEvent).

        Raises:
            StopAsyncIteration: When the stream is complete.
        """
        # Start the collector task lazily on first access
        if self._collector_task is None:
            await self._ensure_collector()

        if self._done and self._event_queue.empty():
            raise StopAsyncIteration
        event = await self._event_queue.get()
        return event

    async def _collect(self) -> None:
        """Background coroutine that processes the provider stream.

        Iterates over the provider stream, processes each chunk,
        and puts events into the internal queue.  When done, puts a
        DoneEvent.  On error, puts an ErrorEvent.
        """
        try:
            async for chunk in self._provider_stream:
                processed = await self._process_chunk(chunk)
                # If the provider yielded its own DoneEvent or ErrorEvent,
                # we should not emit our own wrapper. Otherwise keep going.
                if self._done:
                    break
            # Only emit our own DoneEvent if the provider didn't.
            if not self._done:
                self._final = self._partial
                await self._event_queue.put(DoneEvent(
                    type="done",
                    final=self._final,
                    usage=self._usage,
                ))
                self._done = True
        except Exception as e:
            await self._event_queue.put(ErrorEvent(
                type="error",
                message=str(e),
                is_error=True,
            ))
            self._done = True

    async def _process_chunk(self, chunk: Any) -> bool:
        """Process a single provider stream chunk or event.

        If *chunk* is already a StreamEvent (TextDeltaEvent, ToolCallDeltaEvent,
        DoneEvent, ErrorEvent) it is forwarded directly to the queue and
        returns ``True`` so that ``_collect`` does not emit its own wrapper.

        If *chunk* is a raw dict (OpenAI format) it is processed into typed
        events and returns ``False``.

        Args:
            chunk: A dict (OpenAI streaming chunk) or a StreamEvent object.

        Returns:
            True if the chunk was already a StreamEvent (forwarded),
            False if it was processed as a raw chunk.
        """
        # Check if chunk is already a StreamEvent dataclass
        if hasattr(chunk, "type") and chunk.type in (
            "text_delta", "thinking_delta", "toolcall_delta", "done", "error",
        ):
            await self._event_queue.put(chunk)
            # Update state from forwarded events
            if chunk.type == "done":
                self._final = chunk.final
                self._usage = chunk.usage
                self._done = True
            elif chunk.type == "error":
                self._error = chunk.message
                self._done = True
            elif hasattr(chunk, "partial") and chunk.partial is not None:
                self._partial = chunk.partial
            return True

        # Otherwise treat as raw dict chunk
        if not isinstance(chunk, dict):
            return False

        delta = chunk.get("delta", {})

        if "content" in delta and delta["content"]:
            partial = self._partial or self._make_empty_partial()
            await self._event_queue.put(TextDeltaEvent(
                type="text_delta",
                delta=delta["content"],
                partial=partial,
            ))
            if self._partial is None:
                self._partial = partial
            # Accumulate text content
            self._partial.content.append(TextContent(text=delta["content"]))

        # Reasoning: first non-empty of the OpenAI-compatible field names.
        reasoning = ""
        for _field in ("reasoning_content", "reasoning", "reasoning_text"):
            _val = delta.get(_field)
            if isinstance(_val, str) and _val:
                reasoning = _val
                break
        if reasoning:
            partial = self._partial or self._make_empty_partial()
            await self._event_queue.put(ThinkingDeltaEvent(
                type="thinking_delta",
                delta=reasoning,
                partial=partial,
            ))
            if self._partial is None:
                self._partial = partial
            self._partial.content.append(ThinkingContent(thinking=reasoning))

        if "tool_calls" in delta:
            for tc_delta in delta["tool_calls"]:
                idx = tc_delta.get("index", 0)
                await self._handle_tool_call_delta(tc_delta, idx)

        return False

    def _make_empty_partial(self) -> AssistantMessage:
        """Create a minimal AssistantMessage for the _partial field."""
        return AssistantMessage(
            content=[],
            api=self._model.api if hasattr(self._model, "api") else "openai-completions",
            provider=self._model.provider if hasattr(self._model, "provider") else "openai",
            model=self._model.id if hasattr(self._model, "id") else "unknown",
            usage=Usage(),
            stop_reason="stop",
            timestamp=0,
        )

    async def _handle_tool_call_delta(self, delta: dict, index: int) -> None:
        """Handle a tool call delta, accumulating arguments into _partial.

        Args:
            delta: OpenAI-style tool call delta dict.
            index: The index of this tool call.
        """
        if self._partial is None:
            self._partial = self._make_empty_partial()

        tc_id = delta.get("id", "") or ""
        func = delta.get("function") or {}
        tc_name = func.get("name") or ""
        tc_args = func.get("arguments") or ""

        # Resolve (or create) the block for this call. Follow-up fragments carry
        # only `index`, so the stream index is the stable key.
        tc_block = self._tool_blocks_by_index.get(index)
        if tc_block is None:
            tc_block = ToolCall(id=tc_id, name="", arguments={})
            self._tool_blocks_by_index[index] = tc_block
            self._tool_args_by_index[index] = ""
            self._partial.content.append(tc_block)
        if tc_id and not tc_block.id:
            tc_block.id = tc_id

        # Name and arguments stream as fragments — concatenate. The argument
        # buffer is parsed best-effort for live display (no fabricated values).
        if tc_name:
            tc_block.name += tc_name
        if tc_args:
            self._tool_args_by_index[index] += tc_args
            tc_block.arguments = parse_streaming_json(self._tool_args_by_index[index])

        await self._event_queue.put(ToolCallDeltaEvent(
            type="toolcall_delta",
            delta=delta,
            partial=self._partial,
        ))

    async def result(self) -> AssistantMessage:
        """Wait for the stream to complete and return the final message.

        Returns:
            The fully accumulated AssistantMessage.

        Raises:
            Exception: If the stream produced an ErrorEvent.
        """
        # Ensure the collector task is running (may have been called
        # directly without iterating the stream first).
        await self._ensure_collector()
        if not self._done:
            await self._wait_for_done()
        if self._error:
            raise Exception(self._error)
        return self._final

    async def _wait_for_done(self) -> None:
        """Wait for the DoneEvent from the internal queue.

        Drains the queue until a DoneEvent or ErrorEvent arrives.
        If the collector task has already produced a DoneEvent (e.g.
        because the stream was consumed via async-for), _done will
        already be True and this returns immediately.
        """
        while not self._done:
            event = await self._event_queue.get()
            if event.type == "done":
                self._done = True
                self._final = event.final
                self._usage = event.usage
                return
            elif event.type == "error":
                self._error = event.message
                self._done = True
                return
            # Any other event type is stored in the queue for iteration;
            # keep waiting for done/error.


    def abort(self) -> None:
        """Abort the stream by propagating to the underlying provider.

        Calls abort() on the provider stream if it has one.  The partial
        state is preserved even after abort.
        """
        if hasattr(self._provider_stream, "abort"):
            self._provider_stream.abort()
        # Cancel collector task if running
        if self._collector_task and not self._collector_task.done():
            self._collector_task.cancel()

