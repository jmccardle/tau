"""Compaction on the counts the servers report (D26 T1): the cut, the checks, the notice and the
limits all measure in one unit, the bill.

The fake server here bills like a real one: a hidden overhead on every request, and `cached`
equal to the previous request's `input` where it would find the whole request in its cache. A
node of 4,000 characters is 1,000 tokens. The window is 40,000, the reserve 2,000 (so a summary
may be 1,000 tokens, the hard limit is 38,000), unless a test says otherwise."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario

from tau import (
    Body,
    CommandFailed,
    Compaction,
    ModelCatalog,
    Node,
    Part,
    Stop,
    Usage,
    model_config,
    read_models,
)

K = "x" * 4000  # 1,000 tokens
SUMMARY = "S" * 2000  # what a summarizer writes; its reply reports 500 tokens
WRAPPER = 94  # the ceiling of the words around a summary: 78 bytes and 16 for the node


def catalog(window: int | None = 40_000, **options: object) -> ModelCatalog:
    model: dict[str, object] = {"model": "a", "provider": FAKE_PROVIDER}
    if window is not None:
        model["context_window"] = window
    chosen = {"reserve": 2000, "soft": 10_000, "keep": 1000, **options}
    return read_models(cli={"models": {"a": model}, "extensions": {"compaction": chosen}})


def bill(
    text: str, input: int, output: int, cached: int | None = None, *, stop: Stop = Stop.End
) -> Reply:
    usage: dict[str, int] = {"input": input, "output": output}
    if cached is not None:
        usage["cached"] = cached
    return Reply([Part.Text(text)], stop, meta=json.dumps({"usage": usage}))


def turns(n: int, overhead: int = 0, each: int = 1000) -> list[Reply]:
    """The first `n` replies of a conversation of messages of `each` tokens: reply k bills
    `overhead + 2 * each * k - each` in and `each` out, and reads the whole previous request."""
    replies: list[Reply] = []
    for k in range(1, n + 1):
        bill_in = overhead + 2 * each * k - each
        before = overhead + 2 * each * (k - 1) - each
        text = "a" * (each * 4)
        replies.append(bill(text, bill_in, each, None if k == 1 else before))
    return replies


async def start(
    tmp_path: Path,
    script: Sequence[Reply | Fail],
    window: int | None = 40_000,
    tools: list[FakeTool] | None = None,
    **options: object,
) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=tools or [],
        extensions=[Compaction(), Usage()],
        catalog=catalog(window, **options),
        config=model_config("a"),
    )


async def say(h: Harness, text: str = K) -> None:
    await h.send(text)
    await h.settle()


def walk(h: Harness) -> list[Node]:
    return h.session.tree.walk(h.session.cursor(h.cursor).at)


def summaries(h: Harness) -> list[Node]:
    return [
        n
        for n in h.nodes()
        if isinstance(n.body, Body.User) and "Summarize the conversation" in n.body.text
    ]


def told(h: Harness, word: str) -> list[str]:
    return [n for n in h.notices() if word in n]


def summarizer(text: str, input: int, output: int) -> Reply:
    return bill(text, input, output)


@scenario
async def test_a_soft_compaction_leaves_a_line_the_next_bill_finds_under_the_limit(
    tmp_path: Path,
) -> None:
    # 6,000 tokens of hidden overhead on every bill (tool definitions, the hidden prompt).
    script: list[Reply | Fail] = [*turns(3, overhead=6000), summarizer(SUMMARY, 12_100, 500)]
    script.append(bill("a" * 4000, 8530, 1000))  # S 530 + the kept reply 1000 + "go 4" 1000 + 6000
    h = await start(tmp_path, script)
    for _ in range(3):
        await say(h)
    (notice,) = told(h, "compacted")
    assert notice.startswith("compacted 12.0k → ≤8.6k tokens"), notice
    assert notice.endswith("reported counts only)"), notice
    await say(h)  # the fourth turn ends at 9,530 against a soft limit of 10,000
    assert len(summaries(h)) == 1, "the line was under the limit by the measure the bill uses"
    assert len(h.provider.requests) == 5
    assert (await h.view()).hook_error is None
    await h.close()


@scenario
async def test_when_the_overhead_and_the_tail_alone_pass_the_soft_limit_no_summary_is_paid_for(
    tmp_path: Path,
) -> None:
    # The first request bills 7,000: 6,000 of it overhead. With the tail (1,000) and room for a
    # summary (1,000) the line would be 9,000, over a soft limit of 7,500, however it is cut.
    h = await start(tmp_path, turns(3, overhead=6000), soft=7500)
    for _ in range(3):
        await say(h)
    assert len(h.provider.requests) == 3, "no summarizer was sampled"
    (said,) = told(h, "soft limit")
    assert "cannot keep this under its soft limit" in said
    assert (await h.view()).hook_error is None
    await h.close()


@scenario
async def test_a_summary_that_does_not_fit_the_soft_limit_is_paid_for_once(tmp_path: Path) -> None:
    big = 2000
    script: list[Reply | Fail] = [*turns(3, each=big), summarizer("S" * 60_000, 12_100, 15_000)]
    script += [
        bill("a" * (big * 4), 14_000, big, 10_000),
        bill("a" * (big * 4), 18_000, big, 14_000),
    ]
    h = await start(tmp_path, script)
    for _ in range(5):
        await say(h, "x" * (big * 4))
    assert len(summaries(h)) == 1, "the turns after the first failure did not pay again"
    assert len(h.provider.requests) == 6
    assert not [n for n in h.nodes() if n.copied_from is not None]
    (said,) = told(h, "soft limit")
    assert "15" in said, "the summary's size"
    assert "10.0k" in said, "and the limit"
    assert (await h.view()).hook_error is None
    await h.close()


@scenario
async def test_the_tail_is_cut_after_the_summary_so_the_line_fits(tmp_path: Path) -> None:
    # Six turns of 2,000 tokens; the tail wanted (keep 6,000) is 7,000 and the summary is 5,000,
    # so only 3,906 is left for it: the tail starts two turns later than it was going to.
    script: list[Reply | Fail] = [*turns(6), summarizer("S" * 20_000, 12_100, 5000)]
    h = await start(tmp_path, script, keep=6000)
    for _ in range(6):
        await say(h)
    before = [n for n in h.nodes() if n.copied_from is None and n.derived_from is None]
    users = [n.id for n in before if isinstance(n.body, Body.User) and n.body.text == K]
    replies = [n.id for n in before if isinstance(n.body, Body.Assistant)][:6]
    line = walk(h)
    assert [n.copied_from for n in line[3:]] == [replies[4], users[5], replies[5]]
    assert line[2].derived_from == users[4], "the last node it did not copy"
    (notice,) = told(h, "compacted")
    assert notice.startswith("compacted 12.0k → ≤9.1k tokens"), notice
    await h.close()


@scenario
async def test_compact_refuses_a_summary_no_shorter_than_what_it_replaces(
    tmp_path: Path,
) -> None:
    script: list[Reply | Fail] = [bill("a" * 400, 30, 100), summarizer(SUMMARY, 200, 500)]
    h = await start(tmp_path, script, keep=20)
    await say(h, "u" * 40)
    assert len(h.provider.requests) == 1
    with pytest.raises(CommandFailed, match="shorter"):
        await h.run("compact")
    await h.close()


@scenario
async def test_the_hard_limit_counts_a_big_tool_result_by_its_bytes_and_keeps_the_question(
    tmp_path: Path,
) -> None:
    big = FakeTool("big", "r" * 16_000)  # about 4,000 tokens; its ceiling is 16,000
    call = Reply(
        [Part.Call("c", "big", "{}")],
        Stop.Tools,
        meta=json.dumps({"usage": {"input": 24_010, "output": 100, "cached": 18_000}}),
    )
    script: list[Reply | Fail] = [
        bill("a" * 24_000, 6000, 6000),
        bill("a" * 24_000, 18_000, 6000, 6000),
        call,
        summarizer(SUMMARY, 24_200, 500),
        bill("done", 8000, 10),
    ]
    h = await start(tmp_path, script, tools=[big], soft=36_000)
    await say(h, "x" * 24_000)
    await say(h, "y" * 24_000)
    await say(h, "the current question")
    assert len(h.provider.requests) == 5, "the summary came before the second sample"
    line = walk(h)
    question = next(n for n in h.nodes() if getattr(n.body, "text", "") == "the current question")
    old = {n.id: n for n in h.nodes()}
    summary, asked, called, result, done = line[-5:]
    assert asked.copied_from == question.id
    assert asked.body == question.body, "the question the turn answers is kept verbatim"
    assert isinstance(old[called.copied_from or 0].body, Body.Assistant)
    assert isinstance(result.body, Body.Results)
    assert result.copied_from is not None
    assert summary.derived_from == question.id
    assert done.copied_from is None
    await h.close()


@pytest.mark.parametrize("how", ["abort", "close"])
@scenario
async def test_stopping_the_summarizer_at_the_hard_limit_sends_no_sample(
    tmp_path: Path, how: str
) -> None:
    gate = Gate()
    script: list[Reply | Fail] = [
        bill("a" * 40_000, 60_000, 30_000),
        Reply([Part.Text(SUMMARY)], gate=gate),
        bill("never sent", 100, 10),
    ]
    h = await start(tmp_path, script, window=100_000, soft=96_000)
    await say(h, "u" * 40_000)
    await h.send("v" * 36_000)  # 90,000 and this message, 9,000 tokens or 36,000 bytes: past 98,000
    await h.until(lambda: gate.held == 1)
    other = h.session.label("compaction:1")
    if how == "abort":
        await h.abort(other)
    else:
        await h.client.close_cursor(other)
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 2, "the sample that did not fit was not sent"
    view = await h.view()
    assert view.hook_error is not None
    assert view.hook_error.extension == "compaction"
    await h.close()


@scenario
async def test_a_window_too_small_for_a_summary_is_a_config_error_not_a_crash(
    tmp_path: Path,
) -> None:
    h = await start(
        tmp_path, [bill("a" * 400, 30, 100)], window=3, reserve=None, soft=None, keep=None
    )
    await say(h, "hello")
    view = await h.view()
    assert view.hook_error is not None
    assert "IndexError" not in view.hook_error.error
    assert "no room for a summary" in view.hook_error.error
    await h.close()


@scenario
async def test_compact_works_on_a_model_without_a_window(tmp_path: Path) -> None:
    script: list[Reply | Fail] = [*turns(3), summarizer(SUMMARY, 6000, 500)]
    h = await start(tmp_path, script, window=None, keep=None, reserve=None, soft=None)
    for _ in range(3):
        await say(h)
    result = await h.run("compact")
    assert result is not None
    assert result.startswith("compacted 6.0k →"), result
    assert [n for n in h.nodes() if n.copied_from is not None]
    await h.close()


@scenario
async def test_compact_straight_after_a_compaction_does_not_summarize_the_summary(
    tmp_path: Path,
) -> None:
    script: list[Reply | Fail] = [*turns(3, overhead=6000), summarizer(SUMMARY, 12_100, 500)]
    h = await start(tmp_path, script, keep=500)
    for _ in range(3):
        await say(h)
    assert len(summaries(h)) == 1
    assert await h.run("compact") == "nothing to compact yet"
    assert len(h.provider.requests) == 4
    await h.close()
