"""τ-ai streaming: Streaming event types for LLM chat completion.

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.

All streaming events are produced by stream_simple() and OpenAICompletionsProvider.stream_chat():
    - TextDeltaEvent: text content arriving in the stream
    - ToolCallDeltaEvent: tool call data arriving in the stream
    - DoneEvent: stream complete with final message and usage
    - ErrorEvent: an error occurred during the stream

Reference: SUBPHASE-0.0.md, "4. Streaming Events" section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from tau_ai.types import AssistantMessage, Usage


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
