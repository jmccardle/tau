"""Task marshalling: submit_threadsafe(), and submit()'s foreign-loop raise.

Reference: docs/SUBMISSION-LIFECYCLE.md phase 4, "Task marshalling".

The spec's sentence is the whole test plan: ``submit()`` is safe only from the
session's own loop, so it must RAISE from a foreign one "rather than working by
accident", because "a silent fallback here produces exactly the 'bus disconnected
randomly' class of bug that is investigated as a network problem for hours."
Neovim's E5560 (``:h api-fast`` / ``vim.schedule_wrap``) and Textual's
``call_from_thread`` are the prior art, and both do the same two things: refuse
the fast-context call, and name the fix in the message. So does this.

The detection is what these tests are really pinning, because a false positive
would break every legitimate call in the codebase. Three shapes have to be told
apart:

- a genuinely FOREIGN loop, running concurrently with the session's own — raise;
- a plain thread with no loop at all — raise;
- a NEW loop after the previous one is dead — allow, and rebind. This is not a
  corner case: half this suite calls ``asyncio.run(session.prompt(...))`` more
  than once against one long-lived session (test_agent_session.py:436-438), and
  each of those calls is legitimately a different event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from unittest.mock import patch

import pytest

from tau_llm.streaming import TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission


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
        "source": "bus",
        "submitter": "nats_bus",
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
    """The minimal ``stream_simple`` return shape (mirrors test_submit_admission.py)."""

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


async def _eventually(predicate, what: str, timeout: float = 2.0) -> None:
    """Wait for a task done-callback to land, then let the caller assert.

    ``_run_threadsafe_submission`` resolves the caller's ``concurrent.futures``
    future from INSIDE the coroutine — ``future.set_result(result)`` runs, and
    only then does the coroutine return and the task become done, which is what
    fires ``add_done_callback``. So the future is resolved BEFORE
    ``_on_threadsafe_task_done`` runs, by construction, and how many loop
    iterations separate the two is the scheduler's business.

    Asserting the bookkeeping immediately after ``await asyncio.wrap_future``
    therefore asserts an ordering the code has never promised. It held on 3.11
    and stopped holding on 3.13, where it failed in CI on the 0.9.3 tag.

    Waiting keeps the assertion that matters — the callback DOES run, so a
    finished submission is untracked and its exception is surfaced — and drops
    the accidental one. A callback that never runs still fails, on the timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.005)


