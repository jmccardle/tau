"""Los Alamos chess engine — the M3 Arm A mechanism-proof apparatus.

Reference: docs/M3-DESIGN.md §4.1 (the game), §4.2 (the static eval / signal),
§4.3 (grammar-constrained legal moves), §4.4 (the opponent ladder).

This is the "clean-room mechanism proof" for E-Strategy induction: a 6x6 chess
variant with a dense, externally-verifiable signal. Its correctness is
load-bearing, so it is exercised by test_los_alamos.py rather than trusted.

THE GAME (§4.1, authoritative):
    6x6 board, files a-f, ranks 1-6. Back rank per side is R N Q K N R
    (a=R b=N c=Q d=K e=N f=R, queen on c-file and king on d-file for BOTH
    colors), second rank six pawns. White on ranks 1-2, Black on ranks 5-6.

    NO bishops. The queen keeps full orthogonal+diagonal movement; only the
    bishop *pieces* are absent. Rook orthogonal, knight the L, king one square.
    No castling. Pawns move forward exactly ONE square (no two-square first
    move), capture diagonally forward, NO en passant, and promote on the last
    rank (rank 6 for White, rank 1 for Black) to Q/R/N (no bishop).

    Check / checkmate / stalemate as in standard chess. Draws: stalemate,
    insufficient material, 50-move rule, and threefold repetition are all
    implemented.

REPRESENTATION:
    The board is a flat list of 36 single-character cells. White pieces are
    uppercase (P N R Q K), Black lowercase, '.' is empty. Square index is
    `rank * 6 + file`, file/rank both 0-based (a1 = 0, f6 = 35). Side to move
    is 'w' or 'b'. push()/pop() form an exact-round-trip make/unmake pair.

No external dependencies beyond the stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Board geometry ---------------------------------------------------------

FILES = "abcdef"
RANKS = "123456"
BOARD_SIZE = 6
NUM_SQUARES = BOARD_SIZE * BOARD_SIZE

WHITE = "w"
BLACK = "b"

# The four central squares (§4.2 center-control term): c3, c4, d3, d4.
CENTER_SQUARES = (
    2 * BOARD_SIZE + 2,  # c3
    3 * BOARD_SIZE + 2,  # c4
    2 * BOARD_SIZE + 3,  # d3
    3 * BOARD_SIZE + 3,  # d4
)

# --- Evaluation constants (§4.2) --------------------------------------------

PIECE_VALUE = {"P": 1.0, "N": 3.0, "R": 5.0, "Q": 9.0, "K": 0.0}
# King is not scored as material; terminal outcomes are handled by mate/
# stalemate detection, so no large king constant is needed.

MOBILITY_LAMBDA = 0.1  # λ, the mobility-difference weight (§4.2).
CENTER_BONUS = 0.25  # per-piece bonus for occupying a central square.

MATE_SCORE = 1_000_000.0  # magnitude of a checkmate score in negamax.

PROMOTION_PIECES = ("Q", "R", "N")  # no bishop in this variant.


def square(file: int, rank: int) -> int:
    """File/rank (both 0-based) -> square index."""
    return rank * BOARD_SIZE + file


def file_of(sq: int) -> int:
    return sq % BOARD_SIZE


def rank_of(sq: int) -> int:
    return sq // BOARD_SIZE


def square_to_coord(sq: int) -> str:
    """Square index -> coordinate string, e.g. 0 -> 'a1'."""
    return f"{FILES[file_of(sq)]}{RANKS[rank_of(sq)]}"


def coord_to_square(coord: str) -> int:
    """Coordinate string -> square index, e.g. 'a1' -> 0."""
    if len(coord) != 2 or coord[0] not in FILES or coord[1] not in RANKS:
        raise ValueError(f"bad coordinate: {coord!r}")
    return square(FILES.index(coord[0]), RANKS.index(coord[1]))


def _is_white_piece(piece: str) -> bool:
    return piece.isupper()


# --- Move / undo records ----------------------------------------------------


@dataclass(frozen=True)
class Move:
    """A move in this variant.

    `promotion` is an uppercase piece letter ('Q'/'R'/'N') for a promoting pawn
    move, else None. Colour is applied at push() time. There is no en-passant
    or castling flag because the variant has neither.
    """

    from_sq: int
    to_sq: int
    promotion: str | None = None

    def uci(self) -> str:
        """Coordinate notation: 'a2a3', or 'a5a6q' for a promotion."""
        base = f"{square_to_coord(self.from_sq)}{square_to_coord(self.to_sq)}"
        if self.promotion is not None:
            return base + self.promotion.lower()
        return base


@dataclass
class _Undo:
    """Everything needed to exactly reverse a push()."""

    move: Move
    moved_piece: str
    captured: str
    prev_halfmove_clock: int
    prev_fullmove: int


# --- Piece movement offset tables (structured generator) --------------------

_KNIGHT_DELTAS = ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2))
_KING_DELTAS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
_ROOK_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIAG_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_QUEEN_DIRS = _ROOK_DIRS + _DIAG_DIRS


class Board:
    """A Los Alamos chess position with make/unmake and legal move generation."""

    __slots__ = (
        "squares",
        "side",
        "halfmove_clock",
        "fullmove",
        "_undo_stack",
        "_rep_counts",
    )

    def __init__(self, *, empty: bool = False) -> None:
        self.squares: list[str] = ["."] * NUM_SQUARES
        self.side: str = WHITE
        self.halfmove_clock: int = 0
        self.fullmove: int = 1
        self._undo_stack: list[_Undo] = []
        # Repetition counts keyed by position key (placement + side to move).
        self._rep_counts: dict[str, int] = {}
        if not empty:
            self._setup_initial()
        self._rep_counts[self.position_key()] = 1

    # -- Setup ---------------------------------------------------------------

    def _setup_initial(self) -> None:
        back = "RNQKNR"
        for f in range(BOARD_SIZE):
            self.squares[square(f, 0)] = back[f]  # White back rank (rank 1)
            self.squares[square(f, 1)] = "P"  # White pawns (rank 2)
            self.squares[square(f, 4)] = "p"  # Black pawns (rank 5)
            self.squares[square(f, 5)] = back[f].lower()  # Black back rank (6)

    @classmethod
    def from_piece_map(cls, pieces: dict[str, str], side: str = WHITE) -> Board:
        """Build a position from {coord: piece_char}, e.g. {'a1': 'K'}.

        Used to hand-construct test positions. Fail-early: an unknown piece
        letter or a bad coordinate raises rather than being silently dropped.
        """
        board = cls(empty=True)
        for coord, piece in pieces.items():
            if piece.upper() not in PIECE_VALUE:
                raise ValueError(f"unknown piece: {piece!r}")
            board.squares[coord_to_square(coord)] = piece
        if side not in (WHITE, BLACK):
            raise ValueError(f"bad side: {side!r}")
        board.side = side
        board._rep_counts = {board.position_key(): 1}
        return board

    # -- Position identity ---------------------------------------------------

    def position_key(self) -> str:
        """Placement + side to move. Basis for threefold repetition.

        The variant has no castling rights or en-passant square, so placement
        plus side to move fully identifies a repeatable position.
        """
        return "".join(self.squares) + self.side

    def copy(self) -> Board:
        clone = Board(empty=True)
        clone.squares = list(self.squares)
        clone.side = self.side
        clone.halfmove_clock = self.halfmove_clock
        clone.fullmove = self.fullmove
        clone._rep_counts = dict(self._rep_counts)
        # Undo stack is deliberately not copied: a clone starts a fresh history.
        return clone

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Board):
            return NotImplemented
        return (
            self.squares == other.squares
            and self.side == other.side
            and self.halfmove_clock == other.halfmove_clock
            and self.fullmove == other.fullmove
        )

    # -- King / attack queries ----------------------------------------------

    def king_square(self, color: str) -> int:
        target = "K" if color == WHITE else "k"
        for sq in range(NUM_SQUARES):
            if self.squares[sq] == target:
                return sq
        raise ValueError(f"no {color} king on the board")

    def is_square_attacked(self, sq: int, by_white: bool) -> bool:
        """Is `sq` attacked by any piece of the given colour?"""
        f, r = file_of(sq), rank_of(sq)
        sqs = self.squares

        # Pawn attacks. A white pawn attacks the two squares diagonally *above*
        # it, so `sq` is hit by a white pawn sitting one rank below diagonally.
        pawn_char = "P" if by_white else "p"
        pawn_rank = r - 1 if by_white else r + 1
        if 0 <= pawn_rank < BOARD_SIZE:
            for df in (-1, 1):
                nf = f + df
                if 0 <= nf < BOARD_SIZE and sqs[square(nf, pawn_rank)] == pawn_char:
                    return True

        # Knight.
        knight_char = "N" if by_white else "n"
        for df, dr in _KNIGHT_DELTAS:
            nf, nr = f + df, r + dr
            if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                if sqs[square(nf, nr)] == knight_char:
                    return True

        # King (adjacency).
        king_char = "K" if by_white else "k"
        for df, dr in _KING_DELTAS:
            nf, nr = f + df, r + dr
            if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                if sqs[square(nf, nr)] == king_char:
                    return True

        # Sliding orthogonal: enemy rook or queen.
        rook_char = "R" if by_white else "r"
        queen_char = "Q" if by_white else "q"
        for df, dr in _ROOK_DIRS:
            nf, nr = f + df, r + dr
            while 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                cell = sqs[square(nf, nr)]
                if cell != ".":
                    if cell == rook_char or cell == queen_char:
                        return True
                    break
                nf += df
                nr += dr

        # Sliding diagonal: only the queen (no bishop in this variant).
        for df, dr in _DIAG_DIRS:
            nf, nr = f + df, r + dr
            while 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                cell = sqs[square(nf, nr)]
                if cell != ".":
                    if cell == queen_char:
                        return True
                    break
                nf += df
                nr += dr

        return False

    def is_in_check(self, color: str) -> bool:
        return self.is_square_attacked(self.king_square(color), by_white=(color == BLACK))

    # -- Pseudo-legal move generation (structured generator) -----------------

    def _pseudo_legal_moves(self) -> list[Move]:
        moves: list[Move] = []
        white_to_move = self.side == WHITE
        for sq in range(NUM_SQUARES):
            piece = self.squares[sq]
            if piece == "." or _is_white_piece(piece) != white_to_move:
                continue
            kind = piece.upper()
            if kind == "P":
                self._gen_pawn(sq, white_to_move, moves)
            elif kind == "N":
                self._gen_stepper(sq, white_to_move, _KNIGHT_DELTAS, moves)
            elif kind == "K":
                self._gen_stepper(sq, white_to_move, _KING_DELTAS, moves)
            elif kind == "R":
                self._gen_slider(sq, white_to_move, _ROOK_DIRS, moves)
            elif kind == "Q":
                self._gen_slider(sq, white_to_move, _QUEEN_DIRS, moves)
        return moves

    def _gen_pawn(self, sq: int, white: bool, moves: list[Move]) -> None:
        f, r = file_of(sq), rank_of(sq)
        direction = 1 if white else -1
        last_rank = BOARD_SIZE - 1 if white else 0

        # Single forward push (no two-square first move in this variant).
        nr = r + direction
        if 0 <= nr < BOARD_SIZE and self.squares[square(f, nr)] == ".":
            self._add_pawn_move(sq, square(f, nr), nr == last_rank, moves)

        # Diagonal captures.
        for df in (-1, 1):
            nf = f + df
            if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                target = square(nf, nr)
                cell = self.squares[target]
                if cell != "." and _is_white_piece(cell) != white:
                    self._add_pawn_move(sq, target, nr == last_rank, moves)

    @staticmethod
    def _add_pawn_move(frm: int, to: int, is_promotion: bool, moves: list[Move]) -> None:
        if is_promotion:
            for promo in PROMOTION_PIECES:
                moves.append(Move(frm, to, promo))
        else:
            moves.append(Move(frm, to))

    def _gen_stepper(
        self, sq: int, white: bool, deltas: tuple[tuple[int, int], ...], moves: list[Move]
    ) -> None:
        f, r = file_of(sq), rank_of(sq)
        for df, dr in deltas:
            nf, nr = f + df, r + dr
            if 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                cell = self.squares[square(nf, nr)]
                if cell == "." or _is_white_piece(cell) != white:
                    moves.append(Move(sq, square(nf, nr)))

    def _gen_slider(
        self, sq: int, white: bool, dirs: tuple[tuple[int, int], ...], moves: list[Move]
    ) -> None:
        f, r = file_of(sq), rank_of(sq)
        for df, dr in dirs:
            nf, nr = f + df, r + dr
            while 0 <= nf < BOARD_SIZE and 0 <= nr < BOARD_SIZE:
                cell = self.squares[square(nf, nr)]
                if cell == ".":
                    moves.append(Move(sq, square(nf, nr)))
                else:
                    if _is_white_piece(cell) != white:
                        moves.append(Move(sq, square(nf, nr)))
                    break
                nf += df
                nr += dr

    # -- Legal move generation ----------------------------------------------

    def legal_moves(self) -> list[Move]:
        """Pseudo-legal moves filtered so the mover never leaves its king in
        check. Obviously-correct-by-construction: each candidate is actually
        made and the king's safety tested, then unmade (§ task requirement)."""
        mover = self.side
        legal: list[Move] = []
        for move in self._pseudo_legal_moves():
            self.push(move)
            if not self.is_in_check(mover):
                legal.append(move)
            self.pop()
        return legal

    # -- Make / unmake -------------------------------------------------------

    def push(self, move: Move) -> None:
        moved = self.squares[move.from_sq]
        captured = self.squares[move.to_sq]

        self._undo_stack.append(
            _Undo(
                move=move,
                moved_piece=moved,
                captured=captured,
                prev_halfmove_clock=self.halfmove_clock,
                prev_fullmove=self.fullmove,
            )
        )

        # 50-move clock: reset on a pawn move or a capture, else increment.
        if moved.upper() == "P" or captured != ".":
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        self.squares[move.from_sq] = "."
        if move.promotion is not None:
            promo = move.promotion if moved.isupper() else move.promotion.lower()
            self.squares[move.to_sq] = promo
        else:
            self.squares[move.to_sq] = moved

        if self.side == BLACK:
            self.fullmove += 1
        self.side = BLACK if self.side == WHITE else WHITE

        key = self.position_key()
        self._rep_counts[key] = self._rep_counts.get(key, 0) + 1

    def pop(self) -> None:
        key = self.position_key()
        count = self._rep_counts.get(key, 0)
        if count <= 1:
            self._rep_counts.pop(key, None)
        else:
            self._rep_counts[key] = count - 1

        undo = self._undo_stack.pop()
        self.side = BLACK if self.side == WHITE else WHITE
        self.fullmove = undo.prev_fullmove
        self.halfmove_clock = undo.prev_halfmove_clock
        self.squares[undo.move.from_sq] = undo.moved_piece
        self.squares[undo.move.to_sq] = undo.captured

    # -- Terminal / draw detection ------------------------------------------

    def is_checkmate(self) -> bool:
        return self.is_in_check(self.side) and not self.legal_moves()

    def is_stalemate(self) -> bool:
        return not self.is_in_check(self.side) and not self.legal_moves()

    def is_insufficient_material(self) -> bool:
        """Draw by insufficient material: only kings, or K vs K+single knight.

        Pawns, rooks and queens can force mate; a lone knight cannot. This is
        the conservative standard-chess set restricted to the pieces that exist
        here (no bishops).
        """
        non_king: list[str] = [p for p in self.squares if p != "." and p.upper() != "K"]
        if not non_king:
            return True
        if len(non_king) == 1 and non_king[0].upper() == "N":
            return True
        return False

    def is_fifty_move_draw(self) -> bool:
        # 50 full moves = 100 half-moves without a pawn move or capture.
        return self.halfmove_clock >= 100

    def is_threefold_repetition(self) -> bool:
        return self._rep_counts.get(self.position_key(), 0) >= 3

    def is_draw(self) -> bool:
        return (
            self.is_stalemate()
            or self.is_insufficient_material()
            or self.is_fifty_move_draw()
            or self.is_threefold_repetition()
        )

    def is_game_over(self) -> bool:
        return self.is_checkmate() or self.is_draw()

    # -- Evaluation (§4.2) ---------------------------------------------------

    def _count_mobility(self, color: str) -> int:
        """Legal-move count for `color` at this position (mobility term)."""
        if self.side == color:
            return len(self.legal_moves())
        self.side = color
        try:
            return len(self.legal_moves())
        finally:
            self.side = BLACK if color == WHITE else WHITE

    def evaluate(self) -> float:
        """Static eval in a White-positive convention (§4.2).

        eval = (material_white - material_black)
             + λ · (mobility_white - mobility_black)
             + CENTER_BONUS · (center_white - center_black)

        Material: P1 N3 R5 Q9; king unscored. Mobility is the legal-move-count
        difference. Center is per-piece occupancy of {c3,c4,d3,d4}. A positive
        score favours White regardless of whose turn it is.
        """
        material = 0.0
        center = 0.0
        for sq in range(NUM_SQUARES):
            piece = self.squares[sq]
            if piece == ".":
                continue
            value = PIECE_VALUE[piece.upper()]
            if _is_white_piece(piece):
                material += value
            else:
                material -= value
        for sq in CENTER_SQUARES:
            piece = self.squares[sq]
            if piece == ".":
                continue
            center += 1.0 if _is_white_piece(piece) else -1.0

        mobility = self._count_mobility(WHITE) - self._count_mobility(BLACK)
        return material + MOBILITY_LAMBDA * mobility + CENTER_BONUS * center

    def _evaluate_side_to_move(self) -> float:
        """Eval from the perspective of the side to move (negamax convention)."""
        score = self.evaluate()
        return score if self.side == WHITE else -score

    # -- Opponent ladder: negamax + alpha-beta (§4.4) ------------------------

    def _negamax(self, depth: int, alpha: float, beta: float) -> float:
        if depth == 0:
            return self._evaluate_side_to_move()
        moves = self.legal_moves()
        if not moves:
            # Checkmate (bad for side to move) or stalemate (draw).
            if self.is_in_check(self.side):
                return -MATE_SCORE - depth  # deeper mate slightly less awful
            return 0.0
        best = -MATE_SCORE * 2
        for move in moves:
            self.push(move)
            score = -self._negamax(depth - 1, -beta, -alpha)
            self.pop()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    def best_move(self, depth: int) -> Move:
        """The opponent ladder's move at the given search depth.

        depth 1 is the GREEDY floor (a one-ply material+eval maximizer, §4.4);
        depths 2-4 are alpha-beta minimax on the same eval. Deterministic
        tie-break: legal moves are generated in a fixed square/offset order and
        the FIRST move attaining the best score wins (strict '>'), so the
        measuring stick is reproducible (§4.4 requires this).
        """
        if depth < 1:
            raise ValueError("search depth must be >= 1")
        moves = self.legal_moves()
        if not moves:
            raise ValueError("no legal moves: position is terminal")
        best_move = moves[0]
        best_score = -MATE_SCORE * 4
        for move in moves:
            self.push(move)
            # Full window at the root keeps the choice independent of ordering
            # pruning, so the deterministic tie-break is purely first-best-wins.
            score = -self._negamax(depth - 1, -MATE_SCORE * 4, MATE_SCORE * 4)
            self.pop()
            if score > best_score:
                best_score = score
                best_move = move
        return best_move

    # -- perft ---------------------------------------------------------------

    def perft(self, depth: int) -> int:
        """Count leaf nodes at the given depth via the structured generator."""
        if depth == 0:
            return 1
        if depth == 1:
            return len(self.legal_moves())
        total = 0
        for move in self.legal_moves():
            self.push(move)
            total += self.perft(depth - 1)
            self.pop()
        return total


