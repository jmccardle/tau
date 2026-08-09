"""Tests for tau_agent_core.events — AgentEvent type from SUBPHASE-0.0.md.

Tests verify:
- AgentEvent can be instantiated
- All required event types are valid
- Event fields are properly typed
- Timestamp is ms since epoch
- is_error defaults to False
- Conditional fields work correctly by event type
"""

import time
from typing import get_args

import pytest

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import SubmissionSource


class TestAgentEventCreation:
    """Tests for AgentEvent instantiation."""

    def test_create_agent_event(self):
        """AgentEvent can be instantiated with type and timestamp."""
        event = AgentEvent(
            type="agent_start",
            timestamp=int(time.time() * 1000),
        )
        assert event.type == "agent_start"
        assert event.timestamp > 0

    def test_agent_event_defaults(self):
        """AgentEvent has sensible defaults."""
        event = AgentEvent(
            type="agent_start",
            timestamp=0,
        )
        assert event.is_error is False
        assert event.message is None
        assert event.turn_index is None
        assert event.tool_call_id is None
        assert event.tool_name is None
        assert event.args is None
        assert event.result is None
        assert event.tool_results is None
        assert event.messages is None


class TestAgentEventTypes:
    """Tests for all valid AgentEvent types."""

    # All valid event types from SUBPHASE-0.0.md
    VALID_TYPES = [
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
    ]

    @pytest.mark.parametrize("event_type", VALID_TYPES)
    def test_all_event_types_valid(self, event_type):
        """All documented event types should be valid."""
        event = AgentEvent(
            type=event_type,
            timestamp=0,
        )
        assert event.type == event_type

    def test_agent_start_event(self):
        """agent_start event fires when agent begins processing."""
        event = AgentEvent(
            type="agent_start",
            timestamp=int(time.time() * 1000),
        )
        assert event.type == "agent_start"

    def test_agent_end_event(self):
        """agent_end event fires when agent finishes processing."""
        event = AgentEvent(
            type="agent_end",
            timestamp=int(time.time() * 1000),
            messages=[],
        )
        assert event.type == "agent_end"
        assert isinstance(event.messages, list)

    def test_turn_start_event(self):
        """turn_start event fires at the beginning of a turn."""
        event = AgentEvent(
            type="turn_start",
            timestamp=int(time.time() * 1000),
            turn_index=0,
        )
        assert event.type == "turn_start"
        assert event.turn_index == 0

    def test_turn_end_event(self):
        """turn_end event fires at the end of a turn."""
        event = AgentEvent(
            type="turn_end",
            timestamp=int(time.time() * 1000),
            turn_index=0,
            tool_results=[],
        )
        assert event.type == "turn_end"
        assert event.turn_index == 0
        assert isinstance(event.tool_results, list)

    def test_message_start_event(self):
        """message_start event fires when assistant begins responding."""
        event = AgentEvent(
            type="message_start",
            timestamp=int(time.time() * 1000),
            message={"role": "assistant", "content": []},
        )
        assert event.type == "message_start"
        assert event.message is not None

    def test_message_update_event(self):
        """message_update event fires during streaming."""
        event = AgentEvent(
            type="message_update",
            timestamp=int(time.time() * 1000),
            message={"role": "assistant", "content": [{"type": "text", "text": "H"}]},
        )
        assert event.type == "message_update"

    def test_message_end_event(self):
        """message_end event fires when assistant response is complete."""
        event = AgentEvent(
            type="message_end",
            timestamp=int(time.time() * 1000),
            message={"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        )
        assert event.type == "message_end"

    def test_tool_execution_start_event(self):
        """tool_execution_start fires when tool execution begins."""
        event = AgentEvent(
            type="tool_execution_start",
            timestamp=int(time.time() * 1000),
            tool_call_id="call_123",
            tool_name="ls",
            args={"path": "."},
        )
        assert event.type == "tool_execution_start"
        assert event.tool_call_id == "call_123"
        assert event.tool_name == "ls"

    def test_tool_execution_update_event(self):
        """tool_execution_update fires during tool execution progress."""
        event = AgentEvent(
            type="tool_execution_update",
            timestamp=int(time.time() * 1000),
            tool_call_id="call_123",
            tool_name="ls",
        )
        assert event.type == "tool_execution_update"

    def test_tool_execution_end_event(self):
        """tool_execution_end fires when tool execution completes."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=int(time.time() * 1000),
            tool_call_id="call_123",
            tool_name="ls",
            result="file1.txt\nfile2.py",
        )
        assert event.type == "tool_execution_end"
        assert event.result == "file1.txt\nfile2.py"


class TestAgentEventErrorState:
    """Tests for AgentEvent error states."""

    def test_error_event_has_is_error_true(self):
        """Events with errors should have is_error=True."""
        event = AgentEvent(
            type="agent_start",
            timestamp=int(time.time() * 1000),
            is_error=True,
        )
        assert event.is_error is True

    def test_non_error_event_defaults_false(self):
        """Events without errors should have is_error=False."""
        event = AgentEvent(
            type="agent_start",
            timestamp=int(time.time() * 1000),
        )
        assert event.is_error is False

    def test_tool_execution_error(self):
        """tool_execution_end can represent an error."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=int(time.time() * 1000),
            tool_call_id="call_123",
            tool_name="bash",
            result="Error: command failed",
            is_error=True,
        )
        assert event.is_error is True


class TestAgentEventTimestamps:
    """Tests for AgentEvent timestamp behavior."""

    def test_timestamp_is_integer(self):
        """Event timestamp must be an integer."""
        event = AgentEvent(
            type="agent_start",
            timestamp=1700000000000,
        )
        assert isinstance(event.timestamp, int)

    def test_timestamp_is_milliseconds(self):
        """Event timestamp should be in ms since epoch."""
        ts_ms = int(time.time() * 1000)
        event = AgentEvent(type="agent_start", timestamp=ts_ms)
        assert event.timestamp == ts_ms

    def test_timestamps_are_increasing(self):
        """Multiple events should have increasing timestamps."""
        events = []
        for i in range(10):
            events.append(
                AgentEvent(
                    type="agent_start",
                    timestamp=int(time.time() * 1000) + i * 100,
                )
            )

        for i in range(1, len(events)):
            assert events[i].timestamp > events[i - 1].timestamp


class TestAgentEventConditionalFields:
    """Tests for type-specific conditional fields."""

    def test_agent_start_has_no_turn_index(self):
        """agent_start should not carry turn_index."""
        event = AgentEvent(type="agent_start", timestamp=0, turn_index=None)
        assert event.turn_index is None

    def test_agent_start_carries_message(self):
        """agent_start can carry an initial message."""
        event = AgentEvent(
            type="agent_start",
            timestamp=0,
            message={"role": "user", "content": "Hello"},
        )
        assert event.message is not None

    def test_agent_end_carries_messages(self):
        """agent_end should carry the list of produced messages."""
        event = AgentEvent(
            type="agent_end",
            timestamp=0,
            messages=[
                {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
            ],
        )
        assert isinstance(event.messages, list)
        assert len(event.messages) >= 0

    def test_turn_start_has_turn_index(self):
        """turn_start should carry turn_index."""
        event = AgentEvent(
            type="turn_start",
            timestamp=0,
            turn_index=0,
        )
        assert event.turn_index == 0

    def test_turn_end_carries_tool_results(self):
        """turn_end should carry tool_results list."""
        event = AgentEvent(
            type="turn_end",
            timestamp=0,
            turn_index=0,
            tool_results=[],
        )
        assert isinstance(event.tool_results, list)

    def test_tool_events_have_tool_fields(self):
        """Tool execution events should have tool_call_id and tool_name."""
        event = AgentEvent(
            type="tool_execution_start",
            timestamp=0,
            tool_call_id="call_123",
            tool_name="ls",
            args={"path": "."},
        )
        assert event.tool_call_id == "call_123"
        assert event.tool_name == "ls"
        assert event.args == {"path": "."}

    def test_message_events_have_message_field(self):
        """Message events should carry message data."""
        event = AgentEvent(
            type="message_start",
            timestamp=0,
            message={"role": "assistant", "content": []},
        )
        assert event.message is not None

    def test_tool_execution_end_has_result(self):
        """tool_execution_end should carry the result."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=0,
            tool_call_id="call_123",
            tool_name="ls",
            result="output",
        )
        assert event.result == "output"


class TestAgentEventProvenance:
    """docs/SUBMISSION-LIFECYCLE.md "Provenance on events" (phase 2): submission_id /
    source / submitter / correlation, stamped by AgentSession.submit() onto every event
    a submission-driven turn emits. Wiring is exercised in tau-agent-core's
    agent_session tests; this class is the model's own contract."""

    def test_provenance_defaults_to_none(self):
        """No partial-provenance state: an event from a call that never went
        through submit() (continue_conversation()) carries all four as None."""
        event = AgentEvent(type="agent_start", timestamp=0)
        assert event.submission_id is None
        assert event.source is None
        assert event.submitter is None
        assert event.correlation is None

    @pytest.mark.parametrize("source", get_args(SubmissionSource))
    def test_every_submission_source_is_a_valid_event_source(self, source):
        event = AgentEvent(
            type="agent_start",
            timestamp=0,
            submission_id="11111111-1111-1111-1111-111111111111",
            source=source,
            submitter=f"probe-{source}",
            correlation={"subject": "agent.turn.request"},
        )
        assert event.source == source


class TestAgentEventRoundTrip:
    """Parity enforcement (docs/SUBMISSION-LIFECYCLE.md): every AgentEvent must
    survive model_dump() -> reconstruct, or it is carrying something that will break
    the JSON and webserver renderers three hops downstream — the same property
    test_submission.py already proves for Submission/SubmissionResult."""

    def _round_trip(self, event: AgentEvent) -> AgentEvent:
        return AgentEvent(**event.model_dump())

    def test_round_trip_a_bare_event(self):
        event = AgentEvent(type="agent_start", timestamp=0)
        assert self._round_trip(event) == event

    def test_round_trip_with_full_provenance(self):
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=1700000000000,
            tool_call_id="call_123",
            tool_name="bash",
            result="ok",
            submission_id="sid-1",
            source="bus",
            submitter="nats_bus",
            correlation={"subject": "agent.turn.request", "binding_id": 7, "tags": ["a", "b"]},
        )
        rebuilt = self._round_trip(event)
        assert rebuilt == event
        # A plain, independent structure — mutating the dump must not alias the
        # live event (the same aliasing hazard test_submission.py pins for
        # Submission.correlation).
        dumped = event.model_dump()
        dumped["correlation"]["binding_id"] = 999
        assert event.correlation is not None
        assert event.correlation["binding_id"] == 7

    @pytest.mark.parametrize("source", get_args(SubmissionSource))
    def test_round_trip_every_submission_source(self, source):
        event = AgentEvent(
            type="agent_start", timestamp=0, submission_id="sid", source=source, submitter="x"
        )
        assert self._round_trip(event) == event
