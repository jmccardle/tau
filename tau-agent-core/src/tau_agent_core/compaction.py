"""τ-agent-core compaction — LLM-backed session summarization.

Faithful port of pi's ``packages/agent/src/harness/compaction/compaction.ts``,
adapted to τ's session model. The valuable, behavior-defining pieces are carried
over verbatim where possible: the structured summarization prompts, Usage-based
token estimation, cut-point selection (with split-turn handling), iterative
summary updates, and file-operation tracking.

Two intentional divergences from pi, both Pythonic and documented inline:

1. pi returns ``Result<T, CompactionError>``; τ raises :class:`CompactionError`.
   This matches the repo's Fail-Early rule — a failed summarization raises rather
   than silently yielding a fabricated summary.
2. τ operates on active-path *entry dicts* and message *dicts* (pi uses typed
   ``SessionTreeEntry`` / ``AgentMessage`` objects), because that is the shape
   ``SessionManager`` already produces (``get_active_messages`` /
   ``_build_active_path``).

Persistence of the generated summary into the session tree lives in
``SessionManager.apply_compaction`` — this module only *computes* the summary.

Reference: pi packages/agent/src/harness/compaction/compaction.ts
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

from tau_ai.client import complete_simple
from tau_ai.types import Model, TextContent

from tau_agent_core.compaction_utils import (
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)

# Chars assumed per image content block when estimating tokens (pi: utils ↔
# compaction.ts ESTIMATED_IMAGE_CHARS = 4800).
ESTIMATED_IMAGE_CHARS = 4800


# ─── Errors ──────────────────────────────────────────────────────────────


class CompactionError(Exception):
    """Raised when a compaction cannot complete.

    Pythonic translation of pi's ``Result<T, CompactionError>`` error arm
    (types.ts:161). ``code`` is one of ``"aborted"``, ``"summarization_failed"``,
    or ``"invalid_session"``. Fail-Early: callers handle the failure rather than
    receive a fabricated summary.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─── Settings / results ──────────────────────────────────────────────────


@dataclass
class CompactionSettings:
    """Compaction thresholds and retention settings (pi: CompactionSettings)."""

    enabled: bool = True
    reserve_tokens: int = 16384  # tokens reserved for summary prompt + output
    keep_recent_tokens: int = 20000  # approx recent-context tokens to keep


# Default compaction settings used by the harness (pi: DEFAULT_COMPACTION_SETTINGS).
DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


@dataclass
class CompactionDetails:
    """File-operation details stored alongside a compaction (pi: CompactionDetails)."""

    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


@dataclass
class CompactionResult:
    """Generated compaction data ready to persist (pi: CompactionResult).

    ``compacted_entry_ids`` and ``tokens_saved`` are τ additions — pi computes
    these in its persistence layer; τ threads them through so
    ``SessionManager.apply_compaction`` can record them on the compaction entry.
    """

    summary: str
    first_kept_entry_id: str
    tokens_before: int
    details: CompactionDetails | None = None
    compacted_entry_ids: list[str] = field(default_factory=list)
    tokens_saved: int = 0


# ─── Token estimation ────────────────────────────────────────────────────


def calculate_context_tokens(usage: dict[str, Any]) -> int:
    """Total context tokens from a Usage dict (pi: calculateContextTokens).

    Prefers the provider-reported ``total_tokens``; falls back to the sum of the
    component counts when total is absent/zero.
    """
    total = usage.get("total_tokens", 0) or 0
    if total:
        return int(total)
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_read_tokens", 0) or 0)
        + int(usage.get("cache_write_tokens", 0) or 0)
    )


def _estimate_text_and_image_chars(content: Any) -> int:
    """Char count for a string-or-block-list content value (pi helper)."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    chars = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            chars += len(str(block.get("text", "")))
        elif block.get("type") == "image":
            chars += ESTIMATED_IMAGE_CHARS
    return chars


def estimate_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for one message dict (pi: estimateTokens).

    Conservative ~4-chars-per-token heuristic over the textual payload, by role.
    """
    role = message.get("role")
    chars = 0

    if role == "user":
        chars = _estimate_text_and_image_chars(message.get("content"))
    elif role == "assistant":
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    chars += len(str(block.get("text", "")))
                elif btype == "thinking":
                    chars += len(str(block.get("thinking", "")))
                elif btype == "toolCall":
                    args = block.get("arguments")
                    args_len = len(_safe_json(args)) if args is not None else 0
                    chars += len(str(block.get("name", ""))) + args_len
    elif role == "toolResult":
        chars = _estimate_text_and_image_chars(message.get("content"))
    else:
        return 0

    return math.ceil(chars / 4)


