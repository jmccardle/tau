"""A handful of fixed scenes, kept as SVG (D23 section 9, ruling 5): the empty session, a reply
with thinking and a tool, a question, the tree, the session picker, the `/model` picker and an
options form. They catch what the pilot tests do not read: layout, colour, borders.

Every scene is deterministic: a fixed session id, working directory and size, the fake model, no
animation, no blinking cursor, ages in the middle of their unit. A change that moves a pixel
fails here, with the rows of text that changed. Look at the SVGs before keeping the new ones:
`TAU_UPDATE_SNAPSHOTS=1 pytest tests/tui/test_snapshots.py` rewrites them, and `git diff` shows
them next to the old. They are Textual's drawing, so textual and rich are pinned in the dev
group. Run once (local mode): the rest of the suite holds the wire to the same app.
"""

from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from textual.pilot import Pilot
from textual.widgets import DataTable, OptionList, Tree
from tuikit import Gatekeeper, Tuner, check_screen, running, say, shows, type_line, until
from world import World

from tau import PROTOCOL, Compaction, Part, Stop, UserConfig, Welcome, model_config, read_models
from tau.tui import SessionPicker, TauApp

WELCOME = Welcome(PROTOCOL, "0.16.0", "20261102-120000-abc123", "/work/project", 1)
SMALL = (100, 30)


@pytest.fixture(autouse=True)
def harness_mode() -> None:
    """One mode: these are pictures of the app, not of the wire (this replaces the suite's)."""


async def picture(pilot: Pilot[None], app: TauApp, name: str) -> None:
    """Compare the screen with the SVG kept for this test."""
    app.animation_level = "none"
    for editor in app.screen.query("TextArea, Input"):
        editor.cursor_blink = False  # pyright: ignore[reportAttributeAccessIssue]
    shot = app.export_screenshot(title="tau")
    for _ in range(40):  # layout and the tail-following scroll settle over a few ticks
        await pilot.pause(0.05)
        if (again := app.export_screenshot(title="tau")) == shot:
            break
        shot = again
    else:
        raise AssertionError("the screen never settled")
    check_screen(app, name)


async def start(tmp_path: Path, **kwargs: object) -> Harness:
    return await Harness.create(tmp_path, **kwargs)  # pyright: ignore[reportArgumentType]


@scenario
async def test_an_empty_session(tmp_path: Path) -> None:
    h = await start(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await picture(pilot, app, "an_empty_session")
    await h.close()


@scenario
async def test_a_reply_with_thinking_and_a_tool(
    tmp_path: Path,
) -> None:
    calling = Reply(
        [
            Part.Thinking("The notes file will say."),
            Part.Text("Let me read it."),
            Part.Call("c1", "read", '{"path": "notes.txt"}'),
        ],
        Stop.Tools,
    )
    h = await start(
        tmp_path,
        provider=FakeProvider([calling, say("It says **hello** from the notes.")]),
        tools=[FakeTool("read", "hello from the notes")],
    )
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await type_line(pilot, "what do my notes say?")
        await pilot.press("enter")
        await shows(pilot, app, "It says **hello** from the notes.")
        await picture(pilot, app, "a_reply_with_thinking_and_a_tool")
    await h.close()


@scenario
async def test_a_question_waiting_for_an_answer(
    tmp_path: Path,
) -> None:
    calling = Reply([Part.Call("c1", "read", "{}")], Stop.Tools)
    h = await start(
        tmp_path,
        provider=FakeProvider([calling, say("done")]),
        tools=[FakeTool("read", "ok")],
        extensions=[Gatekeeper()],
    )
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await type_line(pilot, "read it")
        await pilot.press("enter")
        await shows(pilot, app, "run read?")
        await picture(pilot, app, "a_question_waiting_for_an_answer")
    await h.close()


@scenario
async def test_the_tree(tmp_path: Path) -> None:
    h = await start(tmp_path, provider=FakeProvider([say("first answer"), say("second answer")]))
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        for line, answer in (("one", "first answer"), ("two", "second answer")):
            await type_line(pilot, line)
            await pilot.press("enter")
            await shows(pilot, app, answer)
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query("#tree-browser-tree")))
        await picture(pilot, app, "the_tree")
    await h.close()


