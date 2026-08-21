"""AgentSession.submit() stamps AgentEvent provenance (docs/SUBMISSION-LIFECYCLE.md
"Provenance on events", phase 2).

The AgentEvent model's own round-trip/shape contract lives in test_events.py; this
file pins the WIRING — that a real submit()-driven turn actually stamps the events
its AgentLoop emits, that continue_conversation() (predating the Submission
contract) does not, and that nothing filters events by provenance — every
subscriber still sees every event (Jupyter's rule: render differently, never drop).
"""

from __future__ import annotations

import pytest

from tau_llm.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


@pytest.mark.usefixtures("fake_llm")
class TestSubmitStampsProvenance:
    async def test_every_event_from_a_submission_driven_turn_is_stamped(self):
        session = _session()
        seen = []
        session.subscribe(seen.append)

        sub = Submission(
            text="hello",
            source="bus",
            submitter="nats_bus",
            submission_id="sid-123",
            correlation={"subject": "agent.turn.request", "binding_id": 7},
        )
        result = await session.submit(sub)
        assert result.accepted is True
        assert seen, "the fake_llm turn must have emitted at least one event"

        for event in seen:
            assert event.submission_id == "sid-123"
            assert event.source == "bus"
            assert event.submitter == "nats_bus"
            assert event.correlation == {"subject": "agent.turn.request", "binding_id": 7}

    async def test_a_second_submission_stamps_its_own_provenance_not_the_first(self):
        """_current_submission is per-turn, not sticky — successive submissions
        must not bleed their provenance into each other."""
        session = _session()
        seen = []
        session.subscribe(seen.append)

        await session.submit(
            Submission(text="one", source="interactive", submitter="human", submission_id="s1")
        )
        await session.submit(
            Submission(text="two", source="webhook", submitter="hook-42", submission_id="s2")
        )

        first_turn = [e for e in seen if e.submission_id == "s1"]
        second_turn = [e for e in seen if e.submission_id == "s2"]
        assert first_turn and second_turn
        assert all(e.source == "interactive" for e in first_turn)
        assert all(e.source == "webhook" for e in second_turn)

    async def test_prompt_wrapper_stamps_interactive_human(self):
        """prompt() builds source='interactive'/submitter='human' — unchanged
        behaviour, now visible on the events it produces too."""
        session = _session()
        seen = []
        session.subscribe(seen.append)

        await session.prompt("hi")

        assert seen
        assert all(e.source == "interactive" for e in seen)
        assert all(e.submitter == "human" for e in seen)
        assert all(e.submission_id is not None for e in seen)

    async def test_continue_conversation_does_not_stamp_provenance(self):
        """continue_conversation() predates the Submission contract (the OTHER
        of the "two unguarded doors") and has no submission to attribute events
        to — an honest None, not a fabricated one."""
        session = _session()
        await session.prompt("hi")

        seen = []
        session.subscribe(seen.append)
        await session.continue_conversation()

        assert seen
        assert all(e.submission_id is None for e in seen)
        assert all(e.source is None for e in seen)

    async def test_no_subscriber_is_filtered_by_provenance(self):
        """Jupyter's rule, not the obvious one: a subscriber gets EVERY event
        regardless of source — deciding how to render is the subscriber's job,
        never the bus's."""
        session = _session()
        bus_seen, human_seen, all_seen = [], [], []
        session._events.on("all", all_seen.append)

        def _split(event):
            (bus_seen if event.source == "bus" else human_seen).append(event)

        session.subscribe(_split)

        await session.submit(
            Submission(
                text="from a human", source="interactive", submitter="human", submission_id="h1"
            )
        )
        await session.submit(
            Submission(text="from the bus", source="bus", submitter="nats_bus", submission_id="b1")
        )

        assert bus_seen and human_seen
        assert len(all_seen) == len(bus_seen) + len(human_seen)
