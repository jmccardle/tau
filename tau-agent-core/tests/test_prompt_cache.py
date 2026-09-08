"""The reading behind the notice that a prompt cache should have been read.

Gated on the RESULT, never on configuration. Gating on
``models.<name>.prompt_cache_dialect`` would fire only for models an operator had
already configured correctly and stay silent on the one case worth reporting —
an OpenAI-compatible gateway that drops ``cache_control`` on its way to the
Anthropic model it proxies, which is what produced the 0.0%-cached session this
came from.

The head-side wiring (``TurnStream`` collecting completions, ``RenderRouter``
putting the sentence on ``lane_end``) is
``tau-coding-agent/tests/test_cache_miss_notice.py``.

Reference: docs/PROMPT-CACHING.md §7.
"""

from __future__ import annotations

from tau_agent_core.prompt_cache import (
    CACHE_TTL_SECONDS,
    CONVERSATION_PREFIX,
    MIN_CACHEABLE_PROMPT_TOKENS,
    CompletionCache,
    PromptCacheObserver,
    cache_miss_reason,
    completions_from_messages,
    prompt_tokens,
)


def _calls(*pairs: tuple[int, int], reported: bool = True) -> list[CompletionCache]:
    """Completions as ``(cache_read, prompt)``, all reporting unless told otherwise."""
    return [CompletionCache(read=r, prompt=p, reported=reported) for r, p in pairs]


class _Event:
    """The two ``AgentEvent`` attributes ``feed_event`` reads, plus provenance."""

    def __init__(
        self,
        type: str,
        timestamp: int,
        message: dict | None = None,
        submission_id: str | None = "s1",
    ) -> None:
        self.type = type
        self.timestamp = timestamp
        self.message = message
        self.submission_id = submission_id


def _assistant(read: int, total: int, output: int = 10, reported: bool = True) -> dict:
    return {
        "role": "assistant",
        "content": [],
        "usage": {
            "cache_read_tokens": read,
            "cache_reported": reported,
            "output_tokens": output,
            "total_tokens": total,
        },
    }


class TestWithinTheTurn:
    """Two or more calls and nothing read after the first. The prefix
    demonstrably did not change between them, so this cannot be a false
    positive."""

    def test_a_tool_loop_that_never_reads_is_reported(self):
        reason = cache_miss_reason(_calls((0, 20_000), (0, 24_000), (0, 28_000)), None)
        assert reason is not None
        assert "3 calls" in reason
        assert "28,000" in reason

    def test_a_healthy_loop_is_silent(self):
        assert cache_miss_reason(_calls((0, 20_000), (19_900, 24_000)), None) is None

    def test_one_call_is_not_a_loop(self):
        """A single cold call is the write, not a miss."""
        assert cache_miss_reason(_calls((0, 20_000)), None) is None

    def test_a_short_prompt_is_never_flagged(self):
        """Below the minimum cacheable prefix no model caches, so a notice would
        be noise rather than a finding."""
        small = MIN_CACHEABLE_PROMPT_TOKENS - 1
        assert cache_miss_reason(_calls((0, small), (0, small)), None) is None

    def test_the_threshold_is_exclusive(self):
        at = MIN_CACHEABLE_PROMPT_TOKENS
        assert cache_miss_reason(_calls((0, at), (0, at)), None) is None
        assert cache_miss_reason(_calls((0, at + 1), (0, at + 1)), None) is not None


class TestAcrossTheTurnEdge:
    """The first call read nothing while the previous turn's entry was still
    alive."""

    def test_a_cold_first_call_inside_the_ttl_is_reported(self):
        reason = cache_miss_reason(_calls((0, 30_000), (29_000, 31_000)), 40.0)
        assert reason is not None
        assert "40s" in reason

    def test_past_the_ttl_is_expected_not_reported(self):
        assert cache_miss_reason(_calls((0, 30_000), (29_000, 31_000)), 900.0) is None

    def test_the_first_turn_of_a_session_is_silent(self):
        """No previous completion, so there was nothing to read."""
        assert cache_miss_reason(_calls((0, 30_000), (29_000, 31_000)), None) is None

    def test_a_warm_first_call_is_silent(self):
        assert cache_miss_reason(_calls((28_000, 30_000), (29_000, 31_000)), 40.0) is None

    def test_the_ttl_boundary(self):
        assert cache_miss_reason(_calls((0, 30_000), (1, 31_000)), CACHE_TTL_SECONDS) is None
        assert (
            cache_miss_reason(_calls((0, 30_000), (1, 31_000)), CACHE_TTL_SECONDS - 1) is not None
        )


