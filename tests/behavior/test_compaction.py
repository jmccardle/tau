"""`compaction` (D24 section 2): a long branch summarized onto a new line.

The window is tiny so a few lines of talk fill it. A reply that reports its usage gives the
exact count, and the nodes after it add their characters divided by 4. The summary is written by
a summarizer cursor whose reply is scripted like any other sample."""

import asyncio
import json
from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario

from tau import (
    Author,
    Body,
    CommandFailed,
    Compaction,
    Ending,
    ModelCatalog,
    Node,
    Outcome,
    Part,
    Stop,
    Usage,
    model_config,
    read_models,
)

WINDOW = 1000
OPTIONS = {"reserve": 100, "soft": 900, "keep": 20}  # tokens: 80 characters of tail
LONG = "r" * 100  # a reply of 25 tokens
FILL = "." * 600  # bulk in the part that is summarized


def catalog(window: int | None = WINDOW, options: dict[str, object] | None = None) -> ModelCatalog:
    model: dict[str, object] = {"model": "a", "provider": FAKE_PROVIDER}
    if window is not None:
        model["context_window"] = window
    other = {"model": "b", "provider": FAKE_PROVIDER, "context_window": WINDOW}
    return read_models(
        cli={
            "models": {"a": model, "b": other},
            "extensions": {"compaction": OPTIONS if options is None else options},
        }
    )


def reply(text: str = LONG, input: int | None = None, output: int = 5) -> Reply:
    meta = None if input is None else json.dumps({"usage": {"input": input, "output": output}})
    return Reply([Part.Text(text)], Stop.End, meta=meta)


SUMMARY = "Goal: finish the task.\nProgress: three steps."


async def start(
    tmp_path: Path,
    replies: list[Reply | Fail],
    window: int | None = WINDOW,
    **options: object,
) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider(replies),
        extensions=[Compaction(), Usage()],
        catalog=catalog(window, {**OPTIONS, **options}),
        config=model_config("a"),
    )


async def say(h: Harness, text: str) -> None:
    await h.send(text)
    await h.settle()


def walk(h: Harness, cursor: int | None = None) -> list[Node]:
    return h.session.tree.walk(h.session.cursor(h.cursor if cursor is None else cursor).at)


def texts(nodes: list[Node]) -> list[str]:
    out: list[str] = []
    for n in nodes:
        match n.body:
            case Body.User(text=text):
                out.append("U:" + text)
            case Body.Assistant(parts=parts):
                out.append("A:" + "".join(p.text for p in parts if isinstance(p, Part.Text)))
            case _:
                out.append(type(n.body).__name__)
    return out


async def talk_to_the_edge(tmp_path: Path, **options: object) -> Harness:
    """Three turns, the third reply reporting 895 tokens of a 1000 window; the fourth input would
    not fit. Script: three replies, the summary, the fourth reply."""
    h = await start(
        tmp_path,
        [
            reply("one" + FILL),
            reply("two" + FILL),
            reply(LONG, input=895),
            reply(SUMMARY),
            reply("four"),
        ],
        **options,  # pyright: ignore[reportArgumentType]
    )
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    return h


