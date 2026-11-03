"""The tree screen (D23 section 6, TUI follow-up 1): the viewed cursor's walk straight down, every
other branch one folded stub, closed cursors dimmed, and the highlight following the viewed
cursor."""

from contextlib import suppress
from pathlib import Path
from typing import Any

from fakes import FakeProvider, Gate, Reply
from harness import Harness, scenario
from rich.text import Text
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Static, Tree
from tuikit import SIZE, running, say, shows, type_line, until

from tau import Part, Progress
from tau.tui import TauApp

SYS = 1  # the system node: a new session is Config 0, System 1


def rows(app: App[None]) -> list[str]:
    """The rows the tree screen has built, in order: a folded row's content has no widgets."""
    out: list[str] = []
    stack = list(reversed(app.screen.query_one(Tree).root.children))
    while stack:
        node = stack.pop()
        out.append(str(node.label))
        stack.extend(reversed(node.children))
    return out


def highlighted(app: App[None]) -> Any:
    """The row under the highlight: its `node`, its `kind` (`node`, `turn` or `stub`)."""
    node = app.screen.query_one(Tree).cursor_node
    assert node is not None
    assert node.data is not None
    return node.data


def title(app: App[None]) -> str:
    return str(app.screen.query_one(".tau-dialog-title", Static).render())


async def send(pilot: Pilot[None], app: TauApp, text: str, shown: str) -> None:
    await type_line(pilot, text)
    await pilot.press("enter")
    await shows(pilot, app, shown)


async def open_tree(pilot: Pilot[None], app: TauApp) -> None:
    await pilot.press("ctrl+g")
    await until(pilot, lambda: bool(app.screen.query(Tree)))
    await until(pilot, lambda: app.screen.query_one(Tree).cursor_node is not None)