class TestAServerThatAccountsForNoCache:
    """The counters read 0 whether a cache missed or never existed, so a server
    reporting no cache accounting at all is not evidence of anything.

    llama.cpp is that server and τ's default model points at one, so without this
    gate the first long local turn produces a notice telling its reader to
    configure a gateway they are not using (docs/PROMPT-CACHING.md §7)."""

    def test_a_loop_that_never_reads_is_silent_when_nothing_reports(self):
        calls = _calls((0, 20_000), (0, 24_000), (0, 28_000), reported=False)
        assert cache_miss_reason(calls, None) is None

    def test_the_same_loop_is_reported_when_the_server_does_account(self):
        calls = _calls((0, 20_000), (0, 24_000), (0, 28_000), reported=True)
        assert cache_miss_reason(calls, None) is not None

    def test_the_cross_turn_condition_is_gated_too(self):
        calls = _calls((0, 30_000), (29_000, 31_000), reported=False)
        assert cache_miss_reason(calls, 40.0) is None

    def test_one_silent_call_gates_the_whole_turn(self):
        """A mixed turn is a server changing its mind mid-turn, which τ has no
        reading for — so it says nothing rather than picking a majority."""
        calls = [
            CompletionCache(read=0, prompt=20_000, reported=True),
            CompletionCache(read=0, prompt=28_000, reported=False),
        ]
        assert cache_miss_reason(calls, None) is None


class TestNothingToJudge:
    def test_no_completions(self):
        assert cache_miss_reason([], 10.0) is None


class TestTheLatch:
    """One cached read anywhere in the session proves caching is on, and the
    observer is silent from then on — a later cold turn is an expired entry."""

    def test_a_read_in_this_turn_silences_this_turn(self):
        observer = PromptCacheObserver()
        assert observer.observe_turn(_calls((29_000, 30_000), (0, 34_000), (0, 38_000))) is None
        assert observer.cache_confirmed is True

    def test_a_read_in_an_earlier_turn_silences_a_later_one(self):
        observer = PromptCacheObserver()
        observer.observe_turn(_calls((0, 20_000), (19_000, 24_000)))
        assert observer.cache_confirmed is True
        assert observer.observe_turn(_calls((0, 30_000), (0, 34_000), (0, 38_000))) is None

    def test_without_a_read_the_verdict_stands(self):
        observer = PromptCacheObserver()
        assert observer.observe_turn(_calls((0, 30_000), (0, 34_000))) is not None
        assert observer.cache_confirmed is False

    def test_a_fresh_observer_has_no_evidence(self):
        assert PromptCacheObserver().cache_confirmed is False


class TestTheClockIsPerPrefix:
    """The fault this replaced: one ``_last_completion_ms`` on the TUI's router,
    written by every lane, so a sub-agent closing 9s before a user turn made that
    turn's gap describe a prefix it shares nothing with."""

    def test_the_conversation_compares_against_its_own_previous_turn(self):
        observer = PromptCacheObserver()
        observer.observe_turn(_calls((0, 30_000)), first_event_ms=0, last_event_ms=1_000)
        reason = observer.observe_turn(
            _calls((0, 30_000)), first_event_ms=41_000, last_event_ms=42_000
        )
        assert reason is not None
        assert "40s" in reason

    def test_a_branch_neither_reads_nor_writes_that_clock(self):
        observer = PromptCacheObserver()
        observer.observe_turn(_calls((0, 30_000)), first_event_ms=0, last_event_ms=1_000)
        branch = observer.observe_turn(
            _calls((0, 30_000)),
            prefix=None,
            first_event_ms=2_000,
            last_event_ms=3_000,
        )
        assert branch is None

        reason = observer.observe_turn(
            _calls((0, 30_000)), first_event_ms=41_000, last_event_ms=42_000
        )
        assert reason is not None
        assert "40s" in reason, "the branch's 2s gap must not stand in for the conversation's 40s"

    def test_two_prefixes_do_not_see_each_other(self):
        observer = PromptCacheObserver()
        observer.observe_turn(
            _calls((0, 30_000)), prefix="branch:a", first_event_ms=0, last_event_ms=1_000
        )
        assert (
            observer.observe_turn(
                _calls((0, 30_000)),
                prefix=CONVERSATION_PREFIX,
                first_event_ms=2_000,
                last_event_ms=3_000,
            )
            is None
        )


