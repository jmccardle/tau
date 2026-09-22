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

from tau_agent_core.completion import CompletionFailed, resolved_complete
from tau_agent_core.usage import add_usage, usage_of, zero_usage
from tau_llm.client import TextDeltaSink, complete_simple
from tau_llm.types import Model, TextContent

from tau_agent_core.compaction_utils import (
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)
from tau_llm.docs import agent_facing
from tau_llm.tokens import (
    ZERO_TOKENS,
    CharClassCounter,
    ContextCalibrator,
    TextCounter,
    TokenCount,
)

ESTIMATED_IMAGE_TOKENS = 1200
"""Assumed cost of one image. Not measured — see :func:`count_message`."""

COMPACTION_SUMMARY_PREFIX = "[[Compaction summary: "
"""How a compaction summary announces itself once it is back in the context."""


# ─── Errors ──────────────────────────────────────────────────────────────


@agent_facing(topic="compaction")
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


@agent_facing(topic="compaction")
@dataclass
class CompactionSettings:
    """Compaction thresholds and retention settings.

    Attributes:
        enabled: Whether automatic compaction runs at all.
        reserve_tokens: The margin ``m``. The hard limit is
            ``context_window - reserve_tokens``, so this is what a compaction has
            left to spend on its own summarization call and output.
        keep_recent_tokens: How much recent conversation a compaction retains
            uncompacted. A retention size only — it does not set a threshold.
        soft_limit_ratio: Where the soft limit sits as a fraction of the hard one.
            A ratio rather than a fixed gap because the gap has to scale: 20000
            tokens below the hard limit is a reasonable warning distance on a
            128k window and below zero on a 1000-token one.
        hard_limit_tokens: Pin the mid-turn ceiling outright, ignoring the window
            and the reserve. None derives it.
        soft_limit_tokens: Pin the end-of-turn ceiling outright. None derives it.

    :func:`compaction_limits` is what turns these into the two numbers that get
    compared against, and raises rather than return an unusable pair.
    """

    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000
    soft_limit_ratio: float = 0.8
    hard_limit_tokens: int | None = None
    soft_limit_tokens: int | None = None


# Default compaction settings used by the harness (pi: DEFAULT_COMPACTION_SETTINGS).
DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


@agent_facing(topic="compaction")
@dataclass
class CompactionDetails:
    """File-operation details stored alongside a compaction (pi: CompactionDetails)."""

    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


@agent_facing(topic="compaction")
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
    usage: dict[str, int] = field(default_factory=zero_usage)


# ─── Token estimation ────────────────────────────────────────────────────


@agent_facing(topic="compaction")
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


def _text_and_images(content: Any) -> tuple[list[str], int]:
    """Text parts and image count for a string-or-block-list content value."""
    if isinstance(content, str):
        return [content], 0
    if not isinstance(content, list):
        return [], 0
    parts: list[str] = []
    images = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            images += 1
    return parts, images


@agent_facing(topic="compaction")
def message_payload(message: dict[str, Any]) -> tuple[str, int]:
    """The ``(text, image_count)`` of one message dict, as the wire carries it.

    The single spelling of "what is in this message that costs tokens", shared by
    :func:`count_message` and by anything measuring a transcript. An unknown role
    contributes nothing, which is the truth: the wire has no place to put it.
    """
    role = message.get("role")
    if role in ("user", "toolResult"):
        parts, images = _text_and_images(message.get("content"))
        return "\n".join(parts), images
    if role == "assistant":
        parts = []
        images = 0
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype == "thinking":
                    parts.append(str(block.get("thinking", "")))
                elif btype == "toolCall":
                    parts.append(str(block.get("name", "")))
                    args = block.get("arguments")
                    if args is not None:
                        parts.append(_safe_json(args))
                elif btype == "image":
                    images += 1
        return "\n".join(parts), images
    return "", 0


def default_counter() -> TextCounter:
    """The counter used when a caller names none: character classes, no tokenizer.

    Module-level and shared, because :class:`~tau_llm.tokens.CharClassCounter`
    is stateless and constructing one per message on a 10,000-message transcript
    is pure waste.
    """
    return _DEFAULT_COUNTER


_DEFAULT_COUNTER: TextCounter = CharClassCounter()


