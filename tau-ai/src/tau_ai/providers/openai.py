"""τ-ai providers.openai: OpenAI-completions provider.

Reference: PHASE-1-SUBPHASE-2.md, Phase 1 Subphase 2 — OpenAI Provider Implementation.

Implements OpenAICompletionsProvider, the only concrete provider. It:
1. Converts τ Message list to OpenAI API format
2. Converts τ ToolDefinition to OpenAI function_call format
3. Converts OpenAI API responses back to τ AssistantMessage
4. Handles all error cases

Usage:
    provider = OpenAICompletionsProvider()
    stream = await provider.stream_chat(
        model=Model(id="gpt-4o", ...),
        messages=[UserMessage(content=[TextContent(text="hello")])],
    )
    async for event in stream:
        if event.type == "text_delta":
            print(event.delta, end="")
        elif event.type == "done":
            print(f"\nUsage: {event.usage}")
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

import httpx

from tau_ai.providers.base import Provider
from tau_ai.json_parse import parse_json_with_repair, parse_streaming_json
from tau_ai.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_ai.tools import ToolDefinition
from tau_ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)


@dataclass
class _ToolCallAccumulator:
    """Accumulates a single tool call across delta events.

    ``name`` and ``arguments_parts`` are accumulated by concatenation: OpenAI
    streams them as incremental fragments, one piece per chunk.
    """
    id: str = ""
    name: str = ""
    index: int | None = None
    arguments_parts: list[str] = field(default_factory=list)


@dataclass
class _Accumulator:
    """Internal accumulator for building AssistantMessage during streaming.

    Accumulates text deltas, tool call arguments, and metadata
    across streaming events until the response is complete.

    Tool calls are kept in first-seen order (``tool_calls``) and indexed by both
    OpenAI stream ``index`` and tool-call ``id`` so that follow-up argument
    fragments — which carry only ``index`` — route to the right call.
    """
    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls: list[_ToolCallAccumulator] = field(default_factory=list)
    by_index: dict[int, _ToolCallAccumulator] = field(default_factory=dict)
    by_id: dict[str, _ToolCallAccumulator] = field(default_factory=dict)
    has_tool_calls: bool = False
    has_text: bool = False
    has_thinking: bool = False
    response_id: str | None = None


def _resolve_tool_call_block(
    accum: _Accumulator, tc_delta: dict, fallback_index: int
) -> _ToolCallAccumulator:
    """Find or create the accumulator for a streaming tool-call delta.

    OpenAI sends ``id``+``name`` only on a call's first delta; later deltas carry
    only ``index`` plus an arguments fragment. Resolve by ``index`` first (the
    stable key across fragments), then by ``id`` — mirroring pi's
    ``ensureToolCallBlock``. ``fallback_index`` (the position within this chunk's
    ``tool_calls`` array) is used only when the server omits ``index``.
    """
    raw_index = tc_delta.get("index")
    index = raw_index if isinstance(raw_index, int) else fallback_index
    tc_id = tc_delta.get("id") or ""

    block: _ToolCallAccumulator | None = None
    if index is not None and index in accum.by_index:
        block = accum.by_index[index]
    if block is None and tc_id and tc_id in accum.by_id:
        block = accum.by_id[tc_id]

    if block is None:
        block = _ToolCallAccumulator(id=tc_id, index=index)
        accum.tool_calls.append(block)
        if index is not None:
            accum.by_index[index] = block
        if tc_id:
            accum.by_id[tc_id] = block
        return block

    if index is not None and block.index is None:
        block.index = index
        accum.by_index[index] = block
    if tc_id and not block.id:
        block.id = tc_id
        accum.by_id[tc_id] = block
    return block


class AssistantMessageEventStream:
    """Async iterator that yields streaming events and exposes the final result.

    This class implements the return type for OpenAICompletionsProvider.stream_chat().
    It yields TextDeltaEvent, ToolCallDeltaEvent, and DoneEvent instances.

    The final result is available via .result() after the stream completes.
    """

    def __init__(self, event_iter: AsyncIterator[Any]) -> None:
        self._event_iter = event_iter
        self._result: AssistantMessage | None = None
        self._done = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._event_iter

    async def result(self) -> AssistantMessage:
        """Return the final AssistantMessage after the stream completes."""
        if not self._done:
            async for _ in self._event_iter:
                pass
            self._done = True
        if self._result is None:
            raise RuntimeError("No final result available — stream did not produce a DoneEvent")
        return self._result


class OpenAICompletionsProvider(Provider):
    """Provider for OpenAI-compatible APIs (OpenAI, Ollama, vLLM, etc.).

    This is the only concrete provider in τ. It converts τ types to/from
    OpenAI API format and handles streaming responses.

    Reference: PHASE-1-SUBPHASE-2.md, "Implementation Outline" section.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """Initialize the OpenAI provider.

        Args:
            api_key: OpenAI API key. If None, reads from OPENAI_API_KEY env var.
            base_url: Custom API base URL. Defaults to OpenAI production URL.
        """
        import os

        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-fake-key-for-testing")
        self.base_url = base_url or "https://api.openai.com/v1"
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        return self._client

    # ──────────────────────────────────────────────────────────────────
    # Conversion: τ → OpenAI
    # ──────────────────────────────────────────────────────────────────

    def _convert_messages_to_openai(self, messages: list) -> list[dict]:
        """Convert τ messages to OpenAI API message format.

        Conversion rules:
        - UserMessage → {"role": "user", "content": [...]}
          - TextContent → {"type": "text", "text": ...}
          - ImageContent → {"type": "image_url", "image_url": {"url": "data:{mime};base64,{data}"}}
        - AssistantMessage → {"role": "assistant", "content": ..., "tool_calls": ...}
          - Text-only content → {"content": "..."}
          - Tool calls in content → {"tool_calls": [...]}
        - ToolResultMessage → {"role": "tool", "tool_call_id": ..., "content": ...}
        - ThinkingContent → included in content field (OpenAI handles as text)

        Reference: SUBPHASE-0.0.md, "1. Messages" section.

        Args:
            messages: List of τ message objects.

        Returns:
            List of OpenAI-format message dicts.
        """
        openai_messages: list[dict] = []

        for msg in messages:
            if isinstance(msg, UserMessage):
                openai_messages.append(self._convert_user_message(msg))
            elif isinstance(msg, AssistantMessage):
                openai_messages.append(self._convert_assistant_message(msg))
            elif isinstance(msg, ToolResultMessage):
                openai_messages.append(self._convert_tool_result(msg))
            elif isinstance(msg, dict):
                # Convert via _convert_message_dict to handle toolResult → tool,
                # content list → string, etc.
                openai_messages.append(self._convert_message_dict(msg))
            else:
                # Try to convert via model_dump
                if hasattr(msg, "model_dump"):
                    d = msg.model_dump()
                    openai_messages.append(self._convert_message_dict(d))
                else:
                    openai_messages.append({"role": "user", "content": str(msg)})

        return openai_messages

    def _convert_user_message(self, msg: UserMessage) -> dict:
        """Convert UserMessage to OpenAI format."""
        content = msg.content
        if isinstance(content, str):
            return {"role": "user", "content": [{"type": "text", "text": content}]}

        blocks: list[dict] = []
        for block in content:
            if isinstance(block, TextContent):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                b64_data = self._encode_image(block)
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{block.mime_type};base64,{b64_data}"},
                })
            elif isinstance(block, dict):
                blocks.append(block)

        return {"role": "user", "content": blocks if blocks else ""}

    def _encode_image(self, img: ImageContent) -> str:
        """Encode image data as base64 string.

        The data field is assumed to be already base64-encoded.
        We only strip the data: URI prefix if present.
        """
        if img.data.startswith("data:"):
            # Already has data URI prefix, strip it
            data_part = img.data.split(",", 1)[1] if "," in img.data else img.data
            return data_part
        # Return the data as-is (assumed to be base64-encoded)
        return img.data

    def _convert_assistant_message(self, msg: AssistantMessage) -> dict:
        """Convert AssistantMessage to OpenAI format.

        Handles both text-only and tool-call content.
        """
        content_blocks = msg.content
        tool_calls_blocks = [c for c in content_blocks if isinstance(c, ToolCall)]
        text_blocks = [c for c in content_blocks if isinstance(c, TextContent)]
        thinking_blocks = [c for c in content_blocks if isinstance(c, ThinkingContent)]

        result: dict[str, Any] = {"role": "assistant"}

        # Text content
        text_parts = [b.text for b in text_blocks]
        thinking_parts = [b.thinking for b in thinking_blocks]

        if text_parts:
            result["content"] = "\n".join(text_parts)
        elif thinking_parts:
            # Thinking content is included in the content field
            result["content"] = "\n".join(thinking_parts)
        else:
            result["content"] = ""

        # Tool calls
        if tool_calls_blocks:
            result["tool_calls"] = []
            for tc in tool_calls_blocks:
                tool_call_dict = {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                result["tool_calls"].append(tool_call_dict)

        return result

    def _convert_tool_result(self, msg: ToolResultMessage) -> dict:
        """Convert ToolResultMessage to OpenAI tool role format."""
        # Join all text content blocks
        content_parts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                content_parts.append(block.text)
            elif isinstance(block, str):
                content_parts.append(block)

        result: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": " ".join(content_parts),
        }

        return result

    def _convert_message_dict(self, d: dict) -> dict:
        """Convert a generic dict message to OpenAI format.

        Handles toolResult → tool role conversion, extracts tool_call_id,
        and converts list-type content to a string.
        """
        role = d.get("role", "")
        content = d.get("content", "")

        if role in ("user", "assistant"):
            return {"role": role, "content": content}
        elif role in ("toolResult", "tool"):
            # Extract tool_call_id
            tool_call_id = d.get("tool_call_id", "")

            # Convert list-type content to string (e.g. [{"type": "text", "text": "..."}])
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif "content" in block:
                            # Nested content block
                            text_parts.append(str(block["content"]))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = " ".join(text_parts)

            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content if isinstance(content, str) else "",
            }
        else:
            return {"role": role, "content": content}

    def _convert_tools_to_openai(self, tools: list[ToolDefinition]) -> list[dict]:
        """Convert τ tool definitions to OpenAI function format.

        Conversion:
        ToolDefinition.parameters → functions[].parameters (JSON Schema)
        ToolDefinition.description → functions[].description
        ToolDefinition.name → functions[].name

        Reference: PHASE-1-SUBPHASE-2.md, "Tools → OpenAI" section.

        Args:
            tools: List of τ ToolDefinition objects.

        Returns:
            List of OpenAI-format tool dicts.
        """
        openai_tools: list[dict] = []

        for tool in tools:
            openai_tool: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            openai_tools.append(openai_tool)

        return openai_tools

    # ──────────────────────────────────────────────────────────────────
    # Conversion: OpenAI → τ
    # ──────────────────────────────────────────────────────────────────

    def _convert_openai_choice_to_message(self, choice: dict) -> AssistantMessage:
        """Convert OpenAI choice to τ AssistantMessage.

        Handles streaming delta accumulation into a final message.

        Reference: PHASE-1-SUBPHASE-2.md, "OpenAI Choice → τ Message" section.

        Args:
            choice: OpenAI choice dict with delta and finish_reason.

        Returns:
            AssistantMessage with accumulated content.
        """
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Build the final message from accumulated data
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        # Extract text content
        content = delta.get("content", "")
        if content:
            text_parts.append(content)

        # Extract reasoning/thinking
        reasoning = delta.get("reasoning", "")
        if reasoning:
            thinking_parts.append(reasoning)

        # Extract tool calls
        deltas = delta.get("tool_calls", [])
        if deltas:
            for tc_delta in deltas:
                tc_id = tc_delta.get("id", "")
                tc_name = tc_delta.get("function", {}).get("name", "")
                tc_args = tc_delta.get("function", {}).get("arguments", "")
                if tc_id and tc_name:
                    args_dict = parse_streaming_json(tc_args)
                    tool_calls.append(ToolCall(
                        id=tc_id,
                        name=tc_name,
                        arguments=args_dict,
                    ))

        # Build content blocks
        content_blocks: list[Any] = []
        content_blocks.extend([TextContent(type="text", text=t) for t in text_parts])
        content_blocks.extend([
            ThinkingContent(type="thinking", thinking=t) for t in thinking_parts
        ])
        content_blocks.extend(tool_calls)

        model_id = delta.get("model", "unknown")
        response_id = choice.get("message_id", choice.get("id", "unknown"))

        return AssistantMessage(
            content=content_blocks,
            api="openai-completions",
            provider="openai",
            model=model_id,
            response_id=response_id,
            usage=Usage(),
            stop_reason=self._map_finish_reason(finish_reason),
            timestamp=0,
        )

    def _map_finish_reason(self, reason: str | None) -> Literal["stop", "length", "toolUse", "error", "aborted"]:
        """Map OpenAI finish_reason to τ stop_reason."""
        mapping = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "toolUse",
            "content_filter": "stop",
            None: "stop",
        }
        return mapping.get(reason, "stop")

    # ──────────────────────────────────────────────────────────────────
    # Streaming: event production
    # ──────────────────────────────────────────────────────────────────

    def _make_text_event(
        self, text: str, accum: _Accumulator, partial: AssistantMessage
    ) -> TextDeltaEvent:
        """Create a TextDeltaEvent from accumulated text."""
        return TextDeltaEvent(
            type="text_delta",
            delta=text,
            partial=partial,
        )

    def _make_toolcall_event(
        self, delta: dict, accum: _Accumulator, partial: AssistantMessage
    ) -> ToolCallDeltaEvent:
        """Create a ToolCallDeltaEvent from tool call delta."""
        return ToolCallDeltaEvent(
            type="toolcall_delta",
            delta=delta,
            partial=partial,
        )

    def _build_partial_message(self, accum: _Accumulator, model: Model) -> AssistantMessage:
        """Build a partial AssistantMessage from the current accumulation state."""
        content_blocks: list[Any] = []

        for text in accum.text_parts:
            content_blocks.append(TextContent(type="text", text=text))

        for thinking in accum.thinking_parts:
            content_blocks.append(ThinkingContent(type="thinking", thinking=thinking))

        for tc in accum.tool_calls:
            # Display path: arguments may still be mid-stream, so parse
            # leniently (best-effort, {} until enough has arrived).
            args_dict = parse_streaming_json("".join(tc.arguments_parts))
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        return AssistantMessage(
            content=content_blocks,
            api="openai-completions",
            provider="openai",
            model=model.id,
            response_id=accum.response_id,
            usage=Usage(),
            stop_reason="stop",
            timestamp=0,
        )

    def _build_final_message(
        self,
        accum: _Accumulator,
        model: Model,
        usage: Usage,
        stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = None,
    ) -> AssistantMessage:
        """Build the final AssistantMessage with accumulated data."""
        content_blocks: list[Any] = []

        for text in accum.text_parts:
            content_blocks.append(TextContent(type="text", text=text))

        for thinking in accum.thinking_parts:
            content_blocks.append(ThinkingContent(type="thinking", thinking=thinking))

        for tc in accum.tool_calls:
            args_str = "".join(tc.arguments_parts)
            # Authoritative path: the stream is complete, so the arguments must
            # be valid JSON. A complete-but-unparseable payload is a real error
            # — raise (surfaced as an ErrorEvent) rather than fabricate args.
            if args_str.strip():
                args_dict = parse_json_with_repair(args_str)
                if not isinstance(args_dict, dict):
                    raise ValueError(
                        f"Tool call {tc.id!r} ({tc.name!r}) arguments did not decode "
                        f"to a JSON object: {args_str!r}"
                    )
            else:
                args_dict = {}
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        # Determine stop_reason: use explicit value, or fall back to heuristic
        if stop_reason is None:
            if accum.has_tool_calls:
                stop_reason = "toolUse"
            else:
                stop_reason = "stop"

        return AssistantMessage(
            content=content_blocks,
            api="openai-completions",
            provider="openai",
            model=model.id,
            response_id=accum.response_id,
            usage=usage,
            stop_reason=stop_reason,
            timestamp=0,
        )

    # ──────────────────────────────────────────────────────────────────
    # Main interface: stream_chat
    # ──────────────────────────────────────────────────────────────────

    async def stream_chat(
        self,
        model: Model,
        messages: list,
        tools: list[ToolDefinition] | None = None,
        options: dict | None = None,
    ) -> AssistantMessageEventStream:
        """Stream chat completions from OpenAI-compatible API.

        Converts τ messages to OpenAI format, streams the response,
        and produces τ streaming events.

        Reference: PHASE-1-SUBPHASE-2.md, "Streaming event production" section.

        Args:
            model: The Model to use for the request.
            messages: List of τ message objects.
            tools: Optional list of tool definitions.
            options: Optional provider-specific options (temperature, max_tokens, etc.).

        Returns:
            AssistantMessageEventStream yielding TextDeltaEvent, ToolCallDeltaEvent,
            DoneEvent, and ErrorEvent instances.
        """
        if options is None:
            options = {}

        # Convert τ messages to OpenAI format
        openai_messages = self._convert_messages_to_openai(messages)

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = self._convert_tools_to_openai(tools)

        # Build request payload
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": openai_messages,
            "stream": True,
            **options,
        }
        if openai_tools:
            payload["tools"] = openai_tools

        client = self._get_client()
        accum = _Accumulator()

        async def event_generator() -> AsyncIterator[Any]:
            try:
                response = await client.post("/chat/completions", json=payload)

                if response.status_code != 200:
                    error_body = {}
                    try:
                        error_body = response.json()
                    except Exception:
                        pass
                    error_msg = error_body.get("error", {}).get("message", f"HTTP {response.status_code}")
                    error_event = ErrorEvent(
                        type="error",
                        message=f"HTTP {response.status_code}: {error_msg}",
                        is_error=True,
                    )
                    yield error_event
                    return

                response_id = response.headers.get("x-request-id", "unknown")
                usage_data: dict[str, Any] = {}

                # Handle streaming response — read SSE lines async
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")
                    # usage may be at chunk level or inside choice
                    usage = chunk.get("usage") or choice.get("usage")

                    accum.response_id = chunk.get("id", response_id)

                    # Track response_id from any chunk
                    if chunk.get("id"):
                        accum.response_id = chunk["id"]

                    # Accumulate usage from chunks (last chunk has the full usage)
                    if usage:
                        usage_data = usage

                    # Process text delta
                    text = delta.get("content", "") or ""
                    if text:
                        accum.text_parts.append(text)
                        accum.has_text = True
                        partial = self._build_partial_message(accum, model)
                        yield self._make_text_event(text, accum, partial)

                    # Process reasoning/thinking delta
                    reasoning = delta.get("reasoning", "") or ""
                    if reasoning:
                        accum.thinking_parts.append(reasoning)
                        partial = self._build_partial_message(accum, model)

                    # Process tool call deltas. OpenAI streams name and arguments
                    # as incremental FRAGMENTS, one piece per chunk — concatenate
                    # them. Route each fragment to its call by stream `index`
                    # (falling back to `id`), since follow-up argument fragments
                    # carry only the index.
                    deltas = delta.get("tool_calls", [])
                    for i, tc_delta in enumerate(deltas):
                        block = _resolve_tool_call_block(accum, tc_delta, i)
                        func = tc_delta.get("function") or {}
                        tc_name = func.get("name") or ""
                        if tc_name:
                            block.name += tc_name
                        tc_args = func.get("arguments") or ""
                        if tc_args:
                            block.arguments_parts.append(tc_args)
                        accum.has_tool_calls = True

                        partial = self._build_partial_message(accum, model)
                        yield self._make_toolcall_event(tc_delta, accum, partial)

                    # Handle finish_reason
                    if finish_reason:
                        usage_obj = Usage(**usage_data) if usage_data else Usage()
                        # Use the actual finish_reason to set stop_reason
                        stop_reason = self._map_finish_reason(finish_reason)
                        final_msg = self._build_final_message(
                            accum, model, usage_obj, stop_reason
                        )

                        # Emit one final tool-call delta per call, derived from the
                        # already-parsed ToolCall blocks on final_msg (no re-parse).
                        if accum.has_tool_calls:
                            for pos, tc_block in enumerate(final_msg.get_tool_calls()):
                                tc_delta = {
                                    "index": pos,
                                    "id": tc_block.id,
                                    "function": {
                                        "name": tc_block.name,
                                        "arguments": json.dumps(tc_block.arguments),
                                    },
                                }
                                yield ToolCallDeltaEvent(
                                    type="toolcall_delta",
                                    delta=tc_delta,
                                    partial=final_msg,
                                )

                        # Yield done event
                        yield DoneEvent(
                            type="done",
                            final=final_msg,
                            usage=usage_obj,
                        )
                        return

            except Exception as e:
                error_event = ErrorEvent(
                    type="error",
                    message=f"Streaming error: {str(e)}",
                    is_error=True,
                )
                yield error_event
                return

        return AssistantMessageEventStream(event_generator())
