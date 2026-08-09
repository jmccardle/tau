"""The submission SPAN on the bus — ``submission_start`` / ``submission_end`` (B3-a).

docs/SUBMISSION-LIFECYCLE.md, end of "Phasing": a renderer that groups one
user→answer exchange needs the SUBMISSION boundary, and the ``AgentEvent`` stream
does not carry one — ``agent_start``/``agent_end`` bracket a ``loop.run()``, and a
followUp re-entry runs a second one inside a single ``submit()``. These two
channels are that boundary. They ride separate string channels (like
``branch_event``) rather than new ``AgentEvent.type`` members, so the ``type``
Literal stays closed and every existing ``subscribe()`` consumer is untouched.

What this file pins is exactly what a renderer depends on: the span exists, it
brackets the events of the turn it names, it closes even when the turn raises, a
dispatched command / consumed input opens no span at all, and the ``text`` it
carries is the POST-``input``-hook text that actually went to the model.
"""

from __future__ import annotations

import pytest

from tau_ai.types import Model
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


def _record_span(session: AgentSession) -> list[tuple[str, dict]]:
    """Subscribe to both span channels, appending ``(channel, kwargs)`` in order."""
    seen: list[tuple[str, dict]] = []
    session.subscribe_channel("submission_start", lambda **kw: seen.append(("start", kw)))
    session.subscribe_channel("submission_end", lambda **kw: seen.append(("end", kw)))
    return seen


@pytest.mark.usefixtures("fake_llm")
class TestSubmissionSpan:
    async def test_an_ordinary_turn_is_bracketed_by_one_span(self):
        session = _session()
        seen = _record_span(session)

        sub = Submission(text="hello", source="bus", submitter="nats_bus", submission_id="sid-1")
        result = await session.submit(sub)

        assert result.accepted is True
        assert [channel for channel, _ in seen] == ["start", "end"]
        assert seen[0][1]["submission"] is sub
        assert seen[0][1]["text"] == "hello"
        assert seen[1][1]["submission"] is sub

    async def test_the_span_encloses_that_turns_agent_events(self):
        """Ordering is the whole contract: a renderer opens on start, routes the
        events in between, closes on end. If an ``AgentEvent`` escaped the bracket
        it would land in no lane (or the previous one's)."""
        session = _session()
        order: list[str] = []
        session.subscribe_channel("submission_start", lambda **kw: order.append("start"))
        session.subscribe_channel("submission_end", lambda **kw: order.append("end"))
        session.subscribe(lambda event: order.append(event.type))

        await session.submit(
            Submission(text="hi", source="interactive", submitter="human", submission_id="s")
        )

        assert order[0] == "start"
        assert order[-1] == "end"
        assert "agent_start" in order and "agent_end" in order

    async def test_two_submissions_produce_two_disjoint_spans(self):
        session = _session()
        seen = _record_span(session)

        await session.submit(
            Submission(text="one", source="interactive", submitter="human", submission_id="s1")
        )
        await session.submit(
            Submission(text="two", source="webhook", submitter="hook-42", submission_id="s2")
        )

        assert [channel for channel, _ in seen] == ["start", "end", "start", "end"]
        ids = [kw["submission"].submission_id for _, kw in seen]
        assert ids == ["s1", "s1", "s2", "s2"]

    async def test_end_carries_the_side_usage_this_submission_spent_off_the_loop(self):
        """The tokens no ``message_end`` reports (auto-compaction's summarizer, an
        extension's ``ctx.complete()``). A renderer summing the bus alone would
        understate the exchange, so the delta is computed where the ledger lives."""
        session = _session()
        seen = _record_span(session)

        # Bill something to the side ledger from inside the turn — the shape an
        # auto-compaction takes — by reacting to the turn's own first event.
        def _spend(event):
            if event.type == "agent_start":
                session.record_side_usage(
                    {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
                )

        session.subscribe(_spend)

        await session.submit(
            Submission(text="hi", source="interactive", submitter="human", submission_id="s")
        )

        end = next(kw for channel, kw in seen if channel == "end")
        assert end["side_usage"]["input_tokens"] == 7
        assert end["side_usage"]["output_tokens"] == 3
        assert end["side_usage"]["total_tokens"] == 10

    async def test_a_dispatched_command_opens_no_span(self):
        """A command runs no model call and produces no exchange to render. An
        opened-and-immediately-closed span would leave an empty box on screen."""
        session = _session()
        seen = _record_span(session)

        result = await session.submit(
            Submission(
                text="/compact",
                source="interactive",
                submitter="human",
                submission_id="s",
                expand_commands=True,
            )
        )

        assert result.command is not None and result.command.name == "compact"
        assert seen == []

    async def test_the_span_closes_when_the_turn_raises(self):
        """A span left open renders as a permanently "Working…" exchange — the
        silent-hang shape this lifecycle exists to remove."""
        session = _session()
        seen = _record_span(session)

        async def _boom(*args, **kwargs):
            raise RuntimeError("provider exploded")

        session._run_one_turn = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="provider exploded"):
            await session.submit(
                Submission(text="hi", source="timer", submitter="cron", submission_id="s")
            )

        assert [channel for channel, _ in seen] == ["start", "end"]

    async def test_start_carries_the_post_input_hook_text(self):
        """The renderer shows what actually went to the model. Rendering the
        pre-hook text would put a user bubble on screen that the transcript and
        the session log both disagree with."""
        session = _session()
        seen = _record_span(session)

        async def _rewrite(text, images, **_kw):
            return {"handled": False, "prompt": text.upper(), "images": images}

        session._extension_runner.emit_input = _rewrite  # type: ignore[method-assign]
        session._extension_runner.has_handlers = lambda name: name == "input"  # type: ignore[method-assign]

        await session.submit(
            Submission(text="hello", source="bus", submitter="nats", submission_id="s")
        )

        start = next(kw for channel, kw in seen if channel == "start")
        assert start["text"] == "HELLO"


@pytest.mark.usefixtures("fake_llm")
class TestSubscribeChannel:
    async def test_unsubscribe_actually_detaches(self):
        session = _session()
        seen: list[str] = []
        unsub = session.subscribe_channel("submission_start", lambda **kw: seen.append("x"))

        await session.submit(
            Submission(text="a", source="interactive", submitter="human", submission_id="s1")
        )
        unsub()
        await session.submit(
            Submission(text="b", source="interactive", submitter="human", submission_id="s2")
        )

        assert seen == ["x"]
