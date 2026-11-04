"""A hook waits on another cursor and its own cursor is aborted (D24 M7): an abort never cancels
a hook, so its `rest` ends with `Ending.Aborted()` instead of waiting on."""

from pathlib import Path

from fakes import FakeProvider, Gate, Recorder, Reply
from harness import Harness, scenario

from tau import Cursor, Ending, ItemData, Part, Stop, Subject


class Waits:
    """At `pre_sample` of cursor 0, open a cursor beside it, send it a question, and wait."""

    name = "waits"

    def __init__(self) -> None:
        self.ending: Ending | str | None = "never returned"
        self.helper = -1

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        if cursor.id != 0 or self.helper >= 0:
            return
        self.helper = cursor.open(cursor.cursor(cursor.id).at)
        cursor.send(self.helper, "what do you think?")
        self.ending = await cursor.rest(self.helper)


@scenario
async def test_the_wait_ends_with_aborted_when_the_waiting_cursor_is_aborted(
    tmp_path: Path,
) -> None:
    gate = Gate()
    waits = Waits()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hmm")], Stop.End, gate=gate)]),
        extensions=[waits],
        gates=[gate],
    )
    await h.send("go")
    await h.until(lambda: gate.held == 1)  # the helper's sample is held
    await h.abort()
    await h.until(lambda: waits.ending != "never returned")
    assert waits.ending == Ending.Aborted()
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 1, "the aborted step sent no sample of its own"
    assert (await h.view()).ending == Ending.Aborted()
    await h.close()


@scenario
async def test_an_abort_of_another_cursor_is_that_cursors_ending_not_ours(
    tmp_path: Path,
) -> None:
    gate = Gate()
    waits = Waits()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hmm")], Stop.End, gate=gate)]),
        extensions=[waits],
        gates=[gate],
    )
    await h.send("go")
    await h.until(lambda: gate.held == 1)
    await h.abort(waits.helper)
    await h.until(lambda: waits.ending != "never returned")
    assert waits.ending == Ending.Aborted()  # the helper's own step ended aborted
    view = await h.view(0)
    assert view is not None
    gate.release()
    await h.close()


class Asks:
    """At `pre_sample`, ask the user something and wait for the answer."""

    name = "asks"

    def __init__(self) -> None:
        self.answer: object = "never answered"

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        self.answer = await cursor.ask("go on?", bool)


@scenario
async def test_an_abort_while_a_pre_sample_hook_asks_ends_the_step_without_a_sample(
    tmp_path: Path,
) -> None:
    asks = Asks()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("SAMPLED AFTER ABORT")])]),
        extensions=[asks],
    )
    await h.send("go")
    await h.until(lambda: asks.answer == "never answered" and bool(h.driver.state().open_questions))
    await h.abort()
    await h.settle()
    assert asks.answer is None, "the question was declined"
    assert h.provider.requests == [], "no sample was sent"
    assert (await h.view()).ending == Ending.Aborted()
    await h.close()


@scenario
async def test_an_abort_while_the_inputs_are_offered_leaves_the_rest_pending(
    tmp_path: Path,
) -> None:
    first, gate = Gate(), Gate()
    recorder = Recorder(gates={"pre_step": first, "pre_input": gate})
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("never")])]), extensions=[recorder]
    )
    await h.send("one")
    await h.until(lambda: first.held == 1)  # the step is held before it offers the inputs
    await h.send("two")
    first.release()
    await h.until(lambda: gate.held == 1)
    await h.abort()
    gate.release()
    await h.settle()
    view = await h.view()
    assert [i.data.text for i in view.items if isinstance(i.data, ItemData.Input)] == ["two"]
    assert len(recorder.subjects("pre_input")) == 1, "only the first input was offered"
    assert h.provider.requests == []
    await h.close()
