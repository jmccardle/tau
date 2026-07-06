"""Example 51: Delegate fleet — ``20_delegate`` v2 with a live steering dashboard (E11, S72).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S72 (and §6.3 "Delegate fleet"). No pi
original — pi's ``subagent`` spawns children and rolls their output up *after* they
finish; it has no live status surface and no mid-run steering. This is the τ-native
composition the S72 row names: the four E8 atoms wired so a human (or a policy) can
watch a fleet of isolated children run and *steer them mid-flight*.

## What this shows — the four ``ext_kit`` atoms composed into a control loop

``/fleet`` launches one *isolated, read-only* ``tau`` child per task (one task per
line of the command args) and drives them concurrently, each under LIVE supervision:

1. **Pool** (:class:`ext_kit.spawn.WorkerPool`, S53) — at most ``concurrency``
   children run at once; each gets its own context window and can only read the repo
   (the same isolation ``20_delegate``'s parallel mode enforces).
2. **Event-stream supervision** (:class:`ext_kit.stream.StuckDetector` /
   :func:`ext_kit.stream.monitor_stream`, S54) — instead of ``spawn_tau``'s
   batch roll-up, each child is consumed through :func:`ext_kit.spawn.stream_tau`
   *live*: a :class:`~ext_kit.stream.StreamMonitor` folds every event, a
   :class:`~ext_kit.stream.StuckDetector` flags ``N`` identical consecutive tool
   calls, and an optional :class:`~ext_kit.stream.ProgressWatchdog` flags a silent
   child. A flag *stops the stream*, which reaps the child — the **kill** half.
3. **Per-child budget** (:class:`ext_kit.ledger.UsageMeter` +
   :class:`ext_kit.ledger.Ceiling`, S57) — each landed completion's usage is folded
   into a per-child meter; a bang-bang :class:`~ext_kit.ledger.Ceiling` trips when the
   child crosses its dollar (priced) or token (unpriced) budget, killing it.
4. **Live dashboard** (``ctx.ui.panel`` S68) — a keyed panel table of every child
   (status / turns / cost / last tool), re-rendered in place on each event, with a
   per-child **Abort** action button. The panel is the *steering* surface: an
   abort action dispatches :func:`_fleet_abort_command` back into this extension.

The **dial** S72 turns (roadmap §7): the child event stream is promoted to an INPUT
to a control loop — a stuck child is killed and *re-routed* (its task re-dispatched
once to a fresh child), an over-budget child is killed, and the human can abort any
child from the dashboard.

## The durable output — a cross-session cost ledger

Every child's final outcome (``done`` / ``stuck`` / ``stalled`` / ``over_budget`` /
``aborted``) is appended to a cross-session :class:`ext_kit.ledger.CostLedger` (S57,
one JSONL line per child under ``~/.tau/ext-state/<name>.jsonl``) with its dollars,
tokens, model, and re-route count. ``/fleet_ledger`` rolls it up by outcome — spend
that survives a restart. This is the only DURABLE write; it is queried back through
a fresh ``CostLedger`` over the same file (the S57 reload-invariance guarantee).

## The invariant (tree-as-truth)

The dashboard — :class:`_FleetState`'s rows and the S68 panel that mirrors them — is
EPHEMERAL RAM state, exactly like ``50_review_swarm``'s pending-triage table: never
persisted onto the session path, never model-visible, never rewriting a prior node.
The children are *separate processes* with their own context windows, so nothing they
do touches this session's active path. The cost ledger is a plain cross-session file
(``ext_kit.ledger.CostLedger`` / ``FileStore`` root), not a tree node. There is no
hidden model-input side-channel: the fleet's whole footprint on THIS session is the
display-only command output the handlers return.

## Headless parity (§6.3 CLI rule)

Every surface has a JSON representation and a non-interactive policy: ``/fleet`` emits
its live dashboard as ``{"type":"extension","kind":"panel",…}`` records on the
``--mode json`` stream (the panel is visible; its Abort buttons simply can't be
pressed without a TUI), and ``/fleet_abort <id>`` / ``/fleet_abort all`` is a plain
command that steers identically headless — so a parent ``tau -p --mode json`` running
this extension can watch and kill the fleet's children programmatically.

## Fail-Early

No tasks reports usage rather than launching an empty fleet; a per-child dollar budget
is accepted only against a known price (``cost`` block) — a ``max_usd`` with no price
RAISES rather than silently not-enforcing (``ext_kit.ledger`` / this module's own
check); an unpriced run budgets in TOKENS (a documented default ceiling), never a
fabricated ``$0``.

## Usage

    tau -e examples/51_delegate_fleet.py
    > /fleet
    ... audit auth.py for injection
    ... find the slowest test in the suite
    ... check the README links resolve
    Fleet: 3 child(ren) — 2 done, 1 stuck (re-routed 1x).
    > /fleet_ledger
    All-time: 3 event(s), 41200 tokens.
      done: 2 event(s), 30100 tokens
      stuck: 1 event(s), 11100 tokens
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tau_ai import AbortSignal

# ``ext_kit`` lives alongside the numbered examples, not inside an installed
# package — bootstrap ``examples/`` onto the path before importing it, whether run
# directly, imported, or loaded via ``-e`` (D-E6-3), the same as the other
# ext_kit-using demos (20_delegate, 50_review_swarm).
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import ledger, spawn, stream  # noqa: E402  (path insertion precedes import)

#: The extension's config slice key (``api.config`` is sliced by file stem, S40).
EXTENSION_STEM = "51_delegate_fleet"

#: The keyed S68 panel this demo mounts for the live dashboard (re-render / clear by key).
PANEL_KEY = "fleet"

#: Hard cap on fleet size (pi ``subagent`` MAX_PARALLEL_TASKS parity).
MAX_FLEET_TASKS = 8

#: Default bounded concurrency (pi ``subagent`` MAX_CONCURRENCY parity).
DEFAULT_CONCURRENCY = 4

#: Read-only tool allowlist every fleet child gets — an isolated child inspects, it
#: never mutates (the same isolation ``20_delegate`` parallel children enforce).
READONLY_TOOLS: tuple[str, ...] = ("read", "ls", "grep", "find")

#: Default per-child TOKEN budget when the child model is unpriced (no ``cost`` block).
#: A documented ceiling (generous — a real budget is configured), NOT a fabricated
#: price: an unpriced fleet meters tokens, never dollars.
DEFAULT_MAX_TOKENS = 200_000

#: How many times a stuck/stalled child is re-dispatched to a fresh child before the
#: fleet gives up on that task (the *re-route* half of the S72 steering dial).
DEFAULT_MAX_REROUTES = 1

#: Outcomes that trigger a re-route (a fresh child for the same task): a child that
#: wedged in a tool loop or went silent is retried; an over-budget or user-aborted
#: child is NOT (that was a deliberate stop, not a stall).
_REROUTE_ON: frozenset[str] = frozenset({"stuck", "stalled"})

#: Max characters of a task shown in the dashboard's ``task`` column (kept short so the
#: table stays legible; the full task rides the ledger record).
_TASK_DISPLAY_WIDTH = 32


# ── per-child budget (resolved once from config, one meter/ceiling per child) ──


@dataclass(frozen=True)
class _Budget:
    """A resolved per-child budget: a price block (or ``None``), the ceiling, and its
    unit. ``pricing`` is priced only in ``usd`` mode; token mode meters tokens.
    """

    pricing: ledger.Pricing | None
    limit: float
    unit: str  # "usd" | "tokens"


def _resolve_budget(cfg: dict[str, Any]) -> _Budget:
    """Resolve the per-child budget from ``api.config`` (S40), mirroring 43_budget_ledger.

    ``cost`` (a ``{input, output, cache_read}`` USD-per-1M price block) selects USD
    mode and REQUIRES ``max_usd``; its absence selects TOKEN mode against ``max_tokens``
    (default :data:`DEFAULT_MAX_TOKENS`). Fail-Early: pairing a ``max_usd`` with no
    ``cost`` — or a ``cost`` with no ``max_usd`` — RAISES rather than silently pricing
    at zero or not enforcing.
    """
    cost = cfg.get("cost")
    max_usd = cfg.get("max_usd")
    max_tokens = cfg.get("max_tokens")
    if cost is not None:
        if not isinstance(cost, dict):
            raise ValueError(f"{EXTENSION_STEM}: config 'cost' must be a price-block dict")
        if max_usd is None:
            raise ValueError(
                f"{EXTENSION_STEM}: a 'cost' block sets USD mode but no 'max_usd' ceiling "
                "was given (Fail-Early — a priced fleet needs its dollar budget)"
            )
        if max_tokens is not None:
            raise ValueError(
                f"{EXTENSION_STEM}: USD mode ('cost' present) — set 'max_usd', not 'max_tokens'"
            )
        if isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)) or max_usd <= 0:
            raise ValueError(
                f"{EXTENSION_STEM}: 'max_usd' must be a positive number, got {max_usd!r}"
            )
        return _Budget(
            pricing=ledger.Pricing(model=None, cost=cost), limit=float(max_usd), unit="usd"
        )
    if max_usd is not None:
        raise ValueError(
            f"{EXTENSION_STEM}: 'max_usd' needs a 'cost' block to price against "
            "(Fail-Early — no $0 guess); drop it or add 'cost', or budget in 'max_tokens'"
        )
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError(
            f"{EXTENSION_STEM}: 'max_tokens' must be a positive integer, got {max_tokens!r}"
        )
    return _Budget(pricing=None, limit=float(max_tokens), unit="tokens")


# ── resolved run config ──────────────────────────────────────────────────────


def _config_int(cfg: dict[str, Any], key: str, default: int) -> int:
    """Read a positive int from ``api.config`` (S40), else ``default`` (Fail-Early on a typo)."""
    if key not in cfg:
        return default
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"{EXTENSION_STEM}: config '{key}' must be a positive integer, got {value!r}"
        )
    return value


def _config_nonneg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    """Read a NON-negative int (0 allowed) from ``api.config`` — e.g. ``max_reroutes``
    where 0 means "never re-route". Fail-Early: a negative / non-int value RAISES.
    """
    if key not in cfg:
        return default
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"{EXTENSION_STEM}: config '{key}' must be a non-negative integer, got {value!r}"
        )
    return value


def _config_timeout(cfg: dict[str, Any]) -> float | None:
    """Read the optional ``progress_timeout`` (seconds) — a positive float, or ``None``
    (no liveness watchdog). Fail-Early: a present non-positive value RAISES.
    """
    if "progress_timeout" not in cfg:
        return None
    value = cfg["progress_timeout"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(
            f"{EXTENSION_STEM}: config 'progress_timeout' must be a positive number, got {value!r}"
        )
    return float(value)


@dataclass(frozen=True)
class _FleetConfig:
    """The fully-resolved run parameters (read once per ``/fleet`` from ``api.config``)."""

    concurrency: int
    stuck_limit: int
    progress_timeout: float | None
    max_reroutes: int
    model: str | None
    tools: tuple[str, ...]
    budget: _Budget


def _resolve_config(cfg: dict[str, Any]) -> _FleetConfig:
    """Build a :class:`_FleetConfig` from ``api.config`` (S40); Fail-Early on any typo."""
    model = cfg.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"{EXTENSION_STEM}: config 'model' must be a string, got {model!r}")
    tools_cfg = cfg.get("tools")
    if tools_cfg is None:
        tools: tuple[str, ...] = READONLY_TOOLS
    elif isinstance(tools_cfg, list) and all(isinstance(t, str) for t in tools_cfg):
        tools = tuple(tools_cfg)
    else:
        raise ValueError(
            f"{EXTENSION_STEM}: config 'tools' must be a list of strings, got {tools_cfg!r}"
        )
    return _FleetConfig(
        concurrency=_config_int(cfg, "concurrency", DEFAULT_CONCURRENCY),
        stuck_limit=_config_int(cfg, "stuck_limit", stream.DEFAULT_STUCK_LIMIT),
        progress_timeout=_config_timeout(cfg),
        max_reroutes=_config_nonneg_int(cfg, "max_reroutes", DEFAULT_MAX_REROUTES),
        model=model,
        tools=tools,
        budget=_resolve_budget(cfg),
    )


# ── the dashboard state (ephemeral RAM — never durable / model-visible) ──────


@dataclass
class _ChildRow:
    """One child's live dashboard row — mutated in place as its event stream folds.

    EPHEMERAL: this and its sibling rows mirror the S68 panel, never the session path.
    ``cost`` is dollars (priced) or ``None`` (unpriced — the ``tokens`` field carries
    the metered figure then); ``attempt`` counts spawns for this task (>1 ⇒ re-routed).
    """

    id: str
    task: str
    model: str
    status: str = "queued"
    turns: int = 0
    tokens: int = 0
    cost: float | None = None
    last_tool: str = ""
    attempt: int = 1
    final_output: str = ""

    def cost_cell(self) -> str:
        """The ``cost`` column text: dollars when priced, else the metered token count."""
        if self.cost is not None:
            return f"${self.cost:.4f}"
        if self.tokens:
            return f"{self.tokens} tok"
        return "—"

    def as_row(self) -> list[str]:
        """This row as a list of table cells (one per dashboard column)."""
        task = (
            self.task
            if len(self.task) <= _TASK_DISPLAY_WIDTH
            else self.task[: _TASK_DISPLAY_WIDTH - 1] + "…"
        )
        status = self.status if self.attempt == 1 else f"{self.status} (try {self.attempt})"
        return [self.id, task, status, str(self.turns), self.cost_cell(), self.last_tool or "—"]


@dataclass
class _FleetState:
    """The live fleet: ordered child rows + per-child abort signals + an active flag.

    EPHEMERAL shared state on the extension closure (like ``50_review_swarm``'s
    ``_Pending``): the ``/fleet`` driver writes rows/signals here, the S68 panel mirrors
    it, and ``/fleet_abort`` reaches in to trip a child's signal — the steering seam.
    """

    rows: dict[str, _ChildRow] = field(default_factory=dict)
    signals: dict[str, AbortSignal] = field(default_factory=dict)
    active: bool = False

    def reset(self) -> None:
        self.rows = {}
        self.signals = {}
        self.active = False


# ── panel spec (pure: state → S68 spec) ──────────────────────────────────────

_PANEL_COLUMNS: tuple[str, ...] = ("child", "task", "status", "turns", "cost", "last tool")


def _fleet_panel_spec(state: _FleetState) -> dict[str, Any]:
    """The S68 dashboard spec for the current fleet (pure).

    A table row per child plus one **Abort** action per RUNNING child (the per-row
    abort the roadmap §6.3 names — a panel's actions are a flat list, so one labelled
    button per live child), and an ``Abort all`` while the fleet is active. Each action
    dispatches :func:`_fleet_abort_command` (the panel→extension steering loop).
    """
    rows = [row.as_row() for row in state.rows.values()]
    running = sum(1 for r in state.rows.values() if r.status in ("running", "queued"))
    title = f"Fleet — {len(state.rows)} child(ren)" + (
        f", {running} running" if state.active else " (done)"
    )
    actions: list[dict[str, str]] = []
    if state.active:
        for row in state.rows.values():
            if row.status in ("running", "queued"):
                actions.append(
                    {"label": f"Abort {row.id}", "command": "fleet_abort", "args": row.id}
                )
        actions.append({"label": "Abort all", "command": "fleet_abort", "args": "all"})
    return {
        "title": title,
        "table": {"columns": list(_PANEL_COLUMNS), "rows": rows},
        "actions": actions,
    }


# ── one child, streamed + supervised (spawn.stream + stream monitor + ledger) ──


def _child_prompt(task: str) -> str:
    """The child's positional message — pi's ``Task:`` prefix (``20_delegate`` parity)."""
    return f"Task: {task}"


async def _run_child_streamed(
    row: _ChildRow,
    *,
    signal: AbortSignal,
    cfg: _FleetConfig,
    cwd: str,
    render: Any,
) -> str:
    """Stream ONE child attempt under live supervision; return its outcome status.

    Consumes :func:`ext_kit.spawn.stream_tau` through :func:`ext_kit.stream.monitor_stream`
    (a :class:`~ext_kit.stream.StreamMonitor` carrying the :class:`~ext_kit.stream.StuckDetector`
    and optional :class:`~ext_kit.stream.ProgressWatchdog`), folding each event into the
    dashboard ``row`` and a per-child :class:`ext_kit.ledger.UsageMeter` /
    :class:`~ext_kit.ledger.Ceiling`. The stream is stopped (→ child killed) the moment a
    supervisor trips:

    * ``stuck`` / ``stalled`` — the stream monitor's flag (re-routable);
    * ``over_budget`` — the ceiling crosses the per-child dollar/token limit;
    * ``aborted`` — ``signal`` was tripped (by ``/fleet_abort`` or ``ctx`` teardown).

    A clean end with no trip is ``done``. Returns the outcome; the row is left carrying
    the final turns / cost / tokens / last-tool for the dashboard and the ledger.
    """
    meter = ledger.UsageMeter(cfg.budget.pricing)
    ceiling = ledger.Ceiling(cfg.budget.limit)
    monitor = stream.StreamMonitor(
        stuck_limit=cfg.stuck_limit, progress_timeout=cfg.progress_timeout
    )
    flags: list[str] = []
    row.status = "running"
    render()

    outcome = "done"
    gen = stream.monitor_stream(
        spawn.stream_tau(
            _child_prompt(row.task),
            model=cfg.model,
            tools=list(cfg.tools),
            cwd=cwd,
            signal=signal,
        ),
        monitor=monitor,
        on_flag=flags.append,
    )
    try:
        async for event in gen:
            kind = event.get("type")
            if kind == "tool_execution_start":
                row.last_tool = str(event.get("tool_name") or "?")
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    if message.get("model") and row.model in ("", "default"):
                        row.model = str(message["model"])
                    text = spawn.message_text(message)
                    if text:
                        row.final_output = text
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        meter.record(usage)
                        row.tokens = meter.tokens
                        row.cost = meter.usd
                        value = meter.usd if cfg.budget.unit == "usd" else float(meter.tokens)
                        if value is not None and ceiling.update(value) == ledger.CEILING_STOPPED:
                            outcome = "over_budget"
            row.turns = monitor.counters.turns
            render()
            if outcome == "over_budget":
                break
    finally:
        # Stopping the supervised generator aclose()s the underlying stream_tau, which
        # terminates and reaps the child — the *kill* half of "flag/kill".
        await gen.aclose()

    if signal.is_aborted():
        return "aborted"
    if outcome == "over_budget":
        return outcome
    if "stuck" in flags:
        return "stuck"
    if "stalled" in flags:
        return "stalled"
    return outcome


async def _run_task(
    row: _ChildRow,
    *,
    state: _FleetState,
    cfg: _FleetConfig,
    cwd: str,
    render: Any,
) -> str:
    """Run one task to a FINAL outcome, re-routing a stuck/stalled child (the steering dial).

    Wraps :func:`_run_child_streamed` in the re-route loop: a ``stuck`` / ``stalled``
    outcome with re-routes remaining (and no user abort) re-dispatches the SAME task to a
    FRESH child (a new spawn, a fresh :class:`AbortSignal`, ``attempt`` bumped) up to
    ``cfg.max_reroutes`` times. An ``over_budget`` / ``aborted`` / ``done`` outcome is
    final. Returns the final outcome; ``row.status`` is left set to it.
    """
    attempt = 0
    while True:
        row.attempt = attempt + 1
        outcome = await _run_child_streamed(
            row, signal=state.signals[row.id], cfg=cfg, cwd=cwd, render=render
        )
        if (
            outcome in _REROUTE_ON
            and attempt < cfg.max_reroutes
            and not state.signals[row.id].is_aborted()
        ):
            attempt += 1
            row.status = f"re-routing ({outcome})"
            row.last_tool = ""
            render()
            # A fresh signal for the replacement child — the tripped one stays retired.
            state.signals[row.id] = AbortSignal()
            continue
        row.status = outcome
        render()
        return outcome


# ── the fleet driver (pool + live panel + per-child ledger append) ───────────


def _parse_tasks(args: str) -> list[str]:
    """Parse ``/fleet`` args into a task list — one non-blank line per task."""
    return [line.strip() for line in args.splitlines() if line.strip()]


async def run_fleet(
    tasks: list[str],
    *,
    ctx: Any,
    cfg: _FleetConfig,
    cost_ledger: ledger.CostLedger,
    state: _FleetState,
) -> list[str]:
    """Launch + supervise the whole fleet; return the per-task outcomes (task order).

    Seeds a dashboard row and an abort signal per task, renders the live S68 panel on
    every event, runs the tasks through a bounded :class:`ext_kit.spawn.WorkerPool`, and
    appends ONE cross-session :class:`ext_kit.ledger.CostLedger` record per child's final
    outcome (its dollars / tokens / model / attempts). Pure orchestration over the atoms —
    the command handler owns the args, the report, and the config.
    """
    cwd = getattr(ctx, "cwd", ".") or "."
    state.reset()
    for i, task in enumerate(tasks):
        cid = f"c-{i + 1}"
        state.rows[cid] = _ChildRow(id=cid, task=task, model=cfg.model or "default")
        state.signals[cid] = AbortSignal()
    state.active = True

    def render() -> None:
        ctx.ui.panel(PANEL_KEY, _fleet_panel_spec(state))

    render()

    pool = spawn.WorkerPool(cfg.concurrency)
    items = list(state.rows.items())

    async def _run(item: tuple[str, _ChildRow], _index: int) -> str:
        cid, row = item
        outcome = await _run_task(row, state=state, cfg=cfg, cwd=cwd, render=render)
        cost_ledger.append(
            outcome=outcome,
            usd=row.cost,
            tokens=row.tokens,
            model=row.model,
            child=cid,
            task=row.task,
            turns=row.turns,
            attempts=row.attempt,
        )
        return outcome

    outcomes = await pool.map(_run, items)
    state.active = False
    render()
    return outcomes


# ── reports (pure) ───────────────────────────────────────────────────────────


def _fleet_report(tasks: list[str], outcomes: list[str], state: _FleetState) -> str:
    """The S46 ``/fleet`` summary: per-outcome counts + how many were re-routed."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    rerouted = sum(1 for row in state.rows.values() if row.attempt > 1)
    parts = [f"{n} {name}" for name, n in sorted(counts.items())]
    tail = f" (re-routed {rerouted}x)" if rerouted else ""
    return f"Fleet: {len(tasks)} child(ren) — {', '.join(parts)}{tail}."


