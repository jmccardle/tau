"""Whether a session's prompt cache is being read, and what to say when it is not.

Reference: docs/PROMPT-CACHING.md §7.

The reading is head-agnostic and lives here so the TUI, print mode and an RPC
host all reach the same verdict from the same evidence. A head owns only the
delivery — a transcript box, a stderr line, a field on ``agent_end``.

:func:`prompt_tokens` is here rather than in a head because it is the quantity
the whole question is asked about: a prompt below
:data:`MIN_CACHEABLE_PROMPT_TOKENS` is not worth a notice, and the number in the
notice is the one it returns.

Two layers. :func:`cache_miss_reason` is pure — one turn's completions and the
gap since the previous turn, in, a sentence or None out.
:class:`PromptCacheObserver` holds the two things a pure function cannot: the
per-prefix clock that supplies that gap, and the session latch that goes down
for good the first time any completion reads a cached token.
"""

from __future__ import annotations

from typing import Any, NamedTuple

#: The highest minimum cacheable prefix of any current model (Haiku 4.5).
MIN_CACHEABLE_PROMPT_TOKENS = 4096

#: A 5-minute entry is gone past this, so a cold call after it is expected.
CACHE_TTL_SECONDS = 300

#: The prefix key for the session's own conversation, as opposed to a sub-agent's.
CONVERSATION_PREFIX = "conversation"


