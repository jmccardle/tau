"""The C0-vs-C1 induction-loop runner for the M3 Los Alamos chess experiment.

Reference: docs/M3-DESIGN.md §4.4 (curriculum vs. measuring stick — play-vs-greedy
GENERATES trajectories, a fixed deterministic opponent MEASURES), §6 (conditions: C0 =
no-memory baseline, C1 = self-induced ReasoningBank loop; a fresh ``memory:strategy`` root
per condition/seed so no store bleeds into another), and §7/§8 (the material-margin curve
as the clean "did it hang pieces" signal).

This is pure wiring over the already-built pieces: the ``los_alamos`` engine, the
``driver`` game loop + agents, the ``credit`` eval-swing harness, the ``StrategyStore``
versioned tree (run here against the in-memory ``memory_store`` backing), and the
``distiller`` LLM call. It builds ONLY C0 and C1; C2/C3/poison (§6) are later.

The ReasoningBank loop (C1): play a curriculum game with the current assembled strategy
injected, distil its swung moves into lessons after every ``log_every_games`` games,
append each lesson to the immutable log, and consolidate the head every
``consolidate_every_logs`` log entries (a plain deterministic text merge for experiment 1 —
NOT an LLM call, see :func:`merge_consolidated_head`). Then MEASURE both conditions on the
same fixed suite of opening positions (C1 with the final strategy injected, C0 with none).

Functional tests fake the LLM (a greedy ``EngineAgent`` stand-in) and run against the
in-memory store — no GPU, no server. Stdlib + httpx only; sibling imports mirror the other
experiments/m3 modules (pytest puts the directory on sys.path).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from credit import Swing, swung_moves
from distiller import Distiller
from driver import Agent, EngineAgent, GameRecord, LLMAgent, PlyEval, play_game
from los_alamos import PIECE_VALUE, WHITE, Board, parse_move
from memory_store import InMemoryJmftsClient
from tau_jmfts.client import JmftsClient
from tau_jmfts.ext.strategy_store import Family, StrategyStore

# The single strategy family experiment 1 induces into. One family (not per-motif) is
# correct here: the §4.6 position→motif keying is a later condition (C2 rests on it); C0/C1
# inject the whole assembled family document per game, so one family holds everything.
FAMILY_NAME = "los-alamos"

# Terminal reasons that denote a genuine finished draw (mirrors credit._DRAW_REASONS).
_DRAW_REASONS = frozenset({"stalemate", "insufficient-material", "fifty-move", "threefold"})


# =====================================================================================
# Config
# =====================================================================================


@dataclass(frozen=True)
class RunConfig:
    """One run's knobs (§4.4/§6).

    ``condition`` is ``"C0"`` (no memory) or ``"C1"`` (self-induced). ``k_train`` is the
    number of curriculum games (0 for C0). ``measure_suite_size`` is how many fixed
    opening positions to measure on. ``log_every_games`` is the distillation cadence (every
    N games, distil the accumulated games); ``consolidate_every_logs`` folds the head every
    N appended log entries. ``swing_threshold`` is the ``abs(swing)`` a move must reach to
    be a distillation target (§4.2). ``temp_train``/``temp_measure`` are the sampling
    temperatures (measure at 0.0 for a reproducible read). ``opponent_depth`` is the
    engine ladder rung (1 = greedy floor, §4.4). ``max_plies`` caps an unfinished game.
    """

    condition: str
    k_train: int = 20
    measure_suite_size: int = 8
    log_every_games: int = 5
    consolidate_every_logs: int = 10
    swing_threshold: float = 1.0
    temp_train: float = 0.7
    temp_measure: float = 0.0
    opponent_depth: int = 1
    max_plies: int = 120

    def __post_init__(self) -> None:
        if self.condition not in ("C0", "C1"):
            raise ValueError(f"RunConfig.condition must be 'C0' or 'C1', got {self.condition!r}")
        if self.log_every_games < 1:
            raise ValueError("log_every_games must be >= 1")
        if self.consolidate_every_logs < 1:
            raise ValueError("consolidate_every_logs must be >= 1")


# =====================================================================================
# Result schema
# =====================================================================================


@dataclass
class MeasurementGame:
    """One measured game's record for the plot tool (§8)."""

    start_id: str
    llm_side: str  # "w" — the LLM plays White in experiment 1.
    winner: str | None  # "w" | "b" | None (draw or unfinished).
    reason: str
    final_margin: float
    margin_trajectory: list[float]
    # The full game record, persisted so richer move-quality metrics can be computed
    # OFFLINE (no GPU) after the fact: ``moves`` enables replay + centipawn-loss vs a
    # deeper engine search; ``ply_evals`` carries the engine's positional (material +
    # mobility) eval per half-move — a less cascade-sensitive per-move signal than the
    # terminal ``final_margin``. See docs/M3-DESIGN.md §4.2/§7.
    moves: list[str] = field(default_factory=list)
    ply_evals: list[PlyEval] = field(default_factory=list)


