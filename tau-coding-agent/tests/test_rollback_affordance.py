"""B1-e part 1: the TUI affordance for ``multitask_strategy="rollback"``.

``rollback`` — abort the in-flight turn, move the cursor back to the leaf it
started from, run something else in its place — landed in the core in phase 2
(docs/SUBMISSION-LIFECYCLE.md decision 2) and had no caller a human could reach:
no keybinding, no command, nothing. These tests drive the affordance that fixes
that, at the level ``test_app_actions.py`` works at (the real app under Pilot, a
controllable backend double), and pin the three things that make it not a
reimplementation:

* it reaches ``AgentSession.submit`` with ``multitask_strategy="rollback"`` and
  nothing else — the navigate-back lives in the core, once;
* a refusal (``accepted=False`` + ``rejection_reason``) is SHOWN, because a typed
  in-band refusal the UI swallows is the silent drop the whole lifecycle exists to
  prevent; and
* the key is gated on there being a turn to roll back, so ``ctrl+z`` is still the
  input editor's undo the rest of the time.

Reference: docs/SUBMISSION-LIFECYCLE.md decision 2 + "Typed in-band refusal".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.app import Parley, RollbackPromptModal, TreeModeModal
from tau_coding_agent.backends import TauBackend

# --- doubles ----------------------------------------------------------------


class _RollbackBackend:
    """A backend whose turn blocks until something aborts it — plus a rollback seam.

    ``submit_turn`` blocks exactly like ``test_app_actions._BlockingBackend``'s
    does, so a test can observe the in-flight state the affordance requires. ``rollback_turn``
    records the text it was given and answers with a scripted
    :class:`SubmissionResult`; on an ACCEPTED one it does what the real
    ``AgentSession.submit`` does from the app's point of view — releases the aborted
    turn and leaves the replacement on the bound log — so the app's re-render has
    something real to read back.
    """

    def __init__(self, result: SubmissionResult | None = None) -> None:
        self.aborted = False
        self.rollback_calls: list[str] = []
        self.submissions: list[Any] = []
        self._released = asyncio.Event()
        self._log: Any = None
        self._result = result or SubmissionResult(accepted=True, submission_id="s1")

    def bind_session_log(self, session_log) -> None:
        self._log = session_log

    def abort(self) -> None:
        self.aborted = True
        self._released.set()

    async def submit_turn(self, submission, context) -> SubmissionResult:
        self.submissions.append(submission)
        await self._released.wait()
        partial = {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}
        self._log.append_message(partial)
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)

    async def rollback_turn(self, text: str) -> SubmissionResult:
        self.rollback_calls.append(text)
        if self._result.accepted:
            # What submit() does, as the app sees it: the aborted turn unwinds and
            # the replacement turn's messages are on the log by the time it returns.
            self.aborted = True
            self._released.set()
            await asyncio.sleep(0)
            self._log.append_message({"role": "user", "content": text})
            self._log.append_message({"role": "assistant", "content": "replacement"})
        return self._result


class _Submit:
    """Duck-typed Input.Submitted — on_input_submitted only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


@pytest.fixture
def app_and_backend(make_app):
    """``test_app_actions.blocking_app`` with the rollback seam on the double."""
    backend = _RollbackBackend()
    return make_app(create_backend=lambda cfg: backend), backend


def _notifications(app: Parley) -> list[tuple[str, str]]:
    """Record ``notify`` calls as ``(message, severity)`` (test_tree_elide idiom)."""
    seen: list[tuple[str, str]] = []
    original = app.notify

    def fake_notify(message, *args, **kwargs):
        seen.append((str(message), str(kwargs.get("severity", "information"))))
        return original(message, *args, **kwargs)

    app.notify = fake_notify  # type: ignore[method-assign]
    return seen


def _script(app: Parley, values: list[Any]) -> list[Any]:
    """Answer the modal from a script; return the screens the action pushed."""
    pushed: list[Any] = []
    queue = list(values)

    async def fake_push_screen_wait(screen):
        pushed.append(screen)
        return queue.pop(0)

    app.push_screen_wait = fake_push_screen_wait  # type: ignore[method-assign]
    return pushed


# --- the seam: what actually reaches submit() -------------------------------


