"""`/undo` and the undo-marker convention (D33): going back over the last change on a branch.

`/undo` walks back from the cursor to the nearest undo marker of the priority asked (`move` by
default), or for `move` to the last `User` node (edit-last). Markers are `Custom` nodes the
extension that made the change wrote; compaction, prune and `/clear` write them."""

import json
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, build, say, scenario
from shipped import shipped

from tau import (
    Author,
    Body,
    Busy,
    CommandFailed,
    Cursor,
    DefinitionError,
    Draft,
    Ending,
    Node,
    Part,
    Prune,
    Stop,
    Subject,
    UndoMarker,
)


def at(h: Harness, cursor: int | None = None) -> int:
    return h.session.cursor(h.cursor if cursor is None else cursor).at


def tip(h: Harness, cursor: int | None = None) -> Node:
    return h.session.tree.node(at(h, cursor))


async def talk(h: Harness, *texts: str) -> None:
    for text in texts:
        await h.send(text)
        await h.settle()


async def undo(h: Harness, cursor: int | None = None, **args: object) -> str | None:
    result = await h.run("undo", args, cursor)
    await h.settle()
    return result


def replies(*texts: str) -> FakeProvider:
    return FakeProvider([say(t) for t in texts])


# Edit-last


@scenario
async def test_undo_goes_back_before_the_last_message_and_gives_it_back(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies("one", "two"), extensions=[shipped("undo")])
    await talk(h, "first", "second")
    first_reply = h.session.tree.walk(at(h))[-3]
    result = await undo(h)
    assert at(h) == first_reply.id, "the parent of the last User node"
    assert result == f"back to node {first_reply.id}, before your message:\n\nsecond"
    view = await h.view()
    assert not view.busy
    assert not h.session.cursor(h.cursor).auto, "a go-to turns auto-step off"
    assert len(h.provider.requests) == 2, "going back asks the model nothing"
    await h.close()


@scenario
async def test_undo_again_goes_back_one_more_message(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies("one", "two"), extensions=[shipped("undo")])
    await talk(h, "first", "second")
    await undo(h)
    result = await undo(h, json=True)
    assert result is not None
    answer = json.loads(result)
    assert answer["to"] == 1, "the System node: before the first message"
    assert answer["user"]["text"] == "first"
    assert answer["marker"] is None
    with pytest.raises(CommandFailed, match="nothing to undo on this branch"):
        await undo(h)
    await h.close()


class Shouter:
    """A `pre_input` hook that changes what a person typed, so the node's `typed` keeps it."""

    name = "shouter"

    async def pre_input(self, cursor: Cursor, subject: Subject) -> None:
        draft = subject.draft
        assert draft is not None
        assert isinstance(draft.body, Body.User)
        subject.draft = Draft(Body.User(draft.body.text.upper()), draft.delivers, draft.by)


@scenario
async def test_the_json_answer_gives_back_what_was_typed_not_what_a_hook_made_of_it(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path, provider=replies("ok"), extensions=[Shouter(), shipped("undo")]
    )
    await talk(h, "quiet please")
    user = h.session.tree.walk(at(h))[-2]
    assert isinstance(user.body, Body.User)
    assert user.body.text == "QUIET PLEASE"
    result = await undo(h, json=True)
    assert result is not None
    assert json.loads(result) == {
        "from": user.id + 1,
        "to": 1,
        "marker": None,
        "label": "your last message",
        "user": {"node": user.id, "text": "quiet please"},
    }
    await h.close()


# Markers: prune, compact, clear


async def pruned(tmp_path: Path) -> tuple[Harness, int]:
    """Three turns with a tool call each, pruned: the harness, and where the cursor was."""
    script: list[Reply] = []
    for n in range(3):
        script += [Reply([Part.Call(f"c{n}", "work", '{"a": 1}')], Stop.Tools), say(f"done {n}")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=[FakeTool("work", "the result")],
        extensions=[Prune(), shipped("undo")],
    )
    h.session.set_auto(h.cursor, True)
    await talk(h, "one", "two", "three")
    before = at(h)
    result = await h.run("prune")
    await h.settle()
    assert result is not None
    assert result.startswith("pruned")
    return h, before


@scenario
async def test_prune_ends_its_line_in_a_marker_back_to_where_the_cursor_was(tmp_path: Path) -> None:
    h, before = await pruned(tmp_path)
    marker = tip(h)
    assert marker.by == Author.Extension("prune")
    assert UndoMarker.of(marker.body) == UndoMarker("/prune", before, "prune")
    view = await h.view()
    assert not view.busy, "the marker decides nothing: the line's reply does, and it rests"
    await h.close()


