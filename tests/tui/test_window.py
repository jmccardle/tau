"""The render window (D23 section 7): a long walk draws its last turns, and the reader slides the
window along the walk by scrolling against an edge. Presentation only: the walk, the session and
what a send does are the same."""

from pathlib import Path

from fakes import FakeProvider
from harness import Harness, scenario
from tuikit import running, say, shows, type_line, until

from tau import Body


async def long_walk(tmp_path: Path, turns: int) -> Harness:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say(f"re {i}") for i in range(1, turns + 2)])
    )
    for i in range(1, turns + 1):
        await h.send(f"turn {i}")
        await h.settle()
    return h


def drawn(text: str, turn: int) -> bool:
    return f"] turn {turn}\n" in text + "\n"


@scenario
async def test_a_long_walk_draws_its_last_four_turns_and_counts_the_rest(tmp_path: Path) -> None:
    h = await long_walk(tmp_path, 7)
    async with running(h, tmp_path) as (app, _pilot):
        text = app.screen_text()
        assert [drawn(text, t) for t in range(1, 8)] == [False] * 3 + [True] * 4
        assert "⋯ 8 earlier nodes" in text
        assert "turn 1" in text.splitlines()[0], "the header's title is the walk's first message"
        assert "⋯" not in text.split("] turn 7")[1], "nothing is hidden below"
    await h.close()


@scenario
async def test_a_short_walk_draws_whole(tmp_path: Path) -> None:
    h = await long_walk(tmp_path, 4)
    async with running(h, tmp_path) as (app, _pilot):
        assert "earlier" not in app.screen_text()
        assert drawn(app.screen_text(), 1)
    await h.close()


@scenario
async def test_scrolling_against_the_top_slides_the_window_back_one_turn(tmp_path: Path) -> None:
    h = await long_walk(tmp_path, 7)
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+up")  # a page up, or at the top of the page the window slides
        await pilot.press("ctrl+up")
        await pilot.press("ctrl+up")
        await until(pilot, lambda: drawn(app.screen_text(), 3))
        text = app.screen_text()
        assert [drawn(text, t) for t in range(1, 8)] == [
            False,
            False,
            True,
            True,
            True,
            True,
            False,
        ]
        assert "⋯ 6 earlier nodes" in text
        assert "⋯ 2 later nodes" in text
        await pilot.press("ctrl+down")  # at the bottom of the page the window slides forward
        await pilot.press("ctrl+down")
        await pilot.press("ctrl+down")
        await until(pilot, lambda: drawn(app.screen_text(), 7))
        assert "later nodes" not in app.screen_text()
    await h.close()


@scenario
async def test_the_walk_is_unchanged_by_sliding(tmp_path: Path) -> None:
    h = await long_walk(tmp_path, 6)
    before = h.nodes()
    async with running(h, tmp_path) as (app, pilot):
        for _ in range(3):
            await pilot.press("ctrl+up")
        await until(pilot, lambda: drawn(app.screen_text(), 2))
    assert h.nodes() == before
    await h.close()


@scenario
async def test_a_message_sent_while_slid_back_brings_the_window_to_the_tail(
    tmp_path: Path,
) -> None:
    h = await long_walk(tmp_path, 7)
    async with running(h, tmp_path) as (app, pilot):
        for _ in range(3):
            await pilot.press("ctrl+up")
        await until(pilot, lambda: drawn(app.screen_text(), 3))
        await type_line(pilot, "and now")
        await pilot.press("enter")
        await shows(pilot, app, "[User] and now")
        await shows(pilot, app, "[Assistant] re 8")
        text = app.screen_text()
        assert "later nodes" not in text
        assert not drawn(text, 3), "the window is the last four turns again"
    await h.close()


@scenario
async def test_the_window_trims_its_head_as_turns_arrive(tmp_path: Path) -> None:
    h = await long_walk(tmp_path, 4)
    async with running(h, tmp_path) as (app, pilot):
        assert drawn(app.screen_text(), 1)
        assert "earlier" not in app.screen_text()
        await type_line(pilot, "turn 5")
        await pilot.press("enter")
        await shows(pilot, app, "[Assistant] re 5")
        await until(pilot, lambda: "⋯ 4 earlier nodes" in app.screen_text())
        text = app.screen_text()
        assert not drawn(text, 1)
        assert drawn(text, 2)
        assert drawn(text, 5)
        users = [n.body.text for n in h.nodes() if isinstance(n.body, Body.User)]
        assert users == [f"turn {i}" for i in range(1, 6)], "nothing left the session"
    await h.close()
