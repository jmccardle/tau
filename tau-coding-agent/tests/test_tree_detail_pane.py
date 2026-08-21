"""The tree browser's detail pane: the highlighted node, in full, in context.

A browser row is one elided line, which says *which* node without saying *what*
it is. ``TreeDetailPane`` answers the second question beside the rows, using the
transcript's own renderer so a node reads there as it read in the chat.

What these tests pin, in the order the pane builds it:

* the three-node window (parent / selected / first child) and its ``⋯`` rows,
* which boxes are dimmed and which one is not,
* that a move of the tree cursor moves the pane,
* that the previous message stays on screen above the selection,
* the width below which the pane gives the screen back to the tree.

Reference: SESSION-TREE-IMPLEMENTATION.md §3, docs/textual-headless-testing.md
"""

from __future__ import annotations

import pytest
from textual.widgets import Tree

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.app import MessageBox, SessionTreeModal, TreeDetailPane
from tau_coding_agent.testing.render import render_text
from tau_coding_agent.testing.scenes import get_scene, open_scene

#: Comfortably past ``SessionTreeModal.DETAIL_MIN_HEIGHT``, so the pane is drawn.
WIDE = (120, 40)


def _pane(app) -> TreeDetailPane:
    return app.screen.query_one(TreeDetailPane)


def _boxes(app) -> list[MessageBox]:
    return list(_pane(app).query(MessageBox))


def _titles(app) -> list[str]:
    """Each drawn box as ``"Assistant"`` / ``"Assistant (dim)"``.

    The border title is the box's role as the reader sees it, and the suffix is
    the one thing this pane adds to it — so one list says both what was drawn and
    what receded.
    """
    return [
        f"{box.border_title}{' (dim)' if box.has_class('detail-context') else ''}"
        for box in _boxes(app)
    ]


async def _move_to(app, pilot, node_id: str) -> None:
    """Put the tree cursor on ``node_id`` the way a key press would."""
    tree = app.screen.query_one("#tree-browser-tree", Tree)
    target = next(node for node in _walk(tree.root) if node.data == node_id)
    tree.move_cursor(target)
    await pilot.pause()
    await pilot.pause()


def _walk(node):
    for child in node.children:
        yield child
        yield from _walk(child)


# ---------------------------------------------------------------------------
# The three-node window
# ---------------------------------------------------------------------------


async def test_the_pane_draws_the_selected_node_between_its_neighbours() -> None:
    """The scene opens on the leaf ``n4``, whose parent is the ``grep`` result."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, _pilot):
        assert _titles(app) == ["Tool result (dim)", "Assistant"]


async def test_an_interior_node_is_drawn_with_a_neighbour_on_each_side() -> None:
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        # n1 above; n2 selected; n3 below, which is one assistant message whose
        # two tool calls render as boxes of their own — all of it context.
        assert _titles(app) == [
            "System (dim)",
            "User",
            "Assistant (dim)",
            "Tool call (dim)",
            "Tool call (dim)",
        ]


async def test_exactly_one_node_is_undimmed() -> None:
    """The pane's whole job is making one message the obvious one."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        for node_id in ("n1", "n2", "n3", "n4", "n5"):
            await _move_to(app, pilot, node_id)
            lit = [box for box in _boxes(app) if not box.has_class("detail-context")]
            assert lit == _pane(app).selected_boxes, node_id
            assert lit, f"{node_id} drew nothing at full strength"


async def test_moving_the_cursor_moves_the_pane() -> None:
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        before = _pane(app).selected_boxes[0].content_text
        await _move_to(app, pilot, "n1")
        after = _pane(app).selected_boxes[0].content_text
        assert before != after
        assert after.startswith("You are tau")