# --- Move notation (§4.3): parser + formatter -------------------------------


def format_move(move: Move) -> str:
    """Move -> coordinate string, e.g. 'a2a3' or 'a5a6q'."""
    return move.uci()


def parse_move(board: Board, text: str) -> Move:
    """Coordinate string -> the matching *legal* Move on `board`.

    Fail-early: a malformed string, a promotion to a non-existent piece
    (including bishop 'b'), or a move that is not legal in the position raises
    ValueError. There is no illegal-move fallback (§4.3).
    """
    text = text.strip()
    if len(text) not in (4, 5):
        raise ValueError(f"bad move string: {text!r}")
    from_sq = coord_to_square(text[0:2])
    to_sq = coord_to_square(text[2:4])
    promotion: str | None = None
    if len(text) == 5:
        promo = text[4].upper()
        if promo not in PROMOTION_PIECES:
            raise ValueError(f"bad promotion piece: {text[4]!r} (allowed: q/r/n)")
        promotion = promo
    candidate = Move(from_sq, to_sq, promotion)
    for move in board.legal_moves():
        if move == candidate:
            return move
    raise ValueError(f"illegal move for this position: {text!r}")


# --- Grammar helper (§4.3): legal moves + GBNF ------------------------------


def legal_move_strings(board: Board) -> list[str]:
    """The position's legal moves as coordinate strings, in generation order."""
    return [format_move(m) for m in board.legal_moves()]


