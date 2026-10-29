"""The behavior suite, through the Harness. Each test mirrors a Rust scenario
(`crates/tau-core/tests/`); the docstring names it."""

from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

import tau_next
from tau_next import (
    Author,
    Body,
    CallDraft,
    Config,
    Cursor,
    Ending,
    ItemData,
    Outcome,
    Part,
    Stop,
    Subject,
)

SYSTEM = [Body.Config(Config("{}")), Body.System("sys")]


def text(t: str, stop: Stop = Stop.End) -> Reply:
    return Reply([Part.Text(t)], stop)


def calls(*ids: str) -> Reply:
    return Reply([Part.Call(i, f"t{i}", "{}") for i in ids], Stop.Tools)


@scenario
async def test_a_plain_turn(tmp_path: Path) -> None:
    """step.rs `plain_turn`."""
    provider = FakeProvider([text("hello")])
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("hi")
    await h.settle()
    assert h.bodies() == [*SYSTEM, Body.User("hi"), Body.Assistant([Part.Text("hello")], Stop.End)]
    assert h.session.items(h.cursor) == []
    assert provider.requests[0].walk == (0, 1, 2)
    assert provider.requests[0].config == Config("{}")
    assert not h.session.busy(h.cursor)


@scenario
async def test_a_batch_answers_in_call_order_then_samples_again(tmp_path: Path) -> None:
    """step.rs `batch_answers_in_call_order_then_samples_again`."""
    ga, gb = Gate(), Gate()
    provider = FakeProvider([calls("a", "b"), text("done")])
    ta, tb = FakeTool("ta", "A", ga), FakeTool("tb", "B", gb)
    h = await Harness.create(tmp_path, provider=provider, tools=[ta, tb])
    await h.send("look")
    await h.settle()
    assert (ga.held, gb.held) == (1, 1)
    gb.release()  # b finishes first
    await h.settle()
    assert isinstance(h.bodies()[-1], Body.Assistant), "no Results until the join"
    ga.release()
    await h.settle()
    *_, results, reply = h.bodies()
    assert results == Body.Results(
        [("a", Outcome.Ok("A"), Author.Tools()), ("b", Outcome.Ok("B"), Author.Tools())]
    )
    assert reply == Body.Assistant([Part.Text("done")], Stop.End)
    assert len(provider.requests) == 2
    assert provider.requests[1].walk[-1] == 4, "the second sample's walk ends at Results"


@scenario
async def test_a_tool_error_is_an_outcome_and_the_model_sees_it(tmp_path: Path) -> None:
    """step.rs `a_tool_error_is_an_outcome_and_the_model_sees_it`."""
    provider = FakeProvider([calls("a"), text("it failed")])
    tool = FakeTool("ta", RuntimeError("no such file"))
    h = await Harness.create(tmp_path, provider=provider, tools=[tool])
    await h.send("look")
    await h.settle()
    *_, results, reply = h.bodies()
    assert results == Body.Results([("a", Outcome.Err("no such file"), Author.Tools())])
    assert isinstance(reply, Body.Assistant)
    assert len(provider.requests) == 2


@scenario
async def test_an_http_error_leaves_a_side_node_and_the_cursor_where_it_was(
    tmp_path: Path,
) -> None:
    """step.rs `an_http_error_leaves_a_side_node_and_the_cursor_where_it_was`."""
    recorder = Recorder()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Fail("HTTP 500")]), extensions=[recorder]
    )
    await h.send("hi")
    await h.settle()
    snap = h.snapshot()
    assert snap.cursors[h.cursor].at == 2
    assert snap.cursors[h.cursor].walk[-1] == Body.User("hi")
    assert snap.tree[3][1:] == (2, Author.Core(), Body.Custom("sample_error", "HTTP 500"))
    assert not snap.cursors[h.cursor].auto
    assert not snap.cursors[h.cursor].busy
    assert recorder.endings == [Ending.Failed("HTTP 500")]


