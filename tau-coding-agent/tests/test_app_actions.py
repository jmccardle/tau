"""Tests for Parley app-level action / widget wiring (distinct from chat
rendering).

Regression for the "+ New Chat" sidebar button doing nothing: its handler was a
*sync* ``on_button_pressed`` that called the *async* ``action_new_chat()``
without awaiting it, so the coroutine was created and silently discarded
(Python even warned ``coroutine 'Parley.action_new_chat' was never awaited``).

Driven through the real app via ``App.run_test()`` / Pilot.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from tau_agent_core.commands import CommandOutcome, resolve_command
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.app import ChatDisplay, ChatListItem, ChatSelected, ChatSidebar, Parley
from tau_coding_agent.backends import TauBackend
from tau_coding_agent.chat_widgets import ReasoningRegion, ToolBox


def resolve_and_report(text: str) -> CommandOutcome | None:
    """What ``AgentSession.submit`` would report for ``text`` — the double's stand-in.

    The real dispatch lives in the core (tau-agent-core/tests/test_submit_commands.py
    pins it); what this double exists to prove is that the APP routes a command to
    ``submit_command`` and then performs the outcome, so it reuses the same pure
    resolver rather than inventing a second answer.
    """
    invocation = resolve_command(text)
    if invocation is None:
        return None
    return CommandOutcome(
        name=invocation.name, args=invocation.args, performer=invocation.performer
    )


# A reloaded transcript with reasoning + a tool call/result + a final answer,
# used to exercise the global fold toggles and the conversation rollup.
_RELOAD = [
    {"role": "user", "content": "q"},
    {
        "role": "assistant",
        "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
        "content": [
            {"type": "thinking", "thinking": "let me look"},
            {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}},
        ],
    },
    {
        "role": "toolResult",
        "tool_call_id": "c1",
        "tool_name": "ls",
        "is_error": False,
        "content": [{"type": "text", "text": "a.py"}],
    },
    {
        "role": "assistant",
        "usage": {
            "input_tokens": 400,
            "cache_read_tokens": 100,
            "output_tokens": 12,
            "total_tokens": 512,
        },
        "content": [
            {"type": "thinking", "thinking": "done"},
            {"type": "text", "text": "one file"},
        ],
    },
]


@pytest.fixture
def app(make_app):
    # Sandboxing, config, and an injected file catalog all come from the shared
    # ``make_app`` (tests/conftest.py); all this fixture still chooses is that no
    # real backend gets built.
    #
    # The double carries ``system_prompt`` because a new chat now stores the
    # prompt the BACKEND built — a bare ``object()`` would store nothing and the
    # session would silently start with no system message.
    return make_app(
        create_backend=lambda cfg: SimpleNamespace(system_prompt=cfg.get("system_prompt", ""))
    )


async def test_new_chat_button_creates_chat(app, tmp_path):
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is None

        # The button lives in the sidebar, which mounts CLOSED (§8) — a click on
        # a hidden widget lands on whatever is underneath it, which is how this
        # test would silently stop testing anything.
        app.action_toggle_sidebar()
        await pilot.pause()

        await pilot.click("#new-chat-button")
        await pilot.pause()

        # The async action actually ran: a session is active, seeded with the
        # system prompt, and persisted to the (sandboxed) sessions dir.
        assert app.current_session is not None
        assert app.current_session.model == "m"
        assert app.messages[0] == {"role": "system", "content": "sys"}
        assert len(list((tmp_path / "sessions").rglob("*.jsonl"))) == 1


async def test_chat_selected_loads_session_by_ref(app, wait_for_workers_settled):
    """Sidebar click → ``ChatListItem.chat_ref`` → ``ChatSelected`` →
    ``on_chat_selected`` round-trips through the injected ``session_catalog``
    (W10), not a hardcoded ``Path`` — the seam this suite exists to prove.
    """
    async with app.run_test() as pilot:
        await pilot.pause()

        # Seed a session directly through the same catalog the app itself uses
        # (bypassing the UI), so the sidebar has something to select.
        seeded = app.session_catalog.create(
            os.getcwd(), "m", "openai", system_prompt="sys", name="Picked"
        )
        seeded.append_message({"role": "user", "content": "hello"})

        # §8: the sidebar mounts closed, and a closed sidebar deliberately does
        # not mount its rows (``ChatSidebar._apply_sessions`` defers the render).
        # This test reads a ``ChatListItem``, so it opens the sidebar the way a
        # user does first.
        app.action_toggle_sidebar()
        await pilot.pause()

        sidebar = app.query_one(ChatSidebar)
        sidebar.refresh_chats()
        # refresh_chats() only STARTS a thread worker now (Fix B: the catalog
        # fetch is a blocking call, moved off the event loop) — wait for it to
        # land before asserting on the rendered list.
        await wait_for_workers_settled(app)
        await pilot.pause()

        item = app.query_one(ChatListItem)
        # A storage-agnostic ref (str), not a filesystem Path — matches what
        # SessionCatalog.load() accepts back.
        assert item.chat_ref == str(seeded.path)
        assert isinstance(item.chat_ref, str)

        app.post_message(ChatSelected(item.chat_ref))
        await pilot.pause()

        assert app.current_session is not None
        assert app.current_session.id == seeded.id
        assert app.messages[-1] == {"role": "user", "content": "hello"}


# ---------------------------------------------------------------------------
# #6 — global thinking/tool-output toggles + conversation rollup.
# ---------------------------------------------------------------------------


async def _reload(app, pilot) -> ChatDisplay:
    """Reload a known transcript into the display and return it."""
    await app.action_new_chat()
    display = app.query_one(ChatDisplay)
    await display.reload_messages(_RELOAD)
    await pilot.pause()
    return display


async def test_toggle_reasoning_folds_all_regions(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        regions = list(app.query(ReasoningRegion))
        # One reasoning region in the tool step, one on the promoted answer.
        assert len(regions) == 2 and all(r.collapsed for r in regions)  # reload folds them

        # All folded -> first toggle expands all.
        app.action_toggle_reasoning()
        await pilot.pause()
        assert all(not r.collapsed for r in app.query(ReasoningRegion))
        assert app.reasoning_collapsed is False

        # Any expanded -> next toggle collapses all.
        app.action_toggle_reasoning()
        await pilot.pause()
        assert all(r.collapsed for r in app.query(ReasoningRegion))
        assert app.reasoning_collapsed is True


async def test_toggle_tools_folds_all_boxes(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        boxes = list(app.query(ToolBox))
        assert len(boxes) == 1 and all(b.collapsed for b in boxes)  # default collapsed

        app.action_toggle_tools()  # collapsed -> expand all
        await pilot.pause()
        assert all(not b.collapsed for b in app.query(ToolBox))

        app.action_toggle_tools()  # expanded -> collapse all
        await pilot.pause()
        assert all(b.collapsed for b in app.query(ToolBox))


async def test_toggle_with_no_widgets_is_noop(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()  # empty display, no regions/boxes
        app.action_toggle_reasoning()
        app.action_toggle_tools()
        await pilot.pause()
        assert app.reasoning_collapsed is False
        assert app.tools_collapsed is False


def test_aggregate_label_rolls_up_tools_and_tokens():
    # Pure function: 1 tool call; cumulative ↑100+400 ↓30+12 R100; context is the
    # LAST prompt (400 + 100 cached), NOT 100 + 500 summed.
    assert Parley._aggregate_label(_RELOAD) == "1 tool · ↑500 ↓42 R100 · 500 ctx"
    # Plural tools, the k-formatting, and a provider that reports no caching (the
    # R and W arrows are dropped rather than shown as zero).
    many = [
        {
            "role": "assistant",
            "usage": {"input_tokens": 2500, "output_tokens": 300},
            "content": [
                {"type": "toolCall", "id": "a", "name": "ls", "arguments": {}},
                {"type": "toolCall", "id": "b", "name": "cat", "arguments": {}},
            ],
        },
    ]
    assert Parley._aggregate_label(many) == "2 tools · ↑2.5k ↓300 · 2.5k ctx"
    # Nothing to roll up yet -> empty (subtitle then shows just the model).
    assert Parley._aggregate_label([{"role": "user", "content": "hi"}]) == ""


def test_aggregate_label_reports_context_separately_from_cumulative_input():
    """Ten completions on a conversation that grew to 10k. The old single ``N tok``
    was Σ total_tokens = 55.1k, which read as the conversation's size. Cumulative
    input really is 55.0k and stays — labelled ``↑``, where it means what it says —
    while the conversation's actual size, 10.0k, is its own ``ctx`` number."""
    messages = [
        {
            "role": "assistant",
            "usage": {"input_tokens": n * 1000, "output_tokens": 10},
            "content": [],
        }
        for n in range(1, 11)
    ]
    assert Parley._aggregate_label(messages) == "↑55.0k ↓100 · 10.0k ctx"


