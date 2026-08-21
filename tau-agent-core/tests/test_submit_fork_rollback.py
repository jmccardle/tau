"""AgentSession.submit() — the "fork" and "rollback" multitask strategies.

Reference: docs/SUBMISSION-LIFECYCLE.md, "fork" / decision 2 (phase 2, parts 2-3).
Reference: docs/NODE-ADDRESSABLE-AGENTS.md §2 (I1), decision 7 / T5 (entries() is
total).

fork's tree/session machinery (BranchView, open_branch, ctx.spawn_branch) is
already covered by test_spawn_branch.py; these tests pin submit()'s OWN
responsibilities: the turn-complete admission check, scheduling a SUPERVISED
background task rather than awaiting one, and not touching the in-flight turn.
rollback is exercised end-to-end (real _turn_lock contention, a real abort, a
real navigate) because its correctness is entirely about that interleaving.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_llm.streaming import TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import LANE_KEY, InMemorySessionLog
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
    """The minimal ``stream_simple`` return shape ``_stream_response`` consumes
    (mirrors conftest.py's ``fake_llm`` fixture, inlined here so each test
    controls exactly WHEN the reply lands via its own gate)."""

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


# =============================================================================
# fork: the admission check
# =============================================================================


class TestForkAdmission:
    async def test_fork_rejects_a_turn_incomplete_point(self):
        """The concrete admission check the spec requires: an assistant message
        with a dangling toolCall must not become a fork point."""
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": [{"type": "text", "text": "go"}]})
        # append_message (not the explicit-parent append_at) so the log's own
        # cursor actually moves here — this is what fork's admission reads.
        log.append_message(
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "call_1", "name": "read", "arguments": {}}],
            }
        )
        # NOTE: no toolResult appended — a crash-truncated turn, or (as here) a
        # log built to look like one.
        session = AgentSession(session_log=log, model=_model(), tools=[])

        result = await session.submit(_sub("continue", "fork-1", multitask_strategy="fork"))

        assert result.accepted is False
        assert result.rejection_reason is not None
        assert "not turn-complete" in result.rejection_reason
        assert "call_1" in result.rejection_reason
        assert session._forked_tasks == {}, "a rejected fork spawns nothing"

    async def test_fork_accepts_a_turn_complete_point_and_does_not_wait(self, monkeypatch):
        """accepted=True comes back before the branch's own turn has run at
        all — there is no caller left to await it (decision 2)."""
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        session = AgentSession(session_log=log, model=_model(), tools=[])

        started = asyncio.Event()
        release = asyncio.Event()

        async def _gated_prompt(self, text, images=None, context=None):
            started.set()
            await release.wait()
            return []

        monkeypatch.setattr(AgentSession, "prompt", _gated_prompt)

        result = await session.submit(_sub("branch off", "fork-2", multitask_strategy="fork"))

        assert result.accepted is True
        assert result.messages == []
        # The branch has not necessarily even started yet — submit() did not wait.
        assert "fork-2" in session._forked_tasks

        await asyncio.wait_for(started.wait(), timeout=1.0)
        release.set()
        await asyncio.wait_for(session._forked_tasks["fork-2"], timeout=1.0)
        # Done-callback cleanup: the registry does not accumulate finished tasks.
        assert session._forked_tasks == {}

    async def test_fork_does_not_move_or_touch_the_primary_cursor(self, monkeypatch):
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        # tip is captured AFTER construction: __init__ itself appends the W2
        # agent_spec provenance node (a customEntry, moving the cursor).
        session = AgentSession(session_log=log, model=_model(), tools=[])
        tip = log.cursor

        async def _work(self, text, images=None, context=None):
            self._session_log.append_message(
                {"role": "assistant", "content": [{"type": "text", "text": "BRANCH ONLY"}]}
            )
            return []

        monkeypatch.setattr(AgentSession, "prompt", _work)

        result = await session.submit(_sub("branch off", "fork-3", multitask_strategy="fork"))
        assert result.accepted is True
        await asyncio.wait_for(session._forked_tasks["fork-3"], timeout=1.0)

        assert log.cursor == tip, "fork must never move the primary cursor"
        tagged = [e for e in log.entries() if LANE_KEY in e]
        assert tagged, "the branch's work is lane-tagged, not on the primary path"
        primary = ConversationTree(log.entries(), log.cursor).context_for()
        assert "BRANCH ONLY" not in str(primary)

    async def test_fork_before_root_is_admitted(self, monkeypatch):
        """target_id=None (no messages yet) is trivially turn-complete."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        async def _work(self, text, images=None, context=None):
            return []

        monkeypatch.setattr(AgentSession, "prompt", _work)
        result = await session.submit(_sub("go", "fork-4", multitask_strategy="fork"))
        assert result.accepted is True


# =============================================================================
# fork: the supervised task registry
# =============================================================================


