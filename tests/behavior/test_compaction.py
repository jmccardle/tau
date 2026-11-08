"""`compaction` (D24 section 2, D26): a long branch summarized onto a new line.

The fake server bills what its script says, as a real one does (`bill`), so the cut, the checks
and the limits are exercised on reported counts; what no report covers is counted by its bytes.
The window is 40,000 and the reserve 2,000 (a summary may be 1,000 tokens; the hard limit is
38,000); the soft limit is out of the way unless a test is about it. The counts themselves are
`test_counts.py`; the fits and the limits under overhead are `test_compaction_counts.py`.
"""

import asyncio
import json
from collections.abc import Sequence
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

OPTIONS = {"reserve": 2000, "soft": 36_000, "keep": 1000}
BIG = "y" * 14_000  # 3,500 tokens; by its bytes 14,016
SUMMARY = "Goal: finish the task.\nProgress: three steps."
WRAPPED = (
    f"The earlier part of this conversation was summarized:\n\n<summary>\n{SUMMARY}\n</summary>"
)


def catalog(window: int | None = 40_000, options: dict[str, object] | None = None) -> ModelCatalog:
    model: dict[str, object] = {"model": "a", "provider": FAKE_PROVIDER}
    if window is not None:
        model["context_window"] = window
    other = {"model": "b", "provider": FAKE_PROVIDER, "context_window": 40_000}
    return read_models(
        cli={
            "models": {"a": model, "b": other},
            "extensions": {"compaction": OPTIONS if options is None else options},
        }
    )


def bill(text: str, input: int | None = None, output: int = 5, cached: int | None = None) -> Reply:
    """A reply, with the usage its server would report (none: it reports none)."""
    if input is None:
        return Reply([Part.Text(text)], Stop.End)
    usage: dict[str, int] = {"input": input, "output": output}
    if cached is not None:
        usage["cached"] = cached
    return Reply([Part.Text(text)], Stop.End, meta=json.dumps({"usage": usage}))


