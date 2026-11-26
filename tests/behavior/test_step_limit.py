"""`StepLimit`, the extension that turns auto-step off after N unattended steps (D1)."""

from pathlib import Path

from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import Body, Config, Cursor, Part, StepLimit, Stop, Subject, model_config


def text(t: str) -> Reply:
    return Reply([Part.Text(t)], Stop.End)


class KeepGoing:
    """A "continue" extension: after every reply it writes a `User` node, so `next` is
    `Sample` again and the loop would never end."""

    name = "keep_going"

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        cursor.write(Body.User("continue"))
        return None


def replies(n: int) -> FakeProvider:
    return FakeProvider([text(f"r{i}") for i in range(n)])


def limit(n: int) -> Config:
    """The session's root config with the step limit set (`extensions.step_limit.limit`)."""
    return model_config(FAKE_MODEL, {"step_limit": {"limit": n}})


@scenario
async def test_a_limit_below_one_fails_the_step_and_names_the_option(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(1), extensions=[StepLimit()], config=limit(0)
    )
    await h.send("go")
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert "at least 1" in view.hook_error.error
    assert h.provider.requests == []


@scenario
async def test_the_default_limit_is_fifty(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies(60), extensions=[KeepGoing(), StepLimit()])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 50


@scenario
async def test_it_stops_a_continue_loop_after_n_steps(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit()], config=limit(2)
    )
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    assert not h.session.cursor(h.cursor).auto
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_it_stops_a_model_that_calls_tools_every_step(tmp_path: Path) -> None:
    """The cursor moves every step (reply, results), so core never rests it; the limit does."""
    calls = Reply([Part.Call("a", "ta", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([calls] * 10),
        tools=[FakeTool("ta", "A")],
        extensions=[StepLimit()],
        config=limit(3),
    )
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 3
    assert not h.session.cursor(h.cursor).auto
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_a_new_input_starts_the_count_over(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit()], config=limit(2)
    )
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    await h.send("again")
    await h.settle()
    assert len(h.provider.requests) == 4, "the send gave another two steps"
    assert not h.session.cursor(h.cursor).auto


@scenario
async def test_resuming_with_set_auto_gets_a_fresh_count(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit()], config=limit(2)
    )
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    await h.set_auto(True)
    await h.settle()
    assert len(h.provider.requests) == 4
    assert not h.session.cursor(h.cursor).auto


@scenario
async def test_a_step_run_with_auto_step_off_is_not_counted(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit()], config=limit(2)
    )
    await h.input("go")
    await h.settle()
    for _ in range(3):
        await h.step()
        await h.settle()
    assert len(h.provider.requests) == 3, "three asked steps, one reply each"
    await h.set_auto(True)
    await h.settle()
    assert len(h.provider.requests) == 5, "the limit's two steps began at set_auto"


@scenario
async def test_a_run_that_ends_by_itself_is_left_alone(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(1), extensions=[StepLimit()], config=limit(5)
    )
    await h.send("hi")
    await h.settle()
    assert len(h.provider.requests) == 1
    assert not h.session.cursor(h.cursor).busy


async def ask(cursor: Cursor, text: str) -> str:
    """Write a message and have the model answer it (a command that turns auto-step on)."""
    cursor.write(Body.User(text))
    cursor.set_auto(True)
    return "asked"


async def look(cursor: Cursor) -> str:
    """A command that changes nothing."""
    return f"at {cursor.cursor(cursor.id).at}"


class Asking:
    name = "asking"
    commands = (ask, look)

    def __init__(self) -> None:
        self.seen: list[bool] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self.seen.append(cursor.cursor(cursor.id).command)
        return None


@scenario
async def test_a_commands_step_is_not_counted_and_the_model_gets_the_whole_limit(
    tmp_path: Path,
) -> None:
    """A command's step is attended (D16 section 2): after a command that turns auto-step on,
    exactly `limit` model steps run."""
    asking = Asking()
    h = await Harness.create(
        tmp_path,
        provider=replies(10),
        extensions=[KeepGoing(), StepLimit(), asking],
        config=limit(3),
    )
    assert await h.run("ask", {"text": "go"}) == "asked"
    await h.settle()
    assert len(h.provider.requests) == 3, "the command's step took none of the three"
    assert asking.seen == [True, False, False, False], "one command's step, then three"
    assert not h.session.cursor(h.cursor).command, "false at rest"
    assert not h.session.cursor(h.cursor).auto


@scenario
async def test_a_command_run_between_steps_does_not_spend_the_budget(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=replies(10),
        extensions=[KeepGoing(), StepLimit(), Asking()],
        config=limit(2),
    )
    for _ in range(3):  # three commands' steps, none counted, none sampling
        await h.run("look")
        await h.settle()
    assert h.provider.requests == []
    await h.run("ask", {"text": "go"})
    await h.settle()
    assert len(h.provider.requests) == 2
