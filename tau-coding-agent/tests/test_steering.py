"""Typing and reading while the model is generating.

Reference: docs/TUI-STEERING.md.

Two locks came off in this change and each had a reason, so each gets held here:
the transcript no longer snaps to the bottom while a reader is looking somewhere
else, and the editor no longer refuses input for the length of a turn. The rest
is what happens to a line typed during a turn — where it waits, when it is
delivered, and how the user takes it back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.app import ChatDisplay, ChatInput, Parley, PendingInput
from tau_coding_agent.backends import TurnStream
from tau_coding_agent.config import ConfigError


class _Submit:
    """Duck-typed Input.Submitted — ``on_input_submitted`` only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


class _SteerBackend:
    """A backend whose ``submit_turn`` blocks until it is released.

    Deliberately a near-copy of ``test_app_actions._BlockingBackend`` rather than
    an import of it: this one also has to answer for a ``multitask_strategy=
    "steer"`` submission, which returns at ONCE without waiting for the running
    turn, because that is the whole behaviour under test.
    """

    def __init__(self) -> None:
        self.aborted = False
        self._released = asyncio.Event()
        self._log: Any = None
        self.submissions: list[Any] = []

    def bind_session_log(self, session_log) -> None:
        self._log = session_log

    def abort(self) -> None:
        self.aborted = True
        self._released.set()

    def release(self) -> None:
        self._released.set()

    @property
    def turn_texts(self) -> list[str]:
        return [s.text for s in self.submissions if s.multitask_strategy != "steer"]

    @property
    def steer_texts(self) -> list[str]:
        return [s.text for s in self.submissions if s.multitask_strategy == "steer"]

    async def submit_turn(self, submission, context) -> SubmissionResult:
        self.submissions.append(submission)
        if submission.multitask_strategy == "steer":
            # The core queues a steer against the running loop and returns
            # immediately with no messages — it starts no turn of its own.
            return SubmissionResult(accepted=True, submission_id=submission.submission_id)
        await self._released.wait()
        self._released.clear()
        self._log.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}
        )
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)


@pytest.fixture
def app(make_app):
    """A sandboxed app with a backend that never runs a turn.

    The scroll tests drive :class:`ChatDisplay` directly, so nothing here needs
    to reach a model; what they need is a real mounted widget with a real size.
    """
    return make_app(create_backend=lambda cfg: _SteerBackend())


@pytest.fixture
def steer_app(make_app):
    """A running app whose turns block until the test releases them."""

    def _build(strategy: str = "steer") -> tuple[Parley, _SteerBackend]:
        backend = _SteerBackend()
        app = make_app(
            create_backend=lambda cfg: backend,
            config={"steering_strategy": strategy},
        )
        return app, backend

    return _build


async def _until(pilot, predicate, tries: int = 100) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("condition never became true")


async def _settle(app, backend, pilot, tries: int = 200) -> None:
    """Release turns until the app is idle.

    Every test here has to end this way. A ``run_test`` block exited while a
    worker is still blocked in ``submit_turn`` tears the app down underneath that
    worker, and its ``finally`` then writes ``is_generating``, whose watcher asks
    the screen stack that no longer exists.
    """
    for _ in range(tries):
        if not app.is_generating:
            return
        backend.release()
        await pilot.pause()
    raise AssertionError("the app never went idle")


# ---------------------------------------------------------------------------
# §1 — the scroll lock
# ---------------------------------------------------------------------------