@dataclass
class RunResult:
    """The full run output; ``to_dict`` serialises the plot-tool schema plus the strategy.

    ``strategy`` is the induced strategy doc for a C1 run (``None`` for C0): the final
    assembled document plus the full immutable log — every lesson with its provenance
    (``source``), ``consolidated`` flag, and CR-1 ``position`` — preserved so the strategy
    observations these agents make can be analysed after the fact.
    """

    label: str
    condition: str
    config: dict[str, Any]
    training: dict[str, int]
    measurement: list[MeasurementGame] = field(default_factory=list)
    strategy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dump_strategy(store: StrategyStore, family: Family) -> dict[str, Any]:
    """Serialise the induced strategy: the final assembled doc + the full immutable log.

    Each log entry keeps its lesson ``content``, provenance ``source``, ``consolidated``
    flag, and CR-1 ``position`` (temporal order) — the raw material for a later analysis of
    what strategic observations the induction produced.
    """
    log: list[dict[str, Any]] = []
    for doc in store.history(family):
        sc = doc.get("structured_content") or {}
        log.append(
            {
                "content": doc.get("content") or "",
                "source": sc.get("source"),
                "consolidated": sc.get("consolidated"),
                "position": doc.get("position"),
            }
        )
    return {"final_doc": store.assemble(family), "log": log}


# =====================================================================================
# Injection seams (so the whole loop runs with no server under a fake LLM)
# =====================================================================================


class AgentFactory(Protocol):
    """Builds the White (LLM) player for one game, given the strategy read-back + temp."""

    def __call__(self, *, strategy_context: str | None, temperature: float) -> Agent: ...


class DistillerLike(Protocol):
    """The one method the runner needs off a distiller (real or a test spy)."""

    def distill(
        self, game_record: GameRecord, swung: list[Swing], board_start: Board
    ) -> list[str]: ...


def _default_agent_factory(endpoint: str) -> AgentFactory:
    """The production factory: a grammar-constrained ``LLMAgent`` against ``endpoint``."""

    def make(*, strategy_context: str | None, temperature: float) -> Agent:
        return LLMAgent(endpoint, strategy_context=strategy_context, temperature=temperature)

    return make


def greedy_agent_factory() -> AgentFactory:
    """A no-server stand-in: a greedy ``EngineAgent`` that ignores the strategy context.

    Used by ``--dry-run`` and the functional tests so the whole loop is exercisable with no
    LLM. It is a deterministic, always-legal player — the strategy context has no effect,
    so it exercises the WIRING (schedule, store growth, measurement) not the learning.
    """

    def make(*, strategy_context: str | None, temperature: float) -> Agent:
        return EngineAgent(1)

    return make


class _DryRunDistiller:
    """A no-server distiller: returns one canned lesson per game (for ``--dry-run``)."""

    def distill(self, game_record: GameRecord, swung: list[Swing], board_start: Board) -> list[str]:
        return [f"dry-run lesson from a {game_record.reason} game with {len(swung)} swung moves"]


# =====================================================================================
# Measurement suite + the material-margin metric
# =====================================================================================


