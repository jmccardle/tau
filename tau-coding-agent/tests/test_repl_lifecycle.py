"""The REPL head's startup, submission and shutdown — docs/REPL-HEAD.md §3, §5.

What a head OWES the core is an order, not a look: extensions loaded, ONE render
subscription, ``session_start`` after both, and a teardown that detaches, closes
and disposes exactly once. This file pins that order, the submission record every
typed line becomes, and the two ways the loop ends.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.flows import Performed, View
from tau_agent_core.submission import SubmissionResult

from repl_fakes import FakeBackend, ReplEnv, env, performed_result  # noqa: F401


async def test_startup_and_shutdown_run_in_the_documented_order(env: ReplEnv) -> None:
    """§3: bind, resolver, the UI delegate BEFORE extensions load, ONE
    subscribe_render, session_start — then detach, close_all and dispose."""
    rc = await env.run([])
    assert rc == 0
    assert env.events == [
        "bind_session_log",
        "set_model_resolver",
        "set_ui_delegate",
        "load_extensions",
        "subscribe_render",
        "session_start:startup",
        "detach",
        "close_all",
        "shutdown:quit",
    ]


async def test_the_render_subscription_is_taken_once(env: ReplEnv) -> None:
    """Not once per turn: a second subscription renders every token twice."""
    await env.run(["one", "two", "three"])
    assert env.events.count("subscribe_render") == 1


async def test_a_typed_line_becomes_the_documented_submission(env: ReplEnv) -> None:
    """§5: interactive/human, enqueue, commands expanded, input allowed — and
    ``context=None``, which is "the bound log's context_for" (§3)."""
    await env.run(["explain this"])
    assert env.backend is not None
    submission = env.backend.submissions[0]
    assert submission.text == "explain this"
    assert submission.source == "interactive"
    assert submission.submitter == "human"
    assert submission.multitask_strategy == "enqueue"
    assert submission.expand_commands is True
    assert submission.allow_user_input is True
    assert env.backend.contexts == [None]


async def test_the_head_binds_and_appends_nothing(env: ReplEnv) -> None:
    """§3 "bind, do not append": the core is the log's only writer, so the head
    hands the session over and never writes an entry of its own."""
    await env.run(["hello"])
    assert env.backend is not None
    session = env.backend.bound[0]
    assert env.events.count("bind_session_log") == 1
    assert [entry.get("role") for entry in session.context] == ["system"]


async def test_an_empty_line_is_ignored(env: ReplEnv) -> None:
    """Nothing typed is nothing meant — no submission, and the prompt returns."""
    await env.run(["", "   ", "\t"])
    assert env.backend is not None
    assert env.backend.submissions == []


async def test_eof_ends_the_loop_with_exit_code_zero(env: ReplEnv) -> None:
    """Ctrl+D at an idle prompt is a clean exit, and the teardown still runs."""
    rc = await env.run([])
    assert rc == 0
    assert env.events[-1] == "shutdown:quit"


async def test_a_refusal_is_shown_and_the_loop_continues(env: ReplEnv) -> None:
    """A refusal is a RESULT, not an exception: it is printed and the next line
    is still read (docs/SUBMISSION-LIFECYCLE.md's in-band refusal)."""

    def arm(backend: FakeBackend) -> None:
        backend.turn_result = SubmissionResult(
            accepted=False, submission_id="x", rejection_reason="an extension holds the session"
        )

    env.install(arm)
    await env.run(["do it", "and again"])
    assert "an extension holds the session" in env.text
    assert env.backend is not None
    assert len(env.backend.submissions) == 2


async def test_a_command_goes_through_submit_command_not_the_model(env: ReplEnv) -> None:
    """A resolved command is admitted by the command door; nothing is sent as prose."""

    def arm(backend: FakeBackend) -> None:
        backend.command_result = performed_result(
            Performed(flow=None, mutation="compact", data={"message": "compacted 12 entries"})
        )

    env.install(arm)
    await env.run(["/compact"])
    assert env.backend is not None
    assert env.backend.submissions == []
    assert len(env.backend.commands) == 1
    assert env.backend.commands[0].expand_commands is True
    assert "compacted 12 entries" in env.text


async def test_an_arm_this_head_cannot_perform_says_so(env: ReplEnv) -> None:
    """§6: ``/tree`` is a View no prompt line can open, so it raises the one
    wording every head raises with — and is never sent to the model."""

    def arm(backend: FakeBackend) -> None:
        backend.command_result = performed_result(View(name="tree", state={}))

    env.install(arm)
    await env.run(["/tree"])
    assert "/tree resolved to a command this caller must perform" in env.text
    assert "cannot perform it." in env.text
    assert env.backend is not None
    assert env.backend.submissions == []


async def test_a_turn_that_raises_ends_the_turn_and_not_the_session(env: ReplEnv) -> None:
    """§5: a provider error is caught per submission — printed with its traceback,
    and the next prompt is still read."""

    def arm(backend: FakeBackend) -> None:
        async def boom(submission: Any, context: Any) -> SubmissionResult:
            backend.submissions.append(submission)
            raise RuntimeError("provider said 503")

        backend.submit_turn = boom  # type: ignore[method-assign]

    env.install(arm)
    rc = await env.run(["first", "second"])
    assert rc == 0
    assert "turn failed: RuntimeError: provider said 503" in env.text
    assert env.backend is not None
    assert len(env.backend.submissions) == 2


async def test_a_resume_records_a_model_change_through_the_backend(env: ReplEnv) -> None:
    """§3 step 9: the head never appends; a resume onto a different model goes
    through ``record_model_change`` so the core stays the log's only writer."""
    await env.run([])
    assert env.backend is not None
    session_id = env.backend.bound[0].id

    env.install()
    await env.run([], session=session_id, model="other", name="renamed")
    assert "set_session_name:renamed" in env.events
    assert "record_model_change:other" in env.events


async def test_a_fresh_run_records_neither(env: ReplEnv) -> None:
    """Nothing to record: a new session already names the model it was created on."""
    await env.run([], model="other", name="fresh")
    assert not [event for event in env.events if event.startswith(("set_session_name", "record_"))]


async def test_resume_picks_a_session_before_a_backend_exists(env: ReplEnv) -> None:
    """§3 step 2: the picker reads ``catalog.list(cwd)``, not the ``session_id``
    domain — that needs a runtime, the runtime needs the session, and the
    session's model is what step 3 resolves from."""
    await env.run(["hello"])
    assert env.backend is not None
    created = env.backend.bound[0]

    env.install()
    await env.run([], answers=["1"], resume=True)
    assert env.backend is not None
    assert env.backend.bound[0].id == created.id


async def test_an_empty_answer_to_the_picker_exits_without_a_backend(env: ReplEnv) -> None:
    """Declining the pick is not "start a fresh session": nothing is built."""
    await env.run(["hello"])
    env.install()
    rc = await env.run([], answers=[""], resume=True)
    assert rc == 0
    assert env.backend is None


async def test_resume_with_no_saved_sessions_says_so(env: ReplEnv) -> None:
    """Fail-Early: an empty list is a refusal with an instruction, not a fresh run."""
    from tau_coding_agent.headless import CLIError

    with pytest.raises(CLIError, match="no saved sessions to resume"):
        await env.run([], answers=["1"], resume=True)


async def test_a_command_the_core_refuses_by_raising_is_one_line(env: ReplEnv) -> None:
    """§6: a ``ValueError`` out of the command door is the refusal's own reason,
    printed as one line — not a traceback, which is what an unexpected fault gets."""

    def arm(backend: FakeBackend) -> None:
        async def refuse(submission: Any) -> SubmissionResult:
            raise ValueError("no session is bound")

        backend.submit_command = refuse  # type: ignore[method-assign]

    env.install(arm)
    await env.run(["/compact"])
    assert "was refused: no session is bound" in env.text
    assert "Traceback" not in env.text


#: A turn with a delivery point in it, so a mid-turn line becomes a steering task.
_TURN: list[dict[str, Any]] = [
    {"kind": "lane_start", "source": "interactive", "submitter": "human", "text": "do it"},
    {"kind": "text_delta", "delta": "Looking.\n"},
    {"kind": "tool_call", "id": "t1", "name": "read", "arguments": {"path": "main.py"}},
    {"kind": "tool_result", "id": "t1", "name": "read", "result": "ok", "is_error": False},
    {"kind": "completion_end", "output": 12, "context": 30, "stop_reason": "stop"},
    {"kind": "lane_end", "context": 30, "output": 12, "seconds": 0.4},
]


async def test_a_shutdown_asked_for_mid_turn_is_reaped_before_the_teardown(env: ReplEnv) -> None:
    """``ctx.shutdown()`` from a hook sets the flag while the turn is still
    awaiting ``submit_turn``: returning straight out of the loop detaches the
    router and fires ``session_shutdown`` under a running turn, and then a queued
    delivery submits into the session that has just closed its providers."""

    def prepare(backend: FakeBackend) -> None:
        backend.script = list(_TURN)
        original = backend.submit_turn

        async def submit_turn(submission: Any, context: Any) -> SubmissionResult:
            if submission.multitask_strategy != "steer":
                backend.agent_session.shutdown_requested = True
            return await original(submission, context)

        backend.submit_turn = submit_turn  # type: ignore[method-assign]

    env.install(prepare)
    await env.run(["do it", "steer me"])

    events = env.events
    teardown = events.index("detach")
    # Both submissions BEFORE the teardown: the turn finished and its delivery landed.
    assert events[:teardown].count("submit_turn") == 2
    assert events[teardown:] == ["detach", "close_all", "shutdown:quit"]
    assert "ctx 30 · out 12" in env.text


async def test_a_resumed_session_is_drawn_before_the_prompt_opens(env: ReplEnv) -> None:
    """§3 step 16: a ``--continue`` onto a 40-message session that printed nothing
    is visually a fresh one, and the reader learns which conversation they are in
    only by submitting a turn and reading the answer for evidence."""
    await env.run([])
    assert env.backend is not None
    session = env.backend.bound[0]
    session.append_message({"role": "user", "content": "port the parser", "timestamp": 1})
    session.append_message(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Ported it."}],
            "usage": {"output_tokens": 7},
            "timestamp": 2,
        }
    )

    env.install()
    await env.run([], session=session.id)

    text = env.text
    assert "› port the parser" in text
    assert "Ported it." in text
    assert "system prompt (" in text
