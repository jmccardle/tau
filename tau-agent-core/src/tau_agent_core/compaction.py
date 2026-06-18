"""τ-agent-core compaction: session compaction types and configuration.

Reference: PHASE-5-SUBPHASE-0.md
Reference: docs/tau-agent-core.md lines 350-450
Reference: docs/IMPLEMENTATION-PLAN.md lines 360-420
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tau_ai.types import Model
from tau_agent_core.session import SessionEntry


@dataclass
class CompactionConfig:
    """Configuration for a session compaction operation.

    Attributes:
        model: The LLM model to use for generating the summary
        system_prompt: System prompt for the compaction LLM call
        max_context_tokens: Maximum context window size for the model
        margin: Tokens to keep as margin before hitting the context limit
        custom_instructions: Optional custom instructions for the compaction
        compact_callback: Optional async callback for progress updates
    """

    model: Model
    system_prompt: str
    max_context_tokens: int
    margin: int  # tokens to keep as margin before hitting max
    custom_instructions: str | None = None
    compact_callback: Callable[[str, int], Awaitable[None]] | None = None  # for progress


@dataclass
class CompactionResult:
    """Result of a compaction operation.

    Attributes:
        summary: The LLM-generated summary of compacted messages
        first_kept_id: ID of the first message kept in full (after compaction)
        compacted_entry_ids: IDs of entries that were compacted into the summary
        tokens_saved: Estimated number of tokens saved by compaction
        tokens_before: Token count before compaction
        tokens_after: Token count after compaction
    """

    summary: str  # The LLM-generated summary
    first_kept_id: str  # ID of the first message kept in full
    compacted_entry_ids: list[str]  # IDs of entries that were compacted
    tokens_saved: int  # Estimated tokens saved
    tokens_before: int
    tokens_after: int


async def compact_session(
    config: CompactionConfig,
    entries: list[SessionEntry],
) -> CompactionResult:
    """Compact a session's messages into a summary.

    Placeholder implementation. Phase 5 will fill this in with
    actual LLM-based compaction logic.

    Args:
        config: Compaction configuration
        entries: Session entries to compact

    Returns:
        CompactionResult with summary and statistics
    """
    # Placeholder: return a minimal result
    return CompactionResult(
        summary="Session compaction summary",
        first_kept_id=entries[0].id if entries else "",
        compacted_entry_ids=[e.id for e in entries],
        tokens_saved=0,
        tokens_before=0,
        tokens_after=0,
    )


def estimate_tokens(entries: list[SessionEntry]) -> int:
    """Estimate the total token count for a list of session entries.

    Placeholder implementation. Phase 5 will implement
    proper token estimation.

    Args:
        entries: Session entries to estimate tokens for

    Returns:
        Estimated token count
    """
    # Simple character-based estimate: ~4 chars per token
    total_chars = sum(
        len(e.model_dump_json()) for e in entries
    )
    return total_chars // 4


def build_compaction_prompt(
    entries: list[SessionEntry],
    config: CompactionConfig,
) -> str:
    """Build the system prompt for compaction.

    Placeholder implementation. Phase 5 will build a proper
    prompt from the session entries.

    Args:
        entries: Session entries to summarize
        config: Compaction configuration

    Returns:
        System prompt string for the LLM
    """
    prompt = config.system_prompt
    if config.custom_instructions:
        prompt += f"\n\n{config.custom_instructions}"
    return prompt
