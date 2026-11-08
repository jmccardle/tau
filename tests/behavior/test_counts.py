"""Counts and the meter (D26 sections 1, 3 and 4): `Cursor.count` with no counter, and with the
test-only counter an extension may offer. Compaction's use of them is `test_compaction_counts.py`.
"""

import json
import random
from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import (
    Basis,
    CommandFailed,
    Compaction,
    Count,
    CounterAnswer,
    CountJob,
    Cursor,
    DefinitionError,
    ModelCatalog,
    Part,
    RequestCount,
    Stop,
    Usage,
    model_config,
    read_models,
)

K = "x" * 4000  # 1,000 tokens


def bill(input: int, output: int, cached: int | None = None, text: str = "a" * 4000) -> Reply:
    usage: dict[str, int] = {"input": input, "output": output}
    if cached is not None:
        usage["cached"] = cached
    return Reply([Part.Text(text)], Stop.End, meta=json.dumps({"usage": usage}))


class Probe:
    """An extension that counts the walk when told and keeps the answer."""

    name = "probe"

    def __init__(self, counter: object | None = None) -> None:
        self.counted: list[RequestCount] = []
        self.counter = counter
        self.commands = (self.probe,)

    async def probe(self, cursor: Cursor) -> str:
        """Count this walk."""
        self.counted.append(await cursor.count())
        return "counted"


class Fills:
    """A counter that counts every node it is asked about as 7 tokens, plus or minus one."""

    def __init__(self, also: tuple[int, ...] = ()) -> None:
        self.also = also
        self.jobs: list[CountJob] = []

    def count(self, job: CountJob) -> CounterAnswer:
        self.jobs.append(job)
        asked = (*job.unreported, *self.also)
        return CounterAnswer({i: Count(7, Basis.HEURISTIC, 1) for i in asked})


def catalog() -> ModelCatalog:
    model = {"model": "a", "provider": FAKE_PROVIDER, "context_window": 1_000_000}
    return read_models(cli={"models": {"a": model}})


async def run(tmp_path: Path, replies: list[Reply], probe: Probe) -> Harness:
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
    return h


def proved_turns() -> list[Reply]:
    """Three turns of 1,000-token messages; each request reads the whole previous one."""
    return [bill(1000, 1000), bill(3000, 1000, 1000), bill(5000, 1000, 3000)]


@scenario
async def test_with_no_report_every_node_is_a_ceiling_of_its_bytes(tmp_path: Path) -> None:
    probe = Probe()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("hi")])]), extensions=[probe]
    )
    await h.send("hello")
    await h.settle()
    await h.run("probe")
    (counted,) = probe.counted
    assert counted.base_nodes == 0
    assert counted.nodes == {
        1: Count(0, Basis.CEILING, len("sys") + 16),
        2: Count(0, Basis.CEILING, len("hello") + 16),
        3: Count(0, Basis.CEILING, len("hi") + 16),
    }
    assert counted.total == Count(0, Basis.CEILING, 3 + 5 + 2 + 3 * 16)
    await h.close()


@scenario
async def test_reports_give_the_total_the_base_and_a_lump_for_each_proved_span(
    tmp_path: Path,
) -> None:
    probe = Probe()
    h = await run(tmp_path, proved_turns(), probe)
    await h.run("probe")
    (counted,) = probe.counted
    assert counted.total == Count(6000, Basis.REPORTED)
    assert counted.base == Count(1000, Basis.HEURISTIC), "a run of one character takes nothing out"
    assert counted.base_nodes == 3, "the root, the system text and the first message"
    # nodes: 0 root, 1 system, 2 user, 3 reply, 4 user, 5 reply, 6 user, 7 reply
    assert counted.nodes[3] == Count(2000, Basis.REPORTED), "the reply and the next input"
    assert 4 not in counted.nodes, "inside a lump a node has no count of its own"
    assert counted.nodes[5] == Count(2000, Basis.REPORTED)
    assert 6 not in counted.nodes
    assert counted.nodes[7] == Count(1000, Basis.REPORTED), "the newest reply: its output"
    assert counted.nodes[2].basis is Basis.CEILING, "the base holds it; its bytes cap it"
    assert counted.nodes[2].upper == 4000 + 16
    await h.close()