async def test_backend_rollback_turn_submits_the_rollback_strategy():
    """The one hard requirement: the affordance goes through ``submit()`` with
    ``multitask_strategy="rollback"`` — it does not reimplement navigate-back."""
    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://x/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )
    seen: list[tuple[Any, dict]] = []

    async def fake_submit(sub, **kwargs):
        seen.append((sub, kwargs))
        return SubmissionResult(accepted=True, submission_id=sub.submission_id)

    backend.agent_session.submit = fake_submit  # type: ignore[method-assign]

    result = await backend.rollback_turn("try that again, but read the tests first")

    assert result.accepted is True
    sub, kwargs = seen[0]
    assert sub.multitask_strategy == "rollback"
    assert sub.text == "try that again, but read the tests first"
    assert (sub.source, sub.submitter) == ("interactive", "human")
    # A human at the terminal pressed the key, so a hook may ask them a question…
    assert sub.allow_user_input is True
    # …but command dispatch still lives in the TUI (submit() raises on True).
    assert sub.expand_commands is False
    # No `context=` override: the TUI's working list still holds the messages the
    # rollback just un-pathed, so the session must fold its own log instead.
    assert kwargs == {}


async def test_rollback_returns_a_refusal_verbatim():
    """A refusal is a RESULT, not an exception, and the adapter must not eat it."""
    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://x/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )

    async def fake_submit(sub, **kwargs):
        return SubmissionResult(
            accepted=False, submission_id=sub.submission_id, rejection_reason="stale target"
        )

    backend.agent_session.submit = fake_submit  # type: ignore[method-assign]

    result = await backend.rollback_turn("x")
    assert result.accepted is False
    assert result.rejection_reason == "stale target"


# --- the affordance ---------------------------------------------------------


async def test_ctrl_z_during_generation_rolls_the_turn_back(
    app_and_backend, wait_for_workers_settled
):
    """End to end at the key: ctrl+z while a turn streams → the prompt modal → the
    typed text reaches ``rollback_turn`` → the transcript is re-rendered from the
    session, which is the authority after the navigate."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = _notifications(app)
        pushed = _script(app, ["read the tests first"])

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()
        assert app.is_generating is True

        await pilot.press("ctrl+z")
        await wait_for_workers_settled(app)
        await pilot.pause()

        # The binding fired, the modal was the rollback prompt, prefilled with the
        # doomed turn's own text, and the edited text is what was submitted.
        assert isinstance(pushed[0], RollbackPromptModal)
        assert backend.rollback_calls == ["read the tests first"]
        # Re-rendered from the live session (the same seam /compact and the tree
        # browser use), so the replacement turn is what the transcript shows.
        assert app.messages[-1]["content"] == "replacement"
        assert ("Rolled back and re-ran from before the aborted turn", "information") in notes


async def test_the_modal_is_prefilled_with_the_aborted_turn_s_prompt(
    app_and_backend, wait_for_workers_settled
):
    """ "Run that again from before it went wrong" is one keypress: the prefill is
    the prompt of the turn being discarded."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = _script(app, [None])  # cancel the modal

        await app.on_input_submitted(_Submit("summarize the repo"))
        await pilot.pause()

        await app.action_rollback_turn().wait()
        assert pushed[0]._prefill == "summarize the repo"
        # Cancelling submits nothing and leaves the turn alone.
        assert backend.rollback_calls == []
        assert app.is_generating is True

        app.action_cancel_generation()  # release the blocked worker for teardown
        await wait_for_workers_settled(app)


async def test_a_refused_rollback_shows_its_reason(app_and_backend, wait_for_workers_settled):
    """The stale-target guard's ``rejection_reason`` reaches the user. A refusal the
    UI swallowed would be indistinguishable from a rollback that worked."""
    app, backend = app_and_backend
    reason = (
        "rollback target is stale: the turn this submission aborted is no longer "
        "the turn whose slot was just granted"
    )
    backend._result = SubmissionResult(accepted=False, submission_id="s2", rejection_reason=reason)
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = _notifications(app)
        _script(app, ["something else"])

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()

        await app.action_rollback_turn().wait()
        await pilot.pause()

        assert (reason, "warning") in notes
        # Nothing was re-rendered off a refusal.
        assert all(m.get("content") != "replacement" for m in app.messages)

        app.action_cancel_generation()
        await wait_for_workers_settled(app)


