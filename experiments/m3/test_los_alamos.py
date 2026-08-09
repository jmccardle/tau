"""Tests for the Los Alamos chess engine (experiments/m3/los_alamos.py).

Reference: docs/M3-DESIGN.md §4.1-§4.4. The engine is the M3 Arm A
mechanism-proof apparatus, so its correctness is load-bearing and is verified
here rather than trusted.

Coverage:
    - make/unmake exact round-trip across random legal playouts;
    - no generated legal move leaves the mover's own king in check;
    - perft(1..3) stability + cross-check vs an independent brute-force recount;
    - mate-in-1, stalemate, and promotion (Q/R/N) production;
    - greedy (depth 1) grabs a free hanging piece;
    - the grammar helper's move list equals the legal set and yields a
      well-formed GBNF alternation;
    - draw detection (insufficient material, fifty-move, threefold).
"""

from __future__ import annotations

import random

from los_alamos import (
    BLACK,
    WHITE,
    Board,
    Move,
    brute_force_legal_moves,
    brute_force_perft,
    coord_to_square,
    format_move,
    grammar_for_position,
    legal_move_strings,
    parse_move,
)

# --- Initial-position sanity -------------------------------------------------


def test_initial_setup() -> None:
    b = Board()
    # White back rank R N Q K N R on rank 1.
    assert "".join(b.squares[0:6]) == "RNQKNR"
    assert "".join(b.squares[6:12]) == "PPPPPP"
    assert "".join(b.squares[24:30]) == "pppppp"
    assert "".join(b.squares[30:36]) == "rnqknr"
    assert b.side == WHITE
    # Queen on c-file, king on d-file for both colours.
    assert b.squares[2] == "Q" and b.squares[3] == "K"
    assert b.squares[32] == "q" and b.squares[33] == "k"


# --- perft ------------------------------------------------------------------


def test_perft_values_stable() -> None:
    b = Board()
    # No external oracle for this variant; assert the computed values are the
    # stable, reproducible numbers this generator produces.
    assert b.perft(1) == 10
    assert b.perft(2) == 100
    assert b.perft(3) == 1212


def test_perft_cross_check_independent_generator() -> None:
    # The structured generator and the standalone brute-force generator must
    # agree — an independent recount, since there is no external oracle.
    for depth in (1, 2, 3):
        assert Board().perft(depth) == brute_force_perft(Board(), depth)


def test_legal_moves_equal_brute_force_on_random_positions() -> None:
    rng = random.Random(20260717)
    for _ in range(40):
        b = _random_position(rng, plies=rng.randint(0, 12))
        structured = sorted(format_move(m) for m in b.legal_moves())
        brute = sorted(format_move(m) for m in brute_force_legal_moves(b))
        assert structured == brute, b.position_key()


# --- make / unmake round-trip -----------------------------------------------


def test_make_unmake_round_trip_random_playouts() -> None:
    rng = random.Random(11)
    for _ in range(30):
        b = Board()
        _assert_playout_round_trips(b, rng, max_plies=40)


def _assert_playout_round_trips(b: Board, rng: random.Random, max_plies: int) -> None:
    for _ in range(max_plies):
        moves = b.legal_moves()
        if not moves or b.is_game_over():
            break
        move = rng.choice(moves)
        before_squares = list(b.squares)
        before_side = b.side
        before_clock = b.halfmove_clock
        before_full = b.fullmove
        before_key = b.position_key()
        b.push(move)
        b.pop()
        # Exact round-trip after a push/pop.
        assert b.squares == before_squares
        assert b.side == before_side
        assert b.halfmove_clock == before_clock
        assert b.fullmove == before_full
        assert b.position_key() == before_key
        # Advance the game for the next iteration.
        b.push(move)


# --- king-safety invariant --------------------------------------------------