@scenario
async def test_abort_during_a_sample_commits_the_partial_reply(tmp_path: Path) -> None:
    """step.rs `abort_during_sample_commits_the_partial_reply`."""
    gate = Gate()
    reply = Reply([Part.Text("hel"), Part.Text("lo")], Stop.End, gate, gate_after=1)
    recorder = Recorder()
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]), extensions=[recorder])
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    assert await h.abort()
    await h.settle()
    assert h.bodies()[-1] == Body.Assistant([Part.Text("hel")], Stop.Interrupted)
    assert recorder.points.count("post_reply") == 1
    assert recorder.endings == [Ending.Aborted()]
    assert not h.session.auto(h.cursor)


@scenario
async def test_abort_during_tools_interrupts_unfinished_calls(tmp_path: Path) -> None:
    """step.rs `abort_during_tools_interrupts_unfinished_calls_and_runs_post_result_once`."""
    gate = Gate()
    provider = FakeProvider([calls("a", "b"), text("never")])
    recorder = Recorder()
    h = await Harness.create(
        tmp_path,
        provider=provider,
        tools=[FakeTool("ta", "A"), FakeTool("tb", "B", gate)],
        extensions=[recorder],
    )
    await h.send("look")
    await h.settle()
    assert gate.held == 1
    assert await h.abort()
    await h.settle()
    assert h.bodies()[-1] == Body.Results(
        [("a", Outcome.Ok("A"), Author.Tools()), ("b", Outcome.Interrupted(), Author.Tools())]
    )
    assert recorder.points.count("post_result") == 1
    assert recorder.endings == [Ending.Aborted()]
    assert len(provider.requests) == 1, "no sample after the abort"


@scenario
async def test_a_crash_during_a_sample_redelivers_the_input_once(tmp_path: Path) -> None:
    """replay.rs `crash_during_a_sample_loses_the_unsynced_input_node_and_redelivers_its_item`."""
    gate = Gate()
    provider = FakeProvider([Reply([Part.Text("lost")], gate=gate), text("hello")])
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    await h.crash()
    await h.settle()
    assert h.bodies() == SYSTEM, "the unsynced input node is gone"
    assert [i.data for i in h.session.items(h.cursor)] == [ItemData.Input("term", "hi")]
    await h.step()
    await h.settle()
    assert h.bodies() == [
        *SYSTEM,
        Body.User("hi"),
        Body.Assistant([Part.Text("hello")], Stop.End),
    ]
    assert h.session.items(h.cursor) == []
    assert len(provider.requests) == 2


async def crashed_mid_batch(tmp_path: Path) -> tuple[Harness, FakeTool, FakeTool, FakeProvider]:
    """a has answered; b is held; the power goes out."""
    gate = Gate()
    provider = FakeProvider([calls("a", "b"), text("checking")])
    ta, tb = FakeTool("ta", "A"), FakeTool("tb", "B", gate)
    h = await Harness.create(tmp_path, provider=provider, tools=[ta, tb])
    await h.send("look")
    await h.settle()
    assert (len(ta.calls), len(tb.calls), gate.held) == (1, 1, 1)
    await h.crash()
    await h.settle()
    assert isinstance(h.bodies()[-1], Body.Assistant), "rests at the batch"
    assert not h.session.auto(h.cursor)
    gate.release()
    return h, ta, tb, provider


@scenario
async def test_after_a_crash_mid_batch_input_goes_under_the_unanswered_batch(
    tmp_path: Path,
) -> None:
    """step.rs `after_a_crash_mid_batch_input_goes_under_the_unanswered_batch`."""
    h, ta, tb, provider = await crashed_mid_batch(tmp_path)
    await h.input("the power went out, check the files")
    await h.settle()  # input wakes the cursor: pre_step and post_step
    await h.step()
    await h.settle()
    batch, user, reply = h.bodies()[-3:]
    assert isinstance(batch, Body.Assistant)
    assert user == Body.User("the power went out, check the files")
    assert reply == Body.Assistant([Part.Text("checking")], Stop.End)
    assert (len(ta.calls), len(tb.calls)) == (1, 1), "input first: nothing reruns"
    assert provider.requests[1].walk[-2:] == (3, 4)


