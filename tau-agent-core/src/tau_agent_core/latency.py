"""Per-prompt latency collection that refuses to pool a compaction (§16.8, §9).

§5.2's "assembled turn latency, end to end" is the primary output of the sim, and
§9's ``latency.json`` row already carries the exclusion this module implements:
*"an auto-compaction is a full-window model call at end-of-prompt, and a p99 that
is really a compaction is §9 rule 1 with a new surface. Tag and partition it."*

The structural promise here is that **there is no API that returns the pooled
distribution.** :meth:`PromptLatencyCollector.to_latency_json` emits two named
populations and never their union, for the same reason ``Trace`` carries its own
``arm``: a consumer holding one number must not be able to have lost the partition
key on the way.

The marker, and what it is actually worth
-----------------------------------------

§16.8 proposes the bracketing ``agent_start``/``agent_end`` pair with nothing
between as the marker for a compaction-bearing prompt. Measured against the running
harness rather than taken on trust, it is a **sound necessary condition and an
imprecise sufficient one** — which is the right way round for an exclusion, and
worth stating exactly:

*No false negatives.* Both compaction sites bracket unconditionally and the closing
``agent_end`` is in a ``finally`` (``agent_session.py:1411-1415`` for the automatic
path, ``:1279-1283`` for the manual one). No compaction can happen without the
bare pair appearing, including one that raises.

*Four false positives, all confirmed by execution.* The identical pair is emitted
by (a) an auto-compaction whose ``prepare_compaction`` returned ``None``, so no
model call happened at all — reachable, e.g. when ``should_compact`` is still true
straight after a compaction; (b) an auto-compaction that raised
``CompactionError``; (c) the manual ``compact()`` path; and (d) an ordinary
``AgentLoop.run()`` whose ``AbortSignal`` was already aborted before the first
turn, which breaks out above the first ``turn_start`` — a bare pair with no
compaction anywhere near it.

Over-tagging is the safe direction for an exclusion: the cost is a handful of clean
prompts kept out of the pooled population, and the alternative is a full-window
model call inside a p99. So the marker decides the *exclusion* on its own. To keep
the excluded population from being the undecomposable number §9 rule 1 is about, it
is corroborated by a **positive** signal for the sub-partition: the count of
``compaction`` entries appended to the session log, which is written only on the
success path (``agent_session.py:1384-1388``). So a prompt is reported as one of

    ``compaction_committed``      bare pair AND a new compaction entry
    ``bare_bracket_only``         bare pair, nothing committed

and neither is ever pooled with the clean turns.

One further measured fact a consumer must not assume away: a single ``prompt()``
can contain **more than one** bare pair, because ``_end_of_prompt_drain`` runs
``_maybe_auto_compact`` once at the tail and again after every ``followUp``
re-entry (``agent_session.py:1152``, ``:1159``). The counts here are per prompt, not
per prompt-or-one.

Reference: SIM_SPEC_v2.md §16.8 consequence 2, §9 (``latency.json``), §5.2.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tau_agent_core.events import AgentEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tau_agent_core.agent_session import AgentSession


@dataclass(frozen=True)
class PromptLatencySample:
    """One ``prompt()`` call, timed and classified.

    Durations come from ``AgentEvent.timestamp`` (milliseconds, stamped by the
    emitting site) rather than from a clock read in this module, so the sample is
    measured on the same time base as every other agent-tier stage §9 asks for.
    """

    index: int
    started_ms: int
    ended_ms: int
    #: Wall span of the whole ``prompt()``, first event to last.
    total_ms: int
    #: How much of ``total_ms`` sits inside bare brackets.
    bare_bracket_ms: int
    #: Bare ``agent_start``/``agent_end`` pairs seen during this prompt.
    bare_brackets: int
    #: ``compaction`` entries the session log gained during this prompt.
    compactions_committed: int
    #: Every event type seen, in order. §9 rule 2: inspect raw output before
    #: believing an aggregate.
    event_types: tuple[str, ...]

    @property
    def compaction_bearing(self) -> bool:
        """Whether this prompt is excluded from the pooled population."""
        return self.bare_brackets > 0


def _nearest_rank(sorted_values: list[int], quantile: float) -> int:
    """Nearest-rank order statistic; no interpolation, so no invented value.

    Every reported figure is a millisecond span that was actually observed. The
    median is the same estimator at q=0.5 rather than the interpolating one, so the
    four figures in a block are consistent with each other.
    """
    rank = max(1, math.ceil(quantile * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(values: list[int]) -> dict[str, Any]:
    """§9's shape: ``median/p90/p99/max, never a single number``.

    An empty population reports ``{"n": 0}`` and nothing else. A fabricated zero
    median for a population with no members is a value that reads as a measurement.
    """
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": _nearest_rank(ordered, 0.5),
        "p90": _nearest_rank(ordered, 0.90),
        "p99": _nearest_rank(ordered, 0.99),
        "max": ordered[-1],
    }


@dataclass
class _Bracket:
    start_ms: int
    saw_inner: bool = False


@dataclass
class _Window:
    """Accumulator for one in-flight prompt."""

    first_ms: int | None = None
    last_ms: int | None = None
    bare_brackets: int = 0
    bare_bracket_ms: int = 0
    event_types: list[str] = field(default_factory=list)


class PromptLatencyCollector:
    """Times ``prompt()`` calls on one session and partitions the results.

    Usage — the prompt boundary is explicit because it is not recoverable from the
    event stream (a ``followUp`` re-entry produces a second full loop bracket inside
    the same ``prompt()``, so "one bracket per prompt" is false)::

        collector = PromptLatencyCollector(session)
        with collector.prompt():
            await session.prompt("...")
        ...
        artifact = collector.to_latency_json()

    The collector subscribes for its lifetime; call :meth:`close` (or use it as a
    context manager) to unsubscribe.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session
        self._unsubscribe = session.subscribe(self._on_event)
        self._window: _Window | None = None
        self._bracket: _Bracket | None = None
        self._samples: list[PromptLatencySample] = []
        #: Bare brackets observed while no prompt window was open — a manual
        #: ``compact()``, typically. Counted rather than dropped, because a
        #: compaction outside a measured prompt still spent a full window of tokens
        #: and a reader of the artifact should be able to see that it happened.
        self.bare_brackets_outside_prompt = 0
        #: Structural violations of the bracket marker seen on this stream.
        #: Recorded rather than raised at the observation site, because
        #: :class:`~tau_agent_core.events.EventBus` catches a handler exception and
        #: routes it to ``on_error`` — raising there would turn a marker that does
        #: not hold into a stderr line. :meth:`to_latency_json` refuses to produce
        #: an artifact while this list is non-empty, which is the place the failure
        #: has to be loud.
        self._anomalies: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Unsubscribe from the session bus. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None  # type: ignore[assignment]

    def __enter__(self) -> PromptLatencyCollector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── the event side ────────────────────────────────────────────────────

    def _on_event(self, event: AgentEvent) -> None:
        window = self._window
        if window is not None:
            if window.first_ms is None:
                window.first_ms = event.timestamp
            window.last_ms = event.timestamp
            window.event_types.append(event.type)

        if event.type == "agent_start":
            # A nested agent_start would mean a bracket inside a bracket, which the
            # harness does not produce; treating the new one as authoritative would
            # silently lose the outer.
            if self._bracket is not None:
                self._anomalies.append(
                    "nested agent_start on one session bus: the compaction marker assumes "
                    "brackets do not nest, and this stream breaks that assumption"
                )
            self._bracket = _Bracket(start_ms=event.timestamp)
            return

        if event.type == "agent_end":
            bracket = self._bracket
            self._bracket = None
            if bracket is None:
                self._anomalies.append(
                    "agent_end with no open agent_start on this session bus; the bracket "
                    "marker cannot be evaluated over this stream"
                )
                return
            if bracket.saw_inner:
                return
            # A bare bracket: agent_start immediately followed by agent_end. See the
            # module docstring for exactly what this does and does not prove.
            if window is None:
                self.bare_brackets_outside_prompt += 1
            else:
                window.bare_brackets += 1
                window.bare_bracket_ms += event.timestamp - bracket.start_ms
            return

        if self._bracket is not None:
            self._bracket.saw_inner = True

    # ── the prompt side ───────────────────────────────────────────────────

    @contextmanager
    def prompt(self) -> Iterator[None]:
        """Bracket one ``prompt()`` call. Records a sample even if it raises.

        A prompt that raised still consumed time and may still have carried a
        compaction (``_maybe_auto_compact`` emits its ``agent_end`` from a
        ``finally`` and then propagates), so dropping the sample would delete
        exactly the observations the exclusion exists to catch.
        """
        if self._window is not None:
            raise RuntimeError(
                "PromptLatencyCollector.prompt() is already open; prompts on one "
                "session are sequential and overlapping windows would attribute one "
                "prompt's compaction to another"
            )
        before = self._compaction_entry_count()
        self._window = _Window()
        try:
            yield
        finally:
            window = self._window
            self._window = None
            after = self._compaction_entry_count()
            if window is not None and window.first_ms is not None:
                assert window.last_ms is not None
                self._samples.append(
                    PromptLatencySample(
                        index=len(self._samples),
                        started_ms=window.first_ms,
                        ended_ms=window.last_ms,
                        total_ms=window.last_ms - window.first_ms,
                        bare_bracket_ms=window.bare_bracket_ms,
                        bare_brackets=window.bare_brackets,
                        compactions_committed=after - before,
                        event_types=tuple(window.event_types),
                    )
                )

    def _compaction_entry_count(self) -> int:
        """Committed compactions, read off the durable record.

        The positive corroborator for the bracket marker: ``append_compaction`` runs
        only after ``run_compaction`` returns, so this counts summarisations that
        actually completed — never the no-op or the raise.
        """
        return sum(
            1 for entry in self._session.session_log.entries() if entry.get("type") == "compaction"
        )

    # ── the artifact ──────────────────────────────────────────────────────

    @property
    def samples(self) -> list[PromptLatencySample]:
        """Every sample, in order. The raw output §9 rule 2 says to look at first."""
        return list(self._samples)

    @property
    def anomalies(self) -> list[str]:
        """Structural violations of the bracket marker seen on this stream."""
        return list(self._anomalies)

    def to_latency_json(self) -> dict[str, Any]:
        """The ``latency.json`` fragment for §5.2's headline number.

        Two named populations and no union. There is deliberately no ``"all"`` key
        and no method that computes one: a pooled assembled-turn-latency figure over
        compacting and non-compacting prompts is the §9 rule 1 failure this module
        exists to make unavailable.

        Raises:
            RuntimeError: the bracket marker did not hold on this stream. The
                partition would then be computed from a marker known to be wrong,
                which is worse than having no artifact.
        """
        if self._anomalies:
            raise RuntimeError(
                "refusing to emit a latency artifact: the compaction marker did not hold "
                f"on this event stream ({len(self._anomalies)} anomalies) — "
                + "; ".join(self._anomalies)
            )
        clean = [s for s in self._samples if not s.compaction_bearing]
        bearing = [s for s in self._samples if s.compaction_bearing]
        committed = [s for s in bearing if s.compactions_committed > 0]
        uncommitted = [s for s in bearing if s.compactions_committed == 0]
        return {
            "unit": "ms",
            "assembled_turn_latency": {
                # §5.2's headline number. Compaction-bearing prompts are not in here.
                "excluding_compaction_bearing": summarize([s.total_ms for s in clean]),
                # Reported beside it, never merged into it.
                "compaction_bearing": summarize([s.total_ms for s in bearing]),
                "compaction_bearing_committed": summarize([s.total_ms for s in committed]),
                "compaction_bearing_uncommitted": summarize([s.total_ms for s in uncommitted]),
            },
            "counts": {
                "prompts": len(self._samples),
                "excluding_compaction_bearing": len(clean),
                "compaction_bearing": len(bearing),
                "compaction_bearing_committed": len(committed),
                "compaction_bearing_uncommitted": len(uncommitted),
                "bare_brackets_outside_prompt": self.bare_brackets_outside_prompt,
            },
            "compaction_bearing_prompt_indexes": [s.index for s in bearing],
            "marker": {
                "rule": "agent_start immediately followed by agent_end (nothing between)",
                "corroborator": "count of type=='compaction' entries appended to the session log",
                "false_negatives": "none measured; both compaction sites bracket in a finally",
                "false_positives": (
                    "prepare_compaction()->None (no model call); a raised CompactionError; "
                    "the manual compact() path; an AgentLoop.run() whose AbortSignal was "
                    "already aborted before the first turn"
                ),
            },
        }
