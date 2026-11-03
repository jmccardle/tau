"""`RetryProvider` (D24 section 4): a failed sample tried again inside the sample, while nothing
has been shown, with a `Retrying` delta before each wait that an abort cuts short."""

from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, Gate, Reply
from harness import Harness, scenario

from tau import (
    Body,
    Ending,
    Live,
    Part,
    Provider,
    Retrying,
    RetryProvider,
    TextDelta,
)

DONE = Reply([Part.Text("done")])
OVERLOADED = Fail("HTTP 529: overloaded_error: Overloaded", status=529)


class Waits:
    """The waits a `RetryProvider` asked for, answered at once (or held at `gate`)."""

    def __init__(self, gate: Gate | None = None) -> None:
        self.seconds: list[float] = []
        self.gate = gate

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        if self.gate is not None:
            await self.gate.wait()


def retrying(waits: Waits):
    def wrap(fake: FakeProvider) -> Provider:
        return RetryProvider(fake, sleep=waits)

    return wrap


async def failed_after(tmp_path: Path, script: list[Reply | Fail], waits: Waits) -> Harness:
    gates = [] if waits.gate is None else [waits.gate]
    h = await Harness.create(
        tmp_path, provider=FakeProvider(script), wrap=retrying(waits), gates=gates
    )
    await h.client.watch([h.cursor])
    await h.send("go")
    await h.settle()
    return h


def said(h: Harness) -> list[Retrying]:
    return [e.delta for e in h.heard if isinstance(e, Live) and isinstance(e.delta, Retrying)]


@scenario
async def test_a_busy_server_is_tried_again_and_the_reply_is_whole(tmp_path: Path) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [OVERLOADED, DONE], waits)
    assert len(h.provider.requests) == 2
    assert waits.seconds == [1.0]
    last = h.bodies()[-1]
    assert isinstance(last, Body.Assistant)
    assert last.parts == [Part.Text("done")]
    assert not isinstance((await h.view()).ending, Ending.Failed)
    await h.close()


@scenario
async def test_it_says_why_it_waits_before_each_wait(tmp_path: Path) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [OVERLOADED, Fail("HTTP 503: down", status=503), DONE], waits)
    assert said(h) == [
        Retrying(1, 3, 1.0, "HTTP 529: overloaded_error: Overloaded"),
        Retrying(2, 3, 2.0, "HTTP 503: down"),
    ]
    await h.close()


@scenario
async def test_it_waits_one_two_then_four_seconds_and_gives_up_after_three_retries(
    tmp_path: Path,
) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail("HTTP 503: down", status=503)] * 4, waits)
    assert waits.seconds == [1.0, 2.0, 4.0]
    assert len(h.provider.requests) == 4
    view = await h.view()
    assert isinstance(view.ending, Ending.Failed)
    assert "HTTP 503: down" in view.ending.error
    await h.close()


@scenario
@pytest.mark.parametrize("status", [408, 429, 502, 503, 504, 529])
async def test_these_statuses_are_retried(tmp_path: Path, status: int) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail(f"HTTP {status}", status=status), DONE], waits)
    assert len(h.provider.requests) == 2
    await h.close()


@scenario
@pytest.mark.parametrize("status", [400, 401, 404, 500])
async def test_other_statuses_are_not_retried(tmp_path: Path, status: int) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail(f"HTTP {status}", status=status), DONE], waits)
    assert len(h.provider.requests) == 1, "a plain 500 is llama.cpp's deterministic error"
    assert waits.seconds == []
    assert said(h) == []
    assert isinstance((await h.view()).ending, Ending.Failed)
    await h.close()


@scenario
async def test_a_failure_with_no_status_is_not_retried(tmp_path: Path) -> None:
    h = await failed_after(tmp_path, [Fail("a conversion error"), DONE], Waits())
    assert len(h.provider.requests) == 1
    await h.close()


@scenario
async def test_a_500_that_carries_retry_after_is_retried_after_that_wait(tmp_path: Path) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail("HTTP 500", status=500, retry_after=7), DONE], waits)
    assert waits.seconds == [7.0]
    assert said(h) == [Retrying(1, 3, 7.0, "HTTP 500")]
    await h.close()


@scenario
async def test_retry_after_replaces_the_backoff(tmp_path: Path) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail("HTTP 429", status=429, retry_after=3), DONE], waits)
    assert waits.seconds == [3.0]
    await h.close()


@scenario
async def test_a_retry_after_over_sixty_seconds_fails_at_once_with_the_wait_in_the_message(
    tmp_path: Path,
) -> None:
    waits = Waits()
    h = await failed_after(tmp_path, [Fail("HTTP 429", status=429, retry_after=61), DONE], waits)
    assert len(h.provider.requests) == 1
    assert waits.seconds == []
    view = await h.view()
    assert isinstance(view.ending, Ending.Failed)
    assert "61 s" in view.ending.error
    await h.close()


@scenario
async def test_nothing_is_retried_once_something_was_yielded(tmp_path: Path) -> None:
    waits = Waits()
    partial = Fail("HTTP 503: cut off", [TextDelta("par"), Part.Text("par")], status=503)
    h = await failed_after(tmp_path, [partial, DONE], waits)
    assert len(h.provider.requests) == 1, "a second try would show `par` twice"
    assert waits.seconds == []
    await h.close()


@scenario
async def test_an_abort_during_the_wait_ends_the_sample_and_asks_for_no_more(
    tmp_path: Path,
) -> None:
    gate = Gate()
    waits = Waits(gate)
    h = await failed_after(tmp_path, [OVERLOADED, DONE], waits)
    assert waits.seconds == [1.0]
    assert gate.held == 1, "the sample is waiting"
    assert (await h.view()).busy
    await h.abort()
    await h.settle()
    view = await h.view()
    assert not view.busy
    assert isinstance(view.ending, Ending.Aborted)
    assert len(h.provider.requests) == 1
    await h.close()