def _safe_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return "[unserializable]"


def _get_assistant_usage(message: dict[str, Any]) -> dict[str, Any] | None:
    """Usage dict from a successful assistant message, else None (pi: getAssistantUsage)."""
    if message.get("role") != "assistant":
        return None
    if message.get("stop_reason") in ("aborted", "error"):
        return None
    usage = message.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    return None


@dataclass
class ContextUsageEstimate:
    """Estimated context-token usage for a message list (pi: ContextUsageEstimate)."""

    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None


def _last_assistant_usage_info(messages: list[dict[str, Any]]) -> tuple[dict[str, Any], int] | None:
    for i in range(len(messages) - 1, -1, -1):
        usage = _get_assistant_usage(messages[i])
        if usage is not None:
            return usage, i
    return None


def estimate_context_tokens(messages: list[dict[str, Any]]) -> ContextUsageEstimate:
    """Estimate context tokens, anchoring on the last assistant Usage when present.

    Faithful port of pi's estimateContextTokens (compaction.ts:165): the provider
    is the source of truth for everything up to the last assistant turn; only the
    trailing messages after it are heuristically estimated.
    """
    info = _last_assistant_usage_info(messages)
    if info is None:
        estimated = sum(estimate_tokens(m) for m in messages)
        return ContextUsageEstimate(
            tokens=estimated,
            usage_tokens=0,
            trailing_tokens=estimated,
            last_usage_index=None,
        )

    usage, index = info
    usage_tokens = calculate_context_tokens(usage)
    trailing = sum(estimate_tokens(messages[i]) for i in range(index + 1, len(messages)))
    return ContextUsageEstimate(
        tokens=usage_tokens + trailing,
        usage_tokens=usage_tokens,
        trailing_tokens=trailing,
        last_usage_index=index,
    )


def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """Whether context usage exceeds the compaction threshold (pi: shouldCompact)."""
    if not settings.enabled:
        return False
    return context_tokens > context_window - settings.reserve_tokens


# ─── Cut-point selection ─────────────────────────────────────────────────
#
# Adapted to τ entry dicts. τ carries every conversation message as a
# ``type=="message"`` entry distinguished by ``message.role`` (user/assistant/
# toolResult); ``customMessage`` is its own entry type; ``compaction`` marks a
# prior summary. Valid cut points are the user/assistant turn boundaries — never
# mid tool-call (a toolResult must stay attached to its assistant turn).


def _entry_message_role(entry: dict[str, Any]) -> str | None:
    if entry.get("type") != "message":
        return None
    msg = entry.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        return role if isinstance(role, str) else None
    return None


def find_valid_cut_points(
    entries: list[dict[str, Any]], start_index: int, end_index: int
) -> list[int]:
    """Indices where the conversation may be split (pi: findValidCutPoints)."""
    cut_points: list[int] = []
    for i in range(start_index, end_index):
        entry = entries[i]
        etype = entry.get("type")
        if etype == "message":
            if _entry_message_role(entry) in ("user", "assistant"):
                cut_points.append(i)
            # toolResult: not a cut point — keep it with its assistant turn.
        elif etype == "customMessage":
            cut_points.append(i)
    return cut_points


def find_turn_start_index(entries: list[dict[str, Any]], entry_index: int, start_index: int) -> int:
    """First user-visible entry that starts the turn containing ``entry_index``
    (pi: findTurnStartIndex). Returns -1 if none."""
    for i in range(entry_index, start_index - 1, -1):
        entry = entries[i]
        if entry.get("type") == "customMessage":
            return i
        if _entry_message_role(entry) == "user":
            return i
    return -1


