"""Walks another provider, a crash, or a cut-off reply left behind: the Anthropic provider sends
what it can and never fails every later sample on the branch (model.md 10.7)."""

import re
from dataclasses import replace
from pathlib import Path

from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from wire import (
    ANTHROPIC,
    SIGNATURE,
    collect,
    fresh_walk,
    message,
    sent_request,
    thinking,
    walk_of,
)

from tau_next import Author, Body, ModelConfig, Node, Outcome, Part, Replay, Stop

RESOLVED = ANTHROPIC.model(None)
API_ID = re.compile(r"[a-zA-Z0-9_-]+")  # what the Messages API accepts as a tool_use id
SIGNED = Replay("anthropic", '{"type": "thinking", "signature": "' + SIGNATURE + '"}')


async def walk_after(
    tmp_path: Path, script: list[Reply], *inputs: str, system: str = "be brief", result: str = "A"
) -> tuple[Harness, list[Node]]:
    """A session that has run `script` for each input, and its walk."""
    h = await Harness.create(
        tmp_path, system=system, provider=FakeProvider(script), tools=[FakeTool("ta", result)]
    )
    for line in inputs:
        await h.send(line)
        await h.settle()
    return h, h.session.tree.walk(h.session.cursor(h.cursor).at)


async def request(walk: list[Node], tools: bool = True, **options: object) -> dict[str, object]:
    model: ModelConfig = replace(RESOLVED, options={"prompt_caching": False, **options})
    offered = [FakeTool("ta", "A")] if tools else []
    return (await sent_request(ANTHROPIC, walk, model, offered))[2]


async def messages(walk: list[Node], **options: object) -> list[dict[str, object]]:
    return (await request(walk, **options))["messages"]  # type: ignore[return-value]


@scenario
async def test_a_batch_whose_arguments_are_not_an_object_goes_as_text_with_its_results(
    tmp_path: Path,
) -> None:
    script = [
        Reply([Part.Text("looking"), Part.Call("a", "ta", '{"p"')], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h, walk = await walk_after(tmp_path, script, "look")
    results = walk[-2].body
    assert isinstance(results, Body.Results)
    outcome = results.outcomes[0][1]
    assert isinstance(outcome, Outcome.Err)
    assert (await messages(walk[:-1]))[1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking"},
                {"type": "text", "text": '[tool call a: ta {"p"]'},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": f"[tool result a: Error: {outcome.error}]"}],
        },
    ]
    await h.close()


@scenario
async def test_an_empty_reply_after_tool_results_is_left_out_and_the_next_input_joins_them(
    tmp_path: Path,
) -> None:
    script = [
        Reply([Part.Call("a", "ta", "{}")], Stop.Tools),
        Reply([]),
        Reply([Part.Text("x")]),
    ]
    h, walk = await walk_after(tmp_path, script, "go", "next")
    sent = await messages(walk[:-1])
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "A", "is_error": False},
        {"type": "text", "text": "next"},
    ]
    await h.close()


@scenario
async def test_a_reply_of_only_unsent_thinking_is_left_out_and_its_neighbours_merge(
    tmp_path: Path,
) -> None:
    script = [Reply([Part.Thinking("cut off")], Stop.Interrupted), Reply([Part.Text("x")])]
    h, walk = await walk_after(tmp_path, script, "first", "second")
    assert await messages(walk[:-1]) == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        }
    ]
    await h.close()


