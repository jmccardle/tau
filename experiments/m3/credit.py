"""Eval-swing credit assignment for the M3 Los Alamos chess experiment.

Reference: docs/M3-DESIGN.md §4.2 (the dense eval as the credit signal, the
swung-move distillation target, and the eval-swing<->result VALIDATION gate) and
§7 (the self-healing gate — CONDITIONAL credit assignment).

This module is pure Python over a played `GameRecord`: NO LLM, NO server, fully
offline. It reads the per-ply eval trace (`GameRecord.ply_evals`, White-positive
`eval_after` per half-move) and turns it into three things the downstream
distillation/gate steps consume:

1. `ply_swings` / `swung_moves` — the per-move eval-swing from the MOVER's
   perspective, and the swung moves that distillation is allowed to attribute a
   lesson to (a move that swung the eval is attributable; a quiet move is not).

2. `swing_result_correlation` — the §4.2 one-time precondition. It checks that
   per-move eval-swing actually tracks the final game result before eval-swing is
   trusted as the credit signal. If it did not, we would be distilling toward one
   target (eval) and grading by another (outcome) — the quiet slippage §4.2 warns
   about. This function does NOT retune anything; it only reports the coefficient.

3. `strategy_swing_stats` — the §7 gate input. This is the important, subtle one.

   The signal the self-healing gate demotes on is CONDITIONAL-ON-USE:
   ``P(bad eval-swing | this strategy was USED in this move)`` — computed here
   over exactly the moves that INVOKED a strategy (its strategy-hit trace crossed
   with the move-level eval-swing).

   It is explicitly NOT the marginal co-occurrence signal
   ``P(strategy present | loss)`` — demoting a strategy because it merely sat in
   the store during a lost game. That marginal is the rejected "M2 error" (§7): a
   good strategy present in a game later lost to an unrelated blunder would be
   demoted, and a poison that rode along in a win would escape. Only the moves
   that actually used a strategy count toward that strategy's stat here.

Fail-Early (repo rule): no silent defaults for undefined cases. A misaligned
hit-trace, an unfinished game fed to the correlation, a mismatched
records/hit-traces zip, or a variance-free correlation input all RAISE rather
than returning a fabricated number.

Sibling-module imports mirror the other experiments/m3 tests (pytest puts the
directory on sys.path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from driver import GameRecord
from los_alamos import BLACK, WHITE

# The terminal reasons that denote a genuine DRAW (winner is None *and* the game
# actually finished). `max-plies` is deliberately NOT here: it marks an UNFINISHED
# game and must never be scored as a draw (driver.py / §4.4).
_DRAW_REASONS = frozenset({"stalemate", "insufficient-material", "fifty-move", "threefold"})


# --- Per-ply eval-swing (§4.2) ----------------------------------------------


@dataclass(frozen=True)
class Swing:
    """One half-move's eval-swing, sign-adjusted to the MOVER.

    `swing > 0` means the move improved the mover's own position; `swing < 0`
    means it worsened it (a blunder). Because `eval_after` is White-positive, a
    Black move that pushes the eval down (helping Black) yields a POSITIVE swing
    for Black.
    """

    ply_index: int
    side: str  # WHITE or BLACK — the side that made this move.
    move_uci: str
    swing: float


def ply_swings(record: GameRecord, *, initial_eval: float = 0.0) -> list[Swing]:
    """Per-ply eval-swing from the mover's perspective, one `Swing` per ply.

    ``swing_i = (eval_after[i] - prev_eval) * (+1 if side==WHITE else -1)`` where
    ``prev_eval = eval_after[i-1]`` for i>0 and ``prev_eval = initial_eval`` for
    i=0. `initial_eval` defaults to 0.0 — the standard Los Alamos start eval
    (symmetric material, empty center, equal mobility). A caller whose game began
    from a custom position supplies that position's eval instead.
    """
    swings: list[Swing] = []
    prev_eval = initial_eval
    for pe in record.ply_evals:
        if pe.side == WHITE:
            sign = 1.0
        elif pe.side == BLACK:
            sign = -1.0
        else:
            # Fail-Early: a side that is neither WHITE nor BLACK has no defined
            # perspective; do not guess a sign.
            raise ValueError(f"ply {pe.ply_index}: unknown side {pe.side!r}")
        swing = (pe.eval_after - prev_eval) * sign
        swings.append(
            Swing(
                ply_index=pe.ply_index,
                side=pe.side,
                move_uci=pe.move_uci,
                swing=swing,
            )
        )
        prev_eval = pe.eval_after
    return swings


def swung_moves(record: GameRecord, *, threshold: float, initial_eval: float = 0.0) -> list[Swing]:
    """The distillation targets (§4.2): moves with ``abs(swing) >= threshold``.

    These are the moves distillation may attribute a lesson to; quiet moves
    (small swing) are not attributable. Gains (``swing > 0``) and losses
    (``swing < 0``) are both returned — the ReasoningBank success-and-failure
    discipline — and are distinguished by the sign of `Swing.swing`.

    Fail-Early: a negative threshold is undefined and raises.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold!r}")
    return [s for s in ply_swings(record, initial_eval=initial_eval) if abs(s.swing) >= threshold]


# --- §4.2 VALIDATION gate: does eval-swing track winning? --------------------


