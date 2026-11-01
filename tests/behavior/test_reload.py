"""`driver.reload(extensions)` (C2): the driver runs other extensions from the next job on, only
when no effect is out."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, say, scenario

from tau import (
    Body,
    Busy,
    CommandFailed,
    ConfigError,
    Cursor,
    CursorView,
    DefinitionError,
    Part,
    Stop,
    Subject,
)


@dataclass(frozen=True)
class OldOptions:
    size: int = 1


@dataclass(frozen=True)
class NewOptions:
    size: str = "big"


async def old_command(cursor: Cursor) -> str:
    """The old one."""
    return "old"


async def new_command(cursor: Cursor) -> str:
    """The new one."""
    return "new"


async def read_size(cursor: Cursor) -> str:
    """Say the size option here."""
    return str(cursor.options().size)


class Old:
    name = "thing"
    options = OldOptions
    commands = (old_command,)
    tools = (FakeTool("old_tool", "o"),)

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        self.stepped = True


class New:
    name = "thing"
    options = NewOptions
    commands = (new_command, read_size)
    tools = (FakeTool("new_tool", "n"),)


class Unrelated:
    name = "unrelated"


@scenario
async def test_after_a_reload_the_commands_options_tools_and_hooks_are_the_new_ones(
    tmp_path: Path,
) -> None:
    old = Old()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("a"), say("b")]),
        tools=[FakeTool("kept", "k")],
        extensions=[old],
    )
    assert {f.name for f in await h.client.commands()} == {"model", "old_command", "config"}
    await h.send("one")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["kept", "old_tool"]
    assert getattr(old, "stepped", False)

    new = New()
    h.driver.reload([new])
    assert {f.name for f in await h.client.commands()} == {
        "model",
        "new_command",
        "read_size",
        "config",
    }
    form = await h.client.options(h.cursor, "thing")
    assert form.schema["properties"]["size"]["default"] == "big"  # type: ignore[index]
    assert await h.run("new_command") == "new"
    with pytest.raises(LookupError):
        await h.run("old_command")
    await h.settle()
    await h.send("two")
    await h.settle()
    assert h.provider.requests[1].tool_names == ["kept", "new_tool"], "the driver's own stay"
    await h.close()


@scenario
async def test_a_reload_is_refused_while_an_effect_is_out_and_says_which(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("hi")], Stop.End, gate=gate)])
    )
    await h.send("go")
    await h.settle()
    with pytest.raises(Busy, match=rf"cursors \[{h.cursor}\]"):
        h.driver.reload([Unrelated()])
    gate.release()
    await h.settle()
    h.driver.reload([Unrelated()])
    await h.close()


@scenario
async def test_a_reload_is_refused_while_a_command_waits_for_its_step(tmp_path: Path) -> None:
    """The command has no effect out yet (it waits for its `pre_step`), but its cursor is busy
    and it would run with the set it was started under."""
    h = await Harness.create(tmp_path, extensions=[Old()], provider=FakeProvider([]))
    waiting = asyncio.ensure_future(h.client.configure(h.cursor, "thing", {"size": 7}))
    await asyncio.sleep(0)
    assert h.session.cursor(h.cursor).busy
    with pytest.raises(Busy, match=rf"cursors \[{h.cursor}\]"):
        h.driver.reload([New()])
    await waiting
    await h.settle()
    h.driver.reload([New()])
    await h.close()


async def shine(cursor: Cursor, text: str) -> None:
    """Show text."""
    cursor.show(text)


class Badge:
    name = "badge"
    commands = (shine,)


@scenario
async def test_a_reload_drops_what_the_extensions_it_removed_showed(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge(), Old()], provider=FakeProvider([]))
    await h.run("shine", {"text": "plan mode"})
    await h.settle()
    assert (await h.view()).shown == (("badge", "plan mode"),)
    h.driver.reload([Old()])
    await h.settle()
    assert (await h.view()).shown == ()
    last = [e for e in h.heard if isinstance(e, CursorView)][-1]
    assert last.shown == (), "and a client that was connected is told"
    await h.close()


@scenario
async def test_a_reload_keeps_what_the_extensions_it_keeps_showed(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge(), Old()], provider=FakeProvider([]))
    await h.run("shine", {"text": "plan mode"})
    await h.settle()
    h.driver.reload([Old(), Badge()])
    await h.settle()
    assert (await h.view()).shown == (("badge", "plan mode"),)
    await h.close()


@scenario
async def test_a_refused_reload_changes_nothing(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Old()], provider=FakeProvider([say("a")]))

    class Twice:
        name = "twice"
        commands = (old_command, old_command)

    with pytest.raises(DefinitionError, match="two commands are named 'old_command'"):
        h.driver.reload([New(), Twice()])
    assert {f.name for f in await h.client.commands()} == {"model", "old_command", "config"}
    await h.send("one")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["old_tool"]
    await h.close()


@scenario
async def test_a_config_node_an_extension_can_no_longer_read_fails_when_read(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Old()])
    await h.configure("thing", {"size": 7})
    await h.settle()
    nodes = len(h.session.tree)
    assert (await h.client.options(h.cursor, "thing")).schema["properties"]["size"]["default"] == 7  # type: ignore[index]

    h.driver.reload([Unrelated()])
    assert len(h.session.tree) == nodes, "the tree is not rewritten"
    config = h.session.tree.node(h.session.cursor(h.cursor).at).body
    assert isinstance(config, Body.Config)
    assert json.loads(config.config.data) == {"extensions": {"thing": {"size": 7}}}
    with pytest.raises(ConfigError, match="has no options"):
        await h.client.options(h.cursor, "thing")

    h.driver.reload([New()])  # the same name, another type: 7 is not a str
    with pytest.raises(CommandFailed, match=r"ConfigError: extension 'thing': .*size"):
        await h.run("read_size")
    await h.close()


@scenario
async def test_a_stopped_driver_cannot_reload(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    await h.driver.stop()
    with pytest.raises(ConnectionError, match="the driver is stopped"):
        h.driver.reload([])
    h.session.close()


@scenario
async def test_the_hooks_of_a_reloaded_extension_run_and_the_old_ones_do_not(
    tmp_path: Path,
) -> None:
    before, after = Recorder("before"), Recorder("after")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("a"), say("b")]), extensions=[before]
    )
    await h.send("one")
    await h.settle()
    h.driver.reload([after])
    await h.send("two")
    await h.settle()
    assert "pre_step" in before.points
    assert len(before.subjects("pre_step")) == 1
    assert len(after.subjects("pre_step")) == 1
    await h.close()
