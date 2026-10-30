"""examples/retry.py: a wrapper provider that retries a whole sample, never after output."""

import asyncio
import importlib.util
import subprocess
import sys
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from tau_next import (
    Delta,
    Finish,
    ModelConfig,
    Node,
    Part,
    ProviderClosed,
    SampleError,
    Stop,
    TextDelta,
    Tool,
    model_config,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
MODEL = model_config("fake")


def load_example(name: str) -> ModuleType:
    """The example file as a module; the examples import `common` from their own directory."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLES))


retry = load_example("retry")


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
    items = asyncio.run(collect(retry.RetryProvider(inner, sleep=waits)))
    assert items == [Part.Text("hi"), Finish(Stop.End, None, None)]
    assert inner.calls == 2
    assert waits.seconds == [7.0]


def test_a_transport_failure_is_retried_with_exponential_backoff_until_attempts_run_out() -> None:
    inner = ScriptedProvider([Try(error=transport_error()) for _ in range(3)])
    waits = Waits()
    with pytest.raises(SampleError, match="connection reset"):
        asyncio.run(collect(retry.RetryProvider(inner, attempts=3, sleep=waits)))
    assert inner.calls == 3
    assert waits.seconds == [1.0, 2.0]


@pytest.mark.parametrize("first", [Part.Text("partial"), TextDelta("par")])
def test_a_failure_after_a_part_or_delta_was_yielded_is_not_retried(first: Part | Delta) -> None:
    inner = ScriptedProvider(
        [Try([first], SampleError("busy", status=503)), Try([Part.Text("again")])]
    )
    waits = Waits()
    seen: list[Part | Finish | Delta] = []

    async def run() -> None:
        wrapped = retry.RetryProvider(inner, sleep=waits)
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
        asyncio.run(collect(retry.RetryProvider(inner, sleep=waits)))
    assert raised.value is error
    assert inner.calls == 1
    assert waits.seconds == []


@pytest.mark.parametrize("status", [408, 429, 502, 503, 529])
def test_each_transient_status_is_retried(status: int) -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=status)), Try()])
    asyncio.run(collect(retry.RetryProvider(inner, sleep=Waits())))
    assert inner.calls == 2


def test_a_500_that_carries_retry_after_is_retried() -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=500, retry_after=2.0)), Try()])
    asyncio.run(collect(retry.RetryProvider(inner, sleep=Waits())))
    assert inner.calls == 2


def test_every_wait_is_clamped_to_max_wait() -> None:
    inner = ScriptedProvider(
        [
            Try(error=SampleError("x", status=429, retry_after=3600.0)),
            Try(error=transport_error()),
            Try(error=transport_error()),
            Try(error=transport_error()),
            Try(),
        ]
    )
    waits = Waits()
    asyncio.run(collect(retry.RetryProvider(inner, attempts=5, max_wait=1.5, sleep=waits)))
    assert waits.seconds == [1.5, 1.5, 1.5, 1.5]


def test_a_cancel_during_the_wait_propagates_at_once() -> None:
    inner = ScriptedProvider([Try(error=SampleError("x", status=429)), Try([Part.Text("no")])])

    async def scenario() -> None:
        waiting = asyncio.Event()
        never = asyncio.Event()

        async def sleep(seconds: float) -> None:
            waiting.set()
            await never.wait()

        task = asyncio.ensure_future(collect(retry.RetryProvider(inner, sleep=sleep)))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert inner.calls == 1


def test_the_wrapper_is_the_inner_provider_by_name_check_and_close() -> None:
    inner = ScriptedProvider([])
    wrapped = retry.RetryProvider(inner)
    assert wrapped.name == "scripted"
    assert wrapped.check(MODEL) == ["scripted check"]
    asyncio.run(wrapped.aclose())
    assert inner.closed


def test_the_example_runs_against_the_mock_provider(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text((EXAMPLES / "config.json").read_text(encoding="utf-8"))
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / "retry.py")],
        env={"TAU_HOME": str(home), "HOME": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("reply ")
