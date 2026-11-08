"""The tree screen draws what prune and compaction did (D24 M1, D23 section 6): `⧉` on a node
computed from another, `≡ (copy of N)` on a copy, the old line as a stub with its size, the closed
summarizer cursor dimmed, `derived from` / `copied from` in the detail pane with the walk before and
after, and `p` to the source."""

import json
from pathlib import Path
from typing import Any

from fakes import FAKE_PROVIDER, FakeProvider, Reply
from harness import Harness, scenario
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Static, Tree
from tuikit import running, say, shows, type_line, until

from tau import Compaction, Part, Prune, Stop, model_config, read_models
from tau.tui import TauApp


def thoughtful(text: str) -> Reply:
    """A reply with a train of thought, which prune sheds."""
    return Reply([Part.Thinking("t" * 300), Part.Text(text)], Stop.End)


def billed(input: int, output: int, cached: int | None = None) -> str:
    """The usage a server reports: the third reply takes the walk past the hard limit of 36,000."""
    usage = {"input": input, "output": output}
    if cached is not None:
        usage["cached"] = cached
    return json.dumps({"usage": usage})


def rows(app: App[None]) -> list[str]:
    out: list[str] = []
    stack = list(reversed(app.screen.query_one(Tree).root.children))
    while stack:
        node = stack.pop()
        out.append(str(node.label))
        stack.extend(reversed(node.children))
    return out


def highlighted(app: App[None]) -> Any:
    node = app.screen.query_one(Tree).cursor_node
    assert node is not None
    assert node.data is not None
    return node.data


def detail(app: App[None]) -> str:
    return str(app.screen.query_one("#tree-detail-text", Static).render())


async def send(pilot: Pilot[None], app: TauApp, text: str, shown: str) -> None:
    await type_line(pilot, text)
    await pilot.press("enter")
    await shows(pilot, app, shown)


async def open_tree(pilot: Pilot[None], app: TauApp) -> None:
    await pilot.press("ctrl+g")
    await until(pilot, lambda: bool(app.screen.query(Tree)))
    await until(pilot, lambda: app.screen.query_one(Tree).cursor_node is not None)


@scenario
async def test_a_pruned_line_shows_its_marks_and_the_old_line_as_a_stub(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([thoughtful("first"), thoughtful("second"), thoughtful("third")]),
        extensions=[Prune()],
    )
    async with running(h, tmp_path) as (app, pilot):
        for text, shown in (("one", "first"), ("two", "second"), ("three", "third")):
            await send(pilot, app, text, shown)
        await h.run("prune")
        await h.settle()
        await open_tree(pilot, app)
        pruned = h.session.cursor(h.cursor).at
        await until(pilot, lambda: highlighted(app).node.id == pruned)
        listed = rows(app)
        assert any(r.startswith("user: <pruned>") and "⧉" in r for r in listed), listed
        assert any("≡ (copy of" in r for r in listed), listed
        assert any(r.startswith("├┄ ") and "nodes" in r for r in listed), "the old line is a stub"
    await h.close()


@scenario
async def test_the_detail_pane_says_where_a_node_came_from_and_p_goes_there(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([thoughtful("first"), thoughtful("second"), thoughtful("third")]),
        extensions=[Prune()],
    )
    async with running(h, tmp_path) as (app, pilot):
        for text, shown in (("one", "first"), ("two", "second"), ("three", "third")):
            await send(pilot, app, text, shown)
        await h.run("prune")
        await h.settle()
        await open_tree(pilot, app)
        tip = h.session.cursor(h.cursor).at
        await until(pilot, lambda: highlighted(app).node.id == tip)
        source = h.session.tree.node(tip).derived_from
        assert source is not None
        text = detail(app)
        assert f"derived from node {source}\nwalk before: 8 nodes, ≤1.0k tokens\n" in text
        assert "walk after: 6 nodes, ≤149 tokens\n" in text
        await pilot.press("p")
        await until(pilot, lambda: highlighted(app).node.id == source)
        assert f"node {source}  ·  by" in detail(app)
        await pilot.press("p")  # the original has no source
        await pilot.pause(0.1)
        assert highlighted(app).node.id == source
    await h.close()


@scenario
async def test_a_compacted_tree_shows_the_summary_the_copies_and_the_closed_summarizer(
    tmp_path: Path,
) -> None:
    catalog = read_models(
        cli={
            "models": {
                "a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 40_000},
            },
            "extensions": {"compaction": {"reserve": 4000, "soft": 35_000, "keep": 20}},
        }
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                Reply([Part.Text("one" + "." * 600)], Stop.End, meta=billed(10, 160)),
                Reply([Part.Text("two" + "." * 600)], Stop.End, meta=billed(180, 160, 10)),
                Reply([Part.Text("r" * 100)], Stop.End, meta=billed(350, 36_000, 180)),
                say("the summary"),
                say("four"),
            ]
        ),
        extensions=[Compaction()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path) as (app, pilot):
        for text, shown in (("go 1", "one"), ("go 2", "two"), ("go 3", "rrrr"), ("go 4", "four")):
            await send(pilot, app, text, shown)
        await open_tree(pilot, app)
        tip = h.session.cursor(h.cursor).at
        await until(pilot, lambda: highlighted(app).node.id == tip)
        listed = rows(app)
        assert any("user: The earlier part of this conversation" in r and "⧉" in r for r in listed)
        # the input the summary left out of itself is copied, and shows as its own turn
        assert sum(r.startswith("user: go 4") for r in listed) == 1, listed
        assert any("○ 1 compaction:1" in r for r in listed), listed
        assert any(r.startswith("├┄ ") for r in listed), "the old line is a stub"
    await h.close()
