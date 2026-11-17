"""A sample a `pre_sample` hook answers may be a tool batch: its calls run like the model's.

The reply is the hook's (its author is the extension's), the tools phase runs the real tool
through `pre_tool` (so permissions apply) and `pre_result`, `Results` are written by `Tools`, and
with auto-step on the next sample sees the call and its result."""

from pathlib import Path

from fakes import FAKE_MODEL, FakeProvider, FakeTool, Recorder
from harness import Harness, say, scenario

from tau import (
    Author,
    Body,
    Cursor,
    Draft,
    Outcome,
    Part,
    Permissions,
    Stop,
    Subject,
    UserConfig,
    model_config,
)

ARGS = '{"path": "a.txt"}'


class CallsRead:
    """Answers the first sample with a batch of one call."""

    name = "caller"

    def __init__(self, tool: str = "read") -> None:
        self.tool = tool
        self.first = True

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        if self.first:
            self.first = False
            body = Body.Assistant([Part.Call("c1", self.tool, ARGS)], Stop.Tools)
            subject.draft = Draft(body, [], Author.Extension(self.name))


@scenario
async def test_an_answered_sample_that_calls_a_tool_runs_the_real_tool(tmp_path: Path) -> None:
    read, write = FakeTool("read", "contents"), FakeTool("write", "w")
    watch = Recorder("watch")
    provider = FakeProvider([say("done")])
    h = await Harness.create(
        tmp_path, provider=provider, tools=[read, write], extensions=[CallsRead(), watch]
    )
    await h.send("go")
    await h.settle()

    written = [n for n in h.nodes() if isinstance(n.body, Body.Assistant | Body.Results)]
    assert [(type(n.body), n.by) for n in written] == [
        (Body.Assistant, Author.Extension("caller")),
        (Body.Results, Author.Tools()),
        (Body.Assistant, Author.Provider()),
    ]
    answered, results, _ = written
    assert isinstance(results.body, Body.Results)
    assert results.body.outcomes == [("c1", Outcome.Ok("contents"), Author.Tools())]
    assert read.calls == [{"path": "a.txt"}], "the real tool ran"
    assert write.calls == []
    assert watch.points.count("pre_tool") == 1, "the call went through pre_tool"
    assert watch.points.count("pre_result") == 1

    (asked,) = provider.requests  # only the step after the tools sampled the provider
    assert asked.walk[-2:] == (answered.id, results.id), "the next sample sees call and result"
    assert asked.tool_names == ["read", "write"], "the provider is still offered every tool"
    await h.close()


@scenario
async def test_a_deny_rule_stops_an_answered_call_like_the_models(tmp_path: Path) -> None:
    bash = FakeTool("bash", "ran")
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    options = {"deny": ["bash"]}
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("done")]),
        tools=[bash],
        extensions=[Permissions(tmp_path), CallsRead("bash")],
        config=model_config(FAKE_MODEL, {"permissions": options}),
        user_config=UserConfig(
            tmp_path / "tau-home",
            cli={"models": models, "extensions": {"permissions": options}},
        ),
    )
    await h.send("go")
    await h.settle()
    assert bash.calls == []
    results = next(n for n in h.nodes() if isinstance(n.body, Body.Results))
    assert isinstance(results.body, Body.Results)
    ((_, outcome, by),) = results.body.outcomes
    assert isinstance(outcome, Outcome.Err)
    assert "deny rule 'bash'" in outcome.error
    assert by == Author.Extension("permissions"), "the refusal is the hook's, as for the model's"
    assert results.by == Author.Tools()
    await h.close()
