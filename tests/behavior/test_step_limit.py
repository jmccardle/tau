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
