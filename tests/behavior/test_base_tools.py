"""The base tools (Rust) through the Harness: the model calls `read`, then `bash`."""

import json
import os
from pathlib import Path

from fakes import FakeProvider, Reply
from harness import Harness, scenario
from world import World, models, trust

from tau import Author, Body, Outcome, Part, Stop, base_tools, load_config


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


@scenario
async def test_a_project_dotenv_never_reaches_the_environment_of_a_command(
    tmp_path: Path, world: World
) -> None:
    """Loading config returns the `.env` keys in a mapping of their own; nothing writes
    `os.environ`, which is what a `bash` command inherits."""
    root = world.project(config=models(p={}))
    (root / ".env").write_text("TAU_DOTENV_MARKER=from-dotenv\n", encoding="utf-8")
    trust(world, root)  # after the .env: approval pins its bytes
    before = dict(os.environ)
    loaded = load_config(root, world.environ, ask=None)
    assert loaded.environ["TAU_DOTENV_MARKER"] == "from-dotenv"
    args = json.dumps({"command": 'echo "marker=[$TAU_DOTENV_MARKER]"'})
    provider = FakeProvider(
        [Reply([Part.Call("c1", "bash", args)], Stop.Tools), Reply([Part.Text("seen")])]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=base_tools(tmp_path))
    await h.send("print it")
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("c1", Outcome.Ok("marker=[]\n"), Author.Tools())])
    await h.close()
    assert dict(os.environ) == before