class TestSubmitRefusesAForeignCaller:
    async def test_submit_from_a_different_live_loop_raises_and_names_the_fix(self):
        """The E5560 case: another loop, running at the same time as ours."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        # Constructed inside this test's loop, so the session is already bound to
        # it — and this loop stays running for the whole of the to_thread await
        # below, which is what makes the other loop genuinely foreign rather than
        # a successor.
        assert session._loop is asyncio.get_running_loop()
        before = list(session._session_log.entries())

        def _submit_on_another_loop() -> None:
            asyncio.run(session.submit(_sub("from a foreign loop", "f-1")))

        with pytest.raises(RuntimeError) as excinfo:
            await asyncio.to_thread(_submit_on_another_loop)

        message = str(excinfo.value)
        assert "submit_threadsafe" in message, "the raise must name the fix, as E5560 does"
        assert "different event loop" in message
        # Refused at the door: nothing was admitted, so the session is untouched.
        assert session.is_streaming is False
        assert session._turn_lock.locked() is False
        assert session._session_log.entries() == before

    async def test_submit_from_a_plain_thread_with_no_loop_raises(self):
        """No running loop at all — a coroutine driven by something that is not asyncio.

        ``session.submit(sub)`` merely builds a coroutine when called from a plain
        thread; the refusal has to happen when it is DRIVEN, so drive it by hand.
        That is also the only way to reach this branch: a thread that reaches for
        ``asyncio.run`` has a loop and takes the foreign-loop branch above.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        before = list(session._session_log.entries())

        def _drive_without_a_loop() -> None:
            coro = session.submit(_sub("from a plain thread", "f-2"))
            try:
                coro.send(None)
            finally:
                coro.close()

        with pytest.raises(RuntimeError) as excinfo:
            await asyncio.to_thread(_drive_without_a_loop)

        message = str(excinfo.value)
        assert "submit_threadsafe" in message
        assert "no running event loop" in message
        assert session._session_log.entries() == before

    def test_sequential_asyncio_run_against_one_session_is_unaffected(self, fake_llm):
        """The false positive that would matter most: a NEW loop, not a foreign one.

        ``asyncio.run`` creates and closes a loop per call, so the second prompt
        here runs on a different loop object than the first. A naive "compare
        against the loop captured at first use" check would refuse it and break the
        pattern much of this suite is written in.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        assert session._loop is None, "constructed outside a loop, so nothing to capture"

        first = asyncio.run(session.prompt("one"))
        second = asyncio.run(session.prompt("two"))

        assert first and second
        active = str(
            ConversationTree(
                session._session_log.entries(), session._session_log.cursor
            ).context_for()
        )
        assert "one" in active and "two" in active


@pytest.mark.usefixtures("fake_llm")
class TestSubmitThreadsafeDelivers:
    async def test_a_foreign_thread_marshals_a_turn_that_actually_runs(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        # The driver thread never touches this loop directly: it hands the
        # submission over and gets a concurrent.futures.Future back.
        future = await asyncio.to_thread(
            lambda: session.submit_threadsafe(_sub("hello from the bus", "t-1"))
        )
        assert isinstance(future, concurrent.futures.Future)

        result = await asyncio.wrap_future(future)

        assert result.accepted is True
        assert result.submission_id == "t-1"
        assert result.messages, "the turn ran, not merely queued"
        active = str(
            ConversationTree(
                session._session_log.entries(), session._session_log.cursor
            ).context_for()
        )
        assert "hello from the bus" in active
        await _eventually(
            lambda: session._threadsafe_tasks == {},
            "the done-callback to untrack a finished task",
        )

    async def test_a_foreign_loop_marshals_a_turn_that_actually_runs(self):
        """The other foreign context: a thread that runs its own event loop."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        def _from_another_loop() -> concurrent.futures.Future:
            async def _hand_over():
                return session.submit_threadsafe(_sub("from loop B", "t-2"))

            return asyncio.run(_hand_over())

        future = await asyncio.to_thread(_from_another_loop)
        result = await asyncio.wrap_future(future)

        assert result.accepted is True
        active = str(
            ConversationTree(
                session._session_log.entries(), session._session_log.cursor
            ).context_for()
        )
        assert "from loop B" in active

    async def test_the_submitted_strategy_survives_marshalling(self):
        """ "reject" must still reject — a serial drainer would silently make it "enqueue".

        ``nats_bus`` chose ``"reject"`` because "silently queueing would make the
        agent answer a stale utterance minutes later". A marshalling layer that
        ran each submission only after the previous one finished would override
        that decision on the way in, which is exactly the per-source divergence
        this lifecycle exists to delete.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        gate = asyncio.Event()

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream("first reply")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            first = await asyncio.to_thread(
                lambda: session.submit_threadsafe(
                    _sub("first", "r-1", multitask_strategy="enqueue")
                )
            )
            # Let the loop pick the marshalled submission up and admit it.
            while not session.is_streaming:
                await asyncio.sleep(0)

            second = await asyncio.to_thread(
                lambda: session.submit_threadsafe(_sub("second", "r-2"))
            )
            refused = await asyncio.wrap_future(second)
            assert refused.accepted is False
            assert refused.rejection_reason == "a turn is already in flight"

            gate.set()
            assert (await asyncio.wrap_future(first)).accepted is True

    async def test_an_exception_reaches_the_caller_and_the_error_surface(self):
        """A marshalled failure is not silently dropped in either direction."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        surfaced: list[str] = []
        session._surface_extension_error = lambda error: surfaced.append(error.extension_path)

        future = await asyncio.to_thread(
            # ``silent=True`` is submit()'s own NotImplementedError — a real raise
            # from inside submit(), with no LLM or transport involved.
            lambda: session.submit_threadsafe(_sub("boom", "e-1", silent=True))
        )
        with pytest.raises(NotImplementedError):
            await asyncio.wrap_future(future)

        await _eventually(
            lambda: surfaced == ["submit_threadsafe:e-1"],
            "the failure to reach the session's error sink",
        )
        assert session._threadsafe_tasks == {}


