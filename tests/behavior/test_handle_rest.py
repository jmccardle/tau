"""A handle waits on another cursor and aborts it (D17 section 3): `rest` and `abort`."""

import asyncio
import threading
from pathlib import Path

from fakes import Fail, FakeProvider, Gate, Recorder, Reply
from harness import Harness, say, scenario

from tau import (
    Body,
    Cursor,
    CursorId,
    Ending,
    NodeHandle,
    Outcome,
    Part,
    Stop,
    Subject,
    TreeHandle,
)


def calls(tool: str) -> Reply:
    return Reply([Part.Call("c", tool, "{}")], Stop.Tools)


class Boss:
    """An extension whose tools and commands drive cursor `target`, and keep what they saw."""

    name = "boss"

    def __init__(self, target: int = 1) -> None:
        self.target = target
        self.seen: list[Ending | None] = []
        self.threads: list[int] = []
        self.refused: list[str] = []  # by a tool's handle
        self.hook_refused: list[str] = []  # by a cursor hook's
        self.tools = (self.relay, self.look, self.stop, self.stop_self, self.wait_for_self)
        self.commands = (self.peek, self.cut_off)

    async def relay(self, node: NodeHandle) -> str:
        """Send to the target and wait for its step to end."""
        node.send(self.target, "again")
        self.seen.append(await node.rest(self.target))
        return "relayed"

    async def look(self, node: NodeHandle) -> str:
        """Wait for the target to rest."""
        self.seen.append(await node.rest(self.target))
        return "looked"

    def stop(self, node: NodeHandle) -> str:
        """Abort the target, from a worker thread."""
        self.threads.append(threading.get_ident())
        node.abort(self.target)
        return "stopped"

    def stop_self(self, node: NodeHandle) -> str:
        """Abort the cursor this call is a step of."""
        try:
            node.abort(node.cursor_id)
        except ValueError as e:
            self.refused.append(str(e))
        return "tried"

    async def wait_for_self(self, node: NodeHandle) -> str:
        """Wait for the cursor this call is a step of."""
        try:
            await node.rest(node.cursor_id)
        except ValueError as e:
            self.refused.append(str(e))
        return "tried"

    async def peek(self, tree: TreeHandle, cursor: CursorId) -> str:
        """Wait for a cursor to rest."""
        self.seen.append(await tree.rest(cursor))
        return "rested"

    def cut_off(self, tree: TreeHandle, cursor: CursorId) -> None:
        """Abort a cursor."""
        tree.abort(cursor)

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        try:
            cursor.abort(cursor.id)
        except ValueError as e:
            self.hook_refused.append(str(e))
        try:
            await cursor.rest(cursor.id)
        except ValueError as e:
            self.hook_refused.append(str(e))


def outcome(h: Harness, index: int = -2) -> Outcome:
    results = h.bodies()[index]
    assert isinstance(results, Body.Results)
    return results.outcomes[0][1]


async def with_other(tmp_path: Path, script: list[Reply | Fail], boss: Boss) -> tuple[Harness, int]:
    """A harness running `boss`, and a second cursor, open at the same node."""
    h = await Harness.create(tmp_path, provider=FakeProvider(script), extensions=[boss])
    other = await h.client.open(h.session.cursor(h.cursor).at)
    assert other == boss.target
    await h.settle()
    return h, other


@scenario
async def test_send_then_rest_waits_for_the_step_the_send_caused(tmp_path: Path) -> None:
    boss = Boss()
    script: list[Reply | Fail] = [say("earlier"), calls("relay"), Fail("boom"), say("done")]
    h, other = await with_other(tmp_path, script, boss)
    await h.send("first", cursor=other)
    await h.settle()
    assert (await h.view(other)).ending == Ending.Done(), "the ending a stale rest would give"
    await h.send("go")
    await h.settle()
    assert boss.seen == [Ending.Failed("boom")], "the new step's ending, not the last one"
    assert outcome(h) == Outcome.Ok("relayed")
    await h.close()


@scenario
async def test_rest_on_an_idle_cursor_returns_its_last_ending_at_once(tmp_path: Path) -> None:
    boss = Boss()
    h, other = await with_other(tmp_path, [say("earlier"), calls("look"), say("done")], boss)
    await h.send("first", cursor=other)
    await h.settle()
    await h.send("go")
    await h.settle()
    assert boss.seen == [Ending.Done()]
    await h.close()