class TestFeedEvent:
    """The event-stream entry point, which the RPC handler uses because it holds
    no per-lane collector of its own."""

    def test_a_turn_answers_only_on_agent_end(self):
        observer = PromptCacheObserver()
        assert observer.feed_event(_Event("agent_start", 1_000)) is None
        assert observer.feed_event(_Event("message_end", 1_100, _assistant(0, 30_010))) is None
        assert observer.feed_event(_Event("message_end", 1_200, _assistant(0, 34_010))) is None
        reason = observer.feed_event(_Event("agent_end", 1_300))
        assert reason is not None
        assert "2 calls" in reason

    def test_the_next_turn_starts_from_nothing(self):
        observer = PromptCacheObserver()
        observer.feed_event(_Event("agent_start", 1_000))
        observer.feed_event(_Event("message_end", 1_100, _assistant(0, 30_010)))
        observer.feed_event(_Event("agent_end", 1_200))

        observer.feed_event(_Event("agent_start", 2_000))
        observer.feed_event(_Event("message_end", 2_100, _assistant(0, 30_010)))
        reason = observer.feed_event(_Event("agent_end", 2_200))
        assert reason is not None
        assert "1s after the previous one" in reason

    def test_a_turn_outside_any_submission_leaves_no_clock(self):
        """An LLM-backed compaction runs a loop of its own on a prompt that is not
        the conversation's, so its completions must not date the next user turn."""
        observer = PromptCacheObserver()
        observer.feed_event(_Event("agent_start", 0))
        observer.feed_event(_Event("message_end", 100, _assistant(0, 30_010)))
        observer.feed_event(_Event("agent_end", 1_000))

        observer.feed_event(_Event("agent_start", 2_000, submission_id=None))
        observer.feed_event(_Event("message_end", 2_100, _assistant(0, 30_010)))
        assert observer.feed_event(_Event("agent_end", 3_000, submission_id=None)) is None

        observer.feed_event(_Event("agent_start", 41_000))
        observer.feed_event(_Event("message_end", 41_100, _assistant(0, 30_010)))
        reason = observer.feed_event(_Event("agent_end", 42_000))
        assert reason is not None
        assert "40s" in reason

    def test_an_unrelated_event_type_is_ignored(self):
        observer = PromptCacheObserver()
        assert observer.feed_event(_Event("turn_start", 1)) is None
        assert observer.feed_event(_Event("tool_execution_end", 2)) is None


class TestCompletionsFromMessages:
    """Print mode's reading, taken off finished messages rather than events."""

    def test_assistant_usage_becomes_one_entry_each(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(0, 20_010),
            {"role": "toolResult", "content": "…"},
            _assistant(19_000, 24_010),
        ]
        assert completions_from_messages(messages) == _calls((0, 20_000), (19_000, 24_000))

    def test_an_assistant_message_with_no_usage_contributes_nothing(self):
        assert completions_from_messages([{"role": "assistant", "content": []}]) == []


class TestPromptTokens:
    """The prompt size the notice quotes, read as ``total - output``."""

    def test_the_subtraction(self):
        assert prompt_tokens({"total_tokens": 512, "output_tokens": 12}) == 500

    def test_the_field_sum_is_the_fallback_when_no_total_is_reported(self):
        usage = {"input_tokens": 100, "cache_read_tokens": 300, "cache_write_tokens": 100}
        assert prompt_tokens(usage) == 500

    def test_a_total_below_output_is_a_contradiction_not_a_small_number(self):
        assert prompt_tokens({"input_tokens": 300, "output_tokens": 50, "total_tokens": 10}) == 300

    def test_nothing_reported_is_zero(self):
        assert prompt_tokens({}) == 0