@scenario
async def test_a_branch_that_would_not_fit_is_summarized_before_the_sample(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    old_tip = h.session.cursor(h.cursor).at
    before = walk(h)
    await say(h, "go 4")
    new = walk(h)
    assert texts(new[2:]) == [
        "U:The earlier part of this conversation was summarized:\n\n"
        f"<summary>\n{SUMMARY}\n</summary>",
        "A:" + LONG,
        "U:go 4",
        "A:four",
    ]
    assert new[:2] == before[:2], "the prefix is shared, not copied"
    assert new[2].parent == 1
    summary, kept_reply, kept_user, answer = new[2:]
    assert summary.by == Author.Extension("compaction")
    assert [n.copied_from for n in (kept_reply, kept_user)] == [before[-1].id, before[-1].id + 1]
    assert answer.copied_from is None
    assert h.session.tree.walk(old_tip) == before, "the old line is still in the tree"
    await h.close()


@scenario
async def test_the_summary_derives_from_the_last_node_it_replaced(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    before = walk(h)
    await say(h, "go 4")
    summary = walk(h)[2]
    head_last = before[-2]  # before: ..., U:go 3, A:reply 3 (the tail starts here)
    assert texts([head_last]) == ["U:go 3"]
    assert summary.derived_from == head_last.id
    await h.close()


@scenario
async def test_the_cursor_shows_compacting_and_the_clients_are_told(tmp_path: Path) -> None:
    gate = Gate()
    h = await start(
        tmp_path,
        [
            reply("one" + FILL),
            reply("two" + FILL),
            reply(LONG, input=895),
            Reply([Part.Text(SUMMARY)], gate=gate),
            reply("four"),
        ],
    )
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    await h.send("go 4")
    await h.until(lambda: gate.held == 1)
    await h.settle()
    assert dict((await h.view()).shown)["compaction"] == "compacting…"
    gate.release()
    await h.settle()
    assert "compaction" not in dict((await h.view()).shown)
    (told,) = [n for n in h.notices() if n.startswith("compacted")]
    assert told.startswith("compacted 331 → ")
    assert told.endswith(" tokens (nodes 2–6 summarized as node 11)")  # noqa: RUF001
    await h.close()


@scenario
async def test_the_summarizer_is_a_labelled_cursor_that_is_closed_afterwards(
    tmp_path: Path,
) -> None:
    h = await talk_to_the_edge(tmp_path)
    await say(h, "go 4")
    assert h.session.cursors() == [0]
    ((closed, last),) = h.session.closed()
    assert h.session.label("compaction:1") == closed
    prompt = h.session.tree.node(last - 1)
    assert isinstance(prompt.body, Body.User)
    assert prompt.by == Author.Extension("compaction")
    assert prompt.body.text.startswith("go 4\n\nSummarize the conversation above")
    # the request is the branch's request plus one message
    request = h.provider.requests[3]
    ids = tuple(n.id for n in h.session.tree.walk(prompt.id))
    assert request.walk == ids
    before = h.session.tree.node(ids[-2])
    assert isinstance(before.body, Body.Assistant), "the input is merged into the prompt"
    await h.close()


@scenario
async def test_compact_with_a_focus_and_with_nothing_to_compact(tmp_path: Path) -> None:
    h = await start(tmp_path, [reply("one")])
    await say(h, "go 1")
    assert await h.run("compact") == "nothing to compact yet"
    await h.close()
    h = await start(
        tmp_path / "b",
        [reply("a" * 300), reply("b" * 300), reply("c" * 300), reply(SUMMARY)],
        window=None,
    )
    for text in ("go 1" + "y" * 300, "go 2" + "y" * 300, "go 3" + "z" * 200):
        await say(h, text)
    result = await h.run("compact", {"focus": "the parser bug"})
    await h.settle()
    assert result is not None
    assert result.startswith("compacted ")
    (summary_prompt,) = [
        n
        for n in h.nodes()
        if isinstance(n.body, Body.User) and n.body.text.startswith("Summarize the conversation")
    ]
    assert isinstance(summary_prompt.body, Body.User)
    assert summary_prompt.body.text.endswith("pay particular attention to: the parser bug")
    await h.close()


@scenario
async def test_a_model_with_no_window_is_not_compacted_and_the_cursor_says_why(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path, [reply("one" + FILL), reply("two" + FILL)], window=None)
    await say(h, "go 1")
    await say(h, "go 2" + "z" * 8000)
    assert dict((await h.view()).shown)["compaction"] == "compaction: a has no context_window"
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_auto_off_never_compacts_by_itself(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path, auto=False)
    await say(h, "go 4")
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_esc_during_compaction_leaves_the_cursor_where_it_was(tmp_path: Path) -> None:
    gate = Gate()
    h = await start(
        tmp_path,
        [
            reply("one" + FILL),
            reply("two" + FILL),
            reply(LONG, input=895),
            Reply([Part.Text(SUMMARY)], gate=gate),
        ],
    )
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    await h.send("go 4")
    await h.until(lambda: gate.held == 1)
    await h.settle()
    where = h.session.cursor(h.cursor).at
    await h.abort()
    gate.release()
    await h.settle()
    assert h.session.cursor(h.cursor).at == where
    assert not [n for n in h.nodes() if n.copied_from is not None or n.derived_from is not None]
    assert len(h.provider.requests) == 4, "the aborted step sent no sample of its own"
    assert h.session.cursors() == [0], "the summarizer cursor is closed"
    view = await h.view()
    assert view.ending == Ending.Aborted()
    assert "compaction" not in dict(view.shown)
    await h.close()


@scenario
async def test_a_summarizer_that_fails_fails_the_step_and_writes_nothing(tmp_path: Path) -> None:
    h = await start(
        tmp_path,
        [
            reply("one" + FILL),
            reply("two" + FILL),
            reply(LONG, input=895),
            Fail("the server fell over"),
        ],
    )
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    where = h.session.cursor(h.cursor).at
    await say(h, "go 4")
    view = await h.view()
    assert view.hook_error is not None
    assert view.hook_error.extension == "compaction"
    assert "the server fell over" in view.hook_error.error
    assert not [n for n in h.nodes() if n.copied_from is not None]
    assert h.session.cursor(h.cursor).at == where + 1
    await h.close()


@scenario
async def test_compact_recovers_from_a_provider_that_said_the_prompt_was_too_long(
    tmp_path: Path,
) -> None:
    h = await start(
        tmp_path,
        [
            reply("a" * 400),
            reply("b" * 400),
            Fail("prompt is too long", status=400),
            reply(SUMMARY),
            reply("three"),
        ],
        window=None,
    )
    await say(h, "go 1" + "y" * 400)
    await say(h, "go 2" + "y" * 400)
    await say(h, "go 3" + "z" * 400)
    assert h.session.tree.node(h.session.cursor(h.cursor).at).body != Body.Assistant
    result = await h.run("compact")
    await h.settle()
    assert result is not None
    assert result.startswith("compacted")
    await h.step()
    await h.settle()
    last = h.session.tree.node(h.session.cursor(h.cursor).at)
    assert last.body == Body.Assistant([Part.Text("three")], Stop.End, None, None)
    await h.close()


@scenario
async def test_usage_counts_each_real_reply_once_not_its_copies(tmp_path: Path) -> None:
    h = await start(
        tmp_path,
        [
            reply("one" + FILL, input=100),
            reply("two" + FILL, input=100),
            reply(LONG, input=895),
            reply(SUMMARY, input=895),
            reply("four", input=200),
        ],
    )
    for text in ("go 1", "go 2", "go 3", "go 4"):
        await say(h, text)
    out = await h.run("usage")
    assert out is not None
    row = out.splitlines()[1].split()
    assert row[:2] == ["a", "5"], out  # five samples: three replies, the summary, and four
    await h.close()


@scenario
async def test_the_summary_can_be_written_by_another_model(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path, model="b")
    await say(h, "go 4")
    assert [r.model.model for r in h.provider.requests] == ["a", "a", "a", "b", "a"]
    await h.close()


@scenario
async def test_a_remote_client_reads_the_fields(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    await say(h, "go 4")
    seen = await h.client.walk(h.session.cursor(h.cursor).at)
    assert seen == walk(h)
    assert [n.copied_from is not None for n in seen[2:]] == [False, True, True, False]
    assert seen[2].derived_from == 6
    await h.close()


@scenario
async def test_esc_during_a_compact_command_closes_the_summarizer_too(tmp_path: Path) -> None:
    gate = Gate()
    h = await start(
        tmp_path,
        [reply("one" + FILL), reply("two" + FILL), Reply([Part.Text(SUMMARY)], gate=gate)],
        window=None,
    )
    await say(h, "go 1")
    await say(h, "go 2" + "z" * 400)
    where = h.session.cursor(h.cursor).at
    running = asyncio.ensure_future(h.run("compact"))
    await h.until(lambda: gate.held == 1)
    await h.abort()
    gate.release()
    with pytest.raises(CommandFailed):
        await running
    await h.settle()
    assert h.session.cursor(h.cursor).at == where
    assert h.session.cursors() == [0]
    assert not [n for n in h.nodes() if n.copied_from is not None or n.derived_from is not None]
    await h.close()


@scenario
async def test_a_call_the_summarizer_makes_is_refused(tmp_path: Path) -> None:
    work = FakeTool("work", "never runs")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                reply("one" + FILL),
                reply("two" + FILL),
                reply(LONG, input=895),
                Reply([Part.Call("c", "work", "{}")], Stop.Tools),
                reply(SUMMARY),
                reply("four"),
            ]
        ),
        tools=[work],
        extensions=[Compaction()],
        catalog=catalog(),
        config=model_config("a"),
    )
    for text in ("go 1", "go 2", "go 3", "go 4"):
        await say(h, text)
    assert not work.calls
    refused = [
        n
        for n in h.nodes()
        if isinstance(n.body, Body.Results)
        and n.body.outcomes[0][1] == Outcome.Err("summarizing: no tools")
    ]
    assert len(refused) == 1
    assert [n.copied_from is not None for n in walk(h)[2:]] == [False, True, True, False]
    await h.close()


