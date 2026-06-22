"""τ-agent-core compaction: session compaction types and configuration.

Reference: PHASE-5-SUBPHASE-0.md
Reference: PHASE-5-SUBPHASE-1.md
Reference: docs/tau-agent-core.md lines 350-450
Reference: docs/IMPLEMENTATION-PLAN.md lines 360-420
"""

from __future__ import annotations

import time
import uuid
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


def write_compaction_entry(
    session_manager: Any,
    summary: str,
    first_kept_id: str,
    compacted_entry_ids: list[str],
    tokens_saved: int = 0,
) -> str:
    """Write a compaction entry to the session manager.

    Creates a compaction entry that replaces all entries before first_kept_id
    in the active path with a summary.

    Args:
        session_manager: SessionManager instance to write to
        summary: The compaction summary text
        first_kept_id: ID of the first entry to keep
        compacted_entry_ids: IDs of entries that were compacted
        tokens_saved: Estimated tokens saved

    Returns:
        The ID of the written compaction entry
    """
    entry = {
        "id": uuid.uuid4().hex,
        "type": "compaction",
        "timestamp": int(time.time() * 1000),
        "parent_id": None,
        "first_kept_id": first_kept_id,
        "summary": summary,
        "tokens_saved": tokens_saved,
        "compacted_entries": compacted_entry_ids,
    }
    entry_id: str = session_manager.append_entry(entry)
    return entry_id