def measurement_suite(m: int) -> list[Board]:
    """A FIXED, reproducible set of ``m`` distinct opening positions (§4.4).

    Position i = apply the i-th legal move from the standard start (White), then one greedy
    (1-ply) reply (Black), and snapshot the board — which is White (the LLM) to move. The
    legal-move order and ``best_move`` are both deterministic, so the same ``m`` positions
    come back every call; C0 and C1 are therefore measured on IDENTICAL starts (paired).

    Fail-Early: ``m`` beyond the number of opening moves raises rather than silently
    returning fewer positions.
    """
    if m < 0:
        raise ValueError(f"measurement_suite size must be >= 0, got {m}")
    opening_moves = Board().legal_moves()
    if m > len(opening_moves):
        raise ValueError(
            f"measurement_suite: asked for {m} positions but the standard start has only "
            f"{len(opening_moves)} legal opening moves"
        )
    suite: list[Board] = []
    for i in range(m):
        board = Board()
        board.push(board.legal_moves()[i])  # White's i-th opening move.
        board.push(board.best_move(1))  # Black's greedy reply.
        suite.append(board)
    return suite


def _start_id(index: int) -> str:
    """A stable, reproducible id for suite position ``index`` (the suite is deterministic)."""
    return f"suite-{index:02d}"


def material_margin(board: Board, side: str) -> float:
    """Material balance from ``side``'s perspective (§8): sum ``PIECE_VALUE``, then sign.

    White-minus-Black material (P1 N3 R5 Q9; king unscored), negated for a Black-side read.
    Material ONLY — not the full static eval — because it is the cleanest "did it hang
    pieces" signal, which is what the margin curve is for.
    """
    white = sum(PIECE_VALUE[p.upper()] for p in board.squares if p != "." and p.isupper())
    black = sum(PIECE_VALUE[p.upper()] for p in board.squares if p != "." and p.islower())
    diff = white - black
    return diff if side == WHITE else -diff


def margin_trajectory(start: Board, record: GameRecord, side: str) -> list[float]:
    """Per-ply material margin from ``side``, replayed over a copy of ``start``.

    Element 0 is the margin at the starting position; each later element is the margin
    AFTER that ply. The final element is ``final_margin``. Replaying (rather than reading a
    stored trajectory) keeps the metric independent of what the driver happened to record.
    """
    board = start.copy()
    traj = [material_margin(board, side)]
    for uci in record.moves:
        board.push(parse_move(board, uci))
        traj.append(material_margin(board, side))
    return traj


def _winner_code(winner: str | None) -> str | None:
    """Engine winner (WHITE/BLACK/None) → output code ("w"/"b"/None)."""
    if winner is None:
        return None
    return "w" if winner == WHITE else "b"


# =====================================================================================
# The consolidation merge (experiment 1: a plain deterministic text merge, NOT an LLM call)
# =====================================================================================


def merge_consolidated_head(current_head: str, footer_texts: list[str]) -> str:
    """Fold the footer lessons into the head by a deterministic order-preserving merge.

    Experiment 1 does NOT use the LLM to consolidate (§6's agent self-curation is C2, a
    later condition). The head is simply every distinct lesson seen so far, one per bullet,
    in first-seen order: the current head's bullet lines followed by any footer lessons not
    already present. Deterministic and idempotent — re-merging the same footer is a no-op.
    """
    seen: list[str] = []
    for line in current_head.splitlines():
        lesson = line[2:] if line.startswith("- ") else line
        lesson = lesson.strip()
        if lesson and lesson not in seen:
            seen.append(lesson)
    for text in footer_texts:
        lesson = text.strip()
        if lesson and lesson not in seen:
            seen.append(lesson)
    return "\n".join(f"- {lesson}" for lesson in seen)


# =====================================================================================
# The runner
# =====================================================================================


@dataclass
class _TrainCounters:
    """Mutable tallies threaded through the C1 flush closure."""

    lessons_logged: int = 0
    consolidations: int = 0
    logs_since_consolidate: int = 0
    head_text: str = ""  # tracked so the deterministic merge needs no head re-fetch.


