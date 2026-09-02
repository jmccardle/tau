"""Sliding the mounted window along the transcript.

The window bounds how many turns are mounted (``test_live_transcript_window.py``);
this is about moving that bound. Scrolling against the top edge loads older turns
and drops the same number of newer ones, so the reader can walk the linear
history of any point in the conversation without the mounted count — the only
thing the render cost depends on — ever growing.

Reference: docs/TRANSCRIPT-WINDOW.md §7
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tau_coding_agent.app import ChatDisplay, MessageBox
from tau_coding_agent.chat_widgets import ExchangeBox

CAP = ChatDisplay.RENDER_CAP_TURNS


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ChatDisplay()


def _transcript(turns: int, *, system: bool = False) -> list[dict]:
    """A flat persisted transcript: one user + one assistant message per turn."""
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": "sys"})
    for i in range(turns):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


def _users(display: ChatDisplay) -> list[str]:
    """The user prompts currently mounted, top to bottom."""
    return [b.content_text for b in display.query(MessageBox) if b.role == "user"]


def _content(display: ChatDisplay) -> list:
    return [c for c in display.children if isinstance(c, (MessageBox, ExchangeBox))]


@pytest.fixture
async def loaded():
    """A display showing the tail of a 20-turn transcript."""
    messages = _transcript(20)
    async with _Harness().run_test(size=(80, 24)) as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: messages)
        await display.reload_messages(messages)
        await pilot.pause()
        yield display, pilot, messages


# ---------------------------------------------------------------------------
# window_end: the forward twin of render_cap_start
# ---------------------------------------------------------------------------


def test_window_end_agrees_with_render_cap_start_at_the_tail():
    """The identity the whole design rests on. Without it, sliding forward to the
    end would leave a phantom ``⋯ 0 later`` row, and a reload and a
    scrolled-back-then-forward window would mount different things."""
    display = ChatDisplay()
    for turns in range(1, 30):
        for system in (False, True):
            messages = _transcript(turns, system=system)
            start = display.render_cap_start(messages)
            assert display.window_end(messages, start) == len(messages), (turns, system)


def test_a_window_holds_the_cap_in_turns_wherever_it_starts():
    display = ChatDisplay()
    messages = _transcript(20)
    for start in display.turn_starts(messages)[:-CAP]:
        end = display.window_end(messages, start)
        mounted = sum(1 for m in messages[start:end] if m.get("role") == "user")
        assert mounted == CAP, start


def test_a_leading_system_message_does_not_cost_a_turn():
    """Counting turns from ``start`` rather than from the window's first TURN
    would spend one of the four on a message that renders nothing."""
    display = ChatDisplay()
    messages = _transcript(20, system=True)
    end = display.window_end(messages, 0)
    assert sum(1 for m in messages[0:end] if m.get("role") == "user") == CAP


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


def _turn_widgets(display: ChatDisplay) -> int:
    """Widgets belonging to mounted TURNS — the number the render cost is
    proportional to. Excludes the ``⋯`` rows, which are chrome and which a move
    can legitimately add one of."""
    return sum(len(c.query("*")) + 1 for c in _content(display))


async def test_moving_back_loads_older_turns_and_drops_newer_ones(loaded):
    display, pilot, _ = loaded
    assert _users(display) == ["q16", "q17", "q18", "q19"]
    mounted = _turn_widgets(display)

    assert await display.move_window(-1)
    await pilot.pause()

    assert _users(display) == ["q15", "q16", "q17", "q18"]
    # The bound did not move, only the span it names. This is the whole claim of
    # a sliding window over a growing one: reading history costs nothing.
    assert _turn_widgets(display) == mounted


async def test_moving_back_repeatedly_walks_to_the_top(loaded):
    display, pilot, _ = loaded
    for _ in range(50):
        if not await display.move_window(-1):
            break
        await pilot.pause()

    assert _users(display) == ["q0", "q1", "q2", "q3"]
    assert display.elided_count == 0
    assert display.later_count == 2 * (20 - CAP)
    # Refuses to walk off the front rather than raising or looping.
    assert not await display.move_window(-1)


async def test_moving_forward_returns_to_the_tail(loaded):
    display, pilot, _ = loaded
    for _ in range(50):
        if not await display.move_window(-1):
            break
        await pilot.pause()
    assert display.later_count

    for _ in range(50):
        if not await display.move_window(1):
            break
        await pilot.pause()

    assert _users(display) == ["q16", "q17", "q18", "q19"]
    # Back on the tail exactly, which is what lets a live turn mount again.
    assert display.later_count == 0
    assert display._window_end == len(display._reload_source)


async def test_both_rows_appear_in_the_middle_of_the_transcript(loaded):
    display, pilot, _ = loaded
    for _ in range(5):
        await display.move_window(-1)
        await pilot.pause()

    rows = [str(r.content) for r in display.query(".chat-fold")]
    assert len(rows) == 2
    assert rows[0].startswith(f"⋯ {display.elided_count} earlier")
    assert rows[1].startswith(f"⋯ {display.later_count} later")
    # The earlier row stands above the transcript and the later row below it.
    children = list(display.children)
    content = _content(display)
    assert children.index(display.query(".chat-fold").first()) < children.index(content[0])
    assert children.index(display.query(".chat-fold-later").first()) > children.index(content[-1])


async def test_the_reader_keeps_their_place_across_a_move(loaded):
    """A move re-renders everything, so the turn they were reading has to be put
    back under the top edge or a step reads as a jump.

    No ``scroll_home`` first: that gesture IS a move now, and doing it here would
    measure the anchor of a move this test did not make.
    """
    display, pilot, _ = loaded
    assert display._window_start == 32  # q16 opens the window

    assert await display.move_window(-1)
    await pilot.pause()

    # q15 is now loaded ABOVE q16, and the view sits on q16 — the turn that was
    # at the top before the move — rather than back at the document's start.
    assert _users(display)[0] == "q15"
    assert display.scroll_offset.y > 0
    anchor = display._turn_anchors[32]
    assert anchor.content_text == "q16"
    assert display.scroll_offset.y == anchor.virtual_region.y


# ---------------------------------------------------------------------------
# Scrolling is the gesture
# ---------------------------------------------------------------------------


async def test_arriving_at_the_top_does_not_slide(loaded):
    """The first scroll takes the reader TO the edge. Sliding there would mean the
    ``⋯ N earlier`` row can never be looked at or clicked — reaching it would load
    more and scroll it away, every time."""
    display, pilot, _ = loaded
    before = display._window_start

    display.scroll_home(animate=False)
    await pilot.pause()
    await pilot.pause()

    assert display._window_start == before
    assert _users(display)[0] == "q16"
    # The row the reader came for is on screen and clickable.
    assert display.query(".chat-fold")


async def test_scrolling_against_the_top_slides_the_window(loaded):
    display, pilot, _ = loaded
    display.scroll_home(animate=False)
    await pilot.pause()

    # A second scroll, with nowhere left to go, is the gesture.
    display.action_scroll_up()
    await pilot.pause()
    await pilot.pause()

    assert _users(display)[0] == "q15"
    assert display.later_count == 2


async def test_scrolling_against_the_bottom_slides_it_back(loaded):
    display, pilot, _ = loaded
    for _ in range(3):
        await display.move_window(-1)
        await pilot.pause()
    before = display._window_start

    display.scroll_end(animate=False)
    await pilot.pause()
    display.action_scroll_down()
    await pilot.pause()
    await pilot.pause()

    starts = display.turn_starts(display._reload_source)
    assert starts.index(display._window_start) == starts.index(before) + 1


async def test_one_gesture_moves_one_turn(loaded):
    """The claim is taken in the handler, not the coroutine: one flick of a wheel
    is several events, each arriving before any scheduled call runs."""
    display, pilot, _ = loaded
    display.scroll_home(animate=False)
    await pilot.pause()
    before = display._window_start

    display.action_scroll_up()
    display.action_scroll_up()
    display.action_scroll_up()
    await pilot.pause()
    await pilot.pause()

    starts = display.turn_starts(display._reload_source)
    assert starts.index(display._window_start) == starts.index(before) - 1


# ---------------------------------------------------------------------------
# Live turns
# ---------------------------------------------------------------------------


async def test_a_starting_turn_snaps_the_window_back_to_the_tail(loaded):
    """The live state machine mounts into the END of the display, so a window
    showing turn 5 of 20 would grow a live exchange under turn 8."""
    display, pilot, messages = loaded
    for _ in range(6):
        await display.move_window(-1)
        await pilot.pause()
    assert display.later_count

    await display.begin_exchange()
    await pilot.pause()

    assert display.later_count == 0
    assert _users(display)[-1] == "q19"
    # The live exchange really is the last thing, where the stream will land.
    assert isinstance(_content(display)[-1], ExchangeBox)


async def test_the_window_does_not_move_while_a_lane_streams(loaded):
    display, pilot, _ = loaded
    await display.begin_exchange("other", label="agent · fork")
    await pilot.pause()
    start = display._window_start

    assert not await display.move_window(-1)
    display.scroll_home(animate=False)
    await pilot.pause()
    await pilot.pause()

    assert display._window_start == start


async def test_a_trim_never_runs_against_a_slid_window(loaded):
    """trim_to_cap counts user boxes back from the END, which answers for a span
    that is not mounted once the reader has slid away from the tail."""
    display, pilot, _ = loaded
    for _ in range(5):
        await display.move_window(-1)
        await pilot.pause()
    users_before = _users(display)

    assert await display.trim_to_cap() == 0
    assert _users(display) == users_before


# ---------------------------------------------------------------------------
# The escape hatch still works from either end
# ---------------------------------------------------------------------------


async def test_show_all_works_from_the_top_of_a_slid_window(loaded):
    """At the top there is nothing hidden ABOVE, so guarding on elided_count
    would report "nothing to do" on the one gesture that had plenty to do."""
    display, pilot, _ = loaded
    for _ in range(50):
        if not await display.move_window(-1):
            break
        await pilot.pause()
    assert display.elided_count == 0
    assert display.hidden_count

    await display.show_all_messages()
    await pilot.pause()

    assert display.hidden_count == 0
    assert _users(display) == [f"q{i}" for i in range(20)]


async def test_a_reload_always_lands_on_the_tail(loaded):
    """A reload REPLACES the transcript, so a window position taken in the old
    document names nothing in the new one."""
    display, pilot, messages = loaded
    for _ in range(5):
        await display.move_window(-1)
        await pilot.pause()

    await display.reload_messages(messages)
    await pilot.pause()

    assert display.later_count == 0
    assert _users(display) == ["q16", "q17", "q18", "q19"]


async def test_a_short_transcript_has_nowhere_to_slide():
    messages = _transcript(2)
    async with _Harness().run_test(size=(80, 24)) as pilot:
        display = pilot.app.query_one(ChatDisplay)
        display.set_transcript_source(lambda: messages)
        await display.reload_messages(messages)
        await pilot.pause()

        assert not await display.move_window(-1)
        assert not await display.move_window(1)
        assert not display.query(".chat-fold")
