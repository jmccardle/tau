"""The render window while the reader has slid back (D23 section 7): nothing that arrives may
break the page, draw out of order, or land under the row that says it is not drawn."""

import asyncio
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario
from textual.pilot import Pilot
from tuikit import SIZE, running, say, shows, type_line, until

from tau import Part, Progress, Stop, TextDelta
from tau.tui import TauApp

SYS = 1  # the system prompt's node: a cursor opened here has an empty walk


async def turns(tmp_path: Path, count: int, *extra: Reply, **kwargs: object) -> Harness:
    script = [say(f"re {i}") for i in range(1, count + 1)]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([*script, *extra]),
        **kwargs,  # pyright: ignore[reportArgumentType]
    )
    for i in range(1, count + 1):
        await h.send(f"turn {i}")
        await h.settle()
    return h


async def slide_back(pilot: Pilot[None], app: TauApp) -> None:
    for _ in range(4):
        await pilot.press("ctrl+up")
    await until(pilot, lambda: "later nodes" in app.screen_text())


def later(text: str) -> int:
    row = next(line for line in text.splitlines() if "later nodes" in line)
    return int(row.split()[1])


@scenario
async def test_tool_progress_for_a_call_outside_the_window_is_ignored(tmp_path: Path) -> None:
    gate = Gate()
    tool = FakeTool("lookup", "done", gate=gate)
    h = await turns(
        tmp_path,
        7,
        Reply([Part.Text("checking"), Part.Call("c1", "lookup", "{}")], Stop.Tools),
        say("found"),
        tools=[tool],
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "search")
        await pilot.press("enter")
        await until(pilot, lambda: gate.held == 1)
        await shows(pilot, app, "[Assistant] checking")
        await slide_back(pilot, app)
        assert tool.progress is not None
        tool.progress("half way")
        for _ in range(10):
            await pilot.pause()
        assert app.is_running
        gate.release()
        await h.settle()
        await until(pilot, lambda: later(app.screen_text()) > 4)
        assert app.is_running
        while "later nodes" in app.screen_text():  # slid forward, it is all there, caught up
            await pilot.press("ctrl+down")
            await pilot.pause(0.1)
        assert "[Assistant] found" in app.screen_text()
    await h.close()


@pytest.mark.parametrize("delay", range(4, 12))
@scenario
async def test_a_node_arriving_during_a_slide_is_drawn_in_order(tmp_path: Path, delay: int) -> None:
    h = await turns(tmp_path, 7, say("re 8"))
    async with running(h, tmp_path) as (app, pilot):
        chat = app.query_one("#chat-display")
        assert chat.slide(-1)  # pyright: ignore[reportAttributeAccessIssue]
        await until(pilot, lambda: "later nodes" in app.screen_text())
        other = await h.connect("you:cli")
        assert chat.slide(1)  # pyright: ignore[reportAttributeAccessIssue]  # begun, not done
        for _ in range(delay):
            await asyncio.sleep(0)
        await other.send(h.cursor, "late")
        await h.settle()
        for _ in range(30):
            await pilot.pause()
        mid = app.screen_text()
        assert mid.count("turn 7") == 1  # drawn once, not twice
        assert mid.count("re 7") == 1
        while chat.slide(1):  # pyright: ignore[reportAttributeAccessIssue]
            for _ in range(10):
                await pilot.pause()
        body = [ln for ln in app.screen_text().splitlines() if ln.startswith("[")]
        assert body[-4:] == [
            "[term] turn 7",
            "[Assistant] re 7",
            "[you:cli] late",
            "[Assistant] re 8",
        ]
        assert "later nodes" not in app.screen_text(), "no row left behind"
        assert app.screen_text().count("] late") == 1
        assert chat.at_tail  # pyright: ignore[reportAttributeAccessIssue]
        await other.close()
    await h.close()


def streaming(gate: Gate) -> Reply:
    return Reply(
        [TextDelta("streaming words"), Part.Text("streaming words")], gate=gate, gate_after=1
    )


@scenario
async def test_another_clients_reply_does_not_draw_under_the_later_row(tmp_path: Path) -> None:
    gate = Gate()
    h = await turns(tmp_path, 7, streaming(gate))
    async with running(h, tmp_path) as (app, pilot):
        await slide_back(pilot, app)
        before = later(app.screen_text())
        other = await h.connect("you:cli")
        await other.send(h.cursor, "from elsewhere")
        await until(pilot, lambda: gate.held == 1)
        for _ in range(10):
            await pilot.pause()
        text = app.screen_text()
        assert "streaming words" not in text, "not drawn while the window is back"
        assert later(text) > before, "counted"
        # Ctrl+Down still brings the window forward, and the reply in flight is there, whole
        while "later nodes" in app.screen_text():
            await pilot.press("ctrl+down")
            await pilot.pause(0.1)
        await shows(pilot, app, "[Assistant, live] streaming words")
        gate.release()
        await h.settle()
        await shows(pilot, app, "[Assistant] streaming words")
        await other.close()
    await h.close()


@scenario
async def test_sending_from_here_brings_the_window_to_the_tail(tmp_path: Path) -> None:
    h = await turns(tmp_path, 7, say("re 8"))
    async with running(h, tmp_path) as (app, pilot):
        await slide_back(pilot, app)
        await type_line(pilot, "mine")
        await pilot.press("enter")
        await shows(pilot, app, "[Assistant] re 8")
        assert "later nodes" not in app.screen_text()
        assert "[User] mine" in app.screen_text()
    await h.close()


async def flood(progress: Progress) -> str:
    """Say a lot at once: async, so no event waits on a worker thread's hop to the loop."""
    for i in range(20):
        progress(f"line {i}")
    return "flooded"


class Floody:
    name = "floody"
    commands = (flood,)


@scenario
async def test_a_reconnect_while_slid_back_keeps_the_place_and_a_send_returns(
    tmp_path: Path,
) -> None:
    h = await turns(tmp_path, 7, say("re 8"), extensions=[Floody()])
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
        await slide_back(pilot, app)
        await type_line(pilot, "/flood")
        await pilot.press("enter")
        await until(pilot, lambda: app.client is not slow)
        for worker in [w for w in app.workers if w.group == "submit"]:
            await worker.wait()
        if app.draft:  # `/flood` met the cut-off before it finished: it is back in the editor
            await pilot.press("ctrl+c")
            await until(pilot, lambda: app.draft == "")
        assert app.is_running
        assert "later nodes" in app.screen_text(), "the reader's place is kept across the reconnect"
        await type_line(pilot, "hello")
        await pilot.press("enter")
        await shows(pilot, app, "[Assistant] re 8")
        assert "later nodes" not in app.screen_text()
    await app.client.close()
    await h.close()


@scenario
async def test_switching_cursors_while_slid_back_and_returning_draws_the_tail(
    tmp_path: Path,
) -> None:
    h = await turns(tmp_path, 7)
    async with running(h, tmp_path) as (app, pilot):
        await slide_back(pilot, app)
        scout = await h.client.open(SYS)
        await h.set_auto(False, scout)
        await shows(pilot, app, "[0] 1 ")
        await pilot.press("alt+right")
        await shows(pilot, app, " 0 [1]")
        assert "turn 7" not in app.screen_text()
        await pilot.press("alt+left")
        await shows(pilot, app, "] turn 7")
        assert "later nodes" not in app.screen_text()
        assert app.is_running
    await h.close()