class TestFollowingTheTail:
    async def test_new_content_scrolls_down_while_the_reader_is_at_the_bottom(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            for i in range(40):
                display.add_message("user", f"line {i}", source="verbatim")
            await pilot.pause()

            assert display._follow_tail is True
            # Pumped rather than asserted outright: the scroll lands after the
            # layout that the mounts queued, not in the same frame as the call.
            await _until(pilot, lambda: display.is_vertical_scroll_end)

    async def test_scrolling_up_stops_the_view_being_dragged_back_down(self, app):
        """The behaviour the whole change is for: a turn that streams for a minute
        must not yank the view back to the bottom on every delta."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            for i in range(60):
                display.add_message("user", f"line {i}", source="verbatim")
            await pilot.pause()

            display.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert display._follow_tail is False

            here = display.scroll_offset.y
            display.add_message("assistant", "a streamed delta", source="verbatim")
            await pilot.pause()

            assert display.scroll_offset.y == here, "the view was dragged to the bottom"

    async def test_scrolling_back_to_the_bottom_re_attaches(self, app):
        """No separate gesture to learn: being at the bottom IS following."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            for i in range(60):
                display.add_message("user", f"line {i}", source="verbatim")
            await pilot.pause()

            display.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert display._follow_tail is False

            display.scroll_end(animate=False)
            await pilot.pause()
            assert display._follow_tail is True

    async def test_a_reload_re_attaches_the_tail(self, app):
        """A reload replaces the transcript, so a scroll position taken in the old
        document does not describe a place in the new one."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            for i in range(60):
                display.add_message("user", f"line {i}", source="verbatim")
            await pilot.pause()
            display.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert display._follow_tail is False

            await display.reload_messages([{"role": "user", "content": "fresh"}])
            await pilot.pause()

            assert display._follow_tail is True


# ---------------------------------------------------------------------------
# §2 — where a mid-turn line goes
# ---------------------------------------------------------------------------


class TestThePendingBuffer:
    async def test_the_editor_stays_usable_during_a_turn(self, steer_app):
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()

            assert app.is_generating is True
            assert app.query_one("#chat-input", ChatInput).disabled is False

            await _settle(app, backend, pilot)

    async def test_a_line_typed_mid_turn_is_held_not_submitted(self, steer_app):
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()

            await app.on_input_submitted(_Submit("actually, use ripgrep"))
            await pilot.pause()

            assert app._pending_steer == ["actually, use ripgrep"]
            assert backend.turn_texts == ["go"]
            assert backend.steer_texts == []
            pending = app.query_one(PendingInput)
            assert pending.display is True
            assert "actually, use ripgrep" in pending.text

            await _settle(app, backend, pilot)

    async def test_a_second_line_joins_the_first(self, steer_app):
        """The "all at once" convention: one delivery, both lines, in order."""
        app, backend = steer_app(strategy="enqueue")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("first"))
            await app.on_input_submitted(_Submit("second"))
            await pilot.pause()

            assert app._pending_steer == ["first", "second"]

            backend.release()
            await _until(pilot, lambda: len(backend.turn_texts) == 2)
            assert backend.turn_texts[1] == "first\n\nsecond"
            await _settle(app, backend, pilot)

    async def test_a_command_typed_mid_turn_is_refused_and_left_in_the_editor(self, steer_app):
        """``/compact`` rewrites the context the running turn is being answered
        from, and steering delivers through the door that dispatches it."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()

            editor.text = "/compact"
            await app.on_input_submitted(_Submit("/compact"))
            await pilot.pause()

            assert app._pending_steer == []
            assert editor.text == "/compact", "the typed command was thrown away"

            await _settle(app, backend, pilot)

    async def test_enqueue_delivers_the_buffer_as_its_own_turn_at_the_edge(self, steer_app):
        app, backend = steer_app(strategy="enqueue")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("and then tidy up"))
            await pilot.pause()

            backend.release()
            await _until(pilot, lambda: len(backend.turn_texts) == 2)

            assert backend.turn_texts == ["go", "and then tidy up"]
            assert backend.steer_texts == []
            assert app._pending_steer == []
            assert app.query_one(PendingInput).display is False
            await _settle(app, backend, pilot)

    async def test_enqueue_does_not_deliver_at_a_tool_call(self, steer_app):
        """The tool-call boundary belongs to ``"steer"`` alone."""
        app, backend = steer_app(strategy="enqueue")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("hold on"))
            await pilot.pause()

            app._flush_pending_steer(at_tool_call=True)
            await pilot.pause()

            assert app._pending_steer == ["hold on"]
            assert backend.steer_texts == []

            await _settle(app, backend, pilot)

    async def test_steer_delivers_at_a_tool_call_as_a_steer_submission(self, steer_app):
        app, backend = steer_app(strategy="steer")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("use ripgrep instead"))
            await pilot.pause()

            # What the render router delivers when the running turn starts a tool.
            await app._on_render_event(
                {"kind": "tool_call", "lane": "l1", "id": "tc1", "name": "read", "arguments": {}}
            )
            await _until(pilot, lambda: backend.steer_texts == ["use ripgrep instead"])

            assert app._pending_steer == []
            assert app.query_one(PendingInput).display is False
            steered = [s for s in backend.submissions if s.multitask_strategy == "steer"][0]
            assert steered.source == "interactive"
            assert steered.submitter == "human"
            # An `input` hook could rewrite prose into a command, and running
            # ``/compact`` inside a live turn is what this flag prevents.
            assert steered.expand_commands is False

            await _settle(app, backend, pilot)

    async def test_steer_falls_through_to_the_turn_edge_with_no_tool_call(self, steer_app):
        """Not a fallback: a turn that makes no further call to the model has no
        "before its next call" left in it, so the message becomes its own turn."""
        app, backend = steer_app(strategy="steer")
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("one more thing"))
            await pilot.pause()

            backend.release()
            await _until(pilot, lambda: len(backend.turn_texts) == 2)

            assert backend.steer_texts == []
            assert backend.turn_texts == ["go", "one more thing"]
            await _settle(app, backend, pilot)


