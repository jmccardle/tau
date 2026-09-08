"""One clock for an exchange's duration, live and after a reload.

The TUI used to time an exchange with its OWN wall clock, started when the lane
opened, and show nothing at all for a reloaded one — so the number existed in
exactly one head, for exactly as long as that head stayed open. Both paths now
read the agent loop's clock: live off the events, reloaded off the message
timestamps those same events were stamped with.

Reference: docs/MESSAGE-TIMESTAMPS.md §3.
"""

from __future__ import annotations

from tau_coding_agent.backends import TurnStream
from tau_coding_agent.transcript import span_seconds


class _Event:
    """The two fields ``TurnStream.feed`` reads off an AgentEvent."""

    def __init__(self, type_: str, timestamp: int | None) -> None:
        self.type = type_
        self.timestamp = timestamp
        self.turn_index = 0


def _span(*stamps: int | None) -> list[dict]:
    return [{"role": "assistant", "timestamp": s} for s in stamps]


class TestLiveLaneClock:
    def test_span_is_first_event_to_last(self):
        stream = TurnStream(lane="main")
        for event in (
            _Event("turn_start", 1_700_000_000_000),
            _Event("tool_execution_start", 1_700_000_002_500),
            _Event("turn_start", 1_700_000_007_000),
        ):
            stream.feed(event)
        assert stream.elapsed_seconds == 7.0

    def test_a_lane_that_saw_one_event_reports_no_span(self):
        """Not 0.0 — an exchange with nothing to measure says so."""
        stream = TurnStream(lane="main")
        stream.feed(_Event("turn_start", 1_700_000_000_000))
        assert stream.elapsed_seconds is None

    def test_events_without_a_clock_do_not_fabricate_one(self):
        stream = TurnStream(lane="main")
        stream.feed(_Event("turn_start", None))
        stream.feed(_Event("turn_start", None))
        assert stream.elapsed_seconds is None


class TestReloadedSpan:
    def test_first_to_last_message_timestamp(self):
        assert span_seconds(_span(1_700_000_000_000, None, 1_700_000_009_250)) == 9.25

    def test_one_stamped_message_is_not_a_span(self):
        assert span_seconds(_span(1_700_000_000_000, None)) is None

    def test_a_legacy_session_reports_nothing_rather_than_zero(self):
        """Loading mapped the fabricated zeros to None, so nothing is measurable
        and the summary omits the duration exactly as it did before."""
        assert span_seconds(_span(None, None, None)) is None

    def test_live_and_reloaded_agree_on_the_same_turn(self):
        """The parity claim, as an equality: both read the same clock."""
        stamps = [1_700_000_000_000, 1_700_000_004_000, 1_700_000_009_250]
        stream = TurnStream(lane="main")
        for stamp in stamps:
            stream.feed(_Event("turn_start", stamp))
        assert stream.elapsed_seconds == span_seconds(_span(*stamps))