def test_no_legal_move_leaves_own_king_in_check() -> None:
    rng = random.Random(7)
    for _ in range(60):
        b = _random_position(rng, plies=rng.randint(0, 16))
        mover = b.side
        for move in b.legal_moves():
            b.push(move)
            assert not b.is_in_check(mover), f"{format_move(move)} leaves {mover} king in check"
            b.pop()


# --- mate / stalemate / promotion -------------------------------------------


def test_mate_in_one_detection() -> None:
    # White: Kd1, Qa1. Black: Kd6, and pawns walling the king in on rank 5 so
    # it has no escape. Qa1-a6 delivers back-rank mate along rank 6.
    b = Board.from_piece_map(
        {
            "d1": "K",
            "a1": "Q",
            "d6": "k",
            "c5": "p",
            "d5": "p",
            "e5": "p",
        },
        side=WHITE,
    )
    assert not b.is_checkmate()
    mate = parse_move(b, "a1a6")
    b.push(mate)
    assert b.is_in_check(BLACK)
    assert b.is_checkmate()
    assert not b.is_stalemate()


def test_best_move_finds_the_mate() -> None:
    b = Board.from_piece_map(
        {"d1": "K", "a1": "Q", "d6": "k", "c5": "p", "d5": "p", "e5": "p"},
        side=WHITE,
    )
    chosen = b.best_move(depth=2)
    b.push(chosen)
    assert b.is_checkmate(), f"expected mate, got {format_move(chosen)}"


def test_stalemate_detection() -> None:
    # Black king on f6 (corner), White Qe4 and Kd4. Black is NOT in check but
    # every king move (e6/f5/e5) is covered by the queen -> stalemate.
    b = Board.from_piece_map({"d4": "K", "e4": "Q", "f6": "k"}, side=BLACK)
    assert not b.is_in_check(BLACK)
    assert b.is_stalemate()
    assert not b.is_checkmate()
    assert b.is_draw()


def test_promotion_produces_each_piece() -> None:
    # White pawn on a5 promotes on a6.
    for promo, expected in (("q", "Q"), ("r", "R"), ("n", "N")):
        b = Board.from_piece_map({"d1": "K", "d6": "k", "a5": "P"}, side=WHITE)
        move = parse_move(b, f"a5a6{promo}")
        b.push(move)
        assert b.squares[square_to_coord_index("a6")] == expected
        # Round-trips back to a pawn.
        b.pop()
        assert b.squares[square_to_coord_index("a5")] == "P"


def test_promotion_moves_are_generated() -> None:
    b = Board.from_piece_map({"d1": "K", "d6": "k", "a5": "P"}, side=WHITE)
    promo_targets = {m.promotion for m in b.legal_moves() if m.promotion is not None}
    assert promo_targets == {"Q", "R", "N"}  # no bishop


# --- greedy (depth 1) grabs a hanging piece ---------------------------------


def test_greedy_captures_free_hanging_piece() -> None:
    # White Rook on a1 can capture an undefended Black queen on a5 for free.
    # A one-ply material maximizer must take it.
    b = Board.from_piece_map(
        {"d1": "K", "a1": "R", "a5": "q", "d6": "k", "f6": "p"},
        side=WHITE,
    )
    move = b.best_move(depth=1)
    assert format_move(move) == "a1a5", f"greedy passed up a free queen: {format_move(move)}"


# --- notation: parse + format ------------------------------------------------


def test_format_and_parse_round_trip() -> None:
    b = Board()
    for move in b.legal_moves():
        text = format_move(move)
        assert parse_move(b, text) == move