@scenario
async def test_ids_the_api_would_refuse_are_mapped_alike_in_the_call_and_its_result(
    tmp_path: Path,
) -> None:
    ids = ["call.1:a", "call.1.a", "toolu_01B", "café/1"]
    script = [
        Reply([Part.Call(i, "ta", "{}") for i in ids], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h, walk = await walk_after(tmp_path, script, "go")
    sent = await messages(walk[:-1])
    called = [b["id"] for b in sent[1]["content"]]  # type: ignore[attr-defined]
    answered = [b["tool_use_id"] for b in sent[2]["content"]]  # type: ignore[attr-defined]
    assert called == answered, "each result answers its own call"
    assert all(API_ID.fullmatch(i) for i in called), called
    assert len(set(called)) == len(ids), "ids that were different stay different"
    assert called[2] == "toolu_01B", "an id the API accepts is sent as it is"
    assert (await messages(walk[:-1]))[1]["content"] == sent[1]["content"], (
        "the same walk, the same ids"
    )  # type: ignore[index]
    await h.close()


@scenario
async def test_manual_thinking_sends_a_batch_with_no_thinking_as_text_and_adaptive_does_not(
    tmp_path: Path,
) -> None:
    script = [Reply([Part.Call("a", "ta", "{}")], Stop.Tools), Reply([Part.Text("ok")])]
    h, walk = await walk_after(tmp_path, script, "go")
    manual = {"type": "enabled", "budget_tokens": 2048}
    assert (await messages(walk[:-1], thinking=manual))[1:] == [
        {"role": "assistant", "content": [{"type": "text", "text": "[tool call a: ta {}]"}]},
        {"role": "user", "content": [{"type": "text", "text": "[tool result a: A]"}]},
    ]
    adaptive = await messages(walk[:-1], thinking={"type": "adaptive"})
    assert [b["type"] for b in adaptive[1]["content"]] == ["tool_use"]  # type: ignore[attr-defined]
    await h.close()


@scenario
async def test_manual_thinking_keeps_a_signed_batch_as_tool_use(tmp_path: Path) -> None:
    script = [
        Reply([Part.Thinking("mine", SIGNED), Part.Call("a", "ta", "{}")], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h, walk = await walk_after(tmp_path, script, "go")
    manual = {"type": "enabled", "budget_tokens": 2048}
    sent = await messages(walk[:-1], thinking=manual)
    assert [b["type"] for b in sent[1]["content"]] == ["thinking", "tool_use"]  # type: ignore[attr-defined]
    await h.close()


@scenario
async def test_a_signature_that_cannot_be_kept_as_replay_data_fails_the_sample() -> None:
    body = message(thinking(0, ["hm"], signature="\ud800"))
    items, error = await collect(ANTHROPIC.serving(body), fresh_walk())
    assert error is not None
    assert "surrogate" in str(error)
    assert not any(isinstance(i, Part.Thinking) and i.replay for i in items)


def loop_script(first: list[Part]) -> list[Reply]:
    """A tool loop of two batches (`first` opens it) and a closing reply."""
    return [
        Reply(first, Stop.Tools),
        Reply([Part.Call("b", "ta", "{}")], Stop.Tools),
        Reply([Part.Text("done")]),
    ]


def kinds(sent: list[dict[str, object]]) -> list[list[str]]:
    return [[b["type"] for b in m["content"]] for m in sent]  # type: ignore[attr-defined]


MANUAL = {"type": "enabled", "budget_tokens": 2048}


@scenario
async def test_manual_thinking_keeps_every_batch_of_a_loop_that_opened_with_thinking(
    tmp_path: Path,
) -> None:
    script = loop_script([Part.Thinking("mine", SIGNED), Part.Call("a", "ta", "{}")])
    h, walk = await walk_after(tmp_path, script, "go")
    sent = await messages(walk[:-1], thinking=MANUAL)
    assert kinds(sent) == [
        ["text"],
        ["thinking", "tool_use"],
        ["tool_result"],
        ["tool_use"],
        ["tool_result"],
    ]
    await h.close()


@scenario
async def test_manual_thinking_sends_every_batch_of_a_loop_that_opened_without_it_as_text(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(tmp_path, loop_script([Part.Call("a", "ta", "{}")]), "go")
    sent = await messages(walk[:-1], thinking=MANUAL)
    assert kinds(sent) == [["text"], ["text"], ["text"], ["text"], ["text"]]
    assert sent[3]["content"] == [{"type": "text", "text": "[tool call b: ta {}]"}]
    again = await messages(walk[:-1], thinking=MANUAL)
    assert again == sent, "the same walk is the same request"
    await h.close()


@scenario
async def test_manual_thinking_leaves_a_finished_loop_alone(tmp_path: Path) -> None:
    script = [*loop_script([Part.Call("a", "ta", "{}")]), Reply([Part.Text("next")])]
    h, walk = await walk_after(tmp_path, script, "go", "more")
    sent = await messages(walk[:-1], thinking=MANUAL)
    assert kinds(sent) == [
        ["text"],
        ["tool_use"],
        ["tool_result"],
        ["tool_use"],
        ["tool_result"],
        ["text"],
        ["text"],
    ]
    await h.close()


@scenario
async def test_without_tools_a_batch_in_the_history_goes_as_text(tmp_path: Path) -> None:
    script = [Reply([Part.Call("a", "ta", "{}")], Stop.Tools), Reply([Part.Text("ok")])]
    h, walk = await walk_after(tmp_path, script, "go")
    body = await request(walk[:-1], tools=False)
    assert "tools" not in body
    assert kinds(body["messages"]) == [["text"], ["text"], ["text"]]  # type: ignore[arg-type]
    assert kinds(await messages(walk[:-1])) == [["text"], ["tool_use"], ["tool_result"]]
    await h.close()


@scenario
async def test_an_empty_system_and_blank_user_text_are_left_out_of_the_request(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("a")])], "q", system="")
    blank = h.session.append(walk[-1].id, Body.User(""), Author.Core(), 0)
    model = RESOLVED
    body = (await sent_request(ANTHROPIC, h.session.tree.walk(blank), model))[2]
    assert "system" not in body
    assert kinds(body["messages"]) == [["text"], ["text"]]  # type: ignore[arg-type]
    marks = [b for m in body["messages"] for b in m["content"] if "cache_control" in b]  # type: ignore[attr-defined]
    assert marks == [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}]
    await h.close()


@scenario
async def test_an_empty_tool_result_does_not_carry_the_cache_mark(tmp_path: Path) -> None:
    script = [
        Reply([Part.Call("a", "ta", "{}"), Part.Call("b", "ta", "{}")], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h, walk = await walk_after(tmp_path, script, "go", result="")
    model = RESOLVED
    tools = [FakeTool("ta", "")]
    body = (await sent_request(ANTHROPIC, walk[:-1], model, tools))[2]
    results = body["messages"][-1]["content"]  # type: ignore[index]
    assert all("cache_control" not in r for r in results)
    await h.close()


async def sent_kinds(args_raw: str) -> list[str]:
    """The block types sent for a walk whose one call has `args_raw`."""
    walk = walk_of(
        Body.User("go"),
        Body.Assistant([Part.Call("a", "ta", args_raw)], Stop.Tools),
        Body.Results([("a", Outcome.Ok("done"), Author.Core())]),
    )
    request = await sent_request(ANTHROPIC, walk, RESOLVED, [FakeTool("ta", "A")])
    sent: list[dict[str, list[dict[str, str]]]] = request[2]["messages"]  # type: ignore[assignment]
    return [block["type"] for message in sent for block in message["content"]]


@scenario
async def test_arguments_with_a_nan_or_a_lone_surrogate_send_the_batch_as_text() -> None:
    assert "tool_use" in await sent_kinds('{"x": 1}')
    for bad in ('{"x": NaN}', '{"x": Infinity}', '{"x": "\\ud800"}'):
        assert "tool_use" not in await sent_kinds(bad), bad
