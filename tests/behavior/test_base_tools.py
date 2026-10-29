"""The base tools (Rust) through the Harness: the model calls `read`, then `bash`."""

import json
from pathlib import Path

from fakes import FakeProvider, Reply
from harness import Harness, scenario

from tau_next import Author, Body, Outcome, Part, Stop, base_tools


@scenario
async def test_the_model_reads_a_file_with_the_read_tool(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("violet-anchor-42\n")
    args = json.dumps({"path": "secret.txt"})
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "read", args)], Stop.Tools, finish_reason="tool_calls"),
            Reply([Part.Text("The phrase is violet-anchor-42")], finish_reason="stop"),
        ]
    )
    tools = base_tools(tmp_path, overflow_dir=tmp_path / "overflow")
    h = await Harness.create(tmp_path, provider=provider, tools=tools)
    await h.send("read it")
    await h.settle()
    *_, batch, results, final = h.bodies()
    assert batch == Body.Assistant([Part.Call("c1", "read", args)], Stop.Tools, "tool_calls")
    assert results == Body.Results([("c1", Outcome.Ok("violet-anchor-42\n"), Author.Tools())])
    assert final == Body.Assistant([Part.Text("The phrase is violet-anchor-42")], Stop.End, "stop")
    offered = [r.tool_names for r in provider.requests]
    assert offered == [["read", "write", "grep", "bash"]] * 2
    await h.close()


@scenario
async def test_a_failing_command_is_an_error_outcome_the_model_sees(tmp_path: Path) -> None:
    args = json.dumps({"command": "echo nope; exit 2"})
    provider = FakeProvider(
        [Reply([Part.Call("c1", "bash", args)], Stop.Tools), Reply([Part.Text("it failed")])]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=base_tools(tmp_path))
    await h.send("run it")
    await h.settle()
    results = h.bodies()[-2]
    assert results == Body.Results(
        [("c1", Outcome.Err("nope\n\ncommand exited with code 2"), Author.Tools())]
    )
    await h.close()
