"""AgentSession.submit() — self-submission depth propagation (decision 3).

Reference: docs/SUBMISSION-LIFECYCLE.md, decision 3 ("Self-submission depth is a
hard cap that raises") and "Deliberately not adopted" / "No advisory reentrancy
flag".

The defect these pin: ``Submission.depth``, ``MAX_SUBMISSION_DEPTH`` and
``submit()``'s cap check all existed, but NO call site anywhere in any of the four
src trees ever incremented ``depth``. The counter was structurally always zero, so
the cap was decorative and the check under it was dead code — a self-continuing
extension (``turn_end`` hook spawns a task, the task submits, that turn fires
``turn_end`` again) looped forever with nothing to stop it.

``submit()` now DERIVES the depth from
:data:`~tau_agent_core.submission.DRIVING_SUBMISSION_DEPTH`, a context var it
publishes for the lifetime of each turn it admits. The distinction these tests
exist to pin is causal-vs-temporal: a task SPAWNED INSIDE a turn inherits that
turn's depth (``asyncio.Task`` copies the ambient context at creation) even if it
does not reach ``submit()`` until after the turn ended, while a task that PREDATES
the turn — an extension's bus subscription loop, a timer — inherits nothing and
stays at depth 0 however much traffic it delivers mid-turn. Getting that backwards
in either direction is a live bug: the first loses the runaway loop the cap exists
for, the second locks a busy bus out of its own session after ten messages.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_llm.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionContext
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import (
    DRIVING_SUBMISSION_DEPTH,
    MAX_SUBMISSION_DEPTH,
    Submission,
    next_submission_depth,
)


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


def _self_sub(text: str, submission_id: str) -> Submission:
    """The shape a self-continuing extension submits: source="extension", enqueue.

    ``depth`` is deliberately left at its default — the whole point is that the
    submitter does not have to know, and could not be trusted to say.
    """
    return Submission(
        text=text,
        source="extension",
        submitter="self-continuer",
        submission_id=submission_id,
        multitask_strategy="enqueue",
    )


# =============================================================================
# next_submission_depth: the derivation itself
# =============================================================================


class TestNextSubmissionDepth:
    def test_outside_any_turn_the_declared_depth_stands(self):
        assert DRIVING_SUBMISSION_DEPTH.get() is None
        assert next_submission_depth() == 0
        assert next_submission_depth(4) == 4

    def test_inside_a_turn_it_is_one_deeper(self):
        token = DRIVING_SUBMISSION_DEPTH.set(3)
        try:
            assert next_submission_depth() == 4
        finally:
            DRIVING_SUBMISSION_DEPTH.reset(token)

    def test_a_relayed_chain_is_a_floor_not_an_override(self):
        """``declared`` describes a chain relayed from outside this process; the
        context var describes the in-process one. Taking the larger can only
        tighten the bound, which is the safe direction for a guard."""
        token = DRIVING_SUBMISSION_DEPTH.set(1)
        try:
            assert next_submission_depth(7) == 7
        finally:
            DRIVING_SUBMISSION_DEPTH.reset(token)


# =============================================================================
# Propagation through a real submission chain
# =============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestDepthPropagation:
    async def test_a_self_continuing_extension_climbs_and_raises_at_the_cap(self):
        """The runaway decision 3 exists for, run end to end.

        An extension's hook spawns the next submission from a task created inside
        the turn — a DIFFERENT asyncio task, so the same-task reentrancy guard
        (test_submit_reentrant.py) correctly does not fire, and before this fix
        nothing else did either. Each link now inherits the previous turn's depth
        + 1, so the chain terminates: eleven turns run (depths 0..10) and the
        twelfth submission raises.
        """
        held: list[AgentSession] = []
        depths: list[int] = []
        spawned: list[asyncio.Task] = []

        def ext(api):
            async def on_input(event, ctx):
                session = held[0]
                assert session._current_submission is not None
                depths.append(session._current_submission.depth)
                # The self-continuation: a task created from inside the turn.
                spawned.append(
                    asyncio.create_task(session.submit(_self_sub("again", f"chain-{len(depths)}")))
                )

            api.on("input", on_input)

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[ext]
        )
        held.append(session)

        await asyncio.wait_for(session.prompt("outer"), timeout=10.0)

        # Drain the chain: awaiting link i runs its turn, whose own hook appends
        # link i+1. The loop ends when a link refuses to spawn — i.e. when the
        # cap fires.
        errors: list[BaseException] = []
        i = 0
        while i < len(spawned):
            # A regression here (depth not propagating) makes the chain endless;
            # fail loudly rather than hanging the suite on a self-feeding loop.
            assert i < 3 * MAX_SUBMISSION_DEPTH, (
                "the chain never terminated — the depth cap is not bounding self-submission"
            )
            try:
                await asyncio.wait_for(spawned[i], timeout=10.0)
            except RuntimeError as err:
                errors.append(err)
            i += 1

        # Inherited (+1) at every link, never reset: the interactive prompt is
        # depth 0 and each self-submission is exactly one deeper.
        assert depths == list(range(MAX_SUBMISSION_DEPTH + 1))

        assert len(errors) == 1, "exactly one link should be refused — the one past the cap"
        message = str(errors[0])
        assert str(MAX_SUBMISSION_DEPTH + 1) in message, "the raise must name the depth reached"
        assert str(MAX_SUBMISSION_DEPTH) in message, "the raise must name the cap"
        assert "MAX_SUBMISSION_DEPTH" in message

        # Fail-Early: a refusal, not a silent drop, and nothing left half-held.
        assert session._turn_lock.locked() is False
        assert session._current_submission is None

    async def test_the_admitted_record_carries_the_derived_depth(self):
        """``submit()`` ``replace()``s the record, so ``_current_submission`` (what
        ``_stamp_event`` copies provenance from) reports the depth the turn was
        ADMITTED at, not the zero the submitter constructed it with."""
        held: list[AgentSession] = []
        seen: list[Submission] = []
        spawned: list[asyncio.Task] = []

        def ext(api):
            async def on_input(event, ctx):
                session = held[0]
                assert session._current_submission is not None
                seen.append(session._current_submission)
                if len(seen) == 1:
                    spawned.append(
                        asyncio.create_task(session.submit(_self_sub("again", "nested-1")))
                    )

            api.on("input", on_input)

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[ext]
        )
        held.append(session)

        await asyncio.wait_for(session.prompt("outer"), timeout=10.0)
        await asyncio.wait_for(spawned[0], timeout=10.0)

        assert [s.depth for s in seen] == [0, 1]
        # Everything else about the record survives the replace().
        assert seen[1].submission_id == "nested-1"
        assert seen[1].source == "extension"
        assert seen[1].submitter == "self-continuer"
        assert seen[1].multitask_strategy == "enqueue"

    async def test_a_task_predating_the_turn_does_not_inherit_depth(self):
        """The negative case, and the reason this is a context var rather than a
        session attribute: an extension's long-lived subscription task delivering
        a bus message DURING a turn is concurrency, not self-submission. If mere
        temporal overlap counted, a busy bus would climb to the cap and lock
        itself out of its own session."""
        held: list[AgentSession] = []
        depths: list[int] = []
        ready = asyncio.Event()

        def ext(api):
            async def on_input(event, ctx):
                session = held[0]
                assert session._current_submission is not None
                depths.append(session._current_submission.depth)
                ready.set()
                # Yield enough for the pre-existing task below to actually reach
                # submit() and block on the in-flight turn's lock, so this is
                # genuine mid-turn concurrency and not a sequential replay.
                for _ in range(5):
                    await asyncio.sleep(0)

            api.on("input", on_input)

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[ext]
        )
        held.append(session)

        async def subscription_loop():
            await ready.wait()
            return await session.submit(_self_sub("bus message", "bus-1"))

        # Created BEFORE any turn exists — an extension's own long-lived task.
        outside = asyncio.create_task(subscription_loop())
        await asyncio.sleep(0)  # let it reach the await, so its context is fixed

        await asyncio.wait_for(session.prompt("outer"), timeout=10.0)
        result = await asyncio.wait_for(outside, timeout=10.0)

        assert result.accepted is True
        assert depths == [0, 0], "a submission not originated inside the turn stays at depth 0"

    async def test_the_context_is_reset_when_the_turn_ends(self):
        """Sequential top-level turns do not accumulate — the token is reset in
        submit()'s finally, so a human's tenth message is still depth 0."""
        held: list[AgentSession] = []
        depths: list[int] = []

        def ext(api):
            async def on_input(event, ctx):
                session = held[0]
                assert session._current_submission is not None
                depths.append(session._current_submission.depth)

            api.on("input", on_input)

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[ext]
        )
        held.append(session)

        for _ in range(3):
            await asyncio.wait_for(session.prompt("hello"), timeout=10.0)
            assert DRIVING_SUBMISSION_DEPTH.get() is None

        assert depths == [0, 0, 0]


# =============================================================================
# fork: the one strategy with no lock to guard it
# =============================================================================


class TestForkInheritsDepth:
    async def test_the_branch_task_is_spawned_inside_the_depth_context(self, monkeypatch):
        """``fork`` never touches ``_turn_lock``, so the depth cap is the ONLY
        bound on a fork chain. The branch's task must therefore be created while
        the fork submission's depth is published, so the branch's own first
        ``prompt()`` is admitted one deeper."""
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        session = AgentSession(session_log=log, model=_model(), tools=[])

        seen: list[int | None] = []

        async def _recording_spawn_branch(self, parent_id, prompt, **kwargs):
            seen.append(DRIVING_SUBMISSION_DEPTH.get())
            return None

        monkeypatch.setattr(ExtensionContext, "spawn_branch", _recording_spawn_branch)

        result = await session.submit(
            Submission(
                text="branch off",
                source="extension",
                submitter="forker",
                submission_id="fork-depth-1",
                multitask_strategy="fork",
                depth=4,
            )
        )
        assert result.accepted is True
        await asyncio.wait_for(session._forked_tasks["fork-depth-1"], timeout=10.0)

        # The branch inherits 4, so its own prompt() is admitted at 5.
        assert seen == [4]
        assert next_submission_depth_in(seen[0]) == 5

        # And the context does not leak past the (non-blocking) fork submission.
        assert DRIVING_SUBMISSION_DEPTH.get() is None


def next_submission_depth_in(driving: int | None) -> int:
    """``next_submission_depth`` as the branch's own submit() would compute it."""
    token = DRIVING_SUBMISSION_DEPTH.set(driving)
    try:
        return next_submission_depth()
    finally:
        DRIVING_SUBMISSION_DEPTH.reset(token)