@scenario
async def test_undo_after_prune_returns_to_the_old_line_then_edits_its_last_message(
    tmp_path: Path,
) -> None:
    h, before = await pruned(tmp_path)
    marker = at(h)
    assert await undo(h) == f"undid /prune: back to node {before}"
    assert at(h) == before
    assert all(n.id != marker for n in h.session.tree.walk(at(h))), "the marker is off the walk"
    result = await undo(h)
    assert result is not None
    assert result.endswith("before your message:\n\nthree"), "then the old line's last message"
    await h.close()


@scenario
async def test_undo_after_compact_returns_to_the_line_that_was_summarized(tmp_path: Path) -> None:
    summary = "Goal: the task."
    h = await Harness.create(
        tmp_path,
        provider=replies("a" * 400, "b" * 400, summary),
        extensions=[shipped("compaction"), shipped("undo")],
    )
    await talk(h, "go 1" + "y" * 400, "go 2" + "z" * 400)
    before = at(h)
    result = await h.run("compact")
    await h.settle()
    assert result is not None
    assert result.startswith("compacted"), result
    assert UndoMarker.of(tip(h).body) == UndoMarker("/compact", before, "compact")
    assert await undo(h) == f"undid /compact: back to node {before}"
    assert at(h) == before
    await h.close()


@scenario
async def test_undo_after_clear_returns_to_the_conversation(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=replies("one"), extensions=[shipped("session"), shipped("undo")]
    )
    await talk(h, "hello")
    before = at(h)
    await h.run("clear")
    await h.settle()
    assert [type(n.body) for n in h.session.tree.walk(at(h))] == [
        Body.Config,
        Body.System,
        Body.Custom,
    ]
    assert await undo(h) == f"undid /clear: back to node {before}"
    assert at(h) == before
    await h.close()


# Who may undo, and where


@scenario
async def test_undo_refuses_a_busy_cursor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("one"), Reply([Part.Text("two")], gate=gate)]),
        extensions=[shipped("undo")],
    )
    await talk(h, "first")
    await h.send("second")
    await h.settle()
    held = at(h)
    with pytest.raises(Busy):
        await h.run("undo")
    gate.release()
    await h.settle()
    assert at(h) != held, "the step went on"
    await h.close()


@scenario
async def test_two_clients_undoing_the_same_change_undo_it_once(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=replies("one", "two"), extensions=[shipped("undo")])
    await talk(h, "first", "second")
    seen = at(h)
    other = await h.connect("other")
    await other.run(h.cursor, "undo", {"at": seen})
    await h.settle()
    once = at(h)
    with pytest.raises(CommandFailed, match=f"is at node {once}, not {seen}: it moved"):
        await h.run("undo", {"at": seen})
    assert at(h) == once
    await other.close()
    await h.close()


@scenario
async def test_undo_follows_only_its_own_branch(tmp_path: Path) -> None:
    """A marker on another cursor's branch is not on this walk: each cursor undoes its own."""
    h = await Harness.create(
        tmp_path,
        provider=replies("one", "side", "after"),
        extensions=[shipped("session"), shipped("undo")],
    )
    await talk(h, "first")
    fork = at(h)
    side = await h.client.open(fork)
    await h.settle()
    await talk(h, "on main")
    await h.run("clear")
    await h.settle()
    await h.send("on the side", side)
    await h.settle()
    result = await undo(h, side)
    assert result is not None
    assert result.endswith("before your message:\n\non the side"), "edit-last, not main's /clear"
    assert at(h, side) == fork
    assert (await undo(h)).startswith("undid /clear")  # type: ignore[union-attr]
    await h.close()


# Other priorities, and the owner's reversal


class Checkpoints:
    """Writes a `file` marker after each tool result, returning to the node before the batch,
    and reverses it by recording it (the shape of a shadow-git checkpoint, without git)."""

    name = "checkpoints"

    def __init__(self, *, reverses: bool = True) -> None:
        self.reversed: list[UndoMarker] = []
        if reverses:
            self.reverse = self._reverse

    async def post_result(self, cursor: Cursor, subject: Subject) -> None:
        assert subject.node is not None
        batch = subject.node.parent
        assert batch is not None
        marker = UndoMarker("edit of a.py", batch, "checkpoint", "file", {"commit": "abc123"})
        cursor.write(marker.body())

    async def _reverse(self, cursor: Cursor, marker: UndoMarker) -> None:
        self.reversed.append(marker)