async def test_a_repeat_highlight_does_not_rebuild_the_pane() -> None:
    """Textual re-emits ``NodeHighlighted`` for events that do not move the
    cursor. Rebuilding on those would throw away the reader's scroll position and
    any collapsible they had just opened."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        drawn = _boxes(app)
        await _move_to(app, pilot, "n2")
        assert _boxes(app) == drawn, "the same node was rebuilt into new widgets"


# ---------------------------------------------------------------------------
# What the ⋯ rows say
# ---------------------------------------------------------------------------


def _folds(app) -> list[str]:
    return [str(row.content) for row in _pane(app).query(".detail-fold")]


async def test_the_fold_rows_count_what_is_not_drawn() -> None:
    """Selected ``n4``: four entries sit above its parent, nothing below it."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, _pilot):
        assert _folds(app) == ["⋯ 4 earlier"]


async def test_no_fold_row_is_drawn_when_nothing_is_hidden() -> None:
    """``n2``'s parent is the root, so there is no earlier row — the absence has
    to be silence, not ``⋯ 0 earlier``."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        assert not any(row.startswith("⋯ 0") for row in _folds(app))


async def test_a_fork_is_reported_apart_from_the_count() -> None:
    """``n2`` is where the history splits. That is a different fact from "there
    is more conversation below", and usually the reason the browser is open."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        assert _folds(app) == ["⋯ 2 branches from here, 3 later"]


async def test_a_leaf_with_nothing_below_it_gets_no_trailer() -> None:
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n5")
        assert _folds(app) == ["⋯ 1 earlier"]


# ---------------------------------------------------------------------------
# What a node's body is
# ---------------------------------------------------------------------------


async def test_a_branch_summary_shows_more_than_its_row_did() -> None:
    """The row is ``ConversationTree``'s first-line preview. The pane is the
    point at which the rest of the summary becomes readable."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n5")
        body = _pane(app).selected_boxes[0].content_text
        assert body.startswith("Abandoned branch:")
        assert "\n" in body, "the pane drew the row's single line, not the summary"
        assert "one layer up" in body


async def test_an_assistant_turn_brings_its_tool_calls() -> None:
    """One tree node is not one box: ``add_persisted_message`` is the shared
    renderer, so a turn arrives with the same boxes the transcript gave it."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n3")
        titles = [box.border_title for box in _pane(app).selected_boxes]
        assert titles == ["Assistant", "Tool call", "Tool call"]


async def test_a_non_message_node_is_titled_by_its_kind() -> None:
    """``role.capitalize()`` would render this node "Branch_summary"."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n5")
        assert _pane(app).selected_boxes[0].border_title == "Branch summary"


# ---------------------------------------------------------------------------
# Where the selection sits
# ---------------------------------------------------------------------------


async def test_the_previous_message_stays_on_screen_above_the_selection() -> None:
    """Both halves of the rule, which pull against each other: the selection's
    TOP edge must be visible (so it is not scrolled past), and the message before
    it must still be drawn (so the selection reads as following something)."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n4")
        pane = _pane(app)
        selected = pane.selected_boxes[0]
        above = [
            box
            for box in _boxes(app)
            if box.has_class("detail-context") and box.virtual_region.y < selected.virtual_region.y
        ]
        assert above, "n4 has a parent; the pane drew no message above it"
        assert pane.region.y <= selected.region.y < pane.region.bottom
        assert above[-1].region.bottom > pane.region.y


async def test_a_long_previous_message_is_scrolled_past_but_not_out() -> None:
    """``n2``'s parent is the system prompt and its own body is long, so the pane
    genuinely has to scroll. ``scroll_to_widget(top=True)`` would satisfy "top of
    the selection visible" by pushing the previous message off the pane."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await _move_to(app, pilot, "n2")
        pane = _pane(app)
        selected = pane.selected_boxes[0]
        assert pane.scroll_offset.y > 0, "nothing scrolled; this case proves nothing"
        lead = selected.virtual_region.y - pane.scroll_offset.y
        assert lead == pane.LEAD_ROWS


# ---------------------------------------------------------------------------
# The stacked split
# ---------------------------------------------------------------------------


async def test_the_pane_is_stacked_under_the_tree_at_the_full_width() -> None:
    """Not a column split. Both halves are wrapped text, and halving the width
    left the tree rows elided to a few characters and the pane's prose breaking
    every few words."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, _pilot):
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        pane = _pane(app)
        assert tree.region.width == pane.region.width
        assert tree.region.bottom <= pane.region.y, "the pane is beside the tree, not under it"


