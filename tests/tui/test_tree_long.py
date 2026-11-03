"""The tree screen on a long session (D23 section 6, TUI follow-up 1): 30,000 nodes. A redraw
after a node arrives must not cost a rebuild of the rows, and a folded turn must have no widgets."""

from pathlib import Path
from statistics import median
from typing import Any

import pytest
from fakes import FAKE_CONFIG, FakeProvider
from harness import Harness, scenario
from textual.widgets import Tree
from tuikit import running, say, until

from tau import Author, Body, Part, Session, Stop

TURNS = 15_000
REDRAW = 0.1  # seconds the median redraw may take, from the event to the highlight in place
WHO = Author.Frontend("t")


@pytest.fixture(autouse=True)
def harness_mode() -> None:
    """One mode: the cost is the screen's, not the wire's (this replaces the suite's two)."""


def seed(tmp_path: Path) -> tuple[int, int]:
    """A session of `TURNS` turns in `tmp_path`, its cursor at the end; the middle node."""
    session = Session.create(FAKE_CONFIG, "sys", 0, tmp_path / "session")
    at = 1
    middle = 0
    for turn in range(TURNS):
        user = session.append(at, Body.User(f"question {turn}"), WHO, 0)
        at = session.append(
            user, Body.Assistant([Part.Text(f"answer {turn}")], Stop.End), Author.Provider(), 0
        )
        if turn == TURNS // 2:
            middle = at
    session.go_to(0, at, "seed", 0)
    session.close()
    return at, middle


@scenario
async def test_a_redraw_with_thirty_thousand_nodes_changes_rows_in_place(tmp_path: Path) -> None:
    tip, middle = seed(tmp_path)
    h = await Harness.load(tmp_path, provider=FakeProvider([say(f"more {i}") for i in range(8)]))
    assert len(h.nodes()) == 2 * TURNS + 2
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        tree = app.screen.query_one(Tree)
        await until(pilot, lambda: at_node(tree, tip))
        assert len(tree.root.children) == TURNS + 2, "a row per turn, not per node"
        assert sum(1 for n in tree.root.children if n.children) == 1, "only the open turn is built"
        side = await h.client.open(middle)  # a second cursor, in a turn far from the end
        await h.client.send(side, "from the middle")
        await until(pilot, lambda: len(h.nodes()) == 2 * TURNS + 4)
        side_at = (await h.view(side)).at
        spent: list[float] = []
        for i in range(6):
            await h.client.send(h.cursor, f"later {i}")
            end = 2 * TURNS + 4 + 2 * (i + 1) - 1
            await until(pilot, lambda: at_node(tree, end))  # noqa: B023
            spent.append(app.screen.redraw_seconds)  # pyright: ignore[reportAttributeAccessIssue]
        assert median(spent) < REDRAW, f"redraws took {spent}"
        await pilot.press("left_square_bracket")  # to the middle cursor, inside a stub
        await until(pilot, lambda: at_node(tree, side_at))
        assert tree.cursor_node is not None
        assert str(tree.cursor_node.label).startswith("assistant: ")
    await h.close()


def at_node(tree: Tree[Any], node_id: int) -> bool:
    node = tree.cursor_node
    return node is not None and node.data is not None and node.data.node.id == node_id