async def checkpointed(tmp_path: Path, checkpoints: Checkpoints) -> Harness:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("c1", "work", "{}")], Stop.Tools), say("edited")]),
        tools=[FakeTool("work", "done")],
        extensions=[checkpoints, shipped("undo")],
    )
    h.session.set_auto(h.cursor, True)
    await talk(h, "edit a.py")
    return h


@scenario
async def test_a_plain_undo_passes_over_a_file_marker_to_edit_last(tmp_path: Path) -> None:
    checkpoints = Checkpoints()
    h = await checkpointed(tmp_path, checkpoints)
    result = await undo(h)
    assert result is not None
    assert result.endswith("before your message:\n\nedit a.py")
    assert checkpoints.reversed == [], "a file marker is followed only when asked for"
    await h.close()


@scenario
async def test_undo_file_has_the_owner_reverse_it_and_rests_mid_turn(tmp_path: Path) -> None:
    checkpoints = Checkpoints()
    h = await checkpointed(tmp_path, checkpoints)
    walk = h.session.tree.walk(at(h))
    marker = next(n for n in walk if UndoMarker.of(n.body) is not None)
    found = UndoMarker.of(marker.body)
    assert found is not None
    assert found.extra == {"commit": "abc123"}
    assert marker.by == Author.Extension("checkpoints")
    requests = len(h.provider.requests)
    assert await undo(h, priority="file") == f"undid edit of a.py: back to node {found.return_to}"
    assert checkpoints.reversed == [found]
    assert at(h) == found.return_to, "the batch, before its results"
    assert len(h.provider.requests) == requests, "going back mid-turn runs nothing"
    view = await h.view()
    assert not view.busy
    assert isinstance(view.ending, Ending.Done), "the command's step ended, not halted"
    assert not view.auto
    await h.close()


@scenario
async def test_a_file_marker_whose_owner_cannot_reverse_it_is_not_undone(tmp_path: Path) -> None:
    h = await checkpointed(tmp_path, Checkpoints(reverses=False))
    here = at(h)
    with pytest.raises(CommandFailed, match="which is not loaded or has no reverse"):
        await h.run("undo", {"priority": "file"})
    assert at(h) == here
    await h.close()


def test_a_reverse_that_is_not_async_is_refused() -> None:
    class Sync:
        name = "sync"

        def reverse(self, cursor: Cursor, marker: UndoMarker) -> None: ...

    with pytest.raises(DefinitionError, match="`reverse` must be an async method"):
        build([Sync()])


# The marker's shape


def test_a_marker_reads_back_what_was_written() -> None:
    marker = UndoMarker("/x", 4, "x", "file", {"commit": "c"})
    body = marker.body()
    assert isinstance(body, Body.Custom)
    assert body.kind == "undo"
    assert json.loads(body.data) == {
        "label": "/x",
        "return_to": 4,
        "reverses": "x",
        "priority": "file",
        "commit": "c",
    }
    assert UndoMarker.of(body) == marker
    assert UndoMarker.of(Body.Custom("other", "{}")) is None
    assert UndoMarker.of(Body.User("hi")) is None


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ("nope", "not JSON"),
        ("[1]", "a JSON object"),
        ('{"label": "x", "return_to": 1}', "lacks"),
        ('{"label": "", "return_to": 1, "reverses": "x"}', "label is a non-empty"),
        ('{"label": "x", "return_to": "1", "reverses": "x"}', "return_to is a node id"),
        ('{"label": "x", "return_to": 1, "reverses": "x", "priority": "Big"}', "priority"),
    ],
)
def test_a_malformed_marker_is_an_error_naming_what_is_wrong(data: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        UndoMarker.of(Body.Custom("undo", data))


@scenario
async def test_without_undo_markers_are_inert(tmp_path: Path) -> None:
    """Nothing breaks without the extension: a marker never reaches the model, and the next
    message goes on from it."""
    h = await Harness.create(
        tmp_path, provider=replies("one", "two"), extensions=[shipped("session")]
    )
    await talk(h, "hello")
    await h.run("clear")
    await h.settle()
    await talk(h, "again")
    sent = [h.session.tree.node(i).body for i in h.provider.requests[1].walk]
    assert any(isinstance(b, Body.Custom) for b in sent), "the provider is handed the walk"
    assert [b.text for b in sent if isinstance(b, Body.User)] == ["again"]
    await h.close()
