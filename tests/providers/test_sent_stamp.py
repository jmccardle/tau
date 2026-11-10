"""`render` and the stamp of what was sent (D26 section 3.1): a provider that can show its
request (`OpenAIProvider`, `GeminiProvider`) stamps each reply's `meta.sent`, and names in
`extends` the reply before it when that reply's request is a byte prefix of this one. That is
the proof of a span on servers whose cache report cannot prove it; the meter's use of it is
`tests/behavior/test_spans.py`."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from harness import offered, scenario
from wire import (
    DIALECTS,
    GEMINI,
    OPENAI,
    Dialect,
    Server,
    running,
    sse_text,
    walk_of,
)

from tau import (
    Author,
    Body,
    Finish,
    GeminiProvider,
    Node,
    OpenAIProvider,
    Outcome,
    Part,
    Rendered,
    Stop,
    Tool,
)

RENDERING = [OPENAI, GEMINI]


def lookup(part: str) -> str:
    """Look a part up."""
    return f"{part}: bin 3"


def other(part: str) -> str:
    """Another tool."""
    return part


def render(dialect: Dialect, walk: Sequence[Node], tools: Sequence[Tool] = ()) -> Rendered:
    provider = dialect.make(None, {})
    assert isinstance(provider, OpenAIProvider | GeminiProvider)
    return provider.render(walk, dialect.model(), tools)


async def stamp(
    dialect: Dialect, walk: Sequence[Node], tools: Sequence[Tool] = ()
) -> tuple[Server, dict[str, Any], str]:
    """Sample `walk` once: the server, the reply's `meta.sent`, and its whole `meta` text."""
    server = sse_text(dialect.reply("ok"))
    provider = dialect.over(server)
    items = [i async for i in provider.sample(walk, dialect.model(), tools)]
    done = [i for i in items if isinstance(i, Finish)]
    assert done[0].meta is not None
    return server, json.loads(done[0].meta)["sent"], done[0].meta


def batch_walk() -> list[Node]:
    calls = [Part.Call("c1", "lookup", '{"part":"A"}'), Part.Call("c2", "lookup", '{"part":"B"}')]
    by = Author.Core()
    answers = [("c1", Outcome.Ok("A: bin 3"), by), ("c2", Outcome.Err("no such part"), by)]
    return walk_of(
        Body.User("look them up"), Body.Assistant(calls, Stop.Tools, None), Body.Results(answers)
    )


async def tools_of(tmp_path: Path, *functions: object) -> list[Tool]:
    """`functions` as the tools a sample is offered."""
    return list(await offered(tmp_path, *functions))


@pytest.mark.parametrize("dialect", RENDERING, ids=lambda d: d.name)
@scenario
async def test_render_is_the_request_that_is_sent(dialect: Dialect, tmp_path: Path) -> None:
    tools = await tools_of(tmp_path, lookup)
    walk = batch_walk()
    server, _, _ = await stamp(dialect, walk, tools)
    shown = render(dialect, walk, tools)
    assert shown.body == server.bodies[0]
    assert shown.url == str(server.requests[0].url)
    assert len(shown.messages) == len(shown.nodes)


@scenario
async def test_openai_messages_name_the_node_each_came_from(tmp_path: Path) -> None:
    walk = batch_walk()
    config, system, user, reply, results = (n.id for n in walk[-5:])
    shown = render(OPENAI, walk, await tools_of(tmp_path, lookup))
    roles = [json.loads(m)["role"] for m in shown.messages]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assert shown.nodes == ((system,), (user,), (reply,), (results,), (results,))
    assert config not in {n for ids in shown.nodes for n in ids}, "a Config node is not sent"


@scenario
async def test_gemini_contents_name_every_node_merged_into_them() -> None:
    walk = walk_of(Body.User("one"), Body.System("mind the gap"), Body.User("two"))
    one, note, two = (n.id for n in walk[-3:])
    shown = render(GEMINI, walk)
    assert len(shown.messages) == 1, "neighbouring user turns are one content"
    assert shown.nodes == ((one, note, two),)
    assert "sys" in shown.fixed, "the leading system text is outside the conversation"


@pytest.mark.parametrize("dialect", RENDERING, ids=lambda d: d.name)
@scenario
async def test_the_stamp_names_the_reply_whose_request_this_one_extends(dialect: Dialect) -> None:
    first = walk_of(Body.User("hi"))
    _, sent, meta = await stamp(dialect, first)
    assert set(sent) == {"messages", "fixed", "chain"}, "nothing came before: no `extends`"
    second = walk_of(
        Body.User("hi"), Body.Assistant([Part.Text("ok")], Stop.End, None, meta), Body.User("more")
    )
    _, again, _ = await stamp(dialect, second)
    assert again["messages"] == sent["messages"] + 2
    assert again["fixed"] == sent["fixed"]
    assert again["extends"] == second[-2].id
    assert render(dialect, second).extends(render(dialect, first))


@pytest.mark.parametrize("dialect", RENDERING, ids=lambda d: d.name)
@scenario
async def test_a_request_with_other_tools_or_another_past_extends_nothing(
    dialect: Dialect, tmp_path: Path
) -> None:
    _, _, meta = await stamp(dialect, walk_of(Body.User("hi")))
    reply = Body.Assistant([Part.Text("ok")], Stop.End, None, meta)
    tools = await tools_of(tmp_path, other)
    _, with_tools, _ = await stamp(
        dialect, walk_of(Body.User("hi"), reply, Body.User("more")), tools
    )
    assert "extends" not in with_tools, "the tool set is part of what is fixed"
    _, rewritten, _ = await stamp(dialect, walk_of(Body.User("hello"), reply, Body.User("more")))
    assert "extends" not in rewritten, "the first message is not the one that reply answered"
    _, new_system, _ = await stamp(
        dialect, walk_of(Body.User("hi"), reply, Body.User("more"), system="other")
    )
    assert "extends" not in new_system


@scenario
async def test_a_reply_with_no_stamp_before_it_extends_nothing() -> None:
    reply = Body.Assistant([Part.Text("ok")], Stop.End, None, json.dumps({"id": "x"}))
    _, sent, _ = await stamp(OPENAI, walk_of(Body.User("hi"), reply, Body.User("more")))
    assert "extends" not in sent


@scenario
async def test_through_the_driver_each_reply_extends_the_one_before(tmp_path: Path) -> None:
    server = sse_text(OPENAI.reply("ok"))
    async with running(tmp_path / "s", OPENAI.over(server)) as (session, driver, c):
        for text in ("one", "two", "three"):
            await driver.client("test").send(c, text)
            await driver.rest(c)
        walk = session.tree.walk(session.cursor(c).at)
    replies = [n for n in walk if isinstance(n.body, Body.Assistant)]
    metas = [n.body.meta for n in replies if isinstance(n.body, Body.Assistant)]
    stamps = [json.loads(m)["sent"] for m in metas if m is not None]
    assert [s["messages"] for s in stamps] == [2, 4, 6]
    assert [s.get("extends") for s in stamps] == [None, replies[0].id, replies[1].id]


def test_anthropic_does_not_render() -> None:
    """Its spans are proved by its cache report; D26 asks `render` of OpenAI and Gemini."""
    assert not hasattr(DIALECTS["anthropic"].make(None, {}), "render")
