"""examples/speculative/ralph.py through the public API: fresh contexts looped by auto-step."""

import asyncio
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Appended,
    Author,
    Body,
    Busy,
    Config,
    CursorView,
    Ending,
    Event,
    Next,
    Notice,
    Part,
    StepLimit,
    Stop,
    WriteTool,
    model_config,
)
from tau.commandline import parse


def load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "examples" / "speculative" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"speculative_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ralph = load("ralph")
ROOT = 1  # the System node: where the cursor begins


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


def write(path: str, content: str) -> Reply:
    """A reply that calls the base `write` tool: the model editing a file."""
    args = json.dumps({"path": path, "content": content})
    return Reply([Part.Call("w", "write", args)], Stop.Tools)


def config(**extensions: dict[str, object]) -> Config:
    return model_config(FAKE_MODEL, extensions)


def prompt_file(tmp_path: Path, text: str = "task 1") -> Path:
    path = tmp_path / "work" / "PROMPT.md"
    path.parent.mkdir()
    path.write_text(text)
    return path


def users(h: Harness) -> list[str]:
    """The prompts the loop wrote under the root, one per iteration."""
    tree = h.session.tree
    return [
        b.text for b in (tree.node(c).body for c in tree.children(ROOT)) if isinstance(b, Body.User)
    ]


def shown(events: list[Event]) -> list[str]:
    """Each line ralph showed, in order, from the views a client was sent (a repeat is one)."""
    lines: list[str] = []
    for e in events:
        if isinstance(e, CursorView):
            for who, text in e.shown:
                if who == "ralph" and text not in lines[-1:]:
                    lines.append(text)
    return lines


@scenario
async def test_it_loops_fresh_contexts_until_the_model_writes_done(tmp_path: Path) -> None:
    prompt = prompt_file(tmp_path)
    script = [
        write("PROMPT.md", "task 2"),  # iteration 1 edits its own prompt
        say("did task 1"),
        say("did task 2"),
        write("DONE", "yes"),  # iteration 3 says it is finished
        say("all done"),
    ]
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider(script),
        tools=[WriteTool(prompt.parent)],
        extensions=[ralph.Ralph()],
    )
    assert await h.run("ralph", {"prompt": str(prompt)}) == f"ralph: {prompt} from node {ROOT}"
    await h.settle()

    assert [len(r.walk) for r in h.provider.requests] == [3, 5, 3, 3, 5], "every iteration is fresh"
    assert users(h) == ["task 1", "task 2", "task 2"], "the file is read again each iteration"
    assert not h.session.cursor(h.cursor).auto
    assert h.session.cursor(h.cursor).next == Next.Wait
    assert shown(h.heard) == [f"iteration {i} of 25" for i in (1, 2, 3)]
    assert (await h.view()).shown == (), "the line is cleared when the loop stops"
    assert h.notices() == ["ralph: stopped after 3 iterations: DONE exists"]
    await h.close()


@scenario
async def test_the_cursor_never_spins_because_every_iteration_moves_it(tmp_path: Path) -> None:
    prompt = prompt_file(tmp_path)
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([say(f"r{i}") for i in range(6)]),
        extensions=[ralph.Ralph()],
        config=config(ralph={"max_iterations": 6}),
    )
    watcher = await h.connect("tui")
    views: list[CursorView] = []

    async def listen() -> None:
        async for event in watcher:
            if isinstance(event, CursorView) and event.cursor == h.cursor:
                views.append(event)

    listener = asyncio.ensure_future(listen())
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    await watcher.close()
    await listener

    assert len(h.provider.requests) == 6
    running = [v for v in views if v.auto]
    assert all(v.busy for v in running[1:]), "auto-step stayed on, and busy, through six iterations"
    assert not views[-1].auto
    assert len(users(h)) == 6, "six branches from one root"
    await h.close()


@scenario
async def test_a_frontend_counts_iterations_from_the_nodes_the_loop_wrote(tmp_path: Path) -> None:
    """The count is shown to every client; a client also has the nodes."""
    prompt = prompt_file(tmp_path)
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([say("a"), say("b"), say("c")]),
        extensions=[ralph.Ralph()],
        config=config(ralph={"max_iterations": 3}),
    )
    tui = await h.connect("tui")
    seen: list[Event] = []

    async def listen() -> None:
        async for event in tui:
            seen.append(event)

    listener = asyncio.ensure_future(listen())
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    await tui.close()
    await listener
    nodes = [n for e in seen if isinstance(e, Appended) for n in e.nodes]
    mine = Author.Extension("ralph")
    assert sum(n.by == mine and isinstance(n.body, Body.User) for n in nodes) == 3
    assert shown(seen) == [f"iteration {i} of 3" for i in (1, 2, 3)], "the tui is told too"
    assert [e.text for e in seen if isinstance(e, Notice)] == h.notices()
    assert h.notices() == ["ralph: stopped after 3 iterations: max_iterations 3"]
    await h.close()


