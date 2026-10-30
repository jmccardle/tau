"""Hooks, extensions, and frontends acting on cursors and the tree, through the Harness.

Each test names the model.md rule it holds (section 5 Move, section 7 Handles).
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

import tau_next
from tau_next import (
    Author,
    Body,
    CallDraft,
    Config,
    Cursor,
    Ending,
    ItemData,
    NodeHandle,
    Outcome,
    Part,
    Stop,
    Subject,
)

SYSTEM = [Body.Config(Config("{}")), Body.System("sys")]
SYS = 1  # the system node's id: a new session is Config 0, System 1

Hookfn = Callable[[Cursor, Subject], Awaitable[str | None]]


class Scripted:
    """A real extension whose hooks are the functions it is given, by hook point."""

    def __init__(self, name: str, **hooks: Hookfn) -> None:
        self.name = name
        self._hooks = hooks

    def __getattr__(self, point: str) -> Hookfn:
        try:
            return self._hooks[point]
        except KeyError:
            raise AttributeError(point) from None


def text(t: str) -> Reply:
    return Reply([Part.Text(t)], Stop.End)


@scenario
async def test_a_pre_reply_move_chooses_where_the_reply_lands(tmp_path: Path) -> None:
    """A hook's move applies at once (section 5); the commit goes under `at`, so the
    reply branches off the system node and the user node becomes a side node."""
    seen: list[list[tau_next.Move]] = []

    async def nav(cursor: Cursor, subject: Subject) -> str | None:
        cursor.move(SYS)
        return None

    async def watch(cursor: Cursor, subject: Subject) -> str | None:
        seen.append(cursor.moves(since_step=True))
        return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([text("hello")]),
        extensions=[Scripted("nav", pre_reply=nav), Scripted("watch", pre_reply=watch)],
    )
    await h.send("hi")
    await h.settle()
    assert h.bodies() == [*SYSTEM, Body.Assistant([Part.Text("hello")], Stop.End)]
    assert h.nodes()[2].body == Body.User("hi"), "the input is a side node now"
    assert h.nodes()[3].parent == SYS, "a branch: a sibling of the input"
    moves = [(m.from_, m.to, m.by) for m in h.session.cursor(h.cursor).moves]
    assert (2, SYS, Author.Extension("nav")) in moves
    assert [(m.to, m.by) for m in seen[0] if m.from_ == 2] == [(SYS, Author.Extension("nav"))], (
        "a later hook sees the move in the same step"
    )


@scenario
async def test_a_hook_moves_to_a_node_that_does_not_exist_and_fails_the_step(
    tmp_path: Path,
) -> None:
    """I3 holds through the hook: the move is refused and the hook's raise fails the step."""
    raised: list[Exception] = []

    async def bad(cursor: Cursor, subject: Subject) -> str | None:
        try:
            cursor.move(999)
        except tau_next.NoSuchNode as e:
            raised.append(e)
            raise
        return None

    recorder = Recorder("rec")
    h = await Harness.create(tmp_path, extensions=[Scripted("bad", pre_step=bad), recorder])
    await h.step()
    await h.settle()
    assert len(raised) == 1
    assert h.session.cursor(h.cursor).at == SYS
    assert recorder.endings == [Ending.Failed("no node 999")]


@scenario
async def test_an_extension_writes_a_custom_node_that_delivers_its_item(tmp_path: Path) -> None:
    """`cursor.write(body, delivers=[item])` (section 7, Between cursors): one record
    that appends under `at`, moves onto it, and removes the item. `next` skips `Custom`."""

    async def note(cursor: Cursor, subject: Subject) -> str | None:
        for item in cursor.items():
            assert isinstance(item.data, ItemData.Extension)
            cursor.write(Body.Custom("note", item.data.data), delivers=[item.id])
        return None

    provider = FakeProvider([])
    h = await Harness.create(
        tmp_path, provider=provider, extensions=[Scripted("notes", pre_step=note)]
    )
    item = await h.add_item("notes", "remember")
    await h.settle()
    assert h.bodies() == [*SYSTEM, Body.Custom("note", "remember")]
    node = h.nodes()[2]
    assert node.by == Author.Extension("notes")
    assert node.delivers == [item]
    assert h.session.cursor(h.cursor).items == []
    assert h.session.cursor(h.cursor).next == tau_next.Next.Wait
    assert provider.requests == [], "a Custom node never reaches the model, and starts no sample"
    assert h.session.cursor(h.cursor).at == node.id


