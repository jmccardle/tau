"""AgentSession.submit() — the phase-1 admission surface itself.

Reference: docs/SUBMISSION-LIFECYCLE.md, "The one door" step 1 and decisions 1/3/4.

Review-flagged spec gap: "the phase-1 admission surface has no tests at all" —
every behaviour below was hand-verified by the reviewer against the shipped code
and found correct, but nothing in the suite pinned it against regression. This
file covers exactly the six cases the review named: "reject" returning
``accepted=False`` while a turn is in flight, "enqueue" waiting-then-running,
an unhandled strategy raising ``NotImplementedError`` (that case WAS "steer",
until phase 4 shipped it — its behaviour now lives in test_submit_steer.py), the
depth-cap raise, ``store_history=False`` persisting nothing, and
``continue_conversation()``'s ``RuntimeError`` when a turn is already in flight.

``silent=True``'s case changed with B1-c: it used to be admitted (behaving as a
plain ``store_history=False``, with its renderer-suppression half doing nothing)
and now raises. The three inert-field behaviours B1-c settled — that raise,
``expand_commands``'s, and ``allow_user_input``'s enforcement — have their own
file, test_submission_capabilities.py.

fork/rollback have their own file (test_submit_fork_rollback.py); provenance
stamping has its own (test_submit_provenance.py); the ``Submission``/
``SubmissionResult`` dataclasses have theirs (test_submission.py). This file is
scoped to what submit() itself DOES with a strategy, not the data model or the
tree-surgery strategies.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_ai.streaming import TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import MAX_SUBMISSION_DEPTH, Submission


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _sub(text: str, submission_id: str, **overrides) -> Submission:
    fields = {
        "text": text,
        "source": "interactive",
        "submitter": "human",
        "submission_id": submission_id,
    }
    fields.update(overrides)
    return Submission(**fields)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """The minimal ``stream_simple`` return shape (mirrors test_submit_fork_rollback.py)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._text, partial=_assistant(self._text))

        return _gen()

    async def result(self):
        return _assistant(self._text)

    def abort(self):
        pass


class TestReject:
    async def test_reject_returns_accepted_false_without_blocking_or_raising(self):
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate = asyncio.Event()

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream("A's reply")

        from unittest.mock import patch

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_a = asyncio.create_task(
                session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)
            assert session.is_streaming is True

            # "reject" must return immediately — neither blocking on A nor raising.
            result_b = await asyncio.wait_for(
                session.submit(_sub("turn B", "b-1", multitask_strategy="reject")),
                timeout=1.0,
            )
            assert result_b.accepted is False
            assert result_b.rejection_reason == "a turn is already in flight"
            assert result_b.messages == []

            gate.set()
            await asyncio.wait_for(task_a, timeout=1.0)


class TestEnqueue:
    async def test_enqueue_waits_for_the_in_flight_turn_then_runs(self):
        """Distinct from send_user_message(deliver_as="nextTurn"): B is
        GUARANTEED to run within this call, not merely parked for later."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate = asyncio.Event()
        replies = iter(["A's reply", "B's reply"])

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream(next(replies))

        from unittest.mock import patch

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_a = asyncio.create_task(
                session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)
            task_b = asyncio.create_task(
                session.submit(_sub("turn B", "b-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)

            gate.set()
            result_a = await asyncio.wait_for(task_a, timeout=1.0)
            result_b = await asyncio.wait_for(task_b, timeout=1.0)

        assert result_a.accepted is True
        assert result_b.accepted is True
        active = ConversationTree(log.entries(), log.cursor).context_for()
        assert "turn A" in str(active)
        assert "turn B" in str(active), "B must actually run, not stay parked"


class TestUnknownStrategy:
    async def test_unknown_strategy_raises_rather_than_falling_through(self):
        """The ``else`` arm that used to hold ``steer``'s phase-4 gap.

        ``steer`` shipped (see test_submit_steer.py), so every member of
        ``MultitaskStrategy`` is now handled and this arm is reachable only by a
        value outside the Literal. It must still refuse: a submitter that asked
        for a strategy and silently got whichever branch is last has been lied to
        about the concurrency semantics of its own turn.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        with pytest.raises(NotImplementedError, match="not a known strategy"):
            await session.submit(_sub("go", "s-1", multitask_strategy="whenever"))

        # Named gap, not a silent fallback: nothing was admitted.
        assert session.is_streaming is False
        assert session._turn_lock.locked() is False