async def test_rollback_refuses_when_nothing_is_generating(app_and_backend):
    """``submit()`` degrades a rollback with no turn in flight to an ordinary turn —
    right for the core, wrong for a human who asked to discard something. So the TUI
    refuses before submitting, and says why."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = _notifications(app)
        _script(app, ["never asked for"])

        await app.action_rollback_turn().wait()

        assert backend.rollback_calls == []
        assert notes == [("Nothing is generating — rollback discards an in-flight turn", "warning")]


async def test_rollback_refuses_a_slash_command(app_and_backend, wait_for_workers_settled):
    """``expand_commands`` is False until Block 2, so "/compact" would reach the
    model as literal prompt text. Refused with the reason instead."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = _notifications(app)
        _script(app, ["/compact"])

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()
        await app.action_rollback_turn().wait()

        assert backend.rollback_calls == []
        assert any("slash commands are not expanded" in message for message, _ in notes)

        app.action_cancel_generation()
        await wait_for_workers_settled(app)


async def test_rollback_refuses_a_turn_that_finished_while_the_modal_was_open(app_and_backend):
    """The window the modal opens: the turn can end while the prompt is typed, after
    which nothing would be un-pathed. Checked again on the way out of the modal."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = _notifications(app)

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()

        async def finish_then_answer(screen):
            # The turn completes while the modal is up. (Yielding rather than
            # ``workers.wait_for_complete()``: this code runs INSIDE the rollback
            # worker, and a worker may not wait on the worker manager from within.)
            app.action_cancel_generation()
            for _ in range(100):
                if not app.is_generating:
                    break
                await asyncio.sleep(0)
            return "too late"

        app.push_screen_wait = finish_then_answer  # type: ignore[method-assign]
        await app.action_rollback_turn().wait()

        assert backend.rollback_calls == []
        assert any("nothing left to" in message for message, _ in notes)


# --- the key stays the editor's undo the rest of the time -------------------


async def test_the_binding_is_live_only_while_a_turn_runs(
    app_and_backend, wait_for_workers_settled
):
    """``check_action`` False both hides the Footer label and lets the key fall
    through to the ChatInput TextArea's own ctrl+z (undo)."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.check_action("rollback_turn", ()) is False

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()
        assert app.check_action("rollback_turn", ()) is True

        app.action_cancel_generation()
        await wait_for_workers_settled(app)
        await pilot.pause()
        assert app.check_action("rollback_turn", ()) is False


async def test_ctrl_z_when_idle_reaches_the_input_as_undo(app_and_backend):
    """The other half of the same claim, driven through the real key: idle, ctrl+z
    is the editor's undo and no modal is pushed."""
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = _script(app, ["unused"])

        chat_input = app.query_one("#chat-input")
        chat_input.focus()
        await pilot.pause()
        await pilot.press("t", "y", "p", "e", "d")
        await pilot.pause()
        assert chat_input.text == "typed"

        await pilot.press("ctrl+z")
        await pilot.pause()

        assert pushed == []  # no rollback modal: the app did not claim the key
        assert chat_input.text != "typed"  # it reached the TextArea, which undid


async def test_a_modal_takes_escape_back_from_the_app(app_and_backend):
    """Regression for the reason ``check_action`` also gates the Esc binding.

    A ``priority=True`` App binding beats a modal's own, and a dispatched action
    CONSUMES the key even when it no-ops — so Esc used to be eaten by
    ``action_cancel_generation`` and no dialog could be dismissed with it. That was
    invisible until a modal could be open mid-turn (the rollback prompt), where Esc
    would have aborted the very turn being rolled back.

    The action behind Esc is ``escape`` now, and it does something when nothing is
    generating (it offers the tree browser), so ceding the key to a dialog matters
    more than it did: without this, Esc over a dialog would arm a second modal
    instead of closing the first.
    """
    app, backend = app_and_backend
    async with app.run_test() as pilot:
        await pilot.pause()
        dismissed: list[Any] = []
        app.push_screen(TreeModeModal(), lambda value: dismissed.append(value))
        await pilot.pause()

        assert app.check_action("escape", ()) is False
        assert app.check_action("interrupt", ()) is False
        await pilot.press("escape")
        await pilot.pause()

        assert dismissed == [None]
        assert len(app.screen_stack) == 1