@scenario
async def test_a_write_survives_a_reload(tmp_path: Path) -> None:
    """I7: a hook's write and its delivery are one record, replayed on load."""

    async def note(cursor: Cursor, subject: Subject) -> str | None:
        for item in cursor.items():
            cursor.write(Body.Custom("note", "n"), delivers=[item.id])
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("notes", pre_step=note)])
    await h.add_item("notes", "x")
    await h.settle()
    before = h.snapshot()
    await h.crash()
    await h.settle()
    assert h.snapshot() == before


@scenario
async def test_an_append_leaves_every_cursor_where_it_is(tmp_path: Path) -> None:
    """`append` anywhere (section 7): a side note under any node, moving nothing."""
    made: list[int] = []

    async def side(cursor: Cursor, subject: Subject) -> str | None:
        made.append(cursor.append(SYS, Body.Custom("side", "s")))
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("side", post_step=side)])
    await h.step()
    await h.settle()
    assert h.session.cursor(h.cursor).at == SYS
    node = h.session.tree.node(made[0])
    assert (node.parent, node.by, node.body) == (
        SYS,
        Author.Extension("side"),
        Body.Custom("side", "s"),
    )


@scenario
async def test_a_hook_opens_a_cursor_and_labels_it_and_the_label_survives_a_reload(
    tmp_path: Path,
) -> None:
    """open and labels (sections 1, 7): opening wakes the cursor, so its own hooks run."""
    opened: list[int] = []
    ran = Recorder("rec")

    async def spawn(cursor: Cursor, subject: Subject) -> str | None:
        if not opened:
            other = cursor.open(cursor.at)
            opened.append(other)
            cursor.set_label("worker", other)
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("spawn", pre_step=spawn), ran])
    await h.step()
    await h.settle()
    other = opened[0]
    assert h.session.label("worker") == other
    assert h.session.cursor(other).labels == ["worker"]
    assert ran.points.count("pre_step") == 2, "the opened cursor woke and ran its own step"
    await h.crash()
    assert h.session.label("worker") == other
    with pytest.raises(tau_next.NoSuchLabel):
        h.session.label("nobody")


@scenario
async def test_a_hook_removes_an_item_without_waking_anything(tmp_path: Path) -> None:
    """remove_item (section 7): any item, by id; an unknown id is `NoSuchItem`. Only an
    added item counts as a scratchpad change, so removing one starts no second step."""
    refused: list[Exception] = []

    async def purge(cursor: Cursor, subject: Subject) -> str | None:
        for item in cursor.items():
            cursor.remove_item(item.id)
            try:
                cursor.remove_item(item.id)
            except tau_next.NoSuchItem as e:
                refused.append(e)
        return None

    recorder = Recorder("rec")
    h = await Harness.create(tmp_path, extensions=[Scripted("purge", pre_step=purge), recorder])
    await h.add_item("other", "keep?")
    await h.settle()
    assert h.session.cursor(h.cursor).items == []
    assert len(refused) == 1
    assert recorder.points.count("pre_step") == 1, "the removal woke nothing"
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_a_hook_turns_auto_step_back_on_after_a_crash(tmp_path: Path) -> None:
    """Rulings, leave it to an extension: load wakes with auto-step off; a crash-resume
    extension turns its own cursor's auto-step on in `pre_step`, and the step carries on."""
    armed = False

    async def resume(cursor: Cursor, subject: Subject) -> str | None:
        if armed:
            cursor.set_auto(True)
        return None

    gate = Gate()
    provider = FakeProvider([Reply([Part.Text("lost")], gate=gate), text("hello")])
    h = await Harness.create(
        tmp_path, provider=provider, extensions=[Scripted("resume", pre_step=resume)]
    )
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    armed = True
    await h.crash()
    await h.settle()
    assert h.bodies() == [
        *SYSTEM,
        Body.User("hi"),
        Body.Assistant([Part.Text("hello")], Stop.End),
    ]
    assert h.session.cursor(h.cursor).auto, "and it stays on"