def _train_c1(
    config: RunConfig,
    store: StrategyStore,
    family: Family,
    agent_factory: AgentFactory,
    distiller: DistillerLike,
) -> dict[str, int]:
    """Run the C1 curriculum + induction loop; return the training counters.

    For each of ``k_train`` games: play ``LLMAgent(strategy_context=assemble(family))``
    (White) vs a greedy/ladder ``EngineAgent`` (Black). Every ``log_every_games`` games,
    distil each accumulated game's own-side swung moves into lessons and append them; every
    ``consolidate_every_logs`` appended log entries, fold the head (deterministic merge).
    """
    counters = _TrainCounters()

    def flush(batch: list[tuple[int, GameRecord]]) -> None:
        for game_index, record in batch:
            own_swung = [
                s for s in swung_moves(record, threshold=config.swing_threshold) if s.side == WHITE
            ]
            if not own_swung:
                continue  # no attributable moves — do not call the distiller (Fail-Early).
            lessons = distiller.distill(record, own_swung, Board())
            for lesson in lessons:
                store.append_log(family, lesson, source=f"train-game-{game_index}")
                counters.lessons_logged += 1
                counters.logs_since_consolidate += 1
                if counters.logs_since_consolidate >= config.consolidate_every_logs:
                    footer_texts = [(doc.get("content") or "") for doc in store.footer(family)]
                    counters.head_text = merge_consolidated_head(counters.head_text, footer_texts)
                    store.consolidate(family, counters.head_text)
                    counters.consolidations += 1
                    counters.logs_since_consolidate = 0

    pending: list[tuple[int, GameRecord]] = []
    for game_index in range(config.k_train):
        context = store.assemble(family)
        agent = agent_factory(strategy_context=context, temperature=config.temp_train)
        opponent = EngineAgent(config.opponent_depth)
        record = play_game(agent, opponent, max_plies=config.max_plies)
        pending.append((game_index, record))
        if (game_index + 1) % config.log_every_games == 0:
            flush(pending)
            pending = []
    if pending:  # leftover games when k_train is not a multiple of log_every_games.
        flush(pending)

    return {
        "games": config.k_train,
        "lessons_logged": counters.lessons_logged,
        "consolidations": counters.consolidations,
    }


def _measure(
    config: RunConfig,
    agent_factory: AgentFactory,
    strategy_context: str | None,
) -> list[MeasurementGame]:
    """Measure on the fixed suite: LLM (White, temp 0) vs the ladder engine, per position."""
    games: list[MeasurementGame] = []
    for index, start in enumerate(measurement_suite(config.measure_suite_size)):
        agent = agent_factory(strategy_context=strategy_context, temperature=config.temp_measure)
        opponent = EngineAgent(config.opponent_depth)
        record = play_game(agent, opponent, board=start.copy(), max_plies=config.max_plies)
        traj = margin_trajectory(start, record, WHITE)
        games.append(
            MeasurementGame(
                start_id=_start_id(index),
                llm_side="w",
                winner=_winner_code(record.winner),
                reason=record.reason,
                final_margin=traj[-1],
                margin_trajectory=traj,
                moves=list(record.moves),
                ply_evals=list(record.ply_evals),
            )
        )
    return games


