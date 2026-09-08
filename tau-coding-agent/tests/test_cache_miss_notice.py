"""The head-side wiring of the prompt-cache notice.

``TurnStream`` collects one :class:`CompletionCache` per completion and
``RenderRouter`` puts the observer's sentence on ``lane_end``, where a head reads
it — never off the usage itself. The reading those two feed is
``tau-agent-core/tests/test_prompt_cache.py``.

Reference: docs/PROMPT-CACHING.md §7.
"""

from __future__ import annotations

from tau_agent_core.prompt_cache import CompletionCache

from tau_coding_agent.backends import TurnStream


class _Event:
    def __init__(self, timestamp: int, message: dict | None = None) -> None:
        self.type = "message_end" if message else "turn_start"
        self.timestamp = timestamp
        self.message = message
        self.turn_index = 0


def _usage(read: int, total: int, output: int = 10, reported: bool = True) -> dict:
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


def _calls(*pairs: tuple[int, int], reported: bool = True) -> list[CompletionCache]:
    """Completions as ``(cache_read, prompt)``, all reporting unless told otherwise."""
    return [CompletionCache(read=r, prompt=p, reported=reported) for r, p in pairs]


class TestTurnStreamCollectsTheCompletions:
    """The reading the notice is computed from — one entry per LLM call, in
    order, with the prompt size the shared ``prompt_tokens`` helper reports."""

    def test_one_entry_per_completion_in_call_order(self):
        stream = TurnStream(lane="main")
        stream.feed(_Event(1, _usage(read=0, total=20_010)))
        stream.feed(_Event(2, _usage(read=19_000, total=24_010)))

        assert stream.completions == _calls((0, 20_000), (19_000, 24_000))

    def test_a_completion_reporting_no_usage_adds_nothing(self):
        stream = TurnStream(lane="main")
        stream.feed(_Event(1, {"role": "assistant", "content": []}))
        assert stream.completions == []

    def test_a_stream_carries_the_reporting_flag_off_the_usage(self):
        stream = TurnStream(lane="main")
        stream.feed(_Event(1, _usage(read=0, total=20_010, reported=False)))
        assert stream.completions == [CompletionCache(read=0, prompt=20_000, reported=False)]

    def test_a_usage_omitting_the_flag_reads_as_no_accounting(self):
        """The safe direction: absent evidence never manufactures a finding."""
        stream = TurnStream(lane="main")
        stream.feed(
            _Event(1, {"role": "assistant", "content": [], "usage": {"total_tokens": 20_010}})
        )
        assert stream.completions[0].reported is False


class TestTheNoticeReachesLaneEnd:
    """The wiring: a head reads it off ``lane_end``, never off the usage itself."""

    async def test_a_cache_less_tool_loop_puts_a_sentence_on_lane_end(self):
        from tau_agent_core.events import AgentEvent
        from tau_agent_core.submission import Submission
        from tau_coding_agent.backends import RenderRouter

        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="go", source="interactive", submitter="human", submission_id="a")
        await router.on_submission_start(submission=sub, text="go")
        for _ in range(3):
            await router.on_agent_event(
                AgentEvent(
                    type="message_end",
                    timestamp=1_700_000_000_000,
                    message=_usage(read=0, total=30_010),
                    submission_id="a",
                )
            )
        await router.on_submission_end(submission=sub, side_usage={})

        lane_end = [e for e in seen if e["kind"] == "lane_end"][-1]
        assert lane_end["cache_notice"] is not None
        assert "3 calls" in lane_end["cache_notice"]

    async def test_a_healthy_loop_puts_none_there(self):
        from tau_agent_core.events import AgentEvent
        from tau_agent_core.submission import Submission
        from tau_coding_agent.backends import RenderRouter

        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="go", source="interactive", submitter="human", submission_id="a")
        await router.on_submission_start(submission=sub, text="go")
        for read in (0, 29_000, 29_500):
            await router.on_agent_event(
                AgentEvent(
                    type="message_end",
                    timestamp=1_700_000_000_000,
                    message=_usage(read=read, total=30_010),
                    submission_id="a",
                )
            )
        await router.on_submission_end(submission=sub, side_usage={})

        lane_end = [e for e in seen if e["kind"] == "lane_end"][-1]
        assert lane_end["cache_notice"] is None

    async def test_one_routers_latch_covers_every_later_turn(self):
        """The observer belongs to the router, not to a lane, because "this server
        caches" is a fact about the server."""
        from tau_agent_core.events import AgentEvent
        from tau_agent_core.submission import Submission
        from tau_coding_agent.backends import RenderRouter

        seen: list[dict] = []
        router = RenderRouter(seen.append)

        async def _turn(sid: str, reads: tuple[int, ...]) -> dict:
            sub = Submission(text="go", source="interactive", submitter="human", submission_id=sid)
            await router.on_submission_start(submission=sub, text="go")
            for read in reads:
                await router.on_agent_event(
                    AgentEvent(
                        type="message_end",
                        timestamp=1_700_000_000_000,
                        message=_usage(read=read, total=30_010),
                        submission_id=sid,
                    )
                )
            await router.on_submission_end(submission=sub, side_usage={})
            return [e for e in seen if e["kind"] == "lane_end"][-1]

        assert (await _turn("a", (0, 29_000)))["cache_notice"] is None
        assert (await _turn("b", (0, 0, 0)))["cache_notice"] is None

    async def test_a_branch_does_not_date_the_next_user_turn(self):
        """The cross-lane clock fault: a sub-agent closing seconds before a user
        turn used to supply that turn's gap, though it shares no prefix with it."""
        from tau_agent_core.events import AgentEvent
        from tau_agent_core.submission import Submission
        from tau_coding_agent.backends import RenderRouter

        seen: list[dict] = []
        router = RenderRouter(seen.append)

        def _end(sid: str, at: int) -> AgentEvent:
            return AgentEvent(
                type="message_end",
                timestamp=at,
                message=_usage(read=0, total=30_010),
                submission_id=sid,
            )

        first = Submission(text="go", source="interactive", submitter="human", submission_id="a")
        await router.on_submission_start(submission=first, text="go")
        await router.on_agent_event(_end("a", 1_000_000))
        await router.on_submission_end(submission=first, side_usage={})

        branch_event = AgentEvent(
            type="message_end",
            timestamp=1_600_000,
            message=_usage(read=0, total=30_010),
            submission_id=None,
        )
        await router.on_branch_event(lane="sub", label="agent", event=branch_event)
        await router.on_branch_end(lane="sub", label="agent")

        second = Submission(text="go", source="interactive", submitter="human", submission_id="c")
        await router.on_submission_start(submission=second, text="go")
        await router.on_agent_event(_end("c", 1_609_000))
        await router.on_agent_event(
            AgentEvent(
                type="message_end",
                timestamp=1_610_000,
                message=_usage(read=29_000, total=31_010),
                submission_id="c",
            )
        )
        await router.on_submission_end(submission=second, side_usage={})

        lane_end = [e for e in seen if e["kind"] == "lane_end"][-1]
        assert lane_end["cache_notice"] is None, (
            "the 9s gap belongs to the branch; the conversation's own gap is 609s, "
            "which is outside the entry's life and expected to read cold"
        )