@scenario
async def test_a_handle_used_after_its_hook_returned_is_refused(tmp_path: Path) -> None:
    """A hook acts only while its job is out (D11): afterwards `move`, `write`, and
    `set_auto` raise `NotInHook`, and change nothing."""
    kept: list[Cursor] = []

    async def keep(cursor: Cursor, subject: Subject) -> str | None:
        kept.append(cursor)
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("keep", pre_step=keep)])
    await h.step()
    await h.settle()
    before = h.snapshot()
    stale = kept[0]
    with pytest.raises(tau_next.NotInHook):
        stale.move(0)
    with pytest.raises(tau_next.NotInHook):
        stale.write(Body.Custom("late", ""))
    with pytest.raises(tau_next.NotInHook):
        stale.set_auto(True)
    assert h.snapshot() == before


@scenario
async def test_core_refusals_surface_as_typed_exceptions_in_the_hook(tmp_path: Path) -> None:
    """V2, I3: `DuplicateCall` (a `Results` and an `Assistant`), `NoSuchNode`, `NoSuchCursor`."""
    errors: list[type[Exception]] = []

    async def probe(cursor: Cursor, subject: Subject) -> str | None:
        dup = Body.Results(
            [("c", Outcome.Ok("a"), Author.Tools()), ("c", Outcome.Ok("b"), Author.Tools())]
        )
        twice = Body.Assistant([Part.Call("c", "t", "{}"), Part.Call("c", "t", "{}")], Stop.Tools)
        for attempt in (
            lambda: cursor.write(twice),
            lambda: cursor.write(dup),
            lambda: cursor.append(999, Body.Custom("k", "")),
            lambda: cursor.open(999),
            lambda: cursor.set_label("x", 999),
        ):
            try:
                attempt()
            except Exception as e:
                errors.append(type(e))
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("probe", pre_step=probe)])
    before = h.snapshot()
    await h.step()
    await h.settle()
    assert errors == [
        tau_next.DuplicateCall,
        tau_next.DuplicateCall,
        tau_next.NoSuchNode,
        tau_next.NoSuchNode,
        tau_next.NoSuchCursor,
    ]
    assert h.snapshot() == before, "a refusal changes nothing"


@scenario
async def test_a_hook_may_leave_the_draft_and_cursor_as_it_likes(tmp_path: Path) -> None:
    """No hook edit limits: a `pre_sample` hook moves to the system node and the
    sample goes out from there; core accepts the state the hooks left."""

    async def back(cursor: Cursor, subject: Subject) -> str | None:
        cursor.move(SYS)
        return None

    provider = FakeProvider([text("fresh")])
    h = await Harness.create(
        tmp_path, provider=provider, extensions=[Scripted("back", pre_sample=back)]
    )
    await h.send("hi")
    await h.settle()
    assert provider.requests[0].walk == (0, 1), "I1: `at` as it stood when the sample started"
    assert h.bodies() == [*SYSTEM, Body.Assistant([Part.Text("fresh")], Stop.End)]


@scenario
async def test_a_tool_hook_writes_through_its_node_handle(tmp_path: Path) -> None:
    """A node handle has the tree's writes and none of the cursor's own."""
    wrote: list[int] = []

    class Auditor:
        name = "audit"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            wrote.append(node.append(node.id, Body.Custom("audit", call.tool)))
            assert not hasattr(node, "move")
            return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("1", "echo", "{}")], Stop.Tools), text("done")]),
        tools=[FakeTool("echo", "ok")],
        extensions=[Auditor()],
    )
    await h.send("go")
    await h.settle()
    audit = h.session.tree.node(wrote[0])
    assert audit.body == Body.Custom("audit", "echo")
    assert audit.by == Author.Extension("audit")
    assert audit.parent == 3, "under the Assistant node holding the batch"
    assert audit.id not in [n.id for n in h.session.tree.walk(h.session.cursor(h.cursor).at)], (
        "a side node"
    )


@scenario
async def test_a_frontend_go_to_while_a_cursor_hook_runs_aborts_and_moves_after_it(
    tmp_path: Path,
) -> None:
    """Section 5: a go-to on a busy cursor aborts the step, a hook job out included. The
    hook is never cancelled: it finishes, `post_step` gets `Aborted`, then the move applies."""
    gate = Gate()
    recorder = Recorder(gates={"pre_step": gate})
    h = await Harness.create(tmp_path, extensions=[recorder], gates=[gate])
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    assert not await h.go_to(0), "nothing to cancel: a hook is never cancelled"
    assert h.session.cursor(h.cursor).at == SYS, "the move waits for the step to end"
    gate.release()
    await h.settle()
    assert recorder.endings[0] == Ending.Aborted()
    assert h.session.cursor(h.cursor).at == 0
    assert h.session.cursor(h.cursor).moves[-1].by == Author.Frontend("term")
    assert not h.session.cursor(h.cursor).auto
    assert [i.data for i in h.session.cursor(h.cursor).items] == [ItemData.Input("term", "hi")], (
        "the input stays pending"
    )


