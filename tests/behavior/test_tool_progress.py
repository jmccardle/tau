"""A running tool's progress (D15): live events tagged with the call, never saved."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario

from tau_next import (
    Author,
    Body,
    EventStream,
    Live,
    Outcome,
    Part,
    Progress,
    Saved,
    Stop,
    ToolProgress,
    base_tools,
    tool,
)

CALL = Part.Call("c1", "work", "{}")


def calling(*tool_calls: Part) -> FakeProvider:
    return FakeProvider([Reply(list(tool_calls), Stop.Tools), Reply([Part.Text("done")], Stop.End)])


async def next_progress(stream: EventStream) -> ToolProgress:
    """The next `ToolProgress` the stream delivers, within a few seconds."""
    async with asyncio.timeout(5):
        async for event in stream:
            if isinstance(event, Live) and isinstance(event.delta, ToolProgress):
                return event.delta
    raise AssertionError("the stream ended")


@scenario
async def test_progress_arrives_live_tagged_with_the_call_before_its_result_is_saved(
    tmp_path: Path,
) -> None:
    gate = Gate()
    work = FakeTool("work", "finished", gate, shows=["one", "two"])
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[work])
    stream = h.driver.events()
    await h.send("go")
    await h.settle()  # the tool has shown both and waits at its gate
    assert await next_progress(stream) == ToolProgress("c1", "one")
    assert await next_progress(stream) == ToolProgress("c1", "two")
    assert not any(isinstance(b, Body.Results) for b in h.bodies()), "not saved yet"
    gate.release()
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("finished"), Author.Tools())])
    assert await anext(stream) == Saved(h.cursor)
    await stream.aclose()
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


@tool
def counting(n: int, progress: Progress) -> str:
    """Count to `n`, showing each number."""
    for i in range(n):
        progress(f"{i} ")
    return "counted"


@scenario
async def test_a_tool_function_takes_progress_without_the_model_knowing(tmp_path: Path) -> None:
    provider = calling(Part.Call("c1", "counting", json.dumps({"n": 3})))
    h = await Harness.create(tmp_path, provider=provider, tools=[counting])
    stream = h.driver.events()
    await h.send("go")
    seen = ""
    while seen.count(" ") < 3:
        seen += (await next_progress(stream)).text
    assert seen == "0 1 2 "
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("counted"), Author.Tools())])
    assert "progress" not in provider.requests[0].tools[0].parameters["properties"]  # type: ignore[index]
    await stream.aclose()
    await h.close()


@scenario
@pytest.mark.parametrize("how", ["finished", "aborted"])
async def test_progress_after_the_call_is_over_does_nothing(tmp_path: Path, how: str) -> None:
    gate = Gate()
    work = FakeTool("work", "finished", gate)
    h = await Harness.create(tmp_path, provider=calling(CALL), tools=[work])
    stream = h.driver.events()
    await h.send("go")
    await h.settle()
    assert work.progress is not None
    work.progress("while running")
    assert await next_progress(stream) == ToolProgress("c1", "while running")
    if how == "finished":
        gate.release()
    else:
        await h.abort()
    await h.settle()
    while (await anext(stream)) != Saved(h.cursor):
        pass
    work.progress("late")  # no error
    await h.settle()
    await h.close()
    assert [e async for e in stream] == [], "nothing after the end of the call"


@scenario
async def test_bash_shows_its_output_as_it_arrives(tmp_path: Path) -> None:
    command = "echo a; sleep 1; echo b"
    provider = calling(Part.Call("c1", "bash", json.dumps({"command": command})))
    h = await Harness.create(
        tmp_path, provider=provider, tools=base_tools(tmp_path, overflow_dir=tmp_path / "o")
    )
    stream = h.driver.events()
    await h.send("go")
    assert await next_progress(stream) == ToolProgress("c1", "a\n")
    assert not any(isinstance(b, Body.Results) for b in h.bodies()), "b is not out yet"
    assert await next_progress(stream) == ToolProgress("c1", "b\n")
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("a\nb\n"), Author.Tools())])
    await stream.aclose()
    await h.close()
