"""``ext_kit.spawn`` — the *isolated-agent* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S53.

The distilled, hardened core of ``examples/20_delegate.py``: spawn a *separate*
``tau -p --mode json`` process so a child gets its own, isolated context window,
and read its structured output back over pi-faithful ``--mode json`` (step S8 /
E-json), whose per-message ``message_end`` carries the ``usage`` / ``model`` /
``stop_reason`` this module rolls up.

Every child is spawned exactly as pi spawns its subagents (``subagent/index.ts``
``runSingleAgent``)::

    tau -p --mode json --no-session --no-extensions \\
        [--model M] [--tools read,ls,...] \\
        [--append-system-prompt <tmpfile>] "<prompt>"

``--no-extensions`` on the child is what makes recursion safe: a child never
re-loads the parent's extensions and so cannot fork-bomb.

This module composes only τ's **public** surface (the ``tau`` CLI + the
``tau_coding_agent.cli`` entry) — it is part of the *extension-side* kit
(``examples/ext_kit/``), **not** the harness. It provides four things the demo
extensions in E9–E11 build on:

* :func:`spawn_tau` — the workhorse: run one child to completion, enforce live
  limits (turns / wall-clock / budget / stuck-loop), and return a
  :class:`ChildResult` with a rolled-up :class:`ChildUsage`.
* :func:`stream_tau` — a live async iterator over a child's parsed JSON events
  (the substrate S54's ``StuckDetector`` / ``ProgressWatchdog`` watch).
* usage / cost roll-up (:func:`roll_message_usage`, :func:`price_increment`).
* :class:`WorkerPool` — bounded concurrency (pi ``mapWithConcurrencyLimit`` as a
  reusable class), plus :func:`spawn_all` to fan :func:`spawn_tau` out under it.

**Fail-Early.** ``cost`` is ``None`` (unknown) when no price is supplied — an
unpriced run reports *unknown*, never a fabricated ``$0``. A budget
(:attr:`SpawnLimits.max_usd`) is enforceable only when a price is known; the
caller resolves the price and this module refuses to silently not-enforce.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

TauEvent = dict[str, Any]

#: Default stuck window: this many consecutive assistant turns repeating the
#: identical tool-call signature counts as a loop.
DEFAULT_STUCK_LIMIT = 3

#: Grace period a terminated child gets before it is SIGKILLed.
_KILL_GRACE_SECONDS = 5.0

#: stop_reason values this module *imposes* when it trips a limit (as opposed to
#: the child-reported ones like ``"stop"`` / ``"end"`` that ride ``message_end``).
_STOP_REASONS: frozenset[str] = frozenset(
    {"aborted", "timeout", "over_budget", "stuck", "max_turns", "error"}
)

#: Reasons that make a child a *failure* (pi ``isFailedResult`` + the limit trips).
FAILED_REASONS: frozenset[str] = _STOP_REASONS


# ── data carriers ────────────────────────────────────────────────────────────


@dataclass
class ChildUsage:
    """Rolled-up usage for one child (pi ``UsageStats``).

    ``cost`` is ``None`` when no price was supplied — Fail-Early, an unpriced run
    reports *unknown*, not a fabricated ``$0``. It is a running float only once a
    price is known (initialised to ``0.0`` by :func:`spawn_tau` when a ``cost``
    mapping is passed).
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    turns: int = 0
    cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "context_tokens": self.context_tokens,
            "turns": self.turns,
            "cost": self.cost,
        }


@dataclass
class SpawnLimits:
    """Per-child budget / liveness limits enforced live off the E-json stream."""

    max_usd: float | None = None
    max_seconds: float | None = None
    max_turns: int | None = None
    stuck_limit: int = DEFAULT_STUCK_LIMIT