@dataclass
class CutPointResult:
    """Cut point selected for compaction (pi: CutPointResult)."""

    first_kept_entry_index: int
    turn_start_index: int  # -1 when the cut is a clean user-message boundary
    is_split_turn: bool


def find_cut_point(
    entries: list[dict[str, Any]],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    """Choose the cut that retains ~``keep_recent_tokens`` of recent context
    (pi: findCutPoint)."""
    cut_points = find_valid_cut_points(entries, start_index, end_index)
    if not cut_points:
        return CutPointResult(
            first_kept_entry_index=start_index, turn_start_index=-1, is_split_turn=False
        )

    accumulated = 0
    cut_index = cut_points[0]
    for i in range(end_index - 1, start_index - 1, -1):
        entry = entries[i]
        if entry.get("type") != "message":
            continue
        accumulated += estimate_tokens(entry.get("message", {}))
        if accumulated >= keep_recent_tokens:
            for c in cut_points:
                if c >= i:
                    cut_index = c
                    break
            break

    # Walk the cut back over non-message metadata entries so it lands on a real
    # message/compaction boundary (pi: the while-loop at compaction.ts:358).
    while cut_index > start_index:
        prev = entries[cut_index - 1]
        ptype = prev.get("type")
        if ptype in ("compaction", "message"):
            break
        cut_index -= 1

    cut_entry = entries[cut_index]
    is_user_message = _entry_message_role(cut_entry) == "user"
    turn_start_index = (
        -1 if is_user_message else find_turn_start_index(entries, cut_index, start_index)
    )
    return CutPointResult(
        first_kept_entry_index=cut_index,
        turn_start_index=turn_start_index,
        is_split_turn=(not is_user_message and turn_start_index != -1),
    )


# ─── Summarization prompts (verbatim from pi) ────────────────────────────

SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""


# ─── Summary generation (the LLM calls) ──────────────────────────────────


def _summary_text(message: Any) -> str:
    """Join the text content blocks of an AssistantMessage."""
    return "\n".join(c.text for c in message.content if isinstance(c, TextContent))


def _summary_options(
    model: Model, api_key: str | None, max_tokens: int, thinking_level: str | None
) -> dict[str, Any]:
    options: dict[str, Any] = {"max_tokens": max_tokens}
    if api_key:
        options["api_key"] = api_key
    if model.reasoning and thinking_level and thinking_level != "off":
        options["reasoning"] = thinking_level
    return options


async def generate_summary(
    current_messages: list[dict[str, Any]],
    model: Model,
    reserve_tokens: int,
    api_key: str | None,
    *,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    thinking_level: str | None = None,
) -> str:
    """Generate (or iteratively update) a conversation summary (pi: generateSummary).

    Raises:
        CompactionError: on an aborted/errored or otherwise failed completion.
            Fail-Early — no fabricated fallback summary.
    """
    budget = math.floor(0.8 * reserve_tokens)
    max_tokens = min(budget, model.max_tokens) if model.max_tokens > 0 else budget

    base_prompt = UPDATE_SUMMARIZATION_PROMPT if previous_summary else SUMMARIZATION_PROMPT
    if custom_instructions:
        base_prompt = f"{base_prompt}\n\nAdditional focus: {custom_instructions}"

    conversation_text = serialize_conversation(current_messages)
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n"
    if previous_summary:
        prompt_text += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt_text += base_prompt

    context = {
        "messages": [
            {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt_text}],
                "timestamp": _now_ms(),
            },
        ]
    }
    options = _summary_options(model, api_key, max_tokens, thinking_level)

    try:
        response = await complete_simple(model, context, options)
    except Exception as exc:  # provider/transport failure
        raise CompactionError("summarization_failed", f"Summarization failed: {exc}") from exc

    if response.stop_reason == "aborted":
        raise CompactionError("aborted", response.error_message or "Summarization aborted")
    if response.stop_reason == "error":
        raise CompactionError(
            "summarization_failed",
            f"Summarization failed: {response.error_message or 'Unknown error'}",
        )

    return _summary_text(response)