async def test_subtitle_shows_rollup_after_reload(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        # The real reload path sets the working list to the loaded transcript;
        # mirror that so the rollup (derived from app.messages) has it.
        app.messages = _RELOAD
        app._refresh_subtitle()
        await pilot.pause()
        assert app.sub_title == "m · 1 tool · ↑500 ↓42 R100 · 500 ctx"


# ---------------------------------------------------------------------------
# Generation runs in a worker; Esc cooperatively cancels it.
# ---------------------------------------------------------------------------


class _BlockingBackend:
    """A backend whose ``submit_turn`` blocks until ``abort()`` releases it.

    Lets a test observe the in-flight state (worker running, UI responsive) and
    the cooperative cancel: ``abort()`` both records the call and unblocks the
    stream, mimicking the real provider stopping at the next streamed delta.

    Like the real ``TauBackend`` it persists the turn's assistant message through
    the bound live ``Session`` (E3-ctx / D3 — the AgentSession is the sole
    persister), so the app's turn-end rebuild of ``self.messages`` from
    ``session.context`` surfaces the partial answer.

    ``submit_turn`` (not ``stream_chat``, and since B3-a not ``stream_submission``)
    is what the app calls: the TUI owns the :class:`Submission` record and hands it
    to the backend, which admits it through ``AgentSession.submit``. It gets back a
    :class:`SubmissionResult` and nothing else, because rendering comes off the
    persistent bus subscription rather than out of this call's return value. The
    double records every submission it is handed so a test can assert the
    provenance the app stamped.
    """

    def __init__(self) -> None:
        self.aborted = False
        self._released = asyncio.Event()
        self._log = None
        # Every submission the app handed over, in order, plus the context list it
        # was given with it — the seam B2-a exists to make assertable.
        self.submissions: list[Any] = []
        self.contexts: list[list[dict]] = []
        # Command submissions go through a DIFFERENT backend method (B2-b) — no
        # streaming, no exchange — so they are recorded separately and a test can
        # tell "this became a turn" from "this became a command".
        self.command_submissions: list[Any] = []

    async def submit_command(self, submission):
        self.command_submissions.append(submission)
        return SubmissionResult(
            accepted=True,
            submission_id=submission.submission_id,
            command=resolve_and_report(submission.text),
        )

    def bind_session_log(self, session_log) -> None:
        self._log = session_log

    def abort(self) -> None:
        self.aborted = True
        self._released.set()

    def release(self) -> None:
        """Unblock the in-flight turn WITHOUT recording an abort (a normal finish)."""
        self._released.set()

    async def submit_turn(self, submission, context) -> SubmissionResult:
        self.submissions.append(submission)
        self.contexts.append(list(context))
        await self._released.wait()
        # One turn per release, so a queued second submission does not sail through
        # on the first one's already-set event.
        self._released.clear()
        partial = {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}
        # Sole-persister contract: record the produced message through the bound log
        # (the real backend does this inside AgentSession.submit).
        self._log.append_message(partial)
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)


