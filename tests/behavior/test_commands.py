"""Commands (D16 section 2) and the simple cases of section 6 that are commands."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from fakes import FAKE_MODEL, FakeProvider, Gate, Recorder, Reply
from harness import Harness, build, say, scenario

from tau import (
    Author,
    Body,
    Busy,
    Client,
    CommandFailed,
    Cursor,
    CursorId,
    DefinitionError,
    Ending,
    Form,
    ItemData,
    ModelCatalog,
    NodeHandle,
    NodeId,
    Notice,
    Outcome,
    Part,
    Progress,
    Stop,
    Subject,
    TreeHandle,
    read_models,
)


def model(tree: TreeHandle) -> str:
    """Another /model."""
    return "mine"


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
    tree.send(to, note)
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
    assert await h.run("word_count", {"path": str(file)}) == "3 words"
    await h.settle()
    assert h.notices() == [], "the result comes back from the call, not as a notice"
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
    assert isinstance(by_name["hand_off"], Form)
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


def told(h: Harness, cursor: int) -> list[Body]:
    """The bodies of `cursor`'s walk from its `User` node on: what its model was given."""
    bodies = h.bodies(cursor)
    return bodies[next(i for i, b in enumerate(bodies) if isinstance(b, Body.User)) :]


@scenario
async def test_hand_off_to_a_cursor_by_id_is_a_send_its_model_answers(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("noted")]), extensions=[Navigation()]
    )
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    assert (
        await h.run("hand_off", {"to": other, "note": "look at this"}) == f"sent to cursor {other}"
    )
    await h.settle()
    assert told(h, other)[0] == Body.User("look at this")
    (request,) = h.provider.requests
    assert h.session.tree.node(request.walk[-1]).body == Body.User("look at this"), (
        "the model got it"
    )
    assert h.session.cursor(other).items == []


@scenario
async def test_hand_off_to_new_opens_a_cursor_at_the_running_cursors_node(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("started")]), extensions=[Navigation()]
    )
    at = h.session.cursor(h.cursor).at
    told_of = await h.run("hand_off", {"to": "new", "note": "start here"})
    await h.settle()
    (new,) = [c for c in h.session.cursors() if c != h.cursor]
    assert told_of == f"sent to cursor {new}"
    (request,) = h.provider.requests
    sent = h.session.tree.node(request.walk[-1])
    assert sent.body == Body.User("start here")
    assert sent.parent == at, "the new cursor started where the running cursor was"


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
    running = asyncio.ensure_future(h.run("slow"))
    await h.settle()
    assert h.session.cursor(h.cursor).busy
    await h.abort()
    await h.settle()
    with pytest.raises(CommandFailed, match="Aborted"):
        await running
    assert seen == ["cancelled"]
    assert not h.session.cursor(h.cursor).busy
    assert "pre_step" not in recorder.points
    assert h.notices() == []