@dataclass
class ChildResult:
    """Result of one spawned child (pi ``SingleResult``)."""

    prompt: str
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    exit_code: int = 0
    final_output: str = ""
    stderr: str = ""
    stop_reason: str | None = None
    error_message: str | None = None
    usage: ChildUsage = field(default_factory=ChildUsage)

    @property
    def failed(self) -> bool:
        """A child failed if it exited non-zero or ended on a failure stop_reason."""
        return self.exit_code != 0 or (self.stop_reason in FAILED_REASONS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "model": self.model,
            "tools": list(self.tools),
            "exit_code": self.exit_code,
            "final_output": self.final_output,
            "stderr": self.stderr,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage.as_dict(),
        }


# ── cost roll-up ─────────────────────────────────────────────────────────────


def price_increment(
    cost: Mapping[str, Any] | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """Dollar cost of one completion, or ``None`` when unpriced.

    ``cost`` is a per-model ``{input, output, cache_read, cache_write}`` price in
    USD per 1M tokens (the shape of a ``~/.tau/config.json`` model ``cost`` block;
    same formula as ``tau_coding_agent.backends.compute_cost_usd``). Resolving that
    price is the caller's job (formalised by S57 ``ext_kit.ledger``); this module
    only folds a known price into the running total.
    """
    if cost is None:
        return None
    return (
        float(cost.get("input", 0.0)) / 1_000_000 * input_tokens
        + float(cost.get("output", 0.0)) / 1_000_000 * output_tokens
        + float(cost.get("cache_read", 0.0)) / 1_000_000 * cache_read_tokens
    )


def roll_message_usage(
    usage: ChildUsage, message: Mapping[str, Any], cost: Mapping[str, Any] | None
) -> None:
    """Fold one assistant ``message_end``'s ``usage`` into ``usage`` in place.

    Increments the turn counter, accumulates the token counts, sets the running
    context size, and — when ``cost`` is known and ``usage.cost`` has been seeded
    to a float — adds this completion's dollar increment.
    """
    usage.turns += 1
    reported = message.get("usage") or {}
    usage.input += int(reported.get("input_tokens") or 0)
    usage.output += int(reported.get("output_tokens") or 0)
    usage.cache_read += int(reported.get("cache_read_tokens") or 0)
    usage.cache_write += int(reported.get("cache_write_tokens") or 0)
    usage.context_tokens = int(reported.get("total_tokens") or 0)
    inc = price_increment(
        cost,
        input_tokens=int(reported.get("input_tokens") or 0),
        output_tokens=int(reported.get("output_tokens") or 0),
        cache_read_tokens=int(reported.get("cache_read_tokens") or 0),
    )
    if inc is not None and usage.cost is not None:
        usage.cost += inc


# ── message helpers (stuck-detection + final output) ─────────────────────────


def tool_call_signature(message: Mapping[str, Any]) -> str | None:
    """Signature of an assistant message's tool calls (name + args), or ``None``
    when it makes no tool call. Used for stuck-detection.
    """
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            parts.append((block.get("name"), json.dumps(block.get("arguments"), sort_keys=True)))
    return json.dumps(parts, sort_keys=True) if parts else None


def message_text(message: Mapping[str, Any]) -> str:
    """Concatenate an assistant message's text blocks (pi ``getFinalOutput``)."""
    out = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "".join(out)


# ── live limit enforcement ───────────────────────────────────────────────────


class LimitEnforcer:
    """Per-child liveness/budget tracker, driven off each assistant ``message_end``.

    Holds the stuck-detection state (a run of consecutive identical tool-call
    signatures) and, reading the running :class:`ChildUsage`, decides after each
    message whether a :class:`SpawnLimits` bound has tripped. :meth:`observe`
    returns the imposed ``stop_reason`` (``"stuck"`` / ``"max_turns"`` /
    ``"over_budget"``) or ``None`` to keep going. Extracted from ``spawn_tau``'s
    consume loop so the trip logic is unit-testable without a subprocess.

    Caller contract: fold the message into ``usage`` (via :func:`roll_message_usage`)
    *before* calling :meth:`observe`, so the ``max_turns`` / ``max_usd`` checks see
    this message's contribution.
    """

    def __init__(self, limits: SpawnLimits, usage: ChildUsage) -> None:
        self._limits = limits
        self._usage = usage
        self._prev_sig: str | None = None
        self._repeat = 0

    def observe(self, message: Mapping[str, Any]) -> str | None:
        sig = tool_call_signature(message)
        if sig is not None and sig == self._prev_sig:
            self._repeat += 1
            if self._repeat >= self._limits.stuck_limit:
                return "stuck"
        else:
            self._repeat = 0
        self._prev_sig = sig

        if self._limits.max_turns is not None and self._usage.turns >= self._limits.max_turns:
            return "max_turns"
        if (
            self._limits.max_usd is not None
            and self._usage.cost is not None
            and self._usage.cost > self._limits.max_usd
        ):
            return "over_budget"
        return None


# ── child argv construction ──────────────────────────────────────────────────


def build_child_args(
    *,
    prompt: str,
    model: str | None,
    tools: Sequence[str] | None,
    system_prompt_path: str | None,
) -> list[str]:
    """Build the child ``tau`` CLI arguments (pi ``runSingleAgent``).

    Always headless JSON, ephemeral, extension-free. ``--model`` / ``--tools`` /
    ``--append-system-prompt`` are added only when supplied. Unlike
    ``20_delegate`` (which prefixes ``Task:``), the kit passes ``prompt`` verbatim
    as the single positional message — the caller owns the wording.
    """
    args = ["-p", "--mode", "json", "--no-session", "--no-extensions"]
    if model:
        args += ["--model", model]
    if tools:
        args += ["--tools", ",".join(tools)]
    if system_prompt_path:
        args += ["--append-system-prompt", system_prompt_path]
    args.append(prompt)
    return args


def tau_invocation(
    cli_args: Sequence[str], python_executable: str | None = None
) -> tuple[str, list[str]]:
    """Return ``(command, argv)`` that re-runs τ headlessly.

    τ analog of pi's ``getPiInvocation``: re-run a Python interpreter against the
    CLI module (``python -m tau_coding_agent.cli``) rather than depending on a
    ``tau`` console script being on ``PATH`` — robust in a venv and in tests.
    ``python_executable`` defaults to the current interpreter.
    """
    return python_executable or sys.executable, ["-m", "tau_coding_agent.cli", *cli_args]


# ── process lifecycle ────────────────────────────────────────────────────────


def _write_system_prompt(system_prompt: str | None) -> str | None:
    """Write ``system_prompt`` to a temp file for ``--append-system-prompt``, or
    return ``None`` when there is nothing to append. Caller owns the unlink.
    """
    if not (system_prompt and system_prompt.strip()):
        return None
    fd, path = tempfile.mkstemp(prefix="tau-ext-kit-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(system_prompt)
    return path


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Terminate a child, escalating to SIGKILL if it lingers. No-op if reaped."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _create_child(
    cli_args: Sequence[str], *, cwd: str, python_executable: str | None
) -> asyncio.subprocess.Process:
    command, argv = tau_invocation(cli_args, python_executable)
    return await asyncio.create_subprocess_exec(
        command,
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# ── the streamed event iterator ──────────────────────────────────────────────


async def stream_tau(
    prompt: str,
    *,
    model: str | None = None,
    tools: Sequence[str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    system_prompt: str | None = None,
    signal: Any | None = None,
    python_executable: str | None = None,
) -> AsyncIterator[TauEvent]:
    """Spawn a ``tau -p --mode json`` child and yield its parsed JSON events live.

    Each yielded value is one decoded E-json record (session header, then the
    ``AgentSessionEvent`` stream: ``agent_start`` … ``message_end`` …
    ``agent_end``). This is the substrate S54's ``StuckDetector`` /
    ``ProgressWatchdog`` watch; :func:`spawn_tau` is the batteries-included
    consumer that rolls usage up and enforces limits.

    Abort propagation: ``signal`` is anything exposing ``is_aborted() -> bool``
    (e.g. ``tau_ai.AbortSignal`` from ``ctx.signal``). It is polled before each
    event; once aborted the iterator stops and the child is killed. ``timeout``
    is an overall wall-clock deadline over the whole stream. The child is always
    terminated when the iterator is closed (exhaustion, ``break``, exception,
    abort, or timeout).

    stderr is not captured here — a live watcher only cares about events, and an
    unread stderr pipe could deadlock the child. Use :func:`spawn_tau` when you
    need ``exit_code`` / ``stderr``.

    Returns: an ``AsyncIterator[TauEvent]``.
    """
    tmp_prompt = _write_system_prompt(system_prompt)
    proc: asyncio.subprocess.Process | None = None
    try:
        cli_args = build_child_args(
            prompt=prompt,
            model=model,
            tools=list(tools) if tools else None,
            system_prompt_path=tmp_prompt,
        )
        command, argv = tau_invocation(cli_args, python_executable)
        proc = await asyncio.create_subprocess_exec(
            command,
            *argv,
            cwd=cwd or ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if signal is not None and signal.is_aborted():
                return
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    return
            else:
                raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event
    finally:
        if proc is not None:
            await _kill(proc)
        if tmp_prompt:
            try:
                os.unlink(tmp_prompt)
            except OSError:
                pass


# ── the workhorse ────────────────────────────────────────────────────────────


async def spawn_tau(
    prompt: str,
    *,
    model: str | None = None,
    tools: Sequence[str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    system_prompt: str | None = None,
    limits: SpawnLimits | None = None,
    cost: Mapping[str, Any] | None = None,
    signal: Any | None = None,
    python_executable: str | None = None,
) -> ChildResult:
    """Run one ``tau -p --mode json`` child to completion and return a result.

    Streams the child's E-json events, rolls up usage (and cost when ``cost`` is
    supplied), and enforces the :class:`SpawnLimits` live: ``max_turns``,
    ``max_seconds`` (wall clock), ``max_usd`` (priced from per-message tokens ×
    ``cost``), and stuck-detection (``stuck_limit`` consecutive identical
    tool-call signatures). When a limit trips the child is killed and
    ``stop_reason`` is set from the imposed taxonomy.

    ``timeout`` is a convenience alias for ``limits.max_seconds``; passing both is
    a ValueError (Fail-Early — no silent precedence). Abort propagation: ``signal``
    (``ctx.signal``) is polled per event; an abort kills the child and sets
    ``stop_reason="aborted"``. The child is always reaped, even on exception.
    """
    if limits is None:
        limits = SpawnLimits(max_seconds=timeout)
    elif timeout is not None:
        raise ValueError(
            "spawn_tau: pass a wall-clock cap via either `timeout=` or "
            "`limits.max_seconds`, not both"
        )

    if limits.max_usd is not None and cost is None:
        # Fail-Early: a budget is only enforceable against a known price. Refuse to
        # accept a max_usd we would silently never check (the docstring's contract).
        raise ValueError(
            "spawn_tau: a budget (limits.max_usd) is enforceable only with a known "
            "price — pass `cost=` (per-model USD/1M-token map) or drop max_usd; "
            "refusing to silently not-enforce"
        )

    tools_list = list(tools) if tools else None
    result = ChildResult(prompt=prompt, model=model, tools=list(tools_list or []))
    if cost is not None:
        # Seed the running total so roll-up accumulates a float (unpriced stays None).
        result.usage.cost = 0.0

    tmp_prompt = _write_system_prompt(system_prompt)
    try:
        cli_args = build_child_args(
            prompt=prompt, model=model, tools=tools_list, system_prompt_path=tmp_prompt
        )
        proc = await _create_child(cli_args, cwd=cwd or ".", python_executable=python_executable)
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task: asyncio.Task[str] | None = None
        try:
            stderr_pipe = proc.stderr

            async def _drain() -> str:
                data = await stderr_pipe.read()
                return data.decode(errors="replace")

            stderr_task = asyncio.create_task(_drain())

            enforcer = LimitEnforcer(limits, result.usage)

            async def _consume() -> str | None:
                async for raw in proc.stdout:  # type: ignore[union-attr]
                    if signal is not None and signal.is_aborted():
                        return "aborted"
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "message_end":
                        continue
                    message = event.get("message") or {}
                    if message.get("role") != "assistant":
                        continue

                    roll_message_usage(result.usage, message, cost)
                    if message.get("model") and result.model is None:
                        result.model = message["model"]
                    if message.get("stop_reason"):
                        result.stop_reason = message["stop_reason"]
                    text = message_text(message)
                    if text:
                        result.final_output = text

                    override = enforcer.observe(message)
                    if override is not None:
                        return override
                return None

            try:
                override = await asyncio.wait_for(_consume(), timeout=limits.max_seconds)
            except asyncio.TimeoutError:
                override = "timeout"

            if override is not None:
                await _kill(proc)
            exit_code = await proc.wait()
            result.stderr = await stderr_task
            result.exit_code = exit_code

            if override is not None:
                result.stop_reason = override
                if result.error_message is None:
                    result.error_message = f"child {override}"
            elif exit_code != 0 and result.stop_reason not in FAILED_REASONS:
                result.stop_reason = "error"
                if not result.error_message:
                    result.error_message = result.stderr.strip() or f"child exited {exit_code}"
            return result
        finally:
            # Safety net: guarantee the child is reaped and stderr drained even on
            # an exception / cancellation between spawn and normal completion.
            await _kill(proc)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
    finally:
        if tmp_prompt:
            try:
                os.unlink(tmp_prompt)
            except OSError:
                pass


# ── bounded concurrency ──────────────────────────────────────────────────────

_T = TypeVar("_T")
_R = TypeVar("_R")


class WorkerPool:
    """Run async work over a sequence with a bounded number of workers.

    The reusable form of pi's ``mapWithConcurrencyLimit`` (``subagent`` parallel
    mode): at most ``concurrency`` coroutines run at once, and results are
    returned in input order regardless of completion order.
    """

    def __init__(self, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError(f"WorkerPool concurrency must be >= 1, got {concurrency}")
        self._concurrency = concurrency

    @property
    def concurrency(self) -> int:
        return self._concurrency

    async def map(self, fn: Callable[[_T, int], Awaitable[_R]], items: Sequence[_T]) -> list[_R]:
        """Run ``fn(item, index)`` over ``items``, ≤ ``concurrency`` at a time.

        Results are ordered to match ``items``. ``fn`` receives the item and its
        original index. Exceptions propagate (the first to raise aborts the batch).
        """
        work = list(items)
        if not work:
            return []
        limit = min(self._concurrency, len(work))
        results: list[_R | None] = [None] * len(work)
        nxt = 0
        lock = asyncio.Lock()

        async def _worker() -> None:
            nonlocal nxt
            while True:
                async with lock:
                    cur = nxt
                    nxt += 1
                if cur >= len(work):
                    return
                results[cur] = await fn(work[cur], cur)

        await asyncio.gather(*[_worker() for _ in range(limit)])
        return cast("list[_R]", results)


async def spawn_all(
    prompts: Sequence[str],
    *,
    concurrency: int,
    model: str | None = None,
    tools: Sequence[str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    system_prompt: str | None = None,
    limits: SpawnLimits | None = None,
    cost: Mapping[str, Any] | None = None,
    signal: Any | None = None,
    python_executable: str | None = None,
) -> list[ChildResult]:
    """Fan :func:`spawn_tau` out over ``prompts`` under a :class:`WorkerPool`.

    Every child shares the same ``model`` / ``tools`` / limits / ``signal``;
    results come back in ``prompts`` order. This is the bounded-parallel shape the
    delegate's ``parallel`` mode uses, minus the read-only tool guard (that policy
    lives in ``20_delegate`` / the demos, not the neutral kit).
    """
    pool = WorkerPool(concurrency)

    async def _run(prompt: str, _index: int) -> ChildResult:
        return await spawn_tau(
            prompt,
            model=model,
            tools=tools,
            cwd=cwd,
            timeout=timeout,
            system_prompt=system_prompt,
            limits=limits,
            cost=cost,
            signal=signal,
            python_executable=python_executable,
        )

    return await pool.map(_run, prompts)