@agent_facing(topic="compaction")
def count_message(message: dict[str, Any], counter: TextCounter | None = None) -> TokenCount:
    """Tokens one message dict costs, labelled with how the number was reached.

    ``counter`` is any :class:`~tau_llm.tokens.TextCounter`: a
    :class:`~tau_llm.tokens.TokenizerCounter` gives an exact count of the text, a
    :class:`~tau_llm.tokens.CharClassCounter` (the default) gives a labelled
    estimate. Neither includes the chat template — see
    :func:`estimate_context_tokens`, which is where that gap is closed.

    An image is priced at :data:`ESTIMATED_IMAGE_TOKENS`, which is an assumption
    and not a measurement: providers tile images differently and τ has not
    measured any of them. The returned count says ``exact=False`` whenever an
    image is in it, even behind an exact tokenizer.
    """
    text, images = message_payload(message)
    if not text and not images:
        return ZERO_TOKENS
    count = (counter or _DEFAULT_COUNTER).count(text, role=message.get("role"))
    if not images:
        return count
    return TokenCount(
        tokens=count.tokens + images * ESTIMATED_IMAGE_TOKENS,
        source=count.source,
        exact=False,
        includes_template=count.includes_template,
    )


@agent_facing(topic="compaction")
def estimate_tokens(message: dict[str, Any]) -> int:
    """Estimated token count for one message dict, as a bare int.

    The count-only view of :func:`count_message` for the several call sites that
    display a size and have no use for its provenance. Anything DECIDING on the
    number should call :func:`count_message` and read ``exact``.
    """
    return count_message(message).tokens


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


@agent_facing(topic="compaction")
@dataclass
class ContextUsageEstimate:
    """Estimated context-token usage for a message list.

    Attributes:
        tokens: The whole estimate — ``usage_tokens + trailing_tokens``.
        usage_tokens: What the provider billed for everything up to and
            including ``last_usage_index``. Exact when present.
        trailing_tokens: The counted cost of the messages after that anchor.
        last_usage_index: Index of the anchoring assistant message, or None when
            no message on the path has reported usage yet.
        count: ``tokens`` with its provenance attached. ``exact`` is True only
            when every message was either billed or counted by a real tokenizer
            AND the framing around the trailing messages is accounted for — which
            in practice means a calibrated fit, so it is usually False.
    """

    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None
    count: TokenCount = ZERO_TOKENS


def _last_assistant_usage_info(
    messages: list[dict[str, Any]], usage_valid_after: int = 0
) -> tuple[dict[str, Any], int] | None:
    """The newest assistant turn whose usage actually reports a size, with its index.

    Three things disqualify an anchor.

    A usage dict of all zeros is "the provider said nothing", not "the context was
    empty" — the same reading
    :func:`~tau_agent_core.prompt_cache.prompt_tokens` takes. Anchoring on one
    would price every message before it at zero and report a 90k conversation as
    whatever arrived after the silent turn.

    A usage from BEFORE the newest compaction describes a context that no longer
    exists. The turn was billed against the conversation the compaction then
    folded away, so anchoring on it reports the old size plus the new tail.
    Measured live on 2026-09-18: the first request after a mid-turn compaction
    read 22,588 tokens against a billed 5,734 — a ratio of 3.94, over the hard
    limit that had just fired, which is a compaction loop.

    ``usage_valid_after`` is how that is caught, and POSITION IS NOT ENOUGH. The
    kept tail sits after the summary in the flattened path but was billed before
    the fold, so walking backwards and stopping at the summary misses exactly the
    message that causes the fault. A wall-clock boundary distinguishes them:
    :meth:`AgentSession.context_estimate` passes the newest compaction's stamp,
    and any message at or before it is disqualified whatever its position. The
    summary stop below is kept for the other order, where the summary really is
    newer than every usage on the path.
    """
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if is_compaction_summary(message):
            return None
        usage = _get_assistant_usage(message)
        if usage is None or calculate_context_tokens(usage) <= 0:
            continue
        if usage_valid_after and int(message.get("timestamp") or 0) <= usage_valid_after:
            return None
        return usage, i
    return None