@scenario
async def test_after_a_crash_mid_batch_a_step_reruns_the_whole_batch(tmp_path: Path) -> None:
    """step.rs `after_a_crash_mid_batch_a_step_reruns_the_whole_batch`."""
    h, ta, tb, provider = await crashed_mid_batch(tmp_path)
    await h.step()
    await h.settle()
    assert h.bodies()[-1] == Body.Results(
        [("a", Outcome.Ok("A"), Author.Tools()), ("b", Outcome.Ok("B"), Author.Tools())]
    )
    assert (len(ta.calls), len(tb.calls)) == (2, 2)
    assert len(provider.requests) == 1, "a step is one pass: no sample after the tools"


@scenario
async def test_a_go_to_on_a_busy_cursor_aborts_then_moves(tmp_path: Path) -> None:
    """machine.rs `a_go_to_on_a_busy_cursor_aborts_runs_post_hooks_then_moves`."""
    gate = Gate()
    recorder = Recorder()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hello")], gate=gate)]),
        extensions=[recorder],
    )
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    assert await h.go_to(1), "the sample in flight is cancelled"
    await h.settle()
    assert h.session.at(h.cursor) == 1, "moved after post_step"
    assert recorder.endings == [
        Ending.Aborted(),
        Ending.Done(),
    ]
    assert recorder.points == [
        "pre_step",
        "pre_input",
        "pre_sample",
        "post_step",  # the aborted step
        "pre_step",  # the go-to's wake, auto-step off
        "post_step",
    ]
    assert not h.session.auto(h.cursor)
    assert not h.session.busy(h.cursor)
    assert len(h.nodes()) == 3, "the input is committed; the reply never was"


