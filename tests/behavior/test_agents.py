"""`Agents` (D24 section 8): the `agent` tool opens a sub-agent on a fresh cursor, returns its
final reply, and closes it."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Agents,
    Body,
    Ending,
    ModelCatalog,
    NoSuchLabel,
    Outcome,
    Part,
    Permissions,
    StepLimit,
    Stop,
    Usage,
    model_config,
    read_models,
)


def agent(id: str, task: str, to: str | None = None) -> Part.Call:
    args = {"task": task} if to is None else {"task": task, "to": to}
    return Part.Call(id, "agent", json.dumps(args))


def delegating(*calls: Part.Call) -> Reply:
    return Reply(list(calls), Stop.Tools)


def outcomes(h: Harness) -> list[Outcome]:
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    return [o for _, o, _ in body.outcomes]


def texts(h: Harness, request: int) -> list[str]:
    """What the user and system nodes of a request said."""
    nodes = {n.id: n.body for n in h.nodes()}
    found = [nodes[i] for i in h.provider.requests[request].walk]
    return [b.text for b in found if isinstance(b, (Body.System, Body.User))]


async def start(  # noqa: PLR0913 - what a scenario may vary
    tmp_path: Path,
    script: Sequence[Reply | Fail],
    *,
    extensions: list[object] | None = None,
    tools: list[FakeTool] | None = None,
    config: object = None,
    catalog: ModelCatalog | None = None,
    gates: list[Gate] | None = None,
) -> Harness:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=extensions or [Agents()],  # pyright: ignore[reportArgumentType]
        tools=tools or [],
        config=config or model_config(FAKE_MODEL),  # pyright: ignore[reportArgumentType]
        catalog=catalog,
        gates=gates or [],
    )
    await h.send("go")
    await h.settle()
    return h


@scenario
async def test_the_model_is_offered_the_tool_and_the_agent_answers_in_a_fresh_context(
    tmp_path: Path,
) -> None:
    h = await start(
        tmp_path, [delegating(agent("a", "find the bug")), say("found it"), say("thanks")]
    )
    assert h.provider.requests[0].tool_names == ["agent"]
    assert texts(h, 1) == ["sys", "find the bug"], (
        "the agent sees neither `go` nor the parent's work"
    )
    assert outcomes(h) == [Outcome.Ok("agent:1 answered:\nfound it")]
    assert (await h.view()).ending == Ending.Done()
    await h.close()


@scenario
async def test_the_agent_opens_at_the_prefix_so_the_cache_prefix_is_shared(tmp_path: Path) -> None:
    h = await start(tmp_path, [delegating(agent("a", "t")), say("r"), say("done")])
    parent, child = h.provider.requests[0].walk, h.provider.requests[1].walk
    assert child[:2] == parent[:2], "the root Config and the system prompt"
    assert len(child) == 3
    await h.close()


@scenario
async def test_a_finished_agent_is_closed_and_stays_in_the_tree_with_its_label(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path, [delegating(agent("a", "t")), say("r"), say("done")])
    child = h.session.label("agent:1")
    assert child not in h.session.cursors(), "the cursor is closed"
    assert child in [c for c, _ in h.session.closed()]
    last = dict(h.session.closed())[child]
    assert {n.id: n.body for n in h.nodes()}[last] == Body.Assistant([Part.Text("r")], Stop.End)
    assert h.session.cursors() == [h.cursor]
    await h.close()


@scenario
async def test_two_calls_in_one_reply_run_at_once_with_their_own_labels(tmp_path: Path) -> None:
    gate = Gate()
    script = [
        delegating(agent("a", "x"), agent("b", "y")),
        Reply([Part.Text("one")], gate=gate),
        Reply([Part.Text("two")], gate=gate),
        say("both done"),
    ]
    h = await start(tmp_path, script, gates=[gate])
    assert gate.held == 2, "both agents are sampling together"
    assert sorted(c for c in h.session.cursors() if c != h.cursor) == [1, 2]
    assert {h.session.label("agent:1"), h.session.label("agent:2")} == {1, 2}
    gate.release()
    await h.settle()
    assert outcomes(h) == [
        Outcome.Ok("agent:1 answered:\none"),
        Outcome.Ok("agent:2 answered:\ntwo"),
    ]
    await h.close()


@scenario
async def test_to_continues_an_earlier_agent_which_remembers_its_work(tmp_path: Path) -> None:
    script = [
        delegating(agent("a", "read the file")),
        say("it says hi"),
        say("first is done"),
        delegating(agent("b", "and the next one?", to="agent:1")),
        say("it says bye"),
        say("done"),
    ]
    h = await start(tmp_path, script)
    await h.send("more")
    await h.settle()
    assert texts(h, 4) == ["sys", "read the file", "and the next one?"]
    nodes = {n.id: n.body for n in h.nodes()}
    walk = [nodes[i] for i in h.provider.requests[4].walk]
    assert Body.Assistant([Part.Text("it says hi")], Stop.End) in walk
    assert outcomes(h) == [Outcome.Ok("agent:1 answered:\nit says bye")]
    assert h.session.label("agent:1") not in h.session.cursors(), "closed again"
    with pytest.raises(NoSuchLabel):
        h.session.label("agent:2")  # the follow-up reused the agent, it made no new one
    await h.close()


@scenario
async def test_to_continues_an_agent_a_crash_left_open_at_rest(tmp_path: Path) -> None:
    script = [
        delegating(agent("a", "read the file")),
        say("it says hi"),
        say("first is done"),
        delegating(agent("b", "and the next one?", to="agent:1")),
        say("it says bye"),
        say("done"),
    ]
    h = await start(tmp_path, script)
    closed = h.session.label("agent:1")
    left = await h.client.reopen(closed)  # as a kill -9 left it: open, at rest, labelled
    await h.settle()
    await h.send("more")
    await h.settle()
    assert outcomes(h) == [Outcome.Ok("agent:1 answered:\nit says bye")]
    assert texts(h, 4) == ["sys", "read the file", "and the next one?"]
    assert left not in h.session.cursors(), "closed again"
    await h.close()


@scenario
async def test_to_an_agent_that_is_busy_is_an_error_the_model_reads(tmp_path: Path) -> None:
    gate = Gate()
    script = [
        delegating(agent("a", "x"), agent("b", "y", to="agent:1")),
        Reply([Part.Text("one")], gate=gate),
        say("both done"),
    ]
    h = await start(tmp_path, script, gates=[gate])
    gate.release()
    await h.settle()
    assert outcomes(h) == [
        Outcome.Ok("agent:1 answered:\none"),
        Outcome.Err("agent:1 is still working: wait for its answer"),
    ]
    await h.close()


@scenario
async def test_to_an_agent_that_does_not_exist_is_an_error_the_model_reads(tmp_path: Path) -> None:
    h = await start(
        tmp_path,
        [
            delegating(agent("a", "t", to="agent:9"), agent("b", "t", to="compaction:1")),
            say("ok"),
        ],
    )
    assert outcomes(h) == [
        Outcome.Err("there is no agent 'agent:9'"),
        Outcome.Err("there is no agent 'compaction:1'"),
    ]
    assert h.session.cursors() == [h.cursor], "nothing was opened"
    await h.close()


@scenario
async def test_to_nothing_is_an_error_not_a_new_agent(tmp_path: Path) -> None:
    h = await start(tmp_path, [delegating(agent("a", "t", to="")), say("ok")])
    assert outcomes(h) == [Outcome.Err("there is no agent ''")]
    assert h.session.cursors() == [h.cursor], "nothing was opened"
    await h.close()


@scenario
async def test_an_empty_final_reply_is_said_so_not_blamed_on_a_step_limit(tmp_path: Path) -> None:
    h = await start(tmp_path, [delegating(agent("a", "t")), Reply([], Stop.End), say("ok")])
    assert outcomes(h) == [Outcome.Err("agent:1 gave an empty reply")]
    await h.close()


@scenario
async def test_an_agent_that_fails_is_a_tool_error_naming_why_and_is_closed(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path, [delegating(agent("a", "t")), Fail("overloaded"), say("it failed")])
    ((error,),) = [outcomes(h)]
    assert isinstance(error, Outcome.Err)
    assert error.error.startswith("agent:1 failed: ")
    assert "overloaded" in error.error
    assert h.session.cursors() == [h.cursor]
    await h.close()


@scenario
async def test_an_agent_the_step_limit_stopped_is_not_a_final_reply(tmp_path: Path) -> None:
    looping = Reply([Part.Call("x", "ta", "{}")], Stop.Tools)
    script = [delegating(agent("a", "t")), looping, looping, say("gave up")]
    h = await start(
        tmp_path,
        script,
        extensions=[Agents(), StepLimit()],
        tools=[FakeTool("ta", "r")],
        config=model_config(FAKE_MODEL, {"step_limit": {"limit": 2}}),
    )
    (error,) = outcomes(h)
    assert isinstance(error, Outcome.Err)
    assert error.error == "agent:1 stopped without a final reply (a step limit?)"
    await h.close()


# Tools and models


@scenario
async def test_an_agent_is_offered_every_tool_but_agent(tmp_path: Path) -> None:
    tools = [FakeTool("ta", "r"), FakeTool("tb", "r")]
    h = await start(tmp_path, [delegating(agent("a", "t")), say("r"), say("done")], tools=tools)
    assert h.provider.requests[0].tool_names == ["ta", "tb", "agent"]
    assert h.provider.requests[1].tool_names == ["ta", "tb"]
    await h.close()


@scenario
async def test_the_tools_option_picks_what_an_agent_is_offered(tmp_path: Path) -> None:
    tools = [FakeTool("ta", "r"), FakeTool("tb", "r")]
    h = await start(
        tmp_path,
        [delegating(agent("a", "t")), say("r"), say("done")],
        tools=tools,
        config=model_config(FAKE_MODEL, {"agents": {"tools": ["tb"]}}),
    )
    assert h.provider.requests[1].tool_names == ["tb"]
    assert h.provider.requests[0].tool_names == ["ta", "tb", "agent"], "the parent is unchanged"
    await h.close()


@scenario
async def test_an_agent_cannot_be_offered_agent(tmp_path: Path) -> None:
    h = await start(
        tmp_path,
        [delegating(agent("a", "t")), say("done")],
        config=model_config(FAKE_MODEL, {"agents": {"tools": ["agent"]}}),
    )
    (error,) = outcomes(h)
    assert isinstance(error, Outcome.Err)
    assert "agents do not nest" in error.error
    await h.close()


def two_models() -> ModelCatalog:
    return read_models(
        cli={
            "models": {
                name: {"model": name, "provider": FAKE_PROVIDER} for name in (FAKE_MODEL, "cheap")
            }
        }
    )


@scenario
async def test_the_model_option_picks_the_agents_model_and_without_it_the_branchs_own(
    tmp_path: Path,
) -> None:
    script = [delegating(agent("a", "t")), say("r"), say("done")]
    h = await start(
        tmp_path,
        script,
        catalog=two_models(),
        config=model_config(FAKE_MODEL, {"agents": {"model": "cheap"}}),
    )
    assert [r.model.model for r in h.provider.requests] == [FAKE_MODEL, "cheap", FAKE_MODEL]
    await h.close()

    h = await start(
        tmp_path / "branch",
        [delegating(agent("a", "t")), say("r"), say("done")],
        catalog=two_models(),
        config=model_config("cheap"),
    )
    assert [r.model.model for r in h.provider.requests] == ["cheap", "cheap", "cheap"]
    await h.close()


# Abort, usage, permissions


@scenario
async def test_aborting_the_parent_aborts_and_closes_the_agent(tmp_path: Path) -> None:
    gate = Gate()
    h = await start(
        tmp_path,
        [delegating(agent("a", "t")), Reply([Part.Text("r")], gate=gate)],
        gates=[gate],
    )
    child = h.session.label("agent:1")
    assert child in h.session.cursors()
    await h.abort()
    await h.settle()
    assert child not in h.session.cursors(), "closed with the abort"
    assert (await h.view()).ending == Ending.Aborted()
    assert outcomes(h) == [Outcome.Interrupted()]
    assert h.session.cursors() == [h.cursor]
    await h.close()


@scenario
async def test_usage_counts_the_agents_replies(tmp_path: Path) -> None:
    def counted(text: str, input: int) -> Reply:
        meta = json.dumps({"usage": {"input": input, "output": 1}})
        return Reply([Part.Text(text)], meta=meta)

    parent = Reply(
        [agent("a", "t")], Stop.Tools, meta=json.dumps({"usage": {"input": 100, "output": 1}})
    )
    h = await start(
        tmp_path,
        [parent, counted("r", 1000), counted("done", 200)],
        extensions=[Agents(), Usage()],
    )
    report = await h.run("usage")
    assert report is not None
    assert report.splitlines()[1].split() == ["fake", "3", "1.3k", "-", "-", "3", "-"], (
        "100 + 1000 + 200 input tokens over three samples: the agent's reply is counted"
    )
    await h.close()


@scenario
async def test_a_permission_question_is_asked_on_the_agents_own_cursor(tmp_path: Path) -> None:
    run = Reply([Part.Call("x", "bash", json.dumps({"command": "ls"}))], Stop.Tools)
    bash = FakeTool("bash", "listing")
    h = await start(
        tmp_path,
        [delegating(agent("a", "t")), run, say("saw it"), say("done")],
        extensions=[Agents(), Permissions(tmp_path)],
        tools=[bash],
        config=model_config(FAKE_MODEL, {"permissions": {"ask": ["bash"]}}),
    )
    child = h.session.label("agent:1")
    assert (await h.view()).questions == (), "not the parent's"
    (question,) = (await h.view(child)).questions
    assert question.text.startswith("bash: ls")
    assert bash.calls == []
    await h.respond("yes", child)
    await h.settle()
    assert bash.calls == [{"command": "ls"}]
    assert outcomes(h) == [Outcome.Ok("agent:1 answered:\nsaw it")]
    await h.close()


# A tool that was not offered is not run


@scenario
async def test_an_agent_that_calls_a_tool_it_was_not_offered_gets_an_error(tmp_path: Path) -> None:
    bash = FakeTool("bash", "ran bash")
    script = [
        delegating(agent("a", "t")),
        Reply([Part.Call("b", "bash", json.dumps({"command": "rm -rf /"}))], Stop.Tools),
        say("could not"),
        say("done"),
    ]
    h = await start(
        tmp_path,
        script,
        tools=[bash, FakeTool("read", "r")],
        config=model_config(FAKE_MODEL, {"agents": {"tools": ["read"]}}),
    )
    assert h.provider.requests[1].tool_names == ["read"]
    assert bash.calls == [], "the tool was registered but not offered to the agent"
    child = h.session.label("agent:1")
    nodes = {n.id: n.body for n in h.nodes()}
    results = [
        b for n, b in nodes.items() if isinstance(b, Body.Results) and h.session.tree.node(n).parent
    ]
    assert any(
        outcome == Outcome.Err("tool not offered: bash")
        for r in results
        for _, outcome, _ in r.outcomes
    ), child
    await h.close()


@scenario
async def test_agents_do_not_nest_because_an_agent_is_never_offered_agent(tmp_path: Path) -> None:
    script = [
        delegating(agent("a", "t")),
        delegating(agent("b", "deeper")),  # the agent calls `agent`, which it was not offered
        say("could not"),
        say("done"),
    ]
    h = await start(tmp_path, script)
    for label in ("agent:2",):
        with pytest.raises(NoSuchLabel):
            h.session.label(label)
    assert [r.tool_names for r in h.provider.requests][1] == []
    await h.close()
