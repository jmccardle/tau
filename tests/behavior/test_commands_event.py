"""The `Commands` event (D32 section 3): the command forms, sent whole first in the snapshot and
again whenever they change, a state event a slow client holds once and never counts against
it."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from harness import Harness, scenario

from tau import Commands, CursorView, Event, Labels, Notice, TreeHandle


async def hello(tree: TreeHandle) -> str:
    """Say hello."""
    return "hello"


async def bye(tree: TreeHandle) -> str:
    """Say bye."""
    return "bye"


class Greeter:
    name = "greeter"
    commands = (hello,)


class Leaver:
    name = "greeter"
    commands = (bye,)


@dataclass(frozen=True)
class SizeOptions:
    size: int = 1


@dataclass(frozen=True)
class OtherSizeOptions:
    size: int = 1


class Sized:
    name = "sized"
    options = SizeOptions


class OtherSized:
    name = "sized"
    options = OtherSizeOptions


class Announcer:
    """Tells clients about its commands once it has started."""

    name = "announcer"
    commands = (bye,)

    async def start(self, tree: TreeHandle) -> None:
        tree.tell("new commands")


def commands_heard(heard: list[Event]) -> list[Commands]:
    return [e for e in heard if isinstance(e, Commands)]


@scenario
async def test_the_snapshot_opens_with_commands_equal_to_commands(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    await h.settle()
    first = h.heard[0]
    assert isinstance(first, Commands)
    assert first.forms == await h.client.commands()
    assert isinstance(h.heard[-1], Labels), "Labels still ends the snapshot"
    await h.close()


@scenario
async def test_a_reload_to_other_forms_sends_one_commands_after_labels_before_its_notices(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    await h.settle()
    before = len(h.heard)
    await h.driver.reload([Announcer()])
    await h.settle()
    after = h.heard[before:]
    kinds = [type(e).__name__ for e in after if not isinstance(e, CursorView)]
    assert kinds == ["Commands", "Notice"]
    (sent,) = commands_heard(after)
    assert sent.forms == await h.client.commands()
    assert {f.name for f in sent.forms} == {"model", "bye"}
    assert after[-1] == Notice("new commands", None, "announcer", "info")
    await h.close()


@scenario
async def test_a_reload_to_equal_forms_sends_none(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    await h.settle()
    before = len(commands_heard(h.heard))
    await h.driver.reload([Greeter()])
    await h.settle()
    assert len(commands_heard(h.heard)) == before
    await h.close()


@scenario
async def test_a_reload_that_changes_only_an_options_type_sends_one(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Sized()])
    await h.settle()
    before = commands_heard(h.heard)
    await h.driver.reload([OtherSized()])
    await h.settle()
    after = commands_heard(h.heard)
    assert len(after) == len(before) + 1
    assert after[-1].forms == before[-1].forms, "the forms are equal; the options type is not"
    await h.close()


class Growing:
    """An extension whose commands change at run time (an MCP server listing tools)."""

    name = "growing"

    def __init__(self) -> None:
        self.commands: tuple[object, ...] = ()
        self.tree: TreeHandle | None = None

    async def start(self, tree: TreeHandle) -> None:
        self.tree = tree


@scenario
async def test_a_slow_client_holds_one_commands_however_many_changes_and_is_never_cut_off(
    tmp_path: Path,
) -> None:
    grows = Growing()
    h = await Harness.create(tmp_path, extensions=[grows])
    slow = await h.connect("slow", buffer=1)
    tree = grows.tree
    assert tree is not None
    for commands in ((hello,), (bye,), (hello, bye), (bye,)):
        grows.commands = commands
        tree.reassemble()
    await h.settle()
    heard: list[Event] = []
    while True:
        try:
            heard.append(await asyncio.wait_for(anext(slow), 0.2))
        except TimeoutError:
            break
    (sent,) = commands_heard(heard)  # the snapshot's, replaced in its slot by the latest
    assert {f.name for f in sent.forms} == {"model", "bye"}
    assert heard.index(sent) == 0, "first, where the snapshot put it"
    await slow.close()
    await h.close()