def test_parse_rejects_illegal_and_bad_promotion() -> None:
    b = Board()
    for bad in ("a1a1", "a2a5", "zz11", "a2a", "a2a4b"):
        try:
            parse_move(b, bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_promotion_uci_string() -> None:
    assert Move(square_to_coord_index("a5"), square_to_coord_index("a6"), "Q").uci() == "a5a6q"


# --- grammar helper ---------------------------------------------------------


def test_grammar_move_list_equals_legal_set() -> None:
    rng = random.Random(99)
    for _ in range(20):
        b = _random_position(rng, plies=rng.randint(0, 14))
        if b.is_game_over() or not b.legal_moves():
            continue
        listed = legal_move_strings(b)
        legal = [format_move(m) for m in b.legal_moves()]
        assert listed == legal
        assert set(listed) == set(legal)


def test_grammar_is_well_formed_alternation() -> None:
    b = Board()
    grammar = grammar_for_position(b)
    assert grammar.startswith("root ::= ")
    body = grammar[len("root ::= ") :]
    alts = [a.strip() for a in body.split("|")]
    moves = legal_move_strings(b)
    assert len(alts) == len(moves)
    for alt, mv in zip(alts, moves):
        # Each alternative is exactly one double-quoted legal move string.
        assert alt == f'"{mv}"'
        assert alt.startswith('"') and alt.endswith('"')


def test_grammar_raises_on_terminal_position() -> None:
    # Checkmated position: no legal moves, so no emittable grammar.
    b = Board.from_piece_map(
        {"d1": "K", "a1": "Q", "d6": "k", "c5": "p", "d5": "p", "e5": "p"},
        side=WHITE,
    )
    b.push(parse_move(b, "a1a6"))
    assert b.is_checkmate()
    try:
        grammar_for_position(b)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a terminal position")


# --- draw detection ---------------------------------------------------------


def test_insufficient_material() -> None:
    assert Board.from_piece_map({"d1": "K", "d6": "k"}).is_insufficient_material()
    assert Board.from_piece_map({"d1": "K", "d6": "k", "e1": "N"}).is_insufficient_material()
    # A rook can force mate -> sufficient.
    assert not Board.from_piece_map({"d1": "K", "d6": "k", "a1": "R"}).is_insufficient_material()
    # A pawn can promote -> sufficient.
    assert not Board.from_piece_map({"d1": "K", "d6": "k", "a2": "P"}).is_insufficient_material()


def test_fifty_move_clock_resets_on_pawn_and_capture() -> None:
    b = Board()
    b.push(parse_move(b, "b1c3"))  # knight move -> clock increments
    assert b.halfmove_clock == 1
    b.push(parse_move(b, "b6c4"))  # black knight move -> clock increments
    assert b.halfmove_clock == 2
    b.push(parse_move(b, "a2a3"))  # pawn move -> clock resets
    assert b.halfmove_clock == 0


def test_fifty_move_draw_flag() -> None:
    b = Board.from_piece_map({"d1": "K", "d6": "k", "a1": "R"}, side=WHITE)
    b.halfmove_clock = 100
    assert b.is_fifty_move_draw()
    assert b.is_draw()


def test_threefold_repetition() -> None:
    # Shuffle both kings back and forth to repeat the start position twice more.
    b = Board.from_piece_map({"d1": "K", "d6": "k", "a1": "R"}, side=WHITE)
    start_key = b.position_key()
    assert b._rep_counts[start_key] == 1
    cycle = ["d1e1", "d6e6", "e1d1", "e6d6"]
    for _ in range(2):
        for mv in cycle:
            b.push(parse_move(b, mv))
    # Back at the start position for the third time.
    assert b.position_key() == start_key
    assert b._rep_counts[start_key] == 3
    assert b.is_threefold_repetition()
    assert b.is_draw()


# --- determinism ------------------------------------------------------------


def test_best_move_is_deterministic() -> None:
    b = Board()
    first = format_move(b.best_move(depth=2))
    for _ in range(5):
        assert format_move(Board().best_move(depth=2)) == first


# --- helpers ----------------------------------------------------------------


def square_to_coord_index(coord: str) -> int:
    return coord_to_square(coord)


def _random_position(rng: random.Random, plies: int) -> Board:
    """Play `plies` random legal moves from the start; stop early if terminal."""
    b = Board()
    for _ in range(plies):
        moves = b.legal_moves()
        if not moves or b.is_game_over():
            break
        b.push(rng.choice(moves))
    return b