def grammar_for_position(board: Board) -> str:
    """A per-turn GBNF grammar binding the model to exactly the legal moves.

    Returns a rule of the form::

        root ::= "a2a3" | "a2b3" | ...

    one quoted alternative per legal move, in generation order. Fail-early: a
    terminal position (no legal moves) has no emittable move, so this raises
    rather than returning an empty/degenerate grammar (§4.3 — no illegal-move
    fallback to invent).
    """
    strings = legal_move_strings(board)
    if not strings:
        raise ValueError("no legal moves: cannot build a move grammar")
    alternatives = " | ".join(f'"{s}"' for s in strings)
    return f"root ::= {alternatives}"


# --- Independent brute-force generator (for cross-checking perft in tests) ---


def _can_reach_pseudo(board: Board, frm: int, to: int) -> bool:
    """Geometry-only: could the piece on `frm` pseudo-move to `to`?

    Deliberately written independently of Board's offset tables (it reasons in
    file/rank deltas from scratch) so the test suite can cross-check the
    structured generator against an unrelated recount.
    """
    piece = board.squares[frm]
    if piece == "." or frm == to:
        return False
    white = _is_white_piece(piece)
    dest = board.squares[to]
    if dest != "." and _is_white_piece(dest) == white:
        return False  # cannot capture own piece

    df = file_of(to) - file_of(frm)
    dr = rank_of(to) - rank_of(frm)
    adf, adr = abs(df), abs(dr)
    kind = piece.upper()

    if kind == "N":
        return (adf, adr) in ((1, 2), (2, 1))
    if kind == "K":
        return max(adf, adr) == 1
    if kind == "P":
        direction = 1 if white else -1
        if df == 0:  # push
            return dr == direction and dest == "."
        if adf == 1 and dr == direction:  # capture
            return dest != "."
        return False
    if kind == "R":
        if df != 0 and dr != 0:
            return False
        return _clear_path(board, frm, to, df, dr)
    if kind == "Q":
        if not (df == 0 or dr == 0 or adf == adr):
            return False
        return _clear_path(board, frm, to, df, dr)
    return False