@scenario
async def test_a_frontend_go_to_during_a_question_declines_it_then_moves(tmp_path: Path) -> None:
    """Section 7: a question is a hook awaiting; a go-to aborts that step through the
    abort path and declines the question, so the hook finishes with `None`."""
    responses: list[str | None] = []

    async def ask(cursor: Cursor, subject: Subject) -> str | None:
        if not responses:
            responses.append(await cursor.ask("term", "ok?"))
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("asker", pre_step=ask)])
    await h.step()
    await h.settle()
    assert h.driver.open_questions == 1
    assert not await h.go_to(0), "nothing to cancel: the hook finishes"
    await h.settle()
    assert responses == [None], "declined"
    assert h.driver.open_questions == 0
    assert h.session.cursor(h.cursor).at == 0


@scenario
async def test_an_abort_during_a_question_declines_it(tmp_path: Path) -> None:
    """Section 8: abort never cancels a hook; it declines the hook's open question, so the
    hook finishes and `post_step` gets `Aborted`."""
    responses: list[str | None] = []

    async def ask(cursor: Cursor, subject: Subject) -> str | None:
        responses.append(await cursor.ask("term", "send it?"))
        return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Scripted("asker", pre_input=ask), recorder])
    await h.send("hi")
    await h.settle()
    assert [q.text for q in h.driver.questions(h.cursor)] == ["send it?"]
    assert not await h.abort(), "nothing to cancel: the hook finishes"
    await h.settle()
    assert responses == [None]
    assert h.driver.questions(h.cursor) == []
    assert recorder.endings == [Ending.Aborted()]
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_a_frontend_sets_and_removes_a_label(tmp_path: Path) -> None:
    """Labels (section 1): set moves a label, remove drops it, unknown is `NoSuchLabel`."""
    h = await Harness.create(tmp_path)
    other = h.driver.open(SYS, Author.Frontend("term"))
    h.driver.set_label("main", h.cursor)
    h.driver.set_label("main", other)
    assert h.session.label("main") == other
    h.driver.remove_label("main")
    with pytest.raises(tau_next.NoSuchLabel):
        h.driver.remove_label("main")
    await h.settle()


@scenario
async def test_a_post_reply_note_does_not_strand_the_tool_batch(tmp_path: Path) -> None:
    """`next` skips `Custom`, so the batch is read from the node that decided it, not
    from `at`: a note written after a tool-calling reply leaves the batch runnable."""
    batch_nodes: list[int] = []

    async def note(cursor: Cursor, subject: Subject) -> str | None:
        cursor.write(Body.Custom("note", "n"))
        return None

    class Watch:
        name = "watch"

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            batch_nodes.append(node.id)
            return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("1", "echo", "{}")], Stop.Tools), text("done")]),
        tools=[FakeTool("echo", "ok")],
        extensions=[Scripted("n", post_reply=note), Watch()],
    )
    await h.send("go")
    await h.settle()
    assert not h.session.cursor(h.cursor).busy
    assert batch_nodes == [3], "the handle names the Assistant node, not the note"
    assert h.session.tree.node(3).body == Body.Assistant([Part.Call("1", "echo", "{}")], Stop.Tools)
    assert h.bodies()[-5:] == [  # the hook writes a note after each reply
        Body.Assistant([Part.Call("1", "echo", "{}")], Stop.Tools),
        Body.Custom("note", "n"),
        Body.Results([("1", Outcome.Ok("ok"), Author.Tools())]),
        Body.Assistant([Part.Text("done")], Stop.End),
        Body.Custom("note", "n"),
    ]


