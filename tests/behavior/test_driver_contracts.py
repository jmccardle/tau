"""What the driver promises about hooks, questions, extensions, and its own lifetime."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

import pytest
from fakes import FakeClock, FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

from tau import (
    Author,
    Body,
    CallDraft,
    Client,
    Cursor,
    DefinitionError,
    Ending,
    Event,
    NodeHandle,
    Outcome,
    Part,
    Stop,
    Subject,
    ToolError,
)

CALL = Reply([Part.Call("c", "echo", "{}")], Stop.Tools)
DONE = Reply([Part.Text("done")], Stop.End)


def results(h: Harness) -> list[tuple[str, Outcome, Author]]:
    """The outcomes the session's one `Results` node saved."""
    (node,) = [b for b in h.bodies() if isinstance(b, Body.Results)]
    return node.outcomes


async def one_call(tmp_path: Path, tool: FakeTool, *extensions: object) -> Harness:
    """Run a send that makes one call to `echo`, then a final reply; return the settled harness."""
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([CALL, DONE]),
        tools=[tool],
        extensions=list(extensions),  # type: ignore[arg-type]
    )
    await h.send("go")
    await h.settle()
    return h


# Questions (model.md Q9: a hook's timeout is the hook's own)


@scenario
async def test_a_question_whose_asker_timed_out_is_gone(tmp_path: Path) -> None:
    gate = Gate()
    timed_out: list[bool] = []

    class Impatient:
        name = "impatient"

        async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
            try:
                async with asyncio.timeout(0):
                    await cursor.ask("quick?")
            except TimeoutError:
                timed_out.append(True)
            await gate.wait()  # the step is still running when the frontend acts
            return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Impatient(), recorder], gates=[gate])
    await h.step()
    await h.settle()
    assert timed_out == [True]
    assert h.driver.state().open_questions == 0
    assert (await h.view()).questions == ()
    with pytest.raises(LookupError):
        await h.respond("late")
    await h.abort()  # nothing stale to decline: core's abort and the driver agree
    gate.release()
    await h.settle()
    assert recorder.endings == [Ending.Aborted()]
    await h.close()


# Lifetime


@scenario
async def test_rest_raises_when_the_driver_is_stopped_meanwhile_and_after(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hi")], Stop.End, gate)]),
    )
    await h.send("go")
    await h.settle()
    waiting = asyncio.ensure_future(h.driver.rest(h.cursor))
    await asyncio.sleep(0)
    assert not waiting.done(), "the cursor is busy: rest waits"
    await h.driver.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        await waiting
    with pytest.raises(RuntimeError, match="stopped"):
        await h.driver.rest(h.cursor)
    h.session.close()


# A hook that fails costs the call or the step, with its type and traceback


@scenario
async def test_a_tool_hook_that_raises_makes_its_call_err_with_type_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Typo:
        name = "typo"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            raise KeyError("x")

    tool = FakeTool("echo", "ok")
    h = await one_call(tmp_path, tool, Typo())
    assert results(h) == [("c", Outcome.Err("KeyError: 'x'"), Author.Extension("typo"))]
    assert tool.calls == []
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "KeyError: 'x'" in err
    await h.close()


@scenario
async def test_a_cursor_hook_that_raises_fails_its_step_with_type_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def pre_step(cursor: Cursor, subject: Subject) -> str | None:
        raise KeyError("x")

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Raises(pre_step=pre_step), recorder])
    await h.step()
    await h.settle()
    assert recorder.endings == [Ending.Failed("KeyError: 'x'")]
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "KeyError: 'x'" in err
    await h.close()


class Raises:
    """An extension named `raises` with the one cursor hook it is given."""

    name = "raises"

    def __init__(self, **hooks: Callable[[Cursor, Subject], Awaitable[str | None]]) -> None:
        self.__dict__.update(hooks)


# A hook or tool that breaks its return contract costs one call or one step, not the driver