@scenario
async def test_the_config_nodes_of_the_head_are_copied_so_the_branch_keeps_its_options(
    tmp_path: Path,
) -> None:
    h = await start(
        tmp_path,
        [
            reply("one" + FILL),
            reply("two" + FILL),
            reply(LONG, input=895),
            reply(SUMMARY),
            reply("four"),
        ],
    )
    await say(h, "go 1")
    await h.configure("compaction", {"keep": 20})
    for text in ("go 2", "go 3", "go 4"):
        await say(h, text)
    new = walk(h)
    configs = [n for n in new if isinstance(n.body, Body.Config)]
    assert len(configs) == 2, "the root and the copy of the head's"
    assert configs[1].copied_from is not None
    assert h.session.tree.node(configs[1].copied_from).body == configs[1].body
    assert new[2] == configs[1], "ahead of the summary, under the prefix"
    await h.close()


@scenario
async def test_a_keep_that_leaves_no_room_fails_the_step_and_says_so(tmp_path: Path) -> None:
    h = await start(tmp_path, [reply("one" + FILL)], keep=950)
    await say(h, "go 1")
    view = await h.view()
    assert view.hook_error is not None
    assert "keep (950)" in view.hook_error.error
    await h.close()


@scenario
async def test_a_32k_window_with_the_default_options_compacts_instead_of_failing(
    tmp_path: Path,
) -> None:
    big = "y" * 30_000  # 7.5k tokens
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                reply("a" * 30_000),
                reply("b" * 30_000, input=21_000),
                reply(SUMMARY),
                reply("done"),
            ]
        ),
        extensions=[Compaction()],
        catalog=read_models(
            cli={
                "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 32768}}
            }
        ),
        config=model_config("a"),
    )
    for text in (f"go 1{big}", f"go 2{big}", "go 3"):
        await say(h, text)
    view = await h.view()
    assert view.hook_error is None
    assert [n for n in h.notices() if n.startswith("compacted")]
    assert any(n.copied_from is not None for n in h.nodes())
    await h.close()