@agent_facing(topic="compaction")
def estimate_context_tokens(
    messages: list[dict[str, Any]],
    *,
    counter: TextCounter | None = None,
    calibrator: ContextCalibrator | None = None,
    usage_valid_after: int = 0,
) -> ContextUsageEstimate:
    """Context tokens for a message list, anchored on the provider where possible.

    The provider is the source of truth for everything up to the last assistant
    turn that reported usage; only the messages after that anchor are counted
    locally. That anchoring is what keeps the estimate from drifting: the system
    prompt, the tool schemas and every chat-template header before the anchor are
    inside the billed number and need no modelling at all.

    ``counter`` counts the trailing text — a tokenizer for exact, the character
    classes by default. ``calibrator``, when it has a fit, adds the per-message
    framing those trailing messages will cost and scales the payload; without one
    the trailing count omits framing, which under-counts the next request.

    Args:
        messages: The active path, oldest first.
        counter: Text counter for the trailing messages.
        calibrator: A fitted :class:`~tau_llm.tokens.ContextCalibrator`, or None.
        usage_valid_after: Epoch-ms boundary; usage from a message at or before
            it is not an anchor. Pass the newest compaction's timestamp, because
            everything billed before a fold was billed against a context the fold
            removed. 0 disables the check.

    Returns:
        A :class:`ContextUsageEstimate` whose ``count`` says how it was reached.
    """
    info = _last_assistant_usage_info(messages, usage_valid_after)
    if info is None:
        trailing_msgs = messages
        usage_tokens = 0
        index = None
    else:
        usage, anchor = info
        usage_tokens = calculate_context_tokens(usage)
        trailing_msgs = messages[anchor + 1 :]
        index = anchor

    payload = ZERO_TOKENS
    for m in trailing_msgs:
        payload = payload + count_message(m, counter)

    trailing = payload
    if calibrator is not None and trailing_msgs:
        trailing = calibrator.predict(messages=len(trailing_msgs), payload=payload)

    total = usage_tokens + trailing.tokens
    if usage_tokens:
        count = TokenCount(
            tokens=total,
            source="usage" if trailing.tokens == 0 else trailing.source,
            exact=trailing.tokens == 0,
            includes_template=trailing.includes_template or trailing.tokens == 0,
        )
    else:
        count = TokenCount(
            tokens=total,
            source=trailing.source,
            exact=trailing.exact and trailing.includes_template,
            includes_template=trailing.includes_template,
        )

    return ContextUsageEstimate(
        tokens=total,
        usage_tokens=usage_tokens,
        trailing_tokens=trailing.tokens,
        last_usage_index=index,
        count=count,
    )


@agent_facing(topic="compaction")
@dataclass(frozen=True)
class CompactionLimits:
    """The two token ceilings a session compacts against.

    Attributes:
        soft: Checked at the end of an agent turn. Crossing it schedules a
            compaction that runs before the next turn starts, while there is
            still room to do anything else.
        hard: Checked at every turn boundary INSIDE the agent loop. Crossing it
            compacts immediately and the loop continues on the compacted
            context, because the alternative is a request the provider refuses.
        window: The model's context window, for reference in an error message.

    ``soft <= hard <= window`` always holds; :func:`compaction_limits` raises
    rather than return a pair that does not.
    """

    soft: int
    hard: int
    window: int


