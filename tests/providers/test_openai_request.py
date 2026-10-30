"""The request the OpenAI provider builds from a walk. Walks come from Harness sessions."""

from collections.abc import Sequence
from pathlib import Path

from fakes import FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario
from wire import OPENAI, OPENAI_BARE, OPENAI_MODEL, OPENAI_URL, posted, sent, walk_of

from tau_next import (
    Author,
    Body,
    Draft,
    ModelConfig,
    Node,
    Outcome,
    Part,
    Stop,
    Subject,
    Tool,
)


async def body_of(
    walk: Sequence[Node], model: ModelConfig = OPENAI_BARE, tools: Sequence[Tool] = ()
) -> dict[str, object]:
    """The body the provider posted for one sample of `walk`."""
    return (await posted(OPENAI, walk, model, tools, environ={})).bodies[0]


def answer_with_orphan_results(subject: object) -> None:
    assert isinstance(subject, Subject)
    assert subject.draft is not None
    outcomes = [("zz", Outcome.Ok("v"), Author.Tools())]
    subject.draft = Draft(Body.Results(outcomes), [], Author.Provider())


@scenario
async def test_system_and_user_become_messages_and_config_nodes_are_skipped(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        system="be brief",
        provider=FakeProvider([Reply([Part.Text("hi")])]),
    )
    await h.input("hello")
    await h.settle()
    await h.step()
    await h.settle()
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)[:-1]
    server = await posted(OPENAI, walk, OPENAI_BARE, environ={})
    body = server.bodies[0]
    assert str(server.requests[0].url) == f"{OPENAI_URL}/chat/completions"
    assert "authorization" not in server.requests[0].headers
    assert body == {
        "model": OPENAI_MODEL,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ],
    }
    await h.close()


@scenario
async def test_an_answered_batch_becomes_tool_calls_and_one_tool_message_per_outcome(
    tmp_path: Path,
) -> None:
    script = [
        Reply(
            [
                Part.Thinking("hm"),
                Part.Text("looking"),
                Part.Call("a", "ta", '{"x":  1}'),
                Part.Call("b", "tb", "{}"),
            ],
            Stop.Tools,
        ),
        Reply([Part.Text("done")]),
    ]
    tools = [FakeTool("ta", "A"), FakeTool("tb", RuntimeError("boom"))]
    h = await Harness.create(tmp_path, provider=FakeProvider(script), tools=tools)
    await h.send("go")
    await h.settle()
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)
    assert isinstance(walk[-1].body, Body.Assistant), "the second reply is last"
    body = await body_of(walk[:-1], tools=tools)
    assert sent(body)[2:] == [
        {
            "role": "assistant",
            "content": "looking",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "ta", "arguments": '{"x":  1}'},
                },
                {"id": "b", "type": "function", "function": {"name": "tb", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "A"},
        {"role": "tool", "tool_call_id": "b", "content": "Error: boom"},
    ]
    await h.close()


@scenario
async def test_an_unanswered_batch_is_rendered_as_text_never_as_tool_calls(
    tmp_path: Path,
) -> None:
    """model.md 10.6: after a crash mid-batch, input goes under the batch."""
    gate = Gate()
    script = [
        Reply([Part.Text("checking"), Part.Call("a", "ta", '{"p": 1}')], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h = await Harness.create(
        tmp_path, provider=FakeProvider(script), tools=[FakeTool("ta", "A", gate)]
    )
    await h.send("look")
    await h.settle()
    await h.crash()
    await h.settle()
    await h.input("the power went out")
    await h.settle()
    await h.step()
    await h.settle()
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)[:-1]
    body = await body_of(walk)
    assert sent(body)[2:] == [
        {"role": "assistant", "content": 'checking\n[tool call a: ta {"p": 1}]'},
        {"role": "user", "content": "the power went out"},
    ]
    gate.release()
    await h.close()


@scenario
async def test_a_cut_off_reply_with_calls_says_why_it_stopped(tmp_path: Path) -> None:
    script = [Reply([Part.Call("a", "ta", '{"p"')], Stop.Length)]
    h = await Harness.create(tmp_path, provider=FakeProvider(script))
    await h.input("look")
    await h.settle()
    await h.step()
    await h.settle()
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)
    body = await body_of(walk)
    assert sent(body)[1:] == [
        {"role": "user", "content": "look"},
        {"role": "assistant", "content": '[tool call a: ta {"p"]\n[reply stopped: length]'},
    ]
    await h.close()


@scenario
async def test_results_that_answer_no_batch_become_a_user_message(tmp_path: Path) -> None:
    recorder = Recorder(on={"pre_reply": answer_with_orphan_results})
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("x")])]), extensions=[recorder]
    )
    await h.input("go")
    await h.settle()
    await h.step()
    await h.settle()
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)
    assert isinstance(walk[-1].body, Body.Results)
    body = await body_of(walk)
    assert sent(body)[-1] == {"role": "user", "content": "[tool result zz: v]"}
    await h.close()


@scenario
async def test_a_batch_with_a_cut_off_call_goes_as_text_with_its_results() -> None:
    results = [("a", Outcome.Ok("one"), Author.Tools()), ("b", Outcome.Err("bad"), Author.Tools())]
    batch = [Part.Call("a", "ta", "{}"), Part.Call("b", "ta", '{"p"')]
    walk = walk_of(Body.User("go"), Body.Assistant(batch, Stop.Tools, None), Body.Results(results))
    body = await body_of(walk)
    assert sent(body)[1:] == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": '[tool call a: ta {}]\n[tool call b: ta {"p"]'},
        {"role": "user", "content": "[tool result a: one]\n[tool result b: Error: bad]"},
    ]


@scenario
async def test_a_call_with_no_arguments_is_sent_with_an_empty_object() -> None:
    walk = walk_of(
        Body.User("go"),
        Body.Assistant([Part.Call("a", "ta", "")], Stop.Tools, None),
        Body.Results([("a", Outcome.Ok("one"), Author.Tools())]),
    )
    body = await body_of(walk, tools=[FakeTool("ta", "A")])
    function = {"name": "ta", "arguments": "{}"}
    assert sent(body)[2]["tool_calls"] == [{"id": "a", "type": "function", "function": function}]


@scenario
async def test_a_system_node_after_the_first_turn_goes_as_a_user_system_note() -> None:
    walk = walk_of(
        Body.User("hi"),
        Body.Assistant([Part.Text("hello")], Stop.End, None),
        Body.System("now in French"),
        Body.User("again"),
    )
    body = await body_of(walk)
    assert sent(body) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "[system note: now in French]"},
        {"role": "user", "content": "again"},
    ]


@scenario
async def test_calls_in_history_go_as_text_when_the_sample_offers_no_tools() -> None:
    walk = walk_of(
        Body.User("go"),
        Body.Assistant([Part.Call("a", "ta", "{}")], Stop.Tools, None),
        Body.Results([("a", Outcome.Ok("one"), Author.Tools())]),
    )
    body = await body_of(walk)
    assert sent(body)[1:] == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "[tool call a: ta {}]"},
        {"role": "user", "content": "[tool result a: one]"},
    ]
    body = await body_of(walk, tools=[FakeTool("ta", "A")])
    assert "tool_calls" in sent(body)[2]