def prompt_tokens(usage: dict[str, Any]) -> int:
    """One completion's prompt size — the conversation's context when it was sent.

    Read as ``total_tokens - output_tokens``, because ``total_tokens`` is the
    server's own figure for the whole call and every provider's ``output_tokens``
    is the part of it the model generated. The remainder is the prompt, whatever
    fields the provider split it across.

    The alternative — summing ``input + cache_read + cache_write`` — is equal on
    a transcript written by today's code but WRONG on one written before the
    providers stopped double-counting the cached span inside ``input_tokens``: a
    reloaded chat from last week would read ~2× its real size on a cache-heavy
    provider.

    Falls to the field sum only when the server reported no ``total_tokens`` at
    all; ``Usage`` defaults it to 0, so 0 here means "nothing reported", not a
    zero-token prompt. A ``total`` below ``output`` is a contradiction rather than
    a small number, so it takes the same path instead of yielding a negative size.

    This is a per-completion reading. Callers REPLACE it as completions arrive
    rather than summing: prompt N contains prompt N-1 in full, so a sum reports
    the same conversation once per turn.
    """
    total = int(usage.get("total_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    if total >= output and total > 0:
        return total - output
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_tokens", 0) or 0)
        + int(usage.get("cache_write_tokens", 0) or 0)
    )


class CompletionCache(NamedTuple):
    """What one completion says about the prompt cache.

    ``reported`` is the server's ``Usage.cache_reported``: false means it
    accounts for no cache at all, so its ``read`` of 0 is silence rather than a
    miss.
    """

    read: int
    prompt: int
    reported: bool


def completion_cache(usage: dict[str, Any]) -> CompletionCache:
    """Read one completion's cache evidence off its ``usage`` dict."""
    return CompletionCache(
        read=int(usage.get("cache_read_tokens", 0) or 0),
        prompt=prompt_tokens(usage),
        reported=bool(usage.get("cache_reported")),
    )


def completions_from_messages(messages: list[dict[str, Any]]) -> list[CompletionCache]:
    """Every assistant message's cache evidence, in order — the reading off a turn.

    For a caller holding finished messages rather than a live event stream (print
    mode, a reloaded session). Assistant messages with no ``usage`` contribute
    nothing, which is the same silence a completion that reported none produces.
    """
    return [
        completion_cache(message["usage"])
        for message in messages
        if message.get("role") == "assistant" and isinstance(message.get("usage"), dict)
    ]


def cache_miss_reason(
    completions: list[CompletionCache],
    seconds_since_last_completion: float | None,
) -> str | None:
    """Why this turn's prompt cache should have been read and was not, or None.

    Reads the RESULT, never the configuration — the failure worth catching is a
    gateway dropping ``cache_control`` en route to an Anthropic model, and such a
    model has nothing declared to gate on (docs/PROMPT-CACHING.md §7).

    Three gates, then two independent conditions. Silent unless every completion
    ``reported``, the last prompt exceeds :data:`MIN_CACHEABLE_PROMPT_TOKENS`,
    and then either every call after the first read 0 (the prefix demonstrably
    did not change between them), or the first call read 0 within
    :data:`CACHE_TTL_SECONDS` of the previous turn's last completion.

    Knows nothing of the session it sits in, so a caller that has already seen a
    cached read must suppress this itself — :class:`PromptCacheObserver` is what
    does.

    Args:
        completions: One entry per call, in order.
        seconds_since_last_completion: Gap from the previous turn's last
            completion to this turn's start; None when there is no previous turn,
            which a cold session legitimately has nothing to read after.

    Returns:
        A sentence naming what was observed, or None when nothing is wrong.
    """
    if not completions or not all(call.reported for call in completions):
        return None
    last_prompt = completions[-1].prompt
    if last_prompt <= MIN_CACHEABLE_PROMPT_TOKENS:
        return None

    if len(completions) >= 2 and all(call.read == 0 for call in completions[1:]):
        return (
            f"No prompt-cache reads in this turn: {len(completions)} calls, "
            f"{last_prompt:,}-token prompt, 0 cached."
        )

    if (
        completions[0].read == 0
        and seconds_since_last_completion is not None
        and seconds_since_last_completion < CACHE_TTL_SECONDS
    ):
        return (
            f"No prompt-cache read on this turn's first call, "
            f"{seconds_since_last_completion:.0f}s after the previous one — "
            "inside the 5-minute entry's life."
        )
    return None


class PromptCacheObserver:
    """One session's running verdict on whether its prompt cache is being read.

    :meth:`observe_turn` is the interface: a head hands over one turn's
    completions when the turn closes and gets back the sentence to deliver, or
    None. :meth:`feed_event` is the same thing for a caller holding an
    ``AgentEvent`` stream rather than a collected list.

    **The latch.** One ``cache_read_tokens`` above 0 anywhere in the session
    proves the cache is enabled, and the misconfiguration this reports is the
    cache being off — so the first read silences the observer permanently. A
    later cold turn is then read as an expired entry, which it legitimately is.

    **The clock is per prefix, not per session.** ``_last_completion_ms`` was one
    attribute on the TUI's router, written by every lane, so a sub-agent branch
    closing shortly before a user turn made that turn's gap describe a prefix it
    shares nothing with. A caller names the prefix a turn belongs to; a caller
    whose prefix has no next turn (a sub-agent lane, which exists for exactly one
    span) passes None and leaves no clock behind, which drops the cross-turn
    condition for it and keeps the within-turn one.

    Says nothing about how often to deliver a notice — that is a display policy
    and belongs to the head. The TUI says it once per model; an RPC field is set
    on every turn that earns it.
    """

    def __init__(self) -> None:
        self._last_completion_ms: dict[str, int] = {}
        self._cache_confirmed = False
        self._turn: list[CompletionCache] = []
        self._turn_started_ms: int | None = None
        self._turn_prefix: str | None = None

    @property
    def cache_confirmed(self) -> bool:
        """Whether any completion this session has read a cached token."""
        return self._cache_confirmed

    def observe_turn(
        self,
        completions: list[CompletionCache],
        *,
        prefix: str | None = CONVERSATION_PREFIX,
        first_event_ms: int | None = None,
        last_event_ms: int | None = None,
    ) -> str | None:
        """Record one closed turn and return the notice it earns, or None.

        Args:
            completions: One entry per LLM call in the turn, in order.
            prefix: What this turn's prompt prefix is, for the cross-turn clock;
                None for a prefix with no next turn, which stores no clock and is
                compared against none.
            first_event_ms: Epoch ms this turn's first event carried, against
                which the previous turn on ``prefix`` supplies the gap.
            last_event_ms: Epoch ms this turn's last event carried, which becomes
                that clock.

        Returns:
            :func:`cache_miss_reason`'s sentence, or None once the latch is down.
        """
        gap: float | None = None
        if prefix is not None:
            previous = self._last_completion_ms.get(prefix)
            if previous is not None and first_event_ms is not None:
                gap = (first_event_ms - previous) / 1000
            if last_event_ms is not None:
                self._last_completion_ms[prefix] = last_event_ms

        if any(call.read > 0 for call in completions):
            self._cache_confirmed = True
        if self._cache_confirmed:
            return None
        return cache_miss_reason(completions, gap)

    def feed_event(self, event: Any) -> str | None:
        """Accumulate one ``AgentEvent`` and return the notice its turn earns.

        For a caller subscribed to ONE conversation's bus, where an
        ``agent_start``/``agent_end`` pair brackets a turn. A sub-agent's events
        arrive on the sub-session's bus and must not be fed here.

        A turn whose events carry no ``submission_id`` names no prefix: an
        LLM-backed compaction and a ``continue_conversation()`` resume both run a
        loop outside any submission, and their prompt is not the conversation's,
        so letting one set the clock would misdescribe the gap before the next
        user turn — the cross-lane fault in the TUI's router, arrived at by a
        second route.

        Returns a sentence only on the ``agent_end`` that closes a turn.
        """
        kind = getattr(event, "type", None)
        timestamp = getattr(event, "timestamp", None)
        if kind == "agent_start":
            self._turn = []
            self._turn_started_ms = timestamp
            self._turn_prefix = (
                CONVERSATION_PREFIX if getattr(event, "submission_id", None) else None
            )
            return None
        if kind == "message_end":
            message = getattr(event, "message", None)
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self._turn.append(completion_cache(usage))
            return None
        if kind != "agent_end":
            return None
        reason = self.observe_turn(
            self._turn,
            prefix=self._turn_prefix,
            first_event_ms=self._turn_started_ms,
            last_event_ms=timestamp,
        )
        self._turn = []
        self._turn_started_ms = None
        self._turn_prefix = None
        return reason