@agent_facing(topic="compaction")
def compaction_limits(context_window: int, settings: CompactionSettings) -> CompactionLimits:
    """Resolve ``settings`` against ``context_window`` into two absolute ceilings.

    By default the hard limit is ``context_window - reserve_tokens`` — the margin
    a compaction needs for its own summarization call and output — and the soft
    limit sits at ``soft_limit_ratio`` of that, so an end-of-turn compaction fires
    with room to spare. Either can be pinned outright with ``hard_limit_tokens`` /
    ``soft_limit_tokens``, which is how a test asks for a 20k ceiling on a 172k
    model.

    Raises:
        ValueError: when the resolved limits are unusable — non-positive, out of
            order, or past the window. Fail-Early: a hard limit below the soft
            limit would make the mid-turn check fire before the end-of-turn one
            and compact on every single tool call.
    """
    if context_window <= 0:
        raise ValueError(f"context_window must be > 0, got {context_window}")
    if not 0 < settings.soft_limit_ratio <= 1:
        raise ValueError(f"soft_limit_ratio must be in (0, 1], got {settings.soft_limit_ratio}")

    hard = (
        settings.hard_limit_tokens
        if settings.hard_limit_tokens is not None
        else context_window - settings.reserve_tokens
    )
    soft = (
        settings.soft_limit_tokens
        if settings.soft_limit_tokens is not None
        else math.floor(hard * settings.soft_limit_ratio)
    )

    if hard <= 0 or soft <= 0:
        raise ValueError(
            f"compaction limits must be positive; got soft={soft} hard={hard} from "
            f"context_window={context_window} and reserve_tokens="
            f"{settings.reserve_tokens}. The window is too small for that margin — "
            f"lower reserve_tokens or pin the limits directly."
        )
    if soft > hard:
        raise ValueError(
            f"soft limit {soft} is above hard limit {hard}; the mid-turn check would "
            f"fire before the end-of-turn one and compact on every tool call."
        )
    if hard > context_window:
        raise ValueError(
            f"hard limit {hard} exceeds context_window {context_window}; a request at "
            f"that size is refused before any compaction can run."
        )
    return CompactionLimits(soft=soft, hard=hard, window=context_window)


@agent_facing(topic="compaction")
def try_compaction_limits(
    context_window: int, settings: CompactionSettings
) -> CompactionLimits | None:
    """:func:`compaction_limits`, or None when this window cannot carry the margins.

    None is a real state and not a swallowed error: a model whose whole window is
    smaller than the room a compaction needs to run cannot be auto-compacted at
    any threshold, and the honest answer to "should we compact" there is "this
    setting does not apply", not a number. :meth:`AgentSession._maybe_auto_compact`
    says the same thing in its guard.

    Use :func:`compaction_limits` wherever the caller has already established that
    the window is workable and a bad pair is a bug worth raising on.
    """
    try:
        return compaction_limits(context_window, settings)
    except ValueError:
        return None


@agent_facing(topic="compaction")
def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """Whether to compact at the END of a turn, having crossed the soft limit."""
    if not settings.enabled:
        return False
    limits = try_compaction_limits(context_window, settings)
    return limits is not None and context_tokens > limits.soft


