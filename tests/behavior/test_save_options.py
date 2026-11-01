"""τ writes its own config (D1): `cursor.save_options`, and `/config ... --save`.

A `Config` node means this branch; the user's file means every session. Both are checked by the
extension's options type before anything is written.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import (
    Body,
    CallDraft,
    CommandFailed,
    ConfigError,
    Cursor,
    NodeHandle,
    Part,
    Stop,
    Subject,
    UserConfig,
)
from tau.commandline import parse


def remember(cursor: Cursor, size: int) -> None:
    """Save the size for every session."""
    cursor.save_options(size=size)


@dataclass(frozen=True)
class Options:
    size: Annotated[int, "how big"] = 10
    mode: Literal["fast", "slow"] = "fast"

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError(f"a size is at least 1, not {self.size}")


class Tuner:
    """Reads its options at every step and remembers what it saw; `remember` saves a size."""

    name = "tuner"
    options = Options
    commands = (remember,)

    def __init__(self) -> None:
        self.seen: list[Options] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self.seen.append(cursor.options())
        return None

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        node.save_options(mode="slow")
        return None


class Plain:
    name = "plain"
    commands = (remember,)


def home(tmp_path: Path) -> UserConfig:
    return UserConfig(tmp_path / "tau-home")


def saved(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "tau-home" / "config.json").read_text(encoding="utf-8"))


@scenario
async def test_save_options_writes_the_users_file_and_a_running_driver_sees_it(
    tmp_path: Path,
) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner], user_config=home(tmp_path))
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    before = len(h.nodes())
    await h.run("remember", {"size": 4})
    await h.settle()
    assert saved(tmp_path) == {"extensions": {"tuner": {"size": 4}}}
    assert len(h.nodes()) == before, "the file is every session, not a node of this branch"
    await h.step(cursor=other)
    await h.settle()
    assert tuner.seen[-1].size == 4, "use_catalog: the driver reads the file's value at once"


@scenario
async def test_save_options_keeps_what_else_the_file_holds(tmp_path: Path) -> None:
    config = tmp_path / "tau-home" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "default_model": "m",
                "models": {"m": {"model": "m", "provider": "fake"}},
                "extensions": {"tuner": {"mode": "slow"}, "step_limit": {"limit": 3}},
            }
        ),
        encoding="utf-8",
    )
    h = await Harness.create(tmp_path, extensions=[Tuner()], user_config=home(tmp_path))
    await h.run("remember", {"size": 4})
    assert saved(tmp_path) == {
        "default_model": "m",
        "models": {"m": {"model": "m", "provider": "fake"}},
        "extensions": {"tuner": {"mode": "slow", "size": 4}, "step_limit": {"limit": 3}},
    }
    assert [p.name for p in config.parent.iterdir()] == ["config.json"], "no scratch file is left"


@scenario
async def test_save_options_that_the_options_type_refuses_leave_the_file_alone(
    tmp_path: Path,
) -> None:
    config = tmp_path / "tau-home" / "config.json"
    config.parent.mkdir()
    config.write_text('{"extensions": {"tuner": {"size": 2}}}', encoding="utf-8")
    h = await Harness.create(tmp_path, extensions=[Tuner()], user_config=home(tmp_path))
    with pytest.raises(CommandFailed, match="ConfigError: extension 'tuner': a size is at least 1"):
        await h.run("remember", {"size": 0})
    assert config.read_text(encoding="utf-8") == '{"extensions": {"tuner": {"size": 2}}}'
    assert [p.name for p in config.parent.iterdir()] == ["config.json"]


@scenario
async def test_save_options_without_a_user_file_to_write_fails_the_command(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()])
    with pytest.raises(CommandFailed, match="ConfigError: this driver has no user config"):
        await h.run("remember", {"size": 4})


@scenario
async def test_save_options_of_an_extension_with_none_fails_the_command(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Plain()], user_config=home(tmp_path))
    with pytest.raises(CommandFailed, match="ConfigError: extension 'plain' has no options"):
        await h.run("remember", {"size": 4})
    assert not (tmp_path / "tau-home").exists()


@scenario
async def test_a_tool_hook_saves_its_options_through_its_node_handle(tmp_path: Path) -> None:
    call = Reply([Part.Call("a", "noop", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, Reply([Part.Text("done")])]),
        tools=[FakeTool("noop", "out")],
        extensions=[Tuner()],
        user_config=home(tmp_path),
    )
    await h.send("go")
    await h.settle()
    assert saved(tmp_path) == {"extensions": {"tuner": {"mode": "slow"}}}


# /config <extension> name=value --save


@scenario
async def test_config_save_changes_the_branch_and_the_file(tmp_path: Path) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner], user_config=home(tmp_path))
    forms = await h.client.commands()
    options = {"tuner": await h.client.options(h.cursor, "tuner")}
    parsed = parse("/config tuner size=7 --save", forms, options)
    assert (parsed.extension, parsed.args, parsed.save) == ("tuner", {"size": 7}, True)
    await parsed.run(h.client, h.cursor)
    await h.settle()
    node = h.session.tree.node(h.session.cursor(h.cursor).at)
    assert isinstance(node.body, Body.Config)
    assert node.body.config.data == '{"extensions": {"tuner": {"size": 7}}}'
    assert saved(tmp_path) == {"extensions": {"tuner": {"size": 7}}}


@scenario
async def test_config_without_save_writes_no_file(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()], user_config=home(tmp_path))
    await h.configure("tuner", {"size": 7})
    assert not (tmp_path / "tau-home").exists()


@scenario
async def test_config_save_that_does_not_fit_writes_neither(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()], user_config=home(tmp_path))
    before = len(h.nodes())
    with pytest.raises(ConfigError, match="a size is at least 1"):
        await h.configure("tuner", {"size": 0}, save=True)
    assert len(h.nodes()) == before
    assert not (tmp_path / "tau-home").exists()


@scenario
async def test_config_save_without_a_user_file_writes_no_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()])
    before = len(h.nodes())
    with pytest.raises(ConfigError, match="this driver has no user config"):
        await h.configure("tuner", {"size": 5}, save=True)
    assert len(h.nodes()) == before


@scenario
async def test_save_reads_on_a_command_line_as_a_flag_after_the_extension(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()])
    forms = await h.client.commands()
    options = {"tuner": await h.client.options(h.cursor, "tuner")}
    first = parse("/config tuner --save mode=slow", forms, options)
    assert (first.args, first.save) == ({"mode": "slow"}, True)
    assert parse("/config tuner mode=slow", forms, options).save is False
    with pytest.raises(ValueError, match="--save needs a name=value to save"):
        parse("/config tuner --save", forms, options)


class Recalls:
    """Reads what the user's file itself says of its options, from a cursor and from a tool."""

    name = "tuner"
    options = Options

    def __init__(self) -> None:
        self.saved: list[object] = []
        self.commands = (self.recall,)

    def recall(self, cursor: Cursor) -> None:
        """Read the saved options."""
        self.saved.append(cursor.saved_options())

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        self.saved.append(node.saved_options())
        return None