@scenario
async def test_a_failing_cursor_command_raises_for_the_client_and_fails_the_step(
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
    with pytest.raises(CommandFailed, match=r"^RuntimeError: boom$"):
        await h.run("explode")
    if not h.remote:  # over a wire the step may end before the answer arrives
        assert h.session.cursor(h.cursor).busy, "it raises at once, before the step has ended"
    await h.settle()
    assert not h.session.cursor(h.cursor).busy
    assert h.notices() == []
    view = await h.view()
    assert view.hook_error is not None
    assert (view.hook_error.extension, view.hook_error.point) == ("explodes", "pre_step")
    assert view.hook_error.error == "RuntimeError: boom"
    assert "pre_step" not in recorder.points, "a failed step runs no other hook"


@scenario
async def test_a_failing_plain_command_only_raises_for_the_client(tmp_path: Path) -> None:
    def explode(tree: TreeHandle) -> None:
        """Fail."""
        raise RuntimeError("boom")

    class Explodes:
        name = "explodes"
        commands = (explode,)

    h = await Harness.create(tmp_path, extensions=[Explodes()])
    with pytest.raises(CommandFailed, match=r"^RuntimeError: boom$"):
        await h.run("explode")
    assert (await h.view()).hook_error is None


@scenario
async def test_progress_is_a_notice_each_call_and_the_result_is_returned(tmp_path: Path) -> None:
    def count(progress: Progress, upto: int) -> str:
        """Count."""
        for i in range(upto):
            progress(f"at {i}")
        return "counted"

    class Counts:
        name = "counts"
        commands = (count,)

    h = await Harness.create(tmp_path, extensions=[Counts()])
    assert await h.run("count", {"upto": 2}) == "counted"
    await h.settle()
    assert h.notices() == ["at 0", "at 1"]


@scenario
async def test_only_the_client_that_ran_the_command_is_told_its_progress(tmp_path: Path) -> None:
    def count(progress: Progress) -> str:
        """Count."""
        progress("at 0")
        return "counted"

    class Counts:
        name = "counts"
        commands = (count,)

    h = await Harness.create(tmp_path, extensions=[Counts()])
    other = await h.connect("other")
    assert await h.run("count") == "counted"
    await h.settle()
    assert h.notices() == ["at 0"]
    await other.close()
    assert not [e for e in [e async for e in other] if isinstance(e, Notice)]
    await h.close()


# Building the driver.


def test_two_commands_with_one_name_in_one_extension_fail_when_the_driver_is_built() -> None:
    def a(tree: TreeHandle) -> None:
        """A."""

    class Twice:
        name = "twice"
        commands = (a, a)

    with pytest.raises(DefinitionError, match=r"two commands are named 'a' \(extension 'twice'\)"):
        build([Twice()])


# Qualified names (D19 section 7).


def note_a(tree: TreeHandle) -> str:
    """A's note."""
    return "from a"


def note_b(tree: TreeHandle) -> str:
    """B's note."""
    return "from b"


note_a.__name__ = note_b.__name__ = "note"


class A:
    name = "a"
    commands = (note_a,)


class B:
    name = "b"
    commands = (note_b,)


@scenario
async def test_two_extensions_may_share_a_command_name_and_each_is_reached_qualified(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[A(), B()])
    forms = [f for f in await h.client.commands() if f.name == "note"]
    assert [(f.extension, f.qualified) for f in forms] == [("a", "a:note"), ("b", "b:note")]
    assert await h.run("a:note") == "from a"
    assert await h.run("b:note") == "from b"
    await h.close()


@scenario
async def test_a_bare_name_two_extensions_share_is_refused_naming_both(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[A(), B()])
    with pytest.raises(LookupError, match="/note is two commands: /a:note, /b:note"):
        await h.run("note")
    await h.close()


@scenario
async def test_a_bare_name_works_while_one_extension_has_it(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[A()])
    assert await h.run("note") == "from a"
    assert await h.run("a:note") == "from a"
    with pytest.raises(LookupError, match="no command 'b:note'"):
        await h.run("b:note")
    await h.close()


@scenario
async def test_the_built_in_gets_no_special_treatment_an_extension_may_add_model(
    tmp_path: Path,
) -> None:
    class Models:
        name = "models"
        commands = (model,)

    h = await Harness.create(tmp_path, extensions=[Models()])
    with pytest.raises(LookupError, match="/model is two commands: /tau:model, /models:model"):
        await h.run("model")
    assert await h.run("models:model") == "mine"
    await h.close()


def test_a_command_may_not_be_named_config() -> None:
    def config(tree: TreeHandle) -> None:
        """Shadow."""

    class Shadow:
        name = "shadow"
        commands = (config,)

    with pytest.raises(DefinitionError, match="'config'"):
        build([Shadow()])


def test_an_extension_may_not_be_named_tau() -> None:
    class Imposter:
        name = "tau"

    with pytest.raises(DefinitionError, match="'tau'"):
        build([Imposter()])


@pytest.mark.parametrize("name", ["a:b", "a.b", "a b", "ü", "a/b"])
def test_an_extension_name_is_letters_digits_underscore_and_hyphen(name: str) -> None:
    class Odd:
        pass

    Odd.name = name  # type: ignore[attr-defined]
    with pytest.raises(DefinitionError, match="letters, digits"):
        build([Odd()])
    Odd.name = "odd_name-2"  # type: ignore[attr-defined]
    build([Odd()])


def test_a_command_may_not_take_a_nodes_handle() -> None:
    def bad(node: NodeHandle) -> None:
        """Bad."""

    class Bad:
        name = "bad"
        commands = (bad,)

    with pytest.raises(DefinitionError, match="command 'bad', parameter 'node'"):
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


# Who the work is for: no handle knows a client.


class Knows:
    """Tells every client when a step starts, and what a tool's handle knows."""

    name = "knows"

    def __init__(self) -> None:
        self.tools = (self.tool_cursor,)

    async def pre_step(self, cursor: Cursor, subject: object) -> None:
        cursor.tell("step started")

    def tool_cursor(self, node: NodeHandle) -> str:
        """The cursor whose batch this tool runs in."""
        return f"{node.cursor_id}"


async def _next_notice(client: Client) -> str:
    async with asyncio.timeout(1):
        async for event in client:
            if isinstance(event, Notice):
                return event.text
    raise AssertionError("the stream ended")


@scenario
async def test_a_hooks_tell_goes_to_every_connected_client(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Knows()])
    other = await h.connect("other")
    await h.send("hi")
    await h.settle()
    assert h.notices() == ["step started"]
    assert await _next_notice(other) == "step started"
    await other.close()
    await h.send("again")
    await h.settle()
    assert h.notices() == ["step started"] * 2, "a client that left is not told"
    await h.send_unattended("headless")  # nobody is connected: the notice has nobody to reach
    await h.settle()
    assert (await h.view()).hook_error is None, "the hook did not fail"
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_a_tool_knows_its_cursor(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [Reply([Part.Call("c1", "tool_cursor", "{}")], Stop.Tools), say("ok")]
        ),
        extensions=[Knows()],
    )
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.send("go", cursor=other)
    await h.settle()
    (results,) = [b for b in h.bodies(other) if isinstance(b, Body.Results)]
    assert results.outcomes[0][1] == Outcome.Ok(str(other))