async def generate_turn_prefix_summary(
    messages: list[dict[str, Any]],
    model: Model,
    reserve_tokens: int,
    api_key: str | None,
    *,
    thinking_level: str | None = None,
) -> str:
    """Summarize the prefix of a split turn (pi: generateTurnPrefixSummary)."""
    budget = math.floor(0.5 * reserve_tokens)
    max_tokens = min(budget, model.max_tokens) if model.max_tokens > 0 else budget

    conversation_text = serialize_conversation(messages)
    prompt_text = (
        f"<conversation>\n{conversation_text}\n</conversation>\n\n"
        f"{TURN_PREFIX_SUMMARIZATION_PROMPT}"
    )
    context = {
        "messages": [
            {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt_text}],
                "timestamp": _now_ms(),
            },
        ]
    }
    options = _summary_options(model, api_key, max_tokens, thinking_level)

    try:
        response = await complete_simple(model, context, options)
    except Exception as exc:
        raise CompactionError(
            "summarization_failed", f"Turn prefix summarization failed: {exc}"
        ) from exc

    if response.stop_reason == "aborted":
        raise CompactionError(
            "aborted", response.error_message or "Turn prefix summarization aborted"
        )
    if response.stop_reason == "error":
        raise CompactionError(
            "summarization_failed",
            f"Turn prefix summarization failed: {response.error_message or 'Unknown error'}",
        )

    return _summary_text(response)


# ─── Preparation + orchestration ─────────────────────────────────────────


@dataclass
class CompactionPreparation:
    """Prepared inputs for a compaction run (pi: CompactionPreparation)."""

    first_kept_entry_id: str
    messages_to_summarize: list[dict[str, Any]]
    turn_prefix_messages: list[dict[str, Any]]
    is_split_turn: bool
    tokens_before: int
    file_ops: FileOperations
    settings: CompactionSettings
    previous_summary: str | None = None
    compacted_entry_ids: list[str] = field(default_factory=list)


def _build_messages_from_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten active-path entries to LLM messages.

    Mirrors ``SessionManager.get_active_messages`` so token accounting here
    matches what the live loop actually sends: ``message`` entries pass through;
    a prior ``compaction`` entry becomes its summary as a user message.
    """
    messages: list[dict[str, Any]] = []
    for entry in entries:
        etype = entry.get("type")
        if etype == "message":
            msg = entry.get("message")
            if isinstance(msg, dict):
                messages.append(msg)
        elif etype == "compaction":
            summary = entry.get("summary", "")
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": f"[[Compaction summary: {summary}]]"}],
                }
            )
    return messages


def _message_for_compaction(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Message dict to summarize for an entry, or None to skip it.

    pi's getMessageFromEntryForCompaction excludes prior compaction entries (they
    are not re-summarized). τ likewise skips ``compaction``; ``message`` and
    ``customMessage`` entries contribute their stored message dict.
    """
    etype = entry.get("type")
    if etype == "compaction":
        return None
    if etype in ("message", "customMessage"):
        msg = entry.get("message")
        return msg if isinstance(msg, dict) else None
    return None