@scenario
async def test_the_tree_with_a_branch_and_a_closed_cursor(tmp_path: Path) -> None:
    h = await start(tmp_path, provider=FakeProvider([say("first answer"), say("side answer")]))
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first answer")
        scout = await h.client.open(1)
        await h.client.set_label(scout, "scout")
        await h.client.send(scout, "a side question")
        await until(pilot, lambda: len(h.nodes()) == 6)
        gone = await h.client.open(1)
        await h.client.set_label(gone, "compaction:1")
        await h.client.close_cursor(gone)
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query("#tree-browser-tree")))
        await until(
            pilot,
            lambda: "compaction:1" in str(app.screen.query_one(Tree).root.children[1].label),
        )
        await picture(pilot, app, "the_tree_with_a_branch_and_a_closed_cursor")
    await h.close()


@scenario
async def test_the_tree_of_a_compacted_session(tmp_path: Path) -> None:
    usage = '{"usage": {"input": 895, "output": 5}}'
    catalog = read_models(
        cli={
            "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 1000}},
            "extensions": {"compaction": {"reserve": 100, "soft": 900, "keep": 20}},
        }
    )
    replies = [
        Reply([Part.Text("one" + "." * 600)], Stop.End),
        Reply([Part.Text("two" + "." * 600)], Stop.End),
        Reply([Part.Text("r" * 100)], Stop.End, meta=usage),
        say("the summary"),
        say("four"),
    ]
    h = await start(
        tmp_path,
        provider=FakeProvider(replies),
        extensions=[Compaction()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        for line, answer in (("go 1", "one"), ("go 2", "two"), ("go 3", "rrrr"), ("go 4", "four")):
            await type_line(pilot, line)
            await pilot.press("enter")
            await shows(pilot, app, answer)
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query("#tree-browser-tree")))
        await until(
            pilot,
            lambda: any(
                "compaction:1" in str(n.label) for n in app.screen.query_one(Tree).root.children
            ),
        )
        await picture(pilot, app, "the_tree_of_a_compacted_session")
    await h.close()


@scenario
async def test_the_session_picker(
    tmp_path: Path,
    world: World,
) -> None:
    here = tmp_path
    world.session("20261101-100000-aaaaaa", cwd=here, says=["what is a monad"], age=3.5 * 86400)
    world.session("20261101-110000-bbbbbb", cwd=here, name="the named one", age=5.5 * 3600)
    world.session("20261101-120000-cccccc", cwd=here, says=["fix the build"], age=90)
    h = await start(tmp_path, provider=FakeProvider([]))
    sessions = world.tau / "sessions"
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await until(pilot, lambda: app.screen.query_one(DataTable).row_count == 3)
        await picture(pilot, app, "the_session_picker")
    await h.close()


@scenario
async def test_the_model_picker(tmp_path: Path) -> None:
    spec = {"provider": FAKE_PROVIDER}
    catalog = read_models(
        cli={
            "models": {
                "fake": {"model": "fake", **spec},
                "spare": {"model": "spare", **spec},
                "scout": {"model": "scout", **spec},
            }
        }
    )
    h = await start(tmp_path, provider=FakeProvider([]), catalog=catalog)
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await type_line(pilot, "/model")
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#pick-dialog")))
        await until(pilot, lambda: app.screen.query_one("#pick-list", OptionList).option_count == 3)
        await picture(pilot, app, "the_model_picker")
    await h.close()


@scenario
async def test_an_options_form(tmp_path: Path) -> None:
    h = await start(
        tmp_path,
        provider=FakeProvider([]),
        extensions=[Tuner()],
        user_config=UserConfig(tmp_path / "tau-home"),
    )
    async with running(h, tmp_path, welcome=WELCOME, size=SMALL) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#options-dialog")))
        await picture(pilot, app, "an_options_form")
    await h.close()