@scenario
async def test_rest_on_a_cursor_that_is_aborted_returns_aborted(tmp_path: Path) -> None:
    boss = Boss()
    gate = Gate()
    held = Reply([Part.Text("hel"), Part.Text("lo")], Stop.End, gate, gate_after=1)
    h, other = await with_other(tmp_path, [held, calls("look"), say("done")], boss)
    await h.send("first", cursor=other)
    await h.settle()
    assert gate.held == 1
    await h.send("go")
    await h.settle()  # the tool waits on the cursor that waits at its gate
    assert boss.seen == []
    assert h.session.cursor(h.cursor).busy
    await h.abort(other)
    await h.settle()
    assert boss.seen == [Ending.Aborted()]
    assert not h.session.cursor(h.cursor).busy
    await h.close()


@scenario
async def test_a_sync_tool_aborts_another_cursor_from_its_worker_thread(tmp_path: Path) -> None:
    boss = Boss()
    gate = Gate()
    held = Reply([Part.Text("hel"), Part.Text("lo")], Stop.End, gate, gate_after=1)
    recorder = Recorder()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([held, calls("stop"), say("done")]),
        extensions=[boss, recorder],
    )
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.send("first", cursor=other)
    await h.settle()
    assert gate.held == 1
    await h.send("go")
    await h.settle()
    assert boss.threads, "the tool ran"
    assert boss.threads[0] != threading.get_ident(), "on a worker thread"
    assert (await h.view(other)).ending == Ending.Aborted()
    assert h.bodies(other)[-1] == Body.Assistant([Part.Text("hel")], Stop.Interrupted)
    assert not h.session.cursor(other).auto
    assert outcome(h) == Outcome.Ok("stopped")
    await h.close()


@scenario
async def test_a_handle_refuses_to_rest_or_abort_its_own_cursor(tmp_path: Path) -> None:
    boss = Boss()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([calls("stop_self"), calls("wait_for_self"), say("done")]),
        extensions=[boss],
    )
    await h.send("go")
    await h.settle()

    def why(refused: list[str]) -> set[str]:
        return {"abort" if "would cancel" in r else "rest" for r in refused}

    assert why(boss.hook_refused) == {"abort", "rest"}, "a Cursor hook refuses both"
    assert why(boss.refused) == {"abort", "rest"}, "and so does a tool's node handle"
    assert all("cursor 0" in r for r in boss.refused + boss.hook_refused), "each names the cursor"
    assert h.bodies()[-1] == Body.Assistant([Part.Text("done")], Stop.End), "nothing was aborted"
    await h.close()


@scenario
async def test_a_tree_handle_has_no_cursor_of_its_own_so_refuses_none(tmp_path: Path) -> None:
    boss = Boss()
    gate = Gate()
    held = Reply([Part.Text("hel"), Part.Text("lo")], Stop.End, gate, gate_after=1)
    h, other = await with_other(tmp_path, [held], boss)
    await h.run("peek", {"cursor": h.cursor})  # a command may wait on the cursor it runs on
    await h.send("first", cursor=other)
    await h.settle()
    await h.run("cut_off", {"cursor": other})
    await h.settle()
    assert (await h.view(other)).ending == Ending.Aborted()
    assert boss.seen == [None], "cursor 0 has not stepped: its ending is not known"
    await h.close()


class Fan:
    """A tool that waits on two cursors at once."""

    name = "fan"

    def __init__(self) -> None:
        self.seen: list[tuple[Ending | None, ...]] = []
        self.tools = (self.look_both,)

    async def look_both(self, node: NodeHandle) -> str:
        """Wait for cursors 1 and 2 to rest."""
        self.seen.append(tuple(await asyncio.gather(node.rest(1), node.rest(2))))
        return "both"


@scenario
async def test_one_tool_waiting_on_two_cursors_is_one_parked_effect(tmp_path: Path) -> None:
    """`settle` counts effects, not `rest` calls: a tool that gathers two is parked once."""
    fan, gate = Fan(), Gate()
    held = Reply([Part.Text("hello")], gate=gate)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([held, held, calls("look_both"), say("done")]),
        extensions=[fan],
    )
    for _ in range(2):
        await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.send("a", cursor=1)
    await h.send("b", cursor=2)
    await h.settle()
    assert gate.held == 2
    await h.send("go")
    await h.settle()  # two samples at the gate, one tool waiting on both: nothing else runs
    assert gate.held == 2
    assert fan.seen == []
    gate.release()
    await h.settle()
    assert fan.seen == [(Ending.Done(), Ending.Done())]
    assert outcome(h) == Outcome.Ok("both")
    await h.close()
