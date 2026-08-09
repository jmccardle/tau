"""Tests for the M3 eval-swing credit harness (experiments/m3/credit.py).

Reference: docs/M3-DESIGN.md §4.2 (per-move eval-swing, swung-move distillation
targets, and the swing<->result validation gate) and §7 (the CONDITIONAL,
used-not-present credit assignment the self-healing gate demotes on).

Every `GameRecord` and hit-trace here is hand-synthesized — NO LLM, NO server.
The eval-swing arithmetic is exact, so the fixtures assert exact numbers. Sibling
imports mirror test_driver.py (pytest puts the directory on sys.path).
"""

from __future__ import annotations

import math

import pytest

from credit import (
    Swing,
    ply_swings,
    strategy_swing_stats,
    swing_result_correlation,
    swung_moves,
)
from driver import GameRecord, PlyEval
from los_alamos import BLACK, WHITE


def _record(
    evals: list[tuple[str, str, float]],
    *,
    winner: str | None,
    reason: str,
) -> GameRecord:
    """Build a GameRecord from ``(side, move_uci, eval_after)`` tuples.

    `eval_after` is White-positive, exactly as the driver records it.
    """
    ply_evals = [
        PlyEval(ply_index=i, side=side, move_uci=uci, eval_after=ev)
        for i, (side, uci, ev) in enumerate(evals)
    ]
    return GameRecord(
        winner=winner,
        reason=reason,
        moves=[uci for _, uci, _ in evals],
        ply_evals=ply_evals,
    )


# --- ply_swings: sign adjustment both ways ----------------------------------


def test_white_move_dropping_white_eval_is_negative_swing() -> None:
    # White moves and the White-positive eval falls 0.0 -> -3.0: White hung a
    # knight. From White's perspective that is a blunder => negative swing.
    record = _record(
        [(WHITE, "b1c3", -3.0)],
        winner=BLACK,
        reason="checkmate",
    )
    (s,) = ply_swings(record)
    assert s.swing == pytest.approx(-3.0)
    assert s.side == WHITE


def test_black_move_dropping_white_eval_is_positive_swing() -> None:
    # Black moves and the White-positive eval falls 0.0 -> -3.0: that helps Black,
    # so from the MOVER's (Black's) perspective it is a GAIN => positive swing.
    record = _record(
        [(BLACK, "b6c4", -3.0)],
        winner=BLACK,
        reason="checkmate",
    )
    (s,) = ply_swings(record)
    assert s.swing == pytest.approx(3.0)
    assert s.side == BLACK


def test_two_knights_hung_smoke_sequence() -> None:
    # A concrete two-ply case: White hangs a knight (eval 0 -> -3), then Black
    # blunders one straight back (eval -3 -> 0). Each mover's own perspective
    # sees its own move as the -3 blunder.
    record = _record(
        [(WHITE, "b1c3", -3.0), (BLACK, "e6d4", 0.0)],
        winner=None,
        reason="threefold",
    )
    white_swing, black_swing = ply_swings(record)
    assert white_swing.swing == pytest.approx(-3.0)  # White worsened White eval.
    assert black_swing.swing == pytest.approx(-3.0)  # Black worsened Black eval.


def test_ply_zero_uses_default_initial_eval_zero() -> None:
    # First White move lifts the eval to +1.0 from the default 0.0 baseline.
    record = _record([(WHITE, "d2d3", 1.0)], winner=WHITE, reason="checkmate")
    (s,) = ply_swings(record)
    assert s.swing == pytest.approx(1.0)


def test_ply_zero_custom_baseline_shifts_first_swing() -> None:
    # Game started from a position already at +1.5 for White; the first move only
    # reaches +1.0, so from White's view the move LOST 0.5 relative to the start.
    record = _record([(WHITE, "d2d3", 1.0)], winner=WHITE, reason="checkmate")
    (default_s,) = ply_swings(record)
    (custom_s,) = ply_swings(record, initial_eval=1.5)
    assert default_s.swing == pytest.approx(1.0)
    assert custom_s.swing == pytest.approx(-0.5)


def test_prev_eval_chains_across_plies() -> None:
    # White: 0 -> 2 (+2). Black: 2 -> 2.5, White-positive rose so Black's
    # perspective swing is -0.5. White: 2.5 -> 1 (-1.5).
    record = _record(
        [(WHITE, "a", 2.0), (BLACK, "b", 2.5), (WHITE, "c", 1.0)],
        winner=None,
        reason="stalemate",
    )
    w0, b1, w2 = ply_swings(record)
    assert w0.swing == pytest.approx(2.0)
    assert b1.swing == pytest.approx(-0.5)
    assert w2.swing == pytest.approx(-1.5)


# --- swung_moves: threshold + gain/loss separation --------------------------


