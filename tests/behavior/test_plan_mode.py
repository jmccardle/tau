"""`plan_mode` (the built-in, on by default and inert until `/plan`), driven the way a frontend
would drive it.

Each test is one flow: what the person does (`/plan`, answers), what the model is offered and
told, what the tree records, and what the client is sent. The mode is the option `shown` reads,
and the line "plan mode" every client is sent in `CursorView.shown` while it holds.
"""

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario
from shipped import shipped
from web import Page, built, serving

import tau
from tau import (
    Agents,
    Author,
    BashTool,
    Body,
    Client,
    CursorView,
    Ending,
    Event,
    Outcome,
    Part,
    Permissions,
    Stop,
    load_config,
    load_extensions,
    model_config,
)

PLAN = "1. read x\n2. edit y"
APPROVED_TEXT = "The plan is approved. Carry it out."
BRIEF_TEXT = (
    "PLAN MODE. Research with the read-only tools, then reply with a step-by-step plan and "
    "make no changes: write, edit and shell commands are refused until the person approves."
)
APPROVED = Body.User(APPROVED_TEXT)
BRIEF = Body.User(BRIEF_TEXT)


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
        extensions=[shipped("plan_mode")],
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
        (Author.Extension("plan_mode"), BRIEF_TEXT),
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
    examples = Path(__file__).resolve().parents[2] / "examples"
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


@scenario
async def test_it_is_inert_until_plan_the_model_is_offered_every_tool_and_nothing_is_shown(
    tmp_path: Path,
) -> None:
    h = await started(tmp_path, [say("hello")])
    await h.send("hi")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["read", "write", "grep", "bash"]
    assert dict((await h.view()).shown) == {}, "no badge, not even a cleared one"
    assert await shown(h) == "act"
    assert [b for b in h.bodies() if isinstance(b, Body.User)] == [Body.User("hi")], "no brief"
    await h.close()


@scenario
async def test_what_is_read_only_is_not_an_option_a_client_can_change(tmp_path: Path) -> None:
    """Client-facing options are for what a client may configure; the lists are fixed."""
    h = await started(tmp_path, [say("hello")])
    form = await h.client.options(h.cursor, "plan_mode")
    assert sorted(cast("dict[str, object]", form.schema["properties"])) == ["mode"]
    with pytest.raises(ValueError, match="allow_bash"):
        await h.client.configure(h.cursor, "plan_mode", {"allow_bash": ["rm"]})
    await h.close()


@scenario
async def test_find_is_read_only_and_offered_in_plan_mode(tmp_path: Path) -> None:
    tools = [FakeTool(n, "ran") for n in ("read", "find", "edit")]
    h = await started(tmp_path, [say(PLAN)], tools)
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["read", "find"]
    await h.close()


@scenario
async def test_plan_mode_does_not_let_the_model_hand_the_work_to_a_sub_agent(
    tmp_path: Path,
) -> None:
    """A sub-agent is offered every tool, so `agent` is not offered, and refused, in plan mode."""
    call = Part.Call("a", "agent", '{"task": "edit everything"}')
    script = [Reply([call], Stop.Tools), say(PLAN)]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=base_tools(),
        extensions=[shipped("plan_mode"), Agents()],
    )
    await h.run("plan")
    await h.send("go")
    await h.settle()
    assert "agent" not in h.provider.requests[0].tool_names
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    ((_, outcome, _),) = results.outcomes
    assert outcome == Outcome.Err("tool not offered: agent")
    assert h.session.cursors() == [h.cursor], "no sub-agent was opened"
    await h.close()


REFUSED = "plan mode: bash is refused. Put the change in your plan instead."


@scenario
async def test_plan_mode_refuses_before_permissions_asks(tmp_path: Path) -> None:
    """Plan mode's `pre_tool` runs before permissions' (both are guards; plan mode's is first),
    so a person is never asked whether a call may run that plan mode would refuse anyway."""
    tools = base_tools()
    call = Part.Call("a", "bash", '{"command": "echo hi > y.txt"}')
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([call], Stop.Tools), say(PLAN)]),
        tools=tools,
        extensions=[Permissions(tmp_path), shipped("plan_mode")],  # permissions loads first
        config=model_config(FAKE_MODEL, {"permissions": {"ask": ["bash"]}}),
    )
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    assert results.outcomes == [("a", Outcome.Err(REFUSED), Author.Extension("plan_mode"))]
    assert asked(await h.view()) == ["Approve this plan?"], "the only question is plan mode's"
    assert tools[3].calls == []
    await h.close()


