"""Ctrl+C at a REPL prompt — the state table of docs/REPL-HEAD.md §5.

Three states and three answers: idle exits, streaming aborts the turn and hands
the steering buffer back, and aborting does nothing — which is what makes a
second press safe rather than a way to hang waiting for a turn that is already
unwinding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tau_coding_agent.repl_input import INTERRUPT, RECLAIM

from repl_fakes import FakeBackend, ReplEnv, env  # noqa: F401
from test_repl_steering import TOOLLESS_TURN, TURN_WITH_A_TOOL, _arm

#: No test here may outlive this: a hang is the failure being guarded against.
TIMEOUT = 5.0


async def _run(env: ReplEnv, lines: list[str], **kwargs: Any) -> int:
    """Drive one scripted run under a timeout."""
    return await asyncio.wait_for(env.run(lines, **kwargs), timeout=TIMEOUT)


async def test_ctrl_c_at_an_idle_prompt_exits_cleanly(env: ReplEnv) -> None:
    """Nothing is asked and nothing is aborted: the loop ends and the teardown runs."""
    rc = await _run(env, [INTERRUPT])
    assert rc == 0
    assert env.backend is not None
    assert env.backend.aborts == 0
    assert env.submissions == []
    assert env.events[-3:] == ["detach", "close_all", "shutdown:quit"]


async def test_ctrl_c_during_a_turn_aborts_it_and_says_so(env: ReplEnv) -> None:
    """Nothing on the render stream says a turn was aborted, so the head does —
    at ``lane_end``, where the partial answer has already been persisted."""
    env.install(_arm(TURN_WITH_A_TOOL))
    rc = await _run(env, ["do it", INTERRUPT])
    assert rc == 0
    assert env.backend is not None
    assert env.backend.aborts == 1
    assert "⏹ aborted" in env.text


async def test_the_prompt_is_usable_after_an_abort(env: ReplEnv) -> None:
    """The read the press landed on stays outstanding, so the next line is read
    and — the aborted turn having ended — runs as its own turn."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await _run(env, ["do it", INTERRUPT, "what happened?"])
    assert [sub.text for sub in env.submissions] == ["do it", "what happened?"]


async def test_a_second_press_mid_abort_does_nothing_and_does_not_hang(env: ReplEnv) -> None:
    """The abort is already sent; pressing again must not send a second one, and
    must not end the loop while the turn is still unwinding."""
    env.install(_arm(TURN_WITH_A_TOOL))
    rc = await _run(env, ["do it", INTERRUPT, INTERRUPT, INTERRUPT])
    assert rc == 0
    assert env.backend is not None
    assert env.backend.aborts == 1


async def test_an_abort_hands_the_pending_buffer_back(env: ReplEnv) -> None:
    """The turn the line was aimed at is gone: delivering it would start a turn
    the user just stopped, and dropping it would lose what they typed.

    A turn with no tool call in it, so the line is still ``pending`` — never
    handed to any door — when the press lands.
    """
    env.install(_arm(TOOLLESS_TURN))
    await _run(env, ["do it", "use ripgrep", INTERRUPT])
    assert env.reader.drafts == ["use ripgrep"]
    assert [sub.text for sub in env.submissions] == ["do it"]


async def test_an_abort_hands_back_a_delivery_the_core_never_wove_in(env: ReplEnv) -> None:
    """``AgentSession.abort`` clears ``_pending_steer_messages`` and tells nobody,
    so text accepted with ``messages=[]`` stays the head's until ``steer_message``."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await _run(env, ["do it", "use ripgrep", INTERRUPT, INTERRUPT])
    assert env.reader.drafts == ["use ripgrep"]


async def test_a_delivery_reclaimed_before_its_task_ran_is_not_submitted(env: ReplEnv) -> None:
    """The tool call takes the buffer and the abort takes it back; the submission
    that was already scheduled must not put the line into the turn anyway."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await _run(env, ["do it", "use ripgrep", INTERRUPT])
    assert [sub.multitask_strategy for sub in env.submissions] == ["enqueue"]


async def test_up_on_an_empty_prompt_reclaims_the_buffer(env: ReplEnv) -> None:
    """Reclaim is a gesture, not only an abort's side effect: the text comes back
    editable and the buffer is empty afterwards, so it is in exactly one place."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await _run(env, ["do it", "use ripgrep", RECLAIM])
    assert env.reader.drafts == ["use ripgrep"]
    assert [sub.text for sub in env.submissions] == ["do it"]


async def test_a_cancelled_ask_does_not_abort_the_turn(env: ReplEnv) -> None:
    """A press inside an ask cancels the ask and nothing else — the ``--resume``
    picker is the one ask this step can drive, and it declines without aborting."""
    await _run(env, ["hello"])
    env.install()
    rc = await _run(env, [], answers=[INTERRUPT], resume=True)
    assert rc == 0
    assert env.backend is None


async def test_the_reader_keeps_no_handler_once_the_loop_has_ended(env: ReplEnv) -> None:
    """A handler left installed would abort a turn of a session that is gone, so
    a Ctrl+C after the run ends the read the way an unhandled press does."""
    await _run(env, [INTERRUPT])
    env.reader._lines = [INTERRUPT, "still here?"]
    assert await env.reader.read() is None