def _game_result_for_side(record: GameRecord, side: str) -> int:
    """Final result from `side`'s perspective: +1 win, 0 draw, -1 loss.

    Fail-Early: an UNFINISHED game (``reason == "max-plies"``, or any winner=None
    with a non-draw reason) has no result and raises — it must not be scored as a
    draw.
    """
    if record.winner in (WHITE, BLACK):
        return 1 if record.winner == side else -1
    if record.winner is None and record.reason in _DRAW_REASONS:
        return 0
    raise ValueError(
        f"cannot score an unfinished/unknown game "
        f"(winner={record.winner!r}, reason={record.reason!r})"
    )


def swing_result_correlation(records: list[GameRecord]) -> float:
    """Pearson correlation of per-move eval-swing with the final game result.

    The §4.2 one-time precondition. For every ply of every game we form one data
    point:

        x = the move's mover-perspective eval-swing (`Swing.swing`)
        y = that mover's SIDE's final result (+1 win / 0 draw / -1 loss)

    and return Pearson's r over all such points, pooled across games. A positive r
    means moves that improved the mover's position tend to come from the side that
    went on to win — i.e. eval-swing tracks the outcome we grade by, so it is safe
    to distill on. A weak r is the §4.2 signal to retune λ or fall back to
    outcome-attributed credit rather than distilling on eval-swing.

    Fail-Early:
      * an unfinished game raises (via `_game_result_for_side`);
      * fewer than two data points raises (correlation is undefined);
      * a variance-free input (all swings equal, or all results equal — e.g. an
        all-draws sample) raises rather than silently returning 0.0, which would
        masquerade as a real "no correlation" finding.
    """
    xs: list[float] = []
    ys: list[float] = []
    for record in records:
        result_by_side = {
            WHITE: _game_result_for_side(record, WHITE),
            BLACK: _game_result_for_side(record, BLACK),
        }
        for s in ply_swings(record):
            xs.append(s.swing)
            ys.append(float(result_by_side[s.side]))
    return _pearson(xs, ys)


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient, stdlib-only, no silent zero-variance.

    Raises on fewer than two points or on a zero-variance axis (a constant x or
    y), where r is genuinely undefined — never returns a fabricated 0.0.
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys must be the same length")
    if n < 2:
        raise ValueError("need at least two data points to correlate")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        raise ValueError(
            "no variance to correlate: at least one of eval-swing / result is "
            "constant across the sample (e.g. all draws)"
        )
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


# --- §7 CONDITIONAL credit assignment (the gate input) ----------------------

# A strategy-hit trace for ONE game: parallel to `GameRecord.ply_evals`, where
# entry i is the list of strategy ids that were USED to choose the move at ply i
# (empty list = a move chosen without invoking any tracked strategy). "Used", not
# "present": this is the distinction §7's conditional gate turns on.
HitTrace = list[list[str]]


@dataclass(frozen=True)
class StrategyStat:
    """Per-strategy conditional eval-swing stat, over the moves that USED it.

    `bad_fraction` is ``P(bad eval-swing | strategy used here)`` — the fraction of
    invoking moves with ``swing < 0``. A swing of exactly 0.0 is neither a gain nor
    a blunder and does NOT count as bad. `mean_swing` is the mean over the same
    invoking moves. This is the conditional signal the self-healing gate demotes
    on — NOT the marginal ``P(strategy present | loss)`` (see the module docstring).
    """

    strategy_id: str
    count: int  # number of moves that invoked this strategy.
    mean_swing: float
    bad_fraction: float


def strategy_swing_stats(
    records: list[GameRecord], hit_traces: list[HitTrace]
) -> dict[str, StrategyStat]:
    """Conditional eval-swing stats per strategy id (§7), over USED moves only.

    For each game, `hit_traces[g]` is aligned ply-by-ply to `records[g].ply_evals`;
    the move at ply i contributes its mover-perspective swing to EVERY strategy id
    in ``hit_traces[g][i]``. A strategy that is merely PRESENT in a game but never
    named in any ply's hit list contributes nothing to its own stat and is thus
    never penalized for a loss it did not cause — the whole conditional-vs-marginal
    point of §7.

    Fail-Early: `records` and `hit_traces` must be the same length, and each
    game's hit trace must be exactly as long as that game's ply-eval trace; a
    mismatch is a misaligned trace and raises rather than being silently zipped
    short.
    """
    if len(records) != len(hit_traces):
        raise ValueError(f"records/hit_traces length mismatch: {len(records)} != {len(hit_traces)}")

    per_strategy: dict[str, list[float]] = {}
    for record, trace in zip(records, hit_traces):
        swings = ply_swings(record)
        if len(trace) != len(swings):
            raise ValueError(
                f"hit-trace/ply-eval length mismatch for a game: {len(trace)} != {len(swings)}"
            )
        for swing, strategy_ids in zip(swings, trace):
            for sid in strategy_ids:
                per_strategy.setdefault(sid, []).append(swing.swing)

    stats: dict[str, StrategyStat] = {}
    for sid, vals in per_strategy.items():
        count = len(vals)
        mean_swing = sum(vals) / count
        bad = sum(1 for v in vals if v < 0.0)
        stats[sid] = StrategyStat(
            strategy_id=sid,
            count=count,
            mean_swing=mean_swing,
            bad_fraction=bad / count,
        )
    return stats