class TestForkTaskRegistry:
    async def test_abort_cancels_a_running_fork(self, monkeypatch):
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        started = asyncio.Event()

        async def _slow_prompt(self, text, images=None, context=None):
            started.set()
            await asyncio.sleep(30)
            return []  # pragma: no cover — cancelled long before this returns

        monkeypatch.setattr(AgentSession, "prompt", _slow_prompt)

        result = await session.submit(_sub("go", "fork-5", multitask_strategy="fork"))
        assert result.accepted is True
        task = session._forked_tasks["fork-5"]
        await asyncio.wait_for(started.wait(), timeout=1.0)

        session.abort()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        assert task.cancelled()
        assert session._forked_tasks == {}, "the done-callback must untrack a cancelled fork"

    async def test_emit_session_shutdown_drains_forked_tasks(self, monkeypatch):
        """The 'must not become an orphan on session close' half: shutdown
        cancels AND waits, so nothing outlives it."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        started = asyncio.Event()

        async def _slow_prompt(self, text, images=None, context=None):
            started.set()
            await asyncio.sleep(30)
            return []  # pragma: no cover

        monkeypatch.setattr(AgentSession, "prompt", _slow_prompt)

        result = await session.submit(_sub("go", "fork-6", multitask_strategy="fork"))
        assert result.accepted is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await asyncio.wait_for(session.emit_session_shutdown(), timeout=1.0)
        assert session._forked_tasks == {}

    async def test_a_raising_fork_surfaces_through_the_extension_error_sink(self, monkeypatch):
        """Fail-Early: an unmodelled exception from the fork task (spawn_branch
        itself contains modelled failures as BranchResult(ok=False)) must not
        vanish as an untraced asyncio 'Task exception was never retrieved'."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        surfaced: list = []
        session._surface_extension_error = lambda err: surfaced.append(err)  # type: ignore[method-assign]

        async def _boom(parent_id, prompt, **kwargs):
            raise RuntimeError("spawn_branch itself blew up")

        monkeypatch.setattr(session._extension_api.context, "spawn_branch", _boom, raising=False)

        result = await session.submit(_sub("go", "fork-7", multitask_strategy="fork"))
        assert result.accepted is True
        # Let the task run to its (raising) completion.
        for _ in range(5):
            await asyncio.sleep(0)

        assert session._forked_tasks == {}
        assert len(surfaced) == 1
        assert "blew up" in surfaced[0].error


# =============================================================================
# rollback
# =============================================================================