class TestSubmitThreadsafeRefusals:
    def test_unbound_session_raises_rather_than_queueing_into_a_void(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        assert session._loop is None

        with pytest.raises(RuntimeError, match="not bound to a running"):
            session.submit_threadsafe(_sub("nowhere to go", "u-1"))

    async def test_calling_it_from_the_sessions_own_loop_raises(self):
        """Textual's call_from_thread rule: the future could only be completed by
        the very loop that would be blocked waiting for it."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        with pytest.raises(RuntimeError, match="OWN event loop"):
            session.submit_threadsafe(_sub("wrong door", "u-2"))

    async def test_a_future_cancelled_before_the_loop_reaches_it_runs_nothing(self):
        """The race the caller can win: cancel between handing over and pickup.

        Driven through :meth:`_accept_threadsafe` directly because the window is
        otherwise unobservable from this loop — every ``await`` that would let a
        driver thread run also lets the loop drain the callback that starts the
        turn, so a test that cancelled after an ``await`` would be cancelling a
        finished submission and pinning nothing.
        """
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        before = list(session._session_log.entries())

        future: concurrent.futures.Future = concurrent.futures.Future()
        assert future.cancel() is True
        session._accept_threadsafe(_sub("never mind", "u-3"), None, future)

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert session._threadsafe_tasks == {}
        assert session._session_log.entries() == before, "a cancelled submission runs no turn"

    async def test_a_reused_submission_id_is_refused_not_silently_untracked(self):
        """The registry is keyed by id, so a duplicate would drop a live task's
        only strong reference — supervision and the shutdown drain with it."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        gate = asyncio.Event()

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream("first reply")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            first = await asyncio.to_thread(
                lambda: session.submit_threadsafe(
                    _sub("first", "dup-1", multitask_strategy="enqueue")
                )
            )
            while not session.is_streaming:
                await asyncio.sleep(0)
            running_task = session._threadsafe_tasks["dup-1"]

            clash = await asyncio.to_thread(
                lambda: session.submit_threadsafe(_sub("second", "dup-1"))
            )
            with pytest.raises(RuntimeError, match="already in flight"):
                await asyncio.wrap_future(clash)

            assert session._threadsafe_tasks["dup-1"] is running_task
            gate.set()
            assert (await asyncio.wrap_future(first)).accepted is True


class TestLoopBinding:
    async def test_emit_session_start_binds_the_loop_for_a_driver_thread(self):
        """A driver that opens its subscription in ``session_start`` must be able to
        marshal from its own thread before any submission has been made."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        session._loop = None  # as if constructed from synchronous code

        await session.emit_session_start()

        assert session._loop is asyncio.get_running_loop(), (
            "binding must happen before the zero-extension fast path returns"
        )

    async def test_shutdown_drains_a_marshalled_submission_in_flight(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
        gate = asyncio.Event()

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream("never arrives")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            future = await asyncio.to_thread(
                lambda: session.submit_threadsafe(_sub("long turn", "s-1"))
            )
            while not session.is_streaming:
                await asyncio.sleep(0)
            assert "s-1" in session._threadsafe_tasks

            await asyncio.wait_for(session.emit_session_shutdown(), timeout=2.0)

        assert session._threadsafe_tasks == {}
        assert future.done(), "the caller's future is resolved rather than left hanging"
