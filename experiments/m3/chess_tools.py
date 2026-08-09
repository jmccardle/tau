"""Deterministic board-analysis primitives for the M3 *agentic* chess experiment.

Reference: docs/M3-DESIGN.md §4.3 (the agent plays the variant) — but where the
original driver made the bare LLM internally represent the board, this module is
the substrate for giving a Tau agent *tools* that compute board facts reliably.
The smoke probe (this workstream) showed the raw model is threat-blind — it
missed a check on its own king — so board perception must be a solved substrate,
not something the model is tested on. These functions are the ground-truth engine
queries the agent calls instead of guessing.

Design line (load-bearing for attribution): every function here returns a *fact*
— what is legal, what attacks what, what the material eval is — never a
*decision*. There is deliberately NO best-move / lookahead oracle here; if the
engine chose the move, the induced strategy memory would be invisible. `static_eval`
is the one judgment-adjacent primitive and is intended to be an ABLATION axis
(off / move-phase / review-phase-only), not an always-on crutch.

Everything is pure over `los_alamos.Board` and returns JSON-able values (coords as
strings), so the same functions unit-test offline and wrap directly as agent tools.

Fail-Early (repo rule): a bad coordinate, an empty square where a piece is
required, or a malformed FEN RAISES — no silent defaults.
"""

from __future__ import annotations

from los_alamos import (
    BLACK,
    BOARD_SIZE,
    PIECE_VALUE,
    WHITE,
    Board,
    coord_to_square,
    file_of,
    rank_of,
    square,
    square_to_coord,
)

# --- Variant FEN (6x6, no bishops) ------------------------------------------
#
# Standard FEN generalises cleanly: ranks are listed from the top (rank 6) down
# to rank 1, '/'-separated, each rank a run of piece letters and empty-run
# digits; then side to move. The variant has no castling or en-passant, so those
# fields are always '-'. Files a-f, ranks 1-6.


def board_to_fen(board: Board) -> str:
    """Serialise a position to variant FEN, e.g. the start position is
    ``rnqknr/pppppp/6/6/PPPPPP/RNQKNR w - - 0 1``."""
    rows: list[str] = []
    for r in range(BOARD_SIZE - 1, -1, -1):  # rank 6 (index 5) down to rank 1
        row = ""
        empties = 0
        for f in range(BOARD_SIZE):
            p = board.squares[square(f, r)]
            if p == ".":
                empties += 1
                continue
            if empties:
                row += str(empties)
                empties = 0
            row += p
        if empties:
            row += str(empties)
        rows.append(row)
    placement = "/".join(rows)
    return f"{placement} {board.side} - - {board.halfmove_clock} {board.fullmove}"


def board_from_fen(fen: str) -> Board:
    """Parse variant FEN back into a Board. Fail-Early on any malformation."""
    parts = fen.split()
    if len(parts) < 2:
        raise ValueError(f"FEN needs at least placement and side: {fen!r}")
    placement, side = parts[0], parts[1]
    if side not in (WHITE, BLACK):
        raise ValueError(f"bad side to move in FEN: {side!r}")
    rank_groups = placement.split("/")
    if len(rank_groups) != BOARD_SIZE:
        raise ValueError(f"FEN placement needs {BOARD_SIZE} ranks, got {len(rank_groups)}")

    board = Board(empty=True)
    for i, group in enumerate(rank_groups):
        r = BOARD_SIZE - 1 - i  # first group is the top rank (index 5)
        f = 0
        for ch in group:
            if ch.isdigit():
                f += int(ch)
            else:
                if ch.upper() not in PIECE_VALUE:
                    raise ValueError(f"unknown piece {ch!r} in FEN rank {group!r}")
                if f >= BOARD_SIZE:
                    raise ValueError(f"FEN rank {group!r} overflows {BOARD_SIZE} files")
                board.squares[square(f, r)] = ch
                f += 1
        if f != BOARD_SIZE:
            raise ValueError(f"FEN rank {group!r} covers {f} files, need {BOARD_SIZE}")

    board.side = side
    if len(parts) >= 5 and parts[4].isdigit():
        board.halfmove_clock = int(parts[4])
    if len(parts) >= 6 and parts[5].isdigit():
        board.fullmove = int(parts[5])
    board._rep_counts = {board.position_key(): 1}
    return board


# --- Attacker enumeration (the engine only exposes a boolean) ----------------
#
# `Board.is_square_attacked` returns whether ANY piece of a colour hits a square;
# the tools need the *list* of attackers/defenders. This mirrors that method's
# exact geometry (pawn direction, knight L, king adjacency, orthogonal rook/queen,
# diagonal queen-only) but collects source squares instead of returning early.

_KNIGHT_DELTAS = ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2))
_KING_DELTAS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
_ROOK_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIAG_DIRS = ((1, 1), (-1, 1), (1, -1), (-1, -1))