@pytest.fixture
def blocking_app(make_app):
    """Like ``app`` but ``create_backend`` yields a controllable blocking backend."""
    backend = _BlockingBackend()
    return make_app(create_backend=lambda cfg: backend), backend


class _Submit:
    """Duck-typed Input.Submitted — on_input_submitted only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


async def _until(pilot, predicate, tries: int = 100) -> None:
    """Pump the app until ``predicate()`` holds, or fail saying it never did.

    A fixed number of ``pilot.pause()`` calls is a guess about how many scheduler
    turns a hand-off takes; this waits for the thing itself.
    """
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("condition never became true")


async def test_generation_runs_in_worker_and_esc_aborts(blocking_app, wait_for_workers_settled):
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()

        # Submit a turn. on_input_submitted starts a worker and returns — so this
        # await completes even though the backend is still "streaming".
        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()

        # In flight: worker running (blocked in submit_turn), UI live, input gated.
        assert app.is_generating is True
        assert app.query_one("#chat-input").disabled is True
        assert backend.aborted is False

        # Esc → cooperative abort. The backend records it and unblocks the stream.
        app.action_cancel_generation()
        assert backend.aborted is True

        await wait_for_workers_settled(app)
        await pilot.pause()

        # Worker finalized: input restored, flag cleared, partial answer kept.
        assert app.is_generating is False
        assert app.query_one("#chat-input").disabled is False
        assert app.messages[-1]["content"][0]["text"] == "partial"


async def test_cancel_generation_is_noop_when_idle(blocking_app):
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_generating is False
        app.action_cancel_generation()  # nothing in flight
        assert backend.aborted is False


# ---------------------------------------------------------------------------
# B2-a — the TUI is a renderer plus ONE source: a typed prompt is a Submission.
# docs/SUBMISSION-LIFECYCLE.md phase 3.
# ---------------------------------------------------------------------------


async def test_typed_prompt_is_admitted_as_an_interactive_submission(
    blocking_app, wait_for_workers_settled
):
    """What "a human typed this" MEANS is now a record, not a private code path."""
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("hello"))
        await pilot.pause()

        assert len(backend.submissions) == 1
        sub = backend.submissions[0]
        assert sub.text == "hello"
        # Provenance (phase 2): every event this turn emits is attributable to a
        # person at a terminal, distinguishably from a bus/timer/webhook turn.
        assert sub.source == "interactive"
        assert sub.submitter == "human"
        assert sub.submission_id, "an unattributable submission is not attributable"
        # Decision 1: interactive defaults to enqueue (steer is phase 4).
        assert sub.multitask_strategy == "enqueue"
        # B2-b: dispatch moved into submit(), so the interactive frontend declares
        # it. A bus/timer submission still passes False and its "/compact" stays
        # literal prompt text — the flag is the security boundary.
        assert sub.expand_commands is True
        # A human typed it, so a hook under this turn may ask that human a question.
        assert sub.allow_user_input is True

        # The context handed over is the TUI's own working list, ending with the
        # user turn the app rendered — the pre-existing prompt(context=…) contract.
        assert backend.contexts[0][-1] == {"role": "user", "content": "hello"}

        backend.abort()
        await wait_for_workers_settled(app)
        await pilot.pause()


async def test_input_history_and_clear_still_happen_before_the_submission(
    blocking_app, wait_for_workers_settled
):
    """The half of on_input_submitted that is NOT the seam must be untouched."""
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("remember me"))
        await pilot.pause()

        widget = app.query_one("#chat-input")
        assert widget.command_history == ["remember me"]
        assert widget.text == ""
        # The user turn is on the working list and on screen, exactly as before.
        assert app.messages[-1] == {"role": "user", "content": "remember me"}

        backend.abort()
        await wait_for_workers_settled(app)
        await pilot.pause()


async def test_blank_and_slash_commands_never_become_turns(blocking_app):
    """Whitespace is dropped and a built-in command dispatches, as before.

    Since B2-b the "as before" is achieved differently: ``expand_commands`` is True
    and the command reaches ``submit()``, which resolves it and hands back an
    outcome instead of running a turn. What must not change is the observable —
    "/extensions" never becomes a model turn and never sets ``is_generating``.
    """
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("   "))
        await app.on_input_submitted(_Submit("/extensions"))
        await pilot.pause()

        assert backend.submissions == [], "no turn was ever started"
        assert [s.text for s in backend.command_submissions] == ["/extensions"]
        assert backend.command_submissions[0].expand_commands is True
        assert app.is_generating is False
        # The command is chrome: it did not join the model-input working list.
        assert all(m.get("content") != "/extensions" for m in app.messages)


async def test_second_prompt_mid_turn_enqueues_rather_than_dropping(
    blocking_app, wait_for_workers_settled
):
    """Two prompts, two turns, in order — neither cancelled, neither lost.

    Before B2-a the generation worker was ``exclusive``: a second submission
    cancelled the first mid-turn, losing the running turn's partial answer AND the
    new prompt. That is the drop docs/SUBMISSION-LIFECYCLE.md exists to remove. The
    submissions declare ``multitask_strategy="enqueue"``; the second one waits.
    """
    app, backend = blocking_app
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("one"))
        await pilot.pause()
        await app.on_input_submitted(_Submit("two"))
        await pilot.pause()

        # Both are outstanding; the app is still busy and the input still gated.
        assert app.is_generating is True
        assert app.query_one("#chat-input").disabled is True
        # The second turn has not started yet — it is queued, not dropped. Since
        # B3-a what makes it wait is ``_working_list_lock`` (self.messages is the
        # context handed over AND the thing rebuilt at turn end), not a display
        # lock: rendering no longer serializes anything.
        assert [s.text for s in backend.submissions] == ["one"]

        # Finish the first turn. The second is admitted straight after it.
        backend.release()
        await _until(pilot, lambda: len(backend.submissions) == 2)
        assert [s.text for s in backend.submissions] == ["one", "two"]
        # Still busy: a turn ending while another is outstanding must not re-enable
        # the input or clear the flag.
        assert app.is_generating is True
        assert app.query_one("#chat-input").disabled is True

        backend.release()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert app.is_generating is False
        assert app.query_one("#chat-input").disabled is False
        assert [s.text for s in backend.submissions] == ["one", "two"]
        # Two distinct submissions — not one record reused for both turns.
        assert len({s.submission_id for s in backend.submissions}) == 2


def test_taubackend_abort_delegates_to_session():
    from unittest.mock import MagicMock

    backend = TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://x/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )
    backend.agent_session = MagicMock()
    backend.abort()
    backend.agent_session.abort.assert_called_once_with()


# ---------------------------------------------------------------------------
# The render cap's two gestures (docs/PLAN-0.9.4.md §1). The cap itself is
# tested against ChatDisplay in test_chat_rendering.py; what is tested here is
# that a reader can actually reach the messages it left off screen — by mouse
# (the "⋯ N earlier" row is clickable) and by keyboard (the palette action).
# ---------------------------------------------------------------------------


def _long_transcript(turns: int) -> list[dict]:
    msgs: list[dict] = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


async def test_clicking_the_earlier_row_mounts_the_rest(app):
    """The mouse half. Without it the row states a fact and offers no way to act."""
    from tau_coding_agent.app import MessageBox

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        display = app.query_one(ChatDisplay)
        await display.reload_messages(_long_transcript(20))
        await pilot.pause()
        assert display.elided_count == 32

        # The reader reaches the row the only way it can be reached: a reload
        # parks at the newest message, so scrolling up to the top is the gesture
        # that puts the row on screen in the first place.
        display.scroll_home(animate=False)
        await pilot.pause()
        row = display.query_one(".chat-fold")
        await pilot.click(row)
        await pilot.pause()

        assert display.elided_count == 0
        assert len(display.query(MessageBox)) == 40


async def test_the_palette_action_mounts_the_rest_and_says_it_is_doing_so(app):
    """The keyboard half, plus the notice — mounting hundreds of boxes is slow
    enough that a silent action reads as a dead key."""
    from tau_coding_agent.app import MessageBox

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        display = app.query_one(ChatDisplay)
        await display.reload_messages(_long_transcript(20))
        await pilot.pause()

        notices: list[str] = []
        app.notify = lambda msg, **kw: notices.append(msg)  # type: ignore[assignment]
        await app.action_show_all_messages()
        await pilot.pause()

        assert len(display.query(MessageBox)) == 40
        assert notices and "32" in notices[0]


async def test_the_palette_action_says_so_when_nothing_is_hidden(app):
    """Rather than looking broken. Nothing was truncated; there is just nothing
    left to mount."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        display = app.query_one(ChatDisplay)
        await display.reload_messages(_long_transcript(2))
        await pilot.pause()

        notices: list[str] = []
        app.notify = lambda msg, **kw: notices.append(msg)  # type: ignore[assignment]
        await app.action_show_all_messages()
        await pilot.pause()

        assert display.elided_count == 0
        assert notices == ["The whole conversation is already on screen."]