@scenario
async def test_a_reply_moves_the_highlight_while_the_tree_follows(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        assert title(app).endswith("following")
        await h.client.send(h.cursor, "two")
        await until(pilot, lambda: highlighted(app).node.id == 5)
        assert rows(app)[-1].startswith("assistant: second  ◀ [0]")
    await h.close()


@scenario
async def test_moving_by_hand_pauses_following_and_f_resumes(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        await pilot.press("up")
        await until(pilot, lambda: highlighted(app).node.id == 2)
        assert "paused" in title(app)
        await h.client.send(h.cursor, "two")
        await until(pilot, lambda: any(r.startswith("assistant: second") for r in rows(app)))
        assert highlighted(app).node.id == 2, "the highlight stayed where the person put it"
        await pilot.press("f")
        await until(pilot, lambda: highlighted(app).node.id == 5)
        assert title(app).endswith("following")
    await h.close()


@scenario
async def test_a_second_clients_branch_is_a_stub_with_its_cursor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(SYS)
        await h.client.set_label(scout, "scout")
        await h.client.send(scout, "side")
        await until(pilot, lambda: len(h.nodes()) == 6)
        await open_tree(pilot, app)
        await until(pilot, lambda: any("scout" in r for r in rows(app)))
        listed = rows(app)
        assert listed[2] == "├┄ 2 nodes, 1 turn  ◀ 1 scout", "hung off the system node"
        assert listed[3].startswith("user: one")
        assert listed[-1].startswith("assistant: first")
        assert "second" not in "".join(listed), "a folded stub builds none of its rows"
        await pilot.press("up", "up")
        await until(pilot, lambda: highlighted(app).kind == "stub")
        await pilot.press("right")
        await until(pilot, lambda: any(r.startswith("assistant: second") for r in rows(app)))
        assert [r for r in rows(app) if r.startswith("user")] == ["user: side  +1", "user: one  +1"]
    await h.close()


@scenario
async def test_v_views_the_cursor_under_the_highlight_and_redraws_its_line_as_the_spine(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(SYS)
        await h.client.send(scout, "side")
        await until(pilot, lambda: len(h.nodes()) == 6)
        await open_tree(pilot, app)
        await until(pilot, lambda: any(r.startswith("├┄") for r in rows(app)))
        await pilot.press("v")
        await pilot.pause(0.2)
        assert app.cursor == h.cursor, "a folded branch with no highlight on it: nothing happens"
        await pilot.press("up", "up")
        await until(pilot, lambda: highlighted(app).kind == "stub")
        await pilot.press("v")
        await until(pilot, lambda: app.cursor == scout)
        await until(pilot, lambda: rows(app)[-1].startswith("assistant: second"))
        listed = rows(app)
        assert listed[2].startswith("├┄ 2 nodes, 1 turn  ◀ 0"), "cursor 0's branch is the stub now"
        assert listed[-1] == "assistant: second  ◀ [1]"
        assert title(app).endswith("following")
        before = (await h.view()).at
        await pilot.press("up", "enter")  # the choice is made on the cursor now viewed
        await until(pilot, lambda: not app.screen.query(Tree))
        assert (await h.view(scout)).at == 4
        assert (await h.view()).at == before
    await h.close()


@scenario
async def test_brackets_jump_between_cursor_marks_and_unfold_what_hides_them(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(SYS)
        await h.client.send(scout, "side")
        await until(pilot, lambda: len(h.nodes()) == 6)
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        await pilot.press("right_square_bracket")
        await until(pilot, lambda: highlighted(app).node.id == 5)
        assert any(r.startswith("assistant: second") for r in rows(app)), "its stub unfolded"
        assert "paused" in title(app)
        await pilot.press("left_square_bracket")
        await until(pilot, lambda: highlighted(app).node.id == 3)
    await h.close()


@scenario
async def test_enter_on_a_busy_cursor_asks_before_aborting_its_step(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("never")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await open_tree(pilot, app)
        await pilot.press("up")  # from the user message the step is working on, to the system node
        await until(pilot, lambda: highlighted(app).node.id == SYS)
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#confirm-yes")))
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.query("#confirm-yes"))
        assert (await h.view()).busy, "the answer was no: the step goes on"
        gate.release()
    await h.close()


@scenario
async def test_a_closed_cursor_stays_in_the_tree_dimmed(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(SYS)
        await h.client.set_label(scout, "compaction:1")
        await h.client.close_cursor(scout)
        await open_tree(pilot, app)
        await until(pilot, lambda: any("○ 1 compaction:1" in r for r in rows(app)))
        tree = app.screen.query_one(Tree)
        node = next(n for n in tree.root.children if "○" in str(n.label))
        assert str(node.label) == "system: system prompt  ○ 1 compaction:1"
        assert node is not tree.cursor_node
        line = tree.render_label(node, tree.rich_style, tree.rich_style)
        assert isinstance(line, Text)
        dimmed = [s for s in line.spans if getattr(s.style, "dim", False)]
        assert dimmed, "the closed cursor's mark is styled dim"
        assert line.plain[dimmed[-1].start :].strip().startswith("○ 1")
    await h.close()


@scenario
async def test_the_detail_pane_says_how_long_the_viewed_walk_is(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("x" * 400)]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "xxxx")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        detail = app.screen.query_one("#tree-detail-text", Static)
        await until(pilot, lambda: "a walk of 4 nodes" in str(detail.render()))
        assert "about 101 tokens" in str(detail.render()), (
            "(3 + 3 + 400) characters over four, rounded down"
        )


@scenario
async def test_a_branch_made_by_editing_is_a_stub_and_older_turns_stay_folded(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a"), say("b"), say("c")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "a")
        await send(pilot, app, "two", "b")
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft == "two")
        await send(pilot, app, "three", "c")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 7)
        listed = rows(app)
        assert "├┄ 2 nodes, 1 turn" in listed, "the turn that was edited away hangs as a stub"
        assert "user: one  +1" in listed
        assert "assistant: a" not in listed, "a folded turn is not built"
        assert [r for r in listed if r.startswith("assistant")] == ["assistant: c  ◀ [0]"]
    await h.close()


@scenario
async def test_a_folded_turn_lists_the_cursors_inside_it_and_v_works_on_it(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(3)  # at the answer, inside the first turn
        await send(pilot, app, "two", "second")
        await open_tree(pilot, app)
        await until(pilot, lambda: any("assistant: first  ◀ 1" in r for r in rows(app)))
        assert "user: one  +1" in rows(app), "an open turn shows only what is at its own node"
        await pilot.press("home", "down", "down", "left")  # onto the first turn, and fold it
        await until(pilot, lambda: "user: one  +1  ◀ 1" in rows(app))
        await pilot.press("v")
        await until(pilot, lambda: app.cursor == scout)
    await h.close()


@scenario
async def test_the_line_of_a_stub_goes_through_a_cursor_else_the_longest(tmp_path: Path) -> None:
    replies = [say(f"r{i}") for i in range(1, 6)]
    h = await Harness.create(tmp_path, provider=FakeProvider(replies))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "main", "r1")  # cursor 0: nodes 2 and 3
        short = await h.client.open(SYS)  # a branch of the system node: x, then y or z
        await h.client.send(short, "x")
        await until(pilot, lambda: len(h.nodes()) == 6)
        longer = await h.client.open(5)  # where x was answered: z, w, a longer line
        await h.client.send(longer, "z")
        await until(pilot, lambda: len(h.nodes()) == 8)
        await h.client.send(longer, "w")
        await until(pilot, lambda: len(h.nodes()) == 10)
        await h.client.go_to(short, 5)
        await h.client.send(short, "y")  # r5: the shorter line, with cursor `short` on it
        await until(pilot, lambda: len(h.nodes()) == 12)
        await open_tree(pilot, app)
        await pilot.press("up", "up")
        await until(pilot, lambda: highlighted(app).kind == "stub")
        await pilot.press("right")

        def turns() -> list[str]:
            return [r for r in rows(app) if r.startswith(("user", "├┄"))]

        await until(pilot, lambda: len(turns()) > 2)
        line = [r.split("  ")[0] for r in turns()[1:]]
        assert line == ["user: x", "├┄ 4 nodes, 2 turns", "user: y", "user: main"], (
            "the line goes through y, where cursor 1 is; the longer line of z and w is the stub"
        )
        await h.client.close_cursor(short)
        await h.client.close_cursor(longer)
        await until(pilot, lambda: not any("◀ 1" in r or "◀ 2" in r for r in rows(app)))
        await until(pilot, lambda: "user: z" in "".join(rows(app)))
        line = [r.split("  ")[0] for r in turns()[1:]]
        assert line == ["user: x", "├┄ 2 nodes, 1 turn", "user: z", "user: w", "user: main"], (
            "with no open cursor below, the longest line goes straight"
        )
    await h.close()


@scenario
async def test_the_highlight_stays_on_an_unfolded_stub_and_a_jump_survives_a_redraw(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a"), say("b"), say("c")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "a")
        scout = await h.client.open(SYS)
        await h.client.send(scout, "side")
        await until(pilot, lambda: len(h.nodes()) == 6)
        await open_tree(pilot, app)
        await pilot.press("up", "up")
        await until(pilot, lambda: highlighted(app).kind == "stub")
        await pilot.press("right")
        await until(pilot, lambda: any(r.startswith("assistant: b") for r in rows(app)))
        await h.client.send(h.cursor, "two")  # a redraw, paused
        await until(pilot, lambda: any(r.startswith("assistant: c") for r in rows(app)))
        assert highlighted(app).kind == "stub", "not moved to the stub's first child"
        await pilot.press("right_square_bracket")
        await h.client.set_label(h.cursor, "main")  # a redraw straight after the jump
        await until(pilot, lambda: any("main" in r for r in rows(app)))
        await pilot.pause(0.3)
        assert highlighted(app).node.id == (await h.view(scout)).at, "the jump was not undone"
    await h.close()


@scenario
async def test_when_the_viewed_cursor_closes_the_tree_follows_the_apps(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "first")
        scout = await h.client.open(SYS)
        await h.set_auto(False, scout)
        await shows(pilot, app, "[0] 1 ")
        await pilot.press("alt+right")
        await shows(pilot, app, " 0 [1]")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == SYS)
        await h.client.close_cursor(scout)
        await until(pilot, lambda: app.cursor == h.cursor)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        assert title(app).endswith("following")
        await pilot.press("up", "enter")  # goes to the user message, on cursor 0
        await until(pilot, lambda: not app.screen.query(Tree))
        assert (await h.view()).at == 2
    await h.close()


def flood(progress: Progress) -> str:
    """Say a lot at once: a client that reads two events behind is cut off."""
    for i in range(20):
        progress(f"line {i}")
    return "flooded"


class Floody:
    name = "floody"
    commands = (flood,)


@scenario
async def test_after_a_reconnect_the_tree_reads_the_new_session_view(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("first"), say("second")]), extensions=[Floody()]
    )
    slow = await h.connect("me:tui", buffer=2)
    app = TauApp(
        slow,
        h.cursor,
        welcome=None,
        home=tmp_path / "home",
        cwd=tmp_path,
        reconnect=lambda: h.connect("me:tui"),
    )
    async with app.run_test(size=SIZE) as pilot:
        await shows(pilot, app, "idle")
        await send(pilot, app, "one", "first")
        await open_tree(pilot, app)
        await until(pilot, lambda: highlighted(app).node.id == 3)
        with suppress(ConnectionError):  # the flood is the app's own client's, and cuts it off
            await app.client.run(h.cursor, "flood", {})
        await until(pilot, lambda: app.client is not slow)
        await h.client.send(h.cursor, "two")
        await until(pilot, lambda: any(r.startswith("assistant: second") for r in rows(app)))
        await until(pilot, lambda: highlighted(app).node.id == 5)
    await h.close()


@scenario
async def test_an_only_child_below_the_viewed_node_is_a_stub(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a"), say("b")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "a")
        await send(pilot, app, "two", "b")
        await h.client.go_to(h.cursor, 3)
        await pilot.pause(0.3)
        await open_tree(pilot, app)
        assert any(row.startswith("├┄") for row in rows(app)), rows(app)
    await h.close()


@scenario
async def test_another_clients_branch_from_the_viewed_node_shows_and_is_reached(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a"), say("b")]))
    async with running(h, tmp_path) as (app, pilot):
        await send(pilot, app, "one", "a")
        other = await h.client.open(3)
        await h.client.set_label(other, "scout")
        await h.client.send(other, "side")
        await until(pilot, lambda: len(h.nodes()) == 6)
        await open_tree(pilot, app)
        assert any("scout" in row for row in rows(app)), rows(app)
        await pilot.press("]")
        await until(pilot, lambda: "scout" in str(app.screen.query_one(Tree).cursor_node.label))  # type: ignore[union-attr]
    await h.close()