@scenario
async def test_a_reply_naming_a_call_id_twice_fails_the_step_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """V2 for an `Assistant` node: the write is refused, nothing hangs waiting for a
    batch that was never handed out, and the tree is as it was before the reply."""
    recorder = Recorder()
    tool = FakeTool("t", "ran")
    twice = Reply([Part.Call("a", "t", "{}"), Part.Call("a", "t", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path, provider=FakeProvider([twice]), tools=[tool], extensions=[recorder]
    )
    await h.send("go")
    await h.settle()
    assert not h.session.busy(h.cursor), "the step ended"
    assert recorder.endings == [Ending.Failed("a node names a call id twice")]
    assert h.bodies() == [*SYSTEM, Body.User("go")], "the reply was not written"
    assert tool.calls == []
    assert not h.session.auto(h.cursor)


@scenario
async def test_a_pre_input_string_refuses_with_a_notice(tmp_path: Path) -> None:
    """step.rs `pre_input_string_refuses_with_a_notice`."""
    recorder = Recorder(says={"pre_input": "model: fast"})
    provider = FakeProvider([])
    h = await Harness.create(tmp_path, provider=provider, extensions=[recorder])
    await h.send("/model fast")
    await h.settle()
    assert h.notices() == [("term", "model: fast")]
    assert h.session.items(h.cursor) == []
    assert not h.session.auto(h.cursor)
    assert h.bodies() == SYSTEM
    assert recorder.endings == [Ending.Halted("model: fast")]
    assert provider.requests == []


# Beyond the ten mirrored first: coverage-list items the driver also owns.


@scenario
async def test_bad_arguments_and_an_unknown_tool_are_outcomes(tmp_path: Path) -> None:
    """model.md section 2 and D7 (not yet a Rust scenario): the tool answers its own bad
    arguments; a name nobody has is unknown."""
    tool = FakeTool("ta", "A")
    provider = FakeProvider(
        [
            Reply([Part.Call("a", "ta", "not json"), Part.Call("b", "nope", "{}")], Stop.Tools),
            text("ok"),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[tool])
    await h.send("go")
    await h.settle()
    *_, results, _ = h.bodies()
    assert results == Body.Results(
        [
            ("a", Outcome.Err("arguments are not valid JSON"), Author.Tools()),
            ("b", Outcome.Err("unknown tool: nope"), Author.Tools()),
        ]
    )
    assert tool.calls == []


@scenario
async def test_a_pre_tool_hook_can_repair_bad_arguments(tmp_path: Path) -> None:
    """D7: the tool, or a `pre_tool` hook, answers an `args_raw` it cannot use."""

    def repair(subject: Subject | CallDraft) -> None:
        assert isinstance(subject, CallDraft)
        subject.args_raw = '{"fixed": true}'

    tool = FakeTool("ta", "A")
    provider = FakeProvider([Reply([Part.Call("a", "ta", "not json")], Stop.Tools), text("ok")])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        tools=[tool],
        extensions=[Recorder(on={"pre_tool": repair})],
    )
    await h.send("go")
    await h.settle()
    assert tool.calls == [{"fixed": True}]
    assert h.bodies()[-2] == Body.Results([("a", Outcome.Ok("A"), Author.Tools())])
    await h.close()


@scenario
async def test_a_pre_tool_string_is_that_calls_err_and_the_tool_does_not_run(
    tmp_path: Path,
) -> None:
    """model.md section 7, a string from a tool hook; the outcome's `by` is the
    extension (step.rs `each_outcome_records_who_produced_it`)."""
    permissions = Recorder("permissions", says={"pre_tool": "denied"})
    tool = FakeTool("ta", "A")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([calls("a"), text("ok")]),
        tools=[tool],
        extensions=[permissions],
    )
    await h.send("go")
    await h.settle()
    *_, results, _ = h.bodies()
    assert results == Body.Results([("a", Outcome.Err("denied"), Author.Extension("permissions"))])
    assert tool.calls == []


@scenario
async def test_a_step_that_committed_nothing_is_throttled(tmp_path: Path) -> None:
    """step.rs `a_step_that_committed_nothing_is_throttled`, on the fake clock."""

    class Ticker:
        """Adds a second item during the step it was woken by."""

        name = "ticker"
        done = False

        async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
            if not self.done:
                self.done = True
                h.driver.add_item(cursor.id, ItemData.Extension("timer", "tock"))
            return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Ticker(), recorder])
    start = h.clock.now()
    await h.add_item("timer", "tick")
    await h.settle()
    assert recorder.points.count("pre_step") == 1
    assert h.clock.sleepers == 1, "the throttle's sleeper is parked"
    h.clock.advance(249)
    await h.settle()
    assert recorder.points.count("pre_step") == 1
    h.clock.advance(1)
    await h.settle()
    assert h.clock.now() == start + 250
    assert recorder.points.count("pre_step") == 2, "the item left in place does not spin"
    assert not h.session.busy(h.cursor)


@scenario
async def test_a_question_holds_its_step_until_it_is_answered(tmp_path: Path) -> None:
    """A question is a hook awaiting its answer: the hook's job stays out, so the cursor
    is busy. Core's rules for a busy cursor apply: step is refused, input waits for the
    next step's input phase."""

    class Asker:
        name = "asker"
        answers: list[str | None]

        def __init__(self) -> None:
            self.answers = []

        async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
            self.answers.append(await cursor.ask("term", "send it?"))
            return None

    asker = Asker()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([text("ok"), text("ok 2")]), extensions=[asker]
    )
    await h.send("hi")
    await h.settle()
    assert [q.text for q in h.driver.questions(h.cursor)] == ["send it?"]
    assert h.session.busy(h.cursor)
    with pytest.raises(tau_next.Busy):
        await h.step()
    await h.input("more")
    await h.answer("yes")
    await h.settle()
    assert asker.answers == ["yes"]
    assert [q.text for q in h.driver.questions(h.cursor)] == ["send it?"], "the next step's"
    await h.answer(None)
    await h.settle()
    assert asker.answers == ["yes", None]
    assert h.bodies()[2:] == [
        Body.User("hi"),
        Body.Assistant([Part.Text("ok")], Stop.End),
        Body.User("more"),
        Body.Assistant([Part.Text("ok 2")], Stop.End),
    ]