def test_swung_moves_thresholds_and_separates_gain_from_loss() -> None:
    # Swings by ply: +2.0 (gain), -0.5 (quiet), -3.0 (loss), +0.1 (quiet).
    record = _record(
        [
            (WHITE, "m0", 2.0),  # 0 -> 2   : +2.0
            (BLACK, "m1", 1.5),  # 2 -> 1.5 : Black view +0.5 ... wait computed below
            (WHITE, "m2", -1.5),  # 1.5 -> -1.5 : -3.0
            (BLACK, "m3", -1.6),  # -1.5 -> -1.6 : Black view +0.1
        ],
        winner=None,
        reason="fifty-move",
    )
    swings = {s.move_uci: s.swing for s in ply_swings(record)}
    # Sanity on the fixture arithmetic before thresholding.
    assert swings["m0"] == pytest.approx(2.0)
    assert swings["m1"] == pytest.approx(0.5)
    assert swings["m2"] == pytest.approx(-3.0)
    assert swings["m3"] == pytest.approx(0.1)

    swung = swung_moves(record, threshold=1.0)
    moved = {s.move_uci for s in swung}
    assert moved == {"m0", "m2"}  # only |swing| >= 1.0 survive.

    gains = [s for s in swung if s.swing > 0]
    losses = [s for s in swung if s.swing < 0]
    assert [s.move_uci for s in gains] == ["m0"]
    assert [s.move_uci for s in losses] == ["m2"]


def test_swung_moves_negative_threshold_raises() -> None:
    record = _record([(WHITE, "m0", 1.0)], winner=WHITE, reason="checkmate")
    with pytest.raises(ValueError):
        swung_moves(record, threshold=-0.1)


# --- swing_result_correlation: §4.2 validation gate -------------------------


def test_swing_correlates_positively_when_swing_predicts_result() -> None:
    # A White win where White's moves gain and Black's moves lose (positive
    # swings from the winner, negative from the loser), and a mirrored Black win.
    # Winner-side moves have positive swings, loser-side negative => the per-move
    # (swing, side-result) points line up on a positive slope.
    white_win = _record(
        [
            (WHITE, "w0", 1.0),  # +1.0, side result +1
            (BLACK, "b0", 2.0),  # White-positive rose => Black swing -1.0, result -1
            (WHITE, "w1", 3.0),  # +1.0, result +1
        ],
        winner=WHITE,
        reason="checkmate",
    )
    black_win = _record(
        [
            (WHITE, "w0", -1.0),  # -1.0, White result -1
            (BLACK, "b0", -2.0),  # White eval fell => Black swing +1.0, result +1
            (WHITE, "w1", -3.0),  # -1.0, White result -1
        ],
        winner=BLACK,
        reason="checkmate",
    )
    r = swing_result_correlation([white_win, black_win])
    # Every point is (swing=+1, result=+1) or (swing=-1, result=-1): perfect line.
    assert r == pytest.approx(1.0)
    assert -1.0 <= r <= 1.0


def test_correlation_all_draws_has_no_variance_and_raises() -> None:
    # All games drawn => every per-move result is 0 => zero variance in y. Per the
    # documented contract this RAISES rather than silently returning 0.0.
    draw = _record(
        [(WHITE, "w0", 1.0), (BLACK, "b0", 0.5)],
        winner=None,
        reason="stalemate",
    )
    with pytest.raises(ValueError, match="no variance"):
        swing_result_correlation([draw, draw])


def test_correlation_rejects_unfinished_game() -> None:
    # A max-plies game is UNFINISHED, not a draw; feeding it to the outcome
    # correlation must raise, never be scored as 0.
    unfinished = _record(
        [(WHITE, "w0", 1.0), (BLACK, "b0", 0.5)],
        winner=None,
        reason="max-plies",
    )
    finished = _record(
        [(WHITE, "w0", 1.0), (BLACK, "b0", 2.0)],
        winner=WHITE,
        reason="checkmate",
    )
    with pytest.raises(ValueError, match="unfinished"):
        swing_result_correlation([finished, unfinished])


def test_correlation_is_a_real_intermediate_coefficient() -> None:
    # A noisier sample yields an r strictly inside (-1, 1) computed by hand.
    game = _record(
        [
            (WHITE, "w0", 2.0),  # swing +2.0, result +1
            (BLACK, "b0", 2.0),  # swing 0.0,  result -1
            (WHITE, "w1", 1.0),  # swing -1.0, result +1
        ],
        winner=WHITE,
        reason="checkmate",
    )
    xs = [2.0, 0.0, -1.0]
    ys = [1.0, -1.0, 1.0]
    n = 3
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    expected = sxy / math.sqrt(sxx * syy)
    assert swing_result_correlation([game]) == pytest.approx(expected)


# --- strategy_swing_stats: §7 conditional (used, not present) ---------------


