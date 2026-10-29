"""`StepLimit`, the extension that turns auto-step off after N steps (D1)."""

from pathlib import Path

import pytest
from fakes import FakeProvider, Reply
from harness import Harness, scenario

from tau_next import Body, Cursor, Draft, Part, StepLimit, Stop, Subject


def text(t: str) -> Reply:
    return Reply([Part.Text(t)], Stop.End)


class KeepGoing:
    """A "continue" extension: after every reply it writes a `User` node, so `next` is
    `Sample` again and the loop would never end."""

    name = "keep_going"

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        cursor.write(Body.User("continue"))
        return None


class LeaveInputPending:
    """A `pre_input` hook that rewrites the input and delivers nothing."""

    name = "leave_input_pending"

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        assert subject.draft is not None
        subject.draft = Draft(Body.User("edited"), subject.draft.by, [])
        return None


def replies(n: int) -> FakeProvider:
    return FakeProvider([text(f"r{i}") for i in range(n)])


def test_a_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        StepLimit(0)


@scenario
async def test_it_stops_a_continue_loop_after_n_steps(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit(2)])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    assert not h.session.auto(h.cursor)
    assert not h.session.busy(h.cursor)


@scenario
async def test_a_new_input_starts_the_count_over(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit(2)])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    await h.send("again")
    await h.settle()
    assert len(h.provider.requests) == 4, "the send gave another two steps"
    assert not h.session.auto(h.cursor)


@scenario
async def test_resuming_with_set_auto_gets_a_fresh_count(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies(10), extensions=[KeepGoing(), StepLimit(2)])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2
    await h.set_auto(True)
    await h.settle()
    assert len(h.provider.requests) == 4
    assert not h.session.auto(h.cursor)


@scenario
async def test_an_input_a_hook_leaves_pending_steps_until_the_limit(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies(10), extensions=[LeaveInputPending(), StepLimit(3)]
    )
    await h.send("hi")
    await h.settle()
    assert len(h.provider.requests) == 3
    users = [b for b in h.bodies() if isinstance(b, Body.User)]
    assert users == [Body.User("edited")] * 3, "one User per committing step"
    assert not h.session.auto(h.cursor)
    assert len(h.session.items(h.cursor)) == 1, "the input is still pending"


@scenario
async def test_a_run_that_ends_by_itself_is_left_alone(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies(1), extensions=[StepLimit(5)])
    await h.send("hi")
    await h.settle()
    assert len(h.provider.requests) == 1
    assert not h.session.busy(h.cursor)
