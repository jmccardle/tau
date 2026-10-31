"""Commands (D16 section 2) and the simple cases of section 6 that are commands."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from fakes import FAKE_MODEL, FakeProvider, Gate, Recorder, Reply
from harness import Harness, build, say, scenario

from tau_next import (
    Body,
    Busy,
    CommandInfo,
    Cursor,
    CursorId,
    ExtensionContractError,
    ItemData,
    ModelCatalog,
    NodeHandle,
    NodeId,
    Part,
    Progress,
    ToolDefinitionError,
    TreeHandle,
    read_models,
)


def word_count(path: Path) -> str:
    """Count the words in a file."""
    return f"{len(path.read_text().split())} words"


class Words:
    name = "words"  # this module is the extension
    tools = commands = (word_count,)  # the model calls it; a person types /word_count <Tab>


async def branch_from(cursor: Cursor, node: NodeId) -> None:
    """Go back to a node of this cursor's walk."""
    cursor.move(node)


def hand_off(tree: TreeHandle, to: CursorId, note: str) -> str:
    """Send a note to another cursor."""
    tree.add_item(to, note)
    return f"sent to cursor {to}"


class Navigation:
    name = "navigation"
    commands = (
        branch_from,
        hand_off,
    )


@scenario
async def test_a_function_that_is_a_tool_and_a_command_tells_the_client(tmp_path: Path) -> None:
    file = tmp_path / "words.txt"
    file.write_text("one two three")
    h = await Harness.create(tmp_path, extensions=[Words()])
    await h.run("word_count", {"path": str(file)})
    await h.settle()
    assert h.notices() == ["3 words"]
    assert h.session.cursor(h.cursor).busy is False
    assert len(h.nodes()) == 2, "a command that touches nothing writes nothing"


@scenario
async def test_the_model_is_offered_the_same_function_as_a_tool(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Words()])
    await h.send("hi")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["word_count"]


@scenario
async def test_the_client_is_told_each_command_and_its_schema(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Words(), Navigation()])
    infos = await h.client.commands()
    by_name = {c.name: c for c in infos}
    assert list(by_name) == ["model", "word_count", "branch_from", "hand_off"]
    assert by_name["word_count"].description == "Count the words in a file."
    assert by_name["word_count"].schema["properties"] == {
        "path": {"type": "string", "format": "path"}
    }
    assert by_name["branch_from"].schema["required"] == ["node"], "the Cursor is not an argument"
    assert isinstance(by_name["hand_off"], CommandInfo)
    assert list(cast("dict[str, object]", by_name["hand_off"].schema["properties"])) == [
        "to",
        "note",
    ]


@scenario
async def test_branch_from_to_a_reply_moves_there_and_samples_nothing(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a")]), extensions=[Navigation()])
    await h.send("hi")
    await h.settle()
    reply = h.session.cursor(h.cursor).at
    system = 1
    await h.run("branch_from", {"node": system})
    await h.settle()
    assert h.session.cursor(h.cursor).at == system
    await h.run("branch_from", {"node": reply})
    await h.settle()
    assert h.session.cursor(h.cursor).at == reply
    assert len(h.provider.requests) == 1, "a command that leaves the cursor on a reply: no sample"


@scenario
async def test_a_command_that_leaves_the_cursor_on_a_user_node_gets_a_sample(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("a"), say("b")]), extensions=[Navigation()]
    )
    await h.send("hi")
    await h.settle()
    user = h.session.cursor(h.cursor).at - 1
    assert isinstance(h.session.tree.node(user).body, Body.User)
    await h.run("branch_from", {"node": user})
    await h.settle()
    assert len(h.provider.requests) == 2
    assert isinstance(h.session.tree.node(h.session.cursor(h.cursor).at).body, Body.Assistant)


@scenario
async def test_a_node_that_is_not_there_is_refused_and_nothing_runs(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Navigation()])
    before = h.snapshot()
    with pytest.raises(ValueError, match=r"/branch_from: argument 'node' must be an existing node"):
        await h.run("branch_from", {"node": 99})
    assert h.snapshot() == before