# ---------------------------------------------------------------------------
# ctrl+C and Esc: two keys that ask before doing something big (PLAN-0.9.4 §4)
# ---------------------------------------------------------------------------


def _input(app):
    from tau_coding_agent.app import ChatInput

    return app.query_one("#chat-input", ChatInput)


async def test_ctrl_c_stops_the_turn_rather_than_the_program(app):
    """Step 1. The key a terminal user reaches for to stop a runaway process."""
    aborted: list[bool] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = SimpleNamespace(abort=lambda: aborted.append(True))
        app.is_generating = True

        app.action_interrupt()
        await pilot.pause()

        assert aborted == [True]
        assert app.sub_title == "Cancelling…"
        assert app.is_running, "the app must still be up"


async def test_ctrl_c_clears_a_draft_before_it_offers_to_exit(app):
    """Step 2, and the reason steps 3 and 4 exist at all: ``ctrl+C`` used to be
    bound straight to ``quit``, so a mistimed press ended the session with the
    draft in it."""
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = _input(app)
        editor.text = "half a question"

        app.action_interrupt()
        await pilot.pause()

        assert editor.text == ""
        assert "exit" not in app.sub_title, "clearing is not also an exit offer"
        assert app.is_running


async def test_ctrl_c_on_an_empty_input_offers_the_exit_and_then_takes_it(app):
    """Steps 3 and 4."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        before = app.sub_title

        app.action_interrupt()
        await pilot.pause()
        assert app.sub_title == "press ctrl+C again to exit"
        assert app.is_running, "the first press must not exit"

        app.action_interrupt()
        await pilot.pause()
        assert not app.is_running
    assert before != "press ctrl+C again to exit"


async def test_the_exit_offer_lapses_and_puts_the_subtitle_back(app):
    """The offer is timed. A press three seconds later is a fresh first press,
    not a confirmation of something the reader has stopped thinking about."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        settled = app.sub_title

        app.action_interrupt()
        await pilot.pause()
        assert app.sub_title == "press ctrl+C again to exit"

        app._withdraw_offer("exit")  # what the timer fires
        await pilot.pause()
        assert app.sub_title == settled

        app.action_interrupt()
        await pilot.pause()
        assert app.is_running, "the lapsed offer must not still be answerable"
        assert app.sub_title == "press ctrl+C again to exit"


