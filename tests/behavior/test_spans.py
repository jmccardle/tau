"""Proved spans (D26 section 3): the three ways the nodes between two requests are known to cost
the difference of the two bills, `TreeHandle.spans`, and what the meter makes of each. The usage
here is scripted (`FakeProvider`): what a provider must put in a reply's `meta` for each proof is
`tests/providers/test_usage_facts.py` and `test_sent_stamp.py`.

Node ids in these sessions: 0 the config, 1 the system text, 2 the first message, 3 its reply, 4
the second message, 5 its reply, 6 the third message, 7 its reply."""

import json
from pathlib import Path

from fakes import FAKE_PROVIDER, Fail, FakeProvider, Reply
from harness import Harness, scenario

from tau import (
    Basis,
    Compaction,
    Count,
    Cursor,
    ModelCatalog,
    Part,
    RequestCount,
    Span,
    Stop,
    Usage,
    model_config,
    read_models,
)

K = "x" * 4000


def reply(usage: dict[str, int], **meta: object) -> Reply:
    return Reply([Part.Text("a" * 4000)], Stop.End, meta=json.dumps({"usage": usage, **meta}))


class Probe:
    """An extension that reads the walk's spans and its count when told."""

    name = "probe"

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.counted: list[RequestCount] = []
        self.commands = (self.probe,)

    async def probe(self, cursor: Cursor) -> str:
        """Read this walk's spans and count."""
        self.spans = cursor.spans()
        self.counted.append(await cursor.count())
        return "read"


def catalog() -> ModelCatalog:
    model = {"model": "a", "provider": FAKE_PROVIDER, "context_window": 1_000_000}
    return read_models(cli={"models": {"a": model}})


async def run(tmp_path: Path, replies: list[Reply]) -> tuple[Harness, Probe]:
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(replies),
        extensions=[probe],
        catalog=catalog(),
        config=model_config("a"),
    )
    for _ in replies:
        await h.send(K)
        await h.settle()
    await h.run("probe")
    return h, probe


def shape(spans: list[Span]) -> list[tuple[tuple[int, ...], int, str]]:
    return [(tuple(n.id for n in s.nodes), s.cost, s.proof) for s in spans]


@scenario
async def test_a_cache_read_of_the_whole_previous_request_proves_a_span(tmp_path: Path) -> None:
    replies = [
        reply({"input": 1000, "output": 1000}),
        reply({"input": 3000, "output": 1000, "cached": 1000}),
        reply({"input": 5000, "output": 1000, "cached": 3000}),
    ]
    h, probe = await run(tmp_path, replies)
    assert shape(probe.spans) == [((3, 4), 2000, "cache"), ((5, 6), 2000, "cache")]
    await h.close()


@scenario
async def test_llama_cpps_slot_proves_a_span_when_it_held_at_least_the_whole_request(
    tmp_path: Path,
) -> None:
    """The slot keeps the reply it generated too, so the next request usually reads more than
    the previous request from it: `cache_n` 2,090 against a first request of 1,000."""
    replies = [
        reply({"input": 1000, "output": 1000}, timings={"prompt_n": 1000, "cache_n": 0}),
        reply(
            {"input": 3000, "output": 1000, "cached": 2090},
            timings={"prompt_n": 910, "cache_n": 2090},
        ),
        # the slot was lost (another request used it): less than the second request was read
        reply(
            {"input": 5000, "output": 1000, "cached": 2999},
            timings={"prompt_n": 2001, "cache_n": 2999},
        ),
    ]
    h, probe = await run(tmp_path, replies)
    assert shape(probe.spans) == [((3, 4), 2000, "slot")]
    (counted,) = probe.counted
    assert counted.nodes[3] == Count(2000, Basis.REPORTED)
    assert 4 not in counted.nodes, "inside the lump"
    assert counted.nodes[5] == Count(1000, Basis.REPORTED), "unproved: the reply's own output"
    assert counted.nodes[6].basis is Basis.CEILING, "and the message after it by its bytes"
    await h.close()