@scenario
async def test_a_span_the_second_request_does_not_prove_is_a_ceiling(tmp_path: Path) -> None:
    probe = Probe()
    replies = [bill(1000, 1000), bill(3000, 1000, 512), bill(5000, 1000, 3000)]
    h = await run(tmp_path, replies, probe)
    await h.run("probe")
    (counted,) = probe.counted
    assert counted.nodes[3] == Count(1000, Basis.REPORTED), "the reply's own output"
    assert counted.nodes[4] == Count(0, Basis.CEILING, 4000 + 16)
    assert counted.nodes[5] == Count(2000, Basis.REPORTED), "the next span is proved"
    assert counted.total == Count(6000, Basis.REPORTED), "the total is still the bill"
    await h.close()


@scenario
async def test_what_follows_the_newest_report_is_added_as_a_ceiling(tmp_path: Path) -> None:
    probe = Probe()
    call = Reply(
        [Part.Call("c", "big", "{}")],
        Stop.Tools,
        meta=json.dumps({"usage": {"input": 1000, "output": 100}}),
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, Fail("the server fell over")]),
        tools=[FakeTool("big", "r" * 400)],
        extensions=[probe],
    )
    await h.send("go")
    await h.settle()
    await h.run("probe")
    (counted,) = probe.counted
    assert counted.total == Count(1100, Basis.CEILING, 1 + 400 + 16), "the bill, and the result"
    await h.close()


@scenario
async def test_a_copy_that_holds_a_whole_lump_takes_its_originals_count(tmp_path: Path) -> None:
    probe = Probe()
    options = {"extensions": {"compaction": {"reserve": 2000, "soft": 5500, "keep": 3000}}}
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([*proved_turns(), bill(2000, 300, text="s" * 800)]),
        extensions=[Compaction(), Usage(), probe],
        catalog=read_models(
            cli={
                "models": {
                    "a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 40_000}
                },
                **options,
            }
        ),
        config=model_config("a"),
    )
    for _ in range(3):
        await h.send(K)
        await h.settle()
    line = h.session.tree.walk(h.session.cursor(h.cursor).at)
    assert [n.copied_from is not None for n in line] == [False, False, False, True, True, True]
    await h.run("probe")
    (counted,) = probe.counted
    copies = [n for n in line if n.copied_from is not None]
    assert counted.nodes[copies[0].id] == Count(2000, Basis.REPORTED)
    assert copies[1].id not in counted.nodes, "the lump is whole, so its second node is inside it"
    assert counted.nodes[copies[2].id] == Count(1000, Basis.REPORTED)
    await h.close()


@scenario
async def test_a_counter_fills_what_no_report_covers_and_nothing_else(tmp_path: Path) -> None:
    counter = Fills()
    probe = Probe(counter)
    h = await run(tmp_path, proved_turns(), probe)
    assert probe.counter is counter
    await h.run("probe")
    (counted,) = probe.counted
    (job,) = counter.jobs
    assert set(job.unreported) == {1, 2}, "the system text and the first message"
    assert set(job.known) == {3, 5, 7}
    assert counted.nodes[2] == Count(7, Basis.HEURISTIC, 1)
    assert counted.nodes[3] == Count(2000, Basis.REPORTED), "a report is never replaced"
    assert counted.total == Count(6000, Basis.REPORTED), "nothing is after the newest report"
    await h.close()


@scenario
async def test_a_counter_that_counts_a_reported_node_fails_the_count(tmp_path: Path) -> None:
    probe = Probe(Fills(also=(3,)))
    h = await run(tmp_path, proved_turns(), probe)
    with pytest.raises(CommandFailed, match=r"probe.*counted node 3"):
        await h.run("probe")
    await h.close()