def prepare_compaction(
    path_entries: list[dict[str, Any]], settings: CompactionSettings
) -> CompactionPreparation | None:
    """Prepare active-path entries for compaction, or None when inapplicable.

    Faithful port of pi's prepareCompaction (compaction.ts:542), reading τ entry
    dicts. Returns None when there is nothing to compact (empty path, or the path
    already ends in a compaction entry).

    Raises:
        CompactionError("invalid_session"): when the chosen first-kept entry has
            no id.
    """
    if not path_entries or path_entries[-1].get("type") == "compaction":
        return None

    prev_compaction_index = -1
    for i in range(len(path_entries) - 1, -1, -1):
        if path_entries[i].get("type") == "compaction":
            prev_compaction_index = i
            break

    previous_summary: str | None = None
    boundary_start = 0
    if prev_compaction_index >= 0:
        prev = path_entries[prev_compaction_index]
        previous_summary = prev.get("summary")
        prev_first_kept = prev.get("first_kept_id")
        idx = next(
            (j for j, e in enumerate(path_entries) if e.get("id") == prev_first_kept),
            -1,
        )
        boundary_start = idx if idx >= 0 else prev_compaction_index + 1

    boundary_end = len(path_entries)

    tokens_before = estimate_context_tokens(_build_messages_from_entries(path_entries)).tokens

    cut = find_cut_point(path_entries, boundary_start, boundary_end, settings.keep_recent_tokens)
    first_kept_entry = path_entries[cut.first_kept_entry_index]
    first_kept_entry_id = first_kept_entry.get("id")
    if not first_kept_entry_id:
        raise CompactionError(
            "invalid_session", "First kept entry has no id - session may need migration"
        )

    history_end = cut.turn_start_index if cut.is_split_turn else cut.first_kept_entry_index
    messages_to_summarize: list[dict[str, Any]] = []
    for i in range(boundary_start, history_end):
        msg = _message_for_compaction(path_entries[i])
        if msg is not None:
            messages_to_summarize.append(msg)

    turn_prefix_messages: list[dict[str, Any]] = []
    if cut.is_split_turn:
        for i in range(cut.turn_start_index, cut.first_kept_entry_index):
            msg = _message_for_compaction(path_entries[i])
            if msg is not None:
                turn_prefix_messages.append(msg)

    # Files touched across the summarized range (pi seeds from the previous
    # compaction's stored details; τ's CompactionEntry does not persist file
    # lists, so accumulation starts fresh each compaction — documented divergence).
    file_ops = create_file_ops()
    for msg in messages_to_summarize:
        extract_file_ops_from_message(msg, file_ops)
    for msg in turn_prefix_messages:
        extract_file_ops_from_message(msg, file_ops)

    # Entry ids replaced by this compaction = everything from the boundary up to
    # (not including) the first kept entry.
    compacted_entry_ids = [
        eid
        for i in range(boundary_start, cut.first_kept_entry_index)
        if (eid := path_entries[i].get("id"))
    ]

    return CompactionPreparation(
        first_kept_entry_id=first_kept_entry_id,
        messages_to_summarize=messages_to_summarize,
        turn_prefix_messages=turn_prefix_messages,
        is_split_turn=cut.is_split_turn,
        tokens_before=tokens_before,
        file_ops=file_ops,
        settings=settings,
        previous_summary=previous_summary,
        compacted_entry_ids=compacted_entry_ids,
    )


async def compact(
    preparation: CompactionPreparation,
    model: Model,
    api_key: str | None,
    *,
    custom_instructions: str | None = None,
    thinking_level: str | None = None,
) -> CompactionResult:
    """Generate the compaction summary from prepared history (pi: compact).

    On a split turn, the history and the turn prefix are summarized concurrently
    and stitched together (pi uses ``Promise.all``).
    """
    if not preparation.first_kept_entry_id:
        raise CompactionError(
            "invalid_session", "First kept entry has no id - session may need migration"
        )

    if preparation.is_split_turn and preparation.turn_prefix_messages:

        async def _history() -> str:
            if not preparation.messages_to_summarize:
                return "No prior history."
            return await generate_summary(
                preparation.messages_to_summarize,
                model,
                preparation.settings.reserve_tokens,
                api_key,
                custom_instructions=custom_instructions,
                previous_summary=preparation.previous_summary,
                thinking_level=thinking_level,
            )

        history_summary, turn_prefix_summary = await asyncio.gather(
            _history(),
            generate_turn_prefix_summary(
                preparation.turn_prefix_messages,
                model,
                preparation.settings.reserve_tokens,
                api_key,
                thinking_level=thinking_level,
            ),
        )
        summary = (
            f"{history_summary}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_summary}"
        )
    else:
        summary = await generate_summary(
            preparation.messages_to_summarize,
            model,
            preparation.settings.reserve_tokens,
            api_key,
            custom_instructions=custom_instructions,
            previous_summary=preparation.previous_summary,
            thinking_level=thinking_level,
        )

    read_files, modified_files = compute_file_lists(preparation.file_ops)
    summary += format_file_operations(read_files, modified_files)

    tokens_saved = max(0, preparation.tokens_before - math.ceil(len(summary) / 4))

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=preparation.first_kept_entry_id,
        tokens_before=preparation.tokens_before,
        details=CompactionDetails(read_files=read_files, modified_files=modified_files),
        compacted_entry_ids=preparation.compacted_entry_ids,
        tokens_saved=tokens_saved,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
