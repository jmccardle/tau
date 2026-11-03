"""Findings of the 8C-1 review, each a test that failed before its fix."""

from pathlib import Path
from typing import Any, Literal

from fakes import FakeProvider, Gate, Reply
from harness import Harness, scenario
from tuikit import SIZE, Greeter, running, say, shows, type_line, until

from tau import Body, Cursor, Part, Progress, Subject, TextDelta
from tau.tui import TauApp


async def ask_cmd(cursor: Cursor) -> str:
    """Ask, then answer."""
    answer: Any = await cursor.ask("go on?", Literal["yes", "no"])
    return f"got {answer}"


class Asker:
    name = "asker"
    commands = (ask_cmd,)


@scenario
async def test_a_line_sent_while_a_command_waits_does_not_lose_the_commands_result(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Asker()])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/ask_cmd")
        await pilot.press("enter")
        await shows(pilot, app, "go on?")
        app.focus_editor()
        await type_line(pilot, "meanwhile")
        await pilot.press("enter")
        await shows(pilot, app, "> meanwhile")  # pending: the command holds the step
        await pilot.click(".question-buttons Button")
        await until(pilot, lambda: "/ask_cmd: got yes" in app.toasts)
    await h.close()


@scenario
async def test_two_lines_typed_at_once_are_both_sent(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a"), say("b")]))
    async with running(h, tmp_path) as (_, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await type_line(pilot, "two")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                [n.body.text for n in h.nodes() if isinstance(n.body, Body.User)] == ["one", "two"]
            ),
        )
    await h.close()


@scenario
async def test_a_line_the_session_cannot_take_stays_in_the_editor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("never")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        h.session.close()
        gate.release()
        await until(pilot, lambda: app.failure is not None)
        await type_line(pilot, "do not lose me")
        await pilot.press("enter")
        await until(pilot, lambda: len(app.toasts) > 1)
        assert app.draft == "do not lose me"
    await h.driver.stop()


@scenario
async def test_tab_after_the_session_is_lost_is_a_toast_not_a_crash(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("never")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        h.session.close()
        gate.release()
        await until(pilot, lambda: app.failure is not None)
        before = len(app.toasts)
        await type_line(pilot, "/x")
        await pilot.press("tab")
        await until(pilot, lambda: len(app.toasts) > before)
        assert app.is_running
    await h.driver.stop()


class Asks:
    name = "asks"

    def __init__(self, expect: object) -> None:
        self.expect = expect
        self.answers: list[object] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        self.answers.append(await cursor.ask("name?", self.expect))


@scenario
async def test_an_empty_answer_to_a_text_question_is_refused(tmp_path: Path) -> None:
    asks = Asks(str)
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[asks])
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "name?")
        await pilot.click(".question-box Input")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("a value is needed" in str(w.render()) for w in app.query(".form-error")),
        )
        assert asks.answers == [], "an empty line is not an answer"
        await type_line(pilot, "ada")
        await pilot.press("enter")
        await shows(pilot, app, "ok")
        assert asks.answers == ["ada"]
    await h.close()


@scenario
async def test_an_empty_answer_is_none_when_the_question_allows_none(tmp_path: Path) -> None:
    asks = Asks(str | None)
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[asks])
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "name?")
        await pilot.click(".question-box Input")
        await pilot.press("enter")
        await shows(pilot, app, "ok")
        assert asks.answers == [None]
    await h.close()


@scenario
async def test_a_reconnect_mid_reply_does_not_draw_the_live_text_twice(tmp_path: Path) -> None:
    def flood(progress: Progress) -> str:
        """Say a lot at once."""
        for i in range(20):
            progress(f"line {i}")
        return "flooded"

    class Floody:
        name = "floody"
        commands = (flood,)

    gate = Gate()
    reply = Reply(
        [TextDelta("part"), TextDelta("ial"), Part.Text("partial")], gate=gate, gate_after=1
    )
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]), extensions=[Floody()])
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
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "[Assistant, live] part")
        await type_line(pilot, "/flood")
        await pilot.press("enter")
        await until(pilot, lambda: app.client is not slow)
        await pilot.pause(0.3)
        assert "partpart" not in app.screen_text()
        assert app.screen_text().count("[Assistant, live]") == 1
        gate.release()
        await shows(pilot, app, "[Assistant] partial")
    await app.client.close()
    await h.close()


@scenario
async def test_a_session_in_memory_names_the_directory_it_was_given(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    client = await h.connect("me:tui")
    app = TauApp(client, h.cursor, welcome=None, home=tmp_path / "h", cwd=tmp_path / "work")
    async with app.run_test(size=SIZE) as pilot:
        await shows(pilot, app, str(tmp_path / "work"))
    await client.close()
    await h.close()