@scenario
async def test_hand_off_to_a_cursor_by_id(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Navigation()])
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.run("hand_off", {"to": other, "note": "look at this"})
    await h.settle()
    assert h.notices() == [f"sent to cursor {other}"]
    items = [i.data for i in h.session.cursor(other).items]
    assert items == [ItemData.Extension("navigation", "look at this")]


@scenario
async def test_hand_off_to_new_opens_a_cursor_at_the_running_cursors_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Navigation()])
    await h.run("hand_off", {"to": "new", "note": "start here"})
    await h.settle()
    (new,) = [c for c in h.session.cursors() if c != h.cursor]
    assert h.session.cursor(new).at == h.session.cursor(h.cursor).at
    assert h.notices() == [f"sent to cursor {new}"]
    assert [i.data for i in h.session.cursor(new).items] == [
        ItemData.Extension("navigation", "start here")
    ]


@scenario
async def test_a_refused_hand_off_does_not_open_the_new_cursor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Navigation()])
    with pytest.raises(ValueError, match="note"):
        await h.run("hand_off", {"to": "new", "note": 3})
    assert h.session.cursors() == [h.cursor]


@scenario
async def test_an_unknown_command_and_arguments_that_do_not_fit_are_refused(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Words()])
    with pytest.raises(LookupError, match="nope"):
        await h.run("nope")
    with pytest.raises(ValueError, match="missing required arguments"):
        await h.run("word_count")
    with pytest.raises(ValueError, match="unexpected arguments"):
        await h.run("word_count", {"path": "x", "extra": 1})
    with pytest.raises(ValueError, match="must be string"):
        await h.run("word_count", {"path": 3})


# Commands that move the cursor are steps of it.


@scenario
async def test_a_cursor_command_is_refused_busy_while_the_cursor_is_not_at_rest(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("a")], gate=gate)]),
        extensions=[Navigation()],
    )
    await h.send("hi")
    await h.settle()
    with pytest.raises(Busy):
        await h.run("branch_from", {"node": 1})
    gate.release()
    await h.settle()
    assert h.session.cursor(h.cursor).at != 1


@scenario
async def test_a_cursor_command_runs_before_the_extensions_pre_step_hooks(tmp_path: Path) -> None:
    order: list[str] = []

    async def mark(cursor: Cursor) -> None:
        """Note when it runs."""
        order.append("command")

    class Marks:
        name = "marks"
        commands = (mark,)

        async def pre_step(self, cursor: Cursor, subject: object) -> None:
            order.append("pre_step")

    h = await Harness.create(tmp_path, extensions=[Marks()])
    await h.run("mark")
    await h.settle()
    assert order == ["command", "pre_step"]


@scenario
async def test_abort_cancels_a_running_command_and_skips_the_hooks(tmp_path: Path) -> None:
    gate = Gate()
    seen: list[str] = []

    async def slow(cursor: Cursor) -> str:
        """Wait."""
        try:
            await gate.wait()
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
        return "finished"

    class Slow:
        name = "slow"
        commands = (slow,)

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Slow(), recorder], gates=[gate])
    await h.run("slow")
    await h.settle()
    assert h.session.cursor(h.cursor).busy
    assert await h.abort() is False, "core has no effect of its own to cancel in a hook job"
    await h.settle()
    assert seen == ["cancelled"]
    assert not h.session.cursor(h.cursor).busy
    assert "pre_step" not in recorder.points
    assert h.notices() == []


@scenario
async def test_a_failing_cursor_command_tells_the_client_and_fails_the_step(
    tmp_path: Path,
) -> None:
    def explode(cursor: Cursor) -> None:
        """Fail."""
        raise RuntimeError("boom")

    class Explodes:
        name = "explodes"
        commands = (explode,)

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Explodes(), recorder])
    await h.run("explode")
    await h.settle()
    assert h.notices() == ["RuntimeError: boom"]
    view = await h.view()
    assert view.hook_error is not None
    assert (view.hook_error.extension, view.hook_error.point) == ("explodes", "pre_step")
    assert view.hook_error.error == "RuntimeError: boom"
    assert "pre_step" not in recorder.points, "a failed step runs no other hook"