def _ledger_report(cost_ledger: ledger.CostLedger) -> str:
    """The S46 ``/fleet_ledger`` report: the all-time cross-session roll-up by outcome."""
    records = cost_ledger.records()
    if not records:
        return "Fleet ledger: no children recorded yet. Run /fleet first."
    total_usd = cost_ledger.total_usd()
    usd_part = f", ${total_usd:.4f}" if total_usd is not None else ""
    lines = [f"All-time: {len(records)} event(s), {cost_ledger.total_tokens()} tokens{usd_part}."]
    for outcome, stats in sorted(cost_ledger.by_outcome().items()):
        stat_usd = f", ${stats.usd:.4f}" if stats.usd is not None else ""
        lines.append(f"  {outcome}: {stats.count} event(s), {stats.tokens} tokens{stat_usd}")
    return "\n".join(lines)


# ── command handlers ─────────────────────────────────────────────────────────


async def _fleet_command(
    args: str,
    ctx: Any,
    *,
    state: _FleetState,
    cost_ledger: ledger.CostLedger,
    config: dict[str, Any],
) -> str:
    """``/fleet``: launch + supervise a fleet over the tasks (one task per line)."""
    tasks = _parse_tasks(args)
    if not tasks:
        return "No tasks — usage: /fleet <one task per line>."
    if len(tasks) > MAX_FLEET_TASKS:
        return f"Too many tasks ({len(tasks)}); max is {MAX_FLEET_TASKS}."
    cfg = _resolve_config(config)
    outcomes = await run_fleet(tasks, ctx=ctx, cfg=cfg, cost_ledger=cost_ledger, state=state)
    return _fleet_report(tasks, outcomes, state)


