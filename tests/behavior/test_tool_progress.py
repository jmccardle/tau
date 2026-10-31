"""A running tool's progress (D15): live events tagged with the call, never saved."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario

from tau_next import (
    Appended,
    Author,
    Body,
    CallDraft,
    Client,
    CursorView,
    Event,
    Labels,
    Live,
    Node,
    NodeHandle,
    Outcome,
    Part,
    Progress,
    Stop,
    ToolProgress,
    base_tools,
)

CALL = Part.Call("c1", "work", "{}")
BATCH = 3  # the Assistant node holding the calls: Config 0, System 1, User 2, Assistant 3


def calling(*tool_calls: Part) -> FakeProvider:
    return FakeProvider([Reply(list(tool_calls), Stop.Tools), Reply([Part.Text("done")], Stop.End)])


async def next_progress(stream: Client) -> ToolProgress:
    """The next `ToolProgress` the stream delivers, within a few seconds."""
    async with asyncio.timeout(5):
        async for event in stream:
            if isinstance(event, Live) and isinstance(event.delta, ToolProgress):
                return event.delta
    raise AssertionError("the stream ended")


async def next_results(stream: Client) -> Node:
    """The next appended `Results` node the stream delivers, within a few seconds."""
    async with asyncio.timeout(5):
        async for event in stream:
            if isinstance(event, Appended):
                for node in event.nodes:
                    if isinstance(node.body, Body.Results):
                        return node
    raise AssertionError("the stream ended")


@scenario
async def test_progress_arrives_live_tagged_with_the_call_before_its_result_is_saved(
    tmp_path: Path,
) -> None:
    gate = Gate()
    work = FakeTool("work", "finished", gate, shows=["one", "two"])
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[work])
    stream = await h.connect("watcher", watching=[h.cursor])
    await h.send("go")
    await h.settle()  # the tool has shown both and waits at its gate
    assert await next_progress(stream) == ToolProgress(BATCH, "c1", "one")
    assert await next_progress(stream) == ToolProgress(BATCH, "c1", "two")
    assert not any(isinstance(b, Body.Results) for b in h.bodies()), "not saved yet"
    gate.release()
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("finished"), Author.Tools())])
    results = await next_results(stream)
    assert results.body == Body.Results([("c1", Outcome.Ok("finished"), Author.Tools())])
    assert results.parent == BATCH, "the Results node is how a client learns the layer is over"
    await stream.close()
    await h.close()


@scenario
async def test_progress_text_is_in_no_saved_node(tmp_path: Path) -> None:
    work = FakeTool("work", "finished", shows=["secret-progress"])
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[work])
    await h.send("go")
    await h.settle()
    assert "secret-progress" not in repr(h.snapshot())
    await h.close()


class Plain:
    """A tool written before progress: `call(args_raw)` and nothing else."""

    name = "plain"
    description = "No progress."
    parameters: Mapping[str, object] = {"type": "object", "properties": {}}

    async def call(self, args_raw: str) -> Outcome:
        return Outcome.Ok("plain result")


@scenario
async def test_a_tool_that_takes_no_progress_is_called_as_before(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=calling(Part.Call("c1", "plain", "{}")), tools=[Plain()]
    )
    await h.send("go")
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("plain result"), Author.Tools())])
    await h.close()


def counting(n: int, progress: Progress) -> str:
    """Count to `n`, showing each number."""
    for i in range(n):
        progress(f"{i} ")
    return "counted"


@scenario
async def test_a_tool_function_takes_progress_without_the_model_knowing(tmp_path: Path) -> None:
    provider = calling(Part.Call("c1", "counting", json.dumps({"n": 3})))
    h = await Harness.create(tmp_path, provider=provider, tools=[counting])
    stream = await h.connect("watcher", watching=[h.cursor])
    await h.send("go")
    seen = ""
    while seen.count(" ") < 3:
        seen += (await next_progress(stream)).text
    assert seen == "0 1 2 "
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("counted"), Author.Tools())])
    assert "progress" not in provider.requests[0].tools[0].parameters["properties"]  # type: ignore[index]
    await stream.close()
    await h.close()


@scenario
@pytest.mark.parametrize("how", ["finished", "aborted"])
async def test_progress_after_the_call_is_over_does_nothing(tmp_path: Path, how: str) -> None:
    gate = Gate()
    work = FakeTool("work", "finished", gate)
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[work])
    stream = await h.connect("watcher", watching=[h.cursor])
    await h.send("go")
    await h.settle()
    assert work.progress is not None
    work.progress("while running")
    assert await next_progress(stream) == ToolProgress(BATCH, "c1", "while running")
    if how == "finished":
        gate.release()
    else:
        await h.abort()
    await h.settle()
    while not isinstance(await anext(stream), Appended):
        pass
    work.progress("late")  # no error
    await h.settle()
    await h.close()
    assert not [e async for e in stream if isinstance(e, Live)], "nothing live after the call"


@scenario
async def test_bash_shows_its_output_as_it_arrives(tmp_path: Path) -> None:
    command = "echo a; sleep 1; echo b"
    provider = calling(Part.Call("c1", "bash", json.dumps({"command": command})))
    h = await Harness.create(
        tmp_path, provider=provider, tools=base_tools(tmp_path, overflow_dir=tmp_path / "o")
    )
    stream = await h.connect("watcher", watching=[h.cursor])
    await h.send("go")
    assert await next_progress(stream) == ToolProgress(BATCH, "c1", "a\n")
    assert not any(isinstance(b, Body.Results) for b in h.bodies()), "b is not out yet"
    assert await next_progress(stream) == ToolProgress(BATCH, "c1", "b\n")
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("a\nb\n"), Author.Tools())])
    await stream.close()
    await h.close()


FAST = Part.Call("c2", "fast", "{}")


def two_calls() -> tuple[FakeProvider, FakeTool, FakeTool, Gate, Gate]:
    slow_gate, fast_gate = Gate(), Gate()
    slow = FakeTool("work", "slow done", slow_gate, shows=["slow-1", "slow-2"])
    fast = FakeTool("fast", "fast done", fast_gate)
    return calling(CALL, FAST), slow, fast, slow_gate, fast_gate


@scenario
async def test_a_call_answered_before_its_batch_is_whole_changes_nothing_to_hear(
    tmp_path: Path,
) -> None:
    provider, slow, fast, slow_gate, fast_gate = two_calls()
    h = await Harness.create(tmp_path, provider=provider, tools=[slow, fast])
    stream = await h.connect("watcher", watching=[h.cursor])
    seen: list[Event] = []

    async def collect() -> None:
        async for event in stream:
            seen.append(event)

    collecting = asyncio.ensure_future(collect())
    await h.send("go")
    await h.settle()
    for _ in range(3):
        await asyncio.sleep(0)  # the collector catches up
    before = list(seen)
    fast_gate.release()
    await h.settle()
    await asyncio.sleep(0)
    assert seen == before, "the fast call's answer changed no node, so there is nothing to say"
    slow_gate.release()
    await h.settle()
    appended = [n for e in seen[len(before) :] if isinstance(e, Appended) for n in e.nodes]
    assert next(n.body for n in appended) == Body.Results(
        [
            ("c1", Outcome.Ok("slow done"), Author.Tools()),
            ("c2", Outcome.Ok("fast done"), Author.Tools()),
        ]
    ), "the Results node answers both calls"
    await h.close()
    await collecting


@scenario
async def test_progress_flooding_a_full_buffer_is_dropped_and_state_does_not_grow_it(
    tmp_path: Path,
) -> None:
    gate = Gate()
    flood = FakeTool("work", "done", gate, shows=[f"line {i}" for i in range(50)])
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[flood])
    stream = await h.connect("watcher", buffer=4, watching=[h.cursor])
    await h.send("go")
    await h.settle()
    for _ in range(3):
        await h.set_auto(False)  # state changes while the buffer holds progress
    await h.close()
    left = [e async for e in stream]
    lives = [e for e in left if isinstance(e, Live)]
    first = [Live(h.cursor, ToolProgress(BATCH, "c1", f"line {i}")) for i in range(50)]
    assert lives, "some progress is kept"
    assert lives == first[: len(lives)], "the oldest are kept, the later ones dropped"
    assert len(lives) <= 4
    assert len([e for e in left if isinstance(e, CursorView)]) == 1, "state replaces itself"


class Writer:
    """An extension whose `pre_result` changes the tree in the way `write` says, on call `c2`."""

    name = "writer"

    def __init__(self, write: str) -> None:
        self.write = write

    async def pre_result(self, node: NodeHandle, call: CallDraft) -> None:
        if call.id != "c2":
            return
        match self.write:
            case "append":
                node.append(node.id, Body.Custom("note", "from a hook"))
            case "label":
                node.set_label("mark", node.cursors()[0])
            case "item":
                node.add_item(node.cursors()[0], "from a hook")


@scenario
@pytest.mark.parametrize("write", ["append", "label", "item"])
async def test_a_hooks_write_mid_batch_is_announced_when_it_happens(
    tmp_path: Path, write: str
) -> None:
    provider, slow, fast, slow_gate, fast_gate = two_calls()
    h = await Harness.create(
        tmp_path, provider=provider, tools=[slow, fast], extensions=[Writer(write)]
    )
    stream = await h.connect("watcher")
    seen: list[Event] = []

    async def collect() -> None:
        async for event in stream:
            seen.append(event)

    collecting = asyncio.ensure_future(collect())
    await h.send("go")
    await h.settle()
    for _ in range(3):
        await asyncio.sleep(0)
    before = len(seen)
    fast_gate.release()  # the fast call answers, its `pre_result` hook writes; the slow one runs on
    await h.settle()
    for _ in range(3):
        await asyncio.sleep(0)
    heard = {"append": Appended, "label": Labels, "item": CursorView}[write]
    assert [type(e) for e in seen[before:]] == [heard], "announced at once, not when the batch ends"
    slow_gate.release()
    await h.close()
    await collecting


@scenario
async def test_a_provider_cannot_yield_tool_progress(tmp_path: Path) -> None:
    sneaky = ToolProgress(BATCH, "c1", "from a provider")
    provider = FakeProvider([Reply([sneaky])])  # pyright: ignore[reportArgumentType]
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("go")
    await h.settle()
    errors = [
        n.body
        for n in h.nodes()
        if isinstance(n.body, Body.Custom) and n.body.kind == "sample_error"
    ]
    assert errors, "the sample failed"
    assert "ToolProgress" in errors[0].data
    await h.close()