@agent_facing(topic="compaction")
def must_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """Whether to compact NOW, mid-turn, having crossed the hard limit.

    The difference from :func:`should_compact` is when the caller is allowed to
    wait. A soft crossing can wait for the turn to finish; a hard crossing cannot,
    because the next request in this same turn is the one that gets refused.
    """
    if not settings.enabled:
        return False
    limits = try_compaction_limits(context_window, settings)
    return limits is not None and context_tokens > limits.hard


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
    """Choose the cut that retains ~``keep_recent_tokens`` of recent context.

    Walks backwards from ``end_index`` accumulating counted tokens, stops at the
    first message that meets the target, and cuts at the first valid cut point at
    or after it. When the target is met inside the NEWEST turn there is no such
    cut point, and the fallback is the LAST cut point rather than the first: the
    first keeps the whole conversation, which made ``prepare_compaction`` return
    None for any path whose final message exceeded ``keep_recent_tokens`` on its
    own (docs/TOKEN-ACCOUNTING.md §1).

    The initial ``cut_points[0]`` survives for the other case — a conversation
    smaller than the target, where keeping everything is the right answer and
    ``prepare_compaction`` then reports nothing to compact.
    """
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
            cut_index = next((c for c in cut_points if c >= i), cut_points[-1])
            break

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
    on_text_delta: TextDeltaSink | None = None,
) -> tuple[str, dict[str, int]]:
    """Generate (or iteratively update) a conversation summary (pi: generateSummary).

    Returns:
        ``(summary, usage)`` — the text AND what producing it cost. A function that
        spends tokens has to say how many, or the caller cannot account for them:
        this one's input is the whole conversation, and it used to throw that number
        away (see :mod:`tau_agent_core.usage`).

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
        response = await resolved_complete(
            model,
            context,
            options=options,
            complete_fn=complete_simple,
            on_text_delta=on_text_delta,
        )
    except CompletionFailed as exc:
        if exc.stop_reason == "aborted":
            raise CompactionError("aborted", exc.error_message or "Summarization aborted") from exc
        raise CompactionError(
            "summarization_failed",
            f"Summarization failed: {exc.error_message or 'Unknown error'}",
        ) from exc
    except Exception as exc:  # provider/transport failure
        raise CompactionError("summarization_failed", f"Summarization failed: {exc}") from exc

    return _summary_text(response), usage_of(response)


async def generate_turn_prefix_summary(
    messages: list[dict[str, Any]],
    model: Model,
    reserve_tokens: int,
    api_key: str | None,
    *,
    thinking_level: str | None = None,
) -> tuple[str, dict[str, int]]:
    """Summarize the prefix of a split turn (pi: generateTurnPrefixSummary).

    Returns ``(summary, usage)`` — see :func:`generate_summary`. On a split turn this
    call runs CONCURRENTLY with the history summary, so a compaction can spend two
    completions, and both have to be counted.
    """
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
        response = await resolved_complete(
            model, context, options=options, complete_fn=complete_simple
        )
    except CompletionFailed as exc:
        if exc.stop_reason == "aborted":
            raise CompactionError(
                "aborted", exc.error_message or "Turn prefix summarization aborted"
            ) from exc
        raise CompactionError(
            "summarization_failed",
            f"Turn prefix summarization failed: {exc.error_message or 'Unknown error'}",
        ) from exc
    except Exception as exc:
        raise CompactionError(
            "summarization_failed", f"Turn prefix summarization failed: {exc}"
        ) from exc

    return _summary_text(response), usage_of(response)


# ─── Preparation + orchestration ─────────────────────────────────────────


@agent_facing(topic="compaction")
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


def _summary_context_message(summary: str) -> dict[str, Any]:
    """The user message a compaction summary becomes when it re-enters context.

    One spelling of the wrapper, shared by ``_build_messages_from_entries``
    (reading a compaction entry that already exists), ``compact``'s
    ``tokens_saved`` arithmetic (pricing the summary it is about to write), and
    :func:`is_compaction_summary` (recognising one on the way back). Two
    spellings would let the estimate drift from the thing it estimates.
    """
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"{COMPACTION_SUMMARY_PREFIX}{summary}]]"}],
    }


@agent_facing(topic="compaction")
def is_compaction_summary(message: dict[str, Any]) -> bool:
    """Whether this message is a compaction summary re-entering the context.

    Recognised by the marker :func:`_summary_context_message` writes, which is
    why both live here: a second spelling of the prefix would make this silently
    stop matching the thing it is about.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    return (
        isinstance(first, dict)
        and first.get("type") == "text"
        and str(first.get("text", "")).startswith(COMPACTION_SUMMARY_PREFIX)
    )


def _build_messages_from_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten active-path entries to LLM messages.

    Mirrors ``SessionManager.get_active_messages`` so token accounting here
    matches what the live loop actually sends: ``message`` entries pass through;
    a prior ``compaction`` entry becomes its summary as a user message.
    """
    messages: list[dict[str, Any]] = []
    for entry in entries:
        etype = entry.get("type")
        if etype in ("message", "customMessage"):
            msg = entry.get("message")
            if isinstance(msg, dict):
                messages.append(msg)
        elif etype == "compaction":
            messages.append(_summary_context_message(entry.get("summary", "")))
    return messages


@agent_facing(topic="compaction")
def estimate_span_tokens(entries: list[dict[str, Any]]) -> int:
    """Estimated context tokens a SPAN of active-path entries contributes.

    The one spelling of "what did this span cost the context", so the number a
    splice anchor RECORDS (``coveredTokens`` on a ``compaction``/``elide`` entry —
    TREE-BROWSER-AS-EDITOR.md §8.1, §8.2) is produced by the same arithmetic as the
    ``tokensBefore`` it sits beside (``prepare_compaction``, :func:`estimate_tokens`
    via :func:`estimate_context_tokens`). Two spellings would let a browser row show
    "folds 12 entries, 8k tokens" against a ``tokensBefore`` computed on a different
    basis, and nothing would report the mismatch.

    Public because the value must be named at the CALL SITE: §11.3 makes the
    provenance a required keyword argument on the appenders precisely so a caller
    that cannot compute it fails there (``AgentSession._perform_compaction`` and
    ``tree_ops.elide_span`` / ``commit_branch``). Computing it inside the five ``SessionLog``
    implementations instead would put this arithmetic — and the entry→message
    flattening under it — in five places.

    Non-message entries contribute nothing, exactly as they contribute nothing to
    model input: an ``agent_spec``/``customEntry``/``navigate`` node in the span is
    counted by ``coveredEntries`` and priced at zero here, which is the truth.
    """
    return estimate_context_tokens(_build_messages_from_entries(entries)).tokens


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


@agent_facing(topic="compaction")
def prepare_compaction(
    path_entries: list[dict[str, Any]], settings: CompactionSettings
) -> CompactionPreparation | None:
    """Prepare active-path entries for compaction, or None when inapplicable.

    Faithful port of pi's prepareCompaction (compaction.ts:542), reading τ entry
    dicts. Returns None when there is nothing to compact: an empty path, a path
    that already ends in a compaction entry, or — τ's one divergence from pi
    here, see the comment at the guard — a cut that would remove no message from
    the context at all, which is what the shipped ``keep_recent_tokens`` produces
    for every conversation smaller than it.

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
        prev_first_kept = prev.get("firstKeptId") or prev.get("first_kept_id")
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

    if not messages_to_summarize and not turn_prefix_messages:
        return None

    file_ops = create_file_ops()
    for msg in messages_to_summarize:
        extract_file_ops_from_message(msg, file_ops)
    for msg in turn_prefix_messages:
        extract_file_ops_from_message(msg, file_ops)

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