def attackers_of(board: Board, sq: int, by_white: bool) -> list[int]:
    """Every square holding a piece of the given colour that attacks `sq`."""
    f, r = file_of(sq), rank_of(sq)
    sqs = board.squares
    out: list[int] = []

    # Pawn: a white pawn attacks diagonally upward, so `sq` is hit by a white
    # pawn one rank below diagonally (and mirror for black).
    pawn_char = "P" if by_white else "p"
    pawn_rank = r - 1 if by_white else r + 1
    if 0 <= pawn_rank < BOARD_SIZE:
        for df in (-1, 1):
            nf = f + df
            if 0 <= nf < BOARD_SIZE and sqs[square(nf, pawn_rank)] == pawn_char:
                out.append(square(nf, pawn_rank))

    knight_char = "N" if by_white else "n"
    for df, dr in _KNIGHT_DELTAS:
        nf, nr = f + df, r + dr
        if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE and sqs[square(nf, nr)] == knight_char:
            out.append(square(nf, nr))

    king_char = "K" if by_white else "k"
    for df, dr in _KING_DELTAS:
        nf, nr = f + df, r + dr
        if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE and sqs[square(nf, nr)] == king_char:
            out.append(square(nf, nr))

    rook_char = "R" if by_white else "r"
    queen_char = "Q" if by_white else "q"
    for df, dr in _ROOK_DIRS:
        nf, nr = f + df, r + dr
        while 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
            cell = sqs[square(nf, nr)]
            if cell != ".":
                if cell in (rook_char, queen_char):
                    out.append(square(nf, nr))
                break
            nf += df
            nr += dr

    for df, dr in _DIAG_DIRS:
        nf, nr = f + df, r + dr
        while 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
            cell = sqs[square(nf, nr)]
            if cell != ".":
                if cell == queen_char:
                    out.append(square(nf, nr))
                break
            nf += df
            nr += dr

    return out


# --- Facts tools (JSON-able; coords as strings) ------------------------------


def _is_white_piece(piece: str) -> bool:
    return piece.isupper()


def list_legal_moves(board: Board) -> list[str]:
    """Every legal move for the side to move, in coordinate notation."""
    return sorted(m.uci() for m in board.legal_moves())


def check_status(board: Board) -> dict:
    """Whether the side to move is in check, and which enemy pieces give it."""
    king_sq = board.king_square(board.side)
    in_check = board.is_square_attacked(king_sq, by_white=(board.side == BLACK))
    givers = attackers_of(board, king_sq, by_white=(board.side == BLACK)) if in_check else []
    return {
        "side_to_move": board.side,
        "king_square": square_to_coord(king_sq),
        "in_check": in_check,
        "checked_by": [square_to_coord(s) for s in givers],
    }


def attacks_from(board: Board, coord: str) -> list[str]:
    """Squares the piece on `coord` attacks (geometry; any colour, any side)."""
    src = coord_to_square(coord)
    piece = board.squares[src]
    if piece == ".":
        raise ValueError(f"no piece on {coord}")
    white = _is_white_piece(piece)
    return sorted(
        square_to_coord(t)
        for t in range(BOARD_SIZE * BOARD_SIZE)
        if t != src and src in attackers_of(board, t, by_white=white)
    )


def piece_info(board: Board, coord: str) -> dict:
    """Full tactical picture of one piece: where it can go, what it attacks,
    which of its moves are blocked ONLY by leaving its own king in check, and who
    is attacking/defending its square. Fail-Early on an empty square."""
    src = coord_to_square(coord)
    piece = board.squares[src]
    if piece == ".":
        raise ValueError(f"no piece on {coord}")
    white = _is_white_piece(piece)
    color = WHITE if white else BLACK

    info: dict = {
        "coord": coord,
        "piece": piece,
        "color": color,
        "attacks": attacks_from(board, coord),
        "attacked_by": [square_to_coord(s) for s in attackers_of(board, src, by_white=not white)],
        "defended_by": [
            square_to_coord(s) for s in attackers_of(board, src, by_white=white) if s != src
        ],
    }
    # Move targets only make sense for the side to move; report the self-check
    # split (reachable-but-illegal) that the user specifically asked for.
    if board.side == color:
        pseudo = {m.to_sq for m in board._pseudo_legal_moves() if m.from_sq == src}
        legal = {m.to_sq for m in board.legal_moves() if m.from_sq == src}
        info["legal_targets"] = sorted(square_to_coord(t) for t in legal)
        info["blocked_by_self_check"] = sorted(square_to_coord(t) for t in (pseudo - legal))
    else:
        info["legal_targets"] = None  # not this piece's turn
        info["blocked_by_self_check"] = None
    return info


def attackers_defenders(board: Board, coord: str) -> dict:
    """Who attacks and who defends the square at `coord` (colour-agnostic view)."""
    sq = coord_to_square(coord)
    return {
        "coord": coord,
        "occupant": board.squares[sq] if board.squares[sq] != "." else None,
        "white_attackers": [square_to_coord(s) for s in attackers_of(board, sq, by_white=True)],
        "black_attackers": [square_to_coord(s) for s in attackers_of(board, sq, by_white=False)],
    }


def static_eval(board: Board) -> dict:
    """The engine's White-positive static eval + a raw material tally.

    ABLATION axis, not a default tool: exposing this risks the eval doing the
    strategic work the induced memory is meant to do, so it is gated per phase.
    """
    material = {"white": 0.0, "black": 0.0}
    for p in board.squares:
        if p == ".":
            continue
        val = PIECE_VALUE[p.upper()]
        if _is_white_piece(p):
            material["white"] += val
        else:
            material["black"] += val
    return {
        "eval_white_positive": board.evaluate(),
        "material": material,
        "material_margin_white": material["white"] - material["black"],
    }