@scenario
@pytest.mark.parametrize("returned", [5, b"bytes", ["a"]])
async def test_a_cursor_hook_that_returns_a_non_str_fails_its_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], returned: object
) -> None:
    async def pre_step(cursor: Cursor, subject: Subject) -> str | None:
        return returned  # type: ignore[return-value]

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Raises(pre_step=pre_step), recorder])
    await h.step()
    await h.settle()  # the driver lives
    (ending,) = recorder.endings
    assert isinstance(ending, Ending.Failed)
    assert ending.error.startswith("TypeError: pre_step of extension 'raises' returned")
    assert "returned" in capsys.readouterr().err
    await h.close()


@scenario
async def test_a_cursor_hook_that_leaves_a_draft_that_is_not_a_draft_fails_its_step(
    tmp_path: Path,
) -> None:
    async def pre_input(cursor: Cursor, subject: Subject) -> str | None:
        subject.draft = "hijack"  # type: ignore[assignment]
        return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Raises(pre_input=pre_input), recorder])
    await h.send("hi")
    await h.settle()
    (ending,) = recorder.endings
    assert isinstance(ending, Ending.Failed)
    assert ending.error.startswith("TypeError: pre_input of extension 'raises' left 'hijack'")
    await h.close()


@scenario
async def test_a_tool_hook_that_breaks_its_contract_makes_its_call_err(tmp_path: Path) -> None:
    class Wrong:
        name = "wrong"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = "fine"  # type: ignore[assignment]
            return None

    tool = FakeTool("echo", "ok")
    h = await one_call(tmp_path, tool, Wrong())
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("TypeError: pre_tool of extension 'wrong' set the outcome")
    assert by == Author.Extension("wrong")
    assert tool.calls == []
    await h.close()


@scenario
async def test_a_tool_hook_that_returns_a_non_str_makes_its_call_err(tmp_path: Path) -> None:
    class Wrong:
        name = "wrong"

        async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
            return 7  # type: ignore[return-value]

    h = await one_call(tmp_path, FakeTool("echo", "ok"), Wrong())
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("TypeError: pre_result of extension 'wrong' returned 7")
    assert by == Author.Extension("wrong")
    await h.close()


# CallDraft


def test_a_call_draft_keeps_its_id_and_tool_and_lets_the_rest_change() -> None:
    draft = CallDraft("c", "echo", "{}")
    with pytest.raises(AttributeError):
        draft.tool = "real"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        draft.id = "d"  # type: ignore[misc]
    draft.args_raw = '{"a": 1}'
    draft.outcome = Outcome.Ok("x")
    assert (draft.id, draft.tool, draft.args_raw, draft.outcome) == (
        "c",
        "echo",
        '{"a": 1}',
        Outcome.Ok("x"),
    )


@scenario
async def test_a_hook_that_renames_the_tool_is_told_it_cannot(tmp_path: Path) -> None:
    class Alias:
        name = "alias"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.tool = "real"  # type: ignore[misc]
            return None

    echo, real = FakeTool("echo", "old"), FakeTool("real", "new")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([CALL, DONE]),
        tools=[echo, real],
        extensions=[Alias()],
    )
    await h.send("go")
    await h.settle()
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("AttributeError:")
    assert by == Author.Extension("alias")
    assert echo.calls == [] == real.calls, "neither ran"
    await h.close()


# Every pre_tool hook runs (a permissions extension cannot be bypassed by list order)


@scenario
async def test_an_outcome_set_by_an_early_pre_tool_hook_is_seen_by_a_later_one(
    tmp_path: Path,
) -> None:
    seen: list[Outcome | None] = []

    class Cache:
        name = "cache"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = Outcome.Ok("cached")
            return None

    class Permissions:
        name = "permissions"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            seen.append(call.outcome)
            call.outcome = Outcome.Err("denied")
            return None

    tool = FakeTool("echo", "ran")
    h = await one_call(tmp_path, tool, Cache(), Permissions())
    assert seen == [Outcome.Ok("cached")]
    assert results(h) == [("c", Outcome.Err("denied"), Author.Extension("permissions"))]
    assert tool.calls == [], "an outcome answers the call: the tool does not run"
    await h.close()


# A sample that finished before the abort reached it stays a complete reply


