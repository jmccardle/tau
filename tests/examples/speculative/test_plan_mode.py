"""examples/speculative/plan_mode.py, driven the way a frontend would drive it.

Each test is one flow: what the person does (`/plan`, answers), what the model is offered and
told, what the tree records, and what the client is sent. The mode is the option `shown` reads,
and the line "plan mode" every client is sent in `CursorView.shown` while it holds.
"""

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import (
    Author,
    Body,
    Client,
    CursorView,
    Ending,
    Event,
    Outcome,
    Part,
    Stop,
)


def load(name: str) -> Any:
    path = Path(__file__).resolve().parents[3] / "examples" / "speculative" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"speculative_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_mode = load("plan_mode")
PLAN = "1. read x\n2. edit y"
APPROVED = Body.User(plan_mode.APPROVED)
BRIEF = Body.User(plan_mode.BRIEF)


def base_tools() -> list[FakeTool]:
    """The tools plan mode names: it fails the step if the driver lacks one."""
    return [FakeTool(n, "ran") for n in ("read", "write", "grep", "bash")]


async def started(
    tmp_path: Path, script: list[Reply], tools: list[FakeTool] | None = None
) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=base_tools() if tools is None else tools,
        extensions=[plan_mode],
    )


MODE = (("plan_mode", "plan mode"),)


async def shown(h: Harness, cursor: int | None = None) -> str:
    """The mode as a frontend learns it: the options form, read again whenever `at` changes."""
    form = await h.client.options(h.cursor if cursor is None else cursor, "plan_mode")
    return cast("dict[str, dict[str, dict[str, str]]]", form.schema)["properties"]["mode"][
        "default"
    ]


def asked(view: CursorView) -> list[str]:
    return [q.text for q in view.questions]


def users(h: Harness) -> list[tuple[Author, str]]:
    return [(n.by, n.body.text) for n in h.nodes() if isinstance(n.body, Body.User)]


@scenario
async def test_plan_with_a_task_briefs_the_model_and_asks_when_the_reply_ends(
    tmp_path: Path,
) -> None:
    h = await started(tmp_path, [say(PLAN), say("done")])
    assert await shown(h) == "act"
    result = await h.run("plan", {"task": "add retry"})
    assert result == "plan mode on: read-only tools; you will be asked to approve the plan"
    await h.settle()
    assert await shown(h) == "plan"
    # the task, then the brief: the model is told after it is asked (see the friction log)
    assert h.bodies()[-3:-1] == [Body.User("add retry"), BRIEF]
    view = await h.view()
    assert asked(view) == ["Approve this plan?"]
    assert view.questions[0].schema["enum"] == ["approve", "revise", "reject"]
    assert view.busy, "the step is held open by the question"
    assert view.shown == MODE
    assert len(h.provider.requests) == 1
    await h.close()


@scenario
async def test_approve_leaves_plan_mode_and_the_model_carries_the_plan_out(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN), say("done")])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    await h.respond("approve")
    await h.settle()
    assert await shown(h) == "act"
    assert (await h.view()).shown == (), "leaving plan mode clears the line"
    assert h.bodies()[-2:] == [APPROVED, Body.Assistant([Part.Text("done")], Stop.End)]
    # who said "carry out the plan"? the extension: the person only clicked
    by = {n.body: n.by for n in h.nodes() if isinstance(n.body, Body.User)}
    assert by[APPROVED] == Author.Extension("plan_mode")
    assert by[BRIEF] == Author.Extension("plan_mode")
    assert by[Body.User("add retry")] == Author.Extension("plan_mode"), "`/plan` sent it"
    assert len(h.provider.requests) == 2, "auto-step went on to a second sample by itself"
    assert (await h.view()).ending == Ending.Done()
    await h.close()


