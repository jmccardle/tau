"""τ-llm: Unified LLM provider abstraction.

Core types: UserMessage, AssistantMessage, ToolResultMessage, etc.
Tools: ToolDefinition, define_tool, validate_tool_arguments
Abort: AbortSignal for async cancellation
Providers: Provider ABC
Client: stream_simple() / complete_simple() / aclose_providers()
"""

# This distribution's version, read at build time by pyproject.toml's
# [tool.setuptools.dynamic]. Kept in lockstep with the other three packages.
__version__ = "0.9.4"

from tau_llm.types import (
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
from tau_llm.models import (
    DEFAULT_THINKING_LEVEL,
    EXTENDED_THINKING_LEVELS,
    clamp_thinking_level,
    get_supported_thinking_levels,
    is_valid_thinking_level,
)
from tau_llm.compat import Compat, ResolvedCompat, detect_compat, resolve_compat
from tau_llm.tools import ToolDefinition, ToolSpec, define_tool, validate_tool_arguments
from tau_llm.abort import AbortSignal
from tau_llm.constraints import ConstraintViolation, DecodeConstraints
from tau_llm import grammar
from tau_llm.providers.base import Provider
from tau_llm.client import aclose_providers, complete_simple, stream_simple

__all__ = [
    "__version__",
    # Constrained decoding
    "DecodeConstraints",
    "ConstraintViolation",
    "grammar",
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
    # Endpoint wire quirks. `tau_llm.catalog` is deliberately NOT re-exported
    # here: it imports httpx on use and is an operator tool rather than part of
    # the request path, so `import tau_llm` should not pull it in.
    "Compat",
    "ResolvedCompat",
    "detect_compat",
    "resolve_compat",
    # Tools — ToolDefinition is what define_tool returns, and the package
    # docstring has always named it; it was missing from __all__ only because
    # define_tool was a stub nobody could call.
    "ToolDefinition",
    "define_tool",
    "ToolSpec",
    "validate_tool_arguments",
    # Abort
    "AbortSignal",
    # Providers
    "Provider",
    # Client
    "stream_simple",
    "complete_simple",
    "aclose_providers",
]
