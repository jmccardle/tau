"""An extension's `start` and `stop` (D32 section 4): when they run, the tree handle `start` is
given and how long it lives, `spawn`, `reassemble`, the timeouts, and a reload, which stops the
old extensions first."""

import asyncio
import dataclasses
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply, fake_catalog
from harness import Harness, build, say, scenario

from tau import (
    Body,
    Busy,
    Commands,
    ConfigError,
    Cursor,
    DefinitionError,
    ExtensionLoadError,
    ModelCatalog,
    Notice,
    NotInHook,
    Part,
    Stop,
    Subject,
    TreeHandle,
)


class Recorded:
    """An extension that records its lifecycle in `log`, shared between instances."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.tree: TreeHandle | None = None

    async def start(self, tree: TreeHandle) -> None:
        self.log.append(f"start {self.name}")
        self.tree = tree

    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        self.log.append(f"pre_step {self.name}")


class Failing(Recorded):
    async def start(self, tree: TreeHandle) -> None:
        self.log.append(f"start {self.name}")
        raise RuntimeError("no server")


class Slow(Recorded):
    async def start(self, tree: TreeHandle) -> None:
        self.log.append(f"start {self.name}")
        await asyncio.sleep(30)


class BadStop(Recorded):
    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")
        raise RuntimeError("stuck")


def catalog_with(**timeouts: tuple[float | None, float | None]) -> ModelCatalog:
    return dataclasses.replace(fake_catalog(), timeouts=timeouts)


@scenario
async def test_start_runs_once_per_extension_in_load_order_before_any_hook(tmp_path: Path) -> None:
    log: list[str] = []
    a, b = Recorded("a", log), Recorded("b", log)
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[a, b])
    assert log == ["start a", "start b"]
    assert a.tree is not None
    assert a.tree.extension == "a"
    await h.send("go")
    await h.settle()
    assert log == ["start a", "start b", "pre_step a", "pre_step b"]
    await h.driver.stop()
    assert log[-2:] == ["stop b", "stop a"]
    await h.close()


@scenario
async def test_a_start_that_raises_stops_the_earlier_ones_and_names_it(tmp_path: Path) -> None:
    log: list[str] = []
    extensions = [Recorded("a", log), Failing("bad", log), Recorded("c", log)]
    with pytest.raises(ExtensionLoadError, match=r"extension 'bad' did not start: RuntimeError"):
        await Harness.create(tmp_path, extensions=extensions)
    assert log == ["start a", "start bad", "stop a"], "c never starts, bad's stop never runs"


@scenario
async def test_a_start_that_outlives_its_configured_timeout_fails(tmp_path: Path) -> None:
    log: list[str] = []
    with pytest.raises(ExtensionLoadError, match=r"'slow' did not start: TimeoutError.*0.05 s"):
        await Harness.create(
            tmp_path, extensions=[Slow("slow", log)], catalog=catalog_with(slow=(0.05, None))
        )


@scenario
async def test_rest_inside_start_is_refused(tmp_path: Path) -> None:
    class Waits:
        name = "waits"

        async def start(self, tree: TreeHandle) -> None:
            await tree.rest(tree.cursors()[0])

    with pytest.raises(ExtensionLoadError, match=r"ValueError: rest: the driver has not started"):
        await Harness.create(tmp_path, extensions=[Waits()])


@scenario
async def test_start_and_stop_must_be_coroutine_functions() -> None:
    class Plain:
        name = "plain"

        def start(self, tree: TreeHandle) -> None:
            pass

    with pytest.raises(DefinitionError, match=r"extension 'plain': `start` must be an async"):
        build([Plain()])


@scenario
async def test_the_handle_works_from_a_task_after_start_and_is_retired_after_stop(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    ext = Recorded("bot", log)
    h = await Harness.create(tmp_path, provider=FakeProvider([say("hello")]), extensions=[ext])
    tree = ext.tree
    assert tree is not None

    async def later() -> None:
        tree.tell("ready", level="warning")
        tree.send(h.cursor, "from the bot")

    await asyncio.create_task(later())
    await h.settle()
    user = [b for b in h.bodies() if isinstance(b, Body.User)]
    assert [u.text for u in user] == ["from the bot"]
    assert Notice("ready", None, "bot", "warning") in h.notice_events()
    opened = tree.open(0)
    assert opened in tree.cursors()
    await h.driver.stop()
    with pytest.raises(NotInHook, match="extension 'bot' is stopped"):
        tree.add_item(h.cursor, "late")
    await h.close()


@scenario
async def test_tell_on_a_tree_handle_names_its_cursor_and_refuses_a_bad_level(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    ext = Recorded("bot", log)
    h = await Harness.create(tmp_path, extensions=[ext])
    assert ext.tree is not None
    ext.tree.tell("about it", cursor=h.cursor)
    with pytest.raises(ValueError, match="level"):
        ext.tree.tell("loud", level="shout")  # pyright: ignore[reportArgumentType]
    with pytest.raises(LookupError):
        ext.tree.tell("nowhere", cursor=99)
    await h.settle()
    assert h.notice_events() == [Notice("about it", h.cursor, "bot", "info")]
    await h.close()


@scenario
async def test_a_stop_that_raises_does_not_stop_the_rest(tmp_path: Path) -> None:
    log: list[str] = []
    h = await Harness.create(tmp_path, extensions=[Recorded("a", log), BadStop("b", log)])
    await h.driver.stop()
    assert log == ["start a", "start b", "stop b", "stop a"]
    await h.close()


@scenario
async def test_a_spawned_task_that_fails_is_an_error_notice_from_its_extension(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    ext = Recorded("bot", log)
    h = await Harness.create(tmp_path, extensions=[ext])
    assert ext.tree is not None

    async def broken() -> None:
        raise OSError("the server went away")

    task = ext.tree.spawn(broken())
    await asyncio.gather(task, return_exceptions=True)
    await h.settle()
    assert h.notice_events() == [
        Notice("task failed: OSError: the server went away", None, "bot", "error")
    ]
    await h.close()


@scenario
async def test_spawned_tasks_are_cancelled_before_stop_runs(tmp_path: Path) -> None:
    log: list[str] = []

    class Owner(Recorded):
        async def start(self, tree: TreeHandle) -> None:
            await super().start(tree)
            tree.spawn(self.forever())

        async def forever(self) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.log.append("task cancelled")
                raise

    h = await Harness.create(tmp_path, extensions=[Owner("owner", log)])
    await h.driver.stop()
    assert log == ["start owner", "task cancelled", "stop owner"]
    await h.close()


@scenario
async def test_a_configured_stop_timeout_bounds_a_stop(tmp_path: Path) -> None:
    log: list[str] = []

    class Hangs(Recorded):
        async def stop(self) -> None:
            self.log.append("stop hangs")
            await asyncio.sleep(3600)

    h = await Harness.create(
        tmp_path,
        extensions=[Hangs("hangs", log), Recorded("after", log)],
        catalog=catalog_with(hangs=(None, 0.05)),
    )
    await asyncio.wait_for(h.driver.stop(), 5)
    assert log[-2:] == ["stop after", "stop hangs"]
    await h.close()


@scenario
async def test_reassemble_changes_the_tools_from_the_next_sample_and_sends_commands(
    tmp_path: Path,
) -> None:
    class Grows:
        name = "grows"

        def __init__(self) -> None:
            self.tools: tuple[FakeTool, ...] = ()
            self.commands: tuple[object, ...] = ()
            self.tree: TreeHandle | None = None

        async def start(self, tree: TreeHandle) -> None:
            self.tree = tree

    async def listed(tree: TreeHandle) -> str:
        """Say what is listed."""
        return "listed"

    ext = Grows()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("one"), say("two")]), extensions=[ext]
    )
    await h.send("first")
    await h.settle()
    assert h.provider.requests[0].tool_names == []
    before = len([e for e in h.heard if isinstance(e, Commands)])
    ext.tools = (FakeTool("found", "f"),)
    ext.commands = (listed,)
    assert ext.tree is not None
    ext.tree.reassemble()
    await h.settle()
    commands = [e for e in h.heard if isinstance(e, Commands)]
    assert len(commands) == before + 1
    assert "listed" in {f.name for f in commands[-1].forms}
    await h.send("second")
    await h.settle()
    assert h.provider.requests[1].tool_names == ["found"]
    await h.close()


@scenario
async def test_reload_stops_the_old_before_it_starts_the_new(tmp_path: Path) -> None:
    log: list[str] = []
    kept = Recorded("kept", log)
    h = await Harness.create(tmp_path, extensions=[kept, Recorded("old", log)])
    await h.driver.reload([kept, Recorded("new", log)])
    assert log == ["start kept", "start old", "stop old", "start new"]
    await h.close()


@scenario
async def test_reload_is_busy_while_a_cursor_is_not_at_rest(tmp_path: Path) -> None:
    gate = Gate()
    log: list[str] = []
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hi")], Stop.End, gate=gate)]),
        extensions=[Recorded("old", log)],
    )
    await h.send("go")
    await h.settle()
    with pytest.raises(Busy):
        await h.driver.reload([Recorded("new", log)])
    assert log == ["start old", "pre_step old"], "nothing stopped or started"
    gate.release()
    await h.settle()
    await h.close()


@scenario
async def test_a_failed_start_in_a_reload_leaves_the_kept_extensions_alone(tmp_path: Path) -> None:
    log: list[str] = []

    async def kept_command(tree: TreeHandle) -> str:
        """Kept."""
        return "kept"

    async def old_command(tree: TreeHandle) -> str:
        """Old."""
        return "old"

    kept, old = Recorded("kept", log), Recorded("old", log)
    kept.commands = (kept_command,)  # pyright: ignore[reportAttributeAccessIssue]
    old.commands = (old_command,)  # pyright: ignore[reportAttributeAccessIssue]
    h = await Harness.create(tmp_path, extensions=[kept, old])
    with pytest.raises(ExtensionLoadError, match="extension 'bad' did not start"):
        await h.driver.reload([kept, Recorded("good", log), Failing("bad", log)])
    assert log == [
        "start kept",
        "start old",
        "stop old",
        "start good",
        "start bad",
        "stop good",
    ]
    names = {f.name for f in await h.client.commands()}
    assert "kept_command" in names
    assert "old_command" not in names
    await h.settle()
    assert [e for e in h.heard if isinstance(e, Commands)][-1].forms == await h.client.commands()
    await h.close()


@dataclasses.dataclass(frozen=True)
class SomeOptions:
    size: int = 1


class WithOptions:
    name = "with_options"
    options = SomeOptions


@scenario
async def test_a_client_cannot_set_a_timeout_it_is_not_an_option(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[WithOptions()])
    with pytest.raises(ConfigError, match=r"with_options.start_timeout is not set on a branch"):
        await h.configure("with_options", {"start_timeout": 5})
    form = await h.client.options(h.cursor, "with_options")
    assert "start_timeout" not in form.schema["properties"]  # type: ignore[operator]
    await h.close()
