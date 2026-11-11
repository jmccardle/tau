"""Questions are boxes above the editor, answered by a click or declined (D23 section 3)."""

import asyncio
from pathlib import Path
from typing import Any, Literal

from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from textual.widgets import Button
from tuikit import Gatekeeper, answer, running, say, shows, type_line, until

from tau import Author, Body, Cursor, Outcome, Part, Permissions, Stop, Subject, model_config


def calls() -> Reply:
    return Reply([Part.Call("a", "ta", "{}")], Stop.Tools)


async def asking(tmp_path: Path) -> tuple[Harness, FakeTool, Gatekeeper]:
    tool = FakeTool("ta", "ran")
    gate = Gatekeeper()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([calls(), say("done")]), tools=[tool], extensions=[gate]
    )
    await h.send("go")
    return h, tool, gate


@scenario
async def test_a_choice_question_is_answered_by_a_click_and_the_tool_proceeds(
    tmp_path: Path,
) -> None:
    h, tool, gate = await asking(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "run ta?  [allow / deny]")
        assert next(iter(app.query(".question-box"))).border_title == "permissions asks"
        assert tool.calls == [], "the tool waits for the answer"
        await answer(pilot, app)  # the first choice: allow
        await shows(pilot, app, "done")
        assert gate.verdicts == ["allow"]
        assert tool.calls == [{}]
        assert not app.query(".question-box"), "the answered question is gone"
    await h.close()


@scenario
async def test_declining_gives_the_extension_none(tmp_path: Path) -> None:
    h, tool, gate = await asking(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "run ta?")
        await answer(pilot, app, ".question-buttons Button:last-child")  # Decline
        await shows(pilot, app, "done")
        assert gate.verdicts == [None]
        assert tool.calls == []
        results = [n.body for n in h.nodes() if isinstance(n.body, Body.Results)]
        assert results[0] == Body.Results(
            [("a", Outcome.Err("the user said no"), Author.Extension("permissions"))]
        )
    await h.close()


@scenario
async def test_escape_in_a_question_declines_it_and_does_not_abort(tmp_path: Path) -> None:
    h, _tool, gate = await asking(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "run ta?")
        await pilot.press("escape")  # the keyboard is in the question box
        await shows(pilot, app, "done")
        assert gate.verdicts == [None]
    await h.close()


@scenario
async def test_a_question_for_another_client_is_shown_with_its_name_and_no_buttons(
    tmp_path: Path,
) -> None:
    class Asks:
        name = "asks"

        async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
            answer: Any = await cursor.ask("who goes there?", str, to="someone:else")
            self.answer = answer

    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Asks()])
    other = await h.connect("someone:else")
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "who goes there?  (waiting for someone:else)")
        assert not app.query(".question-box Button")
    await other.close()
    await h.close()


@scenario
async def test_a_text_question_is_answered_in_its_field(tmp_path: Path) -> None:
    class Asks:
        name = "asks"
        answer: Any = None

        async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
            self.answer = await cursor.ask("how many?", int)

    asks = Asks()
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[asks])
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "how many?")
        await pilot.click(".question-box Input")
        await pilot.press("x")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("is not an integer" in str(w.render()) for w in app.query(".form-error")),
        )
        assert app.query(".question-box"), "a refused answer stays in its box"
        await pilot.press("backspace", "7", "enter")
        await shows(pilot, app, "ok")
        assert asks.answer == 7
    await h.close()


@scenario
async def test_a_permission_question_shows_the_call_the_rule_and_its_three_answers(
    tmp_path: Path,
) -> None:
    tool = FakeTool("bash", "ran")
    call = Part.Call("a", "bash", '{"command": "git push origin master"}')
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([call], Stop.Tools), say("done")]),
        tools=[tool],
        extensions=[Permissions(tmp_path)],
        config=model_config(FAKE_MODEL, {"permissions": {"ask": ["bash"]}}),
    )
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "bash: git push origin master")
        await shows(pilot, app, "(ask rule: bash)  [yes / always / no]")
        await answer(pilot, app)
        await shows(pilot, app, "done")
        assert tool.calls == [{"command": "git push origin master"}]
    await h.close()


@scenario
async def test_a_question_asked_by_a_step_the_user_started_in_the_tui_is_answered(
    tmp_path: Path,
) -> None:
    """`App.run_async` (the real TUI; `run_test` does not) puts `eager_task_factory` on the loop,
    so a task runs to its first wait inside `create_task`. The step's hook asks from its first
    step, so the question must not need anything set after the task exists."""
    asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)

    class Asks:
        name = "asks"

        def __init__(self) -> None:
            self.answers: list[object] = []

        async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
            self.answers.append(await cursor.ask("proceed?", Literal["yes", "no"]))

    asks = Asks()
    h = await Harness.create(tmp_path, provider=FakeProvider([say("done")]), extensions=[asks])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "proceed?")
        await answer(pilot, app)
        await shows(pilot, app, "done")
        assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_a_question_with_a_no_choice_has_no_decline_button_beside_it(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("a", "bash", '{"command": "ls"}')], Stop.Tools)]),
        tools=[FakeTool("bash", "ran")],
        extensions=[Permissions(tmp_path)],
        config=model_config(FAKE_MODEL, {"permissions": {"ask": ["bash"]}}),
    )
    await h.send("go")
    async with running(h, tmp_path) as (app, pilot):
        await shows(pilot, app, "bash: ls")
        labels = [str(b.label) for b in app.query(".question-buttons Button").results(Button)]
        assert labels == ["yes", "always", "no"]
    await h.close()
