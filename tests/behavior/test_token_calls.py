"""`token_calls` (D26 section 8): before a sample, the server counts the request, and the counter
gives that count as the walk's `rendered` total, so the hard limit reads the server's number.

The provider here is the fake one with a count endpoint of its own (`Counting`): it answers each
count call with the next of `answers` (an int, `None` for "no count endpoint for this", or an
exception to raise) and records the walk it was asked about. What a real server answers is in
`tests/providers/test_llamacpp_fixtures.py`, recorded from llama.cpp."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, FakeProvider, Reply
from harness import Harness, scenario

from tau import (
    Basis,
    Compaction,
    Cursor,
    DefinitionError,
    ModelCatalog,
    ModelConfig,
    Node,
    Part,
    RequestCount,
    SampleError,
    Stop,
    Subject,
    TokenCalls,
    Tokens,
    Tool,
    model_config,
    read_models,
)


class Counting(FakeProvider):
    """A fake provider with a count endpoint: `answers` in order, then nothing."""

    def __init__(self, script: Sequence[Reply], answers: Sequence[int | Exception | None]) -> None:
        super().__init__(script)
        self._answers = list(answers)
        self.asked: list[tuple[int, ...]] = []

    async def count_call(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> int | None:
        self.asked.append(tuple(n.id for n in walk))
        answer = self._answers.pop(0) if self._answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer


class Probe:
    """Counts the walk before every sample, after `token_calls` and before compaction."""

    name = "probe"
    order = {"pre_sample": -10}  # noqa: RUF012 - read by the driver, never changed

    def __init__(self) -> None:
        self.counted: list[RequestCount] = []

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        self.counted.append(await cursor.count())


def reply(text: str, input_tokens: int) -> Reply:
    meta = {"usage": {"input": input_tokens, "output": 5}}
    return Reply([Part.Text(text)], Stop.End, meta=json.dumps(meta))


def catalog(window: int | None = None, **token_calls: object) -> ModelCatalog:
    model: dict[str, object] = {"model": "a", "provider": FAKE_PROVIDER}
    if window is not None:
        model["context_window"] = window
    extensions: dict[str, object] = {"token_calls": token_calls} if token_calls else {}
    return read_models(cli={"models": {"a": model}, "extensions": extensions})


@scenario
async def test_always_counts_each_request_and_the_count_is_its_rendered_total(
    tmp_path: Path,
) -> None:
    provider = Counting([reply("one", 120), reply("two", 160)], [120, 160])
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Tokens(tmp_path / "home", mode="off"), TokenCalls(), probe],
        catalog=catalog(when="always"),
        config=model_config("a"),
    )
    for text in ("hello", "again"):
        await h.send(text)
        await h.settle()
    assert len(provider.asked) == 2
    assert [r.walk for r in provider.requests] == provider.asked, "the walk that is sent"
    totals = [c.total for c in probe.counted]
    assert [(t.tokens, t.bound, t.basis) for t in totals] == [
        (120, 0, Basis.RENDERED),
        (160, 0, Basis.RENDERED),
    ]
    await h.close()


@scenario
async def test_straddle_asks_only_when_the_estimate_cannot_say_whether_it_fits(
    tmp_path: Path,
) -> None:
    """With nothing reported yet the request is all ceilings: 0 tokens, up to its bytes. A hard
    limit between the two is a straddle, and the server's count settles it below the limit, so
    compaction does not run; a window far above the ceiling asks nothing."""
    provider = Counting([reply("one", 40)], [40])
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Tokens(tmp_path / "home", mode="off"), TokenCalls(), Compaction(), probe],
        catalog=read_models(
            cli={
                "models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "context_window": 400}},
                "extensions": {"compaction": {"reserve": 300}},
            }
        ),
        config=model_config("a"),
    )
    await h.send("x " * 200)  # about 400 bytes: a ceiling past the hard limit of 100
    await h.settle()
    assert len(provider.asked) == 1
    assert probe.counted[0].total.basis is Basis.RENDERED
    assert probe.counted[0].total.upper == 40
    assert len(provider.requests) == 1, "sent as it was: no compaction ran"
    await h.close()

    roomy = Counting([reply("one", 40)], [40])
    h = await Harness.create(
        tmp_path / "roomy",
        provider=roomy,
        extensions=[Tokens(tmp_path / "home2", mode="off"), TokenCalls()],
        catalog=catalog(window=1_000_000),
        config=model_config("a"),
    )
    await h.send("hello")
    await h.settle()
    assert roomy.asked == [], "the estimate already says it fits"
    await h.close()


@scenario
async def test_a_count_of_another_walk_is_not_used_and_no_answer_changes_nothing(
    tmp_path: Path,
) -> None:
    provider = Counting([reply("one", 120), reply("two", 160)], [120, None])
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Tokens(tmp_path / "home", mode="off"), TokenCalls(), probe],
        catalog=catalog(when="always"),
        config=model_config("a"),
    )
    for text in ("hello", "again"):
        await h.send(text)
        await h.settle()
    assert probe.counted[0].total.basis is Basis.RENDERED
    assert probe.counted[1].total.basis is not Basis.RENDERED, "the first call's walk is shorter"
    await h.close()


@scenario
async def test_a_failed_count_call_is_told_and_the_sample_goes_on(tmp_path: Path) -> None:
    provider = Counting([reply("one", 120)], [SampleError("HTTP 500: busy", status=500)])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Tokens(tmp_path / "home", mode="off"), TokenCalls()],
        catalog=catalog(when="always"),
        config=model_config("a"),
    )
    await h.send("hello")
    await h.settle()
    assert len(provider.requests) == 1
    assert any("count call failed" in n and "busy" in n for n in h.notices())
    await h.close()


@scenario
async def test_a_provider_without_a_count_endpoint_is_asked_nothing(tmp_path: Path) -> None:
    provider = FakeProvider([reply("one", 120)])
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Tokens(tmp_path / "home", mode="off"), TokenCalls(), probe],
        catalog=catalog(when="always"),
        config=model_config("a"),
    )
    await h.send("hello")
    await h.settle()
    assert probe.counted[0].total.basis is Basis.CEILING
    assert not [n for n in h.notices() if "count call" in n]
    await h.close()


@scenario
async def test_token_calls_without_a_counter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match=r"needs a counter.*tau ext enable tokens"):
        await Harness.create(tmp_path, extensions=[TokenCalls()])
