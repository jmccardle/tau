"""``ext_kit.stream`` — the *event-stream* atom.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S54.

Watching a child's live E-json stream is the second agentic primitive: an
orchestrator that *spawns* work (S53 :func:`ext_kit.spawn.stream_tau`) needs to
*supervise* it — count what it did, notice when it wedges in a tool-call loop,
and notice when it goes silent. This module is that supervisor, composed purely
on the S53 substrate:

* :func:`iter_jsonl` / :func:`read_jsonl` — decode a child's ``--mode json``
  output (a persisted ``.jsonl`` session, or captured stdout) into E-json events.
* :class:`StreamCounters` — turn / tool-call tallies folded off the event stream.
* :class:`StuckDetector` — ``N`` identical *consecutive* tool calls → flag (the
  live form of ``spawn.LimitEnforcer``'s per-message stuck check, but at
  tool-call granularity, off ``tool_execution_start``).
* :class:`ProgressWatchdog` — no event for ``T`` seconds → flag (a pure,
  clock-injectable liveness timer).
* :class:`StreamMonitor` — bundles the three; :meth:`StreamMonitor.observe` folds
  one event and returns an imposed flag (``"stuck"``) or ``None``.
* :func:`monitor_stream` — the batteries-included async driver: wrap
  :func:`ext_kit.spawn.stream_tau`, drive a :class:`StreamMonitor` over it,
  enforce the :class:`ProgressWatchdog` deadline with ``asyncio.wait_for``, yield
  each event through, and stop (→ *kill*, since closing ``stream_tau`` reaps the
  child) the moment a flag trips.

**Fail-Early.** :func:`iter_jsonl` is strict: a non-blank line that is not valid
JSON raises rather than being silently dropped — a corrupt child stream is a real
error a supervisor must surface, not paper over. (``stream_tau`` tolerates stray
lines because it is the raw pipe reader; a reader handed a *known* JSONL artifact
does not.)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

TauEvent = dict[str, Any]

#: Default stuck window: this many identical consecutive tool calls → flagged.
#: Kept equal to ``spawn.DEFAULT_STUCK_LIMIT`` so the live watcher and the
#: batch enforcer agree on what "stuck" means.
DEFAULT_STUCK_LIMIT = 3

#: The flags this module can impose on a stream.
StreamFlag = str
FLAGS: frozenset[str] = frozenset({"stuck", "stalled"})


# ── JSONL reader ─────────────────────────────────────────────────────────────


def iter_jsonl(lines: Iterable[str]) -> Iterator[TauEvent]:
    """Decode an iterable of JSONL text lines into E-json events.

    Blank / whitespace-only lines are skipped (JSONL permits trailing newlines);
    a non-blank line that fails to parse raises ``json.JSONDecodeError`` — the
    reader refuses to silently drop a corrupt record (Fail-Early). Each yielded
    value is one decoded event ``dict`` (a session header or an
    ``AgentSessionEvent``).
    """
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        yield json.loads(line)


def read_jsonl(text: str) -> list[TauEvent]:
    """Eagerly decode a whole ``--mode json`` blob (e.g. a captured child stdout
    or a persisted ``.jsonl`` session) into a list of events. Thin wrapper over
    :func:`iter_jsonl`.
    """
    return list(iter_jsonl(text.splitlines()))


# ── tool-call signature (stream-native) ──────────────────────────────────────


def event_tool_signature(event: Mapping[str, Any]) -> str | None:
    """Signature of a single ``tool_execution_start`` event (name + args), or
    ``None`` for any other event.

    The stream-side counterpart to ``spawn.tool_call_signature`` (which reads an
    assistant ``message_end``'s content): here the unit is one *individual* tool
    call, taken off the ``tool_execution_start`` the agent loop emits before each
    invocation — which is exactly "one tool call" for the "N identical
    consecutive tool calls" stuck rule. ``args`` is canonicalised
    (``sort_keys``) so key order never masks a repeat.
    """
    if event.get("type") != "tool_execution_start":
        return None
    return json.dumps(
        [event.get("tool_name"), json.dumps(event.get("args"), sort_keys=True)],
        sort_keys=True,
    )


# ── counters ─────────────────────────────────────────────────────────────────


@dataclass
class StreamCounters:
    """Running tallies folded off a child's event stream.

    ``turns`` counts ``turn_start`` events (turns begun); ``tool_calls`` counts
    ``tool_execution_start`` events; ``by_tool`` breaks those down by tool name;
    ``assistant_messages`` counts assistant ``message_end`` events. Purely
    additive — safe to observe the same live stream a :class:`StuckDetector` and
    :class:`ProgressWatchdog` also watch.
    """

    turns: int = 0
    tool_calls: int = 0
    assistant_messages: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)

    def observe(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "turn_start":
            self.turns += 1
        elif kind == "tool_execution_start":
            self.tool_calls += 1
            name = event.get("tool_name") or "?"
            self.by_tool[name] = self.by_tool.get(name, 0) + 1
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                self.assistant_messages += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "assistant_messages": self.assistant_messages,
            "by_tool": dict(self.by_tool),
        }


# ── stuck detector ───────────────────────────────────────────────────────────


class StuckDetector:
    """Flags a run of ``limit`` identical *consecutive* tool calls.

    Feed it every stream event via :meth:`observe`. Only ``tool_execution_start``
    events move the state machine: an identical signature (same tool name + args
    as the previous tool call) extends the run; a different signature resets it.
    Non-tool events (text turns, boundaries) are ignored — they neither extend nor
    reset a run, so an assistant that alternates *no* tool call between identical
    calls is still caught. Once ``limit`` identical calls have been seen the
    detector latches :attr:`flagged` ``True`` and :meth:`observe` returns ``True``
    from then on (the caller kills the child on the first ``True``).
    """

    def __init__(self, limit: int = DEFAULT_STUCK_LIMIT) -> None:
        if limit < 1:
            raise ValueError(f"StuckDetector limit must be >= 1, got {limit}")
        self._limit = limit
        self._prev_sig: str | None = None
        self._run = 0
        self.flagged = False

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def run_length(self) -> int:
        """How many identical consecutive tool calls have been seen so far."""
        return self._run

    def observe(self, event: Mapping[str, Any]) -> bool:
        """Fold one event; return ``True`` iff the stuck threshold is (now) met."""
        if self.flagged:
            return True
        sig = event_tool_signature(event)
        if sig is None:
            return False
        if sig == self._prev_sig:
            self._run += 1
        else:
            self._prev_sig = sig
            self._run = 1
        if self._run >= self._limit:
            self.flagged = True
        return self.flagged


# ── progress watchdog ────────────────────────────────────────────────────────


class ProgressWatchdog:
    """Flags when no event has arrived for ``timeout`` seconds (a liveness timer).

    Pure and clock-injectable: :meth:`record` stamps "an event just arrived" from
    ``time_fn`` (default ``time.monotonic``), :meth:`remaining` reports seconds
    left before the deadline, and :meth:`stalled` latches :attr:`flagged` when the
    deadline has passed. :func:`monitor_stream` uses :meth:`remaining` as the
    ``asyncio.wait_for`` timeout so the real gap detection and this bookkeeping
    stay one source of truth; tests drive it directly with a fake ``time_fn``.
    """

    def __init__(self, timeout: float, *, time_fn: Callable[[], float] | None = None) -> None:
        if timeout <= 0:
            raise ValueError(f"ProgressWatchdog timeout must be > 0, got {timeout}")
        self._timeout = timeout
        self._time = time_fn or time.monotonic
        self._last = self._time()
        self.flagged = False

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def last(self) -> float:
        return self._last

    def record(self, *, now: float | None = None) -> None:
        """Reset the gap timer — an event just arrived."""
        self._last = self._time() if now is None else now

    def remaining(self, *, now: float | None = None) -> float:
        """Seconds left before the no-event deadline (may be negative once past)."""
        current = self._time() if now is None else now
        return self._timeout - (current - self._last)

    def stalled(self, *, now: float | None = None) -> bool:
        """Return ``True`` (and latch :attr:`flagged`) iff the deadline has passed."""
        if self.remaining(now=now) <= 0:
            self.flagged = True
        return self.flagged


# ── the composed monitor ─────────────────────────────────────────────────────


class StreamMonitor:
    """Bundle a :class:`StreamCounters`, :class:`StuckDetector`, and (optional)
    :class:`ProgressWatchdog` and fold a whole event stream through them.

    :meth:`observe` updates all three for one event and returns an imposed flag —
    only ``"stuck"`` is decidable per-event; ``"stalled"`` is a *gap* between
    events, which :func:`monitor_stream` detects with a real ``asyncio.wait_for``
    deadline (there is no event on which to decide it). The watchdog is still fed
    here (each observed event resets its timer) so its bookkeeping is correct.
    """

    def __init__(
        self,
        *,
        stuck_limit: int = DEFAULT_STUCK_LIMIT,
        progress_timeout: float | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.counters = StreamCounters()
        self.stuck = StuckDetector(stuck_limit)
        self.watchdog: ProgressWatchdog | None = (
            None
            if progress_timeout is None
            else ProgressWatchdog(progress_timeout, time_fn=time_fn)
        )

    def observe(self, event: Mapping[str, Any]) -> StreamFlag | None:
        """Fold one event; return ``"stuck"`` when it trips, else ``None``."""
        if self.watchdog is not None:
            self.watchdog.record()
        self.counters.observe(event)
        if self.stuck.observe(event):
            return "stuck"
        return None


async def monitor_stream(
    events: AsyncIterator[TauEvent],
    *,
    monitor: StreamMonitor | None = None,
    stuck_limit: int = DEFAULT_STUCK_LIMIT,
    progress_timeout: float | None = None,
    on_flag: Callable[[StreamFlag], None] | None = None,
    time_fn: Callable[[], float] | None = None,
) -> AsyncIterator[TauEvent]:
    """Drive a :class:`StreamMonitor` over a live child event iterator.

    Wrap an :func:`ext_kit.spawn.stream_tau` iterator (or any async event
    iterator) and yield each event straight through while folding it into
    ``monitor`` (a fresh one is built from ``stuck_limit`` / ``progress_timeout``
    if not supplied). The moment a flag trips this generator **stops**:

    * ``"stuck"`` — after yielding the offending event (so the consumer sees it).
    * ``"stalled"`` — the :class:`ProgressWatchdog` deadline is enforced as an
      ``asyncio.wait_for`` timeout on the next event; on expiry no event is
      yielded, the flag fires, and iteration ends.

    Stopping closes the wrapped iterator (``aclose``), which — for
    :func:`stream_tau` — terminates and reaps the child. That is the *kill* half
    of "flag/kill": a supervisor consuming this generator just ``break``\\ s or
    lets it end, and the child dies. ``on_flag`` (if given) is called once with
    the flag name as it fires. Access post-run tallies via the passed ``monitor``.
    """
    mon = monitor or StreamMonitor(
        stuck_limit=stuck_limit, progress_timeout=progress_timeout, time_fn=time_fn
    )
    if mon.watchdog is not None:
        # Start the gap clock at "now" so a monitor built earlier than the stream
        # does not spend part of its first-event budget before the stream opens.
        mon.watchdog.record()

    iterator = events.__aiter__()
    try:
        while True:
            if mon.watchdog is not None:
                remaining = mon.watchdog.remaining()
                if remaining <= 0:
                    mon.watchdog.stalled()
                    if on_flag is not None:
                        on_flag("stalled")
                    return
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                except asyncio.TimeoutError:
                    mon.watchdog.stalled()
                    if on_flag is not None:
                        on_flag("stalled")
                    return
                except StopAsyncIteration:
                    return
            else:
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    return

            flag = mon.observe(event)
            yield event
            if flag is not None:
                if on_flag is not None:
                    on_flag(flag)
                return
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
