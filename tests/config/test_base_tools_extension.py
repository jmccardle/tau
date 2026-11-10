"""`base_tools`, the built-in extension that gives a session the base tools: loaded like
`step_limit`, off with `load` (not by `--no-extensions`), and rooted in the working directory
the process was in when it loaded. Through `tau.load_extensions` and `tau ext` on real files."""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from world import World, loaded, tau

from tau import Outcome, Tool, list_extensions, load_extensions

TOOLS = ["read", "write", "edit", "grep", "find", "bash"]


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    before = os.getcwd()
    yield
    os.chdir(before)


def row(world: World, *argv: str) -> dict[str, object]:
    result = tau(world, "ext", "list", "--json", *argv)
    assert result.code == 0, result.err
    found = {r["key"]: r for r in map(json.loads, result.out.splitlines())}
    return found["base_tools"]


def test_it_is_a_built_in_that_is_on_and_lists_the_six_tools(world: World) -> None:
    found = row(world)
    assert (found["kind"], found["state"]) == ("built-in", "on")
    assert found["extensions"] == ["base_tools"]
    assert found["tools"] == TOOLS
    assert found["commands"] == []


def test_it_loads_before_step_limit(world: World) -> None:
    names = [e.name for e in load_extensions(loaded(world))]
    assert names == [
        "attachments",
        "base_tools",
        "compaction",
        "permissions",
        "prune",
        "skills",
        "step_limit",
        "tokens",
        "usage",
    ]


def test_load_false_turns_it_off_and_the_row_says_which_file_did(world: World) -> None:
    path = world.user_config({"load": {"base_tools": False}})
    assert [e.name for e in load_extensions(loaded(world))] == [
        "attachments",
        "compaction",
        "permissions",
        "prune",
        "skills",
        "step_limit",
        "tokens",
        "usage",
    ]
    found = row(world)
    assert found["state"] == "off"
    assert found["detail"] == f"load.base_tools = false in {path}"
    assert found["tools"] == []


def test_no_extensions_leaves_it_on_and_load_false_still_turns_it_off(world: World) -> None:
    config = loaded(world)
    assert [e.name for e in load_extensions(config, discover=False)] == [
        "attachments",
        "base_tools",
        "compaction",
        "permissions",
        "prune",
        "skills",
        "step_limit",
        "tokens",
        "usage",
    ]
    assert row(world, "--no-extensions")["state"] == "on"
    world.user_config({"load": {"base_tools": False}})
    off = row(world, "--no-extensions")
    assert off["state"] == "off"
    assert str(off["detail"]).startswith("load.base_tools = false in ")
    assert row(world, "--no-extensions", "-e", "base_tools")["state"] == "on"


def test_ext_disable_writes_the_switch(world: World) -> None:
    result = tau(world, "ext", "disable", "base_tools")
    assert result.code == 0, result.err
    assert json.loads((world.tau / "config.json").read_text()) == {"load": {"base_tools": False}}
    assert row(world)["state"] == "off"


def test_the_tools_work_in_the_directory_the_process_was_in_when_they_loaded(
    world: World, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("in the project\n")
    os.chdir(project)
    (base,) = [e for e in load_extensions(loaded(world)) if e.name == "base_tools"]
    os.chdir(tmp_path)
    tools: dict[str, Tool] = {t.name: t for t in getattr(base, "tools")}  # noqa: B009
    assert list(tools) == TOOLS
    outcome = asyncio.run(tools["read"].call(json.dumps({"path": "note.txt"})))
    assert outcome == Outcome.Ok("in the project\n")


def test_a_drop_in_with_the_same_key_shadows_it(world: World) -> None:
    (world.tau / "extensions").mkdir(parents=True)
    (world.tau / "extensions" / "base_tools.py").write_text(
        'name = "base_tools"\n', encoding="utf-8"
    )
    sources = [s for s in list_extensions(world.home, world.environ) if s.key == "base_tools"]
    assert [(s.kind, s.state, s.tools) for s in sources] == [
        ("built-in", "shadowed", ()),
        ("user", "on", ()),
    ]
