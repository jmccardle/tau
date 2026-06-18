"""τ-agent-core settings.

Configuration for the τ agent system, loaded from ~/.tau/settings.json.

Reference: PHASE-5-SUBPHASE-0.md
Reference: SUBPHASE-0.0.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """τ settings (from ~/.tau/settings.json).

    Attributes:
        default_model: Default model identifier for LLM calls
        thinking_level: Thinking mode ("off", "low", "high")
        compaction_enabled: Whether automatic session compaction is enabled
        context_margin: Token margin before hitting context limit
        extension_dirs: Directories to search for extension modules
        api_keys: Mapping of provider name to API key
        custom_system_prompt: Optional custom system prompt override
        tool_execution_mode: Default tool execution mode ("parallel", "sequential")
        max_retries: Maximum number of retries for failed LLM calls
        temperature: Default sampling temperature
        max_tokens: Maximum output tokens (None = provider default)
        reasoning_level: Reasoning mode ("off", "low", "high")
    """

    default_model: str = "gpt-4o"
    thinking_level: str = "off"
    compaction_enabled: bool = True
    context_margin: int = 2000
    extension_dirs: list[str] = field(
        default_factory=lambda: [str(Path.home() / ".tau" / "extensions")],
    )
    api_keys: dict[str, str] = field(default_factory=dict)
    custom_system_prompt: str | None = None
    tool_execution_mode: str = "parallel"
    max_retries: int = 3
    temperature: float = 0.7
    max_tokens: int | None = None
    reasoning_level: str = "off"