async def compact_session(
    config: CompactionConfig,
    entries: list[SessionEntry],
    session_manager: Any | None = None,
) -> CompactionResult:
    """Compact a session's messages into a summary.

    Orchestrates the compaction process:
    1. Determines which entries to compact
    2. Builds the compaction prompt
    3. Calls the LLM to generate a summary
    4. Writes a compaction entry to the session
    5. Returns the result

    Args:
        config: Compaction configuration
        entries: Session entries to compact
        session_manager: Optional session manager for writing the entry

    Returns:
        CompactionResult with summary and statistics
    """
    # Estimate tokens before compaction
    tokens_before = estimate_tokens(entries)

    # Determine the split point: keep the last portion of entries
    # For the placeholder, we keep the last entry and compact the rest
    if len(entries) <= 1:
        # Nothing to compact
        return CompactionResult(
            summary="Session already compact or too short.",
            first_kept_id=entries[0].id if entries else "",
            compacted_entry_ids=[],
            tokens_saved=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
        )

    # Split: compact all but the last entry, keep the last entry
    first_kept_entry = entries[-1]
    to_compact = entries[:-1]
    compacted_ids = [e.id for e in to_compact]

    # Build the compaction prompt. NOTE: `prompt` is intentionally unused for now —
    # the "LLM call" below is still a placeholder that fabricates `summary` instead
    # of sending this prompt to a model. Wiring real LLM-backed compaction is a
    # tracked ROADMAP item, not a lint fix.
    prompt = build_compaction_prompt(to_compact, config)  # noqa: F841

    # Emit progress callback if provided
    if config.compact_callback:
        try:
            await config.compact_callback("Building compaction prompt", tokens_before)
        except Exception:
            pass  # Don't let callback errors break compaction

    # Placeholder LLM call: in production this would call the actual LLM
    # For now, generate a deterministic summary for testing
    summary = config.system_prompt + " - Compacted " + str(len(to_compact)) + " entries"

    # Estimate tokens after compaction (rough: summary + kept entry)
    tokens_after_estimate = len(summary) // 4 + estimate_tokens([first_kept_entry])
    tokens_saved = max(0, tokens_before - tokens_after_estimate)

    # Write compaction entry if session manager is provided
    if session_manager is not None:
        try:
            write_compaction_entry(
                session_manager=session_manager,
                summary=summary,
                first_kept_id=first_kept_entry.id,
                compacted_entry_ids=compacted_ids,
                tokens_saved=tokens_saved,
            )
        except Exception:
            pass  # Don't let write errors break compaction

    # Emit progress callback for completion
    if config.compact_callback:
        try:
            await config.compact_callback("Compaction complete", tokens_saved)
        except Exception:
            pass

    return CompactionResult(
        summary=summary,
        first_kept_id=first_kept_entry.id,
        compacted_entry_ids=compacted_ids,
        tokens_saved=tokens_saved,
        tokens_before=tokens_before,
        tokens_after=tokens_after_estimate,
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
    total_chars = sum(len(e.model_dump_json()) for e in entries)
    return total_chars // 4


def build_compaction_prompt(
    entries: list[SessionEntry],
    config: CompactionConfig,
) -> str:
    """Build the system prompt for compaction.

    Constructs the full compaction prompt including the system prompt,
    optional custom instructions, and conversation text from entries.

    Args:
        entries: Session entries to summarize
        config: Compaction configuration

    Returns:
        System prompt string for the LLM
    """
    prompt = config.system_prompt
    if config.custom_instructions:
        prompt += f"\n\n{config.custom_instructions}"

    # Append conversation text from entries
    conversation_parts: list[str] = []
    for entry in entries:
        # For SessionEntry objects, extract message text if available
        # In practice this would parse the entry types properly
        conversation_parts.append(entry.model_dump_json())

    if conversation_parts:
        prompt += "\n\nConversation before this point:\n"
        prompt += "\n".join(conversation_parts)

    prompt += "\n\nSummary:\n"
    return prompt


def should_compact(
    messages: list[Any],
    model_context_window: int,
    margin: int = 2000,
    estimated_tokens_per_message: int = 15,
) -> bool:
    """Check if the current context is approaching the model's context window.

    Returns True if compaction should be triggered.

    Uses a simple linear estimation: number_of_messages × estimated_tokens_per_message.

    Args:
        messages: List of message objects (any type with length)
        model_context_window: The model's context window size in tokens
        margin: Token margin to keep before hitting the context limit
        estimated_tokens_per_message: Estimated tokens per message

    Returns:
        True if compaction should be triggered
    """
    estimated_tokens = len(messages) * estimated_tokens_per_message
    available = model_context_window - margin
    return estimated_tokens >= available


def prepare_compaction(
    entries: list[dict],
    first_kept_entry_id: str,
    custom_instructions: str | None = None,
) -> dict:
    """Prepare the context for compaction.

    Splits entries into compacted (before first_kept) and kept (at/after first_kept).
    Builds the compaction prompt structure.

    Args:
        entries: List of session entry dicts
        first_kept_entry_id: ID of the first entry to keep in full
        custom_instructions: Optional custom instructions for the compaction

    Returns:
        A dict with:
        - first_kept_entry: the first entry to keep in full
        - compacted_entries: entries to compact (before first_kept)
        - instructions: system prompt for compaction
        - messages: the full message list for the compaction prompt
    """
    # Find the first kept entry
    first_kept_entry = None
    compacted_entries = []

    for entry in entries:
        if entry.get("id") == first_kept_entry_id:
            first_kept_entry = entry
            break
        compacted_entries.append(entry)

    # If first_kept_entry_id is not found, keep all entries
    if first_kept_entry is None:
        first_kept_entry = entries[0] if entries else {}

    # Build instructions for the compaction prompt
    instructions = (
        "You are a context compaction assistant. "
        "Given the following conversation history before the current point, "
        "provide a concise summary that captures the essential information, "
        "decisions, and context needed for future turns.\n\n"
        "IMPORTANT:\n"
        "- Be concise but comprehensive\n"
        "- Include all file paths, code snippets, and configurations mentioned\n"
        "- Note any decisions made or preferences expressed\n"
        "- Do NOT include verbatim conversation - summarize\n"
        "- Include the user's intent and the assistant's approach"
    )

    if custom_instructions:
        instructions += f"\n\n{custom_instructions}"

    # Build the messages list (compactable entries only)
    messages = compacted_entries

    return {
        "first_kept_entry": first_kept_entry,
        "compacted_entries": compacted_entries,
        "instructions": instructions,
        "messages": messages,
    }


def build_compaction_conversation_text(entries: list[dict]) -> str:
    """Build conversation text from session entries for the compaction prompt.

    Extracts readable conversation text from session entry dicts.

    Args:
        entries: List of session entry dicts

    Returns:
        Formatted conversation text
    """
    parts: list[str] = []
    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type == "message":
            msg = entry.get("message", {})
            role = msg.get("role", "unknown")
            content = msg.get("content", [])
            # Extract text from content blocks
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "toolCall":
                            texts.append(f"[Tool call: {block.get('name', 'unknown')}]")
                text = " ".join(texts)
                if text:
                    parts.append(f"{role}: {text}")
            elif isinstance(content, str) and content:
                parts.append(f"{role}: {content}")
        elif entry_type == "compaction":
            summary = entry.get("summary", "")
            if summary:
                parts.append(f"[Compaction: {summary}]")
        else:
            # For other entry types, include a brief representation
            if entry.get("id"):
                parts.append(f"[{entry_type}: {entry['id']}]")
    return "\n".join(parts)