@scenario
async def test_a_newest_node_that_alone_fills_the_room_says_so_and_summarizes_nothing(
    tmp_path: Path,
) -> None:
    big = FakeTool("big", "x" * 4000)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("c", "big", "{}")], Stop.Tools), reply("never")]),
        tools=[big],
        extensions=[Compaction()],
        catalog=catalog(),
        config=model_config("a"),
    )
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "the newest node alone takes 1000 tokens" in view.hook_error.error
    assert "lower keep" not in view.hook_error.error, "keep is not the cause"
    assert len(h.provider.requests) == 1, "no summarizer was run"
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_a_tail_of_several_nodes_that_fills_the_room_fails_naming_keep(
    tmp_path: Path,
) -> None:
    args = json.dumps({"note": "n" * 1600})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("c", "big", args)], Stop.Tools), reply("never")]),
        tools=[FakeTool("big", "x" * 2000)],
        extensions=[Compaction()],
        catalog=catalog(),
        config=model_config("a"),
    )
    await say(h, "go" + "p" * 40)
    view = await h.view()
    assert view.hook_error is not None
    assert "keep = 20" in view.hook_error.error
    assert "lower keep" in view.hook_error.error
    assert len(h.provider.requests) == 1, "no summarizer was run"
    await h.close()


@scenario
async def test_a_compaction_that_would_not_be_shorter_writes_nothing(tmp_path: Path) -> None:
    h = await start(tmp_path, [reply("a" * 100), reply("b" * 100), reply(SUMMARY)], window=None)
    await say(h, "go 1")
    await say(h, "go 2")
    with pytest.raises(CommandFailed, match="would not make it shorter"):
        await h.run("compact")
    await h.settle()
    assert not [n for n in h.nodes() if n.copied_from is not None or n.derived_from is not None]
    await h.close()