@scenario
async def test_a_bash_call_whose_arguments_are_not_an_object_with_a_command_is_refused(
    tmp_path: Path,
) -> None:
    """Plan mode fails closed: what it cannot read as a command is refused in its own words."""
    tools = base_tools()
    calls = [
        Part.Call("a", "bash", "[1]"),
        Part.Call("b", "bash", '"ls"'),
        Part.Call("c", "bash", "{bad"),
        Part.Call("d", "bash", '{"command": 5}'),
    ]
    h = await started(tmp_path, [Reply(calls, Stop.Tools), say(PLAN)], tools)
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    assert [(i, o) for i, o, _ in results.outcomes] == [(i, Outcome.Err(REFUSED)) for i in "abcd"]
    assert tools[3].calls == []
    await h.close()


def copy_plan_mode(extensions: Path) -> Path:
    """Copy the built-in's package into `extensions` as `plan_mode/`, as the guide says to."""
    built_in = Path(tau.__file__).parent / "_plan_mode"
    copy = extensions / "plan_mode"
    shutil.copytree(built_in, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


@scenario
async def test_a_copy_of_the_package_with_its_settings_edited_shadows_the_built_in(
    tmp_path: Path,
) -> None:
    """The settings file: an administrator copies the package and edits only `settings.py`;
    the copy shadows the built-in, and a session offers the edited list."""
    home = tmp_path / "tau-home"
    settings = copy_plan_mode(home / "extensions") / "settings.py"
    text = settings.read_text(encoding="utf-8")
    listed = 'READ_ONLY_TOOLS = ("read", "grep", "find", "fetch")'
    edited = text.replace(listed, 'READ_ONLY_TOOLS = ("read",)')
    assert edited != text
    settings.write_text(edited, encoding="utf-8")
    loaded = load_config(home, {"HOME": str(home), "TAU_HOME": str(home)}, ask=None)
    (copy,) = [e for e in load_extensions(loaded) if e.name == "plan_mode"]

    h = await Harness.create(
        tmp_path, provider=FakeProvider([say(PLAN)]), tools=base_tools(), extensions=[copy]
    )
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["read", "bash"], "grep is no longer offered"
    assert asked(await h.view()) == ["Approve this plan?"]
    await h.close()


def git_repo(path: Path) -> Path:
    """A repository with one commit and a change, so `git diff` and `git log` have output."""
    path.mkdir()
    (path / "a").write_text("a\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "a"], ["commit", "-qm", "a"]):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
        subprocess.run(git, cwd=path, check=True)
    (path / "a").write_text("a\nb\n", encoding="utf-8")
    return path


@scenario
@pytest.mark.skipif(sys.platform == "win32", reason="Windows: the bash tool is not ported")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_an_argument_that_makes_a_read_only_command_write_is_refused(tmp_path: Path) -> None:
    """`settings.REFUSED_ARGUMENTS`: `git diff --output=f` would write `f`, and `--ext-diff`
    would run a configured program. The same commands without them still run."""
    repo = git_repo(tmp_path / "repo")
    refused = [
        "git diff --output=zz",
        "git diff --output zz",
        "git log -p --output=zz",
        "git diff '--output'=zz",
        "git log -p --ext-diff",
    ]
    ran = ["git diff --stat", "git log -n 1 --oneline"]
    calls = [
        Part.Call(str(i), "bash", json.dumps({"command": c})) for i, c in enumerate(refused + ran)
    ]
    h = await started(
        tmp_path,
        [Reply(calls, Stop.Tools), say(PLAN)],
        [BashTool(repo)],  # pyright: ignore[reportArgumentType]
    )
    await h.run("plan")
    await h.send("look around")
    await h.settle()
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    outcomes = [o for _, o, _ in results.outcomes]
    assert outcomes[: len(refused)] == [Outcome.Err(REFUSED)] * len(refused)
    assert all(isinstance(o, Outcome.Ok) for o in outcomes[len(refused) :]), outcomes
    assert not (repo / "zz").exists(), "nothing was written"
    await h.close()


@scenario
async def test_fetch_is_offered_in_plan_mode_and_runs_there_and_search_is_not(
    tmp_path: Path,
) -> None:
    """Reading a page changes nothing, and still goes through permissions; `search` is not on
    the list."""
    with serving({"/doc": Page("<main><p>the docs</p></main>")}) as site:
        call = Part.Call("f", "fetch", json.dumps({"url": f"{site.url}/doc"}))
        h = await Harness.create(
            tmp_path,
            provider=FakeProvider([Reply([call], Stop.Tools), say(PLAN)]),
            tools=[FakeTool("read", "ran"), FakeTool("search", "ran")],
            extensions=[
                shipped("plan_mode"),
                built(tmp_path, "fetch", {"allow_private": True}),
            ],
        )
        await h.run("plan")
        await h.send("look it up")
        await h.settle()
    assert h.provider.requests[0].tool_names == ["read", "fetch"]
    results = next(b for b in h.bodies() if isinstance(b, Body.Results))
    outcome = results.outcomes[0][1]
    assert isinstance(outcome, Outcome.Ok), outcome
    assert outcome.value.endswith("the docs")
    await h.close()