def test_strategy_used_only_in_blunders_gets_bad_stats() -> None:
    # "hang" is invoked on two moves, both blunders (negative swing).
    record = _record(
        [(WHITE, "w0", -2.0), (BLACK, "b0", -1.0)],
        winner=BLACK,
        reason="checkmate",
    )
    # w0: 0 -> -2  => White swing -2.0 (blunder)
    # b0: -2 -> -1 => White-positive rose => Black swing -1.0 (blunder)
    trace = [["hang"], ["hang"]]
    stats = strategy_swing_stats([record], [trace])
    assert stats["hang"].count == 2
    assert stats["hang"].mean_swing == pytest.approx(-1.5)
    assert stats["hang"].bad_fraction == pytest.approx(1.0)


def test_strategy_used_in_good_moves_gets_low_bad_fraction() -> None:
    record = _record(
        [(WHITE, "w0", 2.0), (BLACK, "b0", 1.0)],
        winner=WHITE,
        reason="checkmate",
    )
    # w0: 0 -> 2  => White swing +2.0 (gain)
    # b0: 2 -> 1  => White eval fell => Black swing +1.0 (gain)
    trace = [["center"], ["center"]]
    stats = strategy_swing_stats([record], [trace])
    assert stats["center"].count == 2
    assert stats["center"].mean_swing == pytest.approx(1.5)
    assert stats["center"].bad_fraction == pytest.approx(0.0)


def test_strategy_present_in_loss_but_not_used_is_not_penalized() -> None:
    # THE conditional-vs-marginal test. White loses the game. "good" was used only
    # on White's genuinely strong move (w0, +2.0). The loss came from an unrelated
    # blunder on w1 (-3.0) that "good" was NEVER invoked on. A marginal
    # P(present | loss) would demote "good" for riding along in a lost game; the
    # conditional signal must not, because "good" was not USED in the losing move.
    record = _record(
        [
            (WHITE, "w0", 2.0),  # swing +2.0, uses "good"
            (BLACK, "b0", 2.0),  # White eval rose => Black swing 0.0, no strategy
            (WHITE, "w1", -1.0),  # 2 -> -1 => swing -3.0, the losing blunder, uses "blunder"
        ],
        winner=BLACK,
        reason="checkmate",
    )
    trace = [["good"], [], ["blunder"]]
    stats = strategy_swing_stats([record], [trace])

    # "good" is judged ONLY on the move that used it: a pure gain, never demoted.
    assert stats["good"].count == 1
    assert stats["good"].mean_swing == pytest.approx(2.0)
    assert stats["good"].bad_fraction == pytest.approx(0.0)

    # The actual culprit, used on the blunder, carries the whole bad signal.
    assert stats["blunder"].count == 1
    assert stats["blunder"].bad_fraction == pytest.approx(1.0)


def test_strategy_stats_pool_across_games_and_zero_swing_not_bad() -> None:
    # A strategy used across two games; one invoking move has an exactly-zero
    # swing (a quiet move), which is neither gain nor blunder => not counted bad.
    g1 = _record([(WHITE, "w0", 1.0)], winner=WHITE, reason="checkmate")  # +1.0
    g2 = _record(
        [(WHITE, "w0", 0.0), (WHITE, "w1", -2.0)],
        winner=BLACK,
        reason="checkmate",
    )
    # g2 w0: 0 -> 0 => swing 0.0 (quiet); g2 w1: 0 -> -2 => swing -2.0 (blunder).
    stats = strategy_swing_stats(
        [g1, g2],
        [[["s"]], [["s"], ["s"]]],
    )
    assert stats["s"].count == 3
    assert stats["s"].mean_swing == pytest.approx((1.0 + 0.0 - 2.0) / 3)
    # Only the -2.0 move is bad; the 0.0 move is NOT counted as a blunder.
    assert stats["s"].bad_fraction == pytest.approx(1 / 3)


def test_records_and_hit_traces_length_mismatch_raises() -> None:
    record = _record([(WHITE, "w0", 1.0)], winner=WHITE, reason="checkmate")
    with pytest.raises(ValueError, match="length mismatch"):
        strategy_swing_stats([record], [])


def test_hit_trace_ply_misalignment_raises() -> None:
    # Trace shorter than the ply-eval trace is a misaligned trace => raise, never
    # silently zip short.
    record = _record(
        [(WHITE, "w0", 1.0), (BLACK, "b0", 0.5)],
        winner=WHITE,
        reason="checkmate",
    )
    with pytest.raises(ValueError, match="length mismatch"):
        strategy_swing_stats([record], [[["s"]]])


def test_swing_is_frozen_dataclass() -> None:
    s = Swing(ply_index=0, side=WHITE, move_uci="w0", swing=1.0)
    with pytest.raises(Exception):
        s.swing = 2.0  # type: ignore[misc]