# ---------------------------------------------------------------------------
# §4 — reclaim
# ---------------------------------------------------------------------------


class TestReclaim:
    async def test_up_on_an_empty_editor_takes_the_buffer_back(self, steer_app):
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("wait"))
            await app.on_input_submitted(_Submit("no, this"))
            await pilot.pause()

            await pilot.press("up")
            await pilot.pause()

            assert editor.text == "wait\n\nno, this"
            assert app._pending_steer == []
            assert app.query_one(PendingInput).display is False

            await _settle(app, backend, pilot)

    async def test_esc_gives_the_pending_message_back_instead_of_running_it(self, steer_app):
        """Esc cancels the turn the message was aimed at, so delivering it would
        launch a turn the user just stopped."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("no, stop"))
            await pilot.pause()

            app.action_cancel_generation()
            await _until(pilot, lambda: app.is_generating is False)

            assert editor.text == "no, stop"
            assert app._pending_steer == []
            assert backend.turn_texts == ["go"], "the cancelled steer ran as a turn"

    async def test_the_reclaimed_text_goes_in_front_of_a_draft(self, steer_app):
        """The pending message was typed first, so it reads first. pi combines
        the two the same way round (``restoreQueuedMessagesToEditor``)."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("pending"))
            await pilot.pause()
            editor.text = "half-typed"

            app.action_cancel_generation()
            await _until(pilot, lambda: app.is_generating is False)

            assert editor.text == "pending\n\nhalf-typed"

    async def test_alt_up_reclaims_even_with_a_draft_in_the_box(self, steer_app):
        """pi's own binding for this (``app.message.dequeue``). Bare Up cannot
        cover this case — with a draft in the box it has to mean history."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("pending"))
            await pilot.pause()
            editor.text = "half-typed"
            editor.focus()
            await pilot.pause()

            await pilot.press("alt+up")
            await pilot.pause()

            assert editor.text == "pending\n\nhalf-typed"
            assert app._pending_steer == []

            await _settle(app, backend, pilot)

    async def test_a_new_chat_gives_the_pending_message_back(self, steer_app):
        """A line written about one conversation must not be delivered into
        another."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("about the old chat"))
            await pilot.pause()

            # Every session swap the app can do — new chat, clear, resume, model
            # swap — arrives here.
            app._rebind_after_session_swap()
            await pilot.pause()

            assert app._pending_steer == []
            assert editor.text == "about the old chat"

            await _settle(app, backend, pilot)
            assert backend.turn_texts == ["go"]

    async def test_up_with_a_draft_in_the_editor_still_means_history(self, steer_app):
        """Reclaiming into a box that already had a draft would overwrite it."""
        app, backend = steer_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("pending line"))
            await pilot.pause()

            editor.text = "a draft"
            editor.move_cursor((0, 0))
            await pilot.press("up")
            await pilot.pause()

            # History, not the buffer: "go" and "pending line" were both added to
            # it, so Up walks back to the newest of them.
            assert editor.text == "pending line"
            assert app._pending_steer == ["pending line"]

            await _settle(app, backend, pilot)

    def test_reclaim_with_nothing_pending_reports_nothing(self, make_app):
        app = make_app()
        assert app._reclaim_pending_steer() is None


# ---------------------------------------------------------------------------
# §2 — the setting
# ---------------------------------------------------------------------------


class TestTheSetting:
    def test_the_default_is_steer(self, make_app):
        assert make_app()._steering_strategy == "steer"

    def test_an_unknown_strategy_raises_rather_than_selecting_a_default(self, make_app):
        """Fail-Early: the two strategies put the message in a different place at a
        different time, so a typo that selected the other one would be invisible
        until a steering message failed to land where it was aimed."""
        app = make_app(config={"steering_strategy": "steering"})
        with pytest.raises(ConfigError, match="steering_strategy"):
            app._steering_strategy

    def test_a_bad_value_on_disk_stops_the_app_starting(self, tau_home):
        """The startup check: ``__init__`` resolves it once and discards the value,
        so the failure lands while τ is starting rather than mid-turn."""
        import json

        from tau_coding_agent.session_store import FileSessionCatalog
        from tau_coding_agent import config as config_module

        config_module.CONFIG_PATH.write_text(
            json.dumps({"models": {}, "steering_strategy": "nope"})
        )
        with pytest.raises(ConfigError, match="steering_strategy"):
            Parley(session_catalog=FileSessionCatalog(tau_home / "sessions"))

    def test_the_packaged_default_config_names_a_real_strategy(self):
        import json

        from tau_coding_agent.app import STEERING_CONFIG_KEY, STEERING_STRATEGIES
        from tau_coding_agent.config import DEFAULT_CONFIG_TEMPLATE

        template = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())
        assert template[STEERING_CONFIG_KEY] in STEERING_STRATEGIES