def _clear_path(board: Board, frm: int, to: int, df: int, dr: int) -> bool:
    """Are all squares strictly between frm and to empty?"""
    step_f = (df > 0) - (df < 0)
    step_r = (dr > 0) - (dr < 0)
    f, r = file_of(frm) + step_f, rank_of(frm) + step_r
    while square(f, r) != to:
        if board.squares[square(f, r)] != ".":
            return False
        f += step_f
        r += step_r
    return True


def brute_force_legal_moves(board: Board) -> list[Move]:
    """Every legal move, found by trying all from/to pairs (independent recount).

    For each ordered pair of squares it asks the standalone geometry predicate
    whether the move is pseudo-legal, expands promotions, then confirms legality
    by actually making the move and testing king safety. Used only by tests to
    cross-check Board.legal_moves() and perft.
    """
    white_to_move = board.side == WHITE
    mover = board.side
    result: list[Move] = []
    for frm in range(NUM_SQUARES):
        piece = board.squares[frm]
        if piece == "." or _is_white_piece(piece) != white_to_move:
            continue
        for to in range(NUM_SQUARES):
            if not _can_reach_pseudo(board, frm, to):
                continue
            is_pawn = piece.upper() == "P"
            last_rank = (BOARD_SIZE - 1) if white_to_move else 0
            if is_pawn and rank_of(to) == last_rank:
                candidates = [Move(frm, to, p) for p in PROMOTION_PIECES]
            else:
                candidates = [Move(frm, to)]
            for move in candidates:
                board.push(move)
                if not board.is_in_check(mover):
                    result.append(move)
                board.pop()
    return result


def brute_force_perft(board: Board, depth: int) -> int:
    """perft computed with the independent brute-force generator."""
    if depth == 0:
        return 1
    total = 0
    for move in brute_force_legal_moves(board):
        board.push(move)
        total += brute_force_perft(board, depth - 1)
        board.pop()
    return total


# --- Self-play helper (curriculum trajectories, §4.4) -----------------------


@dataclass
class GameResult:
    """Outcome of a played game."""

    winner: str | None  # WHITE, BLACK, or None for a draw.
    reason: str
    moves: list[str] = field(default_factory=list)
