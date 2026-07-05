"""``ext_kit`` — agentic extension primitives (extension-side, NOT the harness).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 (E8).

A small importable library shipped alongside the demo extensions. Everything here
composes τ's **public** surface only (the ``tau`` CLI + the SDK); it is not part
of ``tau-agent-core`` / ``tau-coding-agent``. One module per atom — see the E8
step table. This package currently ships:

* :mod:`ext_kit.spawn` (S53) — isolated-agent spawning: :func:`~ext_kit.spawn.spawn_tau`,
  the :func:`~ext_kit.spawn.stream_tau` event iterator, usage/cost roll-up, and a
  bounded :class:`~ext_kit.spawn.WorkerPool`.
* :mod:`ext_kit.stream` (S54) — event-stream supervision over a child stream:
  :func:`~ext_kit.stream.iter_jsonl` reader, :class:`~ext_kit.stream.StreamCounters`,
  :class:`~ext_kit.stream.StuckDetector`, :class:`~ext_kit.stream.ProgressWatchdog`,
  and the :func:`~ext_kit.stream.monitor_stream` driver.
* :mod:`ext_kit.gate` (S55) — external-check gates: :func:`~ext_kit.gate.run_gate`
  (exit-code / regex verdict), :func:`~ext_kit.gate.verdict_node` (durable
  ``customMessage`` verdict block), and the anti-cheat
  :func:`~ext_kit.gate.revert_and_recheck`.
"""

from __future__ import annotations

from ext_kit.spawn import (
    DEFAULT_STUCK_LIMIT,
    FAILED_REASONS,
    ChildResult,
    ChildUsage,
    SpawnLimits,
    TauEvent,
    WorkerPool,
    build_child_args,
    price_increment,
    roll_message_usage,
    spawn_all,
    spawn_tau,
    stream_tau,
    tau_invocation,
)
from ext_kit.gate import (
    DEFAULT_VERDICT_TYPE,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_TIMEOUT,
    GateResult,
    RecheckResult,
    revert_and_recheck,
    run_gate,
    verdict_node,
)
from ext_kit.stream import (
    FLAGS,
    ProgressWatchdog,
    StreamCounters,
    StreamMonitor,
    StuckDetector,
    event_tool_signature,
    iter_jsonl,
    monitor_stream,
    read_jsonl,
)

__all__ = [
    "DEFAULT_STUCK_LIMIT",
    "DEFAULT_VERDICT_TYPE",
    "FAILED_REASONS",
    "FLAGS",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_TIMEOUT",
    "ChildResult",
    "ChildUsage",
    "GateResult",
    "ProgressWatchdog",
    "RecheckResult",
    "SpawnLimits",
    "StreamCounters",
    "StreamMonitor",
    "StuckDetector",
    "TauEvent",
    "WorkerPool",
    "build_child_args",
    "event_tool_signature",
    "iter_jsonl",
    "monitor_stream",
    "price_increment",
    "read_jsonl",
    "revert_and_recheck",
    "roll_message_usage",
    "run_gate",
    "spawn_all",
    "spawn_tau",
    "stream_tau",
    "tau_invocation",
    "verdict_node",
]