@scenario
async def test_revise_sends_the_feedback_as_the_next_input_and_stays_in_plan_mode(
    tmp_path: Path,
) -> None:
    h = await started(tmp_path, [say(PLAN), say("2. edit z instead"), say("done")])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    await h.respond("revise")
    await h.settle()
    assert asked(await h.view()) == ["What should change?"]
    await h.respond("use z, not y")
    await h.settle()
    assert asked(await h.view()) == ["Approve this plan?"], "the second plan is asked about too"
    assert await shown(h) == "plan"
    # the task and the feedback are sent by the extension, and the model was briefed once
    assert users(h) == [
        (Author.Extension("plan_mode"), "add retry"),
        (Author.Extension("plan_mode"), plan_mode.BRIEF),
        (Author.Extension("plan_mode"), "use z, not y"),
    ]
    assert len(h.provider.requests) == 2
    await h.respond("approve")
    await h.settle()
    assert h.bodies()[-2] == APPROVED
    await h.close()


@scenario
async def test_reject_leaves_plan_mode_and_acts_on_nothing(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN)])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    await h.respond("reject")
    await h.settle()
    assert await shown(h) == "act"
    assert h.notices() == ["plan rejected: plan mode off"]
    assert len(h.provider.requests) == 1
    assert APPROVED not in h.bodies()
    await h.close()


@scenario
async def test_declining_the_question_halts_and_keeps_plan_mode(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN)])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    await h.respond(None)
    await h.settle()
    view = await h.view()
    assert not view.auto
    assert await shown(h) == "plan", "nothing was decided, so nothing changed"
    await h.close()


@scenario
async def test_abort_declines_the_question_like_any_other(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN)])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    await h.abort()
    await h.settle()
    view = await h.view()
    assert view.questions == ()
    assert not view.busy
    assert not view.auto
    assert await shown(h) == "plan"
    await h.close()


@scenario
async def test_the_model_is_offered_only_read_only_tools_and_a_call_to_another_is_refused(
    tmp_path: Path,
) -> None:
    tools = base_tools()
    read, write, _, bash = tools
    calls = [
        Part.Call("a", "write", "{}"),
        Part.Call("b", "read", "{}"),
        Part.Call("c", "bash", '{"command": "git diff --stat"}'),
        Part.Call("d", "bash", '{"command": "ls && rm -rf /"}'),
        Part.Call("e", "bash", '{"command": "rm x"}'),
    ]
    h = await started(tmp_path, [Reply(calls, Stop.Tools), say(PLAN)], tools)
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    # `write` is not offered; a model may still call it, and the driver refuses it. `bash` is
    # offered, so the refusals of `d` and `e` are plan mode's own
    assert h.provider.requests[0].tool_names == ["read", "grep", "bash"]
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    assert [(i, type(o).__name__) for i, o, _ in results.outcomes] == [
        ("a", "Err"),
        ("b", "Ok"),
        ("c", "Ok"),
        ("d", "Err"),
        ("e", "Err"),
    ]
    refused = results.outcomes[0][1]
    assert isinstance(refused, Outcome.Err)
    assert refused.error == "tool not offered: write"
    own = results.outcomes[4]
    assert isinstance(own[1], Outcome.Err)
    assert "plan mode: bash is refused" in own[1].error
    assert own[2] == Author.Extension("plan_mode")
    assert write.calls == []
    assert read.calls == [{}]
    assert bash.calls == [{"command": "git diff --stat"}]
    await h.close()


@scenario
async def test_plan_mode_offers_the_read_only_tools_the_driver_has_and_no_more(
    tmp_path: Path,
) -> None:
    """Not `read`, `grep` and `bash` by name: those of them in `cursor.tools()`."""
    only_some = [FakeTool(n, "ran") for n in ("bash", "write", "read")]
    h = await started(tmp_path / "some", [say(PLAN)], only_some)
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    assert (await h.view()).hook_error is None
    assert h.provider.requests[0].tool_names == ["bash", "read"], "the driver's order, no write"
    await h.close()

    chat_like = [FakeTool("word_count", "ran")]  # examples/chat.py's driver
    h = await started(tmp_path / "none", [say(PLAN)], chat_like)
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    assert (await h.view()).hook_error is None, "the step does not fail for tools it lacks"
    assert h.provider.requests[0].tool_names == [], "none is offered, and it still asks to approve"
    assert [q.text for q in (await h.view()).questions] == ["Approve this plan?"]
    await h.close()


