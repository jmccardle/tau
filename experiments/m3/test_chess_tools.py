"""Tests for the agentic-chess tool substrate (experiments/m3/chess_tools.py).

Pure, offline, deterministic — no server. Covers variant-FEN round-tripping, the
attacker/defender enumeration the engine doesn't expose, the check detector, and
the piece_info "reachable-but-illegal (self-check)" split that the tools give the
agent instead of making it compute pins in its head.
"""

from __future__ import annotations

import pytest

from chess_tools import (
    attackers_defenders,
    attackers_of,
    board_from_fen,
    board_to_fen,
    check_status,
    list_legal_moves,
    piece_info,
    static_eval,
)
from los_alamos import WHITE, Board, coord_to_square, square_to_coord

START_FEN = "rnqknr/pppppp/6/6/PPPPPP/RNQKNR w - - 0 1"


# --- FEN ---------------------------------------------------------------------


def test_start_position_fen() -> None:
    assert board_to_fen(Board()) == START_FEN


def test_fen_round_trip_start() -> None:
    assert board_from_fen(START_FEN) == Board()


def test_fen_round_trip_constructed() -> None:
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    assert board_from_fen(board_to_fen(b)) == b


def test_fen_rejects_wrong_rank_count() -> None:
    with pytest.raises(ValueError, match="ranks"):
        board_from_fen("6/6/6/6/6 w - - 0 1")  # only 5 ranks


def test_fen_rejects_bad_piece() -> None:
    with pytest.raises(ValueError, match="unknown piece"):
        board_from_fen("bnqknr/pppppp/6/6/PPPPPP/RNQKNR w - - 0 1")  # 'b' = bishop, not in variant


def test_fen_rejects_overfull_rank() -> None:
    with pytest.raises(ValueError, match="files"):
        board_from_fen("rnqknrr/pppppp/6/6/PPPPPP/RNQKNR w - - 0 1")  # 7 on the top rank


# --- attacker enumeration ----------------------------------------------------


def test_attackers_of_collects_all_sources() -> None:
    # A black rook on e6 and a white king on e1 both bear on e2 along the file/adjacency.
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    e2 = coord_to_square("e2")
    assert [square_to_coord(s) for s in attackers_of(b, e2, by_white=False)] == ["e6"]
    assert [square_to_coord(s) for s in attackers_of(b, e2, by_white=True)] == ["e1"]


# --- check detection ---------------------------------------------------------


def test_check_status_blocked_is_not_check() -> None:
    # Rook on e2 blocks the black rook's line to the white king -> NOT in check.
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    st = check_status(b)
    assert st["in_check"] is False and st["checked_by"] == []


def test_check_status_detects_and_names_the_checker() -> None:
    b = Board.from_piece_map({"e1": "K", "e6": "r", "a6": "k"}, side=WHITE)
    st = check_status(b)
    assert st["in_check"] is True
    assert st["checked_by"] == ["e6"]
    assert st["king_square"] == "e1"


# --- piece_info: the self-check split ---------------------------------------


def test_piece_info_pinned_rook_splits_legal_from_self_check() -> None:
    # White rook e2 is pinned to the king (e1) by the black rook (e6). It may
    # only move ALONG the pin; every off-file target is reachable-but-illegal.
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    info = piece_info(b, "e2")
    assert info["piece"] == "R" and info["color"] == WHITE
    assert set(info["legal_targets"]) == {"e3", "e4", "e5", "e6"}  # up the file + capture
    assert set(info["blocked_by_self_check"]) == {"a2", "b2", "c2", "d2", "f2"}  # off the pin
    assert info["attacked_by"] == ["e6"]
    assert info["defended_by"] == ["e1"]


def test_piece_info_opponent_piece_has_no_move_targets() -> None:
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    info = piece_info(b, "e6")  # black piece, White to move
    assert info["legal_targets"] is None and info["blocked_by_self_check"] is None
    assert "e2" in info["attacks"]  # it does bear down the file onto the white rook


def test_piece_info_empty_square_raises() -> None:
    with pytest.raises(ValueError, match="no piece"):
        piece_info(Board(), "c3")


# --- attackers_defenders + static_eval + legal moves -------------------------


def test_attackers_defenders_both_colours() -> None:
    b = Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    ad = attackers_defenders(b, "e2")
    assert ad["occupant"] == "R"
    assert ad["white_attackers"] == ["e1"]
    assert ad["black_attackers"] == ["e6"]


def test_static_eval_start_is_balanced() -> None:
    se = static_eval(Board())
    assert se["material"]["white"] == se["material"]["black"] == 31.0
    assert se["material_margin_white"] == 0.0
    assert se["eval_white_positive"] == 0.0


def test_legal_moves_start_has_ten() -> None:
    # Matches measurement_suite's "10 legal opening moves" (6 pawns + 2*2 knights).
    assert len(list_legal_moves(Board())) == 10