def run(
    config: RunConfig,
    *,
    llm_endpoint: str,
    distiller_endpoint: str,
    out_path: str | Path | None = None,
    store_client: InMemoryJmftsClient | JmftsClient | None = None,
    agent_factory: AgentFactory | None = None,
    distiller: DistillerLike | None = None,
    root_title: str | None = None,
    label: str | None = None,
) -> RunResult:
    """Run one condition (C0 or C1) and return (and optionally write) its :class:`RunResult`.

    C1 trains the store then measures with the final strategy injected; C0 skips training
    entirely, writes NOTHING to the store, and measures with no injection — the paired
    baseline (§6). The store gets a fresh root per run (``root_title``) so conditions/seeds
    stay isolated. ``store_client``/``agent_factory``/``distiller`` are injectable so the
    loop runs under a fake LLM with no server (the functional tests + ``--dry-run``).
    """
    client = store_client if store_client is not None else InMemoryJmftsClient()
    factory = agent_factory if agent_factory is not None else _default_agent_factory(llm_endpoint)
    dstl = distiller if distiller is not None else Distiller(distiller_endpoint)
    run_label = label if label is not None else config.condition

    if config.condition == "C1":
        title = root_title if root_title is not None else f"m3-{run_label}"
        # cast: the in-memory backing duck-types the JmftsClient surface StrategyStore
        # calls; StrategyStore never introspects the concrete client type (see its tests).
        store = StrategyStore(cast(JmftsClient, client), root_title=title)
        family = store.family(FAMILY_NAME)
        training = _train_c1(config, store, family, factory, dstl)
        final_context: str | None = store.assemble(family)
        strategy: dict[str, Any] | None = _dump_strategy(store, family)
    else:
        # C0: no store touched, no injection — the empty-store baseline.
        training = {"games": 0, "lessons_logged": 0, "consolidations": 0}
        final_context = None
        strategy = None

    measurement = _measure(config, factory, final_context)
    result = RunResult(
        label=run_label,
        condition=config.condition,
        config=asdict(config),
        training=training,
        measurement=measurement,
        strategy=strategy,
    )
    if out_path is not None:
        Path(out_path).write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return result


# =====================================================================================
# CLI
# =====================================================================================


def _build_config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        condition=args.condition,
        k_train=args.k_train,
        measure_suite_size=args.measure_suite_size,
        log_every_games=args.log_every_games,
        consolidate_every_logs=args.consolidate_every_logs,
        swing_threshold=args.swing_threshold,
        temp_train=args.temp_train,
        temp_measure=args.temp_measure,
        opponent_depth=args.opponent_depth,
        max_plies=args.max_plies,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. ``--dry-run`` swaps in the greedy fake LLM (no server needed)."""
    parser = argparse.ArgumentParser(description="M3 C0/C1 induction-loop runner")
    parser.add_argument("--condition", choices=["C0", "C1"], required=True)
    parser.add_argument("--k-train", type=int, default=20, dest="k_train")
    parser.add_argument("--measure-suite-size", type=int, default=8, dest="measure_suite_size")
    parser.add_argument("--log-every-games", type=int, default=5, dest="log_every_games")
    parser.add_argument(
        "--consolidate-every-logs", type=int, default=10, dest="consolidate_every_logs"
    )
    parser.add_argument("--swing-threshold", type=float, default=1.0, dest="swing_threshold")
    parser.add_argument("--temp-train", type=float, default=0.7, dest="temp_train")
    parser.add_argument("--temp-measure", type=float, default=0.0, dest="temp_measure")
    parser.add_argument("--opponent-depth", type=int, default=1, dest="opponent_depth")
    parser.add_argument("--max-plies", type=int, default=120, dest="max_plies")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8080/v1/chat/completions",
        help="LLM chat/completions endpoint (also the distiller endpoint unless overridden)",
    )
    parser.add_argument(
        "--distiller-endpoint",
        default=None,
        help="distiller endpoint (defaults to --endpoint)",
    )
    parser.add_argument("--out", default=None, help="write the RunResult JSON here")
    parser.add_argument("--label", default=None, help="RunResult label (defaults to the condition)")
    parser.add_argument("--root-title", default=None, dest="root_title")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use a greedy fake LLM + canned distiller — exercises the whole loop, no server",
    )
    args = parser.parse_args(argv)

    config = _build_config(args)
    distiller_endpoint = args.distiller_endpoint or args.endpoint

    factory: AgentFactory | None = None
    distiller: DistillerLike | None = None
    if args.dry_run:
        factory = greedy_agent_factory()
        distiller = _DryRunDistiller()

    result = run(
        config,
        llm_endpoint=args.endpoint,
        distiller_endpoint=distiller_endpoint,
        out_path=args.out,
        agent_factory=factory,
        distiller=distiller,
        root_title=args.root_title,
        label=args.label,
    )
    if args.out is None:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"wrote {args.out} ({len(result.measurement)} measurement games)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