class TestDepthCap:
    async def test_depth_over_cap_raises_before_anything_runs(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        with pytest.raises(RuntimeError, match="MAX_SUBMISSION_DEPTH"):
            await session.submit(
                _sub("go", "d-1", depth=MAX_SUBMISSION_DEPTH + 1, multitask_strategy="reject")
            )

        assert session.is_streaming is False
        assert session._turn_lock.locked() is False


@pytest.mark.usefixtures("fake_llm")
class TestStoreHistoryAndSilent:
    async def test_store_history_false_answers_but_persists_nothing(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        before = list(session._session_log.entries())

        result = await session.submit(_sub("go", "sh-1", store_history=False))

        assert result.accepted is True
        assert result.messages, "the model still answers the turn"
        assert session._session_log.entries() == before, "but nothing is written to the log"

    async def test_silent_true_raises_rather_than_half_honouring_itself(self):
        """B1-c: ``silent`` promises MORE than the store_history fold it performs.

        It used to be admitted and behave exactly like ``store_history=False`` —
        the renderer-suppression half it is named for did nothing at all, which
        under Fail-Early is worse than an absent field because it reads as a
        working feature. It now raises, naming the block that lands it — the same
        treatment ``multitask_strategy="steer"`` got until phase 4 implemented it
        rather than describing it. The construction-time fold
        (``__post_init__``) is untouched and still tested in test_submission.py.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        before = list(session._session_log.entries())

        with pytest.raises(NotImplementedError, match="silent=True"):
            await session.submit(_sub("go", "sh-2", silent=True))

        # Named gap, not a silent fallback: nothing was admitted, nothing ran.
        assert session.is_streaming is False
        assert session._turn_lock.locked() is False
        assert session._session_log.entries() == before


class TestContinueConversationGuard:
    async def test_continue_conversation_raises_when_a_submit_turn_is_in_flight(self):
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate = asyncio.Event()

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream("A's reply")

        from unittest.mock import patch

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_a = asyncio.create_task(
                session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)
            assert session.is_streaming is True

            with pytest.raises(RuntimeError, match="already in flight"):
                await session.continue_conversation()

            gate.set()
            await asyncio.wait_for(task_a, timeout=1.0)


class TestOnAdmittedTiming:
    """Phase-2 review B2/S2 (docs/REMOTE-CONTROL.md §4[3] C3).

    ``on_admitted`` must fire ONLY on a path that will actually run a turn
    (B2) — before the fix it fired for a resolved command too, which is what
    let the RPC layer's ``_submit_and_acknowledge`` treat "admitted" as
    "the response is already on the wire" and silently discard the
    ``SubmissionResult.command`` a resolved command carries (see
    test_rpc.py's B2 tests for the wire-level half of this). And calling it
    must not be able to wedge ``_turn_lock`` even if it raises (S2).
    """

    async def test_on_admitted_fires_for_an_ordinary_turn(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        fired = []

        async def _stream_simple(model, context, options=None):
            return _Stream("ok")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            result = await session.submit(_sub("hi", "s-1"), on_admitted=lambda: fired.append(True))

        assert fired == [True]
        assert result.accepted is True
        assert result.command is None

    async def test_on_admitted_does_not_fire_for_a_resolved_command(self):
        """B2's root cause, pinned directly: a submission that resolves to a
        command via ``expand_commands`` never reaches the model, so
        ``on_admitted`` — whose whole contract is "this call is now
        committed to running a turn" — must not fire for it."""

        def my_ext(api):
            api.register_command("ledger", {"description": "d", "handler": lambda a, c: "42"})

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[my_ext]
        )
        fired = []

        result = await session.submit(
            _sub("/ledger", "s-1", expand_commands=True),
            on_admitted=lambda: fired.append(True),
        )

        assert fired == []
        assert result.accepted is True
        assert result.command is not None
        assert result.command.name == "ledger"
        assert result.command.performer == "core"
        assert result.command.output == "42"

    async def test_on_admitted_does_not_fire_when_an_input_hook_consumes_the_submission(self):
        def my_ext(api):
            api.on("input", lambda event, ctx: {"handled": True})

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[my_ext]
        )
        fired = []

        result = await session.submit(_sub("hi", "s-1"), on_admitted=lambda: fired.append(True))

        assert fired == []
        assert result.accepted is True
        assert result.messages == []

    async def test_a_raising_on_admitted_still_releases_the_turn_lock(self):
        """S2: ``on_admitted`` is called inside ``submit()``'s own
        try/finally (moved there by the same B2 fix), so a raising callback
        unwinds through the SAME cleanup an ordinary turn gets —
        ``_turn_lock`` is released rather than wedged, and a second
        ``submit()`` afterwards is not permanently blocked behind it."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        def _boom():
            raise RuntimeError("boom")

        async def _stream_simple(model, context, options=None):
            return _Stream("ok")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            with pytest.raises(RuntimeError, match="boom"):
                await session.submit(_sub("hi", "s-1"), on_admitted=_boom)

            assert session._turn_lock.locked() is False

            # A second call must not hang behind a wedged lock.
            result = await asyncio.wait_for(session.submit(_sub("hi", "s-2")), timeout=2.0)
            assert result.accepted is True
