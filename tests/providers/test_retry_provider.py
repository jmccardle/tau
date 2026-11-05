"""`RetryProvider` (D24 section 4) on scripted attempts: the statuses and failures it retries,
the waits, and what it will not retry. (`tests/behavior/test_retry.py` runs it under a driver.)"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

import httpx
import pytest

from tau import (
    Delta,
    Finish,
    ModelConfig,
    Node,
    Part,
    ProviderClosed,
    Retrying,
    RetryProvider,
    SampleError,
    Stop,
    TextDelta,
    Tool,
)

MODEL = ModelConfig("fake", provider="fake")


@dataclass(frozen=True)
class Try:
    """One scripted attempt: yield `items`, then raise `error` if given, else finish."""

    items: Sequence[Part | Delta] = ()
    error: SampleError | None = None


class ScriptedProvider:
    def __init__(self, script: Sequence[Try]) -> None:
        self._script = list(script)
        self.calls = 0
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    def check(self, model: ModelConfig) -> list[str]:
        return ["scripted check"]

    async def aclose(self) -> None:
        self.closed = True

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        attempt = self._script[self.calls]
        self.calls += 1
        for item in attempt.items:
            yield item
        if attempt.error is not None:
            raise attempt.error
        yield Finish(Stop.End, None, None)


class Waits:
    """The sleep injected in place of `asyncio.sleep`: records, never waits."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


async def collect(provider: object) -> list[Part | Finish | Delta]:
    return [item async for item in provider.sample([], MODEL, [])]  # type: ignore[attr-defined]


def transport_error() -> SampleError:
    error = SampleError("connection reset")
    error.__cause__ = httpx.ReadError("reset")
    return error


def test_a_429_is_retried_after_the_wait_it_asked_for_then_succeeds() -> None:
    inner = ScriptedProvider(
        [Try(error=SampleError("slow down", status=429, retry_after=7.0)), Try([Part.Text("hi")])]
    )
    waits = Waits()
    items = asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert items == [
        Retrying(1, 3, 7.0, "slow down"),
        Part.Text("hi"),
        Finish(Stop.End, None, None),
    ]
    assert inner.calls == 2
    assert waits.seconds == [7.0]


def test_a_transport_failure_is_retried_with_exponential_backoff_until_retries_run_out() -> None:
    inner = ScriptedProvider([Try(error=transport_error()) for _ in range(4)])
    waits = Waits()
    with pytest.raises(SampleError, match="connection reset") as failed:
        asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert inner.calls == 4
    assert waits.seconds == [1.0, 2.0, 4.0]
    assert "still failing after 3 retries" in str(failed.value)
    assert isinstance(failed.value.__cause__, SampleError), "the last try's error is kept"


@pytest.mark.parametrize("first", [Part.Text("partial"), TextDelta("par")])
def test_a_failure_after_a_part_or_delta_was_yielded_is_not_retried(first: Part | Delta) -> None:
    inner = ScriptedProvider(
        [Try([first], SampleError("busy", status=503)), Try([Part.Text("again")])]
    )
    waits = Waits()
    seen: list[Part | Finish | Delta] = []

    async def run() -> None:
        wrapped = RetryProvider(inner, sleep=waits)
        async for item in wrapped.sample([], MODEL, []):
            seen.append(item)

    with pytest.raises(SampleError, match="busy"):
        asyncio.run(run())
    assert seen == [first]
    assert inner.calls == 1
    assert waits.seconds == []


@pytest.mark.parametrize(
    "error",
    [
        SampleError("bad walk"),
        SampleError("template error", status=500),
        SampleError("bad request", status=400),
        SampleError("unauthorized", status=401),
        ProviderClosed("closed"),
        ProviderClosed("closed", status=503),
    ],
)
def test_a_deterministic_error_is_raised_at_once(error: SampleError) -> None:
    inner = ScriptedProvider([Try(error=error), Try([Part.Text("never")])])
    waits = Waits()
    with pytest.raises(SampleError) as raised:
        asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert raised.value is error
    assert inner.calls == 1
    assert waits.seconds == []


@pytest.mark.parametrize("status", [408, 429, 502, 503, 504, 529])
def test_each_transient_status_is_retried(status: int) -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=status)), Try()])
    asyncio.run(collect(RetryProvider(inner, sleep=Waits())))
    assert inner.calls == 2


def test_a_500_that_carries_retry_after_is_retried() -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=500, retry_after=2.0)), Try()])
    asyncio.run(collect(RetryProvider(inner, sleep=Waits())))
    assert inner.calls == 2


def test_a_retry_after_over_sixty_seconds_is_not_waited_for_but_fails_naming_the_wait() -> None:
    inner = ScriptedProvider([Try(error=SampleError("slow", status=429, retry_after=61.0)), Try()])
    waits = Waits()
    with pytest.raises(SampleError, match="61 s") as raised:
        asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert (raised.value.status, raised.value.retry_after) == (429, 61.0)
    assert inner.calls == 1
    assert waits.seconds == []


def test_sixty_seconds_exactly_is_waited_for() -> None:
    inner = ScriptedProvider([Try(error=SampleError("slow", status=429, retry_after=60.0)), Try()])
    waits = Waits()
    asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert waits.seconds == [60.0]


def test_each_wait_is_announced_before_it_with_the_try_it_is_and_why() -> None:
    inner = ScriptedProvider(
        [Try(error=SampleError("busy\nmore", status=503)), Try(error=transport_error()), Try()]
    )
    waits = Waits()
    items = asyncio.run(collect(RetryProvider(inner, sleep=waits)))
    assert items == [
        Retrying(1, 3, 1.0, "busy"),
        Retrying(2, 3, 2.0, "connection reset"),
        Finish(Stop.End, None, None),
    ]


def test_a_cancel_during_the_wait_propagates_at_once() -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=429)), Try([Part.Text("no")])])

    async def scenario() -> None:
        waiting = asyncio.Event()
        never = asyncio.Event()

        async def sleep(seconds: float) -> None:
            waiting.set()
            await never.wait()

        task = asyncio.ensure_future(collect(RetryProvider(inner, sleep=sleep)))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert inner.calls == 1


def test_the_wrapper_is_the_inner_provider_by_name_check_and_close() -> None:
    inner = ScriptedProvider([])
    wrapped = RetryProvider(inner)
    assert wrapped.name == "scripted"
    assert wrapped.check(MODEL) == ["scripted check"]
    asyncio.run(wrapped.aclose())
    assert inner.closed