async def tool_turn(
    tmp_path: Path, output: str, replies: list[Reply | Fail], **options: object
) -> Harness:
    """A session with a tool `big` that answers `output`, soft 700 and hard 900 of 1000."""
    return await Harness.create(
        tmp_path,
        provider=FakeProvider(replies),
        tools=[FakeTool("big", output)],
        extensions=[Compaction()],
        catalog=catalog(1000, {"reserve": 100, "soft": 700, "keep": 20, **options}),
        config=model_config("a"),
    )


def call(input: int) -> Reply:
    meta = json.dumps({"usage": {"input": input, "output": 5}})
    return Reply([Part.Call("c", "big", "{}")], Stop.Tools, meta=meta)


def summaries(h: Harness) -> list[Node]:
    return [
        n
        for n in h.nodes()
        if isinstance(n.body, Body.User) and "Summarize the conversation" in n.body.text
    ]


@scenario
async def test_a_turn_that_crosses_the_soft_limit_runs_to_its_end_then_compacts_once(
    tmp_path: Path,
) -> None:
    h = await tool_turn(
        tmp_path,
        "x" * 800,  # 200 tokens: 605 at the call, 810 at the end of the turn
        [call(600), reply("done", input=805), reply(SUMMARY), reply("ok")],
    )
    await say(h, "go" + "p" * 1200)
    assert len(h.provider.requests) == 3, "two samples of the turn, then the summarizer"
    assert not any(
        n.copied_from is not None for n in h.session.tree.walk(h.provider.requests[1].walk[-1])
    )
    (prompt,) = summaries(h)
    assert isinstance(prompt.body, Body.User)
    assert h.provider.requests[2].walk[-2] == prompt.parent
    reply_node = h.session.tree.node(prompt.parent or 0)
    assert isinstance(reply_node.body, Body.Assistant)
    assert texts([reply_node]) == ["A:done"], (
        "the summary was built from a walk ending in the reply"
    )
    tip = walk(h)
    assert tip[-1].copied_from == reply_node.id, "the cursor rests on the compacted line"
    assert [n for n in h.notices() if n.startswith("compacted")]
    await say(h, "next")
    assert len(summaries(h)) == 1, "the next message does not compact again"
    sent = h.provider.requests[3].walk
    assert [h.session.tree.node(i).copied_from is not None for i in sent[2:-1]] == [
        False,
        True,
        True,
        True,
    ]
    await h.close()