# ---------------------------------------------------------------------------
# §5 — showing a delivered steering message
# ---------------------------------------------------------------------------


class _Event:
    def __init__(self, type: str, message: dict | None) -> None:
        self.type = type
        self.message = message


class TestRenderingADeliveredSteer:
    def test_a_user_message_start_becomes_a_steer_message_event(self):
        """``_deliver_steer`` is the only producer of a USER ``message_start``."""
        stream = TurnStream("lane-1")

        out = stream.feed(
            _Event(
                "message_start",
                {"role": "user", "content": [{"type": "text", "text": "use ripgrep"}]},
            )
        )

        assert out == [{"kind": "steer_message", "text": "use ripgrep", "lane": "lane-1"}]

    def test_a_plain_string_content_is_read_too(self):
        stream = TurnStream("lane-1")
        out = stream.feed(_Event("message_start", {"role": "user", "content": "plain"}))
        assert out == [{"kind": "steer_message", "text": "plain", "lane": "lane-1"}]

    def test_an_assistant_message_start_produces_nothing(self):
        """It brackets a completion whose content arrives as deltas; rendering it
        here would draw the answer twice."""
        stream = TurnStream("lane-1")

        out = stream.feed(
            _Event(
                "message_start", {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
            )
        )

        assert out == []

    async def test_the_display_mounts_it_inside_the_open_exchange(self, app):
        """In arrival order: it is a user turn that happened inside somebody else's
        exchange, so hoisting it out would put it above content that preceded it."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            await display.begin_exchange("lane-1", label=None)
            await pilot.pause()

            await display.handle_stream_event(
                {"kind": "steer_message", "lane": "lane-1", "text": "use ripgrep"}
            )
            await pilot.pause()

            exchange = display._lanes["lane-1"].exchange
            assert exchange is not None
            texts = [w._content for w in exchange.query("MessageBox")]
            assert "use ripgrep" in texts

    async def test_a_steer_arriving_last_is_not_promoted_as_the_answer(self, app):
        """``_close_exchange`` promotes the terminal step out below the summary.
        A steering message is a USER box, and promoting one would render the
        person's own words as the model's reply."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            await display.begin_exchange("lane-1", label=None)
            await display.handle_stream_event({"kind": "turn_start", "lane": "lane-1"})
            await display.handle_stream_event(
                {"kind": "text_delta", "lane": "lane-1", "delta": "reading it now"}
            )
            await display.handle_stream_event(
                {
                    "kind": "tool_call",
                    "lane": "lane-1",
                    "id": "tc1",
                    "name": "read",
                    "arguments": {},
                }
            )
            await display.handle_stream_event(
                {"kind": "tool_result", "lane": "lane-1", "id": "tc1", "result": "ok"}
            )
            await display.handle_stream_event(
                {"kind": "steer_message", "lane": "lane-1", "text": "stop, use ripgrep"}
            )
            await pilot.pause()

            await display.finalize_exchange(context=1, output=1, seconds=1.0, lane="lane-1")
            await pilot.pause()

            # Nothing is promoted here — the last ASSISTANT step still holds a
            # tool box, which is the pre-existing "no clean final answer" case.
            # The defect was that the steering message, having no tool box,
            # looked like one.
            top_level = [w._content for w in display.query("MessageBox") if w.parent is display]
            assert "stop, use ripgrep" not in top_level

    async def test_an_answer_after_a_steer_is_still_promoted(self, app):
        """The narrowing must not cost the ordinary promotion: a steer in the
        middle of an exchange leaves the model's last step promotable."""
        async with app.run_test() as pilot:
            await pilot.pause()
            display = app.query_one(ChatDisplay)
            await display.begin_exchange("lane-1", label=None)
            await display.handle_stream_event({"kind": "turn_start", "lane": "lane-1"})
            await display.handle_stream_event(
                {
                    "kind": "tool_call",
                    "lane": "lane-1",
                    "id": "tc1",
                    "name": "read",
                    "arguments": {},
                }
            )
            await display.handle_stream_event(
                {"kind": "tool_result", "lane": "lane-1", "id": "tc1", "result": "ok"}
            )
            await display.handle_stream_event(
                {"kind": "steer_message", "lane": "lane-1", "text": "stop, use ripgrep"}
            )
            await display.handle_stream_event({"kind": "turn_start", "lane": "lane-1"})
            await display.handle_stream_event(
                {"kind": "text_delta", "lane": "lane-1", "delta": "ripgrep it is"}
            )
            await pilot.pause()

            await display.finalize_exchange(context=1, output=1, seconds=1.0, lane="lane-1")
            await pilot.pause()

            top_level = [w._content for w in display.query("MessageBox") if w.parent is display]
            assert "ripgrep it is" in top_level, "the answer was not promoted"
            assert "stop, use ripgrep" not in top_level