@scenario
async def test_a_handle_from_an_earlier_hook_is_refused_in_a_later_one(tmp_path: Path) -> None:
    """A handle is retired when its hook returns: step 1's handle cannot act during step 2's
    hook, though core sees a hook job out on that cursor."""
    kept: list[Cursor] = []
    gate = Gate()

    async def keep(cursor: Cursor, subject: Subject) -> str | None:
        if not kept:
            kept.append(cursor)
            return None
        await gate.wait()
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("k", pre_step=keep)], gates=[gate])
    await h.step()
    await h.settle()
    await h.step()
    await h.settle()
    assert gate.held == 1, "the second step's hook is running"
    with pytest.raises(tau_next.NotInHook):
        kept[0].move(0)
    assert h.session.cursor(h.cursor).at == SYS
    gate.release()
    await h.settle()


@scenario
async def test_a_frontends_label_during_a_hook_is_durable(tmp_path: Path) -> None:
    """Only a hook's own calls ride the phase's sync: a frontend call syncs on return,
    whatever hook is out."""
    gate = Gate()
    h = await Harness.create(
        tmp_path, extensions=[Recorder(gates={"pre_step": gate})], gates=[gate]
    )
    await h.step()
    await h.settle()
    assert gate.held == 1
    h.driver.set_label("main", h.cursor)
    await h.crash()
    assert h.session.label("main") == h.cursor


@scenario
async def test_a_write_cannot_deliver_another_cursors_item(tmp_path: Path) -> None:
    """Core validates the resulting state: a delivered id must be pending for that cursor."""
    refused: list[type[Exception]] = []
    victim = 0

    async def steal(cursor: Cursor, subject: Subject) -> str | None:
        if cursor.id == 0:
            for item in cursor.items_of(victim):
                try:
                    cursor.write(Body.Custom("stolen", ""), delivers=[item.id])
                except tau_next.NotPending as e:
                    refused.append(type(e))
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("s", pre_step=steal)])
    victim = h.driver.open(SYS, Author.Frontend("term"))
    await h.add_item("x", "for-victim", cursor=victim)
    await h.settle()
    await h.step()
    await h.settle()
    assert refused == [tau_next.NotPending]
    assert len(h.session.cursor(victim).items) == 1
    assert h.bodies() == SYSTEM


@scenario
async def test_an_input_a_hook_delivered_itself_is_not_delivered_twice(tmp_path: Path) -> None:
    """The hook's own write is the delivery, and it clears the draft too: a draft of `None`
    commits nothing, so the input is delivered once and there is nothing to sample."""

    async def deliver(cursor: Cursor, subject: Subject) -> str | None:
        for item in cursor.items():
            cursor.write(Body.Custom("seen", ""), delivers=[item.id])
        subject.draft = None
        return None

    provider = FakeProvider([])
    h = await Harness.create(
        tmp_path, provider=provider, extensions=[Scripted("d", pre_input=deliver)]
    )
    await h.send("hi")
    await h.settle()
    assert h.bodies() == [*SYSTEM, Body.Custom("seen", "")]
    assert h.session.cursor(h.cursor).items == []
    assert provider.requests == []
    assert [n.delivers for n in h.nodes()[2:]] == [[0]]


@scenario
async def test_a_lost_pre_hook_write_is_lost_with_its_commit(tmp_path: Path) -> None:
    """D11: a phase's writes, pre hooks included, share one sync before its post hook.
    A crash while a later pre hook is held loses the earlier hook's note and the input."""
    gate = Gate()

    async def jot(cursor: Cursor, subject: Subject) -> str | None:
        cursor.append(SYS, Body.Custom("jot", ""))
        return None

    async def hold(cursor: Cursor, subject: Subject) -> str | None:
        await gate.wait()
        return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([text("hello")]),
        extensions=[Scripted("jot", pre_reply=jot), Scripted("hold", pre_reply=hold)],
        gates=[gate],
    )
    await h.send("hi")
    await h.settle()
    assert gate.held == 1
    assert len(h.session.tree) == 4, "in memory: system, the input, and the note"
    await h.crash()
    await h.settle()
    assert len(h.session.tree) == 2, "neither the note nor the input survived"
    assert h.bodies() == SYSTEM