@scenario
async def test_usage_says_how_counts_are_made(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Usage()])
    out = await h.run("usage")
    assert out is not None
    assert (
        out.splitlines()[-1] == "counts: reported only, with byte ceilings for what is not sent yet"
    )
    await h.close()
    h = await Harness.create(tmp_path / "b", extensions=[Usage(), Probe(Fills())])
    out = await h.run("usage")
    assert out is not None
    assert out.splitlines()[-1] == (
        "counts: reported, and by the probe extension for what is not sent yet"
    )
    await h.close()


@scenario
async def test_two_extensions_offering_a_counter_are_refused(tmp_path: Path) -> None:
    class Other(Probe):
        name = "other"

    with pytest.raises(
        DefinitionError, match="two extensions offer a counter: 'probe' and 'other'"
    ):
        await Harness.create(tmp_path, extensions=[Probe(Fills()), Other(Fills())])


@scenario
async def test_a_counter_without_a_count_method_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="`counter` must have a callable `count"):
        await Harness.create(tmp_path, extensions=[Probe(object())])


# The sum rule (D26 section 1), as a property over random counts.

PROVENANCES = list(Basis)


def random_count(rng: random.Random) -> Count:
    return Count(rng.randrange(0, 5000), rng.choice(PROVENANCES), rng.randrange(0, 300))


def test_a_sum_adds_tokens_and_bounds_and_is_as_weak_as_its_weakest_part() -> None:
    rng = random.Random(26)
    for _ in range(500):
        parts = [random_count(rng) for _ in range(rng.randrange(1, 8))]
        total = parts[0]
        for part in parts[1:]:
            total = total + part
        assert total.tokens == sum(p.tokens for p in parts)
        assert total.bound == sum(p.bound for p in parts)
        assert total.upper == sum(p.upper for p in parts)
        assert total.basis is max((p.basis for p in parts), key=PROVENANCES.index)


def test_a_sum_is_the_same_in_any_order() -> None:
    rng = random.Random(27)
    for _ in range(200):
        parts = [random_count(rng) for _ in range(5)]
        shuffled = rng.sample(parts, len(parts))
        a, b = parts[0], shuffled[0]
        for x, y in zip(parts[1:], shuffled[1:], strict=True):
            a, b = a + x, b + y
        assert a == b


def test_a_count_is_not_negative() -> None:
    with pytest.raises(ValueError, match="not negative"):
        Count(-1, Basis.REPORTED)


@scenario
async def test_a_line_with_no_report_counts_the_tool_definitions_it_will_send(
    tmp_path: Path,
) -> None:
    probe = Probe()
    tool = FakeTool("big", "r", description="d" * 100)
    h = await Harness.create(tmp_path, provider=FakeProvider([]), tools=[tool], extensions=[probe])
    await h.run("probe")
    (counted,) = probe.counted
    assert counted.base.basis is Basis.CEILING
    assert counted.base.bound >= len("big") + 100, "the name and the description, at least"
    assert counted.total.upper >= counted.base.upper
    await h.close()


@scenario
async def test_a_first_message_of_one_repeated_character_stays_in_the_base(tmp_path: Path) -> None:
    # 32 kB of spaces is a few hundred tokens, and no ratio of bytes to tokens says how few: the
    # whole first bill stays in the base. Ordinary text of the same size comes out at a fifth.
    probe = Probe()
    h = await Harness.create(
        tmp_path / "a",
        provider=FakeProvider([bill(10_000, 100, text="a")]),
        extensions=[probe],
    )
    await h.send(" " * 32_000)
    await h.settle()
    await h.run("probe")
    assert probe.counted[0].base == Count(10_000, Basis.HEURISTIC)
    await h.close()
    prose = Probe()
    h = await Harness.create(
        tmp_path / "b",
        provider=FakeProvider([bill(10_000, 100, text="a")]),
        extensions=[prose],
    )
    await h.send("word " * 6400)
    await h.settle()
    await h.run("probe")
    assert prose.counted[0].base == Count(10_000 - 32_000 // 8, Basis.HEURISTIC)
    await h.close()