@agent_facing(topic="compaction")
async def compact(
    preparation: CompactionPreparation,
    model: Model,
    api_key: str | None,
    *,
    custom_instructions: str | None = None,
    thinking_level: str | None = None,
    on_text_delta: TextDeltaSink | None = None,
) -> CompactionResult:
    """Generate the compaction summary from prepared history (pi: compact).

    On a split turn, the history and the turn prefix are summarized concurrently
    and stitched together (pi uses ``Promise.all``).

    ``on_text_delta`` watches the HISTORY summary only, even on a split turn.
    The two completions run concurrently, so feeding both into one sink would
    interleave two documents into unreadable text; the prefix summary is stitched
    on after both finish and arrives in the caller's final result instead. Nothing
    is hidden — the whole summary is returned either way — but what the reader
    watches arrive is one document rather than two shuffled together.
    """
    if not preparation.first_kept_entry_id:
        raise CompactionError(
            "invalid_session", "First kept entry has no id - session may need migration"
        )

    if preparation.is_split_turn and preparation.turn_prefix_messages:

        async def _history() -> tuple[str, dict[str, int]]:
            if not preparation.messages_to_summarize:
                # Nothing was sent to a provider, so nothing was spent. A true zero.
                return "No prior history.", zero_usage()
            return await generate_summary(
                preparation.messages_to_summarize,
                model,
                preparation.settings.reserve_tokens,
                api_key,
                custom_instructions=custom_instructions,
                previous_summary=preparation.previous_summary,
                thinking_level=thinking_level,
                on_text_delta=on_text_delta,
            )

        (
            (history_summary, history_usage),
            (turn_prefix_summary, prefix_usage),
        ) = await asyncio.gather(
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
        usage = add_usage(history_usage, prefix_usage)
    else:
        summary, usage = await generate_summary(
            preparation.messages_to_summarize,
            model,
            preparation.settings.reserve_tokens,
            api_key,
            custom_instructions=custom_instructions,
            previous_summary=preparation.previous_summary,
            thinking_level=thinking_level,
            on_text_delta=on_text_delta,
        )

    read_files, modified_files = compute_file_lists(preparation.file_ops)
    summary += format_file_operations(read_files, modified_files)

    tokens_removed = estimate_context_tokens(
        [*preparation.messages_to_summarize, *preparation.turn_prefix_messages]
    ).tokens
    tokens_saved = tokens_removed - estimate_tokens(_summary_context_message(summary))

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=preparation.first_kept_entry_id,
        tokens_before=preparation.tokens_before,
        details=CompactionDetails(read_files=read_files, modified_files=modified_files),
        compacted_entry_ids=preparation.compacted_entry_ids,
        tokens_saved=tokens_saved,
        usage=usage,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