@scenario
async def test_a_turn_that_crosses_the_hard_limit_is_interrupted_and_compacts_at_once(
    tmp_path: Path,
) -> None:
    h = await tool_turn(
        tmp_path,
        "x" * 1400,  # 350 tokens: 955 before the second sample
        [call(600), reply(SUMMARY), reply("done", input=100)],
    )
    await say(h, "go" + "p" * 1200)
    first, summarizer, second = h.provider.requests
    assert not any(h.session.tree.node(i).copied_from is not None for i in first.walk)
    (prompt,) = summaries(h)
    assert summarizer.walk[-1] == prompt.id, "the summary came before the next sample"
    assert any(h.session.tree.node(i).copied_from is not None for i in second.walk)
    assert texts(walk(h))[-1] == "A:done"
    assert len(summaries(h)) == 1, "the end of the turn is under the soft limit"
    await h.close()


@scenario
async def test_a_soft_limit_above_the_hard_limit_is_refused(tmp_path: Path) -> None:
    h = await tool_turn(tmp_path, "x", [reply("one" + FILL)], hard=600, soft=700)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "soft (700) is above hard (600)" in view.hook_error.error
    await h.close()
    h = await tool_turn(tmp_path / "b", "x", [reply("one" + FILL)], soft=950)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "soft (950) is above the hard limit (900)" in view.hook_error.error
    await h.close()


async def soft_compacted(tmp_path: Path) -> tuple[Harness, int]:
    """A session whose first turn ended past the soft limit and was compacted once: the harness
    and the old line's last reply (the one the summary was built from)."""
    h = await tool_turn(
        tmp_path,
        "x" * 800,
        [call(600), reply("done", input=805), reply(SUMMARY), reply("never")],
    )
    await say(h, "go" + "p" * 1200)
    (prompt,) = summaries(h)
    assert prompt.parent is not None
    return h, prompt.parent


@scenario
async def test_going_back_to_a_reply_does_not_compact_again(tmp_path: Path) -> None:
    h, old_reply = await soft_compacted(tmp_path)
    await h.go_to(old_reply)
    await h.settle()
    assert h.session.cursor(h.cursor).at == old_reply, "the cursor is where it was sent"
    assert len(summaries(h)) == 1
    await h.close()


@scenario
async def test_a_cursor_opened_on_a_reply_does_not_build_its_own_compaction(
    tmp_path: Path,
) -> None:
    h, old_reply = await soft_compacted(tmp_path)
    other = await h.client.open(old_reply)
    await h.settle()
    assert h.session.cursor(other).at == old_reply
    assert len(summaries(h)) == 1
    await h.close()


@scenario
async def test_a_8k_window_works_with_the_default_options(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                reply("a" * 12_000, input=3005),
                reply("b" * 12_000, input=6010),
                reply(SUMMARY),
            ]
        ),
        extensions=[Compaction()],
        catalog=read_models(
            cli={"models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 8192}}}
        ),
        config=model_config("a"),
    )
    await say(h, "g" * 12_000)
    await say(h, "h" * 12_000)
    assert (await h.view()).hook_error is None
    assert [n for n in h.notices() if n.startswith("compacted")]
    await h.close()


@scenario
async def test_a_prefix_that_alone_passes_the_soft_limit_is_said_once_not_failed(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply("a" * 400, input=720), reply("b" * 400, input=720)]),
        system="s" * 3000,  # 750 tokens of prefix, over the soft limit of 700
        extensions=[Compaction()],
        catalog=catalog(1000, {"reserve": 100, "soft": 700, "keep": 20}),
        config=model_config("a"),
    )
    await say(h, "go 1" + "p" * 400)
    await say(h, "go 2" + "p" * 400)
    assert (await h.view()).hook_error is None
    told = [n for n in h.notices() if "soft limit" in n]
    assert len(told) == 1
    assert "the prefix" in told[0]
    assert len(h.provider.requests) == 2, "no summarizer was run"
    await h.close()


@scenario
async def test_a_keep_below_one_is_refused(tmp_path: Path) -> None:
    h = await tool_turn(tmp_path, "x", [reply("one")], keep=0)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "keep is a token count, at least 1" in view.hook_error.error
    await h.close()
