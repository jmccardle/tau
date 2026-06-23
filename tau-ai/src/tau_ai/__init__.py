"""τ-ai: Unified LLM provider abstraction.

Core types: UserMessage, AssistantMessage, ToolResultMessage, etc.
Tools: ToolDefinition, define_tool, validate_tool_arguments
Abort: AbortSignal for async cancellation
Providers: Provider ABC, ProviderRegistry
Client: stream_simple() helper function
"""

from tau_ai.types import (
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    TextContent,
    ThinkingContent,
    ImageContent,
    ToolCall,
    Usage,
    Model,
)
from tau_ai.models import (
    DEFAULT_THINKING_LEVEL,
    EXTENDED_THINKING_LEVELS,
    clamp_thinking_level,
    get_supported_thinking_levels,
    is_valid_thinking_level,
)
from tau_ai.tools import define_tool, validate_tool_arguments
from tau_ai.abort import AbortSignal
from tau_ai.providers.base import Provider
from tau_ai.providers.registry import ProviderRegistry
from tau_ai.client import complete_simple, stream_simple

__all__ = [
    # Types
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "TextContent",
    "ThinkingContent",
    "ImageContent",
    "ToolCall",
    "Usage",
    "Model",
    # Thinking / reasoning levels
    "DEFAULT_THINKING_LEVEL",
    "EXTENDED_THINKING_LEVELS",
    "clamp_thinking_level",
    "get_supported_thinking_levels",
    "is_valid_thinking_level",
    # Tools
    "define_tool",
    "validate_tool_arguments",
    # Abort
    "AbortSignal",
    # Providers
    "Provider",
    "ProviderRegistry",
    # Client
    "stream_simple",
    "complete_simple",
]