@scenario
async def test_step_limit_counts_steps_not_iterations(tmp_path: Path) -> None:
    """One tool call per iteration is two steps, so a limit of 4 is two iterations."""
    prompt = prompt_file(tmp_path)
    call = Reply([Part.Call("c", "t", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([call, say("a"), call, say("b"), call, say("c")]),
        tools=[FakeTool("t", "ok")],
        extensions=[StepLimit(), ralph.Ralph()],
        config=config(step_limit={"limit": 4}),
    )
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    assert len(h.provider.requests) == 4
    assert len(users(h)) == 2
    assert not h.session.cursor(h.cursor).auto
    assert h.session.cursor(h.cursor).next == Next.Wait, "it rests on the finished reply"
    await h.close()


@scenario
async def test_listed_before_step_limit_the_loop_leaves_a_prompt_nobody_sent(
    tmp_path: Path,
) -> None:
    """Hook order decides: ralph restarts first, then StepLimit turns auto-step off."""
    prompt = prompt_file(tmp_path)
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([say(f"r{i}") for i in range(6)]),
        extensions=[ralph.Ralph(), StepLimit()],
        config=config(step_limit={"limit": 3}),
    )
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    assert len(h.provider.requests) == 3
    assert h.session.cursor(h.cursor).next == Next.Sample, "the fourth prompt is written, unsent"
    assert not h.session.cursor(h.cursor).auto
    await h.close()


@scenario
async def test_config_before_the_loop_holds_for_every_iteration_and_during_it_is_busy(
    tmp_path: Path,
) -> None:
    prompt = prompt_file(tmp_path)
    gate = Gate()
    script = [Reply([Part.Text("r0")], gate=gate), *[say(f"r{i}") for i in range(1, 6)]]
    h = await Harness.create(
        tmp_path / "s", provider=FakeProvider(script), extensions=[ralph.Ralph()]
    )
    await h.configure("ralph", {"max_iterations": 2})  # a Config node under the System node
    with pytest.raises(Busy):  # configure returns with the command: its step is still running
        await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    await h.run("ralph", {"prompt": str(prompt)})  # its root is that Config node
    await h.settle()
    with pytest.raises(Busy):
        await h.configure("ralph", {"max_iterations": 5})
    with pytest.raises(Busy):
        await h.run("ralph", {"prompt": str(prompt)})
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 2, "the Config node is on the root's walk, so it held"
    await h.close()


@scenario
async def test_abort_ends_the_loop_and_a_later_step_does_not_resume_it(tmp_path: Path) -> None:
    prompt = prompt_file(tmp_path)
    gate = Gate()
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([Reply([Part.Text("r0")], gate=gate), say("r1"), say("r2")]),
        extensions=[ralph.Ralph()],
    )
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    await h.abort()
    await h.settle()
    assert not h.session.cursor(h.cursor).auto
    h.provider.gates[0].release()
    await h.set_auto(True)  # sends the unanswered prompt once
    await h.settle()
    assert h.notices() == ["ralph: stopped after 1 iterations: Aborted"]
    assert len(h.provider.requests) == 2, "one more sample, and then it rests: the loop is gone"
    assert not h.session.cursor(h.cursor).busy
    await h.close()


@scenario
async def test_a_reload_does_not_loop_and_ralph_starts_it_again(tmp_path: Path) -> None:
    prompt = prompt_file(tmp_path)
    gate = Gate()
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([say("r0"), Reply([Part.Text("r1")], gate=gate), say("r2")]),
        extensions=[ralph.Ralph()],
    )
    await h.run("ralph", {"prompt": str(prompt)})
    await until(lambda: gate.held == 1)
    await h.crash()
    await h.settle()
    assert not h.session.cursor(h.cursor).auto, "auto-step is off after a load"
    assert len(h.provider.requests) == 2
    (prompt.parent / "DONE").write_text("")
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    assert len(h.provider.requests) == 3, "started again: one iteration, then DONE"
    assert h.notices()[-1] == "ralph: stopped after 1 iterations: DONE exists"
    await h.close()


@scenario
async def test_a_missing_prompt_file_fails_the_command_before_anything_is_written(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[ralph.Ralph()])
    with pytest.raises(Exception, match="FileNotFoundError"):
        await h.run("ralph", {"prompt": str(tmp_path / "nope.md")})
    assert len(h.nodes()) == 2
    await h.close()


@scenario
async def test_a_prompt_that_vanishes_mid_loop_fails_the_hook_and_ends_the_loop(
    tmp_path: Path,
) -> None:
    prompt = prompt_file(tmp_path)
    remove = FakeTool("remove", lambda args: (prompt.unlink(), "gone")[1])
    call = Reply([Part.Call("c", "remove", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path / "s",
        provider=FakeProvider([call, say("a"), say("b")]),
        tools=[remove],
        extensions=[ralph.Ralph()],
    )
    await h.run("ralph", {"prompt": str(prompt)})
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert (view.hook_error.extension, view.hook_error.point) == ("ralph", "post_step")
    assert "FileNotFoundError" in view.hook_error.error
    assert isinstance(view.ending, Ending.Done), "the ending was decided before post_step ran"
    assert not view.auto, "a failing post_step turns auto-step off, as a returned string does"
    assert len(h.provider.requests) == 2
    assert isinstance(h.bodies()[-1], Body.Assistant), "the cursor never left the finished reply"
    prompt.write_text("again")
    await h.send("hello")  # an ordinary conversation: the loop is gone
    await h.settle()
    assert len(h.provider.requests) == 3
    await h.close()


@scenario
async def test_the_command_line_is_a_path_and_an_optional_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[ralph.Ralph()])
    commands = await h.client.commands()
    assert parse("/ralph PROMPT.md", commands, {}).args == {"prompt": "PROMPT.md"}
    assert parse("/ralph PROMPT.md 1", commands, {}).args == {"prompt": "PROMPT.md", "root": 1}
    assert await h.client.complete(h.cursor, "node", "") == ["1", "0"], (
        "newest first, then the root"
    )
    await h.close()
