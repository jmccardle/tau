"""Typing a message, and what the transcript shows of the walk (D23 section 9)."""

import json
from pathlib import Path

from fakes import FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario
from textual.widgets import Collapsible
from tuikit import running, say, shows, type_line, until

from tau import (
    Author,
    Body,
    Compaction,
    Part,
    Stop,
    TextDelta,
    ThinkingDelta,
    model_config,
    read_models,
)


@scenario
async def test_enter_sends_the_draft_and_shows_the_reply(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("hello back")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "hi there")
        await pilot.press("enter")
        await shows(pilot, app, "hello back")
        users = [n for n in h.nodes() if isinstance(n.body, Body.User)]
        assert [n.body.text for n in users if isinstance(n.body, Body.User)] == ["hi there"]
        assert users[0].by == Author.Frontend("me:tui")
        assert "[User] hi there" in app.screen_text()
        assert "hi there" in app.screen_text().splitlines()[0], "the title is the first message"
    await h.close()


@scenario
async def test_the_sent_draft_leaves_the_editor_and_ctrl_j_starts_a_line(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("ctrl+j")
        await type_line(pilot, "two")
        await pilot.press("enter")
        await shows(pilot, app, "ok")
        assert app.draft == ""
        first = next(n for n in h.nodes() if isinstance(n.body, Body.User))
        assert first.body == Body.User("one\ntwo")
    await h.close()


@scenario
async def test_the_live_reply_is_drawn_until_its_node_arrives(tmp_path: Path) -> None:
    gate = Gate()
    reply = Reply(
        [
            ThinkingDelta("pondering"),
            Part.Thinking("pondering"),
            TextDelta("par"),
            TextDelta("tial answer"),
            Part.Text("partial answer"),
        ],
        gate=gate,
        gate_after=3,
    )
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "[Assistant, live] par")
        assert "[Assistant, live] partial answer" not in app.screen_text()
        gate.release()
        await shows(pilot, app, "[Assistant] partial answer")
        assert "[Assistant, live]" not in app.screen_text()
    await h.close()


@scenario
async def test_thinking_and_tool_calls_are_boxes_with_their_outcome(tmp_path: Path) -> None:
    tool = FakeTool("lookup", "42 results")
    script = [
        Reply(
            [
                Part.Thinking("let me look"),
                Part.Text("checking"),
                Part.Call("c1", "lookup", '{"q": "x"}'),
            ],
            Stop.Tools,
        ),
        say("found it"),
    ]
    h = await Harness.create(tmp_path, provider=FakeProvider(script), tools=[tool])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "search")
        await pilot.press("enter")
        await shows(pilot, app, "found it")
        text = app.screen_text()
        assert "[Assistant] checking" in text
        assert "✓ lookup(q=x): 42 results" in text
        assert [t.title for t in app.query(Collapsible).filter(".tool-box")] == ["✓ lookup(q=x)"]
        assert [t.title for t in app.query(Collapsible).filter(".reasoning-region")] == ["Thought"]
    await h.close()


@scenario
async def test_a_failed_sample_is_an_error_box(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("the server is down")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "hi")
        await pilot.press("enter")
        await shows(pilot, app, "[Error]")
        assert "the server is down" in app.screen_text()
    await h.close()


@scenario
async def test_another_clients_message_appears_with_its_author(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("noted")]))
    other = await h.connect("me:other")
    async with running(h, tmp_path) as (app, pilot):
        await other.send(h.cursor, "from afar")
        await shows(pilot, app, "[me:other] from afar")
        await shows(pilot, app, "noted")
    await other.close()
    await h.close()


@scenario
async def test_a_branch_by_another_client_reads_the_walk_again(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    other = await h.connect("me:other")
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        root = next(n for n in h.nodes() if isinstance(n.body, Body.System))
        await other.go_to(h.cursor, root.id)
        await until(pilot, lambda: "[User] one" not in app.screen_text())
        assert "first" not in app.screen_text()
        await other.send(h.cursor, "two")
        await shows(pilot, app, "second")
        assert "[me:other] two" in app.screen_text()
    await other.close()
    await h.close()


@scenario
async def test_ctrl_r_and_ctrl_t_fold_the_thinking_and_the_tool_boxes(tmp_path: Path) -> None:
    tool = FakeTool("lookup", "found")
    script = [
        Reply([Part.Thinking("hm"), Part.Call("c1", "lookup", "{}")], Stop.Tools),
        say("done"),
    ]
    h = await Harness.create(tmp_path, provider=FakeProvider(script), tools=[tool])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "done")
        thinking = app.query(Collapsible).filter(".reasoning-region")
        tools = app.query(Collapsible).filter(".tool-box")
        assert [b.collapsed for b in thinking] == [True]
        await pilot.press("ctrl+r")
        await until(pilot, lambda: [b.collapsed for b in thinking] == [False])
        assert [b.collapsed for b in tools] == [True], "tool boxes are folded on their own key"
        await pilot.press("ctrl+t")
        await until(pilot, lambda: [b.collapsed for b in tools] == [False])
    await h.close()


@scenario
async def test_a_tool_call_line_shows_square_brackets_as_typed(tmp_path: Path) -> None:
    tool = FakeTool("bash", "done")
    script = [Reply([Part.Call("c1", "bash", '{"command": "echo [draft] [/b]"}')], Stop.Tools)]
    h = await Harness.create(tmp_path, provider=FakeProvider([*script, say("ok")]), tools=[tool])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "run it")
        await pilot.press("enter")
        await shows(pilot, app, "ok")
        (box,) = app.query(Collapsible).filter(".tool-box")
        (title,) = box.query("CollapsibleTitle")
        assert "bash(command=echo [draft] [/b])" in title.label.plain  # type: ignore[attr-defined]
    await h.close()


@scenario
async def test_the_title_after_a_compaction_is_not_the_summary(tmp_path: Path) -> None:
    window = {"model": "a", "provider": FAKE_PROVIDER, "context_window": 40_000}
    options = {"reserve": 2000, "soft": 10_000, "keep": 1000}
    catalog = read_models(cli={"models": {"a": window}, "extensions": {"compaction": options}})

    def billed(input: int, cached: int | None = None) -> str:
        usage: dict[str, int] = {"input": input, "output": 6000}
        if cached is not None:
            usage["cached"] = cached
        return json.dumps({"usage": usage})

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                Reply([Part.Text("first")], Stop.End, meta=billed(100)),
                Reply([Part.Text("second")], Stop.End, meta=billed(6200, 100)),
                say("the summary of everything"),
            ]
        ),
        extensions=[Compaction()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "question one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await type_line(pilot, "question two")
        await pilot.press("enter")
        await shows(pilot, app, "second")
        await until(pilot, lambda: bool([n for n in h.notices() if n.startswith("compacted")]))
        await until(pilot, lambda: "summary of everything" in app.screen_text())
        title = app.screen_text().splitlines()[0]
        assert "summary of everything" not in title
        assert "question" in title
    await h.close()