class TestRollback:
    async def test_rollback_with_nothing_in_flight_is_a_plain_admission(self):
        """No turn running -> nothing to discard: no navigate entry, the new
        turn simply appends at the current cursor."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=lambda model, context, options=None: _Stream("ok"),
        ):
            result = await session.submit(_sub("hello", "r-1", multitask_strategy="rollback"))

        assert result.accepted is True
        assert not any(e.get("type") == "navigate" for e in log.entries())

    async def test_rollback_aborts_the_in_flight_turn_and_resumes_from_pre_turn_leaf(self):
        """The full interleaving: submission A is genuinely in flight (blocked
        on the fake provider), submission B rolls back — aborting A, waiting
        for it to unwind (which still persists whatever A produced), then
        navigating back to A's OWN pre-turn leaf and running there."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate = asyncio.Event()
        replies = iter(["A's reply", "B's reply"])

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream(next(replies))

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_a = asyncio.create_task(
                session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)
            assert session.is_streaming is True
            pre_a_leaf = session._pre_turn_leaf  # what rollback SHOULD target

            task_b = asyncio.create_task(
                session.submit(_sub("turn B", "b-1", multitask_strategy="rollback"))
            )
            await asyncio.sleep(0)
            assert session._abort_signal.is_aborted() is True, "rollback must abort A"

            gate.set()  # let A's (and then B's) stream_simple call return
            result_a = await asyncio.wait_for(task_a, timeout=1.0)
            result_b = await asyncio.wait_for(task_b, timeout=1.0)

        assert result_a.accepted is True
        assert result_b.accepted is True

        # Decision 7 / T5: entries() stays total. A's abandoned user+assistant
        # nodes (and the navigate marker rollback wrote) are still present.
        entries = log.entries()
        a_user = [
            e
            for e in entries
            if e.get("type") == "message"
            and e.get("message", {}).get("content")
            and "turn A" in str(e["message"]["content"])
        ]
        assert a_user, "A's user message must still be in entries() — nothing was un-said"
        assert any(e.get("type") == "navigate" for e in entries), "rollback's marker is on-disk"

        # But it is off the ACTIVE path: B's turn is parented at pre_a_leaf, not
        # at anything A appended.
        active = ConversationTree(entries, log.cursor).context_for()
        assert "turn A" not in str(active)
        assert "A's reply" not in str(active)
        assert "turn B" in str(active)

        b_user_entry = next(
            e
            for e in entries
            if e.get("type") == "message" and "turn B" in str(e.get("message", {}).get("content"))
        )
        assert b_user_entry["parentId"] == pre_a_leaf

    async def test_rollback_targets_continue_conversations_own_pre_turn_leaf(self):
        """Review fix, must_fix #1, reproduction 1: ``continue_conversation()``
        never recorded ``_pre_turn_leaf`` at all, so a rollback submitted while
        it was in flight read whatever the session-level slot last held (stale,
        or ``None`` on a session that had never run a ``submit()``-driven turn)
        and silently un-pathed the conversation via ``append_navigate(None)``.
        It must now target the cursor immediately before THIS continuation
        started, exactly as it does for a submit()-driven turn."""
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": [{"type": "text", "text": "seed"}]})
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate = asyncio.Event()
        replies = iter(["continued", "rolled back"])

        async def _gated_stream_simple(model, context, options=None):
            await gate.wait()
            return _Stream(next(replies))

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_continue = asyncio.create_task(session.continue_conversation())
            await asyncio.sleep(0)
            assert session.is_streaming is True
            pre_continue_leaf = session._pre_turn_leaf
            assert pre_continue_leaf is not None, (
                "continue_conversation() must record a pre-turn leaf now — "
                "before the fix this attribute was never written by this method"
            )

            task_rollback = asyncio.create_task(
                session.submit(_sub("steer away", "rb-1", multitask_strategy="rollback"))
            )
            await asyncio.sleep(0)
            assert session._abort_signal.is_aborted() is True, (
                "rollback must abort the continuation"
            )

            gate.set()
            await asyncio.wait_for(task_continue, timeout=1.0)
            result = await asyncio.wait_for(task_rollback, timeout=1.0)

        assert result.accepted is True

        entries = log.entries()
        active = ConversationTree(entries, log.cursor).context_for()
        assert "steer away" in str(active)
        assert "continued" not in str(active), (
            "the continuation's reply must fall off the active path"
        )

        rollback_user_entry = next(
            e
            for e in entries
            if e.get("type") == "message"
            and "steer away" in str(e.get("message", {}).get("content"))
        )
        assert rollback_user_entry["parentId"] == pre_continue_leaf

    async def test_rollback_refuses_when_a_queued_turn_completed_first(self):
        """Review fix, must_fix #1, reproduction 2 (the FIFO variant): turn A is
        in flight, submission B is queued behind it with "enqueue", and
        submission C arrives with "rollback". asyncio.Lock is FIFO (the class
        docstring's "Known limitation"), so C's abort signal reaches A, but the
        SLOT — once A unwinds — is granted to B (queued first), which runs a
        FULL turn to completion before C ever resumes. C must detect that the
        turn it aborted (A) is no longer the turn whose slot it just acquired
        and refuse, rather than navigating back to A's pre-turn leaf and
        silently discarding B's successfully-completed turn from the active
        path."""
        log = InMemorySessionLog()
        session = AgentSession(session_log=log, model=_model(), tools=[])

        gate_a = asyncio.Event()
        gate_b = asyncio.Event()
        replies = iter(["A's reply", "B's reply"])
        calls: list[int] = []

        async def _gated_stream_simple(model, context, options=None):
            calls.append(1)
            await (gate_a if len(calls) == 1 else gate_b).wait()
            return _Stream(next(replies))

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
            task_a = asyncio.create_task(
                session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)
            assert session.is_streaming is True

            task_b = asyncio.create_task(
                session.submit(_sub("turn B", "b-1", multitask_strategy="enqueue"))
            )
            await asyncio.sleep(0)  # B is now queued FIFO-behind A on _turn_lock

            task_c = asyncio.create_task(
                session.submit(_sub("turn C", "c-1", multitask_strategy="rollback"))
            )
            await asyncio.sleep(0)
            assert session._abort_signal.is_aborted() is True, "rollback must abort A"

            gate_a.set()  # let A's stream_simple call return; A unwinds and releases the lock
            await asyncio.wait_for(task_a, timeout=1.0)
            # Drain the loop so B (granted the lock next, per FIFO) reaches ITS
            # OWN stream_simple call and blocks there — deterministic because
            # nothing else is runnable in between.
            for _ in range(10):
                await asyncio.sleep(0)
            gate_b.set()
            result_b = await asyncio.wait_for(task_b, timeout=1.0)
            result_c = await asyncio.wait_for(task_c, timeout=1.0)

        assert result_b.accepted is True
        assert result_c.accepted is False, (
            "C must refuse: navigating to A's pre-turn leaf here would discard "
            "B's already-completed turn from the active path"
        )
        assert result_c.rejection_reason is not None

        active = ConversationTree(log.entries(), log.cursor).context_for()
        assert "turn B" in str(active), "B's completed turn must still be on the active path"
        assert "B's reply" in str(active)