@scenario
async def test_plan_mode_entered_by_config_still_briefs_the_model(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN)])
    await h.configure("plan_mode", {"mode": "plan"})
    await h.settle()
    assert h.provider.requests == [], "a config step samples nothing"
    await h.send("add retry")
    await h.settle()
    assert h.bodies()[-3:-1] == [Body.User("add retry"), BRIEF]
    assert asked(await h.view()) == ["Approve this plan?"]
    await h.close()


@scenario
async def test_plan_mode_belongs_to_the_branch_not_the_cursor(tmp_path: Path) -> None:
    h = await started(tmp_path, [say("hi")])
    system = 1
    await h.run("plan")
    assert await shown(h) == "plan"
    other = await h.client.open(system)
    await h.settle()
    assert await shown(h, other) == "act"
    await h.go_to(system)
    await h.settle()
    assert await shown(h) == "act", "going back before the Config node leaves plan mode"
    await h.close()


@scenario
async def test_every_client_sees_the_question_and_the_mode(tmp_path: Path) -> None:
    h = await started(tmp_path, [say(PLAN)])
    await h.run("plan", {"task": "add retry"})
    await h.settle()
    late = await h.connect("alice@laptop:tui")
    snapshot = [e async for e in _snapshot(late)]
    views = [e for e in snapshot if isinstance(e, CursorView)]
    assert asked(views[0]) == ["Approve this plan?"]
    assert views[0].shown == MODE
    await late.answer(views[0].questions[0].id, "reject")
    await h.settle()
    assert await shown(h) == "act"
    await h.close()


async def _snapshot(client: Client) -> AsyncIterator[Event]:
    async for event in client:
        yield event
        if type(event).__name__ == "Labels":
            return


# A line client (examples/chat.py) over the same extension: what it shows today.


def load_chat() -> Any:
    examples = Path(__file__).resolve().parents[3] / "examples"
    sys.path.insert(0, str(examples))
    try:
        return load_from(examples / "chat.py", "speculative_chat")
    finally:
        sys.path.remove(str(examples))


def load_from(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


@scenario
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: the example imports readline, which Python on Windows does not have",
)
async def test_a_line_client_lists_plan_asks_inline_and_says_it_is_in_plan_mode(
    tmp_path: Path,
) -> None:
    chat = load_chat()
    h = await started(tmp_path, [say(PLAN), say("done")])
    client = await h.connect("chat:test", watching=[h.cursor])
    screen: list[str] = []

    async def typed(prompt: str) -> str:
        raise AssertionError(f"the client asked for {prompt!r}: /plan takes no required argument")

    window = chat.Chat(client, h.cursor, screen.append, typed)
    await window.refresh()
    listener = asyncio.ensure_future(window.listen())
    await window.line("/")
    assert (
        "/plan [task]  Enter plan mode on this branch, optionally starting with a task to plan.\n"
        in "".join(screen)
    )
    await window.line("/plan add retry")  # free text: no quotes
    await until(lambda: "? Approve this plan?" in "".join(screen))
    assert "[approve/revise/reject]" in "".join(screen)
    assert "[plan mode on: read-only tools; you will be asked to approve the plan]" in "".join(
        screen
    )
    assert (Author.Extension("plan_mode"), "add retry") in users(h)
    await window.line("approve")
    await h.settle()
    assert h.bodies()[-2] == APPROVED
    # the mode is shown when it begins and when it ends
    assert "[plan_mode: plan mode]" in "".join(screen)
    assert "".join(screen).count("[plan_mode: plan mode]") == 1
    await until(lambda: "[plan_mode: cleared]" in "".join(screen))
    assert "[plan_mode] add retry" in "".join(screen), "an extension's message is shown by name"
    listener.cancel()
    await client.close()
    await h.close()
