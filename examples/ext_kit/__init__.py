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
* :mod:`ext_kit.state` (S56) — the *backplane*: :class:`~ext_kit.state.TreeStore`
  (typed, reload-safe records over the durable ``customEntry`` node, reconstructed
  along the active path), :class:`~ext_kit.state.FileStore` (atomic cross-session
  JSON under ``~/.tau/ext-state/``), and :func:`~ext_kit.state.active_cursor` (the
  "where am I now" leaf-id replay ``41_bookmarks`` uses to record a waypoint).
* :mod:`ext_kit.ledger` (S57) — the *budget / ledger*: :class:`~ext_kit.ledger.Pricing`
  (``from_config`` price lookup + ``cost_of``), :class:`~ext_kit.ledger.UsageMeter`
  (folds ``message_end`` / S45 usage into token + dollar totals),
  :class:`~ext_kit.ledger.CostLedger` (append-only JSONL with ``$/outcome`` queries),
  and the bang-bang :class:`~ext_kit.ledger.Ceiling` controller.
* :mod:`ext_kit.steer` (S58) — *in-loop steering*: :class:`~ext_kit.steer.ReminderBank`
  (the generalized ``21_reminders`` — threshold/cooldown rules drained into a durable
  ``<system-reminder>`` edit), :class:`~ext_kit.steer.TurnDebouncer` (turn-cadence rate
  limiter), and :func:`~ext_kit.steer.wrap_tool` (the pi *tool-override* pattern — shadow
  a built-in tool with ``before`` / ``after`` hooks).
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
from ext_kit.state import (
    CUSTOM_ENTRY_KIND,
    STATE_DIR_NAME,
    FileStore,
    TreeStore,
    active_cursor,
)
from ext_kit.ledger import (
    CEILING_OK,
    CEILING_STOPPED,
    CEILING_WARN,
    DEFAULT_WARN_RATIO,
    Ceiling,
    CostLedger,
    MeterTotals,
    OutcomeStats,
    Pricing,
    UsageMeter,
    usage_tokens,
)
from ext_kit.steer import (
    REMINDER_CLOSE,
    REMINDER_OPEN,
    ReminderBank,
    Rule,
    TurnDebouncer,
    wrap_tool,
)

__all__ = [
    "CEILING_OK",
    "CEILING_STOPPED",
    "CEILING_WARN",
    "CUSTOM_ENTRY_KIND",
    "DEFAULT_STUCK_LIMIT",
    "DEFAULT_VERDICT_TYPE",
    "DEFAULT_WARN_RATIO",
    "FAILED_REASONS",
    "FLAGS",
    "REMINDER_CLOSE",
    "REMINDER_OPEN",
    "STATE_DIR_NAME",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_TIMEOUT",
    "Ceiling",
    "ChildResult",
    "ChildUsage",
    "CostLedger",
    "FileStore",
    "GateResult",
    "MeterTotals",
    "OutcomeStats",
    "Pricing",
    "ProgressWatchdog",
    "RecheckResult",
    "ReminderBank",
    "Rule",
    "SpawnLimits",
    "StreamCounters",
    "StreamMonitor",
    "StuckDetector",
    "TauEvent",
    "TreeStore",
    "TurnDebouncer",
    "UsageMeter",
    "WorkerPool",
    "active_cursor",
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
    "usage_tokens",
    "verdict_node",
    "wrap_tool",
]
