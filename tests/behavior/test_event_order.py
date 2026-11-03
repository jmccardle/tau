"""The order a client's stream keeps when state events replace their own earlier copy (D16
section 5): the snapshot ends at its `Labels` with every cursor in it, a state never points at
nodes the client was not sent, and a `Live` delta follows the state of the step it belongs to."""

import asyncio
from pathlib import Path

from fakes import FakeProvider, Gate, Reply
from harness import Harness, scenario

from tau import (
    Appended,
    Client,
    CursorGone,
    CursorView,
    Event,
    Labels,
    Live,
    Part,
    TextDelta,
)

SYS = 1  # the system node's id: a new session is Config 0, System 1


async def until_labels(client: Client) -> list[Event]:
    """The snapshot: the events up to the first `Labels`, which end it."""
    events: list[Event] = []
    async with asyncio.timeout(2):
        async for event in client:
            events.append(event)
            if isinstance(event, Labels):
                return events
    raise AssertionError("the stream ended before its snapshot did")


def named(events: list[Event]) -> set[int]:
    """The cursors a client has been told of: open (`CursorView`) or closed (`CursorGone`)."""
    return {e.cursor for e in events if isinstance(e, CursorView | CursorGone)}


@scenario
async def test_a_snapshot_with_a_state_changed_after_it_still_has_every_cursor_before_its_labels(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    other = await h.client.open(SYS)
    await h.client.set_label(other, "side")
    client = await h.connect("late")  # not yet read: its snapshot is waiting
    await h.input("hello")  # replaces the first cursor's state
    await h.client.close_cursor(other)  # replaces the other's with a CursorGone
    await h.settle()
    snapshot = await until_labels(client)
    assert named(snapshot) == {h.cursor, other}, "Labels names a cursor the client was not sent"
    await h.close()


@scenario
async def test_a_state_never_precedes_the_nodes_it_points_at(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("hello")])]))
    client = await h.connect("late")  # its snapshot holds the state at the system node
    await h.send("hi")  # the state changes, then nodes are appended, then it changes again
    await h.settle()
    known = SYS  # the nodes it could read by id when it connected
    await h.close()
    views = 0
    async for event in client:
        if isinstance(event, Appended):
            known = max(known, *(n.id for n in event.nodes))
        elif isinstance(event, CursorView):
            views += 1
            assert event.at <= known, f"state at node {event.at}, but nodes are sent up to {known}"
    assert known > SYS, "the scenario appended nodes"
    assert views, "and changed the state after them"


@scenario
async def test_a_live_delta_follows_the_state_that_says_its_step_is_running(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([TextDelta("hel"), Part.Text("hel")], gate=gate, gate_after=1)
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    await h.send("hi")
    await h.settle()  # the step is running, parked at the gate
    client = await h.connect("late", watching=[h.cursor])  # catches up on the delta
    await h.input("more")  # replaces the running state, after the delta was queued
    await h.settle()
    events: list[Event] = []
    async with asyncio.timeout(2):
        while not any(isinstance(e, CursorView) and e.items for e in events):
            events.append(await anext(client))
    gate.release()
    views = [i for i, e in enumerate(events) if isinstance(e, CursorView)]
    delta = next(i for i, e in enumerate(events) if isinstance(e, Live))
    first = events[views[0]]
    assert isinstance(first, CursorView)
    assert first.busy
    assert views[0] < delta, "the delta came before any state of its cursor"
    await h.close()