@scenario
async def test_handles_and_the_driver_read_children_counts_and_labels(tmp_path: Path) -> None:
    """The reads that replace probing for `NoSuchNode`."""
    seen: dict[str, object] = {}

    async def look(cursor: Cursor, subject: Subject) -> str | None:
        cursor.append(SYS, Body.Custom("a", ""))
        cursor.append(SYS, Body.Custom("b", ""))
        seen["children"] = cursor.children(SYS)
        seen["count"] = cursor.node_count()
        seen["labels"] = cursor.all_labels()
        with pytest.raises(tau_next.NoSuchNode):
            cursor.children(999)
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("look", pre_step=look)])
    h.driver.set_label("main", h.cursor)
    await h.step()
    await h.settle()
    assert seen == {"children": [2, 3], "count": 4, "labels": {"main": h.cursor}}
    assert h.driver.children(SYS) == [2, 3]
    assert h.driver.children(0) == [SYS]
    assert h.driver.node_count() == 4 == len(h.nodes())
    assert h.driver.all_labels() == {"main": h.cursor}


def stale_delivery(draft: tau_next.Draft | None) -> tau_next.Draft:
    """A reply draft that delivers an item that is not pending anywhere."""
    body = draft.body if draft is not None else Body.Assistant([Part.Text("x")], Stop.End)
    return tau_next.Draft(body, Author.Provider(), [999])


@scenario
async def test_a_pre_reply_draft_delivering_a_non_pending_item_fails_the_step(
    tmp_path: Path,
) -> None:
    """The hook's invalid state ends the step `Failed(NotPending)`; the driver lives on and
    the tree and scratchpad are unchanged by the refused commit."""

    async def bad(cursor: Cursor, subject: Subject) -> str | None:
        subject.draft = stale_delivery(subject.draft)
        return None

    recorder = Recorder("rec")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([text("hello")]),
        extensions=[Scripted("bad", pre_reply=bad), recorder],
    )
    await h.send("hi")
    await h.settle()
    assert recorder.endings == [Ending.Failed("item 999 is not pending for that cursor")]
    assert h.bodies() == [*SYSTEM, Body.User("hi")], "the input committed; the reply did not"
    assert h.session.cursor(h.cursor).items == []
    assert not h.session.cursor(h.cursor).auto
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_a_pre_sample_answer_delivering_a_non_pending_item_fails_the_step(
    tmp_path: Path,
) -> None:
    """A reply that answers the sample is committed at `pre_reply`'s end: same refusal."""

    async def answer(cursor: Cursor, subject: Subject) -> str | None:
        subject.draft = stale_delivery(None)
        return None

    recorder = Recorder("rec")
    provider = FakeProvider([])
    h = await Harness.create(
        tmp_path, provider=provider, extensions=[Scripted("a", pre_sample=answer), recorder]
    )
    await h.send("hi")
    await h.settle()
    assert recorder.endings == [Ending.Failed("item 999 is not pending for that cursor")]
    assert provider.requests == [], "the hook answered the sample"
    assert h.bodies() == [*SYSTEM, Body.User("hi")]
    assert not h.session.cursor(h.cursor).busy


@scenario
async def test_an_input_draft_delivering_a_foreign_item_fails_the_step_and_does_not_loop(
    tmp_path: Path,
) -> None:
    """Core drops only the offered id, and only when a hook delivered it itself; any other
    id reaches `write`, which refuses it. The step fails once: auto-step goes off, so
    nothing offers the input again."""
    other = 0

    async def redirect(cursor: Cursor, subject: Subject) -> str | None:
        foreign = cursor.items_of(other)[0].id
        subject.draft = tau_next.Draft(Body.User("hijack"), Author.Frontend("term"), [foreign])
        return None

    recorder = Recorder("rec")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([]),
        extensions=[Scripted("r", pre_input=redirect), recorder],
    )
    other = h.driver.open(SYS, Author.Frontend("term"))
    await h.add_item("x", "for-other", cursor=other)
    await h.settle()
    await h.send("hi")
    await h.settle()
    assert recorder.endings[-1] == Ending.Failed("item 0 is not pending for that cursor")
    assert not h.session.cursor(h.cursor).auto
    assert h.bodies() == SYSTEM
    assert len(h.session.cursor(h.cursor).items) == 1, "the input is still pending"
    assert len(h.session.cursor(other).items) == 1
    runs = recorder.points.count("pre_input")
    h.clock.advance(1_000)
    await h.settle()
    assert recorder.points.count("pre_input") == runs, "nothing retried"