async def start(
    tmp_path: Path,
    replies: Sequence[Reply | Fail],
    window: int | None = 40_000,
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


def edge_script(summary: Reply | Fail | None = None, *then: Reply | Fail) -> list[Reply | Fail]:
    """Three turns that bill 25,020 tokens of a window whose hard limit is 38,000, then the
    summary, then the reply to the fourth input. The fourth input, `BIG`, is 14,016 by its bytes:
    it does not fit."""
    return [
        bill("one", 1000, 8000),
        bill("two", 9010, 8000, 1000),
        bill("three", 17_020, 8000, 9010),
        bill(SUMMARY, 25_100, 500) if summary is None else summary,
        *(then or (bill("four", 800, 5),)),
    ]


async def talk_to_the_edge(
    tmp_path: Path, summary: Reply | Fail | None = None, **options: object
) -> Harness:
    h = await start(tmp_path, edge_script(summary), **options)  # pyright: ignore[reportArgumentType]
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    return h


@scenario
async def test_a_branch_that_would_not_fit_is_summarized_before_the_sample(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    old_tip = h.session.cursor(h.cursor).at
    before = walk(h)
    await say(h, BIG)
    new = walk(h)
    assert texts(new[2:]) == ["U:" + WRAPPED, "U:" + BIG, "A:four"]
    assert new[:2] == before[:2], "the prefix is shared, not copied"
    assert new[2].parent == 1
    summary, kept_user, answer = new[2:]
    assert summary.by == Author.Extension("compaction")
    assert kept_user.copied_from == before[-1].id + 1
    assert answer.copied_from is None
    assert h.session.tree.walk(old_tip) == before, "the old line is still in the tree"
    await h.close()


@scenario
async def test_the_summary_derives_from_the_last_node_it_replaced(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    before = walk(h)
    await say(h, BIG)
    summary = walk(h)[2]
    assert texts([before[-1]]) == ["A:three"]
    assert summary.derived_from == before[-1].id
    await h.close()


@scenario
async def test_the_cursor_shows_compacting_and_the_clients_are_told(tmp_path: Path) -> None:
    gate = Gate()
    h = await talk_to_the_edge(tmp_path, Reply([Part.Text(SUMMARY)], gate=gate))
    await h.send(BIG)
    await h.until(lambda: gate.held == 1)
    await h.settle()
    assert dict((await h.view()).shown)["compaction"] == "compacting…"
    gate.release()
    await h.settle()
    assert "compaction" not in dict((await h.view()).shown)
    (told,) = [n for n in h.notices() if n.startswith("compacted")]
    assert told.startswith("compacted ≤39.0k → ≤"), "the request had a new input to count by bytes"
    assert told.endswith("(nodes 2–7 summarized as node 11; reported counts only)")  # noqa: RUF001
    await h.close()


@scenario
async def test_the_summarizer_is_a_labelled_cursor_that_is_closed_afterwards(
    tmp_path: Path,
) -> None:
    h = await talk_to_the_edge(tmp_path)
    await say(h, BIG)
    assert h.session.cursors() == [0]
    ((closed, last),) = h.session.closed()
    assert h.session.label("compaction:1") == closed
    prompt = h.session.tree.node(last - 1)
    assert isinstance(prompt.body, Body.User)
    assert prompt.by == Author.Extension("compaction")
    assert prompt.body.text.startswith(BIG + "\n\nSummarize the conversation above")
    # the request is the branch's request plus one message
    request = h.provider.requests[3]
    ids = tuple(n.id for n in h.session.tree.walk(prompt.id))
    assert request.walk == ids
    before = h.session.tree.node(ids[-2])
    assert isinstance(before.body, Body.Assistant), "the input is merged into the prompt"
    await h.close()


@scenario
async def test_compact_with_a_focus_and_with_nothing_to_compact(tmp_path: Path) -> None:
    h = await start(tmp_path, [bill("one")])
    await say(h, "go 1")
    assert await h.run("compact") == "nothing to compact yet"
    await h.close()
    h = await start(
        tmp_path / "b",
        [bill("a" * 300), bill("b" * 300), bill("c" * 300), bill(SUMMARY)],
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
    h = await start(tmp_path, [bill("one"), bill("two")], window=None)
    await say(h, "go 1")
    await say(h, "go 2" + "z" * 8000)
    assert dict((await h.view()).shown)["compaction"] == "a has no context_window"
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_auto_off_never_compacts_by_itself(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path, auto=False)
    await say(h, BIG)
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()
    h = await start(tmp_path / "b", [bill("a", 2000, 10_000, None)], auto=False, soft=5000)
    await say(h, "go")
    assert not h.notices(), "the soft limit is switched off by auto too"
    await h.close()


@scenario
async def test_esc_during_compaction_leaves_the_cursor_where_it_was(tmp_path: Path) -> None:
    gate = Gate()
    h = await talk_to_the_edge(tmp_path, Reply([Part.Text(SUMMARY)], gate=gate))
    await h.send(BIG)
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
    h = await talk_to_the_edge(tmp_path, Fail("the server fell over"))
    where = h.session.cursor(h.cursor).at
    await say(h, BIG)
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
            bill("a" * 400),
            bill("b" * 400),
            Fail("prompt is too long", status=400),
            bill(SUMMARY),
            bill("three"),
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
    h = await talk_to_the_edge(tmp_path)
    await say(h, BIG)
    out = await h.run("usage")
    assert out is not None
    row = out.splitlines()[1].split()
    assert row[:2] == ["a", "5"], out  # five samples: three replies, the summary, and four
    await h.close()


@scenario
async def test_the_summary_can_be_written_by_another_model(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path, model="b")
    await say(h, BIG)
    assert [r.model.model for r in h.provider.requests] == ["a", "a", "a", "b", "a"]
    await h.close()


@scenario
async def test_a_remote_client_reads_the_fields(tmp_path: Path) -> None:
    h = await talk_to_the_edge(tmp_path)
    await say(h, BIG)
    seen = await h.client.walk(h.session.cursor(h.cursor).at)
    assert seen == walk(h)
    assert [n.copied_from is not None for n in seen[2:]] == [False, True, False]
    assert seen[2].derived_from == 7
    await h.close()


@scenario
async def test_esc_during_a_compact_command_closes_the_summarizer_too(tmp_path: Path) -> None:
    gate = Gate()
    h = await start(
        tmp_path,
        [bill("one" + "r" * 400), bill("two" + "r" * 400), Reply([Part.Text(SUMMARY)], gate=gate)],
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
    script = edge_script(Reply([Part.Call("c", "work", "{}")], Stop.Tools))
    script.insert(4, bill(SUMMARY, 25_200, 500))
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=[work],
        extensions=[Compaction()],
        catalog=catalog(),
        config=model_config("a"),
    )
    for text in ("go 1", "go 2", "go 3", BIG):
        await say(h, text)
    assert not work.calls
    refused = [
        n
        for n in h.nodes()
        if isinstance(n.body, Body.Results)
        and n.body.outcomes[0][1] == Outcome.Err("summarizing: no tools")
    ]
    assert len(refused) == 1
    assert [n.copied_from is not None for n in walk(h)[2:]] == [False, True, False]
    await h.close()


@scenario
async def test_the_config_nodes_of_the_head_are_copied_so_the_branch_keeps_its_options(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path, edge_script())
    await say(h, "go 1")
    await h.configure("compaction", {"keep": 20})
    for text in ("go 2", "go 3", BIG):
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
    h = await start(tmp_path, [bill("one")], keep=36_500)
    await say(h, "go 1")
    view = await h.view()
    assert view.hook_error is not None
    assert "keep (36500)" in view.hook_error.error
    await h.close()


@scenario
async def test_a_32k_window_with_the_default_options_compacts_instead_of_failing(
    tmp_path: Path,
) -> None:
    # reserve 8,192, hard limit 24,576, soft limit 19,660: four turns of 3,000 tokens end at 24,000
    k = "x" * 12_000
    script: list[Reply | Fail] = [
        bill("a" * 12_000, 3000, 3000),
        bill("b" * 12_000, 9000, 3000, 3000),
        bill("c" * 12_000, 15_000, 3000, 9000),
        bill("d" * 12_000, 21_000, 3000, 15_000),
        bill(SUMMARY, 24_100, 500),
    ]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[Compaction()],
        catalog=read_models(
            cli={
                "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 32768}}
            }
        ),
        config=model_config("a"),
    )
    for _ in range(4):
        await say(h, k)
    view = await h.view()
    assert view.hook_error is None
    assert [n for n in h.notices() if n.startswith("compacted")]
    assert any(n.copied_from is not None for n in h.nodes())
    await h.close()


@scenario
async def test_a_request_over_the_window_only_by_its_bytes_is_sent_as_it_is(
    tmp_path: Path,
) -> None:
    # 30,000 characters are 7,500 tokens, but 30,000 by their bytes: past the 24,576 of the hard
    # limit. The reports say 15,000, so the request is not proved too big, and nothing can be
    # compacted but the one message: the model is asked, and says if it cannot take it.
    script: list[Reply | Fail] = [bill("a" * 400, 7500, 7500), bill("fine", 22_500, 100, 7500)]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[Compaction()],
        catalog=read_models(
            cli={
                "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 32768}},
                "extensions": {"compaction": {"soft": 24_000}},
            }
        ),
        config=model_config("a"),
    )
    await say(h, "x" * 100)
    await say(h, "z" * 30_000)
    view = await h.view()
    assert view.hook_error is None
    assert len(h.provider.requests) == 2, "the second sample was sent"
    (told,) = [n for n in h.notices() if "sent as it is" in n]
    assert "the newest node alone takes" in told
    await h.close()


async def tool_hit(tmp_path: Path, output: str, **options: object) -> Harness:
    """A 10,000-token window (hard limit 9,000) whose third reply bills 9,010 and calls a tool
    that answers `output`. The first request billed 6,000, and the two spans after it 1,500."""
    script: list[Reply | Fail] = [
        bill("a", 6000, 100),
        bill("b", 7500, 100, 6000),
        call_reply(9000, 10, 7500),
        bill("never", 100, 5),
    ]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=[FakeTool("big", output)],
        extensions=[Compaction()],
        catalog=catalog(10_000, {"reserve": 1000, "soft": 8000, "keep": 1000, **options}),
        config=model_config("a"),
    )
    for text in ("go 1", "go 2", "go 3"):
        await say(h, text)
    return h


@scenario
async def test_a_newest_node_that_alone_fills_the_room_says_so_and_summarizes_nothing(
    tmp_path: Path,
) -> None:
    h = await tool_hit(tmp_path, "x" * 40_000)
    view = await h.view()
    assert view.hook_error is not None
    assert "the newest node alone takes 40.0k tokens at most" in view.hook_error.error
    assert "lower keep" not in view.hook_error.error, "keep is not the cause"
    assert len(h.provider.requests) == 3, "no summarizer was run"
    assert not [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_the_newest_turn_alone_leaving_no_room_is_not_blamed_on_keep(
    tmp_path: Path,
) -> None:
    h = await tool_hit(tmp_path, "x" * 3000)  # 6,000 and 3,027 and room for a summary: 10,027
    view = await h.view()
    assert view.hook_error is not None
    assert "the newest nodes kept" in view.hook_error.error
    assert "lower keep" not in view.hook_error.error
    await h.close()


@scenario
async def test_a_tail_of_several_turns_that_fills_the_room_fails_naming_keep(
    tmp_path: Path,
) -> None:
    h = await tool_hit(tmp_path, "x" * 3000, keep=5500)  # the last span as well: 4,527
    view = await h.view()
    assert view.hook_error is not None
    assert "keep = 5500" in view.hook_error.error
    assert "lower keep" in view.hook_error.error
    assert len(h.provider.requests) == 3, "no summarizer was run"
    await h.close()


@scenario
async def test_a_compaction_that_would_not_be_shorter_writes_nothing(tmp_path: Path) -> None:
    long_summary = "s" * 2000
    replies = [bill("a" * 100), bill("b" * 100), bill(long_summary)]
    h = await start(tmp_path, replies, window=None, keep=20)
    await say(h, "go 1")
    await say(h, "go 2")
    with pytest.raises(CommandFailed, match="would not make it shorter"):
        await h.run("compact")
    await h.settle()
    assert not [n for n in h.nodes() if n.copied_from is not None or n.derived_from is not None]
    await h.close()


async def tool_turn(
    tmp_path: Path, output: str, replies: Sequence[Reply | Fail], **options: object
) -> Harness:
    """A session with a tool `big` that answers `output`, soft 10,000 and hard 38,000."""
    return await Harness.create(
        tmp_path,
        provider=FakeProvider(replies),
        tools=[FakeTool("big", output)],
        extensions=[Compaction()],
        catalog=catalog(40_000, {"reserve": 2000, "soft": 10_000, "keep": 1000, **options}),
        config=model_config("a"),
    )


def call_reply(input: int, output: int = 100, cached: int | None = None) -> Reply:
    usage: dict[str, int] = {"input": input, "output": output}
    if cached is not None:
        usage["cached"] = cached
    return Reply([Part.Call("c", "big", "{}")], Stop.Tools, meta=json.dumps({"usage": usage}))


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
        "x" * 800,  # 817 by its bytes; the next bill says 5,900
        [
            call_reply(4000),
            bill("done", 9900, 200, 4000),  # 10,100 at the end of the turn: past the soft limit
            bill(SUMMARY, 10_400, 500),
            bill("ok", 1200, 10),
        ],
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
    assert [h.session.tree.node(i).copied_from is not None for i in sent[2:-1]] == [False, True]
    await h.close()


@scenario
async def test_a_turn_that_crosses_the_hard_limit_is_interrupted_and_compacts_at_once(
    tmp_path: Path,
) -> None:
    h = await tool_turn(
        tmp_path,
        "x" * 30_000,  # 30,017 by its bytes, past the hard limit with the 13,110 reported
        [
            bill("one", 1000, 12_000),
            call_reply(13_010, 100, 1000),
            bill(SUMMARY, 13_300, 500),
            bill("done", 800, 10),
        ],
        soft=36_000,
    )
    await say(h, "go 1")
    await say(h, "the current question")
    first, second, summarizer, last = h.provider.requests
    assert not any(h.session.tree.node(i).copied_from is not None for i in second.walk)
    (prompt,) = summaries(h)
    assert summarizer.walk[-1] == prompt.id, "the summary came before the next sample"
    assert any(h.session.tree.node(i).copied_from is not None for i in last.walk)
    assert texts(walk(h))[-1] == "A:done"
    assert len(summaries(h)) == 1, "the end of the turn is under the soft limit"
    await h.close()
    assert first.walk


@scenario
async def test_a_soft_limit_above_the_hard_limit_is_refused(tmp_path: Path) -> None:
    h = await tool_turn(tmp_path, "x", [bill("one")], hard=6000, soft=7000)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "soft (7000) is above hard (6000)" in view.hook_error.error
    await h.close()
    h = await tool_turn(tmp_path / "b", "x", [bill("one")], soft=39_000)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "soft (39000) is above the hard limit (38000)" in view.hook_error.error
    await h.close()


async def soft_compacted(tmp_path: Path) -> tuple[Harness, int]:
    """A session whose first turn ended past the soft limit and was compacted once: the harness
    and the old line's last reply (the one the summary was built from)."""
    h = await tool_turn(
        tmp_path,
        "x" * 800,
        [
            call_reply(4000),
            bill("done", 9900, 200, 4000),
            bill(SUMMARY, 10_400, 500),
            bill("never"),
        ],
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
    # reserve 2,048: hard limit 6,144, soft limit 4,915, keep 2,457; five turns of 500 tokens
    k = "g" * 2000
    script: list[Reply | Fail] = [
        bill("a" * 2000, 500, 500),
        bill("b" * 2000, 1500, 500, 500),
        bill("c" * 2000, 2500, 500, 1500),
        bill("d" * 2000, 3500, 500, 2500),
        bill("e" * 2000, 4500, 500, 3500),
        bill(SUMMARY, 5100, 300),
    ]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[Compaction()],
        catalog=read_models(
            cli={"models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 8192}}}
        ),
        config=model_config("a"),
    )
    for _ in range(5):
        await say(h, k)
    assert (await h.view()).hook_error is None
    assert [n for n in h.notices() if n.startswith("compacted")]
    await h.close()


@scenario
async def test_a_prefix_that_alone_passes_the_soft_limit_is_said_once_not_failed(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([bill("a" * 400, 3500, 20), bill("b" * 400, 3600, 20, 3500)]),
        system="s" * 12_000,  # the first bill is 3,500 tokens, over a soft limit of 3,000
        extensions=[Compaction()],
        catalog=catalog(40_000, {"reserve": 2000, "soft": 3000, "keep": 10}),
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
    h = await tool_turn(tmp_path, "x", [bill("one")], keep=0)
    await say(h, "go")
    view = await h.view()
    assert view.hook_error is not None
    assert "keep is a token count, at least 1" in view.hook_error.error
    await h.close()


@scenario
async def test_a_second_compaction_on_a_32k_window_still_has_room(tmp_path: Path) -> None:
    # reserve 8,192: soft limit 19,660, keep 9,830, room for a summary 4,096. Each turn is a
    # 500-token input and a 2,500-token reply. The first compaction comes at the end of turn 7,
    # and the line it leaves begins with the summary and three kept replies. The first request on
    # that line bills them all (9,550), and the second compaction at the end of turn 11 only has
    # room because the base is what is left of that bill: the prefix, the summary and the input,
    # 1,050, not the replies the line copied, which have counts of their own.
    user = "u" * 2000

    def turn(k: int, first_input: int, before: int | None) -> Reply:
        input = first_input + 3000 * (k - 1)
        return bill("a" * 10_000, input, 2500, before)

    script: list[Reply | Fail] = [turn(1, 520, None)]
    script += [turn(k, 520, 520 + 3000 * (k - 2)) for k in range(2, 8)]
    script.append(bill(SUMMARY, 21_100, 500))
    script.append(turn(1, 9550, None))  # turn 8, on the compacted line
    script += [turn(k, 9550, 9550 + 3000 * (k - 2)) for k in range(2, 5)]
    script.append(bill(SUMMARY, 21_100, 500))
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[Compaction()],
        catalog=read_models(
            cli={
                "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 32768}}
            }
        ),
        config=model_config("a"),
    )
    for _ in range(11):
        await say(h, user)
    assert (await h.view()).hook_error is None
    assert len([n for n in h.notices() if n.startswith("compacted")]) == 2
    assert not [n for n in h.notices() if "cannot keep" in n]
    await h.close()
