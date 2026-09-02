"""The live transcript window: a session that keeps streaming stops growing.

``ChatDisplay`` capped a RELOADED transcript from the start
(``RENDER_CAP_TURNS``) and capped a live one never, so a chat that was trimmed
when it was opened climbed straight back past the cap as the reader worked in
it. Every mounted widget is one more node ``_compositor.full_map`` walks on every
streamed delta, and the cost of a turn is therefore the size of the whole
transcript above it.

These drive the real ``ChatDisplay`` state machine headlessly, the same way
``test_chat_rendering.py`` does, with a transcript source the test owns.

Reference: docs/TRANSCRIPT-WINDOW.md
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tau_coding_agent.app import ChatDisplay, MessageBox
from tau_coding_agent.chat_widgets import ExchangeBox


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ChatDisplay()


def _turn_messages(index: int) -> list[dict]:
    """The two persisted messages one no-tool turn leaves behind."""
    return [
        {"role": "user", "content": f"q{index}"},
        {"role": "assistant", "content": [{"type": "text", "text": f"a{index}"}]},
    ]


async def _live_turn(display: ChatDisplay, pilot, index: int, *, lane: str = "default") -> None:
    """Stream one complete no-tool turn through the live state machine."""
    display.add_message("user", f"q{index}", source="verbatim")
    await display.begin_exchange(lane)
    await display.handle_stream_event({"kind": "turn_start", "turn_index": 0, "lane": lane})
    await display.handle_stream_event({"kind": "text_delta", "delta": f"a{index}", "lane": lane})
    await display.finalize_exchange(context=100, output=10, seconds=1.0, lane=lane)
    await pilot.pause()


def _content_children(display: ChatDisplay) -> list:
    return [c for c in display.children if isinstance(c, (MessageBox, ExchangeBox))]


@pytest.fixture
def transcript() -> list[dict]:
    """The list the app would hold. Mutated in place by the tests, and read
    through a callable, which is the whole point of ``set_transcript_source``."""
    return []


async def test_a_live_session_stops_growing_at_the_cap(transcript):
    """The defect this exists for: 20 live turns used to leave 20 turns mounted."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)

        for index in range(20):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)

        # RENDER_CAP_TURNS user messages survive, and one user box is mounted per
        # user message — that correspondence is what trim_to_cap cuts on.
        users = [b for b in display.query(MessageBox) if b.role == "user"]
        assert len(users) == ChatDisplay.RENDER_CAP_TURNS
        assert [b.content_text for b in users] == ["q16", "q17", "q18", "q19"]


async def test_the_count_matches_what_a_reload_would_have_elided(transcript):
    """One number, one meaning. The row is the reload's row, so the live window
    has to hide exactly what a reload of the same transcript hides — otherwise
    ``⋯ N earlier`` means two different things on two paths."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)
        for index in range(20):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        live_count = display.elided_count

        await display.reload_messages(list(transcript))
        await pilot.pause()

        assert live_count == display.elided_count
        assert display.query_one(".chat-fold")


async def test_the_row_says_the_count_and_is_mounted_once(transcript):
    """Updated in place across turns, not remounted — churning a widget every
    turn is the cost the window exists to stop paying."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)
        for index in range(12):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)

        rows = display.query(".chat-fold")
        assert len(rows) == 1
        expected = f"⋯ {display.elided_count} earlier · scroll up to load, click for all"
        assert str(rows.first().content) == expected
        # It stands ABOVE the transcript, where the messages it counts would be.
        assert list(display.children).index(rows.first()) < list(display.children).index(
            _content_children(display)[0]
        )


async def test_nothing_is_evicted_while_the_reader_is_scrolled_up(transcript):
    """docs/TUI-STEERING.md's whole point: reading while it writes. Removing the
    head moves every row under someone mid-sentence.

    The reader scrolls up but NOT onto the top edge. That edge is now a gesture
    of its own — it slides the window back into history
    (``test_sliding_window.py``) — and a turn starting snaps a slid window
    forward again. Scrolling up to re-read the turn above is the case this test
    is about, and it is the case that stays undisturbed.
    """
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)
        for index in range(8):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        trimmed = len(_content_children(display))

        display.scroll_to(y=max(1, display.max_scroll_y - 5), animate=False)
        await pilot.pause()
        assert not display._follow_tail
        assert display.scroll_offset.y > 0

        for index in range(8, 14):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)

        # Held, not run: the transcript grew and nothing was taken away.
        assert len(_content_children(display)) > trimmed
        assert display._trim_deferred

        # Returning to the tail is what releases it.
        display.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()

        assert not display._trim_deferred
        users = [b for b in display.query(MessageBox) if b.role == "user"]
        assert len(users) == ChatDisplay.RENDER_CAP_TURNS


async def test_nothing_is_evicted_while_another_lane_streams(transcript):
    """trim_to_cap cuts by transcript position, which says nothing about which
    lane a widget belongs to — so it must not run with one still open."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)
        for index in range(12):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        settled = len(_content_children(display))

        # Open a second lane and leave it streaming, then close the first.
        await display.begin_exchange("other", label="agent · fork")
        await display.handle_stream_event({"kind": "turn_start", "turn_index": 0, "lane": "other"})
        transcript.extend(_turn_messages(12))
        display.add_message("user", "q12", source="verbatim")
        await display.begin_exchange()
        await display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        await display.handle_stream_event({"kind": "text_delta", "delta": "a12"})
        await display.finalize_exchange(context=100, output=10, seconds=1.0)
        await pilot.pause()

        # The open lane held the trim off, so the transcript grew instead.
        assert len(_content_children(display)) > settled


async def test_show_all_mounts_the_turns_no_reload_ever_saw(transcript):
    """The stale-source bug the callable exists to prevent: the app REBINDS its
    message list every turn, so restoring the last reload's list would show the
    reader less than they already had."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)

        # A reload establishes _reload_source, then live turns move past it.
        transcript.extend(m for i in range(3) for m in _turn_messages(i))
        await display.reload_messages(list(transcript))
        await pilot.pause()
        for index in range(3, 15):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        assert display.elided_count

        await display.show_all_messages()
        await pilot.pause()

        assert display.elided_count == 0
        users = [b.content_text for b in display.query(MessageBox) if b.role == "user"]
        assert users == [f"q{i}" for i in range(15)]


async def test_a_short_session_is_left_alone(transcript):
    """A transcript within the cap is not touched, and grows no ``⋯`` row."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)
        for index in range(3):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)

        assert display.elided_count == 0
        assert not display.query(".chat-fold")
        users = [b.content_text for b in display.query(MessageBox) if b.role == "user"]
        assert users == ["q0", "q1", "q2"]


async def test_the_widget_count_stays_bounded(transcript):
    """The measurement the window is for, as an assertion: the DOM a delta pays
    for stops growing, instead of growing with the session."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: transcript)

        for index in range(6):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        early = len(display.query("*"))

        for index in range(6, 40):
            transcript.extend(_turn_messages(index))
            await _live_turn(display, pilot, index)
        late = len(display.query("*"))

        # 34 more turns, and the tree the compositor walks did not grow with them.
        assert late <= early + 2


async def test_a_display_with_no_source_still_reloads(transcript):
    """A bare renderer harness sets no source. It keeps the old behaviour rather
    than losing its transcript to an empty callable."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        messages = [m for i in range(20) for m in _turn_messages(i)]
        await display.reload_messages(messages)
        await pilot.pause()
        assert display.elided_count

        await display.show_all_messages()
        await pilot.pause()

        assert display.elided_count == 0
        assert len([b for b in display.query(MessageBox) if b.role == "user"]) == 20