def _fleet_abort_command(args: str, ctx: Any, *, state: _FleetState) -> str:
    """``/fleet_abort <id>`` / ``/fleet_abort all``: trip a child's abort signal (steering).

    The panel-action target and the CLI steering verb: sets the abort signal a live
    child's ``stream_tau`` polls, so the child is killed on its next event. ``all`` aborts
    every still-running child. Fail-Early: an unknown child id is reported, not ignored.
    """
    if not state.active:
        return "No fleet is running."
    spec = args.strip()
    if not spec:
        return "Usage: /fleet_abort <child-id> | /fleet_abort all"
    if spec == "all":
        live = [cid for cid, row in state.rows.items() if row.status in ("running", "queued")]
        for cid in live:
            state.signals[cid].abort()
        return f"Aborting {len(live)} running child(ren)."
    if spec not in state.signals:
        return f"Unknown child {spec!r}. Live children: {', '.join(state.signals) or '(none)'}."
    state.signals[spec].abort()
    return f"Aborting {spec}."


def _fleet_ledger_command(args: str, ctx: Any, *, cost_ledger: ledger.CostLedger) -> str:
    """``/fleet_ledger``: the all-time cross-session cost roll-up (reload-safe read)."""
    return _ledger_report(cost_ledger)


# ── extension entry point ────────────────────────────────────────────────────


