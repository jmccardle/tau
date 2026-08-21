"""AgentSession.submit() — reentrant self-submission (review fix, must_fix #2).

Reference: docs/SUBMISSION-LIFECYCLE.md, decision 3 ("Self-submission depth is a
hard cap that raises") and "Deliberately not adopted" / "No advisory reentrancy
flag".

Before this fix, a hook (``input``/``tool_call``/``turn_end``/``user_turn_end``)
dispatched from INSIDE a submission-driven turn that called back into
``session.submit()``/``session.prompt()``/``ctx.prompt()`` — the shape every one
of those hooks is documented to support — deadlocked silently: every
``multitask_strategy`` either inspects or waits on ``_turn_lock``, which the very
task making the reentrant call already holds, so nothing could ever release it.
``submit()`` now detects same-task reentrancy by comparing ``asyncio.current_task()``
against the task recorded when the outer turn was admitted, and raises immediately
instead. A DIFFERENT task submitting concurrently is unaffected — that is exactly
what "enqueue" is for, and must keep working.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_llm.types import Model
from tau_agent_core.agent_session import AgentSession
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


class TestReentrantSubmissionRaises:
    @pytest.mark.usefixtures("fake_llm")
    async def test_turn_body_calling_prompt_raises_instead_of_hanging(self, monkeypatch):
        """The reviewer's exact reproduction shape: the turn body itself (any
        hook is dispatched from here) calls session.prompt() before the outer
        turn has returned. Wrapped in wait_for as a belt-and-suspenders net —
        if the fix regresses, this fails on a timeout instead of hanging the
        whole test run."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        captured: list[BaseException] = []

        async def _reentrant_run_one_turn(self, *args, **kwargs):
            try:
                await self.prompt("nested")
            except BaseException as err:  # noqa: BLE001 — re-raised immediately below
                captured.append(err)
                raise
            raise AssertionError("session.prompt() should have raised, not returned")

        monkeypatch.setattr(AgentSession, "_run_one_turn", _reentrant_run_one_turn)

        with pytest.raises(RuntimeError, match="reentrant"):
            await asyncio.wait_for(session.prompt("outer"), timeout=2.0)

        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)

    @pytest.mark.usefixtures("fake_llm")
    async def test_input_hook_calling_ctx_prompt_is_surfaced_not_hung(self):
        """The literal reported shape: an extension's ``input`` handler calls
        ``ctx.prompt()``. emit_input's own contract (established, unrelated to
        this fix) is Fail-Closed: a handler exception is SURFACED via
        on_error and dispatch continues — it does not propagate raw out of
        submit(). So the outer prompt() still completes normally; what this
        pins is that it completes AT ALL (no hang) and that the surfaced
        error names the reentrancy, rather than the coroutine just stopping
        forever with nothing to see."""

        def ext(api):
            async def on_input(event, ctx):
                await ctx.prompt("nested")

            api.on("input", on_input)

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[ext]
        )
        surfaced: list = []
        # ExtensionRunner.on_error() supports multiple listeners; add ours
        # alongside the session's own rather than replacing
        # session._surface_extension_error, which ExtensionRunner already
        # captured as a bound-method value at __init__ time (reassigning the
        # attribute afterward would not reach a reference the runner already
        # holds).
        session._extension_runner.on_error(lambda err: surfaced.append(err))

        messages = await asyncio.wait_for(session.prompt("outer"), timeout=2.0)

        assert messages, "the outer turn must still complete once the reentrant call is refused"
        assert len(surfaced) == 1
        assert "reentrant" in surfaced[0].error

    @pytest.mark.usefixtures("fake_llm")
    async def test_a_second_task_submitting_concurrently_is_not_reentrant(self):
        """The negative case the fix must not break: two DIFFERENT asyncio
        tasks submitting to the same session is genuine concurrency, not
        self-reentrancy — "enqueue" must still wait for the first turn and
        then run, not raise."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])

        result_a = await session.submit(
            Submission(
                text="first",
                source="interactive",
                submitter="human",
                submission_id="first-1",
                multitask_strategy="enqueue",
            )
        )
        result_b = await session.submit(
            Submission(
                text="second",
                source="interactive",
                submitter="human",
                submission_id="second-1",
                multitask_strategy="enqueue",
            )
        )

        assert result_a.accepted is True
        assert result_b.accepted is True