async def test_esc_still_cancels_a_turn_first(app):
    """Esc has meant "stop this response" since the beginning; nothing about that
    changes, and it is checked before the tree offer."""
    aborted: list[bool] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = SimpleNamespace(abort=lambda: aborted.append(True))
        app.is_generating = True

        app.action_escape()
        await pilot.pause()

        assert aborted == [True]
        assert "tree" not in app.sub_title


async def test_esc_twice_opens_the_tree_and_once_only_says_so(app):
    """What Esc used to do idle was nothing at all — the binding is priority, so
    the key was consumed and the action no-op'd. Two presses rather than one
    because Esc is also the key people hit to mean "never mind"."""
    opened: list[bool] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        app.action_browse_tree = lambda: opened.append(True)  # type: ignore[method-assign]

        app.action_escape()
        await pilot.pause()
        assert app.sub_title == "press Esc again to view the tree"
        assert opened == []

        app.action_escape()
        await pilot.pause()
        assert opened == [True]


async def test_one_key_withdraws_the_other_keys_offer(app):
    """There is one status bar. An offer the reader can no longer see must not
    still be answerable — press Esc then ctrl+C and you get ctrl+C's warning, not
    an exit."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        app.action_escape()
        await pilot.pause()
        assert app.sub_title == "press Esc again to view the tree"

        app.action_interrupt()
        await pilot.pause()
        assert app.sub_title == "press ctrl+C again to exit"
        assert app.is_running