class Nudges:
    """An extension that sends to another cursor from a command, a tool and a hook."""

    name = "nudges"

    def __init__(self, target: int | None = None) -> None:
        self.target = target
        self.seen: list[Ending | None] = []
        self.commands = (self.nudge, self.nudge_and_wait)
        self.tools = (self.nudge_from_tool,)

    def nudge(self, tree: TreeHandle, to: CursorId, text: str) -> None:
        """Send `text` to a cursor."""
        tree.send(to, text)

    async def nudge_and_wait(self, tree: TreeHandle, to: CursorId, text: str) -> None:
        """Send `text` to a cursor, and keep how the step it caused ended."""
        tree.send(to, text)
        self.seen.append(await tree.rest(to))

    def nudge_from_tool(self, node: NodeHandle, to: CursorId, text: str) -> str:
        """Send `text` to a cursor."""
        node.send(to, text)
        return "sent"

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        if self.target is not None and cursor.id == 0:
            cursor.send(self.target, "from a hook")


def users(h: Harness, cursor: int) -> list[tuple[Author, str]]:
    """The `User` nodes of a cursor's walk, with their authors."""
    walk = h.session.tree.walk(h.session.cursor(cursor).at)
    return [(n.by, n.body.text) for n in walk if isinstance(n.body, Body.User)]


@scenario
async def test_a_command_can_send_to_a_cursor_nobody_has_typed_on(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("heard")]), extensions=[Nudges()])
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.run("nudge", {"to": other, "text": "psst"})
    await h.settle()
    assert users(h, other) == [(Author.Extension("nudges"), "psst")]
    assert users(h, h.cursor) == []
    assert h.bodies(other)[-1] == Body.Assistant([Part.Text("heard")], Stop.End), "it answered"
    await h.close()


@scenario
async def test_a_hook_can_send_to_a_cursor_nobody_has_typed_on(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("heard")]), extensions=[Nudges(target=1)]
    )
    other = await h.client.open(h.session.cursor(h.cursor).at)
    assert other == 1
    await h.settle()
    await h.step()  # cursor 0's pre_step sends
    await h.settle()
    assert users(h, other) == [(Author.Extension("nudges"), "from a hook")]
    await h.close()


@scenario
async def test_an_extensions_halted_input_notifies_no_one_and_ends_halted(tmp_path: Path) -> None:
    recorder = Recorder(says={"pre_input": "not today"})
    nudges = Nudges()
    h = await Harness.create(tmp_path, extensions=[nudges, recorder])
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.run("nudge", {"to": other, "text": "psst"})
    await h.settle()
    assert h.notices() == [], "an extension has no stream to notify"
    await h.run("nudge_and_wait", {"to": other, "text": "again"})
    assert nudges.seen == [Ending.Halted("not today")], "the sender reads the reason from rest"
    assert (await h.view(other)).ending == Ending.Halted("not today")
    assert (await h.view(other)).items == (), "the halted input is gone"
    assert users(h, other) == []
    await h.send("typed", cursor=other)  # a frontend's halted input is told to its client
    await h.settle()
    assert h.notices() == ["not today"]
    await h.close()


@scenario
async def test_an_extensions_pending_input_survives_a_crash_with_its_author(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path,
        extensions=[Nudges(), Recorder(gates={"pre_step": gate})],
        gates=[gate],
    )
    await h.step()
    await h.settle()  # the step waits at pre_step, before the input phase
    await h.run("nudge", {"to": h.cursor, "text": "psst"})
    pending = ItemData.Input(Author.Extension("nudges"), "psst")
    assert [i.data for i in h.session.cursor(h.cursor).items] == [pending]
    assert [i.data for i in (await h.view()).items] == [pending], "a client sees who wrote it"
    await h.crash()
    assert [i.data for i in h.session.cursor(h.cursor).items] == [pending]
    await h.close()


@scenario
async def test_a_tool_hands_off_to_another_cursor_as_its_extension(
    tmp_path: Path,
) -> None:
    class Hands:
        name = "hands"
        tools = (hand_off,)

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                Reply([Part.Call("c1", "hand_off", '{"to": 1, "note": "n"}')], Stop.Tools),
                say("ok"),
                say("ok"),
            ]
        ),
        extensions=[Hands()],
    )
    other = await h.client.open(h.session.cursor(h.cursor).at)
    assert other == 1
    await h.send("go")
    await h.settle()
    assert Body.User("n") in h.bodies(other), "the other cursor's model was given the note"
    assert users(h, other) == [(Author.Extension("hands"), "n")]