@scenario
async def test_saved_options_are_what_the_users_file_alone_holds(tmp_path: Path) -> None:
    config = tmp_path / "tau-home" / "config.json"
    config.parent.mkdir()
    config.write_text('{"extensions": {"tuner": {"mode": "slow"}}}', encoding="utf-8")
    ext = Recalls()
    layers = {"extensions": {"tuner": {"size": 3}}}
    user = UserConfig(tmp_path / "tau-home", cli=layers)
    h = await Harness.create(tmp_path, extensions=[ext], user_config=user)
    await h.run("recall")
    await h.settle()
    assert ext.saved == [{"mode": "slow"}], "not the cli layer's size"
    await h.configure("tuner", {"size": 5})  # a Config node on this branch
    await h.settle()
    await h.run("recall")
    assert ext.saved[-1] == {"mode": "slow"}, "not a node's size either"
    await h.close()


@scenario
async def test_saved_options_are_empty_when_there_is_no_file(tmp_path: Path) -> None:
    ext = Recalls()
    h = await Harness.create(tmp_path, extensions=[ext], user_config=home(tmp_path))
    await h.run("recall")
    assert ext.saved == [{}], "no file is no options"
    await h.close()


@scenario
async def test_saved_options_without_a_user_file_to_read_fail_the_command(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Recalls()])
    with pytest.raises(CommandFailed, match="ConfigError: this driver has no user config"):
        await h.run("recall")
    await h.close()


@scenario
async def test_a_tool_hook_reads_saved_options_too(tmp_path: Path) -> None:
    config = tmp_path / "tau-home" / "config.json"
    config.parent.mkdir()
    config.write_text('{"extensions": {"tuner": {"mode": "slow"}}}', encoding="utf-8")
    ext = Recalls()
    call = Reply([Part.Call("c", "t", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, Reply([Part.Text("ok")])]),
        tools=[FakeTool("t", "x")],
        extensions=[ext],
        user_config=home(tmp_path),
    )
    await h.send("go")
    await h.settle()
    assert ext.saved == [{"mode": "slow"}]
    await h.close()