@scenario
async def test_a_request_stamped_as_extending_the_previous_reply_proves_a_span(
    tmp_path: Path,
) -> None:
    replies = [
        reply({"input": 1000, "output": 1000}, sent={"messages": 2}),
        reply({"input": 3000, "output": 1000, "cached": 512}, sent={"messages": 4, "extends": 3}),
        reply({"input": 5000, "output": 1000}, sent={"messages": 6, "extends": 5}),
    ]
    h, probe = await run(tmp_path, replies)
    assert shape(probe.spans) == [((3, 4), 2000, "prefix"), ((5, 6), 2000, "prefix")]
    (counted,) = probe.counted
    assert counted.total == Count(6000, Basis.REPORTED)
    assert counted.nodes[5] == Count(2000, Basis.REPORTED)
    await h.close()


@scenario
async def test_a_stamp_that_names_another_reply_proves_nothing(tmp_path: Path) -> None:
    replies = [
        reply({"input": 1000, "output": 1000}, sent={"messages": 2}),
        reply({"input": 3000, "output": 1000}, sent={"messages": 4}),  # extends nothing
        reply({"input": 5000, "output": 1000}, sent={"messages": 6, "extends": 3}),  # not 5
    ]
    h, probe = await run(tmp_path, replies)
    assert probe.spans == []
    (counted,) = probe.counted
    assert counted.nodes[4].basis is Basis.CEILING
    await h.close()


@scenario
async def test_a_second_bill_smaller_than_the_first_is_no_span(tmp_path: Path) -> None:
    replies = [
        reply({"input": 3000, "output": 1000}, sent={"messages": 2}),
        reply({"input": 2000, "output": 1000}, sent={"messages": 4, "extends": 3}),
    ]
    h, probe = await run(tmp_path, replies)
    assert probe.spans == []
    assert probe.counted[0].total == Count(3000, Basis.REPORTED)
    await h.close()


@scenario
async def test_a_reply_without_usage_between_two_bills_breaks_the_span(tmp_path: Path) -> None:
    replies = [
        reply({"input": 1000, "output": 1000}),
        Reply([Part.Text("quiet")], Stop.End),
        reply({"input": 5000, "output": 1000, "cached": 1000}),
    ]
    h, probe = await run(tmp_path, replies)
    assert probe.spans == []
    await h.close()


@scenario
async def test_a_summary_costs_its_reply_without_the_thinking(tmp_path: Path) -> None:
    """D26 section 3.2: the summary holds the summarizer's words, not its thinking, so its count
    is the reply's `output` less its `reasoning`. The bills are those of
    `test_compaction_counts`: with 2,000 tokens of thinking in the summarizer's 2,500 of output,
    the line is counted as it is with a summarizer that reports 500 and none."""

    def bill(text: str, input: int, output: int, cached: int | None = None) -> Reply:
        usage = {"input": input, "output": output}
        if cached is not None:
            usage["cached"] = cached
        return Reply([Part.Text(text)], Stop.End, meta=json.dumps({"usage": usage}))

    script: list[Reply | Fail] = [
        bill("a" * 4000, 7000, 1000),
        bill("a" * 4000, 9000, 1000, 7000),
        bill("a" * 4000, 11_000, 1000, 9000),
        Reply(
            [Part.Thinking("t" * 8000), Part.Text("S" * 2000)],
            Stop.End,
            meta=json.dumps({"usage": {"input": 12_100, "output": 2500, "reasoning": 2000}}),
        ),
    ]
    model = {"model": "a", "provider": FAKE_PROVIDER, "context_window": 40_000}
    options = {"reserve": 2000, "soft": 10_000, "keep": 1000}
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[Compaction(), Usage()],
        catalog=read_models(cli={"models": {"a": model}, "extensions": {"compaction": options}}),
        config=model_config("a"),
    )
    for _ in range(3):
        await h.send(K)
        await h.settle()
    (notice,) = [n for n in h.notices() if "compacted" in n]
    assert notice.startswith("compacted 12.0k → ≤8.6k tokens"), notice
    await h.close()