@scenario
async def test_a_complete_sample_answered_after_an_abort_stays_a_complete_reply(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([Part.Text("whole")], Stop.End, gate, gate_after=1)
    recorder = Recorder()
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]), extensions=[recorder])
    await h.send("go")
    await h.settle()
    gate.release()
    await asyncio.sleep(0)  # the provider finishes; the driver has not yet taken its answer
    await h.abort()
    await h.settle()
    assert h.bodies()[-1] == Body.Assistant([Part.Text("whole")], Stop.End)
    assert recorder.endings == [Ending.Aborted()]
    await h.close()


# Extensions are checked when the driver is built


class Bare:
    def __init__(self, name: object = "bare", **attrs: object) -> None:
        self.name = name
        self.__dict__.update(attrs)


@pytest.mark.parametrize(
    ("extensions", "needle"),
    [
        ([Bare(name="")], "`name` must be a non-empty str"),
        ([Bare(name=3)], "`name` must be a non-empty str"),
        ([Bare("a"), Bare("a")], "two extensions are named 'a'"),
        ([Bare(pre_stpe=lambda: None)], "`pre_stpe` is not a hook point"),
        ([Bare(post_tool=lambda: None)], "`post_tool` is not a hook point"),
        ([Bare(pre_step=3)], "hook `pre_step` must be callable"),
    ],
)
@scenario
async def test_a_bad_extension_is_refused_when_the_driver_is_built(
    tmp_path: Path, extensions: list[object], needle: str
) -> None:
    with pytest.raises(DefinitionError, match=needle):
        await Harness.create(tmp_path, extensions=extensions)  # type: ignore[arg-type]


# A tool that is not an async function, or that fails in an odd way, costs its call only


@scenario
async def test_a_tool_whose_call_is_not_async_makes_its_call_err(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Plain:
        name = "echo"
        description = "A call that is a plain def."
        parameters: ClassVar[dict[str, object]] = {"type": "object"}

        def call(self, args_raw: str) -> Outcome:
            return Outcome.Ok("hi")

    h = await one_call(tmp_path, Plain())  # type: ignore[arg-type]
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("TypeError:")
    assert by == Author.Tools()
    assert "Traceback" in capsys.readouterr().err
    await h.close()


@scenario
async def test_a_tool_whose_call_raises_before_returning_an_awaitable_makes_its_call_err(
    tmp_path: Path,
) -> None:
    class Eager:
        name = "echo"
        description = "Raises when called."
        parameters: ClassVar[dict[str, object]] = {"type": "object"}

        def call(self, args_raw: str) -> Outcome:
            raise ValueError("no")

    h = await one_call(tmp_path, Eager())  # type: ignore[arg-type]
    assert results(h) == [("c", Outcome.Err("ValueError: no"), Author.Tools())]
    await h.close()


@scenario
async def test_a_tool_error_is_saved_as_its_message_and_any_other_error_with_its_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = await one_call(tmp_path, FakeTool("echo", ToolError("for the model")))
    assert results(h) == [("c", Outcome.Err("for the model"), Author.Tools())]
    assert "Traceback" not in capsys.readouterr().err
    await h.close()
    h = await one_call(tmp_path / "2", FakeTool("echo", KeyError("x")))
    assert results(h) == [("c", Outcome.Err("KeyError: 'x'"), Author.Tools())]
    assert "Traceback" in capsys.readouterr().err
    await h.close()


@scenario
async def test_a_pre_tool_hook_that_clears_an_outcome_does_not_become_the_blame(
    tmp_path: Path,
) -> None:
    class Cache:
        name = "cache"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = Outcome.Ok("cached")
            return None

    class Undo:
        name = "undo"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = None
            return None

    h = await one_call(tmp_path, FakeTool("echo", "ran"), Cache(), Undo())
    assert results(h) == [("c", Outcome.Ok("ran"), Author.Tools())]
    await h.close()


@scenario
async def test_a_pre_result_hook_that_clears_the_outcome_makes_its_call_err(
    tmp_path: Path,
) -> None:
    class Undo:
        name = "undo"

        async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = None
            return None

    h = await one_call(tmp_path, FakeTool("echo", "ok"), Undo())
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("TypeError: pre_result of extension 'undo' cleared")
    assert by == Author.Extension("undo")
    await h.close()


# A hook that leaks a CancelledError has failed, unless the driver is cancelling it


async def leak_a_cancel() -> None:
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.cancel()
    await task


@scenario
async def test_a_cursor_hook_that_leaks_a_cancelled_error_fails_its_step(tmp_path: Path) -> None:
    async def pre_step(cursor: Cursor, subject: Subject) -> str | None:
        await leak_a_cancel()
        return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Raises(pre_step=pre_step), recorder])
    await h.step()
    await h.settle()
    (ending,) = recorder.endings
    assert isinstance(ending, Ending.Failed)
    assert ending.error.startswith("CancelledError")
    assert not h.session.cursor(h.cursor).busy
    await h.close()