def delegate_fleet_extension(api: Any) -> None:
    """Register ``/fleet``, ``/fleet_abort``, ``/fleet_ledger`` — the fleet control surface."""
    config = api.config
    ledger_name = config.get("ledger_name", EXTENSION_STEM)
    if not isinstance(ledger_name, str) or not ledger_name:
        raise ValueError(f"{EXTENSION_STEM}: config 'ledger_name' must be a non-empty string")
    ledger_dir = config.get("ledger_dir")  # test/opt-in override of the cross-session root
    cost_ledger = ledger.CostLedger(ledger_name, base_dir=ledger_dir)
    state = _FleetState()

    async def fleet_handler(args: str, ctx: Any) -> str:
        return await _fleet_command(args, ctx, state=state, cost_ledger=cost_ledger, config=config)

    def fleet_abort_handler(args: str, ctx: Any) -> str:
        return _fleet_abort_command(args, ctx, state=state)

    def fleet_ledger_handler(args: str, ctx: Any) -> str:
        return _fleet_ledger_command(args, ctx, cost_ledger=cost_ledger)

    api.register_command(
        "fleet",
        {
            "description": "Launch a supervised delegate fleet (usage: /fleet <one task per line>)",
            "handler": fleet_handler,
        },
    )
    api.register_command(
        "fleet_abort",
        {
            "description": "Abort a running fleet child (usage: /fleet_abort <id> | /fleet_abort all)",
            "handler": fleet_abort_handler,
        },
    )
    api.register_command(
        "fleet_ledger",
        {
            "description": "Show the all-time cross-session fleet cost ledger",
            "handler": fleet_ledger_handler,
        },
    )


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/51_delegate_fleet.py`` → ``getattr(module, "register")``).
register = delegate_fleet_extension
