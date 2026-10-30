"""Walks another provider, a crash, or a cut-off reply left behind: the Anthropic provider sends
what it can and never fails every later sample on the branch (model.md 10.7)."""

import re
from dataclasses import replace
from pathlib import Path

from anthropic_streams import (
    ENVIRON,
    RESOLVED,
    SIGNATURE,
    collect,
    message,
    provider,
    stream,
    thinking,
)
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from transcripts import fresh_walk

from tau_next import Author, Body, ModelConfig, Node, Outcome, Part, Replay, Stop
from tau_next.providers.anthropic import AnthropicProvider

SIGNED = Replay("anthropic", '{"type": "thinking", "signature": "' + SIGNATURE + '"}')


async def walk_after(
    tmp_path: Path, script: list[Reply], *inputs: str
) -> tuple[Harness, list[Node]]:
    """A session that has run `script` for each input, and its walk."""
    h = await Harness.create(
        tmp_path, system="be brief", provider=FakeProvider(script), tools=[FakeTool("ta", "A")]
    )
    for line in inputs:
        await h.send(line)
        await h.settle()
    return h, h.session.tree.walk(h.session.cursor(h.cursor).at)


def messages(walk: list[Node], **options: object) -> list[dict[str, object]]:
    model: ModelConfig = replace(RESOLVED, options={"prompt_caching": False, **options})
    body = AnthropicProvider(environ=ENVIRON).request(walk, model, [])[2]
    return body["messages"]  # type: ignore[return-value]


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
    assert messages(walk[:-1])[1:] == [
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
    sent = messages(walk[:-1])
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
    assert messages(walk[:-1]) == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        }
    ]
    await h.close()


@scenario
async def test_a_system_node_after_the_start_is_a_marked_user_message(tmp_path: Path) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("a")])], "q")
    later = h.session.append(walk[-1].id, Body.System("be formal"), Author.Core(), 0)
    sent = messages(h.session.tree.walk(later))
    assert sent[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "[system: be formal]"}],
    }
    await h.close()


@scenario
async def test_ids_the_api_would_refuse_are_mapped_alike_in_the_call_and_its_result(
    tmp_path: Path,
) -> None:
    script = [
        Reply([Part.Call("call.1:a", "ta", "{}"), Part.Call("toolu_01B", "ta", "{}")], Stop.Tools),
        Reply([Part.Text("ok")]),
    ]
    h, walk = await walk_after(tmp_path, script, "go")
    sent = messages(walk[:-1])
    called = [b["id"] for b in sent[1]["content"]]  # type: ignore[attr-defined]
    answered = [b["tool_use_id"] for b in sent[2]["content"]]  # type: ignore[attr-defined]
    assert called == answered
    assert re.fullmatch(r"call_1_a_[0-9a-f]{8}", called[0])
    assert called[1] == "toolu_01B"
    assert messages(walk[:-1])[1]["content"][0]["id"] == called[0]  # type: ignore[index]
    await h.close()


@scenario
async def test_manual_thinking_sends_a_batch_with_no_thinking_as_text_and_adaptive_does_not(
    tmp_path: Path,
) -> None:
    script = [Reply([Part.Call("a", "ta", "{}")], Stop.Tools), Reply([Part.Text("ok")])]
    h, walk = await walk_after(tmp_path, script, "go")
    manual = {"type": "enabled", "budget_tokens": 2048}
    assert messages(walk[:-1], thinking=manual)[1:] == [
        {"role": "assistant", "content": [{"type": "text", "text": "[tool call a: ta {}]"}]},
        {"role": "user", "content": [{"type": "text", "text": "[tool result a: A]"}]},
    ]
    adaptive = messages(walk[:-1], thinking={"type": "adaptive"})
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
    sent = messages(walk[:-1], thinking=manual)
    assert [b["type"] for b in sent[1]["content"]] == ["thinking", "tool_use"]  # type: ignore[attr-defined]
    await h.close()


@scenario
async def test_a_signature_that_cannot_be_kept_as_replay_data_fails_the_sample() -> None:
    body = message(thinking(0, ["hm"], signature="\ud800"))
    items, error = await collect(provider(stream(body)), fresh_walk(), RESOLVED)
    assert error is not None
    assert "replay" in str(error)
    assert not any(isinstance(i, Part.Thinking) and i.replay for i in items)