@scenario
async def test_a_retired_handle_refuses_every_write_and_ask_but_still_reads(
    tmp_path: Path,
) -> None:
    """Once its hook coroutine returns a handle is dead: reads work, all else is `NotInHook`.
    A tool hook's node handle follows the same rule."""
    kept: list[Cursor] = []
    nodes: list[NodeHandle] = []

    class Keeper:
        name = "keeper"

        async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
            kept.append(cursor)
            return None

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            nodes.append(node)
            return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("1", "echo", "{}")], Stop.Tools), text("ok")]),
        tools=[FakeTool("echo", "ok")],
        extensions=[Keeper()],
    )
    await h.send("go")
    await h.settle()
    before = h.snapshot()
    data = Body.Custom("late", "")
    for handle in (kept[0], nodes[0]):
        assert handle.node_count() == len(h.nodes()), "reads still work"
        writes = (
            lambda hd=handle: hd.append(SYS, data),
            lambda hd=handle: hd.open(SYS),
            lambda hd=handle: hd.add_item(0, "d"),
            lambda hd=handle: hd.remove_item(0),
            lambda hd=handle: hd.set_label("late", 0),
            lambda hd=handle: hd.remove_label("late"),
        )
        for write in writes:
            with pytest.raises(tau_next.NotInHook):
                write()
    with pytest.raises(tau_next.NotInHook):
        await kept[0].ask("term", "q?")
    assert h.driver.open_questions == 0
    assert h.snapshot() == before


@scenario
async def test_a_hook_that_delivers_the_offered_input_but_keeps_the_draft_fails_the_step(
    tmp_path: Path,
) -> None:
    """The draft still delivers an item the hook's write removed: the commit is refused
    (`NotPending`), through the same path as any bad draft."""

    async def deliver(cursor: Cursor, subject: Subject) -> str | None:
        for item in cursor.items():
            cursor.write(Body.Custom("seen", ""), delivers=[item.id])
        return None

    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Scripted("d", pre_input=deliver), recorder])
    await h.send("hi")
    await h.settle()
    (ending,) = recorder.endings
    assert isinstance(ending, Ending.Failed)
    assert "not pending" in ending.error
    assert Body.User("hi") not in h.bodies()
    assert not h.session.cursor(h.cursor).auto


@scenario
async def test_a_pre_reply_hook_that_clears_the_draft_drops_the_reply(tmp_path: Path) -> None:
    """`None` commits nothing for the reply too: no `Assistant` node, and the step is not failed."""

    def drop(subject: Subject | CallDraft) -> None:
        assert isinstance(subject, Subject)
        subject.draft = None

    recorder = Recorder(on={"pre_reply": drop})
    h = await Harness.create(tmp_path, provider=FakeProvider([text("lost")]), extensions=[recorder])
    await h.input("hi")
    await h.settle()
    await h.step()
    await h.settle()
    assert h.bodies()[-1] == Body.User("hi")
    assert recorder.endings[-1] == Ending.Done()


@scenario
async def test_a_hook_appends_a_detached_root_without_moving_any_cursor(tmp_path: Path) -> None:
    """A root is made only by `append_root`, which takes a `Config`: it has no parent and
    leaves the cursor where it was. There is no way to ask for a root of another kind."""
    made: list[int] = []

    async def detach(cursor: Cursor, subject: Subject) -> str | None:
        made.append(cursor.append_root(Config("detached")))
        return None

    h = await Harness.create(tmp_path, extensions=[Scripted("d", pre_step=detach)])
    at = h.session.cursor(h.cursor).at
    await h.step()
    await h.settle()
    (root,) = made
    node = h.session.tree.node(root)
    assert node.parent is None
    assert node.body == Body.Config(Config("detached"))
    assert node.by == Author.Extension("d")
    assert h.session.cursor(h.cursor).at == at
    assert h.session.tree.lca(root, at) is None


@scenario
async def test_an_assistant_nodes_meta_survives_a_reload(tmp_path: Path) -> None:
    """`meta` is opaque JSON text a provider attaches to its reply; the log keeps it verbatim."""
    meta = '{"model": "m-1", "id": "r1", "usage": {"total_tokens": 9}}'
    provider = FakeProvider([Reply([Part.Text("hi")], finish_reason="stop", meta=meta)])
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("hello")
    await h.settle()
    reply = h.bodies()[-1]
    assert reply == Body.Assistant([Part.Text("hi")], Stop.End, "stop", meta)
    await h.close()
    again = await Harness.load(tmp_path)
    loaded = again.bodies()[-1]
    assert isinstance(loaded, Body.Assistant)
    assert loaded.meta == meta
    assert loaded == reply
    await again.close()