async def test_the_two_halves_are_even() -> None:
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, _pilot):
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        assert abs(tree.region.height - _pane(app).region.height) <= 1


async def test_the_pane_gives_the_rows_back_to_the_tree_when_short() -> None:
    async with open_scene(get_scene("tree-modal"), (120, 16)) as (app, _pilot):
        assert not _pane(app).display
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        assert tree.region.height >= 10, "the tree did not take the freed rows"


async def test_the_pane_is_drawn_at_its_stated_minimum() -> None:
    async with open_scene(get_scene("tree-modal"), (120, SessionTreeModal.DETAIL_MIN_HEIGHT)) as (
        app,
        _pilot,
    ):
        assert _pane(app).display


async def _selection_rows(height: int) -> int:
    """Rows of the SELECTED box the pane can show at terminal *height*.

    Its top border counts, because the border carries the role label — but a
    border alone identifies a node without showing it, which is what the rows
    beyond it are for.

    The pane is forced visible first: the question is what the layout WOULD give
    at this height, and asking it only where the pane already shows makes the
    answer circular. The branch scene, because its selected node has a neighbour
    on each side — the arrangement the floor is about.
    """
    async with open_scene(get_scene("tree-modal-branch"), (100, height)) as (app, pilot):
        pane = _pane(app)
        pane.display = True
        await app.screen._show_cursor_node()
        for _ in range(3):
            await pilot.pause()
        selected = pane.selected_boxes[0]
        top = max(selected.region.y, pane.region.y)
        return max(0, min(selected.region.bottom, pane.region.bottom) - top)


async def test_detail_pane_min_height_is_where_the_floor_is() -> None:
    """Re-measure the constant rather than trusting the prose beside it.

    Two assertions, because either alone is satisfiable by a wrong number: the
    first says the pane earns its rows where it is drawn, the second says it is
    not withheld above the height where it starts to.
    """
    floor = 3  # the selected box's top border plus two lines of its text
    minimum = SessionTreeModal.DETAIL_MIN_HEIGHT

    at_minimum = await _selection_rows(minimum)
    assert at_minimum >= floor, (
        f"at {minimum} rows the pane shows {at_minimum} rows of the selection, under {floor}"
    )

    below = await _selection_rows(minimum - 2)
    assert below < floor, (
        f"two rows shorter still shows {below}; the pane could be drawn below {minimum}"
    )


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------


async def test_the_pane_settles_on_the_last_node_after_rapid_movement() -> None:
    """A held arrow key posts highlights faster than the pane rebuilds. The pane
    must end on the row the cursor ended on, not on one it passed through."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        for _ in range(8):
            await pilot.press("up")
        for _ in range(4):
            await pilot.pause()
        assert tree.cursor_node is not None
        shown = _pane(app).selected_boxes[0].content_text
        expected = {"n1": "You are tau", "n2": "The streaming accumulator"}
        assert shown.startswith(expected[str(tree.cursor_node.data)])


async def test_the_pane_is_a_view_of_the_same_log_the_rows_are() -> None:
    """A resolver that cannot answer for a row is a broken index, and must raise
    rather than draw a plausible blank."""
    log = [
        {
            "id": "a",
            "parentId": None,
            "type": "message",
            "message": {"role": "user", "content": "hi"},
        }
    ]
    tree = ConversationTree(log, cursor="a")
    modal = SessionTreeModal(tree.tree(), resolve_entry=tree.entry)
    with pytest.raises(KeyError):
        modal._resolve_entry("no-such-entry")


async def test_the_pane_is_reachable_from_the_keyboard() -> None:
    """The boxes are real widgets with real collapsibles, which is only worth
    anything if focus can get to them."""
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, pilot):
        await pilot.press("tab")
        await pilot.pause()
        focused = app.screen.focused
        assert focused is not None
        assert _pane(app) in (focused, *focused.ancestors)


async def test_the_help_line_says_how() -> None:
    async with open_scene(get_scene("tree-modal"), WIDE) as (app, _pilot):
        assert "detail pane" in render_text(app)