@scenario
async def test_a_failing_plain_command_only_tells_the_client(tmp_path: Path) -> None:
    def explode(tree: TreeHandle) -> None:
        """Fail."""
        raise RuntimeError("boom")

    class Explodes:
        name = "explodes"
        commands = (explode,)

    h = await Harness.create(tmp_path, extensions=[Explodes()])
    await h.run("explode")
    await h.settle()
    assert h.notices() == ["RuntimeError: boom"]
    assert (await h.view()).hook_error is None


@scenario
async def test_progress_is_a_notice_each_call(tmp_path: Path) -> None:
    def count(progress: Progress, upto: int) -> str:
        """Count."""
        for i in range(upto):
            progress(f"at {i}")
        return "counted"

    class Counts:
        name = "counts"
        commands = (count,)

    h = await Harness.create(tmp_path, extensions=[Counts()])
    await h.run("count", {"upto": 2})
    await h.settle()
    assert h.notices() == ["at 0", "at 1", "counted"]


@scenario
async def test_only_the_client_that_ran_the_command_is_told(tmp_path: Path) -> None:
    file = tmp_path / "w.txt"
    file.write_text("a b")
    h = await Harness.create(tmp_path, extensions=[Words()])
    other = await h.connect("other")
    await h.run("word_count", {"path": str(file)})
    await h.settle()
    assert h.notices() == ["2 words"]
    await other.close()


# Building the driver.


def test_two_commands_with_one_name_fail_when_the_driver_is_built() -> None:
    def a(tree: TreeHandle) -> None:
        """A."""

    class One:
        name = "one"
        commands = (a,)

    class Two:
        name = "two"
        commands = (a,)

    with pytest.raises(ExtensionContractError, match="two commands are named 'a'"):
        build([One(), Two()])


def test_a_command_may_not_be_named_for_a_built_in() -> None:
    def model(tree: TreeHandle) -> None:
        """Shadow."""

    class Shadow:
        name = "shadow"
        commands = (model,)

    with pytest.raises(ExtensionContractError, match="'model'"):
        build([Shadow()])


def test_an_extension_may_not_be_named_tau() -> None:
    class Imposter:
        name = "tau"

    with pytest.raises(ExtensionContractError, match="'tau'"):
        build([Imposter()])


def test_a_command_may_not_take_a_nodes_handle() -> None:
    def bad(node: NodeHandle) -> None:
        """Bad."""

    class Bad:
        name = "bad"
        commands = (bad,)

    with pytest.raises(ToolDefinitionError, match="command 'bad', parameter 'node'"):
        build([Bad()])


# The built-in /model.


def two_models() -> ModelCatalog:
    models = {name: {"model": name, "provider": "fake"} for name in (FAKE_MODEL, "alt")}
    return read_models(cli={"models": models})


@scenario
async def test_model_writes_a_config_node_and_the_next_sample_uses_it(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("a"), say("b")]),
        catalog=two_models(),
    )
    await h.send("one")
    await h.settle()
    await h.run("model", {"model": "alt"})
    await h.settle()
    config = h.session.tree.node(h.session.cursor(h.cursor).at)
    assert isinstance(config.body, Body.Config)
    assert config.body.config.data == '{"model": "alt"}'
    assert config.by.__class__.__name__ == "Extension"
    await h.send("two")
    await h.settle()
    assert [r.model.model for r in h.provider.requests] == [FAKE_MODEL, "alt"]


@scenario
async def test_model_refuses_a_model_that_is_not_configured(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    with pytest.raises(ValueError, match="configured model"):
        await h.run("model", {"model": "gpt-9"})


@scenario
async def test_a_model_is_set_per_branch(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("a"), say("b")]),
        catalog=two_models(),
    )
    root = h.session.cursor(h.cursor).at
    other = await h.client.open(root)
    await h.run("model", {"model": "alt"})
    await h.settle()
    await h.send("on alt")
    await h.settle()
    await h.send("on the other branch", cursor=other)
    await h.settle()
    assert sorted(r.model.model for r in h.provider.requests) == ["alt", FAKE_MODEL]
