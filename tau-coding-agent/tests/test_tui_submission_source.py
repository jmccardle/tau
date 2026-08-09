"""B2-a: the backend half of "the TUI is a renderer plus ONE source".

docs/SUBMISSION-LIFECYCLE.md phase 3. Before this, ``Submission(`` was constructed
at exactly one site in the whole repo — the ``AgentSession.prompt()`` compatibility
wrapper — so the one door existed and nothing walked through it except the thing it
replaced. ``TauBackend.stream_submission`` is the seam that lets a frontend hand in
its OWN submission record; ``stream_chat`` keeps its message-list contract by
deriving the ordinary interactive one.

What these pin:

* a caller's submission reaches ``AgentSession.submit`` **verbatim** — the record the
  TUI stamped is the record the core admits, not one re-derived from the message list;
* the turn is admitted **exactly once** (``submit`` called once, ``prompt`` never);
* every event the turn emits carries that submission's provenance
  (``source``/``submitter``/``submission_id``), which is what lets a renderer show a
  bus-initiated turn differently from a typed one; and
* the :class:`SubmissionResult` comes back verbatim, refusals included — a typed
  in-band refusal an adapter swallowed is the silent drop this lifecycle exists to
  prevent.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import Submission, SubmissionResult
from tau_coding_agent.backends import TauBackend


def _backend() -> TauBackend:
    """A real TauBackend (and therefore a real AgentSession) against no network."""
    return TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://x/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )


def _stub_turn(backend: TauBackend, events: list[AgentEvent]) -> list[dict[str, Any]]:
    """Replace the agent loop with a scripted emit, keeping REAL admission.

    ``submit()`` — the depth derivation, the turn lock, the provenance publication,
    the event stamping — runs for real; only the model round-trip below it is
    scripted. That is the layer under test: everything ``submit`` does to a
    submission on its way to the loop.
    """
    session = backend.agent_session
    produced = [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]

    async def fake_run_one_turn(
        text, images, context, queued=None, strip_ref_text=None, persist=True
    ):
        for event in events:
            await session._emit_stamped(event)
        return produced

    session._run_one_turn = fake_run_one_turn  # type: ignore[method-assign]
    return produced


async def test_stream_submission_admits_the_callers_record_verbatim():
    backend = _backend()
    _stub_turn(backend, [])
    seen: list[Submission] = []
    real_submit = backend.agent_session.submit

    async def recording_submit(sub, *, context=None):
        seen.append(sub)
        return await real_submit(sub, context=context)

    backend.agent_session.submit = recording_submit  # type: ignore[method-assign]

    # prompt() must NOT be the route any more: a second admission underneath this
    # one is the double-admission B2-a exists to avoid.
    def exploding_prompt(*a, **kw):
        raise AssertionError("stream_submission must not route through prompt()")

    backend.agent_session.prompt = exploding_prompt  # type: ignore[method-assign]

    sub = Submission(
        text="hello",
        source="interactive",
        submitter="human",
        submission_id="sub-1",
        multitask_strategy="enqueue",
        expand_commands=False,
        allow_user_input=True,
    )
    _text, _usage, new_messages, _tcs, result = await backend.stream_submission(
        sub, [{"role": "user", "content": "hello"}], lambda _d: None
    )

    assert len(seen) == 1, "the turn must be admitted exactly once"
    admitted = seen[0]
    assert admitted.text == "hello"
    assert admitted.source == "interactive"
    assert admitted.submitter == "human"
    assert admitted.submission_id == "sub-1"
    assert admitted.multitask_strategy == "enqueue"
    assert admitted.allow_user_input is True
    assert result.accepted is True
    assert result.submission_id == "sub-1"
    assert new_messages and new_messages[-1]["role"] == "assistant"


async def test_events_of_a_typed_turn_carry_interactive_provenance():
    """The renderer-facing half of phase 2, reached through the phase-3 seam."""
    backend = _backend()
    _stub_turn(
        backend,
        [AgentEvent(type="turn_start", timestamp=0, turn_index=0)],
    )
    captured: list[AgentEvent] = []
    backend.agent_session.subscribe(captured.append)

    await backend.stream_submission(
        Submission(
            text="hello",
            source="interactive",
            submitter="human",
            submission_id="sub-prov",
            multitask_strategy="enqueue",
            allow_user_input=True,
            correlation={"widget": "chat-input"},
        ),
        [],
        lambda _d: None,
    )

    stamped = [e for e in captured if e.type == "turn_start"]
    assert stamped, "the scripted turn emitted nothing"
    assert stamped[0].source == "interactive"
    assert stamped[0].submitter == "human"
    assert stamped[0].submission_id == "sub-prov"
    assert stamped[0].correlation == {"widget": "chat-input"}


async def test_stream_submission_returns_a_refusal_verbatim():
    """``accepted=False`` is a RESULT, and the adapter must not launder it."""
    backend = _backend()
    _stub_turn(backend, [])

    refusal = SubmissionResult(
        accepted=False, submission_id="sub-no", rejection_reason="a turn is already in flight"
    )

    async def refusing_submit(sub, *, context=None):
        return refusal

    backend.agent_session.submit = refusing_submit  # type: ignore[method-assign]

    text, _usage, new_messages, _tcs, result = await backend.stream_submission(
        Submission(text="hi", source="interactive", submitter="human", submission_id="sub-no"),
        [],
        lambda _d: None,
    )
    assert result is refusal
    assert result.rejection_reason == "a turn is already in flight"
    assert text == ""
    assert new_messages == []


async def test_stream_chat_derives_the_ordinary_interactive_submission():
    """The message-list contract still works, and admits exactly one submission."""
    backend = _backend()
    _stub_turn(backend, [])
    seen: list[Submission] = []
    real_submit = backend.agent_session.submit

    async def recording_submit(sub, *, context=None):
        seen.append(sub)
        return await real_submit(sub, context=context)

    backend.agent_session.submit = recording_submit  # type: ignore[method-assign]

    out = await backend.stream_chat(
        [{"role": "user", "content": "derive me"}],
        lambda _d: None,
    )

    assert len(out) == 4, "stream_chat's 4-tuple contract is unchanged"
    assert len(seen) == 1
    assert seen[0].text == "derive me"
    assert seen[0].source == "interactive"
    assert seen[0].submitter == "human"
    assert seen[0].multitask_strategy == "enqueue"
    # False, and since B2-b that is a DIVERGENCE from AgentSession.prompt() rather
    # than parity with it: this method returns a 4-tuple with no slot for a
    # CommandOutcome, so a dispatched command would be dropped. Since B2-c no
    # frontend routes through here at all (headless owns its record too) — what is
    # left is the SDK-shaped message-list contract, whose caller cannot know a
    # command is in the list before calling nor receive an outcome after.
    assert seen[0].expand_commands is False
    assert seen[0].allow_user_input is True
    assert seen[0].submission_id, "every submission needs an id to be attributable"


async def test_stream_chat_with_no_user_message_admits_nothing():
    """The pre-existing early return still short-circuits BEFORE admission."""
    backend = _backend()

    async def exploding_submit(sub, *, context=None):
        raise AssertionError("nothing to submit — there is no user message")

    backend.agent_session.submit = exploding_submit  # type: ignore[method-assign]

    text, usage, new_messages, tool_calls = await backend.stream_chat(
        [{"role": "system", "content": "sys"}], lambda _d: None
    )
    assert (text, new_messages, tool_calls) == ("", [], [])
    assert usage["total_tokens"] == 0


async def test_submit_command_returns_the_outcome_without_streaming_anything():
    """B2-b: the command door. ``expand_commands=True`` now dispatches instead of raising.

    ``submit_command`` is deliberately NOT ``stream_submission``: a dispatched command
    runs no model call, so opening an exchange, taking the display lock and subscribing
    to the bus for it would leave an empty collapsible box in the transcript. What comes
    back is the typed outcome the frontend must act on.
    """
    backend = _backend()

    result = await backend.submit_command(
        Submission(
            text="/compact",
            source="interactive",
            submitter="human",
            submission_id="sub-x",
            expand_commands=True,
        )
    )

    assert result.accepted is True
    assert result.messages == []
    assert result.command is not None
    assert (result.command.name, result.command.performer) == ("compact", "frontend")


async def test_submit_command_admits_through_the_same_door_as_a_prompt():
    """Not a private route: it is ``AgentSession.submit``, once, with the record verbatim."""
    backend = _backend()
    seen: list[Submission] = []
    real_submit = backend.agent_session.submit

    async def recording_submit(sub, *, context=None):
        seen.append(sub)
        return await real_submit(sub, context=context)

    backend.agent_session.submit = recording_submit  # type: ignore[method-assign]
    submission = Submission(
        text="/tree",
        source="interactive",
        submitter="human",
        submission_id="sub-y",
        expand_commands=True,
        allow_user_input=True,
    )

    await backend.submit_command(submission)

    assert seen == [submission], "the record admitted is the record handed over"
