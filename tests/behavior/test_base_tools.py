"""The base tools (Rust) through the Harness: the model calls `read`, then `bash`."""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fakes import FakeProvider, Reply
from harness import Harness, scenario
from world import World, models, trust

from tau import Author, Body, Outcome, Part, Stop, base_tools, load_config, load_extensions

BASH_GAP = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows: the bash tool is not ported (README)"
)
# The tools the model is offered: `bash` is not one of them on Windows.
OFFERED = ["read", "write", "edit", "grep", "find", *([] if sys.platform == "win32" else ["bash"])]


@scenario
async def test_the_model_reads_a_file_with_the_read_tool(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_bytes(b"violet-anchor-42\n")
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
    assert offered == [OFFERED] * 2
    await h.close()


@scenario
@BASH_GAP
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
@BASH_GAP
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


@contextmanager
def in_directory(path: Path) -> Iterator[None]:
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


@scenario
async def test_the_base_tools_extension_gives_a_session_the_model_can_edit_and_find_with(
    tmp_path: Path, world: World
) -> None:
    """Loaded as the shared start loads it, in the working directory the session starts in."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_bytes(b"one\ntwo\n")
    (work / "sub").mkdir()
    (work / "sub" / "b.txt").write_bytes(b"two\n")
    edit = json.dumps({"path": "a.txt", "old_string": "two", "new_string": "2"})
    find = json.dumps({"pattern": "*.txt"})
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "edit", edit), Part.Call("c2", "find", find)], Stop.Tools),
            Reply([Part.Text("done")]),
        ]
    )
    with in_directory(work):
        extensions = load_extensions(load_config(work, world.environ, ask=None))
    h = await Harness.create(tmp_path, provider=provider, extensions=extensions)
    await h.send("go")
    await h.settle()
    assert provider.requests[0].tool_names == OFFERED
    diff = "Replaced 1 occurrence in a.txt\n--- a.txt\n+++ a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+2"
    assert h.bodies()[-2] == Body.Results(
        [
            ("c1", Outcome.Ok(diff), Author.Tools()),
            ("c2", Outcome.Ok("a.txt\nsub/b.txt"), Author.Tools()),
        ]
    )
    assert (work / "a.txt").read_text() == "one\n2\n"
    await h.close()


@scenario
async def test_with_load_false_the_model_is_offered_none_of_them(
    tmp_path: Path, world: World
) -> None:
    world.user_config({"load": {"base_tools": False}})
    provider = FakeProvider([Reply([Part.Text("hi")])])
    extensions = load_extensions(load_config(tmp_path, world.environ, ask=None))
    h = await Harness.create(tmp_path, provider=provider, extensions=extensions)
    await h.send("go")
    await h.settle()
    assert provider.requests[0].tool_names == []
    await h.close()


@scenario
async def test_with_no_extensions_the_model_is_still_offered_them(
    tmp_path: Path, world: World
) -> None:
    provider = FakeProvider([Reply([Part.Text("hi")])])
    config = load_config(tmp_path, world.environ, ask=None)
    h = await Harness.create(
        tmp_path, provider=provider, extensions=load_extensions(config, discover=False)
    )
    await h.send("go")
    await h.settle()
    assert provider.requests[0].tool_names == OFFERED
    await h.close()
