"""The fakes at the effect boundary (rule 4), and the clock and gates they need."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass

from tau_next import (
    CallDraft,
    Config,
    Cursor,
    Ending,
    Finish,
    Node,
    NodeHandle,
    Outcome,
    Part,
    SampleError,
    Stop,
    Subject,
    Tool,
)


class Gate:
    """A latch a fake waits at. `held` counts the waiters; `release` lets them
    and every later arrival through."""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []
        self._open = False

    @property
    def held(self) -> int:
        return len(self._waiters)

    async def wait(self) -> None:
        if self._open:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def release(self) -> None:
        self._open = True
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)


class FakeClock:
    """Time that moves only when told."""

    def __init__(self, start: int = 1_000) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms


@dataclass(frozen=True)
class Reply:
    """A scripted sample that ends normally. Held at `gate` after `gate_after` parts."""

    parts: Sequence[Part]
    stop: Stop = Stop.End
    gate: Gate | None = None
    gate_after: int = 0
    finish_reason: str | None = None


@dataclass(frozen=True)
class Fail:
    """A scripted sample that raises `SampleError(error)` after `parts`."""

    error: str
    parts: Sequence[Part] = ()


@dataclass(frozen=True)
class Request:
    """What a sample was asked: the walk's node ids, the config (I1), and the tools offered."""

    walk: tuple[int, ...]
    config: Config
    tools: tuple[Tool, ...] = ()

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]


class FakeProvider:
    """Answers each sample with the next scripted reply and records the request."""

    def __init__(self, script: Sequence[Reply | Fail]) -> None:
        self._script = list(script)
        self._next = 0
        self.requests: list[Request] = []

    @property
    def gates(self) -> list[Gate]:
        return [e.gate for e in self._script if isinstance(e, Reply) and e.gate is not None]

    async def sample(
        self, walk: Sequence[Node], config: Config, tools: Sequence[Tool]
    ) -> AsyncIterator[Part | Finish]:
        self.requests.append(Request(tuple(n.id for n in walk), config, tuple(tools)))
        if self._next >= len(self._script):
            raise AssertionError(f"FakeProvider: no scripted reply for sample {self._next + 1}")
        scripted = self._script[self._next]
        self._next += 1
        match scripted:
            case Reply(parts=parts, stop=stop, gate=gate, gate_after=after, finish_reason=reason):
                for i, part in enumerate(parts):
                    if gate is not None and i == after:
                        await gate.wait()
                    yield part
                if gate is not None and after >= len(parts):
                    await gate.wait()
                yield Finish(stop, reason)
            case Fail(error=error, parts=parts):
                for part in parts:
                    yield part
                raise SampleError(error)


class FakeTool:
    """Records each call's parsed arguments, waits at its gate, then answers `Ok(result)`
    or raises `result`. Checks only that the arguments are JSON."""

    def __init__(
        self,
        name: str,
        result: str | Exception | Callable[[Mapping[str, object]], str],
        gate: Gate | None = None,
        description: str = "A fake tool.",
    ) -> None:
        self._name = name
        self._result = result
        self.gate = gate
        self._description = description
        self.calls: list[Mapping[str, object]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"type": "object", "properties": {}}

    async def call(self, args_raw: str) -> Outcome:
        try:
            args: Mapping[str, object] = json.loads(args_raw)
        except ValueError:
            return Outcome.Err("arguments are not valid JSON")
        self.calls.append(args)
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self._result, Exception):
            raise self._result
        if isinstance(self._result, str):
            return Outcome.Ok(self._result)
        return Outcome.Ok(self._result(args))


class Recorder:
    """An extension that records every hook it is run at, and answers from `says`.

    `says` maps a hook point's name to the string it returns there (a halt, or
    for `pre_tool` and `pre_result` an `Err`). `on` maps a point to a function
    that may edit the subject or call draft. Other points return `None`.
    """

    def __init__(
        self,
        name: str = "recorder",
        says: Mapping[str, str] | None = None,
        on: Mapping[str, Callable[[Subject | CallDraft], None]] | None = None,
        gates: Mapping[str, Gate] | None = None,
    ) -> None:
        self._name = name
        self._says = dict(says or {})
        self._on = dict(on or {})
        self.gates = dict(gates or {})
        self.calls: list[tuple[str, Subject | CallDraft]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def points(self) -> list[str]:
        return [point for point, _ in self.calls]

    def subjects(self, point: str) -> list[Subject | CallDraft]:
        return [s for p, s in self.calls if p == point]

    @property
    def endings(self) -> list[Ending | None]:
        """The ending each `post_step` was run with."""
        return [s.ending for s in self.subjects("post_step") if isinstance(s, Subject)]

    async def _run(self, point: str, subject: Subject | CallDraft) -> str | None:
        self.calls.append((point, subject))
        if point in self._on:
            self._on[point](subject)
        gate = self.gates.get(point)
        if gate is not None:
            await gate.wait()
        return self._says.get(point)

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("pre_step", subject)

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("pre_input", subject)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("pre_sample", subject)

    async def pre_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("pre_reply", subject)

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("post_reply", subject)

    async def post_result(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("post_result", subject)

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        return await self._run("post_step", subject)

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        return await self._run("pre_tool", call)

    async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
        return await self._run("pre_result", call)