@scenario
async def test_a_tool_hook_that_leaks_a_cancelled_error_makes_its_call_err(tmp_path: Path) -> None:
    class Leaky:
        name = "leaky"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            await leak_a_cancel()
            return None

    h = await one_call(tmp_path, FakeTool("echo", "ok"), Leaky())
    ((_, outcome, by),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith("CancelledError")
    assert by == Author.Extension("leaky")
    await h.close()


@scenario
async def test_stopping_the_driver_cancels_a_running_hook_without_failing_it(
    tmp_path: Path,
) -> None:
    gate = Gate()

    async def pre_step(cursor: Cursor, subject: Subject) -> str | None:
        await gate.wait()
        return None

    recorder = Recorder()
    h = await Harness.create(
        tmp_path, extensions=[Raises(pre_step=pre_step), recorder], gates=[gate]
    )
    await h.step()
    await h.settle()
    await h.driver.stop()
    assert recorder.endings == [], "a stop is a crash: nothing is recorded"
    assert h.driver.state().failure is None
    h.session.close()


# The driver dies of its root cause, however it is raised


async def read_all(stream: Client) -> list[Event]:
    return [event async for event in stream]


class Panic(BaseException):
    """What a panic in core looks like to Python."""


class TickingClock(FakeClock):
    """A clock that raises `error` once armed."""

    def __init__(self) -> None:
        super().__init__()
        self.error: BaseException | None = None

    def now(self) -> int:
        if self.error is not None:
            raise self.error
        return super().now()


@scenario
async def test_a_panic_in_the_pump_fails_the_driver_and_ends_the_streams(tmp_path: Path) -> None:
    clock = TickingClock()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("hi")], Stop.End)]), clock=clock
    )
    stream = await h.connect("watcher")
    await h.send("go")
    clock.error = Panic("core panicked")  # the answer to the sample asks the clock
    with pytest.raises(RuntimeError, match="Panic: core panicked"):
        await asyncio.wait_for(h.driver.rest(h.cursor), 1)
    with pytest.raises(ConnectionError, match=r"^RuntimeError: Panic: core panicked$"):
        await asyncio.wait_for(read_all(stream), 1)  # what was buffered, then the failure
    with pytest.raises(RuntimeError, match="Panic"):
        await h.step()
    await h.driver.stop()
    h.session.close()


@scenario
async def test_a_later_error_does_not_replace_the_one_that_killed_the_driver(
    tmp_path: Path,
) -> None:
    """Call `ok` has answered and waits for the pump when call `bad`'s hook dies. The pump then
    meets an error of its own (the clock), which is not the root cause."""
    gate = Gate()
    clock = TickingClock()
    harness: list[Harness] = []

    class Dies:
        name = "dies"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            if call.tool != "bad":
                return None
            await gate.wait()
            while not harness[0].driver.state().queued:  # `ok`'s answer is in the pump's queue
                await asyncio.sleep(0)
            clock.error = RuntimeError("the pump's own error")
            raise Panic("the root cause")

    calls = [Part.Call("1", "ok", "{}"), Part.Call("2", "bad", "{}")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply(calls, Stop.Tools)]),
        tools=[FakeTool("ok", "fine", gate), FakeTool("bad", "never")],
        extensions=[Dies()],
        clock=clock,
        gates=[gate],
    )
    harness.append(h)
    await h.send("go")
    await h.settle()
    gate.release()
    with pytest.raises(RuntimeError, match="the root cause"):
        await asyncio.wait_for(h.driver.rest(h.cursor), 1)
    await h.driver.stop()
    h.session.close()
